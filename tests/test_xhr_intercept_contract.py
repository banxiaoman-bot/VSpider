"""Regression tests for XHR-INTERCEPT-CONTRACT-1.

The XHR/Fetch intercept track used to hard-code xlsx
(``save_intercepted_data`` -> ``_save_dataframe_to_excel``), violating the
output_contract iron rule ("data_manager must read output_contract.container,
never blind-default to xlsx"). These tests lock in:

* no contract            -> legacy xlsx behaviour (backwards compatible)
* container=jsonl/csv    -> append + dedup in that container, suffix rewritten
* non-dataset container  -> falls back to jsonl (rows stay rows, no fake xlsx)
* kind-only contract     -> resolved through the kind->container policy
* browser_env wiring     -> contract stored, late refresh keeps dedup state
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pandas as pd
import pytest

from visual_web_agent import data_manager


@pytest.fixture()
def out_dir(monkeypatch: pytest.MonkeyPatch):
    root = Path(__file__).resolve().parent / ".tmp_xhr_contract_tests"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(data_manager, "resolve_output_path", lambda fn: path / Path(fn).name)
    monkeypatch.setattr(data_manager, "register_artifact", lambda *a, **k: None)
    try:
        yield path
    finally:
        shutil.rmtree(root, ignore_errors=True)


ROWS = [
    {"id": 1, "name": "Alice"},
    {"id": 2, "name": "Bob"},
]


class TestContainerResolution:
    def test_no_contract_keeps_legacy_xlsx(self, out_dir: Path) -> None:
        saved = data_manager.save_intercepted_data(ROWS, filename="xhr_run.xlsx")
        assert saved.endswith(".xlsx")
        assert Path(saved).exists()

    def test_jsonl_contract_writes_jsonl_with_suffix_rewrite(self, out_dir: Path) -> None:
        saved = data_manager.save_intercepted_data(
            ROWS,
            filename="xhr_run.xlsx",
            output_contract={"container": "jsonl", "output_kind": "dataset_records"},
        )
        assert saved.endswith("xhr_run.jsonl")
        lines = Path(saved).read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["name"] == "Alice"
        assert "_extracted_at" in first

    def test_csv_contract_writes_csv(self, out_dir: Path) -> None:
        saved = data_manager.save_intercepted_data(
            ROWS,
            filename="xhr_run.xlsx",
            output_contract={"container": "csv"},
        )
        assert saved.endswith("xhr_run.csv")
        df = pd.read_csv(saved)
        assert list(df["name"]) == ["Alice", "Bob"]

    def test_non_dataset_container_falls_back_to_jsonl(self, out_dir: Path) -> None:
        saved = data_manager.save_intercepted_data(
            ROWS,
            filename="xhr_run.xlsx",
            output_contract={"container": "files_folder", "output_kind": "media_image"},
        )
        assert saved.endswith(".jsonl")

    def test_kind_only_contract_resolves_via_policy(self, out_dir: Path) -> None:
        # dataset_records -> jsonl per the sanctioned kind->container mapping
        saved = data_manager.save_intercepted_data(
            ROWS,
            filename="xhr_run.xlsx",
            output_contract={"output_kind": "dataset_records"},
        )
        assert saved.endswith(".jsonl")


class TestAppendAndDedup:
    def test_jsonl_appends_and_dedupes_by_unique_key(self, out_dir: Path) -> None:
        contract = {"container": "jsonl"}
        data_manager.save_intercepted_data(
            ROWS, filename="feed.xlsx", unique_key="id", output_contract=contract
        )
        saved = data_manager.save_intercepted_data(
            [{"id": 2, "name": "Bob-updated"}, {"id": 3, "name": "Carol"}],
            filename="feed.xlsx",
            unique_key="id",
            output_contract=contract,
        )
        lines = [json.loads(l) for l in Path(saved).read_text(encoding="utf-8").strip().splitlines()]
        by_id = {row["id"]: row["name"] for row in lines}
        assert by_id == {1: "Alice", 2: "Bob-updated", 3: "Carol"}

    def test_xlsx_append_path_unchanged(self, out_dir: Path) -> None:
        data_manager.save_intercepted_data(ROWS, filename="legacy.xlsx", unique_key="id")
        saved = data_manager.save_intercepted_data(
            [{"id": 3, "name": "Carol"}], filename="legacy.xlsx", unique_key="id"
        )
        df = pd.read_excel(saved, engine="openpyxl")
        assert len(df) == 3


class TestResolveInterceptContainer:
    def test_empty_contract_is_xlsx(self) -> None:
        assert data_manager._resolve_intercept_container(None) == "xlsx"
        assert data_manager._resolve_intercept_container({}) == "xlsx"

    def test_dataset_containers_pass_through(self) -> None:
        for c in ("xlsx", "csv", "jsonl"):
            assert data_manager._resolve_intercept_container({"container": c}) == c

    def test_unknown_container_string_falls_back_to_jsonl(self) -> None:
        assert data_manager._resolve_intercept_container({"container": "zip"}) == "jsonl"


class TestBrowserEnvWiring:
    def test_configure_and_refresh_paths_wired(self) -> None:
        root = Path(__file__).resolve().parent.parent
        src = (root / "visual_web_agent" / "browser_env.py").read_text(encoding="utf-8")
        assert "self._intercept_output_contract: dict | None = None" in src
        assert "def set_interceptor_output_contract" in src
        # both intercept tracks pass the contract through to the saver
        assert src.count("output_contract=self._intercept_output_contract") == 2

    def test_main_wires_initial_and_goal_contracts(self) -> None:
        root = Path(__file__).resolve().parent.parent
        src = (root / "visual_web_agent" / "main.py").read_text(encoding="utf-8")
        assert "output_contract=_initial_output_contract or None" in src
        assert "browser.set_interceptor_output_contract(_goal_output_contract or None)" in src

    def test_refresh_does_not_reset_dedup_state(self) -> None:
        """set_interceptor_output_contract must not clear seen-row keys the
        way configure_interceptor does (late contract refresh mid-run)."""
        from visual_web_agent.browser_env import BrowserEnv

        env = BrowserEnv.__new__(BrowserEnv)
        env._intercept_seen_row_keys = {"hash:abc"}
        env._intercept_output_contract = None
        BrowserEnv.set_interceptor_output_contract(env, {"container": "jsonl"})
        assert env._intercept_output_contract == {"container": "jsonl"}
        assert env._intercept_seen_row_keys == {"hash:abc"}

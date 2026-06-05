"""Tests for ``visual_web_agent.io_contract.persistence``.

Uses ``tmp_path`` to keep IO hermetic; no test writes under the real
``runs/`` directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from visual_web_agent.io_contract import (
    INPUT_CONTRACT_FILENAME,
    OUTPUT_CONTRACT_FILENAME,
    MANIFEST_FILENAME,
    ARTIFACTS_DIRNAME,
    append_manifest_item,
    build_input_contract,
    ensure_contract_skeleton,
    ensure_input_contract_skeleton,
    infer_output_contract,
    read_input_contract,
    read_manifest,
    read_output_contract,
    run_dir,
    write_input_contract,
    write_manifest,
    write_output_contract,
    new_manifest,
    append_item,
)


class TestRunDir:
    def test_creates_run_dir_and_artifacts(self, tmp_path: Path) -> None:
        d = run_dir("run_1", base_dir=tmp_path)
        assert d.exists()
        assert (d / ARTIFACTS_DIRNAME).exists()

    def test_invalid_run_id_raises(self, tmp_path: Path) -> None:
        # run_id is a single path component; "." / ".." must never traverse,
        # and path separators / dotted ids are rejected outright.
        for bad in ("../escape", "", "..", ".", "a.b", "x/y", "..\\x"):
            with pytest.raises(ValueError):
                run_dir(bad, base_dir=tmp_path)


class TestInputContractIO:
    def test_write_and_read_round_trip(self, tmp_path: Path) -> None:
        c = build_input_contract(goal="hello", target_url="https://a.com")
        write_input_contract("run_a", c, base_dir=tmp_path)
        path = tmp_path / "run_a" / INPUT_CONTRACT_FILENAME
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["goal"] == "hello"
        assert payload["urls"][0]["url"] == "https://a.com"

        read = read_input_contract("run_a", base_dir=tmp_path)
        assert read is not None
        assert read["version"] == "input_contract.v1"

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        assert read_input_contract("nonexistent", base_dir=tmp_path) is None

    def test_secrets_masked_on_disk_but_not_in_object(self, tmp_path: Path) -> None:
        c = build_input_contract(
            goal="hello",
            target_url="https://a.com",
            vlm_options={"api_key": "vlm-secret", "semantic_api_key": "sem-secret"},
            constraints={"proxy_server": "http://p:3128", "proxy_password": "proxy-secret"},
        )
        write_input_contract("run_sec", c, base_dir=tmp_path)
        raw = (tmp_path / "run_sec" / INPUT_CONTRACT_FILENAME).read_text(encoding="utf-8")
        assert "vlm-secret" not in raw
        assert "sem-secret" not in raw
        assert "proxy-secret" not in raw
        assert "***" in raw
        assert "http://p:3128" in raw  # non-secret survives
        # the in-memory object still holds the real secrets
        assert c.model_overrides.vlm["api_key"] == "vlm-secret"
        assert c.constraints.proxy_password == "proxy-secret"

    def test_skeleton_also_masks_secrets(self, tmp_path: Path) -> None:
        ensure_input_contract_skeleton(
            "run_sk",
            goal="g",
            target_url="https://a.com",
            vlm_options={"api_key": "k-secret"},
            constraints={"proxy_password": "p-secret"},
            base_dir=tmp_path,
        )
        raw = (tmp_path / "run_sk" / INPUT_CONTRACT_FILENAME).read_text(encoding="utf-8")
        assert "k-secret" not in raw
        assert "p-secret" not in raw


class TestOutputContractIO:
    def test_write_and_read_round_trip(self, tmp_path: Path) -> None:
        c = infer_output_contract("帮我下载页面上所有 PDF 报告")
        write_output_contract("run_b", c, base_dir=tmp_path)
        path = tmp_path / "run_b" / OUTPUT_CONTRACT_FILENAME
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["output_kind"] == "media_pdf"
        assert payload["container"] == "files_folder"

        read = read_output_contract("run_b", base_dir=tmp_path)
        assert read is not None
        assert read["version"] == "output_contract.v1"


class TestManifestIO:
    def test_read_returns_fresh_manifest_when_missing(self, tmp_path: Path) -> None:
        m = read_manifest("run_c", base_dir=tmp_path)
        assert m.run_id == "run_c"
        assert m.items == []

    def test_ensure_contract_skeleton_creates_files(self, tmp_path: Path) -> None:
        output_path, manifest_path = ensure_contract_skeleton("run_skel", base_dir=tmp_path)
        assert output_path.exists()
        assert manifest_path.exists()
        assert output_path.name == OUTPUT_CONTRACT_FILENAME
        assert manifest_path.name == MANIFEST_FILENAME

    def test_append_helper_persists(self, tmp_path: Path) -> None:
        append_manifest_item(
            "run_c",
            kind="media_image",
            path="runs/run_c/artifacts/abc.png",
            size=10,
            sha256="aaa",
            source_url="https://example.com/abc.png",
            base_dir=tmp_path,
        )
        m = read_manifest("run_c", base_dir=tmp_path)
        assert len(m.items) == 1
        assert m.items[0].sha256 == "aaa"

    def test_append_dedup_persists(self, tmp_path: Path) -> None:
        append_manifest_item(
            "run_c", kind="media_image", path="p1", sha256="x",
            source_url="https://a.com", base_dir=tmp_path,
        )
        append_manifest_item(
            "run_c", kind="media_image", path="p1", sha256="x",
            source_url="https://b.com", base_dir=tmp_path,
        )
        m = read_manifest("run_c", base_dir=tmp_path)
        assert len(m.items) == 1
        assert "https://a.com" in m.items[0].source_url
        assert "https://b.com" in m.items[0].source_url

    def test_write_then_read(self, tmp_path: Path) -> None:
        m = new_manifest("run_d")
        append_item(m, kind="dataset_rows", path="p1", sha256="d1")
        write_manifest("run_d", m, base_dir=tmp_path)
        m2 = read_manifest("run_d", base_dir=tmp_path)
        assert m2.run_id == "run_d"
        assert len(m2.items) == 1
        assert m2.items[0].kind == "dataset_rows"


class TestEnsureInputContractSkeleton:
    """``ensure_input_contract_skeleton`` guarantees every run drops an
    ``input_contract.json`` built from the goal, without clobbering a richer
    one already persisted by the dispatch layer."""

    def test_writes_when_absent(self, tmp_path: Path) -> None:
        path = ensure_input_contract_skeleton(
            "run_in", goal="抓取列表页", target_url="https://a.com",
            base_dir=tmp_path,
        )
        assert path.exists()
        assert path.name == INPUT_CONTRACT_FILENAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["goal"] == "抓取列表页"
        assert payload["urls"][0]["url"] == "https://a.com"
        assert payload["version"] == "input_contract.v1"

    def test_does_not_clobber_existing(self, tmp_path: Path) -> None:
        rich = build_input_contract(goal="rich goal", target_url="https://x.com")
        write_input_contract("run_in2", rich, base_dir=tmp_path)
        ensure_input_contract_skeleton(
            "run_in2", goal="poor goal", base_dir=tmp_path,
        )
        read = read_input_contract("run_in2", base_dir=tmp_path)
        assert read is not None
        assert read["goal"] == "rich goal"
        assert read["urls"][0]["url"] == "https://x.com"

    def test_captures_file_attachment(self, tmp_path: Path) -> None:
        f = tmp_path / "notes.txt"
        f.write_text("hello", encoding="utf-8")
        ensure_input_contract_skeleton(
            "run_in3", goal="读取附件", file_path=str(f), base_dir=tmp_path,
        )
        read = read_input_contract("run_in3", base_dir=tmp_path)
        assert read is not None
        assert read["attachments"]
        assert read["attachments"][0]["filename"] == "notes.txt"

"""Tests for ``io_contract.runtime`` + the auto-manifest hook in
``data_manager.save_to_excel``.

These cover the OUT-3.2 backfill path: legacy ``save_to_excel`` callers
that do not pass ``run_id`` should still end up with a manifest entry
when the agent loop has called ``set_current_run``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Pure runtime module
# ---------------------------------------------------------------------------

class TestRunContextSlot:
    def setup_method(self) -> None:
        from visual_web_agent.io_contract import clear_current_run

        clear_current_run()

    def teardown_method(self) -> None:
        from visual_web_agent.io_contract import clear_current_run

        clear_current_run()

    def test_default_state_is_empty(self) -> None:
        from visual_web_agent.io_contract import current_run_context, current_run_id

        assert current_run_id() == ""
        ctx = current_run_context()
        assert ctx["run_id"] == ""
        assert ctx["base_dir"] is None

    def test_set_then_get(self, tmp_path: Path) -> None:
        from visual_web_agent.io_contract import (
            current_run_id,
            current_base_dir,
            set_current_run,
        )

        set_current_run("run_42", base_dir=tmp_path)
        assert current_run_id() == "run_42"
        assert Path(str(current_base_dir())) == tmp_path

    def test_invalid_id_silently_clears(self) -> None:
        from visual_web_agent.io_contract import (
            current_run_id,
            set_current_run,
        )

        set_current_run("run_ok")
        set_current_run("../escape")
        assert current_run_id() == ""

        set_current_run("run_ok")
        set_current_run("")
        assert current_run_id() == ""

    def test_clear_is_idempotent(self) -> None:
        from visual_web_agent.io_contract import (
            clear_current_run,
            current_run_id,
            set_current_run,
        )

        set_current_run("run_99")
        clear_current_run()
        clear_current_run()
        assert current_run_id() == ""

    def test_context_is_task_local(self) -> None:
        from visual_web_agent.io_contract import current_run_id, set_current_run

        async def worker(run_id: str) -> str:
            set_current_run(run_id)
            await asyncio.sleep(0)
            return current_run_id()

        async def main() -> list[str]:
            return await asyncio.gather(worker("run_a"), worker("run_b"))

        assert asyncio.run(main()) == ["run_a", "run_b"]


# ---------------------------------------------------------------------------
# data_manager.save_to_excel: when the run slot is set, it must drop a
# manifest entry into runs/<run_id>/manifest.json -- without changing the
# legacy ``str path`` return value.
# ---------------------------------------------------------------------------

class TestSaveToExcelManifestHook:
    def setup_method(self) -> None:
        from visual_web_agent.io_contract import clear_current_run

        clear_current_run()

    def teardown_method(self) -> None:
        from visual_web_agent.io_contract import clear_current_run

        clear_current_run()

    def _ensure_pandas(self) -> None:
        pytest.importorskip("pandas")
        pytest.importorskip("openpyxl")

    def test_writes_manifest_entry_when_run_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._ensure_pandas()
        from visual_web_agent import data_manager as dm
        from visual_web_agent.io_contract import (
            ensure_contract_skeleton,
            set_current_run,
            MANIFEST_FILENAME,
        )

        run_id = "run_writer_smoke"
        ensure_contract_skeleton(run_id, base_dir=tmp_path)
        set_current_run(run_id, base_dir=tmp_path)

        out = dm.save_to_excel(
            [{"title": "X", "url": "https://x/1"}, {"title": "Y", "url": "https://x/2"}],
            "smoke.xlsx",
        )
        assert isinstance(out, str)
        assert Path(out).exists()
        norm_out = out.replace("\\", "/")
        assert f"{run_id}/artifacts/" in norm_out

        manifest_path = tmp_path / run_id / MANIFEST_FILENAME
        assert manifest_path.exists(), (
            "save_to_excel under an active run must write into runs/<id>/manifest.json"
        )
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        items = payload.get("items") or []
        assert items, "manifest should hold at least one dataset_rows item"
        target = next((it for it in items if it.get("kind") == "dataset_rows"), None)
        assert target is not None
        assert target["produced_by"] == "vlm_extract"
        assert target["mime"] == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert target["extra"]["row_count"] == 2
        assert target["extra"]["new_row_count"] == 2
        assert target["size"] > 0
        assert target["sha256"]

    def test_no_manifest_write_when_run_inactive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._ensure_pandas()
        from visual_web_agent import data_manager as dm
        from visual_web_agent.io_contract import MANIFEST_FILENAME

        monkeypatch.setattr(
            dm, "resolve_artifact_path",
            lambda filename, subdir="": tmp_path / Path(filename).name,
            raising=True,
        )

        out = dm.save_to_excel([{"a": 1}], "no_run.xlsx")
        assert Path(out).exists()
        # No run is set, so nothing under tmp_path/<rid>/manifest.json
        # should have appeared. We just check there is no rogue rid dir.
        sibling = list(tmp_path.iterdir())
        # Only the xlsx itself + possibly nothing else
        assert all(p.name == "no_run.xlsx" or p.is_file() for p in sibling)
        # Defensive: no manifest file under any plausible run subdir
        for child in tmp_path.iterdir():
            if child.is_dir():
                assert not (child / MANIFEST_FILENAME).exists()

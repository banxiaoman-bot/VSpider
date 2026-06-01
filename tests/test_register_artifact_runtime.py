"""register_artifact should append manifest when io_contract runtime is set."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def test_register_artifact_uses_current_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from visual_web_agent import artifact_manager as am
    from visual_web_agent.io_contract import (
        MANIFEST_FILENAME,
        clear_current_run,
        ensure_contract_skeleton,
        set_current_run,
    )

    root = Path(__file__).resolve().parents[1] / "workspace" / f"pytest-artifact-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        monkeypatch.setattr(am, "artifact_root", lambda: root)
        monkeypatch.setattr(
            "api_server.broadcast_new_artifact",
            lambda _p: None,
            raising=False,
        )

        run_id = "run_reg_smoke"
        ensure_contract_skeleton(run_id, base_dir=root)
        set_current_run(run_id, base_dir=root)

        src = root / "hello.txt"
        src.write_text("manifest hook", encoding="utf-8")
        am.register_artifact(src, kind="file_generic", produced_by="test")
        clear_current_run()

        run_artifact = root / run_id / "artifacts" / "hello.txt"
        assert run_artifact.exists(), "active run must copy artifact into runs/<id>/artifacts/"
        manifest_path = root / run_id / MANIFEST_FILENAME
        assert manifest_path.exists()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        items = payload.get("items") or []
        assert items
        assert items[-1]["produced_by"] == "test"
        assert str(items[-1].get("path") or "").replace("\\", "/").endswith(
            f"{run_id}/artifacts/hello.txt"
        )
    finally:
        clear_current_run()
        shutil.rmtree(root, ignore_errors=True)

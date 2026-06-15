from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from visual_web_agent import queue_state


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_queue_state_tests"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_save_and_load_queue_snapshot(local_tmp_path: Path) -> None:
    snapshot = {
        "running": True,
        "worker_running": True,
        "worker_config": {"effective_max_workers": 1},
        "active_worker_count": 1,
        "workers": [{"worker_id": "w1", "status": "running", "task_id": "t1"}],
        "current": {
            "task_id": "t1",
            "status": "running",
            "target_url": "https://example.com",
            "prompt": "collect",
            "stop_event": object(),
            "vlm_options": {"api_key": "secret"},
        },
        "queue_length": 1,
        "queue": [
            {
                "task_id": "t2",
                "status": "queued",
                "target_url": "https://example.org",
                "prompt": "collect queued",
                "file_path": "private.xlsx",
                "vlm_options": {"api_key": "secret"},
                "urls": ["https://extra.example.org"],
                "constraints": {
                    "max_runs": 2,
                    "proxy_password": "secret-pass",
                    "proxy_server": "http://queue-user:queue-pass@proxy.example:3128",
                    "proxy_chain": ["chain-user:chain-pass@proxy2.example:8080"],
                },
                "upload_sha256": "abc123",
                "upload_mime": "text/csv",
            }
        ],
    }

    saved = queue_state.save_snapshot(snapshot, base_dir=local_tmp_path)
    loaded = queue_state.load_snapshot(base_dir=local_tmp_path)

    assert loaded == saved
    assert loaded is not None
    assert loaded["schema_version"] == queue_state.SCHEMA_VERSION
    assert loaded["running"] is True
    assert loaded["worker_running"] is True
    assert loaded["current"]["task_id"] == "t1"
    assert "stop_event" not in loaded["current"]
    assert "vlm_options" not in loaded["current"]
    assert loaded["queue"][0]["task_id"] == "t2"
    assert "file_path" not in loaded["queue"][0]
    assert "vlm_options" not in loaded["queue"][0]
    assert loaded["execution_queue"][0]["task_id"] == "t2"
    assert loaded["execution_queue"][0]["file_path"] == "private.xlsx"
    assert loaded["execution_queue"][0]["urls"] == ["https://extra.example.org"]
    assert loaded["execution_queue"][0]["constraints"]["max_runs"] == 2
    assert loaded["execution_queue"][0]["constraints"]["proxy_password"] == "***"
    assert loaded["execution_queue"][0]["constraints"]["proxy_server"] == "http://proxy.example:3128"
    assert loaded["execution_queue"][0]["constraints"]["proxy_chain"] == ["proxy2.example:8080"]
    assert loaded["execution_queue"][0]["upload_sha256"] == "abc123"
    assert loaded["execution_queue"][0]["upload_mime"] == "text/csv"
    # secret is masked before the snapshot is persisted to disk
    assert loaded["execution_queue"][0]["vlm_options"]["api_key"] == "***"
    raw_state = queue_state.queue_state_path(local_tmp_path).read_text(encoding="utf-8")
    assert "secret-pass" not in raw_state
    assert "queue-user" not in raw_state
    assert "queue-pass" not in raw_state
    assert "chain-user" not in raw_state
    assert "chain-pass" not in raw_state

    public = queue_state.load_public_snapshot(base_dir=local_tmp_path)
    assert public is not None
    assert "execution_queue" not in public
    assert "vlm_options" not in public["queue"][0]
    assert "constraints" not in public["queue"][0]


def test_load_missing_snapshot_returns_none(local_tmp_path: Path) -> None:
    assert queue_state.load_snapshot(base_dir=local_tmp_path) is None


def test_load_corrupt_snapshot_returns_none(local_tmp_path: Path) -> None:
    path = queue_state.queue_state_path(local_tmp_path)
    path.write_text("{bad json", encoding="utf-8")

    assert queue_state.load_snapshot(base_dir=local_tmp_path) is None


def test_public_snapshot_sanitizes_legacy_private_fields() -> None:
    public = queue_state.public_snapshot(
        {
            "current": {"task_id": "running", "status": "running", "vlm_options": {"api_key": "secret"}},
            "queue": [
                {
                    "task_id": "legacy",
                    "status": "queued",
                    "target_url": "https://example.com",
                    "file_path": "private.xlsx",
                    "vlm_options": {"api_key": "secret"},
                }
            ],
            "execution_queue": [{"task_id": "legacy", "vlm_options": {"api_key": "secret"}}],
        }
    )

    assert public is not None
    assert "execution_queue" not in public
    assert public["current"]["task_id"] == "running"
    assert "vlm_options" not in public["current"]
    assert public["queue"][0]["task_id"] == "legacy"
    assert "file_path" not in public["queue"][0]
    assert "vlm_options" not in public["queue"][0]


def test_queue_state_source_wiring() -> None:
    root = Path(__file__).resolve().parent.parent
    api_src = (root / "api_server.py").read_text(encoding="utf-8")
    qc_src = (root / "queue_core.py").read_text(encoding="utf-8")
    state_src = (root / "visual_web_agent" / "queue_state.py").read_text(encoding="utf-8")

    assert "from visual_web_agent import queue_state as _queue_state" in qc_src
    assert "def _persist_queue_snapshot_safe() -> None:" in qc_src
    assert 'snapshot["execution_queue"]' in qc_src
    assert "_queue_state.save_snapshot(snapshot)" in qc_src
    assert "\"persisted\": _queue_state.load_public_snapshot()" in api_src
    assert "def save_snapshot(" in state_src
    assert "def load_snapshot(" in state_src
    assert "def load_public_snapshot(" in state_src
    assert "execution_queue" in state_src
    assert "runs" in state_src and "queue" in state_src and "state.json" in state_src

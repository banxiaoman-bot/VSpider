from __future__ import annotations

import shutil
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visual_web_agent import run_registry


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_run_registry_tests"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_create_load_complete_run(local_tmp_path: Path) -> None:
    rec = run_registry.create_run(
        run_id="run_001",
        target_url="https://example.com",
        prompt="collect everything",
        mode="single",
        auth_profiles="default",
        vlm_model="qwen-vl",
        semantic_model="deepseek-chat",
        vlm_model_type="vl",
        vlm_options={"api_key": "secret", "temperature": 0.1},
        base_dir=local_tmp_path,
    )

    assert rec["run_id"] == "run_001"
    assert rec["status"] == "running"
    assert rec["vlm_options"]["api_key"] == "***"

    loaded = run_registry.load_run("run_001", base_dir=local_tmp_path)
    assert loaded is not None
    assert loaded["paths"]["phase_jsonl"] == "logs/phase_run_001.jsonl"
    assert loaded["paths_exist"]["registry_json"] is False

    done = run_registry.complete_run("run_001", success=True, base_dir=local_tmp_path)
    assert done is not None
    assert done["status"] == "succeeded"
    assert done["finished_at"] is not None
    assert done["duration_s"] is not None


def test_list_runs_sorted_and_status_filtered(local_tmp_path: Path) -> None:
    run_registry.create_run(run_id="a", target_url="https://a.test", prompt="a", base_dir=local_tmp_path)
    run_registry.create_run(run_id="b", target_url="https://b.test", prompt="b", base_dir=local_tmp_path)
    run_registry.complete_run("a", success=False, base_dir=local_tmp_path)
    run_registry.complete_run("b", success=True, base_dir=local_tmp_path)

    all_runs = run_registry.list_runs(limit=10, base_dir=local_tmp_path)
    assert [r["run_id"] for r in all_runs] == ["b", "a"]

    failed = run_registry.list_runs(limit=10, status="failed", base_dir=local_tmp_path)
    assert [r["run_id"] for r in failed] == ["a"]


def test_invalid_run_id_is_rejected(local_tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_registry.create_run(
            run_id="../bad",
            target_url="https://example.com",
            prompt="bad",
            base_dir=local_tmp_path,
        )
    assert run_registry.load_run("../bad", base_dir=local_tmp_path) is None


def test_api_runs_endpoints_use_registry(monkeypatch: pytest.MonkeyPatch, local_tmp_path: Path) -> None:
    import api_server

    monkeypatch.setattr(api_server._run_registry, "registry_root", lambda base_dir=None: local_tmp_path)
    monkeypatch.setattr(api_server._run_registry, "_run_path", lambda run_id, base_dir=None: local_tmp_path / f"{run_id}.json")

    api_server._run_registry.create_run(
        run_id="api_run",
        target_url="https://example.com",
        prompt="api prompt",
        base_dir=local_tmp_path,
    )
    api_server._run_registry.complete_run("api_run", success=True, base_dir=local_tmp_path)

    client = TestClient(api_server.app)
    list_resp = client.get("/api/runs")
    assert list_resp.status_code == 200
    payload = list_resp.json()
    assert payload["status"] == "success"
    assert payload["count"] == 1
    assert payload["runs"][0]["run_id"] == "api_run"

    get_resp = client.get("/api/runs/api_run")
    assert get_resp.status_code == 200
    assert get_resp.json()["run"]["status"] == "succeeded"

    missing = client.get("/api/runs/missing")
    assert missing.status_code == 404


def test_create_task_control_writes_registry(monkeypatch: pytest.MonkeyPatch, local_tmp_path: Path) -> None:
    import api_server

    monkeypatch.setattr(api_server._run_registry, "registry_root", lambda base_dir=None: local_tmp_path)
    monkeypatch.setattr(api_server._run_registry, "_run_path", lambda run_id, base_dir=None: local_tmp_path / f"{run_id}.json")

    with api_server._TASK_LOCK:
        api_server.active_tasks["current_task"] = None

    task_id, stop_event = api_server._create_task_control(
        "https://example.com",
        "collect data",
        "",
        "default",
        "qwen-vl",
        "deepseek-chat",
        False,
        mode="single",
        filename="",
        file_size_kb=0.0,
        vlm_model_type="vl",
        vlm_options={"api_key": "secret", "model": "qwen-vl"},
    )

    assert isinstance(stop_event, threading.Event)
    rec = api_server._run_registry.load_run(task_id, base_dir=local_tmp_path)
    assert rec is not None
    assert rec["run_id"] == task_id
    assert rec["target_url"] == "https://example.com"
    assert rec["prompt"] == "collect data"
    assert rec["vlm_options"]["api_key"] == "***"


def test_stop_marks_registry_stop_requested(monkeypatch: pytest.MonkeyPatch, local_tmp_path: Path) -> None:
    import api_server

    monkeypatch.setattr(api_server._run_registry, "registry_root", lambda base_dir=None: local_tmp_path)
    monkeypatch.setattr(api_server._run_registry, "_run_path", lambda run_id, base_dir=None: local_tmp_path / f"{run_id}.json")

    with api_server._TASK_LOCK:
        api_server.active_tasks["current_task"] = None

    task_id, _ = api_server._create_task_control(
        "https://example.com",
        "collect data",
        "",
        mode="single",
    )
    ok, _msg = api_server.request_stop_current_task()
    assert ok is True

    rec = api_server._run_registry.load_run(task_id, base_dir=local_tmp_path)
    assert rec is not None
    assert rec["stop_requested"] is True

    with api_server._TASK_LOCK:
        api_server.active_tasks["current_task"] = None

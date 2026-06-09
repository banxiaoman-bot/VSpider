from __future__ import annotations

import json
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
        urls=["https://extra.example.com"],
        constraints={
            "max_runs": 2,
            "proxy_password": "proxy-secret",
            "proxy_server": "http://reg-user:reg-pass@proxy.example:3128",
            "proxy_chain": ["chain-user:chain-pass@proxy2.example:8080"],
        },
        upload_sha256="abc123",
        upload_mime="text/csv",
        base_dir=local_tmp_path,
    )

    assert rec["run_id"] == "run_001"
    assert rec["status"] == "running"
    assert rec["vlm_options"]["api_key"] == "***"
    assert rec["urls"] == ["https://extra.example.com"]
    assert rec["constraints"]["max_runs"] == 2
    assert rec["constraints"]["proxy_password"] == "***"
    assert rec["constraints"]["proxy_server"] == "http://proxy.example:3128"
    assert rec["constraints"]["proxy_chain"] == ["proxy2.example:8080"]
    assert rec["upload_sha256"] == "abc123"
    assert rec["upload_mime"] == "text/csv"
    raw = (local_tmp_path / "run_001.json").read_text(encoding="utf-8")
    assert "reg-user" not in raw
    assert "reg-pass" not in raw
    assert "chain-user" not in raw
    assert "chain-pass" not in raw

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
    from visual_web_agent.io_contract import persistence as _persistence

    monkeypatch.setattr(api_server._run_registry, "registry_root", lambda base_dir=None: local_tmp_path)
    monkeypatch.setattr(api_server._run_registry, "_run_path", lambda run_id, base_dir=None: local_tmp_path / f"{run_id}.json")
    runs_root = local_tmp_path / "runs_root"
    monkeypatch.setattr(_persistence, "default_runs_root", lambda: runs_root)

    api_server._run_registry.create_run(
        run_id="api_run",
        target_url="https://example.com",
        prompt="api prompt",
        base_dir=local_tmp_path,
    )
    api_server._run_registry.complete_run("api_run", success=True, base_dir=local_tmp_path)
    run_dir = runs_root / "api_run"
    run_dir.mkdir(parents=True)
    (run_dir / "input_contract.json").write_text(
        json.dumps({"version": "input_contract.v1", "goal": "api prompt"}),
        encoding="utf-8",
    )
    (run_dir / "output_contract.json").write_text(
        json.dumps({"version": "output_contract.v1", "output_kind": "answer_text"}),
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        json.dumps({
            "version": "manifest.v1",
            "run_id": "api_run",
            "items": [
                {"kind": "log", "path": "runs/api_run_url0001/manifest.json",
                 "extra": {"entry_type": "child_run", "child_run_id": "api_run_url0001"}},
                {"kind": "dataset_rows", "path": "runs/api_run/artifacts/data.jsonl"},
            ],
        }),
        encoding="utf-8",
    )

    client = TestClient(api_server.app)
    list_resp = client.get("/api/runs")
    assert list_resp.status_code == 200
    payload = list_resp.json()
    assert payload["status"] == "success"
    assert payload["count"] == 1
    assert payload["runs"][0]["run_id"] == "api_run"

    get_resp = client.get("/api/runs/api_run")
    assert get_resp.status_code == 200
    detail = get_resp.json()
    assert detail["run"]["status"] == "succeeded"
    assert detail["contracts"]["input_contract"]["version"] == "input_contract.v1"
    assert detail["contracts"]["output_contract"]["output_kind"] == "answer_text"
    assert detail["contracts"]["manifest"]["version"] == "manifest.v1"
    assert detail["contracts"]["summary"]["manifest_items"] == 2
    assert detail["contracts"]["summary"]["child_runs"] == 1
    assert detail["contracts"]["paths"]["manifest"] == "runs/api_run/manifest.json"

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


def test_run_agent_helper_creates_missing_direct_registry_record(
    monkeypatch: pytest.MonkeyPatch,
    local_tmp_path: Path,
) -> None:
    from visual_web_agent import main as agent_main

    monkeypatch.setattr(run_registry, "registry_root", lambda base_dir=None: local_tmp_path)
    monkeypatch.setattr(run_registry, "_run_path", lambda run_id, base_dir=None: local_tmp_path / f"{run_id}.json")

    owned = agent_main._ensure_run_registry_record(
        run_id="direct_run",
        start_url="https://example.com",
        goal="collect data",
        auth_profiles="admin",
        vlm_options={"model": "vision", "api_key": "secret", "temperature": 0.2},
        run_constraints={"max_steps": 5},
    )

    assert owned is True
    rec = run_registry.load_run("direct_run", base_dir=local_tmp_path)
    assert rec is not None
    assert rec["mode"] == "direct"
    assert rec["status"] == "running"
    assert rec["target_url"] == "https://example.com"
    assert rec["auth_profiles"] == "admin"
    assert rec["vlm_options"]["model"] == "vision"
    assert rec["vlm_options"]["api_key"] == "***"
    assert rec["constraints"]["max_steps"] == 5

    agent_main._complete_owned_run_registry_record("direct_run", owned=owned, success=True)
    done = run_registry.load_run("direct_run", base_dir=local_tmp_path)
    assert done is not None
    assert done["status"] == "succeeded"

    stopped_owned = agent_main._ensure_run_registry_record(
        run_id="direct_stopped",
        start_url="https://example.com/stop",
        goal="stop me",
    )
    agent_main._complete_owned_run_registry_record(
        "direct_stopped",
        owned=stopped_owned,
        success=False,
        stopped=True,
    )
    stopped = run_registry.load_run("direct_stopped", base_dir=local_tmp_path)
    assert stopped is not None
    assert stopped["status"] == "stopped"


def test_run_agent_helper_does_not_overwrite_existing_registry_record(
    monkeypatch: pytest.MonkeyPatch,
    local_tmp_path: Path,
) -> None:
    from visual_web_agent import main as agent_main

    monkeypatch.setattr(run_registry, "registry_root", lambda base_dir=None: local_tmp_path)
    monkeypatch.setattr(run_registry, "_run_path", lambda run_id, base_dir=None: local_tmp_path / f"{run_id}.json")
    run_registry.create_run(
        run_id="api_owned",
        target_url="https://api.example.com",
        prompt="api prompt",
        mode="single",
        base_dir=local_tmp_path,
    )

    owned = agent_main._ensure_run_registry_record(
        run_id="api_owned",
        start_url="https://direct.example.com",
        goal="direct prompt",
    )

    assert owned is False
    agent_main._complete_owned_run_registry_record("api_owned", owned=owned, success=False)
    rec = run_registry.load_run("api_owned", base_dir=local_tmp_path)
    assert rec is not None
    assert rec["status"] == "running"
    assert rec["target_url"] == "https://api.example.com"
    assert rec["prompt"] == "api prompt"

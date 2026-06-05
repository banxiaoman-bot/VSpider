from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_task_queue_tests"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def api_with_tmp_registry(monkeypatch: pytest.MonkeyPatch, local_tmp_path: Path):
    import api_server

    monkeypatch.setattr(api_server._run_registry, "registry_root", lambda base_dir=None: local_tmp_path)
    monkeypatch.setattr(api_server._run_registry, "_run_path", lambda run_id, base_dir=None: local_tmp_path / f"{run_id}.json")
    monkeypatch.setattr(api_server._queue_state, "queue_root", lambda base_dir=None: local_tmp_path / "queue")
    monkeypatch.setattr(api_server._queue_state, "queue_state_path", lambda base_dir=None: local_tmp_path / "queue" / "state.json")
    with api_server._TASK_LOCK:
        api_server.active_tasks["current_task"] = None
        api_server.active_tasks["queue"] = []
        api_server.active_tasks["queue_worker_running"] = False
        api_server.active_tasks["queue_paused"] = False
        api_server.active_tasks["queue_paused_at"] = None
        api_server.active_tasks["queue_pause_reason"] = ""
        api_server.active_tasks["workers"] = {}
    yield api_server
    with api_server._TASK_LOCK:
        api_server.active_tasks["current_task"] = None
        api_server.active_tasks["queue"] = []
        api_server.active_tasks["queue_worker_running"] = False
        api_server.active_tasks["queue_paused"] = False
        api_server.active_tasks["queue_paused_at"] = None
        api_server.active_tasks["queue_pause_reason"] = ""
        api_server.active_tasks["workers"] = {}


def test_enqueue_task_creates_queued_registry(api_with_tmp_registry, local_tmp_path: Path) -> None:
    api_server = api_with_tmp_registry

    item, should_start = api_server._enqueue_task(
        target_url="https://example.com",
        prompt="collect",
        file_path="",
        mode="single",
        vlm_options={"api_key": "secret"},
    )

    assert should_start is True
    snapshot = api_server._queue_snapshot()
    assert snapshot["queue_length"] == 1
    assert snapshot["queue"][0]["task_id"] == item["task_id"]
    persisted = api_server._queue_state.load_snapshot()
    assert persisted is not None
    assert persisted["queue_length"] == 1
    assert persisted["queue"][0]["task_id"] == item["task_id"]
    assert "execution_queue" in persisted
    assert persisted["execution_queue"][0]["task_id"] == item["task_id"]
    assert persisted["execution_queue"][0]["vlm_options"]["api_key"] == "***"
    assert "vlm_options" not in persisted["queue"][0]
    raw_state = api_server._queue_state.queue_state_path().read_text(encoding="utf-8")
    assert "secret" not in raw_state

    rec = api_server._run_registry.load_run(item["task_id"], base_dir=local_tmp_path)
    assert rec is not None
    assert rec["status"] == "queued"
    assert rec["started_at"] is None
    assert rec["vlm_options"]["api_key"] == "***"


def test_cancel_queued_task_updates_registry(api_with_tmp_registry, local_tmp_path: Path) -> None:
    api_server = api_with_tmp_registry
    item, _ = api_server._enqueue_task(
        target_url="https://example.com",
        prompt="collect",
        file_path="",
    )

    ok, message = api_server.cancel_queued_task(item["task_id"])

    assert ok is True
    assert "取消" in message
    assert api_server._queue_snapshot()["queue_length"] == 0
    persisted = api_server._queue_state.load_snapshot()
    assert persisted is not None
    assert persisted["queue_length"] == 0
    rec = api_server._run_registry.load_run(item["task_id"], base_dir=local_tmp_path)
    assert rec is not None
    assert rec["status"] == "stopped"
    assert rec["error"] == "cancelled before start"


def test_task_queue_api_endpoints(api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    item, _ = api_server._enqueue_task(
        target_url="https://example.com",
        prompt="collect",
        file_path="",
    )

    client = TestClient(api_server.app)
    get_resp = client.get("/api/task_queue")
    assert get_resp.status_code == 200
    assert get_resp.json()["queue"]["queue_length"] == 1
    assert get_resp.json()["persisted"]["queue_length"] == 1
    assert "execution_queue" not in get_resp.json()["persisted"]

    del_resp = client.delete(f"/api/task_queue/{item['task_id']}")
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "success"
    assert del_resp.json()["queue"]["queue_length"] == 0
    assert del_resp.json()["persisted"]["queue_length"] == 0
    assert "execution_queue" not in del_resp.json()["persisted"]


def test_queue_metrics_reports_queue_workers_and_registry(api_with_tmp_registry, local_tmp_path: Path) -> None:
    api_server = api_with_tmp_registry
    api_server._run_registry.create_run(
        run_id="failed_metrics",
        target_url="https://metrics.example.com",
        prompt="failed",
        status="failed",
        base_dir=local_tmp_path,
    )
    api_server._run_registry.create_run(
        run_id="retry_metrics",
        target_url="https://metrics.example.com",
        prompt="retry",
        status="queued",
        base_dir=local_tmp_path,
    )
    api_server._run_registry.update_run(
        "retry_metrics",
        extra={"retry_of": "failed_metrics"},
        base_dir=local_tmp_path,
    )
    item, _ = api_server._enqueue_task(
        target_url="https://queued-metrics.example.com",
        prompt="queued",
        file_path="",
    )
    with api_server._TASK_LOCK:
        api_server.active_tasks["queue"][0]["queued_at"] = 1.0
        api_server.active_tasks["workers"] = {
            "worker_metrics": {
                "worker_id": "worker_metrics",
                "status": "idle",
                "task_id": "",
                "started_at": 1.0,
                "last_seen_at": 1.0,
                "stopped_at": None,
                "error": "",
            }
        }
        api_server.active_tasks["queue_worker_running"] = True

    metrics = api_server.queue_metrics(run_limit=20)

    assert metrics["queue"]["length"] == 1
    assert metrics["queue"]["worker_running"] is True
    assert metrics["workers"]["active_count"] == 1
    assert metrics["workers"]["status_counts"]["idle"] == 1
    assert metrics["timing"]["oldest_queued_age_s"] is not None
    assert metrics["registry"]["status_counts"]["failed"] == 1
    assert metrics["registry"]["retry_count"] == 1
    assert metrics["registry"]["retryable_count"] == 1
    assert api_server._queue_snapshot()["queue"][0]["task_id"] == item["task_id"]


def test_task_queue_metrics_api(api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    api_server._enqueue_task(
        target_url="https://metrics-api.example.com",
        prompt="metrics",
        file_path="",
    )
    client = TestClient(api_server.app)

    resp = client.get("/api/task_queue/metrics", params={"run_limit": 10})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "success"
    assert payload["metrics"]["queue"]["length"] == 1
    assert payload["metrics"]["registry"]["sample_size"] >= 1


class _FakeBackgroundTasks:
    def __init__(self) -> None:
        self.calls = []

    def add_task(self, func, *args, **kwargs) -> None:
        self.calls.append((func, args, kwargs))


def test_start_batch_queues_when_current_task_is_running(api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    api_server._create_task_control(
        "https://running.example.com",
        "running task",
        "",
        task_id="running_task",
    )

    bg = _FakeBackgroundTasks()
    result = asyncio.run(
        api_server.start_batch(
            bg,
            target_url="https://example.com",
            prompt="queued task",
            goal="",
            auth_profiles="",
            vlm_model="",
            semantic_model="",
            vlm_model_type="vl",
            vlm_temperature="",
            vlm_max_tokens="",
            vlm_base_url="",
            vlm_api_key="",
            semantic_base_url="",
            semantic_api_key="",
            file=None,
        )
    )

    assert result["status"] == "success"
    assert result["queued"] is True
    assert result["task_id"] != "running_task"
    assert result["queue"]["queue_length"] == 1
    assert result["queue"]["current"]["task_id"] == "running_task"
    assert result["worker_config"]["effective_max_workers"] == 1
    assert len(result["started_workers"]) == 1
    assert bg.calls and bg.calls[0][0] is api_server._queue_worker
    assert bg.calls[0][1] == (result["started_workers"][0],)


def test_queue_worker_runs_tasks_sequentially(monkeypatch: pytest.MonkeyPatch, api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    executed: list[str] = []

    async def fake_run_batch_task(task_id, target_url, prompt, file_path, stop_event, auth_profiles="", vlm_options=None):
        executed.append(task_id)
        with api_server._TASK_LOCK:
            task = api_server.active_tasks.get("current_task")
            if task and task.get("task_id") == task_id:
                task["status"] = "completed"
                task["running"] = False
                task["finished_at"] = 1.0

    monkeypatch.setattr(api_server, "_run_batch_task", fake_run_batch_task)
    item1, _ = api_server._enqueue_task(target_url="https://a.example.com", prompt="a", file_path="")
    item2, _ = api_server._enqueue_task(target_url="https://b.example.com", prompt="b", file_path="")

    asyncio.run(api_server._queue_worker())

    assert executed == [item1["task_id"], item2["task_id"]]
    snapshot = api_server._queue_snapshot()
    assert snapshot["queue_length"] == 0
    assert snapshot["worker_running"] is False
    assert snapshot["active_worker_count"] == 0
    assert snapshot["workers"][0]["status"] == "stopped"
    assert snapshot["current"]["task_id"] == item2["task_id"]
    persisted = api_server._queue_state.load_snapshot()
    assert persisted is not None
    assert persisted["queue_length"] == 0
    assert persisted["workers"][0]["status"] == "stopped"


def test_pause_and_resume_task_queue(api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    api_server._enqueue_task(target_url="https://pause.example.com", prompt="pause", file_path="")

    paused = api_server.pause_task_queue("maintenance")

    assert paused["queue_paused"] is True
    assert paused["queue_pause_reason"] == "maintenance"
    persisted = api_server._queue_state.load_public_snapshot()
    assert persisted is not None
    assert persisted["queue_paused"] is True
    assert persisted["queue_pause_reason"] == "maintenance"

    resumed, should_start = api_server.resume_task_queue()

    assert resumed["queue_paused"] is False
    assert resumed["queue_pause_reason"] == ""
    assert should_start is True


def test_queue_worker_does_not_dequeue_when_paused(api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    item, _ = api_server._enqueue_task(target_url="https://paused.example.com", prompt="paused", file_path="")
    api_server.pause_task_queue("hold")

    async def run_paused_worker_briefly() -> None:
        task = asyncio.create_task(api_server._queue_worker("worker_pause_test"))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_paused_worker_briefly())

    snapshot = api_server._queue_snapshot()
    assert snapshot["queue_paused"] is True
    assert snapshot["queue_length"] == 1
    assert snapshot["queue"][0]["task_id"] == item["task_id"]
    assert snapshot["workers"][0]["status"] == "paused"


def test_task_queue_pause_resume_api(api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    api_server._enqueue_task(target_url="https://api-pause.example.com", prompt="pause", file_path="")
    client = TestClient(api_server.app)

    pause_resp = client.post("/api/task_queue/pause", params={"reason": "maintenance"})
    assert pause_resp.status_code == 200
    pause_payload = pause_resp.json()
    assert pause_payload["status"] == "success"
    assert pause_payload["queue"]["queue_paused"] is True
    assert pause_payload["persisted"]["queue_paused"] is True
    with api_server._TASK_LOCK:
        api_server.active_tasks["workers"] = {
            "worker_resume_test": {
                "worker_id": "worker_resume_test",
                "status": "paused",
                "task_id": "",
                "started_at": 1.0,
                "last_seen_at": 1.0,
                "stopped_at": None,
                "error": "",
            }
        }

    resume_resp = client.post("/api/task_queue/resume")
    assert resume_resp.status_code == 200
    resume_payload = resume_resp.json()
    assert resume_payload["status"] == "success"
    assert resume_payload["queue"]["queue_paused"] is False
    assert resume_payload["persisted"]["queue_paused"] is False


def test_retry_run_as_queued_task_creates_new_queued_run(api_with_tmp_registry, local_tmp_path: Path) -> None:
    api_server = api_with_tmp_registry
    source = api_server._run_registry.create_run(
        run_id="failed_source",
        target_url="https://retry.example.com",
        prompt="retry this",
        status="failed",
        vlm_options={"api_key": "secret", "temperature": 0.2},
        base_dir=local_tmp_path,
    )

    ok, message, retry = api_server.retry_run_as_queued_task(source["run_id"])

    assert ok is True
    assert message == "retry task queued"
    assert retry["source_run_id"] == "failed_source"
    assert retry["source_status"] == "failed"
    assert retry["task_id"] != "failed_source"
    snapshot = api_server._queue_snapshot()
    assert snapshot["queue_length"] == 1
    assert snapshot["queue"][0]["task_id"] == retry["task_id"]
    retried = api_server._run_registry.load_run(retry["task_id"], base_dir=local_tmp_path)
    assert retried is not None
    assert retried["status"] == "queued"
    assert retried["retry_of"] == "failed_source"
    assert retried["retry_source_status"] == "failed"
    assert retried["vlm_options"]["temperature"] == 0.2
    assert "api_key" not in retried["vlm_options"]
    original = api_server._run_registry.load_run("failed_source", base_dir=local_tmp_path)
    assert original is not None
    assert original["status"] == "failed"


def test_retry_run_rejects_non_retryable_status(api_with_tmp_registry, local_tmp_path: Path) -> None:
    api_server = api_with_tmp_registry
    api_server._run_registry.create_run(
        run_id="succeeded_source",
        target_url="https://retry.example.com",
        prompt="do not retry",
        status="succeeded",
        base_dir=local_tmp_path,
    )

    ok, message, retry = api_server.retry_run_as_queued_task("succeeded_source")

    assert ok is False
    assert "not retryable" in message
    assert retry["source"]["status"] == "succeeded"
    assert api_server._queue_snapshot()["queue_length"] == 0


def test_retry_run_api(api_with_tmp_registry, local_tmp_path: Path) -> None:
    api_server = api_with_tmp_registry
    api_server._run_registry.create_run(
        run_id="error_source",
        target_url="https://retry-api.example.com",
        prompt="retry api",
        status="error",
        base_dir=local_tmp_path,
    )
    with api_server._TASK_LOCK:
        api_server.active_tasks["workers"] = {
            "worker_retry_test": {
                "worker_id": "worker_retry_test",
                "status": "idle",
                "task_id": "",
                "started_at": 1.0,
                "last_seen_at": 1.0,
                "stopped_at": None,
                "error": "",
            }
        }
        api_server.active_tasks["queue_worker_running"] = True
    client = TestClient(api_server.app)

    resp = client.post("/api/runs/error_source/retry")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "success"
    assert payload["retry"]["source_run_id"] == "error_source"
    assert payload["retry"]["source_status"] == "error"
    assert payload["queue"]["queue_length"] == 1
    assert payload["started_workers"] == []
    retried = api_server._run_registry.load_run(payload["retry"]["task_id"], base_dir=local_tmp_path)
    assert retried is not None
    assert retried["retry_of"] == "error_source"
    original = api_server._run_registry.load_run("error_source", base_dir=local_tmp_path)
    assert original is not None
    assert original["status"] == "error"


def test_queue_worker_config_has_safe_default(monkeypatch: pytest.MonkeyPatch, api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    monkeypatch.setenv("VSPIDER_QUEUE_MAX_WORKERS", "4")
    monkeypatch.delenv("VSPIDER_EXPERIMENTAL_PARALLEL_RUNS", raising=False)

    config = api_server._queue_worker_config()

    assert config["requested_max_workers"] == 4
    assert config["effective_max_workers"] == 1
    assert config["parallel_enabled"] is False
    assert config["safety_cap_active"] is True


def test_queue_worker_config_allows_experimental_parallel(monkeypatch: pytest.MonkeyPatch, api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    monkeypatch.setenv("VSPIDER_QUEUE_MAX_WORKERS", "3")
    monkeypatch.setenv("VSPIDER_EXPERIMENTAL_PARALLEL_RUNS", "true")

    config = api_server._queue_worker_config()

    assert config["requested_max_workers"] == 3
    assert config["effective_max_workers"] == 3
    assert config["parallel_enabled"] is True
    assert config["safety_cap_active"] is False


def test_start_queue_workers_uses_effective_worker_count(monkeypatch: pytest.MonkeyPatch, api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    monkeypatch.setenv("VSPIDER_QUEUE_MAX_WORKERS", "3")
    monkeypatch.setenv("VSPIDER_EXPERIMENTAL_PARALLEL_RUNS", "true")
    bg = _FakeBackgroundTasks()

    started, config = api_server._start_queue_workers(bg)

    assert len(started) == 3
    assert config["effective_max_workers"] == 3
    assert len(bg.calls) == 3
    assert all(call[0] is api_server._queue_worker for call in bg.calls)
    assert api_server._queue_snapshot()["active_worker_count"] == 3


def test_queue_heartbeat_config_and_stale_detection(monkeypatch: pytest.MonkeyPatch, api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    monkeypatch.setenv("VSPIDER_QUEUE_WORKER_HEARTBEAT_INTERVAL", "0.05")
    monkeypatch.setenv("VSPIDER_QUEUE_WORKER_STALE_SECONDS", "0.1")
    with api_server._TASK_LOCK:
        api_server.active_tasks["workers"] = {
            "worker_old": {
                "worker_id": "worker_old",
                "status": "running",
                "task_id": "task_old",
                "started_at": 1.0,
                "last_seen_at": 1.0,
                "stopped_at": None,
                "error": "",
            }
        }
        api_server.active_tasks["queue_worker_running"] = True

    snapshot = api_server._queue_snapshot()

    assert snapshot["heartbeat_config"]["interval_s"] == 0.05
    assert snapshot["heartbeat_config"]["stale_after_s"] == 0.1
    assert snapshot["stale_worker_count"] == 1
    assert snapshot["workers"][0]["stale"] is True
    assert snapshot["workers"][0]["heartbeat_age_s"] > 0.1


def test_queue_worker_heartbeat_updates_last_seen(monkeypatch: pytest.MonkeyPatch, api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    monkeypatch.setenv("VSPIDER_QUEUE_WORKER_HEARTBEAT_INTERVAL", "0.01")
    with api_server._TASK_LOCK:
        api_server.active_tasks["workers"] = {
            "worker_hb": {
                "worker_id": "worker_hb",
                "status": "running",
                "task_id": "task_hb",
                "started_at": 1.0,
                "last_seen_at": 1.0,
                "stopped_at": None,
                "error": "",
            }
        }

    async def run_heartbeat_once() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(api_server._queue_worker_heartbeat("worker_hb", stop))
        await asyncio.sleep(0.03)
        stop.set()
        await task

    asyncio.run(run_heartbeat_once())

    with api_server._TASK_LOCK:
        last_seen = api_server.active_tasks["workers"]["worker_hb"]["last_seen_at"]
    assert last_seen > 1.0
    persisted = api_server._queue_state.load_snapshot()
    assert persisted is not None
    assert persisted["workers"][0]["worker_id"] == "worker_hb"


def test_queue_watchdog_marks_stale_worker_and_task(monkeypatch: pytest.MonkeyPatch, api_with_tmp_registry, local_tmp_path: Path) -> None:
    api_server = api_with_tmp_registry
    monkeypatch.setenv("VSPIDER_QUEUE_WORKER_STALE_SECONDS", "0.1")
    api_server._run_registry.create_run(
        run_id="stale_task",
        target_url="https://stale.example.com",
        prompt="stale",
        status="running",
        base_dir=local_tmp_path,
    )
    with api_server._TASK_LOCK:
        api_server.active_tasks["current_task"] = {
            "task_id": "stale_task",
            "target_url": "https://stale.example.com",
            "prompt": "stale",
            "status": "running",
            "running": True,
            "started_at": 1.0,
        }
        api_server.active_tasks["workers"] = {
            "worker_stale": {
                "worker_id": "worker_stale",
                "status": "running",
                "task_id": "stale_task",
                "started_at": 1.0,
                "last_seen_at": 1.0,
                "stopped_at": None,
                "error": "",
            }
        }
        api_server.active_tasks["queue_worker_running"] = True

    result = api_server.scan_stale_queue_workers()

    assert result["stale_worker_count"] == 1
    assert result["affected_task_ids"] == ["stale_task"]
    snapshot = api_server._queue_snapshot()
    assert snapshot["active_worker_count"] == 0
    assert snapshot["workers"][0]["status"] == "error"
    assert snapshot["current"]["status"] == "error"
    rec = api_server._run_registry.load_run("stale_task", base_dir=local_tmp_path)
    assert rec is not None
    assert rec["status"] == "error"
    assert rec["error"] == "worker heartbeat stale"
    persisted = api_server._queue_state.load_snapshot()
    assert persisted is not None
    assert persisted["workers"][0]["status"] == "error"


def test_task_queue_watchdog_api(monkeypatch: pytest.MonkeyPatch, api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    monkeypatch.setenv("VSPIDER_QUEUE_WORKER_STALE_SECONDS", "0.1")
    with api_server._TASK_LOCK:
        api_server.active_tasks["workers"] = {
            "worker_stale_api": {
                "worker_id": "worker_stale_api",
                "status": "running",
                "task_id": "stale_api",
                "started_at": 1.0,
                "last_seen_at": 1.0,
                "stopped_at": None,
                "error": "",
            }
        }
        api_server.active_tasks["queue_worker_running"] = True
    client = TestClient(api_server.app)

    resp = client.post("/api/task_queue/watchdog")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "success"
    assert payload["watchdog"]["stale_worker_count"] == 1
    assert payload["queue"]["workers"][0]["status"] == "error"
    assert payload["persisted"]["workers"][0]["status"] == "error"


def test_queue_watchdog_scheduler_config(monkeypatch: pytest.MonkeyPatch, api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    monkeypatch.delenv("VSPIDER_QUEUE_WATCHDOG_ENABLED", raising=False)
    monkeypatch.setenv("VSPIDER_QUEUE_WATCHDOG_INTERVAL", "0.25")

    config = api_server._queue_watchdog_scheduler_config()

    assert config["enabled"] is False
    assert config["interval_s"] == 0.25
    assert api_server._queue_snapshot()["watchdog_config"] == config


def test_queue_watchdog_scheduler_scans_stale_workers(monkeypatch: pytest.MonkeyPatch, api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    monkeypatch.setenv("VSPIDER_QUEUE_WATCHDOG_INTERVAL", "0.01")
    monkeypatch.setenv("VSPIDER_QUEUE_WORKER_STALE_SECONDS", "0.1")
    with api_server._TASK_LOCK:
        api_server.active_tasks["workers"] = {
            "worker_auto_stale": {
                "worker_id": "worker_auto_stale",
                "status": "running",
                "task_id": "auto_stale_task",
                "started_at": 1.0,
                "last_seen_at": 1.0,
                "stopped_at": None,
                "error": "",
            }
        }
        api_server.active_tasks["queue_worker_running"] = True

    async def run_scheduler_once() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(api_server._queue_watchdog_scheduler(stop))
        await asyncio.sleep(0.03)
        stop.set()
        await task

    asyncio.run(run_scheduler_once())

    snapshot = api_server._queue_snapshot()
    assert snapshot["workers"][0]["status"] == "error"
    assert snapshot["active_worker_count"] == 0


def test_recover_queued_tasks_from_persisted_snapshot(api_with_tmp_registry, local_tmp_path: Path) -> None:
    api_server = api_with_tmp_registry
    queued = api_server._run_registry.create_run(
        run_id="queued_task",
        target_url="https://queued.example.com",
        prompt="queued",
        status="queued",
        base_dir=local_tmp_path,
    )
    running = api_server._run_registry.create_run(
        run_id="running_task",
        target_url="https://running.example.com",
        prompt="running",
        status="running",
        base_dir=local_tmp_path,
    )
    api_server._queue_state.save_snapshot(
        {
            "running": True,
            "worker_running": True,
            "current": {
                "task_id": running["task_id"],
                "status": "running",
                "target_url": running["target_url"],
                "prompt": running["prompt"],
            },
            "queue_length": 1,
            "queue": [
                {
                    "task_id": queued["task_id"],
                    "status": "queued",
                    "target_url": queued["target_url"],
                    "prompt": queued["prompt"],
                    "mode": "single",
                }
            ],
            "workers": [{"worker_id": "w1", "status": "running", "task_id": running["task_id"]}],
        }
    )

    result = api_server.recover_queued_tasks()

    assert result["recovered_count"] == 1
    assert result["interrupted_count"] == 1
    assert result["recovered_task_ids"] == ["queued_task"]
    assert result["interrupted_task_ids"] == ["running_task"]
    snapshot = api_server._queue_snapshot()
    assert snapshot["queue_length"] == 1
    assert snapshot["queue"][0]["task_id"] == "queued_task"
    assert snapshot["current"] is None
    interrupted = api_server._run_registry.load_run("running_task", base_dir=local_tmp_path)
    assert interrupted is not None
    assert interrupted["status"] == "error"
    assert interrupted["error"] == "interrupted before queue recovery"


def test_recover_queued_tasks_prefers_execution_queue_payload(api_with_tmp_registry) -> None:
    api_server = api_with_tmp_registry
    api_server._queue_state.save_snapshot(
        {
            "queue_length": 1,
            "queue": [
                {
                    "task_id": "recover_full",
                    "status": "queued",
                    "target_url": "https://public.example.com",
                    "prompt": "public",
                }
            ],
            "execution_queue": [
                {
                    "task_id": "recover_full",
                    "status": "queued",
                    "target_url": "https://full.example.com",
                    "prompt": "full",
                    "file_path": "temp_uploads/input.xlsx",
                    "auth_profiles": "admin",
                    "vlm_model": "model-a",
                    "semantic_model": "semantic-a",
                    "vlm_text_only": True,
                    "mode": "batch",
                    "filename": "input.xlsx",
                    "file_size_kb": 12.5,
                    "vlm_model_type": "vl",
                    "vlm_options": {"api_key": "secret", "temperature": 0.1},
                }
            ],
        }
    )

    result = api_server.recover_queued_tasks()

    assert result["recovered_task_ids"] == ["recover_full"]
    with api_server._TASK_LOCK:
        restored = api_server.active_tasks["queue"][0]
    assert restored["target_url"] == "https://full.example.com"
    assert restored["file_path"] == "temp_uploads/input.xlsx"
    assert restored["auth_profiles"] == "admin"
    # api_key is masked on disk then dropped on recovery (a per-run secret never
    # survives a restart); non-secret overrides like temperature still round-trip.
    assert "api_key" not in restored["vlm_options"]
    assert restored["vlm_options"]["temperature"] == 0.1


def test_task_queue_recover_api(api_with_tmp_registry, local_tmp_path: Path) -> None:
    api_server = api_with_tmp_registry
    api_server._run_registry.create_run(
        run_id="queued_api",
        target_url="https://queued.example.com",
        prompt="queued",
        status="queued",
        base_dir=local_tmp_path,
    )
    api_server._queue_state.save_snapshot(
        {
            "queue_length": 1,
            "queue": [
                {
                    "task_id": "queued_api",
                    "status": "queued",
                    "target_url": "https://queued.example.com",
                    "prompt": "queued",
                }
            ],
        }
    )
    client = TestClient(api_server.app)

    resp = client.post("/api/task_queue/recover")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "success"
    assert payload["recovery"]["recovered_count"] == 1
    assert payload["queue"]["queue_length"] == 1
    assert payload["persisted"]["queue_length"] == 1
    assert "execution_queue" not in payload["persisted"]


def test_task_queue_source_wiring() -> None:
    root = Path(__file__).resolve().parent.parent
    api_src = (root / "api_server.py").read_text(encoding="utf-8")

    assert "active_tasks: dict[str, Any] = {" in api_src
    assert '"queue_worker_running": False' in api_src
    assert '"queue_paused": False' in api_src
    assert '"workers": {}' in api_src
    assert "def _queue_worker_config() -> dict[str, Any]:" in api_src
    assert "def _queue_heartbeat_config() -> dict[str, Any]:" in api_src
    assert "def _queue_watchdog_scheduler_config() -> dict[str, Any]:" in api_src
    assert "def _start_queue_workers(background_tasks: BackgroundTasks)" in api_src
    assert "def pause_task_queue(reason: str = \"\") -> dict[str, Any]:" in api_src
    assert "def resume_task_queue() -> tuple[dict[str, Any], bool]:" in api_src
    assert "def retry_run_as_queued_task(run_id: str) -> tuple[bool, str, dict[str, Any]]:" in api_src
    assert "def queue_metrics(*, run_limit: int = 200) -> dict[str, Any]:" in api_src
    assert "def _persist_queue_snapshot_safe() -> None:" in api_src
    assert "async def _queue_worker_heartbeat(worker_id: str, stop_signal: asyncio.Event) -> None:" in api_src
    assert "async def _queue_watchdog_scheduler(stop_signal: asyncio.Event) -> None:" in api_src
    assert '"heartbeat_config": heartbeat_config' in api_src
    assert '"watchdog_config": watchdog_config' in api_src
    assert '"stale_worker_count": sum(1 for worker in public_workers if worker.get("stale"))' in api_src
    assert "def scan_stale_queue_workers() -> dict[str, Any]:" in api_src
    assert 'VSPIDER_QUEUE_WATCHDOG_ENABLED' in api_src
    assert "def recover_queued_tasks() -> dict[str, Any]:" in api_src
    assert "from visual_web_agent import queue_state as _queue_state" in api_src
    assert "async def _queue_worker(worker_id: str = \"worker_default\") -> None:" in api_src
    assert "def _enqueue_task(" in api_src
    assert '@app.get("/api/task_queue"' in api_src
    assert '@app.get("/api/task_queue/metrics"' in api_src
    assert '"persisted": _queue_state.load_public_snapshot()' in api_src
    assert 'snapshot["execution_queue"]' in api_src
    assert '@app.post("/api/task_queue/pause"' in api_src
    assert '@app.post("/api/task_queue/resume"' in api_src
    assert '@app.post("/api/task_queue/recover"' in api_src
    assert '@app.post("/api/task_queue/watchdog"' in api_src
    assert '@app.post("/api/runs/{run_id}/retry"' in api_src
    assert '@app.delete("/api/task_queue/{task_id}"' in api_src

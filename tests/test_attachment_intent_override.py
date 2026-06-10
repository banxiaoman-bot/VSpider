"""优化 E：附件 intent 用户显式覆盖（``attachment_intent``）回归。

输入契约 §一-B：``attachments[i].intent`` 必推断、且用户可覆盖。本文件锁定
覆盖链路的每一跳：

- ``smart_batch_runner._dispatch_attachment`` 接受 ``intent_override`` 且优先于推断；
- ``smart_batch_runner._persist_io_contracts_safe`` 把用户 intent 写进 ``input_contract.json``；
- ``/api/start_batch`` 校验 ``attachment_intent``（非法快速失败）并把用户覆盖入队；
- ``retry_run_as_queued_task`` 从 ``input_contract.json`` 恢复 intent；
- ``recover_queued_tasks`` 不丢 queue item 上的 ``attachment_intent``；
- worker / runner 接线（source assertion，运行级行为由上面行为测试覆盖）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_attachment_intent_tests"
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
    uploads = local_tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(api_server, "TEMP_UPLOAD_DIR", uploads)

    def _reset() -> None:
        with api_server._TASK_LOCK:
            api_server.active_tasks["current_task"] = None
            api_server.active_tasks["queue"] = []
            api_server.active_tasks["queue_worker_running"] = False
            api_server.active_tasks["queue_paused"] = False
            api_server.active_tasks["queue_paused_at"] = None
            api_server.active_tasks["queue_pause_reason"] = ""
            api_server.active_tasks["workers"] = {}

    _reset()
    yield api_server
    _reset()


class _FakeBackgroundTasks:
    def __init__(self) -> None:
        self.calls = []

    def add_task(self, func, *args, **kwargs) -> None:
        self.calls.append((func, args, kwargs))


def _block_worker_autostart(api_server) -> None:
    """Register a fake idle worker so ``start_batch`` does not schedule the
    real ``_queue_worker`` loop as a background task — ``TestClient`` waits
    for background tasks, and that loop never finishes while a (fake)
    running current task exists."""
    with api_server._TASK_LOCK:
        api_server.active_tasks["workers"] = {
            "worker_test_guard": {
                "worker_id": "worker_test_guard",
                "status": "idle",
                "task_id": "",
                "started_at": 1.0,
                "last_seen_at": 1.0,
                "stopped_at": None,
                "error": "",
            }
        }
        api_server.active_tasks["queue_worker_running"] = True


# ---------------------------------------------------------------------------
# smart_batch_runner: dispatcher honours the user override
# ---------------------------------------------------------------------------


class TestDispatchAttachmentOverride:
    def test_intent_override_wins_over_inference(self, tmp_path: Path) -> None:
        from smart_batch_runner import _dispatch_attachment

        f = tmp_path / "rows.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")

        # csv would infer batch_rows; the user said "read it as context".
        spec, result = _dispatch_attachment(
            str(f), "随便处理一下", intent_override="prompt_context"
        )

        assert spec is not None
        assert spec.intent == "prompt_context"
        assert result is not None

    def test_invalid_override_falls_back_to_inference(self, tmp_path: Path) -> None:
        from smart_batch_runner import _dispatch_attachment

        f = tmp_path / "rows.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")

        spec, _ = _dispatch_attachment(str(f), "", intent_override="bogus_intent")

        assert spec is not None
        assert spec.intent == "batch_rows"


# ---------------------------------------------------------------------------
# smart_batch_runner: persisted input_contract.json records the user intent
# ---------------------------------------------------------------------------


class TestPersistIoContractsIntent:
    def test_user_intent_lands_in_input_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import smart_batch_runner as sbr
        from visual_web_agent.io_contract import persistence as _persistence

        runs_root = tmp_path / "runs"
        monkeypatch.setattr(_persistence, "default_runs_root", lambda: runs_root)

        f = tmp_path / "rows.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")

        sbr._persist_io_contracts_safe(
            run_id="run_intent",
            goal="处理这个文件",
            target_url="https://a.example.com",
            urls=[],
            file_path=str(f),
            auth_profiles="",
            vlm_options=None,
            attachment_intent="upload_to_page",
        )

        payload = json.loads(
            (runs_root / "run_intent" / "input_contract.json").read_text(encoding="utf-8")
        )
        assert payload["attachments"][0]["intent"] == "upload_to_page"


# ---------------------------------------------------------------------------
# api_server: form field validation + queue item plumbing
# ---------------------------------------------------------------------------


class TestStartBatchAttachmentIntentField:
    def test_rejects_invalid_attachment_intent(self, api_with_tmp_registry) -> None:
        api_server = api_with_tmp_registry
        _block_worker_autostart(api_server)

        bg = _FakeBackgroundTasks()
        result = asyncio.run(
            api_server.start_batch(
                bg,
                target_url="https://example.com",
                prompt="do something",
                attachment_intent="definitely_bogus",
            )
        )

        assert result["status"] == "error"
        assert "attachment_intent" in result["message"]

    def test_user_intent_recorded_on_queue_item(self, api_with_tmp_registry) -> None:
        api_server = api_with_tmp_registry
        _block_worker_autostart(api_server)

        client = TestClient(api_server.app)
        resp = client.post(
            "/api/start_batch",
            data={
                "target_url": "https://example.com",
                "prompt": "读取文件内容辅助任务",
                "attachment_intent": "prompt_context",
            },
            files={"file": ("rows.csv", b"a,b\n1,2\n", "text/csv")},
        )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "success"
        assert payload["attachment_intent"] == "prompt_context"
        assert payload["attachment_intent_source"] == "user"
        with api_server._TASK_LOCK:
            item = api_server.active_tasks["queue"][0]
        assert item["attachment_intent"] == "prompt_context"

    def test_auto_intent_keeps_inference(self, api_with_tmp_registry) -> None:
        api_server = api_with_tmp_registry
        _block_worker_autostart(api_server)

        client = TestClient(api_server.app)
        resp = client.post(
            "/api/start_batch",
            data={
                "target_url": "https://example.com",
                "prompt": "对每一行执行填报",
                "attachment_intent": "auto",
            },
            files={"file": ("rows.csv", b"a,b\n1,2\n", "text/csv")},
        )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "success"
        # csv + per-row goal infers batch_rows; auto means "no user override".
        assert payload["attachment_intent"] == "batch_rows"
        assert payload["attachment_intent_source"] == "inferred"
        with api_server._TASK_LOCK:
            item = api_server.active_tasks["queue"][0]
        assert item["attachment_intent"] == ""


class TestEnqueueTaskCarriesIntent:
    def test_enqueue_task_carries_attachment_intent(self, api_with_tmp_registry) -> None:
        api_server = api_with_tmp_registry

        item, _ = api_server._enqueue_task(
            target_url="https://example.com",
            prompt="x",
            file_path="rows.csv",
            attachment_intent="media_source",
        )

        assert item["attachment_intent"] == "media_source"


class TestRunBatchTaskForwardsIntent:
    def test_run_batch_task_forwards_intent_to_runner(
        self, api_with_tmp_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api_server = api_with_tmp_registry
        import smart_batch_runner

        captured: dict = {}

        def fake_sync(*args, **kwargs):
            captured.update(kwargs)
            return True

        monkeypatch.setattr(smart_batch_runner, "run_smart_batch_sync", fake_sync)

        asyncio.run(
            api_server._run_batch_task(
                "tid_intent",
                "https://a.example.com",
                "prompt",
                "rows.csv",
                threading.Event(),
                attachment_intent="prompt_context",
            )
        )

        assert captured.get("attachment_intent") == "prompt_context"


# ---------------------------------------------------------------------------
# retry / recover keep the intent
# ---------------------------------------------------------------------------


class TestRetryRestoresIntent:
    def test_retry_restores_intent_from_input_contract(
        self, api_with_tmp_registry, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api_server = api_with_tmp_registry
        from visual_web_agent.io_contract import (
            build_input_contract,
            write_input_contract,
        )
        from visual_web_agent.io_contract import persistence as _persistence

        runs_root = local_tmp_path / "runs_root"
        monkeypatch.setattr(_persistence, "default_runs_root", lambda: runs_root)
        attachment = local_tmp_path / "upload.csv"
        attachment.write_text("url\nhttps://a.example.com\n", encoding="utf-8")

        api_server._run_registry.create_run(
            run_id="intent_retry_source",
            target_url="https://form.example.com",
            prompt="retry intent",
            status="failed",
            mode="batch",
            base_dir=local_tmp_path,
        )
        contract = build_input_contract(
            goal="retry intent",
            target_url="https://form.example.com",
            attachments=[
                {
                    "path": str(attachment),
                    "filename": attachment.name,
                    "mime": "text/csv",
                    "size": attachment.stat().st_size,
                    "intent": "upload_to_page",
                }
            ],
        )
        write_input_contract("intent_retry_source", contract, base_dir=runs_root)

        ok, message, _retry = api_server.retry_run_as_queued_task("intent_retry_source")

        assert ok is True, message
        with api_server._TASK_LOCK:
            item = api_server.active_tasks["queue"][0]
        assert item["attachment_intent"] == "upload_to_page"


class TestRecoverKeepsIntent:
    def test_recover_preserves_attachment_intent(self, api_with_tmp_registry) -> None:
        api_server = api_with_tmp_registry
        api_server._queue_state.save_snapshot(
            {
                "queue": [
                    {
                        "task_id": "queued_intent",
                        "status": "queued",
                        "target_url": "https://queued.example.com",
                        "prompt": "queued",
                        "attachment_intent": "upload_to_page",
                    }
                ],
            }
        )

        result = api_server.recover_queued_tasks()

        assert result["recovered_count"] == 1
        with api_server._TASK_LOCK:
            item = api_server.active_tasks["queue"][0]
        assert item["attachment_intent"] == "upload_to_page"


# ---------------------------------------------------------------------------
# source wiring (worker loop / runner internals not exercised end-to-end)
# ---------------------------------------------------------------------------


class TestSourceWiring:
    def test_queue_worker_passes_attachment_intent(self) -> None:
        root = Path(__file__).resolve().parent.parent
        api_src = (root / "api_server.py").read_text(encoding="utf-8")
        assert '_run_batch_kwargs["attachment_intent"]' in api_src

    def test_smart_batch_runner_threads_intent_through(self) -> None:
        root = Path(__file__).resolve().parent.parent
        src = (root / "smart_batch_runner.py").read_text(encoding="utf-8")
        # run_smart_batch / run_smart_batch_sync accept and forward the override
        assert 'attachment_intent: str = ""' in src
        assert "intent_override=" in src

from __future__ import annotations

import asyncio

import pytest

from smart_batch_runner import run_smart_batch


def _run(coro):
    return asyncio.run(coro)


def test_single_task_returns_false_when_agent_returns_false(monkeypatch) -> None:
    async def fake_run_agent(**kwargs):
        return False

    import visual_web_agent.main as main_mod

    monkeypatch.setattr(main_mod, "run_agent", fake_run_agent)

    ok = _run(run_smart_batch(
        "https://www.bing.com",
        "do something",
        None,
    ))

    assert ok is False


def test_single_task_returns_true_when_agent_succeeds(monkeypatch) -> None:
    async def fake_run_agent(**kwargs):
        return True

    import visual_web_agent.main as main_mod

    monkeypatch.setattr(main_mod, "run_agent", fake_run_agent)

    ok = _run(run_smart_batch(
        "https://www.bing.com",
        "do something",
        "",
    ))

    assert ok is True


def test_single_task_does_not_overwrite_agent_done_broadcast(monkeypatch) -> None:
    async def fake_run_agent(**kwargs):
        return True

    import smart_batch_runner as runner
    import visual_web_agent.main as main_mod

    calls: list[tuple[bool, str]] = []
    monkeypatch.setattr(main_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(main_mod, "_run_done_was_broadcasted", lambda: True)
    monkeypatch.setattr(
        runner,
        "broadcast_done",
        lambda success, message="": calls.append((success, message)),
    )

    ok = _run(run_smart_batch(
        "https://www.bing.com",
        "do something",
        None,
    ))

    assert ok is True
    assert calls == []


def test_single_task_broadcasts_fallback_when_agent_did_not_broadcast(monkeypatch) -> None:
    async def fake_run_agent(**kwargs):
        return True

    import smart_batch_runner as runner
    import visual_web_agent.main as main_mod

    calls: list[tuple[bool, str]] = []
    monkeypatch.setattr(main_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(main_mod, "_run_done_was_broadcasted", lambda: False)
    monkeypatch.setattr(
        runner,
        "broadcast_done",
        lambda success, message="": calls.append((success, message)),
    )

    ok = _run(run_smart_batch(
        "https://www.bing.com",
        "do something",
        None,
    ))

    assert ok is True
    assert calls == [(True, "🎉 单任务执行完成")]


def test_single_task_returns_false_when_agent_raises(monkeypatch) -> None:
    async def fake_run_agent(**kwargs):
        raise RuntimeError("browser died")

    import visual_web_agent.main as main_mod

    monkeypatch.setattr(main_mod, "run_agent", fake_run_agent)

    ok = _run(run_smart_batch(
        "https://www.bing.com",
        "do something",
        None,
    ))

    assert ok is False


def test_api_background_task_marks_false_return_as_failed(monkeypatch) -> None:
    import api_server

    def fake_run_smart_batch_sync(*args, **kwargs):
        return False

    monkeypatch.setattr(
        "smart_batch_runner.run_smart_batch_sync",
        fake_run_smart_batch_sync,
    )
    task_id = "task-false"
    stop_event = api_server.threading.Event()
    with api_server._TASK_LOCK:
        api_server.active_tasks["current_task"] = {
            "task_id": task_id,
            "running": True,
            "status": "running",
            "stop_event": stop_event,
        }

    _run(api_server._run_batch_task(
        task_id,
        "https://www.bing.com",
        "do something",
        None,
        stop_event,
        "",
        {},
    ))

    with api_server._TASK_LOCK:
        task = api_server.active_tasks["current_task"]
    assert task["running"] is False
    assert task["status"] == "failed"

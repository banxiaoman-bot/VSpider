"""ASYNC fix: ``api_server._schedule`` must hold a strong ref to background tasks.

``asyncio`` keeps only a *weak* reference to the task returned by
``loop.create_task``; if no strong reference is held the task can be
garbage-collected before it finishes, silently dropping the coroutine (here: a
log line / WebSocket broadcast). ``_schedule`` now registers each task in
``_BACKGROUND_TASKS`` and discards it on completion.
"""

from __future__ import annotations

import asyncio
import gc

import pytest

import api_server


@pytest.fixture(autouse=True)
def _restore_api_loop():
    saved_loop = api_server._API_LOOP
    saved_tasks = set(api_server._BACKGROUND_TASKS)
    try:
        yield
    finally:
        api_server._API_LOOP = saved_loop
        api_server._BACKGROUND_TASKS.clear()
        api_server._BACKGROUND_TASKS.update(saved_tasks)


def test_schedule_keeps_strong_ref_and_runs_to_completion() -> None:
    ran: list[str] = []

    async def scenario() -> None:
        api_server._API_LOOP = asyncio.get_running_loop()
        api_server._BACKGROUND_TASKS.clear()

        async def work() -> None:
            await asyncio.sleep(0.02)
            ran.append("done")

        api_server._schedule(work())
        # Referenced immediately so a GC pass can't drop the in-flight task.
        assert len(api_server._BACKGROUND_TASKS) == 1
        gc.collect()
        assert len(api_server._BACKGROUND_TASKS) == 1

        await asyncio.sleep(0.1)
        # The task ran to completion AND was discarded from the registry.
        assert ran == ["done"]
        assert len(api_server._BACKGROUND_TASKS) == 0

    asyncio.run(scenario())


def test_schedule_is_noop_when_loop_missing() -> None:
    # With no event loop registered, _schedule must close the coroutine and
    # return without scheduling anything (CLI-only mode), and never crash.
    api_server._API_LOOP = None
    before = len(api_server._BACKGROUND_TASKS)

    async def work() -> None:  # pragma: no cover - must never run
        raise AssertionError("coroutine should not run without a loop")

    api_server._schedule(work())
    assert len(api_server._BACKGROUND_TASKS) == before

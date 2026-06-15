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

import api_server  # noqa: F401 — ensure module imported
import broadcast as _broadcast_mod


@pytest.fixture(autouse=True)
def _restore_api_loop():
    saved_loop = _broadcast_mod._API_LOOP
    saved_tasks = set(_broadcast_mod._BACKGROUND_TASKS)
    try:
        yield
    finally:
        _broadcast_mod._API_LOOP = saved_loop
        _broadcast_mod._BACKGROUND_TASKS.clear()
        _broadcast_mod._BACKGROUND_TASKS.update(saved_tasks)


def test_schedule_keeps_strong_ref_and_runs_to_completion() -> None:
    ran: list[str] = []

    async def scenario() -> None:
        _broadcast_mod._API_LOOP = asyncio.get_running_loop()
        _broadcast_mod._BACKGROUND_TASKS.clear()

        async def work() -> None:
            await asyncio.sleep(0.02)
            ran.append("done")

        _broadcast_mod._schedule(work())
        assert len(_broadcast_mod._BACKGROUND_TASKS) == 1
        gc.collect()
        assert len(_broadcast_mod._BACKGROUND_TASKS) == 1

        await asyncio.sleep(0.1)
        assert ran == ["done"]
        assert len(_broadcast_mod._BACKGROUND_TASKS) == 0

    asyncio.run(scenario())


def test_schedule_is_noop_when_loop_missing() -> None:
    _broadcast_mod._API_LOOP = None
    before = len(_broadcast_mod._BACKGROUND_TASKS)

    async def work() -> None:  # pragma: no cover
        raise AssertionError("coroutine should not run without a loop")

    _broadcast_mod._schedule(work())
    assert len(_broadcast_mod._BACKGROUND_TASKS) == before

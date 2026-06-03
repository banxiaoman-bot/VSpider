"""Regression tests for the ``resume_run`` agent capability (RUN-RESUME1 step 3).

Covers:
  - ``VSpiderAction`` schema accepts the new action literal
  - handler is registered in ``ActionRegistry``
  - reads a pre-populated ``workflow_memory["__resume_state"]`` (the resume the
    loop already decided for this run) into a readable ``resume_status``
  - falls back to loading ``run_checkpoint.json`` for the active run
  - degrades cleanly when there is nothing to resume
  - records RPA-trail evidence and works without a live page

Deterministic stubs (no Playwright, no network), mirroring
``test_page_to_markdown_handler.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from visual_web_agent.actions import ActionContext, ActionRegistry
from visual_web_agent.io_contract.runtime import clear_current_run, set_current_run
from visual_web_agent.resume_run_action import ResumeRunHandler
from visual_web_agent.run_checkpoint import build_checkpoint_state, save_run_checkpoint
from visual_web_agent.vlm_client import VSpiderAction


class _StubBrowser:
    def __init__(self) -> None:
        self.rpa_trail: list = []


def _make_ctx(memory: dict | None = None, *, memory_key: str = "", page: object = None) -> ActionContext:
    action = VSpiderAction(action="resume_run", target_id=0, type_value="", memory_key=memory_key)
    return ActionContext(
        action=action,
        browser=_StubBrowser(),
        workflow_memory=memory if memory is not None else {},
        page=page,
    )


class TestSchemaAndRegistration:
    def test_schema_accepts_action(self) -> None:
        v = VSpiderAction(action="resume_run", target_id=0, type_value="", memory_key="")
        assert v.action == "resume_run"

    def test_handler_registered(self) -> None:
        assert ActionRegistry.is_registered("resume_run")
        assert isinstance(ActionRegistry.get("resume_run"), ResumeRunHandler)


class TestHandlerBehavior:
    def test_reads_resume_state_from_memory(self) -> None:
        mem = {
            "__resume_state": {
                "resumed": True,
                "from_turn": 7,
                "completed_steps": ["s1", "s2"],
                "item_count": 9,
                "prior_status": "failed",
                "note": "续跑提示",
            }
        }
        ctx = _make_ctx(mem)
        asyncio.run(ResumeRunHandler().execute(ctx))
        status = ctx.workflow_memory["resume_status"]
        assert status["resumed"] is True
        assert status["from_turn"] == 7
        assert status["item_count"] == 9
        assert status["completed_steps"] == ["s1", "s2"]
        assert status["source"] == "workflow_memory"

    def test_loads_checkpoint_when_no_memory_state(self, tmp_path: Path) -> None:
        set_current_run("rr_cp", base_dir=str(tmp_path))
        try:
            save_run_checkpoint(
                "rr_cp",
                build_checkpoint_state(
                    "rr_cp", goal="g", turn=4, status="in_progress",
                    completed_steps=["a"], item_count=5,
                ),
                base_dir=tmp_path,
            )
            ctx = _make_ctx({})
            asyncio.run(ResumeRunHandler().execute(ctx))
        finally:
            clear_current_run()
        status = ctx.workflow_memory["resume_status"]
        assert status["resumed"] is True
        assert status["from_turn"] == 4
        assert status["item_count"] == 5
        assert status["source"] == "checkpoint"

    def test_no_progress_degrades_cleanly(self, tmp_path: Path) -> None:
        set_current_run("rr_empty", base_dir=str(tmp_path))
        try:
            ctx = _make_ctx({})
            asyncio.run(ResumeRunHandler().execute(ctx))
        finally:
            clear_current_run()
        status = ctx.workflow_memory["resume_status"]
        assert status["resumed"] is False
        assert status["source"] == "none"
        assert isinstance(status["note"], str) and status["note"]

    def test_custom_memory_key(self) -> None:
        ctx = _make_ctx({"__resume_state": {"resumed": False}}, memory_key="my_resume")
        asyncio.run(ResumeRunHandler().execute(ctx))
        assert "my_resume" in ctx.workflow_memory

    def test_records_rpa_trail(self) -> None:
        ctx = _make_ctx({"__resume_state": {"resumed": True, "from_turn": 2, "item_count": 1}})
        asyncio.run(ResumeRunHandler().execute(ctx))
        trail = ctx.browser.rpa_trail
        assert trail and trail[-1]["action"] == "resume_run"
        assert trail[-1]["resumed"] is True

    def test_works_without_page(self) -> None:
        ctx = _make_ctx({"__resume_state": {"resumed": True, "from_turn": 1}}, page=None)
        asyncio.run(ResumeRunHandler().execute(ctx))
        assert ctx.workflow_memory["resume_status"]["resumed"] is True

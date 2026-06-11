"""Regression tests for the ``vscroll_capture`` agent capability (VSCROLL-ACTION-1).

The deterministic virtual-list capture (EXTRACT-VSCROLL-1/2/3) was reachable
only from the pre-extract fast-path - before the planner starts. A virtualised
list discovered mid-run (behind a login / navigation / tab switch) still cost
one VLM round per viewport. This locks the mid-run action exposure:

  - ``VSpiderAction`` schema accepts the new action literal
  - handler registered in ``ActionRegistry``; tool registered in the default
    action registry (capability=extract, evidence fields)
  - capability router flags 中英文 virtual-scroll goals and surfaces a
    ``vscroll_capture`` plan step
  - prompt skill block registered and injected for 中英文 triggers only
  - handler: main-document hit, frame fallback, no-hit error, ``type_value``
    row cap, jsonl artifact persistence under an active run

Uses deterministic stub page / frame objects (no Playwright, no network),
mirroring the stub style of ``test_virtual_scroll_nudge.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from visual_web_agent.actions import ActionContext, ActionExecutionError, ActionRegistry
from visual_web_agent.action_registry import build_default_action_registry
from visual_web_agent.capability_router import _backend_plan, _signals
from visual_web_agent.io_contract.runtime import clear_current_run, set_current_run
from visual_web_agent.prompt_skills import SKILL_PROMPTS, VSCROLL_CAPTURE_SKILL
from visual_web_agent.prompts import build_system_prompt
from visual_web_agent.virtual_scroll import (
    VIRTUAL_LIST_ROWS_JS,
    VIRTUAL_LIST_SIGNATURE_JS,
    VIRTUAL_SCROLL_NUDGE_JS,
)
from visual_web_agent.vlm_client import VSpiderAction
from visual_web_agent.vscroll_capture_action import VscrollCaptureHandler


class _ScriptedScope:
    """Routes evaluate() by script identity; each list's last value repeats."""

    def __init__(
        self,
        *,
        sig: list[Any] | None = None,
        rows: list[Any] | None = None,
        nudge: list[Any] | None = None,
        url: str = "",
    ) -> None:
        self._sig = list(sig or [])
        self._rows = list(rows or [])
        self._nudge = list(nudge or [])
        self.url = url

    @staticmethod
    def _next(queue: list[Any]) -> Any:
        if not queue:
            return {}
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if script is VIRTUAL_LIST_SIGNATURE_JS:
            return self._next(self._sig)
        if script is VIRTUAL_LIST_ROWS_JS:
            return self._next(self._rows)
        if script is VIRTUAL_SCROLL_NUDGE_JS:
            return self._next(self._nudge)
        raise AssertionError(f"unexpected script: {script[:60]}")

    async def wait_for_timeout(self, ms: int) -> None:
        return None


class _StubFrame(_ScriptedScope):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)

    def is_detached(self) -> bool:
        return False


class _StubPage(_ScriptedScope):
    """Main-document scope; child frames are attached via ``frames``."""

    def __init__(self, *, child_frames: list[Any] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.main_frame = object()
        self.frames = [self.main_frame, *(child_frames or [])]


class _StubBrowser:
    def __init__(self) -> None:
        self.rpa_trail: list = []


_HIT_SIG = {"found": True, "sig": "Row 1||Row 9", "remaining": 480}
_MISS_SIG = {"found": False, "sig": ""}
_DONE_NUDGE = {"mode": "container", "moved": False, "container_class": "vlist"}


def _make_ctx(page: Any, *, type_value: str = "", memory_key: str = "") -> ActionContext:
    action = VSpiderAction(
        action="vscroll_capture",
        target_id=0,
        type_value=type_value,
        memory_key=memory_key,
    )
    return ActionContext(
        action=action,
        browser=_StubBrowser(),
        workflow_memory={},
        page=page,
    )


def _rows_snap(*texts: str) -> dict:
    return {"found": True, "rows": [{"text": t} for t in texts]}


class TestSchemaAndRegistration:
    def test_schema_accepts_action(self) -> None:
        v = VSpiderAction(action="vscroll_capture", target_id=0, type_value="", memory_key="")
        assert v.action == "vscroll_capture"

    def test_handler_registered(self) -> None:
        assert ActionRegistry.is_registered("vscroll_capture")
        assert isinstance(ActionRegistry.get("vscroll_capture"), VscrollCaptureHandler)

    def test_tool_registered_with_evidence(self) -> None:
        registry = build_default_action_registry()
        tool = registry.get("vscroll_capture")
        assert tool is not None
        assert tool.capability == "extract"
        assert "row_count" in tool.evidence
        assert "complete" in tool.evidence
        assert tool.score_goal("把这个虚拟列表全量采集下来") > 0
        assert tool.score_goal("harvest the infinite scroll list") > 0


class TestRouterSignal:
    def test_chinese_goal_flags_vscroll(self) -> None:
        sig = _signals("把这个无限滚动列表滚到底全量采集", {})
        assert sig["vscroll_capture_preferred"] is True

    def test_english_goal_flags_vscroll(self) -> None:
        sig = _signals("capture every row of the virtual scroll table", {})
        assert sig["vscroll_capture_preferred"] is True

    def test_unrelated_goal_not_flagged(self) -> None:
        sig = _signals("click the submit button and login", {})
        assert sig["vscroll_capture_preferred"] is False

    def test_plan_includes_vscroll_capture(self) -> None:
        sig = _signals("harvest the react-window virtualized list", {})
        plan = _backend_plan(sig, {}, [])
        assert "vscroll_capture" in [step.get("name") for step in plan]

    def test_unrelated_plan_excludes_vscroll_capture(self) -> None:
        sig = _signals("click the submit button", {})
        plan = _backend_plan(sig, {}, [])
        assert "vscroll_capture" not in [step.get("name") for step in plan]


class TestPromptSkill:
    def test_registered_in_skill_map(self) -> None:
        assert "vscroll_capture" in SKILL_PROMPTS
        assert SKILL_PROMPTS["vscroll_capture"] is VSCROLL_CAPTURE_SKILL
        assert "vscroll_capture" in VSCROLL_CAPTURE_SKILL

    def test_injected_for_chinese_goal(self) -> None:
        prompt = build_system_prompt(goal="页面里的虚拟列表滚到底全量采集", browser_state="")
        assert "Virtual List Capture" in prompt

    def test_injected_for_english_goal(self) -> None:
        prompt = build_system_prompt(goal="capture all rows from the infinite scroll list", browser_state="")
        assert "Virtual List Capture" in prompt

    def test_not_injected_for_unrelated_goal(self) -> None:
        prompt = build_system_prompt(goal="点击登录按钮", browser_state="")
        assert "Virtual List Capture" not in prompt


class TestHandlerBehavior:
    def test_main_document_capture_end_to_end(self) -> None:
        page = _StubPage(
            sig=[_HIT_SIG],
            rows=[_rows_snap("Row 1", "Row 2")],
            nudge=[_DONE_NUDGE],
            url="https://example.com/list",
        )
        ctx = _make_ctx(page, memory_key="vrows")
        asyncio.run(VscrollCaptureHandler().execute(ctx))
        mem = ctx.workflow_memory["vrows"]
        assert mem["row_count"] == 2
        assert mem["complete"] is True
        assert mem["where"] == "main"
        assert mem["source_url"] == "https://example.com/list"
        assert [r["text"] for r in mem["rows"]] == ["Row 1", "Row 2"]
        trail = ctx.browser.rpa_trail
        assert trail and trail[-1]["action"] == "vscroll_capture"
        assert trail[-1]["row_count"] == 2
        assert trail[-1]["output_kind"] == "dataset_rows"

    def test_default_memory_key(self) -> None:
        page = _StubPage(sig=[_HIT_SIG], rows=[_rows_snap("Row 1")], nudge=[_DONE_NUDGE])
        ctx = _make_ctx(page)
        asyncio.run(VscrollCaptureHandler().execute(ctx))
        assert "vscroll_rows" in ctx.workflow_memory

    def test_falls_back_to_frame_scope(self) -> None:
        frame = _StubFrame(
            sig=[_HIT_SIG],
            rows=[_rows_snap("F 1", "F 2", "F 3")],
            nudge=[_DONE_NUDGE],
            url="https://example.com/inner",
        )
        page = _StubPage(sig=[_MISS_SIG], child_frames=[frame], url="https://example.com")
        ctx = _make_ctx(page)
        asyncio.run(VscrollCaptureHandler().execute(ctx))
        mem = ctx.workflow_memory["vscroll_rows"]
        assert mem["row_count"] == 3
        assert mem["where"] == "frame"
        assert mem["frame_url"] == "https://example.com/inner"

    def test_no_virtual_list_raises(self) -> None:
        page = _StubPage(sig=[_MISS_SIG])
        ctx = _make_ctx(page)
        with pytest.raises(ActionExecutionError, match="未发现"):
            asyncio.run(VscrollCaptureHandler().execute(ctx))

    def test_empty_capture_raises(self) -> None:
        page = _StubPage(sig=[_HIT_SIG], rows=[{"found": True, "rows": []}], nudge=[_DONE_NUDGE])
        ctx = _make_ctx(page)
        with pytest.raises(ActionExecutionError, match="未采集到行"):
            asyncio.run(VscrollCaptureHandler().execute(ctx))

    def test_type_value_caps_rows(self) -> None:
        page = _StubPage(
            sig=[_HIT_SIG],
            rows=[_rows_snap("Row 1", "Row 2", "Row 3", "Row 4")],
            nudge=[_DONE_NUDGE],
        )
        ctx = _make_ctx(page, type_value="2")
        asyncio.run(VscrollCaptureHandler().execute(ctx))
        mem = ctx.workflow_memory["vscroll_rows"]
        assert mem["row_count"] == 2
        assert mem["complete"] is False
        assert ctx.browser.rpa_trail[-1]["max_rows"] == 2

    def test_headers_map_cells_to_named_fields(self) -> None:
        page = _StubPage(
            sig=[_HIT_SIG],
            rows=[{
                "found": True,
                "headers": ["Name", "Office"],
                "rows": [
                    {"text": "Tiger Nixon Edinburgh", "cells": ["Tiger Nixon", "Edinburgh"]},
                    {"text": "Garrett Winters Tokyo", "cells": ["Garrett Winters", "Tokyo"]},
                ],
            }],
            nudge=[_DONE_NUDGE],
        )
        ctx = _make_ctx(page)
        asyncio.run(VscrollCaptureHandler().execute(ctx))
        mem = ctx.workflow_memory["vscroll_rows"]
        assert mem["headers"] == ["Name", "Office"]
        assert mem["rows"][0] == {"Name": "Tiger Nixon", "Office": "Edinburgh"}
        assert mem["rows"][1] == {"Name": "Garrett Winters", "Office": "Tokyo"}
        assert ctx.browser.rpa_trail[-1]["header_count"] == 2

    def test_junk_type_value_falls_back_to_default(self) -> None:
        assert VscrollCaptureHandler._max_rows("not a number") == 2000
        assert VscrollCaptureHandler._max_rows("") == 2000
        assert VscrollCaptureHandler._max_rows("-5") == 2000
        assert VscrollCaptureHandler._max_rows("999999") == 20000
        assert VscrollCaptureHandler._max_rows("300") == 300

    def test_persists_jsonl_when_run_active(self, tmp_path: Path) -> None:
        page = _StubPage(
            sig=[_HIT_SIG],
            rows=[_rows_snap("Row 1", "Row 2")],
            nudge=[_DONE_NUDGE],
            url="https://example.com/list",
        )
        ctx = _make_ctx(page)
        set_current_run("vscroll_action_test", base_dir=str(tmp_path))
        try:
            asyncio.run(VscrollCaptureHandler().execute(ctx))
        finally:
            clear_current_run()
        path = ctx.workflow_memory["vscroll_rows"]["output_path"]
        assert path
        artifact = Path(path)
        assert artifact.exists()
        assert artifact.suffix == ".jsonl"
        lines = [json.loads(l) for l in artifact.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert [r["text"] for r in lines] == ["Row 1", "Row 2"]

    def test_missing_page_raises(self) -> None:
        ctx = _make_ctx(None)
        with pytest.raises(ActionExecutionError, match="无活动页面"):
            asyncio.run(VscrollCaptureHandler().execute(ctx))

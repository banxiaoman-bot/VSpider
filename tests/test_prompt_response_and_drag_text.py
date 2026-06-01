"""Regression tests for the final two enhancements:

* ``set_prompt_response`` — pre-arms the next native ``prompt()`` reply.
* ``drag_and_drop`` v2 — accepts ``text:<dst>`` for semantic drop targets.

Pure-Python tests (no Playwright runtime).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from visual_web_agent.actions import (
    ActionRegistry,
    DragAndDropHandler,
    SetPromptResponseHandler,
)
from visual_web_agent.prompt_skills import CONFIRM_DIALOG_SKILL, FEW_SHOT_SKILL
from visual_web_agent.vlm_client import VSpiderAction

# J: lightweight stub helper installs set_tab_notice / clear_tab_notice on
# SimpleNamespace browser stubs so handlers migrated to call
# `browser.set_tab_notice(...)` continue to surface notice text in tests
# that previously asserted on `browser._tab_switch_notice`.
import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.dirname(__file__))
from _notice_stub import install_notice_stub  # noqa: E402


# ════════════════════════════════════════════════════════════════════
#                       VLM CLIENT LITERAL
# ════════════════════════════════════════════════════════════════════


class TestVlmClientLiterals:
    def test_set_prompt_response_passes_validation(self) -> None:
        v = VSpiderAction(
            action="set_prompt_response",
            target_id=0,
            type_value="my reason",
            memory_key="",
        )
        assert v.action == "set_prompt_response"
        assert v.type_value == "my reason"

    def test_drag_and_drop_text_target_passes_validation(self) -> None:
        # The drag_and_drop literal is unchanged; the text:<...> form is
        # a runtime convention inside type_value, not a new action name.
        v = VSpiderAction(
            action="drag_and_drop",
            target_id=42,
            type_value="text:In Progress",
            memory_key="",
        )
        assert v.action == "drag_and_drop"
        assert v.type_value == "text:In Progress"


# ════════════════════════════════════════════════════════════════════
#                       SKILL PROMPT CONTENT
# ════════════════════════════════════════════════════════════════════


class TestPromptDialogSkillExtension:
    def test_confirm_dialog_documents_set_prompt_response(self) -> None:
        assert "set_prompt_response" in CONFIRM_DIALOG_SKILL

    def test_explains_one_shot_semantics(self) -> None:
        # The skill must tell VLM the value is consumed once, not persistent.
        assert "一次性" in CONFIRM_DIALOG_SKILL or "one-shot" in CONFIRM_DIALOG_SKILL.lower()

    def test_explains_ordering_constraint(self) -> None:
        """The pre-arm must come BEFORE the trigger action — VLM needs
        this rule explicitly."""
        # Mentions ordering / sequence requirement
        assert "先" in CONFIRM_DIALOG_SKILL


class TestFewShotPresence:
    def test_few_shot_keeps_5_examples(self) -> None:
        # set_prompt_response wasn't added as a new few-shot (skill doc covers
        # it). Existing 5 examples must remain intact and JSON-valid.
        assert FEW_SHOT_SKILL.count("示例 ") == 5


# ════════════════════════════════════════════════════════════════════
#                       SET_PROMPT_RESPONSE HANDLER
# ════════════════════════════════════════════════════════════════════


def _make_browser_stub() -> SimpleNamespace:
    browser = SimpleNamespace(
        _next_prompt_response=None,
        _tab_switch_notice=None,
        rpa_trail=[],
    )
    install_notice_stub(browser)
    return browser


def _make_ctx(*, type_value: str) -> SimpleNamespace:
    action = SimpleNamespace(
        target_id=0,
        type_value=type_value,
        memory_key="",
    )
    return SimpleNamespace(
        action=action,
        browser=_make_browser_stub(),
        page=object(),
        with_rpa_meta=lambda d: d,
    )


class TestSetPromptResponseBehaviour:
    def test_arms_value_on_browser(self) -> None:
        ctx = _make_ctx(type_value="my reason")
        asyncio.run(SetPromptResponseHandler().execute(ctx))
        assert ctx.browser._next_prompt_response == "my reason"

    def test_empty_type_value_arms_empty_string(self) -> None:
        """Explicitly arming '' is a valid use-case — it guarantees the
        listener doesn't fall back to default empty (i.e. it's the same
        result, but the explicit arm proves intent and is recorded in
        rpa_trail)."""
        ctx = _make_ctx(type_value="")
        asyncio.run(SetPromptResponseHandler().execute(ctx))
        assert ctx.browser._next_prompt_response == ""

    def test_overwrites_previous_arm(self) -> None:
        """Two ``set_prompt_response`` actions in a row keep the LAST value;
        order matters."""
        ctx1 = _make_ctx(type_value="first")
        asyncio.run(SetPromptResponseHandler().execute(ctx1))
        # Simulate same browser holding the arm:
        browser = ctx1.browser
        ctx2 = SimpleNamespace(
            action=SimpleNamespace(target_id=0, type_value="second", memory_key=""),
            browser=browser,
            page=object(),
            with_rpa_meta=lambda d: d,
        )
        asyncio.run(SetPromptResponseHandler().execute(ctx2))
        assert browser._next_prompt_response == "second"

    def test_writes_notice_for_vlm(self) -> None:
        ctx = _make_ctx(type_value="hello")
        asyncio.run(SetPromptResponseHandler().execute(ctx))
        assert ctx.browser._tab_switch_notice is not None
        assert "PROMPT ARMED" in ctx.browser._tab_switch_notice

    def test_appends_rpa_trail_entry(self) -> None:
        ctx = _make_ctx(type_value="trail-test")
        asyncio.run(SetPromptResponseHandler().execute(ctx))
        assert len(ctx.browser.rpa_trail) == 1
        entry = ctx.browser.rpa_trail[0]
        assert entry.get("action") == "set_prompt_response"
        assert entry.get("type_value") == "trail-test"


class TestSetPromptResponseRegistration:
    def test_handler_registered(self) -> None:
        assert ActionRegistry._handlers["set_prompt_response"] is SetPromptResponseHandler


# ════════════════════════════════════════════════════════════════════
#                       DRAG_AND_DROP TEXT TARGET
# ════════════════════════════════════════════════════════════════════


class _DragLocator:
    """Minimal Locator stub for the cross-frame text search path."""

    def __init__(self, *, count_value: int = 1, inner_text: str = "drop"):
        self._count = count_value
        self._inner_text = inner_text
        self._handle = SimpleNamespace(name="dest_handle")

    async def count(self):
        return self._count

    def nth(self, _i: int):
        return self

    async def inner_text(self):
        return self._inner_text

    async def element_handle(self):
        return self._handle


class _DragFrame:
    def __init__(self, *, has_match: bool):
        self.url = "https://example.test/main"
        self._has_match = has_match
        self.get_by_text_calls: list[str] = []

    def get_by_text(self, text, exact: bool = False):
        self.get_by_text_calls.append(text)
        if self._has_match:
            return _DragLocator(count_value=1, inner_text=text)
        return _DragLocator(count_value=0)


class _DragPage:
    def __init__(self, frames):
        self.frames = list(frames)
        self.main_frame = self.frames[0] if self.frames else None


def _make_drag_browser_stub(*, src_handle, dest_id_handle=None) -> SimpleNamespace:
    """Build a browser stub whose _resolve_action_target returns:
       - SoM ID == src target_id ➜ source target
       - SoM ID == drop_id (only used in the numeric path) ➜ dest target
    """

    src_target = SimpleNamespace(handle=src_handle)
    if dest_id_handle is not None:
        dest_target = SimpleNamespace(handle=dest_id_handle)

    async def _resolve(tid: int, _name: str):
        if tid == 11:
            return src_target
        if tid == 22 and dest_id_handle is not None:
            return dest_target
        return None

    async def _no_op(*_, **__):
        return None

    async def _xpath(_handle):
        return "/html/body/div"

    async def _ax_sig(*_, **__):
        return ("button", "Move")

    browser = SimpleNamespace(
        _clear_som_overlays=_no_op,
        _resolve_action_target=_resolve,
        _LOCATOR_TIMEOUT=2000,
        _wait_after_action=_no_op,
        _get_xpath=_xpath,
        _get_accessibility_signature=_ax_sig,
        _last_action_error=None,
        _tab_switch_notice=None,
        rpa_trail=[],
    )
    install_notice_stub(browser)
    return browser


class _DragHandle:
    """Source element handle stub. Records drag_to() calls for inspection."""

    def __init__(self):
        self.drag_to_calls = []

    async def scroll_into_view_if_needed(self, **_):
        return None

    async def drag_to(self, dest, **_):
        self.drag_to_calls.append(dest)


class TestDragAndDropTextTarget:
    def test_text_target_resolves_via_get_by_text(self) -> None:
        src_handle = _DragHandle()
        browser = _make_drag_browser_stub(src_handle=src_handle)

        main = _DragFrame(has_match=True)
        page = _DragPage([main])
        ctx = SimpleNamespace(
            action=SimpleNamespace(
                target_id=11,
                type_value="text:In Progress",
                memory_key="",
            ),
            browser=browser,
            page=page,
            with_rpa_meta=lambda d: d,
        )
        asyncio.run(DragAndDropHandler().execute(ctx))
        # The frame's get_by_text was invoked with the dest text
        assert main.get_by_text_calls == ["In Progress"]
        # drag_to() was called once with the locator's element handle
        assert len(src_handle.drag_to_calls) == 1
        # Notice surfaced to VLM
        assert browser._tab_switch_notice is not None
        assert "[DRAG DONE]" in browser._tab_switch_notice

    def test_text_target_falls_through_to_iframe(self) -> None:
        src_handle = _DragHandle()
        browser = _make_drag_browser_stub(src_handle=src_handle)
        main = _DragFrame(has_match=False)
        nested = _DragFrame(has_match=True)
        page = _DragPage([main, nested])
        ctx = SimpleNamespace(
            action=SimpleNamespace(
                target_id=11,
                type_value="text:Trash Bin",
                memory_key="",
            ),
            browser=browser,
            page=page,
            with_rpa_meta=lambda d: d,
        )
        asyncio.run(DragAndDropHandler().execute(ctx))
        # Both frames probed (main first, then iframe)
        assert main.get_by_text_calls == ["Trash Bin"]
        assert nested.get_by_text_calls == ["Trash Bin"]
        # Drag still completed
        assert len(src_handle.drag_to_calls) == 1

    def test_text_target_not_found_records_error(self) -> None:
        src_handle = _DragHandle()
        browser = _make_drag_browser_stub(src_handle=src_handle)
        main = _DragFrame(has_match=False)
        page = _DragPage([main])
        ctx = SimpleNamespace(
            action=SimpleNamespace(
                target_id=11,
                type_value="text:Ghost",
                memory_key="",
            ),
            browser=browser,
            page=page,
            with_rpa_meta=lambda d: d,
        )
        # Handler swallows the runtime error and records on _last_action_error.
        asyncio.run(DragAndDropHandler().execute(ctx))
        assert browser._last_action_error is not None
        assert "Ghost" in str(browser._last_action_error)
        # No drag was performed
        assert src_handle.drag_to_calls == []

    def test_numeric_target_still_works(self) -> None:
        """Backward-compat: classic SoM-ID drop target unchanged."""
        src_handle = _DragHandle()
        dest_handle = SimpleNamespace(name="legacy_dest")
        browser = _make_drag_browser_stub(
            src_handle=src_handle, dest_id_handle=dest_handle,
        )
        main = _DragFrame(has_match=False)  # Should NOT be consulted
        page = _DragPage([main])
        ctx = SimpleNamespace(
            action=SimpleNamespace(
                target_id=11,
                type_value="22",
                memory_key="",
            ),
            browser=browser,
            page=page,
            with_rpa_meta=lambda d: d,
        )
        asyncio.run(DragAndDropHandler().execute(ctx))
        # Drag fired with the dest handle from _resolve_action_target
        assert src_handle.drag_to_calls == [dest_handle]
        # No frame text search performed for the numeric path
        assert main.get_by_text_calls == []

    def test_invalid_type_value_raises_schema_error(self) -> None:
        """type_value="not-a-digit" without text: prefix must raise a
        clean schema error (propagates to dispatcher, matches existing
        handler contract — runtime errors are captured to
        ``_last_action_error``, schema errors propagate)."""
        src_handle = _DragHandle()
        browser = _make_drag_browser_stub(src_handle=src_handle)
        page = _DragPage([_DragFrame(has_match=True)])
        ctx = SimpleNamespace(
            action=SimpleNamespace(
                target_id=11,
                type_value="not-a-digit-or-text-prefix",
                memory_key="",
            ),
            browser=browser,
            page=page,
            with_rpa_meta=lambda d: d,
        )
        with pytest.raises(RuntimeError, match=r"text:|数字"):
            asyncio.run(DragAndDropHandler().execute(ctx))

    def test_text_prefix_with_empty_value_raises_schema_error(self) -> None:
        src_handle = _DragHandle()
        browser = _make_drag_browser_stub(src_handle=src_handle)
        page = _DragPage([_DragFrame(has_match=True)])
        ctx = SimpleNamespace(
            action=SimpleNamespace(
                target_id=11,
                type_value="text:",
                memory_key="",
            ),
            browser=browser,
            page=page,
            with_rpa_meta=lambda d: d,
        )
        with pytest.raises(RuntimeError, match=r"可见文本|empty"):
            asyncio.run(DragAndDropHandler().execute(ctx))

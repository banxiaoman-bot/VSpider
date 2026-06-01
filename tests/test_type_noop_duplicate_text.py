"""Regression: ``type`` action must be a no-op when the target element
already contains exactly the text VLM wants to type.

Failure that motivated this (run_log_20260518_141728):
  step 1: type @e8 "介绍一下deepseek" → succeeded
  step 2: type @e8 "介绍一下deepseek" again — VLM didn't realize step 1
          worked because the AX tree's textbox value field looked the same.
  The second type either appended ("介绍一下deepseek介绍一下deepseek") or
  overwrote, breaking the user's message in any case.

The fix: TypeHandler's no-op precheck reads the element's current text and,
if it exactly matches the target text (after strip), skips the action and
tells VLM via _tab_switch_notice.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from visual_web_agent.actions import TypeHandler

# J: stub set_tab_notice/clear_tab_notice helpers
from _notice_stub import install_notice_stub


def _run(coro):
    return asyncio.run(coro)


class _FakeHandle:
    """Stand-in for a Playwright element handle. Supports the minimal
    surface TypeHandler reaches during the no-op check + viewport probe."""

    def __init__(self, *, current_value: str = "", visible: bool = True) -> None:
        self.current_value = current_value
        self._visible = visible
        self.scroll_into_view_if_needed = AsyncMock()
        self.click = AsyncMock()
        # Sequence of evaluate side effects: viewport-check then text-read
        # then ... we route based on the JS source content
        self.evaluate = AsyncMock(side_effect=self._route_eval)
        self.fill = AsyncMock()
        self.type = AsyncMock()
        self.press = AsyncMock()

    async def _route_eval(self, js: str, *args, **kwargs):
        # Viewport check JS contains "getBoundingClientRect"
        if "getBoundingClientRect" in js and "fully_visible" in js:
            return {
                "top": 100, "bottom": 130, "left": 0, "right": 200,
                "height": 30, "width": 200, "vh": 800, "vw": 1024,
                "fully_visible": self._visible, "partial_clip": 0,
            }
        # No-op check JS reads .value or innerText
        if "el.value" in js and ("innerText" in js or "textContent" in js):
            return self.current_value
        # scrollIntoView fall-through
        return None


class _FakeTarget:
    def __init__(self, handle: _FakeHandle) -> None:
        self.handle = handle


def _build_ctx(*, current_value: str, target_text: str, target_id: int = 8):
    handle = _FakeHandle(current_value=current_value)
    target = _FakeTarget(handle)

    browser = SimpleNamespace()
    browser._LOCATOR_TIMEOUT = 5000
    browser._last_action_error = None
    browser._tab_switch_notice = None
    install_notice_stub(browser)
    browser.rpa_trail = []
    browser._clear_som_overlays = AsyncMock()
    browser._resolve_action_target = AsyncMock(return_value=target)
    browser._get_xpath = AsyncMock(return_value="//input")
    browser._get_accessibility_signature = AsyncMock(return_value=("textbox", "ask"))
    browser._count_interactive_elements = AsyncMock(return_value=10)
    browser._wait_after_action = AsyncMock()

    page = SimpleNamespace(url="https://example.com/", evaluate=AsyncMock())

    action = SimpleNamespace(
        action="type",
        target_id=target_id,
        type_value=target_text,
        memory_key="",
    )
    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = page
    ctx.workflow_memory = {}
    ctx.action = action
    ctx.with_rpa_meta = lambda d: d
    return ctx, browser, handle, target


# ── Core no-op behaviour ────────────────────────────────────────────────────


def test_noop_when_input_already_contains_target_text() -> None:
    ctx, browser, handle, _ = _build_ctx(
        current_value="介绍一下deepseek",
        target_text="介绍一下deepseek",
    )
    result = _run(TypeHandler().execute(ctx))

    # Returns None (dispatcher continues normally)
    assert result is None
    # The actual typing methods were NEVER called — that's the whole point
    handle.click.assert_not_called()
    handle.fill.assert_not_called()
    handle.type.assert_not_called()
    handle.press.assert_not_called()
    # rpa_trail records the no-op
    assert browser.rpa_trail
    last = browser.rpa_trail[-1]
    assert last["action"] == "type"
    assert last["noop"] is True
    # And a notice for VLM next step
    assert browser._tab_switch_notice is not None
    assert "TYPE NO-OP" in browser._tab_switch_notice
    assert "8" in browser._tab_switch_notice  # mentions target_id
    assert "Enter" in browser._tab_switch_notice or "press_key" in browser._tab_switch_notice


def test_proceeds_when_input_empty() -> None:
    """Standard case: input is empty, type proceeds normally."""
    ctx, browser, handle, _ = _build_ctx(
        current_value="",
        target_text="介绍一下deepseek",
    )
    _run(TypeHandler().execute(ctx))
    # The click happens early; we just verify no no-op record was written
    assert all(
        not (isinstance(t, dict) and t.get("noop"))
        for t in browser.rpa_trail
    ), "should not have recorded a noop entry"


def test_proceeds_when_input_has_different_text() -> None:
    """Input has SOMETHING but not the target → still type (overwrites)."""
    ctx, browser, handle, _ = _build_ctx(
        current_value="some other content",
        target_text="介绍一下deepseek",
    )
    _run(TypeHandler().execute(ctx))
    assert all(
        not (isinstance(t, dict) and t.get("noop"))
        for t in browser.rpa_trail
    )


def test_proceeds_when_input_is_only_partial_match() -> None:
    """If the input has the prefix only, it's NOT a no-op — VLM might be
    trying to overwrite with a fresh value."""
    ctx, browser, handle, _ = _build_ctx(
        current_value="介绍一下",  # partial
        target_text="介绍一下deepseek",
    )
    _run(TypeHandler().execute(ctx))
    assert all(
        not (isinstance(t, dict) and t.get("noop"))
        for t in browser.rpa_trail
    )


def test_strip_normalised_comparison() -> None:
    """A trailing newline or whitespace shouldn't defeat the no-op check —
    that would mean we re-type even when functionally equivalent."""
    ctx, browser, handle, _ = _build_ctx(
        current_value="  介绍一下deepseek  \n",  # extra whitespace
        target_text="介绍一下deepseek",
    )
    _run(TypeHandler().execute(ctx))
    assert browser.rpa_trail
    assert browser.rpa_trail[-1].get("noop") is True


def test_empty_target_does_not_trigger_noop() -> None:
    """If user wants to type empty string (clear field), don't shortcut —
    even if current value is also empty, this is a clear-field action that
    might have side effects (e.g. unfocus)."""
    ctx, browser, handle, _ = _build_ctx(
        current_value="",
        target_text="",
    )
    _run(TypeHandler().execute(ctx))
    # No noop entry recorded (handler proceeded with regular type)
    assert all(
        not (isinstance(t, dict) and t.get("noop"))
        for t in browser.rpa_trail
    )


def test_eval_failure_falls_through_safely() -> None:
    """If the value-read evaluate raises, the precheck must swallow the
    error and proceed with normal type (defence in depth)."""
    handle = _FakeHandle(current_value="介绍一下deepseek")

    async def crashing_eval(js, *_a, **_kw):
        # viewport check OK, but the no-op value read crashes
        if "getBoundingClientRect" in js and "fully_visible" in js:
            return {
                "top": 100, "bottom": 130, "left": 0, "right": 200,
                "height": 30, "width": 200, "vh": 800, "vw": 1024,
                "fully_visible": True, "partial_clip": 0,
            }
        if "el.value" in js:
            raise RuntimeError("CDP gone")
        return None

    handle.evaluate = AsyncMock(side_effect=crashing_eval)
    target = _FakeTarget(handle)

    browser = SimpleNamespace()
    browser._LOCATOR_TIMEOUT = 5000
    browser._last_action_error = None
    browser._tab_switch_notice = None
    install_notice_stub(browser)
    browser.rpa_trail = []
    browser._clear_som_overlays = AsyncMock()
    browser._resolve_action_target = AsyncMock(return_value=target)
    browser._get_xpath = AsyncMock(return_value="//input")
    browser._get_accessibility_signature = AsyncMock(return_value=("textbox", "ask"))
    browser._count_interactive_elements = AsyncMock(return_value=10)
    browser._wait_after_action = AsyncMock()

    page = SimpleNamespace(url="https://example.com/", evaluate=AsyncMock())
    action = SimpleNamespace(
        action="type", target_id=8, type_value="介绍一下deepseek", memory_key="",
    )
    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = page
    ctx.workflow_memory = {}
    ctx.action = action
    ctx.with_rpa_meta = lambda d: d

    # Should NOT raise even though no-op precheck eval crashed
    _run(TypeHandler().execute(ctx))

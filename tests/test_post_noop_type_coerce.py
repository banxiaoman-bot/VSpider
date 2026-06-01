"""Regression: after a TYPE NO-OP fires, a duplicate type in the next
step must be hard-rewritten to ``press_key Enter``.

Failure that motivated this (run_log_20260518_145655):
  step 3: type @e11 "介绍一下deepseek" → TYPE NO-OP short-circuit (the input
          already had the text from step 1).
  step 4: VLM thought ACKNOWLEDGES "无需重复输入，下一步应按 Enter" but
          its JSON output is type @e11 "介绍一下deepseek" AGAIN (classic
          thought-action divergence).
  Soft hint via ``_tab_switch_notice`` wasn't strong enough — VLM ignored
  it. step 5 finally emitted Enter, but a step was wasted.

The fix:
  - TypeHandler's no-op path arms ``browser._last_type_noop`` with the
    target_id + value it just refused.
  - main.py's pre-dispatch guard chain checks for this arm. If the new
    head decision is ANOTHER type with the same value, it gets hard-
    rewritten to ``press_key Enter`` before dispatch.
  - The arm is consumed (cleared) every step so it never persists into
    unrelated future actions.

This test pins the handler-side arming behaviour. The guard-side
rewrite is verified by manual runs since the guard chain depends on
the full main-loop state.
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
    def __init__(self, *, current_value: str = "", visible: bool = True) -> None:
        self.current_value = current_value
        self._visible = visible
        self.scroll_into_view_if_needed = AsyncMock()
        self.click = AsyncMock()
        self.evaluate = AsyncMock(side_effect=self._route_eval)
        self.fill = AsyncMock()
        self.type = AsyncMock()
        self.press = AsyncMock()

    async def _route_eval(self, js: str, *args, **kwargs):
        if "getBoundingClientRect" in js and "fully_visible" in js:
            return {
                "top": 100, "bottom": 130, "left": 0, "right": 200,
                "height": 30, "width": 200, "vh": 800, "vw": 1024,
                "fully_visible": True, "partial_clip": 0,
            }
        if "el.value" in js and ("innerText" in js or "textContent" in js):
            return self.current_value
        return None


class _FakeTarget:
    def __init__(self, handle: _FakeHandle) -> None:
        self.handle = handle


def _build_ctx(*, current_value: str, target_text: str, target_id: int = 11):
    handle = _FakeHandle(current_value=current_value)
    target = _FakeTarget(handle)

    browser = SimpleNamespace()
    browser._LOCATOR_TIMEOUT = 5000
    browser._last_action_error = None
    browser._tab_switch_notice = None
    install_notice_stub(browser)
    browser._last_type_noop = None
    browser.rpa_trail = []
    browser._clear_som_overlays = AsyncMock()
    browser._resolve_action_target = AsyncMock(return_value=target)
    browser._get_xpath = AsyncMock(return_value="//input")
    browser._get_accessibility_signature = AsyncMock(return_value=("textbox", "ask"))
    browser._count_interactive_elements = AsyncMock(return_value=10)
    browser._wait_after_action = AsyncMock()

    page = SimpleNamespace(url="https://example.com/", evaluate=AsyncMock())
    action = SimpleNamespace(
        action="type", target_id=target_id, type_value=target_text, memory_key="",
    )
    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = page
    ctx.workflow_memory = {}
    ctx.action = action
    ctx.with_rpa_meta = lambda d: d
    return ctx, browser, handle


# ── No-op arms the coercion flag ────────────────────────────────────────────


def test_noop_arms_last_type_noop_flag() -> None:
    """When TYPE NO-OP fires, browser._last_type_noop must record what
    was refused so the next step's guard can hard-rewrite a duplicate."""
    ctx, browser, _ = _build_ctx(
        current_value="介绍一下deepseek",
        target_text="介绍一下deepseek",
        target_id=11,
    )
    _run(TypeHandler().execute(ctx))

    arm = browser._last_type_noop
    assert arm is not None
    assert arm["target_id"] == 11
    assert arm["value"] == "介绍一下deepseek"


def test_noop_arm_uses_normalised_value() -> None:
    """The arm should store the stripped value so the guard's comparison
    is whitespace-tolerant."""
    ctx, browser, _ = _build_ctx(
        current_value="  介绍一下deepseek  \n",
        target_text="介绍一下deepseek",
    )
    _run(TypeHandler().execute(ctx))
    assert browser._last_type_noop["value"] == "介绍一下deepseek"


def test_real_type_does_not_arm_flag() -> None:
    """A real (non-noop) type must NOT set the arm — otherwise normal
    typing would be misinterpreted as duplicate-detection state."""
    ctx, browser, _ = _build_ctx(
        current_value="",  # empty, real type proceeds
        target_text="hello",
    )
    _run(TypeHandler().execute(ctx))
    # Arm stays untouched (None or absent)
    assert getattr(browser, "_last_type_noop", None) in (None, {})


def test_partial_match_does_not_arm_flag() -> None:
    ctx, browser, _ = _build_ctx(
        current_value="介绍一下",
        target_text="介绍一下deepseek",
    )
    _run(TypeHandler().execute(ctx))
    assert getattr(browser, "_last_type_noop", None) in (None, {})


def test_arm_contains_int_target_id() -> None:
    """target_id must be int (the guard compares against int(_h.get('target_id') or 0))."""
    ctx, browser, _ = _build_ctx(
        current_value="x", target_text="x", target_id=42,
    )
    _run(TypeHandler().execute(ctx))
    arm = browser._last_type_noop
    assert isinstance(arm["target_id"], int)
    assert arm["target_id"] == 42


def test_notice_mentions_auto_coerce_warning() -> None:
    """The user-visible notice should warn that a duplicate type will be
    hard-rewritten next step — so when it happens it's not a surprise."""
    ctx, browser, _ = _build_ctx(
        current_value="hi", target_text="hi",
    )
    _run(TypeHandler().execute(ctx))
    notice = browser._tab_switch_notice or ""
    # Notice should reference Enter (the auto-rewrite target) and
    # explicitly warn about consequence
    assert "Enter" in notice
    assert "重复" in notice or "duplicate" in notice.lower()

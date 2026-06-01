"""Unit tests for ClickNewTabHandler's origin-pinning behavior.

Contract: after middle-click opens a new background tab, the handler must
restore focus to the origin page (ctx.page) and return it, so the dispatcher
skips Tab Guard. This avoids the failure mode where a click_new_tab onto a
disguised SEM ad lands on a heavy SPA whose screenshot deadlocks on font
loading, taking the whole agent down with it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from visual_web_agent.actions import ClickNewTabHandler

# J: stub set_tab_notice/clear_tab_notice helpers
from _notice_stub import install_notice_stub


class _FakePage:
    """Minimal Page double — enough surface for the handler's checks."""

    def __init__(self, url: str = "https://www.baidu.com/s?wd=x", closed: bool = False) -> None:
        self.url = url
        self._closed = closed
        # evaluate is called for href fallback path
        self.evaluate = AsyncMock(return_value=None)

    def is_closed(self) -> bool:
        return self._closed


class _FakeTargetHandle:
    """Element-handle double for middle-click."""

    def __init__(self) -> None:
        self.click = AsyncMock()
        self.scroll_into_view_if_needed = AsyncMock()
        self.evaluate = AsyncMock(return_value="https://example.com/dest")


class _FakeTarget:
    def __init__(self) -> None:
        self.handle = _FakeTargetHandle()


class _FakeContext:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages


def _build_ctx(page: _FakePage, browser_page_after_popup: _FakePage, *, new_tab_appears: bool = True):
    """Construct an ActionContext-like object + browser double.

    `browser_page_after_popup` simulates the state of `browser._page` AFTER
    the popup listener fires. The handler sees this and must restore.

    `new_tab_appears=True` (default) simulates a successful middle-click that
    opened a new background tab. Set False to simulate a no-op (middle-click
    on a non-link element).
    """
    browser = SimpleNamespace()
    browser._page = page  # initially on origin
    browser._LOCATOR_TIMEOUT = 5000
    browser._last_action_error = None
    browser.rpa_trail = []
    browser._tab_switch_notice = None
    install_notice_stub(browser)
    browser._tab_visit_stack: list = []

    # Context starts with origin only; new tab gets appended during the click
    browser._context = _FakeContext([page])

    browser._clear_som_overlays = AsyncMock()
    browser._resolve_action_target = AsyncMock(return_value=_FakeTarget())
    browser._get_xpath = AsyncMock(return_value="/html/body/a")
    browser._get_accessibility_signature = AsyncMock(return_value=("link", "result"))

    # Push/pop stack hooks
    def _push(p):
        if p is None or p.is_closed():
            return
        if browser._tab_visit_stack and browser._tab_visit_stack[-1] is p:
            return
        browser._tab_visit_stack.append(p)
    browser.push_tab_visit = _push
    browser.pop_tab_visit = lambda: None

    # Simulate: by the time _wait_after_action returns, popup listener has
    # already (a) appended new tab to context.pages, (b) swapped browser._page.
    async def _wait_then_swap(*_args, **_kwargs):
        if new_tab_appears and browser_page_after_popup is not page:
            browser._context.pages.append(browser_page_after_popup)
        browser._page = browser_page_after_popup
    browser._wait_after_action = AsyncMock(side_effect=_wait_then_swap)

    # Track restore calls so the test can assert intent.
    activate_calls: list[dict] = []

    async def _activate_page(target_page, reason: str = ""):
        activate_calls.append({"target": target_page, "reason": reason})
        browser._page = target_page

    browser._activate_page = _activate_page

    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = page
    ctx.action = SimpleNamespace(target_id=27, action="click_new_tab", type_value="")
    ctx.with_rpa_meta = lambda d: d
    return ctx, browser, activate_calls


def _run(coro):
    return asyncio.run(coro)


# ── Origin-pin happy path ─────────────────────────────────────────────────────
def test_returns_origin_page_when_alive() -> None:
    """Core contract: handler must return ctx.page (origin) to skip Tab Guard."""
    origin = _FakePage("https://www.baidu.com/s?wd=x")
    new_tab = _FakePage("https://comate.baidu.com/zh?track=SEM")
    ctx, browser, activate_calls = _build_ctx(origin, browser_page_after_popup=new_tab)

    result = _run(ClickNewTabHandler().execute(ctx))

    # Handler returns origin → dispatcher will skip Tab Guard's L3 follow
    assert result is origin


def test_restores_focus_to_origin_when_popup_stole_it() -> None:
    """The real failure mode: popup listener flipped browser._page to the
    new tab. Handler must restore so subsequent screenshot/AX-tree calls
    operate on the origin (where VLM expects to be)."""
    origin = _FakePage("https://www.baidu.com/s?wd=x")
    new_tab = _FakePage("https://comate.baidu.com/zh?track=SEM")
    ctx, browser, activate_calls = _build_ctx(origin, browser_page_after_popup=new_tab)

    _run(ClickNewTabHandler().execute(ctx))

    # _activate_page was called with the origin page to restore focus
    assert len(activate_calls) == 1
    assert activate_calls[0]["target"] is origin
    assert "click_new_tab" in activate_calls[0]["reason"]
    # And the final browser._page reflects the restore
    assert browser._page is origin


def test_no_redundant_restore_when_already_on_origin() -> None:
    """If the popup listener didn't (yet) swap browser._page, no restore needed."""
    origin = _FakePage("https://www.baidu.com/s?wd=x")
    ctx, browser, activate_calls = _build_ctx(origin, browser_page_after_popup=origin)

    result = _run(ClickNewTabHandler().execute(ctx))

    assert result is origin
    assert activate_calls == []  # no restore call


# ── Edge: origin closed mid-action ────────────────────────────────────────────
def test_returns_none_when_origin_closed() -> None:
    """Some links close their parent window after opening the new tab (rare,
    but happens on ad networks). Handler returns None so Tab Guard can fall
    back to a sibling tab."""
    origin = _FakePage("https://www.baidu.com/s?wd=x", closed=True)
    new_tab = _FakePage("https://comate.baidu.com/zh?track=SEM")
    ctx, browser, activate_calls = _build_ctx(origin, browser_page_after_popup=new_tab)

    result = _run(ClickNewTabHandler().execute(ctx))

    assert result is None  # Tab Guard handles
    assert activate_calls == []  # no restore on dead origin


def test_returns_none_when_activate_raises() -> None:
    """If restore itself errors (very rare — origin in mid-close race),
    fail-open to Tab Guard rather than propagate."""
    origin = _FakePage("https://www.baidu.com/s?wd=x")
    new_tab = _FakePage("https://comate.baidu.com/zh?track=SEM")
    ctx, browser, activate_calls = _build_ctx(origin, browser_page_after_popup=new_tab)

    async def _activate_raises(*_args, **_kwargs):
        raise RuntimeError("origin transitioning")
    ctx.browser._activate_page = _activate_raises

    result = _run(ClickNewTabHandler().execute(ctx))
    assert result is None


# ── RPA trail entry recorded regardless of restore outcome ────────────────────
def test_records_rpa_trail_entry() -> None:
    origin = _FakePage("https://www.baidu.com/s?wd=x")
    new_tab = _FakePage("https://comate.baidu.com/zh?track=SEM")
    ctx, browser, _ = _build_ctx(origin, browser_page_after_popup=new_tab)
    _run(ClickNewTabHandler().execute(ctx))
    assert len(browser.rpa_trail) == 1
    assert browser.rpa_trail[0]["action"] == "click_new_tab"


# ── _tab_switch_notice: success path ──────────────────────────────────────────
def test_success_notice_injected_when_new_tab_opened() -> None:
    """The bug from the Playwright/Baidu nested-tab run: focus stays on
    origin so VLM can't tell from the screenshot that click_new_tab worked;
    it retries 14 times. The success notice must explicitly confirm + tell
    VLM the new tab index + remind it focus is intentional."""
    origin = _FakePage("https://www.baidu.com/s?wd=Playwright")
    new_tab = _FakePage("https://baike.baidu.com/item/playwright")
    ctx, browser, _ = _build_ctx(origin, browser_page_after_popup=new_tab)
    _run(ClickNewTabHandler().execute(ctx))

    assert browser._tab_switch_notice is not None
    msg = browser._tab_switch_notice
    # Confirms success explicitly
    assert "成功" in msg
    # New tab index surfaced (it's at index 1 — pages = [origin, new])
    assert "[1]" in msg
    # Explains why screenshot still shows origin
    assert "焦点" in msg and "原页" in msg
    # Tells VLM "don't retry"
    assert "不要" in msg and "无限" in msg
    # Provides next-step guidance with the actual index
    assert "switch_tab" in msg
    assert 'type_value="1"' in msg


def test_success_notice_includes_origin_index() -> None:
    """Multi-tab scenario: origin might be tab [2], not [0]. Notice must
    reference whatever the current origin index actually is."""
    earlier_tab_a = _FakePage("https://a.example")
    earlier_tab_b = _FakePage("https://b.example")
    origin = _FakePage("https://origin.example")
    new_tab = _FakePage("https://new.example")
    ctx, browser, _ = _build_ctx(origin, browser_page_after_popup=new_tab)
    # Inject pre-existing tabs so origin sits at index 2
    browser._context.pages = [earlier_tab_a, earlier_tab_b, origin]

    _run(ClickNewTabHandler().execute(ctx))

    msg = browser._tab_switch_notice
    assert msg is not None
    # Origin is at [2] now, new tab appended at [3]
    assert "[2]" in msg
    assert "[3]" in msg


# ── _tab_switch_notice: no-op path ────────────────────────────────────────────
def test_noop_notice_when_middle_click_did_not_open_new_tab() -> None:
    """Middle-click on a non-link element (e.g. a button) doesn't open a
    new tab. We must NOT push to the visit stack (false parent) AND we
    must tell VLM "this element isn't a link" so it stops retrying."""
    origin = _FakePage("https://example.com")
    ctx, browser, _ = _build_ctx(origin, browser_page_after_popup=origin, new_tab_appears=False)
    _run(ClickNewTabHandler().execute(ctx))

    # Notice fires with failure / suggestion text
    msg = browser._tab_switch_notice
    assert msg is not None
    assert "未能" in msg or "❌" in msg
    assert "不是链接" in msg or "non-link" in msg or "拦截" in msg
    # Stack must NOT be polluted with a fake parent
    assert browser._tab_visit_stack == []


def test_stack_pushed_only_when_new_tab_actually_opened() -> None:
    """Belt-and-suspenders for the no-op case: confirms the stack stays
    clean when nothing happened."""
    origin = _FakePage("https://example.com")
    ctx, browser, _ = _build_ctx(origin, browser_page_after_popup=origin, new_tab_appears=False)
    _run(ClickNewTabHandler().execute(ctx))
    assert browser._tab_visit_stack == []


def test_stack_pushed_when_new_tab_opened() -> None:
    origin = _FakePage("https://origin.example")
    new_tab = _FakePage("https://child.example")
    ctx, browser, _ = _build_ctx(origin, browser_page_after_popup=new_tab)
    _run(ClickNewTabHandler().execute(ctx))
    assert browser._tab_visit_stack == [origin]

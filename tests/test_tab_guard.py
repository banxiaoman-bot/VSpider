"""Unit tests for ensure_active_page (Tab Guard L1/L2/L3).

The key contract change: L3 must only fire when the latest page is GENUINELY
new (opened during the current action), not just because an old tab from a
previous step happens to be the last in context.pages.

Without this discipline, every wait/scroll/type after a click_new_tab would
silently re-focus the new tab, undoing any explicit switch_tab back to the
origin.
"""

from __future__ import annotations

import asyncio

from visual_web_agent.browser_env import BrowserEnv, ensure_active_page


class _FakePage:
    def __init__(self, url: str = "about:blank", closed: bool = False) -> None:
        self.url = url
        self._closed = closed
        self.bring_count = 0
        self.load_states_waited: list[str] = []

    def is_closed(self) -> bool:
        return self._closed

    async def bring_to_front(self) -> None:
        self.bring_count += 1

    async def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        self.load_states_waited.append(state)


class _FakeContext:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages
        self.new_page_called = 0

    async def new_page(self) -> _FakePage:
        self.new_page_called += 1
        p = _FakePage(url="about:blank")
        self.pages.append(p)
        return p


def _run(coro):
    return asyncio.run(coro)


# ── L1: all pages gone ────────────────────────────────────────────────────────
def test_l1_creates_blank_when_no_pages_open() -> None:
    bing = _FakePage("https://bing.com", closed=True)
    ctx = _FakeContext([bing])  # all closed
    # context.pages filter will produce []
    result = _run(ensure_active_page(ctx, bing))
    assert ctx.new_page_called == 1
    assert result is not bing


# ── L2: current page closed ───────────────────────────────────────────────────
def test_l2_fallback_when_current_page_closed() -> None:
    bing = _FakePage("https://bing.com")
    aidaxue = _FakePage("https://aidaxue.com")
    ctx = _FakeContext([bing, aidaxue])
    bing._closed = True  # current page got closed mid-action
    result = _run(ensure_active_page(ctx, bing))
    # Falls back to the last non-closed page (aidaxue)
    assert result is aidaxue
    assert aidaxue.bring_count == 1


# ── L3: follow new tab, but NOT pre-existing tab ──────────────────────────────
def test_l3_follows_when_truly_new_tab_opened() -> None:
    """Action just opened a new tab — L3 should follow it."""
    bing = _FakePage("https://bing.com")
    ctx = _FakeContext([bing])
    known = {id(bing)}
    # Simulate: action opened aidaxue tab
    aidaxue = _FakePage("https://aidaxue.com")
    ctx.pages.append(aidaxue)
    result = _run(ensure_active_page(ctx, bing, known_pages=known))
    assert result is aidaxue, "should follow the newly-opened tab"
    assert aidaxue.bring_count == 1


def test_l3_does_not_follow_pre_existing_tab() -> None:
    """The Bing-multi-tab failure mode: aidaxue tab was opened in step 3 by a
    click_new_tab. Step 4 switch_tab(0) brought focus back to bing. Step 5
    wait runs; current_page=bing. context.pages = [bing, aidaxue], latest
    by index = aidaxue. WITHOUT known_pages tracking, L3 follows aidaxue
    and undoes the switch_tab. WITH known_pages, aidaxue is recognized as
    pre-existing → L3 stands down → bing stays active."""
    bing = _FakePage("https://bing.com")
    aidaxue = _FakePage("https://aidaxue.com")
    ctx = _FakeContext([bing, aidaxue])
    # aidaxue existed BEFORE this action started
    known = {id(bing), id(aidaxue)}
    result = _run(ensure_active_page(ctx, bing, known_pages=known))
    assert result is bing, "must not steal focus to a pre-existing tab"
    assert aidaxue.bring_count == 0, "pre-existing tab should not be brought to front"


def test_l3_legacy_behavior_when_known_pages_none() -> None:
    """Back-compat: callers that don't pass known_pages get the old
    always-follow-latest behavior, so existing call sites that haven't
    migrated don't suddenly stop working."""
    bing = _FakePage("https://bing.com")
    aidaxue = _FakePage("https://aidaxue.com")
    ctx = _FakeContext([bing, aidaxue])
    result = _run(ensure_active_page(ctx, bing))
    assert result is aidaxue


def test_l3_no_change_when_current_is_already_latest() -> None:
    """If current_page is already the latest, no follow / no bring_to_front."""
    bing = _FakePage("https://bing.com")
    ctx = _FakeContext([bing])
    result = _run(ensure_active_page(ctx, bing, known_pages={id(bing)}))
    assert result is bing
    assert bing.bring_count == 0


def test_l3_follows_brand_new_third_tab() -> None:
    """If a NEW (3rd) tab opens while we already had 2, follow the new one."""
    bing = _FakePage("https://bing.com")
    aidaxue = _FakePage("https://aidaxue.com")  # pre-existing
    promo = _FakePage("https://promo.com")  # just opened
    ctx = _FakeContext([bing, aidaxue, promo])
    known = {id(bing), id(aidaxue)}  # promo not in known
    result = _run(ensure_active_page(ctx, bing, known_pages=known))
    assert result is promo, "should follow the genuinely new tab"


def test_browser_env_instance_ensure_creates_blank_when_context_pages_empty() -> None:
    env = BrowserEnv.__new__(BrowserEnv)
    env._context = _FakeContext([])
    env._page = None

    async def _register_page(page, reason="", activate=True):
        if activate:
            env._page = page

    env._register_page = _register_page

    result = _run(env._ensure_active_page(reason="unit test"))

    assert env._context.new_page_called == 1
    assert result is env._page
    assert result.url == "about:blank"

"""Unit tests for BrowserEnv's Tab Visit Stack.

Contract:
- ``push_tab_visit(parent)`` records the page we came from when a child tab opens.
- ``pop_tab_visit()`` returns the most recent ALIVE parent (skips closed
  ones), so close_tab can return focus to where the user was before opening
  the now-closed tab.
- ``peek_tab_visit()`` returns the same without mutating the stack —
  used by get_tabs_state to show VLM the return path.
- ``clear_tab_visit_stack()`` resets state at task boundaries.

This is the structural backbone of nested tab navigation:
  origin → click_new_tab → A → click_new_tab(on A) → B → close → A → close → origin
"""

from __future__ import annotations

import asyncio

from visual_web_agent.browser_env import BrowserEnv


class _FakePage:
    def __init__(self, url: str = "about:blank", closed: bool = False) -> None:
        self.url = url
        self._closed = closed
        self.bring_count = 0
        self._title = url

    def is_closed(self) -> bool:
        return self._closed

    async def title(self) -> str:
        return self._title

    async def bring_to_front(self) -> None:
        self.bring_count += 1


def _new_env() -> BrowserEnv:
    """Construct a bare BrowserEnv without going through __init__ side effects."""
    env = BrowserEnv.__new__(BrowserEnv)
    env._tab_visit_stack = []
    env._page = None
    env._context = None
    return env


def _run(coro):
    return asyncio.run(coro)


# ── push / pop core behavior ───────────────────────────────────────────────────
def test_push_then_pop_returns_parent() -> None:
    env = _new_env()
    origin = _FakePage("https://origin.example")
    env.push_tab_visit(origin)
    assert env.pop_tab_visit() is origin
    assert env._tab_visit_stack == []  # popped


def test_nested_lifo_order() -> None:
    """origin → A → B; closing B returns A, then closing A returns origin."""
    env = _new_env()
    origin = _FakePage("https://origin.example")
    a = _FakePage("https://a.example")

    env.push_tab_visit(origin)  # opened A from origin
    env.push_tab_visit(a)       # opened B from A

    assert env.pop_tab_visit() is a       # close B → back to A
    assert env.pop_tab_visit() is origin  # close A → back to origin
    assert env.pop_tab_visit() is None    # nothing left


def test_pop_skips_dead_parents() -> None:
    """If a parent got closed underneath us (e.g. an OAuth window closed
    itself), pop should skip it rather than return a dead reference."""
    env = _new_env()
    dead = _FakePage("https://dead.example", closed=True)
    alive = _FakePage("https://alive.example")

    env.push_tab_visit(alive)
    env.push_tab_visit(dead)  # parent died after being pushed

    assert env.pop_tab_visit() is alive
    assert env._tab_visit_stack == []  # dead entry was discarded


def test_pop_returns_none_when_all_dead() -> None:
    env = _new_env()
    env.push_tab_visit(_FakePage(closed=True))
    env.push_tab_visit(_FakePage(closed=True))
    assert env.pop_tab_visit() is None


def test_push_rejects_none_and_closed() -> None:
    env = _new_env()
    env.push_tab_visit(None)  # type: ignore[arg-type]
    env.push_tab_visit(_FakePage(closed=True))
    assert env._tab_visit_stack == []


def test_push_dedup_consecutive_same_parent() -> None:
    """Two consecutive click_new_tab on the same origin should not push
    twice — would otherwise pollute the stack."""
    env = _new_env()
    origin = _FakePage("https://origin.example")
    env.push_tab_visit(origin)
    env.push_tab_visit(origin)
    assert len(env._tab_visit_stack) == 1


def test_peek_does_not_mutate() -> None:
    env = _new_env()
    origin = _FakePage("https://origin.example")
    env.push_tab_visit(origin)
    assert env.peek_tab_visit() is origin
    assert env.peek_tab_visit() is origin
    assert len(env._tab_visit_stack) == 1


def test_peek_skips_dead_but_keeps_them() -> None:
    """peek should return the most recent alive entry without cleaning dead
    ones (those get cleaned on pop, which has clear ownership)."""
    env = _new_env()
    alive = _FakePage("https://alive.example")
    dead = _FakePage(closed=True)
    env.push_tab_visit(alive)
    env.push_tab_visit(dead)  # gets rejected (push checks closed)
    # Manually inject a page that becomes dead after push
    env._tab_visit_stack.append(_FakePage(closed=True))
    assert env.peek_tab_visit() is alive
    # Stack length unchanged by peek
    assert len(env._tab_visit_stack) == 2


def test_clear_resets() -> None:
    env = _new_env()
    env.push_tab_visit(_FakePage("https://a"))
    env.push_tab_visit(_FakePage("https://b"))
    env.clear_tab_visit_stack()
    assert env._tab_visit_stack == []


# ── get_tabs_state stack annotation ───────────────────────────────────────────
class _FakeContext:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages


def _env_with_context(pages: list[_FakePage], active: _FakePage) -> BrowserEnv:
    env = _new_env()
    env._context = _FakeContext(pages)
    env._page = active
    return env


def test_get_tabs_state_appends_return_path_when_stack_present() -> None:
    origin = _FakePage("https://origin.example")
    origin._title = "Origin Site"
    child = _FakePage("https://child.example")
    child._title = "Child Doc"
    env = _env_with_context([origin, child], active=child)
    env.push_tab_visit(origin)

    out = _run(env.get_tabs_state())
    assert "[0] Origin Site" in out
    assert "[1] Child Doc (活跃)" in out
    # Stack hint included
    assert "close_tab 将回到 [0] Origin Site" in out


def test_get_tabs_state_no_hint_when_already_on_parent() -> None:
    """If VLM already navigated back to the parent (active == stack top),
    the hint is suppressed — it would just say "close will return to me"."""
    origin = _FakePage("https://origin.example")
    child = _FakePage("https://child.example", closed=True)
    env = _env_with_context([origin], active=origin)
    env.push_tab_visit(origin)  # parent of the now-closed child

    out = _run(env.get_tabs_state())
    assert "close_tab 将回到" not in out


def test_get_tabs_state_no_hint_when_stack_empty() -> None:
    p = _FakePage("https://x.example")
    env = _env_with_context([p], active=p)
    out = _run(env.get_tabs_state())
    assert "close_tab 将回到" not in out


def test_get_tabs_state_skips_stale_parent_not_in_pages() -> None:
    """Parent might have been closed AND removed from context.pages; in that
    case the hint must not lie about a tab index that no longer exists."""
    origin = _FakePage("https://origin.example")
    child = _FakePage("https://child.example")
    env = _env_with_context([child], active=child)  # origin no longer in pages
    env.push_tab_visit(origin)

    out = _run(env.get_tabs_state())
    assert "close_tab 将回到" not in out

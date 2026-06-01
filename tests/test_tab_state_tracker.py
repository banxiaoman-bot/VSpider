"""Tab state delta tracker: forces VLM to read the new tab list when it changes.

Failure mode covered: VLM keeps emitting ``switch_tab(1)`` even after tab 1
got closed because its mental model is frozen at an earlier snapshot. The
tracker re-pushes the ground truth as a tab_switch_notice the moment any
open/close/focus drift occurs.
"""

from __future__ import annotations

import pytest

from visual_web_agent.tab_state_tracker import (
    TabSnapshot,
    _TabRow,
    compute_tab_delta_notice,
    format_tab_delta,
    snapshot_tabs,
)


class _FakePage:
    def __init__(self, url: str = "about:blank", closed: bool = False) -> None:
        self.url = url
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed


class _FakeContext:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages


class _FakeBrowser:
    def __init__(self, pages: list[_FakePage], active_idx: int | None) -> None:
        self._context = _FakeContext(pages)
        self._page = pages[active_idx] if active_idx is not None and 0 <= active_idx < len(pages) else None
        self._tab_switch_notice: str | None = None


# ── snapshot ──────────────────────────────────────────────────────────────────
def test_snapshot_empty_browser_returns_empty() -> None:
    browser = _FakeBrowser([], active_idx=None)
    snap = snapshot_tabs(browser)
    assert snap.rows == ()
    assert snap.active_id is None


def test_snapshot_filters_closed_pages() -> None:
    p0 = _FakePage("https://a.example")
    p1 = _FakePage("https://b.example", closed=True)
    p2 = _FakePage("https://c.example")
    browser = _FakeBrowser([p0, p1, p2], active_idx=0)
    snap = snapshot_tabs(browser)
    assert len(snap.rows) == 2
    assert {r.url for r in snap.rows} == {"https://a.example", "https://c.example"}


def test_snapshot_marks_active() -> None:
    p0 = _FakePage("https://a.example")
    p1 = _FakePage("https://b.example")
    browser = _FakeBrowser([p0, p1], active_idx=1)
    snap = snapshot_tabs(browser)
    assert snap.active_id == id(p1)
    actives = [r for r in snap.rows if r.is_active]
    assert len(actives) == 1
    assert actives[0].url == "https://b.example"


def test_snapshot_handles_no_context() -> None:
    browser = type("X", (), {"_context": None, "_page": None})()
    snap = snapshot_tabs(browser)
    assert snap.rows == ()


# ── format_tab_delta: no change → None ────────────────────────────────────────
def test_no_change_returns_none() -> None:
    p0 = _FakePage("https://a.example")
    p1 = _FakePage("https://b.example")
    rows = (
        _TabRow(page_id=id(p0), url=p0.url, title="a", is_active=True),
        _TabRow(page_id=id(p1), url=p1.url, title="b", is_active=False),
    )
    snap = TabSnapshot(rows=rows)
    assert format_tab_delta(snap, snap) is None


def test_reindex_only_returns_none() -> None:
    """Closing tab 0 shifts tab 1 → index 0. Page id set is unchanged for
    the remaining tab, active unchanged → suppress notice (no real change)."""
    # Note: this scenario requires tab 0 to actually be removed, so it's
    # really a "closed" event. Pure reindexing without close/open can't
    # happen in a normal browser context.
    p_old = _FakePage("https://gone.example")
    p_stay = _FakePage("https://stay.example")
    before = TabSnapshot(rows=(
        _TabRow(page_id=id(p_old), url=p_old.url, title="x", is_active=False),
        _TabRow(page_id=id(p_stay), url=p_stay.url, title="y", is_active=True),
    ))
    after = TabSnapshot(rows=(
        _TabRow(page_id=id(p_stay), url=p_stay.url, title="y", is_active=True),
    ))
    msg = format_tab_delta(before, after)
    # Real close → notice fires (id_old in closed_ids)
    assert msg is not None
    assert "https://gone.example" in msg


# ── opened ────────────────────────────────────────────────────────────────────
def test_open_event_lists_new_tab() -> None:
    p0 = _FakePage("https://origin.example")
    p1 = _FakePage("https://new.example")
    before = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="o", is_active=True),
    ))
    after = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="o", is_active=True),
        _TabRow(page_id=id(p1), url=p1.url, title="n", is_active=False),
    ))
    msg = format_tab_delta(before, after, last_action="click_new_tab", last_target_id=16)
    assert msg is not None
    assert "新开" in msg or "✨" in msg
    assert "https://new.example" in msg
    assert "click_new_tab" in msg
    assert "target_id=16" in msg
    # Ground truth list of CURRENT tabs always appended
    assert "现在共 2 个 tab" in msg


# ── closed ────────────────────────────────────────────────────────────────────
def test_close_event_lists_closed_tab() -> None:
    """The exact failure from run_log_20260514_103433: tab 1 got closed but
    VLM kept referencing it. Notice must enumerate which id+url is gone."""
    p0 = _FakePage("https://bing.example")
    p1 = _FakePage("https://runoob.example")
    before = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="bing", is_active=True),
        _TabRow(page_id=id(p1), url=p1.url, title="runoob", is_active=False),
    ))
    after = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="bing", is_active=True),
    ))
    msg = format_tab_delta(before, after, last_action="close_tab", last_target_id=0)
    assert msg is not None
    assert "关闭" in msg or "❌" in msg
    assert "https://runoob.example" in msg
    # The current truth must be re-stated
    assert "现在共 1 个 tab" in msg
    # And the warning that VLM's old indices may be wrong
    assert "已失效" in msg or "重新规划" in msg


def test_close_and_open_event_lists_both() -> None:
    p0 = _FakePage("https://origin.example")
    gone = _FakePage("https://gone.example")
    new = _FakePage("https://fresh.example")
    before = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="o", is_active=True),
        _TabRow(page_id=id(gone), url=gone.url, title="g", is_active=False),
    ))
    after = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="o", is_active=True),
        _TabRow(page_id=id(new), url=new.url, title="n", is_active=False),
    ))
    msg = format_tab_delta(before, after)
    assert msg is not None
    assert "https://gone.example" in msg
    assert "https://fresh.example" in msg


# ── focus drift only (no count change) ────────────────────────────────────────
def test_focus_drift_alone_triggers_notice() -> None:
    """Same set of tabs, but active page moved — VLM should know."""
    p0 = _FakePage("https://a.example")
    p1 = _FakePage("https://b.example")
    before = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="a", is_active=True),
        _TabRow(page_id=id(p1), url=p1.url, title="b", is_active=False),
    ))
    after = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="a", is_active=False),
        _TabRow(page_id=id(p1), url=p1.url, title="b", is_active=True),
    ))
    msg = format_tab_delta(before, after, last_action="switch_tab", last_target_id=1)
    assert msg is not None
    assert "焦点" in msg or "🎯" in msg
    assert "https://a.example" in msg and "https://b.example" in msg


# ── compute_tab_delta_notice helper ───────────────────────────────────────────
def test_compute_helper_returns_none_when_before_is_none() -> None:
    browser = _FakeBrowser([_FakePage("https://x")], active_idx=0)
    assert compute_tab_delta_notice(browser, before=None) is None


def test_compute_helper_returns_none_when_no_change() -> None:
    p0 = _FakePage("https://x.example")
    browser = _FakeBrowser([p0], active_idx=0)
    before = snapshot_tabs(browser)
    assert compute_tab_delta_notice(browser, before) is None


def test_compute_helper_picks_up_close() -> None:
    """Bing failure replay: snapshot before close, then close, then compute
    helper detects the close from current browser state."""
    p0 = _FakePage("https://origin.example")
    p1 = _FakePage("https://child.example")
    browser = _FakeBrowser([p0, p1], active_idx=0)
    before = snapshot_tabs(browser)
    # Now simulate close
    browser._context.pages = [p0]
    msg = compute_tab_delta_notice(
        browser, before, last_action="close_tab", last_target_id=0
    )
    assert msg is not None
    assert "https://child.example" in msg


# ── parametrized edge cases ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "action,tid",
    [("close_tab", 1), ("click_new_tab", 16), ("switch_tab", 0), ("", 0)],
)
def test_action_label_renders_safely_for_various_actions(action: str, tid: int) -> None:
    p0 = _FakePage("https://a.example")
    p1 = _FakePage("https://b.example")
    before = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="a", is_active=True),
    ))
    after = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="a", is_active=True),
        _TabRow(page_id=id(p1), url=p1.url, title="b", is_active=False),
    ))
    msg = format_tab_delta(before, after, last_action=action, last_target_id=tid)
    assert msg is not None
    # Action label always present even when empty (renders as "<unknown>")
    if action:
        assert action in msg
    else:
        assert "<unknown>" in msg

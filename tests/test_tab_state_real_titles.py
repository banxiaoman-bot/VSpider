"""Tab state tracker must surface real page titles, not URL guesses.

Failure mode (run_log_20260514_183718): VLM kept calling tab [1] "百度文心
助手 - 办公学习一站解决" because its thought-history wrote that label
once and never updated. The actual page at [1] had navigated to a rules
page but the trajectory's notice only showed URL — VLM had no easy way
to spot the mismatch.

Fix: ``browser._page_titles`` cache (populated by ``_register_page``'s
``domcontentloaded`` listener) is read by ``snapshot_tabs`` and surfaced
in the TAB STATE CHANGE notice.
"""

from __future__ import annotations

from visual_web_agent.tab_state_tracker import (
    TabSnapshot,
    _TabRow,
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
    def __init__(self, pages: list[_FakePage], active_idx: int | None = 0) -> None:
        self._context = _FakeContext(pages)
        self._page = (
            pages[active_idx]
            if active_idx is not None and 0 <= active_idx < len(pages)
            else None
        )
        self._page_titles: dict[int, str] = {}


def test_snapshot_uses_cached_title_when_available() -> None:
    """The fix: when the title cache has a real title, surface it."""
    p = _FakePage("https://yiyan.baidu.com/dialog/123")
    browser = _FakeBrowser([p])
    browser._page_titles[id(p)] = "文心一言 - 对话"
    snap = snapshot_tabs(browser)
    assert len(snap.rows) == 1
    assert snap.rows[0].title == "文心一言 - 对话"


def test_snapshot_falls_back_to_url_when_title_not_cached() -> None:
    """Title listeners haven't fired yet — fall back to URL tail
    (better than empty)."""
    p = _FakePage("https://yiyan.baidu.com/dialog/123")
    browser = _FakeBrowser([p])
    # _page_titles is empty
    snap = snapshot_tabs(browser)
    assert len(snap.rows) == 1
    # URL tail used as title
    assert "yiyan.baidu.com" in snap.rows[0].title


def test_snapshot_works_when_page_titles_attr_missing() -> None:
    """Defensive: an older BrowserEnv instance without the cache attr
    shouldn't crash the snapshot path."""
    p = _FakePage("https://x.example/")
    bare = type("X", (), {"_context": _FakeContext([p]), "_page": p})()
    snap = snapshot_tabs(bare)
    assert len(snap.rows) == 1
    # Falls back to URL tail
    assert snap.rows[0].title  # non-empty


def test_snapshot_truncates_long_titles() -> None:
    p = _FakePage("https://x.example/")
    browser = _FakeBrowser([p])
    browser._page_titles[id(p)] = "A" * 200
    snap = snapshot_tabs(browser)
    # Snapshot caps at 50 chars
    assert len(snap.rows[0].title) <= 50


# ── format_tab_delta now uses titles in messages ─────────────────────────────
def test_delta_message_includes_real_title_for_opened_tab() -> None:
    """When a new tab opens, the notice should mention its real title so
    VLM can disambiguate it from other tabs the page might have spawned."""
    old = _FakePage("https://origin.example/")
    new = _FakePage("https://yiyan.baidu.com/dialog/")
    before = TabSnapshot(rows=(
        _TabRow(page_id=id(old), url=old.url, title="origin", is_active=True),
    ))
    after = TabSnapshot(rows=(
        _TabRow(page_id=id(old), url=old.url, title="origin", is_active=True),
        _TabRow(page_id=id(new), url=new.url, title="文心一言 - 对话", is_active=False),
    ))
    msg = format_tab_delta(before, after, last_action="click_new_tab", last_target_id=9)
    assert msg is not None
    assert "文心一言 - 对话" in msg


def test_delta_message_includes_real_title_for_focus_change() -> None:
    """The 18:37 fail mode: VLM thought tab 1 was "Wenxin assistant" but
    it was actually "Baidu Terms of Service". With real titles in the
    feedback, VLM can spot the gap immediately."""
    p0 = _FakePage("https://search.example/")
    p1 = _FakePage("https://terms.example/")
    before = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="搜索结果", is_active=True),
        _TabRow(page_id=id(p1), url=p1.url, title="百度搜索使用规则", is_active=False),
    ))
    after = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="搜索结果", is_active=False),
        _TabRow(page_id=id(p1), url=p1.url, title="百度搜索使用规则", is_active=True),
    ))
    msg = format_tab_delta(before, after, last_action="switch_tab", last_target_id=1)
    assert msg is not None
    # CRUCIAL: real title visible so VLM realizes "this is rules, not chat"
    assert "百度搜索使用规则" in msg
    # NOT what the VLM might be assuming
    # (we test the title-fact is there; absence of misperceptions
    # is tested by absence of "百度文心助手" in the actual title.)


def test_delta_message_lists_current_tabs_with_titles() -> None:
    """Bottom of the notice always re-states the truth — titles included."""
    p0 = _FakePage("https://a.example/")
    p1 = _FakePage("https://b.example/")
    before = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="Page A", is_active=True),
    ))
    after = TabSnapshot(rows=(
        _TabRow(page_id=id(p0), url=p0.url, title="Page A", is_active=False),
        _TabRow(page_id=id(p1), url=p1.url, title="Page B", is_active=True),
    ))
    msg = format_tab_delta(before, after)
    assert msg is not None
    # Current tab list section includes both titles
    assert "Page A" in msg
    assert "Page B" in msg


def test_delta_message_handles_empty_title_gracefully() -> None:
    """Title cache empty for a tab (just opened, listener hasn't fired) —
    don't render bogus empty title; fall back to URL."""
    p = _FakePage("https://x.example/path")
    before = TabSnapshot(rows=())
    after = TabSnapshot(rows=(
        _TabRow(page_id=id(p), url=p.url, title="", is_active=True),
    ))
    msg = format_tab_delta(before, after, last_action="click_new_tab", last_target_id=5)
    assert msg is not None
    # URL still visible even when title missing
    assert "x.example" in msg

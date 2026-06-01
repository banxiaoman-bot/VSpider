"""Detect tab-state changes per action and tell the VLM what happened.

Failure mode addressed (run_log_20260514_103433): after a close_tab fired
silently, the VLM kept emitting ``switch_tab(1)`` for several steps because
its memorised tab list said tab 1 existed. Every subsequent step got
``标签页索引 1 超出范围`` errors but the VLM never updated its mental model.

This module produces a structured snapshot of tab state and a human-readable
delta that gets injected as ``browser._tab_switch_notice`` whenever a
non-trivial change occurred during the previous action. The VLM reads it as
top-of-prompt feedback (not buried in the system prompt), so the new ground
truth is unmissable.

Design goals:
  * **General**: no site/url heuristics — pure structural diff on Page refs.
  * **Cheap**: snapshot is ``[(page_id, url, title?, active?), ...]`` —
    O(N tabs) per action, well under 1 ms.
  * **Accurate**: tracks by ``id(Page)`` so close-induced index shifts don't
    register as fake "tab moved" events; only real opens/closes/focus drift
    surface.
  * **Non-clobbering**: yields ``None`` when handlers (e.g. ``click_new_tab``,
    ``fetch_link_content``) have already set a richer per-action notice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class _TabRow:
    page_id: int
    url: str
    title: str
    is_active: bool


@dataclass(frozen=True)
class TabSnapshot:
    """Immutable snapshot of the browser context's tab state at a moment in
    time. ``page_id`` keys are stable across index shifts; URLs and titles
    are advisory (for human-readable diff output only)."""

    rows: tuple[_TabRow, ...] = field(default_factory=tuple)

    @property
    def page_ids(self) -> tuple[int, ...]:
        return tuple(r.page_id for r in self.rows)

    @property
    def active_id(self) -> int | None:
        for r in self.rows:
            if r.is_active:
                return r.page_id
        return None

    def index_of(self, page_id: int) -> int | None:
        for idx, r in enumerate(self.rows):
            if r.page_id == page_id:
                return idx
        return None


def snapshot_tabs(browser: Any) -> TabSnapshot:
    """Capture the current tab state. Tolerant of partially-initialised or
    closed browsers — returns an empty snapshot rather than raising.

    Real page titles are read from ``browser._page_titles`` (populated by a
    ``domcontentloaded`` / ``framenavigated`` listener in
    ``BrowserEnv._register_page``). When the cache hasn't caught the latest
    navigation yet, fall back to the URL's host+path tail so the snapshot
    is still useful — but in the steady state the VLM sees real titles like
    "百度安全验证" or "百度新闻 - 网络安全规则", not URL guesses.
    """
    ctx = getattr(browser, "_context", None)
    if ctx is None:
        return TabSnapshot()
    try:
        pages = list(ctx.pages)
    except Exception:
        return TabSnapshot()
    active = getattr(browser, "_page", None)
    title_cache: dict[int, str] = getattr(browser, "_page_titles", {}) or {}
    rows: list[_TabRow] = []
    for p in pages:
        try:
            if p.is_closed():
                continue
        except Exception:
            continue
        try:
            url = p.url or ""
        except Exception:
            url = ""
        # Prefer the cached real title; fall back to URL tail.
        cached_title = title_cache.get(id(p), "").strip()
        if cached_title:
            title = cached_title[:50]
        elif url:
            try:
                _short = url.split("?", 1)[0].split("#", 1)[0]
                title = _short[-40:]
            except Exception:
                title = url[:40]
        else:
            title = ""
        rows.append(_TabRow(page_id=id(p), url=url, title=title, is_active=(p is active)))
    return TabSnapshot(rows=tuple(rows))


def _label(row: _TabRow, idx: int) -> str:
    return f"[{idx}] {row.url[:80] if row.url else '(empty)'}"


def format_tab_delta(
    before: TabSnapshot,
    after: TabSnapshot,
    *,
    last_action: str = "",
    last_target_id: int = 0,
) -> str | None:
    """Compose a feedback string when tab structure changed; else ``None``.

    Detects four orthogonal events:
      * **opened**: page ids present in ``after`` but not ``before``
      * **closed**: page ids present in ``before`` but not ``after``
      * **focus_drift**: same id sets, but active id changed
      * **reindex_only**: same id set + same active = NOT an event (caller
        should not see a notice for this)

    Reindex-without-open/close is intentionally suppressed because it's a
    side-effect of close (index shift), not a meaningful state change for
    the VLM to react to.
    """
    before_ids = set(before.page_ids)
    after_ids = set(after.page_ids)
    opened_ids = after_ids - before_ids
    closed_ids = before_ids - after_ids
    focus_changed = (
        before.active_id is not None
        and after.active_id is not None
        and before.active_id != after.active_id
    )

    if not (opened_ids or closed_ids or focus_changed):
        return None

    lines: list[str] = [
        "⚠️ TAB STATE CHANGE — 上一步动作让标签页列表发生了变化。"
    ]
    action_repr = last_action or "<unknown>"
    if last_target_id:
        action_repr = f"{action_repr}(target_id={last_target_id})"
    lines.append(f"  上一步动作: {action_repr}")

    def _label_with_title(row: _TabRow) -> str:
        """Real title + URL, both visible — VLM needs the title to spot
        cases like 'wait this is the rules page, not the chat page'."""
        title = (row.title or "").strip()
        url = (row.url or "")[:80]
        if title:
            return f"{title!r} {url or '(empty)'}".strip()
        return url or "(empty)"

    # Closed first — VLM most likely to act on stale references to gone tabs
    if closed_ids:
        closed_rows = [r for r in before.rows if r.page_id in closed_ids]
        for r in closed_rows:
            idx = before.index_of(r.page_id)
            lines.append(f"  ❌ 关闭: [{idx}] {_label_with_title(r)}")

    if opened_ids:
        opened_rows = [r for r in after.rows if r.page_id in opened_ids]
        for r in opened_rows:
            idx = after.index_of(r.page_id)
            lines.append(f"  ✨ 新开: [{idx}] {_label_with_title(r)}")

    if focus_changed:
        before_active = next((r for r in before.rows if r.page_id == before.active_id), None)
        after_active = next((r for r in after.rows if r.page_id == after.active_id), None)
        if before_active and after_active:
            before_idx = before.index_of(before.active_id)
            after_idx = after.index_of(after.active_id)
            lines.append(
                f"  🎯 焦点: [{before_idx}] {_label_with_title(before_active)} "
                f"→ [{after_idx}] {_label_with_title(after_active)}"
            )

    # Always re-state the current ground truth so the VLM can update its
    # internal tab list verbatim. Title-first so VLM can spot "this isn't
    # the page I thought it was" without having to parse the URL.
    lines.append(f"  现在共 {len(after.rows)} 个 tab:")
    for idx, r in enumerate(after.rows):
        marker = " (活跃)" if r.is_active else ""
        lines.append(f"    [{idx}]{marker} {_label_with_title(r)}")

    lines.append(
        "🚨 你 thought 里旧的 tab 索引可能已失效；以上才是真实状态，重新规划"
        "下一步 switch_tab / close_tab 的目标索引时必须用这份新清单。"
    )
    return "\n".join(lines)


def compute_tab_delta_notice(
    browser: Any,
    before: TabSnapshot | None,
    *,
    last_action: str = "",
    last_target_id: int = 0,
) -> str | None:
    """High-level helper: snapshot current state, diff against ``before``,
    return a notice string or ``None``.

    ``before`` may be ``None`` (e.g. action errored before snapshot was
    captured) → returns ``None`` so caller can skip the notice without
    branching.
    """
    if before is None:
        return None
    after = snapshot_tabs(browser)
    return format_tab_delta(
        before, after, last_action=last_action, last_target_id=last_target_id
    )

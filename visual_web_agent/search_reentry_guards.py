"""Search-box re-entry guard for multi-tab search workflows."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import unquote_plus

logger = logging.getLogger(__name__)

_REENTRY_QUERY_RE = re.compile(
    r"(?:重新|再次|再)\s*(?:输入|搜索|检索|查询)\s*[\"“']?([^\"”'，。；;\n]+)",
    re.I,
)
_FIRST_SEARCH_RE = re.compile(
    r"(?:在[^，。；;\n]{0,20})?(?:搜索(?!框|栏|输入框)|检索|查询)\s*[\"“']?([^\"”'，。；;\n]+)",
    re.I,
)
_REENTRY_CUE_RE = re.compile(r"(?:重新|再次|再)\s*(?:输入|搜索|检索|查询)", re.I)

_SEARCH_INPUT_STATE_JS = r"""
() => {
    const visible = (el) => {
        if (!el || el.disabled || el.readOnly) return false;
        const r = el.getBoundingClientRect();
        if (!r || r.width < 20 || r.height < 12) return false;
        const cs = window.getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || 1) <= 0.01) return false;
        const vw = window.innerWidth || document.documentElement.clientWidth || 0;
        const vh = window.innerHeight || document.documentElement.clientHeight || 0;
        return r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
    };
    const score = (el) => {
        const attrs = [
            el.getAttribute('role'),
            el.getAttribute('type'),
            el.getAttribute('name'),
            el.getAttribute('id'),
            el.getAttribute('placeholder'),
            el.getAttribute('aria-label'),
            el.getAttribute('title'),
        ].join(' ').toLowerCase();
        let s = 0;
        if (el === document.activeElement) s += 100;
        if (attrs.includes('searchbox') || attrs.includes('search')) s += 50;
        if (attrs.includes('q') || attrs.includes('query')) s += 25;
        if (attrs.includes('搜索') || attrs.includes('搜寻') || attrs.includes('输入')) s += 50;
        if ((el.value || '').trim()) s += 10;
        return s;
    };
    const seen = new Set();
    const candidates = [];
    const push = (el) => {
        if (!el || seen.has(el) || !visible(el)) return;
        seen.add(el);
        candidates.push(el);
    };
    for (const sel of [
        'input[type="search"]',
        'input[name="q"]',
        '[role="searchbox"]',
        'input[aria-label*="搜索" i]',
        'input[placeholder*="搜索" i]',
        'input[type="text"]',
        'input:not([type])',
        'textarea'
    ]) {
        for (const el of Array.from(document.querySelectorAll(sel))) push(el);
    }
    candidates.sort((a, b) => score(b) - score(a));
    const el = candidates[0];
    if (!el) return null;
    return {
        target_id: Number(el.getAttribute('data-som-id') || 0),
        value: String(el.value || el.textContent || ''),
        role: el.getAttribute('role') || '',
        type: el.getAttribute('type') || '',
        name: el.getAttribute('name') || '',
        placeholder: el.getAttribute('placeholder') || '',
        ariaLabel: el.getAttribute('aria-label') || '',
        score: score(el),
    };
}
"""


def parse_reentry_query(goal: str) -> str:
    match = _REENTRY_QUERY_RE.search(goal or "")
    if not match:
        return ""
    query = match.group(1).strip()
    query = re.sub(r"\s*(?:点击|并|然后|，|。).*$", "", query).strip()
    return query


def parse_first_search_query(goal: str) -> str:
    for match in _FIRST_SEARCH_RE.finditer(goal or ""):
        query = match.group(1).strip()
        query = re.sub(r"\s*(?:点击|并|然后|，|。).*$", "", query).strip()
        if not query or _REENTRY_CUE_RE.search(query):
            continue
        return query
    return ""


async def _current_search_input_state(browser: Any) -> dict[str, Any] | None:
    page = await browser._ensure_active_page(reason="search reentry guard")
    if not page:
        return None
    for frame in getattr(page, "frames", []) or [getattr(page, "main_frame", None)]:
        if frame is None:
            continue
        try:
            state = await frame.evaluate(_SEARCH_INPUT_STATE_JS)
        except Exception:
            continue
        if isinstance(state, dict):
            return state
    return None


async def apply_search_reentry_guard(
    browser: Any,
    decisions: list[dict[str, Any]],
    *,
    goal: str,
) -> bool:
    """Rewrite confused search re-entry actions into type+Enter.

    This is intentionally narrow: it only triggers when the user explicitly
    asked to re-enter a new query and the current search box still contains a
    different value.
    """
    if not decisions:
        return False
    query = parse_reentry_query(goal)
    if not query:
        return False
    head = decisions[0]
    decision_text = " ".join(
        str(head.get(k) or "")
        for k in ("thought", "progress_review", "current_state", "type_value")
    )
    compact_decision = re.sub(r"\s+", "", decision_text.casefold())
    compact_query = re.sub(r"\s+", "", query.casefold())
    if compact_query and compact_query not in compact_decision:
        return False
    state = await _current_search_input_state(browser)
    if not state:
        return False
    current_value = str(state.get("value") or "").strip()
    if not current_value or current_value.casefold() == query.casefold():
        return False
    first_query = parse_first_search_query(goal)
    if first_query and first_query.casefold() != query.casefold():
        try:
            current_url = str(getattr(browser, "current_url", "") or "")
        except Exception:
            current_url = ""
        decoded_url = unquote_plus(current_url).casefold()
        compact_stage = re.sub(r"\s+", "", decoded_url)
        compact_first = re.sub(r"\s+", "", first_query.casefold())
        if compact_first and compact_first not in compact_stage:
            return False

    action = str(head.get("action") or "")
    if action in {"switch_tab", "close_tab", "done", "ask_human", "error"}:
        return False
    try:
        tid = int(state.get("target_id") or 0)
    except (TypeError, ValueError):
        tid = 0
    if tid < 0:
        tid = 0

    logger.info(
        "[SEARCH REENTRY] rewriting %s -> type(%s, %r)+Enter (current=%r)",
        action,
        tid,
        query,
        current_value[:80],
    )
    decisions[:] = [
        {
            **head,
            "action": "type",
            "target_id": tid,
            "type_value": query,
            "thought": (
                "[SEARCH REENTRY GUARD] User asked to re-enter a new search query; "
                f"current searchbox value is {current_value!r}. Rewriting to type {query!r}.\n"
                + str(head.get("thought") or "")
            ),
        },
        {
            "action": "press_key",
            "target_id": 0,
            "type_value": "Enter",
            "thought": "[SEARCH REENTRY GUARD] Submit the rewritten search query.",
            "status": "success",
            "memory_key": "",
        },
    ]
    return True

"""Virtual-scroll-aware scrolling primitives (Slice EXTRACT-VSCROLL-1).

Virtualised lists (react-window, vue-virtual-scroller, ag-grid, Element Plus
virtual tables...) render a constant window of rows inside an inner scroller:

- ``window.scrollBy`` never moves the inner container, and
- ``document.body.innerText.length`` stays flat while rows are recycled,

so the legacy "scroll the window, compare body length" nudge reports a dead
end and bulk extraction stops early. This module scrolls the dominant inner
container directly and detects progress by comparing a first/last row text
signature instead of the body length.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Shared scroll-container finder: visible, overflow-y scrollable, with real
# scroll headroom; the largest visible area wins (mirrors the drain probe).
_FIND_SCROLLER_JS_SNIPPET = r"""
    const viewportW = window.innerWidth || 0;
    const viewportH = window.innerHeight || 0;
    const scrollableRect = (el) => {
        if (!el || !el.getBoundingClientRect) return null;
        const r = el.getBoundingClientRect();
        const w = Math.max(0, Math.min(r.right, viewportW) - Math.max(r.left, 0));
        const h = Math.max(0, Math.min(r.bottom, viewportH) - Math.max(r.top, 0));
        if (w < 160 || h < 120) return null;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return null;
        if (!/(auto|scroll|overlay)/i.test(style.overflowY || '')) return null;
        if (el.scrollHeight <= el.clientHeight + 80) return null;
        return {el, area: w * h};
    };
    const findScroller = () => {
        const candidates = Array.from(document.querySelectorAll(
            '.el-scrollbar__wrap, .el-table__body-wrapper, .ant-table-body, ' +
            '.ag-body-viewport, .v-data-table__wrapper, [class*="virtual"], ' +
            '[class*="scroller"], [class*="scroll"], [role="grid"], ' +
            '[role="listbox"], main, [role="main"], ul, ol, div'
        ))
            .map(scrollableRect)
            .filter(Boolean)
            .sort((a, b) => b.area - a.area);
        return candidates.length ? candidates[0].el : null;
    };
    const rowSignatureOf = (root) => {
        const isVisible = (el) => {
            if (!el || !el.getBoundingClientRect) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const rows = Array.from(root.querySelectorAll(
            'tr, [role="row"], li, [class*="item"], [class*="row"]'
        )).filter(isVisible);
        if (!rows.length) {
            return String(root.innerText || '').replace(/\s+/g, ' ').slice(0, 400);
        }
        const first = rows[0].innerText || '';
        const last = rows[rows.length - 1].innerText || '';
        return (first + '||' + last).replace(/\s+/g, ' ').slice(0, 400);
    };
"""

VIRTUAL_LIST_SIGNATURE_JS = (
    "() => {"
    + _FIND_SCROLLER_JS_SNIPPET
    + r"""
    const target = findScroller();
    if (!target) return {found: false, sig: ''};
    return {
        found: true,
        sig: rowSignatureOf(target),
        scroll_top: Math.round(target.scrollTop),
        remaining: Math.round(target.scrollHeight - target.scrollTop - target.clientHeight),
    };
}"""
)

VIRTUAL_SCROLL_NUDGE_JS = (
    "(amt) => {"
    + _FIND_SCROLLER_JS_SNIPPET
    + r"""
    const target = findScroller();
    if (!target) {
        const before = window.scrollY || 0;
        window.scrollBy({top: Math.max(amt || 0, window.innerHeight * 1.5), behavior: 'instant'});
        return {mode: 'window', moved: (window.scrollY || 0) > before + 4};
    }
    const before = target.scrollTop;
    const step = Math.max(amt || 0, target.clientHeight * 0.85);
    target.scrollTop = Math.min(before + step, target.scrollHeight);
    target.dispatchEvent(new Event('scroll', {bubbles: true}));
    return {
        mode: 'container',
        moved: target.scrollTop > before + 4,
        container_tag: String(target.tagName || '').toLowerCase(),
        container_class: String(target.className || '').slice(0, 80),
        before_top: Math.round(before),
        after_top: Math.round(target.scrollTop),
        remaining: Math.round(target.scrollHeight - target.scrollTop - target.clientHeight),
    };
}"""
)


async def nudge_virtual_scroll(
    page: Any, *, amount: int = 2000, settle_ms: int = 900
) -> dict:
    """Scroll the dominant inner container (window as fallback) one step.

    Returns an evidence dict: ``mode`` (container/window), ``moved`` (the
    scroll offset actually advanced), ``rows_changed`` (the rendered row
    window changed after settling - the virtual-list progress signal), plus
    container identity and remaining headroom for the trail.
    """
    try:
        before = await page.evaluate(VIRTUAL_LIST_SIGNATURE_JS)
    except Exception as sig_err:
        logger.debug("[VSCROLL] signature probe failed: %s", sig_err)
        before = {}
    if not isinstance(before, dict):
        before = {}
    try:
        move = await page.evaluate(VIRTUAL_SCROLL_NUDGE_JS, amount)
    except Exception as nudge_err:
        logger.debug("[VSCROLL] nudge evaluate failed: %s", nudge_err)
        return {"mode": "error", "moved": False, "rows_changed": False}
    if not isinstance(move, dict):
        move = {"mode": "unknown", "moved": False}
    try:
        wait = getattr(page, "wait_for_timeout", None)
        if callable(wait):
            await wait(settle_ms)
    except Exception:
        pass
    try:
        after = await page.evaluate(VIRTUAL_LIST_SIGNATURE_JS)
    except Exception as sig_err:
        logger.debug("[VSCROLL] post-scroll signature failed: %s", sig_err)
        after = {}
    if not isinstance(after, dict):
        after = {}
    before_sig = str(before.get("sig") or "")
    after_sig = str(after.get("sig") or "")
    result = {
        "mode": str(move.get("mode") or "unknown"),
        "moved": bool(move.get("moved")),
        "rows_changed": bool(after_sig and after_sig != before_sig),
        "container": str(
            move.get("container_class") or move.get("container_tag") or ""
        ),
        "remaining": int(after.get("remaining") or move.get("remaining") or 0),
    }
    logger.debug("[VSCROLL] nudge result=%s", result)
    return result

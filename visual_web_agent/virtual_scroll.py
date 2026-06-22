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

# Shared scroll-container finder: visible, scrollable with real headroom on
# either axis; vertical hits win over horizontal ones (legacy priority), the
# largest visible area wins within an axis (mirrors the drain probe).
# Horizontal support (VSCROLL-H-1) targets card strips / film-strips - a
# shorter-but-wide box (h >= 80, w >= 240) with overflow-x headroom.
_FIND_SCROLLER_JS_SNIPPET = r"""
    const viewportW = window.innerWidth || 0;
    const viewportH = window.innerHeight || 0;
    const scrollableRect = (el) => {
        if (!el || !el.getBoundingClientRect) return null;
        const r = el.getBoundingClientRect();
        const w = Math.max(0, Math.min(r.right, viewportW) - Math.max(r.left, 0));
        const h = Math.max(0, Math.min(r.bottom, viewportH) - Math.max(r.top, 0));
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return null;
        const vOk = w >= 160 && h >= 120
            && /(auto|scroll|overlay)/i.test(style.overflowY || '')
            && el.scrollHeight > el.clientHeight + 80;
        const hOk = w >= 240 && h >= 80
            && /(auto|scroll|overlay)/i.test(style.overflowX || '')
            && el.scrollWidth > el.clientWidth + 80;
        if (!vOk && !hOk) return null;
        return {el, area: w * h, axis: vOk ? 'y' : 'x'};
    };
    const findScroller = () => {
        const candidates = Array.from(document.querySelectorAll(
            '.el-scrollbar__wrap, .el-table__body-wrapper, .ant-table-body, ' +
            '.ag-body-viewport, .v-data-table__wrapper, [class*="virtual"], ' +
            '[class*="scroller"], [class*="scroll"], [class*="carousel"], ' +
            '[class*="strip"], [role="grid"], ' +
            '[role="listbox"], main, [role="main"], ul, ol, div'
        ))
            .map(scrollableRect)
            .filter(Boolean)
            .sort((a, b) => (a.axis === b.axis
                ? b.area - a.area
                : (a.axis === 'y' ? -1 : 1)));
        return candidates.length ? candidates[0] : null;
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
    const hit = findScroller();
    if (!hit) return {found: false, sig: ''};
    const target = hit.el;
    const axis = hit.axis;
    return {
        found: true,
        sig: rowSignatureOf(target),
        axis,
        scroll_top: Math.round(axis === 'x' ? target.scrollLeft : target.scrollTop),
        remaining: Math.round(axis === 'x'
            ? target.scrollWidth - target.scrollLeft - target.clientWidth
            : target.scrollHeight - target.scrollTop - target.clientHeight),
    };
}"""
)

VIRTUAL_SCROLL_NUDGE_JS = (
    "(amt) => {"
    + _FIND_SCROLLER_JS_SNIPPET
    + r"""
    const hit = findScroller();
    if (!hit) {
        const before = window.scrollY || 0;
        window.scrollBy({top: Math.max(amt || 0, window.innerHeight * 1.5), behavior: 'instant'});
        return {mode: 'window', moved: (window.scrollY || 0) > before + 4};
    }
    const target = hit.el;
    const axis = hit.axis;
    const before = axis === 'x' ? target.scrollLeft : target.scrollTop;
    const step = Math.max(
        amt || 0,
        (axis === 'x' ? target.clientWidth : target.clientHeight) * 0.85
    );
    if (axis === 'x') {
        target.scrollLeft = Math.min(before + step, target.scrollWidth);
    } else {
        target.scrollTop = Math.min(before + step, target.scrollHeight);
    }
    target.dispatchEvent(new Event('scroll', {bubbles: true}));
    const afterPos = axis === 'x' ? target.scrollLeft : target.scrollTop;
    return {
        mode: 'container',
        axis,
        moved: afterPos > before + 4,
        container_tag: String(target.tagName || '').toLowerCase(),
        container_class: String(target.className || '').slice(0, 80),
        before_top: Math.round(before),
        after_top: Math.round(afterPos),
        remaining: Math.round(axis === 'x'
            ? target.scrollWidth - target.scrollLeft - target.clientWidth
            : target.scrollHeight - target.scrollTop - target.clientHeight),
    };
}"""
)


VIRTUAL_LIST_ROWS_JS = (
    "() => {"
    + _FIND_SCROLLER_JS_SNIPPET
    + r"""
    const hit = findScroller();
    if (!hit) return {found: false, rows: []};
    const target = hit.el;
    const axis = hit.axis;
    const isVisible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
    const nodes = Array.from(target.querySelectorAll(
        'tr, [role="row"], li, [class*="item"], [class*="row"], [class*="card"]'
    )).filter(isVisible);
    const rows = [];
    for (const el of nodes) {
        // skip wrappers whose row children are captured separately
        const nested = el.querySelector('tr, [role="row"], li');
        if (nested && isVisible(nested)) continue;
        const text = clean(el.innerText || el.textContent);
        if (!text || text.length > 2000) continue;
        const cellNodes = Array.from(el.querySelectorAll(
            'td, th, [role="cell"], [role="gridcell"]'
        )).filter(isVisible);
        const cells = cellNodes.map(c => clean(c.innerText || c.textContent)).filter(Boolean);
        rows.push(cells.length >= 2 ? {text, cells} : {text});
    }
    // VSCROLL-FIELDS-1: column headers usually live outside the scroller
    // (sticky thead / sibling header wrapper) - look upwards for them so
    // captured cells can be mapped onto named fields.
    const headerScope = target.closest(
        'table, .el-table, .ant-table, .n-data-table, .ag-root, ' +
        '[role="grid"], [role="table"], [role="treegrid"]'
    ) || target.parentElement || target;
    let headerCells = Array.from(headerScope.querySelectorAll('thead th, thead td'));
    if (!headerCells.length) {
        headerCells = Array.from(headerScope.querySelectorAll('[role="columnheader"]'));
    }
    if (!headerCells.length) {
        headerCells = Array.from(headerScope.querySelectorAll('.ag-header-cell-text'));
    }
    const headers = headerCells
        .map(c => clean(c.innerText || c.textContent))
        .filter(Boolean)
        .slice(0, 40);
    return {
        found: true,
        rows,
        headers,
        axis,
        scroll_top: Math.round(axis === 'x' ? target.scrollLeft : target.scrollTop),
        remaining: Math.round(axis === 'x'
            ? target.scrollWidth - target.scrollLeft - target.clientWidth
            : target.scrollHeight - target.scrollTop - target.clientHeight),
    };
}"""
)


async def find_virtual_list_scope(
    page: Any, *, include_main: bool = True
) -> dict:
    """Locate the scope (page or child frame) hosting a drainable virtual list.

    One lightweight ``VIRTUAL_LIST_SIGNATURE_JS`` evaluate per scope: a hit
    needs ``found=True`` plus real scroll headroom (``remaining > 0`` - lists
    already rendered in full are covered by the static frame sweeps). The
    main document goes first (optional: the pre-extract drain probe already
    covers it), then child frames in document order; detached and raising
    frames are skipped, mirroring the EXTRACT-IFRAME sweep pattern. The
    returned scope feeds :func:`capture_virtual_list_rows` directly - Frames
    satisfy its evaluate/wait_for_timeout contract as-is.
    """

    async def _probe(scope: Any) -> dict | None:
        try:
            sig = await scope.evaluate(VIRTUAL_LIST_SIGNATURE_JS)
        except Exception as probe_err:
            logger.debug(
                "[VSCROLL SCOPE] probe failed (%s): %s",
                getattr(scope, "url", "?"),
                probe_err,
            )
            return None
        if not isinstance(sig, dict) or not sig.get("found"):
            return None
        try:
            remaining = int(sig.get("remaining") or 0)
        except Exception:
            remaining = 0
        if remaining <= 0:
            return None
        return {"remaining": remaining, "axis": str(sig.get("axis") or "y")}

    if include_main:
        hit = await _probe(page)
        if hit:
            return {"scope": page, "where": "main", "url": "", **hit}
    main_frame = getattr(page, "main_frame", None)
    for frame in list(getattr(page, "frames", None) or []):
        if frame is main_frame:
            continue
        try:
            is_detached = getattr(frame, "is_detached", None)
            if callable(is_detached) and is_detached():
                continue
        except Exception:
            continue
        hit = await _probe(frame)
        if hit:
            url = str(getattr(frame, "url", "") or "")
            logger.info(
                "[VSCROLL SCOPE] virtual list found in frame=%s remaining=%s",
                url or "?",
                hit["remaining"],
            )
            return {"scope": frame, "where": "frame", "url": url, **hit}
    return {"scope": None, "where": "", "url": "", "remaining": 0}


async def capture_virtual_list_rows(
    page: Any,
    *,
    max_rows: int = 2000,
    max_passes: int = 120,
    settle_ms: int = 250,
) -> dict:
    """Deterministic one-shot harvest of a virtualised list.

    Alternates row collection and container nudges, deduplicating recycled
    rows by their text, until the scroller stops moving (complete=True),
    ``max_rows`` is reached, or ``max_passes`` runs out. Replaces ~1 VLM
    round per viewport with a single deterministic call.
    """
    seen: set[str] = set()
    rows: list[dict] = []
    container = ""
    axis = ""
    headers: list[str] = []
    complete = False
    passes = 0
    while passes < max_passes:
        passes += 1
        snap: Any = {}
        try:
            snap = await page.evaluate(VIRTUAL_LIST_ROWS_JS)
        except Exception as snap_err:
            logger.debug("[VSCROLL CAPTURE] row snapshot failed: %s", snap_err)
        if not isinstance(snap, dict):
            snap = {}
        axis = str(snap.get("axis") or axis or "")
        if not headers:
            headers = [
                str(h) for h in (snap.get("headers") or []) if str(h or "").strip()
            ]
        for item in snap.get("rows") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            rows.append(item)
            if len(rows) >= max_rows:
                break
        if len(rows) >= max_rows:
            break
        vs = await nudge_virtual_scroll(page, amount=0, settle_ms=settle_ms)
        container = vs.get("container") or container
        axis = str(vs.get("axis") or axis or "")
        if not vs.get("moved") and not vs.get("rows_changed"):
            complete = True
            break
    result = {
        "rows": rows,
        "row_count": len(rows),
        "passes": passes,
        "complete": complete,
        "container": container,
        "axis": axis or "y",
        "headers": headers,
    }
    logger.info(
        "[VSCROLL CAPTURE] rows=%s passes=%s complete=%s container=%s axis=%s headers=%s",
        len(rows), passes, complete, container or "?", axis or "y", len(headers),
    )
    return result


def map_captured_rows_to_fields(rows: list, headers: list) -> list[dict]:
    """Map captured ``{text, cells}`` rows onto named header fields.

    VSCROLL-FIELDS-1: the capture emits anonymous cell lists; downstream
    consumers (dataset_rows artifacts, the candidate ranking, the VLM) want
    named columns. Positional zip against the harvested headers - duplicate
    header names get a ``_N`` suffix, blank/overflow positions fall back to
    ``col_N``, and rows without cells keep their ``text`` form. Pure and
    dependency-free so every capture call site can use it as-is.
    """
    clean_headers: list[str] = []
    seen: dict[str, int] = {}
    for idx, raw in enumerate(headers or []):
        name = str(raw or "").strip() or f"col_{idx + 1}"
        count = seen.get(name, 0) + 1
        seen[name] = count
        clean_headers.append(name if count == 1 else f"{name}_{count}")
    mapped: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cells = row.get("cells")
        if isinstance(cells, list) and cells:
            record: dict = {}
            for idx, value in enumerate(cells):
                key = (
                    clean_headers[idx]
                    if idx < len(clean_headers)
                    else f"col_{idx + 1}"
                )
                record[key] = value
            mapped.append(record)
        else:
            mapped.append({"text": str(row.get("text") or "")})
    return mapped


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
        "axis": str(move.get("axis") or after.get("axis") or "y"),
    }
    logger.debug("[VSCROLL] nudge result=%s", result)
    return result

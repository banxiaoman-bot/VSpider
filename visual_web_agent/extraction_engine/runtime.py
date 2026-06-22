"""Runtime state and helpers for the extraction subsystem carved out of
``main.run_agent``.

S1a introduces :class:`ExtractState`, a single mutable container for the
~19 run-level extraction counters that the main step loop and the extraction
closures both read and write. Grouping them here breaks the closure/loop
two-way coupling so subsequent slices (S1b-S1d) can move the closures into an
``ExtractRuntime`` that operates on a shared state instance — no behaviour
change, pure relocation of state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

try:  # pragma: no cover - import shim mirrors main.py
    from ..data_sanitizer import extract_tooltip_primary_key, sanitize_extracted_rows, TOOLTIP_UNIQUE_KEY
    from ..phases.goal_parser import (
        _goal_is_tooltip_extract,
        _normalize_output_field_key,
        _parse_goal_target_count,
    )
    from ..phases.decision_helpers import _goal_is_bulk_extraction
    from ..phases.pagination_helpers import (
        _goal_needs_pagination_probe,
        _should_force_first_flip_after_successful_extract,
        _should_schedule_next_page_after_extract,
    )
    from .snapshots import maybe_save_snapshot
    from .cards import extract_semantic_card_rows
    from ..data_writers import save_run_dataset
    from ..artifact_manager import resolve_artifact_path
    from ..virtual_scroll import nudge_virtual_scroll
except ImportError:  # pragma: no cover
    from data_sanitizer import extract_tooltip_primary_key, sanitize_extracted_rows, TOOLTIP_UNIQUE_KEY
    from phases.goal_parser import (
        _goal_is_tooltip_extract,
        _normalize_output_field_key,
        _parse_goal_target_count,
    )
    from phases.decision_helpers import _goal_is_bulk_extraction
    from phases.pagination_helpers import (
        _goal_needs_pagination_probe,
        _should_force_first_flip_after_successful_extract,
        _should_schedule_next_page_after_extract,
    )
    from extraction_engine.snapshots import maybe_save_snapshot
    from extraction_engine.cards import extract_semantic_card_rows
    from data_writers import save_run_dataset
    from artifact_manager import resolve_artifact_path
    from virtual_scroll import nudge_virtual_scroll
import hashlib
import re


@dataclass
class ExtractState:
    """Run-level extraction counters shared by the main loop and ExtractRuntime.

    Field names mirror the original ``main.run_agent`` locals with the leading
    underscore dropped (e.g. ``_total_extracted_rows`` -> ``total_extracted_rows``).
    """

    # ── pagination / flow flags ────────────────────────────────────────────
    extract_count: int = 0
    pagination_probed: bool = False
    pagination_kind: str = ""
    pagination_hint_msg: str = ""
    first_flip_pending: bool = False
    page_is_infinite_scroll: bool = False
    force_next_page_pending: bool = False
    force_extract_after_navigation_pending: bool = False
    block_next_page_until_drained: bool = False
    block_next_page_reason: str = ""
    first_extract_ever_done: bool = False
    pagination_exhausted: bool = False

    # ── row / progress counters ────────────────────────────────────────────
    total_extracted_rows: int = 0
    extract_null_streak: int = 0
    extract_null_total_resets: int = 0

    # ── dedup / progress sets ──────────────────────────────────────────────
    extracted_page_urls: set = field(default_factory=set)
    extracted_page_keys: set = field(default_factory=set)
    seen_extract_row_keys: set = field(default_factory=set)
    tooltip_trigger_keys: set = field(default_factory=set)


@dataclass
class ExtractDeps:
    """Dependency handles for the extraction helpers lifted out of
    ``main.run_agent``. S1b only needs the browser, a logger and the two
    callables the DOM readers used to capture as closures; later slices
    (S1c/S1d) extend this as more closures move in.
    """

    browser: Any
    logger: Any
    evaluate_rows_with_frame_fallback: Callable[..., Any]
    goal: str
    goal_output_mode: str
    requested_output_fields: Any
    data_controller: Any
    event_stream: Any = None
    run_ts: str = ""
    snapshot_goal: str = ""
    goal_output_contract: Any = None
    vlm_output: str = ""
    enable_xhr: bool = False
    broadcast_log_safe: Callable[..., Any] = lambda *_a, **_k: None
    vlm: Any = None


@dataclass
class ExtractCommit:
    """Result of choosing + committing the best extraction candidate.

    Mirrors the seven run_agent locals the explicit-extract choose/commit block
    used to assign in place. When no candidate is chosen, the prior
    ``source_text_for_validation`` / ``log_extract_text_source`` /
    ``current_extract_page_key`` are echoed back unchanged.
    """

    extracted: list
    new_rows: int
    dup_rows: int
    rejected_rows: int
    source_text_for_validation: str
    log_extract_text_source: str
    current_extract_page_key: str


@dataclass
class ExtractPersist:
    """Result of persisting a successful extraction batch (save + snapshot +
    progress). Mirrors the locals the explicit-extract persist block produced;
    the caller writes ``decision['extracted_data']`` / ``decision['snapshot_path']``
    and resets ``_duplicate_zero_extract_streak`` from these.
    """

    extracted: list
    saved_path: str
    snapshot_path: str
    progress_new_rows: int
    progress_total_rows: int


class ExtractRuntime:
    """Read-only DOM extraction helpers moved verbatim from run_agent.

    Each method is a pure relocation of the matching ``_*`` closure: it
    reads the page through ``self.deps`` and does not mutate ``self.state``
    (these six are all read-only probes).
    """

    def __init__(self, deps: ExtractDeps, state: ExtractState) -> None:
        self.deps = deps
        self.state = state

    async def extract_list_rows_via_dom(self, reason: str) -> tuple[list[dict], str]:
        """Extract repeated list/card rows directly with DOM semantics."""
        try:
            _list_page = await self.deps.browser._ensure_active_page(reason=reason)
            _list_rows_js = """() => {
                    const clean = (value) => String(value || '')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    const compact = (value) => clean(value).toLowerCase()
                        .replace(/[^a-z0-9\\u4e00-\\u9fff]+/g, '');
                    const isVisible = (el) => {
                        if (!el || !(el instanceof Element)) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && rect.width > 0
                            && rect.height > 0;
                    };
                    // EXTRACT-SHADOW-2: card/list components render inside
                    // open shadow roots, invisible to plain querySelectorAll
                    // (same walker as the table harvest / form fallback).
                    const deepQueryAll = (selector, root = document) => {
                        const out = Array.from(root.querySelectorAll(selector));
                        for (const host of root.querySelectorAll('*')) {
                            if (host.shadowRoot) out.push(...deepQueryAll(selector, host.shadowRoot));
                        }
                        return out;
                    };
                    const classifyUrl = (href) => {
                        const text = String(href || '').toLowerCase();
                        if (!text) return '';
                        if (text.includes('news.ycombinator.com/item')) return 'detail';
                        if (/\\/(item|story|post|posts|article|articles|thread|threads|comment|comments|detail|details|product|products|issues?)(\\/|\\?|#|$)/.test(text)) {
                            return 'detail';
                        }
                        return 'source';
                    };
                    const firstInt = (value) => {
                        const match = String(value || '').match(/\\d[\\d,]*/);
                        return match ? Number(match[0].replace(/,/g, '')) : null;
                    };
                    const looksMetaLink = (text) => {
                        const t = clean(text).toLowerCase();
                        return !t
                            || /^\\d+[\\d,]*\\s*(points?|comments?|replies?)$/.test(t)
                            || /^\\d+\\s+(seconds?|minutes?|hours?|days?|months?|years?)\\s+ago$/.test(t)
                            || /^\\d+[smhdwy]$/.test(t)
                            || /^(reply|hide|flag|past|favorite|save|share)$/.test(t);
                    };
                    const titleFromRow = (el, links, text) => {
                        const semantic = el.querySelector('h1,h2,h3,h4,[role="heading"],.title,.story-title,.ais-Highlight');
                        const semanticText = clean(semantic ? semantic.innerText || semantic.textContent : '');
                        if (semanticText && semanticText.length >= 4) return semanticText;
                        const link = links.find(l => l.text && !looksMetaLink(l.text));
                        if (link) return link.text;
                        const firstLine = clean((text || '').split(/\\n|\\r/)[0]);
                        return firstLine.length > 220 ? firstLine.slice(0, 220) : firstLine;
                    };
                    const parseMeta = (text, links, el) => {
                        const row = {};
                        const pointMatch = text.match(/(\\d[\\d,]*)\\s*points?/i);
                        if (pointMatch) row.points = firstInt(pointMatch[1]);
                        const commentMatch = text.match(/(\\d[\\d,]*)\\s*(?:comments?|replies?)/i);
                        if (commentMatch) row.review_count = firstInt(commentMatch[1]);
                        const reviewMatch = text.match(/(\\d[\\d,]*)\\s*(?:人评价|评价|条评价|reviews?|ratings?|votes?)/i);
                        if (reviewMatch && !row.review_count) row.review_count = firstInt(reviewMatch[1]);
                        const timeMatch = text.match(/\\b(\\d+\\s+(?:seconds?|minutes?|hours?|days?|months?|years?)\\s+ago|\\d+[smhdwy])\\b/i);
                        if (timeMatch) row.time = clean(timeMatch[1]);

                        const ratingEl = el.querySelector(
                            '.rating_num, .rating_nums, .score, .rating-score, [class*="score"]'
                        );
                        const ratingText = clean(ratingEl ? ratingEl.innerText || ratingEl.textContent : '');
                        const ratingMatch = ratingText.match(/\\b(\\d(?:\\.\\d)?)\\b/)
                            || text.match(/(?:评分|rating|score)\\s*[:：]?\\s*(\\d(?:\\.\\d)?)/i)
                            || text.match(/(?:^|\\s)(\\d\\.\\d)(?:\\s|$)/);
                        if (ratingMatch) row.rating = ratingMatch[1];

                        const summaryEl = el.querySelector(
                            '.quote .inq, .inq, p.quote, .summary, .description, .desc, .intro, [class*="summary"], [class*="description"], [class*="intro"]'
                        );
                        const summaryText = clean(summaryEl ? summaryEl.innerText || summaryEl.textContent : '');
                        if (summaryText && summaryText.length >= 3 && summaryText.length <= 260) {
                            row.summary = summaryText.replace(/^["“”'‘’]+|["“”'‘’]+$/g, '');
                        } else {
                            const quoteMatch = text.match(/[“"']([^“”"']{3,260})[”"']/);
                            if (quoteMatch) row.summary = clean(quoteMatch[1]);
                        }

                        const metaTexts = links.map(l => clean(l.text)).filter(Boolean);
                        const timeIndex = metaTexts.findIndex(t => /^(\\d+\\s+(?:seconds?|minutes?|hours?|days?|months?|years?)\\s+ago|\\d+[smhdwy])$/i.test(t));
                        if (timeIndex > 0 && !row.author) {
                            const prev = metaTexts[timeIndex - 1];
                            if (prev && !looksMetaLink(prev)) row.author = prev;
                        }
                        return row;
                    };
                    const collectLinks = (el) => Array.from(el.querySelectorAll('a[href]'))
                        .filter(isVisible)
                        .map(a => ({
                            text: clean(a.innerText || a.textContent || a.getAttribute('aria-label') || a.getAttribute('title')),
                            href: a.href || ''
                        }))
                        .filter(link => link.href && !link.href.startsWith('javascript:'));

                    const candidates = [];
                    const addCandidate = (el, source) => {
                        if (!isVisible(el)) return;
                        const text = clean(el.innerText || el.textContent);
                        if (text.length < 20 || text.length > 2400) return;
                        const nested = Array.from(el.querySelectorAll(
                            'article, [role="article"], [role="listitem"], li, tbody tr'
                        )).filter(node => node !== el && isVisible(node));
                        if (nested.length >= 3 && text.length > 900) return;
                        const links = collectLinks(el);
                        const key = links[0]?.href || compact(text).slice(0, 220);
                        if (!key) return;
                        candidates.push({el, source, key, text, links});
                    };

                    const directSelectors = [
                        'article', '[role="article"]', '[role="listitem"]',
                        '.Story', '.story', '.ais-Hits-item', '.hit',
                        '.search-result', '.result', '.item', '.card'
                    ];
                    for (const el of deepQueryAll(directSelectors.join(','))) {
                        addCandidate(el, 'selector');
                    }

                    const containerSelectors = [
                        'main', '[role="main"]', '#content', '.content',
                        '.list', '.item-list', '.results', '.search-results',
                        'ol', 'ul', 'section'
                    ];
                    for (const root of deepQueryAll(containerSelectors.join(','))) {
                        if (!isVisible(root)) continue;
                        const children = Array.from(root.children || []).filter(isVisible);
                        if (children.length < 4) continue;
                        const buckets = new Map();
                        for (const child of children) {
                            const cls = clean(child.className || child.tagName).slice(0, 80);
                            buckets.set(cls, (buckets.get(cls) || 0) + 1);
                        }
                        const repeat = Math.max(...Array.from(buckets.values()), 0);
                        if (repeat < 4) continue;
                        for (const child of children) addCandidate(child, 'container');
                    }

                    const seen = new Set();
                    const rows = [];
                    for (const candidate of candidates) {
                        if (seen.has(candidate.key)) continue;
                        seen.add(candidate.key);
                        const links = candidate.links;
                        const row = parseMeta(candidate.text, links, candidate.el);
                        row.title = titleFromRow(candidate.el, links, candidate.text);
                        row._dom_text = candidate.text.slice(0, 1200);

                        for (const link of links) {
                            const role = classifyUrl(link.href);
                            if (role === 'detail' && !row.detail_url) row.detail_url = link.href;
                            if (role === 'source' && !row.source_url) row.source_url = link.href;
                        }
                        row.primary_url = row.source_url || row.detail_url || links[0]?.href || '';
                        if (row.primary_url) row.url = row.primary_url;

                        if (!row.title || row.title.length < 4) continue;
                        if (!row.url && candidate.text.length < 40) continue;
                        rows.push(row);
                    }
                    const bodyText = document.body ? document.body.innerText || '' : '';
                    rows.sort((a, b) => {
                        const aTop = bodyText.indexOf(String(a.title || '').slice(0, 40));
                        const bTop = bodyText.indexOf(String(b.title || '').slice(0, 40));
                        return (aTop < 0 ? 1e9 : aTop) - (bTop < 0 ? 1e9 : bTop);
                    });
                    const selected = rows.slice(0, 120);
                    const sourceText = selected.map((row, index) => [
                        `Item ${index + 1}: ${row.title}`,
                        row._dom_text,
                        row.source_url ? `source_url: ${row.source_url}` : '',
                        row.detail_url ? `detail_url: ${row.detail_url}` : ''
                    ].filter(Boolean).join('\\n')).join('\\n\\n');
                    const publicRows = selected.map(row => {
                        const copy = {...row};
                        delete copy._dom_text;
                        return copy;
                    });
                    return {rows: publicRows, sourceText};
                }"""
            result = await self.deps.evaluate_rows_with_frame_fallback(
                _list_page,
                _list_rows_js,
                log_tag="EXTRACT DOM LIST",
                payload_empty=lambda value: not (
                    isinstance(value, dict) and value.get("rows")
                ),
            )
            if not isinstance(result, dict):
                return [], ""
            rows = result.get("rows") or []
            source_text = str(result.get("sourceText") or "")
            if isinstance(rows, list) and len(rows) >= 2:
                rows = self.normalize_extracted_row_fields(rows)
                self.deps.logger.info(
                    "[EXTRACT DOM] list rows=%s source_chars=%s",
                    len(rows),
                    len(source_text),
                )
                return rows, source_text
        except Exception as list_err:
            self.deps.logger.debug("[EXTRACT DOM] list extraction skipped: %s", list_err)
        return [], ""

    async def extract_visible_table_rows_via_dom(self, reason: str) -> list[dict]:
        try:
            _table_page = await self.deps.browser._ensure_active_page(reason=reason)
            _table_rows_js = """() => {
                    const clean = (value) => String(value || '')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    const cleanHeader = (value) => clean(value)
                        .replace(/\\s*:?[\\s-]*activate to sort column (?:ascending|descending)/ig, '')
                        .replace(/\\s*:?[\\s-]*activate to sort/ig, '')
                        .replace(/\\s*排序(?:升序|降序)?\\s*/g, '')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    const isVisible = (el) => {
                        if (!el || !(el instanceof Element)) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && rect.width > 0
                            && rect.height > 0;
                    };
                    // EXTRACT-SHADOW-1: component-library tables render
                    // inside open shadow roots, invisible to plain
                    // document.querySelectorAll (mirrors actions.deepQueryAll).
                    const deepQueryAll = (selector, root = document) => {
                        const out = Array.from(root.querySelectorAll(selector));
                        for (const host of root.querySelectorAll('*')) {
                            if (host.shadowRoot) out.push(...deepQueryAll(selector, host.shadowRoot));
                        }
                        return out;
                    };
                    const headerText = (el) => cleanHeader(
                        el.getAttribute('aria-label')
                        || el.getAttribute('data-label')
                        || el.getAttribute('title')
                        || el.innerText
                        || el.textContent
                    );
                    const rowCells = (tr, includeTh = false) => {
                        const selector = includeTh
                            ? 'th, td, [role="columnheader"], [role="rowheader"], [role="cell"], [role="gridcell"]'
                            : 'td, [role="cell"], [role="gridcell"]';
                        return Array.from(tr.querySelectorAll(selector))
                            .filter(cell => isVisible(cell))
                            .map(cell => clean(cell.innerText || cell.textContent));
                    };
                    const uniqueHeaders = (headers) => {
                        const seen = new Map();
                        return headers.map((header, index) => {
                            let key = cleanHeader(header) || `column_${index + 1}`;
                            const base = key;
                            const count = (seen.get(base) || 0) + 1;
                            seen.set(base, count);
                            if (count > 1) key = `${base}_${count}`;
                            return key;
                        });
                    };
                    const headerCandidatesFor = (table) => {
                        const candidates = [];
                        const add = (nodes, source) => {
                            const headers = Array.from(nodes || [])
                                .filter(node => node instanceof Element)
                                .map(headerText)
                                .filter(Boolean);
                            if (headers.length) candidates.push({source, headers});
                        };

                        add(table.querySelectorAll('thead th, thead td, [role="columnheader"]'), 'table-head');
                        const headerRows = Array.from(table.querySelectorAll('tr'))
                            .filter(tr => tr.querySelector('th, [role="columnheader"]'));
                        for (const tr of headerRows.slice(0, 3)) {
                            add(tr.querySelectorAll('th, td, [role="columnheader"]'), 'header-row');
                        }

                        const id = table.id ? CSS.escape(table.id) : '';
                        const wrapper = table.closest(
                            '.dt-container, .dataTables_wrapper, .datatable, .table-responsive, .table-container, [role="grid"]'
                        );
                        if (wrapper) {
                            add(wrapper.querySelectorAll('thead th, thead td, [role="columnheader"]'), 'wrapper-head');
                            if (id) {
                                add(
                                    wrapper.querySelectorAll(`[aria-controls="${id}"], [data-dt-column]`),
                                    'wrapper-controls'
                                );
                            }
                        }

                        return candidates;
                    };
                    const chooseHeaders = (table, width) => {
                        const candidates = headerCandidatesFor(table);
                        candidates.sort((a, b) => {
                            const aExact = a.headers.length === width ? 1 : 0;
                            const bExact = b.headers.length === width ? 1 : 0;
                            return (bExact - aExact)
                                || (Math.abs(a.headers.length - width) - Math.abs(b.headers.length - width))
                                || (b.headers.length - a.headers.length);
                        });
                        const best = candidates.find(c => c.headers.length >= width)
                            || candidates.find(c => c.headers.length > 0);
                        if (!best) return [];
                        return uniqueHeaders(best.headers.slice(0, width));
                    };
                    const tables = deepQueryAll('table');
                    let best = { score: 0, rows: [] };

                    for (const table of tables) {
                        if (!isVisible(table)) continue;
                        const bodyRows = Array.from(table.querySelectorAll('tbody tr'))
                            .filter(tr => isVisible(tr));
                        const allRows = Array.from(table.querySelectorAll('tr'))
                            .filter(tr => isVisible(tr));
                        const dataRows = bodyRows.length ? bodyRows : allRows.filter(tr => {
                            const hasDataCells = tr.querySelector('td, [role="cell"], [role="gridcell"]');
                            const hasHeaderCells = tr.querySelector('th, [role="columnheader"]');
                            return hasDataCells && !hasHeaderCells;
                        });
                        const firstDataCells = dataRows.length ? rowCells(dataRows[0]) : [];
                        let headers = firstDataCells.length
                            ? chooseHeaders(table, firstDataCells.length)
                            : [];
                        const parsedRows = [];

                        for (const tr of dataRows) {
                            const cells = rowCells(tr);
                            if (cells.length < 2) continue;
                            if (cells.some(cell => /no matching records|no data/i.test(cell))) {
                                continue;
                            }
                            if (!headers.length || headers.length !== cells.length) {
                                headers = cells.map((_, index) => `column_${index + 1}`);
                            }
                            const row = {};
                            cells.forEach((cell, index) => {
                                row[headers[index] || `column_${index + 1}`] = cell;
                            });
                            parsedRows.push(row);
                        }

                        const namedHeaderBonus = headers.some(h => !/^column_\\d+$/.test(h)) ? 10 : 0;
                        const score = parsedRows.length * Math.max(headers.length, 1) + namedHeaderBonus;
                        if (parsedRows.length >= 2 && score > best.score) {
                            best = { score, rows: parsedRows };
                        }
                    }
                    return best.rows;
                }"""
            rows = await self.deps.evaluate_rows_with_frame_fallback(
                _table_page, _table_rows_js, log_tag="EXTRACT DOM"
            )
            if isinstance(rows, list) and rows:
                self.deps.logger.info("[EXTRACT DOM] visible table rows=%s", len(rows))
                return rows
        except Exception as table_err:
            self.deps.logger.debug("[EXTRACT DOM] table extraction skipped: %s", table_err)
        return []

    async def visible_table_signature(self, reason: str, scope=None) -> str:
        try:
            _sig_page = scope
            if _sig_page is None:
                _sig_page = await self.deps.browser._ensure_active_page(reason=reason)
            if _sig_page is None:
                return ""
            return await _sig_page.evaluate(
                """() => {
                    const clean = (value) => String(value || '')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    const isVisible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && rect.width > 0
                            && rect.height > 0;
                    };
                    // EXTRACT-SHADOW-1: keep parity with the deep table
                    // harvest so autopager verification sees the same table.
                    const deepQueryAll = (selector, root = document) => {
                        const out = Array.from(root.querySelectorAll(selector));
                        for (const host of root.querySelectorAll('*')) {
                            if (host.shadowRoot) out.push(...deepQueryAll(selector, host.shadowRoot));
                        }
                        return out;
                    };
                    const table = deepQueryAll('table')
                        .find(t => isVisible(t));
                    if (!table) return '';
                    return Array.from(table.querySelectorAll('tbody tr'))
                        .filter(tr => isVisible(tr))
                        .slice(0, 5)
                        .map(tr => clean(tr.innerText))
                        .join('|');
                }"""
            ) or ""
        except Exception:
            return ""

    async def auto_advance_table_page_via_dom(self, reason: str) -> bool:
        try:
            _page_for_next = await self.deps.browser._ensure_active_page(reason=reason)
            if _page_for_next is None:
                return False
            _pager_js = """() => {
                    const clean = (value) => String(value || '')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    const isVisible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && rect.width > 0
                            && rect.height > 0;
                    };
                    const isDisabled = (el) => {
                        const cls = String(el.className || '').toLowerCase();
                        return el.disabled
                            || el.getAttribute('aria-disabled') === 'true'
                            || cls.includes('disabled');
                    };
                    // AUTOPAGER-SHADOW-1: pager controls can live inside
                    // open shadow roots together with their table (the
                    // harvest + signature already walk them - SHADOW-1/2).
                    const deepQueryAll = (selector, root = document) => {
                        const out = Array.from(root.querySelectorAll(selector));
                        for (const host of root.querySelectorAll('*')) {
                            if (host.shadowRoot) out.push(...deepQueryAll(selector, host.shadowRoot));
                        }
                        return out;
                    };

                    const tables = deepQueryAll('table')
                        .filter(isVisible);
                    if (window.jQuery && window.jQuery.fn && window.jQuery.fn.dataTable) {
                        for (const table of tables) {
                            if (!window.jQuery.fn.dataTable.isDataTable(table)) {
                                continue;
                            }
                            const dt = window.jQuery(table).DataTable();
                            const info = dt.page.info();
                            if (info && info.page < info.pages - 1) {
                                dt.page('next').draw('page');
                                return {
                                    ok: true,
                                    method: 'datatables_api',
                                    page: info.page + 2,
                                    pages: info.pages
                                };
                            }
                        }
                    }

                    const nextText = /^(next|next page|>|›|»|→|下一页|下页)$/i;
                    const candidates = deepQueryAll(
                        'button,a,[role="button"],[role="link"]'
                    ).filter(isVisible);
                    for (const el of candidates) {
                        const label = clean(
                            el.innerText
                            || el.getAttribute('aria-label')
                            || el.getAttribute('title')
                            || el.textContent
                        );
                        if (!label || !nextText.test(label)) continue;
                        if (isDisabled(el)) continue;
                        el.scrollIntoView({block: 'center', inline: 'center'});
                        el.click();
                        return {ok: true, method: 'dom_next_button', label};
                    }

                    const current = candidates.find(el => {
                        const cls = String(el.className || '').toLowerCase();
                        const label = clean(el.innerText || el.textContent);
                        return /^\\d+$/.test(label)
                            && (cls.includes('current')
                                || cls.includes('active')
                                || el.getAttribute('aria-current') === 'page');
                    });
                    if (current) {
                        const currentNo = Number(clean(current.innerText || current.textContent));
                        const next = candidates.find(el => clean(el.innerText || el.textContent) === String(currentNo + 1));
                        if (next && !isDisabled(next)) {
                            next.scrollIntoView({block: 'center', inline: 'center'});
                            next.click();
                            return {ok: true, method: 'dom_numeric_page', page: currentNo + 1};
                        }
                    }

                    return {ok: false, method: 'not_found'};
                }"""
            _pager_wait_js = """(before) => {
                        const clean = (value) => String(value || '')
                            .replace(/\\s+/g, ' ')
                            .trim();
                        const isVisible = (el) => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0;
                        };
                        // AUTOPAGER-SHADOW-1: keep parity with the deep
                        // before-signature, or a shadow table's page flip
                        // can never be confirmed here.
                        const deepQueryAll = (selector, root = document) => {
                            const out = Array.from(root.querySelectorAll(selector));
                            for (const host of root.querySelectorAll('*')) {
                                if (host.shadowRoot) out.push(...deepQueryAll(selector, host.shadowRoot));
                            }
                            return out;
                        };
                        const table = deepQueryAll('table')
                            .find(t => isVisible(t));
                        if (!table) return false;
                        const after = Array.from(table.querySelectorAll('tbody tr'))
                            .filter(tr => isVisible(tr))
                            .slice(0, 5)
                            .map(tr => clean(tr.innerText))
                            .join('|');
                        return after && after !== before;
                    }"""
            scopes = [_page_for_next]
            main_frame = getattr(_page_for_next, "main_frame", None)
            for frame in list(getattr(_page_for_next, "frames", None) or []):
                if frame is main_frame:
                    continue
                try:
                    is_detached = getattr(frame, "is_detached", None)
                    if callable(is_detached) and is_detached():
                        continue
                except Exception:
                    continue
                scopes.append(frame)
            for scope in scopes:
                scope_label = (
                    "main"
                    if scope is _page_for_next
                    else (getattr(scope, "url", "") or "frame")
                )
                before_sig = await self.visible_table_signature(
                    "table autopager before", scope=scope
                )
                if not before_sig:
                    continue
                try:
                    result = await scope.evaluate(_pager_js)
                except Exception:
                    continue
                if not isinstance(result, dict) or not result.get("ok"):
                    continue
                # A pager was clicked in this scope: verify here and stop -
                # probing further scopes after a click risks double-paging.
                try:
                    await scope.wait_for_function(
                        _pager_wait_js, arg=before_sig, timeout=3000
                    )
                except Exception:
                    after_sig = await self.visible_table_signature(
                        "table autopager after", scope=scope
                    )
                    if not after_sig or after_sig == before_sig:
                        self.deps.logger.info(
                            "[TABLE AUTOPAGER] clicked but visible table "
                            "signature did not change (%s): %s",
                            scope_label,
                            result,
                        )
                        return False
                self.deps.logger.info(
                    "[TABLE AUTOPAGER] advanced page via %s (scope=%s)",
                    result,
                    scope_label,
                )
                return True
            return False
        except Exception as pager_err:
            self.deps.logger.debug("[TABLE AUTOPAGER] skipped: %s", pager_err)
            return False

    async def probe_scroll_drain_state(self, reason: str) -> dict:
        """Return whether the current page/main scroll container is physically drained."""
        try:
            _probe_page = await self.deps.browser._ensure_active_page(reason=reason)
            return await _probe_page.evaluate(
                """() => {
                    const viewportW = window.innerWidth || 0;
                    const viewportH = window.innerHeight || 0;
                    const doc = document.scrollingElement || document.documentElement || document.body;
                    const docRemaining = Math.max(
                        0,
                        (doc.scrollHeight || 0) - ((window.scrollY || doc.scrollTop || 0) + viewportH)
                    );
                    const docScrollable = (doc.scrollHeight || 0) > viewportH + 80;

                    const visibleRect = (el) => {
                        if (!el || !el.getBoundingClientRect) return null;
                        const r = el.getBoundingClientRect();
                        const w = Math.max(0, Math.min(r.right, viewportW) - Math.max(r.left, 0));
                        const h = Math.max(0, Math.min(r.bottom, viewportH) - Math.max(r.top, 0));
                        if (w < 160 || h < 120) return null;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') return null;
                        const overflowY = style.overflowY || '';
                        const scrollable = /(auto|scroll|overlay)/i.test(overflowY)
                            && el.scrollHeight > el.clientHeight + 80;
                        if (!scrollable) return null;
                        return {el, area: w * h, w, h};
                    };

                    const candidates = Array.from(document.querySelectorAll('main, [role="main"], table, tbody, .el-table__body-wrapper, .ant-table-body, .v-data-table__wrapper, [class*="table"], [class*="list"], [class*="content"], div'))
                        .map(visibleRect)
                        .filter(Boolean)
                        .sort((a, b) => b.area - a.area);
                    const best = candidates[0] || null;
                    const containerRemaining = best
                        ? Math.max(0, best.el.scrollHeight - best.el.scrollTop - best.el.clientHeight)
                        : 0;
                    const containerCanScroll = Boolean(best && containerRemaining > 80);
                    const windowCanScroll = Boolean(docScrollable && docRemaining > 80);
                    const atBottom = !windowCanScroll && !containerCanScroll;
                    return {
                        at_bottom: atBottom,
                        window_remaining: Math.round(docRemaining),
                        window_can_scroll: windowCanScroll,
                        container_remaining: Math.round(containerRemaining),
                        container_can_scroll: containerCanScroll,
                        container_tag: best ? String(best.el.tagName || '').toLowerCase() : '',
                        container_class: best ? String(best.el.className || '').slice(0, 80) : '',
                    };
                }"""
            ) or {"at_bottom": False, "probe_failed": True}
        except Exception as probe_err:
            self.deps.logger.debug("[EXTRACT DEDUP] scroll drain probe failed: %s", probe_err)
            return {"at_bottom": False, "probe_failed": True}

    async def compute_data_shape_with_drain(self, reason: str) -> dict:
        """Probe the page data shape, attaching a physical scroll-drain
        override for dense pages (>=10 expected rows). Verbatim relocation of
        the block duplicated across run_agent's auto- and explicit-extract
        paths; the only per-call difference was the drain probe ``reason``.
        """
        data_shape: dict = {}
        try:
            data_shape = await self.deps.browser.probe_data_shape()
            self.deps.logger.info("[DATA SHAPE] %s", data_shape)
            if self.expected_rows_from_data_shape(data_shape) >= 10:
                drain_state = await self.probe_scroll_drain_state(reason)
                data_shape = dict(data_shape)
                data_shape["physically_drained"] = bool(drain_state.get("at_bottom"))
                data_shape["drain_state"] = drain_state
                self.deps.logger.info("[DATA SHAPE] dense drain_state=%s", drain_state)
        except Exception as shape_err:
            self.deps.logger.debug("[DATA SHAPE] skipped: %s", shape_err)
        return data_shape

    async def gather_dom_list_card_candidates(
        self,
        candidates: list,
        *,
        dom_list_rows,
        dom_list_text,
        data_shape,
        card_base_texts,
        fallback_source_text,
        body_text_reason,
    ) -> None:
        """Append DOM_CARDS (when semantic cards extract) + DOM_LIST candidates.

        Verbatim relocation of the block duplicated across run_agent's auto-
        and explicit-extract paths. The three per-call differences are passed
        in: ``card_base_texts`` (texts prepended to the card source ahead of
        the list text), ``fallback_source_text`` (used when ``dom_list_text``
        is empty) and ``body_text_reason`` (the semantic-card body-text probe
        reason). Mutates the caller-owned ``candidates`` list in place; no other
        caller local is touched.
        """
        if not dom_list_rows:
            return
        card_source_text = "\n\n".join(
            text for text in (*card_base_texts, dom_list_text) if text
        )
        dom_card_rows, dom_card_text = extract_semantic_card_rows(
            dom_list_rows,
            source_text=card_source_text,
            requested_fields=self.deps.requested_output_fields,
            goal=self.deps.goal,
        )
        if not dom_card_rows:
            dom_card_body_text = await self.extract_body_text_for_semantic_cards(
                body_text_reason
            )
            if dom_card_body_text:
                dom_card_rows, dom_card_text = extract_semantic_card_rows(
                    dom_list_rows,
                    source_text="\n\n".join(
                        text
                        for text in (dom_card_body_text, card_source_text)
                        if text
                    ),
                    requested_fields=self.deps.requested_output_fields,
                    goal=self.deps.goal,
                )
        if dom_card_rows:
            self.deps.logger.info(
                "[EXTRACT DOM] semantic cards rows=%s source_chars=%s",
                len(dom_card_rows),
                len(dom_card_text or dom_list_text or ""),
            )
            candidates.append(
                self.sanitize_extraction_candidate(
                    name="DOM_CARDS",
                    data=dom_card_rows,
                    source_text=dom_card_text or dom_list_text or fallback_source_text,
                    data_shape=data_shape,
                )
            )
        candidates.append(
            self.sanitize_extraction_candidate(
                name="DOM_LIST",
                data=dom_list_rows,
                source_text=dom_list_text or fallback_source_text,
                data_shape=data_shape,
            )
        )

    async def select_and_commit_extraction(
        self,
        *,
        candidates: list,
        current_url: str,
        current_extract_page_key: str,
        source_text_for_validation: str,
        log_extract_text_source: str,
    ) -> ExtractCommit:
        """Choose the best candidate, commit it, and derive the DOM_TABLE page
        key. Verbatim relocation of run_agent's explicit-extract choose/commit
        block (formerly main.py 6410-6432, minus the trailing
        ``decision['extracted_data']`` write the caller keeps). When no
        candidate is chosen, the prior source_text / log_source / page_key are
        echoed back unchanged (the original ``else`` left them untouched). The
        auto-extract path's choose/commit block has diverged (different locals,
        no DOM_TABLE key) and is left in place.
        """
        chosen = self.choose_best_extraction_candidate(candidates)
        if chosen:
            (
                extracted,
                new_rows,
                dup_rows,
                rejected_rows,
                source_text_for_validation,
            ) = self.commit_extraction_candidate(chosen)
            log_extract_text_source = str(
                chosen.get("name") or "VLM_EXTRACT_OUTPUT"
            )
            if log_extract_text_source == "DOM_TABLE":
                dom_sig = await self.visible_table_signature(
                    "extract DOM page signature"
                )
                if dom_sig:
                    current_extract_page_key = (
                        f"{current_url}#table:"
                        f"{hashlib.md5(dom_sig.encode('utf-8', errors='ignore')).hexdigest()}"
                    )
        else:
            extracted, new_rows, dup_rows, rejected_rows = [], 0, 0, 0
        return ExtractCommit(
            extracted=extracted,
            new_rows=new_rows,
            dup_rows=dup_rows,
            rejected_rows=rejected_rows,
            source_text_for_validation=source_text_for_validation,
            log_extract_text_source=log_extract_text_source,
            current_extract_page_key=current_extract_page_key,
        )

    async def persist_extracted_batch(
        self,
        *,
        extracted: list,
        new_rows: int,
        dup_rows: int,
        rejected_rows: int,
        log_extract_text_source: str,
        source_text_for_validation: str,
        candidates: list,
        data_shape: dict,
        current_url: str,
        current_extract_page_key: str,
        step: int,
    ) -> ExtractPersist:
        """Enrich, save, snapshot and count a successful explicit-extract batch.
        Verbatim relocation of run_agent's explicit persist block (formerly
        main.py ~6436-6515). Writes the shared ``_xs`` counters; returns the
        final rows + saved/snapshot paths + progress for the caller to write
        back into ``decision`` and reset the dedup streak. The auto-extract
        persist block has diverged (different locals / produced_by / no decision
        writes) and is left in place.
        """
        if dup_rows or rejected_rows:
            self.deps.logger.info(
                "[EXTRACT DEDUP] filtered %s duplicate rows, "
                "rejected %s unsupported rows, saving %s new rows",
                dup_rows,
                rejected_rows,
                new_rows,
            )

        # 统计本次新增行数。tooltip 任务按 trigger 主键统计，避免
        # 中间半成品被 UPSERT 覆盖后仍显示累计过高。
        extracted = await self.enrich_rows_with_dom_links(extracted)
        progress_new_rows, progress_total_rows = self.record_extract_progress(
            extracted,
            new_rows,
        )
        self.deps.logger.info(
            f"[EXTRACT] 本次提取 {new_rows} 条，"
            f"累计已提取 {progress_total_rows} 条"
        )
        saved_path = ""
        if self.deps.goal_output_mode == "answer":
            self.deps.logger.info(
                "[ANSWER OUTPUT] answer-only result; not saving Excel artifact"
            )
        else:
            saved_path = save_run_dataset(
                extracted,
                run_id=self.deps.run_ts,
                output_contract=self.deps.goal_output_contract,
                produced_by="vlm_extract",
                step_id=str(step),
                filename_hint=self.deps.vlm_output,
                unique_key=TOOLTIP_UNIQUE_KEY if _goal_is_tooltip_extract(self.deps.goal) else None,
            )
        api_fast = await self.try_dom_api_fast_path(
            extracted,
            source=log_extract_text_source or "VLM_EXTRACT_OUTPUT",
        )
        if api_fast.get("applied"):
            extracted = api_fast.get("fast_path", {}).get("rows") or extracted
            progress_total_rows = self.state.total_extracted_rows
        snapshot_path = await self.save_extraction_snapshot(
            source=log_extract_text_source or "VLM_EXTRACT_OUTPUT",
            rows=extracted,
            output_file=str(saved_path or ""),
            accepted_rows=new_rows,
            duplicate_rows=dup_rows,
            rejected_rows=rejected_rows,
            candidates=candidates,
            data_shape=data_shape,
            source_text=source_text_for_validation,
            metadata={
                "mode": "explicit_extract",
                "progress_new_rows": progress_new_rows,
                "progress_total_rows": progress_total_rows,
            },
        )
        if saved_path:
            self.deps.logger.info(f"[EXTRACT] Saved to: {saved_path}")
        if _goal_is_tooltip_extract(self.deps.goal):
            print(
                f"\033[1;32m✅ [EXTRACT]\033[0m "
                f"成功合并 \033[36m{new_rows}\033[0m 条候选。"
                f"当前唯一提示项: \033[36m{progress_total_rows}\033[0m 条"
            )
        else:
            print(
                f"\033[1;32m✅ [EXTRACT]\033[0m "
                f"成功追加 \033[36m{new_rows}\033[0m 条数据。"
                f"当前总计: \033[36m{progress_total_rows}\033[0m 条"
            )
        self.state.extract_count += 1
        self.state.extracted_page_urls.add(current_url)
        self.state.extracted_page_keys.add(current_extract_page_key)
        return ExtractPersist(
            extracted=extracted,
            saved_path=str(saved_path or ""),
            snapshot_path=str(snapshot_path or ""),
            progress_new_rows=progress_new_rows,
            progress_total_rows=progress_total_rows,
        )

    async def arm_pagination_after_extract(
        self,
        *,
        new_rows: int,
        log_extract_text_source: str,
        data_shape: dict,
    ) -> None:
        """First-flip hard constraint + pagination probe + re-arm after a
        successful explicit extract. Verbatim relocation of the block from
        run_agent's explicit-extract path (formerly main.py ~6637-6773). Writes
        only ``self.state`` (pagination flags/hints); the three per-call inputs
        are passed in. The auto-extract path keeps its own near-identical block
        (the two have diverged in hint-message wording, so they are not merged).
        """
        # ── 首翻引擎硬约束：首次 extract 后强制下一步 next_page ──
        # Bug 修复：用任务级永久锁，避免翻页后 extract_count 重置反复触发
        if (
            not self.state.first_extract_ever_done
            and _should_force_first_flip_after_successful_extract(self.deps.goal)
        ):
            self.state.first_extract_ever_done = True
            self.state.first_flip_pending = True
        # ── Improvement 1：首次 extract 后探测分页器（显式 extract 路径） ──
        if (
            _goal_needs_pagination_probe(self.deps.goal)
            and not self.state.pagination_probed
            and self.state.extract_count == 1
        ):
            self.state.pagination_probed = True
            try:
                _probe = await self.deps.browser.probe_pagination()
                self.state.pagination_kind = _probe.get("kind", "")
                _cands = _probe.get("candidates", [])
                if _probe.get("has_paginator"):
                    _names = ", ".join(
                        f"{c['ref']}={c['name']!r}" for c in _cands[:6]
                    )
                    _should_force_probe_next, _force_probe_reason = (
                        _should_schedule_next_page_after_extract(
                            self.deps.goal,
                            new_rows=new_rows,
                            total_rows=self.state.total_extracted_rows,
                            extract_source=log_extract_text_source,
                            pagination_kind=self.state.pagination_kind,
                            expected_rows=self.expected_rows_from_data_shape(data_shape),
                            physically_drained=bool(data_shape.get("physically_drained")),
                        )
                    )
                    if _should_force_probe_next:
                        self.state.force_next_page_pending = True
                        self.state.first_flip_pending = False
                        self.state.block_next_page_until_drained = False
                        self.state.block_next_page_reason = ""
                        self.deps.logger.info(
                            "[PROBE PAGE] armed next_page after extract: %s",
                            _force_probe_reason,
                        )
                        self.deps.broadcast_log_safe(
                            f"[PROBE PAGE] 已发现分页器，下一轮直接 next_page：{_force_probe_reason}",
                            level="info",
                        )
                        self.state.pagination_hint_msg = (
                            f"📍【系统探测：本页**带分页器**（{self.state.pagination_kind}）】\n"
                            f"已确认页面底部存在翻页控件：{_names}。\n"
                            f"当前页已提取到足够完整的一批数据（{_force_probe_reason}）。"
                            f"下一步**必须**用 next_page（首选 URL Mutation）翻页，"
                            f"**禁止** smooth_scroll 当无限滚动处理。"
                        )
                    else:
                        _drain_state = await self.probe_scroll_drain_state(
                            "pagination probe low-yield explicit extract"
                        )
                        if not bool(_drain_state.get("at_bottom")):
                            self.state.block_next_page_until_drained = True
                            self.state.block_next_page_reason = _force_probe_reason
                        else:
                            self.state.force_next_page_pending = True
                            self.state.first_flip_pending = False
                            self.state.block_next_page_until_drained = False
                            self.state.block_next_page_reason = ""
                            _force_probe_reason = (
                                f"{_force_probe_reason}; physical bottom reached"
                            )
                        self.state.pagination_hint_msg = (
                            f"📍【系统探测：本页**带分页器**（{self.state.pagination_kind}）】\n"
                            f"已确认页面存在翻页控件：{_names}。\n"
                            f"但本次仅新增 {new_rows} 条（{_force_probe_reason}），"
                            "不足以证明当前页已提取完。\n"
                            "下一步请先 smooth_scroll 向下并继续 extract 当前页；"
                            "只有当前页物理触底或无新增后，才使用 next_page。"
                        )
                else:
                    # No paginator detected — mark as infinite scroll
                    self.state.page_is_infinite_scroll = True
                    _should_force_probe_next, _force_probe_reason = (
                        _should_schedule_next_page_after_extract(
                            self.deps.goal,
                            new_rows=new_rows,
                            total_rows=self.state.total_extracted_rows,
                            extract_source=log_extract_text_source,
                            pagination_kind=self.state.pagination_kind,
                            expected_rows=self.expected_rows_from_data_shape(data_shape),
                            physically_drained=bool(data_shape.get("physically_drained")),
                        )
                    )
                    if _should_force_probe_next:
                        self.state.force_next_page_pending = True
                        self.state.first_flip_pending = False
                        self.state.block_next_page_until_drained = False
                        self.state.block_next_page_reason = ""
                        self.deps.logger.info(
                            "[PROBE PAGE] armed universal next_page after extract: %s",
                            _force_probe_reason,
                        )
                        self.deps.broadcast_log_safe(
                            f"[PROBE PAGE] 大批量提取未达量，下一轮交给 next_page 宏动作：{_force_probe_reason}",
                            level="info",
                        )
                        self.state.pagination_hint_msg = (
                            "📍【系统探测：本页**无分页器**（infinite 模式）】\n"
                            "当前提取批次已足够大但目标未达成。"
                            "下一步使用 next_page 宏动作；如果确实没有分页器，"
                            "底层会自动走 L4 滚动兜底加载新数据。"
                        )
                    else:
                        self.state.pagination_hint_msg = (
                            "📍【系统探测：本页**无分页器**】\n"
                            f"本次仅新增 {new_rows} 条（{_force_probe_reason}），"
                            "请继续 smooth_scroll / extract 当前列表；"
                            "如果滚动触底且仍未达量，引擎会再调度 next_page 宏动作。"
                        )
                self.deps.logger.info(
                    f"[PROBE PAGE] kind={self.state.pagination_kind} cands={len(_cands)}"
                )
            except Exception as _probe_err:
                self.deps.logger.warning(f"[PROBE PAGE] 失败忽略：{_probe_err}")
        # ── Re-arm：已知分页器 + 目标未达 → 每次 extract 后强制 next_page ──
        if (
            self.state.pagination_probed
            and self.state.pagination_kind not in ("", "infinite")
            and not self.state.page_is_infinite_scroll
            and not self.state.force_next_page_pending
        ):
            _rearm_target = _parse_goal_target_count(self.deps.goal)
            if _rearm_target is not None and self.state.total_extracted_rows < _rearm_target:
                self.state.force_next_page_pending = True
                self.deps.logger.info(
                    "[REARM NEXT_PAGE] paginator known (%s), target not met "
                    "(%s/%s); re-armed for next step",
                    self.state.pagination_kind,
                    self.state.total_extracted_rows,
                    _rearm_target,
                )

    async def handle_zero_new_rows_feedback(
        self,
        *,
        duplicate_zero_extract_streak: int,
        dup_rows: int,
        rejected_rows: int,
        current_url: str,
        data_shape: dict,
    ) -> int:
        """Inject dedup/no-new-rows VLM feedback for the explicit-extract path
        when row-level filtering yielded 0 new rows. Verbatim relocation of the
        block from run_agent's explicit path (formerly main.py 6435-6519);
        writes pagination flags to ``self.state`` and returns the updated
        ``duplicate_zero_extract_streak``. The caller keeps the
        ``if _new_rows == 0`` guard, the ``_dedup_tripped_last_step = True``
        flag, and the trailing ``break``. The auto-extract path's zero-row block
        is structurally different (no target/streak/drain branches, ends in
        ``continue``) and is intentionally left in place.
        """
        self.deps.logger.warning(
            "[EXTRACT DEDUP] no new rows after row-level filtering "
            "(duplicates=%s, rejected=%s, url=%s)",
            dup_rows,
            rejected_rows,
            current_url,
        )
        _target_count_pre = _parse_goal_target_count(self.deps.goal)
        _pre_reached = (
            _target_count_pre is not None
            and self.state.total_extracted_rows >= _target_count_pre
        )
        if _pre_reached:
            self.deps.vlm.inject_error_feedback(
                f"✅ 你已累计提取 {self.state.total_extracted_rows} 条数据，"
                f"已达成用户要求的 {_target_count_pre} 条。"
                "请立即输出 action=done 结束任务，不要再 extract。"
            )
        else:
            _has_prior_extract_page = bool(
                self.state.extracted_page_urls or self.state.extracted_page_keys
            )
            if _target_count_pre is not None and _has_prior_extract_page:
                duplicate_zero_extract_streak += 1
                _scroll_drain = await self.probe_scroll_drain_state(
                    "duplicate extract drain probe"
                )
                _physically_drained = bool(_scroll_drain.get("at_bottom"))
                _probe_failed = bool(_scroll_drain.get("probe_failed"))
                if _physically_drained or (_probe_failed and duplicate_zero_extract_streak >= 3):
                    self.state.first_flip_pending = True
                    _drain_reason = (
                        "物理触底"
                        if _physically_drained
                        else "触底探测失败且连续多次无新增"
                    )
                    self.deps.vlm.inject_error_feedback(
                        f"⚠️ 系统 extract 净新增为 0，且已确认{_drain_reason}。\n"
                        f"当前累计 {self.state.total_extracted_rows}/{_target_count_pre} 条，"
                        "说明当前页/当前滚动区域已基本榨干但目标尚未达成。\n"
                        "下一步必须执行 next_page（target_id=0, type_value=\"\"），"
                        "让底层优先尝试 URL 变异/分页器/页码；不要继续 smooth_scroll "
                        "或重复 extract 当前页。"
                    )
                else:
                    _remaining_hint = (
                        f"window_remaining={_scroll_drain.get('window_remaining')}, "
                        f"container_remaining={_scroll_drain.get('container_remaining')}"
                    )
                    self.deps.vlm.inject_error_feedback(
                        "⚠️ 系统执行了 extract，但行级去重发现没有新增数据。\n"
                        f"当前累计 {self.state.total_extracted_rows}/{_target_count_pre} 条，"
                        "这只能证明当前视口没有新行，尚不能证明整页已榨干。\n"
                        f"物理滚动探测显示仍有下滑空间（{_remaining_hint}）。"
                        "下一步先 smooth_scroll down 暴露同页下方隐藏数据；"
                        "只有净新增为 0 且物理触底后，系统才会强制 next_page。"
                    )
                    await self.nudge_scroll_after_duplicate_extract(
                        "first duplicate extract before pagination"
                    )
            else:
                duplicate_zero_extract_streak += 1
                _expected_dense_rows = self.expected_rows_from_data_shape(
                    data_shape
                )
                if _expected_dense_rows >= 10:
                    self.state.block_next_page_until_drained = True
                    self.state.block_next_page_reason = (
                        f"dense page exposes about {_expected_dense_rows} rows, "
                        "but viewport/full extraction under-yielded"
                    )
                    self.deps.vlm.inject_error_feedback(
                        "⚠️ 系统探头发现当前页存在密集列表/表格，"
                        f"大约 {_expected_dense_rows} 个结构化条目；"
                        "但本次 extract 没有得到足够新增行。\n"
                        "这说明当前页尚未被可靠提取，下一步先 smooth_scroll down "
                        "或重新 extract 当前页，禁止直接 next_page。"
                    )
                else:
                    self.deps.vlm.inject_error_feedback(
                        "⚠️ 系统执行了 extract，但行级去重发现没有新增数据。\n"
                        "请不要重复提取当前列表。下一步优先 next_page；"
                        "若 next_page 报错，再考虑 smooth_scroll 加载更多。"
                    )
                await self.nudge_scroll_after_duplicate_extract(
                    "explicit extract duplicate rows"
                )
        return duplicate_zero_extract_streak

    def inject_post_extract_pagination_guidance(self) -> None:
        """Inject smart pagination/done VLM guidance after a successful explicit
        extract, based on pages extracted so far vs the row-count target.
        Verbatim relocation of the block from run_agent's explicit path
        (formerly main.py ~6622-6690): pure read of ``self.state`` + browser,
        no control flow, no state writes. The auto-extract path keeps its own
        divergent block (``_auto_pages`` = urls only, final ``else`` instead of
        ``elif extract_count > 0``, different message text), so they are not
        merged.
        """
        _n_pages = max(
            len(self.state.extracted_page_urls), len(self.state.extracted_page_keys)
        )
        _target_count_b = _parse_goal_target_count(self.deps.goal)
        _reached_target_b = (
            _target_count_b is not None
            and self.state.total_extracted_rows >= _target_count_b
        )
        if _reached_target_b:
            self.deps.vlm.inject_error_feedback(
                f"✅ 你已成功提取 {_n_pages} 个不同页面的数据"
                f"（累计 {self.state.total_extracted_rows} 条）。\n"
                f"用户要求获取 {_target_count_b} 条数据，"
                f"当前已达到目标！请立即输出 done 结束任务。"
            )
        elif _n_pages >= 2 and _target_count_b is not None:
            _pag_links_c = self.deps.browser.find_pagination_links()
            if _pag_links_c:
                _pag_hint_c = "\n".join(
                    f"  → [ID: {p['id']}] {p['role']}: \"{p['name']}\""
                    for p in _pag_links_c
                )
                self.deps.vlm.inject_error_feedback(
                    f"✅ 你已成功提取 {_n_pages} 个页面"
                    f"（累计 {self.state.total_extracted_rows} 条），"
                    f"但用户要求 {_target_count_b} 条，"
                    f"还差 {_target_count_b - self.state.total_extracted_rows} 条。\n"
                    f"系统发现了翻页链接：\n{_pag_hint_c}\n"
                    f"【立即操作】请继续翻页，例如："
                    f"click(target_id={_pag_links_c[0]['id']})"
                )
            else:
                self.deps.vlm.inject_error_feedback(
                    f"✅ 你已成功提取 {_n_pages} 个页面"
                    f"（累计 {self.state.total_extracted_rows} 条），"
                    f"但用户要求 {_target_count_b} 条，"
                    f"还差 {_target_count_b - self.state.total_extracted_rows} 条。\n"
                    "请向下滚动查找翻页按钮后继续翻页提取。"
                )
        elif _n_pages >= 2:
            self.deps.vlm.inject_error_feedback(
                f"✅ 你已成功提取 {_n_pages} 个不同页面的数据"
                f"（累计 {self.state.total_extracted_rows} 条）。\n"
                "请仔细回顾用户的原始任务要求，"
                "判断是否需要继续翻页提取更多数据。\n"
                "如果已满足用户需求，请输出 done 结束任务。"
            )
        elif self.state.extract_count > 0:
            _pag_links_b = self.deps.browser.find_pagination_links()
            if _pag_links_b:
                _pag_hint_b = "\n".join(
                    f"  → [ID: {p['id']}] {p['role']}: \"{p['name']}\""
                    for p in _pag_links_b
                )
                self.deps.vlm.inject_error_feedback(
                    f"✅ 你已成功提取当前页数据"
                    f"（第 {_n_pages} 个页面，累计 {self.state.total_extracted_rows} 条）。\n"
                    f"系统在当前页面发现了以下翻页链接：\n{_pag_hint_b}\n"
                    f"【立即操作】请点击翻页链接加载下一页，例如："
                    f"click(target_id={_pag_links_b[0]['id']})\n"
                    f"⚠️ 必须使用上述精确的 ID，不要猜测其他 ID！"
                )
            else:
                self.deps.vlm.inject_error_feedback(
                    f"✅ 你已成功提取当前页数据"
                    f"（第 {_n_pages} 个页面，累计 {self.state.total_extracted_rows} 条）。\n"
                    "当前页面未发现翻页链接，可能已是最后一页。\n"
                    "如果任务还需要更多数据，请尝试向下滚动查找翻页按钮。\n"
                    "如果已完成所有页的提取，请直接输出 done 结束任务。"
                )

    async def detect_canvas_grid(self, reason: str) -> dict:
        """Detect a dominant canvas/svg-rendered grid (EXTRACT-CANVAS-1).

        Sheet engines (Luckysheet / Univer / Handsontable canvas mode /
        x-spreadsheet, ECharts/AntV dashboards) paint rows onto a canvas,
        so every DOM harvest legitimately comes back empty. Returns {}
        when no large canvas/svg exists; otherwise evidence plus fallback
        guidance the planner can act on.
        """
        _canvas_js = """() => {
                const vw = window.innerWidth || 1;
                const vh = window.innerHeight || 1;
                const els = Array.from(document.querySelectorAll('canvas, svg'));
                let best = null;
                for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 500 || r.height < 300) continue;
                    const coverage = (r.width * r.height) / (vw * vh);
                    const cls = [
                        el.className && el.className.baseVal !== undefined
                            ? el.className.baseVal : el.className,
                        el.id,
                        el.parentElement ? el.parentElement.className : '',
                        el.parentElement ? el.parentElement.id : ''
                    ].map(v => String(v || '')).join(' ').toLowerCase();
                    const gridLike = /(grid|table|sheet|spread|cell|excel|luckysheet|univer|handsontable)/.test(cls);
                    const score = coverage + (gridLike ? 1 : 0);
                    if (!best || score > best.score) {
                        best = {
                            score,
                            tag: String(el.tagName || '').toLowerCase(),
                            width: Math.round(r.width),
                            height: Math.round(r.height),
                            coverage: Math.round(coverage * 100) / 100,
                            grid_like: gridLike,
                            class_hint: cls.replace(/\\s+/g, ' ').trim().slice(0, 120)
                        };
                    }
                }
                if (!best) return {found: false};
                delete best.score;
                return {found: true, canvas_count: els.length, ...best};
            }"""
        try:
            _cv_page = await self.deps.browser._ensure_active_page(reason=reason)
            if _cv_page is None:
                return {}
            info = await _cv_page.evaluate(_canvas_js)
        except Exception as cv_err:
            self.deps.logger.debug("[PRE-EXTRACT] canvas grid probe failed: %s", cv_err)
            return {}
        if not isinstance(info, dict) or not info.get("found"):
            return {}
        notice = dict(info)
        notice["guidance"] = (
            "页面主体由 canvas/svg 渲染（DOM 无行可抽），确定性抽取不可用。"
            "按优先级兜底：1) 找「导出/下载 CSV/Excel」按钮走 data_export；"
            "2) 找「表格视图/列表模式」开关切回 DOM 渲染再抽取；"
            "3) 都没有时才用截图视觉抽取，并在结果中明确告知用户精度受限。"
        )
        return notice

    def sanitize_extraction_candidate(self, 
        *,
        name: str,
        data,
        source_text: str = "",
        data_shape: dict | None = None,
    ) -> dict:
        target_count = _parse_goal_target_count(self.deps.goal)
        target_remaining = (
            None if target_count is None
            else max(0, target_count - self.state.total_extracted_rows)
        )
        raw_data = data
        if self.deps.requested_output_fields and isinstance(data, list):
            filtered_rows, schema_stats = self.deps.data_controller.filter_undercomplete_rows(
                data,
                normalize_row=lambda row: (
                    self.normalize_extracted_row_fields([row], project=True)[0]
                ),
            )
            if schema_stats.get("dropped"):
                self.deps.logger.info(
                    "[EXTRACT SCHEMA] %s dropped %s under-complete rows "
                    "(required_hits=%s/%s)",
                    name,
                    schema_stats.get("dropped"),
                    schema_stats.get("required_hits"),
                    schema_stats.get("total_fields"),
                )
            raw_data = filtered_rows
        trial_seen = set(self.state.seen_extract_row_keys)
        result = sanitize_extracted_rows(
            raw_data=raw_data,
            source_text=source_text,
            seen_fingerprints=trial_seen,
            target_remaining=target_remaining,
        )
        rows = result.rows
        completeness = 0.0
        if rows:
            widths = []
            for row in rows:
                if isinstance(row, dict):
                    widths.append(
                        sum(
                            1
                            for value in row.values()
                            if value is not None and str(value).strip()
                        )
                    )
            completeness = (
                sum(widths) / max(len(widths), 1)
                if widths else 1.0
            )
        shape = data_shape or {}
        source = name.upper()
        score = result.accepted * 100.0 + completeness * 5.0
        score -= result.duplicates * 8.0
        score -= result.rejected_total * 12.0
        if "DOM_CARDS" in source:
            score += 58.0
            if result.accepted >= 2:
                score += 12.0
        elif "DOM_LIST" in source:
            if int(shape.get("repeated_list_items") or 0) >= result.accepted >= 2:
                score += 45.0
            else:
                score += 25.0
        elif "DOM_TABLE" in source:
            if int(shape.get("table_rows") or 0) >= result.accepted >= 2:
                score += 35.0
            else:
                score += 12.0
        elif "FULL_PAGE" in source or "AX_TREE" in source or "INNER_TEXT" in source:
            if int(shape.get("repeated_class_count") or 0) >= 5:
                score += 25.0
            if result.accepted >= 10:
                score += 20.0
        elif "VIEWPORT" in source or "VLM" in source:
            score += 3.0

        return {
            "name": name,
            "rows": rows,
            "accepted": result.accepted,
            "duplicates": result.duplicates,
            "rejected": result.rejected_total,
            "fingerprints": set(result.fingerprints),
            "score": score,
            "source_text": source_text,
            "data_shape": shape,
            "data_signature": self.deps.data_controller.rows_signature(rows),
        }

    def expected_rows_from_data_shape(self, data_shape: dict | None) -> int:
        """Estimate how many structured rows the current page physically exposes.

        This is a guardrail for dense list/table pages: if the DOM clearly
        contains ~25 repeated items, a viewport-only 3-row extraction should
        not be treated as a complete page batch.
        """
        shape = data_shape or {}
        try:
            table_rows = int(shape.get("table_rows") or 0)
            table_cells = int(shape.get("table_cells") or 0)
            repeated = int(shape.get("repeated_class_count") or 0)
            repeated_avg_text = int(shape.get("repeated_avg_text") or 0)
        except (TypeError, ValueError):
            return 0

        expected = 0
        if table_rows >= 3 and table_cells >= 2:
            expected = max(expected, table_rows)
        if repeated >= 5 and repeated_avg_text >= 20:
            expected = max(expected, repeated)
        return expected

    def candidate_min_expected_rows(self, candidate: dict) -> int:
        target_count = _parse_goal_target_count(self.deps.goal)
        target_remaining = (
            None if target_count is None
            else max(0, target_count - self.state.total_extracted_rows)
        )
        expected_rows = self.expected_rows_from_data_shape(
            candidate.get("data_shape") or {}
        )
        if expected_rows < 10:
            return 0
        # Dense pages should usually be extracted as a page batch, but if
        # the remaining target is small we only require that many rows.
        # Use magnitude matching, not strict equality: DOM probes may count
        # ads, skeleton rows, placeholders, or hidden repeated nodes.
        page_floor = max(10, int(expected_rows * 0.7))
        if target_remaining is not None:
            return min(expected_rows, target_remaining, page_floor)
        return min(expected_rows, page_floor)

    def is_under_yield_viewport_candidate(self, candidate: dict) -> bool:
        source = str(candidate.get("name") or "").upper()
        if "VIEWPORT" not in source and "VLM" not in source:
            return False
        shape = candidate.get("data_shape") or {}
        if bool(shape.get("physically_drained")):
            return False
        minimum = self.candidate_min_expected_rows(candidate)
        if minimum <= 0:
            return False
        return int(candidate.get("accepted") or 0) < minimum

    def choose_best_extraction_candidate(self, candidates: list[dict]) -> dict | None:
        viable = [c for c in candidates if c.get("accepted", 0) > 0]
        if not viable:
            return None
        filtered: list[dict] = []
        for c in viable:
            if self.is_under_yield_viewport_candidate(c):
                self.deps.logger.info(
                    "[EXTRACT ARBITER] reject under-yield viewport candidate: "
                    "accepted=%s min_expected=%s shape=%s",
                    c.get("accepted"),
                    self.candidate_min_expected_rows(c),
                    c.get("data_shape"),
                )
                continue
            filtered.append(c)
        viable = filtered
        if not viable:
            return None
        def _candidate_priority(candidate: dict) -> int:
            source = str(candidate.get("name") or "").upper()
            if self.deps.goal_output_mode == "answer":
                if "VIEWPORT" in source or "VLM" in source:
                    return 6
                if "FULL_PAGE" in source or "AX_TREE" in source or "INNER_TEXT" in source:
                    return 5
                if "DOM_CARDS" in source:
                    return 3
                if "DOM_TABLE" in source:
                    return 2
                if "DOM_LIST" in source:
                    return 1
                return 0
            if "DOM_CARDS" in source:
                return 5
            if "DOM_LIST" in source:
                return 4
            if "DOM_TABLE" in source:
                return 3
            if "FULL_PAGE" in source or "LIST_ITEMS_TEXT" in source:
                return 2
            if "VIEWPORT" in source or "VLM" in source:
                return 1
            return 0

        if self.deps.goal_output_mode == "answer":
            viable.sort(
                key=lambda c: (
                    _candidate_priority(c),
                    float(c.get("score") or 0),
                    -int(c.get("accepted") or 0),
                ),
                reverse=True,
            )
        else:
            viable.sort(
                key=lambda c: (
                    float(c.get("score") or 0),
                    int(c.get("accepted") or 0),
                    _candidate_priority(c),
                ),
                reverse=True,
            )
        chosen = viable[0]
        self.deps.logger.info(
            "[EXTRACT ARBITER] candidates=%s | selected=%s score=%.1f accepted=%s",
            "; ".join(
                f"{c.get('name')}:score={float(c.get('score') or 0):.1f},"
                f"accepted={c.get('accepted')},dup={c.get('duplicates')},rej={c.get('rejected')}"
                for c in candidates
            ),
            chosen.get("name"),
            float(chosen.get("score") or 0),
            chosen.get("accepted"),
        )
        return chosen

    def commit_extraction_candidate(self, candidate: dict) -> tuple[list, int, int, int, str]:
        self.state.seen_extract_row_keys.update(candidate.get("fingerprints") or set())
        return (
            candidate.get("rows") or [],
            int(candidate.get("accepted") or 0),
            int(candidate.get("duplicates") or 0),
            int(candidate.get("rejected") or 0),
            str(candidate.get("source_text") or ""),
        )

    def record_extract_progress(self, rows, accepted_count: int) -> tuple[int, int]:
        if not _goal_is_tooltip_extract(self.deps.goal):
            self.state.total_extracted_rows += accepted_count
            return accepted_count, self.state.total_extracted_rows

        new_triggers = 0
        for row in rows or []:
            if isinstance(row, dict):
                trigger_key = extract_tooltip_primary_key(row)
            else:
                trigger_key = str(row).strip()
            if trigger_key and trigger_key not in self.state.tooltip_trigger_keys:
                self.state.tooltip_trigger_keys.add(trigger_key)
                new_triggers += 1
        self.state.total_extracted_rows = len(self.state.tooltip_trigger_keys)
        return new_triggers, self.state.total_extracted_rows

    def field_aliases(self, field: str) -> set[str]:
        norm = _normalize_output_field_key(field)
        aliases = {norm} if norm else set()
        alias_map = {
            "url": {"url", "link", "href", "primaryurl", "sourceurl", "detailurl", "网址", "链接"},
            "link": {"url", "link", "href", "primaryurl", "sourceurl", "detailurl", "网址", "链接"},
            "href": {"url", "link", "href"},
            "title": {"title", "name", "heading", "subject", "标题", "名称", "名字"},
            "标题": {"title", "name", "heading", "subject", "标题", "名称", "名字"},
            "name": {"name", "title", "名称", "姓名", "名字"},
            "名称": {"name", "title", "名称", "姓名", "名字"},
            "position": {"position", "职位", "职务", "岗位"},
            "office": {"office", "location", "city", "地区", "地点", "办公室"},
            "age": {"age", "年龄"},
            "time": {"time", "date", "age", "created", "published", "时间", "日期"},
            "author": {"author", "user", "username", "by", "作者", "用户"},
            "rating": {"rating", "score", "评分", "分数", "星级"},
            "score": {"rating", "score", "评分", "分数", "星级"},
            "评分": {"rating", "score", "评分", "分数", "星级"},
            "points": {"points", "score", "votes", "积分", "分数", "点赞"},
            "comments": {"comments", "commentcount", "reviewcount", "评论", "评论数"},
            "reviewcount": {"comments", "commentcount", "reviewcount", "评论", "评论数"},
            "reviews": {"reviews", "reviewcount", "votes", "评价人数", "评价数", "评论数"},
            "评价人数": {"reviews", "reviewcount", "votes", "评价人数", "评价数", "评论数"},
            "summary": {"summary", "description", "intro", "简介", "摘要", "一句话简介"},
            "description": {"summary", "description", "intro", "简介", "摘要", "一句话简介"},
            "intro": {"summary", "description", "intro", "简介", "摘要", "一句话简介"},
            "一句话简介": {"summary", "description", "intro", "简介", "摘要", "一句话简介"},
        }
        for key, values in alias_map.items():
            if norm == key or norm in values:
                aliases.update(values)
        return aliases

    def requested_field_coverage(self, row: dict) -> tuple[int, int]:
        if not self.deps.requested_output_fields or not isinstance(row, dict):
            return 0, 0
        normalized_keys = {
            key: _normalize_output_field_key(key)
            for key in row.keys()
            if row.get(key) is not None and str(row.get(key)).strip()
        }
        hit = 0
        total = 0
        for field in self.deps.requested_output_fields:
            aliases = self.field_aliases(field)
            if not aliases:
                continue
            total += 1
            for norm_key in normalized_keys.values():
                if (
                    norm_key in aliases
                    or any(alias and alias in norm_key for alias in aliases)
                    or any(alias and norm_key in alias for alias in aliases)
                ):
                    hit += 1
                    break
        return hit, total

    def project_row_to_requested_fields(self, row: dict) -> dict:
        if not self.deps.requested_output_fields or not isinstance(row, dict):
            return row

        normalized_keys = {
            key: _normalize_output_field_key(key)
            for key in row.keys()
        }
        column_keys = sorted(
            [
                key for key, norm in normalized_keys.items()
                if re.fullmatch(r"column\d+", norm or "")
            ],
            key=lambda key: int(re.search(r"\d+", normalized_keys[key]).group(0)),
        )
        requested = [
            field for field in self.deps.requested_output_fields
            if _normalize_output_field_key(field)
        ]
        if not requested:
            return row

        if (
            column_keys
            and len(column_keys) >= len(requested)
            and len(column_keys) >= max(2, len(row) - 1)
        ):
            return {
                field: row.get(key)
                for field, key in zip(requested, column_keys)
            }

        projected = {}
        used_keys: set[str] = set()
        for field in requested:
            aliases = self.field_aliases(field)
            best_key = None
            best_score = 0
            for key, norm_key in normalized_keys.items():
                if key in used_keys or not norm_key:
                    continue
                score = 0
                if norm_key in aliases:
                    score = 100
                elif any(alias and alias in norm_key for alias in aliases):
                    score = 80
                elif any(alias and norm_key in alias for alias in aliases):
                    score = 70
                if score > best_score:
                    best_key = key
                    best_score = score
            if best_key is not None:
                projected[field] = row.get(best_key)
                used_keys.add(best_key)

        if not projected and column_keys:
            return {
                field: row.get(key)
                for field, key in zip(requested, column_keys)
            }

        return projected or row

    def normalize_extracted_row_fields(self, rows: list, *, project: bool = True) -> list:
        """Stabilize common forum/list fields before saving."""
        out: list = []
        for row in rows or []:
            if not isinstance(row, dict):
                out.append(row)
                continue
            normalized = dict(row)
            if "time" not in normalized and normalized.get("age"):
                normalized["time"] = normalized.get("age")
            normalized.pop("age", None)

            for key in ("points", "score", "votes", "review_count", "comments", "comment_count"):
                if key in normalized and normalized.get(key) is not None:
                    normalized[key] = self.first_int_value(normalized.get(key))

            if "review_count" not in normalized and "comments" in normalized:
                normalized["review_count"] = normalized.get("comments")
            normalized.pop("comments", None)

            for legacy_key in ("story_url", "discussion_url"):
                if legacy_key in normalized and "source_url" not in normalized and "detail_url" not in normalized:
                    role = "detail" if legacy_key == "discussion_url" else "source"
                    normalized[f"{role}_url"] = normalized.get(legacy_key)
                normalized.pop(legacy_key, None)

            for url_key in ("primary_url", "source_url", "detail_url", "url", "link", "href"):
                raw_url = normalized.get(url_key)
                if not (raw_url and self.is_probable_url(raw_url)):
                    continue
                role = self.classify_url_role(raw_url)
                if role == "detail":
                    normalized.setdefault("detail_url", raw_url)
                else:
                    normalized.setdefault("source_url", raw_url)
            if normalized.get("source_url"):
                normalized["primary_url"] = normalized.get("source_url")
            elif normalized.get("detail_url"):
                normalized["primary_url"] = normalized.get("detail_url")
            if normalized.get("primary_url"):
                normalized["url"] = normalized.get("primary_url")
            if project:
                normalized = self.project_row_to_requested_fields(normalized)
            out.append(normalized)
        return out

    def first_int_value(self, value: object) -> int | object:
        text = str(value or "").strip()
        match = re.search(r"\d[\d,]*", text)
        if not match:
            return value
        try:
            return int(match.group(0).replace(",", ""))
        except ValueError:
            return value

    def classify_url_role(self, value: object) -> str:
        """Classify a URL into a generic extraction role."""
        text = str(value or "").strip().lower()
        if not text:
            return ""
        if "news.ycombinator.com/item" in text:
            return "detail"
        if re.search(r"/(item|story|post|posts|article|articles|thread|threads|comment|comments|detail|details|product|products|issues?)(/|\\?|#|$)", text):
            return "detail"
        return "source"

    def is_probable_url(self, value: object) -> bool:
        text = str(value or "").strip()
        return bool(re.match(r"^https?://", text, flags=re.IGNORECASE))

    async def capture_body_text_excerpt(self, limit: int = 3000) -> str:
        try:
            page = await self.deps.browser._ensure_active_page(reason="extraction snapshot body text")
            if not page:
                return ""
            text = await page.evaluate(
                """() => String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim()"""
            )
            return str(text or "")[:limit]
        except Exception as exc:
            self.deps.logger.debug("[EXTRACTION SNAPSHOT] body text capture skipped: %s", exc)
            return ""

    async def save_extraction_snapshot(self, 
        *,
        step: int = 0,
        source: str,
        rows: list,
        output_file: str,
        accepted_rows: int,
        duplicate_rows: int = 0,
        rejected_rows: int = 0,
        candidates: list[dict] | None = None,
        data_shape: dict | None = None,
        source_text: str = "",
        metadata: dict | None = None,
    ) -> str:
        try:
            snapshot_path = maybe_save_snapshot(
                url=getattr(self.deps.browser, "current_url", "") or "",
                goal=self.deps.snapshot_goal,
                source=source,
                rows=rows,
                requested_fields=self.deps.requested_output_fields,
                output_file=output_file,
                run_id=self.deps.run_ts,
                step=step,
                total_rows=self.state.total_extracted_rows,
                accepted_rows=accepted_rows,
                duplicate_rows=duplicate_rows,
                rejected_rows=rejected_rows,
                candidates=candidates or [],
                data_shape=data_shape or {},
                source_text=source_text,
                body_text=await self.capture_body_text_excerpt(),
                metadata=metadata or {},
            )
            if not snapshot_path:
                return ""
            self.deps.logger.info("[EXTRACTION SNAPSHOT] saved: %s", snapshot_path)
            self.deps.event_stream.extract(
                step=step,
                source=source,
                rows=accepted_rows,
                output_file=output_file,
                metadata={
                    **dict(metadata or {}),
                    "snapshot_path": str(snapshot_path),
                },
            )
            return str(snapshot_path)
        except Exception as exc:
            self.deps.logger.debug("[EXTRACTION SNAPSHOT] save skipped: %s", exc)
            return ""

    async def try_dom_api_fast_path(self, 
        dom_rows: list,
        *,
        step: int = 0,
        source: str,
        dom_text: str = "",
    ) -> dict:
        try:
            from visual_web_agent.content_completeness_guard import (
                evaluate_dom_api_completeness,
                execute_api_fast_path,
            )

            page_text = dom_text or await self.capture_body_text_excerpt(6000)
            verdict = evaluate_dom_api_completeness(
                dom_rows=dom_rows,
                dom_text=page_text,
                run_id=self.deps.run_ts,
                goal=self.deps.goal,
            )
            if not verdict.get("should_fast_path"):
                return {"applied": False, "verdict": verdict}

            cookies: list = []
            if getattr(self.deps.browser, "_context", None) is not None:
                cookies = await self.deps.browser._context.cookies()
            fast = execute_api_fast_path(
                run_id=self.deps.run_ts,
                verdict=verdict,
                cookies=cookies,
            )
            if not fast.get("applied"):
                return fast

            api_rows = fast.get("rows") or []
            if not api_rows:
                return {"applied": False, "reason": "empty_api_rows", "verdict": verdict}

            saved_path = ""
            if self.deps.goal_output_mode != "answer":
                saved_path = save_run_dataset(
                    api_rows,
                    run_id=self.deps.run_ts,
                    output_contract=self.deps.goal_output_contract,
                    produced_by="api_fast_path",
                    filename_hint=self.deps.vlm_output,
                )
            self.record_extract_progress(api_rows, len(api_rows))
            self.deps.logger.info(
                "[API FAST PATH] upgraded %s via %s rows=%s saved=%s",
                source,
                fast.get("endpoint"),
                len(api_rows),
                saved_path,
            )
            self.deps.event_stream.guard(
                step=step,
                name="DOM_API_FAST_PATH",
                message="DOM truncated or sparse; replayed richer API payload.",
                metadata={"verdict": verdict, "fast_path": fast, "source": source},
            )
            try:
                from api_server import broadcast_phase as _bp_api_fp

                _bp_api_fp(
                    "completion_guard",
                    severity="info",
                    message=f"api_fast_path: {fast.get('endpoint', '')[:80]}",
                    step=step,
                    notice_severity=getattr(self.deps.browser, "_last_notice_severity", None),
                    extra={
                        "guard": "dom_api_fast_path",
                        "evaluation": {
                            "status": "complete",
                            "evidence": verdict.get("reasons") or [],
                            "reasons": ["api_fast_path"],
                        },
                        "endpoint": fast.get("endpoint"),
                        "row_count": len(api_rows),
                    },
                )
            except Exception:
                pass
            return {"applied": True, "verdict": verdict, "fast_path": fast, "saved_path": saved_path}
        except Exception as _api_fp_err:
            self.deps.logger.debug("[API FAST PATH] skipped: %s", _api_fp_err)
            return {"applied": False, "error": str(_api_fp_err)}

    def xhr_saved_row_count(self, ) -> tuple[int | None, str]:
        filename = str(getattr(self.deps.browser, "_intercept_filename", "") or "")
        if not filename:
            return None, ""
        # The intercept saver rewrites the suffix per the run's
        # output_contract container (jsonl/csv/xlsx), so probe all
        # dataset suffixes instead of assuming xlsx.
        base = resolve_artifact_path(filename)
        candidates = [base]
        for _suffix in (".jsonl", ".csv", ".xlsx"):
            alt = base.with_suffix(_suffix)
            if alt not in candidates:
                candidates.append(alt)
        path = next((p for p in candidates if p.exists()), base)
        if not path.exists():
            return None, str(path)
        try:
            import pandas as _pd

            suffix = path.suffix.lower()
            if suffix == ".jsonl":
                df = _pd.read_json(path, orient="records", lines=True)
            elif suffix == ".csv":
                df = _pd.read_csv(path)
            else:
                df = _pd.read_excel(path)
            return int(len(df.index)), str(path)
        except Exception as exc:
            self.deps.logger.debug("[XHR HARD KILL] saved-row count skipped: %s", exc)
            return None, str(path)

    def xhr_target_reached(self, ) -> tuple[bool, int, int | None]:
        target = _parse_goal_target_count(self.deps.goal)
        if not self.deps.enable_xhr or target is None or self.deps.browser.intercepted_count <= 0:
            return False, self.deps.browser.intercepted_count, target
        if _goal_is_tooltip_extract(self.deps.goal):
            return False, self.deps.browser.intercepted_count, target
        if not _goal_is_bulk_extraction(self.deps.goal):
            return False, self.deps.browser.intercepted_count, target
        saved_count, saved_path = self.xhr_saved_row_count()
        effective_count = (
            saved_count
            if saved_count is not None
            else int(self.deps.browser.intercepted_count or 0)
        )
        if saved_count is not None and saved_count != self.deps.browser.intercepted_count:
            self.deps.logger.info(
                "[XHR HARD KILL] using saved clean rows=%s instead of raw intercepted=%s (%s)",
                saved_count,
                self.deps.browser.intercepted_count,
                saved_path,
            )
        return effective_count >= target, effective_count, target

    def compact_link_match_text(self, value: object) -> str:
        return re.sub(r"\W+", "", str(value or "").lower(), flags=re.UNICODE)

    def row_primary_link_text(self, row: dict) -> str:
        preferred_markers = (
            "title", "name", "product", "item", "subject", "label",
            "heading", "caption", "标题", "名称", "商品", "项目",
        )
        preferred: list[str] = []
        fallback: list[str] = []
        for key, value in row.items():
            if value is None:
                continue
            key_norm = str(key or "").strip().lower()
            text = re.sub(r"\s+", " ", str(value).strip())
            if len(self.compact_link_match_text(text)) < 8:
                continue
            if any(marker in key_norm for marker in preferred_markers):
                preferred.append(text)
            elif not re.fullmatch(r"[\d\s,.:/%+\-]+", text):
                fallback.append(text)
        candidates = preferred or fallback
        return max(candidates, key=lambda s: len(self.compact_link_match_text(s))) if candidates else ""

    async def enrich_rows_with_dom_links(self, rows: list) -> list:
        """Fill row URLs by matching title/name text to page anchors.

        This is schema-agnostic: it enriches rows only when a stable text
        field has an unambiguous anchor match in the current DOM.
        """
        rows = self.normalize_extracted_row_fields(rows, project=False)
        if not rows or not any(isinstance(row, dict) for row in rows):
            return rows
        page = await self.deps.browser._ensure_active_page(reason="enrich extracted rows with links")
        if not page:
            return rows
        try:
            anchors = await page.evaluate(
                """() => {
                    const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const nearestText = (a) => {
                        const direct = clean(a.innerText || a.textContent || a.getAttribute('aria-label') || a.getAttribute('title'));
                        const row = a.closest('article, [role="article"], [role="listitem"], li, tr, .Story, .story, .ais-Hits-item, .hit');
                        const rowText = clean(row ? row.innerText : '');
                        return clean([direct, rowText].filter(Boolean).join(' '));
                    };
                    return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                        text: nearestText(a),
                        own_text: clean(a.innerText || a.textContent || a.getAttribute('aria-label') || a.getAttribute('title')),
                        href: a.href || ''
                    })).filter(x => x.text && x.href && !x.href.startsWith('javascript:'));
                }"""
            )
        except Exception as exc:
            self.deps.logger.debug("[LINK ENRICH] anchor scan skipped: %s", exc)
            return rows

        anchor_rows: list[dict] = []
        for anchor in anchors or []:
            text = str(anchor.get("text") or "").strip()
            own_text = str(anchor.get("own_text") or "").strip()
            href = str(anchor.get("href") or "").strip()
            compact = self.compact_link_match_text(text)
            if len(compact) >= 8 and href:
                anchor_rows.append(
                    {
                        "href": href,
                        "compact": compact,
                        "own_compact": self.compact_link_match_text(own_text),
                    }
                )
        if not anchor_rows:
            return rows

        enriched_source = 0
        enriched_detail = 0
        out: list = []
        for row in rows:
            if not isinstance(row, dict):
                out.append(row)
                continue
            primary_compact = self.compact_link_match_text(self.row_primary_link_text(row))
            if len(primary_compact) < 8:
                out.append(row)
                continue
            matches = []
            for anchor in anchor_rows:
                a_compact = anchor["compact"]
                if primary_compact in a_compact or a_compact in primary_compact:
                    matches.append((min(len(primary_compact), len(a_compact)), anchor))
            matches.sort(key=lambda item: item[0], reverse=True)
            best_match = matches[0][1] if matches and (len(matches) == 1 or matches[0][0] > matches[1][0]) else None
            if best_match:
                row = dict(row)
                href = best_match["href"]
                role = self.classify_url_role(href)
                if role == "detail":
                    if not row.get("detail_url"):
                        row["detail_url"] = href
                        enriched_detail += 1
                elif not row.get("source_url"):
                    row["source_url"] = href
                    enriched_source += 1
                if not row.get("primary_url"):
                    row["primary_url"] = row.get("source_url") or row.get("detail_url") or href
                row["url"] = row.get("primary_url")

            if isinstance(row, dict) and not row.get("detail_url"):
                detail_matches = []
                for anchor in anchor_rows:
                    href = anchor["href"]
                    if self.classify_url_role(href) != "detail":
                        continue
                    a_compact = anchor["compact"]
                    if primary_compact in a_compact or a_compact in primary_compact:
                        detail_matches.append((min(len(primary_compact), len(a_compact)), anchor))
                detail_matches.sort(key=lambda item: item[0], reverse=True)
                if detail_matches:
                    row = dict(row)
                    row["detail_url"] = detail_matches[0][1]["href"]
                    enriched_detail += 1
            out.append(row)
        if enriched_source or enriched_detail:
            self.deps.logger.info(
                "[LINK ENRICH] Filled source_url=%s detail_url=%s",
                enriched_source,
                enriched_detail,
            )
        return self.normalize_extracted_row_fields(out)

    async def extract_compact_list_text_via_dom(self, reason: str) -> tuple[str, str, int]:
        """Return compact repeated-list item text when the DOM exposes clear rows."""
        try:
            _page_for_items = await self.deps.browser._ensure_active_page(reason=reason)
            result = await _page_for_items.evaluate(
                """() => {
                    const clean = (value) => String(value || '')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    const compact = (value) => clean(value).toLowerCase()
                        .replace(/[^a-z0-9\\u4e00-\\u9fff]+/g, '');
                    const isVisible = (el) => {
                        if (!el || !(el instanceof Element)) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && rect.width > 0
                            && rect.height > 0;
                    };
                    // EXTRACT-SHADOW-2: list/card components can render
                    // inside open shadow roots (same walker as the table
                    // harvest / form fallback).
                    const deepQueryAll = (selector, root = document) => {
                        const out = Array.from(root.querySelectorAll(selector));
                        for (const host of root.querySelectorAll('*')) {
                            if (host.shadowRoot) out.push(...deepQueryAll(selector, host.shadowRoot));
                        }
                        return out;
                    };
                    const candidates = [];
                    const addCandidate = (el, source) => {
                        if (!isVisible(el)) return;
                        const text = clean(el.innerText || el.textContent);
                        if (text.length < 20 || text.length > 1800) return;
                        const childBlocks = Array.from(el.querySelectorAll(
                            'article, [role="article"], [role="listitem"], li, tbody tr'
                        )).filter(node => node !== el && isVisible(node));
                        if (childBlocks.length >= 3 && text.length > 800) return;
                        const links = Array.from(el.querySelectorAll('a[href]'))
                            .filter(isVisible)
                            .map(a => ({
                                text: clean(a.innerText || a.textContent || a.getAttribute('aria-label')),
                                href: a.href || ''
                            }))
                            .filter(a => a.href)
                            .slice(0, 6);
                        const key = links[0]?.href || compact(text).slice(0, 180);
                        if (!key) return;
                        candidates.push({source, key, text, links});
                    };

                    const directSelectors = [
                        'article', '[role="article"]', '[role="listitem"]',
                        '.Story', '.story', '.ais-Hits-item', '.hit',
                        '.search-result', '.result', '.item'
                    ];
                    for (const el of deepQueryAll(directSelectors.join(','))) {
                        addCandidate(el, 'selector');
                    }

                    const containerSelectors = [
                        'main', '[role="main"]', '#content', '.content',
                        '.list', '.item-list', '.results', '.search-results',
                        'ol', 'ul', 'section'
                    ];
                    for (const root of deepQueryAll(containerSelectors.join(','))) {
                        if (!isVisible(root)) continue;
                        const children = Array.from(root.children || []).filter(isVisible);
                        if (children.length < 4) continue;
                        const buckets = new Map();
                        for (const child of children) {
                            const cls = clean(child.className || child.tagName).slice(0, 80);
                            buckets.set(cls, (buckets.get(cls) || 0) + 1);
                        }
                        const repeat = Math.max(...Array.from(buckets.values()), 0);
                        if (repeat < 4) continue;
                        for (const child of children) addCandidate(child, 'container');
                    }

                    const seen = new Set();
                    const rows = [];
                    for (const candidate of candidates) {
                        if (seen.has(candidate.key)) continue;
                        seen.add(candidate.key);
                        rows.push(candidate);
                    }
                    rows.sort((a, b) => {
                        const aTop = document.body.innerText.indexOf(a.text.slice(0, 40));
                        const bTop = document.body.innerText.indexOf(b.text.slice(0, 40));
                        return (aTop < 0 ? 1e9 : aTop) - (bTop < 0 ? 1e9 : bTop);
                    });
                    const selected = rows.slice(0, 120);
                    const lines = selected.map((row, index) => {
                        const linkText = row.links
                            .map(link => {
                                const label = link.text ? `${link.text} -> ` : '';
                                return `${label}${link.href}`;
                            })
                            .join(' ; ');
                        return [
                            `Item ${index + 1}: ${row.text}`,
                            linkText ? `Links: ${linkText}` : ''
                        ].filter(Boolean).join('\\n');
                    });
                    return {
                        count: selected.length,
                        text: lines.join('\\n\\n')
                    };
                }"""
            )
            if not isinstance(result, dict):
                return "", "", 0
            text = str(result.get("text") or "").strip()
            count = int(result.get("count") or 0)
            if count >= 5 and len(text) >= 400:
                self.deps.logger.info(
                    "[EXTRACT FULL] DOM compact list candidate: %s items, %s chars",
                    count,
                    len(text),
                )
                return "LIST_ITEMS_TEXT", text, count
        except Exception as list_err:
            self.deps.logger.debug("[EXTRACT FULL] compact list DOM probe skipped: %s", list_err)
        return "", "", 0

    async def extract_full_page_text_for_data(self, reason: str) -> tuple[str, str]:
        """Return the best full-page text source for semantic extraction."""
        list_source, list_text, list_count = await self.extract_compact_list_text_via_dom(reason)
        if list_text and list_count >= 10:
            self.deps.logger.info(
                "[EXTRACT FULL] using compact DOM list text before AX/innerText "
                "(items=%s, chars=%s)",
                list_count,
                len(list_text),
            )
            return list_source, list_text

        source = "AX_TREE"
        ax_text = await self.deps.browser.extract_page_text_via_ax_tree()
        body_text = ""
        try:
            _page_for_text = await self.deps.browser._ensure_active_page(reason=reason)
            body_text = await _page_for_text.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            body_text = str(body_text or "").strip()
        except Exception as text_err:
            self.deps.logger.debug("[EXTRACT FULL] innerText fallback skipped: %s", text_err)

        ax_text = str(ax_text or "").strip()
        if list_text and len(list_text) > max(len(ax_text) * 0.5, 1200):
            self.deps.logger.info(
                "[EXTRACT FULL] compact DOM list richer than AX slice "
                "(items=%s, list=%s chars, ax=%s chars), using list text",
                list_count,
                len(list_text),
                len(ax_text),
            )
            return list_source, list_text
        if body_text and len(body_text) > max(len(ax_text) * 1.2, 800):
            if ax_text:
                self.deps.logger.info(
                    "[EXTRACT FULL] innerText richer than AX (%s vs %s chars), using combined text",
                    len(body_text),
                    len(ax_text),
                )
                return (
                    "AX_TREE+INNER_TEXT",
                    f"【AX Tree 语义文本】\n{ax_text}\n\n【DOM innerText 全页文本】\n{body_text}",
                )
            self.deps.logger.info("[EXTRACT FULL] AX Tree empty/short, using innerText")
            return "INNER_TEXT_FALLBACK", body_text
        if ax_text:
            return source, ax_text
        return ("INNER_TEXT_FALLBACK", body_text) if body_text else ("", "")

    async def extract_body_text_for_semantic_cards(self, reason: str) -> str:
        """Lightweight body text fallback for schema-driven card extraction."""
        if not self.deps.requested_output_fields:
            return ""
        try:
            _body_page = await self.deps.browser._ensure_active_page(reason=reason)
            text = await _body_page.evaluate(
                """() => document.body ? String(document.body.innerText || '') : ''"""
            )
            return str(text or "").strip()[:20000]
        except Exception as body_err:
            self.deps.logger.debug("[EXTRACT DOM] semantic card body text skipped: %s", body_err)
            return ""

    async def inspect_click_target_for_extract_nav_guard(self, decision: dict) -> dict:
        action_name = str(decision.get("action") or "").strip().lower()
        if action_name not in {"click", "click_text", "click_point"}:
            return {}
        target_id = int(decision.get("target_id") or 0)
        if target_id <= 0:
            return {
                "text": str(decision.get("type_value") or ""),
                "action": action_name,
            }
        try:
            page = await self.deps.browser._ensure_active_page(
                reason="inspect extraction same-page nav target"
            )
            if not page:
                return {}
            return await page.evaluate(
                """(targetId) => {
                    const el = document.querySelector(`[data-som-id="${targetId}"]`);
                    if (!el) return {exists: false, target_id: targetId};
                    const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const anchor = el.closest('a[href]');
                    const role = clean(el.getAttribute('role') || '');
                    const tag = clean(el.tagName || '').toLowerCase();
                    const href = anchor ? anchor.href : (
                        el.href || el.getAttribute('href') || ''
                    );
                    const text = clean(
                        el.innerText || el.textContent ||
                        el.getAttribute('aria-label') ||
                        el.getAttribute('title') || ''
                    );
                    const cls = clean(el.className || '');
                    const parent = el.closest(
                        'nav,header,[role="navigation"],[role="tablist"],.tab,.tabs,.nav,.navbar,.forecast'
                    );
                    return {
                        exists: true,
                        target_id: targetId,
                        tag,
                        role,
                        href,
                        text,
                        class_name: cls,
                        nav_like: Boolean(parent),
                        tab_like: role === 'tab' ||
                            el.getAttribute('aria-controls') ||
                            el.getAttribute('data-toggle') === 'tab' ||
                            /\\b(tab|tabs|nav-link|active)\\b/i.test(cls),
                    };
                }""",
                target_id,
            ) or {}
        except Exception as inspect_err:
            self.deps.logger.debug("[EXTRACT NAV GUARD] target inspection skipped: %s", inspect_err)
            return {}

    async def nudge_scroll_after_duplicate_extract(self, reason: str, scroll_amount: int = 2000) -> bool:
        try:
            _scroll_page = await self.deps.browser._ensure_active_page(reason=reason)
            before_size = await _scroll_page.evaluate("() => document.body.innerText.length")
            await _scroll_page.evaluate(
                """(amt) => {
                    window.scrollBy({top: Math.max(amt, window.innerHeight * 1.5), behavior: 'smooth'});
                }""",
                scroll_amount,
            )
            await _scroll_page.wait_for_timeout(1500)
            try:
                await _scroll_page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            after_size = await _scroll_page.evaluate("() => document.body.innerText.length")
            delta = after_size - before_size
            pct = (delta / before_size * 100) if before_size > 0 else 0.0
            self.deps.logger.info(
                "[EXTRACT DEDUP] nudged page downward (%s): %d→%d bytes (+%d, %.1f%%)",
                reason, before_size, after_size, delta, pct,
            )
            if delta > 100:
                return True
            # EXTRACT-VSCROLL-1: virtualised lists render a constant row
            # window inside an inner scroller, so window.scrollBy does
            # nothing and body length stays flat. Scroll the dominant
            # container itself and compare row signatures instead.
            vs = await nudge_virtual_scroll(_scroll_page, amount=scroll_amount)
            if vs.get("rows_changed") or (
                vs.get("mode") == "container" and vs.get("moved")
            ):
                self.deps.logger.info(
                    "[EXTRACT DEDUP] virtual-scroll nudge advanced (%s): %s",
                    reason,
                    vs,
                )
                return True
            return False
        except Exception as scroll_err:
            self.deps.logger.debug("[EXTRACT DEDUP] duplicate-row scroll nudge failed: %s", scroll_err)
            return False

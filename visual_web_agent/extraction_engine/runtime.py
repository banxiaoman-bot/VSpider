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
    from ..data_sanitizer import extract_tooltip_primary_key, sanitize_extracted_rows
    from ..phases.goal_parser import (
        _goal_is_tooltip_extract,
        _normalize_output_field_key,
        _parse_goal_target_count,
    )
    from ..phases.decision_helpers import _goal_is_bulk_extraction
    from .snapshots import maybe_save_snapshot
    from ..data_writers import save_run_dataset
    from ..artifact_manager import resolve_artifact_path
    from ..virtual_scroll import nudge_virtual_scroll
except ImportError:  # pragma: no cover
    from data_sanitizer import extract_tooltip_primary_key, sanitize_extracted_rows
    from phases.goal_parser import (
        _goal_is_tooltip_extract,
        _normalize_output_field_key,
        _parse_goal_target_count,
    )
    from phases.decision_helpers import _goal_is_bulk_extraction
    from extraction_engine.snapshots import maybe_save_snapshot
    from data_writers import save_run_dataset
    from artifact_manager import resolve_artifact_path
    from virtual_scroll import nudge_virtual_scroll
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

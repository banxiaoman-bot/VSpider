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
    normalize_extracted_row_fields: Callable[..., Any]


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
                rows = self.deps.normalize_extracted_row_fields(rows)
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

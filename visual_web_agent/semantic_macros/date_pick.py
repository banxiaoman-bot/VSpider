"""date_pick: navigate to a target date in a popup calendar widget.

Replaces a class of VLM failure modes on calendar widgets (Element Plus,
Ant Design, Naive UI, Mint UI). Previously these tasks required up to 20
SoM-tagged clicks because the VLM mis-read panel months from screenshots
and re-clicked stale @eN IDs.

Goal patterns recognized:
  * "选择下个月的 15 号" / "下下个月的 5 号"
  * "select next month 15" / "next next month 5"
  * "month after next 15"
  * "in 3 months 15"
  * absolute dates: "2026-06-15" / "2026年6月15日" / "2026/6/15"

Step shape produced:
  {
    "action": "date_pick",
    "anchor": {"section": "...", "placeholder": "Pick a day", "occurrence": 1},
    "target_date": "2026-06-15",          # always normalized YYYY-MM-DD
    "target_date_rule": {"mode": "relative_month", "offset": 1, "day": 15},
    "validate": {"input_should_contain": "2026-06-15"},
  }

Replay strategy:
  1. Locate input via score-based anchor match (section + placeholder)
  2. Click to open the calendar
  3. Parse the panel's visible month (multi-format heuristic incl. Chinese)
  4. Click prev/next-month buttons until the panel matches target month
  5. Locate the day cell by its strict numeric label (cellDayNumber probes
     several known nested containers), excluding prev/next-month spillover
  6. Click and wait for opener.value to match (separator-tolerant)
  7. On postcheck_failed, optionally fall back to setNativeValue (only when
     the runtime config flag DATE_PICK_DIRECT_SET_FALLBACK is on); otherwise
     report failure for the framework's VL judge to second-opinion.
"""

from __future__ import annotations

import re

from . import register
from ._dateutils import month_offset_day, parse_goal_scope_title, parse_relative_month_day
from ._types import Macro


# ── Goal parser ────────────────────────────────────────────────────────────────
def parse(goal: str) -> dict | None:
    """Build a semantic step dict for date-picker tasks; None if not applicable."""
    text = str(goal or "")
    lowered = text.lower()
    has_calendar = any(
        marker in lowered
        for marker in ("date", "calendar", "datepicker", "date-picker", "pick a day")
    ) or any(marker in text for marker in ("日期", "日历"))
    if not has_calendar:
        return None

    target_date = ""
    rule: dict = {}

    relative = parse_relative_month_day(text)
    if relative:
        offset, day = relative
        target_date = month_offset_day(offset, day)
        rule = {"mode": "relative_month", "offset": offset, "day": day}

    if not target_date:
        m = re.search(
            r"(?:month\s+after\s+next|next\s+next\s+month)"
            r"(?:\s+(?:the\s+)?)?(3[01]|[12]?\d)(?:st|nd|rd|th)?",
            lowered,
        )
        if m:
            day = int(m.group(1))
            target_date = month_offset_day(2, day)
            rule = {"mode": "relative_month", "offset": 2, "day": day}

    if not target_date:
        m = re.search(
            r"in\s+([1-9]\d?)\s+months?(?:\s+(?:on\s+)?(?:the\s+)?)?"
            r"(3[01]|[12]?\d)(?:st|nd|rd|th)?",
            lowered,
        )
        if m:
            offset, day = int(m.group(1)), int(m.group(2))
            target_date = month_offset_day(offset, day)
            rule = {"mode": "relative_month", "offset": offset, "day": day}

    if not target_date:
        m = re.search(
            r"next\s+month(?:\s+(?:the\s+)?)?(3[01]|[12]?\d)(?:st|nd|rd|th)?",
            lowered,
        )
        if m:
            day = int(m.group(1))
            target_date = month_offset_day(1, day)
            rule = {"mode": "relative_month", "offset": 1, "day": day}

    if not target_date:
        m = re.search(
            r"\b(20\d{2})[-/.年\s]+(1[0-2]|0?[1-9])[-/.月\s]+(3[01]|[12]?\d)\s*[日号]?\b",
            text,
        )
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            target_date = f"{year:04d}-{month:02d}-{day:02d}"
            rule = {"mode": "absolute", "date": target_date}

    if not target_date:
        return None

    section = parse_goal_scope_title(text)
    if not section:
        m = re.search(
            r"([A-Za-z][A-Za-z0-9 _-]{1,60})\s*(?:标题|区域|section|heading)",
            text, re.I,
        )
        if m:
            section = m.group(1).strip()

    placeholder = ""
    quoted = re.findall(r"[\"“”'‘’]([^\"“”'‘’]{1,80})[\"“”'‘’]", text)
    for item in quoted:
        if re.search(r"pick\s+a\s+day", item, re.I):
            placeholder = item.strip()
            break
    if not placeholder:
        for item in quoted:
            if item.strip() != section and re.search(r"日期|date", item, re.I):
                placeholder = item.strip()
                break
    if not placeholder and re.search(r"pick\s+a\s+day", text, re.I):
        placeholder = "Pick a day"

    occurrence = 1  # legacy hint reserved for future ("第二个" → 2)

    return {
        "action": "date_pick",
        "anchor": {
            "section": section,
            "placeholder": placeholder,
            "occurrence": occurrence,
        },
        "target_date": target_date,
        "target_date_rule": rule,
        "validate": {"input_should_contain": target_date},
    }


# ── JS body ────────────────────────────────────────────────────────────────────
# Ported verbatim from main.py's inline date_pick branch (the battle-tested
# implementation with Fixes 1-4 from the May 7 iteration). Uses primitives
# (norm/textOf/isVisible/allVisible/sleep/clickEl) from the bundle.
#
# Step contract:
#   step.anchor: {section, placeholder, occurrence}
#   step.target_date: "YYYY-MM-DD"
#   step.allow_direct_set: bool (runtime config flag, injected by _replay_rpa)
#
# Returns {ok, reason?, targetDate?, observed?, clickedDay?, clickedAria?,
#          hitsCount?, directSet?, directSetReason?}
_JS_BODY = r"""
const {anchor, target_date: targetDate, allow_direct_set: allowDirectSet} = step;

const setNativeValue = (el, val) => {
    if (!el) return '';
    const proto = el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, val);
    else el.value = val;
    try { el.focus({preventScroll: true}); } catch (e) { try { el.focus(); } catch (_) {} }
    el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
    el.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
    el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true, composed: true}));
    el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', bubbles: true, composed: true}));
    try { el.blur(); } catch (_) {}
    return String(el.value || textOf(el) || '').trim();
};

const parseTarget = (value) => {
    const m = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return null;
    return {year: Number(m[1]), month: Number(m[2]), day: Number(m[3])};
};
const target = parseTarget(targetDate);
if (!target) return {ok: false, reason: 'bad_target_date', targetDate};

/* Anchor with placeholder-vs-typed-value scoring: if the input already shows a
 * typed value, prefer empty siblings. Avoids the trap where Element Plus demo
 * pages keep a pre-filled date in the example input next to the empty one. */
const sectionText = norm(anchor?.section || '');
const placeholder = norm(anchor?.placeholder || '');
const occurrence = Math.max(1, Number(anchor?.occurrence || 1));
const inputs = allVisible('input,textarea,[role=combobox],[contenteditable=true]');
const candidates = [];
for (const input of inputs) {
    const t = norm(textOf(input));
    if (placeholder && !t.includes(placeholder)) continue;
    const r = input.getBoundingClientRect();
    const rawValue = String(input.value ?? input.getAttribute?.('value') ?? '').trim();
    const placeholderAttr = norm(input.getAttribute?.('placeholder') || '');
    const hasTypedValue = rawValue.length > 0;
    const visiblePlaceholderMatch = Boolean(
        placeholder && placeholderAttr.includes(placeholder) && !hasTypedValue
    );
    let score = 100;
    if (placeholder && t.includes(placeholder)) score += 160;
    if (visiblePlaceholderMatch) score += 420;
    if ((input.getAttribute('placeholder') || '').trim()) score += 40;
    if (placeholder && hasTypedValue) score -= 520;
    if (input.readOnly || (input.getAttribute('role') || '').toLowerCase() === 'combobox') score += 30;
    if (sectionText) {
        const before = Array.from(document.querySelectorAll(
            'h1,h2,h3,h4,h5,h6,[role=heading],.demo-block .source,.example-showcase,section,article,div'
        ))
            .filter(isVisible)
            .map(el => ({el, rect: el.getBoundingClientRect(), text: norm(textOf(el))}))
            .filter(x => x.text.includes(sectionText) && x.rect.top <= r.top + 20)
            .sort((a, b) => Math.abs(a.rect.top - r.top) - Math.abs(b.rect.top - r.top));
        if (before[0]) score += Math.max(0, 180 - Math.abs(r.top - before[0].rect.top) / 2);
    }
    score -= Math.abs(r.top - window.innerHeight / 2) / 80;
    candidates.push({input, score});
}
candidates.sort((a, b) => b.score - a.score);
const opener = candidates[occurrence - 1]?.input || candidates[0]?.input;
if (!opener) return {ok: false, reason: 'date_input_not_found', anchor};
clickEl(opener);
await sleep(300);

/* Parse the currently-visible panel's month label across the major libs.
 * Falls back to opener.value's leading YYYY-MM, then today's month. */
const visiblePanelMonth = () => {
    const panels = allVisible(
        '.el-picker-panel,.ant-picker-dropdown,.n-date-panel,'
        + '.mx-datepicker-main,.datepicker,[role=dialog],.el-popper'
    );
    const root = panels[panels.length - 1] || document;
    const txt = textOf(root);
    const monthNames = {
        january: 1, february: 2, march: 3, april: 4, may: 5, june: 6,
        july: 7, august: 8, september: 9, october: 10, november: 11, december: 12,
        jan: 1, feb: 2, mar: 3, apr: 4, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9,
        oct: 10, nov: 11, dec: 12
    };
    let m = txt.match(/(20\d{2})\s*[年\-/. ]\s*(1[0-2]|0?[1-9])\s*(?:月)?/);
    if (m) return {year: Number(m[1]), month: Number(m[2])};
    m = txt.match(/(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s+(20\d{2})/i);
    if (m) return {year: Number(m[2]), month: monthNames[m[1].toLowerCase()]};
    m = txt.match(/(20\d{2})\s+(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)/i);
    if (m) return {year: Number(m[1]), month: monthNames[m[2].toLowerCase()]};
    const currentVal = String(opener?.value || '');
    m = currentVal.match(/^(\d{4})-(\d{2})-/);
    if (m) return {year: Number(m[1]), month: Number(m[2])};
    const now = new Date();
    return {year: now.getFullYear(), month: now.getMonth() + 1};
};
const panelRoot = () => {
    const panels = allVisible(
        '.el-picker-panel,.ant-picker-dropdown,.n-date-panel,'
        + '.mx-datepicker-main,.datepicker,[role=dialog],.el-popper'
    );
    return panels[panels.length - 1] || document;
};

/* Prev/Next month button: heavily score-based to avoid the year-arrow trap
 * (Element Plus has .arrow-right for month AND .d-arrow-right for year on
 * the same panel, both visible). Penalizes anything tagged year/双箭头. */
const navButton = (direction) => {
    const root = panelRoot();
    const cands = allVisible([
        'button',
        '.el-picker-panel__icon-btn',
        '.ant-picker-header-prev-btn',
        '.ant-picker-header-next-btn',
        '[aria-label]',
        '[title]'
    ].join(','), root);
    const wantedNext = direction === 'next';
    const scored = [];
    for (const el of cands) {
        if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
        const raw = [
            el.getAttribute('aria-label'),
            el.getAttribute('title'),
            el.innerText,
            el.textContent,
            el.className,
        ].filter(Boolean).join(' ').toLowerCase();
        const cls = String(el.className || '').toLowerCase();
        const yearish = /year|年|d-arrow|double|super/.test(raw);
        let score = 0;
        if (wantedNext) {
            if (/next\s*month|下个?月|下一月/.test(raw)) score += 100;
            if (/arrow-right/.test(cls) && !/d-arrow-right/.test(cls)) score += 70;
            if (/ant-picker-header-next-btn/.test(cls)) score += 80;
            if (/next/.test(raw) && !yearish) score += 25;
        } else {
            if (/prev(?:ious)?\s*month|上个?月|上一月/.test(raw)) score += 100;
            if (/arrow-left/.test(cls) && !/d-arrow-left/.test(cls)) score += 70;
            if (/ant-picker-header-prev-btn/.test(cls)) score += 80;
            if (/prev|previous/.test(raw) && !yearish) score += 25;
        }
        if (yearish) score -= 120;
        if (score > 0) scored.push({el, score});
    }
    scored.sort((a, b) => b.score - a.score);
    return scored[0]?.el || null;
};
const panelMonth = visiblePanelMonth();
const monthDelta = (target.year - panelMonth.year) * 12 + (target.month - panelMonth.month);
const navDirection = monthDelta >= 0 ? 'next' : 'prev';
for (let i = 0; i < Math.min(Math.abs(monthDelta), 36); i++) {
    const beforeNav = visiblePanelMonth();
    const btn = navButton(navDirection);
    if (!btn) return {ok: false, reason: 'month_nav_not_found', monthDelta};
    clickEl(btn);
    /* Wait until the panel actually advanced, up to 800ms */
    for (let j = 0; j < 10; j++) {
        await sleep(80);
        const afterNav = visiblePanelMonth();
        if (afterNav.year !== beforeNav.year || afterNav.month !== beforeNav.month) break;
    }
}

const ymd = `${target.year}-${String(target.month).padStart(2, '0')}-${String(target.day).padStart(2, '0')}`;

/* Observed-value match across many separator conventions.
 * Includes MDY and DMY layouts and a compact 8-digit form. */
const isTargetObserved = (observed) => {
    const obsNorm = sepNorm(observed);
    const ymdNorm = sepNorm(ymd);
    const ymdLooseNorm = sepNorm(`${target.year}-${target.month}-${target.day}`);
    const mdyLooseNorm = sepNorm(`${target.month}-${target.day}-${target.year}`);
    const dmyLooseNorm = sepNorm(`${target.day}-${target.month}-${target.year}`);
    const compactObserved = obsNorm.replace(/-/g, '');
    const compactYmd = String(target.year) +
        String(target.month).padStart(2, '0') +
        String(target.day).padStart(2, '0');
    return Boolean(
        String(observed || '').includes(ymd) ||
        obsNorm.includes(ymdNorm) ||
        obsNorm.includes(ymdLooseNorm) ||
        obsNorm.includes(mdyLooseNorm) ||
        obsNorm.includes(dmyLooseNorm) ||
        compactObserved.includes(compactYmd)
    );
};

/* Optional last-resort fallback (off by default) — Element Plus' input is
 * usually readonly and rejects setNativeValue, but Naive UI / Vant accept it.
 * Enabled via main.py config DATE_PICK_DIRECT_SET_FALLBACK. */
const directSetAndCheck = async (reason) => {
    if (!allowDirectSet) {
        return {
            ok: false,
            targetDate: ymd,
            observed: String(opener.value || textOf(opener) || '').trim(),
            directSet: false,
            directSetDisabled: true,
            directSetReason: reason,
        };
    }
    const observed = setNativeValue(opener, ymd);
    await sleep(200);
    const finalObserved = String(opener.value || textOf(opener) || observed || '').trim();
    return {
        ok: isTargetObserved(finalObserved),
        targetDate: ymd,
        observed: finalObserved,
        directSet: true,
        directSetReason: reason,
    };
};

const waitForObservedValue = async () => {
    let observed = '';
    for (let i = 0; i < 12; i++) {
        await sleep(i === 0 ? 80 : 120);
        observed = String(opener.value || textOf(opener) || '').trim();
        if (isTargetObserved(observed)) return observed;
        if (i >= 2 && allVisible(
            '.el-picker-panel,.ant-picker-dropdown,.n-date-panel,'
            + '.mx-datepicker-main,.datepicker,[role=dialog],.el-popper'
        ).length === 0) {
            return observed;
        }
    }
    return observed;
};

/* Extract the numeric day from a cell — probe known nested containers in
 * order. Returns null when no descendant has a bare 1-31 string (which
 * happens for headers, empty padding cells, etc). */
const cellDayNumber = (cell) => {
    const probes = [
        cell.querySelector?.('.el-date-table-cell__text'),
        cell.querySelector?.('.ant-picker-cell-inner'),
        cell.querySelector?.('.n-date-panel-date__trigger'),
        cell.querySelector?.('button > span'),
        cell.querySelector?.('span'),
        cell,
    ].filter(Boolean);
    for (const node of probes) {
        const t = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
        const m = t.match(/^([12]?\d|3[01])$/);
        if (m) return Number(m[1]);
    }
    return null;
};

for (let attempt = 0; attempt < 12; attempt++) {
    const panels = allVisible(
        '.el-picker-panel,.ant-picker-dropdown,.n-date-panel,'
        + '.mx-datepicker-main,.datepicker,[role=dialog],.el-popper'
    );
    const root = panels[panels.length - 1] || document;
    const cells = allVisible('td,button,[role=gridcell],.el-date-table-cell,.ant-picker-cell-inner', root);
    const seen = new Set();
    const hits = [];
    for (const el of cells) {
        const cell = el.closest('td,button,[role=gridcell]') || el;
        if (!isVisible(cell)) continue;
        if (seen.has(cell)) continue;
        seen.add(cell);
        const disabled = cell.matches('.disabled,.is-disabled,.ant-picker-cell-disabled,[aria-disabled=true]') ||
            cell.closest('.disabled,.is-disabled,.ant-picker-cell-disabled,[aria-disabled=true]');
        if (disabled) continue;
        const cls = cell.classList || {contains: () => false};
        const isOtherMonth = (
            cls.contains('prev-month') || cls.contains('next-month') ||
            cls.contains('is-prev-month') || cls.contains('is-next-month') ||
            cls.contains('other-month') || cls.contains('is-other-month') ||
            cls.contains('n-date-panel-date--other-month') ||
            cell.matches?.('.ant-picker-cell:not(.ant-picker-cell-in-view)')
        );
        if (isOtherMonth) continue;
        const dayNum = cellDayNumber(cell);
        if (dayNum === null) continue;
        if (dayNum !== target.day) continue;
        let score = 1000;
        if (cls.contains('available') || cls.contains('ant-picker-cell-in-view')) score += 50;
        if (cls.contains('current') || cls.contains('is-current')) score += 30;
        if (cls.contains('today') || cls.contains('is-today')) score += 5;
        const ariaLabel = (cell.getAttribute('aria-label') || el.getAttribute('aria-label') || '').toLowerCase();
        if (ariaLabel.includes(String(target.year))) score += 40;
        hits.push({cell, score, dayNum, ariaLabel});
    }
    hits.sort((a, b) => b.score - a.score);
    if (hits[0]) {
        clickEl(hits[0].cell);
        const observed = await waitForObservedValue();
        if (isTargetObserved(observed)) {
            return {ok: true, targetDate: ymd, observed, clickedDay: hits[0].dayNum, clickedAria: hits[0].ariaLabel};
        }
        const direct = await directSetAndCheck('postcheck_failed_after_cell_click');
        if (direct.ok) return {...direct, clickedDay: hits[0].dayNum, clickedAria: hits[0].ariaLabel};
        return {
            ok: false, reason: 'postcheck_failed', targetDate: ymd, observed,
            clickedDay: hits[0].dayNum, clickedAria: hits[0].ariaLabel, hitsCount: hits.length,
        };
    }
    await sleep(120);
}
const direct = await directSetAndCheck('date_cell_not_found');
if (direct.ok) return direct;
return {ok: false, reason: 'date_cell_not_found', targetDate: ymd, observed: direct.observed};
"""


# ── VL judge fallback question ─────────────────────────────────────────────────
def _question(step: dict, result: dict) -> str | None:
    target_date = str(step.get("target_date") or "").strip()
    if not target_date or not re.match(r"^\d{4}-\d{2}-\d{2}", target_date):
        return None
    slash = target_date.replace("-", "/")
    try:
        cn = f"{target_date[:4]}年{int(target_date[5:7])}月{int(target_date[8:10])}日"
    except (ValueError, IndexError):
        cn = target_date
    return (
        f"截图中是否有日期输入框已显示为 {target_date}（或其等价格式如 "
        f"{slash} / {cn}）？"
    )


# ── Registration ───────────────────────────────────────────────────────────────
MACRO = Macro(
    action="date_pick",
    parse=parse,
    js_body=_JS_BODY,
    postcheck_question=_question,
    retryable_with_vl_reasons=frozenset({"postcheck_failed", "date_cell_not_found"}),
    priority=50,
)

register(MACRO)

"""cascader_pick: drill into a multi-column cascader by labels.

Failure mode it addresses: VLM gets lost in multi-column dropdowns (Element
Plus cascader, Ant Design cascader, etc.) because SoM red-box numbers don't
align cleanly to menuitems and the panel auto-collapses on blur. The
trajectory we saw (`run_log_20260507_142125.html`) burned 20 steps clicking
the wrong @eN repeatedly.

Goal pattern recognized:
  "在基础级联选择器中，依次点击展开：Guide -> Navigation -> Top Navigation。"

Step shape produced:
  {
    "action": "cascader_pick",
    "anchor": {"section": "基础级联选择器", "placeholder": "Select", "occurrence": 1},
    "path": ["Guide", "Navigation", "Top Navigation"],
  }

Replay strategy:
  1. Locate input via findInputByAnchor (section + placeholder + occurrence)
  2. clickEl to open the dropdown
  3. For each label: find the rightmost visible menuitem with matching label
     text, clickEl, wait for the next column to render
  4. Postcheck: opener.value contains every label in order (separator-agnostic)
  5. On postcheck_failed, fall back to vlm.judge_screenshot
"""

from __future__ import annotations

import re

from . import register
from ._types import Macro

# ── Goal parser ────────────────────────────────────────────────────────────────

# Path-keyword trigger: matches the verb introducing a path chain *and*
# requires (lookahead) a separator nearby. The lookahead is what keeps section
# anchors like "基础级联选择器" from hijacking the match — we only fire when
# arrows/dashes follow within ~30 chars.
_PATH_KEYWORDS = re.compile(
    r"(?:依次|逐级)(?:点击|选择|展开|进入|选)*"
    r"(?=[\s:：，,]{0,5}[^。\n]{0,30}(?:->|→|>>|>|›|»))|"
    r"按路径\s*(?:选择|选|进入)?"
    r"(?=[\s:：，,]{0,5}[^。\n]{0,30}(?:->|→|>>|>|›|»))|"
    r"路径(?:为|是)\s*[:：]|"
    r"select\s+(?:the\s+)?(?:cascader\s+)?path\s*[:：]?|"
    r"drill\s+(?:into|down)\s+(?:the\s+)?cascader",
    re.I,
)

_SEPARATORS = re.compile(r"\s*(?:->|→|>>|>|›|»|/|\\)\s*")

_SECTION_FROM_GOAL = re.compile(
    r"在\s*([^，。\n,]+?)\s*(?:中|里|内|里面|中间)"
    r"(?=.{0,40}(?:依次|逐级|选择|展开|点击|cascader))",
    re.I,
)

_PLACEHOLDER_QUOTED = re.compile(
    r"[\"“”‘’'「『]([^\"“”‘’'」』]{1,40})[\"“”‘’'」』]"
)


def _extract_path_from_goal(goal: str) -> list[str] | None:
    """Find the path chain "A -> B -> C" near a path-keyword trigger."""
    text = str(goal or "")
    trigger = _PATH_KEYWORDS.search(text)
    if not trigger:
        return None
    # Look ahead from the trigger for the first separator-rich chunk.
    tail = text[trigger.end():]
    # Stop at sentence end / next clause.
    stop = re.search(r"[。\n]|最后|然后|确认|输出|done", tail)
    chunk = tail[: stop.start()] if stop else tail
    # Trim leading punctuation/colon.
    chunk = chunk.lstrip(":：，, \t")
    parts = [p.strip(" \t『』「」\"'") for p in _SEPARATORS.split(chunk)]
    parts = [p for p in parts if p and 1 <= len(p) <= 60]
    if len(parts) < 2:
        return None
    return parts


# Component-library section aliases — when the goal mentions a demo page
# heading we know the English equivalent of, we substitute it so
# findInputByAnchor matches the English heading on element-plus.org etc.
_SECTION_ALIASES: tuple[tuple[str, str], ...] = (
    (r"基础级联选择器|基础用法|basic\s+usage", "Basic usage"),
    (r"禁用选项|disabled\s+option", "Disabled option"),
    (r"可清空|clearable", "Clearable"),
    (r"仅显示最后一级|show\s+last\s+level", "Only show last level"),
)


def _extract_section(goal: str) -> str:
    m = _SECTION_FROM_GOAL.search(goal or "")
    if m:
        return m.group(1).strip()
    text = str(goal or "")
    for pattern, label in _SECTION_ALIASES:
        if re.search(pattern, text, re.I):
            return label
    return ""


def _extract_placeholder(goal: str) -> str:
    # Prefer a quoted placeholder if present (e.g. 'Select' / 'Please choose')
    for quoted in _PLACEHOLDER_QUOTED.findall(goal or ""):
        q = quoted.strip()
        if re.search(r"select|choose|请选择|请选|placeholder", q, re.I):
            return q
    return "Select"


def _extract_occurrence(goal: str) -> int:
    """Parse '第N个' / 'Nth' / 'second' style hints. Defaults to 1."""
    text = str(goal or "")
    m = re.search(r"第\s*([0-9]+)\s*个", text)
    if m:
        return max(1, int(m.group(1)))
    if re.search(r"\bsecond\b|第二个", text, re.I):
        return 2
    if re.search(r"\bthird\b|第三个", text, re.I):
        return 3
    return 1


def parse(goal: str) -> dict | None:
    text = str(goal or "")
    if not text:
        return None
    path = _extract_path_from_goal(text)
    if not path:
        return None
    return {
        "action": "cascader_pick",
        "anchor": {
            "section": _extract_section(text),
            "placeholder": _extract_placeholder(text),
            "occurrence": _extract_occurrence(text),
        },
        "path": path,
        "validate": {"input_should_contain_each": list(path)},
    }


# ── JS body ────────────────────────────────────────────────────────────────────
# Ported from main.py's inline cascader_pick branch (the user's battle-tested
# implementation, ~250 lines). Uses primitives ``norm/textOf/isVisible/
# allVisible/sleep/clickEl`` from the bundle. Cascader-specific helpers
# (``labelOf``, ``popupRoots``, ``matchMenuItem`` with left-column tracking)
# stay local to this body.
_JS_BODY = r"""
const {anchor, path: pathLabels, validate = {}} = step;

const labelOf = (el) => {
    const probes = [
        el?.querySelector?.('.el-cascader-node__label'),
        el?.querySelector?.('.ant-cascader-menu-item-content'),
        el?.querySelector?.('[data-label]'),
        el?.querySelector?.('span'),
        el,
    ].filter(Boolean);
    for (const node of probes) {
        const text = String(
            node.innerText || node.textContent || node.getAttribute?.('aria-label') || ''
        ).replace(/\s+/g, ' ').trim();
        if (text) return text;
    }
    return '';
};

const popupRoots = () => allVisible([
    '.el-popper',
    '.el-cascader-panel',
    '.ant-cascader-menus',
    '.ant-select-dropdown',
    '[role="menu"]',
    '[role="listbox"]',
    '.dropdown-menu'
].join(','));

const MENUITEM_SELECTOR = [
    '[role="menuitem"]',
    '[role="option"]',
    '.el-cascader-node',
    '.ant-cascader-menu-item',
    '.dropdown-item'
].join(',');

const availableLabels = () => {
    const labels = [];
    for (const root of popupRoots()) {
        for (const node of allVisible(MENUITEM_SELECTOR, root)) {
            const label = labelOf(node);
            if (label) labels.push(label);
        }
    }
    return Array.from(new Set(labels)).slice(0, 12);
};

/* Cascader-specific input finder: like primitives.findInputByAnchor but
 * resolves the click target to the wrapper (`.el-cascader / .el-input`)
 * because Element Plus's inner <input readonly> swallows clicks via
 * `@mousedown.stop`. Returns {input, clickTarget} so postcheck reads value
 * from the input but the click hits the wrapper. */
const sectionText = norm(anchor?.section || '');
const placeholder = norm(anchor?.placeholder || '');
const occurrence = Math.max(1, Number(anchor?.occurrence || 1));
const inputs = allVisible('input,textarea,[role=combobox],[contenteditable=true]');
const candidates = [];
for (const input of inputs) {
    const clickTarget = input.closest?.(
        '.el-cascader,.el-input,.el-input__wrapper,[role=combobox]'
    ) || input;
    if (!isVisible(clickTarget)) continue;
    const r = clickTarget.getBoundingClientRect();
    const rawValue = String(input.value ?? input.getAttribute?.('value') ?? '').trim();
    const placeholderAttr = norm(input.getAttribute?.('placeholder') || '');
    const fieldText = norm([textOf(input), textOf(clickTarget)].join(' '));
    let score = 100;
    if (placeholder && (fieldText.includes(placeholder) || placeholderAttr.includes(placeholder))) score += 360;
    if (!rawValue) score += 60;
    if (input.readOnly || (input.getAttribute('role') || '').toLowerCase() === 'combobox') score += 35;
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
    candidates.push({input, clickTarget, score});
}
candidates.sort((a, b) => b.score - a.score);
const openerEntry = candidates[occurrence - 1] || candidates[0];
if (!openerEntry) {
    return {ok: false, reason: 'cascader_input_not_found', anchor, candidateCount: candidates.length};
}
const opener = openerEntry.input;

clickEl(openerEntry.clickTarget);
await sleep(300);

/* Left-column tracking: each click reveals a new column to the right. Penalize
 * matches whose left coord is to the left of the previously-clicked column,
 * preventing "go back up to a column already visited" mistakes when the same
 * label appears in two columns. */
const matchMenuItem = (label, minLeft) => {
    const target = norm(label);
    const roots = popupRoots().slice().reverse();
    const hits = [];
    for (const root of roots) {
        for (const node of allVisible(MENUITEM_SELECTOR, root)) {
            if (node.matches?.('.is-disabled,.disabled,[aria-disabled="true"],.ant-cascader-menu-item-disabled')) continue;
            if (node.closest?.('.is-disabled,.disabled,[aria-disabled="true"],.ant-cascader-menu-item-disabled')) continue;
            const labelText = labelOf(node);
            if (!labelText || norm(labelText) !== target) continue;
            const r = node.getBoundingClientRect();
            let score = 1000;
            score += r.left / 20;
            score -= Math.abs(r.top - window.innerHeight / 2) / 80;
            if (minLeft >= 0 && r.left + 4 >= minLeft) score += 80;
            if (minLeft >= 0 && r.left + 4 < minLeft) score -= 220;
            hits.push({node, text: labelText, left: r.left, top: r.top, score});
        }
    }
    hits.sort((a, b) => b.score - a.score);
    return hits[0] || null;
};

const clicked = [];
let minLeft = -1;
for (let index = 0; index < pathLabels.length; index++) {
    const label = String(pathLabels[index] || '').trim();
    let hit = null;
    for (let attempt = 0; attempt < 10; attempt++) {
        hit = matchMenuItem(label, minLeft);
        if (hit) break;
        await sleep(120);
    }
    if (!hit) {
        return {
            ok: false, reason: 'menuitem_not_found',
            label, stepIndex: index, clicked, availableLabels: availableLabels(),
        };
    }
    clickEl(hit.node);
    clicked.push({label, observed: hit.text, left: hit.left, top: hit.top});
    minLeft = Math.max(minLeft, hit.left);
    await sleep(250);
}

/* Postcheck with retries: Element Plus' v-model update after the final-leaf
 * click is async; observed value may be empty for ~100-200ms. */
const expected = String(validate?.input_should_contain || pathLabels.join(' / ')).trim();
const readObserved = () => {
    const probes = [
        opener.value,
        opener.getAttribute?.('value'),
        textOf(opener),
        textOf(opener.closest?.('.el-cascader,.el-input,.el-form-item') || opener),
    ];
    return probes.filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
};
for (let attempt = 0; attempt < 10; attempt++) {
    await sleep(attempt === 0 ? 100 : 140);
    const observed = readObserved();
    if (expected && norm(observed).includes(norm(expected))) {
        return {ok: true, expected, observed, clicked};
    }
}
return {ok: false, reason: 'postcheck_failed', expected, observed: readObserved(), clicked};
"""


# ── VL judge fallback question ─────────────────────────────────────────────────
def _question(step: dict, result: dict) -> str | None:
    path = step.get("path") or []
    if not path:
        return None
    validate = step.get("validate") or {}
    expected = str(validate.get("input_should_contain") or " / ".join(path))
    last_label = str(path[-1])
    return (
        f"截图中的级联输入框是否已经显示「{expected}」，"
        f"或至少最终值「{last_label}」已经生效？"
    )


# ── Registration ───────────────────────────────────────────────────────────────
MACRO = Macro(
    action="cascader_pick",
    parse=parse,
    js_body=_JS_BODY,
    postcheck_question=_question,
    retryable_with_vl_reasons=frozenset({"postcheck_failed"}),
    priority=50,
)

register(MACRO)

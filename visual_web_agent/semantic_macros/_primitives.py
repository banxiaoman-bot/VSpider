"""Shared JS primitives bundle injected before every macro body.

These functions are reused across calendars, cascaders, menus, search results,
and any other multi-step DOM-affecting macro. Lifting them out of every
``page.evaluate`` call keeps each macro body small and ensures the click /
visibility / anchor-find logic stays consistent.

Conventions (must hold for every primitive):
- All primitives are ``const`` declarations, never overwritten by macro bodies.
- Selectors target visible, interactive elements only.
- ``clickEl`` dispatches mousedown + mouseup + click + focus to defeat Vue's
  ``@mousedown.stop`` patterns (Element Plus, Naive UI), and uses CDP-level
  initialization. Macros SHOULD use this rather than ``el.click()``.

Macros consume primitives via ``build_macro_js(body)`` in ``__init__``.
"""

from __future__ import annotations

# Triple-backslashes: Python source escapes once → JS string sees \\s\\d\\u etc.
MACRO_PRIMITIVES: str = r"""
const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();

const textOf = (el) => [
    el?.innerText, el?.textContent, el?.getAttribute?.('aria-label'),
    el?.getAttribute?.('placeholder'), el?.getAttribute?.('title'),
    el?.getAttribute?.('value'), el?.name, el?.id
].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();

const isVisible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    const s = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 &&
        s.display !== 'none' && s.visibility !== 'hidden' &&
        Number(s.opacity || '1') > 0;
};

const allVisible = (selector, root = document) =>
    Array.from(root.querySelectorAll(selector)).filter(isVisible);

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const clickEl = (el) => {
    el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const init = {
        bubbles: true, cancelable: true, composed: true,
        view: window, button: 0, buttons: 1,
        clientX: cx, clientY: cy, screenX: cx, screenY: cy,
    };
    el.dispatchEvent(new MouseEvent('mousedown', init));
    el.dispatchEvent(new MouseEvent('mouseup', init));
    el.dispatchEvent(new MouseEvent('click', init));
    if (typeof el.focus === 'function') {
        try { el.focus({preventScroll: true}); } catch (e) { el.focus(); }
    }
};

/* findInputByAnchor: locate an input/combobox by (section heading, placeholder,
 * occurrence). Returns the best candidate element or null. Used by date_pick,
 * cascader_pick, and any other widget that opens via input click. */
const findInputByAnchor = (anchor) => {
    const sectionText = norm(anchor?.section || '');
    const placeholder = norm(anchor?.placeholder || '');
    const occurrence = Math.max(1, Number(anchor?.occurrence || 1));
    const inputs = allVisible('input,textarea,[role=combobox],[contenteditable=true]');
    const candidates = [];
    for (const input of inputs) {
        const t = norm(textOf(input));
        if (placeholder && !t.includes(placeholder)) continue;
        const r = input.getBoundingClientRect();
        let score = 100;
        if (placeholder && t.includes(placeholder)) score += 200;
        if ((input.getAttribute('placeholder') || '').trim()) score += 40;
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
    return candidates[occurrence - 1]?.input || candidates[0]?.input || null;
};

/* findVisiblePopper: latest visible popper / dropdown / dialog root. Useful as
 * the search scope for menuitems and date cells. Falls back to document. */
const findVisiblePopper = () => {
    const panels = allVisible(
        '.el-picker-panel,.ant-picker-dropdown,.n-date-panel,'
        + '.mx-datepicker-main,.datepicker,[role=dialog],.el-popper,'
        + '[role=menu],[role=listbox],.el-cascader-menu,.el-cascader__dropdown'
    );
    return panels[panels.length - 1] || document;
};

/* sepNorm: collapse common date separators (/, ., 年月日, whitespace) to '-'
 * for fuzzy date comparison (2026-06-15 ≡ 2026/06/15 ≡ 2026年6月15日). */
const sepNorm = (s) => norm(s)
    .replace(/[\/.年月日\s]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
"""

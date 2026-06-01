"""DOM fallback for ``type target_id=0`` when targeted_probe finds no @eN."""

from __future__ import annotations

from typing import Any


FILL_BEST_INPUT_JS = r"""
([value]) => {
    const text = String(value ?? "");
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
    const isEditable = (el) => {
        if (!el) return false;
        const tag = (el.tagName || '').toLowerCase();
        const type = String(el.getAttribute('type') || '').toLowerCase();
        return (
            tag === 'textarea' ||
            (tag === 'input' && !['hidden', 'button', 'submit', 'reset', 'checkbox', 'radio', 'file'].includes(type)) ||
            el.isContentEditable ||
            String(el.getAttribute('role') || '').toLowerCase() === 'searchbox'
        );
    };
    const score = (el) => {
        let s = 0;
        const tag = (el.tagName || '').toLowerCase();
        const attrs = [
            el.getAttribute('role'),
            el.getAttribute('type'),
            el.getAttribute('name'),
            el.getAttribute('id'),
            el.getAttribute('placeholder'),
            el.getAttribute('aria-label'),
            el.getAttribute('title'),
        ].join(' ').toLowerCase();
        if (el === document.activeElement) s += 100;
        if (attrs.includes('searchbox') || attrs.includes('search')) s += 50;
        if (attrs.includes('q') || attrs.includes('query')) s += 20;
        if (attrs.includes('搜索') || attrs.includes('搜寻') || attrs.includes('输入搜索')) s += 50;
        if (tag === 'input') s += 10;
        if (tag === 'textarea') s += 5;
        return s;
    };
    const seen = new Set();
    const candidates = [];
    const push = (el) => {
        if (!el || seen.has(el) || !isEditable(el) || !visible(el)) return;
        seen.add(el);
        candidates.push(el);
    };
    push(document.activeElement);
    for (const sel of [
        'input[type="search"]',
        'input[name="q"]',
        'input[aria-label*="搜索" i]',
        'input[placeholder*="搜索" i]',
        '[role="searchbox"]',
        'textarea',
        'input:not([type])',
        'input[type="text"]',
        '[contenteditable="true"]'
    ]) {
        for (const el of Array.from(document.querySelectorAll(sel))) push(el);
    }
    candidates.sort((a, b) => score(b) - score(a));
    const el = candidates[0];
    if (!el) return {ok: false, reason: 'no_visible_input'};
    el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
    el.focus();
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea') {
        const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
        if (setter) setter.call(el, text);
        else el.value = text;
    } else {
        el.textContent = text;
    }
    el.dispatchEvent(new InputEvent('input', {bubbles: true, composed: true, inputType: 'insertText', data: text}));
    el.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
    return {
        ok: true,
        tag,
        role: el.getAttribute('role') || '',
        type: el.getAttribute('type') || '',
        name: el.getAttribute('name') || '',
        placeholder: el.getAttribute('placeholder') || '',
        ariaLabel: el.getAttribute('aria-label') || '',
        value: tag === 'input' || tag === 'textarea' ? el.value : el.textContent,
        score: score(el),
    };
}
"""


async def fill_best_text_input(page: Any, value: str) -> dict[str, Any]:
    """Try every frame and fill the best visible text/search input."""
    for frame in getattr(page, "frames", []) or [getattr(page, "main_frame", None)]:
        if frame is None:
            continue
        try:
            result = await frame.evaluate(FILL_BEST_INPUT_JS, [value])
        except Exception:
            continue
        if isinstance(result, dict) and result.get("ok"):
            result = dict(result)
            result["frame_url"] = getattr(frame, "url", "")
            result["frame_name"] = getattr(frame, "name", "")
            return result
    return {"ok": False, "reason": "no_visible_input"}

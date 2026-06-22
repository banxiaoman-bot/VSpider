"""HITL Form Proxy: detect visible form fields and relay them to the frontend.

Provides ``try_hitl_form_proxy(page, reason)`` which:
1. Scans the page for visible input/textarea/select elements via Playwright.
2. Sends them to the frontend as a ``hitl_form`` WS event.
3. Waits for the user to fill and submit.
4. Injects the submitted values back into the page via Playwright.

Returns ``True`` if the form proxy was used successfully, ``False`` to fall
back to the traditional browser-window HITL.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("vspider.hitl_form_proxy")

_FIELD_JS = """
() => {
    const fields = [];
    const seen = new Set();
    for (const el of document.querySelectorAll(
        'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]):not([type="file"]):not([type="checkbox"]):not([type="radio"]), textarea, select'
    )) {
        const rect = el.getBoundingClientRect();
        if (rect.width < 10 || rect.height < 10) continue;
        if (el.disabled || el.readOnly) continue;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;

        const id = el.id || el.name || `field_${fields.length}`;
        if (seen.has(id)) continue;
        seen.add(id);

        let label = '';
        if (el.id) {
            const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (lbl) label = lbl.textContent.trim();
        }
        if (!label && el.closest('label')) {
            label = el.closest('label').textContent.trim();
        }
        if (!label && el.getAttribute('aria-label')) {
            label = el.getAttribute('aria-label');
        }

        fields.push({
            id: id,
            tag: el.tagName.toLowerCase(),
            type: el.type || 'text',
            name: el.name || '',
            label: label,
            placeholder: el.placeholder || '',
            value: el.value || '',
            required: el.required || false,
            selector: _buildSelector(el),
        });
    }
    return fields;

    function _buildSelector(el) {
        if (el.id) return '#' + CSS.escape(el.id);
        if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
        const parent = el.parentElement;
        if (!parent) return el.tagName.toLowerCase();
        const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
        const idx = siblings.indexOf(el);
        return _buildSelector(parent) + ' > ' + el.tagName.toLowerCase() + (siblings.length > 1 ? `:nth-of-type(${idx + 1})` : '');
    }
}
"""

MIN_FIELDS_FOR_FORM = 1


async def detect_form_fields(page: Any) -> list[dict]:
    """Return a list of visible, editable form fields on the page."""
    try:
        fields = await page.evaluate(_FIELD_JS)
        return fields if isinstance(fields, list) else []
    except Exception as exc:
        logger.debug("Form field detection failed: %s", exc)
        return []


async def fill_form_fields(page: Any, fields: list[dict], values: dict) -> int:
    """Inject user-submitted values back into the page. Returns filled count."""
    filled = 0
    for field in fields:
        val = values.get(field["id"], "")
        if not val:
            continue
        selector = field.get("selector", "")
        if not selector:
            continue
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            await locator.fill(str(val))
            filled += 1
        except Exception as exc:
            logger.warning("Failed to fill field %s: %s", field["id"], exc)
    return filled


async def try_hitl_form_proxy(page: Any, reason: str = "") -> bool:
    """Attempt HITL via form proxy. Returns True if handled, False to fallback."""
    fields = await detect_form_fields(page)
    if len(fields) < MIN_FIELDS_FOR_FORM:
        return False

    try:
        from broadcast import broadcast_hitl_form, wait_for_hitl_form_result
    except ImportError:
        return False

    form_fields = [
        {
            "id": f["id"],
            "type": f["type"],
            "label": f["label"],
            "placeholder": f["placeholder"],
            "value": f["value"],
            "required": f["required"],
            "name": f["name"],
        }
        for f in fields
    ]

    screenshot = ""
    try:
        b64 = await page.screenshot(type="jpeg", quality=60)
        import base64
        screenshot = "data:image/jpeg;base64," + base64.b64encode(b64).decode()
    except Exception:
        pass

    if not broadcast_hitl_form(form_fields, reason=reason, screenshot=screenshot):
        return False

    result = await wait_for_hitl_form_result()
    if result is None:
        return False

    filled = await fill_form_fields(page, fields, result)
    logger.info("[HITL-FORM] Filled %d/%d fields via form proxy", filled, len(fields))
    return True

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from ._base import (
    ActionContext,
    ActionHandler,
    ActionRegistry,
    UnknownActionError,
    _click_locator_with_js_fallback,
    _is_navigation_context_destroyed,
    _resolve_env_placeholders,
    _scroll_largest_container,
)

try:
    from ..vlm_client import VSpiderAction
    from ..browser_env import ActionExecutionError
    from ..auth_vault import SecretResolutionError, resolve_env_placeholders as _resolve_env
    from ..artifact_manager import register_download_artifact
    from ..page_data_controller import DATA_SIGNATURE_JS, pagination_moved
    from ..chat_answer_extractor import clean_chat_answer_text, extract_chat_answer
    from ..chat_send_locator import find_send_button as _chat_find_send_button
except ImportError:
    from vlm_client import VSpiderAction
    from browser_env import ActionExecutionError
    from auth_vault import SecretResolutionError, resolve_env_placeholders as _resolve_env
    from artifact_manager import register_download_artifact
    from page_data_controller import DATA_SIGNATURE_JS, pagination_moved
    from chat_answer_extractor import clean_chat_answer_text, extract_chat_answer
    from chat_send_locator import find_send_button as _chat_find_send_button

if TYPE_CHECKING:
    from playwright.async_api import Page
    from ..browser_env import BrowserEnv

logger = logging.getLogger("vspider.actions")


"""FindText + FormSet handler family"""

@ActionRegistry.register("find_text")
class FindTextHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        page = ctx.page
        text = (ctx.action.type_value or "").strip()
        if not text:
            raise ActionExecutionError("find_text 缺少 type_value，请填写要定位的可见文字或字段标签。")

        result = await page.evaluate(
            """(query) => {
                const q = String(query || '').trim().toLowerCase();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el.innerText, el.textContent, el.getAttribute('aria-label'),
                    el.getAttribute('placeholder'), el.getAttribute('title'),
                    el.getAttribute('value'), el.name, el.id
                ].filter(Boolean).join(' ').trim();
                const selector = [
                    'main *', '[role=main] *', 'article *', 'section *', 'form *',
                    'label', 'input', 'textarea', 'select', 'button',
                    '[role=button]', '[role=textbox]', '[role=combobox]',
                    '[role=checkbox]', '[role=radio]', 'h1', 'h2', 'h3', 'h4',
                    'p', 'div', 'span'
                ].join(',');
                const nodes = Array.from(document.querySelectorAll(selector));
                const candidates = [];
                for (const el of nodes) {
                    if (!isVisible(el)) continue;
                    const t = textOf(el);
                    if (!t || !t.toLowerCase().includes(q)) continue;
                    const r = el.getBoundingClientRect();
                    let score = 0;
                    const tag = el.tagName.toLowerCase();
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    if (['label','input','textarea','select','button','h1','h2','h3','h4'].includes(tag)) score += 40;
                    if (['textbox','combobox','checkbox','radio','button'].includes(role)) score += 30;
                    if (el.closest('form')) score += 35;
                    if (el.closest('main,[role=main],article')) score += 15;
                    if (r.left > 180) score += 25;
                    if (r.width * r.height < 150000) score += 10;
                    if (t.trim().toLowerCase() === q) score += 25;
                    score -= Math.abs((r.top + r.height / 2) - window.innerHeight / 2) / 80;
                    candidates.push({el, score, tag, role, text: t.slice(0, 120), left: r.left, top: r.top});
                }
                candidates.sort((a, b) => b.score - a.score);
                const best = candidates[0];
                if (!best) return {found: false, query};
                best.el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                return {
                    found: true, query, tag: best.tag, role: best.role,
                    text: best.text, left: best.left, top: best.top, score: best.score
                };
            }""",
            text,
        )
        await asyncio.sleep(0.6)
        if not result.get("found"):
            raise ActionExecutionError(
                f"find_text 未找到可见文本 {text!r}。请换更短的字段标签/按钮文字，或先小幅滚动。"
            )
        logger.info(
            "[FIND_TEXT] query=%r matched tag=%s role=%s text=%r score=%s",
            text,
            result.get("tag"),
            result.get("role"),
            result.get("text"),
            result.get("score"),
        )
        print(f"[FIND_TEXT] located text: {text}")
        ctx.browser.rpa_trail.append(
            ctx.with_rpa_meta({"action": "find_text", "type_value": text})
        )
        return None


def _parse_form_set_payload(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    if not text:
        raise ActionExecutionError(
            "form_set 缺少 type_value。格式示例：Activity name=VSpider 测试"
        )
    if text.startswith("{"):
        try:
            data = json.loads(text)
            label = str(data.get("label") or data.get("field") or "").strip()
            value = str(data.get("value") or "").strip()
            if label:
                return label, value
        except Exception:
            pass
    for sep in ("=>", "=", "：", ":"):
        if sep in text:
            left, right = text.split(sep, 1)
            label = left.strip().strip("\"'")
            value = right.strip().strip("\"'")
            if label:
                return label, value
    raise ActionExecutionError(
        "form_set 的 type_value 无法解析。请使用 `字段标签=目标值`，例如 `Activity zone=Zone one`。"
    )


async def _click_visible_text_option(page: "Page", value: str) -> bool:
    if not value:
        return False
    option_selectors = [
        # Element Plus / Element UI
        ".el-select-dropdown__item",
        ".el-dropdown-menu__item",
        ".el-cascader-node",
        ".el-radio",
        ".el-checkbox",
        # Ant Design
        ".ant-select-item",
        ".ant-select-item-option",
        ".ant-dropdown-menu-item",
        # 通用 ARIA
        "[role='option']",
        "[role='menuitem']",
        "[role='listitem']",
        # Naive UI / Vant
        ".n-base-select-option",
        ".van-picker-column__item",
        # 兜底
        "label", "li", "button", "span",
    ]
    # 先轮询等待选项浮层就绪（popper 动画 + teleport 渲染 ~600ms）
    import time as _t
    _start = _t.monotonic()
    _deadline = _start + 2.5  # 最多等 2.5s 等浮层渲染
    while _t.monotonic() < _deadline:
        for selector in option_selectors:
            try:
                loc = page.locator(selector).filter(has_text=value)
                count = await loc.count()
                if count:
                    # 优先 last（避免命中标签），不行再 first
                    for _picker in (loc.last, loc.first):
                        target = _picker
                        try:
                            if await target.is_visible(timeout=1500):
                                await _click_locator_with_js_fallback(
                                    target, f"form option {value!r}", timeout=2000
                                )
                                return True
                        except Exception:
                            continue
            except Exception:
                continue
        # popper 还没就绪，短暂等待后重试
        await asyncio.sleep(0.3)
    # 最后兜底：精确文字匹配整个 page
    try:
        loc = page.get_by_text(value, exact=True).last
        if await loc.is_visible(timeout=1500):
            await _click_locator_with_js_fallback(
                loc, f"form text option {value!r}", timeout=2000
            )
            return True
    except Exception:
        pass
    # 子串模糊兜底
    try:
        loc = page.get_by_text(value, exact=False).first
        if await loc.is_visible(timeout=1500):
            await _click_locator_with_js_fallback(
                loc, f"form text fuzzy {value!r}", timeout=2000
            )
            return True
    except Exception:
        return False
    return False


async def _form_set_bound_control_v2(
    page: "Page", label: str, value: str, *, open_if_needed: bool = True
) -> dict[str, Any]:
    """Set a form field by binding the visible label to one concrete control."""
    result = await page.evaluate(
        """async ({label, value, openIfNeeded}) => {
            const norm = (s) => String(s || '')
                .replace(/\\s+/g, ' ')
                .replace(/[：:]+$/g, '')
                .trim()
                .toLowerCase();
            const labelNorm = norm(label);
            const expected = norm(value);
            const isVisible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    s.display !== 'none' && s.visibility !== 'hidden' &&
                    Number(s.opacity || '1') > 0 &&
                    r.bottom >= 0 && r.right >= 0 &&
                    r.top <= (window.innerHeight || document.documentElement.clientHeight) &&
                    r.left <= (window.innerWidth || document.documentElement.clientWidth);
            };
            const textOf = (el) => [
                el?.innerText,
                el?.textContent,
                el?.getAttribute?.('aria-label'),
                el?.getAttribute?.('placeholder'),
                el?.getAttribute?.('title')
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
            // querySelectorAll that also descends into open shadow roots, so
            // web-component forms (e.g. lit/stencil wrappers) stay reachable.
            const deepQueryAll = (selector, root = document) => {
                const out = Array.from(root.querySelectorAll(selector));
                for (const host of root.querySelectorAll('*')) {
                    if (host.shadowRoot) out.push(...deepQueryAll(selector, host.shadowRoot));
                }
                return out;
            };
            const controlSelector = [
                'input:not([type=hidden])',
                'textarea',
                'select',
                '[contenteditable=true]',
                '[role=combobox]',
                '[role=checkbox]',
                '[role=radio]',
                // TinyMCE classic renders into an editor iframe and keeps
                // its textarea hidden: bind the visible iframe host.
                'iframe[id$="_ifr"]',
                'iframe.tox-edit-area__iframe',
                '.el-select',
                '.ant-select',
                '.n-select',
                '.el-input',
                '.ant-input-affix-wrapper'
            ].join(',');
            const allControls = () => deepQueryAll(controlSelector)
                .filter(isVisible)
                .filter(el => !['button', 'submit', 'reset', 'hidden'].includes(String(el.type || '').toLowerCase()));
            const readValue = (el) => {
                if (!el) return '';
                const tag = String(el.tagName || '').toLowerCase();
                const role = String(el.getAttribute?.('role') || '').toLowerCase();
                if (tag === 'iframe') {
                    // editor iframe readback: same-origin body text only.
                    try {
                        return String(el.contentDocument?.body?.innerText || '').trim();
                    } catch (_) { return ''; }
                }
                if (tag === 'select') {
                    const opt = el.selectedOptions?.[0];
                    return [el.value, opt?.textContent].filter(Boolean).join(' ').trim();
                }
                if (tag === 'input' || tag === 'textarea') {
                    const type = String(el.type || '').toLowerCase();
                    if (type === 'checkbox' || role === 'checkbox') return el.checked ? 'true' : 'false';
                    if (type === 'radio' || role === 'radio') return el.checked ? (el.value || textOf(el)) : '';
                    const rawValue = String(el.value || '').trim();
                    const auto = String(el.getAttribute?.('aria-autocomplete') || el.getAttribute?.('autocomplete') || '').toLowerCase();
                    if (!rawValue && (role === 'combobox' || auto === 'list' || auto === 'both')) {
                        let cur = el.parentElement;
                        for (let i = 0; cur && i < 5; i++, cur = cur.parentElement) {
                            const text = [
                                cur.innerText,
                                cur.getAttribute?.('aria-label'),
                                cur.getAttribute?.('title')
                            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                            if (text && text.length <= 180) return text;
                        }
                    }
                    return rawValue;
                }
                if (el.isContentEditable) return String(el.innerText || el.textContent || '').trim();
                const nested = el.querySelector?.('input:not([type=hidden]),textarea,select,[contenteditable=true]');
                if (nested && isVisible(nested)) return readValue(nested);
                return textOf(el);
            };
            const escHtml = (s) => String(s).replace(/&/g, '&amp;')
                .replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const toParagraphHtml = (text) => String(text)
                .split(/\\n{2,}/)
                .map(part => `<p>${escHtml(part).replace(/\\n/g, '<br>')}</p>`)
                .join('') || '<p></p>';
            // Rich-text editors keep their own document model; a bare
            // textContent write desyncs it (Quill re-renders over it, TinyMCE
            // never sees it). Write through the editor API when one is found,
            // otherwise fall back to the real input chain / structured HTML.
            const setRichTextValue = (el, val) => {
                const text = String(val || '');
                const qlEditor = el.classList?.contains('ql-editor')
                    ? el
                    : (el.querySelector?.('.ql-editor')
                        || el.closest?.('.ql-container')?.querySelector?.('.ql-editor'));
                if (qlEditor) {
                    const container = qlEditor.closest('.ql-container') || qlEditor.parentElement;
                    const quill = (container && container.__quill)
                        || window.Quill?.find?.(container) || null;
                    if (quill && typeof quill.setText === 'function') {
                        quill.setText(text, 'user');
                        return 'quill_api';
                    }
                }
                const tiny = window.tinymce;
                if (tiny && (typeof tiny.get === 'function' || Array.isArray(tiny.editors))) {
                    const editors = Array.from(tiny.editors || []);
                    // classic mode binds the <id>_ifr editor iframe; the
                    // registry key is the original textarea id.
                    const ids = [el.id, el.id?.replace(/_ifr$/, '')].filter(Boolean);
                    const byId = typeof tiny.get === 'function'
                        ? ids.map(id => tiny.get(id)).find(Boolean) : null;
                    const ed = byId || editors.find(e => {
                        const body = e?.getBody?.();
                        if (body && (body === el || body.contains?.(el) || el.contains?.(body))) return true;
                        return Boolean(e?.getContainer?.()?.contains?.(el));
                    });
                    if (ed && typeof ed.setContent === 'function') {
                        ed.setContent(toParagraphHtml(text));
                        ed.fire?.('change');
                        return 'tinymce_api';
                    }
                }
                const ckHost = el.closest?.('.ck-editor__editable') || el;
                if (ckHost?.ckeditorInstance && typeof ckHost.ckeditorInstance.setData === 'function') {
                    ckHost.ckeditorInstance.setData(toParagraphHtml(text));
                    return 'ckeditor5_api';
                }
                if (String(el.tagName || '').toLowerCase() === 'iframe') {
                    // same-origin editor iframe without a reachable API: the
                    // main-document execCommand path cannot reach its body,
                    // so write structured paragraphs directly.
                    try {
                        const body = el.contentDocument?.body;
                        if (body) {
                            body.innerHTML = toParagraphHtml(text);
                            body.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text}));
                            body.dispatchEvent(new Event('change', {bubbles: true}));
                            return 'iframe_structured_paragraphs';
                        }
                    } catch (_) {}
                    return 'iframe_unreachable';
                }
                // Generic contenteditable (ProseMirror/Slate/Lexical listen to
                // beforeinput): select-all + insertText drives the real input chain.
                try {
                    el.focus?.({preventScroll: true});
                    const sel = window.getSelection?.();
                    if (sel && typeof document.execCommand === 'function') {
                        sel.selectAllChildren(el);
                        document.execCommand('delete', false, null);
                        if (document.execCommand('insertText', false, text)) {
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            return 'exec_insert_text';
                        }
                    }
                } catch (_) {}
                el.innerHTML = toParagraphHtml(text);
                el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return 'structured_paragraphs';
            };
            const setNativeValue = (el, val) => {
                const tag = String(el.tagName || '').toLowerCase();
                if (tag === 'iframe') {
                    // TinyMCE classic: the bound control is the editor
                    // iframe host itself.
                    return setRichTextValue(el, val);
                }
                if (el.isContentEditable) {
                    setRichTextValue(el, val);
                    return;
                }
                if (tag === 'select') {
                    const opts = Array.from(el.options || []);
                    const hit = opts.find(o => norm(o.textContent) === expected || norm(o.value) === expected) ||
                        opts.find(o => norm(o.textContent).includes(expected) || norm(o.value).includes(expected));
                    if (hit) el.value = hit.value;
                    else el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return;
                }
                const proto = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                try { el.focus?.({preventScroll: true}); } catch (_) {}
                try { el.dispatchEvent(new FocusEvent('focus', {bubbles: false})); } catch (_) {}
                if (setter) setter.call(el, val); else el.value = val;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.blur?.();
            };
            const clickEl = (el) => {
                el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                const r = el.getBoundingClientRect();
                const cx = r.left + r.width / 2;
                const cy = r.top + r.height / 2;
                const init = {
                    bubbles: true, cancelable: true, composed: true, view: window,
                    button: 0, buttons: 1, clientX: cx, clientY: cy, screenX: cx, screenY: cy
                };
                el.dispatchEvent(new MouseEvent('mousedown', init));
                el.dispatchEvent(new MouseEvent('mouseup', init));
                el.dispatchEvent(new MouseEvent('click', init));
            };
            const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
            const allVisible = (selector, root = document) =>
                deepQueryAll(selector, root).filter(isVisible);
            const parseDateTarget = (raw) => {
                const s = String(raw || '').trim();
                let m = s.match(/(20\\d{2})[-/.](\\d{1,2})[-/.](\\d{1,2})/);
                if (m) {
                    return {year: Number(m[1]), month: Number(m[2]), day: Number(m[3])};
                }
                m = s.match(/(?:next month|下个月|下月).*?(\\d{1,2})/i);
                if (m) {
                    const now = new Date();
                    return {year: now.getFullYear(), month: now.getMonth() + 2, day: Number(m[1])};
                }
                return null;
            };
            const clickDateValue = async (raw, opener) => {
                const target = parseDateTarget(raw);
                if (!target || !target.year || !target.month || !target.day) return null;
                while (target.month > 12) {
                    target.month -= 12;
                    target.year += 1;
                }
                clickEl(opener);
                await sleep(350);
                const monthNames = {
                    january: 1, february: 2, march: 3, april: 4, may: 5, june: 6,
                    july: 7, august: 8, september: 9, october: 10, november: 11, december: 12,
                    jan: 1, feb: 2, mar: 3, apr: 4, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9,
                    oct: 10, nov: 11, dec: 12
                };
                const panelRoots = () => allVisible(
                    '.el-picker-panel,.ant-picker-dropdown,.n-date-panel,.mx-datepicker-main,.datepicker,[role=dialog],.el-popper'
                );
                const visiblePanelMonth = () => {
                    const panels = panelRoots();
                    const root = panels[panels.length - 1] || document;
                    const txt = textOf(root);
                    let m = txt.match(/(20\\d{2})\\s*[-/.年 ]\\s*(1[0-2]|0?[1-9])/);
                    if (m) return {year: Number(m[1]), month: Number(m[2])};
                    m = txt.match(/(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\\s+(20\\d{2})/i);
                    if (m) return {year: Number(m[2]), month: monthNames[m[1].toLowerCase()]};
                    m = txt.match(/(20\\d{2})\\s+(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)/i);
                    if (m) return {year: Number(m[1]), month: monthNames[m[2].toLowerCase()]};
                    const currentVal = String(opener?.value || '');
                    m = currentVal.match(/^(20\\d{2})[-/.](\\d{1,2})[-/.]/);
                    if (m) return {year: Number(m[1]), month: Number(m[2])};
                    const now = new Date();
                    return {year: now.getFullYear(), month: now.getMonth() + 1};
                };
                const panelMonth = visiblePanelMonth();
                const monthDelta = (target.year - panelMonth.year) * 12 + (target.month - panelMonth.month);
                const nextSelectors = [
                    '.el-picker-panel__icon-btn.arrow-right',
                    '.ant-picker-header-next-btn',
                    'button[aria-label*="Next month"]',
                    'button[title*="Next month"]'
                ].join(',');
                const prevSelectors = [
                    '.el-picker-panel__icon-btn.arrow-left',
                    '.ant-picker-header-prev-btn',
                    'button[aria-label*="Previous month"]',
                    'button[title*="Previous month"]'
                ].join(',');
                const navSelector = monthDelta >= 0 ? nextSelectors : prevSelectors;
                for (let i = 0; i < Math.min(Math.abs(monthDelta), 24); i++) {
                    const btn = allVisible(navSelector).find(el =>
                        !el.disabled && el.getAttribute('aria-disabled') !== 'true'
                    );
                    if (!btn) break;
                    clickEl(btn);
                    await sleep(180);
                }
                const ymd = `${target.year}-${String(target.month).padStart(2, '0')}-${String(target.day).padStart(2, '0')}`;
                const dayText = String(target.day);
                for (let i = 0; i < 10; i++) {
                    const panels = panelRoots();
                    const root = panels[panels.length - 1] || document;
                    const cells = allVisible('td,button,[role=gridcell],.el-date-table-cell,.ant-picker-cell-inner', root);
                    const hits = [];
                    for (const el of cells) {
                        const cell = el.closest('td,button,[role=gridcell]') || el;
                        if (!isVisible(cell)) continue;
                        const disabled = cell.matches('.disabled,.is-disabled,.ant-picker-cell-disabled,[aria-disabled=true]') ||
                            cell.closest('.disabled,.is-disabled,.ant-picker-cell-disabled,[aria-disabled=true]');
                        if (disabled) continue;
                        const rawText = [
                            textOf(el), textOf(cell),
                            el.getAttribute('aria-label'), cell.getAttribute('aria-label'),
                            el.getAttribute('title'), cell.getAttribute('title')
                        ].filter(Boolean).join(' ');
                        const t = norm(rawText);
                        let score = 0;
                        if (t === norm(dayText)) score += 80;
                        if (t.includes(norm(ymd))) score += 160;
                        if (t.includes(String(target.year)) && t.includes(dayText)) score += 40;
                        if (cell.classList?.contains('prev-month') || cell.classList?.contains('next-month')) score -= 100;
                        if (cell.classList?.contains('available') || cell.classList?.contains('ant-picker-cell-in-view')) score += 20;
                        if (score > 0) hits.push({cell, score});
                    }
                    hits.sort((a, b) => b.score - a.score);
                    if (hits[0]) {
                        clickEl(hits[0].cell);
                        await sleep(250);
                        return ymd;
                    }
                    await sleep(120);
                }
                return null;
            };
            const directAttrScore = (el) => {
                const attrs = ['aria-label', 'placeholder', 'title', 'name', 'id'];
                let best = 0;
                for (const attr of attrs) {
                    const t = norm(el.getAttribute?.(attr));
                    if (!t) continue;
                    if (t === labelNorm) best = Math.max(best, 240);
                    else if (t.includes(labelNorm) || (t.length >= 5 && labelNorm.includes(t))) best = Math.max(best, 150);
                }
                return best;
            };
            const findLabelHits = () => {
                const nodes = deepQueryAll(
                    'label,[for],.el-form-item__label,.ant-form-item-label,.n-form-item-label,[class*=label],span,div'
                ).filter(isVisible);
                const hits = [];
                for (const el of nodes) {
                    const t = norm(textOf(el));
                    if (!t) continue;
                    const exact = t === labelNorm;
                    const contains = t.includes(labelNorm) || labelNorm.includes(t);
                    if (!exact && !contains) continue;
                    const r = el.getBoundingClientRect();
                    let score = exact ? 220 : 90;
                    if (el.matches('label,[for],.el-form-item__label,.ant-form-item-label,.n-form-item-label')) score += 60;
                    if (r.width * r.height > 120000) score -= 90;
                    hits.push({el, rect: r, score, text: textOf(el)});
                }
                hits.sort((a, b) => b.score - a.score);
                return hits;
            };
            const nearestContainer = (el) => {
                let cur = el;
                for (let i = 0; cur && i < 8; i++, cur = cur.parentElement) {
                    if (cur !== el && cur.querySelectorAll?.(controlSelector).length) {
                        if (
                            cur.matches?.('.el-form-item,.ant-form-item,.n-form-item,.form-group,[class*=form-item],[class*=field]') ||
                            cur.tagName?.toLowerCase() === 'label'
                        ) return cur;
                    }
                }
                cur = el.parentElement;
                for (let i = 0; cur && i < 5; i++, cur = cur.parentElement) {
                    if (cur.querySelectorAll?.(controlSelector).length === 1) return cur;
                }
                return el.parentElement || el;
            };
            const bind = () => {
                const controls = allControls();
                const direct = controls
                    .map(el => ({el, score: directAttrScore(el), reason: 'direct_attr'}))
                    .filter(x => x.score > 0)
                    .sort((a, b) => b.score - a.score)[0];
                if (direct) return direct;

                const labels = findLabelHits();
                for (const hit of labels) {
                    const forId = hit.el.getAttribute?.('for');
                    if (forId) {
                        const escaped = window.CSS?.escape ? CSS.escape(forId) : forId;
                        const target = (hit.el.getRootNode?.() || document).getElementById?.(forId) ||
                            document.getElementById(forId) ||
                            deepQueryAll('#' + escaped)[0] || null;
                        if (target && isVisible(target)) return {el: target, score: 260, reason: 'label_for'};
                        // for= targets the hidden textarea in TinyMCE classic;
                        // its visible stand-in is the <forId>_ifr editor iframe.
                        const standIn = (hit.el.getRootNode?.() || document).getElementById?.(forId + '_ifr') ||
                            document.getElementById(forId + '_ifr') ||
                            deepQueryAll('#' + escaped + '_ifr')[0] || null;
                        if (standIn && isVisible(standIn)) return {el: standIn, score: 260, reason: 'label_for_ifr'};
                    }
                    const container = nearestContainer(hit.el);
                    const localControls = Array.from(container.querySelectorAll?.(controlSelector) || [])
                        .filter(isVisible)
                        .filter(el => el !== hit.el);
                    if (localControls.length === 1) {
                        return {el: localControls[0], score: hit.score + 40, reason: 'local_unique'};
                    }
                }
                if (!labels.length) return null;

                const candidates = [];
                for (const hit of labels.slice(0, 5)) {
                    const lr = hit.rect;
                    const ly = lr.top + lr.height / 2;
                    for (const el of controls) {
                        if (el === hit.el || hit.el.contains?.(el)) continue;
                        const cr = el.getBoundingClientRect();
                        const cy = cr.top + cr.height / 2;
                        const vertical = Math.abs(cy - ly);
                        const rightGap = cr.left - lr.right;
                        const belowGap = cr.top - lr.bottom;
                        let score = hit.score;
                        if (vertical < Math.max(32, Math.max(lr.height, cr.height) * 0.9) && rightGap > -20) {
                            score += 180 - vertical - Math.max(0, rightGap) / 20;
                        } else if (belowGap >= -8 && belowGap < 80 && Math.abs(cr.left - lr.left) < 220) {
                            score += 110 - belowGap;
                        } else {
                            score -= vertical;
                        }
                        score += directAttrScore(el) / 3;
                        if (cr.width < 8 || cr.height < 8) score -= 100;
                        candidates.push({el, score, reason: 'geometry', labelText: hit.text});
                    }
                }
                candidates.sort((a, b) => b.score - a.score);
                return candidates[0] || null;
            };
            const tryChoiceByLabel = () => {
                if (!expected) return null;
                const labels = findLabelHits();
                for (const hit of labels.slice(0, 5)) {
                    let cur = hit.el;
                    for (let depth = 0; cur && depth < 8; depth++, cur = cur.parentElement) {
                        const text = norm(textOf(cur));
                        if (!text.includes(expected)) continue;
                        const options = allVisible(
                            'label,.el-radio,.el-checkbox,.custom-control-label,[role=radio],[role=checkbox],button',
                            cur
                        ).filter(el => {
                            const t = norm(textOf(el));
                            if (!t || t.length > Math.max(80, expected.length + 40)) return false;
                            return t === expected || t.includes(expected);
                        }).map(el => {
                            const r = el.getBoundingClientRect();
                            let score = 100;
                            const t = norm(textOf(el));
                            if (t === expected) score += 180;
                            if (el.matches('label,.el-radio,.el-checkbox,.custom-control-label,[role=radio],[role=checkbox]')) score += 120;
                            if (r.width * r.height > 80000) score -= 160;
                            return {el, score};
                        }).sort((a, b) => b.score - a.score);
                        if (!options[0]) continue;
                        const option = options[0].el;
                        const target = option.closest?.('label,.el-radio,.el-checkbox,.custom-control-label,[role=radio],[role=checkbox]') || option;
                        clickEl(target);
                        return {
                            ok: true,
                            mode: 'choice_text',
                            binding: 'label_scope_choice',
                            observed: textOf(target)
                        };
                    }
                }
                return null;
            };

            const choiceResult = tryChoiceByLabel();
            if (choiceResult) return choiceResult;

            const binding = bind();
            if (!binding?.el) return {ok: false, reason: 'label_or_control_not_found', label};
            let control = binding.el;
            control.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
            const before = readValue(control);
            const beforeRole = String(control.getAttribute?.('role') || '').toLowerCase();
            const beforeAuto = String(control.getAttribute?.('aria-autocomplete') || control.getAttribute?.('autocomplete') || '').toLowerCase();
            const beforeComboLike = beforeRole === 'combobox' || beforeAuto === 'list' || beforeAuto === 'both';
            if (expected && (norm(before) === expected || (beforeComboLike && norm(before).includes(expected)))) {
                return {ok: true, mode: 'already_set', binding: binding.reason, observed: before};
            }
            const tag = String(control.tagName || '').toLowerCase();
            const role = String(control.getAttribute?.('role') || '').toLowerCase();
            const type = String(control.type || '').toLowerCase();

            if (tag === 'select') {
                setNativeValue(control, value);
            } else if (type === 'checkbox' || role === 'checkbox') {
                const want = !/^(false|off|no|0)$/i.test(String(value || 'true'));
                if (Boolean(control.checked) !== want) clickEl(control);
            } else if (type === 'radio' || role === 'radio') {
                clickEl(control);
            } else {
                const input = control.matches?.('input,textarea,[contenteditable=true]')
                    ? control
                    : control.querySelector?.('input:not([type=hidden]),textarea,[contenteditable=true]');
                const inputRole = String(input?.getAttribute?.('role') || '').toLowerCase();
                const inputAuto = String(input?.getAttribute?.('aria-autocomplete') || input?.getAttribute?.('autocomplete') || '').toLowerCase();
                const popup = String(input?.getAttribute?.('aria-haspopup') || control.getAttribute?.('aria-haspopup') || '').toLowerCase();
                const readonlyCombo = input && (input.readOnly || inputRole === 'combobox' || role === 'combobox' || popup === 'listbox' || popup === 'true');
                const dateTarget = parseDateTarget(value);
                if (readonlyCombo) {
                    if (!openIfNeeded) {
                        return {ok: false, reason: 'readonly_combo_not_opened', binding: binding.reason, observed: before};
                    }
                    const marker = '__vspider_form_bound_control__';
                    deepQueryAll(`[data-${marker}]`).forEach(el => el.removeAttribute(`data-${marker}`));
                    let opener = control;
                    for (const sel of ['.el-select__wrapper', '.el-select', '.ant-select-selector', '.ant-select', '.n-base-selection', '[role=combobox]']) {
                        const closest = input.closest?.(sel) || control.closest?.(sel) || control.querySelector?.(sel);
                        if (closest && isVisible(closest)) { opener = closest; break; }
                    }
                    const hint = norm([label, input?.placeholder, input?.getAttribute?.('aria-label'), textOf(control)].filter(Boolean).join(' '));
                    if (dateTarget && /(date|time|pick a date|日期|时间)/i.test(hint)) {
                        const picked = await clickDateValue(value, input || opener);
                        const afterDate = readValue(control);
                        const okDate = Boolean(picked) && norm(afterDate).includes(norm(picked));
                        return {
                            ok: okDate,
                            mode: okDate ? 'date_picker' : 'date_picker_failed',
                            binding: binding.reason,
                            observed: afterDate,
                            expected: picked || value
                        };
                    }
                    if (input && (inputRole === 'combobox' || inputAuto === 'list' || inputAuto === 'both')) {
                        setNativeValue(input, value);
                        await sleep(350);
                    }
                    opener.setAttribute(`data-${marker}`, '1');
                    const r = opener.getBoundingClientRect();
                    return {
                        ok: true,
                        mode: input && !input.readOnly && (inputRole === 'combobox' || inputAuto === 'list' || inputAuto === 'both')
                            ? 'autocomplete_opened'
                            : 'opened',
                        binding: binding.reason,
                        observed: before,
                        click_selector: `[data-${marker}="1"]`,
                        click_point: {x: r.left + r.width / 2, y: r.top + r.height / 2}
                    };
                }
                if (input) setNativeValue(input, value);
                else setNativeValue(control, value);
            }
            await new Promise(resolve => setTimeout(resolve, 60));
            const after = readValue(control);
            const ok = expected ? norm(after) === expected : true;
            return {
                ok,
                mode: ok ? 'bound_control' : 'value_mismatch',
                binding: binding.reason,
                observed: after,
                expected: value,
                before
            };
        }""",
        {"label": label, "value": value, "openIfNeeded": open_if_needed},
    )
    if not isinstance(result, dict):
        return {"ok": False, "reason": "invalid_result", "raw": result}
    return result


_FORM_SET_NOT_FOUND_REASONS = {"label_or_control_not_found", "invalid_result"}


async def _form_set_with_frames(
    page: "Page", label: str, value: str, *, open_if_needed: bool = True
) -> tuple[dict[str, Any], Any]:
    """Bind in the main document first, then fall back to every child iframe.

    Returns ``(result, scope)`` where scope is the Page or Frame the field was
    found in, so follow-up clicks/readbacks run in the right document.
    """
    result = await _form_set_bound_control_v2(
        page, label, value, open_if_needed=open_if_needed
    )
    if result.get("ok") or result.get("reason") not in _FORM_SET_NOT_FOUND_REASONS:
        return result, page
    main_frame = getattr(page, "main_frame", None)
    for frame in list(getattr(page, "frames", None) or []):
        if frame is main_frame:
            continue
        try:
            is_detached = getattr(frame, "is_detached", None)
            if callable(is_detached) and is_detached():
                continue
            frame_result = await _form_set_bound_control_v2(
                frame, label, value, open_if_needed=open_if_needed
            )
        except Exception as frame_err:
            logger.debug(
                "[FORM_SET] frame probe failed (%s): %s",
                getattr(frame, "url", "?"),
                frame_err,
            )
            continue
        if frame_result.get("ok") or frame_result.get("reason") not in _FORM_SET_NOT_FOUND_REASONS:
            frame_result.setdefault("frame_url", getattr(frame, "url", "") or "")
            return frame_result, frame
    return result, page


@ActionRegistry.register("form_set")
class FormSetHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        page = ctx.page
        label, value = _parse_form_set_payload(ctx.action.type_value)
        logger.info("[FORM_SET] label=%r value=%r", label, value)

        result, scope = await _form_set_with_frames(page, label, value)
        if result.get("mode") in ("opened", "autocomplete_opened") and value:
            click_selector = result.get("click_selector")
            if click_selector:
                try:
                    opener = scope.locator(click_selector).first
                    await opener.scroll_into_view_if_needed(timeout=2000)
                    await opener.click(timeout=3000, force=True)
                except Exception as open_err:
                    point = result.get("click_point") or {}
                    if scope is page:
                        try:
                            await page.mouse.click(float(point.get("x")), float(point.get("y")))
                        except Exception:
                            logger.debug("[FORM_SET] bound opener click failed: %s", open_err)
                    else:
                        # click_point is frame-local; page.mouse uses viewport
                        # coords, so skip the coordinate fallback inside iframes.
                        logger.debug("[FORM_SET] bound opener click failed in frame: %s", open_err)
            await asyncio.sleep(0.8)
            option_clicked = await _click_visible_text_option(scope, value)
            if not option_clicked and scope is not page:
                # some widget libraries teleport the dropdown to the top document
                option_clicked = await _click_visible_text_option(page, value)
            if not option_clicked:
                raise ActionExecutionError(
                    f"form_set located {label!r}, but could not select option {value!r}."
                )
            await asyncio.sleep(0.3)
            readback = await _form_set_bound_control_v2(
                scope, label, value, open_if_needed=False
            )
            if result.get("frame_url") and "frame_url" not in readback:
                readback["frame_url"] = result["frame_url"]
            result = readback

        if not result.get("ok"):
            raise ActionExecutionError(
                f"form_set failed for {label!r}: {result.get('reason') or result.get('mode')}; "
                f"expected={value!r}, observed={result.get('observed')!r}, "
                f"binding={result.get('binding')!r}"
            )
        if value and result.get("mode") in ("opened", "autocomplete_opened"):
            raise ActionExecutionError(
                f"form_set opened {label!r}, but the selected value was not reflected on the bound control."
            )
        logger.info(
            "[FORM_SET_V2] label=%r ok mode=%s binding=%s observed=%r frame=%s",
            label,
            result.get("mode"),
            result.get("binding"),
            result.get("observed"),
            result.get("frame_url", "") or "main",
        )
        print(f"[FORM_SET] {label} = {value}")
        ctx.browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "form_set",
                "type_value": f"{label}={value}",
                "method": "bound_control_v2",
                "binding": result.get("binding", ""),
                "verified": True,
                "observed": result.get("observed", ""),
                "frame_url": result.get("frame_url", ""),
            })
        )
        return None

        def _xpath_literal(text: str) -> str:
            if "'" not in text:
                return f"'{text}'"
            parts = text.split("'")
            return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"

        async def _try_open_choice_with_ax() -> bool:
            if not value:
                return False
            choice_label = re.search(
                r"(zone|type|resource|date|time|下拉|选择|复选|单选|开关|日期|时间)",
                label,
                re.I,
            )
            if not choice_label:
                return False
            locators = []
            try:
                locators.append(page.get_by_role("combobox", name=re.compile(re.escape(label), re.I)).first)
            except Exception:
                pass
            try:
                locators.append(page.get_by_label(label, exact=True).first)
            except Exception:
                pass
            try:
                label_lit = _xpath_literal(label)
                locators.append(
                    page.locator(
                        "xpath=("
                        f"//*[normalize-space()={label_lit} or contains(normalize-space(), {label_lit})]"
                        "/following::*["
                        "@role='combobox' or self::select or contains(@class,'select') or "
                        "contains(@class,'picker') or contains(@class,'checkbox') or contains(@class,'radio')"
                        "][1])"
                    ).first
                )
            except Exception:
                pass
            for idx, loc in enumerate(locators):
                try:
                    if await loc.count() <= 0:
                        continue
                    await loc.scroll_into_view_if_needed(timeout=2000)
                    await loc.click(timeout=3000, force=True)
                    await asyncio.sleep(0.8)
                    if await _click_visible_text_option(page, value):
                        logger.info("[FORM_SET] AX fast-path selected %r via locator #%s", value, idx)
                        return True
                except Exception as err:
                    logger.debug("[FORM_SET] AX fast-path locator #%s failed: %s", idx, err)
            return False

        async def _verify_field_state(method: str = "") -> dict[str, Any]:
            return await page.evaluate(
                """({label, value, method}) => {
                    const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const expected = norm(value);
                    const labelNorm = norm(label);
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                            s.display !== 'none' && s.visibility !== 'hidden' &&
                            Number(s.opacity || '1') > 0;
                    };
                    const textOf = (el) => [
                        el.innerText, el.textContent, el.getAttribute('aria-label'),
                        el.getAttribute('placeholder'), el.getAttribute('title'),
                        el.getAttribute('value'), el.value, el.name, el.id
                    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                    const nodes = Array.from(document.querySelectorAll(
                        'label,.el-form-item__label,[class*=form-item__label],input,textarea,select,button,[role],span,div'
                    )).filter(isVisible);
                    const labelHits = [];
                    for (const el of nodes) {
                        const t = norm(textOf(el));
                        if (!t || (t !== labelNorm && !t.includes(labelNorm))) continue;
                        const r = el.getBoundingClientRect();
                        let score = t === labelNorm ? 80 : 30;
                        if (el.matches('label,.el-form-item__label,[class*=form-item__label]')) score += 60;
                        if (el.closest('form,.el-form,[class*=form]')) score += 30;
                        if (r.left > 180) score += 20;
                        labelHits.push({el, score});
                    }
                    labelHits.sort((a, b) => b.score - a.score);
                    const labelEl = labelHits[0]?.el;
                    if (!labelEl) return {ok: false, reason: 'label_not_found', observed: '', method};
                    let formItem = labelEl;
                    for (let i = 0; formItem && i < 8; i++) {
                        if (
                            formItem !== labelEl &&
                            (
                                formItem.classList?.contains('el-form-item') ||
                                formItem.classList?.contains('ant-form-item') ||
                                formItem.classList?.contains('n-form-item') ||
                                formItem.tagName?.toLowerCase() === 'form'
                            )
                        ) break;
                        formItem = formItem.parentElement;
                    }
                    if (!formItem) formItem = labelEl.parentElement || labelEl;

                    const controls = Array.from(formItem.querySelectorAll('input,textarea,select,[contenteditable=true]')).filter(isVisible);
                    const controlValues = controls.map(el => {
                        if (el.tagName?.toLowerCase() === 'select') {
                            const opt = el.selectedOptions?.[0];
                            return [el.value, opt?.textContent].filter(Boolean).join(' ');
                        }
                        return [el.value, el.textContent, el.getAttribute('aria-label')].filter(Boolean).join(' ');
                    });
                    const observed = [textOf(formItem), ...controlValues].join(' ').replace(/\\s+/g, ' ').trim();
                    const observedNorm = norm(observed);

                    const switchRoot = formItem.querySelector('.el-switch,[role=switch]');
                    if (switchRoot) {
                        const checked = switchRoot.classList.contains('is-checked') ||
                            switchRoot.getAttribute('aria-checked') === 'true' ||
                            Boolean(formItem.querySelector('input:checked'));
                        const shouldOn = !/^(false|off|no|0)$/i.test(String(value || ''));
                        return {ok: checked === shouldOn, observed: checked ? 'checked' : 'unchecked', method, mode: 'switch'};
                    }

                    const choiceNodes = Array.from(formItem.querySelectorAll('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]'))
                        .filter(isVisible);
                    const matchingChoice = choiceNodes.find(el => norm(textOf(el)).includes(expected));
                    if (matchingChoice) {
                        const target = matchingChoice.closest?.('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]') || matchingChoice;
                        const checked = target.matches?.('.is-checked,[aria-checked=true]') ||
                            Boolean(target.querySelector?.('.is-checked,[aria-checked=true],input:checked')) ||
                            Boolean(matchingChoice.querySelector?.('.is-checked,[aria-checked=true],input:checked'));
                        if (checked) return {ok: true, observed, method, mode: 'choice_checked'};
                    }

                    if (expected && observedNorm.includes(expected)) {
                        return {ok: true, observed, method, mode: 'value_visible'};
                    }
                    return {ok: false, reason: 'value_not_reflected', observed, expected: value, method};
                }""",
                {"label": label, "value": value, "method": method},
            )

        if await _try_open_choice_with_ax():
            verified = await _verify_field_state("ax_fast_path")
            if not verified.get("ok"):
                raise ActionExecutionError(
                    f"form_set 字段 {label!r} 已执行 AX fast-path，但回读校验失败："
                    f"expected={value!r}, observed={verified.get('observed')!r}, reason={verified.get('reason')}"
                )
            logger.info("[FORM_VERIFY] label=%r ok via %s observed=%r", label, verified.get("method"), verified.get("observed"))
            ctx.browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "form_set",
                    "type_value": f"{label}={value}",
                    "method": "ax_fast_path",
                    "verified": True,
                    "observed": verified.get("observed", ""),
                })
            )
            return None

        result = await page.evaluate(
            """async ({label, value}) => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const labelNorm = norm(label);
                const valueNorm = norm(value);
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el.innerText, el.textContent, el.getAttribute('aria-label'),
                    el.getAttribute('placeholder'), el.getAttribute('title'),
                    el.getAttribute('value'), el.name, el.id
                ].filter(Boolean).join(' ').trim();
                const setNativeValue = (el, val) => {
                    const proto = el instanceof HTMLTextAreaElement
                        ? HTMLTextAreaElement.prototype
                        : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(el, val); else el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.blur?.();
                };
                const clickEl = (el) => {
                    el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                    // 关键修复：Element Plus / Ant Design / Vue 等 SPA 库的 select / dropdown
                    // 通常监听 `mousedown` 而非 `click`（防 input blur 后再触发）。
                    // 单纯 el.click() 只会派发 click 事件，组件不会响应 → 下拉永远不展开。
                    // 这里派发完整 mousedown + mouseup + click 三连，模拟真实鼠标点击。
                    const r = el.getBoundingClientRect();
                    const cx = r.left + Math.max(1, r.width / 2);
                    const cy = r.top + Math.max(1, r.height / 2);
                    const evtInit = {
                        bubbles: true, cancelable: true, composed: true,
                        view: window, button: 0, buttons: 1,
                        clientX: cx, clientY: cy, screenX: cx, screenY: cy,
                    };
                    try {
                        el.dispatchEvent(new MouseEvent('mousedown', evtInit));
                        el.dispatchEvent(new MouseEvent('mouseup', evtInit));
                        el.dispatchEvent(new MouseEvent('click', evtInit));
                    } catch (e) {
                        // fallback：浏览器极端情况下 MouseEvent 构造失败
                        if (typeof el.click === 'function') el.click();
                    }
                    if (typeof el.focus === 'function') {
                        try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); }
                    }
                };
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                const allVisible = (selector, root = document) => Array.from(root.querySelectorAll(selector)).filter(isVisible);
                const clickDateValue = async (dateValue, opener) => {
                    const m = String(dateValue || '').match(/^(\\d{4})-(\\d{2})-(\\d{2})/);
                    if (!m) return false;
                    const targetYear = Number(m[1]);
                    const targetMonth = Number(m[2]);
                    const targetDay = Number(m[3]);
                    if (!targetYear || !targetMonth || !targetDay) return false;
                    clickEl(opener);
                    await sleep(300);

                    const visiblePanelMonth = () => {
                        const panels = allVisible('.el-picker-panel,.ant-picker-dropdown,.n-date-panel,.mx-datepicker-main,.datepicker,[role=dialog],.el-popper');
                        const root = panels[panels.length - 1] || document;
                        const txt = textOf(root);
                        const monthNames = {
                            january: 1, february: 2, march: 3, april: 4, may: 5, june: 6,
                            july: 7, august: 8, september: 9, october: 10, november: 11, december: 12,
                            jan: 1, feb: 2, mar: 3, apr: 4, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9,
                            oct: 10, nov: 11, dec: 12
                        };
                        let m = txt.match(/(20\\d{2})\\s*[年\\-/\\. ]\\s*(1[0-2]|0?[1-9])\\s*(?:月)?/);
                        if (m) return {year: Number(m[1]), month: Number(m[2])};
                        m = txt.match(/(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\\s+(20\\d{2})/i);
                        if (m) return {year: Number(m[2]), month: monthNames[m[1].toLowerCase()]};
                        m = txt.match(/(20\\d{2})\\s+(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)/i);
                        if (m) return {year: Number(m[1]), month: monthNames[m[2].toLowerCase()]};
                        const currentVal = String(opener?.value || '');
                        m = currentVal.match(/^(\\d{4})-(\\d{2})-/);
                        if (m) return {year: Number(m[1]), month: Number(m[2])};
                        const now = new Date();
                        return {year: now.getFullYear(), month: now.getMonth() + 1};
                    };
                    const panelMonth = visiblePanelMonth();
                    const monthDelta = (targetYear - panelMonth.year) * 12 + (targetMonth - panelMonth.month);
                    const nextSelectors = [
                        '.el-picker-panel__icon-btn.arrow-right',
                        '.ant-picker-header-next-btn',
                        'button[aria-label*="Next month"]',
                        'button[title*="Next month"]'
                    ].join(',');
                    const prevSelectors = [
                        '.el-picker-panel__icon-btn.arrow-left',
                        '.ant-picker-header-prev-btn',
                        'button[aria-label*="Previous month"]',
                        'button[title*="Previous month"]'
                    ].join(',');
                    const navSelector = monthDelta >= 0 ? nextSelectors : prevSelectors;
                    for (let i = 0; i < Math.min(Math.abs(monthDelta), 24); i++) {
                        const btn = allVisible(navSelector).find(el => !el.disabled && el.getAttribute('aria-disabled') !== 'true');
                        if (!btn) break;
                        clickEl(btn);
                        await sleep(150);
                    }

                    const ymd = `${targetYear}-${String(targetMonth).padStart(2, '0')}-${String(targetDay).padStart(2, '0')}`;
                    const dayText = String(targetDay);
                    for (let i = 0; i < 10; i++) {
                        const panels = allVisible('.el-picker-panel,.ant-picker-dropdown,.n-date-panel,.mx-datepicker-main,.datepicker,[role=dialog],.el-popper');
                        const root = panels[panels.length - 1] || document;
                        const cells = allVisible('td,button,[role=gridcell],.el-date-table-cell,.ant-picker-cell-inner', root);
                        const hits = [];
                        for (const el of cells) {
                            const cell = el.closest('td,button,[role=gridcell]') || el;
                            if (!isVisible(cell)) continue;
                            const disabled = cell.matches('.disabled,.is-disabled,.ant-picker-cell-disabled,[aria-disabled=true]') ||
                                cell.closest('.disabled,.is-disabled,.ant-picker-cell-disabled,[aria-disabled=true]');
                            if (disabled) continue;
                            const raw = [
                                textOf(el), textOf(cell),
                                el.getAttribute('aria-label'), cell.getAttribute('aria-label'),
                                el.getAttribute('title'), cell.getAttribute('title')
                            ].filter(Boolean).join(' ');
                            const t = norm(raw);
                            let score = 0;
                            if (t === norm(dayText)) score += 80;
                            if (t.includes(norm(ymd))) score += 120;
                            if (t.includes(String(targetYear)) && t.includes(String(targetDay))) score += 40;
                            if (cell.classList?.contains('prev-month') || cell.classList?.contains('next-month')) score -= 90;
                            if (cell.classList?.contains('available') || cell.classList?.contains('ant-picker-cell-in-view')) score += 20;
                            if (score > 0) hits.push({cell, score});
                        }
                        hits.sort((a, b) => b.score - a.score);
                        if (hits[0]) {
                            clickEl(hits[0].cell);
                            await sleep(250);
                            return true;
                        }
                        await sleep(120);
                    }
                    return false;
                };
                const nodes = Array.from(document.querySelectorAll(
                    'label,.el-form-item__label,[class*=form-item__label],input,textarea,select,button,[role],span,div'
                )).filter(isVisible);
                const matches = [];
                for (const el of nodes) {
                    const t = norm(textOf(el));
                    if (!t) continue;
                    const exact = t === labelNorm;
                    const contains = t.includes(labelNorm);
                    if (!exact && !contains) continue;
                    const r = el.getBoundingClientRect();
                    let score = exact ? 80 : 30;
                    if (el.matches('label,.el-form-item__label,[class*=form-item__label]')) score += 50;
                    if (el.closest('form,.el-form,[class*=form]')) score += 30;
                    if (r.left > 180) score += 30;
                    if (r.width * r.height < 80000) score += 10;
                    score -= Math.abs(r.top - window.innerHeight / 2) / 80;
                    matches.push({el, score, text: textOf(el), left: r.left});
                }
                matches.sort((a, b) => b.score - a.score);
                const labelEl = matches[0]?.el;
                if (!labelEl) return {ok: false, reason: 'label_not_found', label};
                labelEl.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});

                const findContainer = (el) => {
                    let cur = el;
                    for (let i = 0; cur && i < 8; i++) {
                        if (
                            cur !== el &&
                            (
                                cur.classList?.contains('el-form-item') ||
                                cur.classList?.contains('ant-form-item') ||
                                cur.classList?.contains('n-form-item') ||
                                cur.tagName?.toLowerCase() === 'form'
                            )
                        ) {
                            return cur;
                        }
                        cur = cur.parentElement;
                    }
                    cur = el.parentElement;
                    for (let i = 0; cur && i < 4; i++) {
                        if (cur.querySelector?.('input,textarea,select,[role=combobox],[role=checkbox],[role=radio],button,.el-select,.ant-select,.n-select')) {
                            return cur;
                        }
                        cur = cur.parentElement;
                    }
                    return el.parentElement;
                };
                const formItem = findContainer(labelEl);
                if (!formItem) return {ok: false, reason: 'container_not_found', label};

                const textInputs = Array.from(formItem.querySelectorAll('input,textarea,[contenteditable=true]'))
                    .filter(el => isVisible(el) && !['hidden','checkbox','radio','button','submit'].includes((el.type || '').toLowerCase()));
                const explicitSelectRoot = formItem.querySelector('.el-select,.ant-select,.n-select,[data-select]');
                if (/(date|time|日期|时间)/i.test(label) && /^\\d{4}-\\d{2}-\\d{2}/.test(value) && textInputs.length) {
                    const picked = await clickDateValue(value, textInputs[0]);
                    if (picked) return {ok: true, mode: 'date_picker', label, value};
                    setNativeValue(textInputs[0], value);
                    return {ok: true, mode: 'date_input', label, value};
                }
                const readonlyCombo = textInputs.find(el => {
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    const popup = (el.getAttribute('aria-haspopup') || '').toLowerCase();
                    return el.readOnly || role === 'combobox' || popup === 'listbox' || popup === 'true';
                });
                if ((explicitSelectRoot || readonlyCombo) && valueNorm && !formItem.querySelector('textarea')) {
                    // Idempotent open：检测下拉浮层是否已经可见，避免重试时再点一次反而 toggle 关闭
                    const _popperOpen = (
                        document.querySelector('.el-select-dropdown:not([style*="display: none"])') ||
                        document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden)') ||
                        document.querySelector('.n-base-select-menu') ||
                        document.querySelector('[role=listbox]:not([aria-hidden=true])')
                    );
                    if (_popperOpen) {
                        return {ok: true, mode: 'opened', label, value, already_open: true};
                    }
                    // ── 关键修复：找到真正的可点击 wrapper（Element Plus 事件挂在 wrapper 而非 input） ──
                    // readonlyCombo 找到的常是内层 <input class="el-select__inner">，但 Vue
                    // 用 @mousedown.stop 委托到 .el-select__wrapper，内层派事件会被 stopPropagation 截胡。
                    // 向上遍历找到真正监听事件的 wrapper 元素，标记 data 属性供 Python 端 Playwright 点击。
                    let wrapper = explicitSelectRoot || readonlyCombo;
                    const _WRAPPER_SELECTORS = [
                        '.el-select__wrapper', '.el-select',
                        '.ant-select-selector', '.ant-select',
                        '.n-base-selection', '[role=combobox]',
                    ];
                    for (const sel of _WRAPPER_SELECTORS) {
                        const child = wrapper?.querySelector?.(sel);
                        if (child && isVisible(child)) { wrapper = child; break; }
                    }
                    let cur = wrapper;
                    for (let i = 0; cur && i < 8; i++) {
                        for (const sel of _WRAPPER_SELECTORS) {
                            if (cur.matches?.(sel)) { wrapper = cur; break; }
                        }
                        if (wrapper !== (explicitSelectRoot || readonlyCombo)) break;
                        cur = cur.parentElement;
                    }
                    // 给 wrapper 加唯一 data 标记，Python 端用 Playwright click 派完整事件链
                    const _marker = '__vspider_form_target__';
                    document.querySelectorAll(`[data-${_marker}]`).forEach(el =>
                        el.removeAttribute(`data-${_marker}`)
                    );
                    wrapper.setAttribute(`data-${_marker}`, '1');
                    const rr = wrapper.getBoundingClientRect();
                    return {
                        ok: true, mode: 'opened', label, value,
                        click_selector: `[data-${_marker}="1"]`,
                        click_point: {x: rr.left + rr.width * 0.5, y: rr.top + rr.height * 0.5},
                    };
                }
                if (textInputs.length) {
                    const input = textInputs[0];
                    setNativeValue(input, value);
                    return {ok: true, mode: 'fill', label, value};
                }

                const switches = Array.from(formItem.querySelectorAll(
                    '.el-switch,[role=switch],input[type=checkbox],.el-checkbox,label'
                )).filter(isVisible);
                if (switches.length && /^(true|on|yes|1|开启|打开|选中|勾选)$/i.test(value || 'true')) {
                    const checked = formItem.querySelector('.is-checked,[aria-checked=true],input:checked');
                    if (!checked) clickEl(switches[0]);
                    return {ok: true, mode: 'toggle', label, value};
                }

                if (valueNorm) {
                    const optionNodes = Array.from(formItem.querySelectorAll('label,.el-radio,.el-checkbox,button,span,div'))
                        .filter(el => isVisible(el) && norm(textOf(el)).includes(valueNorm));
                    if (optionNodes.length) {
                        const choiceHit = optionNodes[0];
                        const choiceTarget = choiceHit.closest?.('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]') || choiceHit;
                        clickEl(choiceTarget);
                        await sleep(150);
                        return {ok: true, mode: 'local_option', label, value};
                    }
                }

                const clickable = Array.from(formItem.querySelectorAll(
                    '.el-select,.el-input,.el-input__wrapper,[role=combobox],[role=button],button,input'
                )).filter(isVisible);
                if (clickable.length) {
                    clickEl(clickable[0]);
                    return {ok: true, mode: 'opened', label, value};
                }
                clickEl(labelEl);
                return {ok: true, mode: 'label_click', label, value};
            }""",
            {"label": label, "value": value},
        )
        await asyncio.sleep(0.6)
        if not result.get("ok"):
            raise ActionExecutionError(
                f"form_set 未找到字段 {label!r}: {result.get('reason')}"
            )
        if result.get("mode") in ("opened", "label_click") and value:
            # JS 端只负责"找控件 + 标记 data 属性"，真正的点击交给 Playwright（CDP 派完整事件链，
            # 穿透 Vue/React 的 mousedown.stop 委托）。这是修复 Element Plus 下拉打不开的关键。
            _click_sel = result.get("click_selector")
            if _click_sel and not result.get("already_open"):
                try:
                    _wrapper_loc = page.locator(_click_sel).first
                    await _wrapper_loc.scroll_into_view_if_needed(timeout=2000)
                    await _wrapper_loc.click(timeout=3000, force=True)
                    logger.info(f"[FORM_SET] Playwright 点击 wrapper {_click_sel} 展开下拉")
                except Exception as _open_err:
                    _pt = result.get("click_point") or {}
                    try:
                        _x = float(_pt.get("x"))
                        _y = float(_pt.get("y"))
                        await page.mouse.move(_x, _y)
                        await page.mouse.down()
                        await page.mouse.up()
                        logger.info("[FORM_SET] page.mouse click fallback at %.1f, %.1f", _x, _y)
                    except Exception as _mouse_err:
                        logger.warning("[FORM_SET] page.mouse click fallback failed: %s", _mouse_err)
                    logger.warning(f"[FORM_SET] Playwright wrapper click 失败: {_open_err}")
            # popper 动画 + teleport 渲染需要充分时间
            # Element Plus transition 250ms + 内部 mount + 选项 list 渲染 ≈ 600-900ms
            await asyncio.sleep(1.0)
            _option_clicked = await _click_visible_text_option(page, value)
            if not _option_clicked:
                _pt = result.get("click_point") or {}
                try:
                    _x = float(_pt.get("x"))
                    _y = float(_pt.get("y"))
                    await page.mouse.click(_x, _y)
                    await asyncio.sleep(0.8)
                    _option_clicked = await _click_visible_text_option(page, value)
                except Exception as _retry_open_err:
                    logger.debug("[FORM_SET] point reopen retry failed: %s", _retry_open_err)
            if not _option_clicked:
                raise ActionExecutionError(
                    f"form_set 已定位字段 {label!r}，但未能点击选项/值 {value!r}。"
                    "可能原因：(a) 下拉浮层渲染慢于 2.5s 等待窗口；"
                    "(b) 选项文字与 value 不完全匹配（含空格/隐藏字符）；"
                    "(c) 该字段不是下拉而是输入框 — 改用 type 动作直接 fill。"
                )
        verified = await _verify_field_state(str(result.get("mode") or "dom_path"))
        if not verified.get("ok"):
            raise ActionExecutionError(
                f"form_set 字段 {label!r} 已执行但回读校验失败："
                f"expected={value!r}, observed={verified.get('observed')!r}, "
                f"mode={result.get('mode')!r}, reason={verified.get('reason')}"
            )
        logger.info(
            "[FORM_VERIFY] label=%r ok via %s observed=%r",
            label,
            verified.get("method"),
            verified.get("observed"),
        )
        logger.info("[FORM_SET] completed label=%r mode=%s", label, result.get("mode"))
        print(f"[FORM_SET] {label} = {value}")
        ctx.browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "form_set",
                "type_value": f"{label}={value}",
                "verified": True,
                "observed": verified.get("observed", ""),
            })
        )
        return None



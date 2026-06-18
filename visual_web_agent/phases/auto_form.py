"""Auto form-fill logic extracted from ``main.py``.

Contains form assignment parsing, submit detection, field validation,
auto-fill orchestration, and related helpers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..browser_env import BrowserEnv

try:
    from ..form_engine import (
        prepare_form_batch_fields as engine_prepare_form_batch_fields,
    )
except ImportError:
    engine_prepare_form_batch_fields = None  # type: ignore[assignment]

try:
    from .goal_parser import _goal_is_form_fill
except ImportError:
    _goal_is_form_fill = lambda goal: False  # type: ignore[assignment]

logger = logging.getLogger("vspider")

_FORM_SUBMIT_RE = re.compile(
    r"^\s*(create|submit|save|ok|confirm|提交|保存|确定|创建|确认)\s*$",
    re.IGNORECASE,
)


def _broadcast_log_safe(message: str, level: str = "info") -> None:
    try:
        from broadcast import broadcast_log
        broadcast_log(message, level=level)
    except Exception:
        logger.log(
            logging.WARNING if level == "warn" else logging.INFO, message
        )

def _looks_like_submit_text(value: object) -> bool:
    return bool(_FORM_SUBMIT_RE.search(str(value or "").strip()))


async def _locate_visible_form_submit_text(
    browser: "BrowserEnv", goal: str, assignments: dict[str, str]
) -> dict:
    """Locate the current form's visible submit control text without clicking it."""
    result = {"found": False, "reason": "not_checked", "text": ""}
    if not assignments:
        result["reason"] = "no_assignments"
        return result

    page = await browser._ensure_active_page(reason="locate visible form submit text")
    if not page:
        result["reason"] = "no_active_page"
        return result

    try:
        info = await page.evaluate(
            """({scopeTitle, labels}) => {
                const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
                const norm = (s) => clean(s).toLowerCase();
                const isVisible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el?.innerText, el?.textContent, el?.getAttribute?.('aria-label'),
                    el?.getAttribute?.('title'), el?.getAttribute?.('value'),
                    el?.value, el?.name, el?.id
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const allVisible = (selector, root = document) =>
                    Array.from(root.querySelectorAll(selector)).filter(isVisible);

                const findScope = () => {
                    const roots = allVisible('form,.el-form,.ant-form,.n-form,section,article,main,[role=main],div');
                    const scored = [];
                    for (const root of roots) {
                        const r = root.getBoundingClientRect();
                        if (r.left < 160 || r.width < 220 || r.height < 60) continue;
                        const txt = norm(textOf(root));
                        let score = 0;
                        if (scopeTitle && txt.includes(norm(scopeTitle))) score += 120;
                        for (const label of labels || []) {
                            if (txt.includes(norm(label))) score += 20;
                        }
                        if (root.matches('form,.el-form,.ant-form,.n-form')) score += 70;
                        if (score > 0) scored.push({root, score, area: r.width * r.height});
                    }
                    scored.sort((a, b) => b.score - a.score || a.area - b.area);
                    return scored[0]?.root || document.body;
                };

                const submitMatcher = /(?:^|\\s)(?:create|submit|save|send|apply|register)(?:\\s|$)|提交|保存|确定|发送|注册/i;
                const scope = findScope();
                const localSubmit = allVisible('button,.el-button,[role=button],input[type=submit],input[type=button]', scope)
                    .find(el => submitMatcher.test(textOf(el)));
                const globalSubmit = allVisible('button,.el-button,[role=button],input[type=submit],input[type=button]')
                    .find(el => submitMatcher.test(textOf(el)) && el.getBoundingClientRect().left > 160);
                const hit = localSubmit || globalSubmit;
                if (!hit) return {found: false, reason: 'submit_not_found'};
                return {
                    found: true,
                    reason: localSubmit ? 'scope_submit' : 'global_submit',
                    text: textOf(hit).trim()
                };
            }""",
            {
                "scopeTitle": _parse_goal_scope_title(goal),
                "labels": list(assignments.keys()),
            },
        )
        if isinstance(info, dict):
            result.update(info)
    except Exception as exc:
        result["reason"] = f"locate_failed: {exc}"
        logger.debug("[FORM SUBMIT REWRITE] failed to locate visible submit text: %s", exc)
    return result


async def _inspect_form_submit_target(
    browser: "BrowserEnv", action: str, decision: dict
) -> dict:
    """Return physical evidence that the current action targets a submit control."""
    result = {
        "is_submit": False,
        "reason": "not_checked",
        "action": action,
        "target_id": int(decision.get("target_id") or 0),
        "type_value": str(decision.get("type_value") or ""),
    }
    if action not in ("click", "click_text"):
        result["reason"] = "unsupported_action"
        return result

    page = await browser._ensure_active_page(reason="inspect form submit target")
    if not page:
        result["reason"] = "no_active_page"
        return result

    try:
        if action == "click_text":
            text = str(decision.get("type_value") or "").strip()
            result["text"] = text
            if not _looks_like_submit_text(text):
                result["reason"] = "click_text_not_submit"
                return result
            info = await page.evaluate(
                """(wanted) => {
                    const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                            s.display !== 'none' && s.visibility !== 'hidden' &&
                            Number(s.opacity || '1') > 0;
                    };
                    const textOf = (el) => [
                        el.innerText, el.textContent,
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.getAttribute('value')
                    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                    const controls = Array.from(document.querySelectorAll(
                        'button,a,[role=button],[role=link],input[type=submit],input[type=button]'
                    )).filter(isVisible);
                    const hit = controls.find(el => norm(textOf(el)) === norm(wanted));
                    if (!hit) return null;
                    return {
                        tag: (hit.tagName || '').toLowerCase(),
                        role: (hit.getAttribute('role') || '').toLowerCase(),
                        type: (hit.getAttribute('type') || '').toLowerCase(),
                        text: textOf(hit),
                        disabled: Boolean(hit.disabled) || hit.getAttribute('aria-disabled') === 'true'
                    };
                }""",
                text,
            )
            if info:
                result.update(info)
                result["is_submit"] = not bool(info.get("disabled"))
                result["reason"] = "click_text_physical_submit"
            else:
                result["reason"] = "click_text_no_physical_submit"
            return result

        target_id = int(decision.get("target_id") or 0)
        if target_id <= 0:
            result["reason"] = "missing_target_id"
            return result

        info = await page.evaluate(
            """(targetId) => {
                const el = document.querySelector(`[data-som-id="${targetId}"]`);
                if (!el) return null;
                const textOf = (node) => [
                    node.innerText, node.textContent,
                    node.getAttribute('aria-label'),
                    node.getAttribute('placeholder'),
                    node.getAttribute('title'),
                    node.getAttribute('value'),
                    node.name, node.id
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const tag = (el.tagName || '').toLowerCase();
                const role = (el.getAttribute('role') || '').toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                const closest = el.closest('button,a,[role=button],[role=link],input[type=submit],input[type=button]');
                const control = closest || el;
                return {
                    tag,
                    role,
                    type,
                    text: textOf(el),
                    controlTag: (control.tagName || '').toLowerCase(),
                    controlRole: (control.getAttribute('role') || '').toLowerCase(),
                    controlType: (control.getAttribute('type') || '').toLowerCase(),
                    controlText: textOf(control),
                    disabled: Boolean(control.disabled) || control.getAttribute('aria-disabled') === 'true'
                };
            }""",
            target_id,
        )
        if not info:
            meta = getattr(browser, "element_mapping", {}).get(f"@e{target_id}", {}) or {}
            role = str(meta.get("role") or "").lower()
            name = str(meta.get("name") or "")
            result.update({"role": role, "text": name, "reason": "mapping_fallback"})
            result["is_submit"] = role in {"button", "link"} and _looks_like_submit_text(name)
            return result

        result.update(info)
        role_values = {
            str(info.get("role") or "").lower(),
            str(info.get("controlRole") or "").lower(),
        }
        tag_values = {
            str(info.get("tag") or "").lower(),
            str(info.get("controlTag") or "").lower(),
        }
        type_values = {
            str(info.get("type") or "").lower(),
            str(info.get("controlType") or "").lower(),
        }
        text_values = [
            str(info.get("controlText") or ""),
            str(info.get("text") or ""),
        ]
        physical_control = (
            bool({"button", "link"} & role_values)
            or bool({"button", "a"} & tag_values)
            or "submit" in type_values
        )
        semantic_submit = any(_looks_like_submit_text(v) for v in text_values)
        result["is_submit"] = (
            not bool(info.get("disabled"))
            and physical_control
            and (semantic_submit or "submit" in type_values)
        )
        result["reason"] = "physical_submit" if result["is_submit"] else "physical_not_submit"
    except Exception as exc:
        result["reason"] = f"inspect_failed: {exc}"
        logger.debug("[FORM SUBMIT GUARD] target inspection failed: %s", exc)
    return result


async def _validate_form_assignments_on_page(
    browser: "BrowserEnv", goal: str, assignments: dict[str, str]
) -> dict:
    """Read back user-requested form fields from the current DOM before submit."""
    if not assignments:
        return {"ok": False, "reason": "no_assignments", "missing": []}
    page = await browser._ensure_active_page(reason="validate form assignments")
    if not page:
        return {"ok": False, "reason": "no_active_page", "missing": list(assignments)}

    try:
        return await page.evaluate(
            """({scopeTitle, fields}) => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const cleanLabel = (s) => String(s || '').replace(/\\s+/g, ' ').trim().replace(/^[*\\s:：-]+|[*\\s:：-]+$/g, '');
                const labelTextOf = (el) => cleanLabel(
                    el?.innerText || el?.textContent || el?.getAttribute?.('aria-label') || ''
                );
                const isVisible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el?.innerText, el?.textContent, el?.getAttribute?.('aria-label'),
                    el?.getAttribute?.('placeholder'), el?.getAttribute?.('title'),
                    el?.getAttribute?.('value'), el?.name, el?.id
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const allVisible = (selector, root = document) =>
                    Array.from(root.querySelectorAll(selector)).filter(isVisible);
                const labels = Object.keys(fields || {});
                const allControls = allVisible('input,textarea,select,[contenteditable=true]', document)
                    .filter(el => !['hidden','button','submit','reset'].includes((el.type || '').toLowerCase()));

                const findScope = () => {
                    const roots = allVisible('form,.el-form,.ant-form,.n-form,section,article,main,[role=main],div');
                    const scored = [];
                    for (const root of roots) {
                        const r = root.getBoundingClientRect();
                        if (r.left < 180 || r.width < 250 || r.height < 80) continue;
                        const txt = norm(textOf(root));
                        let score = 0;
                        if (scopeTitle && txt.includes(norm(scopeTitle))) score += 100;
                        for (const label of labels) if (txt.includes(norm(label))) score += 20;
                        if (root.matches('form,.el-form,.ant-form,.n-form')) score += 60;
                        if (score > 0) scored.push({root, score, area: r.width * r.height});
                    }
                    scored.sort((a, b) => b.score - a.score || a.area - b.area);
                    return scored[0]?.root || document.body;
                };

                const scope = findScope();
                const findFieldBinding = (label, used = new Set()) => {
                    const ln = norm(cleanLabel(label));
                    const nodes = allVisible('label,.el-form-item__label,.ant-form-item-label,.n-form-item-label,span,div', scope);
                    const matches = [];
                    for (const el of nodes) {
                        const raw = labelTextOf(el);
                        const t = norm(raw);
                        if (!t || t !== ln) continue;
                        const r = el.getBoundingClientRect();
                        let score = 200;
                        if (el.tagName === 'LABEL') score += 300;
                        if (el.matches('label,.el-form-item__label,.ant-form-item-label,.n-form-item-label')) score += 200;
                        if (raw.length <= cleanLabel(label).length + 8) score += 40;
                        matches.push({el, score});
                    }
                    matches.sort((a, b) => b.score - a.score);
                    for (const hit of matches.slice(0, 5)) {
                        let cur = hit.el;
                        for (let i = 0; cur && i < 6; i++) {
                            const localControls = allControls.filter(ctrl => !used.has(ctrl) && scope.contains(ctrl) && cur.contains(ctrl));
                            if (localControls.length === 1) {
                                return {labelEl: hit.el, control: localControls[0]};
                            }
                            if (localControls.length > 1) {
                                const lr = hit.el.getBoundingClientRect();
                                const scored = localControls.map(ctrl => {
                                    const cr = ctrl.getBoundingClientRect();
                                    const verticalGap = cr.top - lr.bottom;
                                    const horizontalDelta = Math.abs((cr.left + cr.width / 2) - (lr.left + lr.width / 2));
                                    let score = 220 - horizontalDelta - Math.abs(verticalGap) * 2;
                                    if (verticalGap >= -12 && verticalGap <= 120) score += 180;
                                    if (Math.abs(cr.left - lr.left) <= 40) score += 60;
                                    return {ctrl, score};
                                }).sort((a, b) => b.score - a.score);
                                if (scored[0]) return {labelEl: hit.el, control: scored[0].ctrl};
                            }
                            cur = cur.parentElement;
                        }
                    }
                    const fallbackLabel = matches[0]?.el;
                    if (!fallbackLabel) return null;
                    const lr = fallbackLabel.getBoundingClientRect();
                    const scored = allControls
                        .filter(ctrl => !used.has(ctrl) && scope.contains(ctrl))
                        .map(ctrl => {
                            const cr = ctrl.getBoundingClientRect();
                            const verticalGap = cr.top - lr.bottom;
                            const horizontalDelta = Math.abs((cr.left + cr.width / 2) - (lr.left + lr.width / 2));
                            let score = 200 - horizontalDelta - Math.abs(verticalGap) * 2;
                            if (verticalGap >= -12 && verticalGap <= 120) score += 180;
                            if (Math.abs(cr.left - lr.left) <= 40) score += 60;
                            return {ctrl, score};
                        })
                        .sort((a, b) => b.score - a.score);
                    if (!scored[0]) return null;
                    return {labelEl: fallbackLabel, control: scored[0].ctrl};
                };

                const verifyField = (label, value, used) => {
                    const binding = findFieldBinding(label, used);
                    if (!binding || !binding.control) return {label, ok: false, reason: 'field_not_found'};
                    used.add(binding.control);
                    const expected = norm(value);
                    const control = binding.control;
                    const tag = control.tagName?.toLowerCase();
                    const observed = tag === 'select'
                        ? [control.value, control.selectedOptions?.[0]?.textContent].filter(Boolean).join(' ')
                        : [control.value, control.textContent, control.getAttribute('aria-label')].filter(Boolean).join(' ');
                    const observedNorm = norm(observed || '');

                    const item = control.closest('label,.el-form-item,.ant-form-item,.n-form-item,div,section,article,form') || control.parentElement;
                    const switchRoot = item ? allVisible('.el-switch,[role=switch]', item)[0] : null;
                    if (switchRoot) {
                        const checked = switchRoot.classList.contains('is-checked') ||
                            switchRoot.getAttribute('aria-checked') === 'true' ||
                            Boolean(item.querySelector('input:checked'));
                        const shouldOn = !/^(false|off|no|0|关闭|关)$/i.test(String(value || ''));
                        return {label, ok: checked === shouldOn, mode: 'switch', observed: checked ? 'checked' : 'unchecked'};
                    }

                    const choiceNodes = item ? allVisible('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]', item) : [];
                    const choiceHit = choiceNodes.find(el => norm(textOf(el)).includes(expected));
                    if (choiceHit) {
                        const choiceTarget = choiceHit.closest?.('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]') || choiceHit;
                        const checked = choiceTarget.matches?.('.is-checked,[aria-checked=true]') ||
                            Boolean(choiceTarget.querySelector?.('.is-checked,[aria-checked=true],input:checked')) ||
                            Boolean(choiceHit.querySelector?.('.is-checked,[aria-checked=true],input:checked'));
                        if (checked) return {label, ok: true, mode: 'choice_checked', observed};
                    }

                    if (expected && observedNorm === expected) {
                        return {label, ok: true, mode: 'value_exact', observed};
                    }
                    return {label, ok: false, reason: 'value_not_reflected', expected: value, observed};
                };

                const verifyUsed = new Set();
                const checks = Object.entries(fields || {}).map(([label, value]) => verifyField(label, value, verifyUsed));
                return {
                    ok: checks.length > 0 && checks.every(r => r.ok),
                    checks,
                    missing: checks.filter(r => !r.ok).map(r => r.label),
                    scopeText: textOf(scope).slice(0, 160)
                };
            }""",
            {
                "scopeTitle": _parse_goal_scope_title(goal),
                "fields": assignments,
            },
        )
    except Exception as exc:
        logger.debug("[FORM SUBMIT GUARD] validation failed: %s", exc)
        return {"ok": False, "reason": f"validation_failed: {exc}", "missing": list(assignments)}


def _assignment_is_non_text_control(label: str) -> bool:
    text = str(label or "").lower()
    return any(
        marker in text
        for marker in (
            "zone", "type", "resources", "delivery", "date", "time",
            "下拉", "选择", "复选", "单选", "开关", "日期", "时间",
        )
    )


def _parse_goal_scope_title(goal: str) -> str:
    text = str(goal or "")
    patterns = (
        r"[\"“”'‘’]([^\"“”'‘’]{1,80})[\"“”'‘’]\s*(?:标题|区域|表单|表格)?\s*(?:下方|下面|内部|内|中)",
        r"(?:标题|区域|表单|表格)\s*[\"“”'‘’]([^\"“”'‘’]{1,80})[\"“”'‘’]",
    )
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return ""


def _month_offset_day(offset: int, day: int) -> str:
    today = date.today()
    month_index = today.month - 1 + int(offset or 0)
    year = today.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{min(max(1, int(day or 1)), last_day):02d}"


def _parse_relative_month_day(text: str) -> tuple[int, int] | None:
    """Parse relative month expressions such as 下个月/下下个月/下下下个月 + day."""
    if not text:
        return None
    m = re.search(r"(下{1,6})个?月\s*的?\s*(3[01]|[12]?\d)\s*[号日]?", text)
    if m:
        return len(m.group(1)), int(m.group(2))
    m = re.search(r"下\s*([1-9]\d?)\s*个?月\s*的?\s*(3[01]|[12]?\d)\s*[号日]?", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _parse_semantic_macro(goal: str) -> dict | None:
    """Unified entry point: parse the goal via the ``semantic_macros`` registry.

    Returns the highest-priority matching macro's step dict, or ``None`` if no
    registered macro applies. Callers that need a specific action should
    inspect ``step["action"]``.
    """
    try:
        from .. import semantic_macros as _sm
    except ImportError:
        try:
            from . import semantic_macros as _sm  # type: ignore[no-redef]
        except ImportError:
            import semantic_macros as _sm  # type: ignore[no-redef]
    return _sm.parse_goal(goal)


async def _semantic_goal_currently_satisfied(
    browser: "BrowserEnv",
    goal: str,
) -> dict:
    """Verify simple semantic component goals from current visible state."""
    macro = _parse_semantic_macro(goal)
    if not macro:
        return {"ok": False, "reason": "no_semantic_macro"}

    action = str(macro.get("action") or "")
    if action == "cascader_pick":
        expected = str(
            (macro.get("validate") or {}).get("input_should_contain")
            or " / ".join(macro.get("path") or [])
        ).strip()
    elif action == "date_pick":
        expected = str(macro.get("target_date") or "").strip()
    else:
        return {"ok": False, "reason": f"unsupported_macro:{action}"}
    if not expected:
        return {"ok": False, "reason": "empty_expected_value"}

    try:
        page = await browser._ensure_active_page(reason="semantic goal verification")
        if not page:
            return {"ok": False, "reason": "no_active_page"}
        result = await page.evaluate(
            """expected => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const isVisible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el?.value,
                    el?.innerText,
                    el?.textContent,
                    el?.getAttribute?.('value'),
                    el?.getAttribute?.('aria-label'),
                    el?.getAttribute?.('placeholder'),
                    el?.getAttribute?.('title')
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const probes = [];
                for (const el of Array.from(document.querySelectorAll(
                    'input,textarea,[role=combobox],[contenteditable=true],.el-cascader,.el-input,.el-form-item'
                ))) {
                    if (!isVisible(el)) continue;
                    const text = textOf(el);
                    if (text) probes.push(text);
                }
                const expectedNorm = norm(expected);
                for (const text of probes) {
                    if (norm(text).includes(expectedNorm)) {
                        return {ok: true, observed: text, expected, probes: probes.slice(0, 12)};
                    }
                }
                return {ok: false, expected, probes: probes.slice(0, 12)};
            }""",
            expected,
        )
        return {
            "ok": bool(result and result.get("ok")),
            "action": action,
            "expected": expected,
            "observed": (result or {}).get("observed", ""),
            "probes": (result or {}).get("probes", []),
        }
    except Exception as exc:
        return {"ok": False, "reason": f"probe_failed: {exc}", "expected": expected}


def _semantic_macro_should_own_goal(goal: str, macro: dict | None) -> bool:
    """Return true when a semantic component macro should bypass form handling."""
    if not macro:
        return False
    action = str(macro.get("action") or "")
    if action == "cascader_pick":
        return True
    if action != "date_pick":
        return False
    text = str(goal or "")
    has_standalone_date_language = bool(
        re.search(
            r"date[-\s]?picker|datepicker|pick\s+a\s+day|enter\s+date|日期输入框|日历面板|日历",
            text,
            re.I,
        )
    )
    has_form_language = bool(
        re.search(
            r"表单|注册表|填写|填入|填报|Activity\s+name|Activity\s+zone|"
            r"First\s+Name|Last\s+Name|Mobile|Submit|Create",
            text,
            re.I,
        )
    )
    return has_standalone_date_language and not has_form_language


def _semanticize_rpa_trail(trail: list[dict], goal: str) -> list[dict]:
    """Replace volatile physical replays with semantic intent macros when possible."""
    macro = _parse_semantic_macro(goal)
    if macro:
        return [macro]
    return trail


def _prepare_form_batch_fields(goal: str) -> dict[str, str]:
    return engine_prepare_form_batch_fields(goal)
    fields = _parse_form_assignments(goal)
    text = str(goal or "")
    if re.search(r"Activity\s*time|活动时间|时间区域", text, re.I):
        relative_month_day = _parse_relative_month_day(text)
        offset, day = relative_month_day if relative_month_day else (1, 0)
        if not day:
            chunks = re.split(r"[\r\n]+|(?=\s*\d+\s*[.、)]\s*)", text)
            time_chunk = next((c for c in chunks if re.search(r"Activity\s*time|活动时间|时间区域", c, re.I)), "")
            nums = re.findall(r"\b([1-2]?\d|3[01])\b", time_chunk)
            day = int(nums[-1]) if nums else 0
        if day:
            if 1 <= day <= 31:
                fields["Activity time"] = _month_offset_day(offset, day)
    return fields


async def _auto_form_has_prestart_gate(browser: "BrowserEnv") -> bool:
    """Delay deterministic form fill while a visible Start gate still blocks the real workflow."""
    page = await browser._ensure_active_page(reason="inspect auto form prestart gate")
    if not page:
        return False
    try:
        result = await page.evaluate(
            """() => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el.innerText, el.textContent,
                    el.getAttribute('aria-label'),
                    el.getAttribute('title'),
                    el.getAttribute('value')
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const controls = Array.from(document.querySelectorAll(
                    'button,[role=button],input[type=button],input[type=submit],a'
                )).filter(isVisible);
                const fields = Array.from(document.querySelectorAll('input,textarea,select'))
                    .filter(isVisible)
                    .filter(el => !['hidden','button','submit','reset'].includes((el.type || '').toLowerCase()));
                const hasStart = controls.some(el => /\bstart\b/i.test(textOf(el)));
                const hasSubmit = controls.some(el => /(?:\bsubmit\b|\bcreate\b|提交|保存|确定)/i.test(textOf(el)));
                return {
                    blocked: hasStart && hasSubmit && fields.length >= 2,
                    fieldCount: fields.length,
                };
            }"""
        )
    except Exception as exc:
        logger.debug("[AUTO FORM] failed to inspect prestart gate: %s", exc)
        return False
    if result and result.get("blocked"):
        logger.info(
            "[AUTO FORM] prestart gate detected; defer deterministic fill until Start is cleared. fields=%s",
            result.get("fieldCount"),
        )
        return True
    return False


async def _try_auto_form_fill_bound_controls(
    page,
    *,
    scope_title: str,
    fields: dict[str, str],
    require_submit: bool,
    preset_labels: list[str] | None = None,
) -> dict:
    """Fill generic forms by binding each visible label to one concrete control.

    This is intentionally stricter than the older container-based path: a field is
    complete only when the bound control itself reads back the expected value.
    """
    return await page.evaluate(
        """async ({scopeTitle, fields, requireSubmit, presetLabels}) => {
            const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
            const cleanLabel = (s) => clean(s).replace(/^[*\\s:：-]+|[*\\s:：-]+$/g, '');
            const norm = (s) => cleanLabel(s).toLowerCase();
            const isVisible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    s.display !== 'none' && s.visibility !== 'hidden' &&
                    Number(s.opacity || '1') > 0;
            };
            const textOf = (el) => [
                el?.innerText, el?.textContent, el?.getAttribute?.('aria-label'),
                el?.getAttribute?.('placeholder'), el?.getAttribute?.('title'),
                el?.getAttribute?.('value'), el?.value, el?.name, el?.id
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
            const labelTextOf = (el) => cleanLabel(
                el?.innerText || el?.textContent || el?.getAttribute?.('aria-label') || ''
            );
            // querySelectorAll that also descends into open shadow roots, so
            // web-component forms stay reachable (mirrors actions.deepQueryAll).
            const deepQueryAll = (selector, root = document) => {
                const out = Array.from(root.querySelectorAll(selector));
                for (const host of root.querySelectorAll('*')) {
                    if (host.shadowRoot) out.push(...deepQueryAll(selector, host.shadowRoot));
                }
                return out;
            };
            const allVisible = (selector, root = document) =>
                deepQueryAll(selector, root).filter(isVisible);
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));
            const labels = Object.keys(fields || {});

            const findScope = () => {
                const roots = allVisible('form,.el-form,.ant-form,.n-form,section,article,main,[role=main],div');
                const scored = [];
                for (const root of roots) {
                    const r = root.getBoundingClientRect();
                    if (r.left < 160 || r.width < 220 || r.height < 60) continue;
                    const txt = norm(textOf(root));
                    let score = 0;
                    if (scopeTitle && txt.includes(norm(scopeTitle))) score += 120;
                    for (const label of labels) if (txt.includes(norm(label))) score += 20;
                    if (root.matches('form,.el-form,.ant-form,.n-form')) score += 70;
                    if (score > 0) scored.push({root, score, area: r.width * r.height});
                }
                scored.sort((a, b) => b.score - a.score || a.area - b.area);
                return scored[0]?.root || document.body;
            };

            const scope = findScope();
            // TinyMCE classic hides its textarea and renders into an editor
            // iframe (<id>_ifr / .tox-edit-area__iframe): bind the visible
            // iframe host so labelled rich-text fields stay reachable.
            // srcdoc iframes are inline editor surfaces too (FORM-RICHTEXT-6).
            const controls = allVisible('input,textarea,select,[contenteditable=true],[contenteditable="true"],iframe[id$="_ifr"],iframe.tox-edit-area__iframe,iframe[srcdoc]', scope)
                .filter(el => !['hidden','button','submit','reset'].includes((el.type || '').toLowerCase()));
            const labelNodes = allVisible(
                'label,.el-form-item__label,[class*=form-item__label],.ant-form-item-label,.n-form-item-label,span,div',
                scope
            );

            const findBinding = (label, used) => {
                const labelNorm = norm(label);
                const available = controls.filter(ctrl => !used.has(ctrl));
                const direct = available.find(ctrl => [
                    ctrl.getAttribute('aria-label'),
                    ctrl.getAttribute('placeholder'),
                    ctrl.getAttribute('title'),
                    ctrl.getAttribute('name'),
                    ctrl.id
                ].filter(Boolean).some(t => norm(t) === labelNorm));
                if (direct) return {labelEl: direct, control: direct, method: 'direct_control'};

                const hits = [];
                for (const el of labelNodes) {
                    const raw = labelTextOf(el);
                    if (!raw || norm(raw) !== labelNorm) continue;
                    const r = el.getBoundingClientRect();
                    let score = 200;
                    if (el.tagName === 'LABEL') score += 300;
                    if (el.matches('label,.el-form-item__label,[class*=form-item__label],.ant-form-item-label,.n-form-item-label')) score += 180;
                    if (raw.length <= cleanLabel(label).length + 8) score += 40;
                    if (r.width <= 240 && r.height <= 40) score += 20;
                    hits.push({el, score});
                }
                hits.sort((a, b) => b.score - a.score);

                for (const hit of hits.slice(0, 6)) {
                    const forId = hit.el.getAttribute?.('for');
                    if (forId) {
                        // for= targets the hidden textarea in TinyMCE classic;
                        // its visible stand-in is the <forId>_ifr iframe.
                        const explicit = available.find(ctrl => ctrl.id === forId)
                            || available.find(ctrl => ctrl.id === forId + '_ifr');
                        if (explicit) return {labelEl: hit.el, control: explicit, method: 'for_attr'};
                    }
                    let cur = hit.el;
                    for (let depth = 0; cur && depth < 7; depth++) {
                        const localControls = available.filter(ctrl => cur.contains(ctrl));
                        if (localControls.length === 1) {
                            return {labelEl: hit.el, control: localControls[0], method: 'local_unique'};
                        }
                        if (localControls.length > 1) {
                            const lr = hit.el.getBoundingClientRect();
                            const scored = localControls.map(ctrl => {
                                const cr = ctrl.getBoundingClientRect();
                                const labelMidY = lr.top + lr.height / 2;
                                const controlMidY = cr.top + cr.height / 2;
                                const sameRowGap = Math.abs(controlMidY - labelMidY);
                                const rightGap = cr.left - lr.right;
                                const verticalGap = cr.top - lr.bottom;
                                const horizontalDelta = Math.abs((cr.left + cr.width / 2) - (lr.left + lr.width / 2));
                                let score = 220 - horizontalDelta - Math.abs(verticalGap) * 2;
                                if (sameRowGap <= Math.max(32, Math.max(lr.height, cr.height)) && rightGap >= -24) {
                                    score += 260 - sameRowGap - Math.max(0, rightGap) / 20;
                                }
                                if (verticalGap >= -12 && verticalGap <= 130) score += 180;
                                if (Math.abs(cr.left - lr.left) <= 45) score += 70;
                                return {ctrl, score};
                            }).sort((a, b) => b.score - a.score);
                            if (scored[0]) return {labelEl: hit.el, control: scored[0].ctrl, method: 'local_geometry'};
                        }
                        cur = cur.parentElement;
                    }
                }
                const fallbackLabel = hits[0]?.el;
                if (!fallbackLabel) return null;
                const lr = fallbackLabel.getBoundingClientRect();
                const scored = available.map(ctrl => {
                    const cr = ctrl.getBoundingClientRect();
                    const labelMidY = lr.top + lr.height / 2;
                    const controlMidY = cr.top + cr.height / 2;
                    const sameRowGap = Math.abs(controlMidY - labelMidY);
                    const rightGap = cr.left - lr.right;
                    const verticalGap = cr.top - lr.bottom;
                    const horizontalDelta = Math.abs((cr.left + cr.width / 2) - (lr.left + lr.width / 2));
                    let score = 200 - horizontalDelta - Math.abs(verticalGap) * 2;
                    if (sameRowGap <= Math.max(32, Math.max(lr.height, cr.height)) && rightGap >= -24) {
                        score += 260 - sameRowGap - Math.max(0, rightGap) / 20;
                    }
                    if (verticalGap >= -12 && verticalGap <= 130) score += 180;
                    if (Math.abs(cr.left - lr.left) <= 45) score += 70;
                    return {ctrl, score};
                }).sort((a, b) => b.score - a.score);
                return scored[0] ? {labelEl: fallbackLabel, control: scored[0].ctrl, method: 'page_geometry'} : null;
            };

            const escHtml = (s) => String(s).replace(/&/g, '&amp;')
                .replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const toParagraphHtml = (text) => String(text)
                .split(/\\n{2,}/)
                .map(part => `<p>${escHtml(part).replace(/\\n/g, '<br>')}</p>`)
                .join('') || '<p></p>';
            // Mirrors actions.setRichTextValue: write through the editor API
            // when one is found, else the real input chain, else escaped <p>s.
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
                    // same-origin editor iframe without a reachable API:
                    // the main-document execCommand path cannot reach its
                    // body, so write structured paragraphs directly.
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
            const setNativeValue = (el, value) => {
                if (String(el.tagName || '').toLowerCase() === 'iframe') {
                    return setRichTextValue(el, String(value || ''));
                }
                if (el.isContentEditable) {
                    const method = setRichTextValue(el, String(value || ''));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                    return method;
                } else {
                    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(el, String(value || '')); else el.value = String(value || '');
                }
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
                return 'native_value';
            };
            const clickEl = (el) => {
                el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                if (typeof el.click === 'function') el.click();
                else el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, composed: true, view: window}));
            };
            const valueOf = (control) => {
                const tag = control.tagName?.toLowerCase();
                if (tag === 'select') {
                    return [control.value, control.selectedOptions?.[0]?.textContent]
                        .filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                }
                if (tag === 'iframe') {
                    // editor iframe readback: same-origin body text only.
                    try {
                        return clean(control.contentDocument?.body?.innerText || '');
                    } catch (_) { return ''; }
                }
                if (control.isContentEditable) {
                    return clean(control.innerText || control.textContent || '');
                }
                // aria-label is the field's label, not its value: joining it
                // into the readback made every labelled control fail verify.
                const direct = [control.value, control.textContent]
                    .filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                if (direct) return direct;
                return clean(control.getAttribute('aria-label') || '');
            };
            const verify = (binding, label, value) => {
                const control = binding.control;
                const expected = norm(value);
                const type = (control.type || '').toLowerCase();
                if (type === 'checkbox' || type === 'radio') {
                    const shouldCheck = !/^(false|off|no|0|关闭|关)$/i.test(String(value || 'true'));
                    return {label, ok: Boolean(control.checked) === shouldCheck, observed: control.checked ? 'checked' : 'unchecked', method: binding.method};
                }
                const observed = valueOf(control);
                return {label, ok: expected && norm(observed) === expected, expected: value, observed, method: binding.method};
            };

            const presetSet = new Set((presetLabels || []).map(s => norm(s)));
            let presetHits = 0;
            const used = new Set();
            const results = [];
            const bindings = [];
            for (const [label, value] of Object.entries(fields || {})) {
                const binding = findBinding(label, used);
                if (!binding || !binding.control) {
                    results.push({label, ok: false, reason: 'field_not_found'});
                    continue;
                }
                used.add(binding.control);
                if (presetSet.has(norm(label))) {
                    // already written at frame level (cross-origin editor
                    // iframe); the in-macro write+verify cannot reach that
                    // body, and the Python side verified the frame readback.
                    presetHits += 1;
                    results.push({label, ok: true, mode: 'preset_frame_write', method: binding.method});
                    continue;
                }
                bindings.push([label, value, binding]);
                const control = binding.control;
                const tag = control.tagName?.toLowerCase();
                const type = (control.type || '').toLowerCase();
                const expected = String(value || '');

                if (tag === 'select') {
                    const options = Array.from(control.options || []);
                    const hit = options.find(opt => norm(opt.textContent) === norm(expected)) ||
                        options.find(opt => norm(opt.value) === norm(expected)) ||
                        options.find(opt => norm(opt.textContent).includes(norm(expected)));
                    if (hit) control.value = hit.value;
                    else control.value = expected;
                    control.dispatchEvent(new Event('input', {bubbles: true}));
                    control.dispatchEvent(new Event('change', {bubbles: true}));
                    results.push({label, ok: true, mode: 'select', method: binding.method});
                    continue;
                }
                if (type === 'checkbox' || type === 'radio') {
                    const shouldCheck = !/^(false|off|no|0|关闭|关)$/i.test(expected || 'true');
                    if (Boolean(control.checked) !== shouldCheck) clickEl(control);
                    results.push({label, ok: true, mode: type, method: binding.method});
                    continue;
                }
                const role = (control.getAttribute('role') || '').toLowerCase();
                const popup = (control.getAttribute('aria-haspopup') || '').toLowerCase();
                if ((control.readOnly || role === 'combobox' || popup) && tag !== 'textarea') {
                    results.push({label, ok: false, reason: 'complex_component_requires_form_set_or_macro', method: binding.method});
                    continue;
                }
                if (tag === 'input' || tag === 'textarea' || tag === 'iframe' || control.isContentEditable) {
                    const writeMethod = setNativeValue(control, expected);
                    if (writeMethod === 'iframe_unreachable') {
                        // cross-origin editor iframe: the main-document write
                        // cannot reach its body - surface the failure with the
                        // frame coordinates the Python rescue pass needs.
                        // srcdoc frames expose no src and all share the
                        // about:srcdoc URL, so stamp the host element with a
                        // reusable token the rescue can query in any frame
                        // document (reused on retries, never stacked).
                        let frameToken = '';
                        try {
                            frameToken = control.getAttribute('data-vspider-frame-token')
                                || ('vsp-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8));
                            control.setAttribute('data-vspider-frame-token', frameToken);
                        } catch (_) { frameToken = ''; }
                        results.push({
                            label, ok: false, reason: 'iframe_unreachable', method: binding.method,
                            frameId: control.id || '', frameSrc: control.src || '',
                            frameToken, frameSrcdoc: Boolean(control.hasAttribute?.('srcdoc'))
                        });
                        continue;
                    }
                    results.push({label, ok: true, mode: 'input', method: binding.method});
                    continue;
                }
                results.push({label, ok: false, reason: 'unsupported_control', method: binding.method});
            }

            await sleep(200);
            const verifications = bindings.map(([label, value, binding]) => verify(binding, label, value));
            const verificationOk = (verifications.length > 0 || presetHits > 0) && verifications.every(r => r.ok);
            if (!verificationOk || !results.every(r => r.ok)) {
                return {ok: false, results, verifications, scopeText: textOf(scope).slice(0, 160)};
            }

            const submit = allVisible('button,.el-button,[role=button],input[type=submit],input[type=button]', scope)
                .find(el => /(?:^|\\s)(?:create|submit)(?:\\s|$)|提交|保存|确定/i.test(textOf(el).trim())) ||
                allVisible('button,.el-button,[role=button],input[type=submit],input[type=button]')
                    .find(el => /(?:^|\\s)(?:create|submit)(?:\\s|$)|提交|保存|确定/i.test(textOf(el).trim()));
            let submitted = false;
            let submitText = '';
            if (submit) {
                submitText = textOf(submit).trim();
                clickEl(submit);
                await sleep(500);
                submitted = true;
            }
            if (requireSubmit && !submitted) {
                return {ok: false, reason: 'submit_not_found', results, verifications, requireSubmit, submitted};
            }
            return {ok: true, submitted, submitText, results, verifications, scopeText: textOf(scope).slice(0, 160)};
        }""",
        {
            "scopeTitle": scope_title,
            "fields": fields,
            "requireSubmit": require_submit,
            "presetLabels": list(preset_labels or []),
        },
    )


_AUTO_FORM_NOT_FOUND_REASONS = {"field_not_found", "invalid_result"}


def _auto_form_result_found_nothing(result: object) -> bool:
    """True when the bound-control pass resolved none of the requested fields."""
    if not isinstance(result, dict):
        return True
    if result.get("ok"):
        return False
    results = result.get("results")
    if not isinstance(results, list) or not results:
        return True
    return all(
        isinstance(item, dict)
        and not item.get("ok")
        and item.get("reason") in _AUTO_FORM_NOT_FOUND_REASONS
        for item in results
    )


_FRAME_RICH_TEXT_WRITE_JS = """(text) => {
    const escHtml = (s) => String(s).replace(/&/g, '&amp;')
        .replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const html = String(text || '')
        .split(/\\n{2,}/)
        .map(part => `<p>${escHtml(part).replace(/\\n/g, '<br>')}</p>`)
        .join('') || '<p></p>';
    const body = document.body;
    if (!body) return null;
    body.innerHTML = html;
    body.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: String(text || '')}));
    body.dispatchEvent(new Event('change', {bubbles: true}));
    return String(body.innerText || '');
}"""


def _norm_form_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


async def _resolve_editor_frame(
    page, *, frame_id: object, frame_src: object, frame_token: object = None
):
    """Locate the Playwright frame of an editor iframe by token, id, then URL.

    The token attribute is stamped by the macro JS precisely because srcdoc
    editor iframes expose no src and every one of them reports about:srcdoc,
    leaving id/src with nothing to match. Selectors pierce open shadow roots
    but not frame boundaries, so the lookup walks the page first and then the
    flat frame list to keep nested hosts reachable.
    """
    tok = str(frame_token or "").strip()
    if tok and '"' not in tok:
        selector = f'iframe[data-vspider-frame-token="{tok}"]'
        for host in [page, *list(getattr(page, "frames", None) or [])]:
            try:
                handle = await host.query_selector(selector)
                if handle is None:
                    continue
                frame = await handle.content_frame()
            except Exception:
                continue
            if frame is not None:
                return frame
    fid = str(frame_id or "").strip()
    if fid and '"' not in fid:
        try:
            handle = await page.query_selector(f'iframe[id="{fid}"]')
            if handle is not None:
                frame = await handle.content_frame()
                if frame is not None:
                    return frame
        except Exception:
            pass
    src = str(frame_src or "").strip()
    if src:
        for frame in list(getattr(page, "frames", None) or []):
            try:
                if (getattr(frame, "url", "") or "") == src:
                    return frame
            except Exception:
                continue
    return None


async def _auto_form_rescue_unreachable_iframes(
    page,
    result: object,
    *,
    scope_title: str,
    fields: dict[str, str],
    require_submit: bool,
    rerun_scope=None,
) -> object:
    """Rescue cross-origin editor iframes through Playwright's frame tree.

    Main-document JS cannot reach a cross-origin editor body, but the frame
    itself can be scripted. Write the structured paragraphs there, verify the
    readback, then rerun the macro with the rescued labels preset so the
    remaining fields and the submit step keep their existing semantics. Any
    miss returns the original result (the VLM path stays the backstop).
    """
    if not isinstance(result, dict) or result.get("ok"):
        return result
    unreachable = [
        item
        for item in (result.get("results") or [])
        if isinstance(item, dict) and item.get("reason") == "iframe_unreachable"
    ]
    if not unreachable:
        return result
    writes: list[dict[str, object]] = []
    for item in unreachable:
        label = str(item.get("label") or "")
        expected = fields.get(label)
        if expected is None:
            return result
        frame = await _resolve_editor_frame(
            page,
            frame_id=item.get("frameId"),
            frame_src=item.get("frameSrc"),
            frame_token=item.get("frameToken"),
        )
        if frame is None:
            return result
        try:
            readback = await frame.evaluate(_FRAME_RICH_TEXT_WRITE_JS, str(expected))
        except Exception as frame_err:
            logger.debug(
                "[AUTO FORM] frame-level rescue write failed (%s): %s",
                getattr(frame, "url", "?"),
                frame_err,
            )
            return result
        if _norm_form_text(readback) != _norm_form_text(expected):
            return result
        writes.append(
            {
                "label": label,
                "frame_url": getattr(frame, "url", "") or "",
                "readback_ok": True,
            }
        )
    rerun = await _try_auto_form_fill_bound_controls(
        rerun_scope if rerun_scope is not None else page,
        scope_title=scope_title,
        fields=fields,
        require_submit=require_submit,
        preset_labels=[str(w["label"]) for w in writes],
    )
    if isinstance(rerun, dict):
        rerun["frame_level_writes"] = writes
        if isinstance(result, dict) and result.get("frame_url"):
            rerun.setdefault("frame_url", result["frame_url"])
        logger.info(
            "[AUTO FORM] frame-level rescue wrote %d editor iframe field(s); rerun ok=%s",
            len(writes),
            rerun.get("ok"),
        )
        return rerun
    return result


async def _auto_form_fill_bound_controls_with_frames(
    page,
    *,
    scope_title: str,
    fields: dict[str, str],
    require_submit: bool,
) -> dict:
    """Run the bound-control pass in the main document, then probe child iframes.

    Mirrors actions._form_set_with_frames: only a full miss (every requested
    field unresolved) falls through to iframes; partial hits stay in the main
    document so we never guess across frames.
    """
    result = await _try_auto_form_fill_bound_controls(
        page,
        scope_title=scope_title,
        fields=fields,
        require_submit=require_submit,
    )
    if not _auto_form_result_found_nothing(result):
        return await _auto_form_rescue_unreachable_iframes(
            page,
            result,
            scope_title=scope_title,
            fields=fields,
            require_submit=require_submit,
        )
    main_frame = getattr(page, "main_frame", None)
    for frame in list(getattr(page, "frames", None) or []):
        if frame is main_frame:
            continue
        try:
            is_detached = getattr(frame, "is_detached", None)
            if callable(is_detached) and is_detached():
                continue
            frame_result = await _try_auto_form_fill_bound_controls(
                frame,
                scope_title=scope_title,
                fields=fields,
                require_submit=require_submit,
            )
        except Exception as frame_err:
            logger.debug(
                "[AUTO FORM] frame probe failed (%s): %s",
                getattr(frame, "url", "?"),
                frame_err,
            )
            continue
        if not _auto_form_result_found_nothing(frame_result):
            if isinstance(frame_result, dict):
                frame_result.setdefault("frame_url", getattr(frame, "url", "") or "")
            return await _auto_form_rescue_unreachable_iframes(
                page,
                frame_result,
                scope_title=scope_title,
                fields=fields,
                require_submit=require_submit,
                rerun_scope=frame,
            )
    return result

async def _evaluate_rows_with_frame_fallback(
    page, js: str, *, log_tag: str = "EXTRACT DOM", payload_empty=None
) -> object:
    """Evaluate row-harvesting JS in the main document, then child iframes.

    Returns the first non-empty payload. ``payload_empty`` customises the
    emptiness check for non-list payloads (e.g. ``{rows, sourceText}``
    dicts); the default keeps the original list-only contract. Mirrors the
    FORM-IFRAME-2 pattern: detached/raising frames are skipped, frame hits
    log the URL, and a main-document failure still lets iframes be probed.
    """

    def _is_empty(value: object) -> bool:
        if payload_empty is not None:
            try:
                return bool(payload_empty(value))
            except Exception:
                return True
        return not (isinstance(value, list) and value)

    rows: object = []
    try:
        rows = await page.evaluate(js)
    except Exception as main_err:
        logger.debug("[%s] main-document evaluate failed: %s", log_tag, main_err)
        rows = []
    if not _is_empty(rows):
        return rows
    main_frame = getattr(page, "main_frame", None)
    for frame in list(getattr(page, "frames", None) or []):
        if frame is main_frame:
            continue
        try:
            is_detached = getattr(frame, "is_detached", None)
            if callable(is_detached) and is_detached():
                continue
            frame_rows = await frame.evaluate(js)
        except Exception as frame_err:
            logger.debug(
                "[%s] frame probe failed (%s): %s",
                log_tag,
                getattr(frame, "url", "?"),
                frame_err,
            )
            continue
        if not _is_empty(frame_rows):
            try:
                size = len(frame_rows)
            except Exception:
                size = -1
            logger.info(
                "[%s] payload_size=%s frame=%s",
                log_tag,
                size,
                getattr(frame, "url", "") or "?",
            )
            return frame_rows
    if payload_empty is None:
        return rows if isinstance(rows, list) else []
    return rows

async def _try_auto_form_fill(browser: "BrowserEnv", goal: str) -> bool:
    """Deterministic label/scoped form executor, used before handing control to VLM."""
    if not _goal_is_form_fill(goal):
        return False
    scope_title = _parse_goal_scope_title(goal)
    page = await browser._ensure_active_page(reason="before auto form fill")
    if not page:
        return False
    _default_fields = _prepare_form_batch_fields(goal)
    _active_fields, _challenge_state = await _resolve_active_form_assignments(browser, goal, _default_fields)
    if (
        not _default_fields
        and _should_use_round_form_macro(goal, _default_fields)
        and _challenge_state
        and _challenge_state.get("is_challenge")
        and _challenge_state.get("current_round")
    ):
        try:
            if await _run_rpa_challenge_macro(browser, page, _challenge_state):
                return True
        except Exception as exc:
            logger.warning("[RPA CHALLENGE] deterministic macro failed, falling back: %s", exc)
    fields = _active_fields
    if len(fields) < 2:
        return False
    require_submit = bool(
        re.search(
            r"(create|submit|提交|保存|确定|点击.+按钮|按钮)",
            str(goal or ""),
            re.IGNORECASE,
        )
    )
    repeat_count = _parse_form_repeat_count(goal)
    try:
        if repeat_count > 1:
            repeat_results = []
            for repeat_index in range(1, repeat_count + 1):
                active_page = await browser._ensure_active_page(
                    reason=f"auto form repeat {repeat_index}/{repeat_count}"
                )
                if not active_page:
                    bound_result = {
                        "ok": False,
                        "reason": "no_active_page",
                        "repeatIndex": repeat_index,
                        "repeatCount": repeat_count,
                        "repetitions": repeat_results,
                    }
                    break
                bound_result = await _auto_form_fill_bound_controls_with_frames(
                    active_page,
                    scope_title=scope_title,
                    fields=fields,
                    require_submit=require_submit,
                )
                if isinstance(bound_result, dict):
                    bound_result["repeatIndex"] = repeat_index
                    bound_result["repeatCount"] = repeat_count
                repeat_results.append(bound_result)
                logger.info(
                    "[AUTO FORM] repeat %s/%s bound-control result=%s",
                    repeat_index,
                    repeat_count,
                    bound_result,
                )
                if not isinstance(bound_result, dict) or not bound_result.get("ok"):
                    bound_result = {
                        "ok": False,
                        "reason": "repeat_failed",
                        "repeatIndex": repeat_index,
                        "repeatCount": repeat_count,
                        "repetitions": repeat_results,
                    }
                    break
                if repeat_index < repeat_count:
                    await asyncio.sleep(0.8)
            else:
                bound_result = {
                    "ok": True,
                    "submitted": bool(require_submit),
                    "repeatCount": repeat_count,
                    "repetitions": repeat_results,
                }
        else:
            bound_result = await _auto_form_fill_bound_controls_with_frames(
                page,
                scope_title=scope_title,
                fields=fields,
                require_submit=require_submit,
            )
        logger.info("[AUTO FORM] bound-control result=%s", bound_result)
        try:
            browser._last_auto_form_result = bound_result
        except Exception:
            pass
        if isinstance(bound_result, dict) and bound_result.get("ok"):
            print(f"\033[1;32m✅ [AUTO FORM]\033[0m 已按 label/control 绑定执行表单填报")
            _broadcast_log_safe("[AUTO FORM] Deterministic bound-control form fill executed")
            return True
        logger.info("[AUTO FORM] bound-control path declined; trying component-aware fallback.")
    except Exception as err:
        logger.warning("[AUTO FORM] bound-control path failed: %s", err)
        logger.info("[AUTO FORM] trying component-aware fallback after bound-control error.")

    logger.info("[AUTO FORM] Trying deterministic form fill. scope=%r fields=%s", scope_title, list(fields))
    try:
        result = await page.evaluate(
            """async ({scopeTitle, fields, requireSubmit}) => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el?.innerText, el?.textContent, el?.getAttribute?.('aria-label'),
                    el?.getAttribute?.('placeholder'), el?.getAttribute?.('title'),
                    el?.getAttribute?.('value'), el?.name, el?.id
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                const setNativeValue = (el, val) => {
                    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(el, val); else el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                };
                const clickEl = (el) => {
                    el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                    if (typeof el.click === 'function') el.click();
                    else el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, composed: true, view: window}));
                };
                const deepQueryAll = (selector, root = document) => {
                    const out = Array.from(root.querySelectorAll(selector));
                    for (const host of root.querySelectorAll('*')) {
                        if (host.shadowRoot) out.push(...deepQueryAll(selector, host.shadowRoot));
                    }
                    return out;
                };
                const allVisible = (selector, root = document) => deepQueryAll(selector, root).filter(isVisible);
                const labels = Object.keys(fields || {});

                const findScope = () => {
                    if (scopeTitle) {
                        const titleNorm = norm(scopeTitle);
                        const heading = allVisible('h1,h2,h3,h4,h5,h6')
                            .find(el => norm(textOf(el)) === titleNorm || norm(textOf(el)).includes(titleNorm));
                        if (heading) {
                            let sib = heading.nextElementSibling;
                            for (let i = 0; sib && i < 12; i++, sib = sib.nextElementSibling) {
                                if (sib.querySelector?.('form,.el-form,.ant-form,.n-form')) {
                                    return sib.querySelector('form,.el-form,.ant-form,.n-form') || sib;
                                }
                            }
                            let cur = heading.parentElement;
                            for (let i = 0; cur && i < 6; i++, cur = cur.parentElement) {
                                const form = cur.querySelector?.('form,.el-form,.ant-form,.n-form');
                                if (form && isVisible(form)) return form;
                            }
                        }
                    }
                    const candidateRoots = allVisible('form,.el-form,.ant-form,.n-form,section,article,main,[role=main],div');
                    const scored = [];
                    for (const root of candidateRoots) {
                        const r = root.getBoundingClientRect();
                        if (r.left < 180 || r.width < 250 || r.height < 80) continue;
                        const txt = norm(textOf(root));
                        let score = 0;
                        if (scopeTitle && txt.includes(norm(scopeTitle))) score += 100;
                        for (const label of labels) if (txt.includes(norm(label))) score += 20;
                        if (root.matches('form,.el-form,.ant-form,.n-form')) score += 60;
                        if (score > 0) scored.push({root, score, area: r.width * r.height});
                    }
                    scored.sort((a, b) => b.score - a.score || a.area - b.area);
                    if (scored[0]) return scored[0].root;
                    return document.body;
                };

                const scope = findScope();
                const findFieldContainer = (label) => {
                    const ln = norm(label);
                    const directControls = allVisible('input,textarea,select,[contenteditable=true],[role=combobox]', scope)
                        .filter(el => !['hidden','button','submit','reset'].includes((el.type || '').toLowerCase()));
                    const directHit = directControls.find(el => {
                        const attrs = [
                            el.getAttribute?.('aria-label'),
                            el.getAttribute?.('placeholder'),
                            el.getAttribute?.('title'),
                            el.getAttribute?.('name'),
                            el.id
                        ].filter(Boolean).map(norm);
                        return attrs.some(t => t === ln || t.includes(ln) || ln.includes(t));
                    });
                    if (directHit) {
                        const directContainer = directHit.closest?.(
                            '.form-group,.form-row,.el-form-item,.ant-form-item,.n-form-item,[class*=form-item],[class*=field],.col-md-4,.col-md-6,.col-sm-12'
                        );
                        return (directContainer && directContainer !== directHit)
                            ? directContainer
                            : (directHit.parentElement || directHit);
                    }
                    const nodes = allVisible('label,.el-form-item__label,.ant-form-item-label,.n-form-item-label,span,div', scope);
                    const matches = [];
                    for (const el of nodes) {
                        const t = norm(textOf(el));
                        if (!t || (t !== ln && !t.includes(ln))) continue;
                        const r = el.getBoundingClientRect();
                        let score = t === ln ? 80 : 20;
                        if (el.matches('label,.el-form-item__label,.ant-form-item-label,.n-form-item-label')) score += 60;
                        if (r.left > 180) score += 20;
                        matches.push({el, score});
                    }
                    matches.sort((a, b) => b.score - a.score);
                    const labelEl = matches[0]?.el;
                    if (!labelEl) return null;
                    let cur = labelEl;
                    for (let i = 0; cur && i < 8; i++) {
                        if (cur !== labelEl && (
                            cur.classList?.contains('el-form-item') ||
                            cur.classList?.contains('ant-form-item') ||
                            cur.classList?.contains('n-form-item')
                        )) return cur;
                        cur = cur.parentElement;
                    }
                    cur = labelEl.parentElement;
                    for (let i = 0; cur && i < 5; i++) {
                        if (cur.querySelector?.('input,textarea,select,[role=combobox],[role=checkbox],[role=radio],button,.el-select,.ant-select,.n-select')) return cur;
                        cur = cur.parentElement;
                    }
                    return labelEl.parentElement;
                };

                const clickOption = async (value) => {
                    const vn = norm(value);
                    for (let i = 0; i < 12; i++) {
                        const opts = allVisible([
                            '.el-select-dropdown__item','.el-radio','.el-checkbox',
                            '.ant-select-item-option','[role=option]',
                            '[id^="react-select"][id*="option"]',
                            '.css-yt9ioa-option','.css-1n7v3ny-option',
                            'label','li','span','button'
                        ].join(','));
                        const hit = opts.find(el => norm(textOf(el)) === vn) || opts.find(el => norm(textOf(el)).includes(vn));
                        if (hit) {
                            clickEl(hit);
                            await sleep(250);
                            return true;
                        }
                        await sleep(150);
                    }
                    return false;
                };
                const clickDateValue = async (value, opener) => {
                    const m = String(value || '').match(/^(\\d{4})-(\\d{2})-(\\d{2})/);
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
                        '.n-date-panel-actions + * button[aria-label*=next]',
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
                const verifyField = (label, value) => {
                    const item = findFieldContainer(label);
                    if (!item) return {label, ok: false, reason: 'field_not_found'};
                    const expected = norm(value);
                    const observed = [
                        textOf(item),
                        ...allVisible('input,textarea,select,[contenteditable=true]', item).map(el => {
                            if (el.tagName?.toLowerCase() === 'select') {
                                return [el.value, el.selectedOptions?.[0]?.textContent].filter(Boolean).join(' ');
                            }
                            return [el.value, el.textContent, el.getAttribute('aria-label')].filter(Boolean).join(' ');
                        })
                    ].join(' ').replace(/\\s+/g, ' ').trim();
                    const observedNorm = norm(observed);

                    const switchRoot = allVisible('.el-switch,[role=switch]', item)[0];
                    if (switchRoot) {
                        const checked = switchRoot.classList.contains('is-checked') ||
                            switchRoot.getAttribute('aria-checked') === 'true' ||
                            Boolean(item.querySelector('input:checked'));
                        const shouldOn = !/^(false|off|no|0)$/i.test(String(value || ''));
                        return {label, ok: checked === shouldOn, expected: value, mode: 'switch', observed: checked ? 'checked' : 'unchecked'};
                    }

                    const choiceNodes = allVisible('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]', item);
                    const choiceHit = choiceNodes.find(el => norm(textOf(el)).includes(expected));
                    if (choiceHit) {
                        const choiceTarget = choiceHit.closest?.('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]') || choiceHit;
                        const checked = choiceTarget.matches?.('.is-checked,[aria-checked=true]') ||
                            Boolean(choiceTarget.querySelector?.('.is-checked,[aria-checked=true],input:checked')) ||
                            Boolean(choiceHit.querySelector?.('.is-checked,[aria-checked=true],input:checked'));
                        if (checked) return {label, ok: true, expected: value, mode: 'choice_checked', observed};
                    }

                    if (expected && observedNorm.includes(expected)) {
                        return {label, ok: true, expected: value, mode: 'value_visible', observed};
                    }
                    return {label, ok: false, reason: 'value_not_reflected', expected: value, observed};
                };

                const results = [];
                for (const [label, value] of Object.entries(fields || {})) {
                    const item = findFieldContainer(label);
                    if (!item) {
                        results.push({label, ok: false, reason: 'field_not_found'});
                        continue;
                    }
                    item.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                    await sleep(150);
                    const valueText = String(value || '');
                    const vn = norm(valueText);

                    const textarea = allVisible('textarea', item)[0];
                    if (textarea) {
                        setNativeValue(textarea, valueText);
                        results.push({label, ok: true, mode: 'textarea'});
                        continue;
                    }

                    const selectRoot = allVisible('.el-select,.ant-select,.n-select', item)[0] ||
                        allVisible('[role=combobox]', item)[0];
                    const inputs = allVisible('input,[contenteditable=true]', item)
                        .filter(el => !['hidden','checkbox','radio','button','submit'].includes((el.type || '').toLowerCase()));
                    if (/(date|time|日期|时间)/i.test(label) && /^\\d{4}-\\d{2}-\\d{2}/.test(valueText) && inputs.length) {
                        const picked = await clickDateValue(valueText, inputs[0]);
                        if (picked) {
                            results.push({label, ok: true, mode: 'date_picker'});
                            continue;
                        }
                        setNativeValue(inputs[0], valueText);
                        results.push({label, ok: true, mode: 'date_input'});
                        continue;
                    }
                    const readonly = inputs.find(el => el.readOnly || (el.getAttribute('role') || '').toLowerCase() === 'combobox' || el.getAttribute('aria-haspopup'));
                    const autocomplete = inputs.find(el => {
                        const role = (el.getAttribute('role') || '').toLowerCase();
                        const auto = (el.getAttribute('aria-autocomplete') || el.getAttribute('autocomplete') || '').toLowerCase();
                        return role === 'combobox' || auto === 'list' || auto === 'both' ||
                            /subject|tag|skill|course|autocomplete|联想|科目|课程/i.test(label);
                    });
                    if (autocomplete && /subject|tag|skill|course|autocomplete|联想|科目|课程/i.test(label)) {
                        const query = valueText;
                        clickEl(autocomplete);
                        setNativeValue(autocomplete, query);
                        autocomplete.dispatchEvent(new KeyboardEvent('keydown', {key: query.slice(-1) || 'a', bubbles: true}));
                        autocomplete.dispatchEvent(new KeyboardEvent('keyup', {key: query.slice(-1) || 'a', bubbles: true}));
                        await sleep(500);
                        let ok = await clickOption(valueText);
                        if (!ok && /s$/i.test(valueText)) {
                            setNativeValue(autocomplete, valueText.replace(/s$/i, ''));
                            await sleep(500);
                            ok = await clickOption(valueText);
                        }
                        if (!ok) {
                            autocomplete.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
                            autocomplete.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', bubbles: true}));
                            await sleep(250);
                        }
                        results.push({label, ok: ok || norm(textOf(item)).includes(vn), mode: 'autocomplete'});
                        continue;
                    }
                    const isChoiceValue = /(zone|type|resource|delivery|date|time|subject|course|下拉|选择|复选|单选|开关|日期|时间|科目|课程)/i.test(label);
                    if ((selectRoot || readonly) && isChoiceValue) {
                        let opener = selectRoot || readonly;
                        for (const sel of [
                            '.el-select__wrapper', '.el-select',
                            '.ant-select-selector', '.ant-select',
                            '.n-base-selection', '[role=combobox]'
                        ]) {
                            const closest = readonly?.closest?.(sel) || opener?.closest?.(sel) || opener?.querySelector?.(sel);
                            if (closest && isVisible(closest)) {
                                opener = closest;
                                break;
                            }
                        }
                        clickEl(opener);
                        await sleep(350);
                        const ok = await clickOption(valueText);
                        results.push({label, ok, mode: 'select'});
                        continue;
                    }

                    const switchRoot = allVisible('.el-switch,[role=switch]', item)[0];
                    if (switchRoot) {
                        const shouldOn = /^(true|on|yes|1|开启|打开|选中|勾选)$/i.test(valueText || '开启');
                        const checked = switchRoot.classList.contains('is-checked') || switchRoot.getAttribute('aria-checked') === 'true';
                        if (shouldOn !== checked) clickEl(switchRoot);
                        results.push({label, ok: true, mode: 'switch'});
                        continue;
                    }

                    const choiceHit = allVisible('label,.el-radio,.el-checkbox,span,button', item)
                        .find(el => norm(textOf(el)) === vn) ||
                        allVisible('label,.el-radio,.el-checkbox,span,button', item)
                        .find(el => norm(textOf(el)).includes(vn));
                    if (choiceHit) {
                        const choiceTarget = choiceHit.closest?.('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]') || choiceHit;
                        clickEl(choiceTarget);
                        await sleep(150);
                        const checked = choiceTarget.matches?.('.is-checked,[aria-checked=true],input:checked') ||
                            choiceTarget.querySelector?.('.is-checked,[aria-checked=true],input:checked') ||
                            choiceHit.matches?.('.is-checked,[aria-checked=true],input:checked') ||
                            choiceHit.querySelector?.('.is-checked,[aria-checked=true],input:checked');
                        results.push({label, ok: Boolean(checked) || norm(textOf(choiceTarget)).includes(vn) || choiceTarget.tagName === 'BUTTON', mode: 'choice'});
                        continue;
                    }

                    if (inputs.length) {
                        setNativeValue(inputs[0], valueText);
                        results.push({label, ok: true, mode: 'input'});
                        continue;
                    }
                    results.push({label, ok: false, reason: 'no_control'});
                }

                await sleep(250);
                const verifications = Object.entries(fields || {}).map(([label, value]) => verifyField(label, value));
                const verificationOk = verifications.length > 0 && verifications.every(r => r.ok);
                if (!verificationOk) {
                    return {
                        ok: false,
                        scopeText: textOf(scope).slice(0, 120),
                        results,
                        verifications
                    };
                }

                const submit = allVisible('button,.el-button,[role=button],input[type=submit],input[type=button]', scope)
                    .find(el => /(?:^|\\s)(?:create|submit)(?:\\s|$)|提交|保存|确定/i.test(textOf(el).trim())) ||
                    allVisible('button,.el-button,[role=button],input[type=submit],input[type=button]')
                    .filter(el => el.getBoundingClientRect().left > 180)
                    .find(el => /(?:^|\\s)(?:create|submit)(?:\\s|$)|提交|保存|确定/i.test(textOf(el).trim()));
                let submitted = false;
                let submitText = '';
                if (submit) {
                    submitText = textOf(submit).trim();
                    clickEl(submit);
                    await sleep(500);
                    submitted = true;
                    results.push({label: '__submit__', ok: true, mode: submitText});
                }
                if (requireSubmit && !submitted) {
                    return {
                        ok: false,
                        reason: 'submit_not_found',
                        requireSubmit,
                        submitted,
                        scopeText: textOf(scope).slice(0, 120),
                        results,
                        verifications
                    };
                }

                const fieldResults = results.filter(r => r.label !== '__submit__');
                return {
                    ok: fieldResults.length > 0 && fieldResults.every(r => r.ok) && verificationOk && (!requireSubmit || submitted),
                    submitted,
                    submitText,
                    requireSubmit,
                    scopeText: textOf(scope).slice(0, 120),
                    results,
                    verifications
                };
            }""",
            {
                "scopeTitle": _parse_goal_scope_title(goal),
                "fields": fields,
                "requireSubmit": require_submit,
            },
        )
        logger.info("[AUTO FORM] result=%s", result)
        try:
            browser._last_auto_form_result = result
        except Exception:
            pass
        if isinstance(result, dict) and result.get("ok"):
            print(f"\033[1;32m✅ [AUTO FORM]\033[0m 已按 DOM scope 执行表单填报")
            _broadcast_log_safe("[AUTO FORM] Deterministic scoped form fill executed")
            return True
        return False
    except Exception as err:
        logger.warning("[AUTO FORM] deterministic fill failed, fallback to VLM: %s", err)
        return False


def _format_auto_form_validation_summary(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    repetitions = result.get("repetitions")
    if isinstance(repetitions, list) and repetitions:
        chunks: list[str] = []
        for idx, item in enumerate(repetitions, start=1):
            if not isinstance(item, dict):
                chunks.append(f"第{idx}次: {item!r}")
                continue
            one = _format_auto_form_validation_summary(
                {k: v for k, v in item.items() if k != "repetitions"}
            )
            chunks.append(f"第{idx}次: {one or item!r}")
        status = "成功" if result.get("ok") else "失败"
        return f"重复表单执行{status}: 共{result.get('repeatCount') or len(repetitions)}次; " + " || ".join(chunks)
    verifications = result.get("verifications") or []
    parts: list[str] = []
    if isinstance(verifications, list):
        for item in verifications:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            expected = str(item.get("expected") or "").strip()
            observed = str(item.get("observed") or "").strip()
            method = str(item.get("method") or item.get("mode") or "").strip()
            if label:
                parts.append(f"{label}: expected={expected!r}, observed={observed!r}, method={method}")
    submit_text = str(result.get("submitText") or "").strip()
    submitted = bool(result.get("submitted"))
    suffix = f"; submitted={submitted}"
    if submit_text:
        suffix += f", submit={submit_text!r}"
    if parts:
        return "字段回读: " + " | ".join(parts) + suffix
    return f"字段回读结果: {result!r}"


"""Skill for opening modal dialogs, extracting text, and closing them."""

from __future__ import annotations

import re
from typing import Any

from ..base import AgentSkill, SkillResult, SkillVerification


class ModalDialogSkill(AgentSkill):
    name = "modal_dialogs"
    action = "modal_dialog_macro"
    source = "MODAL_DIALOG_MACRO"
    capability = "dialog"
    aliases = ("modal", "dialog", "弹窗", "对话框")
    required_fields = ("trigger", "title", "content")
    expected_rows = 2

    def match(self, *, url: str, goal: str) -> bool:
        labels = parse_modal_trigger_labels(goal)
        return bool(labels and re.search(r"modal|dialog|弹窗|对话框", str(goal or ""), re.I))

    def dispatch_metadata(self, *, url: str, goal: str) -> dict[str, Any]:
        return {"triggers": parse_modal_trigger_labels(goal), "url": url}

    async def run(self, browser: Any, goal: str) -> SkillResult:
        labels = parse_modal_trigger_labels(goal)
        page = await browser._ensure_active_page(reason="modal dialog skill")
        rows: list[dict[str, Any]] = []
        if page:
            for label in labels:
                clicked = await _click_visible_text(page, label, role="button")
                if not clicked:
                    break
                try:
                    await page.wait_for_selector(
                        'dialog, [role="dialog"], [aria-modal="true"], .modal-content, .modal-dialog, .modal',
                        state="visible",
                        timeout=5000,
                    )
                except Exception:
                    await page.wait_for_timeout(800)
                payload = await _extract_visible_dialog_text(page)
                if payload:
                    rows.append({
                        "trigger": label,
                        "title": payload.get("title") or label,
                        "content": payload.get("content") or payload.get("text") or "",
                    })
                await _close_visible_dialog(page)
        return SkillResult(
            source=self.source,
            rows=rows,
            expected_rows=len(labels) or self.expected_rows,
            required_fields=list(self.required_fields),
            metadata=self.dispatch_metadata(url=getattr(browser, "current_url", ""), goal=goal),
            verifications=[
                SkillVerification(
                    name=f"{self.source}_trigger_count",
                    success=len(rows) == len(labels),
                    expected=len(labels),
                    observed=len(rows),
                    metadata={"triggers": labels},
                )
            ],
        )


def parse_modal_trigger_labels(goal: str) -> list[str]:
    labels: list[str] = []
    for match in re.finditer(r"['\"]([^'\"]*modal[^'\"]*)['\"]", str(goal or ""), re.I):
        label = re.sub(r"\s+", " ", match.group(1)).strip()
        if label and label.lower() not in {item.lower() for item in labels}:
            labels.append(label)
    return labels


async def _click_visible_text(page: Any, text: str, *, role: str | None = None) -> bool:
    label = str(text or "").strip()
    if not label:
        return False
    try:
        if role:
            loc = page.get_by_role(role, name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))
            if await loc.count():
                await loc.first.click(timeout=5000)
                return True
        loc = page.get_by_text(label, exact=True)
        if await loc.count():
            await loc.first.click(timeout=5000)
            return True
        loc = page.get_by_text(re.compile(re.escape(label), re.I))
        if await loc.count():
            await loc.first.click(timeout=5000)
            return True
    except Exception:
        return False
    return False


async def _extract_visible_dialog_text(page: Any) -> dict[str, str]:
    try:
        return await page.evaluate(
            """() => {
                const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const visible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden';
                };
                const dialogs = Array.from(document.querySelectorAll(
                    'dialog, [role="dialog"], [aria-modal="true"], .modal-content, .modal-dialog, .modal'
                )).filter(visible);
                const dialog = dialogs[dialogs.length - 1] || null;
                if (!dialog) return {};
                const titleEl = dialog.querySelector(
                    '[class*="title" i], h1, h2, h3, [id*="title" i]'
                );
                const title = clean(titleEl ? titleEl.innerText || titleEl.textContent : '');
                const bodyClone = dialog.cloneNode(true);
                bodyClone.querySelectorAll('button, [role="button"], .close, [aria-label*="close" i]')
                    .forEach((el) => el.remove());
                let content = clean(bodyClone.innerText || bodyClone.textContent);
                if (title && content.toLowerCase().startsWith(title.toLowerCase())) {
                    content = clean(content.slice(title.length));
                }
                return {title, content, text: clean(dialog.innerText || dialog.textContent)};
            }"""
        )
    except Exception:
        return {}


async def _close_visible_dialog(page: Any) -> bool:
    try:
        dialog = page.locator(
            'dialog, [role="dialog"], [aria-modal="true"], .modal-content, .modal-dialog, .modal'
        ).last
        close_candidates = [
            dialog.get_by_role("button", name=re.compile(r"^\s*(close|关闭|确定|ok)\s*$", re.I)),
            page.get_by_role("button", name=re.compile(r"^\s*(close|关闭|确定|ok)\s*$", re.I)),
            page.locator('[aria-label*="close" i], .close, .btn-close').last,
        ]
        for candidate in close_candidates:
            try:
                if await candidate.count():
                    await candidate.first.click(timeout=5000)
                    await page.wait_for_timeout(400)
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False

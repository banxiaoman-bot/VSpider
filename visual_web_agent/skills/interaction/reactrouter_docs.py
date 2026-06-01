"""Skill for React Router docs sidebar/title navigation checks."""

from __future__ import annotations

import re
from typing import Any

from ..base import AgentSkill, SkillResult, SkillVerification


class ReactRouterDocsSkill(AgentSkill):
    name = "reactrouter_docs"
    action = "reactrouter_docs_macro"
    source = "REACTROUTER_DOCS_MACRO"
    capability = "navigation"
    aliases = ("reactrouter.com", "Upgrading from v6", "Form")
    required_fields = ("step", "title", "url")
    expected_rows = 2

    def match(self, *, url: str, goal: str) -> bool:
        return (
            "reactrouter.com" in str(url or "")
            and bool(re.search(r"Upgrading\s+from\s+v6|Form", str(goal or ""), re.I))
        )

    def dispatch_metadata(self, *, url: str, goal: str) -> dict[str, Any]:
        return {"targets": ["Upgrading from v6", "Form"], "url": url}

    async def run(self, browser: Any, goal: str) -> SkillResult:
        page = await browser._ensure_active_page(reason="reactrouter docs skill")
        rows: list[dict[str, Any]] = []
        if page:
            if "reactrouter.com/" in page.url and "/docs" not in page.url and "/start/" not in page.url:
                clicked_docs = await _click_best_link(page, "Docs", href_patterns=("/docs", "/start/"))
                if not clicked_docs:
                    await page.goto("https://reactrouter.com/docs", wait_until="domcontentloaded", timeout=15000)
            if "api.reactrouter.com" in page.url:
                await page.goto("https://reactrouter.com/docs", wait_until="domcontentloaded", timeout=15000)

            clicked_upgrade = await _click_best_link(
                page,
                "Upgrading from v6",
                href_patterns=("/docs/upgrading/v6", "/upgrading/v6"),
            )
            if not clicked_upgrade:
                await page.goto("https://reactrouter.com/docs/upgrading/v6", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(800)
            rows.append({
                "step": "Upgrading from v6",
                "title": await _extract_main_heading(page, fallback="Upgrading from v6"),
                "url": page.url,
            })

            clicked_form = await _click_best_link(
                page,
                "Form",
                href_patterns=("/api/components/form", "/components/form"),
            )
            if not clicked_form:
                await page.goto("https://reactrouter.com/api/components/Form", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(800)
            rows.append({
                "step": "Form",
                "title": await _extract_main_heading(page, fallback="Form"),
                "url": page.url,
            })
        observed_steps = [str(row.get("step") or "") for row in rows]
        return SkillResult(
            source=self.source,
            rows=rows,
            expected_rows=self.expected_rows,
            required_fields=list(self.required_fields),
            metadata=self.dispatch_metadata(url=getattr(browser, "current_url", ""), goal=goal),
            verifications=[
                SkillVerification(
                    name=f"{self.source}_target_titles",
                    success=observed_steps == ["Upgrading from v6", "Form"],
                    expected=["Upgrading from v6", "Form"],
                    observed=observed_steps,
                )
            ],
        )


async def _click_best_link(page: Any, label: str, *, href_patterns: tuple[str, ...] = ()) -> bool:
    clean_label = str(label or "").strip()
    candidates = await page.locator("a, button").evaluate_all(
        """(nodes, args) => {
            const label = String(args.label || '').toLowerCase();
            const patterns = args.patterns || [];
            const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const visible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    s.display !== 'none' && s.visibility !== 'hidden';
            };
            return nodes.map((el, index) => {
                const text = clean(el.innerText || el.textContent);
                const href = el.href || el.getAttribute('href') || '';
                const textScore = text.toLowerCase() === label ? 3 :
                    (text.toLowerCase().includes(label) ? 1 : 0);
                const hrefScore = patterns.some((p) => href.toLowerCase().includes(String(p).toLowerCase())) ? 4 : 0;
                return {index, text, href, score: (visible(el) ? 1 : -5) + textScore + hrefScore};
            }).filter((item) => item.score > 1).sort((a, b) => b.score - a.score);
        }""",
        {"label": clean_label, "patterns": list(href_patterns)},
    )
    if not candidates:
        return False
    index = int(candidates[0].get("index") or 0)
    try:
        await page.locator("a, button").nth(index).click(timeout=7000)
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
        return True
    except Exception:
        href = str(candidates[0].get("href") or "")
        if href:
            try:
                await page.goto(href, wait_until="domcontentloaded", timeout=15000)
                return True
            except Exception:
                return False
    return False


async def _extract_main_heading(page: Any, *, fallback: str = "") -> str:
    for _ in range(10):
        try:
            value = await page.evaluate(
                """() => {
                    const clean = (text) => String(text || '').replace(/\\s+/g, ' ').trim();
                    const main = document.querySelector('main') || document.body;
                    const h = main.querySelector('h1') || document.querySelector('h1');
                    const title = document.querySelector('title');
                    const ogTitle = document.querySelector('meta[property="og:title"], meta[name="twitter:title"]');
                    return clean(
                        (h ? h.innerText || h.textContent : '') ||
                        (ogTitle ? ogTitle.getAttribute('content') : '') ||
                        (title ? title.innerText || title.textContent : '') ||
                        document.title
                    );
                }"""
            )
            text = str(value or "").strip()
            if text:
                return text
        except Exception:
            pass
        await page.wait_for_timeout(500)
    return fallback

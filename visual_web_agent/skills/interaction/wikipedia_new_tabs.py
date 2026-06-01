"""Skill for opening selected Wikipedia links in new tabs and extracting leads."""

from __future__ import annotations

import re
from typing import Any

from ..base import AgentSkill, SkillResult, SkillVerification


class WikipediaNewTabsSkill(AgentSkill):
    name = "wikipedia_new_tabs"
    action = "wikipedia_new_tab_macro"
    source = "WIKIPEDIA_NEW_TAB_MACRO"
    capability = "new_tab"
    aliases = ("wikipedia.org/wiki/Web_scraping", "data mining", "artificial intelligence")
    required_fields = ("link_text", "target_title", "first_paragraph")
    expected_rows = 2

    def match(self, *, url: str, goal: str) -> bool:
        return (
            "wikipedia.org/wiki/Web_scraping" in str(url or "")
            and bool(re.search(r"data mining|artificial intelligence|New Tab|新标签", str(goal or ""), re.I))
        )

    def dispatch_metadata(self, *, url: str, goal: str) -> dict[str, Any]:
        return {"targets": ["data mining", "artificial intelligence"], "url": url}

    async def run(self, browser: Any, goal: str) -> SkillResult:
        page = await browser._ensure_active_page(reason="wikipedia new tab skill")
        rows: list[dict[str, Any]] = []
        if page:
            context = page.context
            original_page = page
            for link_text in ("data mining", "artificial intelligence"):
                href = await _find_exact_link_href(original_page, link_text)
                if not href:
                    continue
                new_page = await context.new_page()
                try:
                    await new_page.goto(href, wait_until="domcontentloaded", timeout=15000)
                    await new_page.wait_for_timeout(800)
                    payload = await _extract_title_and_first_paragraph(new_page)
                    rows.append({
                        "link_text": link_text,
                        "target_title": str(payload.get("title") or "").strip(),
                        "first_paragraph": str(payload.get("first_paragraph") or "").strip(),
                        "target_url": new_page.url,
                    })
                finally:
                    await new_page.close()
                    await original_page.bring_to_front()
        return SkillResult(
            source=self.source,
            rows=rows,
            expected_rows=self.expected_rows,
            required_fields=list(self.required_fields),
            metadata=self.dispatch_metadata(url=getattr(browser, "current_url", ""), goal=goal),
            verifications=[
                SkillVerification(
                    name=f"{self.source}_target_links",
                    success={row.get("link_text") for row in rows} == {"data mining", "artificial intelligence"},
                    expected=["data mining", "artificial intelligence"],
                    observed=[row.get("link_text") for row in rows],
                )
            ],
        )


async def _find_exact_link_href(page: Any, link_text: str) -> str:
    href = await page.evaluate(
        """(label) => {
            const norm = (v) => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const links = Array.from(document.querySelectorAll('#mw-content-text a[href], main a[href], a[href]'));
            const found = links.find(a => norm(a.innerText || a.textContent) === norm(label));
            return found ? found.href : '';
        }""",
        link_text,
    )
    return str(href or "").strip()


async def _extract_title_and_first_paragraph(page: Any) -> dict[str, str]:
    payload = await page.evaluate(
        """() => {
            const clean = (v) => String(v || '').replace(/\\s+/g, ' ').trim();
            const title = clean(document.querySelector('h1')?.innerText || document.title);
            const paragraphs = Array.from(document.querySelectorAll('#mw-content-text .mw-parser-output > p, main p, p'))
                .map(p => clean(p.innerText || p.textContent))
                .filter(text => text.length > 80 && !/^coordinates\\b/i.test(text));
            return {title, first_paragraph: paragraphs[0] || ''};
        }"""
    )
    return {
        "title": str(payload.get("title") or "").strip(),
        "first_paragraph": str(payload.get("first_paragraph") or "").strip(),
    }

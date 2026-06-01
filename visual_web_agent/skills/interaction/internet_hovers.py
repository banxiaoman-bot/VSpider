"""Skill for the-internet.herokuapp.com hover profile probe."""

from __future__ import annotations

import re
from typing import Any

from ..base import AgentSkill, SkillResult, SkillVerification


class InternetHoversSkill(AgentSkill):
    name = "internet_hovers_profile"
    action = "internet_hovers_macro"
    source = "INTERNET_HOVERS_PROFILE_MACRO"
    capability = "hover"
    aliases = ("the-internet.herokuapp.com/hovers", "hover", "view profile", "avatar")
    required_fields = ("username", "profile_link_text", "final_title_or_heading", "final_url")
    expected_rows = 1

    def match(self, *, url: str, goal: str) -> bool:
        return (
            "the-internet.herokuapp.com/hovers" in str(url or "")
            and bool(re.search(r"hover|悬停|悬浮|头像|View profile", str(goal or ""), re.I))
        )

    def dispatch_metadata(self, *, url: str, goal: str) -> dict[str, Any]:
        return {"target": "middle avatar", "url": url}

    async def run(self, browser: Any, goal: str) -> SkillResult:
        page = await browser._ensure_active_page(reason="internet hovers skill")
        rows: list[dict[str, Any]] = []
        if page:
            figures = page.locator(".figure")
            if await figures.count() >= 2:
                figure = figures.nth(1)
                await figure.scroll_into_view_if_needed(timeout=5000)
                await figure.hover(timeout=7000)
                await page.wait_for_timeout(500)
                caption = figure.locator(".figcaption")
                try:
                    caption_text = re.sub(r"\s+", " ", await caption.inner_text(timeout=5000)).strip()
                except Exception:
                    caption_text = ""
                username_match = re.search(r"name:\s*([^\s]+)", caption_text, re.I)
                username = username_match.group(1) if username_match else caption_text
                link = caption.get_by_text(re.compile(r"View profile", re.I)).first
                try:
                    profile_link_text = re.sub(r"\s+", " ", await link.inner_text(timeout=3000)).strip()
                except Exception:
                    profile_link_text = "View profile"
                await link.click(timeout=7000)
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                await page.wait_for_timeout(500)
                heading = await _extract_main_heading(page)
                rows.append({
                    "username": username,
                    "profile_link_text": profile_link_text,
                    "final_title_or_heading": heading,
                    "final_url": page.url,
                })
        return SkillResult(
            source=self.source,
            rows=rows,
            expected_rows=self.expected_rows,
            required_fields=list(self.required_fields),
            metadata=self.dispatch_metadata(url=getattr(browser, "current_url", ""), goal=goal),
            verifications=[
                SkillVerification(
                    name=f"{self.source}_not_found_page",
                    success=bool(rows and "not found" in str(rows[0].get("final_title_or_heading", "")).lower()),
                    expected="Not Found",
                    observed=rows[0].get("final_title_or_heading", "") if rows else "",
                )
            ],
        )


async def _extract_main_heading(page: Any) -> str:
    try:
        value = await page.evaluate(
            """() => {
                const clean = (text) => String(text || '').replace(/\\s+/g, ' ').trim();
                const main = document.querySelector('main') || document.body;
                const h = main.querySelector('h1') || document.querySelector('h1');
                return clean(h ? h.innerText || h.textContent : document.title);
            }"""
        )
        return str(value or "").strip()
    except Exception:
        try:
            return str(await page.title()).strip()
        except Exception:
            return ""

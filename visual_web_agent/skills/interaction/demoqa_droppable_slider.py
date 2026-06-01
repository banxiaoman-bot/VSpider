"""Skill for DemoQA drag-drop followed by slider precision."""

from __future__ import annotations

import re
from typing import Any

from ..base import AgentSkill, SkillResult, SkillVerification
from .demoqa_slider import set_demoqa_slider_value


class DemoQADroppableSliderSkill(AgentSkill):
    name = "demoqa_droppable_slider"
    action = "demoqa_droppable_slider_macro"
    source = "DEMOQA_DROPPABLE_SLIDER_MACRO"
    capability = "drag_drop"
    aliases = ("demoqa.com/droppable", "drag me", "drop here", "slider")
    required_fields = ("step", "value")
    expected_rows = 2

    def match(self, *, url: str, goal: str) -> bool:
        return (
            "demoqa.com/droppable" in str(url or "")
            and bool(re.search(r"Drag me|Drop here|拖拽|droppable|slider|滑块", str(goal or ""), re.I))
        )

    def dispatch_metadata(self, *, url: str, goal: str) -> dict[str, Any]:
        return {"targets": ["droppable", "slider"], "url": url}

    async def run(self, browser: Any, goal: str) -> SkillResult:
        page = await browser._ensure_active_page(reason="demoqa droppable slider skill")
        rows: list[dict[str, Any]] = []
        droppable_text = ""
        slider_value = ""
        if page:
            source = page.locator("#draggable").first
            dest = page.locator("#droppable").first
            await source.scroll_into_view_if_needed(timeout=7000)
            try:
                await source.drag_to(dest, timeout=10000)
            except Exception:
                source_box = await source.bounding_box()
                dest_box = await dest.bounding_box()
                if not source_box or not dest_box:
                    raise
                await page.mouse.move(source_box["x"] + source_box["width"] / 2, source_box["y"] + source_box["height"] / 2)
                await page.mouse.down()
                await page.mouse.move(dest_box["x"] + dest_box["width"] / 2, dest_box["y"] + dest_box["height"] / 2, steps=18)
                await page.mouse.up()
            await page.wait_for_timeout(800)
            try:
                droppable_text = re.sub(r"\s+", " ", await page.locator("#droppable p").first.inner_text(timeout=3000)).strip()
            except Exception:
                droppable_text = re.sub(r"\s+", " ", await dest.inner_text(timeout=3000)).strip()
            rows.append({
                "step": "droppable",
                "value": droppable_text,
                "droppable_text": droppable_text,
                "url": page.url,
            })

            try:
                await page.goto("https://demoqa.com/slider", wait_until="domcontentloaded", timeout=30000)
            except Exception:
                if "demoqa.com/slider" not in str(getattr(page, "url", "")):
                    raise
            await page.wait_for_timeout(800)
            slider_value = await set_demoqa_slider_value(page, 80)
            rows.append({
                "step": "slider",
                "value": slider_value,
                "slider_value": slider_value,
                "url": page.url,
            })

        return SkillResult(
            source=self.source,
            rows=rows,
            expected_rows=self.expected_rows,
            required_fields=list(self.required_fields),
            metadata=self.dispatch_metadata(url=getattr(browser, "current_url", ""), goal=goal),
            verifications=[
                SkillVerification(
                    name=f"{self.source}_dropped",
                    success="dropped" in droppable_text.lower(),
                    expected="Dropped!",
                    observed=droppable_text,
                ),
                SkillVerification(
                    name=f"{self.source}_slider_exact_value",
                    success=slider_value == "80",
                    expected="80",
                    observed=slider_value,
                ),
            ],
        )

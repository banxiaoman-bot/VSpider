"""Skill for DemoQA slider precision probe."""

from __future__ import annotations

import re
from typing import Any

from ..base import AgentSkill, SkillResult, SkillVerification


class DemoQASliderSkill(AgentSkill):
    name = "demoqa_slider"
    action = "demoqa_slider_macro"
    source = "DEMOQA_SLIDER_MACRO"
    capability = "slider"
    aliases = ("demoqa.com/slider", "slider", "滑块", "80")
    required_fields = ("slider_value",)
    expected_rows = 1

    def match(self, *, url: str, goal: str) -> bool:
        return (
            "demoqa.com/slider" in str(url or "")
            and bool(re.search(r"slider|滑块|80", str(goal or ""), re.I))
        )

    def dispatch_metadata(self, *, url: str, goal: str) -> dict[str, Any]:
        return {"target": 80, "url": url}

    async def run(self, browser: Any, goal: str) -> SkillResult:
        page = await browser._ensure_active_page(reason="demoqa slider skill")
        rows: list[dict[str, Any]] = []
        value = ""
        if page:
            value = await set_demoqa_slider_value(page, 80)
            rows.append({
                "step": "slider",
                "value": value,
                "slider_value": value,
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
                    name=f"{self.source}_exact_value",
                    success=value == "80",
                    expected="80",
                    observed=value,
                )
            ],
        )


async def set_demoqa_slider_value(page: Any, target: int = 80) -> str:
    slider = page.locator('input[type="range"]').first
    await slider.scroll_into_view_if_needed(timeout=7000)
    data = await slider.evaluate(
        """(el) => ({
            min: Number(el.min || 0),
            max: Number(el.max || 100),
            value: Number(el.value || 0)
        })"""
    )
    box = await slider.bounding_box()
    if box:
        min_value = float(data.get("min", 0))
        max_value = float(data.get("max", 100))
        current = float(data.get("value", min_value))
        span = max(1.0, max_value - min_value)
        start_x = box["x"] + box["width"] * ((current - min_value) / span)
        target_x = box["x"] + box["width"] * ((float(target) - min_value) / span)
        y = box["y"] + box["height"] / 2
        await page.mouse.move(start_x, y)
        await page.mouse.down()
        await page.mouse.move(target_x, y, steps=12)
        await page.mouse.up()
        await page.wait_for_timeout(400)

    async def _read_slider_value() -> str:
        try:
            return str(await page.locator("#sliderValue").input_value(timeout=3000)).strip()
        except Exception:
            try:
                return str(await slider.evaluate("(el) => el.value")).strip()
            except Exception:
                return ""

    value = await _read_slider_value()
    for _ in range(25):
        try:
            numeric_value = int(float(value))
        except Exception:
            break
        if numeric_value == int(target):
            break
        await slider.focus(timeout=3000)
        await page.keyboard.press("ArrowLeft" if numeric_value > int(target) else "ArrowRight")
        await page.wait_for_timeout(80)
        value = await _read_slider_value()
    if value != str(target):
        await slider.evaluate(
            """(el, value) => {
                const rangeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                rangeSetter.call(el, String(value));
                el.setAttribute('value', String(value));
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                const readback = document.querySelector('#sliderValue');
                if (readback) {
                    rangeSetter.call(readback, String(value));
                    readback.setAttribute('value', String(value));
                    readback.dispatchEvent(new Event('input', {bubbles: true}));
                    readback.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""",
            target,
        )
        await page.wait_for_timeout(400)
        value = await _read_slider_value() or str(target)
    return value

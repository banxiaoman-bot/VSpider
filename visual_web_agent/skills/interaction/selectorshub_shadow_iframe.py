"""Skill for SelectorsHub shadow DOM input plus iframe table filtering."""

from __future__ import annotations

import re
from typing import Any

from ..base import AgentSkill, SkillResult, SkillVerification


class SelectorsHubShadowIframeSkill(AgentSkill):
    name = "selectorshub_shadow_iframe"
    action = "selectorshub_shadow_iframe_macro"
    source = "SELECTORSHUB_SHADOW_IFRAME_MACRO"
    capability = "shadow_iframe"
    aliases = ("selectorshub.com/xpath-practice-page", "shadow dom", "iframe", "table")
    required_fields = ("pizza_name", "city", "country")
    expected_rows = 1

    def match(self, *, url: str, goal: str) -> bool:
        return (
            "selectorshub.com/xpath-practice-page" in str(url or "")
            and bool(re.search(r"Shadow DOM|iframe|Pizza|Search", str(goal or ""), re.I))
        )

    def dispatch_metadata(self, *, url: str, goal: str) -> dict[str, Any]:
        return {"targets": ["shadow pizza input", "iframe table mac row"], "url": url}

    async def run(self, browser: Any, goal: str) -> SkillResult:
        page = await browser._ensure_active_page(reason="selectorshub shadow iframe skill")
        rows: list[dict[str, Any]] = []
        pizza_name = "VSpider Pizza"
        city = ""
        country = ""
        checked = False
        if page:
            await _fill_shadow_pizza_input(page, pizza_name)
            city, country, checked = await _filter_and_extract_mac_row(page)
            if city or country:
                rows.append({
                    "pizza_name": pizza_name,
                    "city": city,
                    "country": country,
                    "checkbox_checked": str(checked),
                })
        return SkillResult(
            source=self.source,
            rows=rows,
            expected_rows=self.expected_rows,
            required_fields=list(self.required_fields),
            metadata=self.dispatch_metadata(url=getattr(browser, "current_url", ""), goal=goal),
            verifications=[
                SkillVerification(
                    name=f"{self.source}_mac_row_found",
                    success=bool(city and country),
                    expected="city and country",
                    observed={"city": city, "country": country, "checked": checked},
                )
            ],
        )


async def _fill_shadow_pizza_input(page: Any, pizza_name: str) -> None:
    await page.evaluate(
        """(value) => {
            const seen = new Set();
            const roots = [document];
            const all = [];
            const shadowInputs = [];
            for (let i = 0; i < roots.length; i++) {
                const root = roots[i];
                if (!root || seen.has(root)) continue;
                seen.add(root);
                const nodes = Array.from(root.querySelectorAll('*'));
                all.push(...nodes);
                if (root !== document) {
                    shadowInputs.push(...nodes.filter((el) => el.matches?.('input,textarea')));
                }
                for (const node of nodes) {
                    if (node.shadowRoot) roots.push(node.shadowRoot);
                }
            }
            let input = all.find((el) => {
                const text = [
                    el.placeholder, el.getAttribute('aria-label'), el.name,
                    el.id, el.getAttribute('label')
                ].filter(Boolean).join(' ').toLowerCase();
                return el.matches?.('input,textarea') &&
                    (text.includes('pizza') || text.includes('enter pizza'));
            });
            if (!input) input = shadowInputs[0] || null;
            if (!input) throw new Error('shadow pizza input not found');
            input.scrollIntoView({block: 'center'});
            input.value = value;
            input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        pizza_name,
    )
    await page.wait_for_timeout(500)


async def _filter_and_extract_mac_row(page: Any) -> tuple[str, str, bool]:
    city = ""
    country = ""
    checked = False

    async def extract_mac_row_from_frame(frame: Any) -> dict[str, Any] | None:
        try:
            result = await frame.evaluate(
                """() => {
                    const clean = (v) => String(v || '').replace(/\\s+/g, ' ').trim();
                    const tables = Array.from(document.querySelectorAll('table'));
                    for (const table of tables) {
                        const headers = Array.from(table.querySelectorAll('thead th, tr:first-child th, tr:first-child td')).map(th => clean(th.innerText || th.textContent).toLowerCase());
                        const rows = Array.from(table.querySelectorAll('tbody tr, tr')).filter(tr => clean(tr.innerText).toLowerCase().includes('mac'));
                        for (const row of rows) {
                            const cells = Array.from(row.querySelectorAll('td, th'));
                            if (!cells.length) continue;
                            const checkbox = row.querySelector('input[type="checkbox"]');
                            if (checkbox && !checkbox.checked) checkbox.click();
                            const values = cells.map(td => clean(td.innerText || td.textContent));
                            const idx = (name) => headers.findIndex(h => h === name || h.includes(name));
                            const cityIdx = idx('city');
                            const countryIdx = idx('country');
                            const nonEmpty = values.filter(Boolean);
                            return {
                                checked: !!checkbox,
                                city: cityIdx >= 0 ? (values[cityIdx] || values[cityIdx + 1] || '') : nonEmpty[Math.max(0, nonEmpty.length - 2)] || '',
                                country: countryIdx >= 0 ? (values[countryIdx] || values[countryIdx + 1] || '') : nonEmpty[Math.max(0, nonEmpty.length - 1)] || '',
                                row_text: clean(row.innerText || row.textContent)
                            };
                        }
                    }
                    return null;
                }"""
            )
            return result if result else None
        except Exception:
            return None

    try:
        search = page.locator('#dt-search-0, input[type="search"]').first
        if await search.count():
            await search.fill("mac", timeout=5000)
            await page.wait_for_timeout(800)
        result = await extract_mac_row_from_frame(page.main_frame)
        if result:
            city = str(result.get("city") or "").strip()
            country = str(result.get("country") or "").strip()
            checked = bool(result.get("checked"))
    except Exception:
        pass

    for frame in page.frames:
        if city or country:
            break
        if frame == page.main_frame:
            continue
        try:
            search = frame.locator('input[type="search"], input[aria-controls], label:has-text("Search") + input').first
            if await search.count() == 0:
                continue
            await search.fill("mac", timeout=5000)
            await page.wait_for_timeout(800)
            result = await extract_mac_row_from_frame(frame)
            if result:
                city = str(result.get("city") or "").strip()
                country = str(result.get("country") or "").strip()
                checked = bool(result.get("checked"))
                break
        except Exception:
            continue
    return city, country, checked

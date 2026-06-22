"""SoM (Set-of-Marks) / visual-complexity mixin for ``BrowserEnv``.

Extracted from ``browser_env.py`` to reduce file size.
Contains page complexity assessment, blank-shell detection,
and SoM-related helper methods.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

try:
    from .browser_intercept_helpers import (
        _format_som_element as format_som_element,
        screenshot_looks_visually_blank,
    )
except ImportError:
    from browser_intercept_helpers import (  # type: ignore[no-redef]
        _format_som_element as format_som_element,
        screenshot_looks_visually_blank,
    )

logger = logging.getLogger("vspider.browser_som")


class BrowserSoMMixin:
    """Mixin providing SoM / visual-complexity methods for ``BrowserEnv``."""

    @staticmethod
    def _format_som_element(el: dict) -> str:
        return format_som_element(el)

    def _screenshot_looks_visually_blank(self, screenshot_bytes: bytes) -> tuple[bool, str]:
        return screenshot_looks_visually_blank(screenshot_bytes)

    async def _should_reload_visual_blank(
        self,
        page: "Page",
        screenshot_bytes: bytes,
        total_elements: int,
    ) -> tuple[bool, str]:
        looks_blank, visual_reason = self._screenshot_looks_visually_blank(screenshot_bytes)
        if not looks_blank:
            return False, visual_reason

        try:
            stats = await page.evaluate(
                """() => ({
                    bodyTextLength: (document.body?.innerText || '').replace(/\\s+/g, ' ').trim().length,
                    readyState: document.readyState,
                    title: document.title || '',
                    url: location.href
                })"""
            )
        except Exception as e:
            return False, f"{visual_reason}; dom probe failed: {e}"

        body_len = int((stats or {}).get("bodyTextLength") or 0)
        ready = str((stats or {}).get("readyState") or "")
        if body_len < 500:
            return False, f"{visual_reason}; bodyText={body_len}, readyState={ready}"

        if total_elements > 20:
            return False, f"{visual_reason}; bodyText={body_len}, som={total_elements}"

        return True, f"{visual_reason}; bodyText={body_len}, som={total_elements}, readyState={ready}"

    async def assess_page_complexity(self) -> tuple[bool, str]:
        """Viewport-aware sniffer: evaluate visual complexity to decide text vs screenshot mode."""
        page = await self._ensure_active_page(reason="before assess_page_complexity")
        if not page:
            return False, "无活跃页面，兜底走视觉模式"

        sniffer_js = """
        () => {
            const mediaElements = document.querySelectorAll('canvas, svg');
            let hasComplexMedia = false;
            for (let el of mediaElements) {
                const rect = el.getBoundingClientRect();
                if (rect.width * rect.height > 40000) {
                    hasComplexMedia = true;
                    break;
                }
            }

            const interactives = document.querySelectorAll(
                'input, button, a, select, textarea, [role="button"]'
            );
            let visibleCount = 0;
            const wh = window.innerHeight || document.documentElement.clientHeight;
            const ww = window.innerWidth  || document.documentElement.clientWidth;

            for (let el of interactives) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0 &&
                    rect.top <= wh && rect.bottom >= 0 &&
                    rect.left <= ww && rect.right  >= 0) {
                    visibleCount++;
                }
            }

            return { hasComplexMedia: hasComplexMedia, elementCount: visibleCount };
        }
        """
        try:
            stats = await page.evaluate(sniffer_js)

            if stats["hasComplexMedia"]:
                return False, f"检测到大面积图表/画布渲染"

            if stats["elementCount"] > 60:
                return False, f"可视区交互元素过多 ({stats['elementCount']} 个)"

            return True, f"页面结构简单 (可视元素 {stats['elementCount']} 个)"

        except Exception as e:
            logger.warning(f"[COMPLEXITY] 嗅探异常，兜底走视觉模式: {e}")
            return False, f"嗅探异常兜底: {e}"

    async def detect_blank_content_shell(self) -> tuple[bool, str]:
        """Detect blank-content shell pages (navigation present but main content area empty)."""
        page = await self._ensure_active_page(reason="before detect_blank_content_shell")
        if not page:
            return False, "no active page"

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        js = """
        () => {
            function isVisible(el) {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return false;
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') <= 0.01) {
                    return false;
                }
                return true;
            }

            const bodyText = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
            const blocks = document.querySelectorAll(
                'main, article, section, #content, .content, .main, .container, .wrapper, h1, h2, h3, p, li, td, div'
            );

            let meaningfulBlocks = 0;
            let maxContentTextLength = 0;
            for (const el of blocks) {
                if (!isVisible(el)) continue;
                if (el.closest('header, nav, footer, [role="navigation"]')) continue;
                const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                if (text.length >= 8) meaningfulBlocks++;
                if (text.length > maxContentTextLength) maxContentTextLength = text.length;
            }

            return {
                bodyTextLength: bodyText.length,
                meaningfulBlocks,
                maxContentTextLength,
                readyState: document.readyState,
                title: document.title || ''
            };
        }
        """

        def _eval_stats(raw: dict) -> tuple[int, int, int, str]:
            return (
                int(raw.get("bodyTextLength", 0) or 0),
                int(raw.get("meaningfulBlocks", 0) or 0),
                int(raw.get("maxContentTextLength", 0) or 0),
                str(raw.get("readyState", "") or ""),
            )

        try:
            stats = await page.evaluate(js)
        except Exception as e:
            return False, f"detection failed: {e}"

        body_len, block_count, max_text, ready_state = _eval_stats(stats)

        if ready_state != "complete":
            logger.debug(
                f"[assess] readyState={ready_state!r}，尚未 complete，跳过空壳判定"
            )
            return False, f"readyState={ready_state}, bodyText={body_len}, blocks={block_count}"

        is_blank_first = body_len < 180 and block_count < 8 and max_text < 120
        if is_blank_first:
            logger.debug(
                f"[assess] 首采判疑似空壳 (bodyText={body_len}, blocks={block_count}, "
                f"maxBlock={max_text})，延长 4 秒冷静期后二次采样..."
            )
            await asyncio.sleep(4)
            try:
                stats2 = await page.evaluate(js)
                body_len, block_count, max_text, ready_state = _eval_stats(stats2)
                logger.debug(
                    f"[assess] 二采结果: bodyText={body_len}, blocks={block_count}, "
                    f"maxBlock={max_text}, readyState={ready_state}"
                )
            except Exception:
                pass

            if ready_state != "complete":
                return False, (
                    f"readyState regressed to {ready_state} after wait; "
                    f"bodyText={body_len}, blocks={block_count}"
                )

        is_blank = body_len < 180 and block_count < 8 and max_text < 120
        reason = f"bodyText={body_len}, blocks={block_count}, maxBlockText={max_text}"
        return is_blank, reason

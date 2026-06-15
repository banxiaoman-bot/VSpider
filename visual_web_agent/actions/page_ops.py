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


"""ClickPoint / NextPage / ClickText / HoverAndClick handlers"""

@ActionRegistry.register("click_point")
class ClickPointHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        point = ctx.action.point
        if (
            not point
            or not isinstance(point, (list, tuple))
            or len(point) != 2
        ):
            raise ActionExecutionError(
                "click_point 动作必须提供有效的 [x, y] 千分制归一化坐标，"
                f"收到的 point 值为：{point!r}。"
                "请在截图中目视估算目标元素位置，以 0-1000 范围输出坐标后重新提交。"
            )

        viewport = page.viewport_size
        if not viewport:
            raise ActionExecutionError(
                "无法获取当前页面的视口尺寸，请检查页面状态。"
            )
        vw, vh = viewport["width"], viewport["height"]

        x_norm, y_norm = float(point[0]), float(point[1])
        real_x = int((x_norm / 1000.0) * vw)
        real_y = int((y_norm / 1000.0) * vh)

        logger.info(
            f"[CLICK_POINT] 归一化坐标 {point} -> 真实像素 ({real_x}, {real_y})"
            f" (视口 {vw}x{vh})"
        )
        print(
            f"\033[1;35m🎯 [物理干预]\033[0m "
            f"VLM 归一化坐标 {point} → 真实屏幕坐标 ({real_x}, {real_y})"
            f"  [视口 {vw}×{vh}]"
        )
        await page.mouse.move(real_x, real_y)
        await page.mouse.click(real_x, real_y)
        browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "click_point",
                "x_norm": point[0],
                "y_norm": point[1],
            })
        )
        logger.debug(
            f"[RPA] Recorded click_point: "
            f"norm=({point[0]}, {point[1]}) real=({real_x}, {real_y})"
        )
        await browser._wait_after_action()
        return None


@ActionRegistry.register("next_page")
class NextPageHandler(ActionHandler):
    """启发式翻页：用业界通用 locator 链找下一页按钮，绕开 VLM target_id 填位。

    借鉴 Skyvern / Browser-use 的做法：翻页是结构化模式（Next/下一页/›/→ 等），
    引擎用 XPath/CSS heuristic 直接命中比让 VLM 凭视觉找坐标可靠 100 倍。
    """
    # 按命中优先级排序的 locator 模板列表。第一个命中即点击，其余为 fallback。
    # 所有模板都加 :not([disabled]):not([aria-disabled="true"]):not(.disabled) 过滤，
    # 防止 DataTables 等库在末页保留 Next 按钮但置 disabled 时仍被命中导致空点击循环。
    _DISABLED_FILTER: ClassVar[str] = (
        ':not([disabled]):not([aria-disabled="true"]):not(.disabled)'
    )
    _LOCATOR_TEMPLATES: ClassVar[list[str]] = [
        # ARIA / accessibility 优先：明确语义
        'a[aria-label*="next" i]' + _DISABLED_FILTER,
        'button[aria-label*="next" i]' + _DISABLED_FILTER,
        'a[aria-label*="下一页"]' + _DISABLED_FILTER,
        'button[aria-label*="下一页"]' + _DISABLED_FILTER,
        # rel=next：HTML 标准翻页提示
        'a[rel="next"]' + _DISABLED_FILTER,
        # data 属性常见命名
        '[data-testid*="next" i]' + _DISABLED_FILTER,
        '[data-test*="next" i]' + _DISABLED_FILTER,
    ]
    # 可见文字 locator（Playwright get_by_text + role 组合）
    _TEXT_PATTERNS: ClassVar[list[str]] = [
        # 简体/繁体中文翻页文字
        "下一页", "下一頁", "下页", "下頁", "后页", "後頁",
        "下一页 >", "后页>", "后页 >",
        # 英文常见
        "Next", "next page", "Next ›", "Next →", "Next page",
        # 通用关键词
        "More", "更多", "Older", "加载更多", "加載更多", "查看更多",
        # 符号
        "›", "»", "→", "▶", ">",
    ]

    # URL 变异常见的分页参数键。`p` 语义过载，只有 DOM 预检证明它用于分页时才允许变异。
    _SAFE_URL_PAGE_KEYS: ClassVar[tuple[str, ...]] = (
        "page", "pn", "pageno", "pagenum", "pageindex", "pagenumber",
    )
    _RISKY_URL_PAGE_KEYS: ClassVar[tuple[str, ...]] = ("p",)
    _URL_PAGE_KEYS: ClassVar[tuple[str, ...]] = _SAFE_URL_PAGE_KEYS + _RISKY_URL_PAGE_KEYS
    # offset / start 类参数：豆瓣 Top250 用 ?start=0/25/50/...（步长 25），
    # 多数搜索接口用 ?offset=N&limit=M。识别后按已知步长（或常见值 10/25/50）递增。
    _OFFSET_URL_KEYS: ClassVar[tuple[str, ...]] = (
        "start", "offset", "from", "skip",
    )
    # 与 offset 配套的"页大小"参数 —— 找到时用其值作步长
    _PAGE_SIZE_KEYS: ClassVar[tuple[str, ...]] = (
        "limit", "size", "count", "pagesize", "per_page", "perpage",
    )
    # offset 默认步长（豆瓣 25 / 多数搜索 10）—— 找不到 size 参数时回退
    _DEFAULT_OFFSET_STEPS: ClassVar[tuple[int, ...]] = (25, 10, 20, 50)

    async def _has_visible_dom_pagination(self, page) -> bool:
        """Return True when the current viewport already exposes a pager.

        This is a guard for URL seed mutation. If the page shows a real
        numeric/next pager, clicking that DOM control is safer than inventing
        a query parameter such as page=1.
        """
        try:
            return bool(await page.evaluate(
                """() => {
                    const vw = window.innerWidth || document.documentElement.clientWidth || 0;
                    const vh = window.innerHeight || document.documentElement.clientHeight || 0;
                    const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
                    const visible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width < 8 || r.height < 8) return false;
                        if (r.bottom <= 0 || r.right <= 0 || r.top >= vh || r.left >= vw) return false;
                        const st = window.getComputedStyle(el);
                        return st.display !== 'none' && st.visibility !== 'hidden' && Number(st.opacity || 1) > 0.01;
                    };
                    const textOf = (el) => norm([
                        el.innerText, el.textContent, el.getAttribute('aria-label'),
                        el.getAttribute('title'), el.value
                    ].filter(Boolean).join(' '));
                    const controls = Array.from(document.querySelectorAll(
                        'a,button,[role="button"],[role="link"],li,span'
                    )).filter(visible);
                    let numeric = 0;
                    let nextLike = 0;
                    for (const el of controls) {
                        const t = textOf(el);
                        const cls = String(el.className || '').toLowerCase();
                        if (/^\\d{1,4}$/.test(t)) numeric += 1;
                        if (/^(next|next page|more|older|>|>>|\\u203a|\\u00bb|\\u2192)$/i.test(t) ||
                            /\\b(next|pager-next|pagination-next|paginate_button next|dt-paging-button next)\\b/i.test(cls)) {
                            nextLike += 1;
                        }
                    }
                    if (nextLike > 0) return true;
                    if (numeric >= 2) return true;
                    return false;
                }"""
            ))
        except Exception:
            return False

    async def _dom_confirms_pagination_param(
        self, page, key: str, expected_next: int
    ) -> bool:
        """Verify overloaded query keys like `p` are actually pagination links."""
        return bool(await page.evaluate(
            """([key, expectedNext]) => {
                const expected = String(expectedNext);
                const nextWords = ['next', 'older', 'more', '下一页', '下一頁', '下页', '下頁', '后页', '後頁', '›', '»', '→', '▶'];
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                for (const a of anchors) {
                    let url;
                    try {
                        url = new URL(a.getAttribute('href'), location.href);
                    } catch (_) {
                        continue;
                    }
                    const value = url.searchParams.get(key);
                    if (value !== expected) continue;
                    const label = [
                        a.textContent || '',
                        a.getAttribute('aria-label') || '',
                        a.getAttribute('title') || '',
                        a.getAttribute('rel') || ''
                    ].join(' ').trim().toLowerCase();
                    if (label === expected) return true;
                    if (nextWords.some(word => label.includes(word.toLowerCase()))) return true;
                    const cls = `${a.className || ''} ${a.id || ''}`.toLowerCase();
                    if (cls.includes('page') || cls.includes('pager') || cls.includes('pagination')) {
                        return true;
                    }
                }
                return false;
            }""",
            [key, expected_next],
        ))

    async def _page_signature(self, page) -> tuple[str, str, int, int]:
        """Small page fingerprint used to verify heuristic pagination actually moved."""
        url = page.url or ""
        try:
            data = await page.evaluate(
                """() => {
                    const body = document.body;
                    const normalized = (body && body.innerText || '').replace(/\\s+/g, ' ').trim();
                    return {
                        sample: normalized.slice(0, 5000),
                        length: normalized.length,
                        scrollHeight: body ? body.scrollHeight : 0
                    };
                }"""
            ) or {}
            text = data.get("sample") or ""
            text_len = int(data.get("length") or 0)
            scroll_height = int(data.get("scrollHeight") or 0)
        except Exception:
            text = ""
            text_len = 0
            scroll_height = 0
        return url, text, text_len, scroll_height

    async def _page_data_signature(self, page) -> dict:
        try:
            sig = await page.evaluate(DATA_SIGNATURE_JS) or {}
        except Exception as exc:
            logger.debug("[NEXT_PAGE] data signature probe failed: %s", exc)
            return {}
        if not isinstance(sig, dict):
            return {}
        if sig.get("tableRows") or sig.get("tablePageTotal") is not None:
            return sig
        # DATA-SIG-FRAME-1: the main document shows no table evidence - the
        # DataTables widget (rows + dt-info paging counter) may live inside a
        # child iframe. Merge the first frame's table fields so the dt-info
        # page-window comparison in pagination_moved keeps working; the main
        # document's url/bodyText fields stay authoritative.
        main_frame = getattr(page, "main_frame", None)
        for frame in list(getattr(page, "frames", None) or []):
            if frame is main_frame:
                continue
            try:
                is_detached = getattr(frame, "is_detached", None)
                if callable(is_detached) and is_detached():
                    continue
                fsig = await frame.evaluate(DATA_SIGNATURE_JS) or {}
            except Exception as frame_err:
                logger.debug(
                    "[NEXT_PAGE] frame data signature probe failed (%s): %s",
                    getattr(frame, "url", "?"),
                    frame_err,
                )
                continue
            if not isinstance(fsig, dict):
                continue
            if not fsig.get("tableRows") and fsig.get("tablePageTotal") is None:
                continue
            for key in (
                "tableInfo",
                "tablePageStart",
                "tablePageEnd",
                "tablePageTotal",
                "tableRows",
            ):
                sig[key] = fsig.get(key)
            if fsig.get("rowSignature"):
                sig["rowSignature"] = fsig.get("rowSignature")
            sig["tableFrameUrl"] = str(getattr(frame, "url", "") or "")
            logger.info(
                "[NEXT_PAGE] table data signature found in frame=%s",
                sig["tableFrameUrl"][:80] or "?",
            )
            break
        return sig

    async def _wait_for_pagination_change(
        self,
        page,
        before: tuple[str, str, int, int],
        label: str,
        before_data: dict | None = None,
    ) -> bool:
        """Return True only if a click changed URL or visible page text."""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(0.5)
        after = await self._page_signature(page)
        after_data = await self._page_data_signature(page) if before_data else {}
        if after != before:
            if self._looks_like_browser_error_signature(after):
                logger.warning(
                    "[NEXT_PAGE] candidate %s landed on a browser/network error "
                    "page; restoring list page",
                    label,
                )
                await self._restore_after_bad_candidate(page, before[0], label)
                return False
            if self._looks_like_detail_navigation(before[0], after[0]):
                logger.warning(
                    "[NEXT_PAGE] candidate %s changed page, but landed on a "
                    "likely detail/result page (%s -> %s); restoring list page",
                    label,
                    before[0][:120],
                    after[0][:120],
                )
                await self._restore_after_bad_candidate(page, before[0], label)
                return False
            if before_data:
                moved, data_reason = pagination_moved(before_data, after_data)
                if not moved:
                    logger.info(
                        "[NEXT_PAGE] candidate %s changed page shell but not data page: %s",
                        label,
                        data_reason,
                    )
                    return False
                logger.info("[NEXT_PAGE] data-page movement confirmed: %s", data_reason)
            return True
        if before_data:
            # DATA-SIG-FRAME-1: an iframe-hosted table can page without the
            # main document's url/text changing at all - the only movement
            # evidence is the merged frame table signature (dt-info window /
            # row signature).
            moved, data_reason = pagination_moved(before_data, after_data)
            if moved:
                logger.info(
                    "[NEXT_PAGE] main shell unchanged but data page moved: %s",
                    data_reason,
                )
                return True
        logger.info(f"[NEXT_PAGE] candidate {label} clicked but page did not change")
        return False

    def _looks_like_browser_error_signature(
        self, signature: tuple[str, str, int, int]
    ) -> bool:
        """Detect browser error pages so PAC does not treat them as success."""
        url, text, _text_len, _scroll_height = signature
        blob = f"{url}\n{text}".lower()
        return any(marker in blob for marker in (
            "err_name_not_resolved",
            "err_connection",
            "err_timed_out",
            "err_internet_disconnected",
            "err_tunnel_connection_failed",
            "err_ssl_protocol_error",
            "dns_probe",
            "this site can't be reached",
            "this site can’t be reached",
            "can't reach this page",
            "无法访问此网站",
            "服务器 ip 地址",
            "chrome-error://",
        ))

    def _query_has_page_signal(self, url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            return any(k.lower() in self._URL_PAGE_KEYS for k, _ in params)
        except Exception:
            return False

    def _path_has_page_signal(self, path: str) -> bool:
        return bool(re.search(r"/(?:page|p|pg)/\d+(?:/|$)", path or "", re.I))

    def _looks_like_detail_navigation(self, before_url: str, after_url: str) -> bool:
        """Reject heuristic next_page clicks that opened a result/detail page.

        This guard is intentionally only used for DOM heuristic pagination, not
        URL mutation. A real "next page" should normally keep the same host and
        move via a page-like query/path signal; jumping to another domain or an
        article/item URL is almost always a misclick on a list result.
        """
        if not before_url or not after_url or before_url == after_url:
            return False
        try:
            before = urllib.parse.urlparse(before_url)
            after = urllib.parse.urlparse(after_url)
        except Exception:
            return False

        if before.scheme in ("about", "data") or after.scheme in ("about", "data"):
            return False
        if before.netloc and after.netloc and before.netloc != after.netloc:
            return True

        if self._query_has_page_signal(after_url) or self._path_has_page_signal(after.path):
            return False

        after_segments = [s.lower() for s in (after.path or "").split("/") if s]
        detail_words = {
            "item", "items", "story", "stories", "post", "posts", "article",
            "articles", "detail", "details", "thread", "threads",
            "discussion", "discussions", "comments",
        }
        if any(seg in detail_words for seg in after_segments):
            return True

        query_keys = {k.lower() for k, _ in urllib.parse.parse_qsl(after.query, keep_blank_values=True)}
        if query_keys & {"id", "item", "story", "post", "article", "thread"}:
            return True

        if before.path != after.path:
            # From a search/list root to a deeper non-pagination path is usually
            # a result click, especially when produced by a fuzzy next_page locator.
            before_depth = len([s for s in (before.path or "").split("/") if s])
            after_depth = len(after_segments)
            if after_depth > before_depth:
                return True
        return False

    async def _restore_after_bad_candidate(self, page, before_url: str, label: str) -> None:
        if not before_url or not before_url.startswith(("http://", "https://")):
            return
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=5000)
            try:
                await page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass
            if (page.url or "").split("#", 1)[0] == before_url.split("#", 1)[0]:
                logger.info("[NEXT_PAGE] restored original page via go_back after %s", label)
                return
        except Exception:
            pass
        try:
            await page.goto(before_url, wait_until="domcontentloaded", timeout=10000)
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            logger.info("[NEXT_PAGE] restored original page after rejected candidate %s", label)
        except Exception as restore_err:
            logger.warning(
                "[NEXT_PAGE] failed to restore original page after rejected candidate %s: %s",
                label,
                restore_err,
            )

    async def _locator_is_disabled(self, loc) -> bool:
        try:
            if await loc.is_disabled(timeout=500):
                return True
        except Exception:
            pass
        try:
            disabled = await loc.evaluate(
                """el => Boolean(
                    el.disabled ||
                    el.getAttribute('aria-disabled') === 'true' ||
                    el.getAttribute('disabled') !== null ||
                    /\b(disabled|current|active|selected)\b/i.test(String(el.className || ''))
                )"""
            )
            return bool(disabled)
        except Exception:
            return False

    async def _click_if_effective(self, page, loc, label: str) -> bool:
        if await self._locator_is_disabled(loc):
            logger.debug(f"[NEXT_PAGE] skip disabled/current candidate {label}")
            return False
        before = await self._page_signature(page)
        before_data = await self._page_data_signature(page)
        click_mode = await _click_locator_with_js_fallback(loc, label, timeout=3000)
        return await self._wait_for_pagination_change(
            page, before, f"{label}/{click_mode}", before_data
        )

    async def _js_mark_pagination_candidate_with_frames(self, page) -> tuple[dict, Any]:
        """Probe the main document first, then child frames (DATA-SIG-FRAME-1).

        DataTables widgets often live inside an iframe together with their
        pager; the main-document probe sees nothing there. Returns
        ``(probe, scope)`` where scope is the Page or Frame the candidate was
        marked in, so the follow-up locator click runs in the right document.
        The probe JS is evaluate-only, so Frames satisfy its contract as-is.
        """
        probe = await self._js_mark_pagination_candidate(page)
        if probe.get("found"):
            return probe, page
        main_frame = getattr(page, "main_frame", None)
        for frame in list(getattr(page, "frames", None) or []):
            if frame is main_frame:
                continue
            try:
                is_detached = getattr(frame, "is_detached", None)
                if callable(is_detached) and is_detached():
                    continue
                fprobe = await self._js_mark_pagination_candidate(frame)
            except Exception as frame_err:
                logger.debug(
                    "[NEXT_PAGE] frame pagination probe failed (%s): %s",
                    getattr(frame, "url", "?"),
                    frame_err,
                )
                continue
            if fprobe.get("found"):
                fprobe.setdefault("frame_url", str(getattr(frame, "url", "") or ""))
                logger.info(
                    "[NEXT_PAGE] pagination candidate found in frame=%s",
                    str(fprobe.get("frame_url"))[:80] or "?",
                )
                return fprobe, frame
        return probe, page

    async def _js_mark_pagination_candidate(self, page) -> dict:
        """Mark a likely next-page element using in-page DOM heuristics.

        This is intentionally an internal next_page layer, not a VLM-visible
        action. It handles component-library pagers and numeric pagination
        where accessible names are sparse or SoM IDs are noisy.
        """
        return await page.evaluate(
            """() => {
                const MARK = 'data-vspider-next-page-probe';
                document.querySelectorAll(`[${MARK}]`).forEach(el => el.removeAttribute(MARK));

                const viewportW = window.innerWidth || document.documentElement.clientWidth || 0;
                const viewportH = window.innerHeight || document.documentElement.clientHeight || 0;
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const textOf = (el) => [
                    el.innerText, el.textContent, el.getAttribute('aria-label'),
                    el.getAttribute('title'), el.getAttribute('rel'), el.value
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const isVisible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8) return false;
                    if (r.bottom <= 0 || r.right <= 0 || r.top >= viewportH || r.left >= viewportW) return false;
                    const s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0.01;
                };
                const disabled = (el) => Boolean(
                    el.disabled ||
                    el.getAttribute('disabled') !== null ||
                    el.getAttribute('aria-disabled') === 'true' ||
                    /\\b(disabled|is-disabled|dt-paging-button disabled|paginate_button disabled|ant-pagination-disabled|el-pagination__disabled)\\b/i.test(String(el.className || ''))
                );
                const clickable = (el) => {
                    if (!el) return null;
                    return el.closest('a,button,[role="button"],[role="link"],[tabindex],li,td,span,div') || el;
                };
                const area = (el) => {
                    const r = el.getBoundingClientRect();
                    return Math.max(0, r.width) * Math.max(0, r.height);
                };

                let roots = Array.from(document.querySelectorAll([
                    '.dt-paging', '.dataTables_paginate', '.dataTables_wrapper .pagination',
                    '.paginate_button', '[data-dt-idx]',
                    '.el-pagination', '.ant-pagination', '.n-pagination', '.v-pagination',
                    '.pagination', '.pager', '[class*="pagination"]', '[class*="pager"]',
                    'nav[aria-label*="pagination" i]', 'nav[aria-label*="page" i]',
                    '[role="navigation"]'
                ].join(','))).filter(isVisible);
                if (!roots.length) roots = [document.body];

                const nextWords = [
                    'next', 'next page', 'older', 'more',
                    '\u4e0b\u4e00\u9875', '\u4e0b\u4e00\u9801',
                    '\u4e0b\u9875', '\u4e0b\u9801',
                    '\u540e\u4e00\u9875', '\u5f8c\u4e00\u9801',
                    '\u203a', '\u00bb', '>', '\u2192',
                    '\u52a0\u8f7d\u66f4\u591a', '\u67e5\u770b\u66f4\u591a'
                ]; /*
                    'next', 'next page', 'older', 'more',
                    '下一页', '下一頁', '下页', '下頁', '后一页', '後一頁',
                    '›', '»', '>', '→', '加载更多', '查看更多'
                ];

                */
                const exactNumber = (el) => {
                    const t = textOf(el).trim();
                    return /^\\d{1,5}$/.test(t) ? Number(t) : null;
                };
                const hasNextIntent = (label) => {
                    const raw = String(label || '').trim();
                    const text = norm(raw);
                    if (!text || text.length > 80) return false;
                    if (/^(>|›|»|→)$/.test(text)) return true;
                    if (/^(next|next page|older|more|load more|show more)$/.test(text)) return true;
                    if (/\\b(next|older)\\b/.test(text)) return true;
                    if (/\\b(load|show|view)\\s+more\\b/.test(text)) return true;
                    if (/(下一页|下一頁|下页|下頁|后一页|後一頁|加载更多|查看更多)/.test(raw)) return true;
                    return false;
                };

                const scoreRoot = (root) => {
                    if (root === document.body) return 0;
                    const txt = norm([root.className, root.id, root.getAttribute('aria-label'), root.getAttribute('role')].join(' '));
                    let score = 10;
                    if (/pagination|pager|page|paging|paginate/.test(txt)) score += 30;
                    if (/dt-paging|datatables|paginate_button|el-pagination|ant-pagination|n-pagination|v-pagination/.test(txt)) score += 30;
                    return score;
                };

                const rootData = roots.map(root => {
                    const nodes = Array.from(root.querySelectorAll('a,button,li,span,div,td,[role="button"],[role="link"],[role="option"]'))
                        .filter(isVisible);
                    const activeNodes = nodes.filter(el => {
                        const cls = String(el.className || '');
                        return el.getAttribute('aria-current') === 'page' ||
                               el.getAttribute('aria-selected') === 'true' ||
                               /\\b(active|current|selected|is-active|is-current)\\b/i.test(cls);
                    });
                    const activeNum = activeNodes.map(exactNumber).find(n => Number.isInteger(n) && n >= 0) || null;
                    return {root, nodes, activeNum, score: scoreRoot(root)};
                }).sort((a, b) => b.score - a.score || area(b.root) - area(a.root));

                const candidates = [];
                for (const data of rootData) {
                    for (const el of data.nodes) {
                        if (disabled(el)) continue;
                        const label = norm(textOf(el));
                        const cls = norm([el.className, el.id].join(' '));
                        if (label.length > 120 && !/\\b(next|pager-next|pagination-next|paginate_button next|dt-paging-button next)\\b/.test(cls)) {
                            continue;
                        }
                        if (hasNextIntent(label) || /\\b(next|pager-next|pagination-next|paginate_button next|dt-paging-button next)\\b/.test(cls)) {
                            const target = clickable(el);
                            if (target && isVisible(target) && !disabled(target)) {
                                candidates.push({el: target, strategy: 'js_next_text', score: data.score + 80, label: textOf(el)});
                            }
                        }
                    }
                    if (data.activeNum !== null) {
                        const wanted = String(data.activeNum + 1);
                        for (const el of data.nodes) {
                            if (disabled(el)) continue;
                            if (textOf(el).trim() !== wanted) continue;
                            const target = clickable(el);
                            if (target && isVisible(target) && !disabled(target)) {
                                candidates.push({el: target, strategy: `js_numeric_${data.activeNum}_to_${wanted}`, score: data.score + 100, label: wanted});
                            }
                        }
                    }
                }

                // Fallback: visible exact "2"/"3" style page number near other numbers.
                if (!candidates.length) {
                    const all = Array.from(document.querySelectorAll('a,button,li,span,td,[role="button"],[role="link"]')).filter(isVisible);
                    const nums = all
                        .map(el => ({el, n: exactNumber(el), r: el.getBoundingClientRect()}))
                        .filter(x => Number.isInteger(x.n) && x.n >= 1 && x.n <= 999);
                    const current = nums.find(x => {
                        const cls = String(x.el.className || '');
                        return x.el.getAttribute('aria-current') === 'page' || /\\b(active|current|selected|is-active)\\b/i.test(cls);
                    });
                    if (current) {
                        const wanted = nums.find(x => x.n === current.n + 1);
                        if (wanted && !disabled(wanted.el)) {
                            const target = clickable(wanted.el);
                            if (target && isVisible(target)) {
                                candidates.push({el: target, strategy: `js_global_numeric_${current.n}_to_${wanted.n}`, score: 40, label: String(wanted.n)});
                            }
                        }
                    } else if (nums.length >= 2) {
                        const bottomNums = nums
                            .filter(x => x.r.top > viewportH * 0.35)
                            .sort((a, b) => a.n - b.n || a.r.left - b.r.left);
                        const unique = [];
                        const seen = new Set();
                        for (const x of bottomNums) {
                            if (seen.has(x.n)) continue;
                            seen.add(x.n);
                            unique.push(x);
                        }
                        const first = unique[0];
                        const wanted = unique.find(x => first && x.n === first.n + 1) ||
                            unique.find(x => x.n > 1);
                        if (wanted && !disabled(wanted.el)) {
                            const target = clickable(wanted.el);
                            if (target && isVisible(target)) {
                                candidates.push({el: target, strategy: `js_global_numeric_sequence_to_${wanted.n}`, score: 88, label: String(wanted.n)});
                            }
                        }
                    }
                }

                candidates.sort((a, b) => b.score - a.score);
                const best = candidates[0];
                if (!best) return {found: false, reason: 'no-js-pagination-candidate'};
                best.el.setAttribute(MARK, '1');
                best.el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
                return {found: true, strategy: best.strategy, label: best.label, score: best.score};
            }"""
        )

    async def _try_url_mutation(self, page) -> str:
        """Level 0：URL 变异翻页。返回变异后的新 URL（已 goto 完毕）；失败返回 ""。

        安全机制：
        1. 跳过 SPA：若 URL 含 hash 路由（#/...）且 query 没分页参数，放弃变异，
           因为 hash 路由的「翻页」其实是前端 JS 重渲，goto 不会触发。
        2. 数值合法性：page=abc 或 page>9999 这种，跳过。
        3. 翻页生效校验：goto 后再读 page.url，若新 URL path/query 与目标不一致
           （网站可能强制重定向回首页），返回 "" 让上层走 DOM 降级。
        4. DOM 校验：goto 后 _wait_for_page_stable，若页面 body 内容与变异前一致
           （URL 改了但内容没变），也返回 "" 走降级。
        """
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        cur_url = page.url or ""
        if not cur_url or not cur_url.startswith(("http://", "https://")):
            return ""
        parsed = urlparse(cur_url)
        params = list(parse_qsl(parsed.query, keep_blank_values=True))
        has_page_query = any(k.lower() in self._URL_PAGE_KEYS for k, _ in params)

        async def _restore_original(reason: str) -> str:
            """Return to the original page before DOM fallback continues."""
            current = page.url or ""
            if current == cur_url:
                return ""
            try:
                logger.info(f"[NEXT_PAGE L0] {reason}; restoring original URL before fallback")
                try:
                    await page.go_back(wait_until="domcontentloaded", timeout=5000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=2000)
                    except Exception:
                        pass
                    if (page.url or "").split("#", 1)[0] == cur_url.split("#", 1)[0]:
                        return ""
                except Exception:
                    pass
                await page.goto(cur_url, wait_until="domcontentloaded", timeout=10000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
            except Exception as restore_err:
                logger.warning(f"[NEXT_PAGE L0] failed to restore original URL: {restore_err}")
            return ""

        # SPA hash 路由放弃 —— 除非 query 自身已经带分页参数。
        if parsed.fragment and "/" in parsed.fragment and not has_page_query:
            logger.debug("[NEXT_PAGE L0] hash 路由且无分页 query，跳过 URL 变异")
            return ""

        # 变异前快照：用于判断翻页是否真生效
        _before_signature = await self._page_signature(page)

        new_url = ""
        # ── 1. query 参数 page=N 变异 ──
        for i, (k, v) in enumerate(params):
            key = k.lower()
            if key not in self._URL_PAGE_KEYS:
                continue
            try:
                cur_n = int(v)
            except (ValueError, TypeError):
                continue
            if cur_n < 0 or cur_n > 9999:  # 异常值跳过
                continue
            if key in self._RISKY_URL_PAGE_KEYS:
                try:
                    allowed = await self._dom_confirms_pagination_param(
                        page, key, cur_n + 1
                    )
                except Exception as preflight_err:
                    logger.debug(
                        f"[NEXT_PAGE L0] risky param {key!r} preflight failed: {preflight_err}"
                    )
                    allowed = False
                if not allowed:
                    logger.info(
                        f"[NEXT_PAGE L0] skip risky query param {key!r}: "
                        "no pagination href evidence"
                    )
                    continue
            params[i] = (k, str(cur_n + 1))
            new_query = urlencode(params, doseq=True)
            new_url = urlunparse(parsed._replace(query=new_query))
            logger.info(f"[NEXT_PAGE L0] query 变异 {k}={cur_n}→{cur_n+1}: {new_url[:120]}")
            break

        # ── 1.5. offset/start 类参数变异（豆瓣 ?start=25 / 通用 ?offset=N&limit=M） ──
        # 与 page 参数不同，offset 是"行偏移量"而非"页码"，递增步长 = limit/size/per_page。
        # 推断步长优先级：
        #   1. URL 已有 limit/size/per_page → 用其值（最可靠）
        #   2. 当前 offset 值看似是 0 / step 倍数 → 取 _DEFAULT_OFFSET_STEPS[0]=25（豆瓣模式）
        #   3. 否则跳过此分支
        if not new_url:
            _offset_idx = -1
            _offset_key = ""
            _offset_val = 0
            for i, (k, v) in enumerate(params):
                if k.lower() in self._OFFSET_URL_KEYS:
                    try:
                        _offset_val = int(v)
                        if 0 <= _offset_val <= 99999:
                            _offset_idx = i
                            _offset_key = k
                            break
                    except (ValueError, TypeError):
                        continue
            if _offset_idx >= 0:
                # 找步长
                _step = 0
                for k, v in params:
                    if k.lower() in self._PAGE_SIZE_KEYS:
                        try:
                            _s = int(v)
                            if 1 <= _s <= 1000:
                                _step = _s
                                break
                        except (ValueError, TypeError):
                            continue
                if _step == 0:
                    # 没显式 size：用默认步长。豆瓣 Top250 是 25，最常见。
                    _step = self._DEFAULT_OFFSET_STEPS[0]
                _new_offset = _offset_val + _step
                params[_offset_idx] = (_offset_key, str(_new_offset))
                new_url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
                logger.info(
                    f"[NEXT_PAGE L0] offset 变异 {_offset_key}={_offset_val}→{_new_offset} "
                    f"(step={_step}): {new_url[:120]}"
                )

        # ── 2. 路径段 /page/N、/p/N 变异 ──
        if not new_url:
            import re as _re
            m = _re.search(r"(/(?:page|p|pg)/)(\d+)", parsed.path, _re.IGNORECASE)
            if m:
                try:
                    cur_n = int(m.group(2))
                    if 0 <= cur_n <= 9999:
                        new_path = (
                            parsed.path[:m.start(2)]
                            + str(cur_n + 1)
                            + parsed.path[m.end(2):]
                        )
                        new_url = urlunparse(parsed._replace(path=new_path))
                        logger.info(
                            f"[NEXT_PAGE L0] 路径变异 {m.group(0)}→"
                            f"{m.group(1)}{cur_n+1}: {new_url[:120]}"
                        )
                except (ValueError, TypeError):
                    pass

        # ── 3. 首翻 seed：URL 完全没有 page 参数 → 主动追加 page=1（HN Algolia 等场景） ──
        # 修复：?q=AI+Agent 这种首屏 URL 没有 page key，原递增逻辑无法启动；
        # 主动 seed page=1，下一次 next_page 再来就能 1→2 走原 query 递增路径。
        # 仅当 URL 看起来像列表/搜索结果页（query 有内容）时才 seed，避免对静态详情页乱加。
        if not new_url and parsed.query:
            if await self._has_visible_dom_pagination(page):
                logger.info(
                    "[NEXT_PAGE L0] skip seed page=1 because visible DOM pagination exists"
                )
                return ""
            params.append(("page", "1"))
            new_query = urlencode(params, doseq=True)
            new_url = urlunparse(parsed._replace(query=new_query))
            logger.info(f"[NEXT_PAGE L0] 首翻 seed page=1: {new_url[:120]}")

        if not new_url:
            return ""

        # ── 4. 执行 goto + 校验 ──
        try:
            await page.goto(new_url, wait_until="domcontentloaded", timeout=15000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[NEXT_PAGE L0] goto 失败，降级到 DOM: {e}")
            return await _restore_original("goto failed")

        # 校验 URL：站点可能 302 回首页/登录页
        landed = page.url or ""
        if not landed.startswith(("http://", "https://")):
            return await _restore_original("landed URL is invalid")
        # path 必须接近（允许 query 多/少参数），否则视作被劫持
        landed_p = urlparse(landed)
        new_p = urlparse(new_url)
        if landed_p.netloc != new_p.netloc or landed_p.path != new_p.path:
            logger.warning(
                f"[NEXT_PAGE L0] 着陆 URL 不匹配（{landed[:80]} vs {new_url[:80]}），降级"
            )
            return await _restore_original("landed URL mismatch")

        # 校验内容：URL 已变但正文签名完全一致 → 翻页未生效
        _after_signature = await self._page_signature(page)
        if self._looks_like_browser_error_signature(_after_signature):
            logger.warning("[NEXT_PAGE L0] landed on browser/network error page, downgrade")
            return await _restore_original("landed browser error page")
        if _before_signature[1:] == _after_signature[1:]:
            logger.warning("[NEXT_PAGE L0] 着陆页内容与上一页一致，翻页未生效，降级")
            return await _restore_original("landed content unchanged")

        return landed

    async def execute(self, ctx: "ActionContext") -> "Optional[Page]":
        browser = ctx.browser
        page = ctx.page
        if not page:
            raise ActionExecutionError("next_page: 无活动页面。")

        # ── Strategy 0: URL Mutation（最高效，零依赖 DOM）──
        clicked = False
        used_strategy = ""

        # Strategy -1: visible DOM pager first. If the page already exposes a
        # numeric/Next pager, clicking it is safer than inventing a URL query.
        _probe_clicked_but_undetected = False  # 幽灵双击防护标志
        try:
            probe, probe_scope = await self._js_mark_pagination_candidate_with_frames(page)
            if probe.get("found"):
                loc = probe_scope.locator('[data-vspider-next-page-probe="1"]').first
                used_strategy = (
                    f"js_probe_pre_url {probe.get('strategy')} "
                    f"label={probe.get('label')!r}"
                    + (f" frame={probe['frame_url'][:60]!r}" if probe.get("frame_url") else "")
                )
                if await self._click_if_effective(page, loc, used_strategy):
                    clicked = True
                else:
                    # 客户端分页（DataTables 等）不改 URL，且 body.innerText
                    # 前 5000 字符可能被 hero/nav 占据导致 _page_signature
                    # 检测不到变化。但按钮已经被点击，内部状态已翻页。
                    # 若不拦截，L0/L1 会再点同一个按钮造成双击跳页。
                    _probe_score = probe.get("score", 0)
                    _probe_label = str(probe.get("label") or "")
                    if len(_probe_label) > 120:
                        _probe_clicked_but_undetected = True
                        logger.info(
                            "[NEXT_PAGE L-1] probe clicked but label is too broad "
                            "(len=%s); refusing unchanged-page trust",
                            len(_probe_label),
                        )
                    elif _probe_score >= 80:
                        # 高置信度候选（pagination 容器内 + next 文本），
                        # 信任点击已生效，不穿透到 L0/L1。
                        logger.warning(
                            "[NEXT_PAGE L-1] probe clicked (score=%s) but "
                            "page_signature unchanged; trusting click to "
                            "avoid phantom double-advance",
                            _probe_score,
                        )
                        clicked = True
                    else:
                        # 低置信度候选，记录标志但仍允许穿透。
                        # L0/L1 会跳过与探针相同的元素。
                        _probe_clicked_but_undetected = True
                        logger.info(
                            "[NEXT_PAGE L-1] probe clicked (score=%s) but "
                            "undetected; allowing fallthrough with guard",
                            _probe_score,
                        )
            else:
                logger.debug(
                    "[NEXT_PAGE L-1] no visible JS pagination candidate: %s",
                    probe.get("reason"),
                )
        except Exception as probe_err:
            logger.debug("[NEXT_PAGE L-1] JS pagination probe failed: %s", probe_err)

        if clicked:
            logger.info("[NEXT_PAGE] hit strategy %s", used_strategy)
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "next_page",
                    "method": "dom_heuristic",
                    "strategy": used_strategy,
                    "landed_url": page.url or "",
                })
            )
            await browser._wait_after_action()
            return None

        try:
            mutated = await self._try_url_mutation(page)
        except Exception as e:
            logger.debug(f"[NEXT_PAGE L0] 变异异常忽略: {e}")
            mutated = ""
        if mutated:
            logger.info(f"[NEXT_PAGE] L0 URL 变异成功 → {mutated[:120]}")
            print(f"\033[1;35m🚀 [NEXT_PAGE]\033[0m L0 URL 变异 → {mutated[:80]}")
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "next_page",
                    "method": "url_mutation",
                    "strategy": "url_mutation",
                    "landed_url": mutated,
                })
            )
            await browser._wait_after_action()
            return None

        clicked = False
        used_strategy = ""
        # ── Strategy 1: CSS / ARIA selector ──
        for sel in self._LOCATOR_TEMPLATES:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    used_strategy = f"css={sel}"
                    if await self._click_if_effective(page, loc, used_strategy):
                        clicked = True
                        break
            except Exception:
                continue

        # ── Strategy 2: visible text ──
        if not clicked:
            for txt in self._TEXT_PATTERNS:
                try:
                    # 优先精确匹配，缩小误命中
                    loc = page.get_by_text(txt, exact=True).first
                    if await loc.count() > 0 and await loc.is_visible():
                        used_strategy = f'text="{txt}"'
                        if await self._click_if_effective(page, loc, used_strategy):
                            clicked = True
                            break
                except Exception:
                    continue

        # ── Strategy 3: role=link/button + accessible name ──
        if not clicked:
            for txt in self._TEXT_PATTERNS:
                for role in ("link", "button"):
                    try:
                        loc = page.get_by_role(role, name=txt, exact=True).first
                        if await loc.count() > 0 and await loc.is_visible():
                            used_strategy = f"role={role} name=={txt!r}"
                            if await self._click_if_effective(page, loc, used_strategy):
                                clicked = True
                                break
                    except Exception:
                        continue
                if clicked:
                    break

        # ── Strategy 4 (L4): 无限瀑布流兜底 —— 滚动 + 校验内容真增长 ──
        # Twitter / 小红书 / 商品流等纯瀑布流站没有 Next 控件，前面四级全不命中。
        # 关键避坑：scrollY 增量 ≠ 内容增加。HN Algolia 这类**伪无限滚动**站
        # （实为分页器但无标准 Next 控件）滚动只移动视口不加载新条目，
        # 必须同时校验 body innerText 长度 / 列表项数量真实增长，才能判定"翻页成功"。
        # Strategy 3.5: JS pagination probe for component-library/numeric pagers.
        if not clicked:
            try:
                probe, probe_scope = await self._js_mark_pagination_candidate_with_frames(page)
                if probe.get("found"):
                    loc = probe_scope.locator('[data-vspider-next-page-probe="1"]').first
                    used_strategy = (
                        f"js_probe {probe.get('strategy')} "
                        f"label={probe.get('label')!r}"
                        + (f" frame={probe['frame_url'][:60]!r}" if probe.get("frame_url") else "")
                    )
                    if await self._click_if_effective(page, loc, used_strategy):
                        clicked = True
                else:
                    logger.debug(
                        "[NEXT_PAGE L3.5] no JS pagination candidate: %s",
                        probe.get("reason"),
                    )
            except Exception as probe_err:
                logger.debug("[NEXT_PAGE L3.5] JS pagination probe failed: %s", probe_err)

        if not clicked:
            try:
                _state_js = (
                    "() => ({"
                    "  y: window.scrollY,"
                    "  textLen: (document.body && document.body.innerText || '').length,"
                    "  itemCount: document.querySelectorAll("
                    "    'article, li, [role=\"article\"], [role=\"listitem\"], "
                    "    .Story, .item, .row, tr'"
                    "  ).length"
                    "})"
                )
                _before = await page.evaluate(_state_js) or {}
                await page.evaluate(
                    "() => window.scrollBy({top: window.innerHeight * 0.85, "
                    "left: 0, behavior: 'smooth'})"
                )
                await asyncio.sleep(1.0)  # 等懒加载触发
                _after = await page.evaluate(_state_js) or {}

                _delta_y = max(0, int(_after.get("y", 0)) - int(_before.get("y", 0)))
                _delta_text = int(_after.get("textLen", 0)) - int(_before.get("textLen", 0))
                _delta_items = int(_after.get("itemCount", 0)) - int(_before.get("itemCount", 0))

                # 滚轮没动 = 真到页面底部
                if _delta_y < 50:
                    _local = await _scroll_largest_container(page, "down", smooth=True)
                    await asyncio.sleep(1.0)
                    _after_local = await page.evaluate(_state_js) or {}
                    _delta_text_local = (
                        int(_after_local.get("textLen", 0))
                        - int(_before.get("textLen", 0))
                    )
                    _delta_items_local = (
                        int(_after_local.get("itemCount", 0))
                        - int(_before.get("itemCount", 0))
                    )
                    if (
                        _local.get("moved")
                        and (_delta_text_local >= 200 or _delta_items_local > 0)
                    ):
                        used_strategy = (
                            f"local_infinite_scroll target={_local.get('target')} "
                            f"delta_text={_delta_text_local} "
                            f"delta_items={_delta_items_local}"
                        )
                        clicked = True
                        _delta_y = max(_delta_y, 50)
                        _delta_text = max(_delta_text, _delta_text_local)
                        _delta_items = max(_delta_items, _delta_items_local)
                        logger.info("[NEXT_PAGE L4] local scroll succeeded: %s", used_strategy)
                    if not clicked:
                        raise ActionExecutionError(
                        "next_page: 启发式翻页全失败 + 已滚到页面底部（scrollY 不再增长），"
                        "可能已是最后一页。请评估累计提取量，若已达目标输出 done。"
                    )
                # Fix 2 关键：滚轮动了但内容没增长 = 伪无限滚动（实际是分页器但无标准控件）
                # 这种情况 L4 不算成功，应当报错让上层换路（ask_human / done / click 真实 target_id）
                if _delta_text < 50 and _delta_items <= 0:
                    raise ActionExecutionError(
                        f"next_page: L4 滚动后内容未增长（textLen Δ={_delta_text}，"
                        f"itemCount Δ={_delta_items}），本页**不是**真无限滚动 —— "
                        "实际是分页器但 L0-L3 都没识别到下一页控件。请改用 "
                        "click_text 加具体页码（如 type_value=\"2\"）翻页，"
                        "或评估累计量考虑输出 done。"
                    )
                used_strategy = (
                    f"infinite_scroll Δy={_delta_y}px Δtext={_delta_text} "
                    f"Δitems={_delta_items}"
                )
                clicked = True  # 视作成功
            except ActionExecutionError:
                raise
            except Exception as e:
                raise ActionExecutionError(
                    f"next_page: 启发式 locator 链全部未命中且滚动兜底失败（{e}）。"
                    "请改用 click + 真实 target_id 或 click_text + 具体文字。"
                )

        logger.info(f"[NEXT_PAGE] 命中策略 {used_strategy}")
        print(f"\033[1;35m🤖 [NEXT_PAGE]\033[0m 启发式翻页 → {used_strategy}")
        browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "next_page",
                "method": (
                    "infinite_scroll" if used_strategy.startswith("infinite_scroll")
                    else "dom_heuristic"
                ),
                "strategy": used_strategy,
                "landed_url": page.url or "",
            })
        )
        await browser._wait_after_action()
        return None


@ActionRegistry.register("click_text")
class ClickTextHandler(ActionHandler):
    """文本定位点击：page.get_by_text(type_value) 绕开 SoM ID 填位。

    适用场景：密集分页器、文字链、固定 label 按钮 —— 当 VLM 知道按钮的可见文字
    但 target_id 字段总填错时，这条路彻底跳过 VLM 的 schema 填位问题。
    """

    async def execute(self, ctx: "ActionContext") -> "Optional[Page]":
        browser = ctx.browser
        page = ctx.page
        text = (ctx.action.type_value or "").strip()
        if not text:
            raise ActionExecutionError(
                "click_text 必须在 type_value 中提供可见文字（如 '2' / '下一页' / 'Submit'）。"
            )
        if not page:
            raise ActionExecutionError("click_text: 无活动页面。")

        clicked = False
        used = ""
        derived_selector = ""
        cache = None
        from_cache = False
        # ── 0. E4 跨 run selector 缓存（命中仍走可见性+文本指纹校验）──
        try:
            from visual_web_agent.selector_cache import (
                SelectorCache as _SelCache,
                cache_host as _sc_host,
                click_cached_selector as _sc_click,
                derive_selector as _sc_derive,
                selector_cache_enabled as _sc_enabled,
            )

            _host = _sc_host(getattr(page, "url", "") or "")
            if _sc_enabled() and _host:
                cache = _SelCache(_host)
                cached_entry = cache.lookup("click_text", text)
                if cached_entry:
                    async def _cache_click(loc: Any, label: str) -> str:
                        return await _click_locator_with_js_fallback(loc, label, timeout=3000)

                    used = await _sc_click(page, cached_entry, text, _cache_click)
                    if used:
                        clicked = True
                        from_cache = True
                        cache.record_hit("click_text", text)
                    else:
                        cache.invalidate("click_text", text)
        except Exception as cache_err:
            logger.debug("[CLICK_TEXT] selector cache probe skipped: %s", cache_err)
            cache = None

        async def _click_first_visible(locator: Any, label: str, limit: int = 20) -> str:
            nonlocal derived_selector
            try:
                count = await locator.count()
            except Exception:
                return ""
            for idx in range(min(count, limit)):
                candidate = locator.nth(idx)
                try:
                    if not await candidate.is_visible():
                        continue
                    disabled = await candidate.evaluate(
                        """el => !!el.closest(
                            '[aria-disabled="true"], .is-disabled, .disabled, [disabled]'
                        )"""
                    )
                    if disabled:
                        continue
                    if cache is not None and not derived_selector:
                        derived_selector = await _sc_derive(candidate)
                    mode = await _click_locator_with_js_fallback(
                        candidate, label, timeout=3000
                    )
                    return f"{label}#{idx} ({mode})"
                except Exception as click_err:
                    logger.debug(
                        "[CLICK_TEXT] candidate %s#%s failed: %s",
                        label,
                        idx,
                        click_err,
                    )
                    continue
            return ""

        # ── 1. 已展开弹层/菜单优先 ──
        # 避免同名文本误点到全局导航或侧边栏，例如顶部 Guide 链接 vs
        # Element Plus Cascader 弹层里的 Guide 选项。
        menu_selectors = (
            ".el-popper .el-cascader-node",
            ".el-cascader-panel .el-cascader-node",
            ".el-cascader-menu [role='menuitem']",
            ".el-popper [role='menuitem']",
            "[role='menu'] [role='menuitem']",
            "[role='listbox'] [role='option']",
            ".el-select-dropdown__item",
            ".el-dropdown-menu__item",
            ".ant-cascader-menu-item",
            ".ant-select-item-option",
            ".ant-dropdown-menu-item",
            ".dropdown-menu li",
            ".dropdown-item",
        )
        if not clicked:
            for selector in menu_selectors:
                try:
                    loc = page.locator(selector).filter(has_text=text)
                    used = await _click_first_visible(
                        loc, f"click_text popup {selector} {text!r}"
                    )
                    if used:
                        clicked = True
                        break
                except Exception:
                    continue

        # ── 2. 精确匹配 ──
        if not clicked:
            try:
                loc = page.get_by_text(text, exact=True)
                used = await _click_first_visible(loc, f"click_text exact {text!r}")
                clicked = bool(used)
            except Exception:
                pass

        # ── 2.5. 网格/日历/选项型元素（div/td 没有 role=button/link 的情况）──
        # 现象：Element Plus 月份/年份面板里的 "May"/"2026" 是 <div>/<td>，
        # 既不在 tier 1 的弹层 selector 里，也不匹配 tier 3 的 role=link/button，
        # tier 4 的 get_by_text(exact=False).first 又可能误中 aria 文本或隐藏节点。
        # 这里专门覆盖主流 UI 库的日期/级联/Tab 等"可点但没 role"控件。
        if not clicked:
            grid_selectors = (
                # Element Plus
                ".el-date-table td",
                ".el-month-table td",
                ".el-year-table td",
                ".el-picker-panel td",
                # Ant Design
                ".ant-picker-cell",
                ".ant-picker-month-btn",
                ".ant-picker-year-btn",
                # Arco / Naive / 通用
                ".arco-picker-cell",
                ".n-date-panel-date",
                # 通用 ARIA 网格/选项
                "[role='gridcell']",
                "[role='option']",
                "[role='tab']",
                "[role='treeitem']",
            )
            for selector in grid_selectors:
                try:
                    loc = page.locator(selector).filter(has_text=text)
                    used = await _click_first_visible(
                        loc, f"click_text grid {selector} {text!r}"
                    )
                    if used:
                        clicked = True
                        break
                except Exception:
                    continue

        # ── 3. role=link/button + name 精确 ──
        if not clicked:
            for role in ("link", "button"):
                try:
                    loc = page.get_by_role(role, name=text, exact=True).first
                    if await loc.count() > 0 and await loc.is_visible():
                        if cache is not None and not derived_selector:
                            derived_selector = await _sc_derive(loc)
                        mode = await _click_locator_with_js_fallback(
                            loc, f"click_text role={role} {text!r}", timeout=3000
                        )
                        clicked = True
                        used = f"role={role} name=={text!r} ({mode})"
                        break
                except Exception:
                    continue

        # ── 4. 子串匹配（最后兜底）──
        # 短文本/数字页码只允许精确命中，避免 "2" 误点到任意含 2 的标题/计数。
        allow_substring = len(text) > 2 and not text.isdigit()
        if not clicked and allow_substring:
            try:
                loc = page.get_by_text(text, exact=False).first
                if await loc.count() > 0 and await loc.is_visible():
                    if cache is not None and not derived_selector:
                        derived_selector = await _sc_derive(loc)
                    mode = await _click_locator_with_js_fallback(
                        loc, f"click_text substring {text!r}", timeout=3000
                    )
                    clicked = True
                    used = f'substring="{text}" ({mode})'
            except Exception:
                pass

        if not clicked:
            raise ActionExecutionError(
                f"click_text: 在页面上找不到可见且可点击的元素含文字 {text!r}。"
                "请检查 type_value 是否完全匹配按钮显示文字，或换用 next_page / "
                "smooth_scroll 让目标进入视口。"
            )

        if cache is not None and not from_cache and derived_selector:
            try:
                cache.store("click_text", text, derived_selector, signature=text)
                logger.debug(
                    "[CLICK_TEXT] selector cache write-back: %s -> %s",
                    text,
                    derived_selector,
                )
            except Exception as store_err:
                logger.debug("[CLICK_TEXT] selector cache write-back skipped: %s", store_err)
        logger.info(f"[CLICK_TEXT] {text!r} 命中：{used}")
        print(f"\033[1;35m🎯 [CLICK_TEXT]\033[0m {text!r} → {used}")
        browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "click_text", "type_value": text, "strategy": used,
            })
        )
        await browser._wait_after_action()
        return None


@ActionRegistry.register("hover_and_click")
class HoverAndClickHandler(ActionHandler):
    """复合 hover→wait→click 原子操作，专治 hover-trigger 下拉菜单。

    痛点：Element Plus / Ant Design / Element UI 等的 hover-trigger dropdown
    在 Playwright `hover` 后，下一轮 SoM mark_and_screenshot 会移动鼠标做坐标
    采样 → 鼠标离开 trigger → 菜单瞬间收起（200ms hide delay 标准实现）→
    截图捕到 collapsed 状态 → VLM 永远看不到 menuitem → 卡死循环。

    解决：在**同一个 Playwright 会话内**完成 hover → 短延迟 → 点击 menu 文本，
    全程不让 SoM 介入，鼠标自然移动到 menu 区域保持菜单展开。

    用法：
      target_id = hover 触发器红框 ID（如 "Dropdown List" 按钮）
      type_value = 要点击的菜单项可见文字（如 "Action 3"）
    """

    async def execute(self, ctx: "ActionContext") -> "Optional[Page]":
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        menu_text = (ctx.action.type_value or "").strip()
        if not page:
            raise ActionExecutionError("hover_and_click: 无活动页面。")
        if target_id == 0:
            raise ActionExecutionError(
                "hover_and_click 必须提供 hover 触发器的 target_id（非 0）。"
            )
        if not menu_text:
            raise ActionExecutionError(
                "hover_and_click 必须在 type_value 提供菜单项可见文字（如 'Action 3'）。"
            )

        try:
            await browser._clear_som_overlays()
            target = await browser._resolve_action_target(target_id, "click")
            if not target:
                raise ActionExecutionError(
                    f"hover_and_click: hover 触发器 #{target_id} 未找到"
                )
            await target.handle.scroll_into_view_if_needed(timeout=browser._LOCATOR_TIMEOUT)

            # Step 1: hover trigger（不释放）
            logger.info(f"[HOVER_AND_CLICK] step 1: hover #{target_id} 触发器")
            await target.handle.hover(timeout=3000)

            # Step 2: 等菜单展开动画（多数 UI 库 100-300ms 延迟 + transition）
            await asyncio.sleep(0.5)

            # Step 3: 在 menu 内点击目标文本（不重置鼠标）。
            # 菜单弹层常被 portal 到 body 下，且候选文本可能同时出现在源码示例中；
            # 这里遍历可见候选，并在原生点击失败时用 JS click 兜底。
            logger.info(f"[HOVER_AND_CLICK] step 3: 点击菜单项 {menu_text!r}")
            _clicked = False
            _used = ""

            async def _click_visible_candidate(locator: Any, label: str) -> str:
                count = 0
                try:
                    count = await locator.count()
                except Exception:
                    return ""
                for idx in range(min(count, 20)):
                    candidate = locator.nth(idx)
                    try:
                        if not await candidate.is_visible():
                            continue
                        mode = await _click_locator_with_js_fallback(
                            candidate, label, timeout=1500
                        )
                        return f"{label}#{idx} ({mode})"
                    except Exception as click_err:
                        logger.debug(
                            f"[HOVER_AND_CLICK] candidate {label}#{idx} failed: {click_err}"
                        )
                        continue
                return ""

            menu_selectors = (
                '[role="menuitem"]',
                '[role="menuitemcheckbox"]',
                '[role="menuitemradio"]',
                '[role="option"]',
                ".el-dropdown-menu__item",
                ".el-select-dropdown__item",
                ".ant-dropdown-menu-item",
                ".ant-select-item-option",
                ".dropdown-item",
                ".dropdown-menu li",
            )
            for selector in menu_selectors:
                if _clicked:
                    break
                try:
                    loc = page.locator(selector).filter(has_text=menu_text)
                    _used = await _click_visible_candidate(loc, f"selector={selector}")
                    _clicked = bool(_used)
                except Exception:
                    continue

            if not _clicked:
                try:
                    loc = page.get_by_text(menu_text, exact=True)
                    _used = await _click_visible_candidate(loc, f'exact="{menu_text}"')
                    _clicked = bool(_used)
                except Exception:
                    pass
            if not _clicked:
                for role in ("menuitem", "menuitemcheckbox", "menuitemradio", "option", "button"):
                    try:
                        loc = page.get_by_role(role, name=menu_text, exact=True)
                        _used = await _click_visible_candidate(
                            loc, f"role={role} name=={menu_text!r}"
                        )
                        if _used:
                            _clicked = True
                            break
                    except Exception:
                        continue
            if not _clicked:
                # 子串模糊兜底
                try:
                    loc = page.get_by_text(menu_text, exact=False)
                    _used = await _click_visible_candidate(
                        loc, f'substring="{menu_text}"'
                    )
                    _clicked = bool(_used)
                except Exception:
                    pass

            if not _clicked:
                raise ActionExecutionError(
                    f"hover_and_click: hover #{target_id} 后未在菜单中找到 {menu_text!r}。"
                    "可能菜单未展开（trigger 不是 hover 触发型），或文字不完全匹配。"
                    "可改为分步：先 click 触发器（trigger=click），再 click_text 菜单项。"
                )

            logger.info(f"[HOVER_AND_CLICK] ✅ 复合操作成功: hover #{target_id} → click {_used}")
            print(
                f"\033[1;35m🎯 [HOVER+CLICK]\033[0m hover #{target_id} → "
                f"click {menu_text!r} ({_used})"
            )
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "hover_and_click",
                    "hover_target_id": target_id,
                    "menu_text": menu_text,
                    "click_strategy": _used,
                })
            )
            await browser._wait_after_action()
            return None
        except ActionExecutionError:
            raise
        except Exception as e:
            raise ActionExecutionError(f"hover_and_click 执行失败: {e}")



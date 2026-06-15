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


"""Navigation handlers: done / wait / close_tab / goto / scroll / smooth_scroll"""

@ActionRegistry.register("done")
class DoneHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        logger.info("Task marked as done, no action executed")
        return ctx.page  # 显式返回：dispatcher 跳过 Tab Guard


@ActionRegistry.register("wait")
class WaitHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        tv = ctx.action.type_value
        try:
            wait_secs = max(1, min(10, int(float(tv or "2"))))
        except (ValueError, TypeError):
            wait_secs = 2
        logger.info(f"[WAIT] Explicit wait: {wait_secs}s (requested: {tv!r})")
        print(f"⏳ [WAIT] 显式等待 {wait_secs} 秒...")
        await asyncio.sleep(wait_secs)
        ctx.browser.rpa_trail.append(
            ctx.with_rpa_meta({"action": "wait", "type_value": str(wait_secs)})
        )
        return None


@ActionRegistry.register("close_tab")
class CloseTabHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        open_pages = [p for p in browser._context.pages if not p.is_closed()]
        explicit_index: int | None = None
        tv = (ctx.action.type_value or "").strip()
        if tv.isdigit():
            explicit_index = int(tv)
        elif ctx.action.target_id > 0:
            explicit_index = int(ctx.action.target_id)

        target_page = page
        if explicit_index is not None:
            if explicit_index < 0 or explicit_index >= len(open_pages):
                raise ActionExecutionError(
                    f"close_tab target index {explicit_index} out of range; "
                    f"open tabs={len(open_pages)}"
                )
            target_page = open_pages[explicit_index]

        closing_current = target_page is page
        logger.info(
            "[CLOSE TAB] Closing %s tab%s",
            "current" if closing_current else "indexed",
            "" if explicit_index is None else f"[{explicit_index}]",
        )
        try:
            await target_page.close()
            browser.rpa_trail.append(
                ctx.with_rpa_meta(
                    {
                        "action": "close_tab",
                        "type_value": "" if explicit_index is None else str(explicit_index),
                    }
                )
            )
            logger.debug("[RPA] Recorded close_tab")
            logger.info("[CLOSE TAB] Page closed successfully")
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"[CLOSE TAB] Failed to close page: {e}")
        open_pages = [p for p in browser._context.pages if not p.is_closed()]
        if open_pages:
            if closing_current or page.is_closed():
                # ── Tab Visit Stack: 优先回到"开这个 tab 时所在的父" ──
                # 旧行为：跳到 open_pages[-1]，多 tab 场景下经常错。
                # 新行为：从 stack 弹出最近的存活父；若 stack 已空（或所有父
                # 都已关闭），退回 open_pages[-1] 兼容现有 close_tab 调用。
                parent = browser.pop_tab_visit()
                if parent is not None and parent in open_pages:
                    browser._page = parent
                    logger.info(
                        "[CLOSE TAB] returned to stacked parent (stack-aware)"
                    )
                else:
                    browser._page = open_pages[-1]
                    if parent is not None:
                        logger.info(
                            "[CLOSE TAB] stacked parent unavailable; "
                            "fallback to open_pages[-1]"
                        )
            else:
                browser._page = page
            await browser._page.bring_to_front()
            await asyncio.sleep(0.8)
            logger.info(
                f"[CLOSE TAB] Focused back to: "
                f"{(browser._page.url or 'about:blank')[:80]}"
            )
        return None


@ActionRegistry.register("goto")
class GotoHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        url = ctx.action.type_value
        if not url:
            raise ActionExecutionError(
                "goto 动作缺少目标 URL，请在 type_value 中填写完整的 URL。"
            )

        # 🌐 跨系统 goto 前置拦截（A1, Path-2）：开关开时 main.py 才把
        # session_router 经 ActionContext 传入；若目标 URL 属于另一个已规划 system，
        # 则**不**在当前页执行 page.goto（保留当前 system 页面），改在 browser 上
        # 记 _pending_cross_system_goto 指令交给反应式循环 acquire→launch→rebind 到
        # 目标 system 的隔离 session 再导航。session_router 为 None（开关关 / 单系统）
        # → 整段跳过，goto 行为字节不变。
        _xsys_router = getattr(ctx, "session_router", None)
        if _xsys_router is not None:
            _xsys_directive = None
            try:
                _xsys_from = _xsys_router.system_for_url(
                    getattr(browser, "current_url", "") or ""
                )
                _xsys_directive = _xsys_router.plan_goto_interception(url, _xsys_from)
            except Exception as _xsys_err:
                logger.debug(f"[GOTO X-SYS] interception plan skipped: {_xsys_err}")
            if _xsys_directive and _xsys_directive.get("should_intercept"):
                browser._pending_cross_system_goto = _xsys_directive
                _xsys_notice = (
                    f"[CROSS-SYSTEM GOTO] 目标属于另一系统 "
                    f"({_xsys_directive.get('to_system_id')})，已拦截以保留当前页面；"
                    f"反应式循环将切换到目标系统的隔离会话再导航。"
                )
                if not browser._tab_switch_notice:
                    browser._tab_switch_notice = _xsys_notice
                logger.info(f"{_xsys_notice} url={url[:80]}")
                return None

        # 🛡️ 多标签页 goto 智能拦截器
        url_clean = url.split("?")[0].rstrip("/")
        open_pages = [p for p in browser._context.pages if not p.is_closed()]

        target_idx = -1
        for idx, p in enumerate(open_pages):
            if p.url.split("?")[0].rstrip("/") == url_clean:
                target_idx = idx
                break

        try:
            current_idx = open_pages.index(page)
        except ValueError:
            current_idx = -1

        _goto_recorded = False

        if target_idx != -1 and target_idx != current_idx:
            browser._page = open_pages[target_idx]
            await browser._page.bring_to_front()
            await asyncio.sleep(0.5)
            _goto_recorded = True
            logger.info(
                f"[GOTO INTERCEPTOR] 目标已存在于 Tab [{target_idx}]，"
                f"已强制转换为 switch_tab，避免原位覆盖: {url[:80]}"
            )
        elif target_idx != -1 and target_idx == current_idx:
            logger.info(
                f"[GOTO INTERCEPTOR] 当前已在目标页面，忽略 goto 请求: {url[:80]}"
            )
        else:
            logger.info(f"Executing goto: {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await browser._wait_for_page_stable()
                _goto_recorded = True
                logger.info(f"Navigation to {url} succeeded")
            except Exception as e:
                browser._last_action_error = e
                logger.error(f"Navigation to {url} failed: {e}")

        if _goto_recorded:
            browser.rpa_trail.append(
                ctx.with_rpa_meta({"action": "goto", "url": url})
            )
            logger.debug(f"[RPA] Recorded goto: {url}")
        return None


@ActionRegistry.register("scroll")
class ScrollHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        page = ctx.page
        target_id = ctx.action.target_id
        raw_dir = (ctx.action.type_value or "").strip().lower()
        if raw_dir in ("down", "up", "bottom", "top"):
            direction = raw_dir
        elif target_id < 0:
            direction = "up"
        else:
            direction = "down"

        prev_y = await page.evaluate("window.scrollY")

        if direction == "bottom":
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "top":
            await page.evaluate("window.scrollTo(0, 0)")
        elif direction == "up":
            await page.evaluate("window.scrollBy(0, -700)")
        else:
            await page.evaluate("window.scrollBy(0, 700)")

        await asyncio.sleep(1.0)

        new_y = await page.evaluate("window.scrollY")
        logger.info(f"[SCROLL] direction={direction} scrollY: {prev_y} → {new_y}")
        print(f"🔄 [SCROLL] 执行方向: {direction}, 位置变化: {prev_y} → {new_y}")

        if prev_y == new_y:
            local = await _scroll_largest_container(page, direction, smooth=False)
            await asyncio.sleep(0.8)
            if local.get("moved"):
                logger.info(
                    f"[SCROLL LOCAL] direction={direction} target={local.get('target')} "
                    f"scrollTop: {local.get('before')} → {local.get('after')}"
                )
                print(
                    f"🔄 [SCROLL LOCAL] {direction}: {local.get('target')} "
                    f"{local.get('before')} → {local.get('after')}"
                )
                return None
            if direction == "up":
                raise ActionExecutionError(
                    "页面已滚到顶部，无法继续向上滚动。请观察当前截图，换用其它动作。"
                )
            raise ActionExecutionError(
                "页面已滚到底部，无法继续向下滚动。"
                "如果需要翻页，请执行 next_page，或点击页面上可见的"
                "'下一页'/'More'等翻页链接或按钮。"
            )
            raise ActionExecutionError(
                f"执行 scroll ({direction}) 无效：页面未发生滚动，"
                f"可能已到达页面边缘或该方向无滚动条。"
                f"请观察当前截图，不要再继续向该方向滚动。"
            )
        return None


@ActionRegistry.register("smooth_scroll")
class SmoothScrollHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        direction = (ctx.action.type_value or "down").strip().lower()
        if direction == "up":
            js_scroll = "window.scrollBy({top: -window.innerHeight * 0.8, behavior: 'smooth'});"
        else:
            js_scroll = "window.scrollBy({top: window.innerHeight * 0.8, behavior: 'smooth'});"
        try:
            prev_y = await page.evaluate("window.scrollY")
            await page.evaluate(js_scroll)
            await asyncio.sleep(1.0)
            new_y = await page.evaluate("window.scrollY")
            logger.info(
                f"[SMOOTH_SCROLL] direction={direction} scrollY: {prev_y} → {new_y}"
            )
            print(
                f"🌊 [SMOOTH_SCROLL] direction={direction}, 位置变化: {prev_y} → {new_y}"
            )
            if abs(new_y - prev_y) < 1:
                local = await _scroll_largest_container(page, direction, smooth=True)
                await asyncio.sleep(0.8)
                if local.get("moved"):
                    logger.info(
                        f"[SMOOTH_SCROLL LOCAL] direction={direction} "
                        f"target={local.get('target')} "
                        f"scrollTop: {local.get('before')} → {local.get('after')}"
                    )
                    print(
                        f"🌊 [SMOOTH_SCROLL LOCAL] {direction}: {local.get('target')} "
                        f"{local.get('before')} → {local.get('after')}"
                    )
                else:
                    boundary = "底" if direction != "up" else "顶"
                    hint = (
                        f"页面已滚到{boundary}部，无法继续向"
                        f"{'下' if direction != 'up' else '上'}滚动。"
                        f"如果需要翻页，请直接点击页面上可见的'下一页'/'More'等翻页链接或按钮。"
                    )
                    if direction == "up":
                        hint = "页面已滚到顶部，无法继续向上滚动。请观察当前截图，换用其它动作。"
                    else:
                        hint = (
                            "页面已滚到底部，无法继续向下滚动。"
                            "如果需要翻页，请执行 next_page，或点击页面上可见的"
                            "'下一页'/'More'等翻页链接或按钮。"
                        )
                    logger.warning(f"[SMOOTH_SCROLL] {hint}")
                    browser._last_action_error = RuntimeError(hint)
                    raise ActionExecutionError(hint)
            browser.rpa_trail.append(
                ctx.with_rpa_meta({"action": "smooth_scroll", "type_value": direction})
            )
        except ActionExecutionError:
            raise
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"[SMOOTH_SCROLL] Failed: {e}")
        return None



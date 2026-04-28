"""
VSpider 动作执行层（Action Registry）

从 browser_env.py 的 execute_action 巨兽中抽取的独立 Handler 层：
  - ActionContext：Handler 调用上下文（Pydantic，校验 browser 非 None 等不变量）
  - ActionHandler：抽象基类，所有动作 Handler 需实现 async execute(ctx)
  - ActionRegistry：名字 → Handler 的映射表，通过 @register(*names) 装饰器注册
  - 18 个具体 Handler 子类（click / type / hover / goto / switch_tab / ...）

设计约束：
  - 保持 100% 行为一致，不改动作语义，不引入新流程
  - Handler 里统一通过 `ctx.browser._xxx` 访问私有方法（内部迁移，非 API 边界）
  - RPA trail 写入走 `ctx.with_rpa_meta(step)`，保留插值/required_keys 元数据
  - 最后一层错误处理（Tab Guard + ActionExecutionError）仍由 execute_action 统一兜底
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
import urllib.parse
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from pydantic import BaseModel, ConfigDict

try:
    from .vlm_client import VSpiderAction
    from .browser_env import ActionExecutionError
    from .auth_vault import SecretResolutionError, resolve_env_placeholders
    from .artifact_manager import register_artifact
except ImportError:
    from vlm_client import VSpiderAction
    from browser_env import ActionExecutionError
    from auth_vault import SecretResolutionError, resolve_env_placeholders
    from artifact_manager import register_artifact

if TYPE_CHECKING:
    from playwright.async_api import Page
    from .browser_env import BrowserEnv

logger = logging.getLogger("vspider.actions")


def _is_navigation_context_destroyed(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "execution context was destroyed" in text
        or "most likely because of a navigation" in text
        or "cannot find context with specified id" in text
    )


async def _click_locator_with_js_fallback(
    loc: Any, label: str, timeout: int = 3000
) -> str:
    """Click with Playwright first, then DOM-level JS click if actionability blocks it."""
    try:
        await loc.scroll_into_view_if_needed(timeout=2000)
        await loc.click(timeout=timeout)
        return "native"
    except Exception as native_err:
        logger.warning(
            f"[JS CLICK FALLBACK] native click blocked for {label}: {native_err}"
        )
        await loc.evaluate(
            """el => {
                el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
                if (typeof el.click === 'function') {
                    el.click();
                } else {
                    el.dispatchEvent(new MouseEvent('click', {
                        bubbles: true,
                        cancelable: true,
                        composed: true,
                        view: window
                    }));
                }
            }"""
        )
        return "js"


async def _scroll_largest_container(
    page: Any, direction: str, smooth: bool = False
) -> dict:
    """Scroll the largest visible overflow container when window scrolling is ineffective."""
    return await page.evaluate(
        """([direction, smooth]) => {
            const dir = String(direction || 'down').toLowerCase();
            const behavior = smooth ? 'smooth' : 'auto';
            const viewportW = window.innerWidth || document.documentElement.clientWidth || 0;
            const viewportH = window.innerHeight || document.documentElement.clientHeight || 0;

            function visibleRect(el) {
                const r = el.getBoundingClientRect();
                if (!r || r.width < 20 || r.height < 20) return null;
                if (r.bottom <= 0 || r.right <= 0 || r.top >= viewportH || r.left >= viewportW) return null;
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || 1) <= 0.01) return null;
                return r;
            }

            const candidates = [];
            for (const el of Array.from(document.querySelectorAll('*'))) {
                if (el === document.documentElement || el === document.body) continue;
                const r = visibleRect(el);
                if (!r) continue;
                const cs = window.getComputedStyle(el);
                const overflowY = `${cs.overflowY || ''} ${cs.overflow || ''}`.toLowerCase();
                const scrollableStyle = overflowY.includes('auto') || overflowY.includes('scroll') || overflowY.includes('overlay');
                const canScroll = el.scrollHeight > el.clientHeight + 8;
                if (!canScroll || !scrollableStyle) continue;
                const maxTop = el.scrollHeight - el.clientHeight;
                const canMoveDown = el.scrollTop < maxTop - 2;
                const canMoveUp = el.scrollTop > 2;
                if ((dir === 'up' || dir === 'top') ? !canMoveUp : !canMoveDown) continue;
                const area = Math.max(0, Math.min(r.right, viewportW) - Math.max(r.left, 0)) *
                             Math.max(0, Math.min(r.bottom, viewportH) - Math.max(r.top, 0));
                candidates.push({ el, area, top: el.scrollTop, maxTop });
            }
            candidates.sort((a, b) => b.area - a.area);
            const picked = candidates[0];
            if (!picked) return { moved: false, reason: 'no-scrollable-container' };

            const el = picked.el;
            const before = el.scrollTop;
            const delta = Math.max(240, Math.round((el.clientHeight || viewportH || 800) * 0.85));
            if (dir === 'top') el.scrollTo({ top: 0, behavior });
            else if (dir === 'bottom') el.scrollTo({ top: el.scrollHeight, behavior });
            else el.scrollBy({ top: (dir === 'up' ? -delta : delta), behavior });

            const ident = [
                el.tagName ? el.tagName.toLowerCase() : 'element',
                el.id ? `#${el.id}` : '',
                el.className && typeof el.className === 'string'
                    ? '.' + el.className.trim().split(/\\s+/).slice(0, 3).join('.')
                    : ''
            ].join('');
            return {
                moved: true,
                before,
                after: el.scrollTop,
                maxTop: picked.maxTop,
                target: ident,
                candidates: candidates.length
            };
        }""",
        [direction, smooth],
    )


def _resolve_env_placeholders(text: str) -> tuple[str, bool, list[str]]:
    """Resolve {{env:VAR}} placeholders immediately before browser input."""
    try:
        return resolve_env_placeholders(text)
    except SecretResolutionError as exc:
        raise ActionExecutionError(str(exc)) from exc


# ════════════════════════════════════════════════════════════════
#  Action Context
# ════════════════════════════════════════════════════════════════

class ActionContext(BaseModel):
    """Handler 执行上下文。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    action: VSpiderAction
    browser: Any  # BrowserEnv — Pydantic 无法引用未加载的类，运行时鸭子类型
    workflow_memory: dict
    page: Any  # playwright.async_api.Page

    # RPA 元数据（原本透过 dict 的 __rpa_* 字段传递；现显式承载）
    rpa_required_keys: list[str] = []
    rpa_template_value: str = ""

    def with_rpa_meta(self, step: dict) -> dict:
        """原 execute_action 内闭包 _with_rpa_meta 的直接迁移。"""
        tid = self.action.target_id
        if tid not in (None, "", 0):
            step.setdefault(
                "target_id",
                int(tid) if str(tid).isdigit() else tid,
            )
        if self.rpa_required_keys:
            step["required_memory_keys"] = list(self.rpa_required_keys)
        if self.action.action == "type" and self.rpa_template_value:
            step["type_value_template"] = self.rpa_template_value
        if self.action.action == "goto" and self.rpa_template_value:
            step["url_template"] = self.rpa_template_value
        return step


# ════════════════════════════════════════════════════════════════
#  Handler 抽象 & Registry
# ════════════════════════════════════════════════════════════════

class ActionHandler(ABC):
    """所有具体动作 Handler 的基类。"""

    @abstractmethod
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        """
        执行动作。

        返回值语义：
          - None  → 由 dispatcher 走 Tab Guard 流程决定 active_page
          - Page  → 显式接管（如 switch_tab / done），dispatcher 跳过 Tab Guard
        """
        ...


class UnknownActionError(Exception):
    """VLM 返回了未注册的 action 名称。"""


class ActionRegistry:
    """动作名称 → Handler 类 的映射。"""

    _handlers: dict[str, type[ActionHandler]] = {}

    @classmethod
    def register(cls, *names: str):
        def deco(handler_cls: type[ActionHandler]):
            for n in names:
                cls._handlers[n] = handler_cls
            return handler_cls
        return deco

    @classmethod
    def get(cls, action_name: str) -> ActionHandler:
        h = cls._handlers.get(action_name)
        if h is None:
            raise UnknownActionError(f"未注册的动作: {action_name!r}")
        return h()

    @classmethod
    def is_registered(cls, action_name: str) -> bool:
        return action_name in cls._handlers


# ════════════════════════════════════════════════════════════════
#  批次 1：轻量动作（done / wait / close_tab / goto / scroll / smooth_scroll）
# ════════════════════════════════════════════════════════════════

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
        logger.info("[CLOSE TAB] Closing current page and falling back to previous tab")
        try:
            await page.close()
            browser.rpa_trail.append(
                ctx.with_rpa_meta({"action": "close_tab", "type_value": ""})
            )
            logger.debug("[RPA] Recorded close_tab")
            logger.info("[CLOSE TAB] Page closed successfully")
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"[CLOSE TAB] Failed to close page: {e}")
        open_pages = [p for p in browser._context.pages if not p.is_closed()]
        if open_pages:
            browser._page = open_pages[-1]
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
                    logger.warning(f"[SMOOTH_SCROLL] {hint}")
                    browser._last_action_error = RuntimeError(hint)
            browser.rpa_trail.append(
                ctx.with_rpa_meta({"action": "smooth_scroll", "type_value": direction})
            )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"[SMOOTH_SCROLL] Failed: {e}")
        return None


# ════════════════════════════════════════════════════════════════
#  批次 2：交互动作（click / type / hover / press_key / select /
#                     drag_and_drop / remove_element / click_point / switch_tab）
# ════════════════════════════════════════════════════════════════

@ActionRegistry.register("click")
class ClickHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing click: element #{target_id} ({selector})")
        try:
            await browser._clear_som_overlays()
            target = await browser._resolve_action_target(target_id, "click")
            if target:
                await target.handle.scroll_into_view_if_needed(
                    timeout=browser._LOCATOR_TIMEOUT
                )
            else:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes"
                )
            _pending_xpath = await browser._get_xpath(target.handle)
            _pending_ax_role, _pending_ax_name = await browser._get_accessibility_signature(
                page, target.handle
            )

            try:
                await target.handle.click(
                    force=True, timeout=browser._LOCATOR_TIMEOUT
                )
            except Exception as native_err:
                logger.warning(
                    f"[JS CLICK FALLBACK] native click blocked for element #{target_id}: "
                    f"{native_err}"
                )
                await target.handle.evaluate(
                    """el => {
                        el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
                        if (typeof el.click === 'function') {
                            el.click();
                        } else {
                            el.dispatchEvent(new MouseEvent('click', {
                                bubbles: true,
                                cancelable: true,
                                composed: true,
                                view: window
                            }));
                        }
                    }"""
                )
            if _pending_xpath:
                browser.rpa_trail.append(
                    ctx.with_rpa_meta({
                        "action": "click",
                        "xpath": _pending_xpath,
                        "ax_role": _pending_ax_role or "",
                        "ax_name": _pending_ax_name or "",
                        "type_value": "",
                    })
                )
                logger.debug(f"[RPA] Recorded click: {_pending_xpath}")
            logger.info(f"Click element #{target_id} succeeded")
        except Exception as e:
            if _is_navigation_context_destroyed(e):
                logger.info(
                    f"[CLICK] element #{target_id} triggered navigation; "
                    f"ignoring stale execution-context error: {e}"
                )
            else:
                browser._last_action_error = e
                logger.error(
                    f"Click element #{target_id} failed (page may have refreshed): {e}"
                )

        await browser._wait_after_action(is_navigation=True)
        return None


@ActionRegistry.register("click_new_tab")
class ClickNewTabHandler(ActionHandler):
    """
    中键点击：强制在新标签页打开链接。

    定位逻辑与普通 click 完全一致（通过 SoM ID 找到对应 Playwright 元素），
    核心区别在于使用 middle-button click，浏览器会原生地在后台新标签页打开链接，
    而当前页保持不变。

    Tab Guard 会自动跟踪新开的标签页，但由于本 Handler 返回 None，
    dispatcher 会正常走 Tab Guard 流程处理页面切换。
    """

    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing click_new_tab: element #{target_id} ({selector})")
        try:
            await browser._clear_som_overlays()
            target = await browser._resolve_action_target(target_id, "click_new_tab")
            if target:
                await target.handle.scroll_into_view_if_needed(
                    timeout=browser._LOCATOR_TIMEOUT
                )
            else:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes"
                )
            _pending_xpath = await browser._get_xpath(target.handle)
            _pending_ax_role, _pending_ax_name = await browser._get_accessibility_signature(
                page, target.handle
            )

            # 核心：中键点击 → 浏览器原生在新标签页打开
            try:
                await target.handle.click(
                    button="middle", force=True, timeout=browser._LOCATOR_TIMEOUT
                )
            except Exception:
                # middle-click 失败时降级为 Ctrl+Click
                try:
                    await target.handle.click(
                        modifiers=["Control"], force=True,
                        timeout=browser._LOCATOR_TIMEOUT,
                    )
                except Exception:
                    # 最终降级：JS 层 window.open
                    href = await target.handle.evaluate(
                        """el => {
                            const a = el.closest('a') || el.querySelector('a');
                            return a ? a.href : (el.href || el.getAttribute('href') || '');
                        }"""
                    )
                    if href:
                        await page.evaluate(f"window.open({href!r}, '_blank')")
                    else:
                        raise RuntimeError(
                            f"Element #{target_id} has no href; "
                            f"cannot open in new tab. Use regular 'click' instead."
                        )

            if _pending_xpath:
                browser.rpa_trail.append(
                    ctx.with_rpa_meta({
                        "action": "click_new_tab",
                        "xpath": _pending_xpath,
                        "ax_role": _pending_ax_role or "",
                        "ax_name": _pending_ax_name or "",
                        "type_value": "",
                    })
                )
                logger.debug(f"[RPA] Recorded click_new_tab: {_pending_xpath}")
            logger.info(f"click_new_tab element #{target_id} succeeded")
        except Exception as e:
            browser._last_action_error = e
            logger.error(
                f"click_new_tab element #{target_id} failed: {e}"
            )

        await browser._wait_after_action(is_navigation=True)
        return None


@ActionRegistry.register("type")
class TypeHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        type_value = ctx.action.type_value
        workflow_memory = ctx.workflow_memory
        display_value = type_value
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing type: element #{target_id} <- {display_value!r}")
        try:
            await browser._clear_som_overlays()
            target = await browser._resolve_action_target(target_id, "type")

            if not target:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes"
                )

            await target.handle.scroll_into_view_if_needed(
                timeout=browser._LOCATOR_TIMEOUT
            )

            # 视口锁定预检（暗礁 1 防线）：
            # SoM ID 基于视口实时分配；如果输入框只露一半就 type，下一帧截图 ID 会洗牌，
            # VLM 会把同一字段误认成另一个字段反复覆写。强制要求目标元素**完整在视口内**，
            # 否则主动 scrollIntoView({block:"center"}) 居中 + 等待稳定后再操作。
            try:
                _viewport_check = await target.handle.evaluate(
                    """el => {
                        const r = el.getBoundingClientRect();
                        const vh = window.innerHeight;
                        const vw = window.innerWidth;
                        return {
                            top: r.top, bottom: r.bottom, left: r.left, right: r.right,
                            height: r.height, width: r.width, vh, vw,
                            fully_visible: r.top >= 0 && r.bottom <= vh && r.left >= 0 && r.right <= vw,
                            partial_clip: Math.max(0, -r.top) + Math.max(0, r.bottom - vh)
                        };
                    }"""
                )
                if _viewport_check and not _viewport_check.get("fully_visible"):
                    _clip = _viewport_check.get("partial_clip", 0) or 0
                    _h = _viewport_check.get("height", 1) or 1
                    if _h > 0 and (_clip / _h) > 0.2:  # 超过 20% 高度被截断
                        logger.info(
                            f"[TYPE] 目标输入框被视口截断 {_clip:.0f}/{_h:.0f}px "
                            f"({_clip / _h * 100:.0f}%)，强制 scrollIntoView({{block:'center'}})"
                        )
                        await target.handle.evaluate(
                            "el => el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'})"
                        )
                        await asyncio.sleep(0.4)  # 等滚动稳定
            except Exception as _vp_err:
                logger.debug(f"[TYPE] viewport precheck failed (non-fatal): {_vp_err}")

            _pending_xpath = await browser._get_xpath(target.handle)
            _pending_ax_role, _pending_ax_name = await browser._get_accessibility_signature(
                page, target.handle
            )

            count_before = await browser._count_interactive_elements()

            try:
                await target.handle.click(
                    force=True, timeout=browser._LOCATOR_TIMEOUT
                )
            except Exception:
                await target.handle.evaluate("""el => {
                    if (typeof el.click === 'function') el.click();
                    else el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                }""")

            await asyncio.sleep(0.5)

            # 动态插值：{{key}} → workflow_memory 中的真实值
            if workflow_memory and "{{" in type_value:
                def _interpolate(m: re.Match) -> str:
                    key = m.group(1).strip()
                    val = workflow_memory.get(key)
                    if val is None:
                        logger.warning(
                            f"[MEMORY] interpolation: key '{key}' not found in workflow_memory "
                            f"(available: {list(workflow_memory.keys())})"
                        )
                        return m.group(0)
                    return str(val)
                resolved = re.sub(r"\{\{([^}]+)\}\}", _interpolate, type_value)
                if resolved != type_value:
                    logger.info(
                        f"[MEMORY] type interpolation: {type_value!r} → {resolved!r}"
                    )
                type_value = resolved
                display_value = resolved

            env_template = display_value if "{{env:" in display_value else ""
            type_value, used_auth_vault, env_names = _resolve_env_placeholders(type_value)
            if used_auth_vault:
                display_value = env_template
                logger.info(
                    "[AUTH VAULT] type_value resolved from env placeholder(s): %s",
                    ", ".join(env_names),
                )

            modifier = "Meta" if sys.platform == "darwin" else "Control"
            await page.keyboard.press(f"{modifier}+a")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(type_value, delay=50)

            if _pending_xpath:
                browser.rpa_trail.append(
                    ctx.with_rpa_meta({
                        "action": "type",
                        "xpath": _pending_xpath,
                        "ax_role": _pending_ax_role or "",
                        "ax_name": _pending_ax_name or "",
                        "type_value": display_value if used_auth_vault else type_value,
                    })
                )
                logger.debug(
                    "[RPA] Recorded type: %s <- %r",
                    _pending_xpath,
                    display_value if used_auth_vault else type_value,
                )
            logger.info(f"Type into element #{target_id} succeeded")

            await asyncio.sleep(0.4)

            if await browser._is_calendar_popup_visible():
                await page.keyboard.press("Tab")
                logger.info(
                    f"[TYPE] Date picker calendar detected → dismissed with Tab. "
                    f"Next screenshot will show clean input state."
                )
            else:
                appeared = await browser._wait_for_submenu(count_before, max_wait=2.5)
                if appeared:
                    logger.info(
                        f"[TYPE] Async dropdown items appeared after input. "
                        f"Next screenshot will capture the option list for VLM to click."
                    )

        except Exception as e:
            browser._last_action_error = e
            logger.error(f"Type into element #{target_id} failed: {e}")

        await browser._wait_after_action(light_action=True)
        return None


@ActionRegistry.register("hover")
class HoverHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing hover: element #{target_id} ({selector})")
        try:
            await browser._clear_som_overlays()
            target = await browser._resolve_action_target(target_id, "click")
            if not target:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes"
                )
            await target.handle.scroll_into_view_if_needed(
                timeout=browser._LOCATOR_TIMEOUT
            )
            _pending_xpath = await browser._get_xpath(target.handle)
            _pending_ax_role, _pending_ax_name = await browser._get_accessibility_signature(
                page, target.handle
            )

            count_before = await browser._count_interactive_elements()

            await target.handle.hover(force=True, timeout=browser._LOCATOR_TIMEOUT)
            logger.info(f"Hover element #{target_id} succeeded")
            _hover_meta = None
            if _pending_xpath:
                _hover_meta = ctx.with_rpa_meta({
                    "action": "hover",
                    "xpath": _pending_xpath,
                    "ax_role": _pending_ax_role or "",
                    "ax_name": _pending_ax_name or "",
                    "type_value": "",
                })
                browser.rpa_trail.append(_hover_meta)

            appeared = await browser._wait_for_submenu(count_before, max_wait=2.0)
            if appeared:
                logger.info(
                    f"[HOVER] Submenu/dropdown appeared after hovering #{target_id}"
                )
                try:
                    tooltip_text = await page.evaluate(
                        """() => {
                            const selectors = [
                                '[role="tooltip"]',
                                '.el-tooltip__popper',
                                '.el-popper',
                                '.ant-tooltip-inner',
                                '.tooltip',
                                '.v-popper__inner'
                            ];
                            const seen = new Set();
                            const out = [];
                            for (const sel of selectors) {
                                for (const el of document.querySelectorAll(sel)) {
                                    const rect = el.getBoundingClientRect();
                                    const style = window.getComputedStyle(el);
                                    if (
                                        rect.width > 0 && rect.height > 0 &&
                                        style.visibility !== 'hidden' &&
                                        style.display !== 'none' &&
                                        Number(style.opacity || 1) > 0
                                    ) {
                                        const text = (el.innerText || el.textContent || '').trim();
                                        if (text && !seen.has(text)) {
                                            seen.add(text);
                                            out.push(text);
                                        }
                                    }
                                }
                            }
                            return out.join(' | ');
                        }"""
                    )
                    if tooltip_text:
                        logger.info("[HOVER] visible tooltip text: %s", tooltip_text)
                        if isinstance(_hover_meta, dict):
                            _hover_meta["tooltip_text"] = tooltip_text
                except Exception as _tooltip_err:
                    logger.debug("[HOVER] tooltip text capture skipped: %s", _tooltip_err)
            else:
                logger.warning(
                    f"[HOVER TIMEOUT] No new elements appeared after hovering "
                    f"#{target_id} within 2s — parent item may be incorrect or "
                    f"menu requires a click to open."
                )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"Hover element #{target_id} failed: {e}")

        await browser._wait_after_action(light_action=True)
        return None


@ActionRegistry.register("press_key")
class PressKeyHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        key_name = (ctx.action.type_value or "").strip()
        if not key_name:
            raise ActionExecutionError(
                "press_key 动作必须在 type_value 中提供按键名称（如 'Enter'、'Escape'、'Tab'）。"
            )
        logger.info(f"Executing press_key: {key_name!r} (target_id={target_id})")

        _atomic_done = False
        if target_id and int(target_id) > 0:
            try:
                await browser._clear_som_overlays()
                _pk_target = await browser._resolve_action_target(target_id, "click")
                if _pk_target:
                    await _pk_target.handle.press(
                        key_name, timeout=browser._LOCATOR_TIMEOUT
                    )
                    _atomic_done = True
                    logger.info(
                        f"[press_key] 原子级按键成功："
                        f"元素 #{target_id} + {key_name!r}"
                    )
                else:
                    logger.warning(
                        f"[press_key] 元素 #{target_id} 未找到，降级为全局按键"
                    )
            except Exception as _atomic_err:
                logger.warning(
                    f"[press_key] 原子级按键失败: {_atomic_err}，降级为全局按键"
                )

        if not _atomic_done:
            try:
                await page.keyboard.press(key_name)
                logger.info(f"Press key {key_name!r} (global) succeeded")
            except Exception as e:
                browser._last_action_error = e
                logger.error(f"Press key {key_name!r} failed: {e}")

        browser.rpa_trail.append(
            ctx.with_rpa_meta({"action": "press_key", "type_value": key_name})
        )
        logger.debug(f"[RPA] Recorded press_key: {key_name!r}")
        await browser._wait_after_action(is_navigation=(key_name == "Enter"))
        return None


@ActionRegistry.register("select")
class SelectHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        type_value = ctx.action.type_value
        selector = f'[data-som-id="{target_id}"]'
        logger.info(
            f"Executing select: element #{target_id} ({selector}) <- {type_value!r}"
        )
        try:
            await browser._clear_som_overlays()
            target = await browser._resolve_action_target(target_id, "click")
            if not target:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes"
                )
            _pending_xpath = await browser._get_xpath(target.handle)
            _pending_ax_role, _pending_ax_name = await browser._get_accessibility_signature(
                page, target.handle
            )
            try:
                await target.handle.select_option(
                    label=type_value, timeout=browser._LOCATOR_TIMEOUT
                )
            except Exception:
                try:
                    await target.handle.select_option(
                        value=type_value, timeout=browser._LOCATOR_TIMEOUT
                    )
                except Exception:
                    await target.handle.select_option(
                        index=0, timeout=browser._LOCATOR_TIMEOUT
                    )
            logger.info(f"Select element #{target_id} succeeded")
            if _pending_xpath:
                browser.rpa_trail.append(
                    ctx.with_rpa_meta({
                        "action": "select",
                        "xpath": _pending_xpath,
                        "ax_role": _pending_ax_role or "",
                        "ax_name": _pending_ax_name or "",
                        "type_value": type_value,
                    })
                )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"Select element #{target_id} failed: {e}")
        await browser._wait_after_action(is_navigation=False)
        return None


@ActionRegistry.register("drag_and_drop")
class DragAndDropHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        type_value = ctx.action.type_value
        drop_id = int(type_value) if type_value else 0
        logger.info(f"Executing drag_and_drop: #{target_id} → #{drop_id}")
        try:
            await browser._clear_som_overlays()
            source = await browser._resolve_action_target(target_id, "drag_and_drop")
            dest = await browser._resolve_action_target(drop_id, "drag_and_drop")
            if not source:
                raise RuntimeError(f"Drag source element #{target_id} not found")
            if not dest:
                raise RuntimeError(f"Drop target element #{drop_id} not found")
            await source.handle.scroll_into_view_if_needed(
                timeout=browser._LOCATOR_TIMEOUT
            )
            _src_xpath = await browser._get_xpath(source.handle)
            _src_ax_role, _src_ax_name = await browser._get_accessibility_signature(
                page, source.handle
            )
            _dst_xpath = await browser._get_xpath(dest.handle)

            await source.handle.drag_to(dest.handle, timeout=browser._LOCATOR_TIMEOUT)
            logger.info(f"drag_and_drop #{target_id} → #{drop_id} succeeded")
            if _src_xpath:
                browser.rpa_trail.append(
                    ctx.with_rpa_meta({
                        "action": "drag_and_drop",
                        "xpath": _src_xpath,
                        "ax_role": _src_ax_role or "",
                        "ax_name": _src_ax_name or "",
                        "type_value": str(drop_id),
                        "drop_xpath": _dst_xpath or "",
                    })
                )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"drag_and_drop #{target_id} → #{drop_id} failed: {e}")

        await browser._wait_after_action(light_action=False)
        return None


@ActionRegistry.register("remove_element")
class RemoveElementHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"[REMOVE_ELEMENT] Removing element #{target_id} ({selector})")
        try:
            await browser._clear_som_overlays()
            target = await browser._resolve_action_target(target_id, "remove_element")
            if not target:
                raise RuntimeError(
                    f"Element #{target_id} not found — cannot remove"
                )
            _pending_xpath = await browser._get_xpath(target.handle)
            _pending_ax_role, _pending_ax_name = await browser._get_accessibility_signature(
                page, target.handle
            )
            await target.handle.evaluate("""el => {
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('opacity', '0', 'important');
                el.style.setProperty('pointer-events', 'none', 'important');
                el.setAttribute('aria-hidden', 'true');
                el.inert = true;
            }""")
            logger.info(f"[REMOVE_ELEMENT] Element #{target_id} semantically hidden")
            print(
                f"🗑️  [REMOVE_ELEMENT] 已语义隐身元素 #{target_id} "
                f"(xpath={_pending_xpath})"
            )
            if _pending_xpath:
                browser.rpa_trail.append(
                    ctx.with_rpa_meta({
                        "action": "remove_element",
                        "xpath": _pending_xpath,
                        "ax_role": _pending_ax_role or "",
                        "ax_name": _pending_ax_name or "",
                        "type_value": "",
                    })
                )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"[REMOVE_ELEMENT] Failed for element #{target_id}: {e}")
        return None


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

    async def _wait_for_pagination_change(
        self, page, before: tuple[str, str, int, int], label: str
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
        if after != before:
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
            return True
        logger.info(f"[NEXT_PAGE] candidate {label} clicked but page did not change")
        return False

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
        click_mode = await _click_locator_with_js_fallback(loc, label, timeout=3000)
        return await self._wait_for_pagination_change(
            page, before, f"{label}/{click_mode}"
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
                    raise ActionExecutionError(
                        "next_page: 启发式翻页全失败 + 已滚到页面底部（scrollY 不再增长），"
                        "可能已是最后一页。请评估累计提取量，若已达目标输出 done。"
                    )
                # Fix 2 关键：滚轮动了但内容没增长 = 伪无限滚动（实际是分页器但无标准控件）
                # 这种情况 L4 不算成功，应当报错让上层换路（ask_human / done / click 真实 target_id）
                if _delta_text < 200 and _delta_items <= 0:
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

        async def _click_first_visible(locator: Any, label: str, limit: int = 20) -> str:
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


@ActionRegistry.register("switch_tab")
class SwitchTabHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        tab_index = ctx.action.target_id
        open_pages = [p for p in browser._context.pages if not p.is_closed()]
        if 0 <= tab_index < len(open_pages):
            target_page = open_pages[tab_index]

            await browser._register_page(
                target_page,
                reason=f"switch_tab[{tab_index}]",
                activate=True,
            )

            try:
                await target_page.bring_to_front()
            except Exception as _btf_err:
                logger.warning(f"[SWITCH TAB] bring_to_front 失败: {_btf_err}")

            await asyncio.sleep(0.5)
            try:
                await browser._page.wait_for_load_state(
                    "domcontentloaded", timeout=5000
                )
            except Exception:
                pass
            try:
                await browser._page.wait_for_load_state(
                    "networkidle", timeout=5000
                )
            except Exception:
                pass
            await asyncio.sleep(0.5)

            browser.rpa_trail.append(
                ctx.with_rpa_meta({"action": "switch_tab", "target_id": tab_index})
            )
            logger.debug(f"[RPA] Recorded switch_tab: {tab_index}")
            logger.info(
                f"[SWITCH TAB] Switched to tab [{tab_index}]: "
                f"{(browser._page.url or 'about:blank')[:80]}"
            )
            return browser._page  # 显式返回：dispatcher 跳过 Tab Guard
        else:
            raise ActionExecutionError(
                f"switch_tab 失败：标签页索引 {tab_index} 超出范围"
                f"（当前共 {len(open_pages)} 个标签页，有效索引 0~{len(open_pages)-1}）。"
            )


# ════════════════════════════════════════════════════════════════
#  批次 3：数据 & 记忆动作（extract_link / download_image /
#                          upload / save_to_memory）
# ════════════════════════════════════════════════════════════════

@ActionRegistry.register("extract_link")
class ExtractLinkHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing extract_link: element #{target_id} ({selector})")
        try:
            target = await browser._resolve_action_target(target_id, "click")
            if not target:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes"
                )
            extracted_url = await target.handle.evaluate(
                """el => el.getAttribute('data-som-url') || el.getAttribute('href') || el.getAttribute('src') || ''"""
            )
            if extracted_url:
                logger.info(
                    f"[EXTRACT_LINK] Successfully extracted URL: {extracted_url}"
                )
                print(
                    f"\n\033[1;32m[LINK EXTRACTED]\033[0m "
                    f"\033[36m{extracted_url}\033[0m\n"
                )
                try:
                    from .data_manager import save_to_excel
                except ImportError:
                    from data_manager import save_to_excel
                save_to_excel(
                    {"target_id": target_id, "url": extracted_url},
                    "output_links.xlsx",
                )
            else:
                logger.warning(
                    f"[EXTRACT_LINK] No URL found in data-som-url for element #{target_id}"
                )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"Extract link for element #{target_id} failed: {e}")
        return None


@ActionRegistry.register("download_image")
class DownloadImageHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing download_image: element #{target_id} ({selector})")
        try:
            target = await browser._resolve_action_target(target_id, "click")
            if not target:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes"
                )
            extracted_url = await target.handle.evaluate(
                """el => el.getAttribute('data-som-url') || el.getAttribute('src') || el.getAttribute('href') || ''"""
            )

            if extracted_url:
                abs_url = urllib.parse.urljoin(page.url, extracted_url)
                logger.info(f"[DOWNLOAD_IMAGE] Target URL: {abs_url}")

                response = await browser._context.request.get(abs_url)
                img_bytes = await response.body()

                img_dir = browser._download_dir / "images"
                img_dir.mkdir(parents=True, exist_ok=True)

                content_type = response.headers.get("content-type", "")
                ext = ".png"
                if "jpeg" in content_type or "jpg" in content_type:
                    ext = ".jpg"
                elif "gif" in content_type:
                    ext = ".gif"

                filename = f"img_{int(time.time())}{ext}"
                filepath = img_dir / filename
                filepath.write_bytes(img_bytes)

                print(
                    f"\n\033[1;32m✅ 成功下载图片:\033[0m "
                    f"\033[36m{filepath.resolve()}\033[0m\n"
                )
                logger.info(f"[DOWNLOAD_IMAGE] Saved local: {filepath.resolve()}")
                register_artifact(filepath)
            else:
                logger.warning(
                    f"[DOWNLOAD_IMAGE] No URL found for element #{target_id}"
                )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"Download image for element #{target_id} failed: {e}")
        return None


@ActionRegistry.register("upload")
class UploadHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        target_id = ctx.action.target_id
        type_value = ctx.action.type_value
        logger.info(f"Executing upload: element #{target_id} <- {type_value!r}")
        file_path: Optional[Path] = None
        if type_value and type_value.strip():
            candidate = Path(type_value.strip())
            if candidate.exists():
                file_path = candidate
                logger.info(f"[UPLOAD] Using path from type_value: {file_path}")
        if file_path is None and browser._upload_file and browser._upload_file.exists():
            file_path = browser._upload_file
            logger.info(f"[UPLOAD] Using pre-configured --upload-file: {file_path}")
        if file_path is None:
            logger.error(
                f"[UPLOAD] No valid file found. "
                f"Provide a real path via --upload-file or in type_value. "
                f"Got: {type_value!r}"
            )
            return None
        try:
            await browser._clear_som_overlays()
            file_target = await browser._find_file_input(target_id)
            if not file_target:
                raise RuntimeError(
                    f"No <input type='file'> found near element #{target_id}"
                )
            await file_target.handle.set_input_files(str(file_path.resolve()))
            logger.info(
                f"[UPLOAD] File injected successfully: {file_path.resolve()}"
            )
            print(
                f"\n\033[1;32m✅ 文件上传成功:\033[0m "
                f"\033[36m{file_path.resolve()}\033[0m\n"
            )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"[UPLOAD] Failed for element #{target_id}: {e}")

        await browser._wait_after_action(light_action=True)
        return None


@ActionRegistry.register("save_to_memory")
class SaveToMemoryHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        target_id = ctx.action.target_id
        type_value = ctx.action.type_value
        memory_key = ctx.action.memory_key or ""
        workflow_memory = ctx.workflow_memory

        effective_key = memory_key
        if not effective_key:
            effective_key = f"temp_var_{int(time.time())}"
            logger.warning(
                f"[MEMORY] save_to_memory: memory_key missing, "
                f"auto-generated key='{effective_key}'"
            )
            print(
                f"\033[1;33m⚠️  [MEMORY]\033[0m "
                f"VLM 未提供 memory_key，已动态生成临时命名为 '{effective_key}'"
            )

        logger.info(
            f"Executing save_to_memory: element #{target_id} → key={effective_key!r}"
        )

        try:
            extracted_text: str = ""

            if type_value and type_value.strip():
                extracted_text = type_value.strip()
                logger.info(
                    f"[MEMORY] Value sourced from type_value: {extracted_text!r}"
                )
                print(
                    f"\033[36m🧠 [MEMORY]\033[0m "
                    f"VLM 直接传递了文本值: {extracted_text!r}"
                )

            elif target_id and target_id != 0:
                await browser._clear_som_overlays()
                target = await browser._resolve_action_target(target_id, "click")
                if not target:
                    raise RuntimeError(
                        f"Element #{target_id} not found on active page or its iframes"
                    )
                extracted_text = str(
                    await target.handle.evaluate(
                        """el => {
                            const text = (el.innerText || el.textContent || '').trim();
                            const val  = (el.value || '').trim();
                            return text || val || '';
                        }"""
                    )
                ).strip()
                logger.info(
                    f"[MEMORY] Value sourced from DOM element #{target_id}: "
                    f"{extracted_text!r}"
                )
                print(
                    f"\033[36m🔍 [MEMORY]\033[0m "
                    f"从页面元素提取了文本: {extracted_text!r}"
                )

            else:
                raise RuntimeError(
                    "save_to_memory 失败：VLM 既未提供 type_value，"
                    "也未提供有效的 target_id，无法获取要保存的值"
                )

            if not extracted_text:
                logger.warning(
                    f"[MEMORY] save_to_memory: extracted value is empty "
                    f"(element #{target_id})"
                )
            else:
                if workflow_memory is not None:
                    workflow_memory[effective_key] = extracted_text
                    workflow_memory["latest_memory"] = extracted_text
                logger.info(
                    f"[MEMORY] Saved: {effective_key!r} = {extracted_text!r} "
                    f"(latest_memory also updated)"
                )
                print(
                    f"\n\033[1;36m[MEMORY SAVED]\033[0m "
                    f"\033[33m{effective_key}\033[0m = "
                    f"\033[32m{extracted_text!r}\033[0m  "
                    f"\033[90m(latest_memory 已同步)\033[0m\n"
                )

        except Exception as e:
            browser._last_action_error = e
            logger.error(f"[MEMORY] save_to_memory failed: {e}")

        await browser._wait_after_action(light_action=True)
        return None

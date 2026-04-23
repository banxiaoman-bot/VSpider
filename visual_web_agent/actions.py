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
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, ConfigDict

try:
    from .vlm_client import VSpiderAction
    from .browser_env import ActionExecutionError
except ImportError:
    from vlm_client import VSpiderAction
    from browser_env import ActionExecutionError

if TYPE_CHECKING:
    from playwright.async_api import Page
    from .browser_env import BrowserEnv

logger = logging.getLogger("vspider.actions")


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
            except Exception:
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
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing type: element #{target_id} <- {type_value!r}")
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
                        "type_value": type_value,
                    })
                )
                logger.debug(f"[RPA] Recorded type: {_pending_xpath} <- {type_value!r}")
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
            if _pending_xpath:
                browser.rpa_trail.append(
                    ctx.with_rpa_meta({
                        "action": "hover",
                        "xpath": _pending_xpath,
                        "ax_role": _pending_ax_role or "",
                        "ax_name": _pending_ax_name or "",
                        "type_value": "",
                    })
                )

            appeared = await browser._wait_for_submenu(count_before, max_wait=2.0)
            if appeared:
                logger.info(
                    f"[HOVER] Submenu/dropdown appeared after hovering #{target_id}"
                )
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

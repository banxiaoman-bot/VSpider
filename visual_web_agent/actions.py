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
import json
import logging
import re
import sys
import time
import urllib.parse
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field

try:
    from .vlm_client import VSpiderAction
    from .browser_env import ActionExecutionError
    from .auth_vault import SecretResolutionError, resolve_env_placeholders
    from .artifact_manager import register_download_artifact
    from .page_data_controller import DATA_SIGNATURE_JS, pagination_moved
    from .chat_answer_extractor import clean_chat_answer_text, extract_chat_answer
    from .chat_send_locator import find_send_button as _chat_find_send_button
except ImportError:
    from vlm_client import VSpiderAction
    from browser_env import ActionExecutionError
    from auth_vault import SecretResolutionError, resolve_env_placeholders
    from artifact_manager import register_download_artifact
    from page_data_controller import DATA_SIGNATURE_JS, pagination_moved
    from chat_answer_extractor import clean_chat_answer_text, extract_chat_answer
    from chat_send_locator import find_send_button as _chat_find_send_button

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
    # Keep this as Any, not ``dict``. Pydantic copies plain dict fields during
    # validation, which breaks the shared workflow_memory contract: handlers
    # write sentinels such as ``__chat_extract_completed`` and main.py must see
    # those writes on the caller's original dict.
    workflow_memory: Any
    page: Any  # playwright.async_api.Page

    # RPA 元数据（原本透过 dict 的 __rpa_* 字段传递；现显式承载）
    rpa_required_keys: list[str] = []
    rpa_template_value: str = ""

    # Cross-system session routing service (A1, Path-2). browser_env threads
    # the run's SessionRouter here ONLY when VSPIDER_CROSS_SYSTEM_SWITCH is on
    # (main.py attaches it to the browser), so GotoHandler can decide
    # pre-navigation whether a goto is a cross-system hop. None (flag off /
    # single-system run) -> handlers behave exactly as before.
    session_router: Any = None

    # G3: 关联 ID — 每个 ActionContext 实例自动获得一个 8 字符的 trace_id，
    # 由 with_rpa_meta() 自动盖章到每条 RPA trail 条目上，方便前端把
    # 「同一次 VLM 决策产生的多条动作」聚合成一组。
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])

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
        # Cross-system RPA replay (Slice RPA-XSYS): stamp the planned system
        # the step ran in so _replay_rpa can switch the active browser to it
        # before replaying. Only when a session_router is attached (i.e.
        # VSPIDER_CROSS_SYSTEM_SWITCH on) -> no key, byte-identical, when off.
        if self.session_router is not None:
            try:
                _sys_id = self.session_router.system_for_url(
                    getattr(self.browser, "current_url", "") or ""
                )
                if _sys_id:
                    step.setdefault("system_id", _sys_id)
            except Exception:
                pass
        # G3: stamp trace_id once per RPA trail entry (don't clobber a
        # caller-provided override, hence setdefault).
        step.setdefault("trace_id", self.trace_id)
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


# ════════════════════════════════════════════════════════════════
#  批次 2：交互动作（click / type / hover / press_key / select /
#                     drag_and_drop / remove_element / click_point / switch_tab）
# ════════════════════════════════════════════════════════════════

@ActionRegistry.register("find_text")
class FindTextHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        page = ctx.page
        text = (ctx.action.type_value or "").strip()
        if not text:
            raise ActionExecutionError("find_text 缺少 type_value，请填写要定位的可见文字或字段标签。")

        result = await page.evaluate(
            """(query) => {
                const q = String(query || '').trim().toLowerCase();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el.innerText, el.textContent, el.getAttribute('aria-label'),
                    el.getAttribute('placeholder'), el.getAttribute('title'),
                    el.getAttribute('value'), el.name, el.id
                ].filter(Boolean).join(' ').trim();
                const selector = [
                    'main *', '[role=main] *', 'article *', 'section *', 'form *',
                    'label', 'input', 'textarea', 'select', 'button',
                    '[role=button]', '[role=textbox]', '[role=combobox]',
                    '[role=checkbox]', '[role=radio]', 'h1', 'h2', 'h3', 'h4',
                    'p', 'div', 'span'
                ].join(',');
                const nodes = Array.from(document.querySelectorAll(selector));
                const candidates = [];
                for (const el of nodes) {
                    if (!isVisible(el)) continue;
                    const t = textOf(el);
                    if (!t || !t.toLowerCase().includes(q)) continue;
                    const r = el.getBoundingClientRect();
                    let score = 0;
                    const tag = el.tagName.toLowerCase();
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    if (['label','input','textarea','select','button','h1','h2','h3','h4'].includes(tag)) score += 40;
                    if (['textbox','combobox','checkbox','radio','button'].includes(role)) score += 30;
                    if (el.closest('form')) score += 35;
                    if (el.closest('main,[role=main],article')) score += 15;
                    if (r.left > 180) score += 25;
                    if (r.width * r.height < 150000) score += 10;
                    if (t.trim().toLowerCase() === q) score += 25;
                    score -= Math.abs((r.top + r.height / 2) - window.innerHeight / 2) / 80;
                    candidates.push({el, score, tag, role, text: t.slice(0, 120), left: r.left, top: r.top});
                }
                candidates.sort((a, b) => b.score - a.score);
                const best = candidates[0];
                if (!best) return {found: false, query};
                best.el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                return {
                    found: true, query, tag: best.tag, role: best.role,
                    text: best.text, left: best.left, top: best.top, score: best.score
                };
            }""",
            text,
        )
        await asyncio.sleep(0.6)
        if not result.get("found"):
            raise ActionExecutionError(
                f"find_text 未找到可见文本 {text!r}。请换更短的字段标签/按钮文字，或先小幅滚动。"
            )
        logger.info(
            "[FIND_TEXT] query=%r matched tag=%s role=%s text=%r score=%s",
            text,
            result.get("tag"),
            result.get("role"),
            result.get("text"),
            result.get("score"),
        )
        print(f"[FIND_TEXT] located text: {text}")
        ctx.browser.rpa_trail.append(
            ctx.with_rpa_meta({"action": "find_text", "type_value": text})
        )
        return None


def _parse_form_set_payload(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    if not text:
        raise ActionExecutionError(
            "form_set 缺少 type_value。格式示例：Activity name=VSpider 测试"
        )
    if text.startswith("{"):
        try:
            data = json.loads(text)
            label = str(data.get("label") or data.get("field") or "").strip()
            value = str(data.get("value") or "").strip()
            if label:
                return label, value
        except Exception:
            pass
    for sep in ("=>", "=", "：", ":"):
        if sep in text:
            left, right = text.split(sep, 1)
            label = left.strip().strip("\"'")
            value = right.strip().strip("\"'")
            if label:
                return label, value
    raise ActionExecutionError(
        "form_set 的 type_value 无法解析。请使用 `字段标签=目标值`，例如 `Activity zone=Zone one`。"
    )


async def _click_visible_text_option(page: "Page", value: str) -> bool:
    if not value:
        return False
    option_selectors = [
        # Element Plus / Element UI
        ".el-select-dropdown__item",
        ".el-dropdown-menu__item",
        ".el-cascader-node",
        ".el-radio",
        ".el-checkbox",
        # Ant Design
        ".ant-select-item",
        ".ant-select-item-option",
        ".ant-dropdown-menu-item",
        # 通用 ARIA
        "[role='option']",
        "[role='menuitem']",
        "[role='listitem']",
        # Naive UI / Vant
        ".n-base-select-option",
        ".van-picker-column__item",
        # 兜底
        "label", "li", "button", "span",
    ]
    # 先轮询等待选项浮层就绪（popper 动画 + teleport 渲染 ~600ms）
    import time as _t
    _start = _t.monotonic()
    _deadline = _start + 2.5  # 最多等 2.5s 等浮层渲染
    while _t.monotonic() < _deadline:
        for selector in option_selectors:
            try:
                loc = page.locator(selector).filter(has_text=value)
                count = await loc.count()
                if count:
                    # 优先 last（避免命中标签），不行再 first
                    for _picker in (loc.last, loc.first):
                        target = _picker
                        try:
                            if await target.is_visible(timeout=1500):
                                await _click_locator_with_js_fallback(
                                    target, f"form option {value!r}", timeout=2000
                                )
                                return True
                        except Exception:
                            continue
            except Exception:
                continue
        # popper 还没就绪，短暂等待后重试
        await asyncio.sleep(0.3)
    # 最后兜底：精确文字匹配整个 page
    try:
        loc = page.get_by_text(value, exact=True).last
        if await loc.is_visible(timeout=1500):
            await _click_locator_with_js_fallback(
                loc, f"form text option {value!r}", timeout=2000
            )
            return True
    except Exception:
        pass
    # 子串模糊兜底
    try:
        loc = page.get_by_text(value, exact=False).first
        if await loc.is_visible(timeout=1500):
            await _click_locator_with_js_fallback(
                loc, f"form text fuzzy {value!r}", timeout=2000
            )
            return True
    except Exception:
        return False
    return False


async def _form_set_bound_control_v2(
    page: "Page", label: str, value: str, *, open_if_needed: bool = True
) -> dict[str, Any]:
    """Set a form field by binding the visible label to one concrete control."""
    result = await page.evaluate(
        """async ({label, value, openIfNeeded}) => {
            const norm = (s) => String(s || '')
                .replace(/\\s+/g, ' ')
                .replace(/[：:]+$/g, '')
                .trim()
                .toLowerCase();
            const labelNorm = norm(label);
            const expected = norm(value);
            const isVisible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    s.display !== 'none' && s.visibility !== 'hidden' &&
                    Number(s.opacity || '1') > 0 &&
                    r.bottom >= 0 && r.right >= 0 &&
                    r.top <= (window.innerHeight || document.documentElement.clientHeight) &&
                    r.left <= (window.innerWidth || document.documentElement.clientWidth);
            };
            const textOf = (el) => [
                el?.innerText,
                el?.textContent,
                el?.getAttribute?.('aria-label'),
                el?.getAttribute?.('placeholder'),
                el?.getAttribute?.('title')
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
            const controlSelector = [
                'input:not([type=hidden])',
                'textarea',
                'select',
                '[contenteditable=true]',
                '[role=combobox]',
                '[role=checkbox]',
                '[role=radio]',
                '.el-select',
                '.ant-select',
                '.n-select',
                '.el-input',
                '.ant-input-affix-wrapper'
            ].join(',');
            const allControls = () => Array.from(document.querySelectorAll(controlSelector))
                .filter(isVisible)
                .filter(el => !['button', 'submit', 'reset', 'hidden'].includes(String(el.type || '').toLowerCase()));
            const readValue = (el) => {
                if (!el) return '';
                const tag = String(el.tagName || '').toLowerCase();
                const role = String(el.getAttribute?.('role') || '').toLowerCase();
                if (tag === 'select') {
                    const opt = el.selectedOptions?.[0];
                    return [el.value, opt?.textContent].filter(Boolean).join(' ').trim();
                }
                if (tag === 'input' || tag === 'textarea') {
                    const type = String(el.type || '').toLowerCase();
                    if (type === 'checkbox' || role === 'checkbox') return el.checked ? 'true' : 'false';
                    if (type === 'radio' || role === 'radio') return el.checked ? (el.value || textOf(el)) : '';
                    const rawValue = String(el.value || '').trim();
                    const auto = String(el.getAttribute?.('aria-autocomplete') || el.getAttribute?.('autocomplete') || '').toLowerCase();
                    if (!rawValue && (role === 'combobox' || auto === 'list' || auto === 'both')) {
                        let cur = el.parentElement;
                        for (let i = 0; cur && i < 5; i++, cur = cur.parentElement) {
                            const text = [
                                cur.innerText,
                                cur.getAttribute?.('aria-label'),
                                cur.getAttribute?.('title')
                            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                            if (text && text.length <= 180) return text;
                        }
                    }
                    return rawValue;
                }
                if (el.isContentEditable) return String(el.innerText || el.textContent || '').trim();
                const nested = el.querySelector?.('input:not([type=hidden]),textarea,select,[contenteditable=true]');
                if (nested && isVisible(nested)) return readValue(nested);
                return textOf(el);
            };
            const setNativeValue = (el, val) => {
                const tag = String(el.tagName || '').toLowerCase();
                if (el.isContentEditable) {
                    el.textContent = val;
                    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: val}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return;
                }
                if (tag === 'select') {
                    const opts = Array.from(el.options || []);
                    const hit = opts.find(o => norm(o.textContent) === expected || norm(o.value) === expected) ||
                        opts.find(o => norm(o.textContent).includes(expected) || norm(o.value).includes(expected));
                    if (hit) el.value = hit.value;
                    else el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return;
                }
                const proto = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                try { el.focus?.({preventScroll: true}); } catch (_) {}
                try { el.dispatchEvent(new FocusEvent('focus', {bubbles: false})); } catch (_) {}
                if (setter) setter.call(el, val); else el.value = val;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.blur?.();
            };
            const clickEl = (el) => {
                el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                const r = el.getBoundingClientRect();
                const cx = r.left + r.width / 2;
                const cy = r.top + r.height / 2;
                const init = {
                    bubbles: true, cancelable: true, composed: true, view: window,
                    button: 0, buttons: 1, clientX: cx, clientY: cy, screenX: cx, screenY: cy
                };
                el.dispatchEvent(new MouseEvent('mousedown', init));
                el.dispatchEvent(new MouseEvent('mouseup', init));
                el.dispatchEvent(new MouseEvent('click', init));
            };
            const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
            const allVisible = (selector, root = document) =>
                Array.from(root.querySelectorAll(selector)).filter(isVisible);
            const parseDateTarget = (raw) => {
                const s = String(raw || '').trim();
                let m = s.match(/(20\\d{2})[-/.](\\d{1,2})[-/.](\\d{1,2})/);
                if (m) {
                    return {year: Number(m[1]), month: Number(m[2]), day: Number(m[3])};
                }
                m = s.match(/(?:next month|下个月|下月).*?(\\d{1,2})/i);
                if (m) {
                    const now = new Date();
                    return {year: now.getFullYear(), month: now.getMonth() + 2, day: Number(m[1])};
                }
                return null;
            };
            const clickDateValue = async (raw, opener) => {
                const target = parseDateTarget(raw);
                if (!target || !target.year || !target.month || !target.day) return null;
                while (target.month > 12) {
                    target.month -= 12;
                    target.year += 1;
                }
                clickEl(opener);
                await sleep(350);
                const monthNames = {
                    january: 1, february: 2, march: 3, april: 4, may: 5, june: 6,
                    july: 7, august: 8, september: 9, october: 10, november: 11, december: 12,
                    jan: 1, feb: 2, mar: 3, apr: 4, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9,
                    oct: 10, nov: 11, dec: 12
                };
                const panelRoots = () => allVisible(
                    '.el-picker-panel,.ant-picker-dropdown,.n-date-panel,.mx-datepicker-main,.datepicker,[role=dialog],.el-popper'
                );
                const visiblePanelMonth = () => {
                    const panels = panelRoots();
                    const root = panels[panels.length - 1] || document;
                    const txt = textOf(root);
                    let m = txt.match(/(20\\d{2})\\s*[-/.年 ]\\s*(1[0-2]|0?[1-9])/);
                    if (m) return {year: Number(m[1]), month: Number(m[2])};
                    m = txt.match(/(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\\s+(20\\d{2})/i);
                    if (m) return {year: Number(m[2]), month: monthNames[m[1].toLowerCase()]};
                    m = txt.match(/(20\\d{2})\\s+(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)/i);
                    if (m) return {year: Number(m[1]), month: monthNames[m[2].toLowerCase()]};
                    const currentVal = String(opener?.value || '');
                    m = currentVal.match(/^(20\\d{2})[-/.](\\d{1,2})[-/.]/);
                    if (m) return {year: Number(m[1]), month: Number(m[2])};
                    const now = new Date();
                    return {year: now.getFullYear(), month: now.getMonth() + 1};
                };
                const panelMonth = visiblePanelMonth();
                const monthDelta = (target.year - panelMonth.year) * 12 + (target.month - panelMonth.month);
                const nextSelectors = [
                    '.el-picker-panel__icon-btn.arrow-right',
                    '.ant-picker-header-next-btn',
                    'button[aria-label*="Next month"]',
                    'button[title*="Next month"]'
                ].join(',');
                const prevSelectors = [
                    '.el-picker-panel__icon-btn.arrow-left',
                    '.ant-picker-header-prev-btn',
                    'button[aria-label*="Previous month"]',
                    'button[title*="Previous month"]'
                ].join(',');
                const navSelector = monthDelta >= 0 ? nextSelectors : prevSelectors;
                for (let i = 0; i < Math.min(Math.abs(monthDelta), 24); i++) {
                    const btn = allVisible(navSelector).find(el =>
                        !el.disabled && el.getAttribute('aria-disabled') !== 'true'
                    );
                    if (!btn) break;
                    clickEl(btn);
                    await sleep(180);
                }
                const ymd = `${target.year}-${String(target.month).padStart(2, '0')}-${String(target.day).padStart(2, '0')}`;
                const dayText = String(target.day);
                for (let i = 0; i < 10; i++) {
                    const panels = panelRoots();
                    const root = panels[panels.length - 1] || document;
                    const cells = allVisible('td,button,[role=gridcell],.el-date-table-cell,.ant-picker-cell-inner', root);
                    const hits = [];
                    for (const el of cells) {
                        const cell = el.closest('td,button,[role=gridcell]') || el;
                        if (!isVisible(cell)) continue;
                        const disabled = cell.matches('.disabled,.is-disabled,.ant-picker-cell-disabled,[aria-disabled=true]') ||
                            cell.closest('.disabled,.is-disabled,.ant-picker-cell-disabled,[aria-disabled=true]');
                        if (disabled) continue;
                        const rawText = [
                            textOf(el), textOf(cell),
                            el.getAttribute('aria-label'), cell.getAttribute('aria-label'),
                            el.getAttribute('title'), cell.getAttribute('title')
                        ].filter(Boolean).join(' ');
                        const t = norm(rawText);
                        let score = 0;
                        if (t === norm(dayText)) score += 80;
                        if (t.includes(norm(ymd))) score += 160;
                        if (t.includes(String(target.year)) && t.includes(dayText)) score += 40;
                        if (cell.classList?.contains('prev-month') || cell.classList?.contains('next-month')) score -= 100;
                        if (cell.classList?.contains('available') || cell.classList?.contains('ant-picker-cell-in-view')) score += 20;
                        if (score > 0) hits.push({cell, score});
                    }
                    hits.sort((a, b) => b.score - a.score);
                    if (hits[0]) {
                        clickEl(hits[0].cell);
                        await sleep(250);
                        return ymd;
                    }
                    await sleep(120);
                }
                return null;
            };
            const directAttrScore = (el) => {
                const attrs = ['aria-label', 'placeholder', 'title', 'name', 'id'];
                let best = 0;
                for (const attr of attrs) {
                    const t = norm(el.getAttribute?.(attr));
                    if (!t) continue;
                    if (t === labelNorm) best = Math.max(best, 240);
                    else if (t.includes(labelNorm) || (t.length >= 5 && labelNorm.includes(t))) best = Math.max(best, 150);
                }
                return best;
            };
            const findLabelHits = () => {
                const nodes = Array.from(document.querySelectorAll(
                    'label,[for],.el-form-item__label,.ant-form-item-label,.n-form-item-label,[class*=label],span,div'
                )).filter(isVisible);
                const hits = [];
                for (const el of nodes) {
                    const t = norm(textOf(el));
                    if (!t) continue;
                    const exact = t === labelNorm;
                    const contains = t.includes(labelNorm) || labelNorm.includes(t);
                    if (!exact && !contains) continue;
                    const r = el.getBoundingClientRect();
                    let score = exact ? 220 : 90;
                    if (el.matches('label,[for],.el-form-item__label,.ant-form-item-label,.n-form-item-label')) score += 60;
                    if (r.width * r.height > 120000) score -= 90;
                    hits.push({el, rect: r, score, text: textOf(el)});
                }
                hits.sort((a, b) => b.score - a.score);
                return hits;
            };
            const nearestContainer = (el) => {
                let cur = el;
                for (let i = 0; cur && i < 8; i++, cur = cur.parentElement) {
                    if (cur !== el && cur.querySelectorAll?.(controlSelector).length) {
                        if (
                            cur.matches?.('.el-form-item,.ant-form-item,.n-form-item,.form-group,[class*=form-item],[class*=field]') ||
                            cur.tagName?.toLowerCase() === 'label'
                        ) return cur;
                    }
                }
                cur = el.parentElement;
                for (let i = 0; cur && i < 5; i++, cur = cur.parentElement) {
                    if (cur.querySelectorAll?.(controlSelector).length === 1) return cur;
                }
                return el.parentElement || el;
            };
            const bind = () => {
                const controls = allControls();
                const direct = controls
                    .map(el => ({el, score: directAttrScore(el), reason: 'direct_attr'}))
                    .filter(x => x.score > 0)
                    .sort((a, b) => b.score - a.score)[0];
                if (direct) return direct;

                const labels = findLabelHits();
                for (const hit of labels) {
                    const forId = hit.el.getAttribute?.('for');
                    if (forId) {
                        const target = document.getElementById(forId);
                        if (target && isVisible(target)) return {el: target, score: 260, reason: 'label_for'};
                    }
                    const container = nearestContainer(hit.el);
                    const localControls = Array.from(container.querySelectorAll?.(controlSelector) || [])
                        .filter(isVisible)
                        .filter(el => el !== hit.el);
                    if (localControls.length === 1) {
                        return {el: localControls[0], score: hit.score + 40, reason: 'local_unique'};
                    }
                }
                if (!labels.length) return null;

                const candidates = [];
                for (const hit of labels.slice(0, 5)) {
                    const lr = hit.rect;
                    const ly = lr.top + lr.height / 2;
                    for (const el of controls) {
                        if (el === hit.el || hit.el.contains?.(el)) continue;
                        const cr = el.getBoundingClientRect();
                        const cy = cr.top + cr.height / 2;
                        const vertical = Math.abs(cy - ly);
                        const rightGap = cr.left - lr.right;
                        const belowGap = cr.top - lr.bottom;
                        let score = hit.score;
                        if (vertical < Math.max(32, Math.max(lr.height, cr.height) * 0.9) && rightGap > -20) {
                            score += 180 - vertical - Math.max(0, rightGap) / 20;
                        } else if (belowGap >= -8 && belowGap < 80 && Math.abs(cr.left - lr.left) < 220) {
                            score += 110 - belowGap;
                        } else {
                            score -= vertical;
                        }
                        score += directAttrScore(el) / 3;
                        if (cr.width < 8 || cr.height < 8) score -= 100;
                        candidates.push({el, score, reason: 'geometry', labelText: hit.text});
                    }
                }
                candidates.sort((a, b) => b.score - a.score);
                return candidates[0] || null;
            };
            const tryChoiceByLabel = () => {
                if (!expected) return null;
                const labels = findLabelHits();
                for (const hit of labels.slice(0, 5)) {
                    let cur = hit.el;
                    for (let depth = 0; cur && depth < 8; depth++, cur = cur.parentElement) {
                        const text = norm(textOf(cur));
                        if (!text.includes(expected)) continue;
                        const options = allVisible(
                            'label,.el-radio,.el-checkbox,.custom-control-label,[role=radio],[role=checkbox],button',
                            cur
                        ).filter(el => {
                            const t = norm(textOf(el));
                            if (!t || t.length > Math.max(80, expected.length + 40)) return false;
                            return t === expected || t.includes(expected);
                        }).map(el => {
                            const r = el.getBoundingClientRect();
                            let score = 100;
                            const t = norm(textOf(el));
                            if (t === expected) score += 180;
                            if (el.matches('label,.el-radio,.el-checkbox,.custom-control-label,[role=radio],[role=checkbox]')) score += 120;
                            if (r.width * r.height > 80000) score -= 160;
                            return {el, score};
                        }).sort((a, b) => b.score - a.score);
                        if (!options[0]) continue;
                        const option = options[0].el;
                        const target = option.closest?.('label,.el-radio,.el-checkbox,.custom-control-label,[role=radio],[role=checkbox]') || option;
                        clickEl(target);
                        return {
                            ok: true,
                            mode: 'choice_text',
                            binding: 'label_scope_choice',
                            observed: textOf(target)
                        };
                    }
                }
                return null;
            };

            const choiceResult = tryChoiceByLabel();
            if (choiceResult) return choiceResult;

            const binding = bind();
            if (!binding?.el) return {ok: false, reason: 'label_or_control_not_found', label};
            let control = binding.el;
            control.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
            const before = readValue(control);
            const beforeRole = String(control.getAttribute?.('role') || '').toLowerCase();
            const beforeAuto = String(control.getAttribute?.('aria-autocomplete') || control.getAttribute?.('autocomplete') || '').toLowerCase();
            const beforeComboLike = beforeRole === 'combobox' || beforeAuto === 'list' || beforeAuto === 'both';
            if (expected && (norm(before) === expected || (beforeComboLike && norm(before).includes(expected)))) {
                return {ok: true, mode: 'already_set', binding: binding.reason, observed: before};
            }
            const tag = String(control.tagName || '').toLowerCase();
            const role = String(control.getAttribute?.('role') || '').toLowerCase();
            const type = String(control.type || '').toLowerCase();

            if (tag === 'select') {
                setNativeValue(control, value);
            } else if (type === 'checkbox' || role === 'checkbox') {
                const want = !/^(false|off|no|0)$/i.test(String(value || 'true'));
                if (Boolean(control.checked) !== want) clickEl(control);
            } else if (type === 'radio' || role === 'radio') {
                clickEl(control);
            } else {
                const input = control.matches?.('input,textarea,[contenteditable=true]')
                    ? control
                    : control.querySelector?.('input:not([type=hidden]),textarea,[contenteditable=true]');
                const inputRole = String(input?.getAttribute?.('role') || '').toLowerCase();
                const inputAuto = String(input?.getAttribute?.('aria-autocomplete') || input?.getAttribute?.('autocomplete') || '').toLowerCase();
                const popup = String(input?.getAttribute?.('aria-haspopup') || control.getAttribute?.('aria-haspopup') || '').toLowerCase();
                const readonlyCombo = input && (input.readOnly || inputRole === 'combobox' || role === 'combobox' || popup === 'listbox' || popup === 'true');
                const dateTarget = parseDateTarget(value);
                if (readonlyCombo) {
                    if (!openIfNeeded) {
                        return {ok: false, reason: 'readonly_combo_not_opened', binding: binding.reason, observed: before};
                    }
                    const marker = '__vspider_form_bound_control__';
                    document.querySelectorAll(`[data-${marker}]`).forEach(el => el.removeAttribute(`data-${marker}`));
                    let opener = control;
                    for (const sel of ['.el-select__wrapper', '.el-select', '.ant-select-selector', '.ant-select', '.n-base-selection', '[role=combobox]']) {
                        const closest = input.closest?.(sel) || control.closest?.(sel) || control.querySelector?.(sel);
                        if (closest && isVisible(closest)) { opener = closest; break; }
                    }
                    const hint = norm([label, input?.placeholder, input?.getAttribute?.('aria-label'), textOf(control)].filter(Boolean).join(' '));
                    if (dateTarget && /(date|time|pick a date|日期|时间)/i.test(hint)) {
                        const picked = await clickDateValue(value, input || opener);
                        const afterDate = readValue(control);
                        const okDate = Boolean(picked) && norm(afterDate).includes(norm(picked));
                        return {
                            ok: okDate,
                            mode: okDate ? 'date_picker' : 'date_picker_failed',
                            binding: binding.reason,
                            observed: afterDate,
                            expected: picked || value
                        };
                    }
                    if (input && (inputRole === 'combobox' || inputAuto === 'list' || inputAuto === 'both')) {
                        setNativeValue(input, value);
                        await sleep(350);
                    }
                    opener.setAttribute(`data-${marker}`, '1');
                    const r = opener.getBoundingClientRect();
                    return {
                        ok: true,
                        mode: input && !input.readOnly && (inputRole === 'combobox' || inputAuto === 'list' || inputAuto === 'both')
                            ? 'autocomplete_opened'
                            : 'opened',
                        binding: binding.reason,
                        observed: before,
                        click_selector: `[data-${marker}="1"]`,
                        click_point: {x: r.left + r.width / 2, y: r.top + r.height / 2}
                    };
                }
                if (input) setNativeValue(input, value);
                else setNativeValue(control, value);
            }
            await new Promise(resolve => setTimeout(resolve, 60));
            const after = readValue(control);
            const ok = expected ? norm(after) === expected : true;
            return {
                ok,
                mode: ok ? 'bound_control' : 'value_mismatch',
                binding: binding.reason,
                observed: after,
                expected: value,
                before
            };
        }""",
        {"label": label, "value": value, "openIfNeeded": open_if_needed},
    )
    if not isinstance(result, dict):
        return {"ok": False, "reason": "invalid_result", "raw": result}
    return result


@ActionRegistry.register("form_set")
class FormSetHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        page = ctx.page
        label, value = _parse_form_set_payload(ctx.action.type_value)
        logger.info("[FORM_SET] label=%r value=%r", label, value)

        result = await _form_set_bound_control_v2(page, label, value)
        if result.get("mode") in ("opened", "autocomplete_opened") and value:
            click_selector = result.get("click_selector")
            if click_selector:
                try:
                    opener = page.locator(click_selector).first
                    await opener.scroll_into_view_if_needed(timeout=2000)
                    await opener.click(timeout=3000, force=True)
                except Exception as open_err:
                    point = result.get("click_point") or {}
                    try:
                        await page.mouse.click(float(point.get("x")), float(point.get("y")))
                    except Exception:
                        logger.debug("[FORM_SET] bound opener click failed: %s", open_err)
            await asyncio.sleep(0.8)
            option_clicked = await _click_visible_text_option(page, value)
            if not option_clicked:
                raise ActionExecutionError(
                    f"form_set located {label!r}, but could not select option {value!r}."
                )
            await asyncio.sleep(0.3)
            result = await _form_set_bound_control_v2(
                page, label, value, open_if_needed=False
            )

        if not result.get("ok"):
            raise ActionExecutionError(
                f"form_set failed for {label!r}: {result.get('reason') or result.get('mode')}; "
                f"expected={value!r}, observed={result.get('observed')!r}, "
                f"binding={result.get('binding')!r}"
            )
        if value and result.get("mode") in ("opened", "autocomplete_opened"):
            raise ActionExecutionError(
                f"form_set opened {label!r}, but the selected value was not reflected on the bound control."
            )
        logger.info(
            "[FORM_SET_V2] label=%r ok mode=%s binding=%s observed=%r",
            label,
            result.get("mode"),
            result.get("binding"),
            result.get("observed"),
        )
        print(f"[FORM_SET] {label} = {value}")
        ctx.browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "form_set",
                "type_value": f"{label}={value}",
                "method": "bound_control_v2",
                "binding": result.get("binding", ""),
                "verified": True,
                "observed": result.get("observed", ""),
            })
        )
        return None

        def _xpath_literal(text: str) -> str:
            if "'" not in text:
                return f"'{text}'"
            parts = text.split("'")
            return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"

        async def _try_open_choice_with_ax() -> bool:
            if not value:
                return False
            choice_label = re.search(
                r"(zone|type|resource|date|time|下拉|选择|复选|单选|开关|日期|时间)",
                label,
                re.I,
            )
            if not choice_label:
                return False
            locators = []
            try:
                locators.append(page.get_by_role("combobox", name=re.compile(re.escape(label), re.I)).first)
            except Exception:
                pass
            try:
                locators.append(page.get_by_label(label, exact=True).first)
            except Exception:
                pass
            try:
                label_lit = _xpath_literal(label)
                locators.append(
                    page.locator(
                        "xpath=("
                        f"//*[normalize-space()={label_lit} or contains(normalize-space(), {label_lit})]"
                        "/following::*["
                        "@role='combobox' or self::select or contains(@class,'select') or "
                        "contains(@class,'picker') or contains(@class,'checkbox') or contains(@class,'radio')"
                        "][1])"
                    ).first
                )
            except Exception:
                pass
            for idx, loc in enumerate(locators):
                try:
                    if await loc.count() <= 0:
                        continue
                    await loc.scroll_into_view_if_needed(timeout=2000)
                    await loc.click(timeout=3000, force=True)
                    await asyncio.sleep(0.8)
                    if await _click_visible_text_option(page, value):
                        logger.info("[FORM_SET] AX fast-path selected %r via locator #%s", value, idx)
                        return True
                except Exception as err:
                    logger.debug("[FORM_SET] AX fast-path locator #%s failed: %s", idx, err)
            return False

        async def _verify_field_state(method: str = "") -> dict[str, Any]:
            return await page.evaluate(
                """({label, value, method}) => {
                    const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const expected = norm(value);
                    const labelNorm = norm(label);
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                            s.display !== 'none' && s.visibility !== 'hidden' &&
                            Number(s.opacity || '1') > 0;
                    };
                    const textOf = (el) => [
                        el.innerText, el.textContent, el.getAttribute('aria-label'),
                        el.getAttribute('placeholder'), el.getAttribute('title'),
                        el.getAttribute('value'), el.value, el.name, el.id
                    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                    const nodes = Array.from(document.querySelectorAll(
                        'label,.el-form-item__label,[class*=form-item__label],input,textarea,select,button,[role],span,div'
                    )).filter(isVisible);
                    const labelHits = [];
                    for (const el of nodes) {
                        const t = norm(textOf(el));
                        if (!t || (t !== labelNorm && !t.includes(labelNorm))) continue;
                        const r = el.getBoundingClientRect();
                        let score = t === labelNorm ? 80 : 30;
                        if (el.matches('label,.el-form-item__label,[class*=form-item__label]')) score += 60;
                        if (el.closest('form,.el-form,[class*=form]')) score += 30;
                        if (r.left > 180) score += 20;
                        labelHits.push({el, score});
                    }
                    labelHits.sort((a, b) => b.score - a.score);
                    const labelEl = labelHits[0]?.el;
                    if (!labelEl) return {ok: false, reason: 'label_not_found', observed: '', method};
                    let formItem = labelEl;
                    for (let i = 0; formItem && i < 8; i++) {
                        if (
                            formItem !== labelEl &&
                            (
                                formItem.classList?.contains('el-form-item') ||
                                formItem.classList?.contains('ant-form-item') ||
                                formItem.classList?.contains('n-form-item') ||
                                formItem.tagName?.toLowerCase() === 'form'
                            )
                        ) break;
                        formItem = formItem.parentElement;
                    }
                    if (!formItem) formItem = labelEl.parentElement || labelEl;

                    const controls = Array.from(formItem.querySelectorAll('input,textarea,select,[contenteditable=true]')).filter(isVisible);
                    const controlValues = controls.map(el => {
                        if (el.tagName?.toLowerCase() === 'select') {
                            const opt = el.selectedOptions?.[0];
                            return [el.value, opt?.textContent].filter(Boolean).join(' ');
                        }
                        return [el.value, el.textContent, el.getAttribute('aria-label')].filter(Boolean).join(' ');
                    });
                    const observed = [textOf(formItem), ...controlValues].join(' ').replace(/\\s+/g, ' ').trim();
                    const observedNorm = norm(observed);

                    const switchRoot = formItem.querySelector('.el-switch,[role=switch]');
                    if (switchRoot) {
                        const checked = switchRoot.classList.contains('is-checked') ||
                            switchRoot.getAttribute('aria-checked') === 'true' ||
                            Boolean(formItem.querySelector('input:checked'));
                        const shouldOn = !/^(false|off|no|0)$/i.test(String(value || ''));
                        return {ok: checked === shouldOn, observed: checked ? 'checked' : 'unchecked', method, mode: 'switch'};
                    }

                    const choiceNodes = Array.from(formItem.querySelectorAll('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]'))
                        .filter(isVisible);
                    const matchingChoice = choiceNodes.find(el => norm(textOf(el)).includes(expected));
                    if (matchingChoice) {
                        const target = matchingChoice.closest?.('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]') || matchingChoice;
                        const checked = target.matches?.('.is-checked,[aria-checked=true]') ||
                            Boolean(target.querySelector?.('.is-checked,[aria-checked=true],input:checked')) ||
                            Boolean(matchingChoice.querySelector?.('.is-checked,[aria-checked=true],input:checked'));
                        if (checked) return {ok: true, observed, method, mode: 'choice_checked'};
                    }

                    if (expected && observedNorm.includes(expected)) {
                        return {ok: true, observed, method, mode: 'value_visible'};
                    }
                    return {ok: false, reason: 'value_not_reflected', observed, expected: value, method};
                }""",
                {"label": label, "value": value, "method": method},
            )

        if await _try_open_choice_with_ax():
            verified = await _verify_field_state("ax_fast_path")
            if not verified.get("ok"):
                raise ActionExecutionError(
                    f"form_set 字段 {label!r} 已执行 AX fast-path，但回读校验失败："
                    f"expected={value!r}, observed={verified.get('observed')!r}, reason={verified.get('reason')}"
                )
            logger.info("[FORM_VERIFY] label=%r ok via %s observed=%r", label, verified.get("method"), verified.get("observed"))
            ctx.browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "form_set",
                    "type_value": f"{label}={value}",
                    "method": "ax_fast_path",
                    "verified": True,
                    "observed": verified.get("observed", ""),
                })
            )
            return None

        result = await page.evaluate(
            """async ({label, value}) => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const labelNorm = norm(label);
                const valueNorm = norm(value);
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el.innerText, el.textContent, el.getAttribute('aria-label'),
                    el.getAttribute('placeholder'), el.getAttribute('title'),
                    el.getAttribute('value'), el.name, el.id
                ].filter(Boolean).join(' ').trim();
                const setNativeValue = (el, val) => {
                    const proto = el instanceof HTMLTextAreaElement
                        ? HTMLTextAreaElement.prototype
                        : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(el, val); else el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.blur?.();
                };
                const clickEl = (el) => {
                    el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                    // 关键修复：Element Plus / Ant Design / Vue 等 SPA 库的 select / dropdown
                    // 通常监听 `mousedown` 而非 `click`（防 input blur 后再触发）。
                    // 单纯 el.click() 只会派发 click 事件，组件不会响应 → 下拉永远不展开。
                    // 这里派发完整 mousedown + mouseup + click 三连，模拟真实鼠标点击。
                    const r = el.getBoundingClientRect();
                    const cx = r.left + Math.max(1, r.width / 2);
                    const cy = r.top + Math.max(1, r.height / 2);
                    const evtInit = {
                        bubbles: true, cancelable: true, composed: true,
                        view: window, button: 0, buttons: 1,
                        clientX: cx, clientY: cy, screenX: cx, screenY: cy,
                    };
                    try {
                        el.dispatchEvent(new MouseEvent('mousedown', evtInit));
                        el.dispatchEvent(new MouseEvent('mouseup', evtInit));
                        el.dispatchEvent(new MouseEvent('click', evtInit));
                    } catch (e) {
                        // fallback：浏览器极端情况下 MouseEvent 构造失败
                        if (typeof el.click === 'function') el.click();
                    }
                    if (typeof el.focus === 'function') {
                        try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); }
                    }
                };
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                const allVisible = (selector, root = document) => Array.from(root.querySelectorAll(selector)).filter(isVisible);
                const clickDateValue = async (dateValue, opener) => {
                    const m = String(dateValue || '').match(/^(\\d{4})-(\\d{2})-(\\d{2})/);
                    if (!m) return false;
                    const targetYear = Number(m[1]);
                    const targetMonth = Number(m[2]);
                    const targetDay = Number(m[3]);
                    if (!targetYear || !targetMonth || !targetDay) return false;
                    clickEl(opener);
                    await sleep(300);

                    const visiblePanelMonth = () => {
                        const panels = allVisible('.el-picker-panel,.ant-picker-dropdown,.n-date-panel,.mx-datepicker-main,.datepicker,[role=dialog],.el-popper');
                        const root = panels[panels.length - 1] || document;
                        const txt = textOf(root);
                        const monthNames = {
                            january: 1, february: 2, march: 3, april: 4, may: 5, june: 6,
                            july: 7, august: 8, september: 9, october: 10, november: 11, december: 12,
                            jan: 1, feb: 2, mar: 3, apr: 4, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9,
                            oct: 10, nov: 11, dec: 12
                        };
                        let m = txt.match(/(20\\d{2})\\s*[年\\-/\\. ]\\s*(1[0-2]|0?[1-9])\\s*(?:月)?/);
                        if (m) return {year: Number(m[1]), month: Number(m[2])};
                        m = txt.match(/(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\\s+(20\\d{2})/i);
                        if (m) return {year: Number(m[2]), month: monthNames[m[1].toLowerCase()]};
                        m = txt.match(/(20\\d{2})\\s+(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)/i);
                        if (m) return {year: Number(m[1]), month: monthNames[m[2].toLowerCase()]};
                        const currentVal = String(opener?.value || '');
                        m = currentVal.match(/^(\\d{4})-(\\d{2})-/);
                        if (m) return {year: Number(m[1]), month: Number(m[2])};
                        const now = new Date();
                        return {year: now.getFullYear(), month: now.getMonth() + 1};
                    };
                    const panelMonth = visiblePanelMonth();
                    const monthDelta = (targetYear - panelMonth.year) * 12 + (targetMonth - panelMonth.month);
                    const nextSelectors = [
                        '.el-picker-panel__icon-btn.arrow-right',
                        '.ant-picker-header-next-btn',
                        'button[aria-label*="Next month"]',
                        'button[title*="Next month"]'
                    ].join(',');
                    const prevSelectors = [
                        '.el-picker-panel__icon-btn.arrow-left',
                        '.ant-picker-header-prev-btn',
                        'button[aria-label*="Previous month"]',
                        'button[title*="Previous month"]'
                    ].join(',');
                    const navSelector = monthDelta >= 0 ? nextSelectors : prevSelectors;
                    for (let i = 0; i < Math.min(Math.abs(monthDelta), 24); i++) {
                        const btn = allVisible(navSelector).find(el => !el.disabled && el.getAttribute('aria-disabled') !== 'true');
                        if (!btn) break;
                        clickEl(btn);
                        await sleep(150);
                    }

                    const ymd = `${targetYear}-${String(targetMonth).padStart(2, '0')}-${String(targetDay).padStart(2, '0')}`;
                    const dayText = String(targetDay);
                    for (let i = 0; i < 10; i++) {
                        const panels = allVisible('.el-picker-panel,.ant-picker-dropdown,.n-date-panel,.mx-datepicker-main,.datepicker,[role=dialog],.el-popper');
                        const root = panels[panels.length - 1] || document;
                        const cells = allVisible('td,button,[role=gridcell],.el-date-table-cell,.ant-picker-cell-inner', root);
                        const hits = [];
                        for (const el of cells) {
                            const cell = el.closest('td,button,[role=gridcell]') || el;
                            if (!isVisible(cell)) continue;
                            const disabled = cell.matches('.disabled,.is-disabled,.ant-picker-cell-disabled,[aria-disabled=true]') ||
                                cell.closest('.disabled,.is-disabled,.ant-picker-cell-disabled,[aria-disabled=true]');
                            if (disabled) continue;
                            const raw = [
                                textOf(el), textOf(cell),
                                el.getAttribute('aria-label'), cell.getAttribute('aria-label'),
                                el.getAttribute('title'), cell.getAttribute('title')
                            ].filter(Boolean).join(' ');
                            const t = norm(raw);
                            let score = 0;
                            if (t === norm(dayText)) score += 80;
                            if (t.includes(norm(ymd))) score += 120;
                            if (t.includes(String(targetYear)) && t.includes(String(targetDay))) score += 40;
                            if (cell.classList?.contains('prev-month') || cell.classList?.contains('next-month')) score -= 90;
                            if (cell.classList?.contains('available') || cell.classList?.contains('ant-picker-cell-in-view')) score += 20;
                            if (score > 0) hits.push({cell, score});
                        }
                        hits.sort((a, b) => b.score - a.score);
                        if (hits[0]) {
                            clickEl(hits[0].cell);
                            await sleep(250);
                            return true;
                        }
                        await sleep(120);
                    }
                    return false;
                };
                const nodes = Array.from(document.querySelectorAll(
                    'label,.el-form-item__label,[class*=form-item__label],input,textarea,select,button,[role],span,div'
                )).filter(isVisible);
                const matches = [];
                for (const el of nodes) {
                    const t = norm(textOf(el));
                    if (!t) continue;
                    const exact = t === labelNorm;
                    const contains = t.includes(labelNorm);
                    if (!exact && !contains) continue;
                    const r = el.getBoundingClientRect();
                    let score = exact ? 80 : 30;
                    if (el.matches('label,.el-form-item__label,[class*=form-item__label]')) score += 50;
                    if (el.closest('form,.el-form,[class*=form]')) score += 30;
                    if (r.left > 180) score += 30;
                    if (r.width * r.height < 80000) score += 10;
                    score -= Math.abs(r.top - window.innerHeight / 2) / 80;
                    matches.push({el, score, text: textOf(el), left: r.left});
                }
                matches.sort((a, b) => b.score - a.score);
                const labelEl = matches[0]?.el;
                if (!labelEl) return {ok: false, reason: 'label_not_found', label};
                labelEl.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});

                const findContainer = (el) => {
                    let cur = el;
                    for (let i = 0; cur && i < 8; i++) {
                        if (
                            cur !== el &&
                            (
                                cur.classList?.contains('el-form-item') ||
                                cur.classList?.contains('ant-form-item') ||
                                cur.classList?.contains('n-form-item') ||
                                cur.tagName?.toLowerCase() === 'form'
                            )
                        ) {
                            return cur;
                        }
                        cur = cur.parentElement;
                    }
                    cur = el.parentElement;
                    for (let i = 0; cur && i < 4; i++) {
                        if (cur.querySelector?.('input,textarea,select,[role=combobox],[role=checkbox],[role=radio],button,.el-select,.ant-select,.n-select')) {
                            return cur;
                        }
                        cur = cur.parentElement;
                    }
                    return el.parentElement;
                };
                const formItem = findContainer(labelEl);
                if (!formItem) return {ok: false, reason: 'container_not_found', label};

                const textInputs = Array.from(formItem.querySelectorAll('input,textarea,[contenteditable=true]'))
                    .filter(el => isVisible(el) && !['hidden','checkbox','radio','button','submit'].includes((el.type || '').toLowerCase()));
                const explicitSelectRoot = formItem.querySelector('.el-select,.ant-select,.n-select,[data-select]');
                if (/(date|time|日期|时间)/i.test(label) && /^\\d{4}-\\d{2}-\\d{2}/.test(value) && textInputs.length) {
                    const picked = await clickDateValue(value, textInputs[0]);
                    if (picked) return {ok: true, mode: 'date_picker', label, value};
                    setNativeValue(textInputs[0], value);
                    return {ok: true, mode: 'date_input', label, value};
                }
                const readonlyCombo = textInputs.find(el => {
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    const popup = (el.getAttribute('aria-haspopup') || '').toLowerCase();
                    return el.readOnly || role === 'combobox' || popup === 'listbox' || popup === 'true';
                });
                if ((explicitSelectRoot || readonlyCombo) && valueNorm && !formItem.querySelector('textarea')) {
                    // Idempotent open：检测下拉浮层是否已经可见，避免重试时再点一次反而 toggle 关闭
                    const _popperOpen = (
                        document.querySelector('.el-select-dropdown:not([style*="display: none"])') ||
                        document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden)') ||
                        document.querySelector('.n-base-select-menu') ||
                        document.querySelector('[role=listbox]:not([aria-hidden=true])')
                    );
                    if (_popperOpen) {
                        return {ok: true, mode: 'opened', label, value, already_open: true};
                    }
                    // ── 关键修复：找到真正的可点击 wrapper（Element Plus 事件挂在 wrapper 而非 input） ──
                    // readonlyCombo 找到的常是内层 <input class="el-select__inner">，但 Vue
                    // 用 @mousedown.stop 委托到 .el-select__wrapper，内层派事件会被 stopPropagation 截胡。
                    // 向上遍历找到真正监听事件的 wrapper 元素，标记 data 属性供 Python 端 Playwright 点击。
                    let wrapper = explicitSelectRoot || readonlyCombo;
                    const _WRAPPER_SELECTORS = [
                        '.el-select__wrapper', '.el-select',
                        '.ant-select-selector', '.ant-select',
                        '.n-base-selection', '[role=combobox]',
                    ];
                    for (const sel of _WRAPPER_SELECTORS) {
                        const child = wrapper?.querySelector?.(sel);
                        if (child && isVisible(child)) { wrapper = child; break; }
                    }
                    let cur = wrapper;
                    for (let i = 0; cur && i < 8; i++) {
                        for (const sel of _WRAPPER_SELECTORS) {
                            if (cur.matches?.(sel)) { wrapper = cur; break; }
                        }
                        if (wrapper !== (explicitSelectRoot || readonlyCombo)) break;
                        cur = cur.parentElement;
                    }
                    // 给 wrapper 加唯一 data 标记，Python 端用 Playwright click 派完整事件链
                    const _marker = '__vspider_form_target__';
                    document.querySelectorAll(`[data-${_marker}]`).forEach(el =>
                        el.removeAttribute(`data-${_marker}`)
                    );
                    wrapper.setAttribute(`data-${_marker}`, '1');
                    const rr = wrapper.getBoundingClientRect();
                    return {
                        ok: true, mode: 'opened', label, value,
                        click_selector: `[data-${_marker}="1"]`,
                        click_point: {x: rr.left + rr.width * 0.5, y: rr.top + rr.height * 0.5},
                    };
                }
                if (textInputs.length) {
                    const input = textInputs[0];
                    setNativeValue(input, value);
                    return {ok: true, mode: 'fill', label, value};
                }

                const switches = Array.from(formItem.querySelectorAll(
                    '.el-switch,[role=switch],input[type=checkbox],.el-checkbox,label'
                )).filter(isVisible);
                if (switches.length && /^(true|on|yes|1|开启|打开|选中|勾选)$/i.test(value || 'true')) {
                    const checked = formItem.querySelector('.is-checked,[aria-checked=true],input:checked');
                    if (!checked) clickEl(switches[0]);
                    return {ok: true, mode: 'toggle', label, value};
                }

                if (valueNorm) {
                    const optionNodes = Array.from(formItem.querySelectorAll('label,.el-radio,.el-checkbox,button,span,div'))
                        .filter(el => isVisible(el) && norm(textOf(el)).includes(valueNorm));
                    if (optionNodes.length) {
                        const choiceHit = optionNodes[0];
                        const choiceTarget = choiceHit.closest?.('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]') || choiceHit;
                        clickEl(choiceTarget);
                        await sleep(150);
                        return {ok: true, mode: 'local_option', label, value};
                    }
                }

                const clickable = Array.from(formItem.querySelectorAll(
                    '.el-select,.el-input,.el-input__wrapper,[role=combobox],[role=button],button,input'
                )).filter(isVisible);
                if (clickable.length) {
                    clickEl(clickable[0]);
                    return {ok: true, mode: 'opened', label, value};
                }
                clickEl(labelEl);
                return {ok: true, mode: 'label_click', label, value};
            }""",
            {"label": label, "value": value},
        )
        await asyncio.sleep(0.6)
        if not result.get("ok"):
            raise ActionExecutionError(
                f"form_set 未找到字段 {label!r}: {result.get('reason')}"
            )
        if result.get("mode") in ("opened", "label_click") and value:
            # JS 端只负责"找控件 + 标记 data 属性"，真正的点击交给 Playwright（CDP 派完整事件链，
            # 穿透 Vue/React 的 mousedown.stop 委托）。这是修复 Element Plus 下拉打不开的关键。
            _click_sel = result.get("click_selector")
            if _click_sel and not result.get("already_open"):
                try:
                    _wrapper_loc = page.locator(_click_sel).first
                    await _wrapper_loc.scroll_into_view_if_needed(timeout=2000)
                    await _wrapper_loc.click(timeout=3000, force=True)
                    logger.info(f"[FORM_SET] Playwright 点击 wrapper {_click_sel} 展开下拉")
                except Exception as _open_err:
                    _pt = result.get("click_point") or {}
                    try:
                        _x = float(_pt.get("x"))
                        _y = float(_pt.get("y"))
                        await page.mouse.move(_x, _y)
                        await page.mouse.down()
                        await page.mouse.up()
                        logger.info("[FORM_SET] page.mouse click fallback at %.1f, %.1f", _x, _y)
                    except Exception as _mouse_err:
                        logger.warning("[FORM_SET] page.mouse click fallback failed: %s", _mouse_err)
                    logger.warning(f"[FORM_SET] Playwright wrapper click 失败: {_open_err}")
            # popper 动画 + teleport 渲染需要充分时间
            # Element Plus transition 250ms + 内部 mount + 选项 list 渲染 ≈ 600-900ms
            await asyncio.sleep(1.0)
            _option_clicked = await _click_visible_text_option(page, value)
            if not _option_clicked:
                _pt = result.get("click_point") or {}
                try:
                    _x = float(_pt.get("x"))
                    _y = float(_pt.get("y"))
                    await page.mouse.click(_x, _y)
                    await asyncio.sleep(0.8)
                    _option_clicked = await _click_visible_text_option(page, value)
                except Exception as _retry_open_err:
                    logger.debug("[FORM_SET] point reopen retry failed: %s", _retry_open_err)
            if not _option_clicked:
                raise ActionExecutionError(
                    f"form_set 已定位字段 {label!r}，但未能点击选项/值 {value!r}。"
                    "可能原因：(a) 下拉浮层渲染慢于 2.5s 等待窗口；"
                    "(b) 选项文字与 value 不完全匹配（含空格/隐藏字符）；"
                    "(c) 该字段不是下拉而是输入框 — 改用 type 动作直接 fill。"
                )
        verified = await _verify_field_state(str(result.get("mode") or "dom_path"))
        if not verified.get("ok"):
            raise ActionExecutionError(
                f"form_set 字段 {label!r} 已执行但回读校验失败："
                f"expected={value!r}, observed={verified.get('observed')!r}, "
                f"mode={result.get('mode')!r}, reason={verified.get('reason')}"
            )
        logger.info(
            "[FORM_VERIFY] label=%r ok via %s observed=%r",
            label,
            verified.get("method"),
            verified.get("observed"),
        )
        logger.info("[FORM_SET] completed label=%r mode=%s", label, result.get("mode"))
        print(f"[FORM_SET] {label} = {value}")
        ctx.browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "form_set",
                "type_value": f"{label}={value}",
                "verified": True,
                "observed": verified.get("observed", ""),
            })
        )
        return None


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
                    f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
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
    中键点击：在新标签页打开链接，**焦点保留在原页面**。

    "Open in new tab" 的人类语义即"后台 tab"——用户没要求立即跳过去看。
    若 VLM 之后想看新 tab 内容，应显式 switch_tab；本 handler 不替它决定。
    这避免了"点击伪装广告 → 落到重 SPA 新 tab → 截图字体死锁 → agent 崩溃"
    的级联失败（参见 Baidu SEM 'Python入门-...' → comate.baidu.com 案例）。

    实现：
      1. 通过 SoM ID 拿到目标元素（同普通 click）
      2. middle-button click 触发浏览器原生新 tab；popup 监听器会把新 tab
         注册为 active —— 这是要矫正的"反语义"
      3. 显式 ``return ctx.page`` 让 dispatcher **跳过 Tab Guard**，并把
         焦点拨回 origin（若仍存活）
    """

    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing click_new_tab: element #{target_id} ({selector})")
        # Snapshot of pages BEFORE the click so we can detect whether a new
        # tab was actually opened (vs middle-click on non-link → no-op).
        # Used downstream to compose the "success notice" for VLM that
        # confirms the new tab opened even though focus stays on origin.
        _pre_pages: set[int] = (
            {id(p) for p in browser._context.pages if not p.is_closed()}
            if browser._context else set()
        )
        try:
            await browser._clear_som_overlays()
            target = await browser._resolve_action_target(target_id, "click_new_tab")
            if target:
                await target.handle.scroll_into_view_if_needed(
                    timeout=browser._LOCATOR_TIMEOUT
                )
            else:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
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

        # ── Tab Visit Stack：记录"从这个 tab 开出去的" ─────────────────
        # 用途：后续 close_tab(子) 时，知道该返回哪个父。即使 VLM 中途
        # switch_tab 到别处，stack 也忠实记录了开新 tab 这一刻的 origin。
        # 仅在确实有新 tab 时 push（middle-click 在非链接元素上是无效 no-op）。
        try:
            _post_pages = (
                [p for p in browser._context.pages if not p.is_closed()]
                if browser._context else []
            )
            _new_tab_opened = any(id(p) not in _pre_pages for p in _post_pages)
            if _new_tab_opened:
                browser.push_tab_visit(page)
        except Exception as _stack_err:
            logger.debug("[click_new_tab] push_tab_visit failed: %s", _stack_err)
            _post_pages = []
            _new_tab_opened = False

        # ── VLM 反馈通知：补偿 Fix 2 引起的"视觉信号丢失" ─────────────
        # 焦点留 origin 后，VLM 截图看不出 click_new_tab 成功，会陷入
        # "我没点中，换个 ID 再点" 的无限重试（Playwright Baidu 日志里点了 14 次）。
        # 注入到 ``_tab_switch_notice``，下一步 ask() 自动作为 feedback 喂给 VLM。
        if _new_tab_opened and not page.is_closed():
            try:
                # 找出新 tab 的索引 + origin 的索引
                _new_tabs = [p for p in _post_pages if id(p) not in _pre_pages]
                _new_tab = _new_tabs[-1] if _new_tabs else None
                _new_idx = _post_pages.index(_new_tab) if _new_tab in _post_pages else -1
                _origin_idx = _post_pages.index(page) if page in _post_pages else 0
                _total = len(_post_pages)
                _new_url = ""
                try:
                    _new_url = (_new_tab.url or "")[:80] if _new_tab else ""
                except Exception:
                    _new_url = ""
                browser.set_tab_notice(
                    f"✅ 上一步 click_new_tab(元素 #{target_id}) 已成功开启新标签页。\n"
                    f"  • 新标签索引: [{_new_idx}] {_new_url}\n"
                    f"  • 当前共 {_total} 个 tab\n"
                    f"  • 🔒 焦点【保留在原页 [{_origin_idx}]】"
                    f"(per '在新标签页打开' = 后台 tab 语义)\n"
                    f"  • 截图仍是原页 —— 这是正确的，**不是失败**\n"
                    f"⚠️ 不要再次 click_new_tab 同一元素 —— 会无限开新标签。\n"
                    f"下一步选项：\n"
                    f"  (a) 任务要求查看/操作新 tab → switch_tab(target_id={_new_idx}, type_value=\"{_new_idx}\")\n"
                    f"  (b) 任务要求继续在原页操作 → 直接执行下一动作\n"
                    f"  (c) 之后 close_tab(子 tab) 会自动回到父 tab [{_origin_idx}]",
                    severity='info',
                    coalesce=False,
                )
                logger.info(
                    "[click_new_tab] success notice injected: new=[%d] %s",
                    _new_idx, _new_url[:60],
                )
            except Exception as _notice_err:
                logger.debug(
                    "[click_new_tab] notice composition failed: %s",
                    _notice_err,
                )
        elif not _new_tab_opened:
            # No-op detection: middle-click on non-link element didn't open
            # anything. Tell VLM so it stops retrying the wrong element.
            try:
                browser.set_tab_notice(
                    f"❌ click_new_tab(元素 #{target_id}) 未能开启新标签页。\n"
                    f"  • 当前 tab 数量未变化，说明该元素不是链接（或链接被前端拦截）\n"
                    f"  • 不要再用 click_new_tab 操作同一元素\n"
                    f"建议：\n"
                    f"  (a) 改用普通 click 操作（如果元素本身只触发同页跳转）\n"
                    f"  (b) 换一个明确是 <a> link 的元素（看 AX Tree 里 role=link 的项）\n"
                    f"  (c) 用 click_text 按可见文字定位真正的链接",
                    severity='warn',
                    coalesce=False,
                )
                logger.info(
                    "[click_new_tab] no-op notice injected: element #%d "
                    "did not open a new tab", target_id,
                )
            except Exception:
                pass

        # ── 中键打开 = 后台 tab：把焦点拨回 origin ────────────────────
        # popup 监听器会把新 tab 注册为 active（_register_page activate=True），
        # 但 click_new_tab 的语义是"留在当前页"。显式 restore 之后 return page，
        # dispatcher 跳过 Tab Guard，焦点稳稳留在 origin。
        # origin 已关闭则 fallback 让 Tab Guard 处理（return None）。
        if not page.is_closed():
            if browser._page is not page:
                try:
                    await browser._activate_page(
                        page,
                        reason=f"click_new_tab: keep origin focused (target #{target_id})",
                    )
                except Exception as _restore_err:
                    logger.debug(
                        "[click_new_tab] origin restore failed; "
                        "letting Tab Guard handle: %s",
                        _restore_err,
                    )
                    return None
            return page  # 显式返回：dispatcher 跳过 Tab Guard
        return None


@ActionRegistry.register("fetch_link_content", "fetch_links_batch")
class FetchLinkContentHandler(ActionHandler):
    """
    Background tab fetch + JS extract + close + memory write — VLM stays free.

    Replaces the slow loop ``click_new_tab → switch_tab → screenshot →
    extract → close_tab`` (12-20 s, multiple VLM calls) with a single atomic
    action (1-3 s, zero VLM cost between fetch and result).

    Step shape:
      target_id : SoM ID of the link element (preferred — href read via JS)
      type_value: literal URL string (fallback when no link is on-screen)
      memory_key: where to store the result. Always required by the model;
                  auto-generated if missing for back-compat.

    Result written to ``workflow_memory[memory_key]``:
        {"url": <final URL after redirects>,
         "title": <h1 or <title>>,
         "content": <innerText of main/article/body, capped at 6 K>}
    ``latest_memory`` is also updated to a 200-char preview so trailing
    ``save_to_memory``-style consumers keep working.

    Caveats (told to the user via tab_switch_notice):
      * Only supports content extraction. If the new page needs interaction,
        use ``click_new_tab`` + ``switch_tab`` instead.
      * Cloudflare / heavy SPA / login-walled pages may serve different
        content to a programmatic ``goto`` than to a real click — back off
        to the interactive path when that happens.
    """

    _MAX_CONTENT_CHARS = 6000
    _GOTO_TIMEOUT_MS = 15000
    _SETTLE_AFTER_LOAD_S = 0.5
    _MAX_BATCH_LINKS = 20
    _MAX_BATCH_CONCURRENCY = 5

    def _parse_options(self, type_value: str) -> tuple[dict[str, Any], bool]:
        text = (type_value or "").strip()
        if not text or text[0] not in "[{":
            return {}, False
        try:
            data = json.loads(text)
        except Exception:
            return {}, False
        if isinstance(data, dict):
            return data, True
        if isinstance(data, list):
            if all(
                isinstance(item, str)
                and item.lower().startswith(("http://", "https://"))
                for item in data
            ):
                return {"urls": data}, True
            return {"target_ids": data}, True
        return {}, True

    def _parse_target_ids(self, value: Any) -> list[int]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = re.findall(r"@?e?(\d+)", str(value), flags=re.I)
        ids: list[int] = []
        seen: set[int] = set()
        for item in raw_items:
            try:
                if isinstance(item, str):
                    match = re.search(r"\d+", item)
                    if not match:
                        continue
                    tid = int(match.group(0))
                else:
                    tid = int(item)
            except (TypeError, ValueError):
                continue
            if tid > 0 and tid not in seen:
                ids.append(tid)
                seen.add(tid)
        return ids

    def _parse_urls(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = re.split(r"[\s,]+", str(value).strip())
        urls: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            url = str(item or "").strip().strip("'\"")
            if (
                url
                and url.lower().startswith(("http://", "https://"))
                and url not in seen
            ):
                urls.append(url)
                seen.add(url)
        return urls

    def _normalize_selectors(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = re.split(r"[\n;]+|,\s*(?=[.#\[:a-zA-Z*])", str(value))
        selectors: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            selector = str(item or "").strip()
            if selector and selector not in seen:
                selectors.append(selector)
                seen.add(selector)
        return selectors[:12]

    def _extract_mode(self, options: dict[str, Any]) -> str:
        mode = str(
            options.get("mode")
            or options.get("extract_mode")
            or options.get("content_mode")
            or "dom"
        ).strip().lower()
        return "ax" if mode in {"ax", "accessibility", "accessibility_tree"} else "dom"

    def _batch_concurrency(self, options: dict[str, Any], count: int) -> int:
        try:
            configured = int(options.get("concurrency") or options.get("parallel") or 4)
        except (TypeError, ValueError):
            configured = 4
        return max(1, min(count, configured, self._MAX_BATCH_CONCURRENCY))

    def _ensure_http_url(self, url: str, action_name: str) -> str:
        url = str(url or "").strip()
        lowered = url.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ActionExecutionError(
                f"{action_name}: 拒绝访问非 http(s) URL: {url[:80]!r}"
            )
        return url

    async def _resolve_url_from_target(
        self,
        browser: "BrowserEnv",
        target_id: int,
        action_name: str,
    ) -> str:
        await browser._clear_som_overlays()
        target = await browser._resolve_action_target(target_id, action_name)
        if not target:
            raise ActionExecutionError(
                f"{action_name}: 元素 #{target_id} 在当前页或 iframe 中找不到"
            )
        try:
            url = str(
                await target.handle.evaluate(
                    """el => {
                        const a = el.closest('a') || el.querySelector('a') || el;
                        return String(a.href || a.getAttribute('href') || '').trim();
                    }"""
                ) or ""
            ).strip()
        except Exception as href_err:
            raise ActionExecutionError(
                f"{action_name}: 读取 #{target_id} 的 href 失败: {href_err}"
            )
        if not url:
            raise ActionExecutionError(f"{action_name}: 元素 #{target_id} 没有 href")
        return self._ensure_http_url(url, action_name)

    async def _extract_dom_payload(
        self,
        page: "Page",
        selectors: list[str],
    ) -> dict[str, Any]:
        return await page.evaluate(
            r"""([maxChars, selectorList]) => {
                const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
                const removeNoise = (root) => {
                    if (!root || !root.querySelectorAll) return;
                    root.querySelectorAll([
                        'script','style','noscript','template',
                        'nav','footer','header','aside',
                        '[role="navigation"]','[role="banner"]','[role="contentinfo"]',
                        '.nav','.navbar','.footer','.sidebar','.menu',
                        '.cookie','.cookies','.ads','.ad','.advertisement'
                    ].join(',')).forEach((n) => n.remove());
                };
                const textFrom = (node) => {
                    if (!node) return '';
                    const clone = node.cloneNode(true);
                    removeNoise(clone);
                    return clean(clone.innerText || clone.textContent || '');
                };
                const title = clean(
                    document.querySelector('h1')?.innerText
                    || document.title
                    || ''
                );
                const selectors = Array.isArray(selectorList) ? selectorList : [];
                const selected = [];
                for (const sel of selectors) {
                    try {
                        selected.push(...Array.from(document.querySelectorAll(sel)));
                    } catch (_) {}
                }
                if (selected.length) {
                    const joined = selected.map(textFrom).filter(Boolean).join('\n\n');
                    return {
                        title,
                        content: joined.slice(0, maxChars),
                        url: location.href,
                        full_len: joined.length,
                        mode: 'dom',
                        selectors,
                        selector_count: selected.length,
                    };
                }
                const containers = [
                    document.querySelector('article'),
                    document.querySelector('main'),
                    document.querySelector('[role="main"]'),
                    document.querySelector('#mw-content-text'),
                    document.querySelector('#content'),
                    document.querySelector('.content'),
                    document.body,
                ].filter(Boolean);
                let best = '';
                for (const c of containers) {
                    const t = textFrom(c);
                    if (t.length > best.length) best = t;
                    if (best.length >= maxChars) break;
                }
                return {
                    title,
                    content: best.slice(0, maxChars),
                    url: location.href,
                    full_len: best.length,
                    mode: 'dom',
                    selectors,
                    selector_count: 0,
                };
            }""",
            [self._MAX_CONTENT_CHARS, selectors],
        )

    def _flatten_ax_tree(self, tree: Any) -> tuple[str, list[dict[str, Any]]]:
        lines: list[str] = []
        structured: list[dict[str, Any]] = []
        skip_roles = {"generic", "none", "presentation", "InlineTextBox", "LineBreak"}

        def walk(node: Any, depth: int = 0) -> None:
            if isinstance(node, list):
                for child in node:
                    walk(child, depth)
                return
            if not isinstance(node, dict):
                return
            role = str(node.get("role") or "").strip()
            name = str(node.get("name") or "").strip()
            value = str(node.get("value") or "").strip()
            if role not in skip_roles and (role or name or value):
                pieces = [f"[{role or 'node'}]"]
                if name:
                    pieces.append(name)
                if value and value != name:
                    pieces.append(f"= {value}")
                for key in ("checked", "selected", "expanded", "disabled", "pressed"):
                    if key in node:
                        pieces.append(f"{key}:{node.get(key)}")
                lines.append("  " * min(depth, 6) + " ".join(pieces))
                structured.append({
                    "role": role,
                    "name": name,
                    "value": value,
                    "depth": depth,
                })
            for child in node.get("children") or []:
                walk(child, depth + 1)

        walk(tree)
        content = "\n".join(lines)
        return content[: self._MAX_CONTENT_CHARS], structured[:400]

    async def _extract_ax_payload(
        self,
        browser: "BrowserEnv",
        page: "Page",
        selectors: list[str],
    ) -> dict[str, Any]:
        tree = None
        getter = getattr(browser, "_get_ax_tree_via_cdp", None)
        if callable(getter):
            tree = await getter(page, interesting_only=True)
        if not tree:
            payload = await self._extract_dom_payload(page, selectors)
            payload["mode"] = "dom_fallback_from_ax"
            return payload
        content, structured = self._flatten_ax_tree(tree)
        selector_payload: dict[str, Any] = {}
        if selectors:
            try:
                selector_payload = await self._extract_dom_payload(page, selectors)
            except Exception as selector_err:
                logger.debug("[fetch_link_content] selector scope in ax mode failed: %s", selector_err)
        selector_content = str(selector_payload.get("content") or "").strip()
        if selector_content:
            content = (
                "[selector_scope]\n"
                f"{selector_content[: self._MAX_CONTENT_CHARS]}\n\n"
                "[ax_tree]\n"
                f"{content}"
            )[: self._MAX_CONTENT_CHARS]
        try:
            meta = await page.evaluate(
                "() => ({title: document.title || '', url: location.href})"
            )
        except Exception:
            meta = {}
        return {
            "title": str((meta or {}).get("title") or "").strip(),
            "content": content,
            "url": str((meta or {}).get("url") or getattr(page, "url", "") or "").strip(),
            "full_len": len(content),
            "mode": "ax",
            "selectors": selectors,
            "selector_count": int(selector_payload.get("selector_count") or 0),
            "selector_content": selector_content[: self._MAX_CONTENT_CHARS],
            "structured": structured,
        }

    async def _extract_payload(
        self,
        browser: "BrowserEnv",
        page: "Page",
        *,
        mode: str,
        selectors: list[str],
    ) -> dict[str, Any]:
        if mode == "ax":
            return await self._extract_ax_payload(browser, page, selectors)
        return await self._extract_dom_payload(page, selectors)

    async def _fetch_one(
        self,
        ctx: ActionContext,
        url: str,
        *,
        mode: str,
        selectors: list[str],
        action_name: str,
    ) -> dict[str, Any]:
        new_page = None
        try:
            new_page = await ctx.page.context.new_page()
            response = None
            try:
                # Capture the navigation response so we can surface the HTTP
                # status to VLM. None when the URL has no main resource
                # (data:/about: schemes) or when redirected without a body.
                response = await new_page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._GOTO_TIMEOUT_MS,
                )
            except Exception as goto_err:
                raise ActionExecutionError(
                    f"{action_name}: goto 失败 ({url[:80]!r}): {goto_err}"
                )
            await asyncio.sleep(self._SETTLE_AFTER_LOAD_S)
            try:
                http_status = (
                    int(response.status) if response is not None else None
                )
            except Exception:
                http_status = None
            http_ok = bool(http_status and 200 <= http_status < 400)
            try:
                payload = await self._extract_payload(
                    ctx.browser,
                    new_page,
                    mode=mode,
                    selectors=selectors,
                )
            except Exception as ev_err:
                raise ActionExecutionError(
                    f"{action_name}: 内容提取失败 ({url[:80]!r}): {ev_err}"
                )
            payload = dict(payload or {})
            payload["title"] = str(payload.get("title") or "").strip()
            payload["content"] = str(payload.get("content") or "").strip()
            payload["url"] = str(payload.get("url") or url).strip()
            payload["full_len"] = int(payload.get("full_len") or len(payload["content"]))
            payload.setdefault("mode", mode)
            payload.setdefault("selectors", selectors)
            payload["http_status"] = http_status
            payload["http_ok"] = http_ok
            return payload
        finally:
            if new_page is not None:
                try:
                    await new_page.close()
                except Exception as close_err:
                    logger.debug("[%s] new tab close failed: %s", action_name, close_err)

    async def _restore_origin(
        self,
        browser: "BrowserEnv",
        page: "Page",
        action_name: str,
    ) -> None:
        if not page.is_closed() and browser._page is not page:
            try:
                await browser._activate_page(page, reason=f"{action_name}: restore origin")
            except Exception as restore_err:
                logger.debug("[%s] origin restore failed: %s", action_name, restore_err)

    def _single_url_from_options(
        self,
        options: dict[str, Any],
        type_value: str,
        parsed_json: bool,
    ) -> str:
        for key in ("url", "href"):
            if options.get(key):
                return str(options.get(key) or "").strip()
        urls = self._parse_urls(options.get("urls"))
        if urls:
            return urls[0]
        if type_value and not parsed_json:
            return type_value
        return ""

    async def _execute_batch(
        self,
        ctx: ActionContext,
        *,
        options: dict[str, Any],
        type_value: str,
        parsed_json: bool,
    ) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        action_name = ctx.action.action or "fetch_links_batch"
        selectors = self._normalize_selectors(
            options.get("selectors") if "selectors" in options else options.get("selector")
        )
        mode = self._extract_mode(options)
        memory_key = (
            (ctx.action.memory_key or "").strip()
            or str(options.get("memory_key") or options.get("memory_key_prefix") or "").strip()
            or f"fetched_batch_{int(time.time())}"
        )

        target_ids = self._parse_target_ids(
            options.get("target_ids") if "target_ids" in options else options.get("ids")
        )
        urls = self._parse_urls(
            options.get("urls") if "urls" in options else options.get("url")
        )
        if ctx.action.target_id and ctx.action.target_id != 0:
            target_ids = [
                ctx.action.target_id,
                *[tid for tid in target_ids if tid != ctx.action.target_id],
            ]
        if not parsed_json and type_value:
            raw_urls = self._parse_urls(type_value)
            if raw_urls:
                urls.extend(u for u in raw_urls if u not in urls)
            else:
                urls.extend([])
                target_ids.extend(
                    tid for tid in self._parse_target_ids(type_value)
                    if tid not in target_ids
                )

        for tid in target_ids[: self._MAX_BATCH_LINKS]:
            urls.append(await self._resolve_url_from_target(browser, tid, action_name))
        urls = [self._ensure_http_url(url, action_name) for url in urls]
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url not in seen:
                deduped.append(url)
                seen.add(url)
        urls = deduped[: self._MAX_BATCH_LINKS]
        if not urls:
            raise ActionExecutionError(
                f"{action_name}: 需要 type_value JSON 提供 target_ids/urls，或 target_id"
            )

        concurrency = self._batch_concurrency(options, len(urls))
        sem = asyncio.Semaphore(concurrency)

        async def run_one(index: int, url: str) -> dict[str, Any]:
            async with sem:
                item_key = f"{memory_key}_{index + 1}"
                try:
                    payload = await self._fetch_one(
                        ctx,
                        url,
                        mode=mode,
                        selectors=selectors,
                        action_name=action_name,
                    )
                    payload["ok"] = True
                except Exception as err:
                    payload = {
                        "ok": False,
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": str(err),
                        "mode": mode,
                        "selectors": selectors,
                        "full_len": 0,
                    }
                payload["memory_key"] = item_key
                return payload

        results = await asyncio.gather(*(run_one(i, url) for i, url in enumerate(urls)))
        await self._restore_origin(browser, page, action_name)

        workflow_memory = ctx.workflow_memory
        if workflow_memory is not None:
            workflow_memory[memory_key] = results
            previews: list[str] = []
            for result in results:
                key = str(result.get("memory_key") or "")
                if key:
                    workflow_memory[key] = result
                if result.get("ok") and result.get("content"):
                    previews.append(str(result.get("content") or "")[:200])
            workflow_memory["latest_memory"] = "\n\n".join(previews)[:1000]

        try:
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "fetch_links_batch",
                    "type_value": json.dumps(
                        {
                            "urls": urls,
                            "mode": mode,
                            "selectors": selectors,
                            "concurrency": concurrency,
                        },
                        ensure_ascii=False,
                    ),
                    "memory_key": memory_key,
                })
            )
        except Exception as trail_err:
            logger.debug("[%s] rpa_trail append failed: %s", action_name, trail_err)

        ok_count = sum(1 for result in results if result.get("ok"))
        # Build a short HTTP failure summary (4xx/5xx + connection errors)
        # so VLM can decide whether to retry or skip the bad ones.
        _http_failures: list[str] = []
        for _r in results:
            _st = _r.get("http_status")
            _ok = bool(_r.get("ok"))
            _u = str(_r.get("url") or "")[:60]
            if not _ok:
                # Connection error (no status reached)
                _err = str(_r.get("error") or "").splitlines()[0][:80] if _r.get("error") else "fetch error"
                _http_failures.append(f"    · ❌ {_u} — {_err}")
            elif _st is not None and (_st >= 400 or _st < 200):
                _http_failures.append(f"    · ⚠️ HTTP {_st}: {_u}")
        _failures_block = ""
        if _http_failures:
            _failures_block = (
                "\n  • 失败链接（建议跳过或换 selector）:\n"
                + "\n".join(_http_failures[:10])
                + ("\n    · …" if len(_http_failures) > 10 else "")
            )
        browser.set_tab_notice(
            f"✅ fetch_links_batch 完成 — 并发抓取 {ok_count}/{len(results)} 条链接并写回 memory。\n"
            f"  • mode: {mode}\n"
            f"  • selectors: {selectors or 'default main/article/body'}\n"
            f"  • 存储: workflow_memory[{memory_key!r}] = list[{{url, title, content, ok, http_status}}]\n"
            f"  • 单条: workflow_memory[{memory_key + '_1'!r}], ...\n"
            f"  • 焦点仍在原页"
            + _failures_block,
            severity='info',
            coalesce=False,
        )
        logger.info(
            "[FETCH LINKS BATCH] %d/%d ok -> memory[%s]",
            ok_count,
            len(results),
            memory_key,
        )
        print(
            f"\033[1;36m🔗 [FETCH BATCH]\033[0m "
            f"{ok_count}/{len(results)} -> memory[\033[33m{memory_key}\033[0m]"
        )
        return page

    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        action = ctx.action
        target_id = action.target_id
        type_value = (action.type_value or "").strip()
        options, parsed_json = self._parse_options(type_value)
        action_name = action.action or "fetch_link_content"
        if action_name == "fetch_links_batch" or any(
            key in options for key in ("target_ids", "ids", "urls")
        ):
            return await self._execute_batch(
                ctx,
                options=options,
                type_value=type_value,
                parsed_json=parsed_json,
            )

        memory_key = (
            (action.memory_key or "").strip()
            or str(options.get("memory_key") or "").strip()
        )
        if not memory_key:
            memory_key = f"fetched_{int(time.time())}"
            logger.warning(
                "[fetch_link_content] memory_key missing — auto-generated %s",
                memory_key,
            )
        selectors = self._normalize_selectors(
            options.get("selectors") if "selectors" in options else options.get("selector")
        )
        mode = self._extract_mode(options)

        # 1. Resolve URL ────────────────────────────────────────────────
        if target_id and target_id != 0:
            url = await self._resolve_url_from_target(browser, target_id, action_name)
        else:
            url = self._single_url_from_options(options, type_value, parsed_json)
        if not url:
            raise ActionExecutionError(
                "fetch_link_content: 既未提供 target_id (链接元素)，"
                "也未提供 type_value (URL)"
            )
        url = self._ensure_http_url(url, action_name)

        # 2. Background fetch + extract + close ─────────────────────────
        payload = await self._fetch_one(
            ctx,
            url,
            mode=mode,
            selectors=selectors,
            action_name=action_name,
        )
        title = str(payload.get("title") or "").strip()
        content = str(payload.get("content") or "").strip()
        final_url = str(payload.get("url") or url).strip()
        full_len = int(payload.get("full_len") or len(content))

        # 3. Restore origin focus (popup listener may have swapped) ─────
        await self._restore_origin(browser, page, action_name)

        # 4. Write to workflow memory ───────────────────────────────────
        workflow_memory = ctx.workflow_memory
        if workflow_memory is not None:
            stored = {
                "url": final_url,
                "title": title,
                "content": content,
            }
            for optional_key in (
                "mode",
                "selectors",
                "selector_count",
                "selector_content",
                "structured",
            ):
                if optional_key in payload:
                    stored[optional_key] = payload[optional_key]
            workflow_memory[memory_key] = stored
            workflow_memory["latest_memory"] = content[:200]

        # 5. RPA trail (deterministic replay knows nothing but the URL) ─
        try:
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "fetch_link_content",
                    "type_value": json.dumps(
                        {
                            "url": url,
                            "mode": mode,
                            "selectors": selectors,
                        },
                        ensure_ascii=False,
                    ) if (mode != "dom" or selectors) else url,
                    "memory_key": memory_key,
                })
            )
        except Exception as _trail_err:
            logger.debug("[fetch_link_content] rpa_trail append failed: %s", _trail_err)

        # 6. Tell VLM what happened ─────────────────────────────────────
        truncation_note = (
            f" (truncated from {full_len})"
            if full_len > len(content)
            else ""
        )
        # Surface HTTP status. 4xx/5xx is a strong signal for VLM to skip
        # the URL rather than treat the error page as real data.
        _http_status = payload.get("http_status")
        _http_ok = bool(payload.get("http_ok"))
        if _http_status is None:
            _http_line = "ℹ️ HTTP: 状态未知（data:/about: scheme 或 SPA 路由）"
            _http_advisory = ""
        elif 200 <= _http_status < 300:
            _http_line = f"✅ HTTP: {_http_status}"
            _http_advisory = ""
        elif 300 <= _http_status < 400:
            _http_line = f"↪️ HTTP: {_http_status}（重定向，已跟随）"
            _http_advisory = ""
        elif 400 <= _http_status < 500:
            _http_line = f"⚠️ HTTP: {_http_status} 客户端错误"
            _http_advisory = (
                "\n\n⚠️ [HTTP ERROR] 该链接返回 "
                f"{_http_status}（404/403/401 等），抓回的内容很可能是错误页 HTML。"
                "建议：不要 extract 该 memory，改用其他链接或换一个 selector。"
            )
        elif 500 <= _http_status < 600:
            _http_line = f"⚠️ HTTP: {_http_status} 服务器错误"
            _http_advisory = (
                "\n\n⚠️ [HTTP ERROR] 该链接返回 "
                f"{_http_status}（服务端故障）。"
                "建议：稍后用 wait+fetch_link_content 重试一次；连续失败应跳过。"
            )
        else:
            _http_line = f"❓ HTTP: {_http_status}"
            _http_advisory = ""
        browser.set_tab_notice(
            f"✅ fetch_link_content 完成 — 已用 JS 跨 tab 抓取并写回 memory。\n"
            f"  • URL: {final_url[:120]}\n"
            f"  • {_http_line}\n"
            f"  • Title: {title[:80]!r}\n"
            f"  • mode: {mode}; selectors: {selectors or 'default main/article/body'}\n"
            f"  • 抽取: {len(content)} 字{truncation_note}\n"
            f"  • 存储: workflow_memory[{memory_key!r}] = {{url, title, content, http_status}}\n"
            f"  • 新标签页已关闭，焦点仍在原页 — 截图不变是正确的\n"
            f"用法：\n"
            f"  • type_value 模板插值: {{{{{memory_key}.content}}}} / {{{{{memory_key}.title}}}}\n"
            f"  • 多链接抓取：emit fetch_links_batch，type_value 填 JSON {{target_ids:[...]}}",
            severity='info',
            coalesce=False,
        )
        if _http_advisory:
            # J: append HTTP advisory as a coalesced warn notice
            # so frontend gets max(info, warn) = warn severity.
            browser.set_tab_notice(
                _http_advisory, severity='warn', coalesce=True,
            )

        logger.info(
            "[FETCH LINK CONTENT] %s → memory[%s] (%d/%d chars)",
            url[:80], memory_key, len(content), full_len,
        )
        print(
            f"\033[1;36m🔗 [FETCH LINK]\033[0m "
            f"{url[:60]}... → memory[\033[33m{memory_key}\033[0m] "
            f"(\033[32m{len(content)}\033[0m chars)"
        )

        return page  # skip Tab Guard


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
                    f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
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

            # ── No-op short-circuit: input already contains the target text ─
            # When VLM types the same text twice in a row (run_log_20260518_141728
            # step 1+2 both typed "介绍一下deepseek" because step 1's success
            # wasn't obvious from the next screenshot), the second type appends
            # or overwrites and breaks the message. If the element's current
            # text already exactly equals what we're about to type, skip the
            # action and tell VLM the value is set.
            try:
                _current_value = await target.handle.evaluate(
                    """el => {
                        const v = (el.value != null ? el.value : null);
                        if (v != null) return String(v);
                        return String(el.innerText || el.textContent || '');
                    }"""
                )
                _current_norm = str(_current_value or "").strip()
                _target_norm = str(type_value or "").strip()
                if _target_norm and _current_norm == _target_norm:
                    logger.info(
                        "[TYPE NO-OP] element #%s already contains target text "
                        "(%d chars); skipping duplicate type",
                        target_id, len(_target_norm),
                    )
                    print(
                        f"\033[33m⏭️  [TYPE NO-OP]\033[0m 输入框已含目标文本 "
                        f"({len(_target_norm)} 字)，跳过重复输入"
                    )
                    browser.rpa_trail.append(
                        ctx.with_rpa_meta({
                            "action": "type",
                            "target_id": target_id,
                            "type_value": type_value,
                            "noop": True,
                        })
                    )
                    # Arm the post-noop hard-correction signal so the NEXT
                    # step's guard chain can hard-rewrite a duplicate type
                    # to press_key Enter. The notice (soft signal) alone
                    # doesn't always change VLM's mind — its thought may
                    # say "no need to type" while the JSON still emits
                    # type (run_log_20260518_145655 step 4: thought said
                    # "无需重复输入" but action was type again).
                    try:
                        browser._last_type_noop = {
                            "target_id": int(target_id),
                            "value": _target_norm,
                        }
                    except Exception:
                        pass
                    try:
                        browser.set_tab_notice(
                            f"⏭️ [TYPE NO-OP] 输入框 #{target_id} 已含目标文本 "
                            f"{_target_norm[:40]!r}。"
                            "下一步请直接 press_key Enter 提交或点发送按钮；"
                            "不要重复 type 同一内容（如再次 type，系统会自动改写为 press_key Enter）。",
                            severity='info',
                            coalesce=False,
                        )
                    except Exception:
                        pass
                    return None
            except Exception as _noop_err:
                logger.debug("[TYPE] no-op precheck failed (non-fatal): %s", _noop_err)

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
                    f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
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
                        # Surface tooltip text to next VLM step. Without
                        # this, the tooltip is gone by the next screenshot
                        # (mouse moves to SoM sampler) and VLM re-hovers.
                        try:
                            _trimmed = tooltip_text[:200]
                            _tooltip_notice = (
                                f"ℹ️ [TOOLTIP READ] hover ＃{target_id} "
                                f"显示 tooltip: {_trimmed!r}\n"
                                "→ 该文本已被系统读取。下一步可直接 extract / done / "
                                "需要在上下文中使用 tooltip 文本，不要重复 hover 同一元素。"
                            )
                            if not browser._tab_switch_notice:
                                # J: route through set_tab_notice so the
                                # severity slot stays in sync; guard is kept
                                # so a richer Tab Guard notice still wins.
                                browser.set_tab_notice(
                                    _tooltip_notice, severity="info", coalesce=False,
                                )
                        except Exception:
                            pass
                except Exception as _tooltip_err:
                    logger.debug("[HOVER] tooltip text capture skipped: %s", _tooltip_err)
            else:
                logger.warning(
                    f"[HOVER TIMEOUT] No new elements appeared after hovering "
                    f"#{target_id} within 2s — parent item may be incorrect or "
                    f"menu requires a click to open."
                )
                # Tooltips are often non-interactive poppers, so the interactive
                # element count can stay unchanged even when a tooltip is visible.
                try:
                    tooltip_text = await page.evaluate(
                        """() => {
                            const selectors = [
                                '[role="tooltip"]',
                                '.el-tooltip__popper',
                                '.el-popper[aria-hidden="false"]',
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
                        # Surface tooltip text to next VLM step. Without
                        # this, the tooltip is gone by the next screenshot
                        # (mouse moves to SoM sampler) and VLM re-hovers.
                        try:
                            _trimmed = tooltip_text[:200]
                            _tooltip_notice = (
                                f"ℹ️ [TOOLTIP READ] hover ＃{target_id} "
                                f"显示 tooltip: {_trimmed!r}\n"
                                "→ 该文本已被系统读取。下一步可直接 extract / done / "
                                "需要在上下文中使用 tooltip 文本，不要重复 hover 同一元素。"
                            )
                            if not browser._tab_switch_notice:
                                # J: route through set_tab_notice so the
                                # severity slot stays in sync; guard is kept
                                # so a richer Tab Guard notice still wins.
                                browser.set_tab_notice(
                                    _tooltip_notice, severity="info", coalesce=False,
                                )
                        except Exception:
                            pass
                except Exception as _tooltip_err:
                    logger.debug("[HOVER] tooltip text capture skipped: %s", _tooltip_err)
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
                    f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
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
    """drag_and_drop v2 — supports text-located destinations.

    type_value may be:
      * a SoM ID string (digits)         e.g. "42"
      * a text-locator     ``text:<txt>`` e.g. "text:In Progress"

    For text targets we resolve the destination via cross-frame
    ``get_by_text`` (most-specific match wins by shortest text length),
    matching the same heuristic used by row_action / extract_row.
    """

    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        type_value = (ctx.action.type_value or "").strip()
        # ── Parse destination spec ────────────────────────────────────
        is_text_dest = type_value.lower().startswith("text:")
        dest_text = ""
        drop_id = 0
        if is_text_dest:
            dest_text = type_value.split(":", 1)[1].strip()
            if not dest_text:
                raise RuntimeError(
                    "drag_and_drop: type_value 'text:' 后必须有目标可见文本"
                )
        else:
            try:
                drop_id = int(type_value) if type_value else 0
            except ValueError:
                raise RuntimeError(
                    f"drag_and_drop: type_value 必须是 SoM ID (数字) 或 'text:<文本>' "
                    f"形式，得到 {type_value!r}"
                )
        logger.info(
            "Executing drag_and_drop: #%s → %s",
            target_id,
            f"text={dest_text!r}" if is_text_dest else f"#{drop_id}",
        )
        try:
            await browser._clear_som_overlays()
            source = await browser._resolve_action_target(target_id, "drag_and_drop")
            if not source:
                raise RuntimeError(f"Drag source element #{target_id} not found")

            await source.handle.scroll_into_view_if_needed(
                timeout=browser._LOCATOR_TIMEOUT
            )
            _src_xpath = await browser._get_xpath(source.handle)
            _src_ax_role, _src_ax_name = await browser._get_accessibility_signature(
                page, source.handle
            )

            # ── Resolve destination handle ────────────────────────────
            dest_handle = None
            _dst_xpath = ""
            if is_text_dest:
                # Cross-frame text search — pick most-specific (shortest) match.
                try:
                    candidate_frames = list(page.frames)
                except Exception:
                    candidate_frames = [page]
                try:
                    mf = page.main_frame
                    if mf in candidate_frames:
                        candidate_frames.sort(key=lambda f: 0 if f is mf else 1)
                except Exception:
                    pass
                for frame in candidate_frames:
                    try:
                        loc = frame.get_by_text(dest_text, exact=False)
                    except Exception:
                        continue
                    try:
                        n = await loc.count()
                    except Exception:
                        continue
                    if n == 0:
                        continue
                    best_idx = 0
                    if n > 1:
                        lengths = []
                        for i in range(min(n, 10)):
                            try:
                                txt = await loc.nth(i).inner_text()
                                lengths.append((i, len((txt or "").strip())))
                            except Exception:
                                lengths.append((i, 10_000))
                        lengths.sort(key=lambda x: x[1])
                        best_idx = lengths[0][0]
                    cand = loc.nth(best_idx)
                    try:
                        dest_handle = await cand.element_handle()
                    except Exception:
                        dest_handle = None
                    if dest_handle is not None:
                        try:
                            _dst_xpath = await browser._get_xpath(dest_handle)
                        except Exception:
                            _dst_xpath = ""
                        break
                if dest_handle is None:
                    raise RuntimeError(
                        f"Drop target text {dest_text!r} not found in any frame"
                    )
            else:
                dest = await browser._resolve_action_target(drop_id, "drag_and_drop")
                if not dest:
                    raise RuntimeError(f"Drop target element #{drop_id} not found")
                dest_handle = dest.handle
                try:
                    _dst_xpath = await browser._get_xpath(dest_handle)
                except Exception:
                    _dst_xpath = ""

            await source.handle.drag_to(dest_handle, timeout=browser._LOCATOR_TIMEOUT)
            _dest_label = (
                f"text={dest_text!r}" if is_text_dest else f"#{drop_id}"
            )
            logger.info(f"drag_and_drop #{target_id} → {_dest_label} succeeded")
            if _src_xpath:
                browser.rpa_trail.append(
                    ctx.with_rpa_meta({
                        "action": "drag_and_drop",
                        "xpath": _src_xpath,
                        "ax_role": _src_ax_role or "",
                        "ax_name": _src_ax_name or "",
                        "type_value": type_value or str(drop_id),
                        "drop_xpath": _dst_xpath or "",
                        "dest_text": dest_text if is_text_dest else "",
                    })
                )
            try:
                _notice = (
                    f"✅ [DRAG DONE] #{target_id} → {_dest_label}\n"
                    "→ 下一步看页面状态确认 drop 后效果。"
                )
                if not browser._tab_switch_notice:
                    # J: route through set_tab_notice so _last_notice_severity
                    # stays in sync with the legacy _tab_switch_notice slot.
                    browser.set_tab_notice(_notice, severity="info", coalesce=False)
            except Exception:
                pass
        except Exception as e:
            browser._last_action_error = e
            _dest_label = (
                f"text={dest_text!r}" if is_text_dest else f"#{drop_id}"
            )
            logger.error(f"drag_and_drop #{target_id} → {_dest_label} failed: {e}")

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
            return await page.evaluate(DATA_SIGNATURE_JS) or {}
        except Exception as exc:
            logger.debug("[NEXT_PAGE] data signature probe failed: %s", exc)
            return {}

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
            probe = await self._js_mark_pagination_candidate(page)
            if probe.get("found"):
                loc = page.locator('[data-vspider-next-page-probe="1"]').first
                used_strategy = (
                    f"js_probe_pre_url {probe.get('strategy')} "
                    f"label={probe.get('label')!r}"
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
                probe = await self._js_mark_pagination_candidate(page)
                if probe.get("found"):
                    loc = page.locator('[data-vspider-next-page-probe="1"]').first
                    used_strategy = (
                        f"js_probe {probe.get('strategy')} "
                        f"label={probe.get('label')!r}"
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


@ActionRegistry.register("row_action")
class RowActionHandler(ActionHandler):
    """Locate a row by visible text, then click an in-row button by its text.

    # ROW_ACTION_V2  -- enhanced with: cross-frame search, auto-confirm,
    # and canvas-fallback hinting.

    Why this exists
    ===============
    "Delete the row where name='Li Si'" is one of the most common tabular
    intents AND one of the most common ways VLMs fail: every row has its
    own "Delete" button with a different SoM red-box ID, and VLM picks the
    wrong one by visual order (which doesn't match DOM order).

    Schema
    ------
        target_id   = 0  (ignored - row & button are both text-located)
        type_value  = "<row filter>||<button text>[||confirm]"
                       e.g. "Li Si||Delete"
                            "ORD-2024-001||View"
                            "Li Si||Delete||confirm"   <- auto-confirm modal

    Where the 3rd segment, if present and one of
    ``confirm/yes/ok/true/1/确认/确定``, triggers an
    additional click on the post-action confirm modal's "OK" button.

    Implementation (deterministic, no VLM in the loop):
      1. Iterate ``page.frames`` (top + all iframes - admin panels matter).
      2. For each frame, try a chain of row selectors
         (``tr`` / ``[role=row]`` / Element / Ant / Naive / generic).
      3. ``locator.filter(has_text=<row>)`` to lock onto the matching row.
         Multiple matches -> pick the row with the *shortest* total text
         (closest to "just the filter").
      4. ``locator(button|a|[role=button]).filter(has_text=<btn>)`` for
         the in-row action.
      5. Click using the project's standard JS-fallback wrapper.
      6. If ``auto_confirm`` was requested, wait briefly for a modal to
         render in the SAME frame, then click its OK button.
      7. On total failure, probe for large ``canvas`` / ``svg`` elements
         and append a data_export hint to the error message.
    """

    # Class-level constants so tests can monkeypatch them.
    _ROW_SELECTORS = (
        "tr",
        "[role=row]",
        ".el-table__row",
        ".ant-table-row",
        ".n-data-table-tr",
        ".table-row",
    )
    _BUTTON_SELECTORS = (
        "button",
        "a",
        "[role=button]",
        ".el-button",
        ".ant-btn",
        ".n-button",
        "[data-action]",
    )
    # Modal / message-box / Popconfirm scopes searched for the OK button
    # after a row click. Order matters: more-specific first.
    _CONFIRM_MODAL_SCOPES = (
        ".el-message-box",
        ".el-message-box__btns",
        ".ant-modal-confirm",
        ".ant-modal-confirm-btns",
        ".ant-popover-buttons",
        ".el-popconfirm",
        "[role=alertdialog]",
        "[role=dialog]",
        ".n-modal",
        ".v-dialog",
        ".modal.show",  # Bootstrap
    )
    _CONFIRM_LABELS = (
        # In priority order: exact CN first, then EN. Native fallthrough
        # to "Yes" handles Element/Ant/Naive whose i18n strings vary.
        "确定", "确认", "提交", "是",
        "OK", "Ok", "Yes", "Confirm", "Submit",
    )
    _AUTO_CONFIRM_TOKENS = frozenset({
        "confirm", "yes", "ok", "true", "1",
        "确认", "确定", "y",
    })

    async def execute(self, ctx: "ActionContext") -> "Optional[Page]":
        browser = ctx.browser
        page = ctx.page
        raw = (ctx.action.type_value or "").strip()
        if not page:
            raise ActionExecutionError("row_action: 无活动页面。")
        if "||" not in raw:
            raise ActionExecutionError(
                "row_action 必须用 '||' 分隔行筛选文本和按钮文字，"
                f"如 '张三||删除' 或 '张三||删除||confirm'。"
                f"当前 type_value={raw!r}"
            )
        parts = [p.strip() for p in raw.split("||")]
        if len(parts) < 2 or len(parts) > 3:
            raise ActionExecutionError(
                "row_action 期望 '行筛选||按钮文字' 或 "
                "'行筛选||按钮文字||confirm'，"
                f"得到 {len(parts)} 段: {raw!r}"
            )
        row_filter = parts[0]
        btn_text = parts[1]
        auto_confirm = (
            len(parts) == 3
            and parts[2].lower() in self._AUTO_CONFIRM_TOKENS
        )
        if not row_filter or not btn_text:
            raise ActionExecutionError(
                "row_action: 行筛选文本和按钮文字都不能为空。"
                f"当前: row={row_filter!r} button={btn_text!r}"
            )

        await browser._clear_som_overlays()

        # ── Step 1-5: locate + click ─────────────────────────────────
        clicked_path = ""
        used_frame = None
        # page.frames includes the main frame; iterate them all.
        try:
            candidate_frames = list(page.frames)
        except Exception:
            candidate_frames = [page]
        # Order: main frame first (most common); the rest in document order.
        try:
            main_frame = page.main_frame
            if main_frame in candidate_frames:
                candidate_frames.sort(key=lambda f: 0 if f is main_frame else 1)
        except Exception:
            pass

        for frame in candidate_frames:
            for row_sel in self._ROW_SELECTORS:
                try:
                    row_loc = frame.locator(row_sel).filter(has_text=row_filter)
                except Exception:
                    continue
                try:
                    row_count = await row_loc.count()
                except Exception:
                    continue
                if row_count == 0:
                    continue
                best_idx = 0
                if row_count > 1:
                    lengths = []
                    for i in range(min(row_count, 10)):
                        try:
                            txt = await row_loc.nth(i).inner_text()
                            lengths.append((i, len((txt or "").strip())))
                        except Exception:
                            lengths.append((i, 10_000))
                    lengths.sort(key=lambda x: x[1])
                    best_idx = lengths[0][0]
                    logger.info(
                        "[ROW_ACTION] %d rows matched filter %r in frame=%r %s; "
                        "picked row index %d (shortest text)",
                        row_count, row_filter,
                        getattr(frame, "url", "?")[:60], row_sel, best_idx,
                    )
                target_row = row_loc.nth(best_idx)
                try:
                    await target_row.scroll_into_view_if_needed(
                        timeout=browser._LOCATOR_TIMEOUT
                    )
                except Exception:
                    pass
                for btn_sel in self._BUTTON_SELECTORS:
                    try:
                        btn = target_row.locator(btn_sel).filter(has_text=btn_text)
                        if await btn.count() == 0:
                            continue
                        cand = btn.first
                        if not await cand.is_visible():
                            continue
                        mode = await _click_locator_with_js_fallback(
                            cand,
                            f"row_action frame={getattr(frame, 'url', '?')[:40]!r} "
                            f"row={row_sel} filter={row_filter!r} "
                            f"btn={btn_sel} text={btn_text!r}",
                            timeout=3000,
                        )
                        clicked_path = (
                            f"frame={getattr(frame, 'url', 'main')[:60]!r} "
                            f"{row_sel}.has_text({row_filter!r})[{best_idx}] "
                            f"-> {btn_sel}.has_text({btn_text!r}) ({mode})"
                        )
                        used_frame = frame
                        break
                    except Exception as click_err:
                        logger.debug(
                            "[ROW_ACTION] %s.filter(%r) %s.filter(%r) failed: %s",
                            row_sel, row_filter, btn_sel, btn_text, click_err,
                        )
                        continue
                if clicked_path:
                    break
            if clicked_path:
                break

        if not clicked_path:
            # ── Step 7: canvas/svg fallback hint ─────────────────────
            canvas_hint = ""
            try:
                has_canvas_table = await page.evaluate(
                    """() => {
                        const els = [
                            ...document.querySelectorAll('canvas'),
                            ...document.querySelectorAll('svg')
                        ];
                        return els.some(el => {
                            const r = el.getBoundingClientRect();
                            return r.width >= 500 && r.height >= 300;
                        });
                    }"""
                )
            except Exception:
                has_canvas_table = False
            if has_canvas_table:
                canvas_hint = (
                    "\n\n💡 检测到页面含大尺寸 canvas/svg，目标"
                    "表格可能是 canvas/svg 渲染（如 AntV / "
                    "ECharts / Handsontable canvas-mode / PDF preview）。"
                    "row_action 只能操作 DOM 节点，请改用 data_export skill，"
                    "或在原页找一个「导出 CSV / 复制表格」按钮手动触发下载。"
                )
            raise ActionExecutionError(
                f"row_action: 在页面所有 frame 中找不到含 "
                f"{row_filter!r} 的行 + 行内按钮 {btn_text!r}。"
                "检查行筛选文本是否完全可见、按钮文字是否完整匹配。"
                + canvas_hint
            )

        # ── Step 6: auto-confirm a follow-up Modal/MessageBox ────────
        auto_confirm_status = ""
        if auto_confirm:
            try:
                # Brief animation pause; el-message-box typically opens in
                # 100-200ms, ant-modal-confirm in 200-300ms.
                import asyncio as _asyncio
                await _asyncio.sleep(0.4)
                # First scan the click frame, then the main page frames.
                confirm_frames = []
                if used_frame is not None:
                    confirm_frames.append(used_frame)
                try:
                    for f in page.frames:
                        if f is not used_frame:
                            confirm_frames.append(f)
                except Exception:
                    pass

                confirmed_with = ""
                for cf in confirm_frames:
                    for scope_sel in self._CONFIRM_MODAL_SCOPES:
                        for label in self._CONFIRM_LABELS:
                            try:
                                # Button (or [role=button]) within the modal scope
                                btn = (
                                    cf.locator(scope_sel)
                                    .locator("button, [role=button], .el-button, .ant-btn")
                                    .filter(has_text=label)
                                )
                                if await btn.count() == 0:
                                    continue
                                cand = btn.first
                                if not await cand.is_visible():
                                    continue
                                await _click_locator_with_js_fallback(
                                    cand,
                                    f"row_action auto_confirm scope={scope_sel} label={label!r}",
                                    timeout=2000,
                                )
                                confirmed_with = (
                                    f"{scope_sel} > '{label}'"
                                )
                                break
                            except Exception:
                                continue
                        if confirmed_with:
                            break
                    if confirmed_with:
                        break

                if confirmed_with:
                    auto_confirm_status = (
                        f"; auto_confirm OK ({confirmed_with})"
                    )
                else:
                    # Could be a native confirm() - check the recently-captured
                    # dialog text. If so, we're already done (listener accepted it).
                    last_dialog = getattr(browser, "_last_native_dialog", None)
                    if last_dialog and last_dialog.get("type") in {"confirm", "alert"}:
                        auto_confirm_status = (
                            "; auto_confirm: 原生 confirm() 已被系统 accept"
                        )
                    else:
                        auto_confirm_status = (
                            "; auto_confirm: 未发现确认弹窗"
                            "（可能不需要确认，或使用了非标准 modal）"
                        )
            except Exception as confirm_err:
                logger.warning(
                    "[ROW_ACTION] auto_confirm phase error: %s", confirm_err
                )
                auto_confirm_status = f"; auto_confirm error: {confirm_err}"

        logger.info("[ROW_ACTION] ✅ %s%s", clicked_path, auto_confirm_status)
        print(
            f"\033[1;35m🎯 [ROW_ACTION]\033[0m "
            f"row={row_filter!r} → click {btn_text!r}{auto_confirm_status}"
        )
        browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "row_action",
                "row_filter": row_filter,
                "button_text": btn_text,
                "auto_confirm": auto_confirm,
                "strategy": clicked_path,
                "confirm_status": auto_confirm_status.lstrip("; "),
            })
        )
        # Surface to VLM next turn so it knows the destructive op completed
        # AND whether a follow-up confirm was already handled here.
        try:
            _notice = (
                f"✅ [ROW_ACTION DONE] row={row_filter!r} "
                f"button={btn_text!r}"
                + (f" {auto_confirm_status.strip(';').strip()}" if auto_confirm_status else "")
                + "\n→ 下一步请看页面状态确认结果"
                + ("。如果 modal 还在，手动 click_text 确定按钮。" if not auto_confirm or "OK" not in auto_confirm_status else "。")
            )
            if not browser._tab_switch_notice:
                # J: route through set_tab_notice so _last_notice_severity
                # stays in sync with the legacy _tab_switch_notice slot.
                # Guard is preserved (skip-if-prior) — coalesce flag is
                # therefore moot but kept explicit for documentation.
                browser.set_tab_notice(_notice, severity="info", coalesce=False)
        except Exception:
            pass
        await browser._wait_after_action()
        return None


@ActionRegistry.register("extract_row")
class ExtractRowHandler(ActionHandler):
    """Read a value from a table row, write it to ``workflow_memory``.

    Symmetric counterpart to ``row_action`` (which CLICKS an in-row button).
    Use this when the goal is "tell me X for the row matching Y" rather than
    "do something destructive to that row".

    Schema
    ------
        target_id  = 0  (ignored - row & column are both text-located)
        type_value = "<row filter>||<column>"
                     - row filter: visible text uniquely identifying the row
                     - column: column header text (e.g. "Status", "状态")
                              OR ``*`` to capture the whole row's inner_text
        memory_key = (optional) target memory slot; auto-named if absent

    Examples
    --------
        {"action":"extract_row","type_value":"ORD-2024-001||状态",
         "memory_key":"order_status"}
        {"action":"extract_row","type_value":"alice@x.com||*",
         "memory_key":"alice_row_text"}

    Implementation
    --------------
    1. Iterate page.frames (same as row_action v2 - admin iframes matter).
    2. Locate the row via row-selector chain + ``filter(has_text=row)``.
    3. If column == "*", read ``inner_text()`` of the whole row.
       Otherwise: enumerate <th> in the row's parent table, find the
       header text matching the requested column (case-insensitive,
       trimmed), then read the cell at the same column index.
    4. Write to ``workflow_memory[memory_key]``; also update
       ``latest_memory`` for downstream save_to_memory chains.
    5. Push a ``[ROW EXTRACTED]`` notice to ``_tab_switch_notice`` so
       VLM doesn't re-attempt the read on the next turn.
    """

    _ROW_SELECTORS = RowActionHandler._ROW_SELECTORS

    async def execute(self, ctx: "ActionContext") -> "Optional[Page]":
        browser = ctx.browser
        page = ctx.page
        raw = (ctx.action.type_value or "").strip()
        if not page:
            raise ActionExecutionError("extract_row: 无活动页面。")
        if "||" not in raw:
            raise ActionExecutionError(
                "extract_row 必须用 '||' 分隔行筛选和列名，"
                f"如 'ORD-2024-001||状态' 或 'alice||*'。"
                f"当前 type_value={raw!r}"
            )
        parts = [p.strip() for p in raw.split("||")]
        if len(parts) != 2:
            raise ActionExecutionError(
                "extract_row 期望息严格 2 段（行筛选||列名），"
                f"得到 {len(parts)}: {raw!r}"
            )
        row_filter, column = parts[0], parts[1]
        if not row_filter or not column:
            raise ActionExecutionError(
                "extract_row: 行筛选文本和列名都不能为空。"
                f"row={row_filter!r} column={column!r}"
            )
        whole_row = (column == "*")

        await browser._clear_som_overlays()

        try:
            candidate_frames = list(page.frames)
        except Exception:
            candidate_frames = [page]
        try:
            mf = page.main_frame
            if mf in candidate_frames:
                candidate_frames.sort(key=lambda f: 0 if f is mf else 1)
        except Exception:
            pass

        captured_text = ""
        captured_path = ""
        for frame in candidate_frames:
            for row_sel in self._ROW_SELECTORS:
                try:
                    row_loc = frame.locator(row_sel).filter(has_text=row_filter)
                except Exception:
                    continue
                try:
                    row_count = await row_loc.count()
                except Exception:
                    continue
                if row_count == 0:
                    continue
                best_idx = 0
                if row_count > 1:
                    lengths = []
                    for i in range(min(row_count, 10)):
                        try:
                            txt = await row_loc.nth(i).inner_text()
                            lengths.append((i, len((txt or "").strip())))
                        except Exception:
                            lengths.append((i, 10_000))
                    lengths.sort(key=lambda x: x[1])
                    best_idx = lengths[0][0]
                target_row = row_loc.nth(best_idx)
                try:
                    await target_row.scroll_into_view_if_needed(
                        timeout=browser._LOCATOR_TIMEOUT
                    )
                except Exception:
                    pass
                try:
                    if whole_row:
                        text = await target_row.inner_text()
                        captured_text = (text or "").strip()
                    else:
                        # Column-by-header lookup. Run in-frame to keep all
                        # heuristics atomic w.r.t. DOM mutations.
                        cell_text = await target_row.evaluate(
                            """(rowEl, colName) => {
                                const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                const target = norm(colName);
                                let table = rowEl.closest('table');
                                let headers = [];
                                if (table) {
                                    headers = Array.from(table.querySelectorAll('thead th'));
                                    if (!headers.length) {
                                        const firstRow = table.querySelector('tr');
                                        if (firstRow) headers = Array.from(firstRow.querySelectorAll('th, td'));
                                    }
                                }
                                if (!headers.length) {
                                    const grid = rowEl.closest('[role=grid], [role=table]');
                                    if (grid) {
                                        const headerRow = grid.querySelector('[role=rowheader]')
                                            || grid.querySelector('[role=row]');
                                        if (headerRow) headers = Array.from(headerRow.querySelectorAll('[role=columnheader], [role=cell]'));
                                    }
                                }
                                let colIdx = headers.findIndex(h => norm(h.innerText || h.textContent) === target);
                                if (colIdx === -1) {
                                    colIdx = headers.findIndex(h => norm(h.innerText || h.textContent).includes(target));
                                }
                                if (colIdx === -1) return null;
                                const cells = Array.from(rowEl.querySelectorAll(':scope > td, :scope > [role=cell], :scope > [role=gridcell]'));
                                if (!cells.length) return null;
                                if (colIdx >= cells.length) return null;
                                return (cells[colIdx].innerText || cells[colIdx].textContent || '').trim();
                            }""",
                            column,
                        )
                        if cell_text is not None:
                            captured_text = cell_text
                    captured_path = (
                        f"frame={getattr(frame, 'url', 'main')[:60]!r} "
                        f"{row_sel}.has_text({row_filter!r})[{best_idx}]"
                        f"::{column!r}"
                    )
                    break
                except Exception as read_err:
                    logger.debug(
                        "[EXTRACT_ROW] read failed in %s: %s", row_sel, read_err
                    )
                    continue
            if captured_path:
                break

        if not captured_path:
            raise ActionExecutionError(
                f"extract_row: 在页面所有 frame 中找不到含 "
                f"{row_filter!r} 的行。检查行筛选文本是否可见。"
            )
        if not whole_row and not captured_text:
            raise ActionExecutionError(
                f"extract_row: 找到了行，但列 {column!r} 未在表头里匹配。"
                "请在表头中查看列标题原文（如 '下单时间' vs '创建时间'）。"
            )

        # Memory writeback
        memory_key = (getattr(ctx.action, "memory_key", "") or "").strip()
        if not memory_key:
            # Auto-name from row_filter; alphanumeric + underscore.
            normalised = "".join(
                c if c.isalnum() else "_" for c in row_filter[:32]
            ).strip("_") or "row"
            col_part = "all" if whole_row else "".join(
                c if c.isalnum() else "_" for c in column[:16]
            ).strip("_") or "col"
            memory_key = f"row_{normalised}_{col_part}"
            logger.info("[EXTRACT_ROW] memory_key auto-named: %s", memory_key)

        if ctx.workflow_memory is not None:
            try:
                ctx.workflow_memory[memory_key] = captured_text
                ctx.workflow_memory["latest_memory"] = captured_text[:200]
            except Exception as mem_err:
                logger.warning("[EXTRACT_ROW] memory write failed: %s", mem_err)

        logger.info(
            "[EXTRACT_ROW] ✅ row=%r col=%r path=%s -> memory[%s] (%d chars)",
            row_filter, column, captured_path, memory_key, len(captured_text),
        )
        print(
            f"\033[1;36m✂️  [EXTRACT_ROW]\033[0m "
            f"row={row_filter!r} col={column!r} → memory[\033[33m{memory_key}\033[0m] "
            f"({len(captured_text)} 字)"
        )

        browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "extract_row",
                "row_filter": row_filter,
                "column": column,
                "memory_key": memory_key,
                "strategy": captured_path,
                "captured_chars": len(captured_text),
            })
        )
        try:
            preview = captured_text[:120]
            if len(captured_text) > 120:
                preview += "…"
            _notice = (
                f"✂️ [ROW EXTRACTED] row={row_filter!r} "
                f"col={column!r} → memory[{memory_key}]\n"
                f"  · 值: {preview!r}\n"
                "→ 下一步可用 {{" + memory_key + "}} 模板引用，不要重复 extract_row。"
            )
            if not browser._tab_switch_notice:
                # J: route through set_tab_notice so _last_notice_severity
                # stays in sync with the legacy _tab_switch_notice slot.
                # Guard is preserved (skip-if-prior) — coalesce flag is
                # therefore moot but kept explicit for documentation.
                browser.set_tab_notice(_notice, severity="info", coalesce=False)
        except Exception:
            pass

        await browser._wait_after_action(light_action=True)
        return None


@ActionRegistry.register("tree_check")
class TreeCheckHandler(ActionHandler):
    """Toggle a checkbox in a tree-view by visible node text.

    Symmetric counterpart to ``click_text`` for tree controls. The native
    impulse to ``click_text "Settings"`` on a checkbox tree HITS the label
    (which usually toggles selection but not check state) and leaves the
    actual checkbox alone in many UI kits.

    Schema
    ------
        target_id  = 0  (ignored - node is text-located)
        type_value = "<node text>"            (default: check)
                   | "<node text>||check"
                   | "<node text>||uncheck"

    Examples
    --------
        {"action":"tree_check","type_value":"设置"}
        {"action":"tree_check","type_value":"Settings||check"}
        {"action":"tree_check","type_value":"Frontend||uncheck"}

    Implementation
    --------------
    1. Iterate page.frames (cross-iframe).
    2. Locate the tree node by text within a tree-node selector chain
       (``[role=treeitem]`` / ``.el-tree-node`` / ``.ant-tree-treenode``
       / ``.n-tree-node``).
    3. Within that node, find the FIRST checkbox-like element via JS
       (covers ``input[type=checkbox]``, ``[role=checkbox]``, and
       framework-specific wrappers like ``.el-checkbox__input``,
       ``.ant-tree-checkbox``).
    4. Read its current ``aria-checked`` / ``checked`` state. If the
       desired state is already set, no-op (idempotent).
    5. Otherwise click the checkbox using the JS-fallback wrapper.
    """

    _TREE_NODE_SELECTORS = (
        "[role=treeitem]",
        ".el-tree-node",
        ".ant-tree-treenode",
        ".n-tree-node",
        ".tree-node",
    )

    async def execute(self, ctx: "ActionContext") -> "Optional[Page]":
        browser = ctx.browser
        page = ctx.page
        raw = (ctx.action.type_value or "").strip()
        if not page:
            raise ActionExecutionError("tree_check: 无活动页面。")
        if not raw:
            raise ActionExecutionError(
                "tree_check: type_value 必须提供节点文本。"
            )
        parts = [p.strip() for p in raw.split("||")]
        if len(parts) > 2:
            raise ActionExecutionError(
                f"tree_check schema: '<节点文本>[||check|uncheck]'，得到 {len(parts)} 段: {raw!r}"
            )
        node_text = parts[0]
        op = parts[1].lower() if len(parts) == 2 else "check"
        if op not in {"check", "uncheck", "toggle"}:
            raise ActionExecutionError(
                f"tree_check op 必须是 check/uncheck/toggle，得到 {op!r}"
            )
        if not node_text:
            raise ActionExecutionError("tree_check: 节点文本不能为空。")

        await browser._clear_som_overlays()

        try:
            candidate_frames = list(page.frames)
        except Exception:
            candidate_frames = [page]
        try:
            mf = page.main_frame
            if mf in candidate_frames:
                candidate_frames.sort(key=lambda f: 0 if f is mf else 1)
        except Exception:
            pass

        toggled_path = ""
        prior_state = None
        new_state = None
        for frame in candidate_frames:
            for node_sel in self._TREE_NODE_SELECTORS:
                try:
                    node_loc = frame.locator(node_sel).filter(has_text=node_text)
                except Exception:
                    continue
                try:
                    n = await node_loc.count()
                except Exception:
                    continue
                if n == 0:
                    continue
                best_idx = 0
                if n > 1:
                    lengths = []
                    for i in range(min(n, 10)):
                        try:
                            txt = await node_loc.nth(i).inner_text()
                            lengths.append((i, len((txt or "").strip())))
                        except Exception:
                            lengths.append((i, 10_000))
                    lengths.sort(key=lambda x: x[1])
                    best_idx = lengths[0][0]
                node = node_loc.nth(best_idx)
                try:
                    await node.scroll_into_view_if_needed(
                        timeout=browser._LOCATOR_TIMEOUT
                    )
                except Exception:
                    pass
                # Run JS to find checkbox + read current state in one call.
                try:
                    state_info = await node.evaluate(
                        """(nodeEl) => {
                            const cb = nodeEl.querySelector(
                                'input[type=checkbox], '
                                + '[role=checkbox], '
                                + '.el-checkbox__original, '
                                + '.el-checkbox__input, '
                                + '.ant-tree-checkbox, '
                                + '.n-checkbox-box__border'
                            );
                            if (!cb) return {found: false};
                            // Resolve the most-likely interactive ancestor.
                            const interactive = cb.closest(
                                '.el-checkbox, .ant-tree-checkbox, .n-checkbox, label'
                            ) || cb;
                            let checked = false;
                            if (cb.tagName === 'INPUT') {
                                checked = !!cb.checked;
                            } else {
                                const ariaChecked = (cb.getAttribute('aria-checked') || interactive.getAttribute('aria-checked') || '').toLowerCase();
                                if (ariaChecked === 'true') checked = true;
                                else if (ariaChecked === 'mixed') checked = true;
                                // Framework class probes
                                const cls = (cb.className || '') + ' ' + (interactive.className || '');
                                if (/is-checked|ant-tree-checkbox-checked|n-checkbox--checked/.test(cls)) checked = true;
                            }
                            return {found: true, checked};
                        }"""
                    )
                except Exception as state_err:
                    logger.debug(
                        "[TREE_CHECK] state read failed in %s: %s",
                        node_sel, state_err,
                    )
                    continue

                if not state_info or not state_info.get("found"):
                    # Node matched by text but no checkbox in it; skip to
                    # next selector / frame in case a different DOM stamp
                    # has the right shape.
                    continue
                prior_state = bool(state_info.get("checked"))
                want = (
                    True if op == "check"
                    else False if op == "uncheck"
                    else (not prior_state)  # toggle
                )
                if prior_state == want:
                    new_state = prior_state
                    toggled_path = (
                        f"frame={getattr(frame, 'url', 'main')[:60]!r} "
                        f"{node_sel}.has_text({node_text!r}) [no-op: already {prior_state}]"
                    )
                    break
                # Click the interactive element.
                try:
                    cb_loc = node.locator(
                        ".el-checkbox, "
                        ".ant-tree-checkbox, "
                        ".n-checkbox, "
                        "[role=checkbox], "
                        "label:has(input[type=checkbox]), "
                        "input[type=checkbox]"
                    ).first
                    await _click_locator_with_js_fallback(
                        cb_loc,
                        f"tree_check node={node_text!r} op={op}",
                        timeout=2500,
                    )
                    new_state = want
                    toggled_path = (
                        f"frame={getattr(frame, 'url', 'main')[:60]!r} "
                        f"{node_sel}.has_text({node_text!r}) "
                        f"{prior_state} → {want}"
                    )
                    break
                except Exception as click_err:
                    logger.debug(
                        "[TREE_CHECK] click failed in %s: %s", node_sel, click_err
                    )
                    continue
            if toggled_path:
                break

        if not toggled_path:
            raise ActionExecutionError(
                f"tree_check: 在页面所有 frame 中找不到包含 "
                f"{node_text!r} 且含 checkbox 的树节点。"
                "检查：节点文本是否可见？该树是否启用了 checkbox 模式？"
                "（如未启用，请改用 click_text 点节点名选中）"
            )

        logger.info("[TREE_CHECK] ✅ %s", toggled_path)
        print(
            f"\033[1;35m☑️  [TREE_CHECK]\033[0m "
            f"node={node_text!r} op={op} → state={new_state}"
        )
        browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "tree_check",
                "node_text": node_text,
                "op": op,
                "prior_state": prior_state,
                "new_state": new_state,
                "strategy": toggled_path,
            })
        )
        try:
            _notice = (
                f"☑️ [TREE CHECK DONE] node={node_text!r} "
                f"op={op}: {prior_state} → {new_state}\n"
                "→ 下一步请看页面是否需要采集多个节点，或点「确定」提交。"
            )
            if not browser._tab_switch_notice:
                # J: route through set_tab_notice so _last_notice_severity
                # stays in sync with the legacy _tab_switch_notice slot.
                # Guard is preserved (skip-if-prior) — coalesce flag is
                # therefore moot but kept explicit for documentation.
                browser.set_tab_notice(_notice, severity="info", coalesce=False)
        except Exception:
            pass

        await browser._wait_after_action(light_action=True)
        return None


@ActionRegistry.register("set_prompt_response")
class SetPromptResponseHandler(ActionHandler):
    """Pre-arm the response value for the NEXT native ``prompt()`` dialog.

    Native ``prompt()`` is synchronous — the dialog listener must
    accept/dismiss within milliseconds, and there's no way to ask
    VLM what value to inject mid-dialog. This action lets VLM stage
    a value first, then trigger the action that opens the prompt.

    Schema
    ------
        target_id  = 0  (ignored)
        type_value = the string to submit when the next prompt fires

    Behaviour
    ---------
    * The value is **one-shot**: consumed by the next prompt(), then
      cleared. Subsequent prompts fall back to auto-accept-empty.
    * If no prompt() actually fires before the next page navigation
      or task end, the value is silently discarded (no leak across
      tasks).
    * Pairs with ``confirm_dialog`` skill: emit
      ``set_prompt_response`` in the SAME action batch as the click
      that triggers the prompt — they execute sequentially.
    """

    async def execute(self, ctx: "ActionContext") -> "Optional[Page]":
        browser = ctx.browser
        value = ctx.action.type_value or ""
        browser._next_prompt_response = value
        logger.info(
            "[SET_PROMPT_RESPONSE] armed: %r (length=%d)",
            value[:80], len(value),
        )
        print(
            f"\033[1;36m🗒️  [PROMPT ARMED]\033[0m "
            f"value={value[:60]!r} (len={len(value)})"
        )
        browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "set_prompt_response",
                "type_value": value,
            })
        )
        try:
            _notice = (
                f"🗒️ [PROMPT ARMED] 下一个原生 prompt() 将自动提交: {value[:80]!r}\n"
                "→ 请接下来发出会触发 prompt() 的动作（如 click）。"
                "仅一次有效。"
            )
            if not browser._tab_switch_notice:
                # J: route through set_tab_notice so _last_notice_severity
                # stays in sync with the legacy _tab_switch_notice slot.
                # Guard is preserved (skip-if-prior) — coalesce flag is
                # therefore moot but kept explicit for documentation.
                browser.set_tab_notice(_notice, severity="info", coalesce=False)
        except Exception:
            pass
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

def _persist_extracted_link(target_id: object, extracted_url: str) -> str:
    """Persist one extracted link honoring the active run's output_contract
    container (run-scoped), instead of a blind CWD ``output_links.xlsx``
    (mission §一-A: never default to Excel). Returns the written path, or ``""``
    when no run is active (so unit/test envs never litter the CWD).
    """
    try:
        from .io_contract import (
            current_base_dir,
            current_run_id,
            read_output_contract,
        )
        from .data_writers import save_run_dataset
    except ImportError:  # pragma: no cover - standalone import fallback
        from io_contract import (  # type: ignore[no-redef]
            current_base_dir,
            current_run_id,
            read_output_contract,
        )
        from data_writers import save_run_dataset  # type: ignore[no-redef]

    run_id = current_run_id()
    if not run_id:
        return ""
    base = current_base_dir()
    try:
        contract = read_output_contract(run_id, base_dir=base)
    except Exception:
        contract = None
    try:
        return save_run_dataset(
            {"target_id": target_id, "url": extracted_url},
            run_id=run_id,
            output_contract=contract,
            produced_by="extract_link",
            filename_hint="extract_link",
            source_url=str(extracted_url or ""),
            base_dir=base,
        )
    except Exception as exc:  # pragma: no cover - never break the action loop
        logger.warning("[EXTRACT_LINK] contract-aware save failed: %s", exc)
        return ""


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
                    f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
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
                _persist_extracted_link(target_id, extracted_url)
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
                    f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
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
                register_download_artifact(
                    filepath,
                    source_url=abs_url,
                    mime=content_type,
                    produced_by="browser_action",
                    step_id="download_image",
                )
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
                        f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
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


@ActionRegistry.register("chat_extract")
class ChatExtractHandler(ActionHandler):
    """Deterministic AI-chat answer extractor.

    Solves the failure mode where generic ``extract`` grabs search-result
    cards instead of the streaming AI answer on hybrid search-then-answer
    pages (yiyan.baidu.com, chat.baidu.com, etc.). Delegates to
    ``chat_answer_extractor.extract_chat_answer`` which:
      1. Polls until the answer-block text stabilises (streaming complete).
      2. Probes a curated selector cascade for known AI-answer containers.
      3. Falls back to the largest text block outside lists/nav/footer.

    Args from VSpiderAction:
      - ``type_value``: optional JSON ``{"timeout": 12, "min_length": 40}``;
        any plain string is accepted and ignored (logged at debug).
      - ``memory_key``: where to store the result; defaults to
        ``chat_answer_<ts>`` if missing.

    On success the answer text is written to:
      - ``workflow_memory[memory_key]`` = {"answer", "method", "url", ...}
      - ``workflow_memory["latest_memory"]`` = answer[:200]
      - the returned action result's ``extracted_data`` for VLM visibility.
    """

    _DEFAULT_TIMEOUT: ClassVar[float] = 25.0
    _DEFAULT_MIN_LEN: ClassVar[int] = 80

    # JS sweeping the whole document innerText, excluding nav/footer/search,
    # used as the absolute last-resort fallback when both selector cascade
    # AND largest-text heuristic come back empty.
    _WHOLE_PAGE_SWEEP_JS: ClassVar[str] = """
    (() => {
        const EXCLUDES = [
            'header', 'nav', 'footer', 'aside',
            'script', 'style', 'noscript', 'template',
            "[role='search']", "[role='navigation']", "[role='banner']", "[role='contentinfo']",
            "[class*='search-result']", "[class*='search_result']", "[class*='sresult']",
            "[class*='result-list']", "[class*='result_list']",
            "[class*='sidebar']", "[class*='side-bar']", "[class*='related']",
            "[class*='ad-']", "[class*='ads-']", "[id*='search-result']",
        ];
        // Clone the body and surgically strip excluded subtrees so innerText
        // reflects only the "content" zone.
        const clone = document.body ? document.body.cloneNode(true) : null;
        if (!clone) return { text: "", length: 0 };
        for (const sel of EXCLUDES) {
            try {
                clone.querySelectorAll(sel).forEach(n => n.parentNode && n.parentNode.removeChild(n));
            } catch (e) {}
        }
        const text = (clone.innerText || clone.textContent || "").trim();
        return { text: text, length: text.length };
    })()
    """

    @staticmethod
    def _parse_options(type_value: str) -> dict[str, Any]:
        """Accept either JSON or a bare URL/empty string. Never raises."""
        tv = (type_value or "").strip()
        if not tv:
            return {}
        if tv.startswith("{") and tv.endswith("}"):
            try:
                obj = json.loads(tv)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    async def _scroll_page_to_bottom(page: Any) -> None:
        """Trigger lazy-render of streaming chat content by scrolling the page
        and any inner scroll containers to the bottom. Some chat UIs only
        keep recent answer chunks alive in the DOM until the user is near
        the bottom; without this nudge, ``innerText`` is short."""
        try:
            await page.evaluate(
                """() => {
                    try { window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'}); } catch(e){}
                    const sc = document.querySelectorAll('[class*="scroll"], [class*="overflow"], main, article, [role="main"]');
                    for (const el of sc) {
                        try {
                            if (el.scrollHeight > el.clientHeight + 8) el.scrollTop = el.scrollHeight;
                        } catch(e){}
                    }
                }"""
            )
        except Exception as e:
            logger.debug("[chat_extract] pre-scroll failed: %s", e)

    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        action = ctx.action

        opts = self._parse_options(action.type_value or "")
        try:
            timeout = float(opts.get("timeout") or self._DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = self._DEFAULT_TIMEOUT
        try:
            min_length = int(opts.get("min_length") or self._DEFAULT_MIN_LEN)
        except (TypeError, ValueError):
            min_length = self._DEFAULT_MIN_LEN

        memory_key = (action.memory_key or "").strip()
        if not memory_key:
            memory_key = f"chat_answer_{int(time.time())}"
            logger.warning(
                "[chat_extract] memory_key missing — auto-generated %s",
                memory_key,
            )

        logger.info(
            "[chat_extract] start url=%s timeout=%.1fs min_len=%d key=%s",
            (page.url or "")[:90],
            timeout,
            min_length,
            memory_key,
        )
        print(
            f"\033[36m💬 [CHAT EXTRACT]\033[0m 等待 AI 回答流式完成 "
            f"(≤{timeout:.0f}s)，memory_key={memory_key!r}"
        )

        # Pre-scroll: nudge lazy-rendered streaming content into the DOM
        # BEFORE we start polling. This catches the common failure where the
        # answer is below the viewport and innerText only returns the first
        # paragraph.
        await self._scroll_page_to_bottom(page)
        try:
            await asyncio.sleep(0.4)
        except Exception:
            pass

        try:
            result = await extract_chat_answer(
                page,
                timeout=timeout,
                min_length=min_length,
            )
        except Exception as e:
            browser._last_action_error = e
            logger.error("[chat_extract] extractor crashed: %s", e)
            return None

        # ── Last-resort fallback: whole-page innerText sweep ───────────────
        # If the structured extractor came back empty/short on a chat-shaped
        # URL, do one final JS sweep — strip nav/footer/search-result from a
        # clone of <body>, then return whatever innerText is left. Better to
        # surface a 5-KB blob the VLM can summarise than a blank action.
        if (not result.get("ok")) or int(result.get("length") or 0) < min_length:
            try:
                sweep = await page.evaluate(self._WHOLE_PAGE_SWEEP_JS)
                sweep_text = clean_chat_answer_text(
                    str((sweep or {}).get("text") or "").strip()
                )
                if len(sweep_text) >= max(min_length, 120):
                    logger.info(
                        "[chat_extract] selector cascade missed — using whole-page sweep "
                        "(%d chars)", len(sweep_text),
                    )
                    result = {
                        "ok": True,
                        "answer": sweep_text[:8000],
                        "method": "fallback:page-innertext",
                        "selector": None,
                        "length": len(sweep_text),
                        "stable": True,
                        "wait_ms": int(result.get("wait_ms") or 0),
                        "polls": int(result.get("polls") or 0),
                        "url": page.url,
                        "title": (result.get("title") or ""),
                    }
            except Exception as e:
                logger.debug("[chat_extract] sweep fallback failed: %s", e)

        answer = str(result.get("answer") or "")
        ok = bool(result.get("ok"))
        method = str(result.get("method") or "none")

        workflow_memory = ctx.workflow_memory
        if workflow_memory is not None:
            workflow_memory[memory_key] = {
                "answer": answer,
                "method": method,
                "selector": result.get("selector"),
                "length": int(result.get("length") or 0),
                "stable": bool(result.get("stable")),
                "wait_ms": int(result.get("wait_ms") or 0),
                "url": result.get("url") or page.url,
                "title": result.get("title") or "",
            }
            if answer:
                workflow_memory["latest_memory"] = answer[:200]
                # Sentinel: tells the main loop "the chat task's answer is in
                # workflow_memory; you can stop". Without this main.py has no
                # way to know chat_extract is a TERMINAL action, and VLM gets
                # stuck re-emitting chat_extract every step (run_log_20260518_141728
                # ran 18 chat_extract iterations chasing the same answer).
                workflow_memory["__chat_extract_completed"] = {
                    "memory_key": memory_key,
                    "length": int(result.get("length") or 0),
                    "method": method,
                    "url": result.get("url") or page.url,
                }

        if ok:
            print(
                f"\033[32m✅ [CHAT EXTRACT]\033[0m method={method} "
                f"len={result.get('length')} wait={result.get('wait_ms')}ms"
            )
        else:
            err = result.get("error") or "no answer block matched selectors or fallback"
            logger.warning("[chat_extract] failed: %s", err)
            print(f"\033[33m⚠️  [CHAT EXTRACT]\033[0m 未匹配到回答块：{err}")

        # Mirror to action for downstream consumers / RPA trail / VLM history
        try:
            action.extracted_data = {
                "ok": ok,
                "answer": answer,
                "method": method,
                "memory_key": memory_key,
            }
        except Exception:
            pass

        try:
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "chat_extract",
                    "type_value": json.dumps(
                        {"timeout": timeout, "min_length": min_length},
                        ensure_ascii=False,
                    ),
                    "memory_key": memory_key,
                })
            )
        except Exception as _trail_err:
            logger.debug("[chat_extract] rpa_trail append failed: %s", _trail_err)

        return None


# ════════════════════════════════════════════════════════════════
#  Chat Submit —— 治"按 Enter 按不出去"的 chat 站点
# ════════════════════════════════════════════════════════════════

@ActionRegistry.register("chat_submit")
class ChatSubmitHandler(ActionHandler):
    """Deterministic click on the chat send button.

    Solves run_log_20260518_154220: yiyan.baidu.com 的发送按钮是 ``<div>``，
    SoM 漏标 + ``press_key Enter`` 不触发 → MAX_STEPS。

    流程：
      1. 调用 ``chat_send_locator.find_send_button(page)`` 在页面里启发式
         定位发送按钮（先 CSS 选择器级联，再 textbox-anchored 邻近度）。
      2. 找到则用 ``page.mouse.click(x, y)`` 在视口坐标点击（带 actionability）。
      3. 找不到则抛 ``ActionExecutionError``，附带"换 click_point 或 ask_human"
         的明确建议，让 VLM 自决下一步。

    ``type_value`` 接受可选 JSON ``{"wait_after_ms": 1500}`` 调节点击后等待。
    ``target_id`` 不需要（chat_submit 自带 locator）。
    """

    _DEFAULT_WAIT_AFTER_MS: ClassVar[int] = 1200

    @staticmethod
    def _parse_options(type_value: str) -> dict[str, Any]:
        tv = (type_value or "").strip()
        if not tv:
            return {}
        if tv.startswith("{") and tv.endswith("}"):
            try:
                obj = json.loads(tv)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        return {}

    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        action = ctx.action

        opts = self._parse_options(action.type_value or "")
        try:
            wait_after_ms = int(opts.get("wait_after_ms") or self._DEFAULT_WAIT_AFTER_MS)
        except (TypeError, ValueError):
            wait_after_ms = self._DEFAULT_WAIT_AFTER_MS

        # ── 1. 启发式定位发送按钮 ────────────────────────────────
        loc = await _chat_find_send_button(page)
        if not loc.get("found"):
            reason = str(loc.get("reason") or "no candidate matched")
            logger.warning("[chat_submit] locator failed: %s", reason)
            raise ActionExecutionError(
                f"chat_submit 未找到发送按钮：{reason}。\n"
                "建议下一步：\n"
                "  - 如果你能在截图上看到发送按钮（飞机/箭头图标），"
                "    用 click_point 配合归一化坐标点它；\n"
                "  - 或者输出 ask_human 让用户手动发送一次。"
            )

        method = str(loc.get("method") or "?")
        x, y = int(loc.get("x") or 0), int(loc.get("y") or 0)
        bbox = list(loc.get("bbox") or [0, 0, 0, 0])
        selector = loc.get("selector")

        # ── 2. 点击 ──────────────────────────────────────────────
        try:
            await page.mouse.move(x, y)
            await page.mouse.click(x, y)
            logger.info(
                "[chat_submit] clicked send button at (%d,%d) bbox=%s method=%s",
                x, y, bbox, method,
            )
            print(
                f"\033[1;35m🚀 [CHAT SUBMIT]\033[0m method={method} "
                f"click=({x},{y}) bbox={bbox} selector={selector}"
            )
        except Exception as e:
            logger.warning("[chat_submit] mouse.click failed: %s", e)
            raise ActionExecutionError(
                f"chat_submit 找到了按钮坐标 ({x},{y}) 但点击失败: {e}。"
                "考虑改用 click_point 或检查页面是否被遮挡。"
            ) from e

        # ── 3. 等待一段时间让 chat UI 开始响应 ──────────────────────
        try:
            await asyncio.sleep(max(0.0, wait_after_ms / 1000.0))
            await browser._wait_for_page_stable()
        except Exception as _w_err:
            logger.debug("[chat_submit] post-click wait skipped: %s", _w_err)

        # ── 4. RPA trail ────────────────────────────────────────
        try:
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "chat_submit",
                    "type_value": json.dumps(
                        {"wait_after_ms": wait_after_ms},
                        ensure_ascii=False,
                    ),
                    "click_point_x": x,
                    "click_point_y": y,
                    "method": method,
                    "selector": selector,
                })
            )
        except Exception as _trail_err:
            logger.debug("[chat_submit] rpa_trail append failed: %s", _trail_err)

        return None


# ── Out-of-module capability handlers ────────────────────────────────────────
# New deterministic capabilities ship as their own modules (workflow rule §三:
# actions.py is oversized — do not add more handler bodies here). Importing the
# module triggers its @ActionRegistry.register decorator so the agent loop can
# dispatch the new action by name.
try:
    from . import page_to_markdown_action as _page_to_markdown_action  # noqa: F401
except ImportError:  # pragma: no cover - flat-layout fallback, mirrors top imports
    import page_to_markdown_action as _page_to_markdown_action  # type: ignore  # noqa: F401
try:
    from . import resume_run_action as _resume_run_action  # noqa: F401
except ImportError:  # pragma: no cover - flat-layout fallback, mirrors top imports
    import resume_run_action as _resume_run_action  # type: ignore  # noqa: F401

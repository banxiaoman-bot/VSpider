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
    from ..vlm_client import VSpiderAction
    from ..browser_env import ActionExecutionError
    from ..auth_vault import SecretResolutionError, resolve_env_placeholders
    from ..artifact_manager import register_download_artifact
    from ..page_data_controller import DATA_SIGNATURE_JS, pagination_moved
    from ..chat_answer_extractor import clean_chat_answer_text, extract_chat_answer
    from ..chat_send_locator import find_send_button as _chat_find_send_button
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
    from ..browser_env import BrowserEnv

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
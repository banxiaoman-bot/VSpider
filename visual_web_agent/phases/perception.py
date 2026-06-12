"""Perception phase — moved verbatim out of ``main.py`` (slice P1).

Per-turn hybrid perception: SoM screenshot + bot-challenge hook + AX tree
summary + ``BrowserStateSnapshot`` assembly + ``observe`` event. Behavior is a
straight lift-and-delegate from the agent loop; no logic changes in this slice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from ..config import SCREENSHOT_DIR, A11Y_ENHANCER_ENABLED
    from ..browser_state import BrowserStateSnapshot
    from ..a11y_enhancer import (
        A11yEnhancer,
        A11yEnhancerConfig,
        PageMetadata as A11yPageMetadata,
    )
except ImportError:  # script-mode fallback (main.py non-package execution)
    from config import SCREENSHOT_DIR, A11Y_ENHANCER_ENABLED
    from browser_state import BrowserStateSnapshot
    from a11y_enhancer import (
        A11yEnhancer,
        A11yEnhancerConfig,
        PageMetadata as A11yPageMetadata,
    )

logger = logging.getLogger("visual_web_agent.phases.perception")


@dataclass
class PerceptionSnapshot:
    """Everything the agent loop needs back from one perception turn."""

    screenshot_b64: Any
    input_descriptions: str
    ax_tree_text: str
    screenshot_path: str | None
    reasoning_text_source: str | None
    browser_state: BrowserStateSnapshot
    tabs_state: str
    page_summary: str


# E1 逃生阀：连续复用达到该回合数后强制全量感知一次，防 DOM 签名碰撞死视。
_REUSE_STREAK_LIMIT = 3


def _action_mutated_page(result: Any) -> bool:
    """上一动作是否声明改变了 URL / DOM（ActionResult 或 dict 形态均接受）。"""
    if result is None:
        return False
    if isinstance(result, dict):
        return bool(result.get("changed_url") or result.get("changed_dom"))
    return bool(getattr(result, "changed_url", False) or getattr(result, "changed_dom", False))


class PerceptionPhase:
    """每回合「截图 + SoM + AX 摘要 + browser_state 组装」感知段（自 main.py 平移）。

    E1 感知复用：实例跨回合持有 ``last_signature`` / 上一轮快照 / 复用计数，
    DOM 签名未变且上一动作未声明页面变化时跳过截图与 SoM 重注入。
    """

    def __init__(self) -> None:
        self._last_signature: str | None = None
        self._last_snapshot: PerceptionSnapshot | None = None
        self._reuse_streak: int = 0

    async def _probe_signature(self, browser: Any) -> str:
        probe = getattr(browser, "dom_signature", None)
        if not callable(probe):
            return ""
        try:
            return str(await probe() or "")
        except Exception as probe_err:
            logger.debug("[PERCEPTION REUSE] signature probe failed: %s", probe_err)
            return ""

    def _can_reuse(self, browser: Any, signature: str) -> bool:
        if not signature or self._last_snapshot is None:
            return False
        if signature != self._last_signature:
            return False
        if self._reuse_streak >= _REUSE_STREAK_LIMIT:
            return False  # 逃生阀：强制全量感知一次
        if _action_mutated_page(getattr(browser, "_last_action_result", None)):
            return False
        return True

    def _build_reused_snapshot(self, *, step: int) -> PerceptionSnapshot:
        last = self._last_snapshot
        assert last is not None
        state_meta = dict(getattr(last.browser_state, "metadata", {}) or {})
        state_meta["perception_reused"] = True
        reused_state = replace(last.browser_state, step=step, metadata=state_meta)
        return replace(last, browser_state=reused_state)

    async def run(
        self,
        browser: Any,
        *,
        step: int,
        event_stream: Any,
        start_url: str,
        recover_active_page: Callable[[str], Awaitable[Any]],
        wait_for_human_resume: Callable[..., Awaitable[None]],
        bot_challenge_state: Any,
    ) -> PerceptionSnapshot:
        # ── E1 感知复用：签名未变 + 上一动作未改页 → 跳过截图/SoM/AX，复用上一轮 ──
        _signature = await self._probe_signature(browser)
        if self._can_reuse(browser, _signature):
            self._reuse_streak += 1
            reused = self._build_reused_snapshot(step=step)
            logger.info(
                "[PERCEPTION REUSE] dom signature unchanged (streak %d/%d) — skipping screenshot + SoM",
                self._reuse_streak,
                _REUSE_STREAK_LIMIT,
            )
            event_stream.observe(
                step=step,
                url=getattr(browser, "current_url", "") or "",
                screenshot_path=reused.screenshot_path or "",
                ax_lines=int(getattr(reused.browser_state, "ax_line_count", 0) or 0),
                browser_state=reused.browser_state,
                perception_reused=True,
                metadata={
                    "tabs": reused.tabs_state,
                    "reasoning_text_source": reused.reasoning_text_source,
                },
            )
            return reused

        # ════════════════════════════════════════════════════════════
        # 图文双模态融合 (Hybrid Modality)
        # 每一轮都同时采集 SoM 截图 + AX Tree 语义树（含 DOM ID 映射段），融合发送给 VLM。
        # 彻底废除"智能路由/纯文本降级"的单模态切换 —— 视觉与文本互为冗余，
        # VLM 得以用红框数字定位 + AX 语义 / DOM ID 映射校验的方式做综合决策。
        # ════════════════════════════════════════════════════════════
        _tabs_state = await browser.get_tabs_state()
        _tabs_hint = f"\n\n【当前标签页列表】{_tabs_state}" if _tabs_state else ""
        _page_state = await browser.get_active_page_summary()
        _page_hint = f"\n\n【当前页面摘要】{_page_state}" if _page_state else ""

        print("\033[1;36m🧠 [HYBRID]\033[0m 同步采集 SoM 截图 + 无障碍语义树 (AX Tree)，融合决策")
        logger.info("[HYBRID] Collecting SoM screenshot + accessibility tree for fused VLM decision")

        await recover_active_page("before hybrid screenshot")
        try:
            screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step)
        except RuntimeError as screenshot_err:
            if "No active page" not in str(screenshot_err):
                raise
            logger.warning(
                "[BROWSER RECOVERY] screenshot failed with no active page; restarting and retrying once"
            )
            await browser.restart(start_url, reason="retry hybrid screenshot")
            screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step)
        _log_screenshot_path = (
            str(Path(SCREENSHOT_DIR) / f"step_{step:02d}.png") if screenshot_b64 else None
        )

        try:
            try:
                from ..bot_challenge_guard import handle_bot_challenge_step
            except ImportError:
                from bot_challenge_guard import handle_bot_challenge_step
            _bc_result = await handle_bot_challenge_step(
                browser,
                bot_challenge_state,
                hitl_callback=wait_for_human_resume,
            )
            if _bc_result.notice:
                input_descriptions = _bc_result.notice + "\n" + (input_descriptions or "")
            if _bc_result.cleared_after_hitl:
                screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step)
                _log_screenshot_path = (
                    str(Path(SCREENSHOT_DIR) / f"step_{step:02d}.png") if screenshot_b64 else None
                )
                try:
                    from ..auth_harvester import harvest_storage_state as _bc_harvest
                except ImportError:
                    from auth_harvester import harvest_storage_state as _bc_harvest
                try:
                    _bc_hr = await _bc_harvest(browser)
                    if _bc_hr.saved:
                        logger.info("[BOT CHALLENGE] harvested auth profile: %s", _bc_hr.profile_name)
                except Exception as _bc_harv_err:
                    logger.debug("[BOT CHALLENGE] auth harvest skipped: %s", _bc_harv_err)
            # PROXY-4b: when the block persists / the IP looks flagged, swap
            # to the next proxy and reload (policy + budget live downstream).
            try:
                _rerouted = await browser.reroute_proxy_on_block(
                    getattr(browser, "current_url", "") or "",
                    result=_bc_result,
                    state=bot_challenge_state,
                    reason=f"bot challenge {_bc_result.vendor or 'block'}",
                )
            except Exception as _reroute_err:
                _rerouted = False
                logger.debug("[BOT CHALLENGE] proxy reroute skipped: %s", _reroute_err)
            if _rerouted:
                screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step)
                input_descriptions = (
                    "\n🛡️【Bot Challenge · 已切换代理并重载页面】\n"
                    + (input_descriptions or "")
                )
        except Exception as _bc_err:
            logger.debug("[BOT CHALLENGE] step hook skipped: %s", _bc_err)

        try:
            ax_tree_text = await browser.extract_accessibility_tree()
        except Exception as _ax_err:
            logger.warning(
                f"[HYBRID] AX Tree 提取失败，本轮仅凭截图决策: {_ax_err}"
            )
            ax_tree_text = ""

        _log_reasoning_text_source = "AX_TREE" if ax_tree_text else "SCREENSHOT_ONLY"

        # ── A11y Enhancer：增强 AX Tree 信息 ─────────────────────
        if ax_tree_text and A11Y_ENHANCER_ENABLED:
            try:
                _a11y_enhancer = A11yEnhancer(config=A11yEnhancerConfig(
                    enable_grouping=True,
                    enable_state_annotation=True,
                    enable_hidden_hints=True,
                    max_output_chars=15000,
                ))
                _a11y_meta = A11yPageMetadata(
                    url=getattr(browser, "current_url", "") or "",
                    title=(_page_state or "")[:100],
                    total_elements=len(getattr(browser, "_last_som_elements", []) or []),
                    visible_elements=len(getattr(browser, "_last_visible_elements", []) or []),
                    iframe_count=getattr(browser, "_iframe_count", 0),
                    scroll_position=getattr(browser, "_last_scroll_y", 0),
                    page_height=getattr(browser, "_page_height", 0),
                )
                _a11y_result = _a11y_enhancer.enhance(ax_tree_text, _a11y_meta)
                ax_tree_text = _a11y_result.text
                if _a11y_result.summary:
                    logger.debug(f"[A11Y] {_a11y_result.summary}")
            except Exception as _a11y_err:
                logger.debug(f"[A11Y] Enhancement skipped: {_a11y_err}")

        # 兜底：防止超大 AX Tree 撑爆 Token
        if ax_tree_text and len(ax_tree_text) > 15000:
            _orig_len = len(ax_tree_text)
            ax_tree_text = ax_tree_text[:15000] + "\n...[WARNING: AX Tree 过长已截断]..."
            logger.warning(
                f"[HYBRID] AX Tree 长度 {_orig_len} 超过 15000 阈值，已截断以保护上下文窗口"
            )
        _browser_state = await BrowserStateSnapshot.from_browser(
            browser,
            step=step,
            screenshot_path=_log_screenshot_path or "",
            ax_tree_text=ax_tree_text,
            tabs=_tabs_state,
            page_summary=_page_state,
            last_action_result=getattr(browser, "_last_action_result", None),
            metadata={
                "reasoning_text_source": _log_reasoning_text_source,
            },
        )
        event_stream.observe(
            step=step,
            url=getattr(browser, "current_url", "") or "",
            screenshot_path=_log_screenshot_path or "",
            ax_lines=len(ax_tree_text.splitlines()) if ax_tree_text else 0,
            browser_state=_browser_state,
            perception_reused=False,
            metadata={
                "tabs": _tabs_state,
                "reasoning_text_source": _log_reasoning_text_source,
            },
        )

        ax_block = ""
        if ax_tree_text:
            ax_block = (
                "\n\n=====================================\n"
                "【辅助信息：页面无障碍语义树 (AX Tree)】\n"
                "以下是当前页面的纯语义结构，过滤了所有样式噪音。\n"
                "  · 第一段 [可交互元素 @eN 语义快照] 是按阅读顺序编号的紧凑清单：\n"
                "    `@eN [role] \"name\" {states}`，N 与截图红框数字一一对应。\n"
                "    需要操作某元素时，target_id 直接填数字（如 @e5 → target_id=5）。\n"
                "  · 第二段 [页面语义快照] 提供整体 AX 结构（含标题、文本等），辅助理解上下文。\n"
                "  ⚠ 执行 extract 时，**必须从 AX Tree 中读取文本数据**（标题、数值、描述），\n"
                "    而非仅靠截图 OCR。被浮层遮挡的元素在 AX Tree 中仍然存在。\n"
                f"{ax_tree_text}\n"
                "====================================="
            )

        input_descriptions = (
            (input_descriptions or "") + ax_block + _page_hint + _tabs_hint
        )

        snapshot = PerceptionSnapshot(
            screenshot_b64=screenshot_b64,
            input_descriptions=input_descriptions,
            ax_tree_text=ax_tree_text,
            screenshot_path=_log_screenshot_path,
            reasoning_text_source=_log_reasoning_text_source,
            browser_state=_browser_state,
            tabs_state=_tabs_state,
            page_summary=_page_state,
        )
        # E1: 全量感知后刷新复用状态（签名在感知前采样，逃生阀计数归零）
        self._last_signature = _signature or None
        self._last_snapshot = snapshot
        self._reuse_streak = 0
        return snapshot

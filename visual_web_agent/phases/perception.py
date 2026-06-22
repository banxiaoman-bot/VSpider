"""Perception phase — moved verbatim out of ``main.py`` (slice P1).

Per-turn hybrid perception: SoM screenshot + bot-challenge hook + AX tree
summary + ``BrowserStateSnapshot`` assembly + ``observe`` event. Behavior is a
straight lift-and-delegate from the agent loop; no logic changes in this slice.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from ..config import SCREENSHOT_DIR, A11Y_ENHANCER_ENABLED, CONSENT_GUARD_ENABLED
    from ..browser_state import BrowserStateSnapshot
    from ..consent_guard import auto_dismiss_consent, _norm_url
    from ..a11y_enhancer import (
        A11yEnhancer,
        A11yEnhancerConfig,
        PageMetadata as A11yPageMetadata,
    )
except ImportError:  # script-mode fallback (main.py non-package execution)
    from config import SCREENSHOT_DIR, A11Y_ENHANCER_ENABLED, CONSENT_GUARD_ENABLED
    from browser_state import BrowserStateSnapshot
    from consent_guard import auto_dismiss_consent, _norm_url
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

# E2：滚动 / 翻页动作名（视口已变，需重新感知并用 full 选区）。
_SCROLL_ACTION_RE = re.compile(
    r"scroll|翻页|next_page|prev_page|paginate|page_down|page_up|load_more",
    re.IGNORECASE,
)


def _action_mutated_page(result: Any) -> bool:
    """上一动作是否声明改变了 URL / DOM（ActionResult 或 dict 形态均接受）。"""
    if result is None:
        return False
    if isinstance(result, dict):
        return bool(result.get("changed_url") or result.get("changed_dom"))
    return bool(getattr(result, "changed_url", False) or getattr(result, "changed_dom", False))


def _action_name(result: Any) -> str:
    """从 ActionResult / dict 取动作名（缺失返回空串）。"""
    if result is None:
        return ""
    if isinstance(result, dict):
        return str(result.get("action") or "")
    return str(getattr(result, "action", "") or "")


def _action_was_scroll_or_paging(result: Any) -> bool:
    """上一动作是否为滚动 / 翻页（DOM 签名常不变，但视口内容已变）。"""
    name = _action_name(result)
    return bool(name and _SCROLL_ACTION_RE.search(name))


class PerceptionPhase:
    """每回合「截图 + SoM + AX 摘要 + browser_state 组装」感知段（自 main.py 平移）。

    E1 感知复用：实例跨回合持有 ``last_signature`` / 上一轮快照 / 复用计数，
    DOM 签名未变且上一动作未声明页面变化时跳过截图与 SoM 重注入。
    """

    def __init__(self) -> None:
        self._last_signature: str | None = None
        self._last_snapshot: PerceptionSnapshot | None = None
        self._reuse_streak: int = 0
        self._last_ax_lines: set[str] | None = None  # E3 AX 增量 diff
        self._consent_handled_urls: set[str] = set()  # DC-2 自动同意墙守卫去重

    async def _maybe_dismiss_consent(
        self, browser: Any, recover_active_page: Callable[[str], Awaitable[Any]]
    ) -> None:
        """DC-2 守卫：每个新 URL 进入感知前自动关一次 cookie/同意墙（幂等、去重）。

        放在 perception 入口最前：遇墙先点掉「接受全部」再截图/SoM/AX，等价
        类人「进页先关弹窗再看」。按 URL 去重，旧页零开销；任何异常都吞掉，
        绝不让守卫破坏核心感知循环（可用 VSPIDER_CONSENT_GUARD_ENABLED=0 关闭）。
        """
        if not CONSENT_GUARD_ENABLED:
            return
        try:
            # 廉价预检：当前 URL 已守卫过则直接跳过，省去取 page 的往返。
            current_url = getattr(browser, "current_url", "") or ""
            if current_url and _norm_url(current_url) in self._consent_handled_urls:
                return
            page = await recover_active_page("before consent guard")
            await auto_dismiss_consent(
                browser, page, handled_urls=self._consent_handled_urls
            )
        except Exception as guard_err:
            logger.debug("[PERCEPTION] consent guard skipped: %s", guard_err)

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
        last_action = getattr(browser, "_last_action_result", None)
        if _action_mutated_page(last_action):
            return False
        # E2：滚动 / 翻页后 DOM 签名常不变，但视口已移动——上一轮快照已陈旧，
        # 必须重新感知（否则复用旧视口截图/SoM，看不到新内容）。
        if _action_was_scroll_or_paging(last_action):
            return False
        return True

    def _decide_scope(self, browser: Any, *, escape_valve: bool) -> str:
        """E2：决定本轮 SoM 选区。默认 ``viewport``；以下情形回退 ``full``——
        首回合、E1 逃生阀回合、上一动作为滚动 / 翻页、VLM 上一轮显式请求全页。"""
        if self._last_snapshot is None:  # 首回合：尚无任何快照，给全页
            return "full"
        if escape_valve:  # E1 逃生阀：强制全量回合
            return "full"
        if getattr(browser, "_request_full_som", False):  # VLM 显式请求（一次性）
            try:
                browser._request_full_som = False
            except Exception:
                pass
            return "full"
        if _action_was_scroll_or_paging(getattr(browser, "_last_action_result", None)):
            return "full"
        return "viewport"

    def _compute_ax_diff(self, current_text: str, *, force_full: bool) -> tuple[str, bool]:
        """E3: AX 增量 diff — (display_text, is_diff).

        First round (``_last_ax_lines is None``) or *force_full* (E1 escape
        valve) returns the full text unchanged.  Otherwise returns a compact
        diff containing only added / removed lines plus an unchanged count.
        """
        if not current_text:
            return current_text, False

        current_lines = {line for line in current_text.splitlines() if line.strip()}

        if force_full or self._last_ax_lines is None:
            self._last_ax_lines = current_lines
            return current_text, False

        added = sorted(current_lines - self._last_ax_lines)
        removed = sorted(self._last_ax_lines - current_lines)
        unchanged_count = len(current_lines) - len(added)

        self._last_ax_lines = current_lines

        if not added and not removed:
            return f"[AX 无变化] 与上轮完全一致（{unchanged_count} 行）", True

        parts = [f"[AX 增量] {unchanged_count} 行不变"]
        if added:
            parts.append(f"+{len(added)} 新增")
        if removed:
            parts.append(f"-{len(removed)} 已消失")
        header = " | ".join(parts)

        body: list[str] = [header]
        if added:
            body.append("【新增】")
            body.extend(added)
        if removed:
            body.append("【已消失】")
            body.extend(removed)

        return "\n".join(body), True

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
        # ── DC-2 自动同意墙守卫：进页先点掉 cookie/consent「接受全部」再感知（按 URL 去重）──
        await self._maybe_dismiss_consent(browser, recover_active_page)

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

        # ── E2：本轮全量感知，决定 SoM 选区（默认 viewport，必要时回退 full）──
        _escape_valve = bool(
            self._last_snapshot is not None
            and _signature
            and _signature == self._last_signature
            and self._reuse_streak >= _REUSE_STREAK_LIMIT
        )
        _scope = self._decide_scope(browser, escape_valve=_escape_valve)

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
            screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step, scope=_scope)
        except RuntimeError as screenshot_err:
            if "No active page" not in str(screenshot_err):
                raise
            logger.warning(
                "[BROWSER RECOVERY] screenshot failed with no active page; restarting and retrying once"
            )
            await browser.restart(start_url, reason="retry hybrid screenshot")
            screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step, scope=_scope)
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
                screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step, scope=_scope)
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
                screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step, scope=_scope)
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

        # E3: AX 增量 diff — 非首回合且非逃生阀回合时只输出变化行
        ax_tree_text, _ax_is_diff = self._compute_ax_diff(
            ax_tree_text, force_full=_escape_valve,
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
            if _ax_is_diff:
                ax_block = (
                    "\n\n=====================================\n"
                    "【辅助信息：AX Tree 增量变化】\n"
                    "以下是与上一轮相比的变化部分，未变内容沿用上轮。\n"
                    f"{ax_tree_text}\n"
                    "====================================="
                )
            else:
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

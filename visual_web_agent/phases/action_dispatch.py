"""Action dispatch phase — extracted from ``main.py`` (slice G4).

Houses ``PostDecisionGuards`` which manages the post-VLM-decision guard
pipeline: repeat-action detection with two-stage escalation (soft warn →
hard hijack), and zero-target-downgrade loop guard.

Behavior is a straight lift-and-delegate from run_agent; no logic changes.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("visual_web_agent.phases.action_dispatch")

_SUBGOAL_ADVANCE_MARKERS = (
    "应推进", "应进入下一", "应将 subgoal_status 设为 completed",
    "应将subgoal_status设为completed",
    "subgoal_status 设为 completed", "subgoal_status=completed",
    "应标记 completed", "应标记completed",
    "已完成，应", "已达成，应", "已成功展开",
    "进入下一步", "进入下一子目标", "推进至下一",
    "should advance", "advance to next", "next subgoal",
)

_TRANSITIONAL_ACTIONS = {"scroll", "smooth_scroll", "find_text", "wait", "press_key"}


@dataclass
class GuardResult:
    """Outcome of applying post-decision guards."""
    decisions: list[dict]
    repeat_warned: bool = False
    repeat_hijacked: bool = False
    zero_target_ultimatum: bool = False


class PostDecisionGuards:
    """Post-VLM-decision guard state machine for a single run.

    Lifted from run_agent lines 12832-13076 (repeat-action / subgoal
    auto-advance / zero-target-downgrade).
    """

    def __init__(self) -> None:
        self.prev_action_sig: tuple[str, int, str] = ("", 0, "")
        self.repeat_action_count: int = 0
        self.consecutive_zero_target: int = 0

    def apply_repeat_guard(
        self,
        decisions: list[dict],
        *,
        vlm: Any,
    ) -> GuardResult:
        """Two-stage repeat-action guard + subgoal auto-advance.

        Stage 1 (2nd repeat): soft warning injected, action unchanged.
        Stage 2 (≥3rd repeat + advance marker in thought): hard hijack →
        rewrite to wait(1s) + subgoal_status=completed.

        Transitional actions (scroll/wait/press_key) are never intercepted.
        """
        result = GuardResult(decisions=decisions)
        if not decisions or self.prev_action_sig == ("", 0, ""):
            return result

        hd = decisions[0]
        cur_sig = (
            str(hd.get("action", "")),
            int(hd.get("target_id", 0) or 0),
            str(hd.get("type_value", "") or ""),
        )
        is_literal_repeat = (cur_sig == self.prev_action_sig)

        if is_literal_repeat:
            self.repeat_action_count += 1
        else:
            self.repeat_action_count = 1

        thought = str(hd.get("thought", "") or "")
        has_advance_marker = any(m in thought for m in _SUBGOAL_ADVANCE_MARKERS)
        already_completed = hd.get("subgoal_status") == "completed"
        is_transitional = cur_sig[0] in _TRANSITIONAL_ACTIONS

        if (
            is_literal_repeat
            and self.repeat_action_count == 2
            and not is_transitional
            and not already_completed
        ):
            logger.info(
                "[REPEAT WARN] 第 2 次重复 %s %r，注入软警告但不改写动作",
                cur_sig[0], cur_sig[2],
            )
            vlm.inject_error_feedback(
                f"⚠️ 系统警告：你刚连续 2 次输出完全相同的操作 "
                f"{cur_sig[0]} target_id={cur_sig[1]} type_value={cur_sig[2]!r}，"
                f"目标似乎并未推进。\n"
                f"请重新审视当前页面状态：\n"
                f"  · 上一步是否真的成功？（看 AX Tree 元素 value/state 有无变化）\n"
                f"  · 如果真已完成，把 subgoal_status 设为 \"completed\" 并给出**真正的下一步动作**\n"
                f"  · 如果未完成，换 click_text、不同 target_id、或滚动让目标重新就位\n"
                f"⛔ 不要再机械重复同一动作。"
            )
            result.repeat_warned = True

        if (
            is_literal_repeat
            and self.repeat_action_count >= 3
            and not is_transitional
            and has_advance_marker
            and not already_completed
        ):
            logger.info(
                "[SUBGOAL AUTO-ADVANCE] thought 含推进信号但 action 重复 "
                "(%s %s %r)，强制 subgoal_status=completed + 改 wait",
                cur_sig[0], cur_sig[1], cur_sig[2],
            )
            decisions[0]["subgoal_status"] = "completed"
            decisions[0]["action"] = "wait"
            decisions[0]["target_id"] = 0
            decisions[0]["type_value"] = "1"
            decisions[0]["thought"] = (
                "[SUBGOAL AUTO-ADVANCE 引擎改写] thought 写「已完成应推进」"
                f"但原 action={cur_sig[0]} 与上一步完全相同"
                f"（已重复 {self.repeat_action_count} 次），"
                "系统强制推进子目标，本步 wait(1s) 让 VLM 下轮按新子目标决策。\n"
                + thought
            )
            self.repeat_action_count = 0
            result.repeat_hijacked = True

        result.decisions = decisions
        return result

    def apply_zero_target_guard(
        self,
        decisions: list[dict],
        *,
        vlm: Any,
    ) -> GuardResult:
        """Detect consecutive ZERO_TARGET_DOWNGRADE schema misalignment.

        When VLM repeatedly emits click/type with target_id=0 + non-empty
        type_value (validator downgrades to wait), this guard counts to 3
        and then injects an ultimatum feedback.
        """
        result = GuardResult(decisions=decisions)
        head_dec = decisions[0] if decisions else {}
        if head_dec.get("__zero_target_downgraded"):
            self.consecutive_zero_target += 1
            if self.consecutive_zero_target >= 3:
                thought = head_dec.get("thought") or ""
                en_match = re.search(r"@e(\d+)", thought)
                hint_id = en_match.group(1) if en_match else "<你 thought 中提到的 @eN 数字>"
                logger.warning(
                    "[RAW GUARD] 连续 %d 次 ZERO_TARGET_DOWNGRADE，"
                    "VLM schema 错位 — 强制升级反馈（推断目标 @e%s）",
                    self.consecutive_zero_target, hint_id,
                )
                vlm.inject_error_feedback(
                    f"🆘【最后通牒 — 你已连续 {self.consecutive_zero_target} 步 schema 错位】\n"
                    f"你反复输出 click/type target_id=0，但 thought 明明写了真实 @eN（如 @e{hint_id}）。\n"
                    f"问题：你把 @eN 的数字部分填错位置了 —— 应填到 target_id 字段，不是 type_value。\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"【请按下面三选一立即输出，否则任务终止】：\n"
                    f"  A. 直接 click：{{\"action\":\"click\",\"target_id\":{hint_id},"
                    f"\"type_value\":\"\",\"memory_key\":\"\",...}}\n"
                    f"  B. 用 click_text 文本定位（绕开 ID 填位）：\n"
                    f"     {{\"action\":\"click_text\",\"target_id\":0,"
                    f"\"type_value\":\"<按钮可见文字，如 后页 或 2>\",...}}\n"
                    f"  C. 如果任务无法完成，输出 action=done 并在 thought 说明放弃理由。\n"
                    f"⛔ 严禁再输出 target_id=0 + 非空 type_value 的组合。"
                )
                self.consecutive_zero_target = 0
                result.zero_target_ultimatum = True
        else:
            self.consecutive_zero_target = 0

        result.decisions = decisions
        return result

    def record_action_sig(self, decisions: list[dict]) -> None:
        """Record the final action signature for next-step repeat detection.

        Called after all guards have been applied and before execution.
        Lifted from run_agent line 14752.
        """
        if decisions:
            hd = decisions[0]
            self.prev_action_sig = (
                str(hd.get("action", "")),
                int(hd.get("target_id", 0) or 0),
                str(hd.get("type_value", "") or ""),
            )

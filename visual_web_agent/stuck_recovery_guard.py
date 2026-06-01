"""AGENT_STUCK recovery: clear VLM history and force fresh screenshot reasoning."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ELEMENT_NOT_FOUND_RE = re.compile(
    r"Element\s+#(\d+)\s+not found",
    re.I,
)


@dataclass
class StuckRecoveryState:
    missing_target_id: int | None = None
    missing_target_streak: int = 0
    recovery_count: int = 0
    last_loop_type: str = ""
    loop_nudge_streak: int = 0


@dataclass
class StuckRecoveryDecision:
    should_reset: bool = False
    reason: str = ""


def parse_missing_target_id(error_msg: str) -> int | None:
    m = _ELEMENT_NOT_FOUND_RE.search(error_msg or "")
    if not m:
        return None
    try:
        tid = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return tid if tid > 0 else None


def evaluate_failure_recovery(
    state: StuckRecoveryState,
    *,
    error_msg: str,
    target_id: int,
    consecutive_errors: int,
) -> StuckRecoveryDecision:
    """Detect repeated clicks on a missing @eN — classic AGENT_STUCK pattern."""
    missing = parse_missing_target_id(error_msg)
    if missing is not None:
        if missing == state.missing_target_id:
            state.missing_target_streak += 1
        else:
            state.missing_target_id = missing
            state.missing_target_streak = 1
    else:
        state.missing_target_id = None
        state.missing_target_streak = 0

    if state.missing_target_streak >= 2:
        state.recovery_count += 1
        state.missing_target_streak = 0
        state.missing_target_id = None
        return StuckRecoveryDecision(
            should_reset=True,
            reason=(
                f"同一缺失元素 #{missing} 连续失败 ≥2 次；"
                "禁止复用旧 @eN，必须重看当前截图"
            ),
        )

    if consecutive_errors >= 2 and missing is not None:
        state.recovery_count += 1
        state.missing_target_id = None
        state.missing_target_streak = 0
        return StuckRecoveryDecision(
            should_reset=True,
            reason=(
                f"连续 {consecutive_errors} 次动作失败且最新为 Element #{missing} not found"
            ),
        )

    return StuckRecoveryDecision()


def evaluate_loop_recovery(
    state: StuckRecoveryState,
    *,
    loop_type: str,
    nudge_number: int,
) -> StuckRecoveryDecision:
    """After loop_detector nudges twice without progress, hard-reset context."""
    if not loop_type:
        state.loop_nudge_streak = 0
        state.last_loop_type = ""
        return StuckRecoveryDecision()

    if loop_type == state.last_loop_type:
        state.loop_nudge_streak += 1
    else:
        state.last_loop_type = loop_type
        state.loop_nudge_streak = 1

    if state.loop_nudge_streak >= 2 or nudge_number >= 2:
        state.recovery_count += 1
        state.loop_nudge_streak = 0
        state.last_loop_type = ""
        return StuckRecoveryDecision(
            should_reset=True,
            reason=f"loop_detector 重复告警 ({loop_type})，需换策略",
        )
    return StuckRecoveryDecision()


def build_recovery_feedback(reason: str) -> str:
    base = (
        "🔄 [STUCK RECOVERY] 系统已清空你的决策历史。\n"
        "请**只依据当前截图 + AX Tree + 当前 URL**重新规划；"
        "禁止复述旧 thought、禁止复用上一屏 @eN 编号。\n"
        "若当前策略走不通，必须换动作类型或换目标元素。"
    )
    if reason:
        return f"{base}\n触发原因：{reason}"
    return base

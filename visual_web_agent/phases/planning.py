"""Planning phase — extracted from ``main.py`` (slice G2).

Houses ``PlanningPhase`` which manages the Wave 2 Planner / Reflector
lifecycle: state variables, initial plan generation, per-step reflection.

Behavior is a straight lift-and-delegate from run_agent; no logic changes.
"""

from __future__ import annotations

import re
import logging
from typing import Any

logger = logging.getLogger("visual_web_agent.phases.planning")

_MAX_REFLECTS = 5
_REFLECT_INTERVAL = 5


class PlanningPhase:
    """Planner / Reflector state machine for a single run."""

    def __init__(self) -> None:
        self.task_plan: Any | None = None
        self.steps_since_reflect: int = 0
        self.reflect_count: int = 0
        self.dedup_tripped_last_step: bool = False
        self.duplicate_zero_extract_streak: int = 0
        self.abort_requested: bool = False

    async def make_initial_plan(
        self,
        *,
        vlm: Any,
        goal: str,
        initial_url: str,
        workflow_memory: dict,
    ) -> Any | None:
        """Generate the initial task plan via VLM.

        Failures are silently swallowed and return None (Wave 1 fallback).
        Lifted from run_agent lines 11669-11697.
        """
        try:
            plan = await vlm.make_plan(
                goal=goal,
                initial_url=initial_url,
                workflow_memory=workflow_memory,
            )
            self.task_plan = plan
            logger.info(
                "[PLANNER] Generated %d sub-goals",
                len(plan.sub_goals) if hasattr(plan, "sub_goals") else 0,
            )
            return plan
        except Exception as err:
            logger.warning("[PLANNER] call failed, silent fallback: %s", err)
            self.task_plan = None
            return None

    async def maybe_reflect(
        self,
        *,
        vlm: Any,
        current_url: str,
        goal: str,
        consecutive_errors: int = 0,
    ) -> Any | None:
        """Run the Reflector if signals warrant it and limits allow.

        Returns the ReflectDecision if one was made, else None.
        Lifted from run_agent lines 12441-12505.
        """
        if self.task_plan is None:
            return None

        signals: list[str] = []
        if consecutive_errors >= 2:
            signals.append(f"连续 {consecutive_errors} 步 action=error")
        if self.dedup_tripped_last_step:
            signals.append("上一步 extract 被 dedup 拦截")
        if self.steps_since_reflect >= _REFLECT_INTERVAL and self.task_plan is not None:
            signals.append(f"{_REFLECT_INTERVAL} 步兜底检查")

        url_lower = (current_url or "").lower()
        if re.search(r"/(login|signin|sign-in|passport|sso|captcha|verify)\b", url_lower):
            goal_has_cred = bool(re.search(
                r"\{\{\s*(phone|password|username|account|mobile|email)\s*\}\}",
                goal, re.IGNORECASE,
            ))
            if not goal_has_cred:
                signals.append(f"当前 URL 疑似登录/验证页：{current_url}")

        if not signals or self.reflect_count >= _MAX_REFLECTS:
            self.steps_since_reflect += 1
            self.dedup_tripped_last_step = False
            return None

        try:
            rd = await vlm.reflect(
                plan=self.task_plan,
                history_summary=vlm._build_history_summary(),
                signals=signals,
                current_url=current_url or "",
            )
            self.reflect_count += 1
            self.steps_since_reflect = 0
            self.dedup_tripped_last_step = False

            if rd.decision == "advance" and rd.advance_to_idx is not None:
                target_idx = max(0, min(rd.advance_to_idx, len(self.task_plan.sub_goals) - 1))
                self.task_plan.sub_goals[self.task_plan.current_idx].status = "done"
                self.task_plan.current_idx = target_idx
                self.task_plan.sub_goals[target_idx].status = "active"
                vlm.inject_error_feedback(
                    f"🎯 [REFLECTOR] {rd.reason}；"
                    f"系统已推进至子目标 {target_idx + 1}/"
                    f"{len(self.task_plan.sub_goals)}："
                    f"{self.task_plan.sub_goals[target_idx].description}"
                )
            elif rd.decision == "revise" and rd.new_sub_goals:
                self.task_plan.sub_goals = rd.new_sub_goals
                self.task_plan.current_idx = 0
                if self.task_plan.sub_goals:
                    self.task_plan.sub_goals[0].status = "active"
                vlm.inject_error_feedback(
                    f"🔧 [REFLECTOR] 计划已修订（{rd.reason}）。"
                    f"新的当前子目标："
                    f"{self.task_plan.sub_goals[0].description if self.task_plan.sub_goals else '(空)'}"
                )
            elif rd.decision == "abort":
                self.abort_requested = True
                vlm.inject_error_feedback(
                    f"🛑 [REFLECTOR] 判定不可完成（{rd.reason}）。"
                    f"请立即输出 action=done 结束任务。"
                )

            return rd
        except Exception as err:
            logger.warning("[REFLECTOR] call failed, ignored: %s", err)
            self.steps_since_reflect += 1
            self.dedup_tripped_last_step = False
            return None

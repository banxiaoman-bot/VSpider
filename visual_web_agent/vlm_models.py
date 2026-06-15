"""
VSpider VLM 结构化模型定义（从 vlm_client.py 拆出）

包含任务规划与反思审计相关的 Pydantic 模型：
- SubGoal / TaskPlan：任务分解与进度追踪
- ReflectorDecision：Reflector 审计决策
"""

from typing import Literal, List, Optional

from pydantic import BaseModel, Field, model_validator


class SubGoal(BaseModel):
    """单个可独立验证的子目标。"""
    id: int = Field(..., description="子目标序号，从 1 开始")
    description: str = Field(..., description="子目标简述（如 '搜索 python playwright'）")
    exit_criteria: str = Field(
        ...,
        description="退出标准，一句话描述如何判断本子目标完成（如 '搜索结果页加载，可见 >=3 条结果'）"
    )
    status: Literal["pending", "active", "done", "failed"] = Field(
        default="pending",
        description="子目标状态。任务起手时首个为 active，其余 pending"
    )


class TaskPlan(BaseModel):
    """任务计划 = goal + 有序子目标列表。"""
    goal: str = Field(..., description="用户原始目标")
    sub_goals: List[SubGoal] = Field(..., description="3-6 个按序执行的子目标")
    current_idx: int = Field(default=0, description="当前活动子目标索引")

    @property
    def current(self) -> Optional[SubGoal]:
        if 0 <= self.current_idx < len(self.sub_goals):
            return self.sub_goals[self.current_idx]
        return None

    def advance(self) -> bool:
        """推进到下一子目标，返回 True；已是最后一个返回 False（仅标 done 不推进）。"""
        if self.current_idx < len(self.sub_goals) - 1:
            self.sub_goals[self.current_idx].status = "done"
            self.current_idx += 1
            self.sub_goals[self.current_idx].status = "active"
            return True
        self.sub_goals[self.current_idx].status = "done"
        return False

    def summary(self) -> str:
        """一行进度概览，用于 prompt 注入。"""
        icons = {"done": "✅", "active": "▶", "pending": "⏳", "failed": "❌"}
        return "  ".join(
            f"{icons.get(s.status, '·')} {s.id}. {s.description[:24]}"
            for s in self.sub_goals
        )


class ReflectorDecision(BaseModel):
    """Reflector 审计后的决策。"""
    decision: Literal["continue", "advance", "revise", "abort"] = Field(
        ...,
        description=(
            "审计结论："
            "continue=计划无误仅暂时遇阻；"
            "advance=当前子目标实际已完成强推进；"
            "revise=计划有误，给出新子目标列表；"
            "abort=不可完成，终结任务"
        ),
    )
    reason: str = Field(..., description="决策理由，简述现状与结论")
    advance_to_idx: Optional[int] = Field(
        default=None,
        description="advance 专用，推进到的目标索引（0-based）"
    )
    new_sub_goals: Optional[List[SubGoal]] = Field(
        default=None,
        description="revise 专用，完全覆盖的新子目标列表"
    )
    abort_verdict: Optional[Literal["success", "fail"]] = Field(
        default=None,
        description="abort 专用，最终裁决"
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_reflector_payload(cls, data):
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if not str(payload.get("reason") or "").strip():
            payload["reason"] = (
                payload.get("thought")
                or payload.get("summary")
                or payload.get("explanation")
                or "Reflector returned no reason."
            )

        status_map = {
            "todo": "pending",
            "not_started": "pending",
            "in_progress": "active",
            "current": "active",
            "completed": "done",
            "complete": "done",
            "success": "done",
            "error": "failed",
        }
        allowed = {"pending", "active", "done", "failed"}
        goals = payload.get("new_sub_goals")
        if isinstance(goals, list):
            normalized = []
            for idx, goal in enumerate(goals, start=1):
                if not isinstance(goal, dict):
                    normalized.append(goal)
                    continue
                item = dict(goal)
                item.setdefault("id", idx)
                if not str(item.get("exit_criteria") or "").strip():
                    item["exit_criteria"] = (
                        item.get("description") or "Complete this sub-goal."
                    )
                status = str(item.get("status") or "pending").strip().lower()
                item["status"] = status_map.get(
                    status,
                    status if status in allowed else "pending",
                )
                normalized.append(item)
            payload["new_sub_goals"] = normalized
        return payload


TASK_PLAN_SCHEMA: dict = TaskPlan.model_json_schema()
REFLECTOR_DECISION_SCHEMA: dict = ReflectorDecision.model_json_schema()

ERROR_DECISION = {
    "thought": "VLM 请求失败或返回格式异常",
    "action": "error",
    "target_id": 0,
    "type_value": "",
    "memory_key": "",
    "status": "error",
}

ERROR_DECISION_LIST: list = [dict(ERROR_DECISION)]

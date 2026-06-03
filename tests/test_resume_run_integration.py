"""End-to-end wiring integrity for RUN-RESUME1 step 3.

The four sibling files test each layer in isolation (consume helpers, the
``resume_run`` handler, the router, the prompt skill). This file proves the
layers *compose* the way ``main.py::run_agent`` wires them, so a future refactor
that quietly breaks the contract is caught here:

* block #1 (after ``begin``): a resume decision -> ``resume_memory_payload`` ->
  ``workflow_memory["__resume_state"]`` -> consumed by the ``resume_run`` action,
  and ``apply_completed_steps_to_plan`` skips the already-done leading sub-goals.
* block #2 (per-turn ``record``): ``plan_completed_step_labels(...) or None`` is
  the ledger a later resume needs; it must round-trip back through
  ``apply_completed_steps_to_plan`` and the ``or None`` must not wipe a resumed
  ledger on turns with no newly-done sub-goal.

``_consume_block`` / ``_record_ledger`` mirror the exact expressions in main.py's
two guarded blocks (the orchestrator itself needs a live browser + VLM to run).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from visual_web_agent.actions import ActionContext, ActionRegistry
from visual_web_agent.capability_router import _backend_plan, _signals
from visual_web_agent.prompts import build_system_prompt
from visual_web_agent.resume_run_action import ResumeRunHandler
from visual_web_agent.run_checkpoint import ResumeDecision
from visual_web_agent.run_resume_consume import (
    apply_completed_steps_to_plan,
    plan_completed_step_labels,
    resume_memory_payload,
)
from visual_web_agent.vlm_client import VSpiderAction


# ── duck-typed stubs mirroring vlm_client.SubGoal / TaskPlan ─────────────────
@dataclass
class _SG:
    id: int
    description: str
    status: str = "pending"


@dataclass
class _Plan:
    sub_goals: list = field(default_factory=list)
    current_idx: int = 0


class _StubBrowser:
    def __init__(self) -> None:
        self.rpa_trail: list = []


def _consume_block(workflow_memory: dict, task_plan: _Plan, decision: ResumeDecision) -> int:
    """Mirror main.py guarded block #1: publish resume state + skip done steps."""

    skipped = 0
    if decision.should_resume:
        workflow_memory["__resume_state"] = resume_memory_payload(decision)
        skipped = apply_completed_steps_to_plan(task_plan, decision.completed_steps)
    return skipped


def _record_ledger(task_plan: _Plan):
    """Mirror main.py guarded block #2: the completed_steps fed into record()."""

    return plan_completed_step_labels(task_plan) or None


class TestWiringComposition:
    def test_resume_decision_skips_then_action_reads_state(self) -> None:
        plan = _Plan(
            sub_goals=[
                _SG(1, "搜索 python", status="active"),
                _SG(2, "打开第一个结果", status="pending"),
                _SG(3, "提取表格数据", status="pending"),
            ],
            current_idx=0,
        )
        decision = ResumeDecision(
            should_resume=True,
            from_turn=6,
            completed_steps=["搜索 python", "打开第一个结果"],
            item_count=12,
            resumed_from={"turn": 6, "status": "failed", "item_count": 12},
            reason="resumed",
        )
        memory: dict = {}

        skipped = _consume_block(memory, plan, decision)
        assert skipped == 2
        assert plan.current_idx == 2
        assert plan.sub_goals[0].status == "done"
        assert plan.sub_goals[2].status == "active"
        assert memory["__resume_state"]["resumed"] is True

        action = VSpiderAction(action="resume_run", target_id=0, type_value="", memory_key="")
        ctx = ActionContext(action=action, browser=_StubBrowser(), workflow_memory=memory, page=None)
        asyncio.run(ResumeRunHandler().execute(ctx))
        status = ctx.workflow_memory["resume_status"]
        assert status["resumed"] is True
        assert status["from_turn"] == 6
        assert status["item_count"] == 12
        assert status["source"] == "workflow_memory"

    def test_record_ledger_roundtrips_through_skip(self) -> None:
        advanced = _Plan(
            sub_goals=[
                _SG(1, "open page", status="done"),
                _SG(2, "search term", status="done"),
                _SG(3, "extract rows", status="active"),
            ],
            current_idx=2,
        )
        ledger = _record_ledger(advanced)
        assert ledger == ["open page", "search term"]

        fresh = _Plan(
            sub_goals=[
                _SG(1, "open page", status="active"),
                _SG(2, "search term", status="pending"),
                _SG(3, "extract rows", status="pending"),
            ],
            current_idx=0,
        )
        skipped = apply_completed_steps_to_plan(fresh, ledger)
        assert skipped == 2
        assert fresh.current_idx == 2

    def test_empty_ledger_is_none_not_empty_list(self) -> None:
        plan = _Plan(sub_goals=[_SG(1, "a", status="active")])
        assert _record_ledger(plan) is None

    def test_fresh_run_is_inert(self) -> None:
        plan = _Plan(
            sub_goals=[_SG(1, "a", status="active"), _SG(2, "b", status="pending")],
            current_idx=0,
        )
        memory: dict = {}
        skipped = _consume_block(memory, plan, ResumeDecision(False, reason="resume_disabled"))
        assert skipped == 0
        assert "__resume_state" not in memory
        assert plan.current_idx == 0
        assert plan.sub_goals[0].status == "active"


class TestEndToEndChain:
    def test_resume_goal_routes_and_prompts_and_registers(self) -> None:
        goal = "断点续跑，接着上次继续抓取数据"

        sig = _signals(goal, {})
        assert sig["resume_preferred"] is True
        plan_steps = [s.get("name") for s in _backend_plan(sig, {}, [])]
        assert "resume_run" in plan_steps

        assert "Skill: Resume Run" in build_system_prompt(goal=goal, browser_state="")

        assert ActionRegistry.is_registered("resume_run")

    def test_unrelated_goal_skips_whole_chain(self) -> None:
        goal = "点击登录按钮并提交表单"

        sig = _signals(goal, {})
        assert sig["resume_preferred"] is False
        plan_steps = [s.get("name") for s in _backend_plan(sig, {}, [])]
        assert "resume_run" not in plan_steps
        assert "Skill: Resume Run" not in build_system_prompt(goal=goal, browser_state="")

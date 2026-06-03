"""Tests for ``visual_web_agent.run_resume_consume`` (RUN-RESUME1 step 3).

The *consume* side of run-resume: turn a :class:`ResumeDecision` into a
workflow_memory payload + a natural-language directive the VLM/planner can act
on, derive the completed-step ledger from a task plan (record side), and
best-effort skip already-done leading sub-goals on a resumed plan.

Pure logic only (no agent, no VLM, no network) using duck-typed stub plans,
mirroring the stub-frame style of the other RUN-RESUME slices.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from visual_web_agent.run_checkpoint import ResumeDecision
from visual_web_agent.run_resume_consume import (
    apply_completed_steps_to_plan,
    build_resume_note,
    plan_completed_step_labels,
    resume_memory_payload,
)


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


def _resumed_decision(**over) -> ResumeDecision:
    base = dict(
        should_resume=True,
        from_turn=6,
        completed_steps=["搜索 python", "打开第一个结果"],
        item_count=12,
        resumed_from={"turn": 6, "status": "failed", "item_count": 12},
        reason="resumed",
    )
    base.update(over)
    return ResumeDecision(**base)


class TestPlanCompletedStepLabels:
    def test_none_plan_is_empty(self) -> None:
        assert plan_completed_step_labels(None) == []

    def test_only_done_subgoals_in_order(self) -> None:
        plan = _Plan(
            sub_goals=[
                _SG(1, "open page", status="done"),
                _SG(2, "search term", status="done"),
                _SG(3, "extract rows", status="active"),
            ],
            current_idx=2,
        )
        assert plan_completed_step_labels(plan) == ["open page", "search term"]

    def test_skips_blank_descriptions(self) -> None:
        plan = _Plan(sub_goals=[_SG(1, "", status="done"), _SG(2, "real", status="done")])
        assert plan_completed_step_labels(plan) == ["real"]

    def test_no_done_subgoals_is_empty(self) -> None:
        plan = _Plan(sub_goals=[_SG(1, "a", status="active"), _SG(2, "b", status="pending")])
        assert plan_completed_step_labels(plan) == []


class TestResumeMemoryPayload:
    def test_resumed_payload_fields(self) -> None:
        payload = resume_memory_payload(_resumed_decision())
        assert payload["resumed"] is True
        assert payload["from_turn"] == 6
        assert payload["item_count"] == 12
        assert payload["completed_steps"] == ["搜索 python", "打开第一个结果"]
        assert payload["prior_status"] == "failed"
        assert isinstance(payload["note"], str) and payload["note"]

    def test_not_resumed_payload(self) -> None:
        payload = resume_memory_payload(ResumeDecision(False, reason="no_checkpoint"))
        assert payload["resumed"] is False
        assert payload["from_turn"] == 0
        assert payload["completed_steps"] == []
        assert payload["note"] == ""

    def test_accepts_plain_dict_decision(self) -> None:
        payload = resume_memory_payload(
            {"should_resume": True, "from_turn": 3, "completed_steps": ["x"], "item_count": 5}
        )
        assert payload["resumed"] is True
        assert payload["from_turn"] == 3
        assert payload["item_count"] == 5
        assert payload["completed_steps"] == ["x"]


class TestBuildResumeNote:
    def test_resumed_note_mentions_progress(self) -> None:
        note = build_resume_note(_resumed_decision(), goal="抓取数据")
        assert "断点续跑" in note
        assert "6" in note  # from_turn
        assert "12" in note  # item_count

    def test_not_resumed_is_empty(self) -> None:
        assert build_resume_note(ResumeDecision(False, reason="resume_disabled")) == ""


class TestApplyCompletedStepsToPlan:
    def test_none_or_empty_is_zero(self) -> None:
        assert apply_completed_steps_to_plan(None, ["a"]) == 0
        assert apply_completed_steps_to_plan(_Plan(sub_goals=[_SG(1, "a")]), []) == 0

    def test_skips_leading_matched_subgoals(self) -> None:
        plan = _Plan(
            sub_goals=[
                _SG(1, "open page", status="active"),
                _SG(2, "search term", status="pending"),
                _SG(3, "extract rows", status="pending"),
            ],
            current_idx=0,
        )
        skipped = apply_completed_steps_to_plan(plan, ["open page", "search term"])
        assert skipped == 2
        assert plan.current_idx == 2
        assert plan.sub_goals[0].status == "done"
        assert plan.sub_goals[1].status == "done"
        assert plan.sub_goals[2].status == "active"

    def test_match_is_normalized(self) -> None:
        plan = _Plan(sub_goals=[_SG(1, "  Open  PAGE ", status="active"), _SG(2, "next", status="pending")])
        skipped = apply_completed_steps_to_plan(plan, ["open page"])
        assert skipped == 1
        assert plan.current_idx == 1

    def test_never_skips_final_subgoal(self) -> None:
        plan = _Plan(
            sub_goals=[_SG(1, "a", status="active"), _SG(2, "b", status="pending")],
            current_idx=0,
        )
        # both match, but the last sub-goal must remain to be executed
        skipped = apply_completed_steps_to_plan(plan, ["a", "b"])
        assert skipped == 1
        assert plan.current_idx == 1
        assert plan.sub_goals[1].status == "active"

    def test_no_leading_match_leaves_plan_untouched(self) -> None:
        plan = _Plan(
            sub_goals=[_SG(1, "unrelated", status="active"), _SG(2, "b", status="pending")],
            current_idx=0,
        )
        skipped = apply_completed_steps_to_plan(plan, ["b"])  # only a later one matches
        assert skipped == 0
        assert plan.current_idx == 0
        assert plan.sub_goals[0].status == "active"

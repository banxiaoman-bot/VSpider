"""G2 planning phase split — TDD tests.

Verify that ``PlanningPhase`` manages Planner/Reflector state and delegates
to VLM for plan generation and reflection.
Pure unit tests; no browser, no VLM, no network.
"""

from __future__ import annotations

import asyncio
import types


def _make_stub_vlm(plan_result=None, reflect_result=None):
    """VLM stub with make_plan and reflect methods."""
    vlm = types.SimpleNamespace()

    async def _make_plan(**kwargs):
        if plan_result is None:
            raise RuntimeError("plan disabled")
        return plan_result

    async def _reflect(**kwargs):
        if reflect_result is None:
            raise RuntimeError("reflect disabled")
        return reflect_result

    vlm.make_plan = _make_plan
    vlm.reflect = _reflect
    vlm.inject_error_feedback = lambda msg: None
    vlm._build_history_summary = lambda: ""
    return vlm


def _make_stub_plan(sub_goals_count=3):
    sub_goals = []
    for i in range(sub_goals_count):
        sg = types.SimpleNamespace(
            id=i + 1,
            description=f"subgoal_{i + 1}",
            status="pending" if i > 0 else "active",
        )
        sub_goals.append(sg)
    return types.SimpleNamespace(sub_goals=sub_goals, current_idx=0)


class TestPlanningPhaseInit:
    def test_initial_state(self):
        from visual_web_agent.phases.planning import PlanningPhase

        pp = PlanningPhase()
        assert pp.task_plan is None
        assert pp.steps_since_reflect == 0
        assert pp.reflect_count == 0
        assert pp.abort_requested is False
        assert pp.dedup_tripped_last_step is False


class TestMakeInitialPlan:
    def test_success_stores_plan(self):
        from visual_web_agent.phases.planning import PlanningPhase

        plan = _make_stub_plan(2)
        vlm = _make_stub_vlm(plan_result=plan)
        pp = PlanningPhase()

        async def _run():
            return await pp.make_initial_plan(
                vlm=vlm, goal="extract 10 rows",
                initial_url="https://example.com", workflow_memory={},
            )

        result = asyncio.run(_run())
        assert result is plan
        assert pp.task_plan is plan

    def test_failure_returns_none(self):
        from visual_web_agent.phases.planning import PlanningPhase

        vlm = _make_stub_vlm(plan_result=None)
        pp = PlanningPhase()

        async def _run():
            return await pp.make_initial_plan(
                vlm=vlm, goal="extract data",
                initial_url="https://example.com", workflow_memory={},
            )

        result = asyncio.run(_run())
        assert result is None
        assert pp.task_plan is None


class TestMaybeReflect:
    def test_no_plan_skips_reflection(self):
        from visual_web_agent.phases.planning import PlanningPhase

        vlm = _make_stub_vlm()
        pp = PlanningPhase()

        async def _run():
            return await pp.maybe_reflect(
                vlm=vlm, current_url="https://example.com",
                goal="test", consecutive_errors=3,
            )

        result = asyncio.run(_run())
        assert result is None

    def test_no_signals_increments_counter(self):
        from visual_web_agent.phases.planning import PlanningPhase

        vlm = _make_stub_vlm()
        pp = PlanningPhase()
        pp.task_plan = _make_stub_plan(2)

        async def _run():
            return await pp.maybe_reflect(
                vlm=vlm, current_url="https://example.com",
                goal="test", consecutive_errors=0,
            )

        result = asyncio.run(_run())
        assert result is None
        assert pp.steps_since_reflect == 1

    def test_max_reflects_stops(self):
        from visual_web_agent.phases.planning import PlanningPhase

        vlm = _make_stub_vlm()
        pp = PlanningPhase()
        pp.task_plan = _make_stub_plan(2)
        pp.reflect_count = 5

        async def _run():
            return await pp.maybe_reflect(
                vlm=vlm, current_url="https://example.com",
                goal="test", consecutive_errors=3,
            )

        result = asyncio.run(_run())
        assert result is None
        assert pp.steps_since_reflect == 1

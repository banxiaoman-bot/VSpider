"""Routing regression for the ``resume_run`` capability (RUN-RESUME1 step 3).

The capability router should recognise resume / 断点续跑 goals (中英文) and
surface a ``resume_run`` step in the deterministic backend plan, while leaving
unrelated goals untouched.
"""

from __future__ import annotations

from visual_web_agent.capability_router import _backend_plan, _signals


class TestResumeSignal:
    def test_chinese_goal_flags_resume(self) -> None:
        sig = _signals("继续上次没抓完的任务，断点续跑", {})
        assert sig["resume_preferred"] is True

    def test_english_goal_flags_resume(self) -> None:
        sig = _signals("resume the previous run where it left off", {})
        assert sig["resume_preferred"] is True

    def test_unrelated_goal_not_flagged(self) -> None:
        sig = _signals("click the submit button and login", {})
        assert sig["resume_preferred"] is False


class TestResumePlan:
    def test_plan_includes_resume_run(self) -> None:
        sig = _signals("断点续跑，接着上次继续抓取", {})
        plan = _backend_plan(sig, {}, [])
        names = [step.get("name") for step in plan]
        assert "resume_run" in names

    def test_unrelated_plan_excludes_resume_run(self) -> None:
        sig = _signals("click the submit button", {})
        plan = _backend_plan(sig, {}, [])
        names = [step.get("name") for step in plan]
        assert "resume_run" not in names

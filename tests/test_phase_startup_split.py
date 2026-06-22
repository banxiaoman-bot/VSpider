"""G1 startup phase split — TDD tests.

Verify that ``StartupPhase`` can produce a well-formed ``RunContext`` and
that the ``init_loop_guards`` factory returns all required guard objects.
Pure unit tests; no browser, no VLM, no network.
"""

from __future__ import annotations

import types
import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_stub_vlm():
    """Minimal VLM stub (only needs .reset_element_tracker)."""
    vlm = types.SimpleNamespace()
    vlm.reset_element_tracker = lambda: None
    return vlm


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class TestRunContext:
    """RunContext dataclass has all required fields."""

    def test_fields_present(self):
        from visual_web_agent.phases.startup import RunContext

        ctx = RunContext(
            run_ts="20260615_120000_000000",
            vlm_output="output_20260615_120000_000000.xlsx",
            xhr_output="xhr_20260615_120000_000000.xlsx",
            goal_output_mode="default",
            goal_output_contract={},
            initial_output_contract={},
            prompt_images=[],
            prompt_image_policy="adaptive",
            registry_record_owned=False,
        )
        assert ctx.run_ts == "20260615_120000_000000"
        assert ctx.vlm_output.startswith("output_")
        assert ctx.xhr_output.startswith("xhr_")
        assert ctx.goal_output_mode == "default"
        assert ctx.prompt_images == []

    def test_defaults(self):
        from visual_web_agent.phases.startup import RunContext

        ctx = RunContext(
            run_ts="test",
            vlm_output="o.xlsx",
            xhr_output="x.xlsx",
        )
        assert ctx.goal_output_mode == "default"
        assert ctx.goal_output_contract == {}
        assert ctx.initial_output_contract == {}
        assert ctx.prompt_images == []
        assert ctx.prompt_image_policy == "adaptive"
        assert ctx.registry_record_owned is False


class TestInitLoopGuards:
    """init_loop_guards() creates Judge, LoopDetector, FailureStats, and state."""

    def test_returns_all_guards(self):
        from visual_web_agent.phases.startup import init_loop_guards

        vlm = _make_stub_vlm()
        guards = init_loop_guards(vlm)

        assert hasattr(guards, "judge")
        assert hasattr(guards, "loop_detector")
        assert hasattr(guards, "failure_stats")
        assert hasattr(guards, "judge_rejections")
        assert guards.judge_rejections == 0
        assert hasattr(guards, "consecutive_errors")
        assert guards.consecutive_errors == 0
        assert guards.max_consecutive_errors == 3

    def test_loop_detector_config(self):
        from visual_web_agent.phases.startup import init_loop_guards

        vlm = _make_stub_vlm()
        guards = init_loop_guards(vlm)

        assert guards.loop_detector.config.window_size == 8
        assert guards.loop_detector.config.action_repeat_threshold == 3
        assert guards.loop_detector.config.stagnation_threshold == 4


class TestPrepareRunIdentity:
    """prepare_run_identity() sets up run_ts and output filenames."""

    def test_with_caller_run_id(self):
        from visual_web_agent.phases.startup import prepare_run_identity

        ctx = prepare_run_identity(run_id="my-custom-run-123")
        assert ctx.run_ts == "my-custom-run-123"
        assert "my-custom-run-123" in ctx.vlm_output
        assert "my-custom-run-123" in ctx.xhr_output

    def test_without_caller_run_id(self):
        from visual_web_agent.phases.startup import prepare_run_identity

        ctx = prepare_run_identity(run_id="")
        assert len(ctx.run_ts) > 0
        assert ctx.run_ts in ctx.vlm_output

    def test_sanitizes_run_id(self):
        from visual_web_agent.phases.startup import prepare_run_identity

        ctx = prepare_run_identity(run_id="bad chars!@#$%")
        assert "!" not in ctx.run_ts
        assert "@" not in ctx.run_ts

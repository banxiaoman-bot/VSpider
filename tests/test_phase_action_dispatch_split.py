"""G4 action dispatch phase split — TDD tests.

Verify that ``PostDecisionGuards`` manages repeat-action detection,
subgoal auto-advance, and zero-target-downgrade guard.
Pure unit tests; no browser, no VLM, no network.
"""

from __future__ import annotations

import types


def _make_stub_vlm():
    feedback = []
    vlm = types.SimpleNamespace()
    vlm.inject_error_feedback = lambda msg: feedback.append(msg)
    vlm._feedback = feedback
    return vlm


class TestPostDecisionGuardsInit:
    def test_initial_state(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        assert g.prev_action_sig == ("", 0, "")
        assert g.repeat_action_count == 0
        assert g.consecutive_zero_target == 0


class TestRepeatGuard:
    def test_no_repeat_first_step(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        vlm = _make_stub_vlm()
        decisions = [{"action": "click", "target_id": 5, "type_value": ""}]
        result = g.apply_repeat_guard(decisions, vlm=vlm)
        assert not result.repeat_warned
        assert not result.repeat_hijacked
        assert result.decisions[0]["action"] == "click"
        assert len(vlm._feedback) == 0

    def test_second_repeat_soft_warning(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        g.prev_action_sig = ("click", 5, "")
        g.repeat_action_count = 1
        vlm = _make_stub_vlm()
        decisions = [{"action": "click", "target_id": 5, "type_value": ""}]
        result = g.apply_repeat_guard(decisions, vlm=vlm)
        assert result.repeat_warned
        assert not result.repeat_hijacked
        assert result.decisions[0]["action"] == "click"
        assert len(vlm._feedback) == 1
        assert "连续 2 次" in vlm._feedback[0]

    def test_third_repeat_with_advance_marker_hard_hijack(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        g.prev_action_sig = ("click", 5, "")
        g.repeat_action_count = 2
        vlm = _make_stub_vlm()
        decisions = [{
            "action": "click", "target_id": 5, "type_value": "",
            "thought": "已完成，应推进至下一子目标",
        }]
        result = g.apply_repeat_guard(decisions, vlm=vlm)
        assert result.repeat_hijacked
        assert result.decisions[0]["action"] == "wait"
        assert result.decisions[0]["subgoal_status"] == "completed"
        assert g.repeat_action_count == 0

    def test_third_repeat_without_advance_marker_no_hijack(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        g.prev_action_sig = ("click", 5, "")
        g.repeat_action_count = 2
        vlm = _make_stub_vlm()
        decisions = [{
            "action": "click", "target_id": 5, "type_value": "",
            "thought": "正在点击按钮",
        }]
        result = g.apply_repeat_guard(decisions, vlm=vlm)
        assert not result.repeat_hijacked
        assert result.decisions[0]["action"] == "click"

    def test_transitional_action_never_intercepted(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        g.prev_action_sig = ("scroll", 0, "down")
        g.repeat_action_count = 4
        vlm = _make_stub_vlm()
        decisions = [{
            "action": "scroll", "target_id": 0, "type_value": "down",
            "thought": "应推进至下一子目标",
        }]
        result = g.apply_repeat_guard(decisions, vlm=vlm)
        assert not result.repeat_warned
        assert not result.repeat_hijacked
        assert result.decisions[0]["action"] == "scroll"

    def test_already_completed_not_hijacked(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        g.prev_action_sig = ("click", 5, "")
        g.repeat_action_count = 2
        vlm = _make_stub_vlm()
        decisions = [{
            "action": "click", "target_id": 5, "type_value": "",
            "thought": "应推进", "subgoal_status": "completed",
        }]
        result = g.apply_repeat_guard(decisions, vlm=vlm)
        assert not result.repeat_hijacked

    def test_different_action_resets_count(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        g.prev_action_sig = ("click", 5, "")
        g.repeat_action_count = 2
        vlm = _make_stub_vlm()
        decisions = [{"action": "type", "target_id": 3, "type_value": "hello"}]
        result = g.apply_repeat_guard(decisions, vlm=vlm)
        assert g.repeat_action_count == 1
        assert not result.repeat_warned

    def test_empty_decisions_safe(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        vlm = _make_stub_vlm()
        result = g.apply_repeat_guard([], vlm=vlm)
        assert result.decisions == []


class TestZeroTargetGuard:
    def test_counts_consecutive_downgrades(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        vlm = _make_stub_vlm()
        dec = [{"action": "wait", "__zero_target_downgraded": True, "thought": "click @e21"}]
        g.apply_zero_target_guard(dec, vlm=vlm)
        assert g.consecutive_zero_target == 1
        g.apply_zero_target_guard(dec, vlm=vlm)
        assert g.consecutive_zero_target == 2
        result = g.apply_zero_target_guard(dec, vlm=vlm)
        assert result.zero_target_ultimatum
        assert g.consecutive_zero_target == 0
        assert len(vlm._feedback) == 1
        assert "最后通牒" in vlm._feedback[0]
        assert "21" in vlm._feedback[0]

    def test_resets_on_normal_decision(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        g.consecutive_zero_target = 2
        vlm = _make_stub_vlm()
        dec = [{"action": "click", "target_id": 5}]
        g.apply_zero_target_guard(dec, vlm=vlm)
        assert g.consecutive_zero_target == 0
        assert not vlm._feedback

    def test_no_en_match_uses_placeholder(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        g.consecutive_zero_target = 2
        vlm = _make_stub_vlm()
        dec = [{"action": "wait", "__zero_target_downgraded": True, "thought": "click button"}]
        result = g.apply_zero_target_guard(dec, vlm=vlm)
        assert result.zero_target_ultimatum
        assert "<你 thought 中提到的 @eN 数字>" in vlm._feedback[0]

    def test_empty_decisions_safe(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        vlm = _make_stub_vlm()
        result = g.apply_zero_target_guard([], vlm=vlm)
        assert not result.zero_target_ultimatum


class TestRecordActionSig:
    def test_records_signature(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        g.record_action_sig([{"action": "click", "target_id": 7, "type_value": "submit"}])
        assert g.prev_action_sig == ("click", 7, "submit")

    def test_empty_decisions_no_change(self):
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards

        g = PostDecisionGuards()
        g.record_action_sig([])
        assert g.prev_action_sig == ("", 0, "")


class TestGuardResult:
    def test_defaults(self):
        from visual_web_agent.phases.action_dispatch import GuardResult

        r = GuardResult(decisions=[])
        assert not r.repeat_warned
        assert not r.repeat_hijacked
        assert not r.zero_target_ultimatum

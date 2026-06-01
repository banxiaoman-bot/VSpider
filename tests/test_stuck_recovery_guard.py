"""Tests for stuck_recovery_guard."""

from __future__ import annotations

from visual_web_agent.stuck_recovery_guard import (
    StuckRecoveryState,
    evaluate_failure_recovery,
    evaluate_loop_recovery,
    parse_missing_target_id,
)


def test_parse_missing_target_id() -> None:
    assert parse_missing_target_id("Element #59 not found on active page") == 59
    assert parse_missing_target_id("timeout") is None


def test_failure_recovery_after_two_same_missing() -> None:
    state = StuckRecoveryState()
    d1 = evaluate_failure_recovery(
        state,
        error_msg="Element #59 not found on active page",
        target_id=59,
        consecutive_errors=1,
    )
    assert d1.should_reset is False
    d2 = evaluate_failure_recovery(
        state,
        error_msg="Element #59 not found on active page",
        target_id=59,
        consecutive_errors=2,
    )
    assert d2.should_reset is True
    assert "59" in d2.reason


def test_loop_recovery_after_second_nudge() -> None:
    state = StuckRecoveryState()
    d1 = evaluate_loop_recovery(state, loop_type="action_repeat", nudge_number=1)
    assert d1.should_reset is False
    d2 = evaluate_loop_recovery(state, loop_type="action_repeat", nudge_number=2)
    assert d2.should_reset is True

from __future__ import annotations

from visual_web_agent.action_reliability import build_action_reliability_score


def test_action_reliability_score_is_low_risk_for_clean_trace_and_state() -> None:
    score = build_action_reliability_score(
        {
            "version": "browser_action_trace.v1",
            "action": "click",
            "status": "ok",
            "target": {"ref": "@e1"},
            "action_ref": {"ref": "@e1", "selector": "button.submit", "source": "browser_ref"},
            "result_summary": {"url": "https://example.com"},
            "warning_codes": [],
        },
        {
            "version": "browser_state.v2",
            "metrics": {"interactive_count": 8, "action_ref_count": 8},
            "interaction": {"action_ref_count": 8},
            "runtime": {"status": "available"},
        },
    )

    assert score["version"] == "action_reliability_score.v1"
    assert score["source"] == "action_reliability"
    assert score["status"] == "ok"
    assert score["score"] == 1.0
    assert score["risk_level"] == "low"
    assert score["risk_factors"] == []
    assert score["self_healing_policy"]["version"] == "self_healing_policy.v1"
    assert score["self_healing_policy"]["recommended_action"] == "continue"
    assert score["self_healing_policy"]["requires_snapshot_refresh"] is False


def test_action_reliability_detects_selector_and_snapshot_refresh_risk() -> None:
    score = build_action_reliability_score(
        {
            "version": "browser_action_trace.v1",
            "action": "click",
            "status": "warn",
            "target": {"ref": "@e9"},
            "warning_codes": ["selector_fallback_used"],
            "issue_summary": {"recommended_actions": ["refresh_browser_snapshot"]},
        },
        {
            "version": "browser_state.v2",
            "metrics": {"interactive_count": 0, "action_ref_count": 0},
            "interaction": {"action_refs": []},
            "runtime": {"status": "available"},
        },
    )

    codes = {item["code"] for item in score["risk_factors"]}
    assert score["risk_level"] == "medium"
    assert "action_ref_missing" in codes
    assert "selector_missing" in codes
    assert "browser_state_empty" in codes
    assert "browser_state_action_refs_empty" in codes
    policy = score["self_healing_policy"]
    assert policy["requires_snapshot_refresh"] is True
    assert policy["retry_allowed"] is True
    assert "use_similar_selector" in policy["recommended_actions"]
    assert "retry_action_with_new_ref" in policy["recommended_actions"]


def test_action_reliability_escalates_failed_runtime_and_history() -> None:
    score = build_action_reliability_score(
        {
            "version": "browser_action_trace.v1",
            "action": "click",
            "status": "error",
            "target": {"ref": "@e1"},
            "action_ref": {"ref": "@e1", "selector": "button.submit"},
            "result_summary": {
                "failure_code": "timeout",
                "failure_category": "timing",
                "recovery_actions": ["increase_wait_timeout", "check_browser_runtime", "retry_action_once"],
            },
            "warning_codes": ["action_failed", "timeout"],
        },
        {
            "version": "browser_state.v2",
            "metrics": {"interactive_count": 5, "action_ref_count": 5},
            "runtime": {"status": "unavailable"},
        },
        history={"attempt_count": 4, "failed_count": 3, "passed_count": 1},
    )

    codes = {item["code"] for item in score["risk_factors"]}
    assert score["status"] == "error"
    assert score["risk_level"] == "high"
    assert score["score"] < 0.5
    assert "action_error" in codes
    assert "timeout" in codes
    assert "runtime_unavailable" in codes
    assert "history_failure_rate_high" in codes
    assert score["history"]["failure_rate"] == 0.75
    policy = score["self_healing_policy"]
    assert policy["requires_runtime_check"] is True
    assert policy["requires_planner_replan"] is True
    assert policy["escalation"] == "planner_replan"
    assert "avoid_repeating_failed_action" in policy["recommended_actions"]
    assert "replan_with_planner_feedback" in policy["recommended_actions"]

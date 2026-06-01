from __future__ import annotations

from visual_web_agent.deterministic_evaluator import build_deterministic_evaluation


def test_deterministic_evaluator_accepts_completed_verified_execution() -> None:
    evaluation = build_deterministic_evaluation(
        {
            "status": "completed",
            "completed": True,
            "capability": "generic_extractor",
            "result": {"rows": [{"title": "A"}], "row_count": 1},
            "verification": {"passed": True, "summary": "success criteria met", "observed_count": 1, "target_count": 1, "checks": []},
        },
        browser_state={"version": "browser_state.v2", "metrics": {"interactive_count": 1}},
        action_reliability={"version": "action_reliability_score.v1", "risk_level": "low", "score": 1.0, "self_healing_policy": {"recommended_actions": ["continue"]}},
    )

    assert evaluation["version"] == "deterministic_evaluation.v1"
    assert evaluation["source"] == "deterministic_evaluator"
    assert evaluation["status"] == "passed"
    assert evaluation["passed"] is True
    assert evaluation["decision"] == "accept"
    assert evaluation["recommended_action"] == "continue"
    assert evaluation["failed_checks"] == []
    assert evaluation["role_boundaries"]["planner"] == "proposes route and fallback strategy"
    assert evaluation["role_boundaries"]["executor"] == "runs deterministic capability and returns evidence"
    assert evaluation["role_boundaries"]["evaluator"] == "accepts, repairs, replans, or rejects from deterministic evidence"


def test_deterministic_evaluator_repairs_medium_action_reliability() -> None:
    evaluation = build_deterministic_evaluation(
        {
            "status": "completed",
            "completed": True,
            "capability": "browser_control",
            "verification": {"passed": True, "summary": "action completed", "checks": []},
        },
        browser_state={"version": "browser_state.v2", "metrics": {"interactive_count": 0}},
        action_reliability={
            "version": "action_reliability_score.v1",
            "risk_level": "medium",
            "score": 0.5,
            "risk_factors": [{"code": "selector_missing"}],
            "self_healing_policy": {
                "recommended_actions": ["refresh_browser_snapshot", "use_similar_selector"],
                "requires_snapshot_refresh": True,
                "retry_allowed": True,
            },
        },
    )

    assert evaluation["status"] == "needs_repair"
    assert evaluation["passed"] is False
    assert evaluation["decision"] == "repair"
    assert evaluation["failed_checks"] == ["action_reliability"]
    assert evaluation["recommended_actions"] == ["refresh_browser_snapshot", "use_similar_selector"]
    reliability_check = next(item for item in evaluation["checks"] if item["name"] == "action_reliability")
    assert reliability_check["severity"] == "warn"
    assert reliability_check["risk_level"] == "medium"


def test_deterministic_evaluator_replans_for_failure_bundle_or_high_risk() -> None:
    evaluation = build_deterministic_evaluation(
        {
            "status": "error",
            "completed": False,
            "capability": "browser_control",
            "fallback_reason": "click failed",
            "failure_bundle": {
                "version": "capability_execute_failure_bundle.v1",
                "status": "error",
                "primary_failure": "timeout",
                "recommended_action": "check_browser_runtime",
                "recommended_actions": ["check_browser_runtime", "replan_with_planner_feedback"],
            },
        },
        action_reliability={
            "version": "action_reliability_score.v1",
            "risk_level": "high",
            "score": 0.2,
            "self_healing_policy": {
                "requires_planner_replan": True,
                "recommended_actions": ["check_browser_runtime", "replan_with_planner_feedback"],
            },
        },
    )

    assert evaluation["status"] == "failed"
    assert evaluation["passed"] is False
    assert evaluation["decision"] == "replan"
    assert "executor_completed" in evaluation["failed_checks"]
    assert "action_reliability" in evaluation["failed_checks"]
    assert "failure_bundle" in evaluation["failed_checks"]
    assert evaluation["recommended_action"] == "check_browser_runtime"
    assert "replan_with_planner_feedback" in evaluation["recommended_actions"]
    assert evaluation["evidence"]["failure_bundle"]["primary_failure"] == "timeout"


def test_deterministic_evaluator_can_compute_verification_from_result() -> None:
    evaluation = build_deterministic_evaluation(
        {
            "status": "completed",
            "completed": True,
            "capability": "generic_extractor",
            "result": {"rows": [{"title": "A"}], "row_count": 1},
        },
        route={"strategy_context": {"target_count": 1, "requested_fields": ["title"]}},
    )

    assert evaluation["decision"] == "accept"
    verification = evaluation["evidence"]["verification"]
    assert verification["passed"] is True
    assert verification["observed_count"] == 1
    assert verification["required_fields"] == ["title"]

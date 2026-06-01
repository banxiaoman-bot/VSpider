from __future__ import annotations

from visual_web_agent.efficiency_correlation import build_efficiency_correlation_report


def test_efficiency_correlation_accepts_aligned_api_path() -> None:
    report = build_efficiency_correlation_report(
        {"status": "completed", "completed": True, "capability": "api_replay"},
        crawl_efficiency_plan={
            "version": "crawl_efficiency_plan.v1",
            "recommended_path": "api_replay",
            "preferred_order": ["api_replay", "html_extract", "dom_selector", "targeted_probe", "browser_action", "vision_agent"],
            "available_paths": ["api_replay"],
            "skip_browser": True,
            "skip_vlm": True,
        },
        deterministic_evaluation={"version": "deterministic_evaluation.v1", "decision": "accept", "passed": True, "capability": "api_replay"},
        action_reliability={"version": "action_reliability_score.v1", "risk_level": "low", "score": 1.0},
    )

    assert report["version"] == "efficiency_correlation_report.v1"
    assert report["source"] == "efficiency_correlation"
    assert report["status"] == "aligned"
    assert report["passed"] is True
    assert report["alignment"]["recommended_path"] == "api_replay"
    assert report["alignment"]["executed_path"] == "api_replay"
    assert report["alignment"]["matched"] is True
    assert report["failed_checks"] == []
    assert report["recommended_action"] == "keep_current_path"


def test_efficiency_correlation_flags_browser_fallback_when_api_was_available() -> None:
    report = build_efficiency_correlation_report(
        {"status": "completed", "completed": True, "capability": "browser_control"},
        crawl_efficiency_plan={
            "version": "crawl_efficiency_plan.v1",
            "recommended_path": "api_replay",
            "preferred_order": ["api_replay", "html_extract", "dom_selector", "targeted_probe", "browser_action", "vision_agent"],
            "available_paths": ["api_replay", "browser_action"],
            "skip_browser": True,
            "skip_vlm": True,
        },
        deterministic_evaluation={"version": "deterministic_evaluation.v1", "decision": "accept", "passed": True, "capability": "browser_control"},
        action_reliability={"version": "action_reliability_score.v1", "risk_level": "low", "score": 0.95},
    )

    assert report["status"] == "suboptimal"
    assert report["passed"] is False
    assert report["alignment"]["executed_path"] == "browser_action"
    assert report["alignment"]["rank_gap"] == 4
    assert "executed_path_more_expensive_than_recommendation" in report["root_causes"]
    assert "browser_used_despite_skip_browser" in report["root_causes"]
    assert "executed_matches_recommended" in report["failed_checks"]
    assert "skip_browser_respected" in report["failed_checks"]
    assert report["recommended_action"] == "prefer_api_replay_before_browser_action"
    assert report["planner_hints"][0]["kind"] == "prefer_path"


def test_efficiency_correlation_recommends_repair_for_medium_reliability_and_empty_state() -> None:
    report = build_efficiency_correlation_report(
        {"status": "completed", "completed": True, "capability": "browser_control"},
        crawl_efficiency_plan={
            "version": "crawl_efficiency_plan.v1",
            "recommended_path": "targeted_probe",
            "preferred_order": ["api_replay", "html_extract", "dom_selector", "targeted_probe", "browser_action", "vision_agent"],
            "available_paths": ["targeted_probe", "browser_action"],
            "skip_browser": False,
            "skip_vlm": True,
        },
        deterministic_evaluation={
            "version": "deterministic_evaluation.v1",
            "decision": "repair",
            "passed": False,
            "capability": "browser_control",
            "recommended_actions": ["refresh_browser_snapshot", "use_similar_selector"],
        },
        action_reliability={
            "version": "action_reliability_score.v1",
            "risk_level": "medium",
            "score": 0.5,
            "risk_factors": [{"code": "selector_missing"}],
            "self_healing_policy": {"recommended_actions": ["refresh_browser_snapshot", "use_similar_selector"]},
        },
        browser_state={"version": "browser_state.v2", "metrics": {"interactive_count": 0}, "runtime": {"status": "available"}},
    )

    assert report["status"] == "needs_repair"
    assert report["alignment"]["recommended_path"] == "targeted_probe"
    assert report["alignment"]["executed_path"] == "browser_action"
    assert "action_reliability_medium" in report["root_causes"]
    assert "deterministic_evaluation_repair" in report["root_causes"]
    assert "browser_state_interactive_evidence_missing" in report["root_causes"]
    assert "action_reliability_low" in report["failed_checks"]
    assert "browser_state_sufficient" in report["failed_checks"]
    assert "refresh_browser_snapshot" in report["recommended_actions"]
    assert "repair_and_retry_with_efficiency_plan" in report["recommended_actions"]


def test_efficiency_correlation_escalates_high_risk_failure_bundle_to_replan() -> None:
    report = build_efficiency_correlation_report(
        {
            "status": "error",
            "completed": False,
            "capability": "browser_control",
            "failure_bundle": {
                "version": "capability_execute_failure_bundle.v1",
                "primary_failure": "timeout",
                "recommended_actions": ["check_browser_runtime", "replan_with_planner_feedback"],
            },
        },
        crawl_efficiency_plan={
            "version": "crawl_efficiency_plan.v1",
            "recommended_path": "dom_selector",
            "preferred_order": ["api_replay", "html_extract", "dom_selector", "targeted_probe", "browser_action", "vision_agent"],
            "available_paths": ["dom_selector", "browser_action"],
            "skip_browser": True,
            "skip_vlm": True,
        },
        deterministic_evaluation={"version": "deterministic_evaluation.v1", "decision": "replan", "passed": False, "capability": "browser_control"},
        action_reliability={"version": "action_reliability_score.v1", "risk_level": "high", "score": 0.2},
    )

    assert report["status"] == "needs_replan"
    assert "failure_bundle_present" in report["root_causes"]
    assert "action_reliability_high" in report["root_causes"]
    assert "deterministic_evaluation_replan" in report["root_causes"]
    assert "failure_bundle_absent" in report["failed_checks"]
    assert report["recommended_action"] == "prefer_dom_selector_before_browser_action"
    assert "replan_with_efficiency_feedback" in report["recommended_actions"]
    assert report["evidence"]["failure_bundle"]["primary_failure"] == "timeout"

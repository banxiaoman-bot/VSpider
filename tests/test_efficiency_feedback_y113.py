from __future__ import annotations

from visual_web_agent.efficiency_feedback import build_efficiency_planner_feedback


def test_efficiency_feedback_returns_empty_for_aligned_report() -> None:
    feedback = build_efficiency_planner_feedback({
        "version": "efficiency_correlation_report.v1",
        "source": "efficiency_correlation",
        "status": "aligned",
        "failed_checks": [],
        "alignment": {"recommended_path": "api_replay", "executed_path": "api_replay"},
    })

    assert feedback == {}


def test_efficiency_feedback_prefers_api_replay_when_browser_was_unnecessary() -> None:
    feedback = build_efficiency_planner_feedback({
        "version": "efficiency_correlation_report.v1",
        "source": "efficiency_correlation",
        "status": "suboptimal",
        "alignment": {
            "recommended_path": "api_replay",
            "executed_path": "browser_action",
            "executed_capability": "browser_control",
            "rank_gap": 4,
            "skip_browser": True,
            "skip_vlm": True,
        },
        "failed_checks": ["executed_matches_recommended", "skip_browser_respected"],
        "root_causes": ["executed_path_more_expensive_than_recommendation", "browser_used_despite_skip_browser"],
        "recommended_action": "prefer_api_replay_before_browser_action",
        "recommended_actions": ["prefer_api_replay_before_browser_action", "avoid_browser_when_skip_browser_true"],
        "planner_hints": [{"kind": "prefer_path", "path": "api_replay", "message": "Prefer api_replay"}],
    })

    assert feedback["version"] == "planner_feedback.v1"
    assert feedback["source"] == "efficiency_correlation_report"
    assert feedback["feedback_type"] == "efficiency_correlation"
    assert feedback["primary_failure"] == "executed_path_more_expensive_than_recommendation"
    assert feedback["recommended_action"] == "prefer_api_replay_before_browser_action"
    assert feedback["preferred_capabilities"] == ["network_intelligence", "api_replay"]
    assert feedback["avoid_capabilities"] == ["browser_control"]
    assert "open_browser_when_deterministic_path_available" in feedback["avoid_actions"]
    assert feedback["preferred_paths"] == ["api_replay"]
    assert feedback["avoid_paths"] == ["browser_action"]
    assert feedback["efficiency_alignment"]["rank_gap"] == 4


def test_efficiency_feedback_recommends_probe_repair_before_repeating_browser_action() -> None:
    feedback = build_efficiency_planner_feedback({
        "version": "efficiency_correlation_report.v1",
        "source": "efficiency_correlation",
        "status": "needs_repair",
        "alignment": {
            "recommended_path": "targeted_probe",
            "executed_path": "browser_action",
            "executed_capability": "browser_control",
            "skip_browser": False,
            "skip_vlm": True,
        },
        "failed_checks": ["action_reliability_low", "browser_state_sufficient"],
        "root_causes": [
            "action_reliability_medium",
            "deterministic_evaluation_repair",
            "browser_state_interactive_evidence_missing",
        ],
        "recommended_actions": ["refresh_browser_snapshot", "use_similar_selector", "repair_and_retry_with_efficiency_plan"],
    })

    assert feedback["primary_failure"] == "action_reliability_medium"
    assert feedback["preferred_capabilities"] == ["action_registry_macros", "browser_control_find", "selector_generator"]
    assert feedback["avoid_capabilities"] == ["browser_control"]
    assert "repeat_unreliable_action_without_repair" in feedback["avoid_actions"]
    assert "targeted_probe_without_fresh_browser_state" in feedback["avoid_actions"]
    assert "refresh_browser_snapshot" in feedback["recommended_actions"]
    assert feedback["preferred_paths"] == ["targeted_probe"]


def test_efficiency_feedback_projects_replan_report_to_dom_selector_preference() -> None:
    feedback = build_efficiency_planner_feedback({
        "version": "efficiency_correlation_report.v1",
        "source": "efficiency_correlation",
        "status": "needs_replan",
        "alignment": {
            "recommended_path": "dom_selector",
            "executed_path": "browser_action",
            "executed_capability": "browser_control",
            "skip_browser": True,
            "skip_vlm": True,
        },
        "failed_checks": ["failure_bundle_absent"],
        "root_causes": ["failure_bundle_present", "deterministic_evaluation_replan", "action_reliability_high"],
        "recommended_action": "prefer_dom_selector_before_browser_action",
        "recommended_actions": ["prefer_dom_selector_before_browser_action", "replan_with_efficiency_feedback"],
        "evidence": {"failure_bundle": {"primary_failure": "timeout"}},
    })

    assert feedback["status"] == "active"
    assert feedback["failure_category"] == "efficiency_alignment"
    assert feedback["preferred_capabilities"] == ["extractor_select", "generic_extractor"]
    assert feedback["avoid_capabilities"] == ["browser_control"]
    assert "ignore_failure_bundle_before_replan" in feedback["avoid_actions"]
    assert "accept_failed_evaluation_without_replan" in feedback["avoid_actions"]
    assert feedback["recommended_action"] == "prefer_dom_selector_before_browser_action"
    assert "replan_with_efficiency_feedback" in feedback["recommended_actions"]
    assert feedback["evidence"]["root_causes"] == ["failure_bundle_present", "deterministic_evaluation_replan", "action_reliability_high"]

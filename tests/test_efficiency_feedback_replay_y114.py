from __future__ import annotations

from visual_web_agent.efficiency_feedback import build_efficiency_planner_feedback
from visual_web_agent.efficiency_feedback_replay import evaluate_planner_feedback_shadow, replay_efficiency_feedback, replay_efficiency_feedback_batch


def _suboptimal_report() -> dict:
    return {
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
    }


def test_efficiency_feedback_replay_validates_correlation_report_against_planner_contract() -> None:
    replay = replay_efficiency_feedback({
        "goal": "通过接口回放提取商品列表",
        "url": "https://example.com/products",
        "efficiency_correlation_report": _suboptimal_report(),
    })
    checks = {item["name"]: item for item in replay["checks"]}
    feedback_step = replay["execution_plan"]["feedback_step"]

    assert replay["version"] == "efficiency_feedback_replay_report.v1"
    assert replay["source"] == "efficiency_feedback_replay"
    assert replay["passed"] is True
    assert replay["planner_feedback"]["version"] == "planner_feedback.v1"
    assert replay["planner_feedback"]["feedback_type"] == "efficiency_correlation"
    assert replay["planner_feedback"]["preferred_capabilities"] == ["network_intelligence", "api_replay"]
    assert replay["route"]["signals"]["planner_feedback"] is True
    assert replay["route"]["planner_feedback"]["primary_failure"] == "executed_path_more_expensive_than_recommendation"
    assert replay["execution_plan"]["version"] == "planner_contract.v1"
    assert "previous_failure_feedback_active" in replay["execution_plan"]["risk_flags"]
    assert "previous_failure_executed_path_more_expensive_than_recommendation" in replay["execution_plan"]["risk_flags"]
    assert feedback_step["capability"] in {"network_intelligence", "api_replay"}
    assert feedback_step["inputs"]["planner_feedback"]["recommended_action"] == "prefer_api_replay_before_browser_action"
    assert checks["execution_plan.step_inputs.planner_feedback"]["passed"] is True
    assert replay["failed_checks"] == []


def test_efficiency_feedback_replay_accepts_prebuilt_planner_feedback() -> None:
    planner_feedback = build_efficiency_planner_feedback({
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
    })

    replay = replay_efficiency_feedback({
        "goal": "提取商品标题和价格",
        "url": "https://example.com/products",
        "planner_feedback": planner_feedback,
    })
    feedback_step = replay["execution_plan"]["feedback_step"]

    assert replay["passed"] is True
    assert replay["planner_feedback"]["primary_failure"] == "failure_bundle_present"
    assert replay["planner_feedback"]["preferred_capabilities"] == ["extractor_select", "generic_extractor"]
    assert feedback_step["capability"] in {"extractor_select", "generic_extractor"}
    assert feedback_step["inputs"]["planner_feedback"]["primary_failure"] == "failure_bundle_present"
    assert "ignore_failure_bundle_before_replan" in feedback_step["inputs"]["planner_feedback"]["avoid_actions"]
    assert any("Planner feedback from previous failure failure_bundle_present" in item for item in replay["execution_plan"]["notes"])


def test_efficiency_feedback_replay_fails_closed_for_aligned_report() -> None:
    replay = replay_efficiency_feedback({
        "efficiency_correlation_report": {
            "version": "efficiency_correlation_report.v1",
            "status": "aligned",
            "failed_checks": [],
            "alignment": {"recommended_path": "api_replay", "executed_path": "api_replay"},
        }
    })

    assert replay["passed"] is False
    assert replay["planner_feedback"] == {}
    assert any(item["name"] == "planner_feedback.version" for item in replay["failed_checks"])


def test_efficiency_feedback_replay_batch_summarizes_direct_sources() -> None:
    batch = replay_efficiency_feedback_batch([
        {
            "name": "api replay feedback",
            "goal": "通过接口回放提取商品列表",
            "url": "https://example.com/products",
            "efficiency_correlation_report": _suboptimal_report(),
        },
        {
            "name": "aligned report",
            "efficiency_correlation_report": {
                "version": "efficiency_correlation_report.v1",
                "status": "aligned",
                "failed_checks": [],
                "alignment": {"recommended_path": "api_replay", "executed_path": "api_replay"},
            },
        },
    ])

    assert batch["version"] == "efficiency_feedback_replay_batch_report.v1"
    assert batch["source"] == "efficiency_feedback_replay_batch"
    assert batch["source_count"] == 2
    assert batch["passed_count"] == 1
    assert batch["failed_count"] == 1
    assert batch["passed"] is False
    assert batch["summary"]["status"] == "failed"
    assert batch["summary"]["blocking"] is True
    assert batch["summary"]["top_primary_failures"][0]["name"] == "executed_path_more_expensive_than_recommendation"
    assert batch["summary"]["top_preferred_capabilities"][0]["name"] == "api_replay"
    assert batch["summary"]["top_feedback_steps"][0]["name"] in {"network_intelligence", "api_replay"}
    assert batch["summary"]["recommended_focus"]
    assert batch["items"][0]["passed"] is True
    assert batch["items"][0]["source_kind"] == "efficiency_correlation_report"
    assert batch["items"][1]["passed"] is False
    assert any(item["name"] == "planner_feedback.version" for item in batch["failed_checks"])


def test_planner_feedback_shadow_evaluates_feedback_without_route_decision_change() -> None:
    shadow = evaluate_planner_feedback_shadow({
        "goal": "通过接口回放提取商品列表",
        "url": "https://example.com/products",
        "efficiency_correlation_report": _suboptimal_report(),
    })

    assert shadow["version"] == "planner_feedback_shadow_report.v1"
    assert shadow["source"] == "planner_feedback_shadow"
    assert shadow["passed"] is True
    assert shadow["planner_feedback"]["primary_failure"] == "executed_path_more_expensive_than_recommendation"
    assert shadow["baseline"]["signals"].get("planner_feedback") is None
    assert shadow["shadow"]["signals"]["planner_feedback"] is True
    assert shadow["delta"]["backend_plan_changed"] is False
    assert shadow["delta"]["fallback_chain_changed"] is False
    assert "planner_feedback" in shadow["delta"]["signals_added"]
    assert "previous_failure_feedback_active" in shadow["delta"]["risk_flags_added"]
    assert shadow["delta"]["feedback_step"] in {"network_intelligence", "api_replay"}
    assert shadow["shadow"]["execution_plan"]["feedback_step"]["inputs"]["planner_feedback"]["recommended_action"] == "prefer_api_replay_before_browser_action"
    assert shadow["baseline"]["execution_plan"]["planner_feedback_steps"] == []
    assert shadow["shadow"]["execution_plan"]["planner_feedback_steps"]

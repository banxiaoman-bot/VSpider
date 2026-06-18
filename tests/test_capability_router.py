from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import visual_web_agent.spider_lite as spider_lite_mod
from visual_web_agent.action_ref import action_ref_schema, normalize_action_ref, normalize_action_refs, summarize_action_ref_sources
from visual_web_agent.browser_control import BrowserControlManager, BrowserControlSession, build_browser_action_trace
from visual_web_agent.capability_failure_fixture import build_capability_failure_regression_fixture, write_capability_failure_regression_fixture
from visual_web_agent.capability_failure_replay import replay_capability_failure_fixture, replay_capability_failure_fixtures
from visual_web_agent.capability_router import model_role_report, planner_feedback_from_failure_bundle, route_task
from visual_web_agent.capability_manifest import get_capability, list_capabilities
from visual_web_agent.efficiency_feedback_replay import replay_efficiency_feedback
from visual_web_agent.extraction_engine import generic
from visual_web_agent.planner_contract import build_execution_plan
from visual_web_agent.prompts import build_user_message
from visual_web_agent.route_executor import execute_route
from visual_web_agent.success_verifier import verify_route_success
from visual_web_agent.spider_lite import FetchResult, SpiderLiteManager
from visual_web_agent.workflow_graph import build_workflow_graph


def _plan_names(route: dict) -> list[str]:
    return [str(item.get("name") or "") for item in route.get("backend_plan") or []]


def _fallback_names(route: dict) -> list[str]:
    return [str(item.get("capability") or "") for item in route.get("fallback_chain") or []]


@pytest.fixture
def _artifact_tmp(monkeypatch, tmp_path):
    def _resolve(filename, subdir=""):
        return tmp_path / subdir / filename

    def _artifact_url(path):
        return "/download/" + Path(path).name

    monkeypatch.setattr(generic, "resolve_artifact_path", _resolve)
    monkeypatch.setattr(generic, "register_artifact", lambda path: None)
    monkeypatch.setattr(generic, "artifact_url", _artifact_url)
    monkeypatch.setattr(spider_lite_mod, "resolve_artifact_path", _resolve)
    monkeypatch.setattr(spider_lite_mod, "register_artifact", lambda path: None)
    monkeypatch.setattr(spider_lite_mod, "artifact_url", _artifact_url)
    return tmp_path


class _FakeBrowserControlPage:
    url = "https://example.com/form"

    async def evaluate(self, script: str, arg=None):
        script_text = str(script)
        if "document.querySelectorAll(selector)" in script_text:
            return [
                {"tag": "button", "role": "button", "name": "Submit", "selector": "#submit", "type": "", "href": ""},
                {"tag": "input", "role": "", "name": "User", "selector": "#user", "type": "text", "href": ""},
            ]
        if "role_selector" in script_text:
            return {
                "css": "#submit",
                "xpath": "//*[@id=\"submit\"]",
                "role": "button",
                "role_selector": "role=button[name=\"Submit\"]",
                "text": "Submit",
                "tag": "button",
                "type": "",
                "href": "",
                "attributes": {"id": "submit"},
            }
        return [
            {"score": 0.9, "tag": "button", "role": "button", "name": "Save", "selector": "#save", "type": "", "href": ""},
        ]


def test_capability_router_covers_full_structured_crawl_stack() -> None:
    route = route_task(
        "爬取所有分页商品列表，字段: title, price，导出 Excel，并开启缓存回放调试",
        url="https://shop.example/list",
        context={"output_prediction": {"output_kind": "dataset_rows", "output_mode": "artifact"}},
    )
    names = _plan_names(route)
    fallback = _fallback_names(route)

    assert route["intent"]["task_type"] == "crawl_extract"
    assert "run_registry" in names
    assert "task_queue" in names
    assert "network_intelligence" in names
    assert "api_replay" in names
    assert "generic_extractor" in names
    assert "robots_throttle" in names
    assert "spider_lite" in names
    assert "page_response_cache" in names
    assert "feed_export" in names
    assert "item_pipeline" in names
    assert fallback.index("generic_extractor") < fallback.index("spider_lite")
    assert route["model_roles"]["vision_model"]["should_not_do"]
    assert any(item["area"] == "visual model placement" for item in route["audit"]["findings"])
    assert route["execution_plan"]["version"] == "planner_contract.v1"
    assert route["workflow_graph"]["version"] == "workflow_graph.v1"
    assert route["crawl_efficiency_plan"]["version"] == "crawl_efficiency_plan.v1"
    assert route["crawl_efficiency_plan"]["preferred_order"][:3] == ["api_replay", "html_extract", "dom_selector"]
    assert route["execution_plan"]["steps"][0]["capability"] == "run_registry"
    assert any(item["name"] == "generic_extractor" for item in route["capability_manifest"])
    assert route["output_contract"]["output_kind"] == "dataset_rows"
    assert "model_predicted_output_kind" in route["output_contract"]["reasons"]
    assert route["output_contract"]["reasons"].count("model_predicted_output_kind") == 1


def test_capability_router_uses_agent_strategy_field_parser() -> None:
    route = route_task(
        "提取 Name, Position, Office 字段",
        url="https://example.com/table",
    )

    assert route["output_contract"]["fields"] == ["Name", "Position", "Office"]
    assert route["output_contract"]["required_fields"] == ["Name", "Position", "Office"]
    assert route["output_contract"]["requested_fields"] == ["Name", "Position", "Office"]


def test_capability_router_uses_agent_strategy_target_count_parser_without_url_field_noise() -> None:
    route = route_task(
        "Extract top 120 records with fields title, url",
        url="https://example.com/table",
    )

    assert route["strategy_context"]["target_count"] == 120
    assert route["output_contract"]["container"] == "jsonl"
    assert route["output_contract"]["fields"] == ["title", "url"]


def test_capability_router_uses_agent_strategy_page_target_parser() -> None:
    route = route_task(
        "extract first 3 pages of products with fields title, price",
        url="https://example.com/products",
    )

    assert route["strategy_context"]["target_pages"] == 3
    assert "goal_has_page_target" in route["strategy_context"]["reasons"]
    assert "next_page" in route["strategy_context"]["preferred_actions"]


def test_capability_router_surfaces_failure_repair_feedback() -> None:
    route = route_task(
        "打开页面并点击按钮",
        url="https://example.com",
        context={"failure_bundle": {"primary_failure": "selector_missing", "recommended_actions": ["refresh_dom_snapshot"]}},
    )
    assert route["planner_feedback"]["primary_failure"] == "selector_missing"
    assert "selector_generator" in route["failure_repair"]["preferred_capabilities"]
    assert "refresh_dom_snapshot" in route["failure_repair"]["repair_actions"]


def test_capability_manifest_exposes_machine_readable_specs() -> None:
    all_caps = list_capabilities()
    data_plane = list_capabilities(layer="data_plane")
    extractor = get_capability("generic_extractor")
    action_ref = get_capability("action_ref_normalizer")
    capability_router = get_capability("capability_router")
    crawl_efficiency = get_capability("crawl_efficiency_planner")
    browser_backend = get_capability("browser_backend_abstraction")
    browser_pool = get_capability("browser_pool")
    browser_runtime_doctor = get_capability("browser_runtime_doctor")
    browser_control = get_capability("browser_control")
    universal_benchmark = get_capability("universal_benchmark")
    deterministic_evaluator = get_capability("deterministic_evaluator")
    agent_case_benchmark = get_capability("agent_case_benchmark")
    agent_case_regression = get_capability("agent_case_regression_runner")
    run_evidence_bundle = get_capability("run_evidence_bundle")
    efficiency_correlation = get_capability("efficiency_correlation")
    efficiency_feedback = get_capability("efficiency_feedback")
    efficiency_feedback_replay = get_capability("efficiency_feedback_replay")
    planner_feedback_shadow = get_capability("planner_feedback_shadow")
    artifact_manager = get_capability("artifact_manager")
    seven_layers = {
        "intent_planning",
        "operations_plane",
        "runtime_guards",
        "browser_substrate",
        "data_plane",
        "execution_kernel",
        "model_plane",
    }

    assert len(all_caps) >= 12
    assert {item["layer"] for item in all_caps} == seven_layers
    assert any(item["name"] == "capability_router" for item in all_caps)
    assert capability_router is not None
    assert capability_router["milestones"] == [
        "Y29", "Y43", "Y44", "Y53", "Y54", "Y55", "Y56", "Y57", "Y58", "Y59", "Y60", "Y61", "Y62", "Y63", "Y64", "Y65", "Y66", "Y73", "Y74", "Y75", "Y77", "Y78", "Y79", "Y80", "Y81", "Y84", "Y85", "Y86", "Y87", "Y88", "Y89", "Y90", "Y91", "Y92", "Y93", "Y94", "Y95", "Y96", "Y97", "Y98", "Y109", "Y110", "Y112", "Y115", "Y116", "Y117", "Y118", "Y119", "Y120", "Y121", "Y122", "Y124"
    ]
    assert capability_router["output_schema"]["runtime_preflight"] == "dict"
    assert capability_router["output_schema"]["crawl_efficiency_plan"] == "crawl_efficiency_plan.v1"
    assert capability_router["output_schema"]["agent_case_benchmark_matrix"] == "agent_case_benchmark_matrix.v1"
    assert capability_router["output_schema"]["agent_case_benchmark_result"] == "agent_case_benchmark_result.v1"
    assert capability_router["output_schema"]["agent_case_regression_report"] == "agent_case_regression_report.v1"
    assert capability_router["output_schema"]["run_evidence_bundle"] == "run_evidence_bundle.v1"
    assert capability_router["output_schema"]["browser_runtime_doctor_report"] == "browser_runtime_doctor_report.v1"
    assert capability_router["output_schema"]["efficiency_correlation_report"] == "efficiency_correlation_report.v1"
    assert capability_router["output_schema"]["efficiency_feedback_replay_report"] == "efficiency_feedback_replay_report.v1"
    assert capability_router["output_schema"]["efficiency_feedback_replay_reports"] == "list[efficiency_feedback_replay_report_summary.v1]"
    assert capability_router["output_schema"]["efficiency_feedback_replay_batch_report"] == "efficiency_feedback_replay_batch_report.v1"
    assert capability_router["output_schema"]["planner_feedback_shadow_report"] == "planner_feedback_shadow_report.v1"
    assert capability_router["output_schema"]["runtime_context"] == "dict"
    assert capability_router["output_schema"]["runtime_drift"] == "dict"
    assert capability_router["output_schema"]["runtime_issue_summary"] == "dict"
    assert capability_router["output_schema"]["planner_feedback"] == "planner_feedback.v1"
    assert capability_router["output_schema"]["failure_fixture_replay_report"] == "capability_failure_fixture_replay_report.v1"
    assert capability_router["output_schema"]["failure_fixture_replay_batch_report"] == "capability_failure_fixture_replay_batch_report.v1"
    assert capability_router["output_schema"]["failure_fixture_replay_batch_history_trend"] == "capability_failure_fixture_replay_batch_history_trend.v1"
    assert capability_router["output_schema"]["action_trace"] == "browser_action_trace.v1"
    assert capability_router["output_schema"]["action_issue_summary"] == "browser_action_issue_summary.v1"
    assert capability_router["output_schema"]["failure_bundle"] == "capability_execute_failure_bundle.v1"
    assert capability_router["output_schema"]["failure_regression_fixture"] == "capability_failure_regression_fixture.v1"
    assert "capability_execute.action_trace" in capability_router["success_signals"]
    assert "capability_execute.failed_action_trace" in capability_router["success_signals"]
    assert "capability_execute.action_issue_summary.status" in capability_router["success_signals"]
    assert "capability_execute.failure_bundle" in capability_router["success_signals"]
    assert "capability_execute.crawl_efficiency_plan" in capability_router["success_signals"]
    assert "capability_execute.efficiency_correlation_report" in capability_router["success_signals"]
    assert "crawl_efficiency.recommended_path" in capability_router["success_signals"]
    assert "crawl_efficiency.skip_browser" in capability_router["success_signals"]
    assert "crawl_efficiency.skip_vlm" in capability_router["success_signals"]
    assert "capability_trace.crawl_efficiency_ui" in capability_router["success_signals"]
    assert "capability_trace.crawl_efficiency_markdown_summary" in capability_router["success_signals"]
    assert "capability_trace.efficiency_correlation_ui" in capability_router["success_signals"]
    assert "capability_trace.efficiency_correlation_markdown_summary" in capability_router["success_signals"]
    assert "capability_router.planner_feedback" in capability_router["success_signals"]
    assert "execution_plan.planner_feedback" in capability_router["success_signals"]
    assert "capability_execute.error_detail_phase_event" in capability_router["success_signals"]
    assert "capability_execute.phase_event_contract" in capability_router["success_signals"]
    assert "capability_trace.artifact_replay" in capability_router["success_signals"]
    assert "capability_trace.failure_regression_fixture" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_api" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_ui" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_library" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_library_ui" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_replay" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_replay_api" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_replay_ui" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_replay_batch" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_replay_batch_ui" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_replay_batch_triage" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_replay_batch_markdown_summary" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_replay_batch_history" in capability_router["success_signals"]
    assert "capability_trace.failure_fixture_replay_batch_history_trend" in capability_router["success_signals"]
    assert "capability_trace.runtime_codes" in capability_router["success_signals"]
    assert "capability_trace.preflight_codes" in capability_router["success_signals"]
    assert "capability_trace.action_codes" in capability_router["success_signals"]
    assert "capability_trace.action_issue_summary" in capability_router["success_signals"]
    assert "capability_trace.action_failure_taxonomy" in capability_router["success_signals"]
    assert "capability_trace.action_recovery_actions" in capability_router["success_signals"]
    assert "capability_trace.error_detail_action_trace" in capability_router["success_signals"]
    assert "capability_trace.action_search" in capability_router["success_signals"]
    assert "capability_trace.health_alignment" in capability_router["success_signals"]
    assert "capability_trace.markdown_summary" in capability_router["success_signals"]
    assert "POST /api/capabilities/execute" in capability_router["endpoints"]
    assert "POST /api/capabilities/failure_fixture" in capability_router["endpoints"]
    assert "GET /api/capabilities/failure_fixtures" in capability_router["endpoints"]
    assert "POST /api/capabilities/failure_fixture/replay" in capability_router["endpoints"]
    assert "POST /api/capabilities/failure_fixture/replay_batch" in capability_router["endpoints"]
    assert "GET /api/capabilities/failure_fixture/replay_batches" in capability_router["endpoints"]
    assert "GET /api/capabilities/browser_runtime_doctor" in capability_router["endpoints"]
    assert "POST /api/capabilities/browser_runtime_doctor" in capability_router["endpoints"]
    assert "GET /api/capabilities/agent_case_regression" in capability_router["endpoints"]
    assert "POST /api/capabilities/agent_case_regression" in capability_router["endpoints"]
    assert crawl_efficiency is not None
    assert crawl_efficiency["milestones"] == ["Y108"]
    assert crawl_efficiency["layer"] == "intent_planning"
    assert crawl_efficiency["input_schema"]["browser_state"] == "browser_state.v2"
    assert crawl_efficiency["output_schema"]["plan"] == "crawl_efficiency_plan.v1"
    assert "crawl_efficiency.recommended_path" in crawl_efficiency["success_signals"]
    assert "crawl_efficiency.skip_browser" in crawl_efficiency["success_signals"]
    assert "api_replay" in crawl_efficiency["fallback_to"]
    assert any(item["name"] == "spider_lite" for item in data_plane)
    assert extractor is not None
    assert extractor["input_schema"]["source"] == "str|dict|list"
    assert "spider_lite" in extractor["fallback_to"]
    assert action_ref is not None
    assert action_ref["milestones"] == ["Y46"]
    assert "POST /api/action_refs/normalize" in action_ref["endpoints"]
    assert browser_backend is not None
    assert browser_backend["milestones"] == ["Y47", "Y48", "Y51", "Y52"]
    assert browser_backend["input_schema"]["VSPIDER_BROWSER_BACKEND"] == "playwright|remote_playwright"
    assert browser_backend["output_schema"]["health"] == "dict"
    assert "GET /api/browser_control/backend" in browser_backend["endpoints"]
    assert browser_pool is not None
    assert browser_pool["milestones"] == ["Y8", "Y49", "Y51", "Y52", "Y53", "Y55", "Y56"]
    assert browser_pool["output_schema"]["runtime"] == "browser_runtime.v1"
    assert browser_pool["output_schema"]["runtime_preflight"] == "browser_runtime_preflight.v1"
    assert browser_pool["output_schema"]["runtime_drift"] == "browser_runtime_drift.v1"
    assert browser_pool["output_schema"]["runtime_issue_summary"] == "browser_runtime_issue_summary.v1"
    assert "runtime_drift.status" in browser_pool["success_signals"]
    assert "runtime_issue_summary.status" in browser_pool["success_signals"]
    assert browser_runtime_doctor is not None
    assert browser_runtime_doctor["milestones"] == ["Y122"]
    assert browser_runtime_doctor["layer"] == "browser_substrate"
    assert browser_runtime_doctor["changes_state"] is False
    assert "GET /api/capabilities/browser_runtime_doctor" in browser_runtime_doctor["endpoints"]
    assert "POST /api/capabilities/browser_runtime_doctor" in browser_runtime_doctor["endpoints"]
    assert browser_runtime_doctor["input_schema"]["runtime_status"] == "browser_runtime.v1"
    assert browser_runtime_doctor["output_schema"]["report"] == "browser_runtime_doctor_report.v1"
    assert "browser_runtime_doctor.readiness" in browser_runtime_doctor["success_signals"]
    assert "browser_runtime_doctor.recovery_plan" in browser_runtime_doctor["success_signals"]
    assert universal_benchmark is not None
    assert universal_benchmark["milestones"] == ["Y105"]
    assert universal_benchmark["layer"] == "operations_plane"
    assert universal_benchmark["output_schema"]["matrix"] == "universal_benchmark_matrix.v1"
    assert universal_benchmark["output_schema"]["result"] == "universal_benchmark_result.v1"
    assert "coverage.coverage_gaps" in universal_benchmark["success_signals"]
    assert "result.weak_dimensions" in universal_benchmark["success_signals"]
    assert agent_case_benchmark is not None
    assert agent_case_benchmark["milestones"] == ["Y120"]
    assert agent_case_benchmark["layer"] == "operations_plane"
    assert agent_case_benchmark["changes_state"] is False
    assert "GET /api/capabilities/agent_case_benchmark" in agent_case_benchmark["endpoints"]
    assert "POST /api/capabilities/agent_case_benchmark" in agent_case_benchmark["endpoints"]
    assert agent_case_benchmark["output_schema"]["matrix"] == "agent_case_benchmark_matrix.v1"
    assert agent_case_benchmark["output_schema"]["result"] == "agent_case_benchmark_result.v1"
    assert "agent_case_benchmark.pass_rate" in agent_case_benchmark["success_signals"]
    assert "agent_case_benchmark.recommended_focus" in agent_case_benchmark["success_signals"]
    assert agent_case_regression is not None
    assert agent_case_regression["milestones"] == ["Y124"]
    assert agent_case_regression["layer"] == "operations_plane"
    assert agent_case_regression["changes_state"] is False
    assert "GET /api/capabilities/agent_case_regression" in agent_case_regression["endpoints"]
    assert "POST /api/capabilities/agent_case_regression" in agent_case_regression["endpoints"]
    assert agent_case_regression["output_schema"]["report"] == "agent_case_regression_report.v1"
    assert "agent_case_regression.gate.status" in agent_case_regression["success_signals"]
    assert "agent_case_regression.trend.direction" in agent_case_regression["success_signals"]
    assert run_evidence_bundle is not None
    assert run_evidence_bundle["milestones"] == ["Y121"]
    assert run_evidence_bundle["layer"] == "operations_plane"
    assert run_evidence_bundle["changes_state"] is False
    assert "POST /api/capabilities/run_evidence_bundle" in run_evidence_bundle["endpoints"]
    assert run_evidence_bundle["output_schema"]["bundle"] == "run_evidence_bundle.v1"
    assert "run_evidence_bundle.actions" in run_evidence_bundle["success_signals"]
    assert "run_evidence_bundle.debug" in run_evidence_bundle["success_signals"]
    assert deterministic_evaluator is not None
    assert deterministic_evaluator["milestones"] == ["Y107"]
    assert deterministic_evaluator["layer"] == "execution_kernel"
    assert deterministic_evaluator["output_schema"]["evaluation"] == "deterministic_evaluation.v1"
    assert deterministic_evaluator["input_schema"]["browser_state"] == "browser_state.v2"
    assert deterministic_evaluator["input_schema"]["action_reliability"] == "action_reliability_score.v1"
    assert "deterministic_evaluation.decision" in deterministic_evaluator["success_signals"]
    assert "deterministic_evaluation.recommended_actions" in deterministic_evaluator["success_signals"]
    assert efficiency_correlation is not None
    assert efficiency_correlation["milestones"] == ["Y111"]
    assert efficiency_correlation["layer"] == "execution_kernel"
    assert efficiency_correlation["input_schema"]["crawl_efficiency_plan"] == "crawl_efficiency_plan.v1"
    assert efficiency_correlation["input_schema"]["deterministic_evaluation"] == "deterministic_evaluation.v1"
    assert efficiency_correlation["input_schema"]["action_reliability"] == "action_reliability_score.v1"
    assert efficiency_correlation["output_schema"]["report"] == "efficiency_correlation_report.v1"
    assert "efficiency_correlation.alignment" in efficiency_correlation["success_signals"]
    assert "efficiency_correlation.planner_hints" in efficiency_correlation["success_signals"]
    assert efficiency_feedback is not None
    assert efficiency_feedback["milestones"] == ["Y113"]
    assert efficiency_feedback["layer"] == "execution_kernel"
    assert efficiency_feedback["input_schema"]["efficiency_correlation_report"] == "efficiency_correlation_report.v1"
    assert efficiency_feedback["output_schema"]["planner_feedback"] == "planner_feedback.v1"
    assert "efficiency_feedback.preferred_capabilities" in efficiency_feedback["success_signals"]
    assert "efficiency_feedback.avoid_actions" in efficiency_feedback["success_signals"]
    assert efficiency_feedback_replay is not None
    assert efficiency_feedback_replay["milestones"] == ["Y114", "Y115", "Y116", "Y117", "Y118"]
    assert efficiency_feedback_replay["layer"] == "execution_kernel"
    assert "POST /api/capabilities/efficiency_feedback/replay" in efficiency_feedback_replay["endpoints"]
    assert "GET /api/capabilities/efficiency_feedback/replays" in efficiency_feedback_replay["endpoints"]
    assert "POST /api/capabilities/efficiency_feedback/replay_batch" in efficiency_feedback_replay["endpoints"]
    assert efficiency_feedback_replay["input_schema"]["efficiency_correlation_report"] == "efficiency_correlation_report.v1"
    assert efficiency_feedback_replay["input_schema"]["planner_feedback"] == "planner_feedback.v1"
    assert efficiency_feedback_replay["output_schema"]["report"] == "efficiency_feedback_replay_report.v1"
    assert efficiency_feedback_replay["output_schema"]["reports"] == "list[efficiency_feedback_replay_report_summary.v1]"
    assert efficiency_feedback_replay["output_schema"]["batch_report"] == "efficiency_feedback_replay_batch_report.v1"
    assert "efficiency_feedback_replay.execution_plan" in efficiency_feedback_replay["success_signals"]
    assert "efficiency_feedback_replay.api" in efficiency_feedback_replay["success_signals"]
    assert "efficiency_feedback_replay.artifact_library" in efficiency_feedback_replay["success_signals"]
    assert "efficiency_feedback_replay.batch" in efficiency_feedback_replay["success_signals"]
    assert "efficiency_feedback_replay.batch_artifact" in efficiency_feedback_replay["success_signals"]
    assert "capability_trace.efficiency_feedback_replay_ui" in efficiency_feedback_replay["success_signals"]
    assert "capability_trace.efficiency_feedback_replay_library_ui" in efficiency_feedback_replay["success_signals"]
    assert planner_feedback_shadow is not None
    assert planner_feedback_shadow["milestones"] == ["Y119"]
    assert planner_feedback_shadow["layer"] == "execution_kernel"
    assert planner_feedback_shadow["changes_state"] is False
    assert "POST /api/capabilities/efficiency_feedback/shadow" in planner_feedback_shadow["endpoints"]
    assert planner_feedback_shadow["output_schema"]["report"] == "planner_feedback_shadow_report.v1"
    assert "planner_feedback_shadow.no_route_decision_change" in planner_feedback_shadow["success_signals"]
    assert "planner_feedback_shadow.feedback_step" in planner_feedback_shadow["success_signals"]
    assert browser_control is not None
    assert browser_control["milestones"] == ["Y18", "Y21", "Y69", "Y70", "Y72", "Y76", "Y82", "Y83", "Y104", "Y106"]
    assert browser_control["output_schema"]["browser_state"] == "browser_state.v2"
    assert browser_control["output_schema"]["action_trace"] == "browser_action_trace.v1"
    assert browser_control["output_schema"]["action_trace.issue_summary"] == "browser_action_issue_summary.v1"
    assert browser_control["output_schema"]["action_reliability"] == "action_reliability_score.v1"
    assert browser_control["output_schema"]["self_healing_policy"] == "self_healing_policy.v1"
    assert "POST /api/browser_control/click" in browser_control["endpoints"]
    assert "POST /api/browser_control/fill" in browser_control["endpoints"]
    assert "POST /api/browser_control/wait" in browser_control["endpoints"]
    assert "POST /api/browser_control/navigate" in browser_control["endpoints"]
    assert "POST /api/browser_control/screenshot" in browser_control["endpoints"]
    assert "wait" in browser_control["actions"]
    assert "navigate" in browser_control["actions"]
    assert "screenshot" in browser_control["actions"]
    assert "action_trace.status" in browser_control["success_signals"]
    assert "action_trace.error" in browser_control["success_signals"]
    assert "browser_state.version" in browser_control["success_signals"]
    assert "browser_state.metrics.interactive_count" in browser_control["success_signals"]
    assert "browser_state.perception.dom_shape" in browser_control["success_signals"]
    assert "action_reliability.score" in browser_control["success_signals"]
    assert "action_reliability.risk_level" in browser_control["success_signals"]
    assert "self_healing_policy.recommended_actions" in browser_control["success_signals"]
    assert "action_trace.result_summary.failure_code" in browser_control["success_signals"]
    assert "action_trace.result_summary.failure_category" in browser_control["success_signals"]
    assert "action_trace.result_summary.recovery_actions" in browser_control["success_signals"]
    assert "action_trace.action_ref.source" in browser_control["success_signals"]
    assert "action_trace.issue_summary.status" in browser_control["success_signals"]
    assert "action_trace.issue_summary.issue_count" in browser_control["success_signals"]
    assert artifact_manager is not None
    assert artifact_manager["layer"] == "operations_plane"


def test_action_ref_contract_normalizes_browser_som_selector_and_bbox_refs() -> None:
    schema = action_ref_schema()
    refs = normalize_action_refs([
        {"ref": "@e5", "role": "button", "name": "Submit"},
        {"target_id": 3, "bbox": {"x": 10, "y": 20, "width": 30, "height": 40}},
        {"selector": "#login", "element": {"tag": "input", "role": "textbox", "name": "User"}},
        {"point": [500, 250], "confidence": 0.6},
    ])
    summary = summarize_action_ref_sources(refs)

    assert schema["version"] == "action_ref.v1"
    assert refs[0]["source"] == "browser_ref"
    assert refs[0]["ax_id"] == "@e5"
    assert refs[1]["source"] == "som"
    assert refs[1]["som_id"] == 3
    assert refs[2]["source"] == "selector"
    assert refs[2]["selector"] == "#login"
    assert refs[3]["source"] == "point"
    assert refs[3]["point"] == [500.0, 250.0]
    assert summary["count"] == 4
    assert summary["stable_count"] >= 2
    assert normalize_action_ref("@e7")["ref"] == "@e7"


def test_browser_control_outputs_action_refs_for_snapshot_selector_and_similar() -> None:
    import asyncio

    manager = BrowserControlManager()
    manager.sessions["test"] = BrowserControlSession(session_id="test", page=_FakeBrowserControlPage())

    async def run_checks() -> tuple[dict, dict, dict]:
        snapshot = await manager.snapshot(session_id="test")
        selector = await manager.selector("@e1", session_id="test")
        similar = await manager.similar("@e1", session_id="test")
        return snapshot, selector, similar

    snapshot, selector, similar = asyncio.run(run_checks())

    assert snapshot["count"] == 2
    assert snapshot["items"][0]["ref"] == "@e1"
    assert snapshot["action_refs"][0]["version"] == "action_ref.v1"
    assert snapshot["action_refs"][0]["source"] == "browser_ref"
    assert snapshot["action_refs"][0]["selector"] == "#submit"
    assert snapshot["action_ref_summary"]["count"] == 2
    assert selector["selectors"]["css"] == "#submit"
    assert selector["action_ref"]["ref"] == "@e1"
    assert selector["action_ref"]["selectors"]["role"] == "role=button[name=\"Submit\"]"
    assert similar["count"] == 1
    assert similar["action_refs"][0]["ref"] == "@e3"
    assert similar["action_refs"][0]["confidence"] == 0.9
    assert similar["action_ref_summary"]["stable_count"] == 1


def test_execution_plan_contract_maps_route_to_steps_and_risks() -> None:
    route = route_task(
        "登录系统后填写表单并上传文件；如果出现验证码就人工处理",
        url="https://example.com/login",
    )
    plan = build_execution_plan(route)
    step_names = [item["capability"] for item in plan["steps"]]

    assert plan["version"] == "planner_contract.v1"
    assert plan["intent"]["task_type"] == "form_or_transaction"
    assert "browser_control" in step_names
    assert "human_guard" in step_names
    assert "auth_or_captcha_requires_guard" in plan["risk_flags"]
    assert any(item["domain"] == "example.com" for item in plan["systems"])


def test_planner_feedback_from_failure_bundle_guides_next_route_plan() -> None:
    failure_bundle = {
        "version": "capability_execute_failure_bundle.v1",
        "source": "capability_execute",
        "primary_failure": "selector_missing",
        "failure_category": "target_resolution",
        "capability": "browser_control",
        "action": "click",
        "fallback_reason": "button selector missing",
        "recommended_action": "refresh_snapshot_or_use_similar_selector",
        "recommended_actions": ["refresh_browser_snapshot", "use_similar_selector"],
        "action_trace": {
            "version": "browser_action_trace.v1",
            "target": {"ref": "@e1", "selector": "#submit"},
            "action_ref": {"ref": "@e1", "selector": "#submit"},
        },
    }

    feedback = planner_feedback_from_failure_bundle(failure_bundle)
    route = route_task(
        "继续填写表单并点击提交按钮",
        url="https://example.com/form",
        context={"failure_bundle": failure_bundle},
        runtime_status={"version": "browser_runtime.v1", "status": "available"},
    )
    plan = build_execution_plan(route)
    selector_step = next(item for item in plan["steps"] if item["capability"] == "selector_generator")

    assert feedback["version"] == "planner_feedback.v1"
    assert feedback["primary_failure"] == "selector_missing"
    assert feedback["target"]["selector"] == "#submit"
    assert feedback["preferred_capabilities"][:3] == ["browser_control_find", "selector_generator", "action_ref_normalizer"]
    assert "retry_same_action_ref_without_refresh" in feedback["avoid_actions"]
    assert route["signals"]["planner_feedback"] is True
    assert route["planner_feedback"]["primary_failure"] == "selector_missing"
    assert route["execution_plan"]["planner_feedback"]["primary_failure"] == "selector_missing"
    assert plan["planner_feedback"]["recommended_action"] == "refresh_snapshot_or_use_similar_selector"
    assert "previous_failure_feedback_active" in plan["risk_flags"]
    assert "previous_failure_selector_missing" in plan["risk_flags"]
    assert selector_step["inputs"]["planner_feedback"]["primary_failure"] == "selector_missing"
    assert selector_step["inputs"]["planner_feedback"]["target"]["selector"] == "#submit"
    assert any("Planner feedback from previous failure selector_missing" in item for item in plan["notes"])


def test_capability_failure_regression_fixture_from_trace_artifact(tmp_path: Path) -> None:
    failure_bundle = {
        "version": "capability_execute_failure_bundle.v1",
        "source": "capability_execute",
        "primary_failure": "click_intercepted",
        "failure_category": "element_state",
        "capability": "browser_control",
        "action": "click",
        "recommended_action": "close_overlay_or_try_alternate_click",
        "recommended_actions": ["close_overlay", "try_alternate_click"],
        "action_trace": {
            "version": "browser_action_trace.v1",
            "action": "click",
            "warning_codes": ["action_failed", "click_intercepted"],
            "target": {"ref": "@e1", "selector": "#submit"},
            "action_ref": {"ref": "@e1", "selector": "#submit"},
        },
        "action_issue_summary": {
            "version": "browser_action_issue_summary.v1",
            "status": "error",
            "issues": [{"source": "warning_codes", "code": "click_intercepted"}],
        },
    }
    artifact = {
        "type": "capability_execute_trace",
        "request": {"goal": "点击提交", "url": "https://example.com/form", "run_id": "failed/click"},
        "result": {
            "status": "error",
            "completed": False,
            "capability": "browser_control",
            "route": {"intent": {"task_type": "form_or_transaction"}},
            "failure_bundle": failure_bundle,
        },
    }

    fixture = build_capability_failure_regression_fixture(artifact, name="submit click intercepted", tags=["regression"])
    path = write_capability_failure_regression_fixture(artifact, tmp_path, name="submit click intercepted")
    loaded = json.loads(path.read_text(encoding="utf-8"))

    assert fixture["version"] == "capability_failure_regression_fixture.v1"
    assert fixture["source"] == "capability_execute_trace"
    assert fixture["name"] == "submit_click_intercepted"
    assert fixture["inputs"]["goal"] == "点击提交"
    assert fixture["inputs"]["route_intent"]["task_type"] == "form_or_transaction"
    assert fixture["expected"]["primary_failure"] == "click_intercepted"
    assert fixture["expected"]["failure_category"] == "element_state"
    assert fixture["expected"]["recommended_actions"] == ["close_overlay", "try_alternate_click"]
    assert fixture["replay"]["failure_bundle"]["primary_failure"] == "click_intercepted"
    assert {"path": "failure_bundle.primary_failure", "equals": "click_intercepted"} in fixture["assertions"]
    assert path.name == "submit_click_intercepted.json"
    assert loaded["version"] == "capability_failure_regression_fixture.v1"


def test_capability_failure_fixture_replay_validates_planner_feedback() -> None:
    failure_bundle = {
        "version": "capability_execute_failure_bundle.v1",
        "source": "capability_execute",
        "primary_failure": "selector_missing",
        "failure_category": "target_resolution",
        "capability": "browser_control",
        "action": "click",
        "recommended_action": "refresh_browser_snapshot",
        "recommended_actions": ["refresh_browser_snapshot", "use_similar_selector"],
        "action_trace": {
            "version": "browser_action_trace.v1",
            "action": "click",
            "warning_codes": ["action_failed", "selector_missing"],
            "target": {"ref": "@e1", "selector": "#submit"},
            "action_ref": {"ref": "@e1", "selector": "#submit"},
        },
        "action_issue_summary": {
            "version": "browser_action_issue_summary.v1",
            "status": "error",
            "issues": [{"source": "warning_codes", "code": "selector_missing"}],
        },
    }
    fixture = build_capability_failure_regression_fixture({
        "type": "capability_execute_trace",
        "request": {"goal": "继续填写表单并点击提交按钮", "url": "https://example.com/form"},
        "result": {
            "status": "error",
            "completed": False,
            "capability": "browser_control",
            "failure_bundle": failure_bundle,
        },
    }, name="selector missing replay")

    report = replay_capability_failure_fixture(fixture)
    checks = {item["name"]: item for item in report["checks"]}

    assert report["version"] == "capability_failure_fixture_replay_report.v1"
    assert report["passed"] is True
    assert report["fixture"]["primary_failure"] == "selector_missing"
    assert report["planner_feedback"]["passed"] is True
    assert report["planner_feedback"]["preferred_capabilities"][:3] == ["browser_control_find", "selector_generator", "action_ref_normalizer"]
    assert "retry_same_action_ref_without_refresh" in report["planner_feedback"]["avoid_actions"]
    assert report["route"]["passed"] is True
    assert report["route"]["signals"]["planner_feedback"] is True
    assert report["execution_plan"]["passed"] is True
    assert "previous_failure_feedback_active" in report["execution_plan"]["risk_flags"]
    assert "previous_failure_selector_missing" in report["execution_plan"]["risk_flags"]
    assert checks["execution_plan.notes.previous_failure"]["passed"] is True


def test_capability_failure_fixture_batch_replay_summarizes_reports() -> None:
    failure_bundle = {
        "version": "capability_execute_failure_bundle.v1",
        "source": "capability_execute",
        "primary_failure": "selector_missing",
        "failure_category": "target_resolution",
        "capability": "browser_control",
        "action": "click",
        "recommended_actions": ["refresh_browser_snapshot", "use_similar_selector"],
        "action_trace": {
            "version": "browser_action_trace.v1",
            "action": "click",
            "warning_codes": ["action_failed", "selector_missing"],
            "target": {"selector": "#submit"},
        },
        "action_issue_summary": {
            "version": "browser_action_issue_summary.v1",
            "status": "error",
            "issues": [{"source": "warning_codes", "code": "selector_missing"}],
        },
    }
    fixture = build_capability_failure_regression_fixture({
        "type": "capability_execute_trace",
        "request": {"goal": "点击提交", "url": "https://example.com/form"},
        "result": {"status": "error", "completed": False, "capability": "browser_control", "failure_bundle": failure_bundle},
    }, name="selector missing batch")

    report = replay_capability_failure_fixtures([fixture, {"version": "invalid"}])

    assert report["version"] == "capability_failure_fixture_replay_batch_report.v1"
    assert report["fixture_count"] == 2
    assert report["passed_count"] == 1
    assert report["failed_count"] == 1
    assert report["passed"] is False
    assert report["summary"]["status"] == "failed"
    assert report["summary"]["blocking"] is True
    assert report["summary"]["top_primary_failures"][0]["name"] == "selector_missing"
    assert report["summary"]["top_failure_categories"][0]["name"] == "target_resolution"
    assert report["summary"]["top_actions"][0]["name"] == "click"
    assert report["summary"]["top_capabilities"][0]["name"] == "browser_control"
    assert "primary_failure=selector_missing" in report["summary"]["recommended_focus"]
    assert report["items"][0]["passed"] is True
    assert report["items"][0]["primary_failure"] == "selector_missing"
    assert report["items"][0]["action"] == "click"
    assert report["items"][1]["passed"] is False
    assert any(item["name"] == "fixture.version" for item in report["failed_checks"])


def test_efficiency_feedback_replay_validates_planner_feedback_contract() -> None:
    report = replay_efficiency_feedback({
        "goal": "通过接口回放提取商品列表",
        "url": "https://example.com/products",
        "efficiency_correlation_report": {
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
        },
    })

    assert report["version"] == "efficiency_feedback_replay_report.v1"
    assert report["passed"] is True
    assert report["planner_feedback"]["feedback_type"] == "efficiency_correlation"
    assert report["route"]["signals"]["planner_feedback"] is True
    assert "previous_failure_feedback_active" in report["execution_plan"]["risk_flags"]
    assert report["execution_plan"]["feedback_step"]["inputs"]["planner_feedback"]["recommended_action"] == "prefer_api_replay_before_browser_action"


def test_efficiency_feedback_replay_api_accepts_multiple_input_shapes_and_optionally_saves(monkeypatch) -> None:
    import api_server

    temp_root = Path(__file__).resolve().parent / ".tmp_capability_router" / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=True)
    efficiency_report = {
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
    }
    planner_feedback = replay_efficiency_feedback({
        "goal": "提取商品列表",
        "url": "https://example.com/products",
        "efficiency_correlation_report": efficiency_report,
    })["planner_feedback"]

    try:
        monkeypatch.setattr(api_server, "resolve_artifact_path", lambda filename, subdir="": temp_root / subdir / filename)
        monkeypatch.setattr(api_server, "register_artifact", lambda path: None)
        monkeypatch.setattr(api_server, "artifact_url", lambda path: "/download/" + Path(path).name)
        import capability_artifact_persistence as _cap_persist
        monkeypatch.setattr(_cap_persist, "resolve_artifact_path", lambda filename, subdir="": temp_root / subdir / filename)
        monkeypatch.setattr(_cap_persist, "register_artifact", lambda path: None)
        monkeypatch.setattr(_cap_persist, "artifact_url", lambda path: "/download/" + Path(path).name)
        monkeypatch.setattr(_cap_persist, "artifact_root", lambda: temp_root)

        client = TestClient(api_server.app)
        direct_resp = client.post(
            "/api/capabilities/efficiency_feedback/replay",
            json={
                "goal": "通过接口回放提取商品列表",
                "url": "https://example.com/products",
                "efficiency_correlation_report": efficiency_report,
            },
        )
        wrapped_resp = client.post(
            "/api/capabilities/efficiency_feedback/replay",
            json={
                "source": {
                    "type": "capability_execute_trace",
                    "request": {"goal": "通过接口回放提取商品列表", "url": "https://example.com/products"},
                    "result": {"efficiency_correlation_report": efficiency_report},
                }
            },
        )
        feedback_resp = client.post(
            "/api/capabilities/efficiency_feedback/replay",
            json={"planner_feedback": planner_feedback, "goal": "提取商品列表", "url": "https://example.com/products"},
        )
        shadow_resp = client.post(
            "/api/capabilities/efficiency_feedback/shadow",
            json={"efficiency_correlation_report": efficiency_report, "goal": "提取商品列表", "url": "https://example.com/products"},
        )
        saved_resp = client.post(
            "/api/capabilities/efficiency_feedback/replay",
            json={
                "efficiency_correlation_report": efficiency_report,
                "goal": "通过接口回放提取商品列表",
                "url": "https://example.com/products",
                "name": "efficiency replay",
                "save": True,
            },
        )
        list_resp = client.get("/api/capabilities/efficiency_feedback/replays?limit=10")
        limited_resp = client.get("/api/capabilities/efficiency_feedback/replays?limit=0")
        batch_direct_resp = client.post(
            "/api/capabilities/efficiency_feedback/replay_batch",
            json={
                "sources": [
                    {"efficiency_correlation_report": efficiency_report, "goal": "通过接口回放提取商品列表", "url": "https://example.com/products"},
                    {
                        "source": {
                            "efficiency_correlation_report": {
                                "version": "efficiency_correlation_report.v1",
                                "status": "aligned",
                                "failed_checks": [],
                                "alignment": {"recommended_path": "api_replay", "executed_path": "api_replay"},
                            }
                        }
                    },
                ],
            },
        )
        batch_saved_resp = client.post(
            "/api/capabilities/efficiency_feedback/replay_batch",
            json={"limit": 10, "name": "efficiency replay batch", "save": True},
        )
        invalid_resp = client.post("/api/capabilities/efficiency_feedback/replay", json={"source": {"status": "ok"}})

        direct = direct_resp.json()["result"]
        wrapped = wrapped_resp.json()["result"]
        feedback = feedback_resp.json()["result"]
        shadow = shadow_resp.json()["result"]
        saved = saved_resp.json()["result"]
        listed = list_resp.json()["result"]
        limited = limited_resp.json()["result"]
        batch_direct = batch_direct_resp.json()["result"]
        batch_saved = batch_saved_resp.json()["result"]
        saved_doc = json.loads(Path(saved["artifact"]["path"]).read_text(encoding="utf-8"))
        saved_batch_doc = json.loads(Path(batch_saved["artifact"]["path"]).read_text(encoding="utf-8"))

        assert direct_resp.status_code == 200
        assert direct["report"]["version"] == "efficiency_feedback_replay_report.v1"
        assert direct["report"]["passed"] is True
        assert direct["report"]["planner_feedback"]["recommended_action"] == "prefer_api_replay_before_browser_action"
        assert direct["artifact"] is None
        assert wrapped_resp.status_code == 200
        assert wrapped["report"]["route"]["signals"]["planner_feedback"] is True
        assert feedback_resp.status_code == 200
        assert feedback["report"]["planner_feedback"]["version"] == "planner_feedback.v1"
        assert shadow_resp.status_code == 200
        assert shadow["report"]["version"] == "planner_feedback_shadow_report.v1"
        assert shadow["report"]["passed"] is True
        assert shadow["report"]["delta"]["backend_plan_changed"] is False
        assert shadow["report"]["delta"]["fallback_chain_changed"] is False
        assert "planner_feedback" in shadow["report"]["delta"]["signals_added"]
        assert "previous_failure_feedback_active" in shadow["report"]["delta"]["risk_flags_added"]
        assert saved_resp.status_code == 200
        assert saved["artifact"]["path"].endswith(".json")
        assert saved["artifact"]["url"].startswith("/download/")
        assert saved_doc["version"] == "efficiency_feedback_replay_report.v1"
        assert saved_doc["passed"] is True
        assert list_resp.status_code == 200
        assert listed["report_count"] == 1
        assert listed["reports"][0]["path"] == saved["artifact"]["path"]
        assert listed["reports"][0]["url"].startswith("/download/")
        assert listed["reports"][0]["passed"] is True
        assert listed["reports"][0]["primary_failure"] == "executed_path_more_expensive_than_recommendation"
        assert listed["reports"][0]["recommended_action"] == "prefer_api_replay_before_browser_action"
        assert listed["reports"][0]["preferred_capabilities"] == ["network_intelligence", "api_replay"]
        assert "open_browser_when_deterministic_path_available" in listed["reports"][0]["avoid_actions"]
        assert listed["reports"][0]["failed_check_count"] == 0
        assert listed["reports"][0]["feedback_step"] in {"network_intelligence", "api_replay"}
        assert limited_resp.status_code == 200
        assert limited["report_count"] == 0
        assert limited["reports"] == []
        assert batch_direct_resp.status_code == 200
        assert batch_direct["report"]["version"] == "efficiency_feedback_replay_batch_report.v1"
        assert batch_direct["report"]["source_count"] == 2
        assert batch_direct["report"]["passed_count"] == 1
        assert batch_direct["report"]["failed_count"] == 1
        assert batch_direct["report"]["summary"]["status"] == "failed"
        assert batch_direct["report"]["summary"]["top_primary_failures"][0]["name"] == "executed_path_more_expensive_than_recommendation"
        assert any(item["name"] == "planner_feedback.version" for item in batch_direct["report"]["failed_checks"])
        assert batch_direct["artifact"] is None
        assert batch_saved_resp.status_code == 200
        assert batch_saved["report"]["version"] == "efficiency_feedback_replay_batch_report.v1"
        assert batch_saved["report"]["source_count"] == 1
        assert batch_saved["report"]["passed"] is True
        assert batch_saved["report"]["items"][0]["artifact"]["path"] == saved["artifact"]["path"]
        assert batch_saved["artifact"]["path"].endswith(".json")
        assert batch_saved["artifact"]["url"].startswith("/download/")
        assert saved_batch_doc["version"] == "efficiency_feedback_replay_batch_report.v1"
        assert saved_batch_doc["passed"] is True
        assert invalid_resp.status_code == 400
        assert invalid_resp.json()["detail"] == "source does not contain efficiency_correlation_report.v1 or efficiency planner_feedback.v1"
    finally:
        shutil.rmtree(temp_root.parent, ignore_errors=True)


def test_capability_failure_fixture_api_builds_and_optionally_saves(monkeypatch) -> None:
    import api_server

    temp_root = Path(__file__).resolve().parent / ".tmp_capability_router" / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=True)
    failure_bundle = {
        "version": "capability_execute_failure_bundle.v1",
        "source": "capability_execute",
        "primary_failure": "selector_missing",
        "failure_category": "target_resolution",
        "capability": "browser_control",
        "action": "click",
        "recommended_action": "refresh_browser_snapshot",
        "recommended_actions": ["refresh_browser_snapshot", "use_similar_selector"],
        "action_trace": {
            "version": "browser_action_trace.v1",
            "action": "click",
            "warning_codes": ["action_failed", "selector_missing"],
            "target": {"ref": "@e1", "selector": "#submit"},
        },
        "action_issue_summary": {
            "version": "browser_action_issue_summary.v1",
            "status": "error",
            "issues": [{"source": "warning_codes", "code": "selector_missing"}],
        },
    }
    source = {
        "type": "capability_execute_trace",
        "request": {"goal": "点击提交", "url": "https://example.com/form", "run_id": "failed/selector"},
        "result": {"status": "error", "completed": False, "capability": "browser_control", "failure_bundle": failure_bundle},
    }

    try:
        monkeypatch.setattr(api_server, "resolve_artifact_path", lambda filename, subdir="": temp_root / subdir / filename)
        monkeypatch.setattr(api_server, "register_artifact", lambda path: None)
        monkeypatch.setattr(api_server, "artifact_url", lambda path: "/download/" + Path(path).name)

        client = TestClient(api_server.app)
        resp = client.post(
            "/api/capabilities/failure_fixture",
            json={"source": source, "name": "selector failure", "tags": ["regression"]},
        )
        saved_resp = client.post(
            "/api/capabilities/failure_fixture",
            json={"source": source, "name": "selector failure", "save": True},
        )
        invalid_resp = client.post("/api/capabilities/failure_fixture", json={"source": {"status": "ok"}})

        result = resp.json()["result"]
        saved = saved_resp.json()["result"]
        saved_doc = json.loads(Path(saved["artifact"]["path"]).read_text(encoding="utf-8"))

        assert resp.status_code == 200
        assert result["fixture"]["version"] == "capability_failure_regression_fixture.v1"
        assert result["fixture"]["expected"]["primary_failure"] == "selector_missing"
        assert result["artifact"] is None
        assert saved_resp.status_code == 200
        assert saved["artifact"]["path"].endswith(".json")
        assert saved["artifact"]["url"].startswith("/download/")
        assert saved_doc["expected"]["primary_failure"] == "selector_missing"
        assert invalid_resp.status_code == 400
        assert invalid_resp.json()["detail"] == "source does not contain capability_execute_failure_bundle.v1"
    finally:
        shutil.rmtree(temp_root.parent, ignore_errors=True)


def test_capability_failure_fixture_replay_api_validates_and_optionally_saves(monkeypatch) -> None:
    import api_server

    temp_root = Path(__file__).resolve().parent / ".tmp_capability_router" / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=True)
    failure_bundle = {
        "version": "capability_execute_failure_bundle.v1",
        "source": "capability_execute",
        "primary_failure": "selector_missing",
        "failure_category": "target_resolution",
        "capability": "browser_control",
        "action": "click",
        "recommended_action": "refresh_browser_snapshot",
        "recommended_actions": ["refresh_browser_snapshot", "use_similar_selector"],
        "action_trace": {
            "version": "browser_action_trace.v1",
            "action": "click",
            "warning_codes": ["action_failed", "selector_missing"],
            "target": {"ref": "@e1", "selector": "#submit"},
            "action_ref": {"ref": "@e1", "selector": "#submit"},
        },
        "action_issue_summary": {
            "version": "browser_action_issue_summary.v1",
            "status": "error",
            "issues": [{"source": "warning_codes", "code": "selector_missing"}],
        },
    }
    fixture = build_capability_failure_regression_fixture({
        "type": "capability_execute_trace",
        "request": {"goal": "点击提交", "url": "https://example.com/form", "run_id": "failed/selector"},
        "result": {"status": "error", "completed": False, "capability": "browser_control", "failure_bundle": failure_bundle},
    }, name="selector missing replay")

    try:
        monkeypatch.setattr(api_server, "resolve_artifact_path", lambda filename, subdir="": temp_root / subdir / filename)
        monkeypatch.setattr(api_server, "register_artifact", lambda path: None)
        monkeypatch.setattr(api_server, "artifact_url", lambda path: "/download/" + Path(path).name)

        client = TestClient(api_server.app)
        resp = client.post(
            "/api/capabilities/failure_fixture/replay",
            json={"fixture": fixture},
        )
        saved_resp = client.post(
            "/api/capabilities/failure_fixture/replay",
            json={"fixture": fixture, "name": "selector replay", "save": True},
        )
        invalid_resp = client.post("/api/capabilities/failure_fixture/replay", json={"source": {"status": "ok"}})

        result = resp.json()["result"]
        saved = saved_resp.json()["result"]
        saved_doc = json.loads(Path(saved["artifact"]["path"]).read_text(encoding="utf-8"))

        assert resp.status_code == 200
        assert result["report"]["version"] == "capability_failure_fixture_replay_report.v1"
        assert result["report"]["passed"] is True
        assert result["report"]["planner_feedback"]["primary_failure"] == "selector_missing"
        assert result["artifact"] is None
        assert saved_resp.status_code == 200
        assert saved["artifact"]["path"].endswith(".json")
        assert saved["artifact"]["url"].startswith("/download/")
        assert saved_doc["version"] == "capability_failure_fixture_replay_report.v1"
        assert saved_doc["passed"] is True
        assert invalid_resp.status_code == 400
        assert invalid_resp.json()["detail"] == "source does not contain capability_failure_regression_fixture.v1"
    finally:
        shutil.rmtree(temp_root.parent, ignore_errors=True)


def test_capability_failure_fixture_library_and_batch_replay_api(monkeypatch) -> None:
    import api_server

    temp_root = Path(__file__).resolve().parent / ".tmp_capability_router" / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=True)
    failure_bundle = {
        "version": "capability_execute_failure_bundle.v1",
        "source": "capability_execute",
        "primary_failure": "selector_missing",
        "failure_category": "target_resolution",
        "capability": "browser_control",
        "action": "click",
        "recommended_actions": ["refresh_browser_snapshot", "use_similar_selector"],
        "action_trace": {
            "version": "browser_action_trace.v1",
            "action": "click",
            "warning_codes": ["action_failed", "selector_missing"],
            "target": {"selector": "#submit"},
            "action_ref": {"selector": "#submit"},
        },
        "action_issue_summary": {
            "version": "browser_action_issue_summary.v1",
            "status": "error",
            "issues": [{"source": "warning_codes", "code": "selector_missing"}],
        },
    }
    fixture = build_capability_failure_regression_fixture({
        "type": "capability_execute_trace",
        "request": {"goal": "点击提交", "url": "https://example.com/form", "run_id": "failed/selector"},
        "result": {"status": "error", "completed": False, "capability": "browser_control", "failure_bundle": failure_bundle},
    }, name="selector missing library", tags=["regression"])

    try:
        monkeypatch.setattr(api_server, "resolve_artifact_path", lambda filename, subdir="": temp_root / subdir / filename)
        monkeypatch.setattr(api_server, "register_artifact", lambda path: None)
        monkeypatch.setattr(api_server, "artifact_url", lambda path: "/download/" + Path(path).name)
        import capability_artifact_persistence as _cap_persist
        monkeypatch.setattr(_cap_persist, "resolve_artifact_path", lambda filename, subdir="": temp_root / subdir / filename)
        monkeypatch.setattr(_cap_persist, "register_artifact", lambda path: None)
        monkeypatch.setattr(_cap_persist, "artifact_url", lambda path: "/download/" + Path(path).name)
        monkeypatch.setattr(_cap_persist, "artifact_root", lambda: temp_root)

        fixture_dir = temp_root / "capability" / "failure_fixtures"
        fixture_dir.mkdir(parents=True, exist_ok=True)
        fixture_path = fixture_dir / "selector_missing_library.json"
        fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        (fixture_dir / "invalid.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")

        client = TestClient(api_server.app)
        list_resp = client.get("/api/capabilities/failure_fixtures")
        batch_resp = client.post(
            "/api/capabilities/failure_fixture/replay_batch",
            json={"limit": 10, "name": "selector replay batch", "save": True},
        )
        failed_batch_resp = client.post(
            "/api/capabilities/failure_fixture/replay_batch",
            json={"fixtures": [{"version": "invalid"}], "name": "selector replay batch failed", "save": True},
        )
        direct_resp = client.post(
            "/api/capabilities/failure_fixture/replay_batch",
            json={"fixtures": [fixture, {"version": "invalid"}]},
        )

        listed = list_resp.json()["result"]
        batch = batch_resp.json()["result"]
        failed_batch = failed_batch_resp.json()["result"]
        direct = direct_resp.json()["result"]
        saved_doc = json.loads(Path(batch["artifact"]["path"]).read_text(encoding="utf-8"))
        os.utime(batch["artifact"]["path"], (1000, 1000))
        os.utime(failed_batch["artifact"]["path"], (2000, 2000))
        history_resp = client.get("/api/capabilities/failure_fixture/replay_batches")
        history = history_resp.json()["result"]

        assert list_resp.status_code == 200
        assert listed["fixture_count"] == 1
        assert listed["fixtures"][0]["name"] == "selector_missing_library"
        assert listed["fixtures"][0]["primary_failure"] == "selector_missing"
        assert listed["fixtures"][0]["url"].startswith("/download/")
        assert batch_resp.status_code == 200
        assert batch["report"]["version"] == "capability_failure_fixture_replay_batch_report.v1"
        assert batch["report"]["fixture_count"] == 1
        assert batch["report"]["passed_count"] == 1
        assert batch["report"]["summary"]["status"] == "passed"
        assert batch["report"]["summary"]["top_primary_failures"][0]["name"] == "selector_missing"
        assert batch["report"]["items"][0]["artifact"]["path"] == str(fixture_path)
        assert batch["artifact"]["url"].startswith("/download/")
        assert saved_doc["version"] == "capability_failure_fixture_replay_batch_report.v1"
        assert failed_batch_resp.status_code == 200
        assert failed_batch["report"]["failed_count"] == 1
        assert history_resp.status_code == 200
        assert history["report_count"] == 2
        assert history["reports"][0]["status"] == "failed"
        assert history["reports"][1]["status"] == "passed"
        assert history["reports"][0]["fixture_count"] == 1
        assert history["reports"][0]["recommended_focus"]
        assert history["reports"][0]["url"].startswith("/download/")
        assert history["trend"]["version"] == "capability_failure_fixture_replay_batch_history_trend.v1"
        assert history["trend"]["direction"] == "regressed"
        assert history["trend"]["failed_count_delta"] == 1
        assert history["trend"]["latest_pass_rate"] == 0.0
        assert history["trend"]["previous_pass_rate"] == 1.0
        assert history["trend"]["pass_rate_delta"] == -1.0
        assert history["trend"]["focus_changed"] is True
        assert direct_resp.status_code == 200
        assert direct["report"]["fixture_count"] == 2
        assert direct["report"]["failed_count"] == 1
        assert direct["report"]["summary"]["recommended_focus"]
        assert any(item["name"] == "fixture.version" for item in direct["report"]["failed_checks"])
    finally:
        shutil.rmtree(temp_root.parent, ignore_errors=True)


def test_workflow_graph_contract_maps_plan_to_system_nodes_and_edges() -> None:
    route = route_task(
        "从 https://a.example/list 抓取前 3 条数据，然后打开 https://b.example/form 填写表单并导出证据",
        url="https://a.example/list",
    )
    graph = build_workflow_graph(route)
    node_names = [item["capability"] for item in graph["nodes"]]

    assert graph["version"] == "workflow_graph.v1"
    assert len(graph["systems"]) >= 2
    assert len(graph["sessions"]) == len(graph["systems"])
    assert len(graph["nodes"]) == len(route["execution_plan"]["steps"])
    assert len(graph["data_edges"]) == max(0, len(graph["nodes"]) - 1)
    assert "generic_extractor" in node_names
    assert any(edge["contract"]["target_capability"] for edge in graph["data_edges"])
    # E1: two distinct URLs must now produce nodes attributed to >1 system
    # so the cross_system risk flag actually fires.
    node_system_ids = {item["system_id"] for item in graph["nodes"]}
    assert len(node_system_ids) >= 2
    assert "cross_system" in graph["risk_flags"]


def test_capability_router_interaction_task_prefers_macros_and_browser_control() -> None:
    runtime_status = {
        "version": "browser_runtime.v1",
        "status": "available",
        "capacity": {"max_contexts": 2, "available_contexts": 1, "active_contexts": 1},
        "backend_summary": {
            "active_name": "playwright_chromium",
            "health_status": "healthy",
            "health_cache_hit": True,
            "health_cache_stale": False,
        },
    }
    route = route_task(
        "打开表单，填写用户名和密码，点击提交；如果找不到按钮就用相似元素或选择器兜底",
        url="https://example.com/form",
        runtime_status=runtime_status,
    )
    names = _plan_names(route)
    fallback = _fallback_names(route)
    selected = [item["name"] for item in route["selected_agent_tools"]]

    assert route["intent"]["task_type"] == "form_or_transaction"
    assert route["runtime_preflight"]["version"] == "browser_runtime_preflight.v1"
    assert route["runtime_preflight"]["status"] == "pass"
    assert route["runtime_preflight"]["blocking"] is False
    assert route["runtime_preflight"]["recommended_action"] == "continue"
    assert "auto_form_fill" in selected
    assert "browser_pool" in names
    assert next(item for item in route["backend_plan"] if item["name"] == "browser_pool")["milestone"] == "Y8/Y49/Y51/Y52/Y53"
    assert "browser_backend_abstraction" in names
    assert next(item for item in route["backend_plan"] if item["name"] == "browser_backend_abstraction")["milestone"] == "Y47/Y48/Y51/Y52"
    assert "browser_control" in names
    assert "action_ref_normalizer" in names
    assert "selector_generator" in names
    assert "action_registry_macros" in names
    assert "browser_control_find" in fallback
    assert "browser_backend_abstraction" in fallback
    assert "action_ref_normalizer" in fallback
    assert "selector_generator" in fallback
    assert route["action_ref_schema"]["version"] == "action_ref.v1"
    assert route["model_roles"]["vision_model"]["recommended_use"] == "required"
    browser_pool_step = next(item for item in route["execution_plan"]["steps"] if item["capability"] == "browser_pool")
    assert browser_pool_step["inputs"]["runtime_preflight"]["status"] == "pass"
    assert "runtime_snapshot" not in browser_pool_step["inputs"]["runtime_preflight"]


def test_capability_router_runtime_preflight_warns_on_pool_exhaustion() -> None:
    route = route_task(
        "打开浏览器点击提交按钮",
        url="https://example.com/form",
        runtime_status={
            "version": "browser_runtime.v1",
            "status": "pool_exhausted",
            "capacity": {"max_contexts": 1, "available_contexts": 0, "active_contexts": 1},
            "backend_summary": {
                "active_name": "playwright_chromium",
                "health_status": "healthy",
            },
        },
    )

    preflight = route["runtime_preflight"]

    assert preflight["status"] == "warn"
    assert preflight["blocking"] is False
    assert preflight["recommended_action"] == "queue_or_wait_for_browser_context"
    assert "browser_pool_exhausted" in preflight["warnings"]


def test_capability_router_ops_task_keeps_queue_and_runtime_guards() -> None:
    route = route_task("查看任务队列指标，如果 worker stale 就触发 watchdog 恢复并支持重试")
    names = _plan_names(route)

    assert route["intent"]["task_type"] == "operations"
    assert "task_queue" in names
    assert "run_registry" in names
    assert any(item["owner"] == "runtime_guards" for item in route["backend_plan"])


def test_model_role_report_places_semantic_and_vision_models_separately() -> None:
    report = model_role_report()

    assert "decompose ambiguous goals" in report["semantic_model"]["responsibilities"]
    assert "ground actions in screenshot plus AX tree" in report["vision_model"]["responsibilities"]
    assert "global capability routing" in report["vision_model"]["should_not_do"]
    assert "choose fallback order" in report["deterministic_router"]["responsibilities"]


def test_user_message_injects_capability_route_guidance() -> None:
    route = route_task(
        "爬取所有分页商品列表，字段: title, price，导出 JSONL",
        url="https://shop.example/list",
    )
    message = build_user_message(
        goal="爬取所有分页商品列表，字段: title, price，导出 JSONL",
        step=1,
        max_steps=10,
        capability_route=route,
    )

    assert "Capability Route Guidance" in message
    assert "推荐优先能力" in message
    assert "generic_extractor" in message
    assert "spider_lite" in message
    assert "兜底顺序" in message
    assert "视觉模型职责" in message
    assert "不要凭空发明新 action" in message


def test_route_executor_completes_from_selector_source_without_network(_artifact_tmp) -> None:
    result = execute_route({
        "goal": "提取前 2 条 quote 文本",
        "source": """
        <html><body>
          <div class="quote"><span class="text">Alpha</span></div>
          <div class="quote"><span class="text">Beta</span></div>
        </body></html>
        """,
        "selector": ".quote .text::text",
        "export": True,
        "run_id": "route_selector_source",
    })

    assert result["status"] == "completed"
    assert result["capability"] == "extractor_select"
    assert result["result"]["results"] == ["Alpha", "Beta"]
    assert any(item["capability"] == "extractor_select" for item in result["attempts"])


def test_route_executor_skips_network_without_allow_network() -> None:
    result = execute_route({
        "goal": "爬取列表数据并导出",
        "url": "https://example.com/",
        "extract": {"selector": ".quote .text::text"},
    })

    assert result["status"] == "skipped"
    assert result["completed"] is False
    assert any(item["capability"] == "spider_lite" and item["status"] == "skipped" for item in result["attempts"])


def test_route_executor_runs_spider_when_network_allowed(_artifact_tmp) -> None:
    pages = {
        "https://example.com/": '<div class="quote"><span class="text">Alpha</span></div>',
    }

    def fetch(url: str) -> FetchResult:
        return FetchResult(url=url, status_code=200, html=pages[url])

    manager = SpiderLiteManager(fetcher=fetch)
    result = execute_route({
        "goal": "爬取 1 条 quote 文本",
        "url": "https://example.com/",
        "allow_network": True,
        "extract": {"selector": ".quote .text::text"},
        "run_id": "route_spider",
        "export": True,
    }, spider_lite=manager)

    assert result["status"] == "completed"
    assert result["capability"] == "spider_lite"
    assert result["result"]["item_count"] == 1
    assert result["result"]["items"][0]["value"] == "Alpha"


def test_route_executor_uses_target_pages_as_spider_max_pages() -> None:
    class RecordingSpider:
        def __init__(self) -> None:
            self.payload = {}

        def run(self, payload: dict) -> dict:
            self.payload = dict(payload)
            return {
                "status": "success",
                "page_count": payload["max_pages"],
                "item_count": 1,
                "items": [{"value": "Alpha"}],
                "artifact": None,
            }

    spider = RecordingSpider()
    result = execute_route({
        "goal": "extract first 2 pages of quote text",
        "url": "https://example.com/",
        "allow_network": True,
        "extract": {"selector": ".quote .text::text"},
        "run_id": "route_spider_pages",
    }, spider_lite=spider)

    attempt = next(item for item in result["attempts"] if item["capability"] == "spider_lite")
    assert spider.payload["max_pages"] == 2
    assert attempt["target_pages"] == 2
    assert attempt["page_count"] == 2


def test_route_executor_prefers_api_replay_candidate_before_spider(monkeypatch, _artifact_tmp) -> None:
    from visual_web_agent import api_replay as api_replay_mod

    monkeypatch.setattr(api_replay_mod, "resolve_artifact_path", lambda filename, subdir="": _artifact_tmp / subdir / filename)
    monkeypatch.setattr(api_replay_mod, "register_artifact", lambda path: None)
    monkeypatch.setattr(api_replay_mod, "artifact_url", lambda path: "/download/" + Path(path).name)

    class RecordingSpider:
        called = False

        def run(self, payload: dict) -> dict:
            self.called = True
            return {"status": "success", "page_count": 1, "item_count": 1, "items": [{"value": "spider"}]}

    spider = RecordingSpider()

    def fake_fetcher(url: str, headers: dict, timeout_s: float, method: str, body: str):
        assert url == "https://api.example.com/items?page=1&limit=2"
        return 200, {"content-type": "application/json"}, json.dumps({
            "data": [
                {"id": 1, "title": "A"},
                {"id": 2, "title": "B"},
            ]
        })

    result = execute_route({
        "goal": "Extract top 2 records from API",
        "url": "https://example.com/list",
        "allow_network": True,
        "network_candidates": [
            {"endpoint": "https://api.example.com/items?page=*&limit=*", "method": "GET", "score": 9},
        ],
        "api_replay_fetcher": fake_fetcher,
        "run_id": "route_api_replay",
    }, spider_lite=spider)

    attempt = next(item for item in result["attempts"] if item["capability"] == "api_replay")
    assert result["status"] == "completed"
    assert result["capability"] == "api_replay"
    assert result["result"]["row_count"] == 2
    assert attempt["target_count"] == 2
    assert spider.called is False


def test_success_verifier_rejects_missing_required_fields() -> None:
    route = route_task("提取 2 条商品数据，字段: title, price")
    verification = verify_route_success(
        route,
        capability="generic_extractor",
        result={
            "row_count": 2,
            "rows": [
                {"title": "A", "price": "10"},
                {"title": "B", "price": ""},
            ],
        },
    )

    assert verification["passed"] is False
    assert "required_fields" in [item["name"] for item in verification["checks"]]


def test_success_verifier_reads_output_contract_fields() -> None:
    verification = verify_route_success(
        {
            "output_contract": {
                "output_kind": "dataset_rows",
                "fields": ["Title", "title", "price"],
            },
        },
        capability="generic_extractor",
        result={
            "row_count": 1,
            "rows": [{"title": "A"}],
        },
    )

    assert verification["passed"] is False
    assert verification["required_fields"] == ["Title", "price"]
    fields_check = next(item for item in verification["checks"] if item["name"] == "required_fields")
    assert fields_check["missing_examples"][0]["missing"] == ["price"]


def test_success_verifier_merges_payload_and_contract_field_aliases() -> None:
    verification = verify_route_success(
        {
            "strategy_context": {
                "output_contract": {
                    "output_kind": "dataset_records",
                    "required_fields": ["title", "price"],
                },
                "requested_fields": ["url"],
            },
        },
        capability="generic_extractor",
        result={
            "row_count": 1,
            "rows": [{"title": "A", "price": "10", "url": "https://x"}],
        },
        payload={"requested_fields": "price, rating"},
    )

    assert verification["passed"] is False
    assert verification["required_fields"] == ["price", "rating", "title", "url"]
    fields_check = next(item for item in verification["checks"] if item["name"] == "required_fields")
    assert fields_check["missing_examples"][0]["missing"] == ["rating"]


def test_route_executor_export_goal_requires_artifact(monkeypatch) -> None:
    temp_root = Path(__file__).resolve().parent / ".tmp_capability_router" / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=True)
    try:
        monkeypatch.setattr(generic, "resolve_artifact_path", lambda filename, subdir="": temp_root / subdir / filename)
        monkeypatch.setattr(generic, "register_artifact", lambda path: None)
        monkeypatch.setattr(generic, "artifact_url", lambda path: "/download/" + path.name)

        result = execute_route({
            "goal": "导出前 2 条 quote 文本为 JSONL",
            "source": """
            <html><body>
              <div class="quote"><span class="text">Alpha</span></div>
              <div class="quote"><span class="text">Beta</span></div>
            </body></html>
            """,
            "selector": ".quote .text::text",
            "run_id": "route_export",
        })

        artifact_path = result["artifact"]["path"]
        assert result["status"] == "completed"
        assert result["verification"]["save_artifact_required"] is True
        assert result["verification"]["passed"] is True
        assert any(item["name"] == "artifact" and item["passed"] for item in result["verification"]["checks"])
        assert artifact_path.endswith(".jsonl")
        assert "Alpha" in Path(artifact_path).read_text(encoding="utf-8")
    finally:
        shutil.rmtree(temp_root.parent, ignore_errors=True)


def test_capability_router_api_wiring(_artifact_tmp) -> None:
    import api_server

    client = TestClient(api_server.app)
    route_resp = client.post(
        "/api/capabilities/route",
        json={"goal": "抓取前 10 条列表数据并导出", "url": "https://example.com/list"},
    )
    roles_resp = client.get("/api/capabilities/model_roles")
    manifest_resp = client.get("/api/capabilities/manifest")
    manifest_item_resp = client.get("/api/capabilities/manifest/generic_extractor")
    agent_case_matrix_resp = client.get("/api/capabilities/agent_case_benchmark?include_cases=false")
    agent_case_summary_resp = client.post(
        "/api/capabilities/agent_case_benchmark",
        json={
            "include_cases": False,
            "cases": [
                {"id": "form_case", "category": "interaction", "capability": ["form"], "url": "https://example.com/form"},
                {"id": "tabs_case", "category": "tabs", "capability": ["new_tab", "switch_tab"], "url": "https://example.com"},
            ],
            "results": [{"case_id": "form_case", "ok": True}],
        },
    )
    agent_case_regression_resp = client.post(
        "/api/capabilities/agent_case_regression",
        json={
            "include_cases": False,
            "cases": [
                {"id": "form_case", "category": "interaction", "capability": ["form"], "url": "https://example.com/form"},
                {"id": "tabs_case", "category": "tabs", "capability": ["new_tab", "switch_tab"], "url": "https://example.com"},
            ],
            "results": [{"case_id": "form_case", "ok": True}, {"case_id": "tabs_case", "ok": True}],
        },
    )
    run_evidence_bundle_resp = client.post(
        "/api/capabilities/run_evidence_bundle",
        json={
            "run_id": "api-case",
            "result": {
                "status": "completed",
                "completed": True,
                "result": {"rows": [{"title": "A"}], "row_count": 1},
                "verification": {"passed": True},
            },
            "events": [{"type": "run_end", "success": True}],
        },
    )
    browser_runtime_doctor_resp = client.post(
        "/api/capabilities/browser_runtime_doctor",
        json={
            "pool_status": {"max_contexts": 1, "active_count": 0, "available_count": 1},
            "backend_status": {
                "active": {"name": "playwright_chromium", "status": "available"},
                "health": {"status": "healthy", "reachable": True},
                "available": [],
                "session_count": 0,
            },
        },
    )
    plan_resp = client.post(
        "/api/capabilities/plan",
        json={
            "goal": "抓取前 10 条列表数据并导出",
            "url": "https://example.com/list",
            "source": "<html><body><table><tr><td>Hello</td></tr></table></body></html>",
        },
    )
    workflow_resp = client.post(
        "/api/capabilities/workflow",
        json={"goal": "从 https://a.example/list 抓取数据后填写 https://b.example/form", "url": "https://a.example/list"},
    )
    action_ref_schema_resp = client.get("/api/action_refs/schema")
    action_ref_normalize_resp = client.post(
        "/api/action_refs/normalize",
        json={"items": [{"ref": "@e2"}, {"selector": "#submit"}, {"target_id": 4}]},
    )
    execute_resp = client.post(
        "/api/capabilities/execute",
        json={
            "goal": "提取 1 条文本",
            "source": '<p class="item">Hello</p>',
            "selector": ".item::text",
            "export": True,
            "run_id": "api_execute",
        },
    )

    assert route_resp.status_code == 200
    assert route_resp.json()["result"]["intent"]["task_type"] in {"structured_extraction", "crawl_extract"}
    assert "generic_extractor" in _plan_names(route_resp.json()["result"])
    assert route_resp.json()["result"]["execution_plan"]["version"] == "planner_contract.v1"
    assert route_resp.json()["result"]["crawl_efficiency_plan"]["version"] == "crawl_efficiency_plan.v1"
    assert route_resp.json()["result"]["crawl_efficiency_plan"]["preferred_order"][:3] == ["api_replay", "html_extract", "dom_selector"]
    assert roles_resp.json()["result"]["semantic_model"]["position"] == "planner and reflector layer"
    assert manifest_resp.json()["result"]["count"] >= 12
    assert manifest_item_resp.json()["result"]["name"] == "generic_extractor"
    assert agent_case_matrix_resp.json()["result"]["matrix"]["version"] == "agent_case_benchmark_matrix.v1"
    assert agent_case_matrix_resp.json()["result"]["matrix"]["enabled_count"] >= 15
    assert agent_case_matrix_resp.json()["result"]["matrix"]["cases"] == []
    assert agent_case_summary_resp.json()["result"]["summary"]["version"] == "agent_case_benchmark_result.v1"
    assert agent_case_summary_resp.json()["result"]["summary"]["evaluated_count"] == 1
    assert agent_case_summary_resp.json()["result"]["summary"]["not_run_count"] == 1
    assert agent_case_summary_resp.json()["result"]["matrix"]["cases"] == []
    assert agent_case_regression_resp.json()["result"]["report"]["version"] == "agent_case_regression_report.v1"
    assert agent_case_regression_resp.json()["result"]["report"]["gate"]["status"] == "passed"
    assert agent_case_regression_resp.json()["result"]["report"]["summary"]["evaluated_count"] == 2
    assert run_evidence_bundle_resp.json()["result"]["bundle"]["version"] == "run_evidence_bundle.v1"
    assert run_evidence_bundle_resp.json()["result"]["bundle"]["run_id"] == "api-case"
    assert run_evidence_bundle_resp.json()["result"]["bundle"]["status"] == "completed"
    assert run_evidence_bundle_resp.json()["result"]["bundle"]["events"]["by_type"] == {"run_end": 1}
    assert browser_runtime_doctor_resp.json()["result"]["report"]["version"] == "browser_runtime_doctor_report.v1"
    assert browser_runtime_doctor_resp.json()["result"]["report"]["readiness"]["can_start_new_context"] is True
    assert browser_runtime_doctor_resp.json()["result"]["report"]["runtime_status"] == "available"
    assert plan_resp.json()["result"]["execution_plan"]["version"] == "planner_contract.v1"
    assert plan_resp.json()["result"]["workflow_graph"]["version"] == "workflow_graph.v1"
    assert plan_resp.json()["result"]["crawl_efficiency_plan"]["recommended_path"] == "html_extract"
    assert plan_resp.json()["result"]["crawl_efficiency_plan"]["skip_browser"] is True
    assert any(item["capability"] == "generic_extractor" for item in plan_resp.json()["result"]["execution_plan"]["steps"])
    assert workflow_resp.json()["result"]["workflow_graph"]["version"] == "workflow_graph.v1"
    assert len(workflow_resp.json()["result"]["workflow_graph"]["systems"]) >= 2
    assert action_ref_schema_resp.json()["result"]["version"] == "action_ref.v1"
    assert action_ref_normalize_resp.json()["result"]["summary"]["count"] == 3
    assert action_ref_normalize_resp.json()["result"]["refs"][0]["source"] == "browser_ref"
    assert execute_resp.json()["result"]["status"] == "completed"
    assert execute_resp.json()["result"]["capability"] == "extractor_select"
    assert execute_resp.json()["result"]["crawl_efficiency_plan"]["recommended_path"] == "dom_selector"
    assert execute_resp.json()["result"]["crawl_efficiency_plan"]["skip_browser"] is True
    assert execute_resp.json()["result"]["efficiency_correlation_report"]["version"] == "efficiency_correlation_report.v1"
    assert execute_resp.json()["result"]["efficiency_correlation_report"]["alignment"]["recommended_path"] == "dom_selector"
    assert execute_resp.json()["result"]["efficiency_correlation_report"]["alignment"]["executed_path"] == "dom_selector"
    assert execute_resp.json()["result"]["verification"]["passed"] is True
    assert execute_resp.json()["result"]["runtime_context"]["version"] == "capability_execute_runtime_context.v1"
    assert execute_resp.json()["result"]["runtime_context"]["before"]["runtime_snapshot"]["version"] == "browser_runtime.v1"
    assert execute_resp.json()["result"]["runtime_context"]["after"]["runtime_preflight"]["version"] == "browser_runtime_preflight.v1"
    assert execute_resp.json()["result"]["runtime_summary"]["after"]["runtime_status"] in {"available", "limited", "pool_exhausted", "backend_unavailable", "backend_unhealthy"}
    assert execute_resp.json()["result"]["runtime_drift"]["version"] == "browser_runtime_drift.v1"
    assert execute_resp.json()["result"]["runtime_drift"]["blocking"] is False
    assert execute_resp.json()["result"]["runtime_issue_summary"]["version"] == "browser_runtime_issue_summary.v1"
    assert execute_resp.json()["result"]["runtime_issue_summary"]["blocking"] is False


def test_capability_execute_can_write_trace_artifact(monkeypatch, _artifact_tmp) -> None:
    import api_server
    from visual_web_agent.artifact_manager import register_artifact as real_register_artifact
    from visual_web_agent.io_contract import persistence as _persistence

    temp_root = Path(__file__).resolve().parent / ".tmp_capability_router" / uuid.uuid4().hex
    runs_root = temp_root / "runs"
    temp_root.mkdir(parents=True, exist_ok=True)
    try:
        monkeypatch.setattr(api_server, "resolve_artifact_path", lambda filename, subdir="": temp_root / subdir / filename)
        monkeypatch.setattr(api_server, "register_artifact", real_register_artifact)
        monkeypatch.setattr(api_server, "artifact_url", lambda path: "/download/" + Path(path).name)
        monkeypatch.setattr(_persistence, "default_runs_root", lambda: runs_root)

        client = TestClient(api_server.app)
        resp = client.post(
            "/api/capabilities/execute",
            json={
                "goal": "提取 1 条文本",
                "source": '<p class="item">Hello</p>',
                "selector": ".item::text",
                "run_id": "cap-trace-api",
                "export": True,
                "trace_artifact": True,
            },
        )
        result = resp.json()["result"]
        artifact = result["trace_artifact"]
        doc = json.loads(Path(artifact["path"]).read_text(encoding="utf-8"))
        manifest = json.loads((runs_root / "cap-trace-api" / "manifest.json").read_text(encoding="utf-8"))

        assert resp.status_code == 200
        assert result["status"] == "completed"
        assert artifact["path"].endswith(".json")
        assert artifact["url"].startswith("/download/")
        assert doc["type"] == "capability_execute_trace"
        assert doc["request"]["run_id"] == "cap-trace-api"
        assert doc["result"]["capability"] == "extractor_select"
        assert doc["result"]["runtime_context"]["version"] == "capability_execute_runtime_context.v1"
        assert doc["result"]["runtime_summary"]["after"]["preflight_status"] in {"pass", "warn"}
        assert doc["result"]["runtime_drift"]["version"] == "browser_runtime_drift.v1"
        assert doc["result"]["runtime_issue_summary"]["version"] == "browser_runtime_issue_summary.v1"
        assert manifest["items"][0]["kind"] == "log"
        assert manifest["items"][0]["produced_by"] == "capability_execute"
        assert manifest["items"][0]["step_id"] == "trace_artifact"
        assert manifest["items"][0]["mime"] == "application/json"
    finally:
        shutil.rmtree(temp_root.parent, ignore_errors=True)


def test_capability_diagnostic_artifacts_register_run_manifest(monkeypatch, _artifact_tmp) -> None:
    import api_server
    from visual_web_agent.artifact_manager import register_artifact as real_register_artifact
    from visual_web_agent.io_contract import persistence as _persistence

    temp_root = Path(__file__).resolve().parent / ".tmp_capability_router" / uuid.uuid4().hex
    runs_root = temp_root / "runs"
    temp_root.mkdir(parents=True, exist_ok=True)
    run_id = "cap-diagnostics-api"
    source_url = "https://example.com/form"
    source_payload = {
        "source": {
            "type": "capability_execute_trace",
            "request": {"run_id": run_id, "url": source_url},
        },
        "name": "diagnostic report",
    }
    fixture_payload = {"fixture": {}, "name": "fixture replay report"}
    fixture_batch_payload = {"fixtures": [], "name": "fixture replay batch report"}
    efficiency_report = {
        "version": "efficiency_correlation_report.v1",
        "source": "efficiency_correlation",
        "status": "suboptimal",
        "alignment": {
            "recommended_path": "api_replay",
            "executed_path": "browser_action",
            "rank_gap": 4,
            "skip_browser": True,
        },
        "failed_checks": ["executed_matches_recommended"],
        "root_causes": ["executed_path_more_expensive_than_recommendation"],
        "recommended_action": "prefer_api_replay_before_browser_action",
    }
    failure_bundle = {
        "version": "capability_execute_failure_bundle.v1",
        "source": "capability_execute",
        "primary_failure": "selector_missing",
        "failure_category": "target_resolution",
        "capability": "browser_control",
        "action": "click",
        "recommended_actions": ["refresh_browser_snapshot"],
        "action_trace": {
            "version": "browser_action_trace.v1",
            "action": "click",
            "warning_codes": ["action_failed", "selector_missing"],
            "target": {"selector": "#submit"},
            "action_ref": {"selector": "#submit"},
        },
        "action_issue_summary": {
            "version": "browser_action_issue_summary.v1",
            "status": "error",
            "issues": [{"source": "warning_codes", "code": "selector_missing"}],
        },
    }
    fixture = build_capability_failure_regression_fixture({
        "type": "capability_execute_trace",
        "request": {"goal": "点击提交", "url": source_url, "run_id": run_id},
        "result": {"status": "error", "completed": False, "capability": "browser_control", "failure_bundle": failure_bundle},
    }, name="selector missing diagnostic")
    fixture_payload["fixture"] = fixture
    fixture_batch_payload["fixtures"] = [fixture]

    try:
        monkeypatch.setattr(api_server, "resolve_artifact_path", lambda filename, subdir="": temp_root / subdir / filename)
        monkeypatch.setattr(api_server, "register_artifact", real_register_artifact)
        monkeypatch.setattr(api_server, "artifact_url", lambda path: "/download/" + Path(path).name)
        monkeypatch.setattr(_persistence, "default_runs_root", lambda: runs_root)

        api_server._write_efficiency_feedback_replay_artifact(
            replay_efficiency_feedback({"goal": "提取商品列表", "url": source_url, "efficiency_correlation_report": efficiency_report}),
            source_payload,
        )
        api_server._write_efficiency_feedback_replay_batch_artifact(
            {"version": "efficiency_feedback_replay_batch_report.v1", "passed": True, "items": []},
            source_payload,
        )
        api_server._write_capability_failure_fixture_artifact(fixture, source_payload)
        api_server._write_capability_failure_fixture_replay_artifact(
            replay_capability_failure_fixture(fixture),
            fixture_payload,
        )
        api_server._write_capability_failure_fixture_replay_batch_artifact(
            replay_capability_failure_fixtures([fixture]),
            fixture_batch_payload,
        )
        manifest = json.loads((runs_root / run_id / "manifest.json").read_text(encoding="utf-8"))
        observed = {(item["produced_by"], item["step_id"]): item for item in manifest["items"]}

        assert {
            ("efficiency_feedback_replay", "replay_report"),
            ("efficiency_feedback_replay", "replay_batch_report"),
            ("capability_failure_fixture", "failure_fixture"),
            ("capability_failure_fixture_replay", "replay_report"),
            ("capability_failure_fixture_replay", "replay_batch_report"),
        } <= set(observed)
        for item in observed.values():
            assert item["kind"] == "log"
            assert item["mime"] == "application/json"
            assert item["source_url"] == [source_url]
            assert str(runs_root / run_id / "artifacts") in item["path"]
    finally:
        shutil.rmtree(temp_root.parent, ignore_errors=True)


def test_capability_execute_propagates_browser_action_trace(monkeypatch) -> None:
    import api_server

    events: list[tuple[str, dict]] = []
    action_trace = build_browser_action_trace(
        "click",
        session_id="default",
        started_at=1.0,
        ended_at=1.1,
        target={"ref": "@e1", "selector": "#submit"},
        action_ref={"version": "action_ref.v1", "ref": "@e1", "source": "browser_ref", "selector": "#submit"},
        warning_codes=["selector_fallback_used"],
        recommended_action="inspect_browser_action",
    )

    def fake_execute_route(payload: dict, *, spider_lite=None) -> dict:
        return {
            "status": "completed",
            "completed": True,
            "route": {"intent": {"task_type": "browser_interaction"}},
            "attempts": [{"capability": "browser_control", "status": "attempted"}],
            "capability": "browser_control",
            "result": {"action_trace": action_trace, "clicked": True},
            "artifact": None,
            "verification": {"passed": True},
            "fallback_reason": "",
        }

    def fake_broadcast_phase(phase: str, **kwargs) -> None:
        events.append((phase, kwargs))

    monkeypatch.setattr(api_server, "execute_route", fake_execute_route)
    monkeypatch.setattr(api_server, "broadcast_phase", fake_broadcast_phase)

    client = TestClient(api_server.app)
    resp = client.post("/api/capabilities/execute", json={"goal": "点击提交按钮"})
    result = resp.json()["result"]
    telemetry = events[-1][1]["extra"]

    assert resp.status_code == 200
    assert result["action_trace"]["version"] == "browser_action_trace.v1"
    assert result["action_trace"]["action"] == "click"
    assert result["action_issue_summary"]["version"] == "browser_action_issue_summary.v1"
    assert result["action_issue_summary"]["status"] == "warn"
    assert result["crawl_efficiency_plan"]["version"] == "crawl_efficiency_plan.v1"
    assert telemetry["crawl_efficiency_plan"]["version"] == "crawl_efficiency_plan.v1"
    assert result["efficiency_correlation_report"]["version"] == "efficiency_correlation_report.v1"
    assert telemetry["efficiency_correlation_report"]["version"] == "efficiency_correlation_report.v1"
    assert telemetry["efficiency_correlation_report"]["alignment"]["executed_path"] == "browser_action"
    assert telemetry["action_trace"]["action"] == "click"
    assert telemetry["action_issue_summary"]["recommended_action"] == "inspect_browser_action"


def test_route_executor_preserves_failed_action_trace_in_attempt() -> None:
    action_trace = build_browser_action_trace(
        "click",
        session_id="default",
        status="error",
        started_at=1.0,
        ended_at=1.1,
        target={"ref": "@e1", "selector": "#submit"},
        action_ref={"version": "action_ref.v1", "ref": "@e1", "source": "browser_ref", "selector": "#submit"},
        warning_codes=["action_failed"],
        recommended_action="inspect_browser_action",
    )

    class FailingSpider:
        def run(self, payload: dict) -> dict:
            exc = RuntimeError("browser action failed")
            exc.action_trace = action_trace
            exc.action_issue_summary = action_trace["issue_summary"]
            raise exc

    result = execute_route(
        {"goal": "抓取页面", "url": "https://example.com", "allow_network": True},
        spider_lite=FailingSpider(),
    )
    attempt = result["attempts"][-1]

    assert result["completed"] is False
    assert attempt["status"] == "error"
    assert attempt["action_trace"]["status"] == "error"
    assert attempt["action_trace"]["action"] == "click"
    assert attempt["action_issue_summary"]["status"] == "error"


def test_capability_execute_propagates_failed_browser_action_trace_from_attempt(monkeypatch) -> None:
    import api_server

    events: list[tuple[str, dict]] = []
    action_trace = build_browser_action_trace(
        "click",
        session_id="default",
        status="error",
        started_at=1.0,
        ended_at=1.1,
        target={"ref": "@e1", "selector": "#submit"},
        action_ref={"version": "action_ref.v1", "ref": "@e1", "source": "browser_ref", "selector": "#submit"},
        warning_codes=["action_failed"],
        result_summary={
            "failed": True,
            "failure_code": "selector_missing",
            "failure_category": "target_resolution",
            "recovery_actions": ["refresh_browser_snapshot", "use_similar_selector"],
        },
        recommended_action="inspect_browser_action",
    )

    def fake_execute_route(payload: dict, *, spider_lite=None) -> dict:
        return {
            "status": "fallback",
            "completed": False,
            "route": {"intent": {"task_type": "browser_interaction"}},
            "attempts": [{
                "capability": "browser_control",
                "status": "error",
                "reason": "browser action failed",
                "action_trace": action_trace,
                "action_issue_summary": action_trace["issue_summary"],
            }],
            "capability": "browser_control",
            "result": None,
            "artifact": None,
            "verification": {"passed": False},
            "fallback_reason": "browser action failed",
        }

    def fake_broadcast_phase(phase: str, **kwargs) -> None:
        events.append((phase, kwargs))

    monkeypatch.setattr(api_server, "execute_route", fake_execute_route)
    monkeypatch.setattr(api_server, "broadcast_phase", fake_broadcast_phase)

    client = TestClient(api_server.app)
    resp = client.post("/api/capabilities/execute", json={"goal": "点击提交按钮"})
    result = resp.json()["result"]
    telemetry = events[-1][1]["extra"]

    assert resp.status_code == 200
    assert result["completed"] is False
    assert result["action_trace"]["status"] == "error"
    assert result["action_issue_summary"]["status"] == "error"
    assert result["failure_bundle"]["version"] == "capability_execute_failure_bundle.v1"
    assert result["failure_bundle"]["primary_failure"] == "selector_missing"
    assert result["failure_bundle"]["failure_category"] == "target_resolution"
    assert result["failure_bundle"]["recommended_actions"] == ["refresh_browser_snapshot", "use_similar_selector", "inspect_browser_action"]
    assert result["failure_bundle"]["attempt_count"] == 1
    assert result["efficiency_correlation_report"]["version"] == "efficiency_correlation_report.v1"
    assert result["efficiency_correlation_report"]["status"] == "needs_replan"
    assert telemetry["attempts"][0]["action_trace"]["status"] == "error"
    assert telemetry["action_trace"]["warning_codes"] == ["action_failed"]
    assert telemetry["failure_bundle"]["primary_failure"] == "selector_missing"
    assert telemetry["efficiency_correlation_report"]["root_causes"]


def test_capability_execute_exception_trace_broadcasts_and_writes_artifact(monkeypatch) -> None:
    import api_server

    events: list[tuple[str, dict]] = []
    temp_root = Path(__file__).resolve().parent / ".tmp_capability_router" / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=True)
    action_trace = build_browser_action_trace(
        "click",
        session_id="default",
        status="error",
        started_at=1.0,
        ended_at=1.1,
        target={"ref": "@e1", "selector": "#submit"},
        action_ref={"version": "action_ref.v1", "ref": "@e1", "source": "browser_ref", "selector": "#submit"},
        warning_codes=["action_failed"],
        result_summary={
            "failed": True,
            "failure_code": "click_intercepted",
            "failure_category": "element_state",
            "recovery_actions": ["close_overlay", "try_alternate_click"],
        },
        recommended_action="inspect_browser_action",
    )

    def fake_execute_route(payload: dict, *, spider_lite=None) -> dict:
        exc = RuntimeError("browser action crashed")
        exc.action_trace = action_trace
        exc.action_issue_summary = action_trace["issue_summary"]
        raise exc

    def fake_broadcast_phase(phase: str, **kwargs) -> None:
        events.append((phase, kwargs))

    try:
        monkeypatch.setattr(api_server, "execute_route", fake_execute_route)
        monkeypatch.setattr(api_server, "broadcast_phase", fake_broadcast_phase)
        monkeypatch.setattr(api_server, "resolve_artifact_path", lambda filename, subdir="": temp_root / subdir / filename)
        monkeypatch.setattr(api_server, "register_artifact", lambda path: None)
        monkeypatch.setattr(api_server, "artifact_url", lambda path: "/download/" + Path(path).name)

        client = TestClient(api_server.app)
        resp = client.post(
            "/api/capabilities/execute",
            json={"goal": "点击提交按钮", "run_id": "failed/action", "trace_artifact": True},
        )
        detail = resp.json()["detail"]
        telemetry = events[-1][1]["extra"]
        doc = json.loads(Path(detail["trace_artifact"]["path"]).read_text(encoding="utf-8"))

        assert resp.status_code == 500
        assert detail["message"] == "capability route execution failed"
        assert detail["action_trace"]["status"] == "error"
        assert detail["action_issue_summary"]["status"] == "error"
        assert detail["phase_event"]["type"] == "phase"
        assert detail["phase_event"]["phase"] == "capability_execute"
        assert detail["phase_event"]["severity"] == "error"
        assert detail["phase_event"]["action_trace"]["status"] == "error"
        assert detail["phase_event"]["action_issue_summary"]["status"] == "error"
        assert detail["failure_bundle"]["version"] == "capability_execute_failure_bundle.v1"
        assert detail["failure_bundle"]["primary_failure"] == "click_intercepted"
        assert detail["failure_bundle"]["failure_category"] == "element_state"
        assert detail["failure_bundle"]["recommended_actions"] == ["close_overlay", "try_alternate_click", "inspect_browser_action"]
        assert detail["phase_event"]["failure_bundle"]["primary_failure"] == "click_intercepted"
        assert detail["efficiency_correlation_report"]["version"] == "efficiency_correlation_report.v1"
        assert detail["phase_event"]["efficiency_correlation_report"]["version"] == "efficiency_correlation_report.v1"
        assert detail["phase_event"]["trace_artifact"]["path"] == detail["trace_artifact"]["path"]
        assert detail["phase_event"]["attempts"] == telemetry["attempts"]
        assert detail["phase_event"]["runtime_summary"] == telemetry["runtime_summary"]
        assert telemetry["action_trace"]["status"] == "error"
        assert telemetry["failure_bundle"]["primary_failure"] == "click_intercepted"
        assert telemetry["efficiency_correlation_report"]["status"] == "needs_replan"
        assert telemetry["attempts"][0]["action_issue_summary"]["status"] == "error"
        assert doc["result"]["status"] == "error"
        assert doc["result"]["failure_bundle"]["primary_failure"] == "click_intercepted"
        assert doc["result"]["efficiency_correlation_report"]["version"] == "efficiency_correlation_report.v1"
        assert doc["result"]["action_trace"]["warning_codes"] == ["action_failed"]
    finally:
        shutil.rmtree(temp_root.parent, ignore_errors=True)


def test_capability_router_source_wiring() -> None:
    root = Path(__file__).resolve().parent.parent
    api_src = (root / "api_server.py").read_text(encoding="utf-8")
    cap_persist_src = (root / "capability_artifact_persistence.py").read_text(encoding="utf-8")
    cap_exec_src = (root / "capability_execute_helpers.py").read_text(encoding="utf-8")
    api_src_combined = api_src + "\n" + cap_persist_src + "\n" + cap_exec_src
    api_src = api_src_combined
    tq_src = (root / "api_routes" / "task_queue_api.py").read_text(encoding="utf-8")
    action_ref_src = (root / "visual_web_agent" / "action_ref.py").read_text(encoding="utf-8")
    browser_backend_src = (root / "visual_web_agent" / "browser_backend.py").read_text(encoding="utf-8")
    browser_control_api_src = (root / "visual_web_agent" / "browser_control_api.py").read_text(encoding="utf-8")
    browser_control_src = (root / "visual_web_agent" / "browser_control.py").read_text(encoding="utf-8")
    _main_raw = (root / "visual_web_agent" / "main.py").read_text(encoding="utf-8")
    _goal_parser = root / "visual_web_agent" / "phases" / "goal_parser.py"
    main_src = _main_raw + ("\n" + _goal_parser.read_text(encoding="utf-8") if _goal_parser.exists() else "")
    agent_strategy_src = (root / "visual_web_agent" / "agent_strategy.py").read_text(encoding="utf-8")
    router_src = (root / "visual_web_agent" / "capability_router.py").read_text(encoding="utf-8")
    executor_src = (root / "visual_web_agent" / "route_executor.py").read_text(encoding="utf-8")
    verifier_src = (root / "visual_web_agent" / "success_verifier.py").read_text(encoding="utf-8")
    manifest_src = (root / "visual_web_agent" / "capability_manifest.py").read_text(encoding="utf-8")
    agent_case_benchmark_src = (root / "visual_web_agent" / "agent_case_benchmark.py").read_text(encoding="utf-8")
    agent_case_regression_src = (root / "visual_web_agent" / "agent_case_regression.py").read_text(encoding="utf-8")
    run_evidence_bundle_src = (root / "visual_web_agent" / "run_evidence_bundle.py").read_text(encoding="utf-8")
    browser_runtime_doctor_src = (root / "visual_web_agent" / "browser_runtime_doctor.py").read_text(encoding="utf-8")
    capability_api_src = (root / "visual_web_agent" / "capability_api.py").read_text(encoding="utf-8")
    crawl_efficiency_src = (root / "visual_web_agent" / "crawl_efficiency.py").read_text(encoding="utf-8")
    efficiency_correlation_src = (root / "visual_web_agent" / "efficiency_correlation.py").read_text(encoding="utf-8")
    efficiency_feedback_src = (root / "visual_web_agent" / "efficiency_feedback.py").read_text(encoding="utf-8")
    efficiency_feedback_replay_src = (root / "visual_web_agent" / "efficiency_feedback_replay.py").read_text(encoding="utf-8")
    planner_src = (root / "visual_web_agent" / "planner_contract.py").read_text(encoding="utf-8")
    workflow_src = (root / "visual_web_agent" / "workflow_graph.py").read_text(encoding="utf-8")
    run_system_tracker_src = (root / "visual_web_agent" / "run_system_tracker.py").read_text(encoding="utf-8")
    failure_fixture_src = (root / "visual_web_agent" / "capability_failure_fixture.py").read_text(encoding="utf-8")
    failure_fixture_api_src = (root / "visual_web_agent" / "capability_failure_fixture_api.py").read_text(encoding="utf-8")
    failure_replay_src = (root / "visual_web_agent" / "capability_failure_replay.py").read_text(encoding="utf-8")
    _app_raw = (root / "vspider-ui" / "src" / "App.vue").read_text(encoding="utf-8")
    _sub_parts = []
    for _sub_name in ("CapabilityOverviewPane.vue", "CapabilityExecutionTelemetry.vue"):
        _sub_path = root / "vspider-ui" / "src" / "components" / _sub_name
        if _sub_path.exists():
            _sub_parts.append(_sub_path.read_text(encoding="utf-8"))
    app_src = _app_raw + "\n" + "\n".join(_sub_parts)
    capability_trace_list_src = (root / "vspider-ui" / "src" / "components" / "CapabilityTraceList.vue").read_text(encoding="utf-8")
    capability_runtime_panel_src = (root / "vspider-ui" / "src" / "components" / "CapabilityRuntimePanel.vue").read_text(encoding="utf-8")
    capability_plan_pane_src = (root / "vspider-ui" / "src" / "components" / "CapabilityPlanPane.vue").read_text(encoding="utf-8")
    capability_diagnostics_pane_src = (root / "vspider-ui" / "src" / "components" / "CapabilityDiagnosticsPane.vue").read_text(encoding="utf-8")

    assert "from visual_web_agent.capability_api import CapabilityApiDeps" in api_src
    assert "create_capability_router" in api_src
    assert "from .capability_router import model_role_report, route_task" in capability_api_src
    assert "from .action_ref import action_ref_schema, normalize_action_ref" in capability_api_src
    assert "from visual_web_agent.browser_pool import get_browser_runtime_status as _get_browser_runtime_status" in api_src
    assert "from visual_web_agent.browser_pool import build_browser_runtime_drift as _build_browser_runtime_drift" in api_src
    assert "from visual_web_agent.browser_pool import build_browser_runtime_issue_summary as _build_browser_runtime_issue_summary" in api_src
    assert "from visual_web_agent.browser_pool import build_browser_runtime_preflight as _build_browser_runtime_preflight" in api_src
    assert "from .browser_runtime_doctor import build_browser_runtime_doctor" in capability_api_src
    assert "from visual_web_agent.browser_control_api import BrowserControlApiDeps, create_browser_control_router" in api_src
    assert "from .capability_manifest import get_capability, list_capabilities" in capability_api_src
    assert "from .agent_case_benchmark import build_agent_case_benchmark_matrix, load_agent_case_benchmark_cases, summarize_agent_case_benchmark_results" in capability_api_src
    assert "from .agent_case_regression import build_agent_case_regression_report" in capability_api_src
    assert "from .crawl_efficiency import build_crawl_efficiency_plan" in capability_api_src
    assert "from .efficiency_correlation import build_efficiency_correlation_report" in capability_api_src
    assert "from visual_web_agent.efficiency_feedback_replay import evaluate_planner_feedback_shadow, efficiency_feedback_replay_source_kind, replay_efficiency_feedback, replay_efficiency_feedback_batch" in api_src
    assert "from .run_evidence_bundle import build_run_evidence_bundle" in capability_api_src
    assert "from visual_web_agent.capability_failure_fixture_api import CapabilityFailureFixtureApiDeps, create_capability_failure_fixture_router" in api_src
    assert "from visual_web_agent.capability_failure_fixture import build_capability_failure_regression_fixture" in api_src
    assert "from visual_web_agent.capability_failure_replay import replay_capability_failure_fixture, replay_capability_failure_fixtures" in api_src
    assert "from visual_web_agent.route_executor import execute_route" in api_src
    assert "register_artifact, resolve_artifact_path" in api_src
    assert '@router.post("/api/capabilities/route"' in capability_api_src
    assert '@router.post("/api/capabilities/plan"' in capability_api_src
    assert '@router.get("/api/capabilities/agent_case_benchmark")' in capability_api_src
    assert "async def get_agent_case_benchmark(" in capability_api_src
    assert '@router.post("/api/capabilities/agent_case_benchmark")' in capability_api_src
    assert "async def summarize_agent_case_benchmark(" in capability_api_src
    assert "matrix = build_agent_case_benchmark_matrix(cases)" in capability_api_src
    assert "summary = summarize_agent_case_benchmark_results(cases, results)" in capability_api_src
    assert '@router.get("/api/capabilities/agent_case_regression")' in capability_api_src
    assert '@router.post("/api/capabilities/agent_case_regression")' in capability_api_src
    assert '@router.post("/api/capabilities/run_evidence_bundle")' in capability_api_src
    assert "async def build_run_evidence_bundle_route(" in capability_api_src
    assert "bundle = build_run_evidence_bundle(payload)" in capability_api_src
    assert '@router.get("/api/capabilities/browser_runtime_doctor")' in capability_api_src
    assert "async def get_browser_runtime_doctor(" in capability_api_src
    assert '@router.post("/api/capabilities/browser_runtime_doctor")' in capability_api_src
    assert "async def build_browser_runtime_doctor_route(" in capability_api_src
    assert "report = build_browser_runtime_doctor(payload)" in capability_api_src
    assert "def capability_crawl_efficiency_plan(" in capability_api_src
    assert "def capability_efficiency_correlation_report(" in capability_api_src
    assert "def _efficiency_feedback_replay_source(" in api_src
    assert "def _write_efficiency_feedback_replay_artifact(" in api_src
    assert "def _write_efficiency_feedback_replay_batch_artifact(" in api_src
    assert '@app.post("/api/capabilities/efficiency_feedback/replay")' in api_src
    assert "async def replay_efficiency_feedback_route(" in api_src
    assert '@app.post("/api/capabilities/efficiency_feedback/shadow")' in api_src
    assert "async def shadow_efficiency_feedback_route(" in api_src
    assert "report = evaluate_planner_feedback_shadow(source)" in api_src
    assert '@app.get("/api/capabilities/efficiency_feedback/replays")' in api_src
    assert "async def list_efficiency_feedback_replays_route(" in api_src
    assert '@app.post("/api/capabilities/efficiency_feedback/replay_batch")' in api_src
    assert "async def replay_efficiency_feedback_batch_route(" in api_src
    assert "efficiency_feedback_replay_source_kind(source)" in api_src
    assert "def _efficiency_feedback_replay_artifact_dir(" in api_src
    assert "def _read_efficiency_feedback_replay_artifact(" in api_src
    assert "def _efficiency_feedback_replay_summary(" in api_src
    assert "def _list_efficiency_feedback_replay_artifacts(" in api_src
    assert "def _list_efficiency_feedback_replay_sources(" in api_src
    assert "report = replay_efficiency_feedback_batch(sources)" in api_src
    assert '"reports": reports' in api_src
    assert '"preferred_capabilities": [str(item)' in api_src
    assert 'subdir="capability/efficiency_feedback_replays"' in api_src
    assert 'subdir="capability/efficiency_feedback_replay_batches"' in api_src
    assert 'result["crawl_efficiency_plan"] = _capability_crawl_efficiency_plan(' in api_src
    assert 'result["efficiency_correlation_report"] = _capability_efficiency_correlation_report(result)' in api_src
    assert '"crawl_efficiency_plan": route.get("crawl_efficiency_plan") or {}' in capability_api_src
    assert '@router.post("/api/capabilities/workflow"' in capability_api_src
    assert '@router.get("/api/action_refs/schema"' in capability_api_src
    assert '@router.post("/api/action_refs/normalize"' in capability_api_src
    assert "app_browser_control_router = create_browser_control_router(BrowserControlApiDeps(" in api_src
    assert "app.include_router(app_browser_control_router)" in api_src
    assert "app.include_router(create_capability_router(CapabilityApiDeps(" in api_src
    assert '@app.post("/api/capabilities/route"' not in api_src
    assert '@router.post("/api/capabilities/route"' in capability_api_src
    assert '@router.get("/api/browser_control/backend"' in browser_control_api_src
    assert "get_browser_runtime_status(" in tq_src
    assert '@router.get("/api/capabilities/manifest"' in capability_api_src
    assert '@router.get("/api/capabilities/model_roles"' in capability_api_src
    assert "app.include_router(create_capability_failure_fixture_router(CapabilityFailureFixtureApiDeps(" in api_src
    assert "def _capability_failure_fixture_source(" in api_src
    assert "def _write_capability_failure_fixture_artifact(" in api_src
    assert "build_fixture=build_capability_failure_regression_fixture" in api_src
    assert "class CapabilityFailureFixtureApiDeps" in failure_fixture_api_src
    assert "def create_capability_failure_fixture_router(" in failure_fixture_api_src
    assert "router = APIRouter" in failure_fixture_api_src
    assert '@router.post("/api/capabilities/failure_fixture"' in failure_fixture_api_src
    assert "async def build_capability_failure_fixture_route(" in failure_fixture_api_src
    assert "fixture = deps.build_fixture(" in failure_fixture_api_src
    assert 'subdir="capability/failure_fixtures"' in api_src
    assert '@router.get("/api/capabilities/failure_fixtures"' in failure_fixture_api_src
    assert "def _capability_failure_fixture_artifact_dir(" in api_src
    assert "def _read_capability_failure_fixture_artifact(" in api_src
    assert "def _capability_failure_fixture_summary(" in api_src
    assert "def _list_capability_failure_fixture_artifacts(" in api_src
    assert "async def list_capability_failure_fixtures_route(" in failure_fixture_api_src
    assert '@router.post("/api/capabilities/failure_fixture/replay"' in failure_fixture_api_src
    assert "def _capability_failure_fixture_replay_source(" in api_src
    assert "def _write_capability_failure_fixture_replay_artifact(" in api_src
    assert "async def replay_capability_failure_fixture_route(" in failure_fixture_api_src
    assert "report = deps.replay_fixture(deps.replay_source(payload))" in failure_fixture_api_src
    assert 'subdir="capability/failure_fixture_replays"' in api_src
    assert '@router.post("/api/capabilities/failure_fixture/replay_batch"' in failure_fixture_api_src
    assert "def _write_capability_failure_fixture_replay_batch_artifact(" in api_src
    assert "async def replay_capability_failure_fixture_batch_route(" in failure_fixture_api_src
    assert "report = deps.replay_fixtures(fixtures)" in failure_fixture_api_src
    assert 'subdir="capability/failure_fixture_replay_batches"' in api_src
    assert '"source does not contain capability_failure_regression_fixture.v1"' in failure_fixture_api_src
    assert '@app.post("/api/capabilities/execute"' in api_src
    assert "def _capability_execute_runtime_context(" in api_src
    assert "def _capability_execute_runtime_summary(" in api_src
    assert "def _capability_execute_action_trace(" in api_src
    assert "def _capability_execute_exception_action_trace(" in api_src
    assert "def _capability_execute_failed_action_result(" in api_src
    assert "def _capability_execute_failure_bundle(" in api_src
    assert "def _capability_execute_phase_event(" in api_src
    assert "def _capability_execute_phase_event_extra(" in api_src
    assert "def _capability_execute_failure_detail(" in api_src
    assert 'detail["phase_event"] = dict(phase_event)' in api_src
    assert 'detail["failure_bundle"] = dict(result.get("failure_bundle") or {})' in api_src
    assert 'detail["efficiency_correlation_report"] = dict(result.get("efficiency_correlation_report") or {})' in api_src
    assert '"phase": "capability_execute"' in api_src
    assert '"version": "capability_execute_runtime_context.v1"' in api_src
    assert '"runtime_context": result.get("runtime_context")' not in api_src
    assert "runtime_status=runtime_status" in capability_api_src
    assert 'broadcast_phase(\n            "capability_execute"' in api_src
    assert '"attempts": result.get("attempts") or []' in api_src
    assert '"verification": result.get("verification")' in api_src
    assert '"runtime_summary": result.get("runtime_summary")' in api_src
    assert '"runtime_drift": result.get("runtime_drift")' in api_src
    assert '"runtime_issue_summary": result.get("runtime_issue_summary")' in api_src
    assert 'result["action_trace"] = action_trace' in api_src
    assert 'result["action_issue_summary"] = dict(action_trace.get("issue_summary") or {})' in api_src
    assert 'attempt = {\n        "capability": "browser_control"' in api_src
    assert '"action_trace": dict(action_trace)' in api_src
    assert "extra=_capability_execute_phase_event_extra(phase_event)" in api_src
    assert "raise HTTPException(status_code=500, detail=_capability_execute_failure_detail(result, phase_event=phase_event)) from exc" in api_src
    assert '"action_trace": result.get("action_trace")' in api_src
    assert '"action_issue_summary": result.get("action_issue_summary")' in api_src
    assert '"failure_bundle": result.get("failure_bundle")' in api_src
    assert '"crawl_efficiency_plan": result.get("crawl_efficiency_plan")' in api_src
    assert '"efficiency_correlation_report": result.get("efficiency_correlation_report")' in api_src
    assert 'result["failure_bundle"] = _capability_execute_failure_bundle(result)' in api_src
    assert "result[\"runtime_drift\"] = _build_browser_runtime_drift(" in api_src
    assert "result[\"runtime_issue_summary\"] = _build_browser_runtime_issue_summary(" in api_src
    assert "def _write_capability_execute_artifact(" in api_src
    assert '"trace_artifact": result.get("trace_artifact")' in api_src
    assert "route_capabilities_for_task" in main_src
    assert "from .agent_strategy import (" in main_src
    assert "def parse_goal_target_count(" in agent_strategy_src
    assert "def parse_goal_requested_fields(" in agent_strategy_src
    assert "def extraction_targets_reached(" in agent_strategy_src
    assert "def normalize_guard_url(" in agent_strategy_src
    assert "return _agent_strategy_parse_goal_target_count(goal)" in main_src
    assert "return _agent_strategy_parse_goal_requested_fields(goal)" in main_src
    assert "return _agent_strategy_normalize_guard_url(url)" in main_src
    assert 'event_stream.emit(\n                "capability_route"' in main_src
    assert '_broadcast_capability_phase(\n                    "capability_route"' in main_src
    assert '"backend_plan": _capability_route.get("backend_plan")' in main_src
    assert 'capability_manifest=_capability_route.get("capability_manifest")' in main_src
    assert 'action_ref_schema=_capability_route.get("action_ref_schema")' in main_src
    assert 'runtime_preflight=_capability_route.get("runtime_preflight")' in main_src
    assert 'execution_plan=_capability_route.get("execution_plan")' in main_src
    assert 'workflow_graph=_capability_route.get("workflow_graph")' in main_src
    assert "_capability_route: dict | None = None" in main_src
    assert "capability_route=_capability_route" in main_src
    assert "def route_task(" in router_src
    assert "from visual_web_agent.crawl_efficiency import build_crawl_efficiency_plan" in router_src
    assert 'result["crawl_efficiency_plan"] = build_crawl_efficiency_plan(' in router_src
    assert "def planner_feedback_from_failure_bundle(" in router_src
    assert "def _planner_feedback_from_context(" in router_src
    assert "def _planner_feedback_preferred_capabilities(" in router_src
    assert "def _planner_feedback_avoid_actions(" in router_src
    assert 'signals["planner_feedback"] = True' in router_src
    assert 'result["planner_feedback"] = planner_feedback' in router_src
    assert "runtime_status: dict[str, Any] | None = None" in router_src
    assert "build_browser_runtime_preflight" in router_src
    assert 'result["runtime_preflight"] = runtime_preflight' in router_src
    assert "from visual_web_agent.action_ref import action_ref_schema" in router_src
    assert "from visual_web_agent.capability_manifest import summarize_capabilities" in router_src
    assert "from visual_web_agent.planner_contract import build_execution_plan" in router_src
    assert "from visual_web_agent.workflow_graph import build_workflow_graph" in router_src
    assert 'result["execution_plan"] = build_execution_plan(result)' in router_src
    assert 'result["workflow_graph"] = build_workflow_graph(result)' in router_src
    assert 'result["action_ref_schema"] = action_ref_schema()' in router_src
    assert '"action_ref_normalizer"' in router_src
    assert "def audit_model_placement(" in router_src
    assert "Y1" in router_src and "Y28" in router_src
    assert "verify_route_success" in executor_src
    assert "def _attempt_error(capability: str, exc: Exception) -> dict[str, Any]:" in executor_src
    assert 'item["action_trace"] = dict(action_trace)' in executor_src
    assert 'item["action_issue_summary"] = dict(action_issue_summary)' in executor_src
    assert 'attempts.append(_attempt_error("spider_lite", exc))' in executor_src
    assert "def verify_route_success(" in verifier_src
    assert "class CapabilitySpec" in manifest_src
    assert "def build_default_capability_manifest(" in manifest_src
    assert 'name="agent_case_benchmark"' in manifest_src
    assert '"GET /api/capabilities/agent_case_benchmark"' in manifest_src
    assert '"POST /api/capabilities/agent_case_benchmark"' in manifest_src
    assert '"matrix": "agent_case_benchmark_matrix.v1"' in manifest_src
    assert '"result": "agent_case_benchmark_result.v1"' in manifest_src
    assert '"agent_case_benchmark.pass_rate"' in manifest_src
    assert 'milestones=("Y120",)' in manifest_src
    assert 'name="run_evidence_bundle"' in manifest_src
    assert '"POST /api/capabilities/run_evidence_bundle"' in manifest_src
    assert '"bundle": "run_evidence_bundle.v1"' in manifest_src
    assert '"run_evidence_bundle.debug"' in manifest_src
    assert 'milestones=("Y124",)' in manifest_src
    assert 'name="agent_case_regression_runner"' in manifest_src
    assert '"report": "agent_case_regression_report.v1"' in manifest_src
    assert "def build_agent_case_regression_report(" in agent_case_regression_src
    assert "def build_agent_case_regression_trend(" in agent_case_regression_src
    assert '"agent_case_regression_report.v1"' in agent_case_regression_src
    assert 'milestones=("Y121",)' in manifest_src
    assert 'name="browser_runtime_doctor"' in manifest_src
    assert '"GET /api/capabilities/browser_runtime_doctor"' in manifest_src
    assert '"POST /api/capabilities/browser_runtime_doctor"' in manifest_src
    assert '"report": "browser_runtime_doctor_report.v1"' in manifest_src
    assert '"browser_runtime_doctor.readiness"' in manifest_src
    assert 'milestones=("Y122",)' in manifest_src
    assert "def build_agent_case_benchmark_matrix(" in agent_case_benchmark_src
    assert "def summarize_agent_case_benchmark_results(" in agent_case_benchmark_src
    assert "def load_agent_case_benchmark_cases(" in agent_case_benchmark_src
    assert '"agent_case_benchmark_matrix.v1"' in agent_case_benchmark_src
    assert '"agent_case_benchmark_result.v1"' in agent_case_benchmark_src
    assert '"by_route_capability"' in agent_case_benchmark_src
    assert '"result_coverage_rate"' in agent_case_benchmark_src
    assert "def build_run_evidence_bundle(" in run_evidence_bundle_src
    assert '"run_evidence_bundle.v1"' in run_evidence_bundle_src
    assert '"actions"' in run_evidence_bundle_src
    assert '"extraction"' in run_evidence_bundle_src
    assert '"failure"' in run_evidence_bundle_src
    assert '"debug"' in run_evidence_bundle_src
    assert "def _cross_system_summary(" in run_evidence_bundle_src
    assert '"cross_system": cross_system' in run_evidence_bundle_src
    assert "system_transition" in run_evidence_bundle_src
    assert "def build_browser_runtime_doctor(" in browser_runtime_doctor_src
    assert '"browser_runtime_doctor_report.v1"' in browser_runtime_doctor_src
    assert '"readiness"' in browser_runtime_doctor_src
    assert '"recovery_plan"' in browser_runtime_doctor_src
    assert '"browser_pool_exhausted"' in browser_runtime_doctor_src
    assert '"backend_health_unhealthy"' in browser_runtime_doctor_src
    assert 'name="crawl_efficiency_planner"' in manifest_src
    assert '"crawl_efficiency_plan": "crawl_efficiency_plan.v1"' in manifest_src
    assert '"efficiency_correlation_report": "efficiency_correlation_report.v1"' in manifest_src
    assert '"capability_execute.efficiency_correlation_report"' in manifest_src
    assert '"capability_trace.efficiency_correlation_ui"' in manifest_src
    assert '"capability_trace.efficiency_correlation_markdown_summary"' in manifest_src
    assert '"Y112"' in manifest_src
    assert 'milestones=("Y108",)' in manifest_src
    assert 'def build_crawl_efficiency_plan(' in crawl_efficiency_src
    assert '_PREFERRED_ORDER = (' in crawl_efficiency_src
    assert '"api_replay"' in crawl_efficiency_src and '"vision_agent"' in crawl_efficiency_src
    assert 'name="efficiency_correlation"' in manifest_src
    assert '"report": "efficiency_correlation_report.v1"' in manifest_src
    assert 'milestones=("Y111",)' in manifest_src
    assert "def build_efficiency_correlation_report(" in efficiency_correlation_src
    assert '"efficiency_correlation_report.v1"' in efficiency_correlation_src
    assert "replan_with_efficiency_feedback" in efficiency_correlation_src
    assert 'name="efficiency_feedback"' in manifest_src
    assert '"planner_feedback": "planner_feedback.v1"' in manifest_src
    assert 'milestones=("Y113",)' in manifest_src
    assert "def build_efficiency_planner_feedback(" in efficiency_feedback_src
    assert '"source": "efficiency_correlation_report"' in efficiency_feedback_src
    assert '"feedback_type": "efficiency_correlation"' in efficiency_feedback_src
    assert 'name="efficiency_feedback_replay"' in manifest_src
    assert '"POST /api/capabilities/efficiency_feedback/replay"' in manifest_src
    assert '"POST /api/capabilities/efficiency_feedback/shadow"' in manifest_src
    assert '"GET /api/capabilities/efficiency_feedback/replays"' in manifest_src
    assert '"POST /api/capabilities/efficiency_feedback/replay_batch"' in manifest_src
    assert '"report": "efficiency_feedback_replay_report.v1"' in manifest_src
    assert '"reports": "list[efficiency_feedback_replay_report_summary.v1]"' in manifest_src
    assert '"batch_report": "efficiency_feedback_replay_batch_report.v1"' in manifest_src
    assert 'name="planner_feedback_shadow"' in manifest_src
    assert '"report": "planner_feedback_shadow_report.v1"' in manifest_src
    assert '"planner_feedback_shadow.no_route_decision_change"' in manifest_src
    assert 'milestones=("Y119",)' in manifest_src
    assert '"efficiency_feedback_replay.batch"' in manifest_src
    assert '"efficiency_feedback_replay.batch_artifact"' in manifest_src
    assert '"capability_trace.efficiency_feedback_replay_ui"' in manifest_src
    assert '"capability_trace.efficiency_feedback_replay_library_ui"' in manifest_src
    assert 'milestones=("Y114", "Y115", "Y116", "Y117", "Y118")' in manifest_src
    assert "def replay_efficiency_feedback(" in efficiency_feedback_replay_src
    assert "def evaluate_planner_feedback_shadow(" in efficiency_feedback_replay_src
    assert '"planner_feedback_shadow_report.v1"' in efficiency_feedback_replay_src
    assert '"backend_plan_changed"' in efficiency_feedback_replay_src
    assert "def replay_efficiency_feedback_batch(" in efficiency_feedback_replay_src
    assert '"efficiency_feedback_replay_batch_report.v1"' in efficiency_feedback_replay_src
    assert '"top_preferred_capabilities"' in efficiency_feedback_replay_src
    assert "def efficiency_feedback_replay_source_kind(" in efficiency_feedback_replay_src
    assert '\"planner_feedback.v1\"' in efficiency_feedback_replay_src
    assert '"efficiency_feedback_replay_report.v1"' in efficiency_feedback_replay_src
    assert "build_efficiency_planner_feedback" in efficiency_feedback_replay_src
    assert "build_execution_plan(route_with_feedback)" in efficiency_feedback_replay_src
    assert "class ExecutionPlan" in planner_src
    assert "def build_execution_plan(" in planner_src
    assert "def _runtime_preflight_step_input(" in planner_src
    assert "def _planner_feedback_step_input(" in planner_src
    assert "planner_feedback: dict[str, Any] = field(default_factory=dict)" in planner_src
    assert '"planner_feedback": dict(self.planner_feedback)' in planner_src
    assert "planner_feedback = dict(route.get(\"planner_feedback\") or {})" in planner_src
    assert "previous_failure_feedback_active" in planner_src
    assert "Planner feedback from previous failure" in planner_src
    assert "class WorkflowGraph" in workflow_src
    assert "def build_workflow_graph(" in workflow_src
    assert "def _resolve_node_system(" in workflow_src
    assert "_BROWSER_BOUND_CAPABILITIES" in workflow_src
    assert "def _normalise_per_step_systems(" in executor_src
    assert "def _stamp_system_metadata(" in executor_src
    assert '"systems_involved"' in executor_src
    assert '"system_attempts"' in executor_src
    assert 'from visual_web_agent.browser_session_pool import get_browser_session_pool_status as _get_browser_session_pool_status' in api_src
    tq_src = (root / "api_routes" / "task_queue_api.py").read_text(encoding="utf-8")
    assert '@app.get("/api/browser_sessions"' in tq_src
    # E1b: runtime cross-system tracker module + main.py wiring.
    assert "class RunSystemTracker" in run_system_tracker_src
    assert "def build_run_system_tracker(" in run_system_tracker_src
    assert "def resolve_system_for_url(" in run_system_tracker_src
    assert "def observe(" in run_system_tracker_src
    assert '"run_system_tracker.v1"' in run_system_tracker_src
    assert "from visual_web_agent.run_system_tracker import build_run_system_tracker" in main_src
    assert "_run_system_tracker = build_run_system_tracker(_capability_route)" in main_src
    assert "_run_system_tracker.observe(" in main_src
    assert '"system_transition"' in main_src
    assert "def build_capability_failure_regression_fixture(" in failure_fixture_src
    assert "def write_capability_failure_regression_fixture(" in failure_fixture_src
    assert '"capability_failure_regression_fixture.v1"' in failure_fixture_src
    assert '"capability_execute_failure_bundle.v1"' in failure_fixture_src
    assert '"phase": "capability_execute"' in failure_fixture_src
    assert "def replay_capability_failure_fixture(" in failure_replay_src
    assert "def replay_capability_failure_fixtures(" in failure_replay_src
    assert "def _increment_count(" in failure_replay_src
    assert "def _count_rows(" in failure_replay_src
    assert "def _fixture_failure_bundle(" in failure_replay_src
    assert "def _fixture_route_goal(" in failure_replay_src
    assert "def _replay_checks(" in failure_replay_src
    assert '"capability_failure_fixture_replay_report.v1"' in failure_replay_src
    assert '"capability_failure_fixture_replay_batch_report.v1"' in failure_replay_src
    assert '"top_primary_failures"' in failure_replay_src
    assert '"top_failure_categories"' in failure_replay_src
    assert '"top_failed_checks"' in failure_replay_src
    assert '"recommended_focus"' in failure_replay_src
    assert "planner_feedback_from_failure_bundle" in failure_replay_src
    assert "route_task(" in failure_replay_src
    assert "build_execution_plan(route)" in failure_replay_src
    assert '"previous_failure_feedback_active"' in failure_replay_src
    assert "def _capability_failure_fixture_replay_batch_artifact_dir(" in api_src
    assert "def _read_capability_failure_fixture_replay_batch_artifact(" in api_src
    assert "def _capability_failure_fixture_replay_batch_summary(" in api_src
    assert "def _list_capability_failure_fixture_replay_batch_artifacts(" in api_src
    assert "def _capability_failure_fixture_replay_batch_pass_rate(" in api_src
    assert "def _capability_failure_fixture_replay_batch_history_trend(" in api_src
    assert '"capability_failure_fixture_replay_batch_history_trend.v1"' in api_src
    assert '"failed_count_delta": failed_delta' in api_src
    assert '"pass_rate_delta": pass_rate_delta' in api_src
    assert '"focus_changed": bool(previous and latest_focus != previous_focus)' in api_src
    assert '@router.get("/api/capabilities/failure_fixture/replay_batches"' in failure_fixture_api_src
    assert '"trend": trend' in failure_fixture_api_src
    assert "list_capability_failure_fixture_replay_batches_route" in failure_fixture_api_src
    assert "class ActionRef" in action_ref_src
    assert "def normalize_action_ref(" in action_ref_src
    assert "def action_ref_schema(" in action_ref_src
    assert "class BrowserBackendInfo" in browser_backend_src
    assert "class PlaywrightBrowserBackend" in browser_backend_src
    assert "class RemotePlaywrightBrowserBackend" in browser_backend_src
    assert "def list_browser_backends(" in browser_backend_src
    assert "VSPIDER_REMOTE_BROWSER_ENDPOINT" in browser_backend_src
    assert "connect_over_cdp" in browser_backend_src
    assert "from visual_web_agent.action_ref import normalize_action_ref, summarize_action_ref_sources" in browser_control_src
    assert "browser_backend_health_from_info" in browser_control_src
    assert "def backend_status(" in browser_control_src
    assert '"action_refs": action_refs' in browser_control_src
    assert '"action_ref_summary": summarize_action_ref_sources(action_refs)' in browser_control_src
    assert '"action_ref": action_ref' in browser_control_src
    assert "const capabilityExecutionPlanSteps = computed(" in app_src
    assert "const capabilityRuntimePreflight = computed(" in app_src
    assert "const capabilityRuntimePreflightLabel = computed(" in app_src
    assert "const capabilityExecutionRuntimeSummary = computed(" in app_src
    assert "const capabilityExecutionRuntimeDrift = computed(" in app_src
    assert "const capabilityExecutionRuntimeIssueSummary = computed(" in app_src
    assert "const capabilityExecutionRuntimeIssues = computed(" in app_src
    assert "const capabilityExecutionRuntimeActions = computed(" in app_src
    assert "const capabilityExecutionActionTrace = computed(" in app_src
    assert "const capabilityExecutionActionIssueSummary = computed(" in app_src
    assert "const capabilityExecutionActionIssues = computed(" in app_src
    assert "const capabilityExecutionActionIssueActions = computed(" in app_src
    assert "const capabilityTraceSearchQuery = ref('')" in app_src
    assert "const capabilityExecutionRuntimeLabel = computed(" in app_src
    assert "const capabilityExecutionDriftLabel = computed(" in app_src
    assert "const capabilityExecutionIssueLabel = computed(" in app_src
    assert "const capabilityExecutionActionIssueLabel = computed(" in app_src
    assert "const hasRouteRuntimePreflightIssue = phase === 'capability_route'" in app_src
    assert "runtimePreflightStatus === 'warn'" in app_src
    assert "runtimePreflightWarningCount > 0" in app_src
    assert "const hasRuntimeIssue = phase === 'capability_execute'" in app_src
    assert "runtimeIssueStatus === 'warn'" in app_src
    assert "runtimeIssueCount > 0" in app_src
    assert "runtimeDriftStatus === 'warn'" in app_src
    assert "const hasActionIssue = phase === 'capability_execute'" in app_src
    assert "actionIssueStatus === 'warn'" in app_src
    assert "actionIssueStatus === 'error'" in app_src
    assert "actionIssueCount > 0" in app_src
    assert "const hasTraceRuntimeIssue = hasRuntimeIssue || hasRouteRuntimePreflightIssue || hasActionIssue" in app_src
    assert "const previewSeverity = hasTraceRuntimeIssue && formatPhasePreviewSeverity(evt?.severity) === 'info'" in app_src
    assert "severity: previewSeverity" in app_src
    assert "const runtimeAlignment = runtimeIssueStatus === 'warn' || runtimeIssueCount > 0" in app_src
    assert "runtimeIssueAction && runtimeIssueAction !== 'continue'" in app_src
    assert "const routePreflightAlignment = routePreflightStatus === 'warn' || routePreflightWarnings.length > 0" in app_src
    assert "routePreflightAction && routePreflightAction !== 'continue'" in app_src
    assert "alignment: alignment.fallbackReason || runtimeAlignment || routePreflightAlignment || '需要检查执行结果'" in app_src
    assert "parts.push(`runtime=${runtimeIssueStatus}`)" in app_src
    assert "parts.push(`preflight=${runtimePreflightStatus}`)" in app_src
    assert "parts.push(`preflight_warnings=${runtimePreflightWarningCount}`)" in app_src
    assert "parts.push(`preflight_codes=${runtimePreflightWarnings.slice(0, 3).join(',')}`)" in app_src
    assert "parts.push(`preflight_action=${runtimePreflight.recommended_action}`)" in app_src
    assert "parts.push(`runtime_issues=${runtimeIssueCount}`)" in app_src
    assert "parts.push(`runtime_codes=${runtimeIssueCodes.slice(0, 3).join(',')}`)" in app_src
    assert "parts.push(`drift=${runtimeDriftStatus}`)" in app_src
    assert "parts.push(`browser_action=${actionTrace.action}`)" in app_src
    assert "parts.push(`action_issue=${actionIssueStatus}`)" in app_src
    assert "parts.push(`action_issues=${actionIssueCount}`)" in app_src
    assert "parts.push(`action_codes=${actionIssueCodes.slice(0, 3).join(',')}`)" in app_src
    assert "const actionSearchText = [" in app_src
    assert "actionTrace?.target?.selector" in app_src
    assert "actionTrace?.action_ref?.selector" in app_src
    assert "actionWarningCodes.join(' ')" in app_src
    assert "searchText: actionSearchText" in app_src
    assert "const q = String(capabilityTraceSearchQuery.value || '').trim().toLowerCase()" in app_src
    assert "String(row.searchText || row.detail || '').toLowerCase().includes(q)" in app_src
    assert "Route preflight: ${routePreflight.status || 'unknown'}" in app_src
    assert "Route preflight action: ${routePreflight.recommended_action}" in app_src
    assert "Route preflight warnings: ${routePreflightWarnings.slice(0, 5).join(', ')}" in app_src
    assert "Runtime issues: ${runtimeIssue.status || 'unknown'}" in app_src
    assert "Runtime drift: ${runtimeDrift.status || 'unknown'}" in app_src
    assert "Runtime action: ${runtimeIssue.recommended_action}" in app_src
    assert "Runtime issue codes: ${issueCodes.join(', ')}" in app_src
    assert "Runtime actions: ${runtimeActions.join(', ')}" in app_src
    assert "Browser action issues: ${actionIssue.status || 'unknown'}" in app_src
    assert "Browser action: ${actionTrace.action}" in app_src
    assert "Browser action recommendation: ${actionIssue.recommended_action}" in app_src
    assert "Browser action issue codes: ${actionIssueCodes.join(', ')}" in app_src
    assert "Browser action recommendations: ${actionActions.join(', ')}" in app_src
    assert "const capabilityWorkflowGraph = computed(" in app_src
    assert "const capabilityActionRefSchema = computed(" in app_src
    assert "const browserRuntimeStatus = ref(null)" in app_src
    assert "const fetchBrowserRuntimeStatus = async () => {" in app_src
    assert "const browserRuntimeHealthLabel = computed(" in app_src
    assert "const browserRuntimeHealthCacheLabel = computed(" in app_src
    assert "import CapabilityRuntimePanel from './components/CapabilityRuntimePanel.vue'" in app_src
    assert "<CapabilityRuntimePanel" in app_src
    assert "Runtime Preflight" in capability_runtime_panel_src
    assert "runtime_status" in app_src
    assert "contexts" in app_src
    assert "issue_count" in app_src
    assert "capabilityExecutionRuntimeIssues" in app_src or "runtimeIssues" in app_src
    assert "runtime-issue" in app_src or "issue.code" in app_src
    assert "issue.code" in app_src
    assert "capabilityExecutionRuntimeActions" in app_src or "runtimeActions" in app_src
    assert "runtime-action" in app_src or "action" in app_src
    assert "capabilityExecutionActionIssueSummary" in app_src or "actionIssueSummary" in app_src
    assert "capabilityExecutionActionTrace" in app_src or "actionTrace" in app_src
    assert "capabilityExecutionActionIssues" in app_src or "actionIssues" in app_src
    assert "browser-action-issue" in app_src or "issue.code" in app_src
    assert "capabilityExecutionActionIssueActions" in app_src or "actionIssueActions" in app_src
    assert "browser-action-recommendation" in app_src or "action" in app_src
    assert "capabilityTraceSearchQuery" in app_src
    assert "placeholder=\"搜索 action / selector / issue code\"" in capability_trace_list_src
    assert "class=\"capability-trace-search\"" in capability_trace_list_src
    assert "class=\"capability-trace-search-count\"" in capability_trace_list_src
    assert "Browser Runtime" in capability_runtime_panel_src
    assert "结构化执行计划" in capability_plan_pane_src
    assert "跨系统工作流图" in capability_plan_pane_src
    assert "Unified ActionRef" in capability_plan_pane_src
    assert "能力清单摘要" in capability_diagnostics_pane_src


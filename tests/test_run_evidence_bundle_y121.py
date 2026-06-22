from __future__ import annotations

from visual_web_agent.browser_control import build_browser_action_trace
from visual_web_agent.capability_router import route_task
from visual_web_agent.run_evidence_bundle import build_run_evidence_bundle


def test_run_evidence_bundle_summarizes_completed_extraction_run() -> None:
    route = route_task("提取 2 条商品列表并导出", url="https://example.com/products")
    result = {
        "status": "completed",
        "completed": True,
        "route": route,
        "capability": "generic_extractor",
        "result": {
            "source": "DOM_LIST",
            "rows": [{"title": "A"}, {"title": "B"}],
            "row_count": 2,
            "field_coverage": {"requested": ["title"], "missing_by_field": {}},
        },
        "artifact": {"path": "workspace/artifacts/items.jsonl", "url": "/download/items.jsonl"},
        "verification": {"passed": True, "observed_count": 2, "target_count": 2, "summary": "ok"},
        "runtime_context": {
            "version": "capability_execute_runtime_context.v1",
            "before": {"runtime_snapshot": {"status": "available"}},
            "after": {"runtime_snapshot": {"status": "available"}, "runtime_preflight": {"status": "pass", "blocking": False}},
        },
        "runtime_summary": {"after": {"runtime_status": "available"}},
        "runtime_drift": {"version": "browser_runtime_drift.v1", "blocking": False},
        "runtime_issue_summary": {"version": "browser_runtime_issue_summary.v1", "blocking": False},
    }
    events = [
        {"type": "observe", "browser_state": {"version": "browser_state.v2", "url": "https://example.com/products", "title": "Products"}},
        {"type": "extract", "source": "DOM_LIST", "rows": 2, "output_file": "workspace/artifacts/items.jsonl"},
        {"type": "run_end", "success": True, "metadata": {"html_log": "logs/run.html"}},
    ]

    bundle = build_run_evidence_bundle({"run_id": "case-1", "result": result, "events": events, "event_stream_path": "logs/event_stream.jsonl"})

    assert bundle["version"] == "run_evidence_bundle.v1"
    assert bundle["source"] == "run_evidence_bundle"
    assert bundle["status"] == "completed"
    assert bundle["run_id"] == "case-1"
    assert bundle["route"]["backend_plan"]
    assert bundle["execution_plan"]["version"] == "planner_contract.v1"
    assert bundle["runtime"]["after_status"] == "available"
    assert bundle["browser_state"]["version"] == "browser_state.v2"
    assert bundle["actions"]["count"] == 0
    assert bundle["extraction"]["extract_count"] == 2
    assert bundle["extraction"]["row_count"] == 4
    assert bundle["extraction"]["verification"]["passed"] is True
    assert bundle["failure"]["present"] is False
    assert bundle["artifacts"]["count"] >= 2
    assert bundle["events"]["by_type"] == {"extract": 1, "observe": 1, "run_end": 1}
    assert "route.execution_plan" in bundle["debug"]["entrypoints"]
    assert bundle["cross_system"]["runtime_cross_system"] is False
    assert bundle["cross_system"]["transition_count"] == 0
    assert "cross_system.transitions" not in bundle["debug"]["entrypoints"]


def test_run_evidence_bundle_merges_manifest_artifact_metadata() -> None:
    path = "runs/case-2/artifacts/items.jsonl"
    bundle = build_run_evidence_bundle({
        "run_id": "case-2",
        "result": {
            "status": "completed",
            "completed": True,
            "artifact": {"path": path, "url": "/download/runs/case-2/artifacts/items.jsonl"},
        },
        "manifest": {
            "version": "manifest.v1",
            "items": [
                {
                    "kind": "dataset_records",
                    "path": path,
                    "size": 123,
                    "sha256": "abc123",
                    "mime": "application/x-ndjson",
                    "source_url": ["https://example.com/items"],
                    "produced_by": "api_replay",
                    "step_id": "network_replay",
                },
            ],
        },
        "contracts": {
            "manifest": {
                "version": "manifest.v1",
                "items": [
                    {
                        "kind": "media_image",
                        "path": "runs/case-2/artifacts/photo.jpg",
                        "mime": "image/jpeg",
                        "produced_by": "browser_action",
                        "step_id": "download_image",
                    },
                ],
            },
        },
    })

    first = bundle["artifacts"]["items"][0]
    second = bundle["artifacts"]["items"][1]
    assert bundle["artifacts"]["count"] == 2
    assert first["kind"] == "artifact"
    assert first["manifest_kind"] == "dataset_records"
    assert first["mime"] == "application/x-ndjson"
    assert first["sha256"] == "abc123"
    assert first["source_url"] == ["https://example.com/items"]
    assert first["produced_by"] == "api_replay"
    assert first["step_id"] == "network_replay"
    assert second["kind"] == "media_image"
    assert second["evidence_source"] == "manifest"


def test_run_evidence_bundle_surfaces_cross_system_dimension() -> None:
    route = route_task(
        "从 https://a.example/list 抓取前 3 条数据，然后打开 https://b.example/form 填写表单并导出证据",
        url="https://a.example/list",
    )
    events = [
        {
            "type": "system_transition",
            "step": 5,
            "from_system_id": "system_1",
            "from_system_name": "a.example",
            "to_system_id": "system_2",
            "to_system_name": "b.example",
            "url": "https://b.example/form",
        },
        {"type": "extract", "source": "DOM", "rows": 3},
    ]

    bundle = build_run_evidence_bundle({
        "run_id": "xsys-1",
        "result": {"status": "completed", "completed": True, "route": route},
        "events": events,
    })

    cross = bundle["cross_system"]
    assert cross["planned_system_count"] >= 2
    assert cross["planned_cross_system"] is True
    assert cross["transition_count"] == 1
    assert cross["runtime_cross_system"] is True
    assert cross["visited_system_ids"] == ["system_1", "system_2"]
    assert cross["transitions"][0]["to_system_id"] == "system_2"
    assert cross["transitions"][0]["step"] == 5
    assert "cross_system.transitions" in bundle["debug"]["entrypoints"]


def test_run_evidence_bundle_reads_system_transition_from_detail() -> None:
    events = [
        {
            "type": "system_transition",
            "detail": {
                "step": 2,
                "from_system_id": "system_1",
                "to_system_id": "system_2",
                "to_system_name": "b.example",
                "url": "https://b.example/",
            },
        },
    ]
    bundle = build_run_evidence_bundle({"events": events})
    cross = bundle["cross_system"]
    assert cross["transition_count"] == 1
    assert cross["transitions"][0]["from_system_id"] == "system_1"
    assert cross["transitions"][0]["to_system_id"] == "system_2"
    assert cross["runtime_cross_system"] is True


def test_run_evidence_bundle_prioritizes_failure_and_action_trace_debug() -> None:
    trace = build_browser_action_trace(
        "click",
        status="error",
        session_id="s1",
        target={"selector": "#submit"},
        action_ref={"selector": "#submit"},
        warning_codes=["action_failed"],
        recommended_action="refresh_browser_snapshot",
    )
    failure_bundle = {
        "version": "capability_execute_failure_bundle.v1",
        "primary_failure": "click_intercepted",
        "failure_category": "browser_action",
        "recommended_action": "use_similar_selector",
        "recommended_actions": ["use_similar_selector", "refresh_browser_snapshot"],
    }
    result = {
        "status": "failed",
        "completed": False,
        "capability": "browser_control",
        "action_trace": trace,
        "failure_bundle": failure_bundle,
        "runtime_context": {
            "version": "capability_execute_runtime_context.v1",
            "after": {"runtime_snapshot": {"status": "available"}, "runtime_preflight": {"status": "warn", "blocking": False}},
        },
        "trace_artifact": {"path": "workspace/capability/trace.json", "url": "/download/trace.json"},
    }

    bundle = build_run_evidence_bundle({"result": result})

    assert bundle["status"] == "failed"
    assert bundle["actions"]["count"] == 1
    assert bundle["actions"]["error_count"] == 1
    assert bundle["actions"]["traces"][0]["action"] == "click"
    assert bundle["failure"]["present"] is True
    assert bundle["failure"]["primary_failure"] == "click_intercepted"
    assert bundle["debug"]["primary_focus"] == "click_intercepted"
    assert bundle["debug"]["recommended_actions"] == ["use_similar_selector", "refresh_browser_snapshot"]
    assert "failure.bundle" in bundle["debug"]["entrypoints"]
    assert bundle["artifacts"]["items"][0]["kind"] == "trace_artifact"


def test_run_evidence_bundle_reports_no_evidence() -> None:
    bundle = build_run_evidence_bundle({})

    assert bundle["status"] == "no_evidence"
    assert bundle["route"]["backend_plan"] == []
    assert bundle["execution_plan"]["step_count"] == 0
    assert bundle["debug"]["recommended_actions"][0] == "attach_route_result_or_event_stream"

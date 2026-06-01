from __future__ import annotations

from visual_web_agent.agent_case_benchmark import (
    build_agent_case_benchmark_matrix,
    load_agent_case_benchmark_cases,
    normalize_agent_case_benchmark_case,
    summarize_agent_case_benchmark_results,
)


def test_agent_case_benchmark_matrix_tracks_real_task_surfaces() -> None:
    cases = [
        {
            "id": "form_case",
            "category": "interaction",
            "capability": ["form", "date_picker", "component_select"],
            "url": "https://example.com/form",
            "goal": "fill form",
            "expected": {"success": True},
        },
        {
            "id": "table_case",
            "category": "extraction",
            "capability": ["table_extract", "pagination", "excel"],
            "url": "https://example.com/table",
            "goal": "extract rows",
            "enable_xhr": True,
            "expected": {"success": True, "min_output_rows": 30, "required_fields": ["Name"]},
        },
        {
            "id": "tab_case",
            "category": "tabs",
            "capability": ["new_tab", "switch_tab", "close_tab"],
            "url": "https://example.com/tabs",
            "goal": "open tabs",
            "expected": {"success": True},
        },
        {
            "id": "disabled_upload",
            "enabled": False,
            "category": "interaction",
            "capability": ["file_upload", "form"],
            "upload_file": "workspace/test.xlsx",
        },
    ]

    matrix = build_agent_case_benchmark_matrix(cases)

    assert matrix["version"] == "agent_case_benchmark_matrix.v1"
    assert matrix["source"] == "agent_case_benchmark"
    assert matrix["case_count"] == 4
    assert matrix["enabled_count"] == 3
    assert matrix["disabled_count"] == 1
    assert matrix["coverage"]["form"] == 1
    assert matrix["coverage"]["rich_component"] == 1
    assert matrix["coverage"]["html_table"] == 1
    assert matrix["coverage"]["pagination"] == 1
    assert matrix["coverage"]["tab_management"] == 1
    assert "auth_wall" in matrix["coverage_gaps"]
    assert {item["name"] for item in matrix["by_route_capability"]} >= {"browser_control", "generic_extractor", "feed_export"}
    assert {item["name"] for item in matrix["by_expected_requirement"]} >= {"success", "min_output_rows", "required_fields", "xhr_enabled"}
    assert matrix["cases"][1]["risk_tags"] == ["network_xhr"]
    assert "surface:tab_management" in matrix["cases"][2]["risk_tags"]


def test_agent_case_benchmark_detects_cross_system() -> None:
    # (A) explicit cross_system capability token.
    explicit = normalize_agent_case_benchmark_case({
        "id": "explicit_cross",
        "category": "planning",
        "capability": ["cross_system", "navigation", "extract"],
        "url": "https://the-internet.herokuapp.com/",
        "goal": "do something then output",
        "expected": {"success": True},
    })
    assert "cross_system" in explicit["surfaces"]
    assert "cross_system" in explicit["risk_tags"]
    assert "browser_session_pool" in explicit["route_capabilities"]
    assert "browser_pool" in explicit["route_capabilities"]

    # (B) two distinct hosts in the goal, no explicit capability token.
    goal_domains = normalize_agent_case_benchmark_case({
        "id": "goal_two_hosts",
        "category": "planning",
        "capability": ["navigation", "extract"],
        "url": "https://a.example/list",
        "goal": "先在 https://a.example/list 抓取数据，然后打开 https://b.example/form 填写并导出。",
        "expected": {"success": True},
    })
    assert "cross_system" in goal_domains["surfaces"]
    assert "cross_system" in goal_domains["risk_tags"]

    # (C) single host, second URL is the SAME domain -> NOT cross-system.
    same_host = normalize_agent_case_benchmark_case({
        "id": "same_host_two_paths",
        "category": "interaction",
        "capability": ["navigation", "extract"],
        "url": "https://demoqa.com/droppable",
        "goal": "拖拽后导航到 https://demoqa.com/slider 调整滑块。",
        "expected": {"success": True},
    })
    assert "cross_system" not in same_host["surfaces"]
    assert "cross_system" not in same_host["risk_tags"]

    # (D) single host, no URLs in goal -> NOT cross-system.
    single = normalize_agent_case_benchmark_case({
        "id": "single_host",
        "category": "extraction",
        "capability": ["table_extract"],
        "url": "https://example.com/table",
        "goal": "extract rows",
        "expected": {"success": True},
    })
    assert "cross_system" not in single["surfaces"]
    assert "cross_system" not in single["risk_tags"]


def test_agent_case_benchmark_matrix_tracks_cross_system_surface() -> None:
    cases = [
        {
            "id": "x_cross",
            "category": "planning",
            "capability": ["cross_system", "extract"],
            "url": "https://a.example/",
            "goal": "在 https://a.example/ 取数后到 https://b.example/ 核对",
            "expected": {"success": True},
        },
        {
            "id": "plain",
            "category": "extraction",
            "capability": ["table_extract"],
            "url": "https://example.com/table",
            "goal": "extract rows",
            "expected": {"success": True},
        },
    ]
    matrix = build_agent_case_benchmark_matrix(cases)
    assert "cross_system" in matrix["required_surfaces"]
    assert matrix["coverage"]["cross_system"] == 1
    assert "cross_system" not in matrix["coverage_gaps"]
    risk_names = {item["name"] for item in matrix["by_risk_tag"]}
    assert "cross_system" in risk_names
    route_names = {item["name"] for item in matrix["by_route_capability"]}
    assert "browser_session_pool" in route_names


def test_normalize_agent_case_benchmark_case_projects_route_and_requirements() -> None:
    case = normalize_agent_case_benchmark_case({
        "id": "download_case",
        "category": "interaction",
        "capability": "file_download",
        "url": "https://demoqa.com/upload-download",
        "expected": {"success": True, "download_file": "sampleFile.jpeg"},
    })

    assert case["version"] == "agent_case_benchmark_matrix.v1"
    assert case["domain"] == "demoqa.com"
    assert case["capabilities"] == ["file_download"]
    assert case["surfaces"] == ["file_download"]
    assert case["route_capabilities"] == ["browser_control", "artifact_manager"]
    assert "download_file" in case["expected_requirements"]
    assert "surface:file_download" in case["risk_tags"]


def test_agent_case_benchmark_results_separate_not_run_from_failed() -> None:
    cases = [
        {"id": "form_case", "category": "interaction", "capability": ["form"]},
        {"id": "iframe_case", "category": "interaction", "capability": ["iframe"]},
        {"id": "api_case", "category": "extraction", "capability": ["api_replay"]},
    ]
    results = [
        {"case_id": "form_case", "ok": True, "output_rows": 0},
        {"case_id": "iframe_case", "ok": False, "diagnostics": {"failure_type": "selector_missing"}},
    ]

    summary = summarize_agent_case_benchmark_results(cases, results)

    assert summary["version"] == "agent_case_benchmark_result.v1"
    assert summary["source"] == "agent_case_benchmark"
    assert summary["status"] == "failed"
    assert summary["case_count"] == 3
    assert summary["evaluated_count"] == 2
    assert summary["not_run_count"] == 1
    assert summary["passed_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["result_coverage_rate"] == 0.6667
    weak_names = {item["name"] for item in summary["weak_dimensions"]}
    assert "surface:iframe" in weak_names
    assert "capability:iframe" in weak_names
    assert any(item["case_id"] == "api_case" and item["status"] == "not_run" for item in summary["items"])
    assert any(item["case_id"] == "iframe_case" and item["failure_type"] == "selector_missing" for item in summary["items"])


def test_agent_case_benchmark_loads_project_case_suite() -> None:
    cases = load_agent_case_benchmark_cases()
    matrix = build_agent_case_benchmark_matrix(cases)
    summary = summarize_agent_case_benchmark_results(cases, [])

    assert len(cases) >= 20
    assert matrix["version"] == "agent_case_benchmark_matrix.v1"
    assert matrix["enabled_count"] >= 15
    assert matrix["coverage"]["form"] >= 1
    assert matrix["coverage"]["rich_component"] >= 1
    assert matrix["coverage"]["hover_menu"] >= 1
    assert matrix["coverage"]["tab_management"] >= 1
    assert matrix["coverage"]["file_download"] >= 1
    # E1/E1b: the project suite now carries at least one enabled
    # cross-system case so the surface is covered, not a gap.
    assert matrix["coverage"]["cross_system"] >= 1
    assert "cross_system" not in matrix["coverage_gaps"]
    assert summary["version"] == "agent_case_benchmark_result.v1"
    assert summary["status"] == "needs_results"
    assert summary["not_run_count"] == matrix["enabled_count"]
    assert summary["recommended_focus"]

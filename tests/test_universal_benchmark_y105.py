from __future__ import annotations

from visual_web_agent.universal_benchmark import (
    build_universal_benchmark_matrix,
    normalize_universal_benchmark_case,
    summarize_universal_benchmark_results,
)


def test_universal_benchmark_matrix_infers_surface_coverage() -> None:
    cases = [
        {"id": "form_case", "category": "interaction", "capability": ["form", "date_picker"], "expected": {"success": True}},
        {"id": "api_case", "category": "extraction", "capability": ["api_replay", "table_extract"]},
        {"id": "iframe_case", "category": "interaction", "capability": ["iframe", "shadow_dom", "file_download"]},
        {"id": "disabled_auth", "category": "interaction", "capability": ["auth"], "enabled": False},
    ]

    matrix = build_universal_benchmark_matrix(cases)

    assert matrix["version"] == "universal_benchmark_matrix.v1"
    assert matrix["case_count"] == 4
    assert matrix["enabled_count"] == 3
    assert matrix["disabled_count"] == 1
    assert matrix["coverage"]["form"] == 1
    assert matrix["coverage"]["rich_component"] == 1
    assert matrix["coverage"]["api_backed"] == 1
    assert matrix["coverage"]["html_table"] == 1
    assert matrix["coverage"]["iframe"] == 1
    assert matrix["coverage"]["shadow_dom"] == 1
    assert matrix["coverage"]["file_download"] == 1
    assert "infinite_scroll" in matrix["coverage_gaps"]
    assert matrix["cases"][0]["surfaces"] == ["form", "rich_component"]


def test_normalize_universal_benchmark_case_accepts_explicit_surfaces() -> None:
    case = normalize_universal_benchmark_case({
        "id": "explicit",
        "category": "extraction",
        "capability": "list_extract",
        "surfaces": ["card_list", "pagination"],
        "url": "https://example.com",
        "goal": "extract cards",
    })

    assert case["version"] == "universal_benchmark_matrix.v1"
    assert case["capabilities"] == ["list_extract"]
    assert case["surfaces"] == ["card_list", "pagination"]
    assert case["enabled"] is True


def test_summarize_universal_benchmark_results_reports_weak_dimensions() -> None:
    cases = [
        {"id": "form_case", "category": "interaction", "capability": ["form"]},
        {"id": "iframe_case", "category": "interaction", "capability": ["iframe"]},
        {"id": "api_case", "category": "extraction", "capability": ["api_replay"]},
    ]
    results = [
        {"case_id": "form_case", "ok": True},
        {"case_id": "iframe_case", "ok": False, "diagnostics": {"failure_type": "selector_missing"}},
        {"case_id": "api_case", "ok": True},
    ]

    summary = summarize_universal_benchmark_results(cases, results)

    assert summary["version"] == "universal_benchmark_result.v1"
    assert summary["case_count"] == 3
    assert summary["passed_count"] == 2
    assert summary["failed_count"] == 1
    assert summary["pass_rate"] == 0.6667
    weak_names = {item["name"] for item in summary["weak_dimensions"]}
    assert "surface:iframe" in weak_names
    assert "capability:iframe" in weak_names
    assert any(item["case_id"] == "iframe_case" and item["failure_type"] == "selector_missing" for item in summary["items"])

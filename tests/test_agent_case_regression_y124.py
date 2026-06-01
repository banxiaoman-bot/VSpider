from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from visual_web_agent.agent_case_regression import (
    build_agent_case_regression_report,
    build_agent_case_regression_trend,
    list_agent_case_regression_reports,
    select_agent_case_regression_cases,
)


def _cases() -> list[dict]:
    return [
        {"id": "form_case", "category": "interaction", "capability": ["form"], "url": "https://example.com/form"},
        {"id": "table_case", "category": "extraction", "capability": ["table_extract", "excel"], "url": "https://example.com/table", "expected": {"min_output_rows": 2}},
        {"id": "disabled_case", "enabled": False, "category": "interaction", "capability": ["file_upload"]},
    ]


def test_agent_case_regression_selects_cases_by_dimension() -> None:
    cases = _cases()

    assert [case["id"] for case in select_agent_case_regression_cases(cases, categories=["extraction"])] == ["table_case"]
    assert [case["id"] for case in select_agent_case_regression_cases(cases, capabilities=["form"])] == ["form_case"]
    assert [case["id"] for case in select_agent_case_regression_cases(cases, include_disabled=True)] == ["form_case", "table_case", "disabled_case"]
    assert [case["id"] for case in select_agent_case_regression_cases(cases, ids=["disabled_case"])] == ["disabled_case"]


def test_agent_case_regression_report_builds_gate_and_command() -> None:
    report = build_agent_case_regression_report({
        "cases": _cases(),
        "ids": ["form_case", "table_case"],
        "results": [
            {"case_id": "form_case", "ok": True, "output_rows": 0},
            {"case_id": "table_case", "ok": False, "output_rows": 1, "diagnostics": {"failure_type": "min_rows_not_met"}},
        ],
        "thresholds": {"min_pass_rate": 0.75, "max_failed_count": 0, "require_full_coverage": True},
        "include_cases": False,
        "snapshot_drift_limit": 3,
        "skill_replay_check_limit": 5,
    })

    assert report["version"] == "agent_case_regression_report.v1"
    assert report["source"] == "agent_case_regression_runner"
    assert report["mode"] == "offline_summary"
    assert report["selected_count"] == 2
    assert report["matrix"]["cases"] == []
    assert report["summary"]["version"] == "agent_case_benchmark_result.v1"
    assert report["summary"]["evaluated_count"] == 2
    assert report["summary"]["failed_count"] == 1
    assert report["gate"]["status"] == "failed"
    assert "pass_rate_below_threshold" in report["gate"]["reasons"]
    assert "failed_count_above_threshold" in report["gate"]["reasons"]
    assert report["trend"]["version"] == "agent_case_regression_trend.v1"
    assert report["trend"]["direction"] == "baseline"
    assert "run_agent_cases.py" in report["command"]["shell"]
    assert "--id form_case" in report["command"]["shell"]
    assert "--snapshot-drift-limit 3" in report["command"]["shell"]
    assert "inspect_failed_agent_case_reports" in report["recommended_actions"]


def test_agent_case_regression_report_reads_saved_reports_and_trend(tmp_path: Path) -> None:
    report_dir = tmp_path / "agent_case_reports"
    report_dir.mkdir()
    first = report_dir / "agent_cases_20260101_000000.json"
    second = report_dir / "agent_cases_20260102_000000.json"
    first.write_text(json.dumps({
        "generated_at": "2026-01-01T00:00:00",
        "total": 2,
        "passed": 1,
        "failed": 1,
        "results": [
            {"case_id": "form_case", "ok": False, "diagnostics": {"failure_type": "selector_missing"}},
            {"case_id": "table_case", "ok": True},
        ],
    }), encoding="utf-8")
    second.write_text(json.dumps({
        "generated_at": "2026-01-02T00:00:00",
        "total": 2,
        "passed": 2,
        "failed": 0,
        "results": [
            {"case_id": "form_case", "ok": True},
            {"case_id": "table_case", "ok": True},
        ],
    }), encoding="utf-8")

    reports = list_agent_case_regression_reports(report_dir, limit=5)
    report = build_agent_case_regression_report({
        "cases": _cases(),
        "report_dir": str(report_dir),
        "include_saved_reports": True,
        "thresholds": {"min_pass_rate": 1.0},
    })

    assert [Path(item["path"]).name for item in reports] == ["agent_cases_20260102_000000.json", "agent_cases_20260101_000000.json"]
    assert report["latest_report"]["passed"] == 2
    assert report["gate"]["passed"] is True
    assert report["trend"]["direction"] == "improved"
    assert report["trend"]["recovered_case_ids"] == ["form_case"]
    assert report["reports"][0]["pass_rate"] == 1.0


def test_agent_case_regression_trend_detects_regression() -> None:
    trend = build_agent_case_regression_trend([
        {"generated_at": "new", "total": 2, "passed": 1, "failed": 1, "results": [{"case_id": "table_case", "ok": False}]},
        {"generated_at": "old", "total": 2, "passed": 2, "failed": 0, "results": [{"case_id": "table_case", "ok": True}]},
    ])

    assert trend["direction"] == "regressed"
    assert trend["failed_count_delta"] == 1
    assert trend["new_failed_case_ids"] == ["table_case"]


def test_agent_case_regression_api_post_and_get() -> None:
    import api_server

    client = TestClient(api_server.app)
    post_resp = client.post("/api/capabilities/agent_case_regression", json={
        "cases": _cases(),
        "results": [{"case_id": "form_case", "ok": True}, {"case_id": "table_case", "ok": True, "output_rows": 2}],
        "include_cases": False,
    })
    get_resp = client.get("/api/capabilities/agent_case_regression?include_cases=false")

    assert post_resp.status_code == 200
    assert post_resp.json()["result"]["report"]["version"] == "agent_case_regression_report.v1"
    assert post_resp.json()["result"]["report"]["gate"]["status"] == "passed"
    assert get_resp.status_code == 200
    assert get_resp.json()["result"]["report"]["version"] == "agent_case_regression_report.v1"

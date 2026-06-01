from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from .agent_case_benchmark import build_agent_case_benchmark_matrix, default_agent_case_file, load_agent_case_benchmark_cases, summarize_agent_case_benchmark_results


_AGENT_CASE_REGRESSION_REPORT_VERSION = "agent_case_regression_report.v1"
_AGENT_CASE_REGRESSION_TREND_VERSION = "agent_case_regression_trend.v1"


def default_agent_case_report_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "workspace" / "agent_case_reports"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    text = str(value or "").strip()
    if not text:
        return []
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return [text]


def _string_set(value: Any) -> set[str]:
    return {str(item or "").strip() for item in _as_list(value) if str(item or "").strip()}


def _case_capabilities(case: dict[str, Any]) -> set[str]:
    return _string_set(case.get("capability") or case.get("capabilities"))


def select_agent_case_regression_cases(
    cases: list[dict[str, Any]] | None = None,
    *,
    ids: Any = None,
    categories: Any = None,
    capabilities: Any = None,
    include_disabled: bool = False,
    limit: int = 0,
) -> list[dict[str, Any]]:
    all_cases = [dict(item) for item in (cases or load_agent_case_benchmark_cases()) if isinstance(item, dict)]
    id_set = _string_set(ids)
    category_set = _string_set(categories)
    capability_set = _string_set(capabilities)
    selected: list[dict[str, Any]] = []
    for case in all_cases:
        case_id = str(case.get("id") or "")
        if case.get("enabled") is False and not include_disabled and not (id_set and case_id in id_set):
            continue
        if id_set and case_id not in id_set:
            continue
        if category_set and str(case.get("category") or "") not in category_set:
            continue
        if capability_set and not capability_set.intersection(_case_capabilities(case)):
            continue
        selected.append(case)
        if int(limit or 0) > 0 and len(selected) >= int(limit):
            break
    return selected


def _load_report_path(path: str | Path) -> dict[str, Any]:
    report_path = Path(path)
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict):
        data = dict(data)
        data.setdefault("path", str(report_path))
        return data
    return {}


def _report_generated_at(report: dict[str, Any]) -> str:
    return str(report.get("generated_at") or report.get("created_at") or "")


def _report_sort_key(report: dict[str, Any]) -> tuple[str, str]:
    return (_report_generated_at(report), str(report.get("path") or ""))


def _normalize_runner_result(result: dict[str, Any]) -> dict[str, Any]:
    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    failure_type = str(diagnostics.get("failure_type") or result.get("failure_type") or "")
    item = {
        "case_id": str(result.get("case_id") or result.get("id") or ""),
        "ok": bool(result.get("ok")) if "ok" in result else bool(result.get("passed")),
        "returned": bool(result.get("returned")),
        "output_rows": int(result.get("output_rows") or result.get("rows") or 0),
        "failure_type": failure_type,
        "diagnostics": dict(diagnostics),
        "error": str(result.get("error") or ""),
        "log_file": str(result.get("log_file") or ""),
        "event_stream_file": str(result.get("event_stream_file") or ""),
        "output_file": str(result.get("output_file") or ""),
    }
    if not item["ok"]:
        item["status"] = "failed"
    else:
        item["status"] = "passed"
    if not item["case_id"]:
        item["status"] = "not_run"
    return item


def _report_results(report: dict[str, Any]) -> list[dict[str, Any]]:
    raw_results = report.get("results")
    if not isinstance(raw_results, list):
        raw_results = report.get("items")
    if not isinstance(raw_results, list):
        return []
    return [_normalize_runner_result(item) for item in raw_results if isinstance(item, dict)]


def _result_id_set(results: list[dict[str, Any]], *, passed: bool | None = None) -> set[str]:
    out: set[str] = set()
    for result in results:
        case_id = str(result.get("case_id") or result.get("id") or "")
        if not case_id:
            continue
        if passed is None or bool(result.get("ok") or result.get("passed")) is passed:
            out.add(case_id)
    return out


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    results = _report_results(report)
    total = int(report.get("total") or len(results) or 0)
    passed = int(report.get("passed") or sum(1 for item in results if item.get("ok")) or 0)
    failed = int(report.get("failed") or max(0, total - passed) or 0)
    return {
        "generated_at": _report_generated_at(report),
        "path": str(report.get("path") or ""),
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "result_count": len(results),
        "failed_case_ids": sorted(_result_id_set(results, passed=False)),
    }


def list_agent_case_regression_reports(report_dir: str | Path | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
    root = Path(report_dir) if report_dir else default_agent_case_report_dir()
    if not root.exists() or not root.is_dir():
        return []
    reports = []
    for path in root.glob("agent_cases_*.json"):
        report = _load_report_path(path)
        if report:
            reports.append(report)
    reports.sort(key=_report_sort_key, reverse=True)
    return [_report_summary(report) for report in reports[: max(0, int(limit or 0))]]


def load_agent_case_regression_reports(source: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = dict(source or {})
    reports: list[dict[str, Any]] = []
    raw_reports = payload.get("reports")
    if isinstance(raw_reports, list):
        reports.extend(dict(item) for item in raw_reports if isinstance(item, dict))
    if isinstance(payload.get("report"), dict):
        reports.append(dict(payload.get("report") or {}))
    raw_paths = payload.get("report_paths") or payload.get("paths")
    if isinstance(raw_paths, (str, Path)):
        raw_paths = [raw_paths]
    if isinstance(raw_paths, list):
        for path in raw_paths:
            report = _load_report_path(path)
            if report:
                reports.append(report)
    if payload.get("include_saved_reports"):
        root = payload.get("report_dir") or None
        for summary in list_agent_case_regression_reports(root, limit=int(payload.get("saved_report_limit") or 20)):
            if summary.get("path"):
                report = _load_report_path(str(summary.get("path")))
                if report:
                    reports.append(report)
    reports.sort(key=_report_sort_key, reverse=True)
    return reports


def build_agent_case_regression_command(source: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(source or {})
    script = Path("tests") / "agent_cases" / "run_agent_cases.py"
    argv = ["python", str(script)]
    for case_id in _as_list(payload.get("ids") or payload.get("id")):
        argv.extend(["--id", str(case_id)])
    for category in _as_list(payload.get("categories") or payload.get("category")):
        argv.extend(["--category", str(category)])
    for capability in _as_list(payload.get("capabilities") or payload.get("capability")):
        argv.extend(["--capability", str(capability)])
    if bool(payload.get("include_disabled")):
        argv.append("--include-disabled")
    if bool(payload.get("run", True)):
        argv.append("--run")
    if payload.get("snapshot_drift_limit") is not None:
        argv.extend(["--snapshot-drift-limit", str(int(payload.get("snapshot_drift_limit") or 0))])
    if payload.get("skill_replay_check_limit") is not None:
        argv.extend(["--skill-replay-check-limit", str(int(payload.get("skill_replay_check_limit") or 0))])
    if bool(payload.get("skip_skill_lint")):
        argv.append("--skip-skill-lint")
    if bool(payload.get("skill_lint_strict")):
        argv.append("--skill-lint-strict")
    return {
        "argv": argv,
        "shell": " ".join(shlex.quote(part) for part in argv),
        "cwd": str(Path(__file__).resolve().parents[1]),
        "opens_browser": bool(payload.get("run", True)),
    }


def _gate_summary(summary: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    min_pass_rate = float(thresholds.get("min_pass_rate") if thresholds.get("min_pass_rate") is not None else 1.0)
    max_failed_count = int(thresholds.get("max_failed_count") if thresholds.get("max_failed_count") is not None else 0)
    require_full_coverage = bool(thresholds.get("require_full_coverage", True))
    reasons: list[str] = []
    if float(summary.get("pass_rate") or 0.0) < min_pass_rate:
        reasons.append("pass_rate_below_threshold")
    if int(summary.get("failed_count") or 0) > max_failed_count:
        reasons.append("failed_count_above_threshold")
    if require_full_coverage and int(summary.get("not_run_count") or 0):
        reasons.append("not_all_selected_cases_evaluated")
    return {
        "passed": not reasons,
        "status": "passed" if not reasons else "failed",
        "blocking": bool(reasons),
        "reasons": reasons,
        "thresholds": {
            "min_pass_rate": min_pass_rate,
            "max_failed_count": max_failed_count,
            "require_full_coverage": require_full_coverage,
        },
    }


def build_agent_case_regression_trend(reports: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    normalized = [_report_summary(report) for report in (reports or [])]
    latest = normalized[0] if normalized else {}
    previous = normalized[1] if len(normalized) > 1 else {}
    latest_failed = set(latest.get("failed_case_ids") or [])
    previous_failed = set(previous.get("failed_case_ids") or [])
    pass_rate_delta = round(float(latest.get("pass_rate") or 0.0) - float(previous.get("pass_rate") or 0.0), 4) if previous else 0.0
    failed_delta = int(latest.get("failed") or 0) - int(previous.get("failed") or 0) if previous else 0
    if not latest:
        direction = "empty"
    elif not previous:
        direction = "baseline"
    elif failed_delta < 0 or pass_rate_delta > 0:
        direction = "improved"
    elif failed_delta > 0 or pass_rate_delta < 0:
        direction = "regressed"
    else:
        direction = "stable"
    return {
        "version": _AGENT_CASE_REGRESSION_TREND_VERSION,
        "report_count": len(normalized),
        "direction": direction,
        "latest": latest,
        "previous": previous,
        "failed_count_delta": failed_delta,
        "pass_rate_delta": pass_rate_delta,
        "new_failed_case_ids": sorted(latest_failed - previous_failed),
        "recovered_case_ids": sorted(previous_failed - latest_failed),
    }


def _recommended_actions(gate: dict[str, Any], summary: dict[str, Any], trend: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    if gate.get("passed"):
        actions.append("continue")
    if "not_all_selected_cases_evaluated" in (gate.get("reasons") or []):
        actions.append("run_selected_agent_cases")
    if int(summary.get("failed_count") or 0):
        actions.append("inspect_failed_agent_case_reports")
    if trend.get("direction") == "regressed":
        actions.append("compare_latest_agent_case_regression")
    if not actions:
        actions.append("run_agent_case_regression")
    return list(dict.fromkeys(actions))


def build_agent_case_regression_report(source: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(source or {})
    raw_cases = payload.get("cases")
    cases = [dict(item) for item in raw_cases if isinstance(item, dict)] if isinstance(raw_cases, list) else load_agent_case_benchmark_cases(payload.get("cases_path") or None)
    selected_cases = select_agent_case_regression_cases(
        cases,
        ids=payload.get("ids") or payload.get("id"),
        categories=payload.get("categories") or payload.get("category"),
        capabilities=payload.get("capabilities") or payload.get("capability"),
        include_disabled=bool(payload.get("include_disabled")),
        limit=int(payload.get("limit") or 0),
    )
    reports = load_agent_case_regression_reports(payload)
    direct_results = payload.get("results")
    if isinstance(direct_results, list):
        latest_results = [_normalize_runner_result(item) for item in direct_results if isinstance(item, dict)]
        latest_report = {"generated_at": str(payload.get("generated_at") or "inline"), "results": latest_results, "total": len(latest_results), "passed": sum(1 for item in latest_results if item.get("ok")), "failed": sum(1 for item in latest_results if not item.get("ok"))}
        reports = [latest_report] + reports
    else:
        latest_report = reports[0] if reports else {}
        latest_results = _report_results(latest_report)
    matrix = build_agent_case_benchmark_matrix(selected_cases)
    summary = summarize_agent_case_benchmark_results(selected_cases, latest_results)
    thresholds = payload.get("thresholds") if isinstance(payload.get("thresholds"), dict) else {}
    gate = _gate_summary(summary, thresholds)
    trend = build_agent_case_regression_trend(reports)
    report_summaries = [_report_summary(report) for report in reports[: max(0, int(payload.get("history_limit") or 10))]]
    result = {
        "version": _AGENT_CASE_REGRESSION_REPORT_VERSION,
        "source": "agent_case_regression_runner",
        "mode": "offline_summary",
        "case_file": str(payload.get("cases_path") or default_agent_case_file()),
        "report_dir": str(payload.get("report_dir") or default_agent_case_report_dir()),
        "selected_count": len(selected_cases),
        "case_count": len(cases),
        "report_count": len(reports),
        "latest_report": _report_summary(latest_report) if latest_report else {},
        "matrix": matrix,
        "summary": summary,
        "gate": gate,
        "trend": trend,
        "reports": report_summaries,
        "command": build_agent_case_regression_command(payload),
        "recommended_actions": _recommended_actions(gate, summary, trend),
    }
    if not bool(payload.get("include_cases", False)):
        result["matrix"] = dict(result["matrix"])
        result["matrix"]["cases"] = []
    return result

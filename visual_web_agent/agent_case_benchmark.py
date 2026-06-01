from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


_AGENT_CASE_BENCHMARK_MATRIX_VERSION = "agent_case_benchmark_matrix.v1"
_AGENT_CASE_BENCHMARK_RESULT_VERSION = "agent_case_benchmark_result.v1"

_REQUIRED_SURFACES = (
    "api_backed",
    "html_table",
    "card_list",
    "detail_page",
    "pagination",
    "infinite_scroll",
    "form",
    "rich_component",
    "modal_dialog",
    "hover_menu",
    "iframe",
    "shadow_dom",
    "file_upload",
    "file_download",
    "auth_wall",
    "tab_management",
    "search",
    "chat",
    "visual_dense",
    "drag_drop",
    "recovery",
    "natural_language",
    "cross_system",
)

_CAPABILITY_SURFACE_HINTS = {
    "api_replay": "api_backed",
    "xhr": "api_backed",
    "xhr_extract": "api_backed",
    "network": "api_backed",
    "table": "html_table",
    "table_extract": "html_table",
    "spreadsheet": "html_table",
    "card": "card_list",
    "list_extract": "card_list",
    "feed_filter": "card_list",
    "news_filter": "card_list",
    "detail": "detail_page",
    "pagination": "pagination",
    "next_page": "pagination",
    "infinite_scroll": "infinite_scroll",
    "scroll": "infinite_scroll",
    "form": "form",
    "submit": "form",
    "checkbox": "form",
    "radio": "form",
    "label_binding": "form",
    "dynamic_layout": "form",
    "component_select": "rich_component",
    "date_picker": "rich_component",
    "relative_date": "rich_component",
    "cascader": "rich_component",
    "multi_level_menu": "rich_component",
    "slider": "rich_component",
    "switch": "rich_component",
    "autocomplete": "rich_component",
    "modal": "modal_dialog",
    "dialog": "modal_dialog",
    "hover": "hover_menu",
    "dropdown": "hover_menu",
    "tooltip": "hover_menu",
    "hidden_text": "hover_menu",
    "iframe": "iframe",
    "shadow_dom": "shadow_dom",
    "file_upload": "file_upload",
    "upload": "file_upload",
    "file_download": "file_download",
    "download": "file_download",
    "auth": "auth_wall",
    "login": "auth_wall",
    "new_tab": "tab_management",
    "link_open": "tab_management",
    "switch_tab": "tab_management",
    "tab_switch": "tab_management",
    "close_tab": "tab_management",
    "search": "search",
    "chat": "chat",
    "ai_portal": "chat",
    "model_switch": "chat",
    "dense_ui": "visual_dense",
    "visual": "visual_dense",
    "visual_fallback": "visual_dense",
    "drag_drop": "drag_drop",
    "drag": "drag_drop",
    "precision": "drag_drop",
    "http_error": "recovery",
    "guard": "recovery",
    "done": "recovery",
    "intent": "natural_language",
    "weather": "natural_language",
    "cross_system": "cross_system",
    "multi_system": "cross_system",
    "cross_domain": "cross_system",
}

_CAPABILITY_ROUTE_HINTS = {
    "api_replay": ("network_intelligence", "api_replay"),
    "xhr": ("network_intelligence", "api_replay"),
    "xhr_extract": ("network_intelligence", "api_replay"),
    "list_extract": ("generic_extractor", "item_pipeline", "feed_export"),
    "table_extract": ("generic_extractor", "item_pipeline", "feed_export"),
    "spreadsheet": ("generic_extractor", "item_pipeline", "feed_export"),
    "excel": ("feed_export", "artifact_manager"),
    "pagination": ("spider_lite", "page_response_cache"),
    "next_page": ("browser_control", "action_registry_macros"),
    "infinite_scroll": ("browser_control", "action_registry_macros"),
    "form": ("browser_control", "action_registry_macros", "action_ref_normalizer"),
    "submit": ("browser_control", "action_registry_macros"),
    "component_select": ("browser_control", "selector_generator", "action_ref_normalizer"),
    "date_picker": ("browser_control", "action_registry_macros"),
    "cascader": ("browser_control", "action_registry_macros"),
    "slider": ("browser_control", "action_registry_macros"),
    "hover": ("browser_control", "action_registry_macros"),
    "dropdown": ("browser_control", "action_registry_macros"),
    "tooltip": ("browser_control", "generic_extractor"),
    "iframe": ("browser_control", "selector_generator"),
    "shadow_dom": ("browser_control", "selector_generator"),
    "file_upload": ("browser_control", "artifact_manager"),
    "file_download": ("browser_control", "artifact_manager"),
    "download": ("browser_control", "artifact_manager"),
    "new_tab": ("browser_control", "browser_pool"),
    "switch_tab": ("browser_control", "browser_pool"),
    "tab_switch": ("browser_control", "browser_pool"),
    "close_tab": ("browser_control", "browser_pool"),
    "search": ("browser_control", "network_intelligence"),
    "chat": ("browser_control", "semantic_planner_reflector"),
    "model_switch": ("browser_control", "semantic_planner_reflector"),
    "visual_fallback": ("vision_agent", "browser_control"),
    "dense_ui": ("vision_agent", "selector_generator"),
    "drag_drop": ("browser_control", "action_registry_macros"),
    "drag": ("browser_control", "action_registry_macros"),
    "http_error": ("browser_control", "human_guard"),
    "guard": ("human_guard", "semantic_planner_reflector"),
    "weather": ("generic_extractor", "network_intelligence"),
    "cross_system": ("browser_pool", "browser_session_pool"),
    "multi_system": ("browser_pool", "browser_session_pool"),
    "cross_domain": ("browser_pool", "browser_session_pool"),
}

_HIGH_RISK_CAPABILITIES = {
    "auth",
    "login",
    "file_upload",
    "file_download",
    "download",
    "shadow_dom",
    "iframe",
    "drag_drop",
    "drag",
    "slider",
    "precision",
    "infinite_scroll",
    "new_tab",
    "switch_tab",
    "tab_switch",
    "close_tab",
    "chat",
    "model_switch",
    "visual_fallback",
    "dense_ui",
    "image_search",
    "cross_system",
    "multi_system",
    "cross_domain",
}

# Capability tokens that explicitly declare a cross-system task.
_CROSS_SYSTEM_CAPABILITIES = {
    "cross_system",
    "multi_system",
    "cross_domain",
}

# Match http(s) URLs embedded in a goal string so we can count how many
# distinct hosts a single case spans (mirrors InputContract.has_cross_system).
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>)\]}\u3009\u3011\u300d\uff09\uff0c\uff1b,;]+")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _dedupe(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _case_capabilities(case: dict[str, Any]) -> list[str]:
    return _dedupe(_as_list(case.get("capability") or case.get("capabilities")))


def _case_surfaces(case: dict[str, Any], capabilities: list[str]) -> list[str]:
    surfaces = _dedupe(_as_list(case.get("surface") or case.get("surfaces")))
    for capability in capabilities:
        surface = _CAPABILITY_SURFACE_HINTS.get(capability.lower())
        if surface:
            surfaces.append(surface)
    if _case_is_cross_system(case, capabilities):
        surfaces.append("cross_system")
    if not surfaces:
        surfaces.append("generic_page")
    return _dedupe(surfaces)


def _case_expected_requirements(case: dict[str, Any]) -> list[str]:
    expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
    requirements: list[str] = []
    if expected.get("success") is False:
        requirements.append("negative_success")
    else:
        requirements.append("success")
    if expected.get("min_output_rows") is not None:
        requirements.append("min_output_rows")
    if expected.get("required_fields"):
        requirements.append("required_fields")
    if expected.get("download_file"):
        requirements.append("download_file")
    if expected.get("last_done_contains"):
        requirements.append("last_done_contains")
    if case.get("upload_file"):
        requirements.append("upload_file")
    if case.get("enable_xhr"):
        requirements.append("xhr_enabled")
    return _dedupe(requirements)


def _case_risk_tags(case: dict[str, Any], capabilities: list[str], surfaces: list[str]) -> list[str]:
    tags: list[str] = []
    if case.get("enabled") is False:
        tags.append("disabled")
    if case.get("enable_xhr"):
        tags.append("network_xhr")
    if case.get("upload_file"):
        tags.append("local_file")
    for capability in capabilities:
        if capability.lower() in _HIGH_RISK_CAPABILITIES:
            tags.append(f"capability:{capability}")
    for surface in surfaces:
        if surface in {"auth_wall", "shadow_dom", "iframe", "file_upload", "file_download", "tab_management", "visual_dense", "drag_drop", "infinite_scroll", "chat"}:
            tags.append(f"surface:{surface}")
    category = str(case.get("category") or "").strip()
    if category in {"stress", "visual", "multimodal", "ai_portal", "planning", "natural_language", "tabs", "recovery"}:
        tags.append(f"category:{category}")
    if _case_is_cross_system(case, capabilities):
        tags.append("cross_system")
    return _dedupe(tags) or ["standard"]


def _case_route_capabilities(capabilities: list[str]) -> list[str]:
    out: list[str] = []
    for capability in capabilities:
        out.extend(_CAPABILITY_ROUTE_HINTS.get(capability.lower()) or ())
    return _dedupe(out)


def _domain(url: str) -> str:
    try:
        return urlparse(str(url or "")).netloc.lower()
    except Exception:
        return ""


def _normalize_host(netloc: str) -> str:
    host = str(netloc or "").split("@")[-1].split(":", 1)[0].strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _hosts_in_text(text: str) -> set[str]:
    """Collect distinct normalized hosts from any http(s) URLs in ``text``."""

    hosts: set[str] = set()
    for raw in _URL_IN_TEXT_RE.findall(str(text or "")):
        try:
            host = _normalize_host(urlparse(raw).netloc)
        except Exception:
            host = ""
        if host:
            hosts.add(host)
    return hosts


def _case_is_cross_system(case: dict[str, Any], capabilities: list[str]) -> bool:
    """Detect whether a case spans more than one system.

    Two independent signals (either is sufficient):

    1. an explicit ``cross_system`` / ``multi_system`` / ``cross_domain``
       capability token declared by the case author, or
    2. the case's ``url`` + ``goal`` together reference >= 2 distinct
       hosts (mirrors ``InputContract.has_cross_system``).
    """

    if {c.lower() for c in capabilities} & _CROSS_SYSTEM_CAPABILITIES:
        return True
    hosts: set[str] = set()
    primary = _normalize_host(urlparse(str(case.get("url") or "")).netloc)
    if primary:
        hosts.add(primary)
    hosts |= _hosts_in_text(case.get("goal"))
    return len(hosts) >= 2


def default_agent_case_file() -> Path:
    return Path(__file__).resolve().parents[1] / "tests" / "agent_cases" / "cases.json"


def load_agent_case_benchmark_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    case_path = Path(path) if path else default_agent_case_file()
    if not case_path.exists():
        return []
    data = json.loads(case_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [dict(item) for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        return [dict(item) for item in data.get("cases") if isinstance(item, dict)]
    return []


def normalize_agent_case_benchmark_case(case: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(case or {}) if isinstance(case, dict) else {}
    capabilities = _case_capabilities(data)
    surfaces = _case_surfaces(data, capabilities)
    expected = data.get("expected") if isinstance(data.get("expected"), dict) else {}
    return {
        "version": _AGENT_CASE_BENCHMARK_MATRIX_VERSION,
        "id": str(data.get("id") or ""),
        "category": str(data.get("category") or "uncategorized"),
        "url": str(data.get("url") or ""),
        "domain": _domain(str(data.get("url") or "")),
        "goal": str(data.get("goal") or ""),
        "enabled": data.get("enabled") is not False,
        "enable_xhr": bool(data.get("enable_xhr")),
        "capabilities": capabilities,
        "surfaces": surfaces,
        "route_capabilities": _case_route_capabilities(capabilities),
        "risk_tags": _case_risk_tags(data, capabilities, surfaces),
        "expected_requirements": _case_expected_requirements(data),
        "expected": dict(expected),
    }


def build_agent_case_benchmark_matrix(cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    normalized = [normalize_agent_case_benchmark_case(case) for case in (cases or [])]
    enabled = [case for case in normalized if case.get("enabled")]
    category_counts: Counter[str] = Counter()
    capability_counts: Counter[str] = Counter()
    route_capability_counts: Counter[str] = Counter()
    surface_counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()
    expected_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    for case in enabled:
        category_counts[str(case.get("category") or "uncategorized")] += 1
        if case.get("domain"):
            domain_counts[str(case.get("domain"))] += 1
        for capability in case.get("capabilities") or []:
            capability_counts[str(capability)] += 1
        for capability in case.get("route_capabilities") or []:
            route_capability_counts[str(capability)] += 1
        for surface in case.get("surfaces") or []:
            surface_counts[str(surface)] += 1
        for tag in case.get("risk_tags") or []:
            risk_counts[str(tag)] += 1
        for requirement in case.get("expected_requirements") or []:
            expected_counts[str(requirement)] += 1
    coverage = {surface: int(surface_counts.get(surface, 0)) for surface in _REQUIRED_SURFACES}
    coverage_gaps = [surface for surface, count in coverage.items() if not count]
    return {
        "version": _AGENT_CASE_BENCHMARK_MATRIX_VERSION,
        "source": "agent_case_benchmark",
        "case_count": len(normalized),
        "enabled_count": len(enabled),
        "disabled_count": len(normalized) - len(enabled),
        "required_surfaces": list(_REQUIRED_SURFACES),
        "coverage": coverage,
        "coverage_gaps": coverage_gaps,
        "by_category": _counter_rows(category_counts),
        "by_capability": _counter_rows(capability_counts),
        "by_route_capability": _counter_rows(route_capability_counts),
        "by_surface": _counter_rows(surface_counts),
        "by_risk_tag": _counter_rows(risk_counts),
        "by_expected_requirement": _counter_rows(expected_counts),
        "by_domain": _counter_rows(domain_counts),
        "cases": normalized,
    }


def _result_status(result: dict[str, Any]) -> tuple[str, bool | None]:
    raw_status = str(result.get("status") or "").strip().lower()
    if raw_status in {"not_run", "skipped", "pending", "unknown"}:
        return "not_run", None
    if "ok" in result:
        passed = bool(result.get("ok"))
        return ("passed" if passed else "failed"), passed
    if "passed" in result:
        passed = bool(result.get("passed"))
        return ("passed" if passed else "failed"), passed
    if raw_status in {"passed", "success", "ok"}:
        return "passed", True
    if raw_status in {"failed", "error"}:
        return "failed", False
    return "not_run", None


def summarize_agent_case_benchmark_results(
    cases: list[dict[str, Any]] | None = None,
    results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    matrix = build_agent_case_benchmark_matrix(cases or [])
    cases_by_id = {str(case.get("id") or ""): case for case in matrix.get("cases") or [] if str(case.get("id") or "")}
    results_by_id = {
        str(item.get("case_id") or item.get("id") or ""): dict(item)
        for item in (results or [])
        if isinstance(item, dict) and str(item.get("case_id") or item.get("id") or "")
    }
    dimension_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "evaluated": 0, "passed": 0, "failed": 0, "not_run": 0})
    rows: list[dict[str, Any]] = []
    for case_id, case in cases_by_id.items():
        if not case.get("enabled"):
            continue
        result = results_by_id.get(case_id, {})
        status, passed = _result_status(result)
        diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
        failure_type = str(diagnostics.get("failure_type") or result.get("failure_type") or "")
        row = {
            "case_id": case_id,
            "status": status,
            "passed": passed is True,
            "category": case.get("category"),
            "capabilities": list(case.get("capabilities") or []),
            "route_capabilities": list(case.get("route_capabilities") or []),
            "surfaces": list(case.get("surfaces") or []),
            "risk_tags": list(case.get("risk_tags") or []),
            "expected_requirements": list(case.get("expected_requirements") or []),
            "failure_type": failure_type,
            "output_rows": int(result.get("output_rows") or result.get("rows") or 0),
        }
        rows.append(row)
        dimensions = [f"category:{row['category']}"]
        dimensions.extend(f"capability:{item}" for item in row["capabilities"])
        dimensions.extend(f"route_capability:{item}" for item in row["route_capabilities"])
        dimensions.extend(f"surface:{item}" for item in row["surfaces"])
        dimensions.extend(f"risk:{item}" for item in row["risk_tags"])
        dimensions.extend(f"expected:{item}" for item in row["expected_requirements"])
        for dimension in dimensions:
            stats = dimension_stats[dimension]
            stats["total"] += 1
            if status == "not_run":
                stats["not_run"] += 1
            else:
                stats["evaluated"] += 1
                if passed:
                    stats["passed"] += 1
                else:
                    stats["failed"] += 1
    evaluated_count = sum(1 for row in rows if row["status"] != "not_run")
    passed_count = sum(1 for row in rows if row["status"] == "passed")
    failed_count = sum(1 for row in rows if row["status"] == "failed")
    not_run_count = sum(1 for row in rows if row["status"] == "not_run")
    dimension_rows = []
    for name, stats in dimension_stats.items():
        evaluated = int(stats["evaluated"] or 0)
        passed = int(stats["passed"] or 0)
        pass_rate = round(passed / evaluated, 4) if evaluated else 0.0
        dimension_rows.append({
            "name": name,
            "total": int(stats["total"] or 0),
            "evaluated": evaluated,
            "passed": passed,
            "failed": int(stats["failed"] or 0),
            "not_run": int(stats["not_run"] or 0),
            "pass_rate": pass_rate,
        })
    weak_dimensions = sorted(
        [item for item in dimension_rows if item["evaluated"] and item["pass_rate"] < 1.0],
        key=lambda item: (item["pass_rate"], -item["evaluated"], item["name"]),
    )[:12]
    not_run_dimensions = sorted(
        [item for item in dimension_rows if item["not_run"]],
        key=lambda item: (-item["not_run"], item["name"]),
    )[:12]
    if not rows:
        status = "empty"
    elif not evaluated_count:
        status = "needs_results"
    elif failed_count:
        status = "failed"
    elif not_run_count:
        status = "partial"
    else:
        status = "passed"
    focus_items = [item["name"] for item in weak_dimensions[:3]]
    if not focus_items:
        focus_items = [f"coverage_gap:{item}" for item in matrix.get("coverage_gaps", [])[:3]]
    if not focus_items:
        focus_items = [item["name"] for item in not_run_dimensions[:3]]
    return {
        "version": _AGENT_CASE_BENCHMARK_RESULT_VERSION,
        "source": "agent_case_benchmark",
        "status": status,
        "case_count": len(rows),
        "result_count": len(results_by_id),
        "evaluated_count": evaluated_count,
        "not_run_count": not_run_count,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "pass_rate": round(passed_count / evaluated_count, 4) if evaluated_count else 0.0,
        "result_coverage_rate": round(evaluated_count / len(rows), 4) if rows else 0.0,
        "coverage_gaps": list(matrix.get("coverage_gaps") or []),
        "weak_dimensions": weak_dimensions,
        "not_run_dimensions": not_run_dimensions,
        "recommended_focus": " · ".join(focus_items),
        "items": rows,
    }

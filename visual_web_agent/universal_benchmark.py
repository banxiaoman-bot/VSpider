from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


_UNIVERSAL_BENCHMARK_MATRIX_VERSION = "universal_benchmark_matrix.v1"
_UNIVERSAL_BENCHMARK_RESULT_VERSION = "universal_benchmark_result.v1"

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
)

_CAPABILITY_SURFACE_HINTS = {
    "api_replay": "api_backed",
    "xhr_extract": "api_backed",
    "network": "api_backed",
    "table": "html_table",
    "table_extract": "html_table",
    "card": "card_list",
    "list_extract": "card_list",
    "detail": "detail_page",
    "next_page": "pagination",
    "pagination": "pagination",
    "infinite_scroll": "infinite_scroll",
    "form": "form",
    "label_binding": "form",
    "dynamic_layout": "form",
    "component_select": "rich_component",
    "date_picker": "rich_component",
    "cascader": "rich_component",
    "slider": "rich_component",
    "modal": "modal_dialog",
    "dialog": "modal_dialog",
    "hover": "hover_menu",
    "dropdown": "hover_menu",
    "tooltip": "hover_menu",
    "iframe": "iframe",
    "shadow_dom": "shadow_dom",
    "upload": "file_upload",
    "file_upload": "file_upload",
    "download": "file_download",
    "file_download": "file_download",
    "auth": "auth_wall",
    "login": "auth_wall",
}


def _counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _case_capabilities(case: dict[str, Any]) -> list[str]:
    raw = case.get("capability") or case.get("capabilities") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(item).strip() for item in raw if str(item or "").strip()]


def _case_surfaces(case: dict[str, Any], capabilities: list[str]) -> list[str]:
    raw = case.get("surface") or case.get("surfaces") or []
    if isinstance(raw, str):
        raw = [raw]
    surfaces = [str(item).strip() for item in raw if str(item or "").strip()]
    for capability in capabilities:
        surface = _CAPABILITY_SURFACE_HINTS.get(str(capability).lower())
        if surface:
            surfaces.append(surface)
    if not surfaces:
        surfaces.append("generic_page")
    return list(dict.fromkeys(surfaces))


def normalize_universal_benchmark_case(case: dict[str, Any]) -> dict[str, Any]:
    capabilities = _case_capabilities(case)
    surfaces = _case_surfaces(case, capabilities)
    expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
    return {
        "version": _UNIVERSAL_BENCHMARK_MATRIX_VERSION,
        "id": str(case.get("id") or ""),
        "category": str(case.get("category") or "uncategorized"),
        "capabilities": capabilities,
        "surfaces": surfaces,
        "url": str(case.get("url") or ""),
        "goal": str(case.get("goal") or ""),
        "enabled": case.get("enabled") is not False,
        "expected": dict(expected),
    }


def build_universal_benchmark_matrix(cases: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = [normalize_universal_benchmark_case(case) for case in cases]
    enabled = [case for case in normalized if case.get("enabled")]
    category_counts: Counter[str] = Counter()
    capability_counts: Counter[str] = Counter()
    surface_counts: Counter[str] = Counter()
    for case in enabled:
        category_counts[str(case.get("category") or "uncategorized")] += 1
        for capability in case.get("capabilities") or []:
            capability_counts[str(capability)] += 1
        for surface in case.get("surfaces") or []:
            surface_counts[str(surface)] += 1
    coverage = {
        surface: int(surface_counts.get(surface, 0))
        for surface in _REQUIRED_SURFACES
    }
    gaps = [surface for surface, count in coverage.items() if not count]
    return {
        "version": _UNIVERSAL_BENCHMARK_MATRIX_VERSION,
        "case_count": len(normalized),
        "enabled_count": len(enabled),
        "disabled_count": len(normalized) - len(enabled),
        "required_surfaces": list(_REQUIRED_SURFACES),
        "coverage": coverage,
        "coverage_gaps": gaps,
        "by_category": _counter_rows(category_counts),
        "by_capability": _counter_rows(capability_counts),
        "by_surface": _counter_rows(surface_counts),
        "cases": normalized,
    }


def summarize_universal_benchmark_results(
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    matrix = build_universal_benchmark_matrix(cases)
    cases_by_id = {case["id"]: case for case in matrix["cases"]}
    results_by_id = {str(item.get("case_id") or item.get("id") or ""): item for item in results}
    dimension_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0})
    rows: list[dict[str, Any]] = []
    for case_id, case in cases_by_id.items():
        if not case.get("enabled"):
            continue
        result = results_by_id.get(case_id, {})
        passed = bool(result.get("ok") if "ok" in result else result.get("passed"))
        failure_type = str((result.get("diagnostics") or {}).get("failure_type") or result.get("failure_type") or "")
        row = {
            "case_id": case_id,
            "passed": passed,
            "category": case.get("category"),
            "capabilities": list(case.get("capabilities") or []),
            "surfaces": list(case.get("surfaces") or []),
            "failure_type": failure_type,
        }
        rows.append(row)
        dimensions = [f"category:{row['category']}"]
        dimensions.extend(f"capability:{item}" for item in row["capabilities"])
        dimensions.extend(f"surface:{item}" for item in row["surfaces"])
        for dimension in dimensions:
            dimension_stats[dimension]["total"] += 1
            if passed:
                dimension_stats[dimension]["passed"] += 1
    passed_count = sum(1 for row in rows if row["passed"])
    dimension_rows = []
    for name, stats in dimension_stats.items():
        total = int(stats["total"] or 0)
        passed = int(stats["passed"] or 0)
        pass_rate = round(passed / total, 4) if total else 0.0
        dimension_rows.append({"name": name, "total": total, "passed": passed, "pass_rate": pass_rate})
    weak = sorted(
        [item for item in dimension_rows if item["total"] and item["pass_rate"] < 1.0],
        key=lambda item: (item["pass_rate"], -item["total"], item["name"]),
    )[:8]
    return {
        "version": _UNIVERSAL_BENCHMARK_RESULT_VERSION,
        "case_count": len(rows),
        "passed_count": passed_count,
        "failed_count": len(rows) - passed_count,
        "pass_rate": round(passed_count / len(rows), 4) if rows else 0.0,
        "coverage_gaps": list(matrix["coverage_gaps"]),
        "weak_dimensions": weak,
        "recommended_focus": " · ".join(item["name"] for item in weak[:3]),
        "items": rows,
    }

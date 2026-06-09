from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from visual_web_agent.io_contract.output_contract import (
    normalize_output_fields,
    output_contract_fields,
)
from visual_web_agent.task_templates import TaskTemplate


_FIELD_RE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]+")


def verify_route_success(
    route: dict[str, Any],
    *,
    capability: str,
    result: dict[str, Any] | None = None,
    artifact: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload or {}
    result = result or {}
    artifact = artifact or None
    template = _route_template(route)
    target_count = _target_count(route, payload)
    required_fields = _required_fields(route, payload)
    output_contract = (route.get("strategy_context") or {}).get("output_contract") or route.get("output_contract") or {}
    save_artifact_required = bool(output_contract.get("save_artifact") or payload.get("export") or (route.get("intent") or {}).get("requires_artifact"))
    observed_count = _observed_count(capability, result)
    rows = _result_rows(capability, result)
    checks: list[dict[str, Any]] = []
    count_passed = observed_count > 0 and (target_count is None or observed_count >= target_count)
    checks.append({
        "name": "count",
        "passed": count_passed,
        "observed": observed_count,
        "target": target_count,
        "detail": "observed rows/items/results meet target" if count_passed else "not enough rows/items/results",
    })
    if required_fields:
        fields_check = _check_required_fields(rows, required_fields, target_count)
        checks.append(fields_check)
    if save_artifact_required:
        artifact_check = _check_artifact(artifact)
        checks.append(artifact_check)
    template_check = _check_template(template, result, artifact, capability)
    if template_check is not None:
        checks.append(template_check)
    passed = all(bool(item.get("passed")) for item in checks)
    failed = [str(item.get("name") or "check") for item in checks if not item.get("passed")]
    verification = {
        "passed": passed,
        "status": "passed" if passed else "failed",
        "capability": capability,
        "target_count": target_count,
        "observed_count": observed_count,
        "required_fields": required_fields,
        "save_artifact_required": save_artifact_required,
        "checks": checks,
        "summary": "success criteria met" if passed else "failed checks: " + ", ".join(failed),
    }
    verification["verification_summary"] = {
        "status": verification["status"],
        "passed_checks": [item["name"] for item in checks if item.get("passed")],
        "failed_checks": failed,
    }
    return verification


def _route_template(route: dict[str, Any]) -> TaskTemplate | None:
    template_data = route.get("template") if isinstance(route.get("template"), dict) else None
    if not template_data:
        return None
    return TaskTemplate(
        id=str(template_data.get("id") or ""),
        name=str(template_data.get("name") or ""),
        task_type=str(template_data.get("task_type") or ""),
        description=str(template_data.get("description") or ""),
        triggers=tuple(str(item) for item in (template_data.get("triggers") or []) if item),
        steps=(),
        required_capabilities=tuple(str(item) for item in (template_data.get("required_capabilities") or []) if item),
        success_criteria=dict(template_data.get("success_criteria") or {}),
        fallback_caps=tuple(str(item) for item in (template_data.get("fallback_caps") or []) if item),
        recommended_output_kind=str(template_data.get("recommended_output_kind") or "mixed"),
        recommended_container=str(template_data.get("recommended_container") or "files_folder"),
        parameters_schema=dict(template_data.get("parameters_schema") or {}),
        notes=tuple(str(item) for item in (template_data.get("notes") or []) if item),
    )


# Family-specific template checks (api_replay / login / visual) only make
# sense -- and may only veto -- when the executing capability belongs to that
# template's family. A planner may *match* an api_replay template yet the run
# may complete via a different deterministic capability (e.g. extractor_select)
# that already satisfied count / fields / artifact; that must not be vetoed by
# a check that reads a foreign result shape.
_TEMPLATE_CAPABILITY_FAMILIES = {
    "api_replay": frozenset({"api_replay", "network_intelligence"}),
    "login_then_action": frozenset({"login_then_action", "auth_harvester", "browser_control"}),
    "visual_recovery": frozenset({"visual_recovery", "browser_control"}),
}


def _check_template(
    template: TaskTemplate | None,
    result: dict[str, Any],
    artifact: dict[str, Any] | None,
    capability: str = "",
) -> dict[str, Any] | None:
    if template is None:
        return None
    if template.task_type == "crawl_pagination":
        # Count whatever the executing capability actually produced; fall back
        # to spider / generic shapes for planner-driven runs that don't pass a
        # capability.
        observed = (
            _observed_count(capability, result)
            or _observed_count("spider_lite", result)
            or _observed_count("generic_extractor", result)
        )
        return {"name": "template_count", "passed": observed > 0, "observed": observed, "detail": "pagination yielded rows" if observed > 0 else "pagination yielded no rows"}
    if template.task_type == "export_artifact":
        return {"name": "template_artifact", "passed": _artifact_exists(artifact), "detail": "artifact present" if _artifact_exists(artifact) else "artifact missing"}
    family = _TEMPLATE_CAPABILITY_FAMILIES.get(template.task_type)
    if family is not None and capability and capability not in family:
        return None
    if template.task_type == "api_replay":
        ok = bool(
            result.get("response")
            or result.get("data")
            or result.get("items")
            or result.get("rows")
            or _observed_count("api_replay", result) > 0
        )
        return {"name": "template_api", "passed": ok, "detail": "api replay returned data" if ok else "api replay returned no data"}
    if template.task_type == "login_then_action":
        ok = bool(result.get("authenticated") or result.get("login_ok") or result.get("action_result"))
        return {"name": "template_login", "passed": ok, "detail": "login/action succeeded" if ok else "login/action not confirmed"}
    if template.task_type == "visual_recovery":
        ok = bool(result.get("recovered") or result.get("action_evidence"))
        return {"name": "template_visual", "passed": ok, "detail": "visual recovery succeeded" if ok else "visual recovery not confirmed"}
    return None


def _artifact_exists(artifact: dict[str, Any] | None) -> bool:
    if not isinstance(artifact, dict) or not artifact:
        return False
    path_text = str(artifact.get("path") or "").strip()
    if path_text:
        path = Path(path_text)
        return path.exists() and path.is_file() and path.stat().st_size > 0
    return bool(str(artifact.get("url") or "").strip())


def _target_count(route: dict[str, Any], payload: dict[str, Any]) -> int | None:
    for value in (payload.get("target_count"), (route.get("strategy_context") or {}).get("target_count")):
        try:
            number = int(value or 0)
        except Exception:
            number = 0
        if number > 0:
            return number
    return None


def _required_fields(route: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    values.extend(_as_list(payload.get("required_fields")))
    values.extend(_as_list(payload.get("requested_fields")))
    if isinstance(payload.get("item_pipeline"), dict):
        values.extend(output_contract_fields(payload["item_pipeline"]))

    if isinstance(payload.get("output_contract"), dict):
        values.extend(output_contract_fields(payload["output_contract"]))

    route_contract = route.get("output_contract")
    if isinstance(route_contract, dict):
        values.extend(output_contract_fields(route_contract))

    strategy = route.get("strategy_context") if isinstance(route.get("strategy_context"), dict) else {}
    strategy_contract = strategy.get("output_contract") if isinstance(strategy.get("output_contract"), dict) else None
    values.extend(output_contract_fields(strategy_contract))
    values.extend(_as_list(strategy.get("requested_fields")))
    values.extend(_as_list(strategy.get("required_fields")))

    return normalize_output_fields(values)


def _observed_count(capability: str, result: dict[str, Any]) -> int:
    if capability == "extractor_select":
        values = result.get("results")
        if isinstance(values, list):
            return len([item for item in values if _not_empty(item)])
        try:
            return int(result.get("count") or 0)
        except Exception:
            return 0
    if capability == "spider_lite":
        try:
            return int(result.get("item_count") or len(result.get("items") or []))
        except Exception:
            return 0
    try:
        return int(result.get("row_count") or len(result.get("rows") or []))
    except Exception:
        return 0


def _result_rows(capability: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    if capability == "extractor_select":
        values = result.get("results") or []
        if not isinstance(values, list):
            return []
        return [{"value": item} for item in values]
    if capability == "spider_lite":
        values = result.get("items") or []
    else:
        values = result.get("rows") or []
    rows: list[dict[str, Any]] = []
    if isinstance(values, list):
        for item in values:
            if isinstance(item, dict):
                rows.append(dict(item))
            elif _not_empty(item):
                rows.append({"value": item})
    return rows


def _check_required_fields(rows: list[dict[str, Any]], required_fields: list[str], target_count: int | None) -> dict[str, Any]:
    valid_count = 0
    missing_examples: list[dict[str, Any]] = []
    for row in rows:
        normalized = {_field_name(key): value for key, value in row.items()}
        missing = [field for field in required_fields if not _not_empty(normalized.get(_field_name(field)))]
        if missing:
            if len(missing_examples) < 3:
                missing_examples.append({"missing": missing, "row": row})
        else:
            valid_count += 1
    target = target_count if target_count is not None else 1
    passed = valid_count >= target
    return {
        "name": "required_fields",
        "passed": passed,
        "valid_rows": valid_count,
        "target": target,
        "fields": required_fields,
        "missing_examples": missing_examples,
        "detail": "required fields are present" if passed else "required fields missing or empty",
    }


def _check_artifact(artifact: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(artifact, dict) or not artifact:
        return {"name": "artifact", "passed": False, "detail": "artifact is required but missing"}
    path_text = str(artifact.get("path") or "").strip()
    if path_text:
        path = Path(path_text)
        exists = path.exists() and path.is_file()
        size = path.stat().st_size if exists else 0
        return {
            "name": "artifact",
            "passed": exists and size > 0,
            "path": path_text,
            "size": size,
            "detail": "artifact file exists" if exists and size > 0 else "artifact file missing or empty",
        }
    url = str(artifact.get("url") or "").strip()
    return {
        "name": "artifact",
        "passed": bool(url),
        "url": url,
        "detail": "artifact URL present" if url else "artifact path/url missing",
    }


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _field_name(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower().replace("-", "_")
    text = _FIELD_RE.sub("_", text).strip("_")
    parts = [part for part in text.split("_") if part]
    return parts[-1] if parts else ""


def _not_empty(value: Any) -> bool:
    return value is not None and str(value).strip() != ""

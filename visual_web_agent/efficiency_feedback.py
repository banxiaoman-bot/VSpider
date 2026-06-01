from __future__ import annotations

from typing import Any


_PATH_CAPABILITIES = {
    "api_replay": ["network_intelligence", "api_replay"],
    "html_extract": ["generic_extractor", "extractor_select"],
    "dom_selector": ["extractor_select", "generic_extractor"],
    "targeted_probe": ["action_registry_macros", "browser_control_find", "selector_generator"],
    "browser_action": ["browser_control", "action_ref_normalizer", "selector_generator"],
    "vision_agent": ["vision_agent", "semantic_planner_reflector"],
}

_AVOID_PATH_CAPABILITIES = {
    "browser_action": ["browser_control"],
    "vision_agent": ["vision_agent"],
    "targeted_probe": ["action_registry_macros"],
}

_ROOT_CAUSE_AVOID_ACTIONS = {
    "executed_path_more_expensive_than_recommendation": ["skip_expensive_fallback_before_recommended_path"],
    "browser_used_despite_skip_browser": ["open_browser_when_deterministic_path_available"],
    "vlm_used_despite_skip_vlm": ["call_vlm_when_deterministic_path_available"],
    "action_reliability_medium": ["repeat_unreliable_action_without_repair"],
    "action_reliability_high": ["repeat_unreliable_action_without_repair"],
    "deterministic_evaluation_repair": ["accept_repair_decision_without_fixing_evidence"],
    "deterministic_evaluation_replan": ["accept_failed_evaluation_without_replan"],
    "deterministic_evaluation_reject": ["retry_rejected_path_without_new_evidence"],
    "browser_state_interactive_evidence_missing": ["targeted_probe_without_fresh_browser_state"],
    "failure_bundle_present": ["ignore_failure_bundle_before_replan"],
}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, dict) else {}


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
    for value in values:
        text = str(value or "").strip()
        if text and text != "continue" and text not in out:
            out.append(text)
    return out


def _path_capabilities(path: str) -> list[str]:
    return list(_PATH_CAPABILITIES.get(str(path or ""), []))


def _avoid_capabilities(path: str) -> list[str]:
    return list(_AVOID_PATH_CAPABILITIES.get(str(path or ""), []))


def _avoid_actions(root_causes: list[str]) -> list[str]:
    actions: list[str] = []
    for cause in root_causes:
        actions.extend(_ROOT_CAUSE_AVOID_ACTIONS.get(str(cause or ""), []))
    return _dedupe(actions)


def _recommended_action(report: dict[str, Any], preferred_path: str, executed_path: str) -> str:
    action = str(report.get("recommended_action") or "")
    if action and action != "continue":
        return action
    if preferred_path and preferred_path != "unknown" and executed_path and executed_path != preferred_path:
        return f"prefer_{preferred_path}_before_{executed_path}"
    if preferred_path and preferred_path != "unknown":
        return f"prefer_{preferred_path}"
    return "review_efficiency_correlation"


def build_efficiency_planner_feedback(
    efficiency_correlation_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = _as_dict(efficiency_correlation_report)
    if str(report.get("version") or "") != "efficiency_correlation_report.v1":
        return {}
    status = str(report.get("status") or "unknown")
    if status == "aligned" and not report.get("failed_checks"):
        return {}
    alignment = _as_dict(report.get("alignment"))
    preferred_path = str(alignment.get("recommended_path") or "")
    executed_path = str(alignment.get("executed_path") or "")
    executed_capability = str(alignment.get("executed_capability") or "")
    root_causes = _dedupe(_as_list(report.get("root_causes")))
    preferred_capabilities = _path_capabilities(preferred_path)
    if not preferred_capabilities:
        for hint in _as_list(report.get("planner_hints")):
            if isinstance(hint, dict):
                preferred_capabilities.extend(_path_capabilities(str(hint.get("path") or hint.get("prefer_path") or "")))
    avoid_capabilities = _avoid_capabilities(executed_path)
    if executed_capability and executed_path != preferred_path and executed_path in {"browser_action", "vision_agent"}:
        avoid_capabilities.append(executed_capability)
    recommended_actions = _dedupe(_as_list(report.get("recommended_actions")))
    recommended_action = _recommended_action(report, preferred_path, executed_path)
    if recommended_action and recommended_action not in recommended_actions:
        recommended_actions.insert(0, recommended_action)
    return {
        "version": "planner_feedback.v1",
        "source": "efficiency_correlation_report",
        "status": "active",
        "feedback_type": "efficiency_correlation",
        "primary_failure": root_causes[0] if root_causes else status,
        "failure_category": "efficiency_alignment",
        "failed_capability": executed_capability,
        "failed_action": executed_path,
        "fallback_reason": ",".join(root_causes),
        "recommended_action": recommended_action,
        "recommended_actions": recommended_actions,
        "preferred_capabilities": _dedupe(preferred_capabilities),
        "avoid_capabilities": _dedupe(avoid_capabilities),
        "avoid_actions": _avoid_actions(root_causes),
        "preferred_paths": _dedupe([preferred_path]),
        "avoid_paths": _dedupe([executed_path] if executed_path != preferred_path else []),
        "planner_hints": [dict(item) for item in _as_list(report.get("planner_hints")) if isinstance(item, dict)],
        "target": {"ref": "", "selector": ""},
        "efficiency_alignment": dict(alignment),
        "evidence": {
            "status": status,
            "failed_checks": _dedupe(_as_list(report.get("failed_checks"))),
            "root_causes": root_causes,
            "report_source": str(report.get("source") or ""),
        },
    }

from __future__ import annotations

from typing import Any

from visual_web_agent.capability_router import route_task
from visual_web_agent.efficiency_feedback import build_efficiency_planner_feedback
from visual_web_agent.planner_contract import build_execution_plan


_EFFICIENCY_FEEDBACK_REPLAY_REPORT_VERSION = "efficiency_feedback_replay_report.v1"
_EFFICIENCY_FEEDBACK_REPLAY_BATCH_REPORT_VERSION = "efficiency_feedback_replay_batch_report.v1"
_PLANNER_FEEDBACK_SHADOW_REPORT_VERSION = "planner_feedback_shadow_report.v1"


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


def _iter_dicts(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 5:
        return []
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        data = dict(value)
        out.append(data)
        for item in data.values():
            out.extend(_iter_dicts(item, depth + 1))
    elif isinstance(value, list):
        for item in value:
            out.extend(_iter_dicts(item, depth + 1))
    return out


def _find_versioned(source: dict[str, Any], version: str) -> dict[str, Any]:
    for item in _iter_dicts(source):
        if str(item.get("version") or "") == version:
            return dict(item)
    return {}


def _source_text(source: dict[str, Any], keys: tuple[str, ...]) -> str:
    for item in _iter_dicts(source):
        for key in keys:
            value = str(item.get(key) or "")
            if value:
                return value
    return ""


def _is_efficiency_planner_feedback(feedback: dict[str, Any]) -> bool:
    return (
        str(feedback.get("version") or "") == "planner_feedback.v1"
        and (
            str(feedback.get("source") or "") == "efficiency_correlation_report"
            or str(feedback.get("feedback_type") or "") == "efficiency_correlation"
        )
    )


def efficiency_feedback_replay_source_kind(source: dict[str, Any] | None = None) -> str:
    data = _as_dict(source)
    if _find_versioned(data, "efficiency_correlation_report.v1"):
        return "efficiency_correlation_report"
    if _is_efficiency_planner_feedback(_find_versioned(data, "planner_feedback.v1")):
        return "planner_feedback"
    return ""


def _increment_count(counts: dict[str, int], value: Any) -> None:
    name = str(value or "")
    if name:
        counts[name] = counts.get(name, 0) + 1


def _count_rows(counts: dict[str, int]) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _check(name: str, actual: Any, expected: Any) -> dict[str, Any]:
    return {"name": name, "passed": actual == expected, "actual": actual, "expected": expected}


def _contains_check(name: str, values: list[Any], expected: Any) -> dict[str, Any]:
    return {"name": name, "passed": expected in values, "actual": values, "expected": expected}


def _step_with_feedback(execution_plan: dict[str, Any], preferred_capabilities: list[str]) -> dict[str, Any]:
    preferred = {str(item or "") for item in preferred_capabilities if str(item or "")}
    for step in _as_list(execution_plan.get("steps")):
        if isinstance(step, dict) and str(step.get("capability") or "") in preferred and isinstance(step.get("inputs"), dict) and step["inputs"].get("planner_feedback"):
            return dict(step)
    for step in _as_list(execution_plan.get("steps")):
        if isinstance(step, dict) and isinstance(step.get("inputs"), dict) and step["inputs"].get("planner_feedback"):
            return dict(step)
    return {}


def _capability_names(items: Any, key: str = "name") -> list[str]:
    return [
        str(item.get(key) or "")
        for item in _as_list(items)
        if isinstance(item, dict) and str(item.get(key) or "")
    ]


def _steps_with_planner_feedback(execution_plan: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for step in _as_list(execution_plan.get("steps")):
        if isinstance(step, dict) and isinstance(step.get("inputs"), dict) and step["inputs"].get("planner_feedback"):
            capability = str(step.get("capability") or "")
            if capability:
                out.append(capability)
    return out


def _feedback_from_source(source: dict[str, Any]) -> dict[str, Any]:
    feedback = _as_dict(source.get("planner_feedback"))
    if _is_efficiency_planner_feedback(feedback):
        return feedback
    if str(source.get("version") or "") == "planner_feedback.v1" and _is_efficiency_planner_feedback(source):
        return dict(source)
    report = _as_dict(source.get("efficiency_correlation_report")) or _find_versioned(source, "efficiency_correlation_report.v1") or source
    return build_efficiency_planner_feedback(report)


def replay_efficiency_feedback(
    source: dict[str, Any] | None = None,
    *,
    goal: str = "Review efficiency feedback before next route",
    url: str = "",
) -> dict[str, Any]:
    data = _as_dict(source)
    planner_feedback = _feedback_from_source(data)
    preferred_capabilities = [str(item) for item in _as_list(planner_feedback.get("preferred_capabilities")) if str(item or "")]
    primary_failure = str(planner_feedback.get("primary_failure") or "")
    route_goal = str(data.get("goal") or _source_text(data, ("goal", "prompt", "task")) or goal or "Review efficiency feedback before next route")
    route_url = str(data.get("url") or _source_text(data, ("url", "target_url", "start_url")) or url or "")
    route = route_task(
        route_goal,
        url=route_url,
        context={"efficiency_feedback": planner_feedback},
        runtime_status={"version": "browser_runtime.v1", "status": "available"},
    )
    route_with_feedback = dict(route)
    route_signals = dict(route.get("signals") or {})
    if planner_feedback:
        route_signals["planner_feedback"] = True
        route_signals["previous_failure"] = primary_failure
        route_with_feedback["signals"] = route_signals
        route_with_feedback["planner_feedback"] = planner_feedback
    execution_plan = build_execution_plan(route_with_feedback) if planner_feedback else {}
    feedback_step = _step_with_feedback(execution_plan, preferred_capabilities)
    risk_flags = [str(item) for item in _as_list(execution_plan.get("risk_flags")) if str(item or "")]
    notes = [str(item) for item in _as_list(execution_plan.get("notes")) if str(item or "")]
    checks = [
        _check("planner_feedback.version", str(planner_feedback.get("version") or ""), "planner_feedback.v1"),
        _check("planner_feedback.source", str(planner_feedback.get("source") or ""), "efficiency_correlation_report"),
        _check("planner_feedback.feedback_type", str(planner_feedback.get("feedback_type") or ""), "efficiency_correlation"),
        _check("planner_feedback.preferred_capabilities", bool(preferred_capabilities), True),
        _check("route.signals.planner_feedback", bool((route_with_feedback.get("signals") or {}).get("planner_feedback")), True),
        _check("route.planner_feedback.primary_failure", str((route_with_feedback.get("planner_feedback") or {}).get("primary_failure") or ""), primary_failure),
        _check("execution_plan.version", str(execution_plan.get("version") or ""), "planner_contract.v1"),
        _check("execution_plan.planner_feedback.primary_failure", str((execution_plan.get("planner_feedback") or {}).get("primary_failure") or ""), primary_failure),
        _contains_check("execution_plan.risk_flags.previous_failure_feedback_active", risk_flags, "previous_failure_feedback_active"),
        _contains_check("execution_plan.risk_flags.previous_efficiency_failure", risk_flags, f"previous_failure_{primary_failure}"),
        _check("execution_plan.step_inputs.planner_feedback", bool(feedback_step), True),
        _check("execution_plan.notes.efficiency_feedback", any("Planner feedback from previous failure" in item for item in notes), True),
    ]
    passed = all(bool(item.get("passed")) for item in checks)
    return {
        "version": _EFFICIENCY_FEEDBACK_REPLAY_REPORT_VERSION,
        "source": "efficiency_feedback_replay",
        "passed": passed,
        "planner_feedback": dict(planner_feedback),
        "route": {
            "passed": bool((route_with_feedback.get("signals") or {}).get("planner_feedback")) and bool(route_with_feedback.get("planner_feedback")),
            "intent": dict(route_with_feedback.get("intent") or {}),
            "signals": dict(route_with_feedback.get("signals") or {}),
            "planner_feedback": dict(route_with_feedback.get("planner_feedback") or {}),
        },
        "execution_plan": {
            "passed": bool(execution_plan.get("planner_feedback")) and "previous_failure_feedback_active" in risk_flags,
            "version": str(execution_plan.get("version") or ""),
            "risk_flags": risk_flags,
            "notes": notes,
            "planner_feedback": dict(execution_plan.get("planner_feedback") or {}),
            "feedback_step": feedback_step,
        },
        "checks": checks,
        "failed_checks": [dict(item) for item in checks if not bool(item.get("passed"))],
    }


def evaluate_planner_feedback_shadow(
    source: dict[str, Any] | None = None,
    *,
    goal: str = "Review planner feedback shadow impact",
    url: str = "",
) -> dict[str, Any]:
    data = _as_dict(source)
    planner_feedback = _feedback_from_source(data)
    preferred_capabilities = [str(item) for item in _as_list(planner_feedback.get("preferred_capabilities")) if str(item or "")]
    primary_failure = str(planner_feedback.get("primary_failure") or "")
    route_goal = str(data.get("goal") or _source_text(data, ("goal", "prompt", "task")) or goal or "Review planner feedback shadow impact")
    route_url = str(data.get("url") or _source_text(data, ("url", "target_url", "start_url")) or url or "")
    route_context = _as_dict(data.get("context"))
    for key in ("failure_bundle", "capability_execute_failure_bundle", "last_failure_bundle", "capability_execute", "planner_feedback", "efficiency_feedback"):
        route_context.pop(key, None)
    baseline_route = route_task(
        route_goal,
        url=route_url,
        context=route_context,
        runtime_status={"version": "browser_runtime.v1", "status": "available"},
    )
    baseline_plan = dict(baseline_route.get("execution_plan") or build_execution_plan(baseline_route))
    shadow_route = dict(baseline_route)
    baseline_signals = dict(baseline_route.get("signals") or {})
    shadow_signals = dict(baseline_signals)
    if planner_feedback:
        shadow_signals["planner_feedback"] = True
        shadow_signals["previous_failure"] = primary_failure
        shadow_route["signals"] = shadow_signals
        shadow_route["planner_feedback"] = planner_feedback
    shadow_plan = build_execution_plan(shadow_route) if planner_feedback else {}
    feedback_step = _step_with_feedback(shadow_plan, preferred_capabilities)
    baseline_backend = _capability_names(baseline_route.get("backend_plan"))
    shadow_backend = _capability_names(shadow_route.get("backend_plan"))
    baseline_fallback = _capability_names(baseline_route.get("fallback_chain"), key="capability")
    shadow_fallback = _capability_names(shadow_route.get("fallback_chain"), key="capability")
    baseline_risk_flags = [str(item) for item in _as_list(baseline_plan.get("risk_flags")) if str(item or "")]
    shadow_risk_flags = [str(item) for item in _as_list(shadow_plan.get("risk_flags")) if str(item or "")]
    baseline_notes = [str(item) for item in _as_list(baseline_plan.get("notes")) if str(item or "")]
    shadow_notes = [str(item) for item in _as_list(shadow_plan.get("notes")) if str(item or "")]
    checks = [
        _check("planner_feedback.version", str(planner_feedback.get("version") or ""), "planner_feedback.v1"),
        _check("baseline.signals.planner_feedback", bool(baseline_signals.get("planner_feedback")), False),
        _check("shadow.signals.planner_feedback", bool(shadow_signals.get("planner_feedback")), True),
        _check("backend_plan.unchanged", shadow_backend, baseline_backend),
        _check("fallback_chain.unchanged", shadow_fallback, baseline_fallback),
        _check("shadow.execution_plan.version", str(shadow_plan.get("version") or ""), "planner_contract.v1"),
        _check("shadow.execution_plan.planner_feedback.primary_failure", str((shadow_plan.get("planner_feedback") or {}).get("primary_failure") or ""), primary_failure),
        _contains_check("shadow.execution_plan.risk_flags.previous_failure_feedback_active", shadow_risk_flags, "previous_failure_feedback_active"),
        _check("shadow.execution_plan.step_inputs.planner_feedback", bool(feedback_step), True),
    ]
    passed = all(bool(item.get("passed")) for item in checks)
    return {
        "version": _PLANNER_FEEDBACK_SHADOW_REPORT_VERSION,
        "source": "planner_feedback_shadow",
        "passed": passed,
        "planner_feedback": dict(planner_feedback),
        "baseline": {
            "signals": baseline_signals,
            "backend_plan": baseline_backend,
            "fallback_chain": baseline_fallback,
            "execution_plan": {
                "version": str(baseline_plan.get("version") or ""),
                "risk_flags": baseline_risk_flags,
                "notes": baseline_notes,
                "planner_feedback_steps": _steps_with_planner_feedback(baseline_plan),
            },
        },
        "shadow": {
            "signals": shadow_signals,
            "backend_plan": shadow_backend,
            "fallback_chain": shadow_fallback,
            "execution_plan": {
                "version": str(shadow_plan.get("version") or ""),
                "risk_flags": shadow_risk_flags,
                "notes": shadow_notes,
                "planner_feedback_steps": _steps_with_planner_feedback(shadow_plan),
                "feedback_step": feedback_step,
            },
        },
        "delta": {
            "backend_plan_changed": shadow_backend != baseline_backend,
            "fallback_chain_changed": shadow_fallback != baseline_fallback,
            "signals_added": [key for key, value in shadow_signals.items() if baseline_signals.get(key) != value],
            "risk_flags_added": [flag for flag in shadow_risk_flags if flag not in baseline_risk_flags],
            "notes_added": [note for note in shadow_notes if note not in baseline_notes],
            "planner_feedback_steps_added": [
                capability for capability in _steps_with_planner_feedback(shadow_plan) if capability not in _steps_with_planner_feedback(baseline_plan)
            ],
            "feedback_step": str(feedback_step.get("capability") or ""),
            "recommended_action": str(planner_feedback.get("recommended_action") or ""),
        },
        "checks": checks,
        "failed_checks": [dict(item) for item in checks if not bool(item.get("passed"))],
    }


def replay_efficiency_feedback_batch(sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    failed_check_counts: dict[str, int] = {}
    primary_failure_counts: dict[str, int] = {}
    recommended_action_counts: dict[str, int] = {}
    preferred_capability_counts: dict[str, int] = {}
    feedback_step_counts: dict[str, int] = {}
    for index, source in enumerate(sources or []):
        data = dict(source or {}) if isinstance(source, dict) else {}
        report = replay_efficiency_feedback(data)
        planner_feedback = dict(report.get("planner_feedback") or {}) if isinstance(report.get("planner_feedback"), dict) else {}
        execution_plan = dict(report.get("execution_plan") or {}) if isinstance(report.get("execution_plan"), dict) else {}
        feedback_step = dict(execution_plan.get("feedback_step") or {}) if isinstance(execution_plan.get("feedback_step"), dict) else {}
        checks = [dict(item) for item in (report.get("checks") or []) if isinstance(item, dict)]
        failed_checks = [
            str(item.get("name") or "")
            for item in checks
            if not bool(item.get("passed")) and str(item.get("name") or "")
        ]
        for name in failed_checks:
            _increment_count(failed_check_counts, name)
        primary_failure = str(planner_feedback.get("primary_failure") or "")
        recommended_action = str(planner_feedback.get("recommended_action") or "")
        preferred_capabilities = [
            str(item)
            for item in _as_list(planner_feedback.get("preferred_capabilities"))
            if str(item or "")
        ]
        for capability in preferred_capabilities:
            _increment_count(preferred_capability_counts, capability)
        feedback_step_capability = str(feedback_step.get("capability") or "")
        _increment_count(primary_failure_counts, primary_failure)
        _increment_count(recommended_action_counts, recommended_action)
        _increment_count(feedback_step_counts, feedback_step_capability)
        items.append({
            "index": index,
            "name": str(data.get("name") or data.get("run_id") or primary_failure or f"source_{index + 1}"),
            "source_kind": efficiency_feedback_replay_source_kind(data),
            "primary_failure": primary_failure,
            "recommended_action": recommended_action,
            "preferred_capabilities": preferred_capabilities[:8],
            "feedback_step": feedback_step_capability,
            "passed": bool(report.get("passed")),
            "check_count": len(checks),
            "failed_check_count": len(failed_checks),
            "failed_checks": failed_checks[:8],
            "report": report,
        })
    passed_count = sum(1 for item in items if bool(item.get("passed")))
    failed_count = len(items) - passed_count
    failed_checks = _count_rows(failed_check_counts)
    status = "empty" if not items else "passed" if failed_count == 0 else "failed"
    top_primary_failures = _count_rows(primary_failure_counts)
    top_failed_checks = failed_checks[:5]
    recommended_focus_parts = []
    if top_primary_failures:
        recommended_focus_parts.append(f"primary_failure={top_primary_failures[0]['name']}")
    if top_failed_checks:
        recommended_focus_parts.append(f"check={top_failed_checks[0]['name']}")
    return {
        "version": _EFFICIENCY_FEEDBACK_REPLAY_BATCH_REPORT_VERSION,
        "source": "efficiency_feedback_replay_batch",
        "source_count": len(items),
        "passed_count": passed_count,
        "failed_count": failed_count,
        "passed": bool(items) and failed_count == 0,
        "summary": {
            "status": status,
            "blocking": failed_count > 0,
            "top_primary_failures": top_primary_failures,
            "top_recommended_actions": _count_rows(recommended_action_counts),
            "top_preferred_capabilities": _count_rows(preferred_capability_counts),
            "top_feedback_steps": _count_rows(feedback_step_counts),
            "top_failed_checks": top_failed_checks,
            "recommended_focus": " · ".join(recommended_focus_parts),
        },
        "failed_checks": failed_checks,
        "items": items,
    }

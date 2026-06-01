from __future__ import annotations

from typing import Any


_EFFICIENCY_CORRELATION_REPORT_VERSION = "efficiency_correlation_report.v1"
_BROWSER_OR_VISION_PATHS = {"targeted_probe", "browser_action", "vision_agent"}
_CAPABILITY_PATHS = {
    "api_replay": "api_replay",
    "generic_extractor": "html_extract",
    "spider_lite": "html_extract",
    "extractor_select": "dom_selector",
    "action_registry_macros": "targeted_probe",
    "browser_control": "browser_action",
    "vision_agent": "vision_agent",
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


def _dedupe(items: list[Any]) -> list[str]:
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text != "continue" and text not in out:
            out.append(text)
    return out


def _latest_attempt(result: dict[str, Any]) -> dict[str, Any]:
    attempts = result.get("attempts")
    if isinstance(attempts, list):
        for item in reversed(attempts):
            if isinstance(item, dict):
                return dict(item)
    return {}


def _executed_capability(execution_result: dict[str, Any], deterministic_evaluation: dict[str, Any]) -> str:
    latest_attempt = _latest_attempt(execution_result)
    return str(
        execution_result.get("capability")
        or deterministic_evaluation.get("capability")
        or latest_attempt.get("capability")
        or ""
    )


def _executed_path(capability: str, execution_result: dict[str, Any], plan: dict[str, Any]) -> str:
    explicit = str(execution_result.get("crawl_efficiency_path") or "")
    if explicit:
        return explicit
    preferred = [str(item) for item in _as_list(plan.get("preferred_order")) if str(item or "")]
    if capability in preferred:
        return capability
    return _CAPABILITY_PATHS.get(capability, capability or "unknown")


def _rank(path: str, plan: dict[str, Any]) -> int | None:
    preferred = [str(item) for item in _as_list(plan.get("preferred_order")) if str(item or "")]
    if path in preferred:
        return preferred.index(path)
    return None


def _interactive_count(browser_state: dict[str, Any]) -> int:
    metrics = _as_dict(browser_state.get("metrics"))
    if metrics.get("interactive_count") not in (None, ""):
        return int(metrics.get("interactive_count") or 0)
    perception = _as_dict(browser_state.get("perception"))
    som = _as_dict(perception.get("som"))
    return int(som.get("interactive_count") or len(_as_list(som.get("elements"))))


def _runtime_status(browser_state: dict[str, Any], plan: dict[str, Any]) -> str:
    state_runtime = _as_dict(browser_state.get("runtime"))
    if state_runtime:
        return str(state_runtime.get("status") or state_runtime.get("runtime_status") or "unknown")
    summary = _as_dict(plan.get("efficiency_summary"))
    return str(summary.get("runtime_status") or "unknown")


def _check(name: str, passed: bool, severity: str, detail: str, **extra: Any) -> dict[str, Any]:
    item = {"name": name, "passed": bool(passed), "severity": str(severity or "info"), "detail": str(detail or "")}
    item.update(extra)
    return item


def _planner_hint(kind: str, message: str, **extra: Any) -> dict[str, Any]:
    item = {"kind": str(kind or "hint"), "message": str(message or "")}
    item.update(extra)
    return item


def build_efficiency_correlation_report(
    execution_result: dict[str, Any] | None = None,
    *,
    crawl_efficiency_plan: dict[str, Any] | None = None,
    deterministic_evaluation: dict[str, Any] | None = None,
    action_reliability: dict[str, Any] | None = None,
    browser_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = _as_dict(execution_result)
    plan = _as_dict(crawl_efficiency_plan) or _as_dict(result.get("crawl_efficiency_plan"))
    evaluation = _as_dict(deterministic_evaluation) or _as_dict(result.get("deterministic_evaluation"))
    reliability = _as_dict(action_reliability) or _as_dict(result.get("action_reliability"))
    state = _as_dict(browser_state) or _as_dict(result.get("browser_state"))
    failure_bundle = _as_dict(result.get("failure_bundle"))
    recommended_path = str(plan.get("recommended_path") or "unknown")
    available_paths = [str(item) for item in _as_list(plan.get("available_paths")) if str(item or "")]
    capability = _executed_capability(result, evaluation)
    executed_path = _executed_path(capability, result, plan)
    recommended_rank = _rank(recommended_path, plan)
    executed_rank = _rank(executed_path, plan)
    rank_gap = None
    if recommended_rank is not None and executed_rank is not None:
        rank_gap = executed_rank - recommended_rank
    matched = bool(recommended_path and recommended_path != "unknown" and executed_path == recommended_path)
    used_more_expensive_path = bool(rank_gap is not None and rank_gap > 0)
    skip_browser = bool(plan.get("skip_browser"))
    skip_vlm = bool(plan.get("skip_vlm"))
    browser_used = executed_path in _BROWSER_OR_VISION_PATHS
    vlm_used = executed_path == "vision_agent"
    evaluation_decision = str(evaluation.get("decision") or "unknown")
    evaluation_passed = bool(evaluation.get("passed")) if evaluation else bool(result.get("completed"))
    risk_level = str(reliability.get("risk_level") or "unknown")
    risk_factors = [str(item.get("code") or "") for item in _as_list(reliability.get("risk_factors")) if isinstance(item, dict)]
    interactive_count = _interactive_count(state) if state else int(_as_dict(plan.get("efficiency_summary")).get("interactive_count") or 0)
    runtime_status = _runtime_status(state, plan)
    checks: list[dict[str, Any]] = []
    checks.append(_check(
        "crawl_efficiency_plan_present",
        str(plan.get("version") or "") == "crawl_efficiency_plan.v1",
        "error",
        "crawl_efficiency_plan.v1 evidence present" if str(plan.get("version") or "") == "crawl_efficiency_plan.v1" else "crawl efficiency plan missing or invalid",
        observed=plan.get("version"),
    ))
    checks.append(_check(
        "executed_matches_recommended",
        matched or not used_more_expensive_path,
        "warn",
        "executed path matches or is not more expensive than recommendation" if matched or not used_more_expensive_path else f"recommended {recommended_path} but executed {executed_path}",
        recommended_path=recommended_path,
        executed_path=executed_path,
        rank_gap=rank_gap,
    ))
    checks.append(_check(
        "skip_browser_respected",
        not (skip_browser and browser_used),
        "warn",
        "browser was skipped as recommended" if not (skip_browser and browser_used) else f"plan recommended skip_browser but executed {executed_path}",
        skip_browser=skip_browser,
        browser_used=browser_used,
    ))
    checks.append(_check(
        "skip_vlm_respected",
        not (skip_vlm and vlm_used),
        "warn",
        "VLM was skipped as recommended" if not (skip_vlm and vlm_used) else "plan recommended skip_vlm but executed vision_agent",
        skip_vlm=skip_vlm,
        vlm_used=vlm_used,
    ))
    if evaluation:
        checks.append(_check(
            "deterministic_evaluation_accepts",
            evaluation_decision == "accept" and evaluation_passed,
            "error" if evaluation_decision in {"replan", "reject"} else "warn",
            f"deterministic evaluation decision={evaluation_decision}",
            decision=evaluation_decision,
            evaluation_passed=evaluation_passed,
        ))
    if reliability:
        checks.append(_check(
            "action_reliability_low",
            risk_level in {"low", "unknown"},
            "error" if risk_level == "high" else "warn",
            f"action reliability risk={risk_level}",
            risk_level=risk_level,
            risk_factors=risk_factors,
        ))
    if state or recommended_path in {"targeted_probe", "browser_action"}:
        checks.append(_check(
            "browser_state_sufficient",
            recommended_path not in {"targeted_probe", "browser_action"} or interactive_count > 0,
            "warn",
            "browser_state has interactive evidence" if interactive_count > 0 else "browser action/probe path lacks interactive browser_state evidence",
            interactive_count=interactive_count,
            runtime_status=runtime_status,
        ))
    if failure_bundle:
        checks.append(_check(
            "failure_bundle_absent",
            False,
            "error",
            str(failure_bundle.get("primary_failure") or "failure bundle present"),
            primary_failure=failure_bundle.get("primary_failure"),
        ))
    failed_checks = [str(item.get("name") or "check") for item in checks if not item.get("passed")]
    root_causes: list[str] = []
    if used_more_expensive_path:
        root_causes.append("executed_path_more_expensive_than_recommendation")
    if skip_browser and browser_used:
        root_causes.append("browser_used_despite_skip_browser")
    if skip_vlm and vlm_used:
        root_causes.append("vlm_used_despite_skip_vlm")
    if risk_level == "medium":
        root_causes.append("action_reliability_medium")
    if risk_level == "high":
        root_causes.append("action_reliability_high")
    if evaluation_decision in {"repair", "replan", "reject"}:
        root_causes.append(f"deterministic_evaluation_{evaluation_decision}")
    if failure_bundle:
        root_causes.append("failure_bundle_present")
    if recommended_path in {"targeted_probe", "browser_action"} and interactive_count <= 0:
        root_causes.append("browser_state_interactive_evidence_missing")
    planner_hints: list[dict[str, Any]] = []
    if recommended_path != "unknown":
        planner_hints.append(_planner_hint("prefer_path", f"Prefer {recommended_path} when the same evidence is available", path=recommended_path))
    if used_more_expensive_path:
        planner_hints.append(_planner_hint("avoid_unnecessary_fallback", f"Avoid {executed_path} before trying {recommended_path}", avoid_path=executed_path, prefer_path=recommended_path))
    if risk_level in {"medium", "high"}:
        planner_hints.append(_planner_hint("repair_reliability", f"Repair action reliability risk={risk_level} before repeating the same path", risk_level=risk_level, risk_factors=risk_factors))
    if failure_bundle:
        planner_hints.append(_planner_hint("use_failure_feedback", "Project failure bundle into planner feedback before the next attempt", primary_failure=failure_bundle.get("primary_failure")))
    actions = []
    if not failed_checks:
        actions.append("keep_current_path")
    if used_more_expensive_path and recommended_path != "unknown":
        actions.append(f"prefer_{recommended_path}_before_{executed_path}")
    if skip_browser and browser_used:
        actions.append("avoid_browser_when_skip_browser_true")
    if skip_vlm and vlm_used:
        actions.append("avoid_vlm_when_skip_vlm_true")
    if recommended_path in {"targeted_probe", "browser_action"} and interactive_count <= 0:
        actions.append("refresh_browser_snapshot")
    policy = _as_dict(reliability.get("self_healing_policy"))
    actions.extend(_as_list(policy.get("recommended_actions")))
    actions.extend(_as_list(evaluation.get("recommended_actions")))
    actions.extend(_as_list(failure_bundle.get("recommended_actions")))
    if evaluation_decision in {"replan", "reject"} or risk_level == "high" or failure_bundle:
        actions.append("replan_with_efficiency_feedback")
    if evaluation_decision == "repair" or risk_level == "medium":
        actions.append("repair_and_retry_with_efficiency_plan")
    recommended_actions = _dedupe(actions) or ["continue"]
    if evaluation_decision in {"replan", "reject"} or risk_level == "high" or failure_bundle:
        status = "needs_replan"
    elif evaluation_decision == "repair" or risk_level == "medium":
        status = "needs_repair"
    elif used_more_expensive_path or (skip_browser and browser_used) or (skip_vlm and vlm_used):
        status = "suboptimal"
    else:
        status = "aligned"
    return {
        "version": _EFFICIENCY_CORRELATION_REPORT_VERSION,
        "source": "efficiency_correlation",
        "status": status,
        "passed": status == "aligned",
        "alignment": {
            "recommended_path": recommended_path,
            "executed_path": executed_path,
            "executed_capability": capability,
            "matched": matched,
            "recommended_rank": recommended_rank,
            "executed_rank": executed_rank,
            "rank_gap": rank_gap,
            "available_paths": available_paths,
            "skip_browser": skip_browser,
            "skip_vlm": skip_vlm,
        },
        "checks": checks,
        "failed_checks": failed_checks,
        "root_causes": _dedupe(root_causes),
        "planner_hints": planner_hints,
        "recommended_action": recommended_actions[0],
        "recommended_actions": recommended_actions,
        "evidence": {
            "evaluation_decision": evaluation_decision,
            "evaluation_passed": evaluation_passed,
            "action_reliability_risk": risk_level,
            "action_reliability_score": reliability.get("score"),
            "risk_factors": risk_factors,
            "browser_state_version": state.get("version"),
            "interactive_count": interactive_count,
            "runtime_status": runtime_status,
            "failure_bundle": failure_bundle,
        },
    }

from __future__ import annotations

from typing import Any

from visual_web_agent.success_verifier import verify_route_success


_DETERMINISTIC_EVALUATION_VERSION = "deterministic_evaluation.v1"


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


def _latest_attempt(result: dict[str, Any]) -> dict[str, Any]:
    attempts = result.get("attempts")
    if isinstance(attempts, list):
        for item in reversed(attempts):
            if isinstance(item, dict):
                return dict(item)
    return {}


def _verification_from_result(
    execution_result: dict[str, Any],
    route: dict[str, Any],
    payload: dict[str, Any],
    capability: str,
) -> dict[str, Any]:
    verification = execution_result.get("verification")
    if isinstance(verification, dict):
        return dict(verification)
    result_payload = execution_result.get("result")
    if isinstance(result_payload, dict) and capability:
        return verify_route_success(
            route,
            capability=capability,
            result=result_payload,
            artifact=_as_dict(execution_result.get("artifact")),
            payload=payload,
        )
    return {}


def _check(name: str, passed: bool, severity: str, detail: str, **extra: Any) -> dict[str, Any]:
    item = {"name": name, "passed": bool(passed), "severity": str(severity or "info"), "detail": str(detail or "")}
    item.update(extra)
    return item


def build_deterministic_evaluation(
    execution_result: dict[str, Any] | None = None,
    *,
    route: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    browser_state: dict[str, Any] | None = None,
    action_reliability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = _as_dict(execution_result)
    route_data = _as_dict(route) or _as_dict(result.get("route"))
    payload_data = _as_dict(payload)
    latest_attempt = _latest_attempt(result)
    capability = str(result.get("capability") or latest_attempt.get("capability") or "")
    verification = _verification_from_result(result, route_data, payload_data, capability)
    reliability = _as_dict(action_reliability) or _as_dict(result.get("action_reliability"))
    policy = _as_dict(reliability.get("self_healing_policy"))
    state = _as_dict(browser_state) or _as_dict(result.get("browser_state"))
    failure_bundle = _as_dict(result.get("failure_bundle"))
    checks: list[dict[str, Any]] = []
    completed = bool(result.get("completed"))
    execution_status = str(result.get("status") or "unknown")
    checks.append(_check(
        "executor_completed",
        completed and execution_status not in {"error", "failed"},
        "error",
        "executor returned completed result" if completed else str(result.get("fallback_reason") or "executor did not complete"),
        observed=execution_status,
    ))
    if verification:
        checks.append(_check(
            "success_verification",
            bool(verification.get("passed")),
            "error",
            str(verification.get("summary") or "verification present"),
            observed=verification.get("observed_count"),
            target=verification.get("target_count"),
            checks=verification.get("checks") or [],
        ))
    if state:
        checks.append(_check(
            "browser_state_contract",
            str(state.get("version") or "") == "browser_state.v2",
            "warn",
            "browser_state.v2 evidence present" if str(state.get("version") or "") == "browser_state.v2" else "browser_state contract missing or invalid",
            observed=state.get("version"),
        ))
    if reliability:
        risk_level = str(reliability.get("risk_level") or "unknown")
        checks.append(_check(
            "action_reliability",
            risk_level == "low",
            "warn" if risk_level == "medium" else "error",
            f"action reliability risk={risk_level}",
            observed=reliability.get("score"),
            risk_level=risk_level,
            risk_factors=reliability.get("risk_factors") or [],
        ))
    if failure_bundle:
        checks.append(_check(
            "failure_bundle",
            False,
            "error",
            str(failure_bundle.get("primary_failure") or failure_bundle.get("recommended_action") or "failure bundle present"),
            observed=failure_bundle.get("status"),
        ))
    failed_error_checks = [item for item in checks if not item.get("passed") and item.get("severity") == "error"]
    failed_warn_checks = [item for item in checks if not item.get("passed") and item.get("severity") == "warn"]
    recommended_actions = _dedupe(
        _as_list(policy.get("recommended_actions"))
        + _as_list(failure_bundle.get("recommended_actions"))
        + [policy.get("recommended_action"), failure_bundle.get("recommended_action")]
    )
    if failed_error_checks and not recommended_actions:
        recommended_actions = ["inspect_failed_checks"]
    if failed_warn_checks and not recommended_actions:
        recommended_actions = ["repair_and_retry"]
    if not failed_error_checks and not failed_warn_checks:
        recommended_actions = ["continue"]
    if failed_error_checks:
        if policy.get("requires_planner_replan") or failure_bundle:
            decision = "replan"
        elif policy.get("retry_allowed"):
            decision = "repair"
        else:
            decision = "reject"
    elif failed_warn_checks:
        decision = "repair"
    else:
        decision = "accept"
    status = "passed" if decision == "accept" else ("needs_repair" if decision == "repair" else "failed")
    return {
        "version": _DETERMINISTIC_EVALUATION_VERSION,
        "source": "deterministic_evaluator",
        "status": status,
        "passed": decision == "accept",
        "decision": decision,
        "capability": capability,
        "checks": checks,
        "failed_checks": [str(item.get("name") or "check") for item in checks if not item.get("passed")],
        "recommended_action": recommended_actions[0],
        "recommended_actions": recommended_actions,
        "role_boundaries": {
            "planner": "proposes route and fallback strategy",
            "executor": "runs deterministic capability and returns evidence",
            "evaluator": "accepts, repairs, replans, or rejects from deterministic evidence",
        },
        "evidence": {
            "execution_status": execution_status,
            "completed": completed,
            "verification": verification,
            "action_reliability": reliability,
            "browser_state_version": state.get("version"),
            "failure_bundle": failure_bundle,
        },
    }

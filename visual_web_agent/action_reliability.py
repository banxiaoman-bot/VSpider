from __future__ import annotations

from typing import Any


_ACTION_RELIABILITY_SCORE_VERSION = "action_reliability_score.v1"
_SELF_HEALING_POLICY_VERSION = "self_healing_policy.v1"

_ELEMENT_ACTIONS = {"click", "dblclick", "focus", "hover", "fill", "type", "press", "check", "uncheck", "select", "scrollintoview", "upload", "get", "is", "wait"}
_RUNTIME_BAD_STATUSES = {"unavailable", "unhealthy", "error", "failed", "offline"}


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
    values: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text != "continue" and text not in values:
            values.append(text)
    return values


def _history_stats(history: Any) -> dict[str, Any]:
    if isinstance(history, dict):
        attempts = int(history.get("attempt_count") or history.get("attempts") or 0)
        failures = int(history.get("failed_count") or history.get("failures") or 0)
        successes = int(history.get("passed_count") or history.get("successes") or 0)
        if not attempts:
            attempts = failures + successes
        failure_rate = float(history.get("failure_rate") or (failures / attempts if attempts else 0.0))
        return {
            "attempt_count": attempts,
            "failed_count": failures,
            "passed_count": successes,
            "failure_rate": round(max(0.0, min(1.0, failure_rate)), 4),
        }
    rows = [item for item in _as_list(history) if isinstance(item, dict)]
    attempts = len(rows)
    failures = sum(1 for item in rows if item.get("ok") is False or item.get("passed") is False or str(item.get("status") or "").lower() == "error")
    successes = attempts - failures
    return {
        "attempt_count": attempts,
        "failed_count": failures,
        "passed_count": successes,
        "failure_rate": round(failures / attempts, 4) if attempts else 0.0,
    }


def _add_factor(factors: list[dict[str, Any]], code: str, category: str, weight: float, message: str) -> None:
    factors.append({
        "code": code,
        "category": category,
        "weight": round(max(0.0, min(1.0, float(weight or 0.0))), 4),
        "message": message,
    })


def _runtime_status(browser_state: dict[str, Any]) -> str:
    runtime = _as_dict(browser_state.get("runtime"))
    return str(runtime.get("status") or runtime.get("runtime_status") or "").lower()


def _interactive_count(browser_state: dict[str, Any]) -> int:
    metrics = _as_dict(browser_state.get("metrics"))
    if metrics.get("interactive_count") not in (None, ""):
        return int(metrics.get("interactive_count") or 0)
    perception = _as_dict(browser_state.get("perception"))
    som = _as_dict(perception.get("som"))
    return int(som.get("interactive_count") or len(_as_list(som.get("elements"))))


def _action_ref_count(browser_state: dict[str, Any]) -> int:
    interaction = _as_dict(browser_state.get("interaction"))
    if interaction.get("action_ref_count") not in (None, ""):
        return int(interaction.get("action_ref_count") or 0)
    return len(_as_list(interaction.get("action_refs")))


def build_action_reliability_score(
    action_trace: dict[str, Any] | None = None,
    browser_state: dict[str, Any] | None = None,
    history: Any = None,
) -> dict[str, Any]:
    trace = _as_dict(action_trace)
    state = _as_dict(browser_state)
    result_summary = _as_dict(trace.get("result_summary"))
    issue_summary = _as_dict(trace.get("issue_summary"))
    action_ref = _as_dict(trace.get("action_ref"))
    target = _as_dict(trace.get("target"))
    action = str(trace.get("action") or target.get("action") or "unknown")
    status = str(trace.get("status") or "ok").lower()
    if status not in {"ok", "warn", "error"}:
        status = "ok"
    factors: list[dict[str, Any]] = []
    if status == "error":
        _add_factor(factors, "action_error", "execution", 0.35, "action trace status is error")
    elif status == "warn":
        _add_factor(factors, "action_warn", "execution", 0.12, "action trace status is warn")
    warning_codes = [str(item) for item in _as_list(trace.get("warning_codes")) if str(item or "")]
    for code in warning_codes[:6]:
        if code == "action_failed":
            continue
        _add_factor(factors, code, "trace_warning", 0.08, f"action trace warning: {code}")
    failure_code = str(result_summary.get("failure_code") or "")
    failure_category = str(result_summary.get("failure_category") or "")
    if failure_code:
        _add_factor(factors, failure_code, failure_category or "failure", 0.22, f"previous action failure: {failure_code}")
    ref = str(action_ref.get("ref") or target.get("ref") or "")
    selector = str(action_ref.get("selector") or target.get("selector") or "")
    if action in _ELEMENT_ACTIONS and ref and not action_ref:
        _add_factor(factors, "action_ref_missing", "target_resolution", 0.16, "element action has ref but no normalized action_ref")
    if action in _ELEMENT_ACTIONS and ref and not selector:
        _add_factor(factors, "selector_missing", "target_resolution", 0.18, "element action has ref but no selector evidence")
    interactive_count = _interactive_count(state)
    action_ref_count = _action_ref_count(state)
    if action in _ELEMENT_ACTIONS and state and interactive_count <= 0:
        _add_factor(factors, "browser_state_empty", "perception", 0.16, "browser_state.v2 has no interactive elements")
    if action in _ELEMENT_ACTIONS and state and action_ref_count <= 0:
        _add_factor(factors, "browser_state_action_refs_empty", "perception", 0.12, "browser_state.v2 has no action refs")
    runtime_status = _runtime_status(state)
    if runtime_status in _RUNTIME_BAD_STATUSES:
        _add_factor(factors, "runtime_unavailable", "runtime", 0.22, f"browser runtime status is {runtime_status}")
    hist = _history_stats(history)
    if hist["attempt_count"] >= 2 and hist["failure_rate"] >= 0.5:
        _add_factor(factors, "history_failure_rate_high", "history", min(0.25, hist["failure_rate"] * 0.25), "recent attempts failed frequently")
    penalty = min(0.95, sum(float(item["weight"]) for item in factors))
    score = round(max(0.0, 1.0 - penalty), 4)
    factor_codes = {str(item.get("code") or "") for item in factors}
    recoverable_target_codes = {
        "action_warn",
        "selector_fallback_used",
        "action_ref_missing",
        "selector_missing",
        "browser_state_empty",
        "browser_state_action_refs_empty",
    }
    hard_failure_codes = {
        "action_error",
        "runtime_unavailable",
        "timeout",
        "backend_unavailable",
        "context_closed",
        "history_failure_rate_high",
    }
    if status != "error" and factor_codes and not factor_codes.intersection(hard_failure_codes) and factor_codes.issubset(recoverable_target_codes):
        score = max(score, 0.5)
    risk_level = "high" if score < 0.45 or status == "error" else ("medium" if score < 0.75 or factors else "low")
    actions = _dedupe(
        [trace.get("recommended_action")]
        + _as_list(result_summary.get("recovery_actions"))
        + _as_list(issue_summary.get("recommended_actions"))
    )
    if factor_codes.intersection({"selector_missing", "action_ref_missing", "browser_state_empty", "browser_state_action_refs_empty"}):
        actions.extend(["refresh_browser_snapshot", "use_similar_selector", "retry_action_with_new_ref"])
    if factor_codes.intersection({"runtime_unavailable", "timeout", "backend_unavailable", "context_closed"}):
        actions.extend(["check_browser_runtime", "reopen_browser_session"])
    if "history_failure_rate_high" in factor_codes:
        actions.extend(["avoid_repeating_failed_action", "replan_with_planner_feedback"])
    actions = _dedupe(actions) or ["continue"]
    policy = {
        "version": _SELF_HEALING_POLICY_VERSION,
        "source": "action_reliability",
        "action": action,
        "recommended_action": actions[0],
        "recommended_actions": actions,
        "retry_allowed": risk_level != "high" or bool(factor_codes.intersection({"selector_missing", "action_ref_missing", "browser_state_empty", "browser_state_action_refs_empty", "timeout"})),
        "requires_snapshot_refresh": bool(factor_codes.intersection({"selector_missing", "action_ref_missing", "browser_state_empty", "browser_state_action_refs_empty"})),
        "requires_runtime_check": bool(factor_codes.intersection({"runtime_unavailable", "timeout", "backend_unavailable", "context_closed"})),
        "requires_planner_replan": risk_level == "high" or "history_failure_rate_high" in factor_codes,
        "escalation": "planner_replan" if risk_level == "high" else "none",
    }
    return {
        "version": _ACTION_RELIABILITY_SCORE_VERSION,
        "source": "action_reliability",
        "action": action,
        "status": "error" if risk_level == "high" else ("warn" if risk_level == "medium" else "ok"),
        "score": score,
        "risk_level": risk_level,
        "risk_factors": factors,
        "history": hist,
        "evidence": {
            "trace_status": status,
            "warning_codes": warning_codes,
            "failure_code": failure_code,
            "failure_category": failure_category,
            "interactive_count": interactive_count,
            "action_ref_count": action_ref_count,
            "runtime_status": runtime_status,
        },
        "self_healing_policy": policy,
    }

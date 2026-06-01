from __future__ import annotations

from typing import Any

from visual_web_agent.browser_pool import build_browser_runtime_drift, build_browser_runtime_issue_summary, build_browser_runtime_preflight, get_browser_runtime_status


_BROWSER_RUNTIME_DOCTOR_VERSION = "browser_runtime_doctor_report.v1"


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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _dedupe(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item != "continue" and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _runtime_from_source(source: dict[str, Any]) -> dict[str, Any]:
    for key in ("runtime_status", "runtime", "runtime_snapshot", "after_runtime_status", "after_status"):
        item = source.get(key)
        if isinstance(item, dict) and item:
            return dict(item)
    context = _as_dict(source.get("runtime_context"))
    after = _as_dict(context.get("after"))
    after_snapshot = _as_dict(after.get("runtime_snapshot"))
    if after_snapshot:
        return after_snapshot
    pool = _as_dict(source.get("pool_status")) or _as_dict(source.get("pool"))
    backend = _as_dict(source.get("backend_status")) or _as_dict(source.get("backend"))
    if pool or backend:
        return get_browser_runtime_status(pool_status=pool, backend_status=backend)
    return {}


def _before_runtime(source: dict[str, Any]) -> dict[str, Any]:
    for key in ("before_runtime_status", "before_status", "before_runtime", "runtime_before"):
        item = source.get(key)
        if isinstance(item, dict) and item:
            return dict(item)
    context = _as_dict(source.get("runtime_context"))
    return _as_dict(_as_dict(context.get("before")).get("runtime_snapshot"))


def _after_runtime(source: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    for key in ("after_runtime_status", "after_status", "after_runtime", "runtime_after"):
        item = source.get(key)
        if isinstance(item, dict) and item:
            return dict(item)
    context = _as_dict(source.get("runtime_context"))
    after_snapshot = _as_dict(_as_dict(context.get("after")).get("runtime_snapshot"))
    return after_snapshot or dict(runtime)


def _preflight_from_source(source: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    for key in ("runtime_preflight", "preflight", "after_preflight"):
        item = source.get(key)
        if isinstance(item, dict) and item:
            return dict(item)
    context = _as_dict(source.get("runtime_context"))
    after_preflight = _as_dict(_as_dict(context.get("after")).get("runtime_preflight"))
    if after_preflight:
        return after_preflight
    return build_browser_runtime_preflight(runtime) if runtime else build_browser_runtime_preflight({})


def _before_preflight(source: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    for key in ("before_preflight", "runtime_preflight_before"):
        item = source.get(key)
        if isinstance(item, dict) and item:
            return dict(item)
    context = _as_dict(source.get("runtime_context"))
    before_preflight = _as_dict(_as_dict(context.get("before")).get("runtime_preflight"))
    if before_preflight:
        return before_preflight
    return build_browser_runtime_preflight(before) if before else {}


def _drift_from_source(source: dict[str, Any], before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    for key in ("runtime_drift", "drift"):
        item = source.get(key)
        if isinstance(item, dict) and item:
            return dict(item)
    if before and after:
        return build_browser_runtime_drift(before, after)
    return {}


def _issue_summary_from_source(source: dict[str, Any], before_preflight: dict[str, Any], after_preflight: dict[str, Any], drift: dict[str, Any]) -> dict[str, Any]:
    for key in ("runtime_issue_summary", "issue_summary"):
        item = source.get(key)
        if isinstance(item, dict) and item:
            return dict(item)
    return build_browser_runtime_issue_summary(before_preflight=before_preflight, after_preflight=after_preflight, drift=drift)


def _check(name: str, passed: bool, severity: str, code: str, message: str, *, observed: Any = None, recommended_action: str = "continue", source: str = "runtime") -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "severity": str(severity or "warn"),
        "status": "pass" if passed else str(severity or "warn"),
        "code": str(code or name),
        "message": str(message or code or name),
        "observed": observed,
        "recommended_action": str(recommended_action or "continue"),
        "source": str(source or "runtime"),
    }


def _runtime_checks(runtime: dict[str, Any], preflight: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    summary = _as_dict(runtime.get("backend_summary"))
    capacity = _as_dict(runtime.get("capacity"))
    pool = _as_dict(runtime.get("pool"))
    runtime_state = str(runtime.get("status") or "unknown")
    health_state = str(summary.get("health_status") or "unknown")
    available_contexts = _safe_int(capacity.get("available_contexts"), -1)
    max_contexts = _safe_int(capacity.get("max_contexts"), 0)
    active_contexts = _safe_int(capacity.get("active_contexts"), 0)
    session_count = _safe_int(capacity.get("backend_session_count"), 0)
    checks.append(_check(
        "runtime_snapshot_present",
        bool(runtime),
        "error",
        "runtime_snapshot_missing",
        "browser runtime snapshot is available" if runtime else "browser runtime snapshot is missing",
        observed=runtime_state,
        recommended_action="fetch_browser_runtime_status",
    ))
    if not runtime:
        return checks
    checks.append(_check(
        "backend_available",
        runtime_state != "backend_unavailable",
        "error",
        "active_backend_unavailable",
        "active browser backend is available" if runtime_state != "backend_unavailable" else "active browser backend is unavailable",
        observed=runtime_state,
        recommended_action="check_browser_backend_configuration",
    ))
    checks.append(_check(
        "backend_health",
        health_state not in {"unhealthy", "not_configured"},
        "error",
        "backend_health_unhealthy",
        "browser backend health is usable" if health_state not in {"unhealthy", "not_configured"} else "browser backend health is not usable",
        observed=health_state,
        recommended_action="check_browser_backend_health",
    ))
    checks.append(_check(
        "pool_capacity",
        runtime_state != "pool_exhausted" and available_contexts != 0,
        "error",
        "browser_pool_exhausted",
        "browser pool has available context capacity" if runtime_state != "pool_exhausted" and available_contexts != 0 else "browser pool has no available context capacity",
        observed={"available_contexts": available_contexts, "active_contexts": active_contexts, "max_contexts": max_contexts},
        recommended_action="queue_or_wait_for_browser_context",
    ))
    checks.append(_check(
        "backend_health_cache",
        not bool(summary.get("health_cache_stale")),
        "warn",
        "backend_health_cache_stale",
        "backend health cache is fresh" if not bool(summary.get("health_cache_stale")) else "backend health cache is stale",
        observed={"age_s": summary.get("health_cache_age_s"), "ttl_s": summary.get("health_cache_ttl_s")},
        recommended_action="refresh_browser_runtime_status",
    ))
    if bool(capacity.get("safety_cap_active")):
        checks.append(_check(
            "parallel_safety_cap",
            False,
            "warn",
            "parallel_safety_cap_active",
            "parallel browser contexts are capped by safety defaults",
            observed={"parallel_enabled": bool(capacity.get("parallel_enabled")), "max_contexts": max_contexts},
            recommended_action="enable_experimental_parallel_runs_only_if_safe",
            source="pool",
        ))
    if int(pool.get("total_failed") or 0) > 0 or str(pool.get("last_error") or ""):
        checks.append(_check(
            "pool_release_errors",
            False,
            "warn",
            "browser_pool_recent_release_failure",
            "browser pool has recorded release failures",
            observed={"total_failed": pool.get("total_failed"), "last_error": pool.get("last_error")},
            recommended_action="inspect_browser_pool_last_error",
            source="pool",
        ))
    if max_contexts > 0 and session_count > max_contexts:
        checks.append(_check(
            "backend_session_count",
            False,
            "warn",
            "backend_session_count_exceeds_pool_capacity",
            "backend session count exceeds configured pool capacity",
            observed={"backend_session_count": session_count, "max_contexts": max_contexts},
            recommended_action="reconcile_browser_backend_sessions",
            source="backend",
        ))
    preflight_warnings = [str(item) for item in _as_list(preflight.get("warnings")) if str(item or "")]
    checks.append(_check(
        "runtime_preflight",
        str(preflight.get("status") or "") != "warn" and not preflight_warnings,
        "warn",
        "runtime_preflight_warn",
        "runtime preflight is clean" if str(preflight.get("status") or "") != "warn" and not preflight_warnings else "runtime preflight has warnings",
        observed={"status": preflight.get("status"), "warnings": preflight_warnings},
        recommended_action=str(preflight.get("recommended_action") or "refresh_browser_runtime_status"),
        source="preflight",
    ))
    return checks


def _drift_checks(drift: dict[str, Any]) -> list[dict[str, Any]]:
    if not drift:
        return []
    warnings = [str(item) for item in _as_list(drift.get("warnings")) if str(item or "")]
    status = str(drift.get("status") or "unknown")
    severity = "error" if any(item in {"runtime_status_regressed", "backend_health_regressed", "pool_capacity_exhausted_during_execute"} for item in warnings) else "warn"
    return [_check(
        "runtime_drift",
        status in {"stable", "unknown"} and not warnings,
        severity,
        "runtime_drift_regressed" if warnings else "runtime_drift_changed",
        "runtime drift is stable" if status in {"stable", "unknown"} and not warnings else "runtime drift requires attention",
        observed={"status": status, "warnings": warnings, "changes": list(drift.get("changes") or [])},
        recommended_action=str(drift.get("recommended_action") or "continue_with_monitoring"),
        source="runtime_drift",
    )]


def _issue_checks(issue_summary: dict[str, Any]) -> list[dict[str, Any]]:
    if not issue_summary:
        return []
    issue_count = _safe_int(issue_summary.get("issue_count"), 0)
    status = str(issue_summary.get("status") or "unknown")
    return [_check(
        "runtime_issue_summary",
        issue_count == 0 and status in {"ok", "unknown"},
        "warn",
        "runtime_issue_summary_warn",
        "runtime issue summary is clean" if issue_count == 0 and status in {"ok", "unknown"} else "runtime issue summary reports warnings",
        observed={"status": status, "issue_count": issue_count, "issues": list(issue_summary.get("issues") or [])},
        recommended_action=str(issue_summary.get("recommended_action") or "refresh_browser_runtime_status"),
        source="runtime_issue_summary",
    )]


def _readiness(runtime: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _as_dict(runtime.get("backend_summary"))
    capacity = _as_dict(runtime.get("capacity"))
    runtime_state = str(runtime.get("status") or "unknown")
    available_contexts = _safe_int(capacity.get("available_contexts"), 0)
    backend_available = runtime_state != "backend_unavailable" and bool(runtime)
    backend_healthy = str(summary.get("health_status") or "unknown") not in {"unhealthy", "not_configured"}
    pool_available = runtime_state != "pool_exhausted" and available_contexts > 0
    hard_fail = any(not item.get("passed") and item.get("severity") == "error" for item in checks)
    return {
        "ready_for_browser_actions": bool(runtime) and backend_available and backend_healthy and pool_available and not hard_fail,
        "can_start_new_context": bool(runtime) and backend_available and backend_healthy and pool_available,
        "ready_for_parallel": bool(capacity.get("parallel_enabled")) and available_contexts > 1,
        "backend_available": backend_available,
        "backend_healthy": backend_healthy,
        "pool_available": pool_available,
        "available_contexts": available_contexts,
        "runtime_status": runtime_state,
    }


def _status(checks: list[dict[str, Any]], issue_summary: dict[str, Any], drift: dict[str, Any]) -> str:
    if any(not item.get("passed") and item.get("severity") == "error" for item in checks):
        return "blocked"
    if any(not item.get("passed") and item.get("severity") == "warn" for item in checks):
        return "needs_attention"
    if str(issue_summary.get("status") or "") == "watch" or str(drift.get("status") or "") == "changed":
        return "watch"
    return "healthy"


def _recovery_plan(checks: list[dict[str, Any]], actions: list[str]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for action in actions:
        for check in checks:
            if action == check.get("recommended_action") and not check.get("passed"):
                plan.append({
                    "action": action,
                    "reason": str(check.get("message") or check.get("code") or ""),
                    "source": str(check.get("source") or "runtime"),
                    "severity": str(check.get("severity") or "warn"),
                    "code": str(check.get("code") or ""),
                })
                break
        else:
            plan.append({"action": action, "reason": "runtime doctor recommendation", "source": "doctor", "severity": "info", "code": action})
    return plan


def build_browser_runtime_doctor(source: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _as_dict(source)
    runtime = _runtime_from_source(data)
    before = _before_runtime(data)
    after = _after_runtime(data, runtime)
    preflight = _preflight_from_source(data, runtime)
    before_preflight = _before_preflight(data, before)
    drift = _drift_from_source(data, before, after)
    issue_summary = _issue_summary_from_source(data, before_preflight, preflight, drift)
    checks = _runtime_checks(runtime, preflight) + _drift_checks(drift) + _issue_checks(issue_summary)
    failed_checks = [item for item in checks if not item.get("passed")]
    recommended_actions = _dedupe(
        [item.get("recommended_action") for item in failed_checks]
        + _as_list(issue_summary.get("recommended_actions"))
        + [issue_summary.get("recommended_action"), drift.get("recommended_action"), preflight.get("recommended_action")]
    ) or ["continue"]
    readiness = _readiness(runtime, checks)
    status = _status(checks, issue_summary, drift)
    warning_codes = _dedupe([item.get("code") for item in failed_checks if item.get("severity") == "warn"])
    error_codes = _dedupe([item.get("code") for item in failed_checks if item.get("severity") == "error"])
    summary = _as_dict(runtime.get("backend_summary"))
    capacity = _as_dict(runtime.get("capacity"))
    return {
        "version": _BROWSER_RUNTIME_DOCTOR_VERSION,
        "source": "browser_runtime_doctor",
        "status": status,
        "blocking": status == "blocked",
        "runtime_status": str(runtime.get("status") or "unknown"),
        "readiness": readiness,
        "summary": {
            "active_backend": str(summary.get("active_name") or ""),
            "backend_health": str(summary.get("health_status") or "unknown"),
            "available_contexts": capacity.get("available_contexts"),
            "active_contexts": capacity.get("active_contexts"),
            "max_contexts": capacity.get("max_contexts"),
            "health_cache_stale": bool(summary.get("health_cache_stale")),
            "issue_count": len(failed_checks),
            "error_count": len(error_codes),
            "warning_count": len(warning_codes),
        },
        "checks": checks,
        "issues": [
            {
                "code": str(item.get("code") or ""),
                "severity": str(item.get("severity") or "warn"),
                "source": str(item.get("source") or "runtime"),
                "message": str(item.get("message") or ""),
                "recommended_action": str(item.get("recommended_action") or "continue"),
            }
            for item in failed_checks
        ],
        "warning_codes": warning_codes,
        "error_codes": error_codes,
        "recommended_action": recommended_actions[0],
        "recommended_actions": recommended_actions,
        "recovery_plan": _recovery_plan(checks, recommended_actions),
        "evidence": {
            "runtime": runtime,
            "preflight": preflight,
            "before_preflight": before_preflight,
            "drift": drift,
            "issue_summary": issue_summary,
        },
    }

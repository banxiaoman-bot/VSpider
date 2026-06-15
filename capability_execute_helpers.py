"""Capability execute runtime helpers.

Extracted from api_server.py (Slice 3) — runtime context, action trace,
failure bundle, and phase event helpers for /api/capabilities/execute.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from pathlib import Path
from typing import Any

from visual_web_agent.artifact_manager import artifact_url, resolve_artifact_path, register_artifact
from visual_web_agent.browser_pool import (
    build_browser_runtime_drift as _build_browser_runtime_drift,
    build_browser_runtime_issue_summary as _build_browser_runtime_issue_summary,
    build_browser_runtime_preflight as _build_browser_runtime_preflight,
    get_browser_pool_status as _get_browser_pool_status,
    get_browser_runtime_status as _get_browser_runtime_status,
)

logger = logging.getLogger("vspider.api")


def _capability_execute_runtime_context(backend_status: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = _get_browser_runtime_status(
        pool_status=_get_browser_pool_status(),
        backend_status=backend_status or {},
    )
    return {
        "runtime_snapshot": runtime,
        "runtime_preflight": _build_browser_runtime_preflight(runtime),
    }


def _capability_execute_runtime_summary(context: dict[str, Any]) -> dict[str, Any]:
    runtime = dict(context.get("runtime_snapshot") or {})
    preflight = dict(context.get("runtime_preflight") or {})
    summary = dict(runtime.get("backend_summary") or {})
    capacity = dict(runtime.get("capacity") or {})
    return {
        "runtime_status": str(runtime.get("status") or "unknown"),
        "preflight_status": str(preflight.get("status") or "unknown"),
        "preflight_blocking": bool(preflight.get("blocking")),
        "recommended_action": str(preflight.get("recommended_action") or ""),
        "warnings": list(preflight.get("warnings") or []),
        "active_backend": str(summary.get("active_name") or ""),
        "backend_health": str(summary.get("health_status") or ""),
        "available_contexts": capacity.get("available_contexts"),
        "max_contexts": capacity.get("max_contexts"),
        "cache_stale": bool(summary.get("health_cache_stale")),
    }


def _capability_execute_action_trace(result: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(result or {})
    candidates: list[Any] = [data.get("action_trace")]
    nested = data.get("result")
    if isinstance(nested, dict):
        candidates.append(nested.get("action_trace"))
    attempts = data.get("attempts")
    if isinstance(attempts, list):
        for item in reversed(attempts):
            if isinstance(item, dict):
                candidates.append(item.get("action_trace"))
    for item in candidates:
        if isinstance(item, dict) and str(item.get("version") or "") == "browser_action_trace.v1":
            return dict(item)
    return {}


def _capability_execute_exception_action_trace(exc: Exception) -> dict[str, Any]:
    item = getattr(exc, "action_trace", None)
    if isinstance(item, dict) and str(item.get("version") or "") == "browser_action_trace.v1":
        return dict(item)
    return {}


def _capability_execute_failed_action_result(
    exc: Exception,
    action_trace: dict[str, Any],
    runtime_before: dict[str, Any],
    runtime_after: dict[str, Any],
) -> dict[str, Any]:
    action_issue_summary = getattr(exc, "action_issue_summary", None) or action_trace.get("issue_summary")
    if not isinstance(action_issue_summary, dict):
        action_issue_summary = {}
    attempt = {
        "capability": "browser_control",
        "status": "error",
        "reason": str(exc),
        "action_trace": dict(action_trace),
        "action_issue_summary": dict(action_issue_summary),
    }
    result: dict[str, Any] = {
        "status": "error",
        "completed": False,
        "route": {},
        "attempts": [attempt],
        "capability": "browser_control",
        "result": None,
        "artifact": None,
        "verification": {"passed": False, "summary": str(exc)},
        "fallback_reason": str(exc),
        "action_trace": dict(action_trace),
        "action_issue_summary": dict(action_issue_summary),
    }
    result["runtime_context"] = {
        "version": "capability_execute_runtime_context.v1",
        "before": runtime_before,
        "after": runtime_after,
    }
    result["runtime_summary"] = {
        "before": _capability_execute_runtime_summary(runtime_before),
        "after": _capability_execute_runtime_summary(runtime_after),
    }
    result["runtime_drift"] = _build_browser_runtime_drift(
        runtime_before.get("runtime_snapshot"),
        runtime_after.get("runtime_snapshot"),
    )
    result["runtime_issue_summary"] = _build_browser_runtime_issue_summary(
        before_preflight=runtime_before.get("runtime_preflight"),
        after_preflight=runtime_after.get("runtime_preflight"),
        drift=result.get("runtime_drift"),
    )
    return result


def _capability_execute_failure_bundle(result: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(result or {})
    action_trace = _capability_execute_action_trace(data)
    if not action_trace or str(action_trace.get("status") or "").lower() != "error":
        return {}
    action_issue_summary = data.get("action_issue_summary") or action_trace.get("issue_summary")
    if not isinstance(action_issue_summary, dict):
        action_issue_summary = {}
    result_summary = dict(action_trace.get("result_summary") or {})
    warning_codes = [str(item) for item in (action_trace.get("warning_codes") or []) if str(item or "")]
    primary_failure = str(result_summary.get("failure_code") or "")
    if not primary_failure:
        primary_failure = next((code for code in warning_codes if code != "action_failed"), "")
    if not primary_failure:
        primary_failure = "action_error"
    recovery_actions: list[str] = []
    for item in result_summary.get("recovery_actions") or []:
        action_item = str(item or "")
        if action_item and action_item != "continue":
            recovery_actions.append(action_item)
    for item in action_issue_summary.get("recommended_actions") or []:
        action_item = str(item or "")
        if action_item and action_item != "continue":
            recovery_actions.append(action_item)
    recommended_action = str(action_issue_summary.get("recommended_action") or action_trace.get("recommended_action") or "")
    if recommended_action and recommended_action != "continue":
        recovery_actions.append(recommended_action)
    attempts = data.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
    runtime_issue_summary = data.get("runtime_issue_summary")
    if not isinstance(runtime_issue_summary, dict):
        runtime_issue_summary = {}
    runtime_drift = data.get("runtime_drift")
    if not isinstance(runtime_drift, dict):
        runtime_drift = {}
    trace_artifact = data.get("trace_artifact")
    if not isinstance(trace_artifact, dict):
        trace_artifact = {}
    return {
        "version": "capability_execute_failure_bundle.v1",
        "source": "capability_execute",
        "status": "error",
        "blocking": bool(action_issue_summary.get("blocking") or runtime_issue_summary.get("blocking")),
        "primary_failure": primary_failure,
        "failure_category": str(result_summary.get("failure_category") or ""),
        "action": str(action_trace.get("action") or ""),
        "capability": data.get("capability"),
        "completed": bool(data.get("completed")),
        "fallback_reason": data.get("fallback_reason"),
        "attempt_count": len(attempts),
        "recommended_action": recommended_action or (recovery_actions[0] if recovery_actions else "inspect_browser_action"),
        "recommended_actions": list(dict.fromkeys(recovery_actions)),
        "action_trace": dict(action_trace),
        "action_issue_summary": dict(action_issue_summary),
        "runtime_issue_summary": dict(runtime_issue_summary),
        "runtime_drift": dict(runtime_drift),
        "attempts": list(attempts),
        "trace_artifact": dict(trace_artifact),
    }


def _capability_execute_phase_event(
    result: dict[str, Any],
    *,
    severity: str,
    message: str,
    completed: bool,
) -> dict[str, Any]:
    return {
        "type": "phase",
        "phase": "capability_execute",
        "severity": str(severity or "info"),
        "message": str(message or ""),
        "execution_status": result.get("status"),
        "completed": bool(completed),
        "capability": result.get("capability"),
        "attempts": result.get("attempts") or [],
        "verification": result.get("verification"),
        "fallback_reason": result.get("fallback_reason"),
        "artifact": result.get("artifact"),
        "trace_artifact": result.get("trace_artifact"),
        "runtime_summary": result.get("runtime_summary"),
        "runtime_drift": result.get("runtime_drift"),
        "runtime_issue_summary": result.get("runtime_issue_summary"),
        "action_trace": result.get("action_trace"),
        "action_issue_summary": result.get("action_issue_summary"),
        "failure_bundle": result.get("failure_bundle"),
        "crawl_efficiency_plan": result.get("crawl_efficiency_plan"),
        "efficiency_correlation_report": result.get("efficiency_correlation_report"),
        "route_intent": (result.get("route") or {}).get("intent"),
    }


def _capability_execute_phase_event_extra(phase_event: dict[str, Any]) -> dict[str, Any]:
    reserved = {"type", "phase", "severity", "message", "step", "duration_ms", "notice_severity", "ts"}
    return {key: value for key, value in phase_event.items() if key not in reserved}


def _capability_execute_failure_detail(
    result: dict[str, Any],
    *,
    phase_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {"message": "capability route execution failed"}
    if isinstance(result.get("action_trace"), dict):
        detail["action_trace"] = dict(result.get("action_trace") or {})
    if isinstance(result.get("action_issue_summary"), dict):
        detail["action_issue_summary"] = dict(result.get("action_issue_summary") or {})
    if isinstance(result.get("trace_artifact"), dict):
        detail["trace_artifact"] = dict(result.get("trace_artifact") or {})
    if isinstance(result.get("failure_bundle"), dict):
        detail["failure_bundle"] = dict(result.get("failure_bundle") or {})
    if isinstance(result.get("efficiency_correlation_report"), dict):
        detail["efficiency_correlation_report"] = dict(result.get("efficiency_correlation_report") or {})
    if phase_event is None:
        phase_event = _capability_execute_phase_event(
            result,
            severity="error",
            message=str(result.get("fallback_reason") or "capability route execution failed"),
            completed=False,
        )
    detail["phase_event"] = dict(phase_event)
    return detail


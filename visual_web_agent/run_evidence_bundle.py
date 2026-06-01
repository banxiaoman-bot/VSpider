from __future__ import annotations

from collections import Counter
from typing import Any


_RUN_EVIDENCE_BUNDLE_VERSION = "run_evidence_bundle.v1"


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
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _capability_names(items: Any, key: str = "name") -> list[str]:
    names: list[str] = []
    for item in _as_list(items):
        if isinstance(item, dict):
            name = str(item.get(key) or item.get("capability") or "")
            if name:
                names.append(name)
    return _dedupe(names)


def _route_from_source(source: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    for item in (source.get("route"), source.get("capability_route"), result.get("route")):
        if isinstance(item, dict):
            return dict(item)
    phase_event = _as_dict(source.get("phase_event")) or _as_dict(result.get("phase_event"))
    detail = _as_dict(phase_event.get("detail"))
    route = _as_dict(detail.get("route"))
    if route:
        return route
    return {}


def _execution_plan_from_source(source: dict[str, Any], result: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    for item in (source.get("execution_plan"), result.get("execution_plan"), route.get("execution_plan")):
        if isinstance(item, dict):
            return dict(item)
    return {}


def _event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for event in events:
        kind = str(event.get("type") or event.get("event") or "")
        if kind:
            counts[kind] += 1
    return dict(sorted(counts.items()))


def _last_browser_state(source: dict[str, Any], result: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    for item in (source.get("browser_state"), result.get("browser_state")):
        if isinstance(item, dict):
            return dict(item)
    for event in reversed(events):
        state = event.get("browser_state")
        if isinstance(state, dict):
            return dict(state)
    return {}


def _trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
    issue = _as_dict(trace.get("issue_summary"))
    return {
        "version": str(trace.get("version") or ""),
        "action": str(trace.get("action") or ""),
        "status": str(trace.get("status") or ""),
        "blocking": bool(trace.get("blocking") or issue.get("blocking")),
        "warning_codes": [str(item) for item in _as_list(trace.get("warning_codes")) if str(item or "")],
        "recommended_action": str(trace.get("recommended_action") or issue.get("recommended_action") or ""),
        "duration_ms": int(trace.get("duration_ms") or 0),
        "session_id": str(trace.get("session_id") or ""),
        "target": dict(trace.get("target") or {}),
        "action_ref": dict(trace.get("action_ref") or {}),
        "issue_summary": issue,
    }


def _collect_action_traces(source: dict[str, Any], result: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    for item in (source.get("action_trace"), source.get("browser_action_trace"), result.get("action_trace")):
        if isinstance(item, dict):
            traces.append(item)
    for attempt in _as_list(result.get("attempts")) + _as_list(source.get("attempts")):
        if isinstance(attempt, dict) and isinstance(attempt.get("action_trace"), dict):
            traces.append(dict(attempt.get("action_trace") or {}))
    for event in events:
        for item in (event.get("action_trace"), _as_dict(event.get("detail")).get("action_trace")):
            if isinstance(item, dict):
                traces.append(dict(item))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for trace in traces:
        summary = _trace_summary(trace)
        key = "|".join([
            summary["version"],
            summary["action"],
            summary["status"],
            summary["session_id"],
            str(summary["duration_ms"]),
        ])
        if key not in seen:
            seen.add(key)
            out.append(summary)
    return out


def _collect_extracts(source: dict[str, Any], result: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    extracts: list[dict[str, Any]] = []
    result_payload = _as_dict(result.get("result"))
    if result_payload:
        rows = result_payload.get("rows") if isinstance(result_payload.get("rows"), list) else []
        extracts.append({
            "source": str(result_payload.get("source") or result.get("capability") or ""),
            "row_count": int(result_payload.get("row_count") or len(rows) or result_payload.get("count") or 0),
            "field_coverage": dict(result_payload.get("field_coverage") or {}),
            "artifact": dict(result.get("artifact") or {}),
        })
    for event in events:
        if str(event.get("type") or "") == "extract":
            extracts.append({
                "source": str(event.get("source") or ""),
                "row_count": int(event.get("rows") or event.get("row_count") or 0),
                "output_file": str(event.get("output_file") or ""),
                "metadata": dict(event.get("metadata") or {}),
            })
    for item in _as_list(source.get("extracts")):
        if isinstance(item, dict):
            extracts.append(dict(item))
    return extracts


def _collect_artifacts(source: dict[str, Any], result: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for key in ("artifact", "trace_artifact", "output_artifact"):
        for container in (source, result):
            item = container.get(key)
            if isinstance(item, dict):
                artifacts.append({"kind": key, **dict(item)})
    for event in events:
        output_file = str(event.get("output_file") or "")
        if output_file:
            artifacts.append({"kind": "event_output_file", "path": output_file})
        metadata = _as_dict(event.get("metadata"))
        for key in ("html_log", "event_stream", "snapshot_path", "artifact_path"):
            value = str(metadata.get(key) or "")
            if value:
                artifacts.append({"kind": key, "path": value})
    for item in _as_list(source.get("artifacts")):
        if isinstance(item, dict):
            artifacts.append(dict(item))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for artifact in artifacts:
        key = str(artifact.get("path") or artifact.get("url") or artifact.get("name") or artifact)
        if key and key not in seen:
            seen.add(key)
            out.append(artifact)
    return out


def _failure_bundle(source: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    for item in (source.get("failure_bundle"), result.get("failure_bundle")):
        if isinstance(item, dict):
            return dict(item)
    phase_event = _as_dict(source.get("phase_event")) or _as_dict(result.get("phase_event"))
    for item in (phase_event.get("failure_bundle"), _as_dict(phase_event.get("detail")).get("failure_bundle")):
        if isinstance(item, dict):
            return dict(item)
    return {}


def _runtime_summary(source: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    runtime_context = _as_dict(source.get("runtime_context")) or _as_dict(result.get("runtime_context"))
    runtime_summary = _as_dict(source.get("runtime_summary")) or _as_dict(result.get("runtime_summary"))
    runtime_drift = _as_dict(source.get("runtime_drift")) or _as_dict(result.get("runtime_drift"))
    runtime_issue_summary = _as_dict(source.get("runtime_issue_summary")) or _as_dict(result.get("runtime_issue_summary"))
    after = _as_dict(runtime_context.get("after"))
    before = _as_dict(runtime_context.get("before"))
    after_preflight = _as_dict(after.get("runtime_preflight"))
    after_snapshot = _as_dict(after.get("runtime_snapshot"))
    return {
        "context_version": str(runtime_context.get("version") or ""),
        "before_status": str(_as_dict(before.get("runtime_snapshot")).get("status") or ""),
        "after_status": str(after_snapshot.get("status") or ""),
        "after_preflight_status": str(after_preflight.get("status") or ""),
        "summary": runtime_summary,
        "drift": runtime_drift,
        "issue_summary": runtime_issue_summary,
        "blocking": bool(runtime_drift.get("blocking") or runtime_issue_summary.get("blocking") or after_preflight.get("blocking")),
    }


def _route_summary(route: dict[str, Any], execution_plan: dict[str, Any]) -> dict[str, Any]:
    crawl_plan = _as_dict(route.get("crawl_efficiency_plan"))
    return {
        "goal": str(route.get("goal") or execution_plan.get("goal") or ""),
        "url": str(route.get("url") or execution_plan.get("url") or ""),
        "intent": dict(route.get("intent") or execution_plan.get("intent") or {}),
        "signals": dict(route.get("signals") or {}),
        "backend_plan": _capability_names(route.get("backend_plan")),
        "fallback_chain": _capability_names(route.get("fallback_chain"), key="capability"),
        "crawl_efficiency": {
            "version": str(crawl_plan.get("version") or ""),
            "recommended_path": str(crawl_plan.get("recommended_path") or ""),
            "skip_browser": bool(crawl_plan.get("skip_browser")),
            "skip_vlm": bool(crawl_plan.get("skip_vlm")),
        },
    }


def _execution_summary(execution_plan: dict[str, Any]) -> dict[str, Any]:
    steps = [dict(item) for item in _as_list(execution_plan.get("steps")) if isinstance(item, dict)]
    return {
        "version": str(execution_plan.get("version") or ""),
        "step_count": len(steps),
        "step_capabilities": _capability_names(steps, key="capability"),
        "risk_flags": [str(item) for item in _as_list(execution_plan.get("risk_flags")) if str(item or "")],
        "notes": [str(item) for item in _as_list(execution_plan.get("notes")) if str(item or "")][:8],
        "planner_feedback": dict(execution_plan.get("planner_feedback") or {}),
    }


def _workflow_graph_from(source: dict[str, Any], result: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    for item in (route.get("workflow_graph"), source.get("workflow_graph"), result.get("workflow_graph")):
        if isinstance(item, dict):
            return dict(item)
    return {}


def _cross_system_summary(
    source: dict[str, Any],
    result: dict[str, Any],
    route: dict[str, Any],
    execution_plan: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Surface the cross-system dimension (E1 plan + E1b runtime hops).

    Planned systems come from the route's ``workflow_graph.systems``;
    runtime cross-system hops come from ``system_transition`` events
    emitted by ``run_system_tracker`` during the agent loop.
    """

    workflow_graph = _workflow_graph_from(source, result, route)
    systems = [dict(item) for item in _as_list(workflow_graph.get("systems")) if isinstance(item, dict)]
    planned_systems = [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or item.get("domain") or item.get("id") or ""),
            "domain": str(item.get("domain") or ""),
            "type": str(item.get("type") or ""),
        }
        for item in systems
    ]
    risk_flags: set[str] = set()
    for container in (execution_plan.get("risk_flags"), workflow_graph.get("risk_flags")):
        for flag in _as_list(container):
            if str(flag or ""):
                risk_flags.add(str(flag))
    transitions: list[dict[str, Any]] = []
    for event in events:
        kind = str(event.get("type") or event.get("event") or "")
        if kind != "system_transition":
            continue
        detail = _as_dict(event.get("detail"))

        def _field(name: str) -> Any:
            value = event.get(name)
            return value if value is not None else detail.get(name)

        transitions.append({
            "step": int(_field("step") or 0),
            "from_system_id": str(_field("from_system_id") or ""),
            "from_system_name": str(_field("from_system_name") or ""),
            "to_system_id": str(_field("to_system_id") or ""),
            "to_system_name": str(_field("to_system_name") or ""),
            "url": str(_field("url") or ""),
        })
    visited: list[str] = []
    for transition in transitions:
        for system_id in (transition["from_system_id"], transition["to_system_id"]):
            if system_id and system_id not in visited:
                visited.append(system_id)
    return {
        "planned_system_count": len(planned_systems),
        "planned_systems": planned_systems,
        "planned_cross_system": "cross_system" in risk_flags,
        "transition_count": len(transitions),
        "transitions": transitions,
        "visited_system_ids": visited,
        "runtime_cross_system": len(transitions) > 0,
    }


def _debug_summary(
    *,
    status: str,
    failure: dict[str, Any],
    action_traces: list[dict[str, Any]],
    runtime: dict[str, Any],
    extracts: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    cross_system: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recommended: list[Any] = []
    if failure:
        recommended.extend(_as_list(failure.get("recommended_actions")))
        recommended.append(failure.get("recommended_action"))
    for trace in action_traces:
        recommended.append(trace.get("recommended_action"))
    if runtime.get("blocking"):
        recommended.append("inspect_runtime_before_retry")
    if status == "no_evidence":
        recommended.append("attach_route_result_or_event_stream")
    if not extracts and status not in {"failed", "blocked"}:
        recommended.append("inspect_extraction_or_verification_evidence")
    if not artifacts:
        recommended.append("save_trace_or_output_artifact")
    primary_focus = ""
    if failure:
        primary_focus = str(failure.get("primary_failure") or failure.get("failure_category") or "failure_bundle")
    elif runtime.get("blocking"):
        primary_focus = "runtime_blocking"
    elif any(trace.get("status") == "error" for trace in action_traces):
        primary_focus = "action_trace_error"
    elif not extracts:
        primary_focus = "evidence_incomplete"
    else:
        primary_focus = "review_passed_run"
    return {
        "primary_focus": primary_focus,
        "recommended_actions": _dedupe(recommended) or ["review_run_evidence"],
        "entrypoints": [item for item in [
            "route.execution_plan",
            "actions.traces" if action_traces else "",
            "failure.bundle" if failure else "",
            "runtime.issue_summary" if runtime.get("issue_summary") else "",
            "extraction.extracts" if extracts else "",
            "artifacts.items" if artifacts else "",
            "cross_system.transitions" if (cross_system or {}).get("transition_count") else "",
        ] if item],
    }


def _status(result: dict[str, Any], failure: dict[str, Any], action_traces: list[dict[str, Any]], runtime: dict[str, Any], events: list[dict[str, Any]], route: dict[str, Any]) -> str:
    if failure:
        return "failed"
    if runtime.get("blocking"):
        return "blocked"
    if any(trace.get("status") == "error" or trace.get("blocking") for trace in action_traces):
        return "failed"
    if result.get("completed") is True or str(result.get("status") or "") == "completed":
        return "completed"
    if events or route or result:
        return "partial"
    return "no_evidence"


def build_run_evidence_bundle(source: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _as_dict(source)
    result = _as_dict(data.get("result")) or _as_dict(data.get("execution_result"))
    if not result and any(key in data for key in ("status", "completed", "attempts", "verification", "artifact", "failure_bundle")):
        result = dict(data)
    events = [dict(item) for item in _as_list(data.get("events")) if isinstance(item, dict)]
    route = _route_from_source(data, result)
    execution_plan = _execution_plan_from_source(data, result, route)
    action_traces = _collect_action_traces(data, result, events)
    extracts = _collect_extracts(data, result, events)
    artifacts = _collect_artifacts(data, result, events)
    failure = _failure_bundle(data, result)
    runtime = _runtime_summary(data, result)
    browser_state = _last_browser_state(data, result, events)
    cross_system = _cross_system_summary(data, result, route, execution_plan, events)
    status = _status(result, failure, action_traces, runtime, events, route)
    return {
        "version": _RUN_EVIDENCE_BUNDLE_VERSION,
        "source": "run_evidence_bundle",
        "status": status,
        "run_id": str(data.get("run_id") or result.get("run_id") or data.get("trace_id") or ""),
        "route": _route_summary(route, execution_plan),
        "execution_plan": _execution_summary(execution_plan),
        "runtime": runtime,
        "browser_state": browser_state,
        "actions": {
            "count": len(action_traces),
            "error_count": sum(1 for trace in action_traces if trace.get("status") == "error"),
            "warn_count": sum(1 for trace in action_traces if trace.get("status") == "warn"),
            "blocking_count": sum(1 for trace in action_traces if trace.get("blocking")),
            "traces": action_traces,
        },
        "extraction": {
            "extract_count": len(extracts),
            "row_count": sum(int(item.get("row_count") or item.get("rows") or 0) for item in extracts if isinstance(item, dict)),
            "verification": dict(result.get("verification") or data.get("verification") or {}),
            "extracts": extracts,
        },
        "failure": {
            "present": bool(failure),
            "bundle": failure,
            "primary_failure": str(failure.get("primary_failure") or ""),
            "failure_category": str(failure.get("failure_category") or ""),
            "recommended_action": str(failure.get("recommended_action") or ""),
        },
        "artifacts": {
            "count": len(artifacts),
            "items": artifacts,
        },
        "events": {
            "count": len(events),
            "by_type": _event_counts(events),
            "event_stream_path": str(data.get("event_stream_path") or data.get("event_stream_file") or ""),
            "last_event_type": str((events[-1].get("type") if events else "") or ""),
        },
        "cross_system": cross_system,
        "debug": _debug_summary(status=status, failure=failure, action_traces=action_traces, runtime=runtime, extracts=extracts, artifacts=artifacts, cross_system=cross_system),
    }

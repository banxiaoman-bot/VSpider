from __future__ import annotations

from typing import Any

from visual_web_agent.action_reliability import build_action_reliability_score
from visual_web_agent.capability_router import planner_feedback_from_failure_bundle, route_task
from visual_web_agent.planner_contract import build_execution_plan


_CAPABILITY_FAILURE_FIXTURE_REPLAY_REPORT_VERSION = "capability_failure_fixture_replay_report.v1"
_CAPABILITY_FAILURE_FIXTURE_REPLAY_BATCH_REPORT_VERSION = "capability_failure_fixture_replay_batch_report.v1"
_CAPABILITY_FAILURE_REGRESSION_FIXTURE_VERSION = "capability_failure_regression_fixture.v1"
_CAPABILITY_FAILURE_RECOVERY_DECISION_REPLAY_VERSION = "capability_failure_recovery_decision_replay.v1"


def replay_capability_failure_fixture(fixture: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(fixture or {}) if isinstance(fixture, dict) else {}
    inputs = dict(data.get("inputs") or {}) if isinstance(data.get("inputs"), dict) else {}
    expected = dict(data.get("expected") or {}) if isinstance(data.get("expected"), dict) else {}
    failure_bundle = _fixture_failure_bundle(data)
    primary_failure = str(expected.get("primary_failure") or failure_bundle.get("primary_failure") or "")
    failure_category = str(expected.get("failure_category") or failure_bundle.get("failure_category") or "")
    planner_feedback = planner_feedback_from_failure_bundle(failure_bundle)
    goal = _fixture_route_goal(inputs, expected, failure_bundle)
    url = str(inputs.get("url") or "")
    route: dict[str, Any] = {}
    execution_plan: dict[str, Any] = {}
    if failure_bundle:
        route = route_task(
            goal,
            url=url,
            context={"failure_bundle": failure_bundle},
            runtime_status={"version": "browser_runtime.v1", "status": "available"},
        )
        execution_plan = build_execution_plan(route)
    checks = _replay_checks(data, failure_bundle, expected, planner_feedback, route, execution_plan)
    passed = all(bool(item.get("passed")) for item in checks)
    recovery_decision = replay_recovery_decision(failure_bundle)
    return {
        "version": _CAPABILITY_FAILURE_FIXTURE_REPLAY_REPORT_VERSION,
        "fixture": {
            "name": str(data.get("name") or ""),
            "version": str(data.get("version") or ""),
            "primary_failure": primary_failure,
            "failure_category": failure_category,
        },
        "planner_feedback": {
            "passed": bool(planner_feedback) and str(planner_feedback.get("primary_failure") or "") == primary_failure,
            "version": str(planner_feedback.get("version") or ""),
            "primary_failure": str(planner_feedback.get("primary_failure") or ""),
            "preferred_capabilities": list(planner_feedback.get("preferred_capabilities") or []),
            "avoid_actions": list(planner_feedback.get("avoid_actions") or []),
        },
        "route": {
            "passed": bool(route.get("planner_feedback")) and bool((route.get("signals") or {}).get("planner_feedback")),
            "intent": dict(route.get("intent") or {}),
            "signals": dict(route.get("signals") or {}),
            "planner_feedback": dict(route.get("planner_feedback") or {}),
        },
        "execution_plan": {
            "passed": bool(execution_plan.get("planner_feedback")) and "previous_failure_feedback_active" in list(execution_plan.get("risk_flags") or []),
            "version": str(execution_plan.get("version") or ""),
            "risk_flags": list(execution_plan.get("risk_flags") or []),
            "notes": list(execution_plan.get("notes") or []),
            "planner_feedback": dict(execution_plan.get("planner_feedback") or {}),
        },
        "checks": checks,
        "recovery_decision": recovery_decision,
        "passed": passed,
    }


def replay_recovery_decision(failure_bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    """C2: offline recovery-decision replay.

    Feeds the captured failure's ``action_trace`` back through the self-healing
    policy builder and reports whether a concrete recovery decision *would* be
    produced. Purely additive — it never gates the planner-feedback loop, so the
    locked FIXTURE-AUDIT-1 ``report['passed']`` contract is unaffected.
    """
    bundle = dict(failure_bundle or {}) if isinstance(failure_bundle, dict) else {}
    action_trace = bundle.get("action_trace")
    has_action_trace = isinstance(action_trace, dict) and bool(action_trace)
    browser_state = bundle.get("browser_state") if isinstance(bundle.get("browser_state"), dict) else {}
    history = bundle.get("action_history") or bundle.get("history")
    score = build_action_reliability_score(
        action_trace if isinstance(action_trace, dict) else {},
        browser_state,
        history,
    )
    policy = dict(score.get("self_healing_policy") or {})
    recommended_actions = [str(item) for item in (policy.get("recommended_actions") or []) if str(item or "")]
    concrete_actions = [item for item in recommended_actions if item != "continue"]
    risk_level = str(score.get("risk_level") or "low")
    has_recovery_decision = bool(has_action_trace and (concrete_actions or risk_level in {"medium", "high"}))
    return {
        "version": _CAPABILITY_FAILURE_RECOVERY_DECISION_REPLAY_VERSION,
        "has_action_trace": bool(has_action_trace),
        "risk_level": risk_level,
        "score": float(score.get("score") or 0.0),
        "retry_allowed": bool(policy.get("retry_allowed")),
        "requires_snapshot_refresh": bool(policy.get("requires_snapshot_refresh")),
        "requires_planner_replan": bool(policy.get("requires_planner_replan")),
        "recommended_action": str(policy.get("recommended_action") or ""),
        "recommended_actions": recommended_actions,
        "has_recovery_decision": has_recovery_decision,
    }


def replay_capability_failure_fixtures(fixtures: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    failed_check_counts: dict[str, int] = {}
    primary_failure_counts: dict[str, int] = {}
    failure_category_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    capability_counts: dict[str, int] = {}
    for index, fixture in enumerate(fixtures or []):
        data = dict(fixture or {}) if isinstance(fixture, dict) else {}
        report = replay_capability_failure_fixture(data)
        expected = dict(data.get("expected") or {}) if isinstance(data.get("expected"), dict) else {}
        failure_bundle = _fixture_failure_bundle(data)
        fixture_info = dict(report.get("fixture") or {}) if isinstance(report.get("fixture"), dict) else {}
        checks = [dict(item) for item in (report.get("checks") or []) if isinstance(item, dict)]
        failed_checks = [
            str(item.get("name") or "")
            for item in checks
            if not bool(item.get("passed")) and str(item.get("name") or "")
        ]
        for name in failed_checks:
            failed_check_counts[name] = failed_check_counts.get(name, 0) + 1
        primary_failure = str(fixture_info.get("primary_failure") or expected.get("primary_failure") or failure_bundle.get("primary_failure") or "")
        failure_category = str(fixture_info.get("failure_category") or expected.get("failure_category") or failure_bundle.get("failure_category") or "")
        action = str(expected.get("action") or failure_bundle.get("action") or "")
        capability = str(expected.get("capability") or failure_bundle.get("capability") or "")
        _increment_count(primary_failure_counts, primary_failure)
        _increment_count(failure_category_counts, failure_category)
        _increment_count(action_counts, action)
        _increment_count(capability_counts, capability)
        items.append({
            "index": index,
            "name": str(fixture_info.get("name") or data.get("name") or f"fixture_{index + 1}"),
            "version": str(fixture_info.get("version") or data.get("version") or ""),
            "primary_failure": primary_failure,
            "failure_category": failure_category,
            "action": action,
            "capability": capability,
            "passed": bool(report.get("passed")),
            "check_count": len(checks),
            "failed_check_count": len(failed_checks),
            "failed_checks": failed_checks[:8],
            "report": report,
        })
    passed_count = sum(1 for item in items if bool(item.get("passed")))
    failed_count = len(items) - passed_count
    recovery_decision_count = sum(
        1
        for item in items
        if bool(((item.get("report") or {}).get("recovery_decision") or {}).get("has_recovery_decision"))
    )
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
        "version": _CAPABILITY_FAILURE_FIXTURE_REPLAY_BATCH_REPORT_VERSION,
        "fixture_count": len(items),
        "passed_count": passed_count,
        "failed_count": failed_count,
        "passed": bool(items) and failed_count == 0,
        "summary": {
            "status": status,
            "blocking": failed_count > 0,
            "recovery_decision_count": recovery_decision_count,
            "top_primary_failures": top_primary_failures,
            "top_failure_categories": _count_rows(failure_category_counts),
            "top_actions": _count_rows(action_counts),
            "top_capabilities": _count_rows(capability_counts),
            "top_failed_checks": top_failed_checks,
            "recommended_focus": " · ".join(recommended_focus_parts),
        },
        "failed_checks": failed_checks,
        "items": items,
    }


def _increment_count(counts: dict[str, int], name: str) -> None:
    key = str(name or "unknown")
    counts[key] = counts.get(key, 0) + 1


def _count_rows(counts: dict[str, int]) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count, "failed_count": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _fixture_failure_bundle(fixture: dict[str, Any]) -> dict[str, Any]:
    replay = dict(fixture.get("replay") or {}) if isinstance(fixture.get("replay"), dict) else {}
    failure_bundle = replay.get("failure_bundle") or fixture.get("failure_bundle")
    if isinstance(failure_bundle, dict) and str(failure_bundle.get("version") or "") == "capability_execute_failure_bundle.v1":
        return dict(failure_bundle)
    return {}


def _fixture_route_goal(inputs: dict[str, Any], expected: dict[str, Any], failure_bundle: dict[str, Any]) -> str:
    goal = str(inputs.get("goal") or "")
    if goal:
        return goal
    action = str(expected.get("action") or failure_bundle.get("action") or "action")
    primary_failure = str(expected.get("primary_failure") or failure_bundle.get("primary_failure") or "failure")
    return f"Replay browser {action} failure after {primary_failure}"


def _replay_checks(
    fixture: dict[str, Any],
    failure_bundle: dict[str, Any],
    expected: dict[str, Any],
    planner_feedback: dict[str, Any],
    route: dict[str, Any],
    execution_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    primary_failure = str(expected.get("primary_failure") or failure_bundle.get("primary_failure") or "")
    risk_flags = list(execution_plan.get("risk_flags") or [])
    notes = [str(item) for item in (execution_plan.get("notes") or []) if str(item or "")]
    checks = [
        _check("fixture.version", str(fixture.get("version") or ""), _CAPABILITY_FAILURE_REGRESSION_FIXTURE_VERSION),
        _check("replay.failure_bundle.version", str(failure_bundle.get("version") or ""), "capability_execute_failure_bundle.v1"),
        _check("failure_bundle.primary_failure", str(failure_bundle.get("primary_failure") or ""), primary_failure),
        _check("planner_feedback.version", str(planner_feedback.get("version") or ""), "planner_feedback.v1"),
        _check("planner_feedback.primary_failure", str(planner_feedback.get("primary_failure") or ""), primary_failure),
        _check("planner_feedback.preferred_capabilities", bool(planner_feedback.get("preferred_capabilities")), True),
        _check("planner_feedback.avoid_actions", bool(planner_feedback.get("avoid_actions")), True),
        _check("route.signals.planner_feedback", bool((route.get("signals") or {}).get("planner_feedback")), True),
        _check("route.planner_feedback.primary_failure", str((route.get("planner_feedback") or {}).get("primary_failure") or ""), primary_failure),
        _check("execution_plan.planner_feedback.primary_failure", str((execution_plan.get("planner_feedback") or {}).get("primary_failure") or ""), primary_failure),
        _check("execution_plan.risk_flags.previous_failure_feedback_active", "previous_failure_feedback_active" in risk_flags, True),
        _check("execution_plan.risk_flags.previous_failure_code", f"previous_failure_{primary_failure}" in risk_flags, True),
        _check("execution_plan.notes.previous_failure", any("Planner feedback from previous failure" in item for item in notes), True),
    ]
    return checks


def _check(name: str, actual: Any, expected: Any) -> dict[str, Any]:
    return {
        "name": name,
        "passed": actual == expected,
        "actual": actual,
        "expected": expected,
    }

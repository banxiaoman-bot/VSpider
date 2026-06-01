from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_CAPABILITY_FAILURE_REGRESSION_FIXTURE_VERSION = "capability_failure_regression_fixture.v1"


def build_capability_failure_regression_fixture(source: dict[str, Any] | None = None, *, name: str = "", tags: list[str] | None = None) -> dict[str, Any]:
    data = dict(source or {}) if isinstance(source, dict) else {}
    failure_bundle = _extract_failure_bundle(data)
    if not failure_bundle:
        return {}
    result = _extract_execute_result(data)
    request = dict(data.get("request") or {}) if isinstance(data.get("request"), dict) else {}
    action_trace = dict(failure_bundle.get("action_trace") or {}) if isinstance(failure_bundle.get("action_trace"), dict) else {}
    action_issue_summary = dict(failure_bundle.get("action_issue_summary") or {}) if isinstance(failure_bundle.get("action_issue_summary"), dict) else {}
    primary_failure = str(failure_bundle.get("primary_failure") or "")
    failure_category = str(failure_bundle.get("failure_category") or "")
    recommended_actions = [
        str(item)
        for item in (failure_bundle.get("recommended_actions") or [])
        if str(item or "") and str(item or "") != "continue"
    ]
    fixture_name = _fixture_name(name, primary_failure, str(action_trace.get("action") or failure_bundle.get("action") or "failure"))
    fixture_tags = _fixture_tags(tags or [], primary_failure, failure_category, action_trace)
    return {
        "version": _CAPABILITY_FAILURE_REGRESSION_FIXTURE_VERSION,
        "source": _fixture_source(data),
        "name": fixture_name,
        "tags": fixture_tags,
        "inputs": {
            "goal": str(request.get("goal") or result.get("goal") or ""),
            "url": str(request.get("url") or result.get("url") or ""),
            "run_id": str(request.get("run_id") or ""),
            "route_intent": dict((result.get("route") or {}).get("intent") or {}) if isinstance(result.get("route"), dict) else {},
        },
        "expected": {
            "primary_failure": primary_failure,
            "failure_category": failure_category,
            "action": str(action_trace.get("action") or failure_bundle.get("action") or ""),
            "capability": str(failure_bundle.get("capability") or result.get("capability") or ""),
            "recommended_action": str(failure_bundle.get("recommended_action") or ""),
            "recommended_actions": list(dict.fromkeys(recommended_actions)),
            "warning_codes": [str(item) for item in (action_trace.get("warning_codes") or []) if str(item or "")],
            "issue_codes": [
                str(item.get("code") or "")
                for item in (action_issue_summary.get("issues") or [])
                if isinstance(item, dict) and str(item.get("code") or "")
            ],
        },
        "replay": {
            "phase_event": _extract_phase_event(data, result),
            "failure_bundle": dict(failure_bundle),
        },
        "assertions": _fixture_assertions(failure_bundle, action_trace, action_issue_summary),
    }


def write_capability_failure_regression_fixture(source: dict[str, Any] | None, output_dir: str | Path, *, name: str = "", tags: list[str] | None = None) -> Path:
    fixture = build_capability_failure_regression_fixture(source, name=name, tags=tags)
    if not fixture:
        raise ValueError("source does not contain capability_execute_failure_bundle.v1")
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{_safe_filename(str(fixture.get('name') or 'capability_failure'))}.json"
    target.write_text(json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def _extract_execute_result(data: dict[str, Any]) -> dict[str, Any]:
    if str(data.get("type") or "") == "capability_execute_trace" and isinstance(data.get("result"), dict):
        return dict(data.get("result") or {})
    if str(data.get("phase") or "") == "capability_execute":
        return dict(data)
    result = data.get("result")
    if isinstance(result, dict):
        return dict(result)
    return dict(data)


def _extract_failure_bundle(data: dict[str, Any]) -> dict[str, Any]:
    candidates: list[Any] = [data.get("failure_bundle")]
    if isinstance(data.get("detail"), dict):
        detail = data.get("detail") or {}
        candidates.append(detail.get("failure_bundle"))
        if isinstance(detail.get("phase_event"), dict):
            candidates.append((detail.get("phase_event") or {}).get("failure_bundle"))
    if isinstance(data.get("result"), dict):
        candidates.append((data.get("result") or {}).get("failure_bundle"))
    if isinstance(data.get("phase_event"), dict):
        candidates.append((data.get("phase_event") or {}).get("failure_bundle"))
    for item in candidates:
        if isinstance(item, dict) and str(item.get("version") or "") == "capability_execute_failure_bundle.v1":
            return dict(item)
    return {}


def _extract_phase_event(data: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    if str(data.get("phase") or "") == "capability_execute":
        return dict(data)
    if isinstance(data.get("phase_event"), dict):
        return dict(data.get("phase_event") or {})
    if isinstance(data.get("detail"), dict) and isinstance((data.get("detail") or {}).get("phase_event"), dict):
        return dict((data.get("detail") or {}).get("phase_event") or {})
    return {
        "type": "phase",
        "phase": "capability_execute",
        "severity": "error",
        "message": str(result.get("fallback_reason") or "capability execute failed"),
        "execution_status": result.get("status"),
        "completed": bool(result.get("completed")),
        "capability": result.get("capability"),
        "failure_bundle": result.get("failure_bundle"),
    }


def _fixture_source(data: dict[str, Any]) -> str:
    if str(data.get("type") or "") == "capability_execute_trace":
        return "capability_execute_trace"
    if str(data.get("phase") or "") == "capability_execute":
        return "capability_execute_phase_event"
    if isinstance(data.get("detail"), dict):
        return "http_error_detail"
    return "capability_execute_failure_bundle"


def _fixture_name(name: str, primary_failure: str, action: str) -> str:
    value = str(name or "").strip()
    if not value:
        value = "_".join(item for item in ("capability", action, primary_failure) if item)
    return _safe_filename(value or "capability_failure")


def _fixture_tags(tags: list[str], primary_failure: str, failure_category: str, action_trace: dict[str, Any]) -> list[str]:
    out = [str(item) for item in tags if str(item or "")]
    out.extend(["capability_execute", "browser_action_failure"])
    if primary_failure:
        out.append(primary_failure)
    if failure_category:
        out.append(failure_category)
    action = str(action_trace.get("action") or "")
    if action:
        out.append(f"action_{action}")
    return list(dict.fromkeys(out))


def _fixture_assertions(failure_bundle: dict[str, Any], action_trace: dict[str, Any], action_issue_summary: dict[str, Any]) -> list[dict[str, Any]]:
    assertions = [
        {"path": "failure_bundle.version", "equals": "capability_execute_failure_bundle.v1"},
        {"path": "failure_bundle.primary_failure", "equals": str(failure_bundle.get("primary_failure") or "")},
    ]
    if failure_bundle.get("failure_category"):
        assertions.append({"path": "failure_bundle.failure_category", "equals": str(failure_bundle.get("failure_category") or "")})
    if action_trace.get("action"):
        assertions.append({"path": "action_trace.action", "equals": str(action_trace.get("action") or "")})
    if action_issue_summary.get("status"):
        assertions.append({"path": "action_issue_summary.status", "equals": str(action_issue_summary.get("status") or "")})
    return assertions


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip()).strip("._-")
    return safe[:120] or "capability_failure"

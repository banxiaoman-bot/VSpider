"""Capability artifact persistence helpers.

Extracted from api_server.py (Slice 3) — pure persistence functions for
capability execution traces, efficiency feedback replay artifacts, and
capability failure fixture artifacts.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from visual_web_agent.artifact_manager import (
    artifact_root,
    artifact_url,
    register_artifact,
    resolve_artifact_path,
)

logger = logging.getLogger("vspider.api")


def _write_capability_execute_artifact(payload: dict[str, Any], result: dict[str, Any]) -> dict[str, str]:
    run_id = str(payload.get("run_id") or payload.get("trace_id") or "manual")
    safe_run_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", run_id).strip("._-") or "manual"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = resolve_artifact_path(f"capability_execute_{safe_run_id}_{stamp}.json", subdir="capability")
    safe_request = {
        key: payload.get(key)
        for key in ("goal", "prompt", "url", "target_url", "start_url", "run_id", "trace_id")
        if payload.get(key) not in (None, "")
    }
    doc = {
        "type": "capability_execute_trace",
        "created_at": time.time(),
        "request": safe_request,
        "result": result,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _register_capability_execute_trace_artifact(path, payload)
    return {"path": str(path), "url": artifact_url(path)}


def _capability_payload_text(payload: Any, keys: tuple[str, ...], *, _depth: int = 0) -> str:
    if _depth > 6:
        return ""
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value).strip()
        for value in payload.values():
            found = _capability_payload_text(value, keys, _depth=_depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _capability_payload_text(value, keys, _depth=_depth + 1)
            if found:
                return found
    return ""


def _register_capability_report_artifact(
    path: Path,
    payload: dict[str, Any],
    *,
    produced_by: str,
    step_id: str,
) -> None:
    raw_run_id = _capability_payload_text(payload, ("run_id", "trace_id"))
    run_manifest_id = re.sub(r"[^0-9A-Za-z_-]+", "_", raw_run_id).strip("_")
    if not run_manifest_id:
        register_artifact(path)
        return
    source_url = _capability_payload_text(payload, ("url", "target_url", "start_url"))
    try:
        register_artifact(
            path,
            run_id=run_manifest_id,
            kind="log",
            mime="application/json",
            source_url=source_url,
            produced_by=produced_by,
            step_id=step_id,
        )
    except TypeError:
        register_artifact(path)


def _register_capability_execute_trace_artifact(path: Path, payload: dict[str, Any]) -> None:
    _register_capability_report_artifact(
        path,
        payload,
        produced_by="capability_execute",
        step_id="trace_artifact",
    )


def _capability_failure_fixture_source(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("source", "trace", "artifact", "detail", "result", "failure_bundle", "phase_event"):
        item = payload.get(key)
        if isinstance(item, dict):
            if key in {"failure_bundle", "phase_event"}:
                return {key: item}
            return dict(item)
    return dict(payload)


def _capability_failure_fixture_replay_source(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("fixture", "source", "report_source"):
        item = payload.get(key)
        if isinstance(item, dict):
            return dict(item)
    return dict(payload)


def _efficiency_feedback_replay_source(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("efficiency_correlation_report", "planner_feedback", "source", "report_source", "result", "detail", "artifact", "phase_event"):
        item = payload.get(key)
        if isinstance(item, dict):
            if key in {"efficiency_correlation_report", "planner_feedback"}:
                source = {key: item}
                for text_key in ("goal", "prompt", "task", "url", "target_url", "start_url"):
                    if payload.get(text_key):
                        source[text_key] = payload.get(text_key)
                return source
            return dict(item)
    return dict(payload)


def _write_efficiency_feedback_replay_artifact(report: dict[str, Any], payload: dict[str, Any]) -> dict[str, str]:
    feedback = dict(report.get("planner_feedback") or {}) if isinstance(report.get("planner_feedback"), dict) else {}
    raw_name = str(payload.get("name") or feedback.get("primary_failure") or payload.get("run_id") or "efficiency_feedback_replay")
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw_name).strip("._-") or "efficiency_feedback_replay"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = resolve_artifact_path(f"{safe_name}_{stamp}.json", subdir="capability/efficiency_feedback_replays")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _register_capability_report_artifact(
        path,
        payload,
        produced_by="efficiency_feedback_replay",
        step_id="replay_report",
    )
    return {"path": str(path), "url": artifact_url(path)}


def _write_efficiency_feedback_replay_batch_artifact(report: dict[str, Any], payload: dict[str, Any]) -> dict[str, str]:
    raw_name = str(payload.get("name") or payload.get("run_id") or "efficiency_feedback_replay_batch")
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw_name).strip("._-") or "efficiency_feedback_replay_batch"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = resolve_artifact_path(f"{safe_name}_{stamp}.json", subdir="capability/efficiency_feedback_replay_batches")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _register_capability_report_artifact(
        path,
        payload,
        produced_by="efficiency_feedback_replay",
        step_id="replay_batch_report",
    )
    return {"path": str(path), "url": artifact_url(path)}


def _efficiency_feedback_replay_artifact_dir() -> Path:
    return resolve_artifact_path("_efficiency_feedback_replay_dir_probe", subdir="capability/efficiency_feedback_replays").parent


def _read_efficiency_feedback_replay_artifact(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict) and str(data.get("version") or "") == "efficiency_feedback_replay_report.v1":
        return dict(data)
    return {}


def _efficiency_feedback_replay_summary(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    feedback = dict(report.get("planner_feedback") or {}) if isinstance(report.get("planner_feedback"), dict) else {}
    execution_plan = dict(report.get("execution_plan") or {}) if isinstance(report.get("execution_plan"), dict) else {}
    feedback_step = dict(execution_plan.get("feedback_step") or {}) if isinstance(execution_plan.get("feedback_step"), dict) else {}
    failed_checks = [
        str(item.get("name") or "")
        for item in (report.get("failed_checks") or [])
        if isinstance(item, dict) and str(item.get("name") or "")
    ]
    stat = path.stat()
    return {
        "name": path.stem,
        "path": str(path),
        "url": artifact_url(path),
        "modified_at": stat.st_mtime,
        "passed": bool(report.get("passed")),
        "primary_failure": str(feedback.get("primary_failure") or ""),
        "recommended_action": str(feedback.get("recommended_action") or ""),
        "preferred_capabilities": [str(item) for item in (feedback.get("preferred_capabilities") or []) if str(item or "")][:8],
        "avoid_actions": [str(item) for item in (feedback.get("avoid_actions") or []) if str(item or "")][:8],
        "failed_check_count": len(failed_checks),
        "failed_checks": failed_checks[:8],
        "feedback_step": str(feedback_step.get("capability") or ""),
    }


def _list_efficiency_feedback_replay_artifacts(limit: int = 50) -> list[dict[str, Any]]:
    root = _efficiency_feedback_replay_artifact_dir()
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if len(items) >= max(0, limit):
            break
        report = _read_efficiency_feedback_replay_artifact(path)
        if not report:
            continue
        items.append(_efficiency_feedback_replay_summary(path, report))
    return items


def _list_efficiency_feedback_replay_sources(limit: int = 100) -> list[dict[str, Any]]:
    root = _efficiency_feedback_replay_artifact_dir()
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if len(items) >= max(0, limit):
            break
        report = _read_efficiency_feedback_replay_artifact(path)
        feedback = dict(report.get("planner_feedback") or {}) if isinstance(report.get("planner_feedback"), dict) else {}
        if not feedback:
            continue
        items.append({
            "name": path.stem,
            "planner_feedback": feedback,
            "artifact": {"path": str(path), "url": artifact_url(path)},
        })
    return items


def _capability_failure_fixture_artifact_dir() -> Path:
    return resolve_artifact_path("_fixture_dir_probe", subdir="capability/failure_fixtures").parent


def _read_capability_failure_fixture_artifact(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict) and str(data.get("version") or "") == "capability_failure_regression_fixture.v1":
        return dict(data)
    return {}


def _capability_failure_fixture_summary(path: Path, fixture: dict[str, Any]) -> dict[str, Any]:
    expected = dict(fixture.get("expected") or {}) if isinstance(fixture.get("expected"), dict) else {}
    stat = path.stat()
    return {
        "name": str(fixture.get("name") or path.stem),
        "path": str(path),
        "url": artifact_url(path),
        "modified_at": stat.st_mtime,
        "primary_failure": str(expected.get("primary_failure") or ""),
        "failure_category": str(expected.get("failure_category") or ""),
        "action": str(expected.get("action") or ""),
        "capability": str(expected.get("capability") or ""),
        "tags": [str(item) for item in (fixture.get("tags") or []) if str(item or "")],
    }


def _list_capability_failure_fixture_artifacts(limit: int = 100) -> list[dict[str, Any]]:
    root = _capability_failure_fixture_artifact_dir()
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if len(items) >= max(0, limit):
            break
        fixture = _read_capability_failure_fixture_artifact(path)
        if not fixture:
            continue
        summary = _capability_failure_fixture_summary(path, fixture)
        summary["fixture"] = fixture
        items.append(summary)
    return items


def _write_capability_failure_fixture_artifact(fixture: dict[str, Any], payload: dict[str, Any]) -> dict[str, str]:
    raw_name = str(payload.get("name") or fixture.get("name") or payload.get("run_id") or "capability_failure")
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw_name).strip("._-") or "capability_failure"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = resolve_artifact_path(f"{safe_name}_{stamp}.json", subdir="capability/failure_fixtures")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _register_capability_report_artifact(
        path,
        payload,
        produced_by="capability_failure_fixture",
        step_id="failure_fixture",
    )
    return {"path": str(path), "url": artifact_url(path)}


def _write_capability_failure_fixture_replay_artifact(report: dict[str, Any], payload: dict[str, Any]) -> dict[str, str]:
    fixture = dict(report.get("fixture") or {}) if isinstance(report.get("fixture"), dict) else {}
    raw_name = str(payload.get("name") or fixture.get("name") or payload.get("run_id") or "capability_failure_replay")
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw_name).strip("._-") or "capability_failure_replay"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = resolve_artifact_path(f"{safe_name}_{stamp}.json", subdir="capability/failure_fixture_replays")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _register_capability_report_artifact(
        path,
        payload,
        produced_by="capability_failure_fixture_replay",
        step_id="replay_report",
    )
    return {"path": str(path), "url": artifact_url(path)}


def _write_capability_failure_fixture_replay_batch_artifact(report: dict[str, Any], payload: dict[str, Any]) -> dict[str, str]:
    raw_name = str(payload.get("name") or payload.get("run_id") or "capability_failure_replay_batch")
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw_name).strip("._-") or "capability_failure_replay_batch"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = resolve_artifact_path(f"{safe_name}_{stamp}.json", subdir="capability/failure_fixture_replay_batches")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _register_capability_report_artifact(
        path,
        payload,
        produced_by="capability_failure_fixture_replay",
        step_id="replay_batch_report",
    )
    return {"path": str(path), "url": artifact_url(path)}


def _capability_failure_fixture_replay_batch_artifact_dir() -> Path:
    return resolve_artifact_path("_batch_replay_dir_probe", subdir="capability/failure_fixture_replay_batches").parent


def _read_capability_failure_fixture_replay_batch_artifact(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict) and str(data.get("version") or "") == "capability_failure_fixture_replay_batch_report.v1":
        return dict(data)
    return {}


def _capability_failure_fixture_replay_batch_summary(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    summary = dict(report.get("summary") or {}) if isinstance(report.get("summary"), dict) else {}
    stat = path.stat()
    return {
        "name": path.stem,
        "path": str(path),
        "url": artifact_url(path),
        "modified_at": stat.st_mtime,
        "fixture_count": int(report.get("fixture_count") or 0),
        "passed_count": int(report.get("passed_count") or 0),
        "failed_count": int(report.get("failed_count") or 0),
        "passed": bool(report.get("passed")),
        "status": str(summary.get("status") or ("passed" if report.get("passed") else "failed")),
        "recommended_focus": str(summary.get("recommended_focus") or ""),
        "top_primary_failures": list(summary.get("top_primary_failures") or [])[:5],
        "top_failed_checks": list(summary.get("top_failed_checks") or [])[:5],
    }


def _list_capability_failure_fixture_replay_batch_artifacts(limit: int = 50) -> list[dict[str, Any]]:
    root = _capability_failure_fixture_replay_batch_artifact_dir()
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if len(items) >= max(0, limit):
            break
        report = _read_capability_failure_fixture_replay_batch_artifact(path)
        if not report:
            continue
        items.append(_capability_failure_fixture_replay_batch_summary(path, report))
    return items


def _capability_failure_fixture_replay_batch_pass_rate(report: dict[str, Any]) -> float:
    fixture_count = int(report.get("fixture_count") or 0)
    if fixture_count <= 0:
        return 0.0
    return round(float(int(report.get("passed_count") or 0)) / float(fixture_count), 4)


def _capability_failure_fixture_replay_batch_history_trend(reports: list[dict[str, Any]]) -> dict[str, Any]:
    latest = dict(reports[0]) if reports else {}
    previous = dict(reports[1]) if len(reports) > 1 else {}
    latest_failed = int(latest.get("failed_count") or 0)
    previous_failed = int(previous.get("failed_count") or 0)
    latest_pass_rate = _capability_failure_fixture_replay_batch_pass_rate(latest)
    previous_pass_rate = _capability_failure_fixture_replay_batch_pass_rate(previous)
    failed_delta = latest_failed - previous_failed if previous else 0
    pass_rate_delta = round(latest_pass_rate - previous_pass_rate, 4) if previous else 0.0
    if not latest:
        direction = "empty"
    elif not previous:
        direction = "baseline"
    elif failed_delta < 0:
        direction = "improved"
    elif failed_delta > 0:
        direction = "regressed"
    elif pass_rate_delta > 0:
        direction = "improved"
    elif pass_rate_delta < 0:
        direction = "regressed"
    else:
        direction = "stable"
    latest_focus = str(latest.get("recommended_focus") or "")
    previous_focus = str(previous.get("recommended_focus") or "")
    return {
        "version": "capability_failure_fixture_replay_batch_history_trend.v1",
        "report_count": len(reports),
        "direction": direction,
        "latest_report": str(latest.get("name") or ""),
        "previous_report": str(previous.get("name") or ""),
        "latest_status": str(latest.get("status") or ""),
        "previous_status": str(previous.get("status") or ""),
        "latest_failed_count": latest_failed,
        "previous_failed_count": previous_failed,
        "failed_count_delta": failed_delta,
        "latest_pass_rate": latest_pass_rate,
        "previous_pass_rate": previous_pass_rate,
        "pass_rate_delta": pass_rate_delta,
        "latest_recommended_focus": latest_focus,
        "previous_recommended_focus": previous_focus,
        "focus_changed": bool(previous and latest_focus != previous_focus),
    }

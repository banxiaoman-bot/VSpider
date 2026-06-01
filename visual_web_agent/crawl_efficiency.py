from __future__ import annotations

from typing import Any


_CRAWL_EFFICIENCY_PLAN_VERSION = "crawl_efficiency_plan.v1"
_PREFERRED_ORDER = (
    "api_replay",
    "html_extract",
    "dom_selector",
    "targeted_probe",
    "browser_action",
    "vision_agent",
)


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


def _payload_source(payload: dict[str, Any]) -> str:
    value = payload.get("source")
    if value in (None, ""):
        value = payload.get("html")
    if isinstance(value, str):
        return value
    return ""


def _has_html_signal(source: str) -> bool:
    text = str(source or "").lower()
    return "<html" in text or "<table" in text or "<body" in text or "<div" in text or "<li" in text


def _candidate_score(candidate: dict[str, Any]) -> int:
    for key in ("score", "row_count", "count"):
        try:
            value = int(candidate.get(key) or 0)
        except Exception:
            value = 0
        if value:
            return value
    return 0


def _network_candidates(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    values = inputs.get("network_candidates")
    if values is None:
        values = inputs.get("candidates")
    rows: list[dict[str, Any]] = []
    for item in _as_list(values):
        if isinstance(item, dict):
            rows.append(dict(item))
    return rows


def _browser_state(inputs: dict[str, Any]) -> dict[str, Any]:
    state = _as_dict(inputs.get("browser_state"))
    if state:
        return state
    payload = _as_dict(inputs.get("payload"))
    return _as_dict(payload.get("browser_state"))


def _interactive_count(browser_state: dict[str, Any]) -> int:
    metrics = _as_dict(browser_state.get("metrics"))
    if metrics.get("interactive_count") not in (None, ""):
        return int(metrics.get("interactive_count") or 0)
    perception = _as_dict(browser_state.get("perception"))
    som = _as_dict(perception.get("som"))
    return int(som.get("interactive_count") or len(_as_list(som.get("elements"))))


def _runtime_status(inputs: dict[str, Any], browser_state: dict[str, Any]) -> str:
    runtime = _as_dict(inputs.get("runtime_status")) or _as_dict(browser_state.get("runtime"))
    return str(runtime.get("status") or runtime.get("runtime_status") or "unknown")


def _add_candidate(candidates: list[dict[str, Any]], name: str, available: bool, score: float, reason: str, evidence: dict[str, Any] | None = None) -> None:
    candidates.append({
        "name": name,
        "available": bool(available),
        "score": round(max(0.0, min(1.0, float(score or 0.0))), 4),
        "reason": str(reason or ""),
        "evidence": dict(evidence or {}),
    })


def build_crawl_efficiency_plan(
    payload: dict[str, Any] | None = None,
    *,
    route: dict[str, Any] | None = None,
    network_candidates: list[dict[str, Any]] | None = None,
    browser_state: dict[str, Any] | None = None,
    runtime_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload_data = _as_dict(payload)
    route_data = _as_dict(route)
    inputs = {
        "payload": payload_data,
        "route": route_data,
        "network_candidates": network_candidates,
        "browser_state": browser_state,
        "runtime_status": runtime_status,
    }
    source = _payload_source(payload_data)
    html_signal = _has_html_signal(source)
    selector = str(payload_data.get("selector") or (_as_dict(payload_data.get("extract")).get("selector") or ""))
    candidates = _network_candidates(inputs)
    best_network_score = max([_candidate_score(item) for item in candidates] or [0])
    state = _browser_state(inputs)
    interactive_count = _interactive_count(state) if state else 0
    runtime = _runtime_status(inputs, state)
    route_intent = _as_dict(route_data.get("intent"))
    task_type = str(route_intent.get("task_type") or "")
    structured = task_type in {"structured_extraction", "crawl_extract"} or bool(route_data.get("signals", {}).get("structured") if isinstance(route_data.get("signals"), dict) else False)
    browser_needed = task_type in {"browser_interaction", "form_or_transaction", "ai_chat_workflow"} or bool(payload_data.get("requires_browser"))
    _add_candidate(
        candidates := [],
        "api_replay",
        best_network_score > 0,
        0.95 if best_network_score >= 8 else (0.82 if best_network_score > 0 else 0.0),
        "captured network candidate can be replayed before opening or driving a browser" if best_network_score > 0 else "no captured API/XHR candidate evidence",
        {"candidate_count": len(_network_candidates(inputs)), "best_score": best_network_score},
    )
    _add_candidate(
        candidates,
        "html_extract",
        bool(source and html_signal),
        0.78 if source and html_signal else 0.0,
        "HTML/source is available for deterministic extraction" if source and html_signal else "HTML/source not available",
        {"source_length": len(source), "html_signal": html_signal},
    )
    _add_candidate(
        candidates,
        "dom_selector",
        bool(source and selector),
        0.72 if source and selector else 0.0,
        "selector and source are available for deterministic DOM selection" if source and selector else "selector/source pair not available",
        {"selector": selector},
    )
    _add_candidate(
        candidates,
        "targeted_probe",
        bool(state and interactive_count > 0 and browser_needed),
        0.62 if state and interactive_count > 0 and browser_needed else 0.0,
        "browser_state.v2 has interactive elements for local targeted probing" if state and interactive_count > 0 and browser_needed else "targeted probing needs browser_state.v2 plus browser interaction intent",
        {"browser_state_version": state.get("version"), "interactive_count": interactive_count},
    )
    _add_candidate(
        candidates,
        "browser_action",
        bool(browser_needed and runtime.lower() not in {"unavailable", "unhealthy", "error", "offline"}),
        0.46 if browser_needed else 0.0,
        "browser action is available but should run after deterministic data/DOM options" if browser_needed else "browser action not required by current intent",
        {"runtime_status": runtime, "browser_needed": browser_needed},
    )
    _add_candidate(
        candidates,
        "vision_agent",
        True,
        0.18,
        "vision model is the last resort for ambiguous visual grounding",
        {"structured": structured, "browser_needed": browser_needed},
    )
    ranked = sorted(
        candidates,
        key=lambda item: (
            0 if item["name"] in _PREFERRED_ORDER else 1,
            _PREFERRED_ORDER.index(item["name"]) if item["name"] in _PREFERRED_ORDER else 99,
        ),
    )
    available = [item for item in ranked if item.get("available")]
    recommended = available[0]["name"] if available else "vision_agent"
    skip_browser = recommended in {"api_replay", "html_extract", "dom_selector"}
    skip_vlm = recommended != "vision_agent"
    return {
        "version": _CRAWL_EFFICIENCY_PLAN_VERSION,
        "source": "crawl_efficiency",
        "preferred_order": list(_PREFERRED_ORDER),
        "recommended_path": recommended,
        "skip_browser": bool(skip_browser),
        "skip_vlm": bool(skip_vlm),
        "candidates": ranked,
        "available_paths": [item["name"] for item in available],
        "efficiency_summary": {
            "network_candidate_count": len(_network_candidates(inputs)),
            "best_network_score": best_network_score,
            "html_source_available": bool(source and html_signal),
            "selector_available": bool(selector),
            "browser_state_available": bool(state),
            "interactive_count": interactive_count,
            "runtime_status": runtime,
            "browser_needed": browser_needed,
        },
    }

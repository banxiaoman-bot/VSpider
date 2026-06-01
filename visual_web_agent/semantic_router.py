"""Semantic routing enrichment — planner signals fused into capability routing."""

from __future__ import annotations

import re
from typing import Any

from visual_web_agent.experience_reuse import build_experience_hints
from visual_web_agent.planner_exit_criteria import parse_subgoal_plan
from visual_web_agent.page_understanding import (
    page_understanding_policy,
    reprioritize_agent_tools,
)


_PERCEPTION_CAP = {
    "name": "targeted_probe",
    "layer": "perception",
    "slice": "page_understanding",
    "endpoints": ["targeted_probe"],
    "reason": "Local intent-matched perception before full-page SoM or site macros.",
    "model_role": "deterministic_router",
    "risk": "low",
}

_EXTRACT_CAP = {
    "name": "generic_extractor",
    "layer": "extraction",
    "slice": "page_understanding",
    "endpoints": ["POST /api/extractor/run"],
    "reason": "Structured extraction after targeted_probe surfaces table/list candidates.",
    "model_role": "deterministic_router",
    "risk": "low",
}


def infer_semantic_route_signals(
    goal: str,
    url: str,
    *,
    experience_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = f"{goal or ''} {url or ''}".lower()
    experience = experience_hints or build_experience_hints(goal, url)
    page_policy = page_understanding_policy(goal, url)
    prefer_api = bool(re.search(r"\b(api|xhr|json|network|replay)\b|接口|网络", text, re.I))
    prefer_extract = bool(re.search(r"\b(extract|table|list|scrape)\b|提取|表格|列表", text, re.I))
    prefer_form = bool(re.search(r"\b(fill|form|submit)\b|填写|表单|提交", text, re.I))
    return {
        "version": "semantic_route.v1",
        "page_understanding_first": bool(page_policy.get("generic_surface")),
        "prefer_api_fast_path": prefer_api,
        "prefer_structured_extract": prefer_extract,
        "prefer_form_macros": prefer_form and not page_policy.get("generic_surface"),
        "experience_reuse": experience,
        "recommended_capabilities": list(experience.get("recommended_capabilities") or []),
    }


def apply_semantic_route_enrichment(
    *,
    goal: str,
    url: str,
    signals: dict[str, Any],
    selected_tools: list[dict[str, Any]],
    strategy_context: dict[str, Any] | None = None,
    experience_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    experience = experience_hints or build_experience_hints(goal, url)
    semantic = infer_semantic_route_signals(goal, url, experience_hints=experience)
    tools = reprioritize_agent_tools(selected_tools, goal=goal, url=url)

    inject: list[dict[str, Any]] = []
    if semantic.get("page_understanding_first"):
        inject.append(dict(_PERCEPTION_CAP))
    if semantic.get("prefer_structured_extract"):
        inject.append(dict(_EXTRACT_CAP))

    strategy = dict(strategy_context or {})
    strategy["semantic_route"] = semantic
    strategy["experience_reuse"] = experience
    if semantic.get("recommended_capabilities"):
        caps = list(strategy.get("capabilities") or [])
        for cap in semantic["recommended_capabilities"]:
            if cap not in caps:
                caps.append(cap)
        strategy["capabilities"] = caps

    patched_signals = dict(signals)
    if semantic.get("page_understanding_first"):
        patched_signals["page_understanding_first"] = True
    if experience.get("rpa_cache", {}).get("hit"):
        patched_signals["rpa_cache_available"] = True
    if experience.get("failure_fixtures"):
        patched_signals["failure_fixture_hints"] = True

    return {
        "semantic_route": semantic,
        "experience_reuse": experience,
        "selected_agent_tools": tools,
        "inject_capabilities": inject,
        "signals_patch": patched_signals,
        "strategy_context_patch": strategy,
    }


def inject_capabilities_into_plan(
    backend_plan: list[dict[str, Any]],
    inject: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not inject:
        return backend_plan
    existing = {str(item.get("name") or "") for item in backend_plan if isinstance(item, dict)}
    plan = list(backend_plan)
    for cap in reversed(inject):
        name = str(cap.get("name") or "")
        if not name or name in existing:
            continue
        item = dict(cap)
        item.setdefault("owner", item.get("model_role") or "deterministic_router")
        plan.insert(0, item)
        existing.add(name)
    return plan


def merge_task_plan_into_route(task_plan: Any, route: dict[str, Any]) -> dict[str, Any]:
    """Fuse planner sub-goals back into an existing capability route."""

    if task_plan is None or not isinstance(route, dict):
        return route
    sub_goals = list(getattr(task_plan, "sub_goals", None) or [])
    if not sub_goals:
        return route
    merged = dict(route)
    descriptions = [str(getattr(sg, "description", "") or "") for sg in sub_goals]
    try:
        merged["planner_exit_criteria"] = parse_subgoal_plan(task_plan)
    except Exception:
        merged["planner_exit_criteria"] = []
    merged["planner_sub_goals"] = descriptions
    semantic = dict(merged.get("semantic_route") or {})
    semantic["planner_sub_goal_count"] = len(descriptions)
    semantic["planner_fused"] = True
    merged["semantic_route"] = semantic
    strategy = dict(merged.get("strategy_context") or {})
    strategy["planner_sub_goals"] = descriptions
    merged["strategy_context"] = strategy
    execution_plan = dict(merged.get("execution_plan") or {})
    if execution_plan:
        execution_plan["planner_sub_goals"] = descriptions
        merged["execution_plan"] = execution_plan
    return merged

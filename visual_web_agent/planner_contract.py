from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from visual_web_agent.capability_manifest import build_default_capability_manifest, summarize_capabilities
from visual_web_agent.task_templates import TaskTemplate


@dataclass(frozen=True)
class SuccessCriteria:
    signals: tuple[str, ...] = ()
    target_count: int | None = None
    target_pages: int | None = None
    required_fields: tuple[str, ...] = ()
    artifact_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "signals": list(self.signals),
            "target_count": self.target_count,
            "target_pages": self.target_pages,
            "required_fields": list(self.required_fields),
            "artifact_required": self.artifact_required,
        }


@dataclass(frozen=True)
class PlanStep:
    id: str
    order: int
    capability: str
    title: str
    purpose: str
    owner: str
    deterministic: bool
    changes_state: bool
    risk: str
    cost: str
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    success_criteria: SuccessCriteria = field(default_factory=SuccessCriteria)
    fallback_to: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    endpoints: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order": self.order,
            "capability": self.capability,
            "title": self.title,
            "purpose": self.purpose,
            "owner": self.owner,
            "deterministic": self.deterministic,
            "changes_state": self.changes_state,
            "risk": self.risk,
            "cost": self.cost,
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs),
            "success_criteria": self.success_criteria.to_dict(),
            "fallback_to": list(self.fallback_to),
            "preconditions": list(self.preconditions),
            "endpoints": list(self.endpoints),
            "actions": list(self.actions),
        }


@dataclass(frozen=True)
class ExecutionPlan:
    version: str
    goal: str
    url: str
    intent: dict[str, Any]
    systems: tuple[dict[str, Any], ...]
    steps: tuple[PlanStep, ...]
    fallback_chain: tuple[dict[str, Any], ...]
    capability_summary: tuple[dict[str, Any], ...]
    risk_flags: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    planner_feedback: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "goal": self.goal,
            "url": self.url,
            "intent": dict(self.intent),
            "systems": [dict(item) for item in self.systems],
            "steps": [step.to_dict() for step in self.steps],
            "fallback_chain": [dict(item) for item in self.fallback_chain],
            "capability_summary": [dict(item) for item in self.capability_summary],
            "risk_flags": list(self.risk_flags),
            "notes": list(self.notes),
            "planner_feedback": dict(self.planner_feedback),
        }


def build_execution_plan(route: dict[str, Any]) -> dict[str, Any]:
    goal = str(route.get("goal") or "")
    url = str(route.get("url") or "")
    intent = dict(route.get("intent") or {})
    signals = dict(route.get("signals") or {})
    strategy_context = dict(route.get("strategy_context") or {})
    planner_feedback = dict(route.get("planner_feedback") or {})
    template_data = route.get("template") if isinstance(route.get("template"), dict) else {}
    template = TaskTemplate(
        id=str(template_data.get("id") or ""),
        name=str(template_data.get("name") or ""),
        task_type=str(template_data.get("task_type") or ""),
        description=str(template_data.get("description") or ""),
        triggers=tuple(str(item) for item in (template_data.get("triggers") or []) if item),
        steps=(),
    ) if template_data else None
    failure_repair = dict(route.get("failure_repair") or {})
    backend_plan = [item for item in (route.get("backend_plan") or []) if isinstance(item, dict)]
    fallback_chain = [item for item in (route.get("fallback_chain") or []) if isinstance(item, dict)]
    manifest = build_default_capability_manifest()
    steps: list[PlanStep] = []
    for item in backend_plan:
        name = str(item.get("name") or "")
        if not name:
            continue
        spec = manifest.get(name)
        owner = str(item.get("owner") or (spec.owner if spec else "deterministic_router"))
        deterministic = bool(item.get("deterministic", spec.deterministic if spec else True))
        step_id = _safe_step_id(len(steps) + 1, name)
        step = PlanStep(
            id=step_id,
            order=len(steps) + 1,
            capability=name,
            title=_title_for(name, item, spec, template),
            purpose=str(item.get("reason") or (spec.description if spec else "")),
            owner=owner,
            deterministic=deterministic,
            changes_state=bool(spec.changes_state if spec else _default_changes_state(name)),
            risk=str(item.get("risk") or (spec.risk if spec else "low")),
            cost=str(spec.cost if spec else _default_cost(owner)),
            inputs={**_step_inputs(name, route), **({"failure_repair": failure_repair} if failure_repair and name in set(failure_repair.get("preferred_capabilities") or []) else {})},
            outputs=dict(spec.output_schema if spec else {}),
            success_criteria=_success_criteria(name, route),
            fallback_to=tuple(_fallback_targets(name, fallback_chain, spec)),
            preconditions=tuple(spec.preconditions if spec else ()),
            endpoints=tuple(item.get("endpoints_or_actions") or (spec.endpoints if spec else ())),
            actions=tuple(spec.actions if spec else ()),
        )
        steps.append(step)
    risk_flags = _risk_flags(signals, steps, planner_feedback)
    systems = tuple(_systems_from_route(route))
    names = [step.capability for step in steps]
    plan = ExecutionPlan(
        version="planner_contract.v1",
        goal=goal,
        url=url,
        intent=intent,
        systems=systems,
        steps=tuple(steps),
        fallback_chain=tuple(fallback_chain),
        capability_summary=tuple(summarize_capabilities(names)),
        risk_flags=tuple(risk_flags),
        notes=tuple(_plan_notes(intent, signals, steps, planner_feedback)),
        planner_feedback=planner_feedback,
    )
    return plan.to_dict()


def _success_criteria(name: str, route: dict[str, Any]) -> SuccessCriteria:
    strategy = dict(route.get("strategy_context") or {})
    signals = dict(route.get("signals") or {})
    target_count = strategy.get("target_count")
    target_pages = strategy.get("target_pages")
    required_fields = tuple(str(item) for item in (strategy.get("requested_fields") or []) if item)
    artifact_required = bool(signals.get("artifact_required") or (route.get("intent") or {}).get("requires_artifact"))
    if name in {"generic_extractor", "extractor_select", "spider_lite", "api_replay"}:
        observed = ("row_count", "item_count", "required_fields", "verification.passed")
    elif name in {"feed_export", "artifact_manager"}:
        observed = ("artifact.path", "artifact.url")
    elif name in {"browser_control", "action_registry_macros"}:
        observed = ("action_result", "readback", "page_state")
    elif name == "human_guard":
        observed = ("human_required", "safe_abort")
    else:
        observed = ("status", "evidence")
    return SuccessCriteria(
        signals=observed,
        target_count=int(target_count) if isinstance(target_count, int) and target_count > 0 else None,
        target_pages=int(target_pages) if isinstance(target_pages, int) and target_pages > 0 else None,
        required_fields=required_fields,
        artifact_required=artifact_required if name in {"generic_extractor", "extractor_select", "spider_lite", "feed_export", "artifact_manager"} else False,
    )


def _step_inputs(name: str, route: dict[str, Any]) -> dict[str, Any]:
    strategy = dict(route.get("strategy_context") or {})
    planner_feedback = dict(route.get("planner_feedback") or {})
    base = {"goal": route.get("goal") or "", "url": route.get("url") or ""}
    if name in {"generic_extractor", "extractor_select"}:
        base.update({
            "requested_fields": strategy.get("requested_fields") or [],
            "target_count": strategy.get("target_count"),
            "target_pages": strategy.get("target_pages"),
        })
    elif name == "spider_lite":
        base.update({
            "start_url": route.get("url") or "",
            "max_pages": strategy.get("target_pages") or "planner_defined",
            "extract": "planner_defined",
        })
    elif name == "api_replay":
        base.update({"candidate_source": "network_intelligence"})
    elif name == "browser_control":
        base.update({"snapshot": "ax_or_som", "action_ref": "@ref"})
    elif name == "feed_export":
        base.update({"format": strategy.get("output_mode") or (route.get("intent") or {}).get("output_mode") or "jsonl"})
    if name in {"browser_pool", "browser_backend_abstraction", "browser_control"} and route.get("runtime_preflight"):
        base["runtime_preflight"] = _runtime_preflight_step_input(route.get("runtime_preflight"))
    if planner_feedback and name in set(planner_feedback.get("preferred_capabilities") or []) | {"browser_control", "action_ref_normalizer", "selector_generator", "browser_control_find", "semantic_planner_reflector"}:
        base["planner_feedback"] = _planner_feedback_step_input(planner_feedback)
    return {key: value for key, value in base.items() if value not in (None, "", [])}


def _runtime_preflight_step_input(preflight: Any) -> dict[str, Any]:
    data = dict(preflight or {}) if isinstance(preflight, dict) else {}
    return {
        "version": str(data.get("version") or ""),
        "status": str(data.get("status") or ""),
        "blocking": bool(data.get("blocking")),
        "warnings": list(data.get("warnings") or []),
        "recommended_action": str(data.get("recommended_action") or ""),
        "summary": dict(data.get("summary") or {}),
    }


def _planner_feedback_step_input(feedback: Any) -> dict[str, Any]:
    data = dict(feedback or {}) if isinstance(feedback, dict) else {}
    target = dict(data.get("target") or {}) if isinstance(data.get("target"), dict) else {}
    return {
        "version": str(data.get("version") or ""),
        "primary_failure": str(data.get("primary_failure") or ""),
        "failure_category": str(data.get("failure_category") or ""),
        "failed_action": str(data.get("failed_action") or ""),
        "recommended_action": str(data.get("recommended_action") or ""),
        "recommended_actions": list(data.get("recommended_actions") or []),
        "avoid_actions": list(data.get("avoid_actions") or []),
        "target": {
            "ref": str(target.get("ref") or ""),
            "selector": str(target.get("selector") or ""),
        },
    }


def _fallback_targets(name: str, fallback_chain: list[dict[str, Any]], spec: Any) -> list[str]:
    explicit = list(spec.fallback_to) if spec else []
    chain_names = [str(item.get("capability") or "") for item in fallback_chain if item.get("capability")]
    if name in chain_names:
        idx = chain_names.index(name)
        for candidate in chain_names[idx + 1: idx + 4]:
            if candidate and candidate not in explicit:
                explicit.append(candidate)
    return explicit[:6]


def _systems_from_route(route: dict[str, Any]) -> list[dict[str, Any]]:
    url = str(route.get("url") or "")
    parsed = urlparse(url)
    domain = parsed.netloc.split("@")[-1].split(":", 1)[0] if parsed.netloc else (route.get("signals") or {}).get("domain") or ""
    if not domain:
        return []
    return [{
        "id": "system_1",
        "type": "web",
        "domain": domain,
        "url": url,
        "session": "default_browser_session",
    }]


def _risk_flags(signals: dict[str, Any], steps: list[PlanStep], planner_feedback: dict[str, Any] | None = None) -> list[str]:
    flags: list[str] = []
    feedback = dict(planner_feedback or {})
    if signals.get("auth_or_captcha"):
        flags.append("auth_or_captcha_requires_guard")
    if signals.get("bot_challenge"):
        flags.append("anti_bot_challenge_guarded")
    if any(step.risk != "low" for step in steps):
        flags.append("medium_or_higher_risk_step")
    if any(not step.deterministic for step in steps):
        flags.append("model_dependent_fallback_present")
    if signals.get("browser_interaction"):
        flags.append("browser_state_changes_possible")
    if feedback:
        flags.append("previous_failure_feedback_active")
        primary_failure = str(feedback.get("primary_failure") or "")
        if primary_failure:
            flags.append(f"previous_failure_{primary_failure}")
    return flags


def _plan_notes(intent: dict[str, Any], signals: dict[str, Any], steps: list[PlanStep], planner_feedback: dict[str, Any] | None = None) -> list[str]:
    notes = ["Prefer deterministic data/API/extractor capabilities before VLM browser grounding."]
    feedback = dict(planner_feedback or {})
    if intent.get("requires_visual_grounding"):
        notes.append("Use vision grounding only after semantic locator, selector, or macro paths are insufficient.")
    if signals.get("artifact_required"):
        notes.append("Do not mark extraction complete until artifact export or trace artifact evidence exists.")
    if any(step.capability == "human_guard" for step in steps):
        notes.append("CAPTCHA, 2FA, auth walls, and high-risk barriers must escalate instead of blind retries.")
    if feedback:
        failure = str(feedback.get("primary_failure") or "previous_failure")
        action = str(feedback.get("recommended_action") or "")
        if action:
            notes.append(f"Planner feedback from previous failure {failure}: prefer {action} before repeating the same browser action.")
        avoid_actions = [str(item) for item in (feedback.get("avoid_actions") or []) if str(item or "")]
        if avoid_actions:
            notes.append(f"Avoid after previous failure: {', '.join(avoid_actions[:3])}.")
    return notes


def _safe_step_id(order: int, name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name.lower()).strip("_") or "step"
    return f"step_{order:02d}_{safe}"


def _title_for(name: str, item: dict[str, Any], spec: Any, template: TaskTemplate | None = None) -> str:
    stage = str(item.get("stage") or (spec.category if spec else "capability"))
    if template is not None and template.name:
        return f"{stage}: {name} [{template.name}]"
    return f"{stage}: {name}"


def _default_changes_state(name: str) -> bool:
    return name in {"task_queue", "robots_throttle", "spider_lite", "feed_export", "browser_control", "action_registry_macros", "vision_agent"}


def _default_cost(owner: str) -> str:
    if owner == "vision_model":
        return "high"
    if owner == "semantic_model":
        return "medium"
    return "low"

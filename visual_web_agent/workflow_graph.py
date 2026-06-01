from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class WorkflowSystem:
    id: str
    type: str
    name: str
    domain: str = ""
    url: str = ""
    auth_required: bool = False
    capabilities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "domain": self.domain,
            "url": self.url,
            "auth_required": self.auth_required,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class WorkflowSession:
    id: str
    system_id: str
    type: str
    state: str = "planned"
    auth_profile: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "system_id": self.system_id,
            "type": self.type,
            "state": self.state,
            "auth_profile": self.auth_profile,
        }


@dataclass(frozen=True)
class WorkflowNode:
    id: str
    order: int
    step_id: str
    system_id: str
    session_id: str
    capability: str
    purpose: str
    owner: str
    risk: str
    deterministic: bool
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    verifies: tuple[str, ...] = ()
    fallback_to: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    success_criteria: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order": self.order,
            "step_id": self.step_id,
            "system_id": self.system_id,
            "session_id": self.session_id,
            "capability": self.capability,
            "purpose": self.purpose,
            "owner": self.owner,
            "risk": self.risk,
            "deterministic": self.deterministic,
            "produces": list(self.produces),
            "consumes": list(self.consumes),
            "verifies": list(self.verifies),
            "fallback_to": list(self.fallback_to),
            "preconditions": list(self.preconditions),
            "success_criteria": dict(self.success_criteria),
        }


@dataclass(frozen=True)
class WorkflowDataEdge:
    id: str
    source_node_id: str
    target_node_id: str
    data_type: str
    contract: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "data_type": self.data_type,
            "contract": dict(self.contract),
        }


@dataclass(frozen=True)
class WorkflowGraph:
    version: str
    goal: str
    intent: dict[str, Any]
    systems: tuple[WorkflowSystem, ...]
    sessions: tuple[WorkflowSession, ...]
    nodes: tuple[WorkflowNode, ...]
    data_edges: tuple[WorkflowDataEdge, ...]
    artifacts: tuple[dict[str, Any], ...] = ()
    repair_branches: tuple[dict[str, Any], ...] = ()
    risk_flags: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    failure_repair: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "goal": self.goal,
            "intent": dict(self.intent),
            "systems": [item.to_dict() for item in self.systems],
            "sessions": [item.to_dict() for item in self.sessions],
            "nodes": [item.to_dict() for item in self.nodes],
            "data_edges": [item.to_dict() for item in self.data_edges],
            "artifacts": [dict(item) for item in self.artifacts],
            "repair_branches": [dict(item) for item in self.repair_branches],
            "risk_flags": list(self.risk_flags),
            "notes": list(self.notes),
            "failure_repair": dict(self.failure_repair),
        }


def build_workflow_graph(route: dict[str, Any]) -> dict[str, Any]:
    execution_plan = dict(route.get("execution_plan") or {})
    failure_repair = dict(route.get("failure_repair") or {})
    goal = str(route.get("goal") or execution_plan.get("goal") or "")
    intent = dict(route.get("intent") or execution_plan.get("intent") or {})
    systems = _build_systems(route, execution_plan)
    sessions = tuple(
        WorkflowSession(
            id=f"session_{idx + 1}",
            system_id=system.id,
            type="browser" if system.type == "web" else "logical",
            auth_profile="required" if system.auth_required else "default",
        )
        for idx, system in enumerate(systems)
    )
    nodes = _build_nodes(execution_plan, systems, sessions)
    data_edges = _build_edges(nodes, execution_plan)
    artifacts = _planned_artifacts(nodes, intent)
    repair_branches = _repair_branches(failure_repair, nodes)
    graph = WorkflowGraph(
        version="workflow_graph.v1",
        goal=goal,
        intent=intent,
        systems=tuple(systems),
        sessions=sessions,
        nodes=tuple(nodes),
        data_edges=tuple(data_edges),
        artifacts=tuple(artifacts),
        repair_branches=tuple(repair_branches),
        risk_flags=tuple(_risk_flags(route, execution_plan, nodes)),
        notes=tuple(_notes(route, execution_plan, systems, nodes)),
        failure_repair=failure_repair,
    )
    return graph.to_dict()


def _build_systems(route: dict[str, Any], execution_plan: dict[str, Any]) -> list[WorkflowSystem]:
    candidates: list[dict[str, Any]] = []
    if isinstance(execution_plan.get("systems"), list):
        candidates.extend(item for item in execution_plan["systems"] if isinstance(item, dict))
    context = route.get("context") if isinstance(route.get("context"), dict) else {}
    for item in context.get("systems") or []:
        if isinstance(item, dict):
            candidates.append(item)
    urls = _extract_urls(str(route.get("goal") or ""))
    if route.get("url"):
        urls.insert(0, str(route.get("url") or ""))
    for url in urls:
        candidates.append({"type": "web", "url": url})
    seen_domains: set[str] = set()
    systems: list[WorkflowSystem] = []
    auth_required = bool((route.get("signals") or {}).get("auth_or_captcha"))
    for item in candidates:
        url = str(item.get("url") or "")
        parsed = urlparse(url)
        domain = str(item.get("domain") or parsed.netloc.split("@")[-1].split(":", 1)[0] or "")
        key = domain or str(item.get("name") or item.get("id") or len(systems) + 1)
        if key in seen_domains:
            continue
        seen_domains.add(key)
        system_id = str(item.get("id") or f"system_{len(systems) + 1}")
        systems.append(WorkflowSystem(
            id=system_id,
            type=str(item.get("type") or "web"),
            name=str(item.get("name") or domain or system_id),
            domain=domain,
            url=url,
            auth_required=auth_required or bool(item.get("auth_required")),
            capabilities=tuple(str(cap) for cap in item.get("capabilities") or [] if cap),
        ))
    if not systems:
        systems.append(WorkflowSystem(id="system_1", type="logical", name="default_task_context"))
    return systems


# Capabilities that mutate / observe a browser context — these MUST be
# attributed to a concrete browser-backed system so the Agent loop (E1b)
# can decide which session to acquire.
_BROWSER_BOUND_CAPABILITIES: frozenset[str] = frozenset({
    "browser_control",
    "browser_control_find",
    "browser_pool",
    "browser_backend_abstraction",
    "browser_runtime_doctor",
    "selector_generator",
    "action_registry_macros",
    "action_ref_normalizer",
    "auto_form_fill",
    "vision_agent",
    "media_harvester",
    "human_guard",
    "hover_and_click",
    "next_page",
    "targeted_probe",
})

# Capabilities that consume a URL but do not need a browser context
# (pure HTTP / API replay). They are attributed to a web system when one
# exists so cross-system traces still surface the network hop, but they
# do not block on browser session availability.
_NETWORK_BOUND_CAPABILITIES: frozenset[str] = frozenset({
    "api_replay",
    "network_intelligence",
    "spider_lite",
})

# Capabilities that are purely logical: they transform data already in
# memory or write artefacts. They get attributed to the dedicated
# ``system_logical`` slot when one is present; otherwise they fall back
# to the first system so the graph stays connected.
_LOGICAL_CAPABILITIES: frozenset[str] = frozenset({
    "generic_extractor",
    "extractor_select",
    "item_pipeline",
    "feed_export",
    "artifact_manager",
    "run_registry",
    "task_queue",
    "capability_manifest",
})


def _resolve_node_system(
    capability: str,
    step: dict[str, Any],
    systems: list[WorkflowSystem],
    sessions: tuple[WorkflowSession, ...],
    order_idx: int,
) -> tuple[str, str]:
    """Return ``(system_id, session_id)`` for one node.

    Resolution order:

    1. Explicit ``step["system_id"]`` (and optional ``step["session_id"]``)
       provided by the planner wins outright.
    2. Browser- or network-bound capabilities are attributed to a web
       system. When multiple web systems exist, we walk them in plan
       order so the second URL gets steps after the first URL has been
       handled.
    3. Logical capabilities prefer a system whose ``type != "web"`` if
       one was declared (``logical``), otherwise the first system.
    4. Unknown capabilities fall back to the first system.

    ``session_id`` is looked up from ``sessions`` by ``system_id``;
    when no session matches we fall back to the first session.
    """

    if not systems:
        return ("system_1", "session_1")

    explicit_system = str(step.get("system_id") or "").strip()
    explicit_session = str(step.get("session_id") or "").strip()

    web_systems = [s for s in systems if s.type == "web"]
    logical_systems = [s for s in systems if s.type != "web"]

    chosen: WorkflowSystem | None = None
    if explicit_system:
        for s in systems:
            if s.id == explicit_system:
                chosen = s
                break

    if chosen is None:
        if capability in _LOGICAL_CAPABILITIES:
            chosen = logical_systems[0] if logical_systems else systems[0]
        elif capability in _BROWSER_BOUND_CAPABILITIES or capability in _NETWORK_BOUND_CAPABILITIES:
            if web_systems:
                # Round-robin across web systems by node order so the
                # second URL eventually gets steps too.
                chosen = web_systems[order_idx % len(web_systems)]
            else:
                chosen = systems[0]
        else:
            chosen = systems[0]

    system_id = chosen.id
    if explicit_session:
        return (system_id, explicit_session)
    for s in sessions:
        if s.system_id == system_id:
            return (system_id, s.id)
    return (system_id, sessions[0].id if sessions else "session_1")


def _build_nodes(
    execution_plan: dict[str, Any],
    systems: list[WorkflowSystem],
    sessions: tuple[WorkflowSession, ...],
) -> list[WorkflowNode]:
    nodes: list[WorkflowNode] = []
    for idx, step in enumerate(execution_plan.get("steps") or []):
        if not isinstance(step, dict):
            continue
        capability = str(step.get("capability") or "")
        if not capability:
            continue
        produces = tuple(_produces_for(capability, step))
        consumes = tuple(_consumes_for(capability, nodes))
        verifies = tuple(str(item) for item in ((step.get("success_criteria") or {}).get("signals") or []) if item)
        system_id, session_id = _resolve_node_system(capability, step, systems, sessions, idx)
        nodes.append(WorkflowNode(
            id=f"node_{idx + 1:02d}_{_safe_name(capability)}",
            order=idx + 1,
            step_id=str(step.get("id") or f"step_{idx + 1:02d}"),
            system_id=system_id,
            session_id=session_id,
            capability=capability,
            purpose=str(step.get("purpose") or ""),
            owner=str(step.get("owner") or "deterministic_router"),
            risk=str(step.get("risk") or "low"),
            deterministic=bool(step.get("deterministic", True)),
            produces=produces,
            consumes=consumes,
            verifies=verifies,
            fallback_to=tuple(str(item) for item in (step.get("fallback_to") or []) if item),
            preconditions=tuple(str(item) for item in (step.get("preconditions") or []) if item),
            success_criteria=dict(step.get("success_criteria") or {}),
        ))
    return nodes


def _build_edges(nodes: list[WorkflowNode], execution_plan: dict[str, Any]) -> list[WorkflowDataEdge]:
    edges: list[WorkflowDataEdge] = []
    for idx in range(1, len(nodes)):
        source = nodes[idx - 1]
        target = nodes[idx]
        data_type = _edge_type(source, target)
        contract = {
            "source_capability": source.capability,
            "target_capability": target.capability,
            "success_criteria": _step_success_contract(target.step_id, execution_plan),
            "preconditions": list(target.preconditions),
        }
        edges.append(WorkflowDataEdge(
            id=f"edge_{idx:02d}_{_safe_name(source.capability)}_to_{_safe_name(target.capability)}",
            source_node_id=source.id,
            target_node_id=target.id,
            data_type=data_type,
            contract=contract,
        ))
    return edges


def _produces_for(capability: str, step: dict[str, Any]) -> list[str]:
    if capability in {"network_intelligence", "api_replay"}:
        return ["api_candidates", "structured_payload"]
    if capability in {"generic_extractor", "extractor_select", "spider_lite", "item_pipeline"}:
        return ["items"]
    if capability in {"feed_export", "artifact_manager"}:
        return ["artifact"]
    if capability in {"browser_control", "selector_generator", "action_registry_macros", "vision_agent"}:
        return ["browser_state", "action_evidence"]
    if capability == "human_guard":
        return ["human_decision"]
    return ["evidence"]


def _consumes_for(capability: str, previous_nodes: list[WorkflowNode]) -> list[str]:
    if not previous_nodes:
        return ["task_intent"]
    if capability in {"api_replay"}:
        return ["api_candidates"]
    if capability in {"generic_extractor", "extractor_select", "spider_lite"}:
        return ["source_html_or_json", "task_intent"]
    if capability in {"item_pipeline", "feed_export", "artifact_manager"}:
        return ["items"]
    if capability in {"browser_control", "selector_generator", "action_registry_macros", "vision_agent"}:
        return ["browser_state", "task_intent"]
    if capability == "human_guard":
        return ["risk_signal", "evidence"]
    return ["previous_evidence"]


def _edge_type(source: WorkflowNode, target: WorkflowNode) -> str:
    if "artifact" in target.produces:
        return "items_to_artifact"
    if "items" in target.produces:
        return "source_to_items"
    if "browser_state" in target.produces:
        return "state_transition"
    if target.capability == "human_guard":
        return "risk_escalation"
    return "evidence_flow"


def _step_success_contract(step_id: str, execution_plan: dict[str, Any]) -> dict[str, Any]:
    for step in execution_plan.get("steps") or []:
        if isinstance(step, dict) and step.get("id") == step_id:
            return dict(step.get("success_criteria") or {})
    return {}


def _repair_branches(failure_repair: dict[str, Any], nodes: list[WorkflowNode]) -> list[dict[str, Any]]:
    if not failure_repair:
        return []
    repair_actions = [str(item) for item in (failure_repair.get("repair_actions") or []) if str(item or "")]
    preferred = [str(item) for item in (failure_repair.get("preferred_capabilities") or []) if str(item or "")]
    if not repair_actions and not preferred:
        return []
    branch_nodes = [node.id for node in nodes if node.capability in preferred]
    return [{
        "id": "repair_branch_1",
        "primary_failure": str(failure_repair.get("primary_failure") or ""),
        "repair_actions": repair_actions,
        "preferred_capabilities": preferred,
        "entry_nodes": branch_nodes,
        "transitions": [
            {"from": node.id, "to": node.id, "mode": "repair_retry"} for node in nodes if node.capability in preferred
        ],
    }]


def _planned_artifacts(nodes: list[WorkflowNode], intent: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if intent.get("requires_artifact") or any("artifact" in node.produces for node in nodes):
        artifacts.append({
            "id": "artifact_1",
            "type": "execution_evidence",
            "producer": next((node.id for node in nodes if "artifact" in node.produces), "planned_export"),
            "required": True,
        })
    return artifacts


def _risk_flags(route: dict[str, Any], execution_plan: dict[str, Any], nodes: list[WorkflowNode]) -> list[str]:
    flags = list(execution_plan.get("risk_flags") or [])
    if len({node.system_id for node in nodes}) > 1 and "cross_system" not in flags:
        flags.append("cross_system")
    if any(node.capability == "human_guard" for node in nodes) and "human_guard_present" not in flags:
        flags.append("human_guard_present")
    return flags


def _notes(route: dict[str, Any], execution_plan: dict[str, Any], systems: list[WorkflowSystem], nodes: list[WorkflowNode]) -> list[str]:
    notes = ["Workflow graph is generated from planner_contract.v1 and can be executed step-by-step in a future kernel."]
    if len(systems) > 1:
        notes.append("Multiple systems detected; keep per-system session state and verify data handoff edges.")
    if any(node.capability in {"generic_extractor", "spider_lite", "api_replay"} for node in nodes):
        notes.append("Data acquisition nodes should finish before browser mutation nodes when possible.")
    notes.extend(str(item) for item in execution_plan.get("notes") or [] if item)
    return notes


def _extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s)\]}>\"']+", text or ""):
        value = match.group(0)
        if value not in urls:
            urls.append(value)
    return urls


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").lower()).strip("_")
    return safe or "node"

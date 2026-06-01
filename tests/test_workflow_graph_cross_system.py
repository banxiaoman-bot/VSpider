"""Tests for ``workflow_graph`` cross-system node attribution (E1).

The pre-E1 behaviour of :func:`build_workflow_graph` bound every node to
``systems[0]`` regardless of how many systems were declared, so the
``cross_system`` risk flag could never fire. These tests pin the new
behaviour introduced by ``_resolve_node_system``:

- browser- and network-bound capabilities are spread across declared
  web systems (round-robin) so the second URL is actually represented,
- logical capabilities prefer a non-web ``system_logical`` slot,
- explicit ``step["system_id"]`` always wins,
- ``risk_flags`` now contains ``"cross_system"`` whenever nodes span
  more than one ``system_id``.
"""

from __future__ import annotations

from typing import Any

import pytest

from visual_web_agent.capability_router import route_task
from visual_web_agent.workflow_graph import (
    WorkflowSession,
    WorkflowSystem,
    _resolve_node_system,
    build_workflow_graph,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _systems_pair() -> list[WorkflowSystem]:
    return [
        WorkflowSystem(id="system_1", type="web", name="a.example", domain="a.example"),
        WorkflowSystem(id="system_2", type="web", name="b.example", domain="b.example"),
    ]


def _sessions_for(systems: list[WorkflowSystem]) -> tuple[WorkflowSession, ...]:
    return tuple(
        WorkflowSession(id=f"session_{idx + 1}", system_id=s.id, type="browser")
        for idx, s in enumerate(systems)
    )


def _node_system_map(graph: dict[str, Any]) -> dict[str, str]:
    return {item["capability"]: item["system_id"] for item in graph.get("nodes", [])}


# ---------------------------------------------------------------------------
# _resolve_node_system unit tests
# ---------------------------------------------------------------------------


class TestResolveNodeSystem:
    def test_browser_capability_picks_first_web_system_for_first_node(self) -> None:
        systems = _systems_pair()
        sessions = _sessions_for(systems)
        sid, sess = _resolve_node_system("browser_control", {}, systems, sessions, 0)
        assert sid == "system_1"
        assert sess == "session_1"

    def test_browser_capability_round_robin_across_web_systems(self) -> None:
        systems = _systems_pair()
        sessions = _sessions_for(systems)
        # idx=1 -> second web system
        sid, sess = _resolve_node_system("browser_control", {}, systems, sessions, 1)
        assert sid == "system_2"
        assert sess == "session_2"
        # idx=2 wraps back to first
        sid2, _ = _resolve_node_system("browser_control", {}, systems, sessions, 2)
        assert sid2 == "system_1"

    def test_logical_capability_picks_logical_system_when_present(self) -> None:
        systems = [
            WorkflowSystem(id="system_1", type="web", name="a.example", domain="a.example"),
            WorkflowSystem(id="system_logical", type="logical", name="logical"),
        ]
        sessions = _sessions_for(systems)
        sid, _ = _resolve_node_system("generic_extractor", {}, systems, sessions, 0)
        assert sid == "system_logical"

    def test_logical_capability_falls_back_to_first_system_when_no_logical(self) -> None:
        systems = _systems_pair()
        sessions = _sessions_for(systems)
        sid, _ = _resolve_node_system("generic_extractor", {}, systems, sessions, 0)
        assert sid == "system_1"

    def test_unknown_capability_falls_back_to_first_system(self) -> None:
        systems = _systems_pair()
        sessions = _sessions_for(systems)
        sid, _ = _resolve_node_system("totally_made_up_capability", {}, systems, sessions, 0)
        assert sid == "system_1"

    def test_explicit_system_id_wins(self) -> None:
        systems = _systems_pair()
        sessions = _sessions_for(systems)
        step = {"system_id": "system_2"}
        sid, sess = _resolve_node_system("browser_control", step, systems, sessions, 0)
        assert sid == "system_2"
        assert sess == "session_2"

    def test_explicit_session_id_wins(self) -> None:
        systems = _systems_pair()
        sessions = _sessions_for(systems)
        step = {"system_id": "system_1", "session_id": "custom_session_99"}
        sid, sess = _resolve_node_system("browser_control", step, systems, sessions, 0)
        assert sid == "system_1"
        assert sess == "custom_session_99"

    def test_unknown_explicit_system_falls_back_to_capability_rule(self) -> None:
        systems = _systems_pair()
        sessions = _sessions_for(systems)
        step = {"system_id": "system_does_not_exist"}
        sid, _ = _resolve_node_system("browser_control", step, systems, sessions, 1)
        # Falls back to round-robin (idx=1 -> system_2)
        assert sid == "system_2"

    def test_no_systems_returns_default(self) -> None:
        sid, sess = _resolve_node_system("browser_control", {}, [], tuple(), 0)
        assert sid == "system_1"
        assert sess == "session_1"

    def test_network_capability_attributed_to_web_system(self) -> None:
        systems = _systems_pair()
        sessions = _sessions_for(systems)
        sid, _ = _resolve_node_system("api_replay", {}, systems, sessions, 0)
        assert sid == "system_1"

    def test_session_lookup_falls_back_when_system_has_no_session(self) -> None:
        systems = [WorkflowSystem(id="system_alone", type="web", name="x")]
        sessions = (WorkflowSession(id="session_other", system_id="system_orphan", type="browser"),)
        sid, sess = _resolve_node_system("browser_control", {}, systems, sessions, 0)
        assert sid == "system_alone"
        # No matching session for system_alone -> fall back to first session.
        assert sess == "session_other"


# ---------------------------------------------------------------------------
# build_workflow_graph end-to-end via route_task
# ---------------------------------------------------------------------------


class TestBuildWorkflowGraphCrossSystem:
    def test_single_url_does_not_trigger_cross_system(self) -> None:
        route = route_task(
            "抓取这个页面所有商品名称",
            url="https://shop.example.com/list",
        )
        graph = build_workflow_graph(route)
        assert len(graph["systems"]) >= 1
        node_system_ids = {node["system_id"] for node in graph["nodes"]}
        assert len(node_system_ids) == 1
        assert "cross_system" not in graph["risk_flags"]

    def test_two_url_goal_triggers_cross_system(self) -> None:
        route = route_task(
            "从 https://a.example/list 抓取前 3 条数据，然后打开 https://b.example/form 填写表单并导出证据",
            url="https://a.example/list",
        )
        graph = build_workflow_graph(route)
        # >=2 systems declared (could be a.example, b.example, +logical etc.)
        assert len(graph["systems"]) >= 2
        node_system_ids = {node["system_id"] for node in graph["nodes"]}
        # E1 contract: at least 2 distinct system_ids across the node set.
        assert len(node_system_ids) >= 2, (
            f"Expected cross-system attribution, got node systems: {node_system_ids}, "
            f"declared systems: {[s['id'] for s in graph['systems']]}"
        )
        assert "cross_system" in graph["risk_flags"]

    def test_two_url_goal_attributes_nodes_to_each_web_system(self) -> None:
        route = route_task(
            "从 https://a.example/list 抓取前 3 条数据，然后打开 https://b.example/form 填写表单并导出证据",
            url="https://a.example/list",
        )
        graph = build_workflow_graph(route)
        web_system_ids = {s["id"] for s in graph["systems"] if s["type"] == "web"}
        node_web_system_ids = {
            node["system_id"]
            for node in graph["nodes"]
            if node["system_id"] in web_system_ids
        }
        # Both declared web systems should receive at least one node.
        assert node_web_system_ids == web_system_ids

    def test_every_node_has_matching_session(self) -> None:
        route = route_task(
            "从 https://a.example/list 抓取前 3 条数据，然后打开 https://b.example/form 填写表单并导出证据",
            url="https://a.example/list",
        )
        graph = build_workflow_graph(route)
        session_by_id = {s["id"]: s for s in graph["sessions"]}
        for node in graph["nodes"]:
            assert node["session_id"] in session_by_id, (
                f"node {node['id']} has dangling session_id={node['session_id']}"
            )
            # Session.system_id must point back at the node's system.
            assert session_by_id[node["session_id"]]["system_id"] == node["system_id"]

    def test_logical_node_prefers_logical_system_when_one_is_declared(self) -> None:
        # Synthesize a minimal route with a logical system slot. We bypass
        # route_task and feed build_workflow_graph directly so we can pin
        # exactly the systems we want.
        route = {
            "goal": "test logical attribution",
            "execution_plan": {
                "version": "planner_contract.v1",
                "systems": [
                    {"id": "system_web_1", "type": "web", "url": "https://a.example/", "domain": "a.example"},
                    {"id": "system_logical", "type": "logical", "name": "logical"},
                ],
                "steps": [
                    {"id": "step_01", "capability": "browser_control", "purpose": "open"},
                    {"id": "step_02", "capability": "generic_extractor", "purpose": "extract"},
                ],
            },
        }
        graph = build_workflow_graph(route)
        node_map = _node_system_map(graph)
        assert node_map["browser_control"] == "system_web_1"
        assert node_map["generic_extractor"] == "system_logical"

    def test_explicit_step_system_id_is_honoured_end_to_end(self) -> None:
        route = {
            "goal": "test explicit override",
            "execution_plan": {
                "version": "planner_contract.v1",
                "systems": [
                    {"id": "system_a", "type": "web", "url": "https://a.example/", "domain": "a.example"},
                    {"id": "system_b", "type": "web", "url": "https://b.example/", "domain": "b.example"},
                ],
                "steps": [
                    {"id": "step_01", "capability": "browser_control", "purpose": "open A", "system_id": "system_b"},
                    {"id": "step_02", "capability": "browser_control", "purpose": "open B", "system_id": "system_a"},
                ],
            },
        }
        graph = build_workflow_graph(route)
        nodes = graph["nodes"]
        assert nodes[0]["system_id"] == "system_b"
        assert nodes[1]["system_id"] == "system_a"
        assert "cross_system" in graph["risk_flags"]

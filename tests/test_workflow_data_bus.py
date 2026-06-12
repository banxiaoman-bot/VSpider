"""S8: ``workflow_data_bus`` — runtime relay over ``WorkflowDataEdge``.

Pre-S8 ``workflow_graph.data_edges`` was pure declaration: ``_build_edges``
created edges but nothing in the repo consumed them, so cross-system data
handoff (A 系统 items 喂 B 系统 step) only worked through files. These
tests pin the bus runtime: publish on outgoing edges, deliver-once
consume on incoming edges, capability-level addressing for the executor,
payload-free snapshots, the per-run registry, and the ``route_executor``
wiring that publishes a completed capability's result.
"""

from __future__ import annotations

from typing import Any

import pytest

from visual_web_agent.workflow_data_bus import (
    WorkflowDataBus,
    clear_run_bus,
    get_run_bus,
)


def _graph() -> dict[str, Any]:
    """A 2-system graph: extractor on system A feeds form-filler on system B."""

    return {
        "version": "workflow_graph.v1",
        "nodes": [
            {"id": "node_01_generic_extractor", "capability": "generic_extractor",
             "system_id": "sys_a", "produces": ["items"]},
            {"id": "node_02_browser_control", "capability": "browser_control",
             "system_id": "sys_b", "consumes": ["items"]},
            {"id": "node_03_artifact_manager", "capability": "artifact_manager",
             "system_id": "system_logical", "consumes": ["items"]},
        ],
        "data_edges": [
            {"id": "edge_01", "source_node_id": "node_01_generic_extractor",
             "target_node_id": "node_02_browser_control", "data_type": "source_to_items"},
            {"id": "edge_02", "source_node_id": "node_02_browser_control",
             "target_node_id": "node_03_artifact_manager", "data_type": "items_to_artifact"},
        ],
    }


class TestPublishConsume:
    def test_publish_parks_payload_on_outgoing_edges(self) -> None:
        bus = WorkflowDataBus(_graph())
        packets = bus.publish("node_01_generic_extractor", [{"name": "row1"}, {"name": "row2"}])
        assert [p.edge_id for p in packets] == ["edge_01"]
        assert packets[0].item_count == 2
        assert packets[0].target_node_id == "node_02_browser_control"

    def test_consume_delivers_once(self) -> None:
        bus = WorkflowDataBus(_graph())
        rows = [{"name": "row1"}]
        bus.publish("node_01_generic_extractor", rows)
        first = bus.consume("node_02_browser_control")
        assert first == [rows]
        assert bus.consume("node_02_browser_control") == []

    def test_consume_with_mark_false_peeks(self) -> None:
        bus = WorkflowDataBus(_graph())
        bus.publish("node_01_generic_extractor", ["x"])
        assert bus.consume("node_02_browser_control", mark=False) == [["x"]]
        assert bus.consume("node_02_browser_control") == [["x"]]

    def test_data_type_filter(self) -> None:
        bus = WorkflowDataBus(_graph())
        bus.publish("node_02_browser_control", {"items": [1, 2, 3]})
        assert bus.consume("node_03_artifact_manager", data_type="source_to_items") == []
        delivered = bus.consume("node_03_artifact_manager", data_type="items_to_artifact")
        assert delivered == [{"items": [1, 2, 3]}]

    def test_publish_terminal_node_creates_nothing(self) -> None:
        bus = WorkflowDataBus(_graph())
        assert bus.publish("node_03_artifact_manager", ["x"]) == []

    def test_item_count_for_dict_payloads(self) -> None:
        bus = WorkflowDataBus(_graph())
        packets = bus.publish("node_01_generic_extractor", {"rows": [1, 2, 3, 4]})
        assert packets[0].item_count == 4


class TestCapabilityAddressing:
    def test_publish_and_consume_by_capability(self) -> None:
        bus = WorkflowDataBus(_graph())
        packets = bus.publish_by_capability("generic_extractor", [{"k": "v"}])
        assert [p.edge_id for p in packets] == ["edge_01"]
        delivered = bus.consume_by_capability("browser_control")
        assert delivered == [[{"k": "v"}]]

    def test_unknown_capability_is_noop(self) -> None:
        bus = WorkflowDataBus(_graph())
        assert bus.publish_by_capability("ghost_capability", ["x"]) == []
        assert bus.consume_by_capability("ghost_capability") == []


class TestSnapshotAndPending:
    def test_snapshot_is_payload_free(self) -> None:
        bus = WorkflowDataBus(_graph())
        secret_rows = [{"password": "do-not-log"}]
        bus.publish("node_01_generic_extractor", secret_rows)
        snap = bus.snapshot()
        assert snap["version"] == "workflow_data_bus.v1"
        assert snap["edge_count"] == 2
        assert snap["packet_count"] == 1
        assert snap["pending_count"] == 1
        assert "payload" not in snap["packets"][0]
        assert "do-not-log" not in str(snap)

    def test_pending_clears_after_consume(self) -> None:
        bus = WorkflowDataBus(_graph())
        bus.publish("node_01_generic_extractor", ["x"])
        assert len(bus.pending("node_02_browser_control")) == 1
        bus.consume("node_02_browser_control")
        assert bus.pending("node_02_browser_control") == []


class TestRunRegistry:
    def test_same_run_id_shares_one_bus(self) -> None:
        clear_run_bus("run_bus_t1")
        try:
            bus1 = get_run_bus("run_bus_t1", workflow_graph=_graph())
            assert bus1 is not None
            bus1.publish("node_01_generic_extractor", ["relay"])
            bus2 = get_run_bus("run_bus_t1")
            assert bus2 is bus1
            assert bus2.consume("node_02_browser_control") == [["relay"]]
        finally:
            clear_run_bus("run_bus_t1")

    def test_no_graph_no_bus(self) -> None:
        clear_run_bus("run_bus_t2")
        assert get_run_bus("run_bus_t2") is None

    def test_edgeless_graph_no_bus(self) -> None:
        clear_run_bus("run_bus_t3")
        assert get_run_bus("run_bus_t3", workflow_graph={"data_edges": []}) is None

    def test_clear_releases(self) -> None:
        clear_run_bus("run_bus_t4")
        bus = get_run_bus("run_bus_t4", workflow_graph=_graph())
        assert bus is not None
        clear_run_bus("run_bus_t4")
        assert get_run_bus("run_bus_t4") is None


class TestRouteExecutorWiring:
    HTML = """
    <html><body>
      <ul>
        <li class="name">Alice</li>
        <li class="name">Bob</li>
      </ul>
    </body></html>
    """

    PAYLOAD = {
        "goal": "extract data from page",
        "url": "https://a.example/list",
        "source": HTML,
        "selector": "li.name",
        "requested_fields": ["value"],
        "export": True,
    }

    def test_completed_route_publishes_data_handoff(self) -> None:
        from visual_web_agent.route_executor import execute_route

        clear_run_bus("run_bus_exec")
        try:
            outcome = execute_route({**self.PAYLOAD, "run_id": "run_bus_exec"})
            assert outcome["completed"] is True
            handoff = outcome["data_handoff"]
            assert handoff["run_id"] == "run_bus_exec"
            assert handoff["published_capability"] == "extractor_select"
            assert handoff["bus"]["version"] == "workflow_data_bus.v1"
            # extractor_select aliases onto the graph's generic_extractor
            # node, which has a downstream consumer -> packets must exist.
            assert handoff["published_edges"]
        finally:
            clear_run_bus("run_bus_exec")

    def test_downstream_step_consumes_relay_in_same_run(self) -> None:
        """A->B relay: the published items are readable from the same run's
        bus by the downstream node, without any file round-trip."""

        from visual_web_agent.route_executor import execute_route

        clear_run_bus("run_bus_exec2")
        try:
            outcome = execute_route({**self.PAYLOAD, "run_id": "run_bus_exec2"})
            assert outcome["completed"] is True
            bus = get_run_bus("run_bus_exec2")
            assert bus is not None
            pending = bus.pending()
            assert pending, "expected unconsumed packets parked for downstream steps"
            target = pending[0]["target_node_id"]
            delivered = bus.consume(target)
            assert delivered
            # the relayed payload is the extractor result containing both rows
            assert "Alice" in str(delivered[0])
        finally:
            clear_run_bus("run_bus_exec2")

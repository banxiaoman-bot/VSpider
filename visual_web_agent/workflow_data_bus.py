"""In-run data relay over ``WorkflowDataEdge`` (S8).

``workflow_graph`` has always *declared* data edges (`source_node_id ->
target_node_id` with a ``data_type``), but nothing consumed them at
runtime: system A's extracted items could only reach system B's step via a
file on disk. :class:`WorkflowDataBus` is that missing runtime — a pure
in-memory bus, one per run, that:

- is built from a ``workflow_graph`` dict (the edges/nodes are the wiring),
- lets a producing step ``publish()`` its payload onto every outgoing edge,
- lets a consuming step ``consume()`` the packets parked on its incoming
  edges (each packet is delivered once),
- exposes a payload-free ``snapshot()`` for event_stream / evidence so the
  relay is observable without dumping row data into logs.

No FS / network IO. Thread-safe so the reactive loop and background
executors can share one bus. ``get_run_bus`` keeps a per-run registry so
two executor calls inside the same run see the same packets (A -> B relay
across calls); ``clear_run_bus`` releases it when the run ends.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

VERSION = "workflow_data_bus.v1"

# route_executor finishes with the concrete extractor name while planner
# graph nodes carry the engine-level capability; map between them so a
# completed capability still finds its graph node.
_CAPABILITY_FALLBACKS: dict[str, tuple[str, ...]] = {
    "extractor_select": ("generic_extractor",),
}


def _count_items(payload: Any) -> int:
    if payload is None:
        return 0
    if isinstance(payload, (list, tuple)):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("items", "rows", "results", "records"):
            value = payload.get(key)
            if isinstance(value, (list, tuple)):
                return len(value)
        return 1
    return 1


@dataclass
class DataPacket:
    """One payload parked on one edge, delivered to the target at most once."""

    edge_id: str
    source_node_id: str
    target_node_id: str
    data_type: str
    payload: Any
    item_count: int
    produced_at: float = field(default_factory=time.time)
    consumed: bool = False
    consumed_at: float = 0.0

    def meta(self) -> dict[str, Any]:
        """Payload-free description (safe for event_stream / snapshots)."""

        return {
            "edge_id": self.edge_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "data_type": self.data_type,
            "item_count": self.item_count,
            "consumed": self.consumed,
        }


class WorkflowDataBus:
    def __init__(self, workflow_graph: dict[str, Any] | None = None) -> None:
        graph = workflow_graph if isinstance(workflow_graph, dict) else {}
        self._edges: list[dict[str, Any]] = [
            dict(edge) for edge in (graph.get("data_edges") or []) if isinstance(edge, dict)
        ]
        self._nodes_by_id: dict[str, dict[str, Any]] = {}
        self._node_ids_by_capability: dict[str, list[str]] = {}
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "")
            if not node_id:
                continue
            self._nodes_by_id[node_id] = dict(node)
            capability = str(node.get("capability") or "")
            if capability:
                self._node_ids_by_capability.setdefault(capability, []).append(node_id)
        self._packets: list[DataPacket] = []
        self._lock = threading.Lock()

    # ----- topology -------------------------------------------------------

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    def edges_from(self, source_node_id: str) -> list[dict[str, Any]]:
        sid = str(source_node_id or "")
        return [dict(e) for e in self._edges if str(e.get("source_node_id") or "") == sid]

    def edges_to(self, target_node_id: str) -> list[dict[str, Any]]:
        tid = str(target_node_id or "")
        return [dict(e) for e in self._edges if str(e.get("target_node_id") or "") == tid]

    def node_ids_for_capability(self, capability: str) -> list[str]:
        cap = str(capability or "")
        ids = list(self._node_ids_by_capability.get(cap) or [])
        if not ids:
            for alias in _CAPABILITY_FALLBACKS.get(cap, ()):
                ids = list(self._node_ids_by_capability.get(alias) or [])
                if ids:
                    break
        return ids

    # ----- produce --------------------------------------------------------

    def publish(
        self,
        source_node_id: str,
        payload: Any,
        *,
        data_type: str = "",
    ) -> list[DataPacket]:
        """Park ``payload`` on every outgoing edge of ``source_node_id``.

        When ``data_type`` is given only edges of that type receive the
        payload. Returns the created packets (empty when the node has no
        outgoing edges — that is not an error: a terminal step simply has
        no downstream consumer).
        """

        packets: list[DataPacket] = []
        wanted = str(data_type or "")
        with self._lock:
            for edge in self._edges:
                if str(edge.get("source_node_id") or "") != str(source_node_id or ""):
                    continue
                edge_type = str(edge.get("data_type") or "")
                if wanted and edge_type != wanted:
                    continue
                packet = DataPacket(
                    edge_id=str(edge.get("id") or ""),
                    source_node_id=str(edge.get("source_node_id") or ""),
                    target_node_id=str(edge.get("target_node_id") or ""),
                    data_type=edge_type,
                    payload=payload,
                    item_count=_count_items(payload),
                )
                self._packets.append(packet)
                packets.append(packet)
        return packets

    def publish_by_capability(
        self,
        capability: str,
        payload: Any,
        *,
        data_type: str = "",
    ) -> list[DataPacket]:
        """Publish from every node carrying ``capability`` (executor-side API).

        ``route_executor`` thinks in capabilities, not node ids; this maps the
        finished capability back to its graph node(s) and publishes there.
        """

        packets: list[DataPacket] = []
        for node_id in self.node_ids_for_capability(capability):
            packets.extend(self.publish(node_id, payload, data_type=data_type))
        return packets

    # ----- consume --------------------------------------------------------

    def consume(
        self,
        target_node_id: str,
        *,
        data_type: str = "",
        mark: bool = True,
    ) -> list[Any]:
        """Deliver (once) every unconsumed payload parked for ``target_node_id``."""

        wanted = str(data_type or "")
        out: list[Any] = []
        now = time.time()
        with self._lock:
            for packet in self._packets:
                if packet.consumed:
                    continue
                if packet.target_node_id != str(target_node_id or ""):
                    continue
                if wanted and packet.data_type != wanted:
                    continue
                out.append(packet.payload)
                if mark:
                    packet.consumed = True
                    packet.consumed_at = now
        return out

    def consume_by_capability(
        self,
        capability: str,
        *,
        data_type: str = "",
        mark: bool = True,
    ) -> list[Any]:
        out: list[Any] = []
        for node_id in self.node_ids_for_capability(capability):
            out.extend(self.consume(node_id, data_type=data_type, mark=mark))
        return out

    def pending(self, target_node_id: str = "") -> list[dict[str, Any]]:
        """Payload-free metadata of undelivered packets (optionally per target)."""

        tid = str(target_node_id or "")
        with self._lock:
            return [
                p.meta()
                for p in self._packets
                if not p.consumed and (not tid or p.target_node_id == tid)
            ]

    # ----- evidence -------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Observable state for event_stream / run evidence. Never includes payloads."""

        with self._lock:
            packets = [p.meta() for p in self._packets]
        return {
            "version": VERSION,
            "edge_count": len(self._edges),
            "packet_count": len(packets),
            "pending_count": sum(1 for p in packets if not p["consumed"]),
            "packets": packets,
        }


# ---------------------------------------------------------------------------
# Per-run registry: two executor calls in one run share the same bus, so
# system A's publish in call 1 is consumable by system B's step in call 2.
# ---------------------------------------------------------------------------

_RUN_BUSES: dict[str, WorkflowDataBus] = {}
_REGISTRY_LOCK = threading.Lock()


def get_run_bus(
    run_id: str,
    *,
    workflow_graph: dict[str, Any] | None = None,
) -> WorkflowDataBus | None:
    """Fetch (or lazily create) the run's bus.

    A bus is only created when a ``workflow_graph`` is supplied — without
    edges there is nothing to relay, and returning ``None`` lets callers
    skip wiring instead of carrying a dead object.
    """

    rid = str(run_id or "").strip()
    if not rid:
        return None
    with _REGISTRY_LOCK:
        bus = _RUN_BUSES.get(rid)
        if bus is None and isinstance(workflow_graph, dict) and workflow_graph.get("data_edges"):
            bus = WorkflowDataBus(workflow_graph)
            _RUN_BUSES[rid] = bus
        return bus


def clear_run_bus(run_id: str) -> None:
    with _REGISTRY_LOCK:
        _RUN_BUSES.pop(str(run_id or "").strip(), None)


__all__ = [
    "VERSION",
    "DataPacket",
    "WorkflowDataBus",
    "get_run_bus",
    "clear_run_bus",
]

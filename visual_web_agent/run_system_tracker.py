"""Runtime cross-system tracking (E1b).

E1 taught ``workflow_graph`` to attribute *planned* nodes to systems and
``route_executor`` to bucket *planned* attempts per system. But VSpider's
live agent loop in ``main.py`` is **reactive**: a VLM looks at a
screenshot and decides the next action step-by-step — it does not march
through ``workflow_graph`` nodes in order. So "switch browser session
when plan-step N targets system X" does not map cleanly onto runtime.

What *does* map cleanly is the URL: every time the agent navigates, the
current page host tells us which declared system it is actually on. This
module turns that into an observable signal:

- :class:`RunSystemTracker` holds the planned systems (from
  ``capability_route["workflow_graph"]["systems"]``), indexes them by
  domain, and resolves any URL to the matching system.
- ``observe(url, step=...)`` records a :class:`SystemTransition` whenever
  the resolved system changes, so the run trace shows real cross-system
  hops.
- ``summary()`` exposes a stable snapshot for the event stream / run
  evidence.

It is **pure logic** (no Playwright, no network) so it is fully
unit-testable and safe to call from the hot loop behind a ``try/except``.
True multi-context session switching (holding system A's page alive while
working on B) remains a future slice (E1c) built on
``browser_session_pool``; this module is the observability foundation
that records when such switches would happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


VERSION = "run_system_tracker.v1"


def _host_of(url: str) -> str:
    """Extract a bare lowercase host from a URL (no port, no userinfo)."""

    try:
        netloc = urlparse(str(url or "")).netloc
    except Exception:
        return ""
    if not netloc:
        return ""
    host = netloc.split("@")[-1].split(":", 1)[0]
    return host.strip().lower()


def _domain_match_score(host: str, domain: str) -> int:
    """Return a specificity score for ``host`` against ``domain``.

    0  -> no match
    1  -> suffix match (host endswith .domain, or domain endswith .host)
    2  -> exact match

    Higher is more specific; callers pick the highest-scoring system.
    """

    host = (host or "").strip().lower()
    domain = (domain or "").strip().lower()
    if not host or not domain:
        return 0
    if host == domain:
        return 2
    if host.endswith("." + domain) or domain.endswith("." + host):
        return 1
    return 0


@dataclass
class SystemTransition:
    """One recorded hop from one system to another during a run."""

    step: int
    url: str
    from_system_id: str
    from_system_name: str
    to_system_id: str
    to_system_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": int(self.step),
            "url": self.url,
            "from_system_id": self.from_system_id,
            "from_system_name": self.from_system_name,
            "to_system_id": self.to_system_id,
            "to_system_name": self.to_system_name,
        }


@dataclass
class RunSystemTracker:
    """Resolve runtime URLs to planned systems and record transitions."""

    systems: list[dict[str, Any]] = field(default_factory=list)
    current_system_id: str = ""
    current_system_name: str = ""
    transitions: list[SystemTransition] = field(default_factory=list)
    visited_system_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Index web systems by domain for fast, specificity-ranked lookup.
        self._domain_index: list[tuple[str, dict[str, Any]]] = []
        for system in self.systems or []:
            if not isinstance(system, dict):
                continue
            domain = str(system.get("domain") or "").strip().lower()
            if domain:
                self._domain_index.append((domain, system))

    # ----- resolution -----------------------------------------------------

    def resolve_system_for_url(self, url: str) -> dict[str, Any] | None:
        """Return the most specific system whose domain matches ``url``.

        Returns ``None`` when no host is parseable or no declared system
        matches (e.g. the agent wandered to an unplanned domain).
        """

        host = _host_of(url)
        if not host:
            return None
        best: dict[str, Any] | None = None
        best_score = 0
        for domain, system in self._domain_index:
            score = _domain_match_score(host, domain)
            if score > best_score:
                best_score = score
                best = system
        return best

    # ----- observation ----------------------------------------------------

    def observe(self, url: str, *, step: int = 0) -> SystemTransition | None:
        """Record a transition when the resolved system changes.

        Returns the :class:`SystemTransition` when a *change* happened,
        otherwise ``None`` (same system, or URL resolves to no known
        system, in which case we keep the previous system sticky).
        """

        system = self.resolve_system_for_url(url)
        if system is None:
            return None
        system_id = str(system.get("id") or "")
        system_name = str(system.get("name") or system.get("domain") or system_id)
        if not system_id:
            return None

        if system_id not in self.visited_system_ids:
            self.visited_system_ids.append(system_id)

        if system_id == self.current_system_id:
            return None

        transition = SystemTransition(
            step=int(step),
            url=str(url or ""),
            from_system_id=self.current_system_id,
            from_system_name=self.current_system_name,
            to_system_id=system_id,
            to_system_name=system_name,
        )
        self.current_system_id = system_id
        self.current_system_name = system_name
        self.transitions.append(transition)
        return transition

    # ----- introspection --------------------------------------------------

    @property
    def cross_system_active(self) -> bool:
        return len(self.visited_system_ids) > 1

    def summary(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "system_count": len(self.systems or []),
            "systems": [
                {
                    "id": str(s.get("id") or ""),
                    "name": str(s.get("name") or s.get("domain") or s.get("id") or ""),
                    "domain": str(s.get("domain") or ""),
                    "type": str(s.get("type") or ""),
                }
                for s in (self.systems or [])
                if isinstance(s, dict)
            ],
            "visited_system_ids": list(self.visited_system_ids),
            "visited_count": len(self.visited_system_ids),
            "transition_count": len(self.transitions),
            "transitions": [t.to_dict() for t in self.transitions],
            "current_system_id": self.current_system_id,
            "current_system_name": self.current_system_name,
            "cross_system": self.cross_system_active,
        }


def build_run_system_tracker(capability_route: dict[str, Any] | None) -> RunSystemTracker:
    """Build a tracker from a capability route's workflow_graph systems.

    Tolerant of missing / malformed input: returns an inert tracker
    (``observe`` always returns ``None``) when no systems are present, so
    the caller can wire it unconditionally behind a ``try/except``.
    """

    systems: list[dict[str, Any]] = []
    if isinstance(capability_route, dict):
        workflow_graph = capability_route.get("workflow_graph")
        if isinstance(workflow_graph, dict):
            raw_systems = workflow_graph.get("systems")
            if isinstance(raw_systems, list):
                systems = [s for s in raw_systems if isinstance(s, dict)]
    return RunSystemTracker(systems=systems)


__all__ = [
    "VERSION",
    "SystemTransition",
    "RunSystemTracker",
    "build_run_system_tracker",
]

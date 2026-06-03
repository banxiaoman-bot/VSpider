"""Cross-system session routing (E1c-1).

``run_system_tracker`` (E1b) resolves the *current URL* to a planned system
and records transitions, but it is pure observability -- it never switches
the browser context. ``browser_session_pool`` (E1) can hold one
``BrowserSession`` per ``(run_id, system_id, auth_profile)`` triple, but it
is unwired infrastructure: nothing decides *which* ``auth_profile`` a system
should use, nor isolates each system's storage_state.

:class:`SessionRouter` fills exactly that gap and nothing more. It is the
pure coordination layer between the planned systems (from
``capability_route["workflow_graph"]["systems"]``) and the pool:

- resolve, per ``system_id``, the ``auth_profile`` to acquire with
  (an explicit per-call override > the system's declared profile > ``auto``),
- carve a merged storage_state down to the subset that belongs to a
  system's domain (reusing ``auth_harvester``'s host filter so the rule
  stays identical to ``apply_storage_state_to_context``),
- delegate the actual ``BrowserSession`` acquire / release to the pool.

The auth-resolution and storage_state-subset logic are pure (no browser, no
IO). Acquiring a session does create a ``BrowserEnv`` via the pool, so the
pool is injected to keep the unit under test free of Playwright. Wiring this
router into ``route_executor`` (E1c-2) and the reactive agent loop (E1c-3)
are deliberately separate slices; this module touches neither.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .browser_session_pool import BrowserSession, BrowserSessionPool


VERSION = "session_router.v1"


def _clean_profile(value: Any) -> str:
    profile = str(value or "").strip()
    return profile or "auto"


@dataclass
class SystemAuthPlan:
    """Per-system auth-profile + domain lookup built from planned systems.

    ``systems`` is the ``workflow_graph["systems"]`` list; each entry is a
    dict with at least ``id`` and optionally ``domain`` / ``auth_profile``
    (see :class:`visual_web_agent.workflow_graph.WorkflowSystem`).
    """

    systems: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_id: dict[str, dict[str, Any]] = {}
        for system in self.systems or []:
            if not isinstance(system, dict):
                continue
            system_id = str(system.get("id") or "").strip()
            if system_id and system_id not in self._by_id:
                self._by_id[system_id] = system

    def auth_profile_for(self, system_id: str) -> str:
        system = self._by_id.get(str(system_id or "").strip())
        if system is None:
            return "auto"
        return _clean_profile(system.get("auth_profile"))

    def domain_for(self, system_id: str) -> str:
        system = self._by_id.get(str(system_id or "").strip())
        if system is None:
            return ""
        return str(system.get("domain") or "").strip().lower()

    def known_system_ids(self) -> list[str]:
        return list(self._by_id.keys())


@dataclass
class SessionRouter:
    """Route ``(run_id, system_id)`` to a pooled :class:`BrowserSession`.

    ``pool`` is injected so tests can pass a :class:`BrowserSessionPool` with
    a stub ``env_factory``. When ``pool`` is ``None`` the module-level
    singleton in ``browser_session_pool`` is used, matching how the rest of
    the runtime reaches the default pool.
    """

    run_id: str
    plan: SystemAuthPlan = field(default_factory=SystemAuthPlan)
    pool: BrowserSessionPool | None = None

    # ----- resolution (pure) ---------------------------------------------

    def resolved_auth_profile(self, system_id: str, override: str = "auto") -> str:
        """An explicit override wins, else the plan's profile, else ``auto``."""

        chosen = _clean_profile(override)
        if chosen != "auto":
            return chosen
        return self.plan.auth_profile_for(system_id)

    def storage_state_for_system(
        self, system_id: str, full_state: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Subset of ``full_state`` whose cookies / origins match the system.

        Returns the empty state when the system declares no domain (or is
        unknown), so a caller never accidentally applies another system's
        cookies to it.
        """

        domain = self.plan.domain_for(system_id)
        if not domain:
            return {"cookies": [], "origins": []}
        from .auth_harvester import _filter_state_to_host

        filtered, _, _ = _filter_state_to_host(full_state or {}, domain)
        return filtered

    # ----- pool delegation ------------------------------------------------

    def acquire(self, system_id: str, *, auth_profile: str = "auto") -> BrowserSession:
        profile = self.resolved_auth_profile(system_id, auth_profile)
        if self.pool is not None:
            return self.pool.acquire_session(
                run_id=self.run_id, system_id=system_id, auth_profile=profile
            )
        from .browser_session_pool import acquire_session as _acquire_session

        return _acquire_session(
            run_id=self.run_id, system_id=system_id, auth_profile=profile
        )

    def get_active(self, system_id: str, *, auth_profile: str = "auto") -> BrowserSession | None:
        profile = self.resolved_auth_profile(system_id, auth_profile)
        if self.pool is not None:
            return self.pool.get_active_session(
                self.run_id, system_id, auth_profile=profile
            )
        from .browser_session_pool import get_active_session as _get_active_session

        return _get_active_session(self.run_id, system_id, auth_profile=profile)

    def plan_switch(
        self,
        *,
        to_system_id: str,
        from_system_id: str = "",
        to_system_name: str = "",
    ) -> dict[str, Any]:
        """Describe the session switch a cross-system hop implies (pure).

        Returns a directive dict the reactive loop can record as evidence
        (``session_switch`` event) and, in a later slice, act on. ``should_switch``
        is False for a no-op hop (same system, or a blank target) so callers can
        guard cheaply. No pool / browser side effects happen here.
        """

        target = str(to_system_id or "").strip()
        should_switch = bool(target) and target != str(from_system_id or "").strip()
        return {
            "should_switch": should_switch,
            "run_id": self.run_id,
            "from_system_id": str(from_system_id or ""),
            "to_system_id": target,
            "to_system_name": str(to_system_name or "") or target,
            "auth_profile": self.resolved_auth_profile(target) if should_switch else "auto",
            "domain": self.plan.domain_for(target) if should_switch else "",
        }

    async def release_all(self, *, error: str = "") -> list[str]:
        if self.pool is not None:
            return await self.pool.release_run(self.run_id, error=error)
        from .browser_session_pool import release_run_sessions as _release_run_sessions

        return await _release_run_sessions(self.run_id, error=error)


def build_session_router(
    run_id: str,
    capability_route: dict[str, Any] | None,
    *,
    pool: BrowserSessionPool | None = None,
) -> SessionRouter:
    """Build a router from a capability route's ``workflow_graph.systems``.

    Tolerant of missing / malformed input: an absent or empty systems list
    yields an inert plan (every ``auth_profile`` resolves to ``auto``), so
    the caller can construct the router unconditionally behind a
    ``try/except`` and single-system runs see no behaviour change.
    """

    systems: list[dict[str, Any]] = []
    if isinstance(capability_route, dict):
        workflow_graph = capability_route.get("workflow_graph")
        if isinstance(workflow_graph, dict):
            raw_systems = workflow_graph.get("systems")
            if isinstance(raw_systems, list):
                systems = [s for s in raw_systems if isinstance(s, dict)]
    return SessionRouter(run_id=run_id, plan=SystemAuthPlan(systems=systems), pool=pool)


__all__ = [
    "VERSION",
    "SystemAuthPlan",
    "SessionRouter",
    "build_session_router",
]

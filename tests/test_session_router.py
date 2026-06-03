"""Tests for ``visual_web_agent.session_router`` (E1c-1).

``SessionRouter`` is the pure coordination layer that sits on top of
``browser_session_pool``: it resolves, per planned system, which
``auth_profile`` to use and which subset of a merged storage_state belongs
to that system, then delegates the actual ``BrowserSession`` acquisition to
the pool. The auth-resolution and storage_state-subset logic are fully pure
(no browser, no IO); the acquire/release paths are exercised against a real
``BrowserSessionPool`` parameterised with a stub ``env_factory``.
"""

from __future__ import annotations

import asyncio

import pytest

from visual_web_agent.session_router import (
    SessionRouter,
    SystemAuthPlan,
    build_session_router,
)
from visual_web_agent.browser_session_pool import BrowserSessionPool


_SYSTEMS = [
    {"id": "system_1", "name": "Alpha", "domain": "alpha.com", "type": "web", "auth_profile": "alpha_login"},
    {"id": "system_2", "name": "Beta", "domain": "beta.com", "type": "web", "auth_profile": "auto"},
]


_FULL_STATE = {
    "cookies": [
        {"name": "a", "domain": ".alpha.com"},
        {"name": "b", "domain": "beta.com"},
    ],
    "origins": [
        {"origin": "https://alpha.com"},
        {"origin": "https://beta.com"},
    ],
}


class _StubBrowserEnv:
    async def close(self) -> None:
        return None


def _stub_factory() -> _StubBrowserEnv:
    return _StubBrowserEnv()


def _pool() -> BrowserSessionPool:
    return BrowserSessionPool(env_factory=_stub_factory)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# SystemAuthPlan: pure auth-profile + domain resolution
# ---------------------------------------------------------------------------


class TestSystemAuthPlan:
    def test_auth_profile_for_declared_system(self) -> None:
        plan = SystemAuthPlan(systems=_SYSTEMS)
        assert plan.auth_profile_for("system_1") == "alpha_login"

    def test_auth_profile_defaults_auto_for_blank_declaration(self) -> None:
        plan = SystemAuthPlan(systems=_SYSTEMS)
        assert plan.auth_profile_for("system_2") == "auto"

    def test_auth_profile_defaults_auto_for_unknown_system(self) -> None:
        plan = SystemAuthPlan(systems=_SYSTEMS)
        assert plan.auth_profile_for("system_x") == "auto"

    def test_domain_for_system(self) -> None:
        plan = SystemAuthPlan(systems=_SYSTEMS)
        assert plan.domain_for("system_1") == "alpha.com"
        assert plan.domain_for("system_x") == ""

    def test_known_system_ids(self) -> None:
        plan = SystemAuthPlan(systems=_SYSTEMS)
        assert plan.known_system_ids() == ["system_1", "system_2"]


# ---------------------------------------------------------------------------
# SessionRouter: auth resolution + storage_state subset
# ---------------------------------------------------------------------------


class TestSessionRouterResolution:
    def test_resolved_auth_profile_uses_plan(self) -> None:
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=_pool())
        assert router.resolved_auth_profile("system_1") == "alpha_login"

    def test_resolved_auth_profile_override_wins(self) -> None:
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=_pool())
        assert router.resolved_auth_profile("system_1", "manual") == "manual"

    def test_resolved_auth_profile_auto_override_falls_back_to_plan(self) -> None:
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=_pool())
        assert router.resolved_auth_profile("system_1", "auto") == "alpha_login"

    def test_storage_state_filtered_to_system_domain(self) -> None:
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=_pool())
        subset = router.storage_state_for_system("system_1", _FULL_STATE)
        names = [c["name"] for c in subset["cookies"]]
        assert names == ["a"]
        assert len(subset["origins"]) == 1

    def test_storage_state_empty_for_unknown_system(self) -> None:
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=_pool())
        subset = router.storage_state_for_system("system_x", _FULL_STATE)
        assert subset == {"cookies": [], "origins": []}


# ---------------------------------------------------------------------------
# SessionRouter: pool delegation
# ---------------------------------------------------------------------------


class TestSessionRouterAcquire:
    def test_acquire_delegates_with_resolved_profile(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        session = router.acquire("system_1")
        assert session.run_id == "r1"
        assert session.system_id == "system_1"
        assert session.auth_profile == "alpha_login"

    def test_acquire_dedupes_same_triple(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        first = router.acquire("system_1")
        second = router.acquire("system_1")
        assert first.session_id == second.session_id
        assert pool.total_acquired == 1

    def test_acquire_override_profile(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        session = router.acquire("system_2", auth_profile="manual")
        assert session.auth_profile == "manual"

    def test_release_all_releases_run_sessions(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        router.acquire("system_1")
        router.acquire("system_2")
        released = _run(router.release_all())
        assert len(released) == 2
        assert pool.status()["active_count"] == 0


# ---------------------------------------------------------------------------
# build_session_router: tolerant construction from a capability route
# ---------------------------------------------------------------------------


class TestSessionRouterPlanSwitch:
    def test_plan_switch_describes_target_session(self) -> None:
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=_pool())
        directive = router.plan_switch(
            to_system_id="system_1", from_system_id="system_2", to_system_name="Alpha"
        )
        assert directive["should_switch"] is True
        assert directive["run_id"] == "r1"
        assert directive["from_system_id"] == "system_2"
        assert directive["to_system_id"] == "system_1"
        assert directive["to_system_name"] == "Alpha"
        assert directive["auth_profile"] == "alpha_login"
        assert directive["domain"] == "alpha.com"

    def test_plan_switch_same_system_is_noop(self) -> None:
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=_pool())
        directive = router.plan_switch(to_system_id="system_1", from_system_id="system_1")
        assert directive["should_switch"] is False

    def test_plan_switch_blank_target_is_noop(self) -> None:
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=_pool())
        directive = router.plan_switch(to_system_id="", from_system_id="system_1")
        assert directive["should_switch"] is False

    def test_plan_switch_to_name_defaults_to_id(self) -> None:
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=_pool())
        directive = router.plan_switch(to_system_id="system_2", from_system_id="system_1")
        assert directive["to_system_name"] == "system_2"


class TestSessionRouterAcquireForSwitch:
    def test_acquire_for_switch_pools_target_and_stages_state(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        directive = router.acquire_for_switch(
            to_system_id="system_1",
            from_system_id="system_2",
            to_system_name="Alpha",
            full_state=_FULL_STATE,
        )
        assert directive["should_switch"] is True
        assert directive["staged"] is True
        assert directive["session_id"]
        assert directive["cookie_count"] == 1  # only the .alpha.com cookie survives the subset
        assert directive["auth_profile"] == "alpha_login"
        assert pool.total_acquired == 1

    def test_acquire_for_switch_noop_acquires_nothing(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        directive = router.acquire_for_switch(to_system_id="system_1", from_system_id="system_1")
        assert directive["should_switch"] is False
        assert directive["staged"] is False
        assert directive["session_id"] == ""
        assert directive["cookie_count"] == 0
        assert pool.total_acquired == 0

    def test_acquire_for_switch_dedupes_repeat_hop(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        first = router.acquire_for_switch(to_system_id="system_1", from_system_id="system_2")
        second = router.acquire_for_switch(to_system_id="system_1", from_system_id="system_2")
        assert first["session_id"] == second["session_id"]
        assert pool.total_acquired == 1

    def test_acquire_for_switch_without_state_stages_zero_cookies(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        directive = router.acquire_for_switch(to_system_id="system_1", from_system_id="system_2")
        assert directive["staged"] is True
        assert directive["session_id"]
        assert directive["cookie_count"] == 0

    def test_acquire_for_switch_directive_carries_evidence_contract(self) -> None:
        # Locks the directive keys the reactive loop emits as session_switch /
        # session_switch_verified evidence (main.py hot-loop hook). A rename of
        # any of these silently breaks that wiring, so guard the full key set.
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        directive = router.acquire_for_switch(
            to_system_id="system_1", from_system_id="system_2", full_state=_FULL_STATE
        )
        for key in (
            "should_switch",
            "run_id",
            "from_system_id",
            "to_system_id",
            "to_system_name",
            "auth_profile",
            "domain",
            "session_id",
            "staged",
            "cookie_count",
        ):
            assert key in directive, f"missing evidence key: {key}"


class TestSessionRouterConfirmActive:
    def test_confirm_active_true_after_acquire(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        directive = router.acquire_for_switch(to_system_id="system_1", from_system_id="system_2")
        assert router.confirm_active("system_1", directive["session_id"]) is True

    def test_confirm_active_false_when_never_acquired(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        assert router.confirm_active("system_1", "session_phantom") is False

    def test_confirm_active_false_on_session_id_mismatch(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        router.acquire_for_switch(to_system_id="system_1", from_system_id="system_2")
        assert router.confirm_active("system_1", "wrong_id") is False

    def test_confirm_active_false_on_blank_id(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        router.acquire_for_switch(to_system_id="system_1", from_system_id="system_2")
        assert router.confirm_active("system_1", "") is False


class TestSessionRouterReleaseAfterSwitch:
    def test_release_all_frees_session_acquired_for_switch(self) -> None:
        pool = _pool()
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        directive = router.acquire_for_switch(to_system_id="system_1", from_system_id="system_2")
        released = _run(router.release_all())
        assert directive["session_id"] in released
        assert router.confirm_active("system_1", directive["session_id"]) is False
        assert pool.total_released == 1


class TestBuildSessionRouter:
    def test_build_from_capability_route(self) -> None:
        route = {"workflow_graph": {"systems": _SYSTEMS}}
        router = build_session_router("r1", route, pool=_pool())
        assert router.plan.known_system_ids() == ["system_1", "system_2"]
        assert router.resolved_auth_profile("system_1") == "alpha_login"

    def test_build_inert_when_no_systems(self) -> None:
        router = build_session_router("r1", {}, pool=_pool())
        assert router.plan.known_system_ids() == []
        assert router.resolved_auth_profile("anything") == "auto"

    def test_build_tolerates_none_route(self) -> None:
        router = build_session_router("r1", None, pool=_pool())
        assert router.plan.known_system_ids() == []

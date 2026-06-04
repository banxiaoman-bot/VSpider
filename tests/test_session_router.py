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


class TestSessionRouterPlanGotoInterception:
    """Pure pre-navigation directive (A1, Path-2): resolve a goto target URL to
    a planned system and decide whether the reactive loop should intercept the
    navigation as a cross-system hop *before* page.goto runs (so the current
    system's page is preserved). Pure -- no pool / browser -- so it is
    stub-frame regressable per workflow rule §四. Only a *known* target system
    that differs from a *known* current system intercepts; the first navigation
    (blank current), same-system, and unknown-domain gotos fall through to a
    normal page.goto."""

    def _router(self) -> SessionRouter:
        return SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=_pool())

    def test_system_for_url_resolves_planned_domain(self) -> None:
        router = self._router()
        assert router.system_for_url("https://alpha.com/dashboard") == "system_1"
        assert router.system_for_url("https://beta.com/list?x=1") == "system_2"

    def test_system_for_url_matches_subdomain(self) -> None:
        router = self._router()
        assert router.system_for_url("https://app.beta.com/x") == "system_2"

    def test_system_for_url_blank_for_unknown_or_empty(self) -> None:
        router = self._router()
        assert router.system_for_url("https://example.org/x") == ""
        assert router.system_for_url("") == ""

    def test_cross_system_goto_should_intercept(self) -> None:
        router = self._router()
        directive = router.plan_goto_interception(
            "https://beta.com/page", current_system_id="system_1"
        )
        assert directive["should_intercept"] is True
        assert directive["to_system_id"] == "system_2"
        assert directive["from_system_id"] == "system_1"
        assert directive["target_url"] == "https://beta.com/page"
        assert directive["domain"] == "beta.com"
        assert directive["auth_profile"] == "auto"  # system_2 declares auto

    def test_same_system_goto_not_intercepted(self) -> None:
        router = self._router()
        directive = router.plan_goto_interception(
            "https://alpha.com/other", current_system_id="system_1"
        )
        assert directive["should_intercept"] is False
        assert directive["to_system_id"] == ""

    def test_unknown_domain_not_intercepted(self) -> None:
        router = self._router()
        directive = router.plan_goto_interception(
            "https://example.org/x", current_system_id="system_1"
        )
        assert directive["should_intercept"] is False
        assert directive["to_system_id"] == ""

    def test_blank_current_system_not_intercepted(self) -> None:
        # The first navigation establishes the home system; never intercept it.
        router = self._router()
        directive = router.plan_goto_interception("https://beta.com", current_system_id="")
        assert directive["should_intercept"] is False

    def test_directive_carries_interception_contract(self) -> None:
        # Lock the keys the GotoHandler + hot-loop consume. A rename silently
        # breaks the pre-nav interception wiring, so guard the full key set.
        router = self._router()
        directive = router.plan_goto_interception("https://beta.com", current_system_id="system_1")
        for key in (
            "should_intercept",
            "run_id",
            "target_url",
            "from_system_id",
            "to_system_id",
            "to_system_name",
            "auth_profile",
            "domain",
        ):
            assert key in directive, f"missing interception key: {key}"


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


class _StubContext:
    """Minimal Playwright-context stand-in: records the two calls
    ``auth_manager.apply_storage_state_to_context`` makes."""

    def __init__(self) -> None:
        self.added_cookies: list[dict] = []
        self.init_scripts: list[str] = []

    async def add_cookies(self, cookies) -> None:
        self.added_cookies.extend(cookies)

    async def add_init_script(self, script) -> None:
        self.init_scripts.append(script)


class _StubLaunchBrowserEnv:
    """Stub BrowserEnv whose ``start`` records the isolated-profile override
    and simulates landing on a URL; ``_context`` drives the real apply code."""

    def __init__(self, *, landed_url: str = "https://alpha.com/home") -> None:
        self.started: list[dict] = []
        self._context = _StubContext()
        self.current_url = ""
        self._landed_url = landed_url

    async def start(self, url: str, *, user_data_dir_override=None) -> None:
        self.started.append({"url": url, "user_data_dir_override": user_data_dir_override})
        self.current_url = self._landed_url

    async def close(self) -> None:
        return None


class TestSessionRouterLaunchForSwitch:
    def test_launch_starts_isolated_profile_and_applies_subset(self) -> None:
        pool = BrowserSessionPool(env_factory=_StubLaunchBrowserEnv)
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        directive = router.acquire_for_switch(
            to_system_id="system_1", from_system_id="system_2", full_state=_FULL_STATE
        )
        session = router.get_active("system_1")
        result = _run(
            router.launch_for_switch(
                session,
                "https://alpha.com/home",
                user_data_dir="/tmp/sys_alpha",
                full_state=_FULL_STATE,
            )
        )
        assert result["launched"] is True
        assert result["session_id"] == directive["session_id"]
        assert result["applied_cookies"] == 1  # only the .alpha.com cookie in the subset
        assert result["final_url"] == "https://alpha.com/home"
        assert session.browser.started == [
            {"url": "https://alpha.com/home", "user_data_dir_override": "/tmp/sys_alpha"}
        ]
        assert len(session.browser._context.added_cookies) == 1

    def test_launch_without_state_applies_nothing(self) -> None:
        pool = BrowserSessionPool(env_factory=_StubLaunchBrowserEnv)
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        router.acquire_for_switch(to_system_id="system_1", from_system_id="system_2")
        session = router.get_active("system_1")
        result = _run(router.launch_for_switch(session, "https://alpha.com", full_state=None))
        assert result["launched"] is True
        assert result["applied_cookies"] == 0
        assert session.browser._context.added_cookies == []

    def test_launch_blank_user_data_dir_passes_none(self) -> None:
        pool = BrowserSessionPool(env_factory=_StubLaunchBrowserEnv)
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        router.acquire_for_switch(to_system_id="system_1", from_system_id="system_2")
        session = router.get_active("system_1")
        _run(router.launch_for_switch(session, "https://alpha.com", user_data_dir="", full_state=_FULL_STATE))
        assert session.browser.started[0]["user_data_dir_override"] is None


class TestSessionRouterActivateSwitch:
    def test_no_switch_returns_none(self) -> None:
        pool = BrowserSessionPool(env_factory=_StubLaunchBrowserEnv)
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        directive = router.acquire_for_switch(to_system_id="system_1", from_system_id="system_1")
        got = _run(router.activate_switch(directive, url="https://x", home_browser="HOME"))
        assert got is None

    def test_forward_hop_launches_once_and_returns_session_browser(self) -> None:
        pool = BrowserSessionPool(env_factory=_StubLaunchBrowserEnv)
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        directive = router.acquire_for_switch(
            to_system_id="system_1", from_system_id="system_2", full_state=_FULL_STATE
        )
        session = router.get_active("system_1")
        got = _run(
            router.activate_switch(
                directive,
                url="https://alpha.com",
                user_data_dir_base="/profiles",
                full_state=_FULL_STATE,
            )
        )
        assert got is session.browser
        assert session.browser.started[0]["user_data_dir_override"] == "/profiles/sys_system_1"
        # revisiting the same system reuses the launched handle, never re-starting
        got2 = _run(
            router.activate_switch(
                directive, url="https://alpha.com", user_data_dir_base="/profiles", full_state=_FULL_STATE
            )
        )
        assert got2 is session.browser
        assert len(session.browser.started) == 1

    def test_hop_back_to_home_returns_home_browser_without_launch(self) -> None:
        pool = BrowserSessionPool(env_factory=_StubLaunchBrowserEnv)
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        directive = router.acquire_for_switch(to_system_id="system_2", from_system_id="system_1")
        got = _run(
            router.activate_switch(
                directive, url="https://beta.com", home_system_id="system_2", home_browser="HOME_LEASE"
            )
        )
        assert got == "HOME_LEASE"
        session2 = router.get_active("system_2")
        assert session2.browser.started == []  # a home hop never launches a pool session

    def test_unknown_target_returns_none(self) -> None:
        pool = BrowserSessionPool(env_factory=_StubLaunchBrowserEnv)
        router = SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool)
        directive = {"should_switch": True, "to_system_id": "system_9", "run_id": "r1"}
        got = _run(router.activate_switch(directive, url="https://x"))
        assert got is None


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

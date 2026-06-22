"""XSYS-E2E-1: dual-login cross-system relay, end to end (offline).

The unit suites already pin SessionRouter / BrowserSessionPool /
route_executor in isolation. This module drives one continuous scenario -
a run that starts in system_1 (alpha.com, logged in as alpha_admin), hops
to system_2 (beta.com, logged in as beta_ops), then returns home - through
the REAL router + pool + storage-apply code (only BrowserEnv/Playwright is
stubbed), asserting the mission-rule invariants:

  - each system's BrowserSession gets ONLY its own login state (cookie and
    origin localStorage subsets are mutually exclusive; foreign domains
    never leak),
  - both sessions coexist under one run_id and are released together,
  - the forward hop launches an isolated profile and applies the subset to
    the new context; the home hop returns the home browser lease without
    launching anything,
  - the evidence chain (directive keys, confirm_active) stays verifiable.

The same invariants were validated against real Playwright contexts with a
live-browser probe (storage_state readback + renderer-level cookie /
localStorage isolation) before commit.
"""

from __future__ import annotations

import asyncio

from visual_web_agent.browser_session_pool import BrowserSessionPool
from visual_web_agent.session_router import SessionRouter, SystemAuthPlan


_SYSTEMS = [
    {"id": "system_1", "name": "Alpha ERP", "domain": "alpha.example", "type": "web", "auth_profile": "alpha_admin"},
    {"id": "system_2", "name": "Beta CRM", "domain": "beta.example", "type": "web", "auth_profile": "beta_ops"},
]

_DUAL_LOGIN_STATE = {
    "cookies": [
        {"name": "session_a", "value": "AAA", "domain": ".alpha.example", "path": "/"},
        {"name": "session_b", "value": "BBB", "domain": "beta.example", "path": "/"},
        {"name": "tracker", "value": "junk", "domain": ".other.example", "path": "/"},
    ],
    "origins": [
        {"origin": "https://alpha.example", "localStorage": [{"name": "token", "value": "tokA"}]},
        {"origin": "https://beta.example", "localStorage": [{"name": "token", "value": "tokB"}]},
        {"origin": "https://other.example", "localStorage": [{"name": "token", "value": "tokX"}]},
    ],
}


class _StubContext:
    def __init__(self) -> None:
        self.added_cookies: list[dict] = []
        self.init_scripts: list[str] = []

    async def add_cookies(self, cookies) -> None:
        self.added_cookies.extend(cookies)

    async def add_init_script(self, script) -> None:
        self.init_scripts.append(script)


class _StubBrowserEnv:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self._context = _StubContext()
        self.current_url = ""

    async def start(self, url: str, *, user_data_dir_override=None) -> None:
        self.started.append({"url": url, "user_data_dir_override": user_data_dir_override})
        self.current_url = url

    async def close(self) -> None:
        return None


def _router() -> tuple[SessionRouter, BrowserSessionPool]:
    pool = BrowserSessionPool(env_factory=_StubBrowserEnv)
    router = SessionRouter(
        run_id="xsys_e2e", plan=SystemAuthPlan(systems=_SYSTEMS), pool=pool
    )
    return router, pool


def _run(coro):
    return asyncio.run(coro)


class TestDualLoginSubsetsAreMutuallyExclusive:
    def test_each_system_sees_only_its_own_state(self) -> None:
        router, _ = _router()
        sub_a = router.storage_state_for_system("system_1", _DUAL_LOGIN_STATE)
        sub_b = router.storage_state_for_system("system_2", _DUAL_LOGIN_STATE)

        assert [c["name"] for c in sub_a["cookies"]] == ["session_a"]
        assert [c["name"] for c in sub_b["cookies"]] == ["session_b"]
        assert [o["origin"] for o in sub_a["origins"]] == ["https://alpha.example"]
        assert [o["origin"] for o in sub_b["origins"]] == ["https://beta.example"]

    def test_foreign_domains_never_leak(self) -> None:
        router, _ = _router()
        for system_id in ("system_1", "system_2"):
            subset = router.storage_state_for_system(system_id, _DUAL_LOGIN_STATE)
            names = {c["name"] for c in subset["cookies"]}
            origins = {o["origin"] for o in subset["origins"]}
            assert "tracker" not in names
            assert "https://other.example" not in origins

    def test_unknown_system_gets_the_empty_state(self) -> None:
        router, _ = _router()
        assert router.storage_state_for_system("system_9", _DUAL_LOGIN_STATE) == {
            "cookies": [],
            "origins": [],
        }


class TestDualLoginRelayEndToEnd:
    """system_1 (home) -> system_2 -> back home, one continuous run."""

    def test_forward_hop_launches_and_applies_only_beta_state(self) -> None:
        router, pool = _router()
        directive = router.acquire_for_switch(
            to_system_id="system_2",
            from_system_id="system_1",
            to_system_name="Beta CRM",
            full_state=_DUAL_LOGIN_STATE,
        )
        assert directive["should_switch"] is True
        assert directive["auth_profile"] == "beta_ops"
        assert directive["cookie_count"] == 1

        browser = _run(
            router.activate_switch(
                directive,
                url="https://beta.example/orders",
                user_data_dir_base="/profiles",
                full_state=_DUAL_LOGIN_STATE,
            )
        )
        session = router.get_active("system_2")
        assert browser is session.browser
        assert len(session.browser.started) == 1
        applied = session.browser._context.added_cookies
        assert [c["name"] for c in applied] == ["session_b"], (
            "the beta context must receive ONLY the beta login cookie"
        )

    def test_home_hop_returns_home_lease_without_launching(self) -> None:
        router, pool = _router()
        fwd = router.acquire_for_switch(
            to_system_id="system_2", from_system_id="system_1",
            full_state=_DUAL_LOGIN_STATE,
        )
        _run(router.activate_switch(
            fwd, url="https://beta.example", user_data_dir_base="/profiles",
            full_state=_DUAL_LOGIN_STATE,
        ))

        back = router.acquire_for_switch(
            to_system_id="system_1", from_system_id="system_2",
            full_state=_DUAL_LOGIN_STATE,
        )
        got = _run(router.activate_switch(
            back,
            url="https://alpha.example/import",
            home_system_id="system_1",
            home_browser="HOME_LEASE",
            full_state=_DUAL_LOGIN_STATE,
        ))
        assert got == "HOME_LEASE"
        home_session = router.get_active("system_1")
        assert home_session.browser.started == [], (
            "hopping home must reuse the home lease, never launch a pool session"
        )

    def test_both_sessions_coexist_then_release_together(self) -> None:
        router, pool = _router()
        fwd = router.acquire_for_switch(
            to_system_id="system_2", from_system_id="system_1",
            full_state=_DUAL_LOGIN_STATE,
        )
        router.acquire_for_switch(
            to_system_id="system_1", from_system_id="system_2",
            full_state=_DUAL_LOGIN_STATE,
        )
        status = pool.status()
        assert status["active_count"] == 2, "dual sessions must coexist in one run"
        assert router.confirm_active("system_2", fwd["session_id"]) is True

        released = _run(router.release_all())
        assert len(released) == 2
        assert pool.status()["active_count"] == 0

    def test_repeat_hops_reuse_the_same_session(self) -> None:
        router, pool = _router()
        first = router.acquire_for_switch(
            to_system_id="system_2", from_system_id="system_1",
            full_state=_DUAL_LOGIN_STATE,
        )
        second = router.acquire_for_switch(
            to_system_id="system_2", from_system_id="system_1",
            full_state=_DUAL_LOGIN_STATE,
        )
        assert first["session_id"] == second["session_id"]
        assert pool.total_acquired == 1, "A->B->A->B must not stack contexts"

"""S10: flag-on dual-system E2E — contract -> graph -> sessions -> data relay.

XSYS-E2E-1 (``test_xsys_dual_login_e2e``) pins router + pool + storage
isolation in isolation. This module drives the whole M2 chain introduced by
S6-S9 as ONE offline scenario, with ``VSPIDER_CROSS_SYSTEM_SWITCH=1``:

  1. the user submits two URLs -> ``input_contract.urls[]`` gets per-host
     ``system_id``s (S7) and carries the declared ``auth_profile``;
  2. the workflow graph builds its systems FROM the contract (S6), so
     SessionRouter resolves each system to its declared profile;
  3. the router pre-acquires one pool session per planned system (S9) and
     a forward hop applies only the target system's login subset;
  4. system A's extracted items reach system B's step through the
     in-memory ``WorkflowDataBus`` (S8) — no file round-trip.

Only BrowserEnv/Playwright is stubbed; contract parsing, graph building,
auth resolution, pool bookkeeping, storage filtering and the data bus are
all the real implementations.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from visual_web_agent.browser_session_pool import BrowserSessionPool
from visual_web_agent.cross_system_config import (
    CROSS_SYSTEM_SWITCH_ENV,
    cross_system_enabled,
)
from visual_web_agent.io_contract.input_contract import build_input_contract
from visual_web_agent.session_router import build_session_router
from visual_web_agent.workflow_data_bus import WorkflowDataBus, clear_run_bus, get_run_bus
from visual_web_agent.workflow_graph import build_workflow_graph

RUN_ID = "xsys_relay_e2e"

ERP_URL = "https://erp.example.com/list"
CRM_URL = "https://crm.example.com/form"

_DUAL_LOGIN_STATE = {
    "cookies": [
        {"name": "erp_session", "value": "AAA", "domain": ".erp.example.com", "path": "/"},
        {"name": "crm_session", "value": "BBB", "domain": "crm.example.com", "path": "/"},
    ],
    "origins": [
        {"origin": "https://erp.example.com", "localStorage": [{"name": "t", "value": "tokA"}]},
        {"origin": "https://crm.example.com", "localStorage": [{"name": "t", "value": "tokB"}]},
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


def _run(coro):
    return asyncio.run(coro)


def _contract_route() -> dict[str, Any]:
    """input_contract (S7 ids) -> route dict with an A-extract -> B-fill plan."""

    contract = build_input_contract(
        goal="从 ERP 列表导出数据，然后填到 CRM 表单",
        urls=[
            {"url": ERP_URL, "auth_profile": "erp_admin"},
            {"url": CRM_URL, "auth_profile": "crm_ops"},
        ],
    )
    erp_sid = next(u.system_id for u in contract.urls if u.url == ERP_URL)
    crm_sid = next(u.system_id for u in contract.urls if u.url == CRM_URL)
    return {
        "goal": contract.goal,
        "url": ERP_URL,
        "context": {"input_contract": contract.to_dict()},
        "execution_plan": {
            "version": "planner_contract.v1",
            "steps": [
                {"id": "step_01", "capability": "generic_extractor",
                 "purpose": "extract rows from ERP", "system_id": erp_sid},
                {"id": "step_02", "capability": "browser_control",
                 "purpose": "fill CRM form with relayed rows", "system_id": crm_sid},
            ],
        },
    }


@pytest.fixture()
def flag_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(CROSS_SYSTEM_SWITCH_ENV, "1")
    yield
    # monkeypatch auto-restores


class TestFlagOnDualSystemRelay:
    def test_full_chain_contract_to_relay(self, flag_on) -> None:
        assert cross_system_enabled() is True

        # --- S7+S6: contract urls become first-class systems with profiles
        route = _contract_route()
        graph = build_workflow_graph(route)
        systems = {s["id"]: s for s in graph["systems"]}
        assert "sys_erp_example_com" in systems
        assert "sys_crm_example_com" in systems
        assert systems["sys_erp_example_com"]["auth_profile"] == "erp_admin"
        assert systems["sys_crm_example_com"]["auth_profile"] == "crm_ops"
        assert "cross_system" in graph["risk_flags"]

        nodes = {n["capability"]: n for n in graph["nodes"]}
        assert nodes["generic_extractor"]["system_id"] == "sys_erp_example_com"
        assert nodes["browser_control"]["system_id"] == "sys_crm_example_com"
        assert graph["data_edges"], "A->B plan must declare a data edge"

        # --- S9: pre-acquire both planned sessions with resolved profiles
        pool = BrowserSessionPool(env_factory=_StubBrowserEnv)
        router = build_session_router(RUN_ID, {"workflow_graph": graph}, pool=pool)
        plan = router.pre_acquire_sessions(
            [s["id"] for s in graph["systems"] if s["type"] == "web"]
        )
        assert all(item["acquired"] for item in plan)
        by_system = {item["system_id"]: item for item in plan}
        assert by_system["sys_erp_example_com"]["auth_profile"] == "erp_admin"
        assert by_system["sys_crm_example_com"]["auth_profile"] == "crm_ops"
        assert pool.status()["active_count"] == 2

        # --- hop A -> B: only CRM's login subset lands in CRM's context
        directive = router.acquire_for_switch(
            to_system_id="sys_crm_example_com",
            from_system_id="sys_erp_example_com",
            full_state=_DUAL_LOGIN_STATE,
        )
        assert directive["should_switch"] is True
        assert directive["auth_profile"] == "crm_ops"
        browser = _run(router.activate_switch(
            directive,
            url=CRM_URL,
            user_data_dir_base="/profiles",
            full_state=_DUAL_LOGIN_STATE,
        ))
        crm_session = router.get_active("sys_crm_example_com")
        assert browser is crm_session.browser
        applied = crm_session.browser._context.added_cookies
        assert [c["name"] for c in applied] == ["crm_session"], (
            "CRM context must receive only the CRM login cookie"
        )

        # --- S8: A's items reach B through the run's in-memory bus
        clear_run_bus(RUN_ID)
        try:
            bus = get_run_bus(RUN_ID, workflow_graph=graph)
            assert bus is not None
            erp_rows = [{"order_no": "A-1"}, {"order_no": "A-2"}]
            packets = bus.publish_by_capability("generic_extractor", erp_rows)
            assert packets, "extractor node must have a downstream edge"
            relayed = bus.consume_by_capability("browser_control")
            assert relayed == [erp_rows], "B must receive A's rows from memory"
            # delivered once: a retry of B's step does not double-fill
            assert bus.consume_by_capability("browser_control") == []
            snap = bus.snapshot()
            assert snap["pending_count"] == 0
            assert "A-1" not in str(snap), "snapshot must stay payload-free"
        finally:
            clear_run_bus(RUN_ID)

        # --- teardown: both sessions released together under one run_id
        released = _run(router.release_all())
        assert len(released) == 2
        assert pool.status()["active_count"] == 0

    def test_default_is_on_and_opt_out_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # S11: unset -> on (zero-config multi-system); explicit 0 -> off.
        monkeypatch.delenv(CROSS_SYSTEM_SWITCH_ENV, raising=False)
        assert cross_system_enabled() is True
        monkeypatch.setenv(CROSS_SYSTEM_SWITCH_ENV, "0")
        assert cross_system_enabled() is False

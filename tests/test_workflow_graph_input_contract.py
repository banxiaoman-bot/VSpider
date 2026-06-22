"""S6: ``workflow_graph._build_systems`` reads ``input_contract.urls[]``.

Pre-S6 the user's declared ``urls[].system_id / auth_profile`` were parsed
into ``input_contract.json`` but never reached the workflow graph:
``_build_systems`` re-derived systems from the goal text, ``WorkflowSystem``
had no ``auth_profile`` field, and ``SessionRouter`` therefore always
resolved every system to ``auto``. These tests pin the S6 contract:

- ``input_contract.urls[]`` entries are the highest-priority system
  candidates (their system_id / auth_profile survive dedup),
- ``WorkflowSystem.to_dict()`` carries ``auth_profile`` (additive field),
- sessions inherit a concrete auth_profile when one is declared,
- ``build_session_router`` resolves the declared profile end-to-end.
"""

from __future__ import annotations

from typing import Any

from visual_web_agent.session_router import build_session_router
from visual_web_agent.workflow_graph import (
    WorkflowSystem,
    _build_systems,
    build_workflow_graph,
)


def _route_with_contract(urls: list[dict[str, Any]], goal: str = "", url: str = "") -> dict[str, Any]:
    return {
        "goal": goal,
        "url": url,
        "context": {
            "input_contract": {
                "version": "input_contract.v1",
                "goal": goal,
                "urls": urls,
            }
        },
        "execution_plan": {
            "version": "planner_contract.v1",
            "steps": [
                {"id": "step_01", "capability": "browser_control", "purpose": "open A"},
                {"id": "step_02", "capability": "browser_control", "purpose": "open B"},
            ],
        },
    }


class TestWorkflowSystemAuthProfileField:
    def test_to_dict_carries_auth_profile(self) -> None:
        system = WorkflowSystem(id="s1", type="web", name="a", auth_profile="ops_account")
        assert system.to_dict()["auth_profile"] == "ops_account"

    def test_default_auth_profile_is_auto(self) -> None:
        system = WorkflowSystem(id="s1", type="web", name="a")
        assert system.to_dict()["auth_profile"] == "auto"


class TestBuildSystemsFromContract:
    def test_contract_urls_become_systems_with_declared_identity(self) -> None:
        route = _route_with_contract([
            {"url": "https://erp.example.com/login", "role": "start",
             "system_id": "erp", "auth_profile": "erp_admin"},
            {"url": "https://crm.example.com/", "role": "start",
             "system_id": "crm", "auth_profile": "crm_ops"},
        ])
        systems = _build_systems(route, dict(route["execution_plan"]))
        by_id = {s.id: s for s in systems}
        assert by_id["erp"].auth_profile == "erp_admin"
        assert by_id["erp"].domain == "erp.example.com"
        assert by_id["erp"].auth_required is True
        assert by_id["crm"].auth_profile == "crm_ops"

    def test_contract_entry_wins_over_goal_extracted_same_domain(self) -> None:
        route = _route_with_contract(
            [{"url": "https://erp.example.com/login", "system_id": "erp",
              "auth_profile": "erp_admin"}],
            goal="打开 https://erp.example.com/login 录入数据",
            url="https://erp.example.com/login",
        )
        systems = _build_systems(route, dict(route["execution_plan"]))
        erp_systems = [s for s in systems if s.domain == "erp.example.com"]
        assert len(erp_systems) == 1
        assert erp_systems[0].id == "erp"
        assert erp_systems[0].auth_profile == "erp_admin"

    def test_same_domain_two_explicit_system_ids_both_kept(self) -> None:
        # cross-account on one host: the user said these are two systems.
        route = _route_with_contract([
            {"url": "https://portal.example.com/a", "system_id": "acct_a",
             "auth_profile": "profile_a"},
            {"url": "https://portal.example.com/b", "system_id": "acct_b",
             "auth_profile": "profile_b"},
        ])
        systems = _build_systems(route, dict(route["execution_plan"]))
        ids = {s.id for s in systems}
        assert {"acct_a", "acct_b"} <= ids

    def test_auto_markers_do_not_leak_as_identity(self) -> None:
        route = _route_with_contract([
            {"url": "https://a.example/", "system_id": "auto", "auth_profile": "auto"},
        ])
        systems = _build_systems(route, dict(route["execution_plan"]))
        assert systems[0].id.startswith("system_")
        assert systems[0].auth_profile == "auto"
        assert systems[0].auth_required is False

    def test_no_contract_keeps_legacy_goal_extraction(self) -> None:
        route = {
            "goal": "从 https://a.example/list 抓数据",
            "url": "https://a.example/list",
            "execution_plan": {"steps": []},
        }
        systems = _build_systems(route, {})
        assert systems[0].domain == "a.example"
        assert systems[0].auth_profile == "auto"

    def test_contract_at_route_top_level_also_read(self) -> None:
        route = {
            "goal": "",
            "input_contract": {
                "urls": [{"url": "https://x.example/", "system_id": "x",
                          "auth_profile": "px"}]
            },
            "execution_plan": {"steps": []},
        }
        systems = _build_systems(route, {})
        assert systems[0].id == "x"
        assert systems[0].auth_profile == "px"


class TestGraphSessionsAndRouter:
    def test_sessions_inherit_declared_auth_profile(self) -> None:
        route = _route_with_contract([
            {"url": "https://erp.example.com/", "system_id": "erp",
             "auth_profile": "erp_admin"},
            {"url": "https://crm.example.com/", "system_id": "crm"},
        ])
        graph = build_workflow_graph(route)
        session_by_system = {s["system_id"]: s for s in graph["sessions"]}
        assert session_by_system["erp"]["auth_profile"] == "erp_admin"
        # no declared profile -> legacy required/default semantics preserved
        assert session_by_system["crm"]["auth_profile"] in ("default", "required")

    def test_session_router_resolves_declared_profile_end_to_end(self) -> None:
        route = _route_with_contract([
            {"url": "https://erp.example.com/", "system_id": "erp",
             "auth_profile": "erp_admin"},
        ])
        capability_route = {"workflow_graph": build_workflow_graph(route)}
        router = build_session_router("run_s6", capability_route)
        assert router.resolved_auth_profile("erp") == "erp_admin"
        # explicit override still wins
        assert router.resolved_auth_profile("erp", "override_p") == "override_p"
        # unknown system stays auto
        assert router.resolved_auth_profile("ghost") == "auto"

    def test_graph_systems_dicts_expose_auth_profile(self) -> None:
        route = _route_with_contract([
            {"url": "https://erp.example.com/", "system_id": "erp",
             "auth_profile": "erp_admin"},
        ])
        graph = build_workflow_graph(route)
        erp = next(s for s in graph["systems"] if s["id"] == "erp")
        assert erp["auth_profile"] == "erp_admin"


# ---------------------------------------------------------------------------
# S7: contract-side system_id inference + route_task context injection
# ---------------------------------------------------------------------------


class TestAssignSystemIds:
    def test_multi_host_contract_gets_per_host_system_ids(self) -> None:
        from visual_web_agent.io_contract.input_contract import build_input_contract

        c = build_input_contract(
            goal="x",
            urls=["https://erp.example.com/login", "https://crm.example.com/"],
        )
        by_url = {u.url: u for u in c.urls}
        assert by_url["https://erp.example.com/login"].system_id == "sys_erp_example_com"
        assert by_url["https://crm.example.com/"].system_id == "sys_crm_example_com"

    def test_same_host_urls_share_one_system_id(self) -> None:
        from visual_web_agent.io_contract.input_contract import build_input_contract

        c = build_input_contract(
            goal="x",
            urls=[
                "https://erp.example.com/a",
                "https://erp.example.com/b",
                "https://crm.example.com/",
            ],
        )
        erp_ids = {u.system_id for u in c.urls if "erp" in u.url}
        assert erp_ids == {"sys_erp_example_com"}

    def test_single_host_contract_stays_auto(self) -> None:
        from visual_web_agent.io_contract.input_contract import build_input_contract

        c = build_input_contract(goal="x", urls=["https://a.example/x", "https://a.example/y"])
        assert all(u.system_id == "auto" for u in c.urls)

    def test_user_provided_system_id_never_overwritten(self) -> None:
        from visual_web_agent.io_contract.input_contract import build_input_contract

        c = build_input_contract(
            goal="x",
            urls=[
                {"url": "https://erp.example.com/", "system_id": "my_erp"},
                {"url": "https://crm.example.com/"},
            ],
        )
        by_url = {u.url: u for u in c.urls}
        assert by_url["https://erp.example.com/"].system_id == "my_erp"
        assert by_url["https://crm.example.com/"].system_id == "sys_crm_example_com"


class TestRouteTaskContractFlow:
    def test_route_task_context_contract_reaches_graph_systems(self) -> None:
        from visual_web_agent.capability_router import route_task
        from visual_web_agent.io_contract.input_contract import build_input_contract

        contract = build_input_contract(
            goal="从 ERP 导出数据后填到 CRM",
            urls=[
                {"url": "https://erp.example.com/list", "auth_profile": "erp_admin"},
                {"url": "https://crm.example.com/form"},
            ],
        )
        route = route_task(
            "从 ERP 导出数据后填到 CRM",
            url="https://erp.example.com/list",
            context={"input_contract": contract.to_dict()},
        )
        systems = {s["id"]: s for s in route["workflow_graph"]["systems"]}
        assert "sys_erp_example_com" in systems
        assert "sys_crm_example_com" in systems
        assert systems["sys_erp_example_com"]["auth_profile"] == "erp_admin"
        assert systems["sys_erp_example_com"]["domain"] == "erp.example.com"

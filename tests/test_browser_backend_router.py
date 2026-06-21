"""Deterministic regression for the three-backend task-type router.

Locks the routing maths of ``visual_web_agent/browser_backend_router.py`` so the
``browser_backend_route.v1`` contract and the hard safety rules (vision never on
lightpanda, high anti-bot prefers cloak, bulk pixel-free prefers lightpanda,
explicit override, graceful availability degrade) cannot silently drift.

All routing-logic tests inject fixed personas so behaviour is independent of the
host environment; ``build_backend_for`` is exercised through a monkeypatched
factory so no real browser is ever constructed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import visual_web_agent.browser_backend_router as router_mod
from visual_web_agent.browser_backend_router import (
    ROUTE_CONTRACT_VERSION,
    BackendPersona,
    BackendRoutingDecision,
    BrowserBackendRouter,
    TaskProfile,
    build_backend_for,
    default_backend_personas,
    resolve_kind_alias,
    route_browser_backend,
)

_CDP_ENV_VARS = (
    "VSPIDER_REMOTE_BROWSER_ENDPOINT",
    "VSPIDER_BROWSER_CDP_ENDPOINT",
    "VSPIDER_BROWSER_WS_ENDPOINT",
    "VSPIDER_LIGHTPANDA_ENDPOINT",
)


@pytest.fixture(autouse=True)
def _clean_backend_env(monkeypatch):
    """Isolate every test from stray backend env hints."""

    monkeypatch.delenv("VSPIDER_BROWSER_BACKEND", raising=False)
    for name in _CDP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


def _fixed_personas(
    *, lightpanda_status: str = "available", cloak_status: str = "planned"
) -> list[BackendPersona]:
    return [
        BackendPersona(
            name="chromium",
            backend_slot="playwright_chromium",
            renders=True,
            anti_bot_level="medium",
            throughput="low",
            status="available",
        ),
        BackendPersona(
            name="lightpanda",
            backend_slot="remote_playwright",
            renders=False,
            anti_bot_level="low",
            throughput="high",
            status=lightpanda_status,
        ),
        BackendPersona(
            name="cloakbrowser",
            backend_slot="stealth_browser",
            renders=True,
            anti_bot_level="high",
            throughput="low",
            status=cloak_status,
        ),
    ]


def _router(**kwargs) -> BrowserBackendRouter:
    return BrowserBackendRouter(personas=_fixed_personas(**kwargs))


class TestAliasResolution:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("chromium", "chromium"),
            ("chrome", "chromium"),
            ("playwright", "chromium"),
            ("playwright_chromium", "chromium"),
            ("local", "chromium"),
            ("lightpanda", "lightpanda"),
            ("remote", "lightpanda"),
            ("remote_playwright", "lightpanda"),
            ("cdp", "lightpanda"),
            ("ws", "lightpanda"),
            ("cloak", "cloakbrowser"),
            ("cloakbrowser", "cloakbrowser"),
            ("stealth", "cloakbrowser"),
            ("stealth_browser", "cloakbrowser"),
            ("  Chromium  ", "chromium"),
            ("", ""),
            ("does-not-exist", ""),
        ],
    )
    def test_resolve_kind_alias(self, raw, expected):
        assert resolve_kind_alias(raw) == expected


class TestRoutingDecision:
    def test_plain_visual_defaults_to_chromium(self):
        decision = _router().route(TaskProfile())
        assert decision.contract_version == ROUTE_CONTRACT_VERSION
        assert decision.preferred == "chromium"
        assert decision.selected == "chromium"
        assert decision.backend_slot == "playwright_chromium"
        # lightpanda is available but not preferred for a plain visual task.
        assert decision.fallback_chain == ["lightpanda"]

    def test_vision_task_excludes_lightpanda(self):
        decision = _router().route(TaskProfile(needs_vision=True))
        assert "lightpanda" not in decision.preference_order
        by_name = {c["name"]: c for c in decision.candidates}
        assert by_name["lightpanda"]["eligible"] is False
        assert decision.selected == "chromium"
        assert any("lightpanda excluded" in r for r in decision.reasons)

    def test_screenshot_task_also_excludes_lightpanda(self):
        decision = _router().route(TaskProfile(needs_screenshot=True))
        assert "lightpanda" not in decision.preference_order
        assert decision.selected == "chromium"

    def test_bulk_pixel_free_prefers_lightpanda(self):
        decision = _router().route(TaskProfile(bulk_scale=True))
        assert decision.preferred == "lightpanda"
        assert decision.selected == "lightpanda"
        assert decision.backend_slot == "remote_playwright"
        assert any("lightpanda throughput" in r for r in decision.reasons)

    def test_bulk_degrades_when_lightpanda_not_configured(self):
        decision = _router(lightpanda_status="not_configured").route(
            TaskProfile(bulk_scale=True)
        )
        # Intent still ranks lightpanda first, but it is not wired -> degrade.
        assert decision.preferred == "lightpanda"
        assert decision.selected == "chromium"
        assert any("degraded" in r for r in decision.reasons)

    def test_high_anti_bot_prefers_cloak_when_available(self):
        decision = _router(cloak_status="available").route(
            TaskProfile(anti_bot_level="high")
        )
        assert decision.preferred == "cloakbrowser"
        assert decision.selected == "cloakbrowser"
        assert decision.backend_slot == "stealth_browser"

    def test_high_anti_bot_degrades_to_chromium_when_cloak_planned(self):
        decision = _router().route(TaskProfile(anti_bot_level="high"))
        assert decision.preferred == "cloakbrowser"
        assert decision.selected == "chromium"
        assert any("anti-bot" in r for r in decision.reasons)

    def test_explicit_backend_override_wins(self):
        decision = _router().route(TaskProfile(explicit_backend="lightpanda"))
        assert decision.preferred == "lightpanda"
        assert decision.selected == "lightpanda"
        assert any("explicit backend hint: lightpanda" in r for r in decision.reasons)

    def test_render_safety_rule_beats_explicit_override(self):
        # Even an explicit lightpanda request must not serve a vision task.
        decision = _router().route(
            TaskProfile(needs_vision=True, explicit_backend="lightpanda")
        )
        by_name = {c["name"]: c for c in decision.candidates}
        assert by_name["lightpanda"]["eligible"] is False
        assert decision.selected == "chromium"

    def test_explicit_override_via_env(self, monkeypatch):
        monkeypatch.setenv("VSPIDER_BROWSER_BACKEND", "remote")
        decision = _router().route(TaskProfile())
        assert decision.selected == "lightpanda"

    def test_decision_to_dict_contract_shape(self):
        decision = _router().route(TaskProfile())
        data = decision.to_dict()
        assert set(data) == {
            "contract_version",
            "preferred",
            "selected",
            "backend_slot",
            "preference_order",
            "fallback_chain",
            "task_profile",
            "candidates",
            "reasons",
        }
        assert data["contract_version"] == "browser_backend_route.v1"
        assert isinstance(data["candidates"], list) and data["candidates"]
        for cand in data["candidates"]:
            assert set(cand) >= {
                "name",
                "backend_slot",
                "status",
                "eligible",
                "fit_score",
                "reasons",
                "persona",
            }

    def test_returns_routing_decision_instance(self):
        assert isinstance(_router().route(TaskProfile()), BackendRoutingDecision)


class TestDefaultPersonas:
    def test_lightpanda_gated_by_cdp_env(self, monkeypatch):
        personas = {p.name: p for p in default_backend_personas()}
        assert personas["lightpanda"].status == "not_configured"
        assert personas["chromium"].status == "available"
        assert personas["cloakbrowser"].status == "planned"

        monkeypatch.setenv("VSPIDER_REMOTE_BROWSER_ENDPOINT", "ws://127.0.0.1:9222")
        personas2 = {p.name: p for p in default_backend_personas()}
        assert personas2["lightpanda"].status == "available"

    def test_route_browser_backend_convenience_uses_defaults(self):
        # With env cleared lightpanda is not configured; a vision task must land
        # on chromium through the default-persona convenience wrapper.
        decision = route_browser_backend(needs_vision=True)
        assert decision.contract_version == ROUTE_CONTRACT_VERSION
        assert decision.selected == "chromium"


class TestBuildBackendFor:
    def test_persona_maps_to_concrete_kind(self, monkeypatch):
        calls: list[str] = []

        def _fake(kind: str = ""):
            calls.append(kind)
            return SimpleNamespace(kind=kind)

        monkeypatch.setattr(router_mod, "build_default_browser_backend", _fake)

        build_backend_for("chromium")
        build_backend_for("lightpanda")
        assert calls == ["playwright", "remote"]

    def test_cloak_not_implemented(self):
        with pytest.raises(NotImplementedError):
            build_backend_for("cloakbrowser")
        with pytest.raises(NotImplementedError):
            build_backend_for("stealth")

    def test_unknown_persona_raises_value_error(self):
        with pytest.raises(ValueError):
            build_backend_for("totally-unknown")

    def test_router_build_uses_selection(self, monkeypatch):
        calls: list[str] = []

        def _fake(kind: str = ""):
            calls.append(kind)
            return SimpleNamespace(kind=kind)

        monkeypatch.setattr(router_mod, "build_default_browser_backend", _fake)

        router = _router()
        decision = router.route(TaskProfile())
        router.build(decision)
        assert calls == ["playwright"]

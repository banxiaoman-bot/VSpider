"""Tests for page understanding, experience reuse, and semantic routing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from visual_web_agent import experience_reuse as er
from visual_web_agent import page_understanding as pu
from visual_web_agent import semantic_router as sr
from visual_web_agent.capability_router import route_task


class TestPageUnderstanding:
    def test_generic_goal_boosts_targeted_probe(self) -> None:
        tools = [
            {"name": "wikipedia_new_tab_macro", "match_score": 10, "aliases": []},
            {"name": "targeted_probe", "match_score": 2, "aliases": []},
        ]
        ranked = pu.reprioritize_agent_tools(
            tools,
            goal="点击页面上的提交按钮",
            url="https://unknown.example/form",
        )
        assert ranked[0]["name"] == "targeted_probe"

    def test_site_macro_kept_when_url_matches(self) -> None:
        tools = [
            {
                "name": "demoqa_slider_macro",
                "match_score": 3,
                "aliases": ["demoqa.com/slider"],
            },
            {"name": "targeted_probe", "match_score": 4, "aliases": []},
        ]
        ranked = pu.reprioritize_agent_tools(
            tools,
            goal="move slider",
            url="https://demoqa.com/slider",
        )
        assert ranked[0]["name"] == "demoqa_slider_macro"


class TestExperienceReuse:
    def test_rpa_cache_hint_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        root = Path(__file__).resolve().parent / ".tmp_intelligence" / "rpa_miss"
        root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(er, "_RPA_CACHE_DIR", root)
        hint = er.lookup_rpa_cache_hint("https://a.test", "extract table")
        assert hint["hit"] is False

    def test_rpa_cache_hint_hit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        root = Path(__file__).resolve().parent / ".tmp_intelligence" / "rpa_hit"
        root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(er, "_RPA_CACHE_DIR", root)
        key = er._rpa_cache_key("https://a.test", "extract table")
        path = root / f"{key}.json"
        path.write_text(
            json.dumps({"replayable": True, "trail": [{"action": "click"}]}),
            encoding="utf-8",
        )
        hint = er.lookup_rpa_cache_hint("https://a.test", "extract table")
        assert hint["hit"] is True
        assert hint["trail_steps"] == 1

    def test_failure_fixture_scan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        root = Path(__file__).resolve().parent / ".tmp_intelligence" / "fixtures"
        root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(er, "_fixture_root", lambda: root)
        fixture = {
            "version": "capability_failure_regression_fixture.v1",
            "name": "login_timeout",
            "expected": {
                "url": "https://shop.example/checkout",
                "primary_failure": "timeout",
                "capability": "browser_control",
            },
        }
        (root / "f1.json").write_text(json.dumps(fixture), encoding="utf-8")
        hints = er.scan_failure_fixture_hints(
            "https://shop.example/pay",
            "complete checkout",
        )
        assert len(hints) == 1
        assert hints[0]["capability"] == "browser_control"


class TestSemanticRouter:
    def test_inject_targeted_probe_into_plan(self) -> None:
        plan = [{"name": "browser_control", "layer": "browser"}]
        inject = [sr._PERCEPTION_CAP]
        merged = sr.inject_capabilities_into_plan(plan, inject)
        names = [item["name"] for item in merged]
        assert "targeted_probe" in names
        assert names.index("targeted_probe") < names.index("browser_control")

    def test_route_task_exposes_semantic_fields(self) -> None:
        route = route_task("提取页面表格数据", url="https://quotes.toscrape.com/")
        assert "semantic_route" in route
        assert "experience_reuse" in route
        assert route.get("semantic_route", {}).get("prefer_structured_extract") is True
        plan_names = [item.get("name") for item in route.get("backend_plan") or []]
        assert "targeted_probe" in plan_names

    def test_merge_task_plan_into_route(self) -> None:
        class _SG:
            def __init__(self, desc: str) -> None:
                self.description = desc

        class _Plan:
            sub_goals = [_SG("open form"), _SG("submit")]

        merged = sr.merge_task_plan_into_route(_Plan(), {"strategy_context": {}})
        assert merged["planner_sub_goals"] == ["open form", "submit"]
        assert merged["semantic_route"]["planner_fused"] is True

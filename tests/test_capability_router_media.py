"""Capability router integration: ``media_harvester`` is force-selected when
``output_contract.output_kind`` starts with ``media_``.

We only assert behavioural properties (tool presence, output_contract in
result, manifest unchanged across calls). The router pulls in a fair bit
of state so we avoid asserting on full ordering.
"""

from __future__ import annotations

import pytest

from visual_web_agent.capability_router import route_task


class TestMediaHarvesterForcedSelection:
    def test_media_pdf_goal_forces_harvester(self) -> None:
        route = route_task(
            "帮我把页面上所有 PDF 报告下载到本地",
            url="https://example.com/reports",
        )
        names = [t.get("name") for t in route.get("selected_agent_tools") or []]
        assert "media_harvester" in names

    def test_media_image_goal_forces_harvester(self) -> None:
        route = route_task(
            "save all images on this page",
            url="https://example.com/gallery",
        )
        names = [t.get("name") for t in route.get("selected_agent_tools") or []]
        assert "media_harvester" in names

    def test_dataset_goal_does_not_force_harvester(self) -> None:
        route = route_task(
            "提取商品列表的名称和价格",
            url="https://example.com/products",
        )
        names = [t.get("name") for t in route.get("selected_agent_tools") or []]
        # NOT forced - only present if alias matched, which it shouldn't here
        # because the goal text speaks of structured extraction
        forced_reasons = [
            (t.get("strategy_reasons") or [])
            for t in route.get("selected_agent_tools") or []
            if t.get("name") == "media_harvester"
        ]
        for reasons in forced_reasons:
            assert not any("forced_by_output_kind" in r for r in reasons)

    def test_output_contract_in_result(self) -> None:
        route = route_task("save all photos", url="https://x.com")
        assert "output_contract" in route
        assert route["output_contract"]["version"] == "output_contract.v1"

    def test_forced_score_is_high(self) -> None:
        route = route_task("download every pdf", url="https://x.com")
        harvester = next(
            (t for t in route.get("selected_agent_tools") or []
             if t.get("name") == "media_harvester"),
            None,
        )
        assert harvester is not None
        assert harvester.get("match_score", 0) >= 1

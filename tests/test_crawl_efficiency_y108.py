from __future__ import annotations

from visual_web_agent.crawl_efficiency import build_crawl_efficiency_plan


def test_crawl_efficiency_prefers_api_replay_before_browser_or_vlm() -> None:
    plan = build_crawl_efficiency_plan(
        {"goal": "extract product list", "requires_browser": True},
        route={"intent": {"task_type": "structured_extraction"}},
        network_candidates=[
            {"endpoint": "https://api.example.com/items", "score": 9, "row_count": 20},
        ],
        browser_state={"version": "browser_state.v2", "metrics": {"interactive_count": 12}},
        runtime_status={"status": "available"},
    )

    assert plan["version"] == "crawl_efficiency_plan.v1"
    assert plan["source"] == "crawl_efficiency"
    assert plan["preferred_order"] == [
        "api_replay",
        "html_extract",
        "dom_selector",
        "targeted_probe",
        "browser_action",
        "vision_agent",
    ]
    assert plan["recommended_path"] == "api_replay"
    assert plan["skip_browser"] is True
    assert plan["skip_vlm"] is True
    assert plan["available_paths"][0] == "api_replay"
    assert plan["efficiency_summary"]["network_candidate_count"] == 1
    assert plan["efficiency_summary"]["best_network_score"] == 9


def test_crawl_efficiency_prefers_html_before_dom_selector() -> None:
    plan = build_crawl_efficiency_plan(
        {
            "source": "<html><body><table><tr><td>A</td></tr></table></body></html>",
            "selector": "table tr",
        },
        route={"intent": {"task_type": "structured_extraction"}},
    )

    assert plan["recommended_path"] == "html_extract"
    assert plan["skip_browser"] is True
    assert plan["skip_vlm"] is True
    assert plan["available_paths"][:2] == ["html_extract", "dom_selector"]
    html = next(item for item in plan["candidates"] if item["name"] == "html_extract")
    dom = next(item for item in plan["candidates"] if item["name"] == "dom_selector")
    assert html["available"] is True
    assert dom["available"] is True
    assert html["score"] > dom["score"]


def test_crawl_efficiency_uses_targeted_probe_before_browser_action_for_interaction() -> None:
    plan = build_crawl_efficiency_plan(
        {"goal": "click submit", "requires_browser": True},
        route={"intent": {"task_type": "browser_interaction"}},
        browser_state={
            "version": "browser_state.v2",
            "metrics": {"interactive_count": 3},
            "runtime": {"status": "available"},
        },
    )

    assert plan["recommended_path"] == "targeted_probe"
    assert plan["skip_browser"] is False
    assert plan["skip_vlm"] is True
    assert plan["available_paths"][:2] == ["targeted_probe", "browser_action"]
    probe = next(item for item in plan["candidates"] if item["name"] == "targeted_probe")
    assert probe["available"] is True
    assert probe["evidence"]["interactive_count"] == 3


def test_crawl_efficiency_falls_back_to_vision_when_no_deterministic_evidence() -> None:
    plan = build_crawl_efficiency_plan(
        {"goal": "understand visual layout"},
        route={"intent": {"task_type": "general_browser_agent"}},
        runtime_status={"status": "unavailable"},
    )

    assert plan["recommended_path"] == "vision_agent"
    assert plan["skip_browser"] is False
    assert plan["skip_vlm"] is False
    assert plan["available_paths"] == ["vision_agent"]
    assert plan["efficiency_summary"]["browser_needed"] is False

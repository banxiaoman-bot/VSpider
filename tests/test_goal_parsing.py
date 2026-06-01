from visual_web_agent.extraction_engine.strategies import (
    choose_pre_extract_reached_candidate,
    click_target_is_same_page_extract_nav,
    data_shape_exposes_target_candidate,
    same_document_url,
)
from types import SimpleNamespace

from visual_web_agent.main import (
    _done_targets_final_subgoal,
    _extraction_targets_reached,
    _parse_goal_target_count,
)


def test_parse_relative_three_day_forecast_target_count():
    assert (
        _parse_goal_target_count(
            "提取今天、明天及后天的天气现象、最高/最低温度、风力等级"
        )
        == 3
    )
    assert _parse_goal_target_count("extract today, tomorrow and day after tomorrow") == 3


def test_explicit_numeric_target_still_wins():
    assert _parse_goal_target_count("提取最新的 20 篇文章") == 20


def test_parse_multi_day_forecast_horizon():
    assert _parse_goal_target_count("提取未来7天天气预报") == 7
    assert _parse_goal_target_count("打开7日预报页并提取7日预报") == 7
    assert _parse_goal_target_count("extract next 7 days weather forecast") == 7


def test_extraction_targets_reached_rows_and_pages():
    assert _extraction_targets_reached(
        "extract next 7 days weather forecast",
        total_rows=7,
    )["reached"]
    assert not _extraction_targets_reached(
        "extract next 7 days weather forecast",
        total_rows=6,
    )["reached"]
    page_state = _extraction_targets_reached("extract first 2 pages", total_pages=2)
    assert page_state["reached"]
    assert page_state["pages_reached"]


def test_pre_extract_reached_candidate_prefers_ordered_families():
    chosen = choose_pre_extract_reached_candidate(
        [
            {"name": "FULL_PAGE:AX_TREE", "accepted": 20, "score": 3000},
            {"name": "DOM_CARDS", "accepted": 20, "score": 2500},
            {"name": "DOM_TABLE", "accepted": 20, "score": 1000},
        ],
        20,
    )
    assert chosen["name"] == "DOM_TABLE"


def test_pre_extract_reached_candidate_ignores_under_target():
    chosen = choose_pre_extract_reached_candidate(
        [
            {"name": "DOM_TABLE", "accepted": 19, "score": 5000},
            {"name": "DOM_CARDS", "accepted": 20, "score": 100},
        ],
        20,
    )
    assert chosen["name"] == "DOM_CARDS"


def test_same_page_nav_helpers_detect_extract_nav_targets():
    current = "https://example.com/weather/101.shtml?x=1#today"
    assert same_document_url(current, "https://example.com/weather/101.shtml?x=1#7d")
    assert not same_document_url(current, "https://example.com/weather/7d/101.shtml")
    assert data_shape_exposes_target_candidate(
        {"repeated_list_items": 7, "table_rows": 0, "table_cells": 0},
        7,
    )
    assert not data_shape_exposes_target_candidate(
        {"repeated_list_items": 6, "table_rows": 0, "table_cells": 0},
        7,
    )
    assert click_target_is_same_page_extract_nav(
        {"role": "link", "href": "https://example.com/weather/101.shtml?x=1#7d"},
        current,
    )
    assert click_target_is_same_page_extract_nav(
        {"role": "tab", "text": "7 days forecast", "tab_like": True},
        current,
    )
    assert _parse_goal_target_count("提取一周天气") == 7


def test_done_targets_final_subgoal_accepts_completed_last_step():
    plan = SimpleNamespace(
        current_idx=1,
        sub_goals=[
            SimpleNamespace(status="done"),
            SimpleNamespace(status="active"),
        ],
    )

    assert _done_targets_final_subgoal(
        plan,
        {
            "action": "done",
            "subgoal_status": "completed",
            "thought": "",
            "progress_review": "",
            "current_state": "",
        },
    )


def test_done_targets_final_subgoal_rejects_nonfinal_step():
    plan = SimpleNamespace(
        current_idx=0,
        sub_goals=[
            SimpleNamespace(status="active"),
            SimpleNamespace(status="pending"),
        ],
    )

    assert not _done_targets_final_subgoal(
        plan,
        {
            "action": "done",
            "subgoal_status": "completed",
            "thought": "",
            "progress_review": "",
            "current_state": "",
        },
    )

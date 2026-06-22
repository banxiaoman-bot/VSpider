from __future__ import annotations

from visual_web_agent.agent_strategy import (
    extraction_targets_reached,
    normalize_guard_url,
    normalize_output_field_key,
    parse_goal_requested_fields,
    parse_goal_target_count,
    parse_goal_target_pages,
)


def test_agent_strategy_parses_row_targets_from_chinese_and_english_goals() -> None:
    assert parse_goal_target_count("获取新闻列表前90条") == 90
    assert parse_goal_target_count("前26部电影的标题、评分、评价人数和一句话简介") == 26
    assert parse_goal_target_count("Extract top 12 records into Excel") == 12
    assert parse_goal_target_count("今天、明天和后天的天气") == 3
    assert parse_goal_target_count("未来7天天气预报") == 7
    assert parse_goal_target_count("获取全部内容") is None


def test_agent_strategy_parses_page_targets_and_reached_status() -> None:
    assert parse_goal_target_pages("抓取前5页商品") == 5
    assert parse_goal_target_pages("extract first 3 pages") == 3
    assert parse_goal_target_pages("抓取商品列表") is None

    rows = extraction_targets_reached("抓取前10条商品", total_rows=10, total_pages=0)
    pages = extraction_targets_reached("抓取前3页商品", total_rows=2, total_pages=3)

    assert rows["reached"] is True
    assert rows["rows_reached"] is True
    assert rows["row_target"] == 10
    assert pages["reached"] is True
    assert pages["pages_reached"] is True
    assert pages["page_target"] == 3


def test_agent_strategy_parses_requested_fields_and_normalizes_keys() -> None:
    assert normalize_output_field_key(" Title / URL ") == "titleurl"
    assert parse_goal_requested_fields("抓取前10条数据，字段为 title, price, url，并导出 Excel") == ["title", "price", "url"]
    assert parse_goal_requested_fields("提取 Name, Position, Office 字段") == ["Name", "Position", "Office"]
    assert parse_goal_requested_fields("前26部电影的标题、评分、评价人数和一句话简介，保存为表格") == ["标题", "评分", "评价人数", "一句话简介"]
    assert parse_goal_requested_fields("抓取前50条数据") == []


def test_parse_requested_fields_prefers_explicit_spec_in_output_section() -> None:
    """显式 '字段: name(描述)' 即便位于 【输出要求】 段（会被 extract_core_goal 剥离），
    也应取机器字段名(name) 并去掉括号描述——否则字段名错配导致抽取行被全量丢弃。"""
    goal = (
        "提取首页所有名言的文字和对应作者\n\n"
        "【输出要求】\n"
        "字段: quote(名言文字), author(作者); 输出为结构化列表"
    )
    assert parse_goal_requested_fields(goal) == ["quote", "author"]


def test_parse_requested_fields_strips_parenthetical_descriptions() -> None:
    """括号描述应被去掉，保留机器字段名。"""
    assert parse_goal_requested_fields("字段: title(标题), price(价格)") == ["title", "price"]


def test_agent_strategy_normalizes_guard_url_without_query_or_fragment() -> None:
    assert normalize_guard_url("https://example.com/path/list?q=abc#top") == "https://example.com/path/list"
    assert normalize_guard_url("") == ""

"""Unit tests for date_pick macro (parser + registry integration)."""

from __future__ import annotations

import calendar
import re
from datetime import date

from visual_web_agent import semantic_macros
from visual_web_agent.semantic_macros.date_pick import parse
from visual_web_agent.semantic_macros._dateutils import (
    month_offset_day,
    next_month_day,
    parse_goal_scope_title,
    parse_relative_month_day,
)


# ── _dateutils helpers ────────────────────────────────────────────────────────
def test_month_offset_day_basic() -> None:
    today = date.today()
    # offset=0, day=1 → today's month, day 1
    expected = f"{today.year:04d}-{today.month:02d}-01"
    assert month_offset_day(0, 1) == expected


def test_month_offset_day_clamps_to_month_length() -> None:
    """31 in a 30-day month should clamp to 30."""
    today = date.today()
    # Find a month that has 30 days and is reachable via small offset.
    for offset in range(0, 13):
        month_index = today.month - 1 + offset
        year = today.year + month_index // 12
        month = month_index % 12 + 1
        last_day = calendar.monthrange(year, month)[1]
        if last_day == 30:
            result = month_offset_day(offset, 31)
            assert result == f"{year:04d}-{month:02d}-30", result
            return


def test_parse_relative_month_day_simple() -> None:
    assert parse_relative_month_day("下个月15号") == (1, 15)
    assert parse_relative_month_day("下下个月5号") == (2, 5)
    assert parse_relative_month_day("下下下个月的8日") == (3, 8)


def test_parse_relative_month_day_numeric_offset() -> None:
    assert parse_relative_month_day("下3个月20号") == (3, 20)


def test_parse_relative_month_day_no_match() -> None:
    assert parse_relative_month_day("提取表格") is None
    assert parse_relative_month_day("") is None


def test_parse_goal_scope_title() -> None:
    assert parse_goal_scope_title('"Enter Date" 标题下方') == "Enter Date"
    assert parse_goal_scope_title("没有引号") == ""


# ── Goal parser ───────────────────────────────────────────────────────────────
def test_parse_relative_chinese() -> None:
    step = parse('找到 "Enter Date" 标题下方的 "Pick a day" 输入框，选择下个月的 15 号。')
    assert step is not None
    assert step["action"] == "date_pick"
    assert step["target_date_rule"]["mode"] == "relative_month"
    assert step["target_date_rule"]["offset"] == 1
    assert step["target_date_rule"]["day"] == 15
    assert step["anchor"]["section"] == "Enter Date"
    assert step["anchor"]["placeholder"] == "Pick a day"
    assert step["anchor"]["occurrence"] == 1
    assert step["validate"]["input_should_contain"] == step["target_date"]


def test_parse_returns_none_when_no_calendar_keyword() -> None:
    """We REQUIRE a calendar-domain keyword somewhere in the goal to fire."""
    assert parse("点击下个月的 15 号按钮") is None
    assert parse("") is None


def test_parse_absolute_date() -> None:
    step = parse('在 date picker 选择 2026-07-15')
    assert step is not None
    assert step["target_date"] == "2026-07-15"
    assert step["target_date_rule"] == {"mode": "absolute", "date": "2026-07-15"}


def test_parse_absolute_chinese_format() -> None:
    step = parse("打开日期选择器，选择 2026年7月15日")
    assert step is not None
    assert step["target_date"] == "2026-07-15"


def test_parse_english_next_month() -> None:
    step = parse("In the date picker, select next month 5")
    assert step is not None
    assert step["target_date_rule"] == {"mode": "relative_month", "offset": 1, "day": 5}


def test_parse_english_month_after_next() -> None:
    step = parse("Open the calendar and pick month after next 20")
    assert step is not None
    assert step["target_date_rule"] == {"mode": "relative_month", "offset": 2, "day": 20}


def test_parse_english_in_n_months() -> None:
    step = parse("In the calendar pick a day in 3 months 8")
    assert step is not None
    assert step["target_date_rule"] == {"mode": "relative_month", "offset": 3, "day": 8}


def test_parse_relative_target_date_uses_today() -> None:
    """Sanity: parser regenerates target_date from today() each call."""
    step = parse("打开日历，选择下个月的 1 号")
    assert step is not None
    assert step["target_date"] == next_month_day(1)


def test_parse_picks_up_placeholder_fallback() -> None:
    """Even without quoted placeholder, English 'pick a day' phrase implies it."""
    step = parse("In the date picker click pick a day field, choose next month 10")
    assert step is not None
    assert step["anchor"]["placeholder"] == "Pick a day"


# ── Registry integration ──────────────────────────────────────────────────────
def test_macro_is_registered() -> None:
    macro = semantic_macros.get("date_pick")
    assert macro is not None
    assert macro.action == "date_pick"
    assert "date_pick" in semantic_macros.actions()


def test_parse_goal_routes_to_date_pick() -> None:
    g = '点击 "Enter Date" 区域的 "Pick a day"，选择下个月 15 号。'
    step = semantic_macros.parse_goal(g)
    assert step is not None
    assert step["action"] == "date_pick"
    assert step["target_date_rule"]["day"] == 15


def test_macro_js_body_includes_critical_primitives() -> None:
    macro = semantic_macros.get("date_pick")
    assert macro is not None
    js = semantic_macros.build_macro_js(macro.js_body)
    for needle in (
        "const norm",        # primitive
        "const clickEl",     # primitive
        "const sepNorm",     # primitive
        "cellDayNumber",     # body helper
        "navButton",         # body helper
        "isTargetObserved",  # body helper
        "allow_direct_set",  # runtime flag plumbing
    ):
        assert needle in js, f"missing in built JS: {needle}"


# ── VL judge question ─────────────────────────────────────────────────────────
def test_postcheck_question_uses_target_date() -> None:
    macro = semantic_macros.get("date_pick")
    assert macro is not None
    question = macro.postcheck_question(
        {"target_date": "2026-06-15"},
        {"reason": "postcheck_failed", "observed": "2026-06-01"},
    )
    assert question is not None
    assert "2026-06-15" in question
    assert "2026/06/15" in question  # slash variant offered
    assert re.search(r"2026年\s*6月\s*15日", question)


def test_postcheck_question_returns_none_on_bad_target() -> None:
    macro = semantic_macros.get("date_pick")
    assert macro is not None
    assert macro.postcheck_question({"target_date": "garbage"}, {}) is None
    assert macro.postcheck_question({}, {}) is None


# ── retryable_with_vl_reasons covers the empirically retryable cases ──────────
def test_retryable_reasons_match_known_failures() -> None:
    macro = semantic_macros.get("date_pick")
    assert macro is not None
    # postcheck_failed: clicked but observed value didn't match
    # date_cell_not_found: panel was open but no cell with target day was visible
    # Both should fall through to the VL judge for a second opinion.
    assert "postcheck_failed" in macro.retryable_with_vl_reasons
    assert "date_cell_not_found" in macro.retryable_with_vl_reasons
    # bad_target_date / date_input_not_found / month_nav_not_found are hard
    # failures — VL can't recover them.
    assert "bad_target_date" not in macro.retryable_with_vl_reasons
    assert "month_nav_not_found" not in macro.retryable_with_vl_reasons

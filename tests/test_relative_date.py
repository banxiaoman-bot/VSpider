"""Comprehensive regression suite for the generic relative-date resolver.

This module is the single point of validation for the generic capability
that replaces the previous site-specific date-next-month skill. Adding a
new relative-date phrasing should require:
  * one new entry to a matcher table, AND
  * one new parametrised test row here.

If you add a new test row but the resolver doesn't catch it, the design
is broken — go fix the resolver, don't weaken the test.
"""
from __future__ import annotations

import calendar
import sys
from datetime import date
from pathlib import Path

import pytest

_VWA = Path(__file__).resolve().parents[1] / "visual_web_agent"
sys.path.insert(0, str(_VWA))

from relative_date import (  # noqa: E402
    RelativeDateMatch,
    format_resolver_hint,
    resolve_relative_date,
)


# A non-edge fixed "today" — far from month boundaries so most tests don't
# accidentally exercise day-clamping or year-wrap as their primary concern.
TODAY = date(2026, 5, 22)  # Friday


# ── Helper ────────────────────────────────────────────────────────────────
def _resolve(goal: str, today: date = TODAY) -> RelativeDateMatch | None:
    return resolve_relative_date(goal, today=today)


# ──────────────────────────────────────────────────────────────────────────
# 1. Simple day offsets
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("今天", date(2026, 5, 22)),
        ("今日", date(2026, 5, 22)),
        ("明天", date(2026, 5, 23)),
        ("明日", date(2026, 5, 23)),
        ("后天", date(2026, 5, 24)),
        ("大后天", date(2026, 5, 25)),
        ("昨天", date(2026, 5, 21)),
        ("昨日", date(2026, 5, 21)),
        ("前天", date(2026, 5, 20)),
        ("大前天", date(2026, 5, 19)),
        ("today", date(2026, 5, 22)),
        ("tomorrow", date(2026, 5, 23)),
        ("yesterday", date(2026, 5, 21)),
        ("the day after tomorrow", date(2026, 5, 24)),
    ],
)
def test_simple_day_offsets(phrase, expected):
    m = _resolve(f"提取{phrase}的天气数据")
    assert m is not None, f"no match for {phrase!r}"
    assert m.target == expected
    assert m.delta_months == 0


# ──────────────────────────────────────────────────────────────────────────
# 2. N-day arithmetic
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("3天后", date(2026, 5, 25)),
        ("三天后", date(2026, 5, 25)),
        ("7天前", date(2026, 5, 15)),
        ("七天之前", date(2026, 5, 15)),
        ("10天以后", date(2026, 6, 1)),       # crosses month boundary
        ("十天之后", date(2026, 6, 1)),
        ("30天后", date(2026, 6, 21)),
        ("in 5 days", date(2026, 5, 27)),
        ("in 2 days", date(2026, 5, 24)),
        ("10 days ago", date(2026, 5, 12)),
        ("1 day ago", date(2026, 5, 21)),
    ],
)
def test_n_day_arithmetic(phrase, expected):
    m = _resolve(f"查看{phrase}的报表")
    assert m is not None, f"no match for {phrase!r}"
    assert m.target == expected
    assert m.delta_months == 0


# ──────────────────────────────────────────────────────────────────────────
# 3. Month-relative + day  ← the user's "下个月15号" case + extensions
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "phrase,expected,delta_m",
    [
        # The user's original case
        ("下个月15号", date(2026, 6, 15), +1),
        ("下月15号", date(2026, 6, 15), +1),
        ("下个月的15号", date(2026, 6, 15), +1),
        # +2 month
        ("下下月3号", date(2026, 7, 3), +2),
        ("下下个月3号", date(2026, 7, 3), +2),
        # +3 month
        ("下下下月10号", date(2026, 8, 10), +3),
        # Negative direction
        ("上个月20号", date(2026, 4, 20), -1),
        ("上月5号", date(2026, 4, 5), -1),
        ("上上月12号", date(2026, 3, 12), -2),
        ("上上上个月1号", date(2026, 2, 1), -3),
        # Current month
        ("本月15号", date(2026, 5, 15), 0),
        ("这个月8号", date(2026, 5, 8), 0),
        # N-month arithmetic
        ("3个月后的5号", date(2026, 8, 5), +3),
        ("三个月后5号", date(2026, 8, 5), +3),
        ("6个月后10号", date(2026, 11, 10), +6),
        ("2个月前5号", date(2026, 3, 5), -2),
        # English
        ("the 15th of next month", date(2026, 6, 15), +1),
        ("next month, the 20th", date(2026, 6, 20), +1),
        ("last month, the 1st", date(2026, 4, 1), -1),
        ("in 2 months, the 3rd", date(2026, 7, 3), +2),
    ],
)
def test_month_relative_with_day(phrase, expected, delta_m):
    m = _resolve(phrase)
    assert m is not None, f"no match for {phrase!r}"
    assert m.target == expected, (
        f"{phrase!r} → got {m.target}, expected {expected}"
    )
    assert m.delta_months == delta_m


# ──────────────────────────────────────────────────────────────────────────
# 4. Month boundaries
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "phrase,expected,delta_m",
    [
        ("月底", date(2026, 5, 31), 0),
        ("本月最后一天", date(2026, 5, 31), 0),
        ("月初", date(2026, 5, 1), 0),
        ("下月底", date(2026, 6, 30), +1),
        ("下个月底", date(2026, 6, 30), +1),
        ("下月初", date(2026, 6, 1), +1),
        ("下下月底", date(2026, 7, 31), +2),
        ("上月底", date(2026, 4, 30), -1),
        ("上个月初", date(2026, 4, 1), -1),
        # English
        ("end of month", date(2026, 5, 31), 0),
        ("end of the month", date(2026, 5, 31), 0),
        ("start of month", date(2026, 5, 1), 0),
        ("beginning of the month", date(2026, 5, 1), 0),
    ],
)
def test_month_boundaries(phrase, expected, delta_m):
    m = _resolve(phrase)
    assert m is not None, f"no match for {phrase!r}"
    assert m.target == expected
    assert m.delta_months == delta_m


# ──────────────────────────────────────────────────────────────────────────
# 5. Relative weekday  (today=Friday 2026-05-22)
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "phrase,expected",
    [
        # This week: Mon=18, Tue=19, Wed=20, Thu=21, Fri=22, Sat=23, Sun=24
        ("本周一", date(2026, 5, 18)),
        ("本周三", date(2026, 5, 20)),
        ("本周五", date(2026, 5, 22)),  # today
        ("本周日", date(2026, 5, 24)),
        ("本周天", date(2026, 5, 24)),
        ("这周一", date(2026, 5, 18)),
        # Next week (May 25-31): Mon=25 ... Sun=31
        ("下周一", date(2026, 5, 25)),
        ("下周三", date(2026, 5, 27)),
        ("下周五", date(2026, 5, 29)),
        ("下周日", date(2026, 5, 31)),
        ("下星期三", date(2026, 5, 27)),
        ("下礼拜五", date(2026, 5, 29)),
        # Last week (May 11-17)
        ("上周一", date(2026, 5, 11)),
        ("上周五", date(2026, 5, 15)),
        # Two weeks
        ("下下周三", date(2026, 6, 3)),
        ("上上周五", date(2026, 5, 8)),
        # English
        ("this monday", date(2026, 5, 18)),
        ("next wednesday", date(2026, 5, 27)),
        ("last friday", date(2026, 5, 15)),
    ],
)
def test_relative_weekday(phrase, expected):
    m = _resolve(phrase)
    assert m is not None, f"no match for {phrase!r}"
    assert m.target == expected


# ──────────────────────────────────────────────────────────────────────────
# 6. Year-relative + month + day
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("明年6月15号", date(2027, 6, 15)),
        ("明年的6月15号", date(2027, 6, 15)),
        ("去年12月1号", date(2025, 12, 1)),
        ("后年1月1号", date(2028, 1, 1)),
        ("前年8月8号", date(2024, 8, 8)),
        ("今年12月25号", date(2026, 12, 25)),
    ],
)
def test_year_relative_with_month_day(phrase, expected):
    m = _resolve(phrase)
    assert m is not None, f"no match for {phrase!r}"
    assert m.target == expected


# ──────────────────────────────────────────────────────────────────────────
# 7. Cross-cutting: day-clamping, year-wrap, leap year
# ──────────────────────────────────────────────────────────────────────────
def test_day_clamping_jan_31_plus_one_month_lands_on_feb_last():
    """1月31日 + 1月 → 2月28/29日（不是 3月3日）."""
    m = resolve_relative_date("下个月31号", today=date(2026, 1, 31))
    assert m is not None
    assert m.target == date(2026, 2, 28)  # 2026 is non-leap


def test_day_clamping_jan_31_plus_one_month_in_leap_year():
    m = resolve_relative_date("下个月31号", today=date(2024, 1, 31))
    assert m is not None
    assert m.target == date(2024, 2, 29)


def test_year_wrap_december_plus_one_month():
    """12月 + 1月 → 次年 1月."""
    m = resolve_relative_date("下个月5号", today=date(2026, 12, 22))
    assert m is not None
    assert m.target == date(2027, 1, 5)
    assert m.delta_months == +1


def test_year_wrap_january_minus_one_month():
    m = resolve_relative_date("上个月15号", today=date(2026, 1, 22))
    assert m is not None
    assert m.target == date(2025, 12, 15)


def test_year_wrap_n_days_crosses_year_end():
    m = resolve_relative_date("30天后", today=date(2026, 12, 15))
    assert m is not None
    assert m.target == date(2027, 1, 14)


def test_month_boundary_february_in_leap_year():
    m = resolve_relative_date("月底", today=date(2024, 2, 5))
    assert m is not None
    assert m.target == date(2024, 2, 29)


def test_month_boundary_february_in_non_leap_year():
    m = resolve_relative_date("月底", today=date(2026, 2, 5))
    assert m is not None
    assert m.target == date(2026, 2, 28)


def test_weekday_when_today_is_target_weekday():
    """today=Friday, ask for 本周五 → today itself."""
    m = resolve_relative_date("本周五", today=date(2026, 5, 22))
    assert m is not None
    assert m.target == date(2026, 5, 22)


def test_weekday_when_today_is_monday():
    """today=Monday, 上周五 should be 4 days before."""
    today = date(2026, 5, 25)  # Monday
    m = resolve_relative_date("上周五", today=today)
    assert m is not None
    assert m.target == date(2026, 5, 22)  # previous Friday


# ──────────────────────────────────────────────────────────────────────────
# 8. Negative path — non-relative goals must NOT match
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "goal",
    [
        "提取员工表格前30行",                 # extract goal, no relative date
        "Click the 'Submit' button",
        "登录账号 admin 密码 123456",
        "在搜索框里输入 Python 教程",
        "选择 2026-05-20 这一天",            # absolute date — not relative
        "",
        "   ",
    ],
)
def test_non_relative_goals_return_none(goal):
    assert resolve_relative_date(goal, today=TODAY) is None


def test_resolver_does_not_eat_quantity_phrases():
    """Make sure 'top 30 results' doesn't get parsed as 30 days/months/etc."""
    assert resolve_relative_date("Top 30 results", today=TODAY) is None
    assert resolve_relative_date("前 50 条数据", today=TODAY) is None
    assert resolve_relative_date("抓取前26部电影", today=TODAY) is None


# ──────────────────────────────────────────────────────────────────────────
# 9. Disambiguation — most-specific matcher wins
# ──────────────────────────────────────────────────────────────────────────
def test_specific_month_with_day_wins_over_simple_today():
    """Goal mentions both 'today' and '下个月15号' → month-with-day wins."""
    m = _resolve("今天打开日历选下个月15号")
    assert m is not None
    assert m.target == date(2026, 6, 15)
    assert m.delta_months == +1


def test_proximity_does_not_cross_pollinate():
    """'本月15号' + '下个月底' → bind 15号 to 本月, not to 下个月."""
    m = _resolve("本月15号开始 下个月底完成")
    assert m is not None
    # First-match-wins: relative_month_with_day matcher iterates 下/上 first.
    # If 下个月 wins (no day in its proximity window), we'd fall to 本月.
    # If 本月 wins via 15号 in proximity, we get May 15.
    # Either is acceptable, but the result must not be "下个月15号" (June 15).
    assert m.target != date(2026, 6, 15), (
        "month-with-day cross-pollinated: bound '15号' to '下个月' instead of '本月'"
    )


# ──────────────────────────────────────────────────────────────────────────
# 10. Hint formatter
# ──────────────────────────────────────────────────────────────────────────
def test_format_resolver_hint_emits_marker_and_iso_date():
    m = _resolve("下个月15号")
    assert m is not None
    hint = format_resolver_hint(m)
    # Marker for select_skills fast-path
    assert "【相对日期解析】" in hint
    # ISO date for VLM matching
    assert "2026-06-15" in hint
    assert "2026年6月15日" in hint
    # Delta hint
    assert "+1" in hint


def test_format_resolver_hint_for_zero_delta():
    """Δ=0 cases should not show '与本月差值' to avoid noise."""
    m = _resolve("今天")
    assert m is not None
    hint = format_resolver_hint(m)
    assert "与本月差值" not in hint
    assert m.target.isoformat() in hint

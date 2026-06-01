"""Regression: RELATIVE_DATE_SKILL prompt is registered + injected via the
generic resolver.

Replaces the previous site-specific test_date_next_month_skill.py with a
suite that covers ALL relative-date phrasings the resolver supports —
not just "下个月". The trigger logic uses the resolver itself as ground
truth (via ``prompts._resolver_matches``), so new phrasings added to the
resolver automatically get the skill injected without a separate config
update.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_VWA = Path(__file__).resolve().parents[1] / "visual_web_agent"
sys.path.insert(0, str(_VWA))

from prompt_skills import RELATIVE_DATE_SKILL, SKILL_PROMPTS  # noqa: E402
from prompts import build_system_prompt  # noqa: E402


# ── Skill registration ────────────────────────────────────────────────────
def test_skill_is_registered_in_prompt_map():
    assert "relative_date" in SKILL_PROMPTS
    assert SKILL_PROMPTS["relative_date"] is RELATIVE_DATE_SKILL


def test_skill_content_pins_critical_rules():
    body = RELATIVE_DATE_SKILL
    # Must claim genericity over multiple phrase categories
    for category in ("今天", "下个月", "下周", "月底", "明年"):
        assert category in body, f"missing category {category!r}"
    # Must reference the system-injected hint marker (the load-bearing contract)
    assert "【相对日期解析】" in body
    # Must teach plain visual matching (not arithmetic)
    assert "绝对日期" in body
    assert "delta_months" in body or "差值" in body
    # Anti-patterns block must be present
    assert "反模式" in body or "❌" in body


# ── Trigger via system-injected hint marker (the most reliable path) ──────
def test_skill_injected_when_goal_contains_resolver_hint_marker():
    """When main.py has appended the resolver hint, the marker alone fires
    the skill — even if the original goal vocab somehow slipped past the
    keyword set."""
    rendered = build_system_prompt(
        goal=(
            "用户原始目标\n\n【相对日期解析】（系统自动计算...）\n"
            "· 识别短语: \"下个月15号\"\n· 绝对日期: 2026-06-15\n· 置信度: high"
        ),
        browser_state="",
    )
    assert RELATIVE_DATE_SKILL in rendered


# ── Trigger via resolver match (no marker, raw phrasing) ──────────────────
@pytest.mark.parametrize(
    "goal",
    [
        # User's original cases
        "Activity time 区域：在弹出的日历中选定下个月的 15 号",
        "找到日期输入框，选择下个月的 15 号",
        # Other relative-date phrasings — the resolver gates skill injection,
        # so all of these should pull in the skill automatically
        "选定下下月3号",
        "选择上个月20号",
        "请选择三个月后的5号",
        "提取明天的天气",
        "查看3天后的报表",
        "下周三的会议安排",
        "本月底之前完成",
        "明年6月15号是什么星期",
        "Pick the 15th of next month",
        "Select 3 days from now",
        "Choose next Wednesday",
        "End of month report",
    ],
)
def test_skill_injected_for_any_relative_date_phrasing(goal):
    rendered = build_system_prompt(goal=goal, browser_state="")
    assert RELATIVE_DATE_SKILL in rendered, (
        f"relative_date skill not injected for goal={goal!r}"
    )


# ── Negative path: plain goals must NOT pull this skill ───────────────────
@pytest.mark.parametrize(
    "goal",
    [
        # Plain extract — no relative date
        "提取员工表格前30行",
        "Top 50 hot HN posts about AI Agent",
        # Form filling — no date
        "填写姓名和邮箱然后提交",
        # Absolute date — not relative
        "选择 2026-05-20 这一天",
        "Click the date 2026-06-15",
        # Empty
        "",
    ],
)
def test_skill_not_injected_for_non_relative_goal(goal):
    rendered = build_system_prompt(goal=goal, browser_state="")
    assert RELATIVE_DATE_SKILL not in rendered, (
        f"relative_date skill UNEXPECTEDLY injected for goal={goal!r}"
    )


# ── Cross-cutting: FORM_SKILL is co-injected ──────────────────────────────
def test_form_skill_co_injected_for_date_goal():
    """The relative-date block leans on FORM_SKILL's calendar verification
    rules ("read input value to confirm")."""
    from prompt_skills import FORM_SKILL

    rendered = build_system_prompt(
        goal="弹出日历后选下个月15号",  # no form-vocab; tests the co-injection
        browser_state="",
    )
    assert RELATIVE_DATE_SKILL in rendered
    assert FORM_SKILL in rendered


def test_form_skill_not_duplicated_when_form_already_triggered():
    from prompt_skills import FORM_SKILL

    rendered = build_system_prompt(
        goal="填写表单，活动时间选下个月15号",
        browser_state="",
    )
    assert rendered.count(FORM_SKILL) == 1
    assert RELATIVE_DATE_SKILL in rendered


# ── Resolver-as-trigger contract ─────────────────────────────────────────
def test_resolver_match_alone_triggers_skill_without_keyword_in_goal():
    """A goal phrased purely with a recognised English relative-date phrase
    that's not in the keyword fallback should still trigger the skill via
    the resolver-call path."""
    # "in 14 days" is in the keyword list, but let's pick a wording NOT in
    # the fallback to verify the resolver pathway works:
    # "this monday" is in the resolver but the keyword list only has "this week"
    rendered = build_system_prompt(
        goal="schedule a meeting for this monday",
        browser_state="",
    )
    assert RELATIVE_DATE_SKILL in rendered

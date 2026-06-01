"""Regression: chat / assistant goals must load CHAT_ENTRY_SKILL.

Failure pattern (run_log_20260514_183718):
  Goal: "...找到文心助手接口，点击进去...输入框中输入'介绍一下deepseek'..."

  VLM clicked a Baidu home page link labelled "文心", landed on a page
  with a search-shaped input box, typed the question + Enter — Enter
  triggered a Baidu SEARCH (not Wenxin chat). Whole task derailed.

Fix: ``CHAT_ENTRY_SKILL`` prompt + ``_CHAT_ENTRY_TRIGGERS`` so when goal
mentions "助手/对话/聊天/介绍一下/etc.", VLM gets a verify-the-page-is-
chat-before-Enter rule.
"""

from __future__ import annotations

import pytest

from visual_web_agent.prompt_skills import CHAT_ENTRY_SKILL, SKILL_PROMPTS
from visual_web_agent.prompts import _CHAT_ENTRY_TRIGGERS, build_system_prompt


# ── Skill content ────────────────────────────────────────────────────────────
def test_chat_entry_skill_registered() -> None:
    """The new skill is in the dispatch table."""
    assert "chat_entry" in SKILL_PROMPTS
    assert SKILL_PROMPTS["chat_entry"] == CHAT_ENTRY_SKILL


def test_chat_entry_skill_warns_about_search_box_trap() -> None:
    """Core warning: search-shaped input on landing page is NOT chat."""
    assert "search" in CHAT_ENTRY_SKILL.lower() or "搜索" in CHAT_ENTRY_SKILL


def test_chat_entry_skill_demands_url_verification() -> None:
    """Concrete actionable rule: check URL before typing."""
    # Mentions URL inspection
    assert "URL" in CHAT_ENTRY_SKILL
    # Lists concrete domain markers VLM should look for
    for marker in ("yiyan", "chat"):
        assert marker in CHAT_ENTRY_SKILL.lower()


def test_chat_entry_skill_provides_recovery_actions() -> None:
    """If not a real chat page → tells VLM the alternatives."""
    assert "goto" in CHAT_ENTRY_SKILL
    # Discourages blind type + Enter
    assert "type" in CHAT_ENTRY_SKILL


def test_chat_entry_skill_includes_real_world_failure_examples() -> None:
    """Documenting the actual failure helps VLM pattern-match in future."""
    # The 18:37 specific scenario gets called out
    assert "百度" in CHAT_ENTRY_SKILL or "Baidu" in CHAT_ENTRY_SKILL
    assert "文心" in CHAT_ENTRY_SKILL or "Wenxin" in CHAT_ENTRY_SKILL


# ── Trigger dispatch via build_system_prompt ─────────────────────────────────
@pytest.mark.parametrize(
    "goal_phrase",
    [
        "找到文心助手接口",
        "用通义对话回答",
        "让 AI 介绍一下 DeepSeek",
        "在豆包聊天框里问问",
        "Open Claude and ask about Python",
        "use ChatGPT to summarize",
        "打开 kimi，输入问题",
    ],
)
def test_chat_entry_triggers_load_the_skill(goal_phrase: str) -> None:
    """Each phrase exercises a different trigger; all must load chat_entry."""
    prompt = build_system_prompt(goal=goal_phrase)
    # The skill content should be in the assembled prompt
    assert "Chat / Assistant Entry Verification" in prompt, (
        f"chat_entry skill not loaded for goal: {goal_phrase!r}"
    )


def test_chat_entry_not_loaded_for_unrelated_goal() -> None:
    """Pure extraction / form / login goals must NOT load chat_entry
    (would just add noise to those prompts)."""
    prompt = build_system_prompt(goal="在表单里填写姓名、手机号、地址并提交")
    assert "Chat / Assistant Entry Verification" not in prompt


def test_chat_entry_triggers_cover_chinese_and_english_brands() -> None:
    """Sanity: triggers list covers the major assistants."""
    triggers_lower = {t.lower() for t in _CHAT_ENTRY_TRIGGERS}
    # Chinese brands
    for brand in ("文心", "通义", "豆包"):
        assert brand in _CHAT_ENTRY_TRIGGERS, f"missing trigger: {brand}"
    # English brands
    for brand in ("chatgpt", "claude", "gemini"):
        assert brand in triggers_lower, f"missing trigger: {brand}"
    # Generic verbs
    for verb in ("助手", "对话", "聊天"):
        assert verb in _CHAT_ENTRY_TRIGGERS, f"missing trigger: {verb}"


def test_chat_entry_triggers_match_18_37_failed_goal() -> None:
    """Direct regression: the 18:37 goal text must trigger the skill."""
    goal = (
        "打开页面，找到文心助手接口，点击进去，会跳转到一个新页面，"
        "然后再文心助手正中间输入框中输入'介绍一下deepseek'，"
        "然后回车或者点击发送的图案，获取文心助手回答的内容。"
    )
    prompt = build_system_prompt(goal=goal)
    assert "Chat / Assistant Entry Verification" in prompt

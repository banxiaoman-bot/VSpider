"""Prompt-skill regression for the ``page_to_markdown`` capability.

The skill block must be registered in ``SKILL_PROMPTS`` and injected into the
system prompt for markdown / readability / RAG goals (中英文), while staying
out of unrelated goals to avoid prompt bloat.
"""

from __future__ import annotations

from visual_web_agent.prompt_skills import PAGE_TO_MARKDOWN_SKILL, SKILL_PROMPTS
from visual_web_agent.prompts import build_system_prompt


_MARKER = "Page → Fit Markdown"


class TestSkillRegistration:
    def test_registered_in_skill_map(self) -> None:
        assert "page_to_markdown" in SKILL_PROMPTS
        assert SKILL_PROMPTS["page_to_markdown"] is PAGE_TO_MARKDOWN_SKILL

    def test_skill_mentions_action_name(self) -> None:
        assert "page_to_markdown" in PAGE_TO_MARKDOWN_SKILL


class TestSkillInjection:
    def test_injected_for_chinese_goal(self) -> None:
        prompt = build_system_prompt(goal="把当前页面转成 markdown 正文喂给大模型", browser_state="")
        assert _MARKER in prompt

    def test_injected_for_english_goal(self) -> None:
        prompt = build_system_prompt(goal="give me the readable main content for RAG", browser_state="")
        assert _MARKER in prompt

    def test_not_injected_for_unrelated_goal(self) -> None:
        prompt = build_system_prompt(goal="点击登录按钮", browser_state="")
        assert _MARKER not in prompt

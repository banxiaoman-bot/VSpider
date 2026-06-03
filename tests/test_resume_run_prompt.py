"""Prompt-skill regression for the ``resume_run`` capability (RUN-RESUME1 step 3).

The skill block must be registered in ``SKILL_PROMPTS`` and injected into the
system prompt for resume / 断点续跑 goals (中英文), while staying out of
unrelated goals to avoid prompt bloat.
"""

from __future__ import annotations

from visual_web_agent.prompt_skills import RESUME_RUN_SKILL, SKILL_PROMPTS
from visual_web_agent.prompts import build_system_prompt


_MARKER = "Skill: Resume Run"


class TestSkillRegistration:
    def test_registered_in_skill_map(self) -> None:
        assert "resume_run" in SKILL_PROMPTS
        assert SKILL_PROMPTS["resume_run"] is RESUME_RUN_SKILL

    def test_skill_mentions_action_name(self) -> None:
        assert "resume_run" in RESUME_RUN_SKILL


class TestSkillInjection:
    def test_injected_for_chinese_goal(self) -> None:
        prompt = build_system_prompt(goal="接着上次断点续跑，继续上次没抓完的任务", browser_state="")
        assert _MARKER in prompt

    def test_injected_for_english_goal(self) -> None:
        prompt = build_system_prompt(goal="resume the previous run where it left off", browser_state="")
        assert _MARKER in prompt

    def test_not_injected_for_unrelated_goal(self) -> None:
        prompt = build_system_prompt(goal="点击登录按钮", browser_state="")
        assert _MARKER not in prompt

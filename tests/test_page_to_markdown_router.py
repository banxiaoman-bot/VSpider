"""Routing regression for the ``page_to_markdown`` capability.

The capability router should recognise markdown / readability / RAG goals
(中英文) and surface a ``page_to_markdown`` step in the deterministic backend
plan, while leaving unrelated goals untouched.
"""

from __future__ import annotations

from visual_web_agent.capability_router import _backend_plan, _signals


class TestMarkdownSignal:
    def test_chinese_goal_flags_markdown(self) -> None:
        sig = _signals("把这个页面转成 markdown，正文提取喂给大模型", {})
        assert sig["markdown_preferred"] is True

    def test_english_goal_flags_markdown(self) -> None:
        sig = _signals("convert this page to readable markdown for RAG", {})
        assert sig["markdown_preferred"] is True

    def test_unrelated_goal_not_flagged(self) -> None:
        sig = _signals("click the submit button and login", {})
        assert sig["markdown_preferred"] is False


class TestMarkdownPlan:
    def test_plan_includes_page_to_markdown(self) -> None:
        sig = _signals("convert page to markdown for rag", {})
        plan = _backend_plan(sig, {}, [])
        names = [step.get("name") for step in plan]
        assert "page_to_markdown" in names

    def test_unrelated_plan_excludes_page_to_markdown(self) -> None:
        sig = _signals("click the submit button", {})
        plan = _backend_plan(sig, {}, [])
        names = [step.get("name") for step in plan]
        assert "page_to_markdown" not in names

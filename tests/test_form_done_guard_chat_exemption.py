"""Regression: FORM DONE GUARD must not block chat-task `done` decisions.

Failure that motivated this (run_log_20260518_123412):
  Goal: "打开页面，在中间输入框中输入'介绍一下deepseek'，然后回车...
        获取ai返回的内容。"
  The goal contains "输入框" → ``_goal_is_form_fill`` returned True.
  ``_prepare_form_batch_fields`` then carved "打开页面，在中间" as a
  field label (the substring before "输入框").
  Every ``action=done`` from step 6 onward got blocked by FORM DONE GUARD
  because no input on the chat page is named "打开页面，在中间".
  Result: 14 wasted steps, MAX_STEPS exhausted, task failed despite the
  AI answer being on screen from step 5.

Fix:
  - ``_goal_is_chat_task`` recognises chat brand/intent vocabulary.
  - ``_goal_is_form_fill`` early-returns False for chat tasks.
  - FORM DONE GUARD itself has a chat-host / chat_extract-in-trail bypass
    in case a future goal-parser refactor accidentally re-classifies a
    chat goal as form-fill.
"""

from __future__ import annotations

import pytest

from visual_web_agent.main import (
    _goal_is_chat_task,
    _goal_is_form_fill,
    _prepare_form_batch_fields,
)


# ── _goal_is_chat_task positive cases ───────────────────────────────────────


@pytest.mark.parametrize(
    "goal",
    [
        # Exact goal from the failing trajectory
        "打开页面，在中间输入框中输入'介绍一下deepseek'，然后回车或者点击发送的图案，获取ai返回的内容。",
        # Earlier brand-routed variant
        "打开页面，找到文心助手接口，点击进去，会跳转到一个新页面，然后再文心助手正中间输入框中输入'介绍一下deepseek'",
        # Brand-only
        "问 ChatGPT: 翻译一下 hello world",
        "用 Claude 帮我写一段 Python 代码",
        "通义 解释一下 Transformer 的注意力机制",
        "Kimi 总结一下这段文字",
        "豆包 写首七律",
        "智谱清言 介绍一下中国唐诗",
        "腾讯元宝 帮我翻译这段英文",
        "Perplexity search for transformer history",
        "ask the assistant about quantum computing",
        # Intent-only (no brand named)
        "在对话框输入问题然后获取AI回答内容",
        "聊天框中提问然后读取助手回答",
    ],
)
def test_chat_task_detected(goal: str) -> None:
    assert _goal_is_chat_task(goal) is True, f"chat goal not detected: {goal!r}"
    # And the form-fill detector MUST yield False on the same input
    assert _goal_is_form_fill(goal) is False, (
        f"chat goal mis-classified as form-fill: {goal!r}"
    )


# ── _goal_is_chat_task negative cases ───────────────────────────────────────


@pytest.mark.parametrize(
    "goal",
    [
        # Real form-fill tasks must stay form=True
        "在RPA Challenge上完成10轮表单填写，提交后下载结果",
        "在demoqa填写注册表 firstName=Tom lastName=Lee",
        "Activity name=Hello, Activity zone=Zone one，然后点 Submit",
        "填写基本form: 用户名=admin, 密码=123",
        "Fill basic form with username and password and click submit",
        "在表单中填入name=张三 phone=13800000000",
    ],
)
def test_form_fill_not_misclassified_as_chat(goal: str) -> None:
    assert _goal_is_chat_task(goal) is False, (
        f"form goal mis-classified as chat: {goal!r}"
    )
    assert _goal_is_form_fill(goal) is True, (
        f"real form goal lost its form-fill flag: {goal!r}"
    )


@pytest.mark.parametrize(
    "goal",
    [
        # Bare extraction / navigation goals — neither chat nor form
        "提取这页表格的全部数据保存到 Excel",
        "点击下一页按钮直到末页",
        "打开微博热搜榜并截图",
        "登录后查看个人资料",
    ],
)
def test_neutral_goals_are_neither_chat_nor_form(goal: str) -> None:
    assert _goal_is_chat_task(goal) is False
    assert _goal_is_form_fill(goal) is False


# ── End-to-end pipeline: the failing trajectory's exact goal ────────────────


def test_failing_trajectory_goal_does_not_set_form_assignments() -> None:
    """The full failure chain: form_fill=False ⇒ _form_assignments stays
    empty ⇒ FORM DONE GUARD's outer ``if _form_assignments`` skips the
    block ⇒ done is accepted."""
    goal = (
        "打开页面，在中间输入框中输入'介绍一下deepseek'，"
        "然后回车或者点击发送的图案，获取ai返回的内容。"
    )
    assert _goal_is_form_fill(goal) is False
    # The orchestrator wraps _prepare_form_batch_fields in
    # ``if _is_form_fill_goal else {}`` — so the chat task never even
    # reaches the buggy "field-label = garbage" map. The mapping function
    # is still called in this test to document the (still-broken) raw
    # behaviour we are deliberately avoiding via the upstream gate.
    raw_fields = _prepare_form_batch_fields(goal)
    # Sanity: the raw parser DOES still produce a garbage label — that's
    # the pre-existing failure mode. Our fix is at the higher gate.
    assert "打开页面，在中间" in raw_fields or "打开页面，在中间" in (
        next(iter(raw_fields), "") if raw_fields else ""
    )


# ── Brand alias coverage ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "marker",
    [
        "文心", "通义", "豆包", "kimi", "moonshot", "智谱", "chatglm",
        "元宝", "deepseek", "yiyan", "tongyi", "qwen",
        "chatgpt", "claude", "anthropic", "gemini", "copilot", "perplexity",
    ],
)
def test_chat_brand_keywords_match(marker: str) -> None:
    goal = f"请打开 {marker} 然后帮我处理这个问题"
    assert _goal_is_chat_task(goal) is True, (
        f"brand keyword {marker!r} not recognised"
    )

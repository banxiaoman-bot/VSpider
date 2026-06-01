"""Regression: Planner must not add login-probe subgoals for chat tasks.

Failure that motivated this (run_log_20260518_125806):
  Goal: "打开页面，在中间输入框中输入'介绍一下deepseek'，
        然后回车或者点击发送的图案，获取ai返回的内容。"
  Planner produced 5 subgoals, the FIRST being
    "1. 探测当前页面是否已登录百度文心一言"
  → Step 1 VLM dutifully clicked the "登录" button.
  → Page swapped to the SMS/captcha login form.
  → User Ctrl-C'd before things got worse.

The root cause is the Planner LLM over-applying its "login wall detection"
rule to chat tasks where the chat input is usable WITHOUT login. The fix:
  1. Planner prompt: rule 6b/6c explicitly forbid login-probe subgoals
     on chat tasks and remind the LLM that runtime guards handle login
     walls — the plan layer should NOT defend against them.
  2. Belt-and-suspenders post-Planner filter in ``VLMClient.make_plan``:
     after parsing the LLM's output, if the goal is a chat task,
     drop any subgoal whose description matches "login probe" patterns.

This test covers the post-filter. The prompt-rule change is implicit —
even if the LLM ignores the prompt and produces a login subgoal, the
filter strips it.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from visual_web_agent.vlm_client import VLMClient


def _build_mock_client(raw_plan_json: str) -> VLMClient:
    v = VLMClient.__new__(VLMClient)
    v.semantic_model = "qwen-vl-max"
    v.semantic_client = MagicMock()
    v.client = v.semantic_client
    v.model = v.semantic_model
    v.max_tokens = 4096
    v._use_structured = True

    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content=raw_plan_json))]

    async def _create(*_a, **_kw):
        return mock_response

    v.semantic_client.chat = MagicMock()
    v.semantic_client.chat.completions = MagicMock()
    v.semantic_client.chat.completions.create = AsyncMock(side_effect=_create)
    return v


def _run(coro):
    return asyncio.run(coro)


# ── Chat task: login-probe subgoal must be dropped ──────────────────────────


def test_login_probe_dropped_for_chat_task() -> None:
    raw = json.dumps({
        "goal": "test",
        "sub_goals": [
            {"id": 1, "description": "探测当前页面是否已登录百度文心一言",
             "exit_criteria": "若登录页则abort", "status": "pending"},
            {"id": 2, "description": "在输入框输入 '介绍一下deepseek'",
             "exit_criteria": "输入框已显示文本", "status": "pending"},
            {"id": 3, "description": "按回车提交",
             "exit_criteria": "AI 回答开始", "status": "pending"},
            {"id": 4, "description": "等待 AI 流式回复结束",
             "exit_criteria": "回答稳定", "status": "pending"},
            {"id": 5, "description": "提取 AI 返回内容",
             "exit_criteria": "extracted_data 完整", "status": "pending"},
        ],
        "current_idx": 0,
    })
    v = _build_mock_client(raw)
    goal = "打开页面，在中间输入框中输入'介绍一下deepseek'，然后回车，获取ai返回的内容"
    plan = _run(v.make_plan(goal, "https://yiyan.baidu.com"))

    assert len(plan.sub_goals) == 4, (
        f"login-probe subgoal not dropped: {[sg.description for sg in plan.sub_goals]}"
    )
    descs = [sg.description for sg in plan.sub_goals]
    assert all("探测" not in d or "登录" not in d for d in descs), (
        f"login-probe subgoal still present: {descs}"
    )
    # IDs must be re-sequenced 1..N
    assert [sg.id for sg in plan.sub_goals] == [1, 2, 3, 4]
    # First subgoal flips to active
    assert plan.sub_goals[0].status == "active"
    assert plan.current_idx == 0


@pytest.mark.parametrize(
    "login_desc",
    [
        "探测当前页面是否已登录",
        "检查是否需要登录",
        "确认登录状态",
        "判断是否处于已登陆状态",
        "Probe login state",
        "Check whether user is signed in",
        "Verify auth before continuing",
    ],
)
def test_various_login_probe_phrasings_dropped(login_desc: str) -> None:
    raw = json.dumps({
        "goal": "test",
        "sub_goals": [
            {"id": 1, "description": login_desc,
             "exit_criteria": "...", "status": "pending"},
            {"id": 2, "description": "输入问题并提交",
             "exit_criteria": "...", "status": "pending"},
            {"id": 3, "description": "提取 AI 回答",
             "exit_criteria": "...", "status": "pending"},
        ],
        "current_idx": 0,
    })
    v = _build_mock_client(raw)
    plan = _run(v.make_plan(
        "用 ChatGPT 翻译一下 hello world", "https://chat.openai.com"
    ))
    descs = [sg.description for sg in plan.sub_goals]
    assert login_desc not in descs, (
        f"login-probe '{login_desc}' should have been dropped, got: {descs}"
    )


# ── Non-chat task: login-probe subgoals MUST stay (no regression) ───────────


def test_login_probe_kept_for_non_chat_task() -> None:
    """Real form-fill / private-content tasks legitimately need the login
    probe. The filter must not touch them."""
    raw = json.dumps({
        "goal": "test",
        "sub_goals": [
            {"id": 1, "description": "探测当前页面是否已登录",
             "exit_criteria": "若登录页则 abort", "status": "pending"},
            {"id": 2, "description": "进入个人订单页",
             "exit_criteria": "订单列表已加载", "status": "pending"},
            {"id": 3, "description": "提取最近 5 条订单",
             "exit_criteria": "extracted_data 包含 5 条", "status": "pending"},
        ],
        "current_idx": 0,
    })
    v = _build_mock_client(raw)
    plan = _run(v.make_plan(
        "登录后查看个人订单列表", "https://example.com/account"
    ))
    descs = [sg.description for sg in plan.sub_goals]
    assert any("探测" in d and "登录" in d for d in descs), (
        f"non-chat task lost its login-probe subgoal: {descs}"
    )
    assert len(plan.sub_goals) == 3


# ── Defence in depth: filter only fires when chat goal AND login subgoal ────


def test_chat_task_without_login_probe_passes_unchanged() -> None:
    """If the LLM already obeyed the prompt and didn't emit a login subgoal,
    the plan must pass through untouched (same length, same IDs)."""
    raw = json.dumps({
        "goal": "test",
        "sub_goals": [
            {"id": 1, "description": "在文心助手输入问题",
             "exit_criteria": "...", "status": "pending"},
            {"id": 2, "description": "等待并提取 AI 回答",
             "exit_criteria": "...", "status": "pending"},
        ],
        "current_idx": 0,
    })
    v = _build_mock_client(raw)
    plan = _run(v.make_plan(
        "问文心：介绍一下 deepseek", "https://yiyan.baidu.com"
    ))
    assert len(plan.sub_goals) == 2
    assert [sg.id for sg in plan.sub_goals] == [1, 2]


def test_only_login_subgoal_for_chat_task_keeps_at_least_one() -> None:
    """Edge: if the LLM somehow only produced a login subgoal (and nothing
    else), the filter must NOT empty the plan to zero subgoals (would
    crash downstream). Either keep it or fall through to fallback."""
    raw = json.dumps({
        "goal": "test",
        "sub_goals": [
            {"id": 1, "description": "探测是否已登录",
             "exit_criteria": "...", "status": "pending"},
        ],
        "current_idx": 0,
    })
    v = _build_mock_client(raw)
    plan = _run(v.make_plan(
        "问 ChatGPT 一些问题", "https://chat.openai.com"
    ))
    # We require at least 1 subgoal so the plan layer stays consistent.
    assert len(plan.sub_goals) >= 1


# ── Planner prompt: rule 6b/6c must mention chat exemption ──────────────────


def test_planner_prompt_documents_chat_login_exemption() -> None:
    """If a future prompt refactor strips the chat-login exemption text,
    this catches it — the LLM-side guard would silently regress."""
    from visual_web_agent.prompts import build_plan_prompts
    system, _user = build_plan_prompts(
        "问 ChatGPT 介绍 DeepSeek", "https://chat.openai.com"
    )
    # The rule must mention the chat-task exemption and forbid login probe
    assert "聊天" in system or "chat" in system.lower()
    # And it must explicitly call out the no-login-probe rule
    assert "探测登录" in system or "检查登录" in system or "no login probe" in system.lower()

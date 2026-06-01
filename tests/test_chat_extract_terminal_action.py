"""Regression: chat_extract is a TERMINAL action — main.py must end the
task when an answer was captured, instead of waiting for VLM to emit done.

Failure that motivated this (run_log_20260518_141728):
  step 4: VLM emits chat_extract → handler writes answer to
          workflow_memory['ai_response_deepseek'] ✓
  step 5: VLM looks at screen, sees the same answer, emits chat_extract
          AGAIN with a slightly different memory_key.
  step 6/7/9/12/14/15/16/17: 8 more chat_extract iterations.
  step 18: MAX_STEPS hit. Task technically "completed" but ran 14 wasted
           steps for a 4-step task.

The structural fix:
  - ``ChatExtractHandler`` sets ``workflow_memory['__chat_extract_completed']``
    when an answer is captured.
  - main.py's dispatch loop checks for this sentinel right after the
    chat_extract action runs, sets ``_task_completed = True``, and breaks.
  - The next chat_extract in the same batch won't fire — sentinel only
    triggers when the just-executed action was chat_extract AND there's
    an answer.

This test pins the contract at the handler boundary (sentinel write).
The dispatch-level integration is exercised by manual runs.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


from visual_web_agent.actions import ChatExtractHandler


def _run(coro):
    return asyncio.run(coro)


def _build_handler_ctx(*, answer_text: str, action_memory_key: str = "ai_answer"):
    """Build a ctx where the JS extractor returns the given answer."""
    page = SimpleNamespace(
        url="https://yiyan.baidu.com/chat/abc",
        evaluate=AsyncMock(return_value={
            "method": "selector:[class*='ai-answer']",
            "selector": "[class*='ai-answer']",
            "text": answer_text,
            "length": len(answer_text),
            "html_len": len(answer_text) + 20,
            "title": "文心一言",
            "url": "https://yiyan.baidu.com/chat/abc",
            "streaming": False,
            "candidates_scanned": 1,
        }),
    )
    browser = SimpleNamespace()
    browser._last_action_error = None
    browser.rpa_trail = []
    action = SimpleNamespace(
        action="chat_extract",
        target_id=0,
        type_value="",
        memory_key=action_memory_key,
        extracted_data=None,
    )
    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = page
    ctx.workflow_memory = {}
    ctx.action = action
    ctx.with_rpa_meta = lambda d: d
    return ctx, browser, page, action


# ── Sentinel written on successful chat_extract ─────────────────────────────


def test_handler_writes_completion_sentinel_on_success() -> None:
    """When chat_extract captures a non-empty answer, the handler must set
    workflow_memory['__chat_extract_completed'] so main.py knows to stop."""
    long_answer = "DeepSeek 是一家中国 AI 公司，专注于大语言模型研发。" * 8
    ctx, _, _, _ = _build_handler_ctx(answer_text=long_answer, action_memory_key="ai_ans")
    _run(ChatExtractHandler().execute(ctx))

    sentinel = ctx.workflow_memory.get("__chat_extract_completed")
    assert sentinel is not None, "completion sentinel was not written"
    assert sentinel.get("memory_key") == "ai_ans"
    assert int(sentinel.get("length") or 0) >= 80
    assert sentinel.get("method", "").startswith("selector:")


def test_sentinel_carries_correct_memory_key_for_each_call() -> None:
    """Each chat_extract call's sentinel must point at THIS call's
    memory_key — so main.py reads the right entry from workflow_memory."""
    ctx, _, _, _ = _build_handler_ctx(
        answer_text="x" * 200, action_memory_key="wenxin_answer_1",
    )
    _run(ChatExtractHandler().execute(ctx))
    sentinel = ctx.workflow_memory["__chat_extract_completed"]
    assert sentinel["memory_key"] == "wenxin_answer_1"
    # And the actual stored answer is reachable under that key
    assert "ai" in (ctx.workflow_memory.get("wenxin_answer_1") or {}).get("answer", "").lower() or \
           ctx.workflow_memory.get("wenxin_answer_1", {}).get("answer", "")


def test_handler_does_not_write_sentinel_on_failure() -> None:
    """If chat_extract failed to find an answer (e.g. blank page), the
    sentinel must NOT be set — task should not falsely complete."""
    page = SimpleNamespace(
        url="https://opaque.example/",
        evaluate=AsyncMock(return_value={
            "method": "none", "selector": None, "text": "", "length": 0,
            "html_len": 0, "title": "", "url": "https://opaque.example/",
            "streaming": False, "candidates_scanned": 5,
        }),
    )
    browser = SimpleNamespace()
    browser._last_action_error = None
    browser.rpa_trail = []
    action = SimpleNamespace(
        action="chat_extract", target_id=0, type_value="", memory_key="x",
        extracted_data=None,
    )
    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = page
    ctx.workflow_memory = {}
    ctx.action = action
    ctx.with_rpa_meta = lambda d: d

    # Short timeout so we don't poll forever
    action.type_value = '{"timeout": 1.0, "min_length": 80}'
    _run(ChatExtractHandler().execute(ctx))

    assert "__chat_extract_completed" not in ctx.workflow_memory


def test_sentinel_length_matches_answer_length() -> None:
    answer = "DeepSeek 是一家中国 AI 公司。" * 6
    ctx, _, _, _ = _build_handler_ctx(answer_text=answer, action_memory_key="ans")
    _run(ChatExtractHandler().execute(ctx))
    sentinel = ctx.workflow_memory["__chat_extract_completed"]
    assert sentinel["length"] == len(answer)


def test_sentinel_records_url() -> None:
    ctx, _, _, _ = _build_handler_ctx(answer_text="A" * 200, action_memory_key="x")
    _run(ChatExtractHandler().execute(ctx))
    assert ctx.workflow_memory["__chat_extract_completed"]["url"]


# ── Sentinel format (for main.py readers) ───────────────────────────────────


def test_sentinel_is_jsonable() -> None:
    """main.py logs the sentinel via _broadcast_log_safe — it must be
    JSON-serialisable, or the broadcast will silently drop it."""
    ctx, _, _, _ = _build_handler_ctx(answer_text="x" * 100)
    _run(ChatExtractHandler().execute(ctx))
    json.dumps(ctx.workflow_memory["__chat_extract_completed"], ensure_ascii=False)

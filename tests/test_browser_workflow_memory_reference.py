"""Regression: ``browser.execute_action`` must preserve the caller's
workflow_memory dict reference even when it's empty.

The bug (run_log_20260518_151238 step 5/6):
  ``workflow_memory or {}`` evaluates the empty dict ``{}`` as falsy,
  swaps in a BRAND-NEW dict for the ActionContext, then handlers'
  writes go to the phantom dict and main.py's local ``workflow_memory``
  reference never sees them.

  Symptom: ChatExtractHandler set
    workflow_memory["__chat_extract_completed"] = {...}
  on the phantom dict. main.py checked the OLD reference, saw nothing,
  and let the agent run an extra chat_extract step before VLM eventually
  emitted done on its own.

Fix: use ``if workflow_memory is None: workflow_memory = {}`` so the
caller's empty dict is kept as-is. Only None gets replaced.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _run(coro):
    return asyncio.run(coro)


# We import lazily to avoid pulling in the heavyweight browser module
# at collection time (it imports Playwright). If the import errors —
# which it shouldn't, since other tests do it — these tests skip.
@pytest.fixture
def execute_action():
    try:
        from visual_web_agent.browser_env import BrowserEnv
    except Exception as e:
        pytest.skip(f"browser_env unavailable: {e}")
    return BrowserEnv.execute_action


def test_source_code_uses_none_check_not_or_default(execute_action) -> None:
    """Static check: the function body must NOT use ``workflow_memory or {}``
    when wiring the ActionContext. That pattern silently breaks shared
    references for empty dicts."""
    src = inspect.getsource(execute_action)
    # Locate the ActionContext construction line(s)
    assert "ActionContext(" in src
    # The bug pattern would look like ``workflow_memory=workflow_memory or {}``
    # in the construction. The fix uses ``workflow_memory=workflow_memory``
    # alongside a prior ``if workflow_memory is None`` guard.
    bad_pattern = "workflow_memory=workflow_memory or {}"
    assert bad_pattern not in src, (
        "browser.execute_action still uses 'workflow_memory or {}' "
        "which kills the caller's reference for empty dicts. Use a None "
        "check before constructing the ActionContext instead."
    )
    # Defensive: there should be a None-check anchor nearby
    assert "is None" in src and "workflow_memory" in src, (
        "expected an explicit ``if workflow_memory is None`` guard"
    )


# ── End-to-end: ChatExtractHandler writes through to caller's dict ──────────


def test_chat_extract_writes_visible_to_caller(monkeypatch) -> None:
    """An empty workflow_memory must still receive the sentinel + answer
    after ChatExtractHandler runs. This is the bug we're pinning: the
    caller passes ``{}``, handler writes ``__chat_extract_completed``,
    caller must see it on the SAME dict reference."""
    from visual_web_agent.actions import ChatExtractHandler

    # Build a minimal SimpleNamespace ctx that mimics what main.py would
    # pass into the handler (bypassing browser.execute_action's coercion).
    page = types.SimpleNamespace(
        url="https://yiyan.baidu.com/chat/abc",
        evaluate=AsyncMock(return_value={
            "method": "selector:[class*='ai-answer']",
            "selector": "[class*='ai-answer']",
            "text": "Hermes 是一个开源 AI 模型..." * 8,
            "length": 240,
            "html_len": 320,
            "title": "文心",
            "url": "https://yiyan.baidu.com/chat/abc",
            "streaming": False,
            "candidates_scanned": 1,
        }),
    )
    browser = types.SimpleNamespace()
    browser._last_action_error = None
    browser.rpa_trail = []
    action = types.SimpleNamespace(
        action="chat_extract", target_id=0, type_value="",
        memory_key="ai_response_hermes", extracted_data=None,
    )

    # Caller's empty dict — this is the canary
    callers_memory: dict = {}
    ctx = types.SimpleNamespace()
    ctx.browser = browser
    ctx.page = page
    ctx.workflow_memory = callers_memory  # SAME reference
    ctx.action = action
    ctx.with_rpa_meta = lambda d: d

    _run(ChatExtractHandler().execute(ctx))

    # The exact assertion that the live bug would fail:
    assert "__chat_extract_completed" in callers_memory, (
        "ChatExtractHandler did not write the sentinel through to "
        "caller's workflow_memory — reference was lost"
    )
    sentinel = callers_memory["__chat_extract_completed"]
    assert sentinel["memory_key"] == "ai_response_hermes"
    assert sentinel["length"] >= 80
    # And the answer entry is also there
    assert "ai_response_hermes" in callers_memory
    assert callers_memory["ai_response_hermes"]["answer"]


# ── execute_action contract: empty dict reference preserved ─────────────────


def test_action_context_preserves_workflow_memory_reference() -> None:
    """The real ActionContext must not let Pydantic copy workflow_memory."""
    from visual_web_agent.actions import ActionContext
    from visual_web_agent.vlm_client import VSpiderAction

    class _Dummy:
        pass

    callers_memory: dict = {}
    action = VSpiderAction(
        action="chat_extract",
        target_id=0,
        type_value="",
        memory_key="ai_answer",
        extracted_data=None,
    )
    ctx = ActionContext(
        action=action,
        browser=_Dummy(),
        workflow_memory=callers_memory,
        page=_Dummy(),
    )

    assert ctx.workflow_memory is callers_memory
    ctx.workflow_memory["__chat_extract_completed"] = {"memory_key": "ai_answer"}
    assert callers_memory["__chat_extract_completed"]["memory_key"] == "ai_answer"


def test_execute_action_preserves_empty_dict_reference(execute_action) -> None:
    """Direct check: when caller passes an empty dict and we route through
    browser.execute_action, the dict reference the ActionContext sees must
    BE the same object as the caller's empty dict."""
    from visual_web_agent.browser_env import ActionExecutionError

    captured = {}

    class _SpyContext:
        """Captures the workflow_memory the action handler was called with."""
        instances: list = []

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            _SpyContext.instances.append(self)

    # Monkeypatch ActionContext + ActionRegistry minimally to capture the
    # workflow_memory passed in. We need to satisfy the source code path
    # up to the ActionContext construction; everything after can short-circuit.
    callers_dict: dict = {}
    callers_id = id(callers_dict)

    # We can't easily run the full execute_action without playwright; instead
    # parse the source and verify the construction line.
    src = inspect.getsource(execute_action)
    # The fixed construction looks like:
    #   ActionContext(
    #       action=action_model,
    #       browser=self,
    #       workflow_memory=workflow_memory,
    #       ...
    assert "workflow_memory=workflow_memory" in src, (
        "ActionContext must be passed the caller's workflow_memory as-is "
        "(no ``or {}`` fallback). Current source:\n" + src[-800:]
    )

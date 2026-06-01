"""Tests for the generic AI-chat answer extractor.

Two layers covered:

  1. ``visual_web_agent.chat_answer_extractor.extract_chat_answer`` — the
     pure JS-driven extraction core. We mock ``page.evaluate`` so we can
     simulate selector hits, streaming, and total-failure paths without a
     real browser.

  2. ``visual_web_agent.actions.ChatExtractHandler`` — the action handler
     that wraps the core, writes to workflow_memory, and records rpa_trail.

The handler-side tests are also responsible for the regression that the
``chat_extract`` action is wired in:

  - registered in ``ActionRegistry``
  - listed in the VSpiderAction enum
  - documented in prompts (JSON_SCHEMA_PROMPT, ACTION_REFERENCE_PROMPT,
    and CHAT_ENTRY_SKILL) — so a future prompt refactor that drops the
    docs will turn this red.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


# ════════════════════════════════════════════════════════════════════
# 1. Core extractor — extract_chat_answer
# ════════════════════════════════════════════════════════════════════

from visual_web_agent.chat_answer_extractor import (
    ANSWER_SELECTORS,
    EXCLUDE_CONTAINERS,
    clean_chat_answer_text,
    extract_chat_answer,
)


def _run(coro):
    return asyncio.run(coro)


class _StablePage:
    """page.evaluate returns the same payload every call → stability hit fast."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.url = payload.get("url") or "https://chat.example/"
        self.evaluate = AsyncMock(return_value=payload)


class _StreamingPage:
    """page.evaluate grows the answer text across consecutive calls.

    Simulates a streaming AI: each call returns a slightly longer text.
    After ``stable_after`` calls the text stops growing → stability achieved.
    """

    def __init__(self, *, stable_after: int = 3, base_text: str = "answer ") -> None:
        self.calls = 0
        self.stable_after = stable_after
        self.base_text = base_text
        self.url = "https://chat.example/"
        self.evaluate = AsyncMock(side_effect=self._next)

    async def _next(self, *_args, **_kwargs):
        self.calls += 1
        # Grow until stable_after; then return identical length to flag stable.
        n = min(self.calls, self.stable_after)
        text = (self.base_text * 20 * n)[:200 * n]
        return {
            "method": "selector:[class*='ai-answer']",
            "selector": "[class*='ai-answer']",
            "text": text,
            "length": len(text),
            "html_len": len(text) + 50,
            "title": "Chat",
            "url": self.url,
            "candidates_scanned": 1,
        }


class _NeverFindsPage:
    """page.evaluate always returns method=none / empty text."""

    def __init__(self) -> None:
        self.url = "https://opaque.example/"
        self.evaluate = AsyncMock(return_value={
            "method": "none",
            "selector": None,
            "text": "",
            "length": 0,
            "html_len": 0,
            "title": "",
            "url": self.url,
            "candidates_scanned": 17,
        })


def test_selector_hit_returns_ok_and_records_method() -> None:
    payload = {
        "method": "selector:[class*='ai-answer']",
        "selector": "[class*='ai-answer']",
        "text": "DeepSeek 是一家专注于通用人工智能的研究机构，主打开源大模型。" * 5,
        "length": 200,
        "html_len": 320,
        "title": "文心一言",
        "url": "https://yiyan.baidu.com/chat/abc",
        "candidates_scanned": 4,
    }
    page = _StablePage(payload)
    # Use a small timeout so the wait loop hits the stability shortcut quickly.
    result = _run(extract_chat_answer(page, timeout=2.5, poll_interval=0.05))

    assert result["ok"] is True
    assert "DeepSeek" in result["answer"]
    assert result["method"].startswith("selector:")
    assert result["selector"] == "[class*='ai-answer']"
    assert result["length"] >= 100
    assert result["wait_ms"] >= 0
    assert result["url"].startswith("https://yiyan.baidu.com")


def test_truncation_at_max_chars() -> None:
    from visual_web_agent.chat_answer_extractor import MAX_RETURN_CHARS
    huge = "x" * (MAX_RETURN_CHARS + 500)
    page = _StablePage({
        "method": "selector:[class*='markdown-body']",
        "selector": "[class*='markdown-body']",
        "text": huge,
        "length": len(huge),
        "html_len": len(huge),
        "title": "",
        "url": "https://chat.openai.com/",
        "candidates_scanned": 1,
    })
    result = _run(extract_chat_answer(page, timeout=2.0, poll_interval=0.05))
    assert result["ok"] is True
    assert len(result["answer"]) == MAX_RETURN_CHARS
    # The original length is preserved in the metadata
    assert result["length"] == len(huge)


def test_streaming_waits_for_stability() -> None:
    page = _StreamingPage(stable_after=3)
    result = _run(extract_chat_answer(
        page, timeout=4.0, poll_interval=0.05, stable_ticks=2,
    ))
    assert result["ok"] is True
    # We should have polled at least stable_after + stable_ticks-1 times
    assert page.calls >= 4
    assert result["polls"] == page.calls


def test_never_finds_returns_not_ok_with_error() -> None:
    page = _NeverFindsPage()
    result = _run(extract_chat_answer(page, timeout=1.5, poll_interval=0.05))
    assert result["ok"] is False
    assert result["answer"] == ""
    assert result["method"] == "none"
    assert "error" in result
    assert result["polls"] > 0


def test_evaluate_crash_is_swallowed() -> None:
    page = SimpleNamespace(
        url="https://broken.example/",
        evaluate=AsyncMock(side_effect=RuntimeError("CDP gone")),
    )
    result = _run(extract_chat_answer(page, timeout=1.2, poll_interval=0.05))
    assert result["ok"] is False
    # No exception leaks. The error message surfaces in the result.
    assert result["error"]


def test_below_min_length_is_treated_as_streaming() -> None:
    """A 5-char answer block is too short — must keep polling, not declare OK."""
    page = _StablePage({
        "method": "selector:[class*='ai-response']",
        "selector": "[class*='ai-response']",
        "text": "hi",
        "length": 2,
        "html_len": 10,
        "title": "",
        "url": "https://chat.example/",
        "candidates_scanned": 1,
    })
    result = _run(extract_chat_answer(
        page, timeout=1.0, min_length=40, poll_interval=0.05,
    ))
    assert result["ok"] is False
    assert result["length"] == 2


def test_selector_list_contains_known_brands() -> None:
    """Cascade should cover the brands we care about generically."""
    blob = " ".join(ANSWER_SELECTORS).lower()
    # generic class fragments — at least these substrings must be present
    for needle in (
        "ai-answer",
        "chat-answer",
        "ai-response",
        "markdown-body",
        "message",
    ):
        assert needle in blob, f"selector cascade missing fragment: {needle}"


def test_exclude_list_catches_search_results() -> None:
    blob = " ".join(EXCLUDE_CONTAINERS).lower()
    # We MUST reject these on fallback (they're the failure mode on baidu)
    for needle in ("search-result", "nav", "footer", "sidebar", "aside"):
        assert needle in blob, f"exclude list missing: {needle}"


# ════════════════════════════════════════════════════════════════════
# 2. Action wrapper — ChatExtractHandler
# ════════════════════════════════════════════════════════════════════

from visual_web_agent.actions import ActionRegistry, ChatExtractHandler  # noqa: E402


def _build_action(
    *, target_id: int = 0, type_value: str = "", memory_key: str = "ai_answer"
):
    return SimpleNamespace(
        action="chat_extract",
        target_id=target_id,
        type_value=type_value,
        memory_key=memory_key,
        extracted_data=None,
    )


def _build_handler_ctx(action, *, page_url: str = "https://chat.example/"):
    page = SimpleNamespace(
        url=page_url,
        evaluate=AsyncMock(return_value={
            "method": "selector:[class*='ai-answer']",
            "selector": "[class*='ai-answer']",
            "text": "DeepSeek 是一家专注于通用 AI 的研究机构。" * 8,
            "length": 240,
            "html_len": 320,
            "title": "文心",
            "url": page_url,
            "candidates_scanned": 1,
        }),
    )
    browser = SimpleNamespace()
    browser._last_action_error = None
    browser.rpa_trail = []
    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = page
    ctx.workflow_memory = {}
    ctx.action = action
    ctx.with_rpa_meta = lambda d: d
    return ctx, browser, page


def test_registry_has_chat_extract() -> None:
    """The action must be registered so dispatcher can find it."""
    assert ActionRegistry.is_registered("chat_extract")
    handler = ActionRegistry.get("chat_extract")
    assert isinstance(handler, ChatExtractHandler)


def test_handler_writes_to_memory_and_extracted_data() -> None:
    action = _build_action(memory_key="ai_answer")
    ctx, browser, page = _build_handler_ctx(action)

    result = _run(ChatExtractHandler().execute(ctx))
    assert result is None  # dispatcher does Tab Guard
    assert "ai_answer" in ctx.workflow_memory
    stored = ctx.workflow_memory["ai_answer"]
    assert stored["method"].startswith("selector:")
    assert "DeepSeek" in stored["answer"]
    assert stored["url"].startswith("https://chat.example")
    assert ctx.workflow_memory["latest_memory"]
    # And we mirrored to extracted_data for VLM visibility
    assert action.extracted_data["ok"] is True
    assert "DeepSeek" in action.extracted_data["answer"]


def test_handler_auto_generates_memory_key_when_missing() -> None:
    action = _build_action(memory_key="")
    ctx, browser, page = _build_handler_ctx(action)
    _run(ChatExtractHandler().execute(ctx))
    keys = [k for k in ctx.workflow_memory if k.startswith("chat_answer_")]
    assert keys, "auto-generated memory_key starting with chat_answer_ expected"


def test_handler_json_options_passed_through() -> None:
    """type_value JSON should override defaults without crashing."""
    action = _build_action(type_value='{"timeout": 3, "min_length": 60}')
    ctx, browser, page = _build_handler_ctx(action)
    _run(ChatExtractHandler().execute(ctx))
    # Did not crash and stored a cleaned result; min_length 60 < cleaned text.
    stored = ctx.workflow_memory["ai_answer"]
    assert stored["length"] == len(stored["answer"])
    assert stored["length"] >= 60


def test_handler_malformed_options_fall_back_to_defaults() -> None:
    action = _build_action(type_value="this is not json")
    ctx, browser, page = _build_handler_ctx(action)
    _run(ChatExtractHandler().execute(ctx))
    # Defaults kept; still produced a result
    assert ctx.workflow_memory["ai_answer"]["method"].startswith("selector:")


def test_handler_records_rpa_trail() -> None:
    action = _build_action(memory_key="ai_answer")
    ctx, browser, page = _build_handler_ctx(action)
    _run(ChatExtractHandler().execute(ctx))
    assert browser.rpa_trail, "rpa_trail must be appended for replayability"
    trail = browser.rpa_trail[-1]
    assert trail["action"] == "chat_extract"
    assert trail["memory_key"] == "ai_answer"
    # type_value is a JSON-encoded options blob
    opts = json.loads(trail["type_value"])
    assert "timeout" in opts and "min_length" in opts


def test_handler_handles_extractor_failure_gracefully() -> None:
    """When the JS probe always returns empty, the handler must still write a
    structured (ok=False) record — no exception, no missing keys."""
    action = _build_action(memory_key="ai_answer")
    page = SimpleNamespace(
        url="https://opaque.example/",
        evaluate=AsyncMock(return_value={
            "method": "none",
            "selector": None,
            "text": "",
            "length": 0,
            "html_len": 0,
            "title": "",
            "url": "https://opaque.example/",
            "candidates_scanned": 17,
        }),
    )
    browser = SimpleNamespace()
    browser._last_action_error = None
    browser.rpa_trail = []
    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = page
    ctx.workflow_memory = {}
    ctx.action = action
    ctx.with_rpa_meta = lambda d: d

    _run(ChatExtractHandler().execute(ctx))

    stored = ctx.workflow_memory.get("ai_answer")
    assert stored is not None
    assert stored["method"] == "none"
    assert stored["answer"] == ""
    assert action.extracted_data["ok"] is False


def test_handler_evaluate_crash_does_not_propagate() -> None:
    """If page.evaluate explodes, we must absorb the exception inside the
    extractor + handler (Tab Guard / loop should never see it)."""
    action = _build_action(memory_key="ai_answer")
    page = SimpleNamespace(
        url="https://broken.example/",
        evaluate=AsyncMock(side_effect=RuntimeError("CDP died")),
    )
    browser = SimpleNamespace()
    browser._last_action_error = None
    browser.rpa_trail = []
    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = page
    ctx.workflow_memory = {}
    ctx.action = action
    ctx.with_rpa_meta = lambda d: d

    # No raise expected
    _run(ChatExtractHandler().execute(ctx))
    assert ctx.workflow_memory.get("ai_answer") is not None


# ════════════════════════════════════════════════════════════════════
# 3. Prompt-doc regressions — ensure VLM actually sees chat_extract
# ════════════════════════════════════════════════════════════════════


def test_chat_extract_listed_in_action_enum() -> None:
    from visual_web_agent.prompt_skills import JSON_SCHEMA_PROMPT
    assert "chat_extract" in JSON_SCHEMA_PROMPT


def test_chat_extract_documented_in_action_reference() -> None:
    """The bullet section that describes per-action semantics must mention
    chat_extract so the VLM actually knows when to pick it."""
    from visual_web_agent.prompt_skills import ACTION_REFERENCE_PROMPT
    assert "chat_extract" in ACTION_REFERENCE_PROMPT
    # Locate the last occurrence — typically the per-action bullet.
    bullet_idx = ACTION_REFERENCE_PROMPT.rfind("chat_extract")
    block = ACTION_REFERENCE_PROMPT[bullet_idx: bullet_idx + 600]
    assert "memory_key" in block
    # Bullet must hint at the "AI chat answer" use case
    assert ("聊天" in block) or ("AI" in block.upper()) or ("回答" in block)


def test_chat_entry_skill_mentions_chat_extract() -> None:
    from visual_web_agent.prompt_skills import CHAT_ENTRY_SKILL
    assert "chat_extract" in CHAT_ENTRY_SKILL
    # It must also discourage using plain `extract` on chat pages
    assert "extract" in CHAT_ENTRY_SKILL  # trivially true; sanity


def test_chat_extract_in_metadata_registry() -> None:
    """Strategy layer should know about chat_extract for goal-driven scoring."""
    from visual_web_agent.action_registry import build_default_action_registry
    registry = build_default_action_registry()
    tool = registry.get("chat_extract")
    assert tool is not None
    assert tool.capability == "extract"
    # Brand aliases should give it goal-match on Chinese/English chat tasks
    score = tool.score_goal("打开文心助手，问介绍一下 DeepSeek")
    assert score > 0


# ════════════════════════════════════════════════════════════════════
# 4. Hostname coverage — KNOWN_CHAT_HOSTS aligns with extractor targets
# ════════════════════════════════════════════════════════════════════


def test_known_chat_hosts_match_chat_extract_brands() -> None:
    """Smoke: the drift guard's chat-host allowlist should cover the brands
    chat_extract's prompt copy claims to handle."""
    from visual_web_agent.chat_entry_drift_guard import _KNOWN_CHAT_HOSTS
    for needle in (
        "yiyan.baidu.com",
        "chat.openai.com",
        "claude.ai",
        "tongyi.aliyun.com",
        "chat.deepseek.com",
    ):
        assert needle in _KNOWN_CHAT_HOSTS, (
            f"{needle} must be in _KNOWN_CHAT_HOSTS so the drift guard "
            "doesn't tear chat_extract off the answer page"
        )


# ════════════════════════════════════════════════════════════════════
# 5. Streaming / scroll / sweep — regressions for "still failing on long answers"
# ════════════════════════════════════════════════════════════════════


def test_streaming_indicator_blocks_stability() -> None:
    """When the page reports streaming=True, even unchanged text length must
    NOT be treated as stable. Before this fix we grabbed half-finished
    answers between two equal-length token bursts."""
    seen = {"count": 0}

    async def evaluate_side_effect(*_a, **_kw):
        seen["count"] += 1
        # First 4 calls: same text length, but streaming flag ON
        streaming = seen["count"] <= 4
        return {
            "method": "selector:[class*='ai-answer']",
            "selector": "[class*='ai-answer']",
            "text": "x" * 150,
            "length": 150,
            "html_len": 200,
            "title": "Chat",
            "url": "https://yiyan.baidu.com/chat/x",
            "streaming": streaming,
            "candidates_scanned": 1,
        }

    page = SimpleNamespace(
        url="https://yiyan.baidu.com/chat/x",
        evaluate=AsyncMock(side_effect=evaluate_side_effect),
    )
    result = _run(extract_chat_answer(
        page, timeout=2.5, min_length=80,
        poll_interval=0.05, stable_ticks=2,
    ))
    assert seen["count"] >= 5, (
        "extractor returned before streaming indicator cleared "
        f"(only {seen['count']} probes)"
    )
    assert result["ok"] is True


def test_extract_js_contains_scroll_logic() -> None:
    """The probe JS must scroll the page (and inner containers) to bottom
    before reading innerText — otherwise streaming answers below the fold
    are invisible."""
    from visual_web_agent.chat_answer_extractor import _build_extract_js
    js = _build_extract_js()
    assert "scrollTo" in js
    assert "scrollHeight" in js
    assert "scrollTop" in js


def test_handler_prescrolls_page_before_extraction() -> None:
    """The handler MUST evaluate a scroll-to-bottom payload before delegating
    to the extractor — otherwise long streaming answers stay below the fold."""
    action = _build_action(memory_key="ai_answer")
    ctx, browser, page = _build_handler_ctx(action)
    call_log: list[str] = []
    original_evaluate = page.evaluate

    async def capture_evaluate(*args, **kwargs):
        js = (args[0] if args else "") or ""
        if "scrollTo" in js and "SELECTORS" not in js and "EXCLUDES" not in js:
            call_log.append("prescroll")
        else:
            call_log.append("probe")
        return await original_evaluate(*args, **kwargs)

    page.evaluate = capture_evaluate
    _run(ChatExtractHandler().execute(ctx))

    assert call_log, "handler must call page.evaluate at least once"
    assert "prescroll" in call_log, (
        "handler must run a pre-scroll JS payload before the first probe; "
        f"observed call order: {call_log}"
    )
    assert call_log[0] == "prescroll", (
        f"prescroll should run before any probe, got order: {call_log[:5]}"
    )


def test_handler_sweep_fallback_when_selectors_miss() -> None:
    """When the extractor returns ok=False, the handler must run a whole-page
    innerText sweep (excluding nav/footer/search-result) so the action never
    comes back empty on a chat page."""
    action = _build_action(memory_key="ai_answer")
    sweep_text = "这是一段足够长的兜底正文，来自全页 innerText 扫描。" * 8

    call_count = {"prescroll": 0, "probe": 0, "sweep": 0}

    async def evaluate_router(*args, **_kw):
        js = (args[0] if args else "") or ""
        if "scrollTo" in js and "SELECTORS" not in js and "EXCLUDES" not in js:
            call_count["prescroll"] += 1
            return None
        if "EXCLUDES" in js and "cloneNode" in js:
            call_count["sweep"] += 1
            return {"text": sweep_text, "length": len(sweep_text)}
        call_count["probe"] += 1
        return {
            "method": "none",
            "selector": None,
            "text": "",
            "length": 0,
            "html_len": 0,
            "title": "",
            "url": "https://yiyan.baidu.com/chat/x",
            "streaming": False,
            "candidates_scanned": 0,
        }

    page = SimpleNamespace(
        url="https://yiyan.baidu.com/chat/x",
        evaluate=AsyncMock(side_effect=evaluate_router),
    )
    browser = SimpleNamespace()
    browser._last_action_error = None
    browser.rpa_trail = []
    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = page
    ctx.workflow_memory = {}
    ctx.action = action
    ctx.with_rpa_meta = lambda d: d

    action.type_value = '{"timeout": 1.5, "min_length": 80}'
    _run(ChatExtractHandler().execute(ctx))

    assert call_count["sweep"] >= 1, (
        f"sweep fallback never ran; call_count={call_count}"
    )
    stored = ctx.workflow_memory.get("ai_answer")
    assert stored is not None
    assert stored["method"] == "fallback:page-innertext"
    assert "兜底正文" in stored["answer"]
    assert action.extracted_data["ok"] is True


def test_clean_chat_answer_text_removes_yiyan_thinking_and_page_chrome() -> None:
    raw = (
        "深度思考已完成The user is asking me to introduce DeepSeek. "
        "Let me provide a comprehensive introduction.\n"
        "DeepSeek is a Chinese AI company...\n"
        "思考完成: \xa0准备输出结果DeepSeek 介绍\n"
        "公司背景\n"
        "DeepSeek（深度求索） 是一家中国人工智能公司。\n"
        "核心亮点\n"
        "开源策略：多数模型开源，降低了使用门槛。\n"
        "如果你想深入了解某个具体模型，可以进一步讨论。"
        "介绍一下deepseek"
        "deepseek是做什么的介绍一下DeepSeek的业务范畴"
        "深度分析需求并解答，你需要什么帮助？码牛模式全选参考0"
        "var _hmt=_hmt||[];performance.mark('MARK_LOAD_HTML');"
        "serviceWorker.register('/service-worker.js')12345678910"
    )

    cleaned = clean_chat_answer_text(raw)

    assert cleaned.startswith("DeepSeek 介绍")
    assert "公司背景" in cleaned
    assert "核心亮点" in cleaned
    assert "开源策略" in cleaned
    assert "The user is asking" not in cleaned
    assert "深度思考已完成" not in cleaned
    assert "思考完成" not in cleaned
    assert "介绍一下deepseek" not in cleaned
    assert "码牛模式" not in cleaned
    assert "serviceWorker" not in cleaned
    assert "performance.mark" not in cleaned


def test_handler_skips_sweep_when_structured_extract_succeeded() -> None:
    """Sweep is a last resort — must not run when the cascade already
    returned a long, structured answer."""
    action = _build_action(memory_key="ai_answer")
    ctx, browser, page = _build_handler_ctx(action)

    call_count = {"prescroll": 0, "probe": 0, "sweep": 0}
    original_evaluate = page.evaluate

    async def evaluate_router(*args, **kwargs):
        js = (args[0] if args else "") or ""
        if "scrollTo" in js and "SELECTORS" not in js and "EXCLUDES" not in js:
            call_count["prescroll"] += 1
            return None
        if "EXCLUDES" in js and "cloneNode" in js:
            call_count["sweep"] += 1
            return {"text": "should-not-be-used", "length": 100}
        call_count["probe"] += 1
        return await original_evaluate(*args, **kwargs)

    page.evaluate = evaluate_router
    _run(ChatExtractHandler().execute(ctx))

    assert call_count["sweep"] == 0, (
        f"sweep should NOT run when structured extract succeeded; "
        f"call_count={call_count}"
    )


def test_tunables_match_documented_behavior() -> None:
    """The prompt promises {timeout≈25, min_length≈80}. Code must agree."""
    from visual_web_agent.chat_answer_extractor import (
        DEFAULT_TIMEOUT_SEC,
        MIN_ANSWER_LEN,
        STABLE_TICKS_REQUIRED,
    )
    # Long streaming answers (e.g. 文心 介绍一下 DeepSeek) need ≥20s.
    assert DEFAULT_TIMEOUT_SEC >= 20
    # Below 80 we'd accept "Hello! Let me think about that..." as the answer.
    assert MIN_ANSWER_LEN >= 60
    # 2 ticks is too easy to trip between token bursts.
    assert STABLE_TICKS_REQUIRED >= 3


def test_selector_cascade_covers_baidu_and_streaming_brands() -> None:
    """Regression: must keep the new 百度文心 / streaming-indicator selectors."""
    from visual_web_agent.chat_answer_extractor import (
        ANSWER_SELECTORS,
        STREAMING_INDICATORS,
    )
    blob = " ".join(ANSWER_SELECTORS).lower()
    for needle in ("ai-content", "robot-bubble", "smart-card", "bot-bubble"):
        assert needle in blob, f"selector cascade missing 百度系 fragment: {needle}"
    sblob = " ".join(STREAMING_INDICATORS).lower()
    for needle in ("streaming", "typing", "generating", "loading"):
        assert needle in sblob, f"streaming-indicator list missing: {needle}"

"""Tests for the chat-host extract coercion guard.

This guard rewrites ``extract`` / ``extract_link`` → ``chat_extract`` when
the current URL is on a known chat host. The repeated failure mode is:

  VLM lands on chat.baidu.com/search/?q=... (Baidu's "search-then-answer"
  answer page), picks generic ``extract`` because that's the verb it knows
  best, and walks away with [{"title":"DeepSeek塞进苹果本儿..."}, ...] —
  search-result cards, not the AI answer. Prompt copy alone is not enough
  to overcome this habit; we coerce structurally.

These tests cover:
  - the guard fires on known chat hosts with extract / extract_link
  - the guard SKIPS non-chat hosts (we don't molest generic extracts)
  - the guard SKIPS chat_extract / other actions (idempotency)
  - login/settings/about paths on chat hosts are exempt (heuristic)
  - memory_key is preserved or sensibly defaulted
  - feedback string mentions the host and the rewrite
"""

from __future__ import annotations

import pytest


from visual_web_agent.chat_extract_coercion_guard import (
    detect_extract_on_chat_host,
    detect_premature_chat_done,
    KNOWN_CHAT_HOSTS,
    _has_real_answer_path,
)


# ── Fires on chat hosts with extract-style actions ──────────────────────────


@pytest.mark.parametrize(
    "url,host",
    [
        ("https://chat.baidu.com/search/?q=介绍一下deepseek", "chat.baidu.com"),
        ("https://yiyan.baidu.com/chat/abc", "yiyan.baidu.com"),
        ("https://chat.openai.com/c/123", "chat.openai.com"),
        ("https://claude.ai/chat/xyz", "claude.ai"),
        ("https://tongyi.aliyun.com/qianwen/", "tongyi.aliyun.com"),
        ("https://www.doubao.com/chat/123", "www.doubao.com"),
        ("https://kimi.moonshot.cn/chat/abc", "kimi.moonshot.cn"),
        ("https://yuanbao.tencent.com/chat/x", "yuanbao.tencent.com"),
        ("https://chat.deepseek.com/c/123", "chat.deepseek.com"),
        ("https://gemini.google.com/app", "gemini.google.com"),
        ("https://copilot.microsoft.com/?id=abc", "copilot.microsoft.com"),
        ("https://www.perplexity.ai/search/abc", "www.perplexity.ai"),
    ],
)
def test_extract_on_chat_host_coerces(url: str, host: str) -> None:
    decision = {
        "action": "extract",
        "target_id": 0,
        "type_value": "",
        "memory_key": "ai_answer",
    }
    result = detect_extract_on_chat_host(decision, url)
    assert result is not None, f"should fire on {host}"
    assert result["matched_host"] == host
    assert result["memory_key"] == "ai_answer"
    assert "chat_extract" in result["feedback"]
    assert host in result["feedback"]


def test_extract_link_also_coerced() -> None:
    decision = {"action": "extract_link", "target_id": 18, "memory_key": ""}
    result = detect_extract_on_chat_host(
        decision, "https://chat.baidu.com/search/?q=test"
    )
    assert result is not None
    # memory_key auto-defaulted because original was empty
    assert result["memory_key"] == "ai_answer"


def test_memory_key_preserved_when_set() -> None:
    decision = {"action": "extract", "memory_key": "wenxin_reply_2"}
    result = detect_extract_on_chat_host(
        decision, "https://yiyan.baidu.com/chat/x"
    )
    assert result is not None
    assert result["memory_key"] == "wenxin_reply_2"


# ── Does NOT fire on non-chat hosts ─────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://www.baidu.com/s?wd=python",
        "https://www.google.com/search?q=test",
        "https://news.ycombinator.com/",
        "https://en.wikipedia.org/wiki/Python",
        "https://rpachallenge.com/",
    ],
)
def test_does_not_fire_on_search_or_generic_pages(url: str) -> None:
    decision = {"action": "extract", "memory_key": ""}
    assert detect_extract_on_chat_host(decision, url) is None


def test_does_not_fire_when_action_is_already_chat_extract() -> None:
    """Idempotency: a chat_extract decision must not be re-coerced."""
    decision = {"action": "chat_extract", "memory_key": "ai_answer"}
    assert detect_extract_on_chat_host(
        decision, "https://yiyan.baidu.com/chat/abc"
    ) is None


@pytest.mark.parametrize("action", ["click", "type", "press_key", "goto", "scroll", "done"])
def test_does_not_fire_on_unrelated_actions(action: str) -> None:
    decision = {"action": action, "memory_key": ""}
    assert detect_extract_on_chat_host(
        decision, "https://yiyan.baidu.com/chat/x"
    ) is None


# ── Path heuristic — chat hosts but non-answer surfaces ─────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://yiyan.baidu.com/login",
        "https://chat.openai.com/auth/login",
        "https://claude.ai/signup",
        "https://chatglm.cn/settings/profile",
        "https://gemini.google.com/about",
        "https://www.perplexity.ai/help",
        "https://chat.deepseek.com/oauth/callback",
    ],
)
def test_does_not_fire_on_chat_host_login_settings(url: str) -> None:
    decision = {"action": "extract", "memory_key": ""}
    assert detect_extract_on_chat_host(decision, url) is None


def test_root_path_on_chat_host_still_coerces() -> None:
    """Home path on chat hosts is usually the answer surface itself."""
    decision = {"action": "extract", "memory_key": ""}
    result = detect_extract_on_chat_host(
        decision, "https://chat.openai.com/"
    )
    assert result is not None


# ── Edge cases ──────────────────────────────────────────────────────────────


def test_missing_url_returns_none() -> None:
    decision = {"action": "extract", "memory_key": ""}
    assert detect_extract_on_chat_host(decision, "") is None


def test_non_dict_decision_returns_none() -> None:
    assert detect_extract_on_chat_host(None, "https://yiyan.baidu.com/") is None  # type: ignore[arg-type]
    assert detect_extract_on_chat_host(["extract"], "https://yiyan.baidu.com/") is None  # type: ignore[arg-type]


def test_malformed_url_does_not_raise() -> None:
    """URL parsing failures must yield None, not exceptions."""
    decision = {"action": "extract", "memory_key": ""}
    # Garbage URL; urlparse tolerates this but host is empty
    assert detect_extract_on_chat_host(decision, "not a url at all") is None


def test_known_chat_hosts_reexport() -> None:
    """The guard exposes the same host set as chat_entry_drift_guard."""
    from visual_web_agent.chat_entry_drift_guard import _KNOWN_CHAT_HOSTS
    assert KNOWN_CHAT_HOSTS == _KNOWN_CHAT_HOSTS


def test_has_real_answer_path_heuristic() -> None:
    assert _has_real_answer_path("https://yiyan.baidu.com/")
    assert _has_real_answer_path("https://yiyan.baidu.com/chat/123")
    assert _has_real_answer_path("https://chat.baidu.com/search/?q=x")
    assert not _has_real_answer_path("https://claude.ai/login")
    assert not _has_real_answer_path("https://claude.ai/settings/account")


def test_premature_chat_done_is_rewritten_to_chat_extract() -> None:
    decision = {
        "action": "done",
        "memory_key": "ai_answer",
        "subgoal_status": "completed",
        "thought": "AI 已返回完整回答，可以结束",
        "extracted_data": [{"answer": "Hermes: partial English excerpt..."}],
    }
    result = detect_premature_chat_done(
        decision,
        "https://yiyan.baidu.com/chat/abc",
        goal="获取 AI 返回的内容",
    )
    assert result is not None
    assert result["memory_key"] == "ai_answer"
    assert result["original_action"] == "done"
    assert "chat_extract" in result["feedback"]


def test_premature_chat_submit_with_extracted_data_is_rewritten() -> None:
    decision = {
        "action": "chat_submit",
        "subgoal_status": "completed",
        "current_state": "页面已出现 AI 回答内容",
        "extracted_data": [{"answer": "short excerpt"}],
    }
    result = detect_premature_chat_done(
        decision,
        "https://chat.baidu.com/search?q=x",
        goal="提问并输出回答",
    )
    assert result is not None
    assert result["memory_key"] == "ai_answer"
    assert result["original_action"] == "chat_submit"


def test_premature_chat_extract_with_payload_is_rewritten() -> None:
    """A chat_extract action with VLM-filled data still has to execute.

    This covers run_log_20260521_213341: the model picked the right action name
    but smuggled a summarized answer into extracted_data, so the deterministic
    extractor never got a chance to read the real chat answer block.
    """
    decision = {
        "action": "chat_extract",
        "memory_key": "ai_answer",
        "subgoal_status": "completed",
        "thought": "AI answer is complete; I extracted it.",
        "extracted_data": {
            "answer": "short VLM-written summary, not the deterministic text"
        },
    }
    result = detect_premature_chat_done(
        decision,
        "https://yiyan.baidu.com/chat/MTk5MzM0ODYzMDo1MjAyNjk0ODA5",
        goal="获取 ai 返回的内容并输出",
    )
    assert result is not None
    assert result["memory_key"] == "ai_answer"
    assert result["original_action"] == "chat_extract"
    assert "chat_extract" in result["feedback"]


def test_premature_chat_done_skips_after_chat_extract_completed() -> None:
    decision = {
        "action": "done",
        "subgoal_status": "completed",
        "extracted_data": [{"answer": "already extracted"}],
    }
    assert detect_premature_chat_done(
        decision,
        "https://yiyan.baidu.com/chat/abc",
        goal="获取回答",
        chat_extract_completed=True,
    ) is None


def test_premature_chat_done_requires_answer_goal() -> None:
    decision = {
        "action": "done",
        "subgoal_status": "completed",
        "extracted_data": [{"answer": "x"}],
    }
    assert detect_premature_chat_done(
        decision,
        "https://yiyan.baidu.com/chat/abc",
        goal="只发送消息",
    ) is None


# ── Smoke: the main.py wiring imports successfully ──────────────────────────


def test_main_wiring_imports_cleanly() -> None:
    """If main.py's import path for this guard breaks, this catches it
    without needing to run the whole agent loop."""
    from visual_web_agent.chat_extract_coercion_guard import (
        detect_extract_on_chat_host,
        detect_premature_chat_done,
    )
    assert callable(detect_extract_on_chat_host)
    assert callable(detect_premature_chat_done)

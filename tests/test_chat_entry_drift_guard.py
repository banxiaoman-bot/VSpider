"""Direct regression for the 19:24 Wenxin failure: VLM types into what
looks like a chat input, presses Enter, lands on /search/, then burns
11 steps trying to find a "back to chat" entry that doesn't exist.

The guard catches the URL-drift signal post-submit and auto-rewrites the
next action to ``goto`` a known direct-chat URL.
"""

from __future__ import annotations

import pytest

from visual_web_agent.chat_entry_drift_guard import (
    KNOWN_CHAT_URLS,
    detect_chat_to_search_drift,
    infer_chat_url_for_goal,
    is_on_known_chat_domain,
    looks_like_search_url,
)


def _decision(action: str, target_id: int = 0, type_value: str = "", thought: str = "") -> dict:
    return {
        "action": action,
        "target_id": target_id,
        "type_value": type_value,
        "thought": thought,
        "memory_key": "",
    }


# ── looks_like_search_url ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "https://www.baidu.com/s?wd=Python",
        "https://chat.baidu.com/search/abc",
        "https://chat.baidu.com/search?q=test",
        "https://www.google.com/search?q=python",
        "https://so.com/serp?keyword=x",
        "https://example.com/results?p=1",
        "https://cn.bing.com/search?q=hello",
        "https://search.example.com/?query=x",
    ],
)
def test_recognized_as_search_url(url: str) -> None:
    assert looks_like_search_url(url), url


@pytest.mark.parametrize(
    "url",
    [
        "https://yiyan.baidu.com/",
        "https://chat.baidu.com/",
        "https://chat.openai.com/?model=gpt-4",  # non-search query key
        "https://claude.ai/new",
        "https://example.com/article/123",
        "",
        "https://example.com/dialog/abc",
    ],
)
def test_not_search_url(url: str) -> None:
    assert not looks_like_search_url(url), url


def test_search_url_with_empty_query_value_not_search() -> None:
    """``?q=`` with no value is the form's initial state, not a search."""
    assert not looks_like_search_url("https://example.com/page?q=&other=1")


# ── infer_chat_url_for_goal ──────────────────────────────────────────────────
def test_infer_chinese_brand() -> None:
    result = infer_chat_url_for_goal("打开文心助手问问 DeepSeek")
    assert result is not None
    brand, url = result
    assert "yiyan.baidu.com" in url


def test_infer_english_brand() -> None:
    result = infer_chat_url_for_goal("Open ChatGPT and ask about Python")
    assert result is not None
    brand, url = result
    assert "openai.com" in url


def test_longer_brand_wins_over_shorter() -> None:
    """'文心一言' must match before '文心' (longer key has priority)."""
    result = infer_chat_url_for_goal("用文心一言总结今天的新闻")
    assert result is not None
    brand, url = result
    # Either is acceptable — both map to the same URL — but key length
    # ordering ensures deterministic behavior.
    assert "yiyan.baidu.com" in url


def test_no_brand_returns_none() -> None:
    assert infer_chat_url_for_goal("抓取淘宝的商品列表") is None
    assert infer_chat_url_for_goal("") is None


def test_known_urls_table_covers_major_brands() -> None:
    """Sanity: the most common Chinese + English assistants must be in the table."""
    for brand in ("文心", "通义", "豆包", "kimi"):
        assert brand in KNOWN_CHAT_URLS, f"missing CN brand: {brand}"
    for brand in ("chatgpt", "claude", "gemini"):
        assert brand in KNOWN_CHAT_URLS, f"missing EN brand: {brand}"


# ── is_on_known_chat_domain ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "https://chat.baidu.com/",
        "https://chat.baidu.com/search/?q=介绍",  # /search/ on chat host
        "https://yiyan.baidu.com/dialog/123",
        "https://chat.openai.com/c/abc",
        "https://chatgpt.com/",
        "https://claude.ai/new",
        "https://gemini.google.com/app",
        "https://kimi.moonshot.cn/chat/abc",
        "https://chatglm.cn/main/alltoolsdetail",
        "https://yuanbao.tencent.com/chat",
        "https://chat.deepseek.com/a/chat/s/123",
        "https://tongyi.aliyun.com/qianwen",
    ],
)
def test_is_on_known_chat_domain_positive(url: str) -> None:
    assert is_on_known_chat_domain(url), url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.baidu.com/",
        "https://www.baidu.com/s?wd=x",  # Baidu *search* host, NOT chat
        "https://www.google.com/search?q=x",
        "https://cn.bing.com/search?q=x",
        "https://example.com/chat",
        "",
    ],
)
def test_is_on_known_chat_domain_negative(url: str) -> None:
    assert not is_on_known_chat_domain(url), url


# ── detect_chat_to_search_drift: positive path (real drift cases) ────────────
def test_fires_on_baidu_s_path() -> None:
    """Baidu home `/s?wd=...` IS a real drift — user landed on
    www.baidu.com (search) instead of chat.baidu.com (chat)."""
    history = [("press_key", 0, "", "https://baidu.com/")]
    result = detect_chat_to_search_drift(
        _decision("click"),
        history,
        current_url="https://www.baidu.com/s?wd=hello&from=home",
        goal="在文心问问 hello",
    )
    assert result is not None
    assert "yiyan.baidu.com" in result["goto_url"]


def test_fires_when_chat_link_routes_to_external_search() -> None:
    """Hypothetical: 'wenxin' link routes to bing.com/search instead of
    the real chat — that IS drift."""
    history = [("press_key", 0, "", "https://example.com/")]
    result = detect_chat_to_search_drift(
        _decision("extract"),
        history,
        current_url="https://cn.bing.com/search?q=test",
        goal="用文心助手回答 test",
    )
    assert result is not None


# ── detect_chat_to_search_drift: must NOT fire on chat-host /search/ ─────────
def test_does_NOT_fire_on_baidu_chat_search_path() -> None:
    """🟢 KEY REGRESSION: Baidu chat.baidu.com **intentionally** routes
    type+Enter to /search/?q=... AS THE NORMAL ANSWER FLOW. The AI answer
    streams in above the search results. We MUST NOT rewrite this to goto
    or we'd tear the VLM off the correct page."""
    history = [
        ("type", 6, "", "https://chat.baidu.com/"),
        ("press_key", 0, "", "https://chat.baidu.com/"),
    ]
    result = detect_chat_to_search_drift(
        _decision("extract"),
        history,
        current_url="https://chat.baidu.com/search/?q=介绍一下deepseek",
        goal="打开文心助手，输入'介绍一下deepseek'，回车，获取回答",
    )
    assert result is None, (
        "Baidu chat /search/ is normal — drift guard must not fire"
    )


def test_does_NOT_fire_on_yiyan_baidu_search() -> None:
    history = [("press_key", 0, "", "https://yiyan.baidu.com/")]
    result = detect_chat_to_search_drift(
        _decision("extract"),
        history,
        current_url="https://yiyan.baidu.com/search/?q=question",
        goal="文心一言 回答 question",
    )
    assert result is None


def test_does_NOT_fire_when_assistant_landed_on_own_chat_host() -> None:
    """Even if URL has search-like keys, if we're already ON the chat
    host, that's the chat's own behavior — don't rewrite."""
    history = [("press_key", 0, "", "https://chat.openai.com/")]
    result = detect_chat_to_search_drift(
        _decision("extract"),
        history,
        current_url="https://chat.openai.com/c/abc?q=x",  # hypothetical
        goal="ChatGPT answer this question",
    )
    assert result is None


def test_fires_for_click_submit_action_too_when_off_chat_host() -> None:
    """Click-based submit on a non-chat domain → still fire."""
    history = [("click", 21, "", "https://www.baidu.com/")]
    result = detect_chat_to_search_drift(
        _decision("extract"),
        history,
        current_url="https://www.baidu.com/s?wd=x",
        goal="文心助手回答 X",
    )
    assert result is not None


# ── Negative paths: must NOT fire ────────────────────────────────────────────
def test_does_not_fire_without_brand_in_goal() -> None:
    """No brand in goal → no known recovery URL → don't trigger."""
    history = [("press_key", 0, "", "https://example.com/")]
    result = detect_chat_to_search_drift(
        _decision("extract"),
        history,
        current_url="https://example.com/search/?q=x",
        goal="提取淘宝商品列表",
    )
    assert result is None


def test_does_not_fire_on_real_chat_page() -> None:
    """If post-submit URL doesn't look like search, all is well."""
    history = [("press_key", 0, "", "https://yiyan.baidu.com/")]
    result = detect_chat_to_search_drift(
        _decision("extract"),
        history,
        current_url="https://yiyan.baidu.com/dialog/123",
        goal="文心 ask DeepSeek question",
    )
    assert result is None


def test_does_not_fire_without_recent_submit() -> None:
    """A scroll or wait before reading the URL doesn't count — only fires
    in the IMMEDIATE aftermath of a submit. Uses a non-chat host so the
    chat-domain bypass doesn't pre-empt this test."""
    history = [
        ("press_key", 0, "", "https://example.com/"),
        ("scroll", 0, "", "https://example.com/search/?q=x"),  # last action is scroll
    ]
    result = detect_chat_to_search_drift(
        _decision("extract"),
        history,
        current_url="https://example.com/search/?q=x",
        goal="用文心助手回答",
    )
    assert result is None


def test_does_not_fire_with_empty_history() -> None:
    result = detect_chat_to_search_drift(
        _decision("click"),
        [],
        current_url="https://example.com/search/?q=x",
        goal="文心",
    )
    assert result is None


def test_does_not_rewrite_existing_recovery_goto() -> None:
    """If the VLM is ALREADY trying to goto the same chat URL, don't
    rewrite — it's already correctly recovering. Test from a real-drift
    URL (baidu.com/s) so the chat-domain bypass doesn't pre-empt."""
    history = [("press_key", 0, "", "https://www.baidu.com/")]
    result = detect_chat_to_search_drift(
        _decision("goto", type_value="https://yiyan.baidu.com"),
        history,
        current_url="https://www.baidu.com/s?wd=x",
        goal="文心助手",
    )
    assert result is None


def test_does_rewrite_existing_goto_to_different_url() -> None:
    """A goto to some unrelated URL should still be rewritten to the
    canonical chat URL — VLM was making a worse choice."""
    history = [("press_key", 0, "", "https://www.baidu.com/")]
    result = detect_chat_to_search_drift(
        _decision("goto", type_value="https://www.bing.com/"),  # not the chat URL
        history,
        current_url="https://www.baidu.com/s?wd=x",
        goal="文心助手",
    )
    assert result is not None


# ── Defensive ────────────────────────────────────────────────────────────────
def test_non_dict_head_safely_returns_none() -> None:
    result = detect_chat_to_search_drift(
        "not a dict",  # type: ignore[arg-type]
        [("press_key", 0, "", "https://x/")],
        current_url="https://x/search/?q=x",
        goal="文心",
    )
    assert result is None


def test_malformed_history_entries_skipped() -> None:
    """Tolerate truncated/None entries; still detect drift on non-chat hosts."""
    history = [
        None,
        ("press_key",),  # too short
        ("press_key", 0, "", "https://www.baidu.com/"),
    ]
    result = detect_chat_to_search_drift(
        _decision("extract"),
        history,
        current_url="https://www.baidu.com/s?wd=x",
        goal="文心",
    )
    assert result is not None


# ── Feedback content (uses real-drift URL: baidu.com/s) ──────────────────────
def test_feedback_includes_concrete_chat_url() -> None:
    history = [("press_key", 0, "", "https://www.baidu.com/")]
    result = detect_chat_to_search_drift(
        _decision("extract"),
        history,
        current_url="https://www.baidu.com/s?wd=x",
        goal="文心助手",
    )
    assert result is not None
    feedback = result["feedback"]
    assert "yiyan.baidu.com" in feedback
    assert "search" in feedback.lower() or "搜索" in feedback
    # Tells the model what we did
    assert "goto" in feedback.lower() or "改写" in feedback


def test_feedback_includes_current_url_for_context() -> None:
    bad_url = "https://www.baidu.com/s?wd=test&from=foo"
    history = [("press_key", 0, "", "https://www.baidu.com/")]
    result = detect_chat_to_search_drift(
        _decision("extract"),
        history,
        current_url=bad_url,
        goal="文心",
    )
    assert result is not None
    # The current URL is quoted in the feedback so the VLM knows what
    # triggered the rewrite.
    assert bad_url[:50] in result["feedback"]

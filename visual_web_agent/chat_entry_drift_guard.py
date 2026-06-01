"""Detect "I'm now on a search page after typing into a chat-shaped input"
and reroute to a known chat URL.

Failure mode covered (run_log_20260514_192447):

  step 1:  click "文心" link on baidu.com         (entry click)
  step 3:  type @e6 "介绍一下deepseek"           (typed into what *looked
                                                  like* a chat input)
  step 5:  press_key Enter                       (URL → /search/?q=...)
  step 6:  VLM correctly notices URL shows /search/, but emits ``extract``
           on the search results (not recovery).
  step 7-16: 11 wasted steps trying to click a "back to chat" entry that
             doesn't exist; eventually 3 consecutive VLM API failures kill
             the agent.

The root cause is Baidu's unreliable routing — the "文心" link sometimes
goes to the real chat (chat.baidu.com) and sometimes to a search-AI hybrid
where the input looks identical but Enter triggers a search. The VLM has
no visual way to distinguish them BEFORE submitting.

But AFTER submitting, the URL itself is the smoking gun: ``/search/`` /
``?q=`` / ``?wd=`` paths can only mean "this is a search results page,
not the chat answer". So we catch it post-submit and rewrite the next
action to ``goto`` a known direct-chat URL.

Two pieces:
  1. ``KNOWN_CHAT_URLS`` — brand keyword → direct chat URL, used both by
     the prompt (for VLM to know) and by this guard (for auto-recovery).
  2. ``detect_chat_to_search_drift`` — fires when the previous action was
     a submission (press_key Enter / click submit) AND the current URL
     matches search-page patterns AND the goal mentions a known brand.
     Returns a rewritten head decision (``goto <chat_url>``) plus a
     feedback string explaining what happened.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ── Known chat / assistant URLs ──────────────────────────────────────────────
# Maintained as ordered dict so longer / more-specific keys win when scanning
# the goal. All values are *direct* chat URLs — NOT marketing landing pages,
# NOT login pages — that the agent can ``goto`` to bypass home-page routing.
KNOWN_CHAT_URLS: dict[str, str] = {
    # Chinese assistants
    "文心一言": "https://yiyan.baidu.com",
    "文心助手": "https://yiyan.baidu.com",
    "文心": "https://yiyan.baidu.com",
    "wenxin": "https://yiyan.baidu.com",
    "yiyan": "https://yiyan.baidu.com",
    "通义千问": "https://tongyi.aliyun.com",
    "通义": "https://tongyi.aliyun.com",
    "tongyi": "https://tongyi.aliyun.com",
    "qwen": "https://chat.qwen.ai",
    "豆包": "https://www.doubao.com/chat",
    "doubao": "https://www.doubao.com/chat",
    "kimi": "https://kimi.moonshot.cn",
    "moonshot": "https://kimi.moonshot.cn",
    "智谱清言": "https://chatglm.cn",
    "chatglm": "https://chatglm.cn",
    "智谱": "https://chatglm.cn",
    "腾讯元宝": "https://yuanbao.tencent.com",
    "元宝": "https://yuanbao.tencent.com",
    "hunyuan": "https://yuanbao.tencent.com",
    "deepseek 对话": "https://chat.deepseek.com",
    "deepseek chat": "https://chat.deepseek.com",
    # English assistants
    "chatgpt": "https://chat.openai.com",
    "chat gpt": "https://chat.openai.com",
    "openai": "https://chat.openai.com",
    "claude": "https://claude.ai/new",
    "anthropic": "https://claude.ai/new",
    "gemini": "https://gemini.google.com/app",
    "copilot": "https://copilot.microsoft.com",
    "perplexity": "https://www.perplexity.ai",
}


# ── Known chat domains where /search/ is part of the normal flow ─────────────
# CRITICAL: Baidu's chat.baidu.com and yiyan.baidu.com submit user questions
# to ``/search/?q=...`` AS THE NORMAL ANSWER FLOW — the AI answer streams in
# above / alongside the search-results panel ("search-then-answer" UX). If
# we treat this as drift, the guard would tear the VLM off the *correct*
# answer page. So: if the post-submit URL is on a known chat host, we DO
# NOT fire regardless of path/query.
#
# These hostnames are the destination of ``KNOWN_CHAT_URLS`` and their
# common variants (with / without ``www.``).
_KNOWN_CHAT_HOSTS: frozenset[str] = frozenset({
    "chat.baidu.com",
    "yiyan.baidu.com",
    "tongyi.aliyun.com",
    "chat.qwen.ai",
    "www.doubao.com",
    "doubao.com",
    "kimi.moonshot.cn",
    "chatglm.cn",
    "www.chatglm.cn",
    "yuanbao.tencent.com",
    "chat.deepseek.com",
    "chat.openai.com",
    "chatgpt.com",
    "www.chatgpt.com",
    "claude.ai",
    "www.claude.ai",
    "gemini.google.com",
    "copilot.microsoft.com",
    "www.perplexity.ai",
    "perplexity.ai",
})


# ── Search-page URL fingerprints ─────────────────────────────────────────────
# Path segments first (stronger signal), then query params (weaker but
# enough when combined with chat context).
_SEARCH_PATH_PATTERNS = (
    "/search/",
    "/search?",
    "/s?",          # baidu.com/s?wd=...
    "/serp",
    "/results",
)
_SEARCH_QUERY_KEYS = ("q", "wd", "query", "keyword", "word", "search", "p")

# Submission-style actions that, when followed by a /search/ URL, indicate
# the "chat-shaped input was actually a search box" trap.
_SUBMIT_ACTIONS = frozenset({"press_key", "click", "click_text", "click_new_tab"})


def is_on_known_chat_domain(url: str) -> bool:
    """True when the URL's host is a known chat / assistant domain where
    a ``/search/`` path is part of the legitimate answer flow (Baidu) or
    simply a chat URL (most others).

    Used to suppress the drift guard — on these domains, a /search/ URL
    is *not* a recovery signal, so rewriting to ``goto`` would be wrong.
    """
    if not url:
        return False
    try:
        host = urlparse(url).netloc.lower().split(":", 1)[0]
    except Exception:
        return False
    if not host:
        return False
    if host in _KNOWN_CHAT_HOSTS:
        return True
    # Accept ``www.`` and other prefix-less forms
    if host.startswith("www."):
        if host[4:] in _KNOWN_CHAT_HOSTS:
            return True
    return False


def looks_like_search_url(url: str) -> bool:
    """True when the URL's path or query indicates a search results page."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    path = (parsed.path or "").lower()
    if any(p in path for p in _SEARCH_PATH_PATTERNS):
        return True
    # Query-based signal: must have BOTH a known search key AND a non-empty
    # value (so ``?q=`` alone with empty value doesn't trip).
    query = (parsed.query or "")
    if query:
        for key in _SEARCH_QUERY_KEYS:
            if re.search(rf"(?:^|&){re.escape(key)}=[^&]+", query, re.I):
                return True
    return False


def infer_chat_url_for_goal(goal: str) -> tuple[str, str] | None:
    """Pick a direct chat URL from ``KNOWN_CHAT_URLS`` based on the goal.

    Returns ``(matched_brand, chat_url)`` or ``None``. Multi-word keys are
    tried first so "文心一言" wins over "文心" if both fragments appear.
    """
    if not goal:
        return None
    text = goal.lower()
    # Sort by key length descending so longer / more-specific keys win.
    ordered = sorted(KNOWN_CHAT_URLS.items(), key=lambda kv: -len(kv[0]))
    for brand, url in ordered:
        if brand.lower() in text:
            return brand, url
    return None


def _last_submit_action(recent_actions: Iterable[tuple]) -> tuple | None:
    """Return the most recent submit-style action tuple, or ``None``."""
    for rec in reversed(list(recent_actions)):
        if not rec or len(rec) < 2:
            continue
        try:
            action = str(rec[0] or "")
        except Exception:
            continue
        if action in _SUBMIT_ACTIONS:
            return rec
        # If the LAST action is non-submit (e.g. wait), stop looking — this
        # guard is only for the immediate-aftermath of a submission. Older
        # submissions in deeper history are someone else's problem.
        return None
    return None


def detect_chat_to_search_drift(
    head_decision: dict[str, Any],
    recent_actions: Iterable[tuple],
    current_url: str,
    goal: str,
) -> dict[str, Any] | None:
    """Return a structured rewrite dict when chat→search drift is detected.

    Returns:
        ``{"feedback": str, "goto_url": str, "matched_brand": str}`` when
        the trap was triggered; ``None`` otherwise.

    Args:
        head_decision: VLM's current decision (will be replaced by goto).
        recent_actions: ``(action, target_id, point_bucket, url)`` history;
            we only look at the most-recent submit-like entry.
        current_url: URL just observed by the VLM.
        goal: full goal text, used to pick which chat URL to recover to.
    """
    if not isinstance(head_decision, dict):
        return None
    # Goal must reference a known chat brand AND we must know its direct URL
    brand_match = infer_chat_url_for_goal(goal)
    if brand_match is None:
        return None
    brand, chat_url = brand_match

    # The most recent action must be a submission
    last = _last_submit_action(recent_actions)
    if last is None:
        return None
    last_action = str(last[0])

    # CRITICAL: on known chat hosts (chat.baidu.com, yiyan.baidu.com,
    # claude.ai, chat.openai.com, …), a /search/ path is **normal**.
    # Baidu's chat in particular shows search results alongside the AI
    # answer streaming in above — rewriting to ``goto`` would tear the
    # VLM off the correct page. Suppress.
    if is_on_known_chat_domain(current_url):
        return None

    # Current URL must scream "search page"
    if not looks_like_search_url(current_url):
        return None

    # Don't bounce repeatedly: if the head ALREADY is a goto to the chat URL,
    # the VLM is already trying to recover — let it through, don't rewrite.
    head_action = str(head_decision.get("action") or "")
    head_type_value = str(head_decision.get("type_value") or "")
    if head_action == "goto":
        chat_host = urlparse(chat_url).netloc.lower()
        try:
            head_host = urlparse(head_type_value).netloc.lower()
        except Exception:
            head_host = ""
        if chat_host and head_host == chat_host:
            return None

    feedback = (
        f"⚠️ [CHAT DRIFT GUARD] 你上一步 {last_action} 提交后，浏览器跳到了搜索结果页：\n"
        f"  当前 URL: {current_url[:140]}\n"
        f"该 URL 的 path/query 显示这是【搜索】页面（含 /search/ 或 ?q=/?wd=），"
        "**不是聊天回复页**。这意味着刚才那个看上去像聊天输入框的东西，"
        f"其实是百度搜索框。\n"
        f"💡 自动恢复：goto 一个已知的 {brand} 聊天 URL "
        f"({chat_url}) — 那是真正的对话入口。\n"
        "本步动作已被系统改写为 goto。"
    )
    logger.warning(
        "[CHAT DRIFT GUARD] %s submit + URL %s → rewriting head to goto %s",
        last_action, (current_url or "")[:80], chat_url,
    )
    return {
        "feedback": feedback,
        "goto_url": chat_url,
        "matched_brand": brand,
    }

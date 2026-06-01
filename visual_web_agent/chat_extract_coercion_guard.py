"""Force ``extract`` → ``chat_extract`` when the page IS a known chat host.

Failure mode covered (run_log_20260515_144253):

  step 5:  URL = chat.baidu.com/search/?q=介绍一下deepseek
           (this is Baidu's "search-then-answer" answer page — the real
           AI answer is in the page, but generic ``extract`` picks up the
           reference-list area beside/below it)
  step 6:  VLM emits ``extract`` (target_id=0, FULL_PAGE:LIST_ITEMS_TEXT) →
           captures `[{"title":"一条视频带新手入门deepseek..."}, ...]`
           — wrong content, search results NOT the AI answer.
  step 7-12: VLM keeps scrolling and clicking, never realises it should
             have used ``chat_extract``; eventually drifts off the page.

Adding more prompt text doesn't help — the VLM keeps reaching for the
familiar ``extract`` verb. The structural fix is to coerce the action at
dispatch time, exactly like CHAT DRIFT GUARD does for goto, but in the
opposite direction (we accept the page, but rewrite the verb).

Behavioural contract
--------------------
Trigger conditions (ALL must hold):
  1. Current page host ∈ ``KNOWN_CHAT_HOSTS`` (from chat_entry_drift_guard).
  2. Head decision's ``action`` ∈ ``{"extract", "extract_link"}``.
  3. Decision did not already come from ``chat_extract`` (idempotency).

When triggered:
  - Returns a rewritten head with ``action="chat_extract"``,
    ``target_id=0``, ``type_value=""`` (handler defaults are good), and
    a ``memory_key`` preserved from the original (or auto-named).
  - Returns a feedback string for ``inject_error_feedback`` so the VLM
    learns for next time.

When NOT triggered (returns ``None``):
  - Page is not on a chat host (the engine has no opinion).
  - VLM already chose ``chat_extract`` / something else (don't double-coerce).
  - We're on the host's *index* / *login* / *about* pages — those are not
    answer pages and don't contain AI replies (path heuristic).

This guard is intentionally narrow: it does NOT touch ``goto``, ``click``,
``type``, ``press_key``, etc. The CHAT DRIFT GUARD covers the post-submit
URL-jump trap; this one covers the post-answer extract-the-wrong-thing trap.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable
from urllib.parse import urlparse

try:
    from .chat_entry_drift_guard import _KNOWN_CHAT_HOSTS, is_on_known_chat_domain
except ImportError:  # pragma: no cover
    from chat_entry_drift_guard import _KNOWN_CHAT_HOSTS, is_on_known_chat_domain  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


# Paths on chat hosts that DON'T contain an answer — login walls, settings,
# the marketing landing root. Coercing extract→chat_extract there would
# either return a sweep of useless chrome or an empty answer; better to
# let the VLM keep its original action.
_NON_ANSWER_PATHS: tuple[str, ...] = (
    "/login",
    "/signin",
    "/signup",
    "/register",
    "/auth",
    "/oauth",
    "/settings",
    "/profile",
    "/about",
    "/help",
    "/faq",
    "/pricing",
    "/terms",
    "/privacy",
)

# Actions we consider "should have been chat_extract on a chat host"
_COERCIBLE_ACTIONS: frozenset[str] = frozenset({"extract", "extract_link"})
_DONE_LIKE_ACTIONS: frozenset[str] = frozenset({"done", "chat_submit", "chat_extract"})


def _has_real_answer_path(url: str) -> bool:
    """True iff the URL's path likely shows an AI answer (or is the answer
    surface itself, like ``/`` on chat.openai.com after the model replied).

    Heuristic: any path is OK unless it's a login/settings/about-style page.
    The home path ``/`` is treated as answer-capable because most chat
    sites stream the reply on the same root URL.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    path = (parsed.path or "/").lower()
    for needle in _NON_ANSWER_PATHS:
        if path.startswith(needle):
            return False
    return True


def detect_extract_on_chat_host(
    head_decision: dict[str, Any],
    current_url: str,
) -> dict[str, Any] | None:
    """Return a structured coercion dict when the trap applies, else ``None``.

    Returns:
        ``{"feedback": str, "memory_key": str, "matched_host": str}`` when
        ``head_decision`` should be rewritten to ``chat_extract``. Caller
        is responsible for actually mutating the decision.

    The function never raises — exceptions during URL parsing yield a clean
    ``None`` so the main loop is unaffected.
    """
    if not isinstance(head_decision, dict):
        return None

    action = str(head_decision.get("action") or "").strip()
    if action not in _COERCIBLE_ACTIONS:
        return None

    if not is_on_known_chat_domain(current_url):
        return None

    if not _has_real_answer_path(current_url):
        return None

    # Extract host for diagnostic + feedback ────────────────────────────
    try:
        host = urlparse(current_url).netloc.lower().split(":", 1)[0]
    except Exception:
        host = ""

    # Preserve / synthesise memory_key — chat_extract requires it
    memory_key = str(head_decision.get("memory_key") or "").strip()
    if not memory_key:
        memory_key = "ai_answer"

    feedback = (
        "🤖 [CHAT EXTRACT GUARD] 当前页是已知聊天/AI 助手域名 "
        f"({host or 'chat host'})，你本步选了 `{action}`，但通用 extract "
        "在这种页面常常抓到搜索结果/参考列表而漏掉 AI 回答正文。\n"
        "💡 系统已把本步动作改写为 `chat_extract`（专用于聊天页：等待流式完成 → "
        "选回答容器 → 排除搜索结果 → 全页 innerText 兜底）。\n"
        f"结果会写到 workflow_memory[{memory_key!r}]。下一步通常直接 action=done。\n"
        "下一轮你自己在 chat 域名上提取回答时请直接选 `chat_extract`，不要走 extract。"
    )

    logger.warning(
        "[CHAT EXTRACT GUARD] coercing %s → chat_extract on host=%s url=%s",
        action, host, (current_url or "")[:80],
    )

    return {
        "feedback": feedback,
        "memory_key": memory_key,
        "matched_host": host,
    }


def detect_premature_chat_done(
    head_decision: dict[str, Any],
    current_url: str,
    *,
    goal: str = "",
    chat_extract_completed: bool = False,
) -> dict[str, Any] | None:
    """Force chat pages through deterministic ``chat_extract`` before done.

    The failure mode is subtle: after ``chat_submit`` succeeds, the next AX
    snapshot may contain partial answer text, internal "thinking" text, or a
    short visible excerpt. VLMs often copy that into ``done.extracted_data`` and
    finish. Some models even emit ``chat_extract`` with their own
    ``extracted_data`` payload, which looks like the right action but still
    bypasses the deterministic handler. That produces wrong-language or
    truncated answers. The runtime must insist on the terminal extractor once
    per answer.
    """
    if chat_extract_completed:
        return None
    if not isinstance(head_decision, dict):
        return None

    action = str(head_decision.get("action") or "").strip()
    if action not in _DONE_LIKE_ACTIONS:
        return None
    if not is_on_known_chat_domain(current_url):
        return None
    if not _has_real_answer_path(current_url):
        return None

    blob = " ".join(
        str(head_decision.get(k) or "")
        for k in ("thought", "progress_review", "current_state", "type_value")
    ).lower()
    goal_blob = str(goal or "").lower()
    extracted = head_decision.get("extracted_data")
    wants_answer = any(
        kw in goal_blob
        for kw in (
            "answer", "reply", "response", "content",
            "回答", "回复", "返回", "内容", "获取", "提取", "输出",
        )
    )
    claims_answer = any(
        kw in blob
        for kw in (
            "ai 已返回", "ai已返回", "ai 回答", "ai回答", "完整回答",
            "回答内容", "回复内容", "answer", "reply", "response",
        )
    )
    doneish = (
        bool(extracted)
        or str(head_decision.get("subgoal_status") or "").lower() == "completed"
        or claims_answer
    )
    if not (wants_answer and doneish):
        return None

    try:
        host = urlparse(current_url).netloc.lower().split(":", 1)[0]
    except Exception:
        host = ""
    memory_key = str(head_decision.get("memory_key") or "").strip() or "ai_answer"
    feedback = (
        "🧾 [CHAT DONE GUARD] 当前是聊天/AI 助手页面，用户目标要求获取回答内容。"
        f"你试图用 `{action}` 结束，并把可见片段或 AX Tree 摘要塞进 extracted_data；"
        "这类内容可能是英文思考过程、截断片段或页面摘要，不是可靠正文。"
        "系统已改写为 `chat_extract`：等待流式回答稳定后，从回答容器中确定性提取完整正文。"
    )
    return {
        "feedback": feedback,
        "memory_key": memory_key,
        "matched_host": host,
        "original_action": action,
    }


# Re-export the host set so the FORCE EXTRACT branch in main.py can ask it
# directly without re-importing chat_entry_drift_guard.
KNOWN_CHAT_HOSTS = _KNOWN_CHAT_HOSTS

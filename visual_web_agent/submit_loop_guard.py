"""Catch VLM re-clicking a submit-like target after the browser state already
changed.

Pattern (observed in run_log_20260514_103433 steps 3-5 and 13-14):

  step N    : click @e2 (搜索)              URL=cn.bing.com/
  step N+1  : click @e2 (搜索) AGAIN        URL=cn.bing.com/search?q=Python教程
                                            ↑ submit already succeeded, but VLM
                                              read the still-loading screenshot
                                              and decided to retry

The existing LOOP GUARD only fires when the SAME ``(action, target_id, url)``
key repeats three times. When the URL changes between attempts (which is
the precise signal that submission succeeded), the LOOP GUARD's per-URL
counter resets and never trips. This module fills that gap: a single
repeat-after-URL-change is enough to be a problem worth surfacing.

Behaviour:
  * Action restricted to interactive click variants (``click``,
    ``click_text``, ``click_new_tab``). Other actions (type, hover,
    press_key, …) are dispatched on different signals.
  * Only the IMMEDIATELY PREVIOUS action is inspected — older history is
    LOOP GUARD's territory.
  * Same ``target_id`` + previous URL ≠ current URL ⇒ fire.
  * URL comparison strips query/fragment so the genuine "navigation
    happened" case is still detected (it's the path/host that changed),
    while query nonces alone don't false-trigger.
  * Adds a "smoking gun" line when the current URL contains common search
    query keys (``q``, ``wd``, ``query`` …) — clear evidence of success.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


_INTERACTIVE_CLICK_ACTIONS = frozenset(
    {"click", "click_text", "click_new_tab"}
)

# Query keys that, if present in the current URL after a re-click, are
# strong indicators that the previous click actually submitted a form.
_QUERY_PARAM_REGEX = re.compile(
    r"[?&](?:q|wd|query|keyword|search|p|text|word|key)=", re.IGNORECASE
)


def _default_url_normalizer(url: str) -> str:
    """Scheme + host + path. Query/fragment dropped so timestamp nonces
    don't fragment the key, but a genuine navigation (path change) does."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
    except Exception:
        return url[:160]


def detect_redundant_click_after_navigation(
    head_decision: dict[str, Any],
    recent_actions: Iterable[tuple],
    current_url: str,
    *,
    url_normalizer: Callable[[str], str] | None = None,
) -> str | None:
    """Return feedback if the head decision is a redundant click after the
    page already navigated; ``None`` otherwise.

    Args:
        head_decision: VLM decision dict to inspect; must have ``action``,
            ``target_id`` keys. Other actions return ``None``.
        recent_actions: iterable of ``(action, target_id, point_bucket, url)``
            tuples, oldest first (main.py's ``_last_actions`` shape).
        current_url: URL the head decision is about to act on.
        url_normalizer: optional URL canonicalization. Default strips
            query/fragment so submission-induced query changes still show as
            a navigation.
    """
    if not isinstance(head_decision, dict):
        return None
    action = str(head_decision.get("action") or "")
    if action not in _INTERACTIVE_CLICK_ACTIONS:
        return None

    try:
        tid = int(head_decision.get("target_id") or 0)
    except (TypeError, ValueError):
        return None
    # target_id == 0 means "no element" for several variants (e.g.
    # click_text uses type_value for the text). Skip those — the
    # cross-page SoM staleness path handles them better.
    if tid <= 0:
        return None

    history = list(recent_actions)
    if not history:
        return None
    last = history[-1]
    if not last or len(last) < 4:
        return None

    try:
        last_action = str(last[0] or "")
        last_tid = int(last[1])
        last_url = str(last[3] or "")
    except (TypeError, ValueError):
        return None
    if last_action != action or last_tid != tid:
        return None
    if not last_url or not current_url:
        return None

    normalize = url_normalizer or _default_url_normalizer
    last_key = normalize(last_url)
    cur_key = normalize(current_url)
    if not last_key or not cur_key:
        return None
    if last_key == cur_key:
        # Same page — LOOP GUARD's job, not ours.
        return None

    has_query_evidence = bool(_QUERY_PARAM_REGEX.search(current_url))
    type_value = str(head_decision.get("type_value") or "").strip()

    lines = [
        f"⚠️ [SUBMIT LOOP GUARD] 你又点了一次 {action}(target_id={tid})"
        f"{f' type_value={type_value!r}' if type_value else ''}，"
        "但浏览器 URL 已经变化：",
        f"  上一步 URL: {last_key[:120]}",
        f"  当前 URL:   {cur_key[:120]}",
    ]
    if has_query_evidence:
        lines.append(
            "  💡 当前 URL 含查询参数 (?q=/?wd=/?query=…) — submit 已生效。"
        )
    lines.extend([
        "🚨 上一步动作其实成功了。当前已是新页面，不要再次点击同一个 target_id：",
        "  • 旧 @eN 红框编号在新页里指向的是不同元素",
        "  • 截图/AX Tree 已是新页 — 重新读，挑当前页该用的下一步动作",
        "  • 如果新页面已满足子目标 → 推进 subgoal_status 或 done",
        "  • 如果需要在新页面取内容 → fetch_link_content / extract 不是再次 click",
    ])
    logger.info(
        "[SUBMIT LOOP] action=%s tid=%s url changed %s → %s",
        action, tid, last_key[:60], cur_key[:60],
    )
    return "\n".join(lines)

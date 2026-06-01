"""Multi-tab session guards: anchor tab + thought vs current URL reality check.

Addresses cases where the VLM trusts its own narrative over the live page URL /
tab index (e.g. claims to be on the start tab while still on a third-party
landing site). Designed to be site-agnostic — recognition is structural
(action verb + destination concept) verified against the live URL, not pinned
to specific brands.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── Goal-side signals: did the user explicitly ask to return to the start tab?
_GOAL_RETURN_TAB_RE = re.compile(
    r"第一个标签页|"
    r"最初[的]?标签|"
    r"切回.{0,8}(?:标签|页|搜索|起始|首页|原)|"
    r"切换回.{0,10}(?:标签|搜索|原页|原标签|起始|首页)|"
    r"回到.{0,8}(?:原标签|第一个标签|起始|首页|搜索)",
    re.I,
)


# ── Thought-side signals: VLM claims to have returned to the start tab.
# Site-agnostic: verb of return + destination concept ("start/first/origin").
_THOUGHT_VERB_OF_RETURN = re.compile(
    r"(?:已|已经)?\s*(?:切换|切|返回|回|跳).{0,3}(?:回|至|到)",
    re.I,
)
_THOUGHT_DEST_IS_START = re.compile(
    r"(?:原|起始|首页|第一个|最初|搜索页|主页|起点)(?:页|标签|tab|标签页)?",
    re.I,
)
_THOUGHT_CLAIMS_CURRENT_AT_START = re.compile(
    r"当前(?:活跃)?(?:标签页|页面|tab)\s*(?:为|是|位于)"
    r"\s*[^，。\.\n]{0,40}?"
    r"(?:原|起始|首页|第一个|最初|搜索页|主页)",
    re.I,
)

# ── Actions that should NOT trigger an anchor pre-empt (tab-mgmt / meta / goto)
_NON_INTERACTIVE_ACTIONS = frozenset(
    {
        "switch_tab",
        "close_tab",
        "wait",
        "done",
        "ask_human",
        "error",
        "goto",
        "extract",
        "save_to_memory",
    }
)


def goal_requests_tab_return(goal: str) -> bool:
    return bool(goal and _GOAL_RETURN_TAB_RE.search(goal))


def infer_tab_session_anchor(goal: str) -> int | None:
    """Anchor the start tab (index 0) when the user explicitly asks to return."""
    if goal_requests_tab_return(goal):
        return 0
    return None


def infer_start_host(start_url: str) -> str:
    try:
        return (urlparse(start_url or "").netloc or "").lower()
    except Exception:
        return ""


def _registrable_suffix(host: str) -> str:
    """Approximate eTLD+1 suffix for naive comparison.

    Examples: ``www.bing.com`` -> ``bing.com``; ``cn.bing.com`` -> ``bing.com``;
    ``google.co.uk`` -> ``google.co.uk`` (kept whole for compound TLD safety).
    """
    if not host:
        return ""
    h = host.lower().split(":", 1)[0]
    parts = h.split(".")
    if len(parts) <= 2:
        return h
    # Compound ccTLDs: keep last 3 labels when the second-last is a known short tld
    _COMPOUND = {"co", "com", "net", "org", "gov", "edu", "ac"}
    if len(parts[-2]) <= 3 and parts[-2] in _COMPOUND and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def url_matches_start_host(url: str, start_host: str) -> bool:
    """True when ``url``'s netloc shares the registrable suffix of ``start_host``.

    Substring matching is intentionally avoided — ``bing.com`` must not match
    ``bingo.com``.
    """
    if not start_host:
        return True
    try:
        url_host = (urlparse(url or "").netloc or "").lower().split(":", 1)[0]
    except Exception:
        return False
    if not url_host:
        return False
    target = _registrable_suffix(start_host)
    if not target:
        return False
    return url_host == target or url_host.endswith("." + target)


def normalize_switch_tab_decision(head: dict[str, Any]) -> None:
    """Sync ``target_id`` and ``type_value`` for switch_tab decisions.

    For switch_tab the authoritative field is ``type_value`` (the tab index as
    a string). If it is a digit, ``target_id`` is brought into alignment. If it
    is missing/non-digit but ``target_id`` is set, ``type_value`` is derived
    from ``target_id``. No site-specific heuristics.
    """
    if str(head.get("action") or "") != "switch_tab":
        return
    try:
        tid = int(head.get("target_id") or 0)
    except (TypeError, ValueError):
        tid = 0
    tv = str(head.get("type_value") or "").strip()
    if tv.isdigit():
        tvi = int(tv)
        if tvi < 0:
            return
        if tid != tvi:
            logger.warning(
                "[SWITCH_TAB FIX] target_id=%s != type_value=%s; "
                "tab index authority is type_value, syncing target_id to %s",
                tid,
                tv,
                tvi,
            )
            head["target_id"] = tvi
        head["type_value"] = str(head["target_id"])
        return
    # type_value missing or non-numeric → derive from target_id if reasonable
    if tid >= 0:
        head["type_value"] = str(tid)


def _thought_claims_at_start(thought: str) -> bool:
    """Detect site-agnostic 'I'm back on the start tab' claims."""
    if not thought:
        return False
    if _THOUGHT_CLAIMS_CURRENT_AT_START.search(thought):
        return True
    return bool(
        _THOUGHT_VERB_OF_RETURN.search(thought)
        and _THOUGHT_DEST_IS_START.search(thought)
    )


def _switch_tab_decision(
    tab_index: int,
    *,
    thought_suffix: str,
    base: dict[str, Any],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "action": "switch_tab",
        "target_id": tab_index,
        "type_value": str(tab_index),
        "thought": thought_suffix + str(base.get("thought") or ""),
        "status": str(base.get("status") or "success"),
        "memory_key": str(base.get("memory_key") or ""),
    }
    for k in ("subgoal_status", "current_state", "point"):
        if k in base:
            out[k] = base[k]
    return out


def apply_tab_session_guards(
    browser: Any,
    decisions: list[dict[str, Any]],
    *,
    goal: str,
    start_url: str,
    tab_anchor_index: int | None,
) -> str | None:
    """Mutate ``decisions`` in place.

    Returns optional VLM feedback when:
    - the head decision is a contradiction with no anchor available to coerce.

    Returns None when:
    - nothing to do, OR
    - the head decision was coerced/prepended to recover state.
    """
    if not decisions:
        return None
    head = decisions[0]
    normalize_switch_tab_decision(head)

    thought = str(head.get("thought") or "")
    try:
        cur_url = str(browser.current_url or "")
    except Exception:
        cur_url = ""
    start_host = infer_start_host(start_url)

    claim_at_start = _thought_claims_at_start(thought)
    url_mismatch = bool(start_host) and not url_matches_start_host(cur_url, start_host)

    # ── Reality check: thought claims return, URL says otherwise ──────────────
    if claim_at_start and url_mismatch:
        msg = (
            "⚠️ [REALITY CHECK] 你的 thought 写「已切回起始/搜索/第一个标签」，"
            f"但浏览器当前 URL 为：{cur_url[:180]}\n"
            f"该 URL 与任务起始域（{start_host}）不匹配。"
            "请以 URL 与【当前标签页列表】为准；要回起点必须先 switch_tab。"
        )
        if tab_anchor_index is not None:
            logger.info(
                "[TAB SESSION] coercing head -> switch_tab(%s) "
                "(claim_at_start && URL=%s mismatches start_host=%s)",
                tab_anchor_index,
                cur_url[:80],
                start_host,
            )
            decisions[0] = _switch_tab_decision(
                tab_anchor_index,
                thought_suffix=f"[REALITY CHECK 引擎改写] {msg}\n",
                base=head,
            )
            return None
        return msg

    # ── Anchor pre-empt: user wants to return to start, but VLM is acting on
    # the wrong tab. Pre-empt any element-targeted action with a switch_tab.
    if tab_anchor_index is None:
        return None
    act = str(head.get("action") or "")
    if act in _NON_INTERACTIVE_ACTIONS:
        return None
    try:
        active_idx = int(browser.get_active_tab_index())
    except Exception:
        active_idx = -1
    if active_idx < 0 or active_idx == tab_anchor_index:
        return None

    stub = _switch_tab_decision(
        tab_anchor_index,
        thought_suffix=(
            f"[TAB ANCHOR] 目标要求在起始标签（{tab_anchor_index}）上操作，"
            f"但当前活动标签为 {active_idx}；先切回锚定标签再继续。\n"
        ),
        base=head,
    )
    decisions.insert(0, stub)
    logger.info(
        "[TAB ANCHOR] prepended switch_tab(%s) before %s on tab %s",
        tab_anchor_index,
        act,
        active_idx,
    )
    return None

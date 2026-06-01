"""Detect VLM stuck in a switch_tab(N) loop and break it.

Failure mode (run_log_20260514_183718 steps 10-18):

  step 10:  switch_tab(1)  status=success
  step 11:  press_key Enter
  step 12:  switch_tab(1)  status=success   ← same tab, VLM thinks it didn't work
  step 13:  click @e21
  step 14:  switch_tab(1)  status=success
  ...

Each switch_tab fires correctly (Playwright reports success), but the VLM's
mental model of *what's on tab 1* is wrong. The destination tab is not the
Wenxin chat page the VLM expects — it's a "rules / terms" page that
happens to be at index 1. So the VLM keeps re-emitting switch_tab(1)
hoping for a different outcome.

This module:
  * Counts consecutive ``switch_tab(target_id=N)`` decisions for the same
    ``N`` (ignoring intervening non-switch_tab actions only when they're
    explicitly within the "still on wrong tab" diagnostic pattern, i.e.
    the user already saw the destination and keeps coming back).
  * Triggers at ``threshold=2`` (default): one harmless retry is fine,
    two means the VLM is wrong about the destination's identity.
  * Returns a feedback string telling the VLM "tab N is what it is —
    update your mental model from the current tab list" plus a strong
    suggestion to *use a different action* (close_tab the wrong tab,
    goto a known URL, or done if state matches).

Sibling guard to ``wait_loop_guard`` (waits) and ``submit_loop_guard``
(re-clicks). Together they cover the three flavors of "VLM doesn't trust
its own success and retries".
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def _read_target_id(decision: dict[str, Any]) -> int | None:
    """target_id for switch_tab can be in either ``target_id`` or, in the
    half-broken convention some VLMs use, ``type_value``. Accept both."""
    if not isinstance(decision, dict):
        return None
    try:
        tid = int(decision.get("target_id") or 0)
    except (TypeError, ValueError):
        tid = -1
    if tid > 0:
        return tid
    tv = str(decision.get("type_value") or "").strip()
    if tv.isdigit():
        return int(tv)
    if tid == 0:
        # target_id=0 type_value="0" is the canonical "switch to tab 0" form
        # (since target_id=0 has the meta meaning of "no element"). It IS a
        # real index — accept it only when ``type_value`` confirms 0.
        if tv == "0":
            return 0
    return None


def detect_switch_tab_loop(
    head_decision: dict[str, Any],
    recent_actions: Iterable[tuple],
    *,
    threshold: int = 2,
) -> str | None:
    """Return feedback when the head is the (threshold)th consecutive
    ``switch_tab(N)`` for the same N; otherwise ``None``.

    Args:
        head_decision: VLM decision dict; must have ``action`` etc.
        recent_actions: ``(action, target_id, point_bucket, url)`` tuples,
            oldest first (main.py ``_last_actions`` shape).
        threshold: number of consecutive switch_tabs at which to fire. Two
            means "the VLM emitted switch_tab(N) twice in a row" — the
            first one was already a retry; one more is the loop signal.
    """
    if not isinstance(head_decision, dict):
        return None
    if str(head_decision.get("action") or "") != "switch_tab":
        return None
    head_tid = _read_target_id(head_decision)
    if head_tid is None:
        return None
    if threshold < 2:
        return None

    history = list(recent_actions)
    needed = threshold - 1
    if len(history) < needed:
        return None

    # Walk the tail of history; tolerate ``len(rec) >= 2`` shape so older
    # truncated entries don't crash the check.
    consecutive = 0
    for rec in reversed(history):
        if not rec or len(rec) < 2:
            break
        try:
            rec_action = str(rec[0])
            rec_tid = int(rec[1])
        except (TypeError, ValueError):
            break
        if rec_action != "switch_tab" or rec_tid != head_tid:
            break
        consecutive += 1
        if consecutive >= needed:
            break

    if consecutive < needed:
        return None

    logger.info(
        "[SWITCH TAB LOOP] head=switch_tab(%s) is the %sth consecutive same-target switch",
        head_tid, consecutive + 1,
    )
    return (
        f"⚠️ [SWITCH TAB LOOP GUARD] 你已经连续 {consecutive + 1} 次 "
        f"switch_tab(target_id={head_tid})。\n"
        f"每次的 status=success — Playwright 确实把焦点切到了 [{head_tid}]。\n"
        f"🚨 问题不是 switch 没生效，而是 **[{head_tid}] 这个 tab 的内容**和"
        f"你 thought 里想象的不一样。\n"
        "从 prompt 顶部的 TAB STATE CHANGE / 当前标签页列表读出 "
        f"[{head_tid}] 的真实 title 和 URL，更新心智模型，然后选一个**不是 "
        f"switch_tab({head_tid}) 的动作**：\n"
        f"  • 若 [{head_tid}] 真的是错误页 → close_tab({head_tid}) 并尝试其他入口\n"
        "  • 若任务可以在当前页继续 → 直接操作当前页元素\n"
        "  • 若已无可行路径 → goto 一个已知正确的 URL（如 yiyan.baidu.com）\n"
        "  • 若状态已满足目标 → 直接 done"
    )

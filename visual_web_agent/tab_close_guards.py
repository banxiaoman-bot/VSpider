"""Guards for tab-title actions accidentally emitted as DOM clicks."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_CLOSE_TAB_GOAL_RE = re.compile(r"(?:关闭|关掉|close).{0,20}(?:标签|tab|新标签|窗口)")
_TAB_INDEX_RE = re.compile(r"(?:标签页|tab|\[)\s*\[?(\d+)\]?")
_CLOSE_INTENT_RE = re.compile(r"(?:关闭|关掉|close).{0,40}(?:标签|tab|新标签|窗口|它|这个|该页)")
_KEEP_CURRENT_RE = re.compile(r"(?:不要|不|别|勿).{0,12}(?:关闭|关掉|close).{0,12}(?:当前|本|现在).{0,8}(?:页|标签|tab)")
_CLOSE_TAB_GOAL_RE = re.compile(_CLOSE_TAB_GOAL_RE.pattern, re.I)
_TAB_INDEX_RE = re.compile(_TAB_INDEX_RE.pattern, re.I)
_CLOSE_INTENT_RE = re.compile(_CLOSE_INTENT_RE.pattern, re.I)
_KEEP_CURRENT_RE = re.compile(_KEEP_CURRENT_RE.pattern, re.I)


def _open_pages(browser: Any) -> list[Any]:
    ctx = getattr(browser, "_context", None)
    if not ctx:
        return []
    return [p for p in getattr(ctx, "pages", []) if not p.is_closed()]


async def rewrite_redundant_close_to_done(
    browser: Any,
    decisions: list[dict[str, Any]],
    *,
    goal: str,
) -> bool:
    """Stop a final close-tab loop after the target non-current tab is gone."""
    if not decisions or not _CLOSE_TAB_GOAL_RE.search(goal or ""):
        return False
    if not _KEEP_CURRENT_RE.search(goal or ""):
        return False
    head = decisions[0]
    if str(head.get("action") or "") != "close_tab":
        return False
    if len(_open_pages(browser)) != 1:
        return False
    logger.info("[TAB CLOSE GUARD] only current tab remains; rewriting close_tab -> done")
    decisions[0] = {
        **head,
        "action": "done",
        "target_id": 0,
        "type_value": "done",
        "thought": (
            "[TAB CLOSE GUARD] The non-current result tab is already closed, the "
            "current page is preserved, and the requested tab-close work is complete."
        ),
        "progress_review": "The requested non-current tab is already closed; finish.",
        "current_state": "Only the required current page remains open.",
        "subgoal_status": "completed",
        "status": "success",
    }
    return True


def _intent_text(head: dict[str, Any]) -> str:
    return " ".join(
        str(head.get(k) or "")
        for k in ("thought", "progress_review", "current_state", "type_value")
    )


def _active_index(open_pages: list[Any], page: Any) -> int:
    try:
        return open_pages.index(page)
    except ValueError:
        return -1


def _extract_tab_index(text: str, open_count: int, active_idx: int) -> int | None:
    for match in _TAB_INDEX_RE.finditer(text or ""):
        try:
            idx = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < open_count and idx != active_idx:
            return idx
    return None


async def rewrite_close_intent_to_close_tab(
    browser: Any,
    decisions: list[dict[str, Any]],
    *,
    goal: str,
) -> bool:
    """Rewrite any clear non-current-tab close intent into ``close_tab``.

    This catches the common failure where the model understands "close tab 1"
    in thought/progress fields but still emits a normal DOM click.
    """
    if not decisions or not _CLOSE_TAB_GOAL_RE.search(goal or ""):
        return False
    head = decisions[0]
    action = str(head.get("action") or "")
    if action in {"close_tab", "done", "ask_human", "error"}:
        return False
    intent = _intent_text(head)
    if not _CLOSE_INTENT_RE.search(intent):
        return False

    page = getattr(browser, "_page", None)
    if not page:
        return False
    open_pages = _open_pages(browser)
    if len(open_pages) <= 1:
        return False
    active_idx = _active_index(open_pages, page)

    chosen_idx = _extract_tab_index(intent, len(open_pages), active_idx)
    if chosen_idx is None:
        non_current = [idx for idx in range(len(open_pages)) if idx != active_idx]
        if len(non_current) == 1:
            chosen_idx = non_current[0]
    if chosen_idx is None:
        return False

    logger.info(
        "[TAB CLOSE GUARD] rewriting %s#%s -> close_tab[%s] from close intent",
        action,
        head.get("target_id"),
        chosen_idx,
    )
    decisions[0] = {
        **head,
        "action": "close_tab",
        "target_id": 0,
        "type_value": str(chosen_idx),
        "thought": (
            "[TAB CLOSE GUARD] Decision text says to close a browser tab; "
            f"rewriting DOM action {action!r} to close_tab for tab index {chosen_idx}."
        ),
    }
    return True


async def rewrite_tab_title_click_to_close_tab(
    browser: Any,
    decisions: list[dict[str, Any]],
    *,
    goal: str,
) -> bool:
    """Rewrite ``click_text`` on a browser-tab title into ``close_tab``.

    Browser tab titles are not DOM text. When the model says it wants to close a
    non-current tab but emits ``click_text(type_value=<tab title>)``, executing
    that click only searches the current page and fails. This guard keeps the
    rewrite narrow: it only fires for close-tab goals, target_id=0, and a
    non-current open tab whose title contains the requested text.
    """
    if not decisions or not _CLOSE_TAB_GOAL_RE.search(goal or ""):
        return False
    head = decisions[0]
    if str(head.get("action") or "") != "click_text":
        return False
    try:
        target_id = int(head.get("target_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    if target_id != 0:
        return False

    wanted = str(head.get("type_value") or "").strip()
    thought = str(head.get("thought") or "")
    if not wanted and not thought:
        return False
    intent_text = " ".join(
        str(head.get(k) or "")
        for k in ("thought", "progress_review", "current_state")
    )
    if not _CLOSE_INTENT_RE.search(intent_text):
        return False

    page = getattr(browser, "_page", None)
    if not page:
        return False
    open_pages = _open_pages(browser)
    if len(open_pages) <= 1:
        return False
    try:
        active_idx = open_pages.index(page)
    except ValueError:
        active_idx = -1

    chosen_idx: int | None = None
    if wanted:
        wanted_low = wanted.casefold()
        for idx, p in enumerate(open_pages):
            if idx == active_idx:
                continue
            try:
                title = (await p.title()) or ""
            except Exception:
                title = ""
            if wanted_low in title.casefold():
                chosen_idx = idx
                break

    if chosen_idx is None:
        matches = [int(m.group(1)) for m in _TAB_INDEX_RE.finditer(thought)]
        for idx in matches:
            if 0 <= idx < len(open_pages) and idx != active_idx:
                chosen_idx = idx
                break

    if chosen_idx is None:
        return False

    logger.info(
        "[TAB CLOSE GUARD] rewriting click_text(%r) -> close_tab[%s]",
        wanted,
        chosen_idx,
    )
    decisions[0] = {
        **head,
        "action": "close_tab",
        "target_id": 0,
        "type_value": str(chosen_idx),
        "thought": (
            "[TAB CLOSE GUARD] Browser tab titles are not clickable DOM text; "
            f"rewriting to close_tab for tab index {chosen_idx}.\n"
            + thought
        ),
    }
    return True

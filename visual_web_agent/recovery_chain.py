"""Multi-strategy recovery chain — try deterministic paths before re-asking VLM."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _decision(
    action: str,
    *,
    target_id: int = 0,
    type_value: str = "",
    thought: str,
) -> dict[str, Any]:
    return {
        "action": action,
        "target_id": target_id,
        "type_value": type_value,
        "memory_key": "",
        "thought": thought,
        "status": "success",
        "progress_review": "",
        "current_state": "",
        "subgoal_status": "in_progress",
    }


async def attempt_stuck_recovery_chain(
    browser: Any,
    *,
    goal: str,
    start_url: str,
    tab_anchor_index: int | None,
) -> list[dict[str, Any]] | None:
    """Return executable decisions when a deterministic recovery path exists."""
    try:
        from .tab_session_guards import infer_start_host, url_matches_start_host
    except ImportError:
        from tab_session_guards import infer_start_host, url_matches_start_host

    start_host = infer_start_host(start_url)
    try:
        cur_url = str(browser.current_url or "")
    except Exception:
        cur_url = ""
    try:
        active_idx = int(browser.get_active_tab_index())
    except Exception:
        active_idx = -1

    # 1) Tab anchor — wrong tab while user asked to return to start
    if tab_anchor_index is not None and active_idx >= 0 and active_idx != tab_anchor_index:
        logger.info(
            "[RECOVERY CHAIN] tab_anchor: active=%s anchor=%s",
            active_idx,
            tab_anchor_index,
        )
        return [
            _decision(
                "switch_tab",
                target_id=tab_anchor_index,
                type_value=str(tab_anchor_index),
                thought=(
                    f"[RECOVERY CHAIN] 先切回锚定标签 {tab_anchor_index}，"
                    "再基于当前截图继续任务。"
                ),
            )
        ]

    on_start_host = url_matches_start_host(cur_url, start_host)

    # 2) Search re-entry — user asked to type a new query on the start search page
    if on_start_host:
        try:
            from .search_reentry_guards import (
                apply_search_reentry_guard,
                parse_reentry_query,
                _current_search_input_state,
            )
        except ImportError:
            from search_reentry_guards import (
                apply_search_reentry_guard,
                parse_reentry_query,
                _current_search_input_state,
            )
        reentry_query = parse_reentry_query(goal)
        if reentry_query:
            state = await _current_search_input_state(browser)
            tid = int((state or {}).get("target_id") or 0)
            if tid > 0:
                stub = [
                    _decision(
                        "type",
                        target_id=tid,
                        type_value=reentry_query,
                        thought=(
                            f"[RECOVERY CHAIN] 在搜索框输入「{reentry_query}」"
                            "（绕开 VLM 重复失败路径）。"
                        ),
                    ),
                    _decision(
                        "press_key",
                        target_id=0,
                        type_value="Enter",
                        thought="[RECOVERY CHAIN] 提交搜索。",
                    ),
                ]
                await apply_search_reentry_guard(browser, stub, goal=goal)
                if stub[0].get("action") == "type":
                    logger.info("[RECOVERY CHAIN] search re-entry type+Enter")
                    return stub

    # 3) First organic SERP result — DOM probe instead of stale @eN click
    if on_start_host:
        try:
            from .search_result_guards import (
                _probe_first_organic_result,
                goal_requests_first_result_new_tab,
            )
            from .search_reentry_guards import parse_first_search_query
        except ImportError:
            from search_result_guards import (
                _probe_first_organic_result,
                goal_requests_first_result_new_tab,
            )
            from search_reentry_guards import parse_first_search_query
        if goal_requests_first_result_new_tab(goal, None):
            expected_query = parse_first_search_query(goal)
            if expected_query:
                result = await _probe_first_organic_result(browser, expected_query)
                tid = int((result or {}).get("target_id") or 0)
                if tid > 0:
                    href = str((result or {}).get("href") or "")[:120]
                    logger.info("[RECOVERY CHAIN] organic click_new_tab #%s %s", tid, href)
                    return [
                        _decision(
                            "click_new_tab",
                            target_id=tid,
                            thought=(
                                "[RECOVERY CHAIN] DOM 选中首个 organic 结果并新标签打开。"
                            ),
                        )
                    ]

    return None

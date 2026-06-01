"""Tests for recovery_chain (offline logic only)."""

from __future__ import annotations

import asyncio

from visual_web_agent.recovery_chain import attempt_stuck_recovery_chain


class _BrowserTabWrong:
    def __init__(self) -> None:
        self.current_url = "https://evil.example/landing"

    def get_active_tab_index(self) -> int:
        return 2


def test_recovery_chain_tab_anchor_first() -> None:
    br = _BrowserTabWrong()
    out = asyncio.run(
        attempt_stuck_recovery_chain(
            br,
            goal="切回第一个标签页再搜索",
            start_url="https://www.bing.com/",
            tab_anchor_index=0,
        )
    )
    assert out is not None
    assert out[0]["action"] == "switch_tab"
    assert out[0]["target_id"] == 0

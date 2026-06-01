"""Unit tests for nav_resilience."""

from __future__ import annotations

import asyncio

from visual_web_agent.nav_resilience import navigate_with_retry


class _FakePage:
    def __init__(self, outcomes: list[str | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.url = ""
        self.calls: list[tuple[str, str, int]] = []

    async def goto(self, url: str, *, wait_until: str, timeout: int):
        self.calls.append((url, wait_until, timeout))
        if not self._outcomes:
            raise RuntimeError("no more outcomes")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.url = outcome
        return None


def test_first_strategy_success() -> None:
    page = _FakePage(["https://example.com/ok"])
    result = asyncio.run(navigate_with_retry(page, "https://example.com"))
    assert result.success is True
    assert result.final_strategy == "domcontentloaded"
    assert result.degraded is False
    assert len(page.calls) == 1


def test_degrades_to_commit() -> None:
    page = _FakePage([RuntimeError("timeout"), RuntimeError("timeout"), "https://x/"])
    result = asyncio.run(navigate_with_retry(page, "https://x"))
    assert result.success is True
    assert result.final_strategy == "commit"
    assert result.degraded is True
    assert len(page.calls) == 3


def test_all_strategies_fail() -> None:
    page = _FakePage([RuntimeError("a"), RuntimeError("b"), RuntimeError("c")])
    result = asyncio.run(navigate_with_retry(page, "https://fail"))
    assert result.success is False
    assert len(result.attempts) == 3

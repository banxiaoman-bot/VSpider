"""Tests for bot_challenge_guard (offline classifier + stub browser)."""

from __future__ import annotations

import asyncio
from typing import Any

from visual_web_agent.bot_challenge_guard import (
    BotChallengeState,
    classify_probe_payload,
    handle_bot_challenge_step,
    probe_bot_challenge,
)


class _StubPage:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def evaluate(self, _js: str) -> dict[str, Any]:
        return dict(self._payload)


class _StubBrowser:
    def __init__(self, payloads: list[dict[str, Any] | None]) -> None:
        self._payloads = list(payloads)
        self._idx = 0

    async def _ensure_active_page(self, reason: str = "") -> _StubPage | None:
        if self._idx >= len(self._payloads):
            return _StubPage({"url": "https://example.com/", "title": "OK", "vendor": "", "detected": False})
        raw = self._payloads[self._idx]
        self._idx += 1
        if raw is None:
            return None
        return _StubPage(raw)


def test_classify_cloudflare_turnstile() -> None:
    probe = classify_probe_payload({
        "url": "https://example.com/",
        "title": "Just a moment...",
        "vendor": "cloudflare",
        "detected": True,
        "cloudflare": True,
        "turnstile": True,
    })
    assert probe is not None
    assert probe.vendor == "cloudflare"


def test_classify_cf_url_without_vendor_field() -> None:
    probe = classify_probe_payload({
        "url": "https://example.com/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1",
        "title": "Wait",
        "detected": False,
    })
    assert probe is not None
    assert probe.vendor == "cloudflare"


def test_classify_clean_page_returns_none() -> None:
    assert classify_probe_payload({
        "url": "https://www.bing.com/search?q=cat",
        "title": "cat - Search",
        "detected": False,
    }) is None


def test_passive_wait_clears_without_hitl() -> None:
    cf = {
        "url": "https://example.com/",
        "title": "Checking your browser",
        "vendor": "cloudflare",
        "detected": True,
        "cloudflare": True,
    }
    clean = {
        "url": "https://example.com/",
        "title": "Home",
        "detected": False,
    }
    br = _StubBrowser([cf, clean])
    state = BotChallengeState()
    hitl_called: list[str] = []

    async def _hitl(reason: str) -> None:
        hitl_called.append(reason)

    result = asyncio.run(
        handle_bot_challenge_step(
            br,
            state,
            hitl_callback=_hitl,
            passive_wait_seconds=0.01,
            poll_interval=0.01,
        )
    )
    assert result.detected is True
    assert result.cleared is True
    assert result.action == "passive_wait"
    assert hitl_called == []


def test_hitl_when_challenge_persists() -> None:
    cf = {
        "url": "https://example.com/",
        "title": "Just a moment...",
        "vendor": "cloudflare",
        "detected": True,
        "cloudflare": True,
    }
    br = _StubBrowser([cf, cf, cf])
    state = BotChallengeState()
    hitl_called: list[str] = []

    async def _hitl(reason: str) -> None:
        hitl_called.append(reason)

    result = asyncio.run(
        handle_bot_challenge_step(
            br,
            state,
            hitl_callback=_hitl,
            passive_wait_seconds=0.0,
            poll_interval=0.01,
        )
    )
    assert result.action == "hitl"
    assert len(hitl_called) == 1
    assert "Cloudflare" in hitl_called[0]


def test_probe_bot_challenge_returns_none_on_clean() -> None:
    br = _StubBrowser([{
        "url": "https://example.com/",
        "title": "Example",
        "detected": False,
    }])
    out = asyncio.run(probe_bot_challenge(br))
    assert out is None

"""Vendor-dispatch regression tests for the anti-bot challenge closed loop.

Complements ``test_bot_challenge_guard.py`` (which focuses on Cloudflare
passive-wait + early-clearance signals) by exercising the *per-vendor
branching* of ``handle_bot_challenge_step`` and the capability-router
``bot_challenge`` route that drives the loop:

* classification dispatch: cloudflare / turnstile / recaptcha / hcaptcha /
  generic 验证码 each land on the right ``vendor``.
* solver branch is attempted only for the vendors that have a sitekey
  contract (``cloudflare`` / ``captcha``) and skipped for reCAPTCHA /
  hCaptcha (those go straight to HITL).
* solver success clears without HITL; solver failure falls through to HITL.
* the per-run HITL budget caps escalation (``max_hitl``).
* the deterministic router emits the ``bot_challenge`` signal and ranks
  ``bot_challenge_guard`` in the backend plan / fallback chain / risk flags
  for anti-bot goals, but never for a plain extraction goal.

All offline: a tiny stub browser feeds scripted probe payloads; the
third-party solver is monkeypatched so no network is touched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from visual_web_agent import captcha_solver as captcha_solver_mod
from visual_web_agent.bot_challenge_guard import (
    BotChallengeState,
    _build_notice,
    _vendor_label,
    classify_probe_payload,
    handle_bot_challenge_step,
)
from visual_web_agent.capability_router import route_task
from visual_web_agent.captcha_solver import CaptchaSolveResult
from visual_web_agent.planner_contract import build_execution_plan


# ---------------------------------------------------------------------------
# Stub browser: scripted probe payloads, no real page evaluation
# ---------------------------------------------------------------------------


class _StubPage:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def evaluate(self, _js: str, *args: Any) -> Any:
        return dict(self._payload)


class _StubBrowser:
    """Returns one scripted probe payload per ``_ensure_active_page`` call.

    ``None`` payloads model "no active page"; once the script is exhausted a
    clean (non-challenge) page is returned so trailing probes read as cleared.
    """

    def __init__(self, payloads: list[dict[str, Any] | None]) -> None:
        self._payloads = list(payloads)
        self._idx = 0
        self.page_calls = 0

    async def _ensure_active_page(self, reason: str = "") -> _StubPage | None:
        self.page_calls += 1
        if self._idx >= len(self._payloads):
            return _StubPage({"url": "https://site.com/", "title": "OK", "vendor": "", "detected": False})
        raw = self._payloads[self._idx]
        self._idx += 1
        if raw is None:
            return None
        return _StubPage(raw)


def _probe(vendor: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "url": "https://site.com/",
        "title": "Just a moment...",
        "vendor": vendor,
        "detected": True,
    }
    payload.update(extra)
    return payload


def _run(coro):
    return asyncio.run(coro)


async def _noop_hitl(reason: str) -> None:  # pragma: no cover - trivial
    return None


# ---------------------------------------------------------------------------
# 1. Classification dispatch per vendor
# ---------------------------------------------------------------------------


def test_classify_turnstile_only_payload_is_cloudflare() -> None:
    probe = classify_probe_payload(_probe("", detected=True, turnstile=True, cloudflare=False))
    assert probe is not None
    assert probe.vendor == "cloudflare"


def test_classify_recaptcha_payload() -> None:
    probe = classify_probe_payload(_probe("", detected=True, recaptcha=True))
    assert probe is not None
    assert probe.vendor == "recaptcha"


def test_classify_hcaptcha_payload() -> None:
    probe = classify_probe_payload(_probe("", detected=True, hcaptcha=True))
    assert probe is not None
    assert probe.vendor == "hcaptcha"


def test_classify_generic_captcha_text_payload() -> None:
    probe = classify_probe_payload(_probe("", detected=True, captcha_text=True))
    assert probe is not None
    assert probe.vendor == "captcha"


@pytest.mark.parametrize(
    "vendor,label",
    [
        ("cloudflare", "Cloudflare 人机验证"),
        ("recaptcha", "reCAPTCHA"),
        ("hcaptcha", "hCaptcha"),
        ("captcha", "验证码/风控页"),
    ],
)
def test_vendor_labels_and_notices(vendor: str, label: str) -> None:
    assert _vendor_label(vendor) == label
    probe = classify_probe_payload(_probe(vendor, **{vendor if vendor != "captcha" else "captcha_text": True}))
    assert probe is not None
    notice = _build_notice(probe, action="hitl")
    assert label in notice


# ---------------------------------------------------------------------------
# 2. reCAPTCHA / hCaptcha skip the solver and go straight to HITL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vendor", ["recaptcha", "hcaptcha"])
def test_recaptcha_hcaptcha_skip_solver_and_hitl(vendor: str, monkeypatch) -> None:
    solver_calls: list[str] = []

    async def _spy_solver(page, *, page_url):  # pragma: no cover - should not run
        solver_calls.append(page_url)
        return CaptchaSolveResult(True, provider="capsolver", token="t")

    monkeypatch.setattr(captcha_solver_mod, "solver_enabled", lambda: True)
    monkeypatch.setattr(captcha_solver_mod, "try_solve_turnstile", _spy_solver)

    payload = _probe(vendor, **{vendor: True})
    br = _StubBrowser([payload, payload])
    hitl_called: list[str] = []

    async def _hitl(reason: str) -> None:
        hitl_called.append(reason)

    result = _run(
        handle_bot_challenge_step(
            br, BotChallengeState(), hitl_callback=_hitl,
            passive_wait_seconds=0.0, poll_interval=0.01,
        )
    )
    assert result.vendor == vendor
    assert result.action == "hitl"
    assert len(hitl_called) == 1
    # solver must not be attempted for non-sitekey vendors
    assert solver_calls == []


# ---------------------------------------------------------------------------
# 3. Cloudflare solver branch: success clears without HITL
# ---------------------------------------------------------------------------


def test_cloudflare_solver_success_clears_without_hitl(monkeypatch) -> None:
    async def _ok_solver(page, *, page_url):
        return CaptchaSolveResult(True, provider="capsolver", token="tok-123")

    monkeypatch.setattr(captcha_solver_mod, "solver_enabled", lambda: True)
    monkeypatch.setattr(captcha_solver_mod, "try_solve_turnstile", _ok_solver)

    cf = _probe("cloudflare", cloudflare=True)
    clean = {"url": "https://site.com/", "title": "Home", "detected": False}
    # idx0 initial probe (cf), idx1 solver page (cf), idx2 recheck (clean)
    br = _StubBrowser([cf, cf, clean])
    hitl_called: list[str] = []

    async def _hitl(reason: str) -> None:
        hitl_called.append(reason)

    result = _run(
        handle_bot_challenge_step(
            br, BotChallengeState(), hitl_callback=_hitl,
            passive_wait_seconds=0.0, poll_interval=0.01,
        )
    )
    assert result.action == "solver"
    assert result.cleared is True
    assert "capsolver" in result.notice
    assert hitl_called == []


# ---------------------------------------------------------------------------
# 4. Cloudflare solver failure falls through to HITL
# ---------------------------------------------------------------------------


def test_cloudflare_solver_failure_falls_through_to_hitl(monkeypatch) -> None:
    async def _bad_solver(page, *, page_url):
        return CaptchaSolveResult(False, provider="capsolver", reason="timeout")

    monkeypatch.setattr(captcha_solver_mod, "solver_enabled", lambda: True)
    monkeypatch.setattr(captcha_solver_mod, "try_solve_turnstile", _bad_solver)

    cf = _probe("cloudflare", cloudflare=True)
    br = _StubBrowser([cf, cf, cf])
    hitl_called: list[str] = []

    async def _hitl(reason: str) -> None:
        hitl_called.append(reason)

    result = _run(
        handle_bot_challenge_step(
            br, BotChallengeState(), hitl_callback=_hitl,
            passive_wait_seconds=0.0, poll_interval=0.01,
        )
    )
    assert result.action == "hitl"
    assert len(hitl_called) == 1
    assert "Cloudflare" in hitl_called[0]


# ---------------------------------------------------------------------------
# 5. Generic captcha vendor is solver-eligible too
# ---------------------------------------------------------------------------


def test_generic_captcha_attempts_solver(monkeypatch) -> None:
    solver_calls: list[str] = []

    async def _ok_solver(page, *, page_url):
        solver_calls.append(page_url)
        return CaptchaSolveResult(True, provider="2captcha", token="tok")

    monkeypatch.setattr(captcha_solver_mod, "solver_enabled", lambda: True)
    monkeypatch.setattr(captcha_solver_mod, "try_solve_turnstile", _ok_solver)

    cap = _probe("captcha", captcha_text=True)
    clean = {"url": "https://site.com/", "title": "Home", "detected": False}
    br = _StubBrowser([cap, cap, clean])

    result = _run(
        handle_bot_challenge_step(
            br, BotChallengeState(), hitl_callback=_noop_hitl,
            passive_wait_seconds=0.0, poll_interval=0.01,
        )
    )
    assert result.action == "solver"
    assert result.vendor == "captcha"
    assert solver_calls, "captcha vendor must be solver-eligible"


# ---------------------------------------------------------------------------
# 6. Per-run HITL budget caps escalation
# ---------------------------------------------------------------------------


def test_max_hitl_budget_blocks_further_escalation() -> None:
    recap = _probe("recaptcha", recaptcha=True)
    br = _StubBrowser([recap])
    state = BotChallengeState(max_hitl_per_run=2)
    state.hitl_count = 2  # budget already spent

    result = _run(
        handle_bot_challenge_step(
            br, state, hitl_callback=_noop_hitl,
            passive_wait_seconds=0.0, poll_interval=0.01,
        )
    )
    assert result.action == "max_hitl"
    assert result.cleared is False
    assert "上限" in result.notice


def test_solver_disabled_recaptcha_still_hitl(monkeypatch) -> None:
    # solver disabled (no api key) -> cloudflare path also lands on HITL
    monkeypatch.setattr(captcha_solver_mod, "solver_enabled", lambda: False)
    cf = _probe("cloudflare", cloudflare=True)
    br = _StubBrowser([cf, cf])
    hitl_called: list[str] = []

    async def _hitl(reason: str) -> None:
        hitl_called.append(reason)

    result = _run(
        handle_bot_challenge_step(
            br, BotChallengeState(), hitl_callback=_hitl,
            passive_wait_seconds=0.0, poll_interval=0.01,
        )
    )
    assert result.action == "hitl"
    assert len(hitl_called) == 1


# ---------------------------------------------------------------------------
# 7. Router dispatch: anti-bot goals rank the guard; plain goals do not
# ---------------------------------------------------------------------------


def _plan_names(route: dict) -> list[str]:
    return [str(item.get("name") or "") for item in route.get("backend_plan") or []]


def _fallback_names(route: dict) -> list[str]:
    return [str(item.get("capability") or "") for item in route.get("fallback_chain") or []]


@pytest.mark.parametrize(
    "goal",
    [
        "抓取数据，遇到 Cloudflare Turnstile 人机验证就处理",
        "采集列表，如果出现滑块验证或反爬风控就过验",
        "scrape the catalog and handle any recaptcha / hcaptcha challenge",
    ],
)
def test_router_routes_anti_bot_goals_to_guard(goal: str) -> None:
    route = route_task(goal, url="https://shop.example/list")
    assert route["signals"]["bot_challenge"] is True
    assert "bot_challenge_guard" in _plan_names(route)
    assert "bot_challenge_guard" in _fallback_names(route)
    plan = build_execution_plan(route)
    assert "anti_bot_challenge_guarded" in plan["risk_flags"]
    step_names = [s["capability"] for s in plan["steps"]]
    assert "bot_challenge_guard" in step_names
    # the guard is a deterministic runtime guard, not a model step
    guard_step = next(s for s in plan["steps"] if s["capability"] == "bot_challenge_guard")
    assert guard_step["owner"] == "runtime_guards"
    assert guard_step["deterministic"] is True


def test_router_plain_goal_has_no_bot_challenge_guard() -> None:
    route = route_task("抓取商品列表并导出 Excel", url="https://shop.example/list")
    assert route["signals"]["bot_challenge"] is False
    assert "bot_challenge_guard" not in _plan_names(route)
    assert "bot_challenge_guard" not in _fallback_names(route)

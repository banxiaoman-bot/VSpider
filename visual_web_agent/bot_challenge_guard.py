"""Deterministic bot-challenge detection (Cloudflare / Turnstile / CAPTCHA) + HITL."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_PROBE_JS = """
() => {
  const url = location.href || '';
  const title = (document.title || '').trim();
  const bodyText = ((document.body && document.body.innerText) || '').slice(0, 8000);
  const htmlSnippet = (document.documentElement && document.documentElement.innerHTML || '').slice(0, 60000);

  const q = (sel) => { try { return !!document.querySelector(sel); } catch (_) { return false; } };

  const cfUrl = /challenges\\.cloudflare\\.com|cdn-cgi\\/challenge|__cf_chl/i.test(url);
  const cfTitle = /just a moment|attention required|please wait|checking your browser/i.test(title);
  const cfBody = /(checking your browser|verify you are human|cloudflare|ray id|cf-turnstile|cf-challenge-running|enable javascript and cookies)/i.test(bodyText);
  const cfDom = q('#challenge-form') || q('.cf-turnstile') || q('#cf-turnstile') || q('.cf-browser-verification');
  const turnstileIframe = q('iframe[src*="challenges.cloudflare"]') || q('iframe[src*="turnstile"]');
  const turnstileInput = q('input[name="cf-turnstile-response"]');
  const recaptcha = q('iframe[src*="recaptcha"]') || q('.g-recaptcha') || q('#recaptcha');
  const hcaptcha = q('iframe[src*="hcaptcha"]') || q('.h-captcha');
  const captchaText = /(验证码|滑块|拼图|人机验证|captcha|verify you)/i.test(bodyText);
  const wafHtml = /cf-turnstile|cf-challenge|data-ray|__cf_chl/i.test(htmlSnippet);

  const cloudflare = !!(cfUrl || cfTitle || turnstileIframe || turnstileInput
    || (cfDom && (cfBody || wafHtml)) || (cfBody && wafHtml));
  const turnstile = !!(turnstileIframe || turnstileInput || q('.cf-turnstile'));
  const captcha = !!(recaptcha || hcaptcha || (captchaText && !cloudflare));

  let vendor = '';
  if (cloudflare || turnstile) vendor = 'cloudflare';
  else if (recaptcha) vendor = 'recaptcha';
  else if (hcaptcha) vendor = 'hcaptcha';
  else if (captchaText) vendor = 'captcha';

  return {
    url,
    title,
    vendor,
    detected: !!(vendor),
    cloudflare,
    turnstile,
    recaptcha,
    hcaptcha,
    captcha_text: captchaText,
  };
}
"""

_CF_URL_RE = re.compile(
    r"challenges\.cloudflare\.com|cdn-cgi/challenge|__cf_chl",
    re.I,
)
_CF_TITLE_RE = re.compile(
    r"just a moment|attention required|checking your browser",
    re.I,
)


@dataclass
class BotChallengeProbe:
    vendor: str
    url: str = ""
    title: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class BotChallengeState:
    encounter_count: int = 0
    hitl_count: int = 0
    last_vendor: str = ""
    max_hitl_per_run: int = 3


@dataclass
class BotChallengeStepResult:
    detected: bool = False
    cleared: bool = True
    vendor: str = ""
    notice: str = ""
    cleared_after_hitl: bool = False
    action: str = "none"  # none | passive_wait | hitl | max_hitl


def classify_probe_payload(payload: dict[str, Any] | None) -> BotChallengeProbe | None:
    """Pure classifier for unit tests and offline replay."""
    if not payload:
        return None

    url = str(payload.get("url") or "")
    title = str(payload.get("title") or "")

    vendor = str(payload.get("vendor") or "").strip().lower()
    if not vendor and bool(payload.get("detected")):
        if payload.get("cloudflare") or payload.get("turnstile"):
            vendor = "cloudflare"
        elif payload.get("recaptcha"):
            vendor = "recaptcha"
        elif payload.get("hcaptcha"):
            vendor = "hcaptcha"
        elif payload.get("captcha_text"):
            vendor = "captcha"

    if not vendor:
        if _CF_URL_RE.search(url) or _CF_TITLE_RE.search(title):
            vendor = "cloudflare"
        else:
            return None

    return BotChallengeProbe(
        vendor=vendor,
        url=url,
        title=title,
        evidence={k: payload.get(k) for k in (
            "cloudflare", "turnstile", "recaptcha", "hcaptcha", "captcha_text",
        )},
    )


async def probe_bot_challenge(browser: Any) -> BotChallengeProbe | None:
    page = await browser._ensure_active_page(reason="bot challenge probe")
    if not page:
        return None
    try:
        payload = await page.evaluate(_PROBE_JS)
    except Exception as exc:
        logger.debug("[BOT CHALLENGE] probe failed: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    return classify_probe_payload(payload)


def _vendor_label(vendor: str) -> str:
    labels = {
        "cloudflare": "Cloudflare 人机验证",
        "recaptcha": "reCAPTCHA",
        "hcaptcha": "hCaptcha",
        "captcha": "验证码/风控页",
    }
    return labels.get(vendor, vendor or "人机验证")


def _build_notice(probe: BotChallengeProbe, *, action: str) -> str:
    label = _vendor_label(probe.vendor)
    if action == "passive_wait":
        return (
            f"\n🛡️【Bot Challenge 探测】检测到 {label}，"
            "系统正在被动等待页面自动通过（请勿盲目点击）…\n"
        )
    if action == "hitl":
        return (
            f"\n🛡️【Bot Challenge · 需人工】检测到 {label}。\n"
            "请在浏览器窗口中手动完成验证，完成后在 UI 点继续或终端回车。\n"
            f"URL: {probe.url[:120]}\n"
        )
    if action == "max_hitl":
        return (
            f"\n🛡️【Bot Challenge · 已达人工上限】{label} 仍未通过，"
            "请检查网络/代理或更换入口后再试。\n"
        )
    return ""


async def handle_bot_challenge_step(
    browser: Any,
    state: BotChallengeState,
    *,
    hitl_callback: Callable[[str], Awaitable[None]] | None = None,
    passive_wait_seconds: float = 15.0,
    poll_interval: float = 2.0,
) -> BotChallengeStepResult:
    """Detect bot challenges; passive-wait then escalate to HITL."""
    probe = await probe_bot_challenge(browser)
    if probe is None:
        return BotChallengeStepResult(detected=False, cleared=True)

    state.encounter_count += 1
    state.last_vendor = probe.vendor
    label = _vendor_label(probe.vendor)
    logger.info(
        "[BOT CHALLENGE] detected vendor=%s url=%s title=%s",
        probe.vendor,
        probe.url[:100],
        probe.title[:80],
    )

    # Phase 1: passive wait — real Chromium often clears CF interstitial alone
    deadline = asyncio.get_event_loop().time() + passive_wait_seconds
    notice = _build_notice(probe, action="passive_wait")
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll_interval)
        recheck = await probe_bot_challenge(browser)
        if recheck is None:
            logger.info("[BOT CHALLENGE] cleared during passive wait")
            return BotChallengeStepResult(
                detected=True,
                cleared=True,
                vendor=probe.vendor,
                action="passive_wait",
            )

    # Phase 1b: optional third-party Turnstile solver
    if probe.vendor in {"cloudflare", "captcha"}:
        try:
            try:
                from .captcha_solver import solver_enabled, try_solve_turnstile
            except ImportError:
                from captcha_solver import solver_enabled, try_solve_turnstile
            if solver_enabled():
                page = await browser._ensure_active_page(reason="captcha solver")
                if page:
                    solved = await try_solve_turnstile(page, page_url=probe.url or str(getattr(browser, "current_url", "") or ""))
                    if solved.success:
                        await asyncio.sleep(poll_interval)
                        recheck = await probe_bot_challenge(browser)
                        if recheck is None:
                            logger.info("[BOT CHALLENGE] cleared after captcha solver (%s)", solved.provider)
                            return BotChallengeStepResult(
                                detected=True,
                                cleared=True,
                                vendor=probe.vendor,
                                action="solver",
                                notice=f"\n🛡️【Bot Challenge】已通过 {solved.provider} 自动解 Turnstile。\n",
                            )
        except Exception as _solver_err:
            logger.debug("[BOT CHALLENGE] solver skipped: %s", _solver_err)

    # Phase 2: HITL
    if state.hitl_count >= state.max_hitl_per_run:
        return BotChallengeStepResult(
            detected=True,
            cleared=False,
            vendor=probe.vendor,
            notice=_build_notice(probe, action="max_hitl"),
            action="max_hitl",
        )

    state.hitl_count += 1
    reason = f"{label} — 请在浏览器中手动完成验证后继续"
    notice = _build_notice(probe, action="hitl")

    if hitl_callback is not None:
        try:
            await hitl_callback(reason)
        except Exception as exc:
            logger.warning("[BOT CHALLENGE] HITL callback failed: %s", exc)

    recheck = await probe_bot_challenge(browser)
    cleared = recheck is None
    if cleared:
        logger.info("[BOT CHALLENGE] cleared after HITL")
    else:
        logger.warning("[BOT CHALLENGE] still present after HITL vendor=%s", probe.vendor)

    return BotChallengeStepResult(
        detected=True,
        cleared=cleared,
        vendor=probe.vendor,
        notice=notice,
        cleared_after_hitl=cleared,
        action="hitl",
    )

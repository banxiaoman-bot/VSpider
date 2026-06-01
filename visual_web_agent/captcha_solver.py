"""Optional third-party Turnstile / CAPTCHA solver (Capsolver / 2Captcha)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_EXTRACT_SITEKEY_JS = """
() => {
  const fromAttr = document.querySelector('[data-sitekey]');
  if (fromAttr) return fromAttr.getAttribute('data-sitekey') || '';
  const iframe = document.querySelector('iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"]');
  if (iframe && iframe.src) {
    try {
      const u = new URL(iframe.src);
      return u.searchParams.get('k') || u.searchParams.get('sitekey') || '';
    } catch (_) {}
  }
  const inp = document.querySelector('input[name="cf-turnstile-response"]');
  if (inp && inp.parentElement) {
    const parentKey = inp.parentElement.getAttribute('data-sitekey');
    if (parentKey) return parentKey;
  }
  return '';
}
"""

_INJECT_TOKEN_JS = """
(token) => {
  const fields = [
    document.querySelector('[name="cf-turnstile-response"]'),
    document.querySelector('[name="g-recaptcha-response"]'),
    document.querySelector('textarea[name="h-captcha-response"]'),
  ].filter(Boolean);
  for (const el of fields) {
    el.value = token;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
  if (typeof window.tsCallback === 'function') {
    try { window.tsCallback(token); } catch (_) {}
  }
  if (typeof window.turnstileCallback === 'function') {
    try { window.turnstileCallback(token); } catch (_) {}
  }
  return fields.length;
}
"""


@dataclass
class CaptchaSolveResult:
    success: bool
    provider: str = ""
    token: str = ""
    reason: str = ""


def solver_config() -> dict[str, str]:
    return {
        "provider": (os.getenv("VSPIDER_CAPTCHA_SOLVER") or "capsolver").strip().lower(),
        "api_key": (os.getenv("VSPIDER_CAPTCHA_API_KEY") or "").strip(),
    }


def solver_enabled() -> bool:
    cfg = solver_config()
    return bool(cfg["api_key"]) and cfg["provider"] in {"capsolver", "2captcha"}


async def extract_turnstile_sitekey(page: Any) -> str:
    try:
        raw = await page.evaluate(_EXTRACT_SITEKEY_JS)
    except Exception as exc:
        logger.debug("[CAPTCHA SOLVER] sitekey extract failed: %s", exc)
        return ""
    return str(raw or "").strip()


async def inject_captcha_token(page: Any, token: str) -> int:
    if not token:
        return 0
    try:
        count = await page.evaluate(_INJECT_TOKEN_JS, token)
        return int(count or 0)
    except Exception as exc:
        logger.debug("[CAPTCHA SOLVER] token inject failed: %s", exc)
        return 0


async def _http_json(method: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=60.0) as client:
        if method.upper() == "GET":
            resp = await client.get(url, params=payload)
        else:
            resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}


async def _solve_capsolver(*, api_key: str, sitekey: str, page_url: str) -> CaptchaSolveResult:
    created = await _http_json(
        "POST",
        "https://api.capsolver.com/createTask",
        {
            "clientKey": api_key,
            "task": {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": sitekey,
            },
        },
    )
    task_id = str(created.get("taskId") or "")
    if not task_id:
        return CaptchaSolveResult(False, provider="capsolver", reason=str(created.get("errorDescription") or "no taskId"))

    deadline = time.time() + 120
    while time.time() < deadline:
        await asyncio.sleep(3)
        result = await _http_json(
            "POST",
            "https://api.capsolver.com/getTaskResult",
            {"clientKey": api_key, "taskId": task_id},
        )
        status = str(result.get("status") or "")
        if status == "ready":
            solution = result.get("solution") or {}
            token = str((solution.get("token") if isinstance(solution, dict) else "") or "")
            if token:
                return CaptchaSolveResult(True, provider="capsolver", token=token, reason="capsolver ready")
            return CaptchaSolveResult(False, provider="capsolver", reason="empty token")
        if status == "failed":
            return CaptchaSolveResult(False, provider="capsolver", reason=str(result.get("errorDescription") or "failed"))
    return CaptchaSolveResult(False, provider="capsolver", reason="timeout")


async def _solve_2captcha(*, api_key: str, sitekey: str, page_url: str) -> CaptchaSolveResult:
    created = await _http_json(
        "GET",
        "https://2captcha.com/in.php",
        {
            "key": api_key,
            "method": "turnstile",
            "sitekey": sitekey,
            "pageurl": page_url,
            "json": 1,
        },
    )
    if str(created.get("status")) != "1":
        return CaptchaSolveResult(False, provider="2captcha", reason=str(created.get("request") or "create failed"))
    task_id = str(created.get("request") or "")

    deadline = time.time() + 120
    while time.time() < deadline:
        await asyncio.sleep(5)
        result = await _http_json(
            "GET",
            "https://2captcha.com/res.php",
            {"key": api_key, "action": "get", "id": task_id, "json": 1},
        )
        if str(result.get("status")) == "1":
            token = str(result.get("request") or "")
            if token:
                return CaptchaSolveResult(True, provider="2captcha", token=token, reason="2captcha ready")
        req = str(result.get("request") or "")
        if req and req != "CAPCHA_NOT_READY":
            return CaptchaSolveResult(False, provider="2captcha", reason=req)
    return CaptchaSolveResult(False, provider="2captcha", reason="timeout")


async def try_solve_turnstile(page: Any, *, page_url: str) -> CaptchaSolveResult:
    cfg = solver_config()
    if not solver_enabled():
        return CaptchaSolveResult(False, reason="solver disabled")

    sitekey = await extract_turnstile_sitekey(page)
    if not sitekey:
        return CaptchaSolveResult(False, reason="sitekey not found")

    provider = cfg["provider"]
    api_key = cfg["api_key"]
    logger.info("[CAPTCHA SOLVER] solving turnstile via %s sitekey=%s…", provider, sitekey[:12])

    if provider == "2captcha":
        solved = await _solve_2captcha(api_key=api_key, sitekey=sitekey, page_url=page_url)
    else:
        solved = await _solve_capsolver(api_key=api_key, sitekey=sitekey, page_url=page_url)

    if not solved.success:
        logger.warning("[CAPTCHA SOLVER] failed: %s", solved.reason)
        return solved

    injected = await inject_captcha_token(page, solved.token)
    if injected <= 0:
        return CaptchaSolveResult(False, provider=provider, reason="token inject found no fields")
    await asyncio.sleep(2)
    return solved

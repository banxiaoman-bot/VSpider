"""Login detection and preflight login — extracted from ``main.py``.

Contains login URL detection, login settings resolution, vault integration,
and the preflight login flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from ..browser_env import BrowserEnv

from ._env_utils import _env_or_default, _env_int, _env_flag, _text_matches_patterns

logger = logging.getLogger("vspider")

_DEFAULT_LOGIN_OPEN_SELECTOR = (
    "a:has-text('登录'), button:has-text('登录'), [role='button']:has-text('登录'), "
    "a:has-text('登录/注册'), button:has-text('登录/注册'), "
    "a:has-text('登录系统'), button:has-text('登录系统'), "
    "a:has-text('进入系统'), button:has-text('进入系统'), "
    "a:has-text('立即登录'), button:has-text('立即登录'), "
    "a:has-text('统一身份认证'), button:has-text('统一身份认证'), "
    "a:has-text('单点登录'), button:has-text('单点登录'), "
    "a:has-text('Sign in'), button:has-text('Sign in'), "
    "a:has-text('Log in'), button:has-text('Log in')"
)
_DEFAULT_LOGIN_USER_SELECTOR = (
    "input[placeholder*='账号' i], input[placeholder*='用户名' i], "
    "input[placeholder*='手机' i], input[placeholder*='邮箱' i], "
    "input[placeholder*='工号' i], input[placeholder*='员工号' i], "
    "input[placeholder*='统一账号' i], input[placeholder*='域账号' i], "
    "input[placeholder*='用户编码' i], input[placeholder*='人员编号' i], "
    "input[name*='user' i], input[name*='login' i], input[name*='account' i], "
    "input[name*='emp' i], input[name*='staff' i], input[name*='job' i], "
    "input[type='email'], input[type='tel'], input[autocomplete='username']"
)
_DEFAULT_LOGIN_PASSWORD_SELECTOR = (
    "input[type='password'], input[autocomplete='current-password']"
)
_DEFAULT_LOGIN_PASSWORD_MODE_SELECTOR = (
    "text=/密码登录|账号登录|账号密码登录|工号登录|统一账号登录|使用密码|password login/i"
)
_DEFAULT_LOGIN_SUBMIT_SELECTOR = (
    "button[type='submit'], input[type='submit'], "
    "button:has-text('登录'), [role='button']:has-text('登录'), "
    "button:has-text('登录系统'), [role='button']:has-text('登录系统'), "
    "button:has-text('进入系统'), [role='button']:has-text('进入系统'), "
    "button:has-text('立即登录'), [role='button']:has-text('立即登录'), "
    "button:has-text('Sign in'), button:has-text('Log in')"
)

try:
    from ..url_guard import UrlGuardError, check_url
except ImportError:
    class UrlGuardError(Exception): pass  # type: ignore[no-redef]
    check_url = lambda url, **kw: None  # type: ignore[assignment]

def _url_looks_like_login(url: str) -> bool:
    base_patterns = [
        r"login", r"signin", r"sign-in", r"auth", r"passport", r"account",
        r"sso", r"cas", r"oauth", r"authserver", r"iam", r"uap", r"uc",
    ]
    return _text_matches_patterns(
        url or "",
        base_patterns,
        extra_env_name="VSPIDER_EXTRA_LOGIN_URL_KEYWORDS",
    )


async def _first_visible_locator(page, selector: str, limit: int = 12):
    if not selector:
        return None
    try:
        locator = page.locator(selector)
        count = await locator.count()
    except Exception:
        return None

    for idx in range(min(count, limit)):
        candidate = locator.nth(idx)
        try:
            if await candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


async def _wait_for_visible_locator(page, selector: str, timeout_ms: int = 4000, poll_ms: int = 200):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000.0
    while loop.time() < deadline:
        candidate = await _first_visible_locator(page, selector)
        if candidate:
            return candidate
        await asyncio.sleep(poll_ms / 1000.0)
    return None






_LOGIN_SITE_PROFILE_FIELDS = {
    "PASSWORD_MODE_SELECTOR": "password_mode_selector",
    "SUCCESS_TIMEOUT_MS": "success_timeout_ms",
    "OPEN_SELECTOR": "open_selector",
    "USER_SELECTOR": "user_selector",
    "PWD_SELECTOR": "pwd_selector",
    "SUCCESS_SELECTOR": "success_selector",
    "SUBMIT_SELECTOR": "submit_selector",
    "DOMAINS": "domains_raw",
    "USER": "user_account",
    "PWD": "user_pwd",
}


def _normalize_login_domain(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        try:
            text = urlsplit(text).hostname or text
        except Exception:
            pass
    if text.startswith("*."):
        text = text[2:]
    text = text.lstrip(".")
    text = text.split("/")[0]
    text = text.split(":")[0]
    return text.strip()


def _split_login_domains(value: str) -> list[str]:
    return [
        domain
        for domain in (
            _normalize_login_domain(part)
            for part in re.split(r"[\s,;]+", value or "")
        )
        if domain
    ]


def _current_page_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return ""


def _base_login_settings() -> dict:
    return {
        "open_selector": _env_or_default("VSPIDER_LOGIN_OPEN_SELECTOR", _DEFAULT_LOGIN_OPEN_SELECTOR),
        "user_selector": _env_or_default("VSPIDER_LOGIN_USER_SELECTOR", _DEFAULT_LOGIN_USER_SELECTOR),
        "pwd_selector": _env_or_default("VSPIDER_LOGIN_PWD_SELECTOR", _DEFAULT_LOGIN_PASSWORD_SELECTOR),
        "password_mode_selector": _env_or_default(
            "VSPIDER_LOGIN_PASSWORD_MODE_SELECTOR", _DEFAULT_LOGIN_PASSWORD_MODE_SELECTOR
        ),
        "submit_selector": _env_or_default("VSPIDER_LOGIN_SUBMIT_SELECTOR", _DEFAULT_LOGIN_SUBMIT_SELECTOR),
        "success_selector": os.getenv("VSPIDER_LOGIN_SUCCESS_SELECTOR", "").strip(),
        "success_timeout_ms": _env_int("VSPIDER_LOGIN_SUCCESS_TIMEOUT_MS", 10000),
        "user_account": os.getenv("VSPIDER_LOGIN_USER", "").strip(),
        "user_pwd": os.getenv("VSPIDER_LOGIN_PWD", "").strip(),
        "profile_name": "GLOBAL",
        "matched_domain": "",
        "credential_source": "global",
        "credentials_available": False,
        "blocked_reason": "",
    }


def _load_login_site_profiles() -> list[dict]:
    prefix = "VSPIDER_LOGIN_SITE_"
    profiles: dict[str, dict] = {}

    for env_name, env_value in os.environ.items():
        if not env_name.startswith(prefix):
            continue

        remainder = env_name[len(prefix):]
        field_name = ""
        site_key = ""
        for suffix, mapped_field in _LOGIN_SITE_PROFILE_FIELDS.items():
            token = f"_{suffix}"
            if remainder.endswith(token):
                site_key = remainder[:-len(token)].strip("_")
                field_name = mapped_field
                break

        if not site_key or not field_name:
            continue

        profile = profiles.setdefault(site_key, {"site_key": site_key})
        profile[field_name] = str(env_value or "").strip()

    loaded_profiles: list[dict] = []
    for site_key, profile in profiles.items():
        domains = _split_login_domains(profile.get("domains_raw", ""))
        if not domains:
            logger.warning(
                f"[PRELOGIN] Ignoring site login profile '{site_key}' because *_DOMAINS is missing."
            )
            continue

        loaded_profiles.append(
            {
                "site_key": site_key,
                "domains": domains,
                "user_account": profile.get("user_account", "").strip(),
                "user_pwd": profile.get("user_pwd", "").strip(),
                "open_selector": profile.get("open_selector", "").strip(),
                "user_selector": profile.get("user_selector", "").strip(),
                "pwd_selector": profile.get("pwd_selector", "").strip(),
                "password_mode_selector": profile.get("password_mode_selector", "").strip(),
                "submit_selector": profile.get("submit_selector", "").strip(),
                "success_selector": profile.get("success_selector", "").strip(),
                "success_timeout_ms": profile.get("success_timeout_ms", "").strip(),
            }
        )

    return loaded_profiles


def _resolve_login_settings(url: str) -> dict:
    settings = _base_login_settings()
    settings["credentials_available"] = bool(settings["user_account"] and settings["user_pwd"])
    if not settings["credentials_available"]:
        settings["blocked_reason"] = "global credentials are missing"

    site_profiles = _load_login_site_profiles()
    if not site_profiles:
        return settings

    host = _current_page_host(url)
    allow_global_fallback = _env_flag("VSPIDER_LOGIN_ALLOW_GLOBAL_FALLBACK", default=False)

    best_profile = None
    best_domain = ""
    for profile in site_profiles:
        for domain in profile.get("domains", []):
            if host == domain or host.endswith(f".{domain}"):
                if len(domain) > len(best_domain):
                    best_profile = profile
                    best_domain = domain

    if best_profile:
        for field_name in (
            "open_selector",
            "user_selector",
            "pwd_selector",
            "password_mode_selector",
            "submit_selector",
            "success_selector",
        ):
            if best_profile.get(field_name):
                settings[field_name] = best_profile[field_name]

        timeout_raw = best_profile.get("success_timeout_ms", "")
        if timeout_raw:
            try:
                timeout_value = int(timeout_raw)
            except Exception:
                timeout_value = settings["success_timeout_ms"]
            if timeout_value > 0:
                settings["success_timeout_ms"] = timeout_value

        settings["user_account"] = best_profile.get("user_account", "").strip()
        settings["user_pwd"] = best_profile.get("user_pwd", "").strip()
        settings["profile_name"] = best_profile["site_key"]
        settings["matched_domain"] = best_domain
        settings["credential_source"] = "site-profile"
        settings["credentials_available"] = bool(settings["user_account"] and settings["user_pwd"])
        settings["blocked_reason"] = (
            ""
            if settings["credentials_available"]
            else f"site profile '{best_profile['site_key']}' is missing USER/PWD"
        )
        return settings

    if allow_global_fallback:
        settings["credential_source"] = "global-fallback"
        settings["profile_name"] = "GLOBAL_FALLBACK"
        settings["blocked_reason"] = (
            ""
            if settings["credentials_available"]
            else "global fallback credentials are missing"
        )
        return settings

    settings["credential_source"] = "unmatched-site-profile"
    settings["profile_name"] = ""
    settings["matched_domain"] = ""
    settings["credentials_available"] = False
    settings["blocked_reason"] = (
        f"no configured site login profile matched host '{host or url or '<unknown>'}'"
    )
    return settings


def _resolve_login_vault_value(value: str, label: str) -> str:
    try:
        resolved, used, names = resolve_env_placeholders(value)
    except SecretResolutionError as exc:
        raise RuntimeError(str(exc)) from exc
    if used:
        logger.info(
            "[AUTH VAULT] Resolved pre-login %s from env placeholder(s): %s",
            label,
            ", ".join(names),
        )
    return resolved


async def _run_preflight_login(
    browser: BrowserEnv,
    goal: str,
    source: str = "startup",
    require_login: bool = False,
) -> bool:
    if source == "startup":
        print("[SECURITY] Checking whether the page needs login...")
    logger.info(f"[PRELOGIN] Checking whether the active page needs authentication (source={source}).")

    page = await browser._ensure_active_page(reason="preflight login")
    if not page:
        logger.warning("[PRELOGIN] No active page available, skip pre-flight login.")
        return False

    login_settings = _resolve_login_settings(page.url)
    login_open_selector = login_settings["open_selector"]
    user_selector = login_settings["user_selector"]
    pwd_selector = login_settings["pwd_selector"]
    password_mode_selector = login_settings["password_mode_selector"]
    submit_selector = login_settings["submit_selector"]
    success_selector = login_settings["success_selector"]
    success_timeout_ms = login_settings["success_timeout_ms"]
    login_enabled = _env_flag("VSPIDER_LOGIN_ENABLED", default=True)
    force_login = _env_flag("VSPIDER_LOGIN_FORCE", default=False)

    if login_settings["credential_source"] == "site-profile":
        logger.info(
            "[PRELOGIN] Using site login profile '%s' for host '%s' (matched domain '%s').",
            login_settings["profile_name"],
            _current_page_host(page.url) or "<unknown>",
            login_settings["matched_domain"],
        )
    elif login_settings["credential_source"] == "global-fallback":
        logger.info(
            "[PRELOGIN] No site profile matched host '%s'; using global fallback credentials.",
            _current_page_host(page.url) or "<unknown>",
        )

    password_locator = await _first_visible_locator(page, pwd_selector)
    password_mode_locator = await _first_visible_locator(page, password_mode_selector)
    login_entry = await _first_visible_locator(page, login_open_selector)
    login_like_url = _url_looks_like_login(page.url)
    explicit_login_goal = _goal_requires_login_flow(goal)
    login_surface_visible = bool(password_locator or password_mode_locator)
    proactive_login = force_login or require_login
    should_attempt_login = bool(
        login_surface_visible or (proactive_login and (login_entry or login_like_url or explicit_login_goal or require_login))
    )

    if not login_enabled:
        logger.info("[PRELOGIN] Disabled by VSPIDER_LOGIN_ENABLED=false.")
        if source == "startup":
            print("[SECURITY] Pre-login interceptor disabled by environment.")
        return False

    if not should_attempt_login:
        logger.info(
            "[PRELOGIN] Login surface not visible; skip native login "
            f"(force={force_login}, require_login={require_login}, source={source})."
        )
        if source == "startup":
            print("[SECURITY] No visible login form or popup detected, skipping auto login.")
        return False

    user_account = login_settings["user_account"]
    user_pwd = login_settings["user_pwd"]
    if not login_settings["credentials_available"] and not (proactive_login and login_entry):
        if source == "startup":
            print("[AUTH] Credentials not configured for this site, skipping auto login.")
        logger.warning(
            "[PRELOGIN] Credentials unavailable for auto login: %s.",
            login_settings["blocked_reason"] or "unknown reason",
        )
        return False

    try:
        if not password_locator:
            if login_entry:
                logger.info("[PRELOGIN] Opening login form from visible login entry.")
                await login_entry.click(timeout=5000)
                await asyncio.sleep(0.6)
                page = await browser._ensure_active_page(reason="preflight login entry clicked") or page
            password_mode_locator = await _first_visible_locator(page, password_mode_selector)
            if password_mode_locator:
                logger.info("[PRELOGIN] Switching to password-login mode.")
                await password_mode_locator.click(timeout=5000)
                await asyncio.sleep(0.4)
            password_locator = await _wait_for_visible_locator(page, pwd_selector, timeout_ms=4000)

        if not password_locator:
            logger.info("[PRELOGIN] Login form not exposed after probing, hand back to VLM.")
            if source == "startup":
                print("[SECURITY] Password field not found after probing, handing control back to VLM.")
            return False

        login_settings = _resolve_login_settings(page.url)
        user_selector = login_settings["user_selector"]
        pwd_selector = login_settings["pwd_selector"]
        submit_selector = login_settings["submit_selector"]
        success_selector = login_settings["success_selector"]
        success_timeout_ms = login_settings["success_timeout_ms"]
        user_account = login_settings["user_account"]
        user_pwd = login_settings["user_pwd"]
        if not login_settings["credentials_available"]:
            if source == "startup":
                print("[AUTH] Credentials are still unavailable after opening the login surface.")
            logger.warning(
                "[PRELOGIN] Credentials unavailable for auto login after login surface opened: %s.",
                login_settings["blocked_reason"] or "unknown reason",
            )
            return False

        if source == "startup":
            print("[AUTH] Login required, starting native Playwright login...")
        else:
            print("[AUTH] Login popup detected during execution, taking over with native login...")

        user_account = _resolve_login_vault_value(user_account, "username")
        user_pwd = _resolve_login_vault_value(user_pwd, "password")

        user_locator = await _first_visible_locator(page, user_selector)
        if user_locator:
            await user_locator.fill(user_account)
        else:
            logger.warning("[PRELOGIN] Username input not found; it may be prefilled or selector needs tuning.")

        await password_locator.fill(user_pwd)

        submit_locator = await _first_visible_locator(page, submit_selector)
        if submit_locator:
            await submit_locator.click(timeout=5000)
        else:
            await password_locator.press("Enter")

        page = await browser._ensure_active_page(reason="preflight login submitted") or page

        success = False
        if success_selector:
            success = bool(
                await _wait_for_visible_locator(page, success_selector, timeout_ms=success_timeout_ms)
            )
        else:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + success_timeout_ms / 1000.0
            while loop.time() < deadline:
                page = await browser._ensure_active_page(reason="preflight login waiting") or page
                still_visible = await _first_visible_locator(page, pwd_selector)
                if not still_visible:
                    success = True
                    break
                await asyncio.sleep(0.25)

        if not success:
            raise TimeoutError(
                "login success condition not met; consider setting VSPIDER_LOGIN_SUCCESS_SELECTOR"
            )

        print("[AUTH] Native login succeeded and state has been persisted to browser_data.")
        logger.info("[PRELOGIN] Native Playwright login succeeded and state is persisted.")
        await page.wait_for_load_state("domcontentloaded", timeout=success_timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        return True
    except Exception as exc:
        print(f"[AUTH] Native login failed: {exc}. Falling back to VLM.")
        logger.warning(f"[PRELOGIN] Native login failed, falling back to VLM: {exc}")
        return False


def _goal_requires_login_flow(goal: str) -> bool:
    """
    判断任务目标是否真的涉及登录流程。

    避免在与登录无关的页面上，仅凭"没看到登录按钮"就注入
    "当前已登录"这种强结论，干扰模型理解主任务。
    """
    login_patterns = [
        r"登录", r"登陆", r"login", r"sign[\s-]?in", r"sign[\s-]?on",
        r"账号", r"帐户", r"账户", r"用户名", r"user\s*name",
        r"密码", r"password", r"扫码", r"二维码", r"验证码", r"otp",
        r"credential", r"signin", r"统一身份认证", r"单点登录", r"sso",
        r"工号", r"员工号", r"域账号", r"统一账号", r"ukey", r"usb\s*key", r"ca证书", r"ca登录",
    ]
    return _text_matches_patterns(
        goal,
        login_patterns,
        extra_env_name="VSPIDER_EXTRA_LOGIN_KEYWORDS",
    )

"""
VSpider 主程序入口

实现 Agent 的核心执行循环：
截图 -> VLM 决策 -> 执行操作 -> 循环

用法：
    python main.py --url <目标URL> --goal <任务描述>
    python main.py --url <目标URL> --goal <任务描述> --user-data-dir ./browser_data

示例：
    python main.py --url "http://192.168.1.100/login" --goal "登录营销2.0系统并进入查询页面"
    python main.py --url "https://example.com" --goal "查询数据" --user-data-dir ./browser_data
"""

import asyncio
import argparse
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_APP_ROOT = Path(__file__).resolve().parent
for _dotenv_path in (_PROJECT_ROOT / ".env", _APP_ROOT / ".env"):
    if _dotenv_path.exists():
        load_dotenv(dotenv_path=_dotenv_path, override=False)

try:
    from .config import MAX_STEPS, SCREENSHOT_DIR
    from .browser_env import BrowserEnv, ActionExecutionError
    from .vlm_client import VLMClient
    from .data_manager import save_to_excel
    from .trajectory_logger import HtmlLogger
except ImportError:
    from config import MAX_STEPS, SCREENSHOT_DIR
    from browser_env import BrowserEnv, ActionExecutionError
    from vlm_client import VLMClient
    from data_manager import save_to_excel
    from trajectory_logger import HtmlLogger

# ========== 日志配置 ==========
# Windows 终端默认编码不是 UTF-8，中文会显示为 ????
# 强制将 stdout/stderr 切换为 UTF-8 以正确显示中文日志
if sys.platform == "win32":
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
_LOG_DATEFMT = "%H:%M:%S"

# 控制台 Handler
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))

# 文件 Handler（UTF-8 编码，保证回看日志时中文不乱码）
_log_dir = Path(__file__).parent
_file_handler = logging.FileHandler(
    _log_dir / "run.log", mode="a", encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))

logging.basicConfig(
    level=logging.INFO,
    handlers=[_console_handler, _file_handler],
)
logger = logging.getLogger("vspider.main")


def _broadcast_log_safe(message: str, level: str = "info") -> None:
    """向 API WebSocket 广播日志；未运行 API 服务时静默降级。"""
    try:
        from api_server import broadcast_log

        broadcast_log(message, level=level)
    except Exception:
        pass


def _broadcast_done_safe(success: bool, message: str = "") -> None:
    """向 API WebSocket 广播任务结束信号；未运行 API 时静默降级。"""
    try:
        from api_server import broadcast_done

        broadcast_done(success, message)
    except Exception:
        pass


_RPA_CACHE_DIR = Path(__file__).parent / "rpa_cache"


def _rpa_cache_path(url: str, goal: str) -> Path:
    """根据 url + goal 生成稳定的缓存文件路径（MD5 哈希命名）。"""
    key = hashlib.md5(f"{url}||{goal}".encode("utf-8")).hexdigest()
    return _RPA_CACHE_DIR / f"{key}.json"


def _extract_core_goal(goal: str) -> str:
    if not goal:
        return ""
    parts = re.split(r"\n\s*\n【[^】]+】\s*\n?", str(goal), maxsplit=1)
    return (parts[0] if parts else str(goal)).strip()


def _normalize_url_for_rpa(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlsplit(str(url).strip())
    except Exception:
        return str(url).strip().rstrip("/")

    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, "", ""))


def _normalize_goal_for_rpa(goal: str) -> str:
    text = _extract_core_goal(goal).lower()
    if not text:
        return ""
    text = re.sub(r"\{\{[^}]+\}\}", "{{var}}", text)
    text = re.sub(r"https?://\S+", "<url>", text)
    text = re.sub(r"\b\d+\s*[\.\):：、]\s*", " ", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"[\"'`“”‘’]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _build_rpa_match_metadata(url: str, goal: str) -> dict:
    core_goal = _extract_core_goal(goal)
    return {
        "source_url": str(url or ""),
        "source_goal": str(goal or ""),
        "core_goal": core_goal,
        "normalized_url": _normalize_url_for_rpa(url),
        "normalized_goal": _normalize_goal_for_rpa(core_goal),
    }


def _parse_goal_target_count(goal: str) -> int | None:
    """
    从用户目标中解析出目标数据条数。

    支持格式示例：
    - "获取新闻列表前90条" → 90
    - "抓取100条评论" → 100
    - "提取前50个商品" → 50
    - "获取全部内容" → None（不确定数量）

    Returns:
        解析出的目标数量，未指定则返回 None。
    """
    m = re.search(r'(?:前|共|取|抓)\s*(\d+)\s*(?:条|个|项|篇|则)', goal)
    if m:
        return int(m.group(1))
    m = re.search(r'(\d+)\s*(?:条|个|项|篇|则)\s*(?:数据|内容|信息|记录|新闻|商品|评论)', goal)
    if m:
        return int(m.group(1))
    return None


def _goal_should_skip_rpa(goal: str) -> tuple[bool, str]:
    """
    某些 prompt 本身就是为了测试异常链路/容错恢复而设计的，
    例如“点击一个绝对不存在的元素”“故意触发错误步骤”等。
    这类任务不应启用肌肉记忆，否则会被旧缓存直接短路，测不到真实恢复逻辑。
    """
    text = str(goal or "").strip()
    if not text:
        return False, ""

    suspicious_patterns: list[tuple[str, str]] = [
        (r"target_id\s*=\s*9999", "goal explicitly injects an invalid target_id"),
        (r"点击.*不存在的元素", "goal explicitly asks to click a nonexistent element"),
        (r"绝对不存在的元素", "goal explicitly asks for a guaranteed-missing element"),
        (r"不存在的(?:按钮|链接|控件|元素)", "goal contains an explicit nonexistent control step"),
        (r"无效的?(?:按钮|链接|控件|元素|target_id)", "goal contains an explicit invalid control step"),
        (r"故意.*(?:错误|失败|异常|无效)", "goal explicitly injects an error/failure step"),
        (r"(?:错误|异常|失败).*(?:步骤|动作|链路|流程|场景)", "goal is testing an error-handling path"),
        (r"(?:容错|恢复|鲁棒|自愈|报错)测试", "goal is explicitly testing recovery / robustness"),
        (r"测试.*(?:错误|异常|失败|容错|恢复|自愈)", "goal is explicitly testing error recovery"),
    ]
    for pattern, reason in suspicious_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True, reason
    return False, ""


def _load_rpa_cache_payload(path: Path) -> dict | None:
    try:
        return _normalize_rpa_cache_payload(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        logger.warning(f"[RPA] Failed to load cache {path.name}: {exc}")
        return None


def _find_similar_rpa_cache(url: str, goal: str, exclude_path: Path | None = None) -> tuple[Path, dict, str] | None:
    if not _RPA_CACHE_DIR.exists():
        return None

    target_meta = _build_rpa_match_metadata(url, goal)
    target_url = target_meta.get("normalized_url") or ""
    target_goal = target_meta.get("normalized_goal") or ""
    if not target_url or not target_goal:
        return None

    best: tuple[float, int, float, Path, dict, str] | None = None
    for path in _RPA_CACHE_DIR.glob("*.json"):
        if exclude_path and path == exclude_path:
            continue

        payload = _load_rpa_cache_payload(path)
        if not payload or not payload.get("replayable", True):
            continue

        candidate_url = payload.get("normalized_url") or ""
        candidate_goal = payload.get("normalized_goal") or ""
        if not candidate_url or not candidate_goal:
            continue
        if candidate_url != target_url:
            continue

        ratio = SequenceMatcher(None, target_goal, candidate_goal).ratio()
        exact_core = candidate_goal == target_goal
        if not exact_core and ratio < 0.88:
            continue

        fail_count = int(payload.get("fail_count", 0) or 0)
        mtime = path.stat().st_mtime
        score = ratio + (0.08 if exact_core else 0.0) - min(fail_count, 3) * 0.05
        reason = "normalized core goal exact match" if exact_core else f"similarity={ratio:.3f}"
        candidate_key = (score, -fail_count, mtime, path, payload, reason)
        if best is None or candidate_key[:3] > best[:3]:
            best = candidate_key

    if not best:
        return None

    return best[3], best[4], best[5]


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


def _env_or_default(name: str, default: str) -> str:
    value = os.getenv(name, "")
    return value.strip() if value and value.strip() else default


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


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except Exception:
        return default
    return value if value > 0 else default


@lru_cache(maxsize=None)
def _split_env_keywords(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "") or ""
    if not raw.strip():
        return ()
    parts = [
        part.strip()
        for part in re.split(r"[\r\n,;|]+", raw)
        if part and part.strip()
    ]
    return tuple(parts)


def _text_matches_patterns(text: str, base_patterns: list[str], extra_env_name: str = "") -> bool:
    patterns = list(base_patterns)
    if extra_env_name:
        patterns.extend(re.escape(item) for item in _split_env_keywords(extra_env_name))
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


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


def _goal_prefers_visual_navigation(goal: str) -> bool:
    """
    某些任务天然依赖“看见列表中的具体条目再点击”，
    例如排行榜、搜索结果列表、按序号选择第 N 个项目等。
    这类场景如果 text-only 抽到的 DOM 太稀，应尽快回退到视觉模式。
    """
    patterns = [
        r"排名", r"榜单", r"列表", r"top\s*\d+", r"top250",
        r"第\s*\d+\s*(个|条|项|部|集|页|名|行|列|条记录)", r"第[一二三四五六七八九十两]+",
        r"第一[个条项目部集名行列]", r"第二[个条项目部集名行列]",
        r"点击列表中的", r"找到并点击", r"按.*排序", r"最多播放", r"最热", r"最新",
        r"搜索结果", r"结果页", r"查询结果", r"明细", r"详情", r"表格", r"报表",
        r"电影", r"视频", r"商品", r"文章", r"工单", r"告警", r"缺陷", r"台账",
        r"设备", r"站点", r"站所", r"变电站", r"线路", r"馈线", r"回路", r"台区",
        r"审批", r"流程", r"任务单", r"检修", r"巡检", r"户号", r"档案",
    ]
    return _text_matches_patterns(
        goal,
        patterns,
        extra_env_name="VSPIDER_EXTRA_VISUAL_NAV_KEYWORDS",
    )


def _goal_should_force_vision(goal: str) -> bool:
    """
    对“榜单/列表/表格/结果页里定位具体条目”的任务，默认优先视觉模式。
    这类任务在 text-only 下最容易误点站点全局导航，内网页面尤其如此。
    """
    return _env_flag("VSPIDER_FORCE_VISION_FOR_LIST_TASKS", default=True) and _goal_prefers_visual_navigation(goal)


def _should_fallback_to_vision(goal: str, text_dom: str) -> tuple[bool, str]:
    lines = [line.strip() for line in (text_dom or "").splitlines() if line.strip()]
    if not lines:
        return True, "text-only DOM empty"

    id_lines = [line for line in lines if line.startswith("[ID:")]
    text_lines = [line for line in lines if line.startswith("[TEXT]")]

    if _goal_prefers_visual_navigation(goal):
        if len(lines) < 18:
            return True, f"goal needs list-item navigation but text-only DOM is sparse ({len(lines)} lines)"
        if len(id_lines) < 12 and len(text_lines) < 4:
            return True, (
                "goal needs visual list discovery but current text-only DOM mostly contains "
                "navigation chrome"
            )

    return False, ""


def _find_target_line(input_descriptions: str, target_id: int) -> str:
    if not input_descriptions or not target_id:
        return ""
    pattern = rf"^\[ID:\s*{int(target_id)}\](.*)$"
    for line in input_descriptions.splitlines():
        m = re.match(pattern, line.strip())
        if m:
            return line.strip()
    return ""


def _decision_is_irrelevant_nav_click(decision: dict, goal: str, input_descriptions: str) -> bool:
    """
    当任务本质上是在列表/结果页里找具体条目时，
    若模型却去点击“电影/音乐/阅读/首页/工作台”之类的全站导航，应直接拦截。
    """
    if (decision.get("action") or "").strip().lower() != "click":
        return False
    if not _goal_prefers_visual_navigation(goal):
        return False

    target_id = int(decision.get("target_id", 0) or 0)
    line = _find_target_line(input_descriptions, target_id)
    if not line:
        return False

    nav_patterns = [
        r">首页</", r">电影</", r">音乐</", r">阅读</", r">读书</", r">同城</", r">小组</",
        r">FM</", r">时间</", r">豆品</", r">播客</", r">工作台</", r">控制台</",
        r">系统管理</", r">帮助</", r">设置</", r">个人中心</", r">消息</",
    ]
    return _text_matches_patterns(
        line,
        nav_patterns,
        extra_env_name="VSPIDER_EXTRA_GLOBAL_NAV_KEYWORDS",
    )


def _goal_explicitly_requests_voice_or_camera(goal: str) -> bool:
    patterns = [
        r"语音", r"麦克风", r"voice", r"microphone",
        r"相机", r"camera", r"拍照", r"图片搜索", r"以图搜图",
        r"扫码", r"扫一扫", r"qr", r"lens",
    ]
    return any(re.search(pattern, goal, flags=re.IGNORECASE) for pattern in patterns)


def _goal_is_plain_search_task(goal: str) -> bool:
    if _goal_explicitly_requests_voice_or_camera(goal):
        return False
    search_patterns = [
        r"搜索", r"查询", r"search", r"query",
        r"搜索框", r"关键词", r"输入.*搜索框", r"点击搜索按钮",
    ]
    return any(re.search(pattern, goal, flags=re.IGNORECASE) for pattern in search_patterns)


def _decision_is_auxiliary_search_control_click(
    decision: dict,
    goal: str,
    input_descriptions: str,
) -> bool:
    """
    搜索页常见误点：把语音搜索、相机/拍照搜索、扫码入口当成主搜索按钮。
    这类按钮通常不是用户要的“正常输入 + 搜索”路径，应优先拦截。
    """
    if (decision.get("action") or "").strip().lower() != "click":
        return False
    if not _goal_is_plain_search_task(goal):
        return False

    target_id = int(decision.get("target_id", 0) or 0)
    line = _find_target_line(input_descriptions, target_id)
    if not line:
        return False

    aux_patterns = [
        r"语音", r"麦克风", r"voice", r"microphone",
        r"相机", r"camera", r"拍照", r"图片搜索", r"以图搜图",
        r"扫码", r"扫一扫", r"lens",
    ]
    return any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in aux_patterns)


def _decision_is_non_submit_search_control_click(
    decision: dict,
    goal: str,
    input_descriptions: str,
) -> bool:
    """
    搜索页另一个常见误判：把搜索建议项、清除按钮、历史记录入口当成"搜索提交按钮"。
    这类控件会让流程停留在输入态，随后模型又直接 done。
    """
    if (decision.get("action") or "").strip().lower() != "click":
        return False
    if not _goal_is_plain_search_task(goal):
        return False

    target_id = int(decision.get("target_id", 0) or 0)
    line = _find_target_line(input_descriptions, target_id)
    if not line:
        return False

    patterns = [
        r"搜索建议", r"建议", r"suggest", r"history", r"历史记录",
        r"删除", r"清除", r"clear", r"trigger",
    ]
    if not any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in patterns):
        return False

    # 真实搜索按钮本身也可能带 search 字样；避免误杀明显 submit 场景
    submit_markers = [r"搜索", r"提交", r"submit", r"search button", r"百度一下"]
    if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in submit_markers):
        return False
    return True


def _search_goal_done_looks_premature(
    goal: str,
    start_url: str,
    current_url: str,
    page_summary: str,
    text_dom: str,
) -> bool:
    """
    对常规搜索任务做一层完成态校验：
    - 若仍停留在起始搜索页/输入态，且出现搜索建议、删除、历史记录等痕迹，则不应 done
    - 若 URL 已带查询参数或页面明显进入结果态，则允许 done
    """
    if not _goal_is_plain_search_task(goal):
        return False

    current_url = (current_url or "").strip()
    start_url = (start_url or "").strip()
    summary_text = "\n".join(part for part in (page_summary, text_dom) if part)

    result_markers = [
        r"结果页", r"搜索结果", r"results?", r"search results?",
        r"\bq=", r"\bquery=", r"\bwd=", r"\bkeyword=", r"\btext=",
    ]
    if any(re.search(pattern, current_url, flags=re.IGNORECASE) for pattern in result_markers):
        return False
    if any(re.search(pattern, summary_text, flags=re.IGNORECASE) for pattern in result_markers):
        return False

    input_stage_markers = [
        r"搜索建议", r"suggest", r"历史记录", r"history",
        r"删除", r"清除", r"clear",
    ]
    same_page = current_url.rstrip("/") == start_url.rstrip("/")
    if same_page and any(re.search(pattern, summary_text, flags=re.IGNORECASE) for pattern in input_stage_markers):
        return True

    return False


def _decision_implies_completion(decision: dict) -> bool:
    """
    当模型在 thought/current_state 中已经明确承认"任务已完成"，
    但 action 仍然输出 click/type 等动作时，进行通用兜底。
    """
    if (decision.get("action") or "").strip().lower() == "done":
        return False

    text = "\n".join(
        str(decision.get(key, "") or "")
        for key in ("current_state", "thought")
    ).strip()
    if not text:
        return False

    completion_patterns = [
        r"任务已完成",
        r"用户目标已(?:经)?(?:全部)?达成",
        r"目标已(?:经)?(?:全部)?达成",
        r"已全部达成",
        r"无需再操作",
        r"不需要再操作",
        r"可直接结束任务",
        r"可以直接结束任务",
        r"task (?:is )?complete(?:d)?",
        r"goal (?:has been )?achieved",
        r"already completed",
        r"no further action needed",
        r"all required steps have been completed",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in completion_patterns)


def _goal_explicitly_requests_backtracking(goal: str) -> bool:
    """用户目标若明确要求返回/回到某页，则不启用回退拦截。"""
    backtrack_patterns = [
        r"返回", r"回到", r"回退", r"上一页",
        r"go back", r"return to", r"back to",
    ]
    return any(re.search(pattern, goal, flags=re.IGNORECASE) for pattern in backtrack_patterns)


def _decision_is_regressive_backtrack(decision: dict, goal: str) -> bool:
    """
    结果页/确认页上最常见的误判是：为了"补走中间步骤"又返回首页重做。
    除非用户明确要求返回，否则这种回退一般应直接视为 done。
    """
    if _goal_explicitly_requests_backtracking(goal):
        return False

    action = (decision.get("action") or "").strip().lower()
    if action not in {"click", "goto", "switch_tab"}:
        return False

    text = "\n".join(
        str(decision.get(key, "") or "")
        for key in ("current_state", "thought")
    ).strip()
    if not text:
        return False

    result_page_patterns = [
        r"结果页", r"结果页面", r"搜索结果", r"查询结果",
        r"成功页", r"确认页", r"已提交", r"已执行完毕",
        r"search results?", r"results? page", r"confirmation page",
        r"success page", r"submitted successfully",
    ]
    backtrack_patterns = [
        r"返回首页", r"回到首页", r"返回主页", r"回到主页",
        r"返回主页面", r"回到主页面", r"返回上一页", r"回到上一页",
        r"返回上一步", r"回到上一步",
        r"go back", r"back to home", r"return to home",
        r"return to homepage", r"back to the main page",
    ]

    return (
        any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in result_page_patterns)
        and any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in backtrack_patterns)
    )


def _force_done_decision(decision: dict) -> dict:
    """将一条自相矛盾的决策强制纠正为 done。"""
    normalized = dict(decision)
    normalized["action"] = "done"
    normalized["target_id"] = 0
    normalized["type_value"] = ""
    normalized["memory_key"] = ""
    normalized["point"] = None
    return normalized


def _compact_rpa_trail(trail: list[dict]) -> list[dict]:
    """
    压缩连续重复的 RPA 动作，避免把重复提交/重复点击固化进缓存。
    只压缩"完全相同"的连续动作，尽量不改变真实流程语义。
    """
    compacted: list[dict] = []

    def _same_step(prev: dict, cur: dict) -> bool:
        return (
            (prev.get("action") or "") == (cur.get("action") or "")
            and (prev.get("xpath") or "") == (cur.get("xpath") or "")
            and (prev.get("ax_role") or "") == (cur.get("ax_role") or "")
            and (prev.get("ax_name") or "") == (cur.get("ax_name") or "")
            and prev.get("target_id") == cur.get("target_id")
            and (prev.get("type_value") or "") == (cur.get("type_value") or "")
            and (prev.get("type_value_template") or "") == (cur.get("type_value_template") or "")
            and (prev.get("url") or "") == (cur.get("url") or "")
            and (prev.get("url_template") or "") == (cur.get("url_template") or "")
            and sorted(prev.get("required_memory_keys") or []) == sorted(cur.get("required_memory_keys") or [])
            and prev.get("x_norm") == cur.get("x_norm")
            and prev.get("y_norm") == cur.get("y_norm")
        )

    for step in trail:
        if not isinstance(step, dict):
            continue
        if compacted and _same_step(compacted[-1], step):
            continue
        compacted.append(dict(step))

    return compacted


# ── 动态内容阈值：ax_name 超过此长度视为动态内容（新闻标题/商品名等），
#    语义定位器大概率与当前页面不匹配，应快速降级到 XPath 结构寻址 ──
_DYNAMIC_CONTENT_NAME_THRESHOLD = 12


def _is_dynamic_content_step(step: dict) -> bool:
    """
    判断当前步骤是否涉及动态内容。

    动态内容特征：
      1. save_to_memory 动作 — 值一定随页面内容变化
      2. ax_name 长度 > 12 — 长文本通常为新闻标题、商品名等时效性内容
    """
    if step.get("action") == "save_to_memory":
        return True
    ax_name = str(step.get("ax_name") or "").strip()
    if len(ax_name) > _DYNAMIC_CONTENT_NAME_THRESHOLD:
        return True
    return False


async def _resolve_composite_locator(page, step: dict, timeout_ms: int):
    """
    复合定位器：Priority 1 语义定位 → Priority 2 XPath 兜底。

    ★ 动态内容智能降级：
      当 step 被判定为动态内容（长 ax_name / save_to_memory）时，
      Priority 1 的 timeout 从默认值压缩到 100ms，实现近乎立即降级到 XPath。
      这样不会死等"昨天的新闻标题"，而是直接用 XPath 物理结构定位。
    """
    from playwright.async_api import Error as PlaywrightError

    ax_role = str(step.get("ax_role") or "").strip().lower()
    ax_name = str(step.get("ax_name") or "").strip()
    xpath = str(step.get("xpath") or "").strip()
    is_dynamic = _is_dynamic_content_step(step)

    # ── Priority 1: 语义定位器（get_by_role + name） ──
    # 动态内容：timeout 压缩到 100ms 快速降级；
    # 纯粹无 ax_name 的 save_to_memory 直接跳过 Priority 1。
    if ax_role and ax_name:
        semantic_timeout = 100 if is_dynamic else timeout_ms
        if is_dynamic:
            logger.info(
                f"[RPA DYNAMIC] 检测到动态内容 (ax_name={ax_name!r}, len={len(ax_name)})，"
                f"语义定位 timeout 压缩至 {semantic_timeout}ms，将快速降级到 XPath"
            )
        try:
            semantic_loc = page.get_by_role(ax_role, name=ax_name).first
            await semantic_loc.wait_for(state="visible", timeout=semantic_timeout)
            return semantic_loc, f"role={ax_role!r}, name={ax_name!r}"
        except PlaywrightError as sem_err:
            logger.debug(
                f"[RPA REPLAY] semantic locator failed, will try xpath: {type(sem_err).__name__}: {sem_err}"
            )
        except Exception as sem_err:
            logger.debug(
                f"[RPA REPLAY] semantic locator failed, will try xpath: {type(sem_err).__name__}: {sem_err}"
            )

    # ── Priority 2: XPath 物理结构定位 ──
    if xpath:
        try:
            xpath_loc = page.locator(f"xpath={xpath}").first
            await xpath_loc.wait_for(state="visible", timeout=timeout_ms)
            return xpath_loc, f"xpath={xpath}"
        except PlaywrightError as xpath_err:
            logger.debug(
                f"[RPA REPLAY] xpath locator failed: {type(xpath_err).__name__}: {xpath_err}"
            )
        except Exception as xpath_err:
            logger.debug(
                f"[RPA REPLAY] xpath locator failed: {type(xpath_err).__name__}: {xpath_err}"
            )

    raise RuntimeError(
        "Composite locator failed on current page: "
        f"role={ax_role!r}, name={ax_name!r}, xpath={xpath!r}"
    )


def _normalize_rpa_cache_payload(payload) -> dict:
    """兼容旧版 list 缓存与新版 dict 缓存。"""
    if isinstance(payload, list):
        return {
            "version": 1,
            "replayable": True,
            "reason": "",
            "trail": payload,
            "fail_count": 0,
            "max_failures": 2,
            "completes_task": True,
            "source_url": "",
            "source_goal": "",
            "core_goal": "",
            "normalized_url": "",
            "normalized_goal": "",
        }
    if isinstance(payload, dict):
        trail = payload.get("trail")
        if not isinstance(trail, list):
            trail = []
        meta = _build_rpa_match_metadata(
            str(payload.get("source_url", "") or ""),
            str(payload.get("source_goal", "") or ""),
        )
        return {
            "version": int(payload.get("version", 4) or 4),
            "replayable": bool(payload.get("replayable", True)),
            "reason": str(payload.get("reason", "") or ""),
            "trail": trail,
            "fail_count": int(payload.get("fail_count", 0) or 0),
            "max_failures": int(payload.get("max_failures", 2) or 2),
            "completes_task": bool(payload.get("completes_task", True)),
            "source_url": str(payload.get("source_url", "") or meta["source_url"]),
            "source_goal": str(payload.get("source_goal", "") or meta["source_goal"]),
            "core_goal": str(payload.get("core_goal", "") or meta["core_goal"]),
            "normalized_url": str(payload.get("normalized_url", "") or meta["normalized_url"]),
            "normalized_goal": str(payload.get("normalized_goal", "") or meta["normalized_goal"]),
        }
    raise ValueError("Unsupported RPA cache payload format")


def _extract_placeholder_keys(text: str) -> list[str]:
    if not text or "{{" not in text:
        return []
    return sorted({m.strip() for m in re.findall(r"\{\{([^}]+)\}\}", text) if m.strip()})


def _stable_memory_keys(workflow_memory: dict | None) -> list[str]:
    if not workflow_memory:
        return []
    keys: list[str] = []
    for key in workflow_memory.keys():
        if not key or key == "latest_memory":
            continue
        if re.match(r"temp_var_\d+$", str(key)):
            continue
        keys.append(str(key))
    return sorted(set(keys))


def _resolve_replay_template(text: str, workflow_memory: dict | None) -> str:
    if not text or "{{" not in text or not workflow_memory:
        return text

    def _replace(match: re.Match) -> str:
        key = match.group(1).strip()
        val = workflow_memory.get(key)
        return str(val) if val is not None else match.group(0)

    return re.sub(r"\{\{([^}]+)\}\}", _replace, text)


def _write_rpa_cache_payload(path: Path, payload: dict) -> None:
    _RPA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _mark_rpa_cache_failure(path: Path, payload: dict, error_msg: str = "") -> dict:
    updated = dict(payload)
    updated["fail_count"] = int(updated.get("fail_count", 0) or 0) + 1
    max_failures = int(updated.get("max_failures", 2) or 2)
    if updated["fail_count"] >= max_failures:
        updated["replayable"] = False
        updated["reason"] = (
            f"replay failed repeatedly ({updated['fail_count']} times)"
            + (f": {error_msg[:120]}" if error_msg else "")
        )
    _write_rpa_cache_payload(path, updated)
    return updated


async def _replay_rpa(
    browser: "BrowserEnv",
    trail: list[dict],
    workflow_memory: dict | None = None,
) -> bool:
    """
    极速 RPA 回放：直接用 XPath/坐标执行缓存动作，完全跳过 VLM。

    Returns:
        True  → 全程无报错，任务完成
        False → 任意步骤失败，需降级回 VLM 主循环
    """
    from playwright.async_api import Error as PlaywrightError

    compacted_trail = _compact_rpa_trail(trail)
    if len(compacted_trail) != len(trail):
        logger.info(
            f"[RPA REPLAY] Compacted cached trail: {len(trail)} → {len(compacted_trail)} steps"
        )

    _RPA_TIMEOUT = 5000  # 每步最长等待 5s，防止卡死

    for idx, step in enumerate(compacted_trail):
        act = step.get("action")
        step_label = f"Step {idx + 1}/{len(compacted_trail)} ({act})"
        try:
            page = browser._page
            if page is None or page.is_closed():
                raise RuntimeError("no active page available for cached replay")
            if act == "goto":
                url = (
                    step.get("url_template")
                    or step.get("url")
                    or step.get("type_value_template")
                    or step.get("type_value")
                    or ""
                )
                url = _resolve_replay_template(str(url), workflow_memory)
                if not url:
                    raise RuntimeError("cached goto step missing url")
                logger.info(f"[RPA REPLAY] {step_label}: goto={url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=_RPA_TIMEOUT)
                await page.wait_for_load_state("domcontentloaded", timeout=_RPA_TIMEOUT)
                await asyncio.sleep(0.8)

            elif act == "click":
                loc, locator_desc = await _resolve_composite_locator(page, step, _RPA_TIMEOUT)
                logger.info(f"[RPA REPLAY] {step_label}: {locator_desc}")
                await loc.click(timeout=_RPA_TIMEOUT)
                # click 可能触发导航或弹新标签页，优先等待当前激活页稳定
                active_page = browser._page if browser._page and not browser._page.is_closed() else page
                await active_page.wait_for_load_state("domcontentloaded", timeout=_RPA_TIMEOUT)
                await asyncio.sleep(0.8)

            elif act == "type":
                val = step.get("type_value_template") or step.get("type_value", "")
                val = _resolve_replay_template(str(val), workflow_memory)
                loc, locator_desc = await _resolve_composite_locator(page, step, _RPA_TIMEOUT)
                logger.info(f"[RPA REPLAY] {step_label}: {locator_desc} <- {val!r}")
                await loc.fill(val, timeout=_RPA_TIMEOUT)
                await asyncio.sleep(0.3)

            elif act == "hover":
                loc, locator_desc = await _resolve_composite_locator(page, step, _RPA_TIMEOUT)
                logger.info(f"[RPA REPLAY] {step_label}: {locator_desc}")
                await loc.hover(timeout=_RPA_TIMEOUT)
                await asyncio.sleep(0.5)

            elif act == "click_point":
                # ── click_point 无 Playwright 自动等待，必须手动保证页面就绪 ──
                # 步骤 1：等待 DOM 加载完成（防止目标 Canvas/动态 UI 尚未渲染）
                await page.wait_for_load_state("domcontentloaded", timeout=_RPA_TIMEOUT)
                # 步骤 2：强制留 1.5s 给 Canvas 绘制 / 动态组件挂载完毕
                await asyncio.sleep(1.5)
                # 步骤 3：视口归一化换算 → 真实像素坐标
                viewport = page.viewport_size or {"width": 1280, "height": 800}
                real_x = int((step["x_norm"] / 1000.0) * viewport["width"])
                real_y = int((step["y_norm"] / 1000.0) * viewport["height"])
                logger.info(
                    f"[RPA REPLAY] {step_label}: "
                    f"norm=({step['x_norm']}, {step['y_norm']}) → real=({real_x}, {real_y})"
                )
                await page.mouse.click(real_x, real_y)
                await asyncio.sleep(0.8)

            elif act == "press_key":
                key_name = step.get("type_value_template") or step.get("type_value") or ""
                key_name = _resolve_replay_template(str(key_name), workflow_memory)
                if not key_name:
                    raise RuntimeError("cached press_key step missing key")
                logger.info(f"[RPA REPLAY] {step_label}: key={key_name!r}")
                await page.keyboard.press(key_name)
                if key_name == "Enter":
                    await page.wait_for_load_state("domcontentloaded", timeout=_RPA_TIMEOUT)
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(0.3)

            elif act == "switch_tab":
                tab_index = int(step.get("target_id", 0))
                open_pages = [p for p in browser._context.pages if not p.is_closed()]
                if not (0 <= tab_index < len(open_pages)):
                    raise RuntimeError(f"cached switch_tab target out of range: {tab_index}")
                browser._page = open_pages[tab_index]
                await browser._page.bring_to_front()
                await asyncio.sleep(0.8)

            elif act == "close_tab":
                logger.info(f"[RPA REPLAY] {step_label}: close active tab")
                await page.close()
                open_pages = [p for p in browser._context.pages if not p.is_closed()]
                if not open_pages:
                    raise RuntimeError("no remaining page after cached close_tab")
                browser._page = open_pages[-1]
                await browser._page.bring_to_front()
                await browser._page.wait_for_load_state("domcontentloaded", timeout=_RPA_TIMEOUT)
                await asyncio.sleep(0.8)

            elif act == "wait":
                try:
                    wait_secs = max(1, min(10, int(float(step.get("type_value") or "2"))))
                except (ValueError, TypeError):
                    wait_secs = 2
                logger.info(f"[RPA REPLAY] {step_label}: wait {wait_secs}s")
                await asyncio.sleep(wait_secs)

            elif act == "save_to_memory":
                # ═══════════════════════════════════════════════════════════
                # ★ save_to_memory 回放：动态重提取（绝不使用缓存硬编码值）
                #
                # 问题：录制时 type_value 记录的是当时页面的具体文本
                #       （如"总书记引领强国之路｜以质图强..."），但回放时
                #       页面内容已经变化，必须从当前最新页面重新提取。
                #
                # 策略：
                #   1. 用复合定位器（快速降级到 XPath）在当前页面找到目标元素
                #   2. 从元素的 innerText / value 提取最新文本
                #   3. 将 fresh_text 写入 workflow_memory
                # ═══════════════════════════════════════════════════════════
                memory_key = (
                    step.get("memory_key")
                    or step.get("type_value_template", "").strip("{}")
                    or ""
                ).strip()
                if not memory_key:
                    memory_key = f"temp_var_{int(time.time())}"
                    logger.warning(
                        f"[RPA DYNAMIC] save_to_memory 缺少 memory_key，"
                        f"自动生成: {memory_key!r}"
                    )

                # 尝试从当前页面动态提取最新文本
                fresh_text = ""
                try:
                    loc, locator_desc = await _resolve_composite_locator(
                        page, step, _RPA_TIMEOUT
                    )
                    # 提取最新文本：优先 innerText，次选 input value
                    raw_text = await loc.evaluate(
                        """el => {
                            const text = (el.innerText || el.textContent || '').trim();
                            const val  = (el.value || '').trim();
                            return text || val || '';
                        }"""
                    )
                    # 清理隐藏字符、多余换行、首尾空格
                    fresh_text = re.sub(
                        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "",
                        str(raw_text or ""),
                    ).strip()
                    fresh_text = re.sub(r"\s+", " ", fresh_text).strip()
                    logger.info(
                        f"[RPA DYNAMIC] 重新从页面提取了最新文本: "
                        f"{fresh_text!r} (via {locator_desc})"
                    )
                    print(
                        f"\033[1;36m🔄 [RPA DYNAMIC]\033[0m "
                        f"从当前页面重新提取了最新文本: "
                        f"\033[32m{fresh_text[:80]!r}\033[0m"
                    )
                except Exception as extract_err:
                    logger.warning(
                        f"[RPA DYNAMIC] 页面元素定位失败，"
                        f"无法动态提取文本: {extract_err}"
                    )
                    # 降级：尝试用缓存的 type_value 作为最后手段
                    cached_val = (step.get("type_value") or "").strip()
                    if cached_val:
                        fresh_text = cached_val
                        logger.warning(
                            f"[RPA DYNAMIC] 降级使用缓存文本: {fresh_text!r}"
                        )
                    else:
                        raise RuntimeError(
                            f"save_to_memory 回放失败：既无法从页面提取，"
                            f"缓存也无 type_value. 错误: {extract_err}"
                        )

                # 写入 workflow_memory
                if fresh_text and workflow_memory is not None:
                    workflow_memory[memory_key] = fresh_text
                    workflow_memory["latest_memory"] = fresh_text
                    logger.info(
                        f"[RPA DYNAMIC] Memory 已更新: "
                        f"{memory_key!r} = {fresh_text!r}"
                    )
                elif not fresh_text:
                    logger.warning(
                        f"[RPA DYNAMIC] save_to_memory 提取结果为空，"
                        f"未写入 workflow_memory"
                    )

                await asyncio.sleep(0.3)

            elif act == "smooth_scroll":
                direction = (step.get("type_value") or "down").strip().lower()
                if direction == "up":
                    js_scroll = "window.scrollBy({top: -window.innerHeight * 0.8, behavior: 'smooth'});"
                else:
                    js_scroll = "window.scrollBy({top: window.innerHeight * 0.8, behavior: 'smooth'});"
                logger.info(f"[RPA REPLAY] {step_label}: smooth_scroll direction={direction}")
                await page.evaluate(js_scroll)
                await asyncio.sleep(1.0)

            elif act == "remove_element":
                xpath = step.get("xpath", "")
                ax_role = step.get("ax_role", "")
                ax_name = step.get("ax_name", "")
                if not xpath:
                    logger.debug(f"[RPA REPLAY] {step_label}: remove_element skipped (no xpath)")
                else:
                    logger.info(
                        f"[RPA REPLAY] {step_label}: remove_element role={ax_role!r}, name={ax_name!r}, xpath={xpath}"
                    )
                    try:
                        loc = page.locator(f"xpath={xpath}")
                        if await loc.count() > 0:
                            await loc.evaluate("el => el.remove()")
                            logger.info(f"[RPA REPLAY] Element at {xpath} removed from DOM")
                        else:
                            # 节点已不存在（可能上次执行已删），视为成功，继续回放
                            logger.debug(f"[RPA REPLAY] remove_element target already gone: {xpath}")
                    except Exception as _rm_err:
                        # 删除失败不阻断整个 RPA 回放，仅警告
                        logger.warning(f"[RPA REPLAY] remove_element soft-fail: {_rm_err}")

            elif act == "select":
                val = step.get("type_value_template") or step.get("type_value", "")
                val = _resolve_replay_template(str(val), workflow_memory)
                loc, locator_desc = await _resolve_composite_locator(page, step, _RPA_TIMEOUT)
                logger.info(f"[RPA REPLAY] {step_label}: {locator_desc} <- {val!r}")
                try:
                    await loc.select_option(label=val, timeout=_RPA_TIMEOUT)
                except Exception:
                    try:
                        await loc.select_option(value=val, timeout=_RPA_TIMEOUT)
                    except Exception:
                        await loc.select_option(index=0, timeout=_RPA_TIMEOUT)
                await asyncio.sleep(0.5)

            else:
                logger.debug(f"[RPA REPLAY] {step_label}: skipping unsupported action")

        except (PlaywrightError, Exception) as rpa_err:
            print(
                f"\033[1;31m⚠️  [肌肉记忆]\033[0m "
                f"回放中断于动作 {idx + 1}，原因: {rpa_err}"
            )
            logger.warning(f"[RPA REPLAY] {step_label} FAILED — {type(rpa_err).__name__}: {rpa_err}")
            return False

    return True


async def _replay_ready_rpa_steps(
    browser: "BrowserEnv",
    payload: dict,
    cursor: int,
    workflow_memory: dict | None,
) -> tuple[int, list[dict], bool]:
    """回放当前已满足前置条件的连续缓存步骤。"""
    if not payload.get("replayable", True):
        return cursor, [], False

    trail = payload.get("trail") or []
    if cursor >= len(trail):
        return cursor, [], False

    available_keys = set((workflow_memory or {}).keys())
    ready_steps: list[dict] = []
    next_cursor = cursor
    while next_cursor < len(trail):
        step = trail[next_cursor]
        required = set(step.get("required_memory_keys") or [])
        if required and not required.issubset(available_keys):
            break
        ready_steps.append(step)
        next_cursor += 1

    if not ready_steps:
        return cursor, [], False

    logger.info(
        f"[RPA PARTIAL] Replaying cached steps {cursor + 1}-{next_cursor}/{len(trail)} "
        f"(ready after prerequisites satisfied)"
    )
    ok = await _replay_rpa(browser, ready_steps, workflow_memory)
    if not ok:
        return cursor, [], True
    return next_cursor, ready_steps, False


def _print_manual_warning(title: str, message: str):
    """
    在终端打印醒目的红色验证码警告。
    使用 ANSI 转义序列实现红色高亮，兼容大多数终端。
    """
    # ANSI: \033[1;31m = 粗体红色, \033[0m = 重置
    RED_BOLD = "\033[1;31m"
    YELLOW_BOLD = "\033[1;33m"
    RESET = "\033[0m"

    warning_lines = [
        "",
        f"{RED_BOLD}{'!' * 70}{RESET}",
        f"{RED_BOLD}!!!                                                              !!!{RESET}",
        f"{RED_BOLD}!!! {title.center(60)} !!!{RESET}",
        f"{RED_BOLD}!!!                                                              !!!{RESET}",
        f"{RED_BOLD}{'!' * 70}{RESET}",
        "",
        f"{YELLOW_BOLD}  >> {message}{RESET}",
        f"{YELLOW_BOLD}  >> After finishing it, come back here and press [Enter] to continue...{RESET}",
        "",
    ]
    for line in warning_lines:
        print(line)


def _apply_runtime_overrides(args) -> None:
    """Apply runtime config overrides from CLI arguments and natural-language constraints."""
    import config

    config.BROWSER_USER_DATA_DIR = args.user_data_dir

    override_text = "\n".join(
        part for part in (args.constraints, args.context, args.goal, args.output) if part
    )
    viewport_match = re.search(r"(\d{3,4})\s*[xX]\s*(\d{3,4})", override_text)
    if viewport_match:
        width = int(viewport_match.group(1))
        height = int(viewport_match.group(2))
        if width >= 800 and height >= 600:
            config.VIEWPORT_WIDTH = width
            config.VIEWPORT_HEIGHT = height
            logger.info(f"[VIEWPORT] Runtime override from prompt: {width}x{height}")


async def run_agent(
    start_url: str,
    goal: str,
    enable_xhr: bool = False,
    upload_file: str = "",
    xhr_pattern: str = "",
    require_login: bool = False,
    stop_event: threading.Event | None = None,
) -> bool:
    """
    Agent 核心运转循环。

    Args:
        start_url:   初始页面 URL
        goal:        用户的自然语言任务目标
        enable_xhr:  是否启用通用 XHR 拦截器（--xhr 参数）
        upload_file: 预配置的上传文件路径（--upload-file 参数）
        xhr_pattern: 精准截胡 URL 关键词（--xhr-pattern 参数）。
                     非空时开启"混合调度主引擎"：
                     一旦拦截到匹配 URL 的 API 响应，立即保存数据并终止 VLM 循环。
    """
    from datetime import datetime

    # 每次运行生成独立的带时间戳文件名，避免多次运行数据混在一起
    _run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _vlm_output = f"output_{_run_ts}.xlsx"       # VLM extract 提取的结果
    _xhr_output = f"xhr_{_run_ts}.xlsx"           # XHR 拦截到的 API 数据（分开存）

    browser = BrowserEnv()
    vlm = VLMClient()
    # HTML 轨迹日志实例化在 try 块外，确保 finally 中的 finalize() 始终可访问
    html_logger = HtmlLogger(goal=goal)
    _run_succeeded = False

    def _check_stop(context: str) -> None:
        if stop_event and stop_event.is_set():
            raise RuntimeError(f"STOP_REQUESTED::{context}")

    try:
        _broadcast_log_safe("VSpider Agent started", level="info")
        _check_stop("before_browser_start")
        # 启动浏览器并导航
        await browser.start(start_url)

        # 预配置上传文件路径（--upload-file 参数）
        if upload_file:
            from pathlib import Path as _Path
            _uf = _Path(upload_file)
            if _uf.exists():
                browser.set_upload_file(_uf)
                logger.info(f"[UPLOAD] Pre-configured upload file: {_uf.resolve()}")
            else:
                logger.warning(f"[UPLOAD] --upload-file path not found: {upload_file}")

        # 配置 XHR/Fetch 拦截器（双轨制）
        #   轨道 1（精准截胡）：--xhr-pattern 指定 URL 关键词，命中后跳过 VLM
        #   轨道 2（通用拦截）：--xhr 参数开启，全量收集所有 API 数据列表
        browser.configure_interceptor(
            enabled=enable_xhr,
            filename=_xhr_output,
            unique_key=None,
            min_list_size=20,
            url_pattern=xhr_pattern or None,
        )
        if xhr_pattern:
            logger.info(
                f"[XHR HYBRID] Primary engine (url_pattern={xhr_pattern!r}) → "
                f"will intercept and save to {_xhr_output}"
            )
        if enable_xhr:
            logger.info(f"[XHR] General interceptor enabled → {_xhr_output}")
        else:
            logger.info("[XHR] General interceptor disabled (use --xhr to enable)")

        # ── 预检登录拦截器：在 VLM 接管前，优先用原生 Playwright 完成一次安全登录 ──
        await _run_preflight_login(browser, goal, require_login=require_login)

        # ── 登录状态 DOM 检测（Python 侧，在主循环前执行一次）──────────────
        # 直接用 JS 检查页面有无可见的"登录"按钮，把确认结论注入 goal。
        # 这是事实陈述，VLM 无法用任务目标文本推翻它。
        if _goal_requires_login_flow(goal):
            has_login_btn = await browser.detect_login_button()
            if not has_login_btn:
                login_note = (
                    "\n\n【系统自动检测结果】当前页面 DOM 中未发现可见的'登录'按钮，"
                    "系统已确认当前处于登录态。请跳过所有登录操作，直接执行任务目标。"
                )
                goal = goal + login_note
                logger.info("[LOGIN DETECT] No login button found → already logged in, injected note into goal.")
            else:
                logger.info("[LOGIN DETECT] Login button found → not logged in, proceed with login flow.")
        else:
            logger.info("[LOGIN DETECT] Goal does not involve login flow → skip login-state prompt injection.")
        # ──────────────────────────────────────────────────────────────────

        logger.info(f"{'=' * 60}")
        logger.info(f"[TARGET] {goal}")
        _broadcast_log_safe(f"[TARGET] {goal}")
        logger.info(f"[URL] {start_url}")
        _broadcast_log_safe(f"[URL] {start_url}")
        logger.info(f"[MAX STEPS] {MAX_STEPS}")
        logger.info(f"{'=' * 60}")

        # 滑窗：记录最近 6 步的 (action, target_id, landing_url)，用于检测点击死循环
        # 升级为 URL-aware：只有"同一 ID 被点击 ≥ 3 次 **且** 着陆 URL 完全相同"才判定死循环，
        # 翻页 / A-B 循环切换等 URL 在变化的合法重复不再误伤。
        _last_actions: list[tuple[str, int, str]] = []
        _LOOP_GUARD_WINDOW = 6
        _loop_guard_blocked_ids: set[int] = set()  # 触发过 LOOP GUARD 的 target_id，后续直接拦截
        _extract_count = 0  # 连续 extract 次数（中间无翻页 click），>=2 强制 done
        _total_extracted_rows = 0  # 跨页累加的总行数
        _extract_null_streak = 0  # 连续 extract+null 降级次数，用于三级升级策略
        _extracted_page_urls: set = set()  # 已成功提取数据的不同页面 URL 集合

        # ── 自愈计数器 ────────────────────────────────────────────────
        # 连续执行失败超过 _MAX_CONSECUTIVE_ERRORS 次时强制转 ask_human
        _MAX_CONSECUTIVE_ERRORS = 3
        _consecutive_errors = 0

        # ── 跨页面记忆库 ──────────────────────────────────────────────
        # VLM 通过 save_to_memory 动作写入，通过 {{key}} 插值在 type 动作读取
        workflow_memory: dict = {}
        _rpa_cache_allowed = True
        _rpa_skip_reason = ""
        _executed_cached_trail: list[dict] = []
        _blank_page_recovery_attempted: set[str] = set()
        _force_vision_next_step = False
        _skip_rpa_for_goal, _skip_rpa_reason = _goal_should_skip_rpa(goal)
        if _skip_rpa_for_goal:
            _rpa_cache_allowed = False
            _rpa_skip_reason = _skip_rpa_reason
            logger.info(
                f"[RPA] Disabled for this run because the goal is intentionally testing "
                f"an invalid/error path: {_rpa_skip_reason}"
            )
            print(
                f"\n\033[1;33m⚡ [RPA]\033[0m 当前任务包含故意注入的错误/无效步骤，"
                f"已自动禁用肌肉记忆回放。\n"
                f"   reason: {_rpa_skip_reason}"
            )

        # ══════════════════════════════════════════════════════════════
        # RPA 肌肉记忆：支持"全量极速回放"与"条件满足后的半回放"
        # ══════════════════════════════════════════════════════════════
        _rpa_exact_path = _rpa_cache_path(start_url, goal)
        _rpa_path = _rpa_exact_path
        _rpa_payload: dict | None = None
        _rpa_match_reason = "exact hash match"
        _rpa_cursor = 0
        browser.clear_rpa_trail()  # 清空上次遗留的轨迹

        if not _skip_rpa_for_goal:
            if _rpa_exact_path.exists():
                logger.info(f"[RPA] Exact cache hit: {_rpa_exact_path}")
                _rpa_payload = _load_rpa_cache_payload(_rpa_exact_path)
            if _rpa_payload is None:
                _similar_match = _find_similar_rpa_cache(start_url, goal, exclude_path=_rpa_exact_path)
                if _similar_match:
                    _rpa_path, _rpa_payload, _rpa_match_reason = _similar_match
                    logger.info(
                        f"[RPA] Similar cache selected: {_rpa_path} | reason={_rpa_match_reason}"
                    )
        if _rpa_payload:
            if not _rpa_payload.get("replayable", True):
                _reason = _rpa_payload.get("reason") or "marked as non-replayable"
                print(
                    f"\n\033[1;33m⚡ [RPA]\033[0m 发现缓存: {_rpa_path.name}\n"
                    f"   该缓存已标记为不适合极速回放（{_reason}），直接进入 VLM 模式..."
                )
                logger.info(f"[RPA] Cache is non-replayable, skip replay: {_reason}")
            else:
                _match_label = "精确命中" if _rpa_path == _rpa_exact_path else f"近似命中（{_rpa_match_reason}）"
                print(
                    f"\n\033[1;33m⚡ [RPA]\033[0m 发现肌肉记忆缓存: {_rpa_path.name}\n"
                    f"   {_match_label}，将在条件满足时自动尝试极速回放..."
                )
                _new_cursor, _replayed_steps, _replay_failed = await _replay_ready_rpa_steps(
                    browser, _rpa_payload, _rpa_cursor, workflow_memory
                )
                if _replay_failed:
                    logger.warning("[RPA] Startup replay failed, falling back to VLM loop.")
                    _rpa_payload = _mark_rpa_cache_failure(
                        _rpa_path, _rpa_payload, "startup replay failure"
                    )
                else:
                    _rpa_cursor = _new_cursor
                    _executed_cached_trail.extend(_replayed_steps)
                    if (
                        _rpa_payload.get("completes_task", True)
                        and _rpa_cursor >= len(_rpa_payload.get("trail") or [])
                        and _replayed_steps
                    ):
                        print(f"\033[1;32m✅ [RPA]\033[0m 极速回放成功！全程跳过 VLM，任务已完成。")
                        logger.info("[RPA] Replay succeeded — task done without VLM.")
                        _run_succeeded = True
                        return True
        # ──────────────────────────────────────────────────────────────

        for step in range(1, MAX_STEPS + 1):
            _check_stop(f"before_step_{step}")
            logger.info(f"\n{'-' * 50}")
            logger.info(f">> Step {step}/{MAX_STEPS}")
            logger.info(f"{'-' * 50}")

            # ── 本轮日志收集状态 ──────────────────────────────────────────
            _log_screenshot_path: str | None = None
            _log_decision: list[dict] | None = None
            _log_error: str | None = None

            try:
                _login_intercepted = await _run_preflight_login(
                    browser,
                    goal,
                    source=f"step-{step}-start",
                    require_login=require_login,
                )
                if _login_intercepted:
                    logger.info("[PRELOGIN] Login popup handled before reasoning; refreshing context.")

                if _goal_prefers_visual_navigation(goal):
                    _page = await browser._ensure_active_page(reason="blank page recovery check")
                    _current_url = (_page.url or "") if _page else ""
                    if _current_url and _current_url not in _blank_page_recovery_attempted:
                        _is_blank_shell, _blank_reason = await browser.detect_blank_content_shell()
                        if _is_blank_shell:
                            logger.warning(
                                f"[PAGE RECOVERY] Detected likely blank content shell → reload once "
                                f"({_blank_reason})"
                            )
                            _blank_page_recovery_attempted.add(_current_url)
                            await browser.reload_active_page(reason="blank content shell")

                if _rpa_payload and _rpa_payload.get("replayable", True):
                    _trail = _rpa_payload.get("trail") or []
                    if _rpa_cursor < len(_trail):
                        _new_cursor, _replayed_steps, _replay_failed = await _replay_ready_rpa_steps(
                            browser, _rpa_payload, _rpa_cursor, workflow_memory
                        )
                        if _replay_failed:
                            logger.warning("[RPA] Partial replay failed, continue with VLM loop.")
                            _rpa_payload = _mark_rpa_cache_failure(
                                _rpa_path, _rpa_payload, "partial replay failure"
                            )
                        elif _replayed_steps:
                            _executed_cached_trail.extend(_replayed_steps)
                            _rpa_cursor = _new_cursor
                            if (
                                _rpa_payload.get("completes_task", True)
                                and _rpa_cursor >= len(_trail)
                            ):
                                logger.info("[RPA] Partial replay completed the remaining task.")
                                await browser.mark_and_screenshot(step=99)
                                _run_succeeded = True
                                return True
                            continue

                # ══════════════════════════════════════════════════════════════
                # 混合调度：主引擎（网络截胡）优先于备用引擎（VLM 视觉提取）
                # ══════════════════════════════════════════════════════════════
                # 主引擎生效条件：
                #   - 使用了 --xhr-pattern 参数
                #   - 网络拦截器已捕获到匹配 URL 的 API 数据快照
                # 一旦命中，立即保存数据并退出主循环，无需再截图或请求 VLM。
                core_data = browser.intercepted_data
                if core_data is not None:
                    _record_cnt = len(core_data) if isinstance(core_data, list) else 1
                    logger.info(
                        f"[HYBRID PRIMARY] XHR engine captured {_record_cnt} records — "
                        f"skipping screenshot + VLM, saving directly."
                    )
                    saved_path = save_to_excel(core_data, _xhr_output)
                    logger.info(f"[HYBRID PRIMARY] Saved to: {saved_path}")
                    print(
                        f"\n\033[1;32m[主引擎生效]\033[0m 网络层已自动截获核心 API 数据，"
                        f"共 \033[36m{_record_cnt}\033[0m 条，"
                        f"已保存至 \033[33m{saved_path}\033[0m。\n"
                        f"跳过 VLM 视觉提取，任务完成。\n"
                    )
                    await browser.mark_and_screenshot(step=99)
                    _run_succeeded = True
                    break
                # ════════════════════════════════════════════════════════════
                # 图文双模态融合 (Hybrid Modality)
                # 每一轮都同时采集 SoM 截图 + 精简 DOM 树，融合发送给 VLM。
                # 彻底废除"智能路由/纯文本降级"的单模态切换 —— 视觉与文本互为冗余，
                # VLM 得以用红框数字定位 + DOM 属性校验的方式做综合决策。
                # ════════════════════════════════════════════════════════════
                _tabs_state = await browser.get_tabs_state()
                _tabs_hint = f"\n\n【当前标签页列表】{_tabs_state}" if _tabs_state else ""
                _page_state = await browser.get_active_page_summary()
                _page_hint = f"\n\n【当前页面摘要】{_page_state}" if _page_state else ""

                # 旧的 _force_vision_next_step 信号在双模态下已失效，这里消耗掉以保持语义干净
                _force_vision_next_step = False

                print("\033[1;36m🧠 [HYBRID]\033[0m 同步采集 SoM 截图 + 无障碍语义树 (AX Tree)，融合决策")
                logger.info("[HYBRID] Collecting SoM screenshot + accessibility tree for fused VLM decision")

                screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step)
                _log_screenshot_path = (
                    str(Path(SCREENSHOT_DIR) / f"step_{step:02d}.jpg") if screenshot_b64 else None
                )

                try:
                    ax_tree_text = await browser.extract_accessibility_tree()
                except Exception as _ax_err:
                    logger.warning(
                        f"[HYBRID] AX Tree 提取失败，本轮仅凭截图决策: {_ax_err}"
                    )
                    ax_tree_text = ""

                # 兜底：防止超大 AX Tree 撑爆 Token
                if ax_tree_text and len(ax_tree_text) > 15000:
                    _orig_len = len(ax_tree_text)
                    ax_tree_text = ax_tree_text[:15000] + "\n...[WARNING: AX Tree 过长已截断]..."
                    logger.warning(
                        f"[HYBRID] AX Tree 长度 {_orig_len} 超过 15000 阈值，已截断以保护上下文窗口"
                    )

                ax_block = ""
                if ax_tree_text:
                    ax_block = (
                        "\n\n=====================================\n"
                        "【辅助信息：页面无障碍语义树 (AX Tree)】\n"
                        "以下是当前页面的纯语义结构，过滤了所有样式噪音。\n"
                        "  · 第一段 [交互元素 (SoM ID 映射)] 的 [ID: N] 与截图红框数字一一对应，\n"
                        "    重点参考每个元素的 Role 和 Name。\n"
                        "  · 第二段 [页面语义快照] 提供整体 AX 结构，辅助理解上下文。\n"
                        "  ⚠ 执行 extract 时，**必须从 AX Tree 中读取文本数据**（标题、数值、描述），\n"
                        "    而非仅靠截图 OCR。被浮层遮挡的元素在 AX Tree 中仍然存在。\n"
                        f"{ax_tree_text}\n"
                        "====================================="
                    )

                input_descriptions = (
                    (input_descriptions or "") + ax_block + _page_hint + _tabs_hint
                )

                decisions = await vlm.ask(
                    screenshot_b64,
                    goal,
                    step,
                    input_descriptions,
                    workflow_memory,
                )

                _log_decision = decisions

                # ── extract+null 即时自动提取 ────────────────────────────
                # 借鉴 browser-use 架构：VLM 只需给出 extract 意图，
                # 数据由系统从 AX Tree 自动提取，不再浪费步数等 VLM 重试。
                _has_extract_downgrade = any(
                    d.get("__extract_downgraded") for d in decisions
                )
                if _has_extract_downgrade:
                    _extract_null_streak += 1
                    # ── 同页去重：如果当前 URL 已成功提取过，不再重复提取 ──
                    _current_auto_url = browser.current_url
                    if _current_auto_url in _extracted_page_urls:
                        logger.warning(
                            f"[EXTRACT AUTO DEDUP] 当前页面已提取过，跳过重复提取: "
                            f"{_current_auto_url}"
                        )
                        _dedup_pag_links = browser.find_pagination_links()
                        if _dedup_pag_links:
                            _pag_hint = "\n".join(
                                f"  → [ID: {p['id']}] {p['role']}: \"{p['name']}\""
                                for p in _dedup_pag_links
                            )
                            vlm.inject_error_feedback(
                                f"⚠️ 当前页面（{_current_auto_url}）的数据已经提取过了，"
                                f"不会重复提取。\n"
                                f"系统在当前页面发现了以下翻页链接：\n{_pag_hint}\n"
                                f"【立即操作】请点击上述翻页链接翻页，例如："
                                f"click(target_id={_dedup_pag_links[0]['id']})\n"
                                f"⚠️ 必须使用上述精确的 ID，不要猜测其他 ID！"
                            )
                        else:
                            vlm.inject_error_feedback(
                                f"⚠️ 当前页面（{_current_auto_url}）的数据已经提取过了，"
                                f"不会重复提取。\n"
                                "当前页面未发现翻页链接。\n"
                                "如果任务需要更多数据，请尝试向下滚动查找翻页按钮。\n"
                                "如果已完成所有页的提取，请直接输出 done 结束任务。"
                            )
                        continue  # 跳到下一步重新截图
                    logger.warning(
                        f"[EXTRACT AUTO] VLM 输出 extract+null "
                        f"(第 {_extract_null_streak} 次)，启动 AX Tree 自动提取"
                    )
                    try:
                        # ── 通过 AX Tree 获取全页语义文本（优于 innerText）──
                        # AX Tree 天然过滤 script/style/广告噪音，只保留语义内容
                        _ax_text = await browser.extract_page_text_via_ax_tree()
                        if not _ax_text:
                            # AX Tree 失败时降级为 innerText
                            _page_for_extract = await browser._ensure_active_page(
                                reason="extract auto fallback to innerText"
                            )
                            _ax_text = await _page_for_extract.evaluate(
                                "() => document.body.innerText"
                            )
                            _ax_text = (_ax_text or "")[:16000]
                            logger.info("[EXTRACT AUTO] AX Tree 为空，降级使用 innerText")

                        # ── 尝试用纯文本 VLM 调用结构化提取 ──
                        _structured_auto = await vlm.extract_structured_data(
                            page_text=_ax_text,
                            goal=goal,
                        )
                        if _structured_auto and isinstance(_structured_auto, list) and len(_structured_auto) > 0:
                            _auto_extracted = _structured_auto
                            _new_rows = len(_auto_extracted)
                            logger.info(
                                f"[EXTRACT AUTO] 全页结构化提取成功，共 {_new_rows} 条"
                            )
                        else:
                            # 结构化失败，降级为原始文本 blob
                            _auto_extracted = {
                                "source": "auto_extract_from_ax_tree",
                                "page_url": browser.current_url,
                                "page_text": _ax_text[:8000],
                            }
                            _new_rows = 1
                            logger.info(
                                f"[EXTRACT AUTO] 结构化提取未返回数据，"
                                f"降级保存原始 AX Tree 文本 (长度={len(_ax_text)})"
                            )
                        saved_path = save_to_excel(
                            _auto_extracted, _vlm_output,
                        )
                        _total_extracted_rows += _new_rows
                        _extract_count += 1
                        _extracted_page_urls.add(browser.current_url)
                        # 与显式 extract 路径保持一致：含数据提取的轨迹不适合极速回放
                        _rpa_cache_allowed = False
                        _rpa_skip_reason = "contains auto-extract steps"
                        logger.info(
                            f"[EXTRACT AUTO] Saved to: {saved_path} "
                            f"(累计 {_total_extracted_rows} 条)"
                        )
                        print(
                            f"\033[1;33m⚡ [EXTRACT AUTO]\033[0m "
                            f"VLM 未填充数据，系统已从 AX Tree 全页提取 "
                            f"\033[36m{_new_rows}\033[0m 条数据。"
                            f"当前总计: \033[36m{_total_extracted_rows}\033[0m 条"
                        )
                        _log_decision = [{
                            "action": "extract",
                            "extracted_data": _auto_extracted,
                        }]
                        # 将降级的 wait 动作替换为已完成的 extract
                        # 不需要执行内层动作循环，直接跳到翻页引导
                        decisions = _log_decision
                    except Exception as _auto_err:
                        logger.error(
                            f"[EXTRACT AUTO] 自动提取失败: {_auto_err}"
                        )
                    # 无论是否成功，注入智能翻页/结束引导
                    _auto_pages = len(_extracted_page_urls)
                    _target_count = _parse_goal_target_count(goal)
                    _reached_target = (
                        _target_count is not None
                        and _total_extracted_rows >= _target_count
                    )
                    if _reached_target:
                        # 已达到用户指定的目标数量，强烈建议 done
                        vlm.inject_error_feedback(
                            f"✅ 系统已自动提取当前页数据。"
                            f"你已经成功提取了 {_auto_pages} 个不同页面的数据"
                            f"（累计 {_total_extracted_rows} 条）。\n"
                            f"用户要求获取 {_target_count} 条数据，"
                            f"当前已累计 {_total_extracted_rows} 条，"
                            f"**已达到目标**！请立即输出 done 结束任务。"
                        )
                    elif _auto_pages >= 2 and _target_count is not None:
                        # 已提取 2+ 页但未达标，明确要求继续
                        _pag_links = browser.find_pagination_links()
                        if _pag_links:
                            _pag_hint = "\n".join(
                                f"  → [ID: {p['id']}] {p['role']}: \"{p['name']}\""
                                for p in _pag_links
                            )
                            vlm.inject_error_feedback(
                                f"✅ 系统已自动提取当前页数据。"
                                f"已提取 {_auto_pages} 个页面"
                                f"（累计 {_total_extracted_rows} 条），"
                                f"但用户要求 {_target_count} 条，"
                                f"还差 {_target_count - _total_extracted_rows} 条。\n"
                                f"系统发现了翻页链接：\n{_pag_hint}\n"
                                f"【立即操作】请继续点击翻页链接加载下一页，例如："
                                f"click(target_id={_pag_links[0]['id']})\n"
                                f"⚠️ 必须使用上述精确的 ID！"
                            )
                        else:
                            vlm.inject_error_feedback(
                                f"✅ 系统已自动提取当前页数据。"
                                f"已提取 {_auto_pages} 个页面"
                                f"（累计 {_total_extracted_rows} 条），"
                                f"但用户要求 {_target_count} 条，"
                                f"还差 {_target_count - _total_extracted_rows} 条。\n"
                                "请向下滚动查找翻页按钮后继续翻页提取。"
                            )
                    elif _auto_pages >= 2:
                        # 不确定目标数量，让 VLM 自行判断
                        vlm.inject_error_feedback(
                            f"✅ 系统已自动提取当前页数据。"
                            f"你已经成功提取了 {_auto_pages} 个不同页面的数据"
                            f"（累计 {_total_extracted_rows} 条）。\n"
                            "请仔细回顾用户的原始任务要求，"
                            "判断是否需要继续翻页提取更多数据。\n"
                            "如果已满足用户需求，请输出 done 结束任务。"
                        )
                    else:
                        _pag_links = browser.find_pagination_links()
                        if _pag_links:
                            _pag_hint = "\n".join(
                                f"  → [ID: {p['id']}] {p['role']}: \"{p['name']}\""
                                for p in _pag_links
                            )
                            vlm.inject_error_feedback(
                                f"✅ 系统已自动提取当前页数据"
                                f"（已提取 {_auto_pages} 个页面，"
                                f"累计 {_total_extracted_rows} 条）。\n"
                                f"系统在当前页面发现了以下翻页链接：\n{_pag_hint}\n"
                                f"【立即操作】请点击翻页链接加载下一页，例如："
                                f"click(target_id={_pag_links[0]['id']})\n"
                                f"⚠️ 必须使用上述精确的 ID，不要猜测其他 ID！"
                            )
                        else:
                            vlm.inject_error_feedback(
                                f"✅ 系统已自动提取当前页数据"
                                f"（已提取 {_auto_pages} 个页面，"
                                f"累计 {_total_extracted_rows} 条）。\n"
                                "当前页面未发现翻页链接，可能已是最后一页。\n"
                                "如果任务还需要更多数据，请尝试向下滚动查找翻页按钮。\n"
                                "如果已完成所有页的提取，请直接输出 done 结束任务。"
                            )
                    # 跳过内层动作循环（因为 extract 已自动完成）
                    # 但如果连续太多次自动提取同一页面，强制结束
                    if _extract_null_streak >= 3:
                        logger.warning(
                            "[EXTRACT AUTO] 连续 3 次自动提取，强制结束任务"
                        )
                        _run_succeeded = True
                        _task_completed = True
                        break
                    continue  # 跳到下一步重新截图
                else:
                    if _extract_null_streak > 0:
                        logger.info(
                            f"[EXTRACT AUTO] extract+null 连击已中断 "
                            f"(was {_extract_null_streak})"
                        )
                    _extract_null_streak = 0

                # ── 内层动作循环：依次执行批次中的每个动作 ──────────────────
                # break → 中止本批次，进入下一步（重新截图）
                # _task_completed = True + break → 中止批次并退出外层主循环
                _task_completed = False

                # 🛡️ 连招熔断：连招开始前记录当前域名，用于检测意外跨域跳转
                # 单动作批次无需检测（不构成连招），只在 len > 1 时启用
                _batch_initial_domain = (
                    urlparse(browser.current_url).netloc
                    if len(decisions) > 1 else ""
                )

                for _action_idx, decision in enumerate(decisions):
                    _check_stop(f"before_step_{step}_action_{_action_idx + 1}")
                    if len(decisions) > 1:
                        logger.info(
                            f"[BATCH] 执行动作 [{_action_idx + 1}/{len(decisions)}]: "
                            f"{decision.get('action')}"
                        )

                    # 3. 检查异常状态
                    status = decision.get("status", "")
                    action = decision.get("action", "")

                    if status == "error" or action == "error":
                        logger.warning("[WARN] VLM returned error status, retrying...")
                        break  # 中止本批次，进入下一步（重新截图）

                    if status == "captcha_detected" or action == "ask_human":
                        # ===== 人类接管协议 (HITL)：红色高亮提示 + 阻塞等待 =====
                        # 提取 VLM 的求助原因（type_value 承载具体描述）
                        _hitl_reason = (decision.get("type_value") or "").strip()

                        if status == "captcha_detected":
                            _print_manual_warning(
                                "CAPTCHA DETECTED - MANUAL ACTION REQUIRED",
                                f"Please complete the CAPTCHA in the browser window manually."
                                + (f"\n  VLM 求助原因: {_hitl_reason}" if _hitl_reason else ""),
                            )
                        else:
                            # ask_human：VLM 主动发起求助
                            _hitl_display = _hitl_reason or "VLM 遭遇障碍，需要人工协助"
                            _print_manual_warning(
                                "⏸️  HUMAN-IN-THE-LOOP — VSpider 主动求援",
                                f"VLM 求助原因: {_hitl_display}",
                            )
                            logger.warning(
                                f"[HITL] VLM 主动触发人类接管 | 原因: {_hitl_display}"
                            )

                        # ask_human 含人工干预，流程不可回放，禁止写入 RPA 缓存
                        _rpa_cache_allowed = False
                        _rpa_skip_reason = "contains ask_human / manual intervention step"

                        # 阻塞等待用户在浏览器中手动完成后按 Enter
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            input,
                            "\n👉 请在弹出的浏览器窗口中手动完成操作（滑块验证 / 扫码登录 / 手动填写等）。\n✅ 操作完成后，在此终端按下 [回车键] 以恢复自动化流程...\n",
                        )
                        logger.info("[HITL] 用户已确认手动操作完成，恢复 VSpider 自动化流程...")
                        break  # 中止本批次，进入下一步（重新截图）

                    if _decision_implies_completion(decision):
                        logger.info(
                            "[DONE COERCE] VLM thought/current_state already indicates task completion; "
                            f"overriding action={action!r} to done."
                        )
                        decision = _force_done_decision(decision)
                        decisions[_action_idx] = decision
                        status = decision.get("status", "")
                        action = decision.get("action", "")

                    if _decision_is_regressive_backtrack(decision, goal):
                        logger.info(
                            "[REGRESSION GUARD] VLM is trying to navigate back from a result/confirmation page; "
                            f"overriding action={action!r} to done."
                        )
                        decision = _force_done_decision(decision)
                        decisions[_action_idx] = decision
                        status = decision.get("status", "")
                        action = decision.get("action", "")

                    if _decision_is_irrelevant_nav_click(decision, goal, input_descriptions):
                        logger.warning(
                            "[NAV GUARD] Blocked a likely global-navigation misclick while the goal "
                            "requires locating a concrete list/item entry."
                        )
                        _force_vision_next_step = True
                        vlm.inject_error_feedback(
                            "你刚才试图点击站点全局导航（如首页、电影、音乐、工作台等），"
                            "但当前任务要求在列表/结果页中定位具体条目。"
                            "下一步请忽略全局导航，只聚焦列表项本身。"
                        )
                        break

                    if _decision_is_auxiliary_search_control_click(decision, goal, input_descriptions):
                        logger.warning(
                            "[SEARCH GUARD] Blocked a likely click on a voice/camera/scanner control "
                            "while the goal expects a normal typed search flow."
                        )
                        vlm.inject_error_feedback(
                            "你刚才试图点击语音搜索、拍照搜索、扫码或相机按钮。"
                            "当前任务要求的是常规搜索路径：优先聚焦搜索输入框、输入关键词，"
                            "然后点击搜索按钮或按 Enter 提交。"
                            "除非用户明确要求语音/相机/扫码，否则不要再点击这类辅助入口。"
                        )
                        break

                    if _decision_is_non_submit_search_control_click(decision, goal, input_descriptions):
                        logger.warning(
                            "[SEARCH GUARD] Blocked a likely click on a suggestion/clear/history control "
                            "while the goal expects search submission."
                        )
                        vlm.inject_error_feedback(
                            "你刚才试图点击搜索建议项、删除按钮、清除按钮或历史记录入口。"
                            "这些控件通常不会真正提交搜索。"
                            "当前任务应优先选择真正的搜索按钮，或直接使用 press_key + Enter 提交。"
                        )
                        break

                    # 4. 数据提取（跨页累加模式）
                    if action == "extract":
                        _rpa_cache_allowed = False
                        _rpa_skip_reason = "contains extract steps"
                        extracted = decision.get("extracted_data")
                        _current_url = browser.current_url

                        # ── 同页去重：如果当前 URL 已经提取过，跳过 ──
                        if _current_url in _extracted_page_urls:
                            # Step 0：先判断是否已达用户指定的数量目标，优先引导 done
                            _target_count_pre = _parse_goal_target_count(goal)
                            _pre_reached = (
                                _target_count_pre is not None
                                and _total_extracted_rows >= _target_count_pre
                            )
                            logger.warning(
                                "[EXTRACT DEDUP] 当前页面已提取过，跳过重复提取"
                            )
                            _n_pages = len(_extracted_page_urls)
                            if _pre_reached:
                                # ✅ 已达目标：不再建议翻页/滚动，立即要求 done
                                vlm.inject_error_feedback(
                                    f"✅ 你已累计提取 {_total_extracted_rows} 条数据，"
                                    f"已达成用户要求的 {_target_count_pre} 条。\n"
                                    f"请立即输出 action=done 结束任务，不要再 extract、"
                                    f"不要翻页、不要滚动。"
                                )
                            elif _n_pages >= 2:
                                # 已提取 2+ 页面，强烈建议 done
                                vlm.inject_error_feedback(
                                    f"⚠️ 当前页面数据已经提取过了！\n"
                                    f"你已经成功提取了 {_n_pages} 个不同页面的数据"
                                    f"（累计 {_total_extracted_rows} 条）。\n"
                                    "请回顾用户的原始任务要求：如果用户要求的页数已经提取完毕，"
                                    "请立即输出 done 结束任务！\n"
                                    "只有当用户明确要求更多页面时才继续翻页。"
                                )
                            else:
                                vlm.inject_error_feedback(
                                    "⚠️ 当前页面数据已经提取过了！你正在重复提取同一页面！\n"
                                    "系统已帮你跳过。请立即执行以下操作之一：\n"
                                    "1. smooth_scroll(target_id=0, type_value='down') 向下滚动找到【下一页】按钮\n"
                                    "2. 找到并 click 【下一页】按钮翻页\n"
                                    "3. 如果所有页提取完毕，输出 done 结束任务"
                                )
                            # 仅在未达目标时才自动滚动寻找分页按钮
                            if not _pre_reached:
                                try:
                                    _scroll_page = await browser._ensure_active_page(
                                        reason="auto scroll for pagination"
                                    )
                                    await _scroll_page.evaluate(
                                        "window.scrollBy({top: 600, behavior: 'smooth'})"
                                    )
                                    logger.info("[EXTRACT DEDUP] 已自动向下滚动 600px")
                                except Exception:
                                    pass
                            break

                        if extracted:
                            # ── AX Tree 全页结构化提取：突破视口限制 ──
                            # VLM 只能看到视口中的 ~10 条，但一页通常有 20+ 条数据。
                            # 通过 AX Tree 获取全页语义文本，用纯文本 VLM 调用结构化提取全部数据。
                            try:
                                _full_page_text = await browser.extract_page_text_via_ax_tree()
                                if not _full_page_text:
                                    # AX Tree 失败时降级为 innerText
                                    _page_for_full = await browser._ensure_active_page(
                                        reason="extract fallback to innerText"
                                    )
                                    _full_page_text = await _page_for_full.evaluate(
                                        "() => document.body.innerText"
                                    )
                                    logger.info("[EXTRACT FULL] AX Tree 为空，降级使用 innerText")
                                _full_extracted = await vlm.extract_structured_data(
                                    page_text=_full_page_text,
                                    goal=goal,
                                    example_data=extracted,
                                )
                                if _full_extracted and len(_full_extracted) > len(
                                    extracted if isinstance(extracted, list) else [extracted]
                                ):
                                    logger.info(
                                        f"[EXTRACT FULL] AX Tree 全页提取 {len(_full_extracted)} 条 "
                                        f"vs VLM 视口 {len(extracted) if isinstance(extracted, list) else 1} 条，"
                                        f"采用全页数据"
                                    )
                                    extracted = _full_extracted
                                elif _full_extracted:
                                    logger.info(
                                        f"[EXTRACT FULL] AX Tree 全页 {len(_full_extracted)} 条 "
                                        f"≤ VLM {len(extracted) if isinstance(extracted, list) else 1} 条，"
                                        f"保留 VLM 原始数据"
                                    )
                            except Exception as _full_err:
                                logger.warning(
                                    f"[EXTRACT FULL] 全页提取失败，使用 VLM 原始数据: {_full_err}"
                                )

                            # 统计本次新增行数
                            _new_rows = len(extracted) if isinstance(extracted, list) else 1
                            _total_extracted_rows += _new_rows
                            logger.info(
                                f"[EXTRACT] 本次提取 {_new_rows} 条，"
                                f"累计已提取 {_total_extracted_rows} 条"
                            )
                            # save_to_excel 内部已支持追加写入 + 去重
                            saved_path = save_to_excel(extracted, _vlm_output)
                            logger.info(f"[EXTRACT] Saved to: {saved_path}")
                            print(
                                f"\033[1;32m✅ [EXTRACT]\033[0m "
                                f"成功追加 \033[36m{_new_rows}\033[0m 条数据。"
                                f"当前总计: \033[36m{_total_extracted_rows}\033[0m 条"
                            )
                            _extract_count += 1
                            _extracted_page_urls.add(_current_url)
                        else:
                            logger.warning("[EXTRACT] No extracted_data in VLM response")

                        # ── 智能翻页/结束引导（根据已提取页数 + 目标数量决定建议） ──
                        _n_pages = len(_extracted_page_urls)
                        _target_count_b = _parse_goal_target_count(goal)
                        _reached_target_b = (
                            _target_count_b is not None
                            and _total_extracted_rows >= _target_count_b
                        )
                        if _reached_target_b:
                            vlm.inject_error_feedback(
                                f"✅ 你已成功提取 {_n_pages} 个不同页面的数据"
                                f"（累计 {_total_extracted_rows} 条）。\n"
                                f"用户要求获取 {_target_count_b} 条数据，"
                                f"当前已达到目标！请立即输出 done 结束任务。"
                            )
                        elif _n_pages >= 2 and _target_count_b is not None:
                            _pag_links_c = browser.find_pagination_links()
                            if _pag_links_c:
                                _pag_hint_c = "\n".join(
                                    f"  → [ID: {p['id']}] {p['role']}: \"{p['name']}\""
                                    for p in _pag_links_c
                                )
                                vlm.inject_error_feedback(
                                    f"✅ 你已成功提取 {_n_pages} 个页面"
                                    f"（累计 {_total_extracted_rows} 条），"
                                    f"但用户要求 {_target_count_b} 条，"
                                    f"还差 {_target_count_b - _total_extracted_rows} 条。\n"
                                    f"系统发现了翻页链接：\n{_pag_hint_c}\n"
                                    f"【立即操作】请继续翻页，例如："
                                    f"click(target_id={_pag_links_c[0]['id']})"
                                )
                            else:
                                vlm.inject_error_feedback(
                                    f"✅ 你已成功提取 {_n_pages} 个页面"
                                    f"（累计 {_total_extracted_rows} 条），"
                                    f"但用户要求 {_target_count_b} 条，"
                                    f"还差 {_target_count_b - _total_extracted_rows} 条。\n"
                                    "请向下滚动查找翻页按钮后继续翻页提取。"
                                )
                        elif _n_pages >= 2:
                            vlm.inject_error_feedback(
                                f"✅ 你已成功提取 {_n_pages} 个不同页面的数据"
                                f"（累计 {_total_extracted_rows} 条）。\n"
                                "请仔细回顾用户的原始任务要求，"
                                "判断是否需要继续翻页提取更多数据。\n"
                                "如果已满足用户需求，请输出 done 结束任务。"
                            )
                        elif _extract_count > 0:
                            _pag_links_b = browser.find_pagination_links()
                            if _pag_links_b:
                                _pag_hint_b = "\n".join(
                                    f"  → [ID: {p['id']}] {p['role']}: \"{p['name']}\""
                                    for p in _pag_links_b
                                )
                                vlm.inject_error_feedback(
                                    f"✅ 你已成功提取当前页数据"
                                    f"（第 {_n_pages} 个页面，累计 {_total_extracted_rows} 条）。\n"
                                    f"系统在当前页面发现了以下翻页链接：\n{_pag_hint_b}\n"
                                    f"【立即操作】请点击翻页链接加载下一页，例如："
                                    f"click(target_id={_pag_links_b[0]['id']})\n"
                                    f"⚠️ 必须使用上述精确的 ID，不要猜测其他 ID！"
                                )
                            else:
                                vlm.inject_error_feedback(
                                    f"✅ 你已成功提取当前页数据"
                                    f"（第 {_n_pages} 个页面，累计 {_total_extracted_rows} 条）。\n"
                                    "当前页面未发现翻页链接，可能已是最后一页。\n"
                                    "如果任务还需要更多数据，请尝试向下滚动查找翻页按钮。\n"
                                    "如果已完成所有页的提取，请直接输出 done 结束任务。"
                                )

                        # 连续 extract 守卫（防止 VLM 不翻页也不 done 陷入死循环）
                        if _extract_count >= 3:
                            logger.warning(
                                "[EXTRACT GUARD] 连续 extract 无翻页动作，"
                                f"已累积 {_total_extracted_rows} 条数据，强制结束任务。"
                            )
                            _run_succeeded = True
                            _task_completed = True
                            break

                        # 提取后中止本批次，下一步重新截图（VLM 可能还需要翻页提取更多）
                        break
                    else:
                        # 翻页动作（click/scroll/smooth_scroll）重置连续 extract 计数
                        if action in ("click", "scroll", "smooth_scroll"):
                            _extract_count = 0

                    # 5. 记忆库日志（save_to_memory 动作由 execute_action 内部写入 workflow_memory）
                    if action == "save_to_memory":
                        if workflow_memory:
                            logger.info(f"[MEMORY] Current workflow_memory: {workflow_memory}")

                    # 6. 任务完成
                    if action == "done":
                        page_summary = await browser.get_active_page_summary()
                        current_url = browser.current_url
                        text_dom_for_done = ""
                        if _goal_is_plain_search_task(goal):
                            try:
                                # 用 AX Tree 代替旧的 DOM 文本快照 —— name 字段里仍然包含搜索结果链接标题，
                                # 对 _search_goal_done_looks_premature 的子串检测来说是等价的信息源。
                                text_dom_for_done = await browser.extract_accessibility_tree()
                            except Exception as _done_ax_err:
                                logger.debug(f"[DONE GUARD] AX tree snapshot skipped: {_done_ax_err}")
                        if _search_goal_done_looks_premature(
                            goal,
                            start_url,
                            current_url,
                            page_summary,
                            text_dom_for_done,
                        ):
                            logger.warning(
                                "[DONE GUARD] Blocked a premature done on a search task "
                                "because the page still looks like the input/suggestion stage."
                            )
                            vlm.inject_error_feedback(
                                "你刚才试图结束任务，但当前页面仍像是搜索输入/建议阶段，"
                                "还没有明确进入搜索结果页。"
                                "请不要直接 done；应尝试真正提交搜索，优先使用搜索按钮或 press_key + Enter。"
                            )
                            break

                        # 如果 done 时也附带了 VLM 提取数据，一并保存
                        extracted = decision.get("extracted_data")
                        if extracted:
                            logger.info(f"[EXTRACT] Final data extracted: {extracted}")
                            save_to_excel(extracted, _vlm_output)
                        # 打印 XHR 拦截汇总
                        if browser.intercepted_count > 0:
                            logger.info(
                                f"[XHR SUMMARY] Total records intercepted this session: "
                                f"{browser.intercepted_count}"
                            )
                        if workflow_memory:
                            logger.info(f"[MEMORY] Final workflow_memory state: {workflow_memory}")
                        logger.info("[DONE] Task completed! VLM determined the goal has been achieved.")
                        _broadcast_log_safe("[DONE] Task completed! VLM determined the goal has been achieved.")
                        _broadcast_done_safe(True, "Task completed")
                        await browser.mark_and_screenshot(step=99)

                        # ── RPA 肌肉记忆：保存成功轨迹供下次极速回放 ──────────
                        if browser.rpa_trail or _executed_cached_trail or not _rpa_cache_allowed:
                            try:
                                merged_trail = _executed_cached_trail + browser.rpa_trail
                                compacted_trail = _compact_rpa_trail(merged_trail)
                                if len(compacted_trail) != len(merged_trail):
                                    logger.info(
                                        f"[RPA] Compacted trail before save: "
                                        f"{len(merged_trail)} → {len(compacted_trail)} steps"
                                    )
                                cache_meta = _build_rpa_match_metadata(start_url, goal)
                                # 提取类任务（获取/提取/抓取/extract）的轨迹不应声称
                                # 回放即可完成任务，必须保留 VLM 提取步骤
                                _is_extract_goal = bool(re.search(
                                    r"获取|提取|抓取|采集|爬取|extract|scrape|crawl",
                                    goal, re.IGNORECASE,
                                ))
                                _trail_has_extract = any(
                                    s.get("action") == "extract" for s in compacted_trail
                                )
                                _completes = not (_is_extract_goal and not _trail_has_extract)
                                cache_payload = {
                                    "version": 4,
                                    "replayable": _rpa_cache_allowed and bool(compacted_trail),
                                    "reason": "" if (_rpa_cache_allowed and compacted_trail) else (_rpa_skip_reason or "marked as non-replayable"),
                                    "trail": compacted_trail if (_rpa_cache_allowed and compacted_trail) else [],
                                    "fail_count": 0,
                                    "max_failures": 2,
                                    "completes_task": _completes,
                                    **cache_meta,
                                }
                                _write_rpa_cache_payload(_rpa_exact_path, cache_payload)
                                if cache_payload["replayable"]:
                                    print(
                                        f"\033[1;33m💾 [RPA]\033[0m 肌肉记忆已保存"
                                        f"（{len(compacted_trail)} 步）→ {_rpa_exact_path.name}"
                                    )
                                    logger.info(f"[RPA] Trail saved: {_rpa_exact_path} ({len(compacted_trail)} steps)")
                                else:
                                    logger.info(
                                        f"[RPA] Cache written as non-replayable: {_rpa_exact_path} "
                                        f"(reason: {_rpa_skip_reason or 'marked as non-replayable'})"
                                    )
                            except Exception as _save_err:
                                logger.warning(f"[RPA] Failed to save trail (non-fatal): {_save_err}")
                        # ────────────────────────────────────────────────────────

                        _task_completed = True
                        _run_succeeded = True
                        break

                    # 7. 执行浏览器操作

                    _raw_type_value = str(decision.get("type_value") or "")
                    _placeholder_keys = _extract_placeholder_keys(_raw_type_value)
                    _required_memory_keys = sorted(
                        set(_stable_memory_keys(workflow_memory)) | set(_placeholder_keys)
                    )
                    if _placeholder_keys:
                        decision["__rpa_template_value"] = _raw_type_value
                    if _required_memory_keys:
                        decision["__rpa_required_keys"] = _required_memory_keys

                    # ── 动态插值（主循环层）：execute_action 前将 {{key}} 替换为真实值 ──
                    # 作为 browser_env.py 内部插值的前置补充：
                    #   - browser_env 只在 type 动作内插值；
                    #   - 此处对所有动作的 type_value 统一处理，包括 goto/press_key 等。
                    # 两层都运行是安全的：第一层替换后 {{}} 消失，第二层不再重复替换。
                    if decision.get("type_value") and workflow_memory:
                        for _mem_k, _mem_v in workflow_memory.items():
                            _placeholder = f"{{{{{_mem_k}}}}}"
                            if _placeholder in decision["type_value"]:
                                decision["type_value"] = decision["type_value"].replace(
                                    _placeholder, str(_mem_v)
                                )
                                logger.info(
                                    f"[INTERPOLATION] {_placeholder} → {_mem_v!r}"
                                )
                                print(
                                    f"\033[1;35m✨ [INTERPOLATION]\033[0m "
                                    f"{_placeholder} → \033[32m{_mem_v!r}\033[0m"
                                )

                    # ── LOOP GUARD 前置拦截：已进入黑名单的 target_id 直接阻断 ───
                    _click_target_id = decision.get("target_id", 0)
                    if action in ("click", "click_new_tab") and _click_target_id in _loop_guard_blocked_ids:
                        logger.warning(
                            f"[LOOP GUARD] 前置拦截：target_id={_click_target_id} 已在黑名单，跳过执行"
                        )
                        _block_msg = (
                            f"🚫 系统强制拦截：元素 #{_click_target_id} 已被 LOOP GUARD 封禁！\n"
                            f"该元素此前已被检测为死循环陷阱，本次点击已被直接取消（未执行）。\n"
                            f"【强制指令】立刻停止对 #{_click_target_id} 的一切操作！\n"
                            f"请仔细观察最新截图，选择一个**完全不同的元素**继续任务 ——\n"
                            f"例如跳过广告，点击下方的第二个/第三个搜索结果；\n"
                            f"或者如果任务实际已完成（结果页已打开、标签已切换），请直接 action=done。"
                        )
                        vlm.inject_error_feedback(_block_msg)
                        # 方案②：前置拦截也标注历史，让 VLM 下一轮看到"本次未执行"
                        vlm.annotate_last_result(
                            f"🚫 被LOOP GUARD前置拦截(#{_click_target_id}在黑名单), 未执行"
                        )
                        break  # 不执行，直接进入下一步重新截图+决策

                    # ── 预采样：记录 execute_action 前的标签数与 URL，供结果回填 ──
                    _pre_pages_count = (
                        len(browser._context.pages) if browser._context else 0
                    )
                    _pre_url = browser.current_url

                    # ── 自愈执行：捕获 ActionExecutionError 并注入 VLM 反馈 ──────────
                    try:
                        active_page = await browser.execute_action(decision, workflow_memory)
                        if active_page is not None:
                            # execute_action 内部已更新 browser._page，此处仅做日志追踪
                            logger.debug(f"[TAB GUARD] Active page after action: {(active_page.url or 'about:blank')[:80]}")

                        # ── 标签页切换感知：将 Tab Guard 切换事件注入 VLM 反馈 ──────
                        _tab_switched_this_step = bool(browser._tab_switch_notice)
                        if browser._tab_switch_notice:
                            vlm.inject_error_feedback(browser._tab_switch_notice)
                            logger.info(f"[TAB SWITCH] Injected page-switch notice for next VLM step")
                            browser._tab_switch_notice = None

                        # ── URL-aware LOOP GUARD（后置检测）──────────────────────
                        # 在 execute_action 之后才能拿到真正的 landing URL，
                        # 只有「同一 ID 被点击 ≥ 3 次 **且** 着陆 URL 完全相同」才判定死循环，
                        # 翻页（URL 递增）或 A/B 循环切换（URL 交替变化）不再误伤。
                        _landing_url = browser.current_url
                        _cur_action_key = (action, decision.get("target_id", 0), _landing_url)
                        _click_repeat_count = (
                            _last_actions.count(_cur_action_key) + 1  # 本次也计入
                            if action in ("click", "click_new_tab") and _cur_action_key[1] != 0
                            else 0
                        )
                        _last_actions.append(_cur_action_key)
                        if len(_last_actions) > _LOOP_GUARD_WINDOW:
                            _last_actions.pop(0)

                        # ── 方案②：把本次执行结果回填到 VLM 历史，让下一轮决策看见「已做什么」 ──
                        _post_pages_count = (
                            len(browser._context.pages) if browser._context else 0
                        )
                        _outcome_parts: list[str] = []
                        if _landing_url and _landing_url != _pre_url:
                            _outcome_parts.append(f"跳转 url={_landing_url[:80]}")
                        else:
                            _outcome_parts.append(f"url 未变 ({_landing_url[:60]})")
                        if _post_pages_count != _pre_pages_count:
                            _outcome_parts.append(
                                f"标签 {_pre_pages_count}→{_post_pages_count}"
                            )
                        if _tab_switched_this_step:
                            _outcome_parts.append("触发TabGuard切换")
                        vlm.annotate_last_result("✅ " + " | ".join(_outcome_parts))

                        if _click_repeat_count >= 3:
                            logger.warning(
                                f"[LOOP GUARD] 检测到动作死循环（URL-aware）！"
                                f"target_id={_cur_action_key[1]} 在最近 {_LOOP_GUARD_WINDOW} 步内"
                                f"被点击 {_click_repeat_count} 次且着陆 URL 相同 "
                                f"({_landing_url[:80]}), 交由 VLM 自主反思。"
                            )
                            _page_summary = await browser.get_active_page_summary()
                            _loop_guard_msg = (
                                f"🚨 严重警告：你陷入了操作死循环！\n"
                                f"系统检测到你在最近 {_LOOP_GUARD_WINDOW} 步里已经"
                                f"{_click_repeat_count} 次点击了同一个元素 #{_cur_action_key[1]}，"
                                f"且每次点击后着陆的 URL 完全相同（{_landing_url[:120]}），"
                                f"说明页面没有任何有效进展。"
                                f"这个元素大概率是无效的、被前端禁用的、或者是诱导点击的陷阱"
                                f"（例如 SEM/广告链接，点开后又被 close_tab 撤销）。\n"
                                f"【系统强制指令】：绝对禁止在下一步中再次尝试点击或操作该元素！"
                                f"请立刻观察最新截图，寻找其他完全不同的路径 —— "
                                f"例如改点击**第二个/第三个**搜索结果（跳过广告位），"
                                f"或换一个关键词重新搜索。"
                                f"如果当前页面已经明显达到用户目标状态"
                                f"（如表单已提交、已跳转到结果页、已进入目标标签页、已完成下载/导出），"
                                f"请直接输出 action=done 结束任务。"
                            )
                            if _page_summary:
                                _loop_guard_msg += f"\n【当前页面摘要】{_page_summary}"
                            vlm.inject_error_feedback(_loop_guard_msg)
                            _loop_guard_blocked_ids.add(_cur_action_key[1])
                            logger.info(
                                f"[LOOP GUARD] target_id={_cur_action_key[1]} 已加入黑名单，"
                                f"后续点击将被前置拦截。当前黑名单: {_loop_guard_blocked_ids}"
                            )
                            break  # 中止本批次，进入下一步（重新截图，让 VLM 看着报错重新决策）

                        _login_intercepted = await _run_preflight_login(
                            browser,
                            goal,
                            source=f"step-{step}-action-{_action_idx + 1}",
                            require_login=require_login,
                        )
                        if _login_intercepted:
                            logger.info("[PRELOGIN] Login popup handled after action; restarting from fresh page state.")
                            break
                        # 执行成功：清零连续错误计数
                        _consecutive_errors = 0

                        # 🛡️ 连招熔断保护 (Macro Guard)
                        # 仅在连招批次（len > 1）且非 goto 动作时检查域名漂移
                        if _batch_initial_domain and action != "goto":
                            _current_domain = urlparse(browser.current_url).netloc
                            if _current_domain and _current_domain != _batch_initial_domain:
                                _guard_msg = (
                                    f"🚨 严重警告：在执行第 {_action_idx + 1} 步（{action}）时，"
                                    f"页面意外跳转到了外部域名 {_current_domain}"
                                    f"（原域名：{_batch_initial_domain}），"
                                    f"可能是动态 DOM 变化导致误触了广告或隐藏链接。"
                                    f"\n连招已被强制中断！请仔细观察最新截图，"
                                    f"找到正确的元素 ID，重新规划动作，不要再下发长连招。"
                                )
                                logger.warning(
                                    f"[MACRO GUARD] 跨域跳转熔断: "
                                    f"{_batch_initial_domain} → {_current_domain}"
                                )
                                vlm.inject_error_feedback(_guard_msg)
                                break  # 中止本批次，进入下一步（重新截图）

                    except ActionExecutionError as exec_err:
                        _consecutive_errors += 1
                        err_msg = str(exec_err)
                        _log_error = err_msg
                        logger.warning(
                            f"[SELF-HEAL] Action failed ({_consecutive_errors}/{_MAX_CONSECUTIVE_ERRORS}): "
                            f"{err_msg}"
                        )
                        # 方案②：失败也回填历史，避免 VLM 误以为动作已经成功
                        vlm.annotate_last_result(f"❌ 失败: {err_msg[:80]}")

                        if _consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                            # 连续失败达到上限，交人工处理
                            _print_manual_warning(
                                "AGENT STUCK - CONSECUTIVE FAILURES",
                                f"The agent failed {_MAX_CONSECUTIVE_ERRORS} times in a row. "
                                f"Last error: {err_msg[:120]}",
                            )
                            await asyncio.get_event_loop().run_in_executor(None, input)
                            logger.info("[MANUAL] User confirmed, resuming agent...")
                            _consecutive_errors = 0
                            vlm.inject_error_feedback("")  # 清空积压反馈
                        else:
                            # 将错误注入下一轮 VLM 提示，引导换策略
                            vlm.inject_error_feedback(err_msg)
                        break  # 中止本批次，进入下一步（重新截图）

                # 内层批次循环结束：若任务完成则退出外层主循环
                if _task_completed:
                    break

            finally:
                # 无论本步以何种方式退出（continue/break/正常/异常），均实时写入轨迹日志
                html_logger.log_step(
                    step_num=step,
                    screenshot_path=_log_screenshot_path,
                    action_dict=_log_decision,
                    error_msg=_log_error,
                    memory_state=dict(workflow_memory),
                )

        else:
            # for-else: 循环正常结束（没有 break），说明达到最大步数
            logger.warning(f"[WARN] Max steps reached ({MAX_STEPS}), task not completed.")
            _broadcast_log_safe(f"[WARN] Max steps reached ({MAX_STEPS}), task not completed.", level="warn")
            _broadcast_done_safe(False, f"Max steps reached ({MAX_STEPS})")
            # 保存最终状态截图
            await browser.mark_and_screenshot(step=99)

    except KeyboardInterrupt:
        logger.info("\n[STOP] User interrupted execution.")
        _broadcast_log_safe("[STOP] User interrupted execution.", level="warn")
        _broadcast_done_safe(False, "User interrupted execution")
    except Exception as e:
        msg = str(e)
        if msg.startswith("STOP_REQUESTED::"):
            stop_ctx = msg.split("::", 1)[1] if "::" in msg else "unknown"
            logger.warning(f"[STOP] Agent cancelled by stop signal at {stop_ctx}")
            _broadcast_log_safe(
                f"[STOP] Agent cancelled by stop signal at {stop_ctx}",
                level="warn",
            )
            _broadcast_done_safe(False, "Agent cancelled by stop signal")
        else:
            logger.error(f"[ERROR] Agent exception: {type(e).__name__}: {e}", exc_info=True)
            _broadcast_log_safe(f"[ERROR] Agent exception: {type(e).__name__}: {e}", level="error")
            _broadcast_done_safe(False, f"Agent exception: {type(e).__name__}: {e}")
    finally:
        html_logger.finalize()
        await browser.close()
    return _run_succeeded


def main():
    """CLI 入口：解析命令行参数并启动 Agent。"""
    default_user_data_dir = str(Path(__file__).parent / "browser_data")

    parser = argparse.ArgumentParser(
        description="VSpider - Visual Web Agent (Offline/Intranet Edition)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python main.py --url "http://192.168.1.100/login" --goal "Login and query data"\n'
            '  python main.py --url "https://example.com" --goal "Find contact page"\n'
            '  python main.py --url "http://10.0.0.1" --goal "Login" --user-data-dir ./browser_data'
        ),
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Target webpage starting URL",
    )
    parser.add_argument(
        "--goal",
        required=True,
        help="Natural language task goal description",
    )
    parser.add_argument(
        "--user-data-dir",
        default=default_user_data_dir,
        help=f"Browser user data directory for session/cookie persistence (default: {default_user_data_dir})",
    )
    parser.add_argument(
        "--context",
        default="",
        help="Extra context instructions for the agent",
    )
    parser.add_argument(
        "--constraints",
        default="",
        help="Strict constraints and rules for the agent",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output requirements for extracted data",
    )
    parser.add_argument(
        "--xhr",
        action="store_true",
        default=False,
        help="Enable XHR/Fetch interceptor to capture API responses (for enterprise systems with JSON table data)",
    )
    parser.add_argument(
        "--upload-file",
        default="",
        help="Pre-configure the file path to upload when the agent encounters a file upload element (bypasses system file picker dialog)",
    )
    parser.add_argument(
        "--xhr-pattern",
        default="",
        dest="xhr_pattern",
        help=(
            "Enable XHR primary engine: URL substring to match against API responses. "
            "When a response URL contains this pattern, the data is captured immediately "
            "and the VLM loop is skipped. "
            "Example: --xhr-pattern 'api/board' (Baidu hot search), "
            "--xhr-pattern '/api/orders' (enterprise order API)."
        ),
    )
    parser.add_argument(
        "--require-login",
        action="store_true",
        default=False,
        help=(
            "Proactively attempt native account/password login for the current site before the VLM loop. "
            "Use this when a task should start from an authenticated state, such as Bilibili account-only actions."
        ),
    )
    args = parser.parse_args()

    # 将 user-data-dir 注入 config（始终生效，确保 Cookie 持久化）
    _apply_runtime_overrides(args)

    # 组装最终的目标 Prompt
    full_goal = args.goal
    if args.context:
        full_goal += f"\n\n【环境与上下文】\n{args.context}"
    if args.constraints:
        full_goal += f"\n\n【操作约束与限制】\n{args.constraints}"
    if args.output:
        full_goal += f"\n\n【输出要求】\n{args.output}"

    logger.info("VSpider - Visual Web Agent starting...")
    logger.info(f"Assembled Full Goal:\n{full_goal}")
    asyncio.run(
        run_agent(
            args.url,
            full_goal,
            enable_xhr=args.xhr,
            upload_file=args.upload_file,
            xhr_pattern=args.xhr_pattern,
            require_login=args.require_login,
        )
    )


if __name__ == "__main__":
    main()

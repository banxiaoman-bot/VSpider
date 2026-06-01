"""Detect mid-task session expiry / silent auth drop, and arm a return URL.

Failure mode covered
--------------------
You're 8 steps into "提取淘宝订单前 20 条" — the agent has scrolled, clicked
into a detail page, switched tabs. On step 9 the active page suddenly
redirects to ``passport.taobao.com/login.htm?from=...`` because the cookie
just expired or the platform's bot-control kicked in. The agent's planned
next step (extract orders) is now executing on a login form. Without a
sniffer:

  - VLM probably wastes 5+ steps trying to make sense of the login form
  - eventually ``ask_human`` triggers but the original target URL is gone
    (the redirect overwrote it) — user logs in, agent has no idea where it
    was supposed to go back to
  - the partial progress (8 steps of work, scrolled-to position, opened
    detail tab) is lost

With this guard:

  step 8: extract → succeeds → URL still ``trade.taobao.com/orderlist.htm?…``
  step 9: VLM emits next action (say, click row #3 to expand detail)
          ↓
          BEFORE the action runs, the sniffer compares last-known business URL
          to current URL. They're different and current matches login-URL
          patterns. Sniffer fires:
            - records ``return_url = trade.taobao.com/orderlist.htm?…``
            - rewrites action to ``ask_human``, reason includes the saved URL
            - main loop's post-ask_human handler reads the saved URL and
              ``goto``s back automatically after harvest
  user logs in manually
  next iteration: URL is back at the business page; agent resumes from where
  it left off, all progress intact

Trigger conditions (all must hold)
----------------------------------

1. We have a remembered "last business URL" (a URL the agent was working
   on at least one step ago that did NOT match login patterns).
2. The current URL is "login-shaped":
   - host or path contains ``/login`` / ``/signin`` / ``/passport`` /
     ``/sso`` / ``/auth`` etc.
   - OR the page has visible password / SMS / captcha input fields
     (this we can't check here without page access; left to a future
     enhancement — the URL heuristic alone catches most cases).
3. The agent is NOT already mid-``ask_human`` / ``done`` (idempotency).
4. The current URL is NOT the agent's initial start URL (those legitimately
   start on login pages and don't need rescue).

What this guard does NOT do
---------------------------

- It doesn't try to log in automatically — that's the user's job via the
  existing ``ask_human`` flow plus the new auto-harvest.
- It doesn't trip on "user typed into a chat input and the page jumped to
  /search/" — that's CHAT DRIFT GUARD's job; chat hosts are excluded here.
- It doesn't trip on the very first step (no remembered business URL yet).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

try:
    from .chat_entry_drift_guard import is_on_known_chat_domain
except ImportError:  # pragma: no cover
    from chat_entry_drift_guard import is_on_known_chat_domain  # type: ignore[no-redef]

logger = logging.getLogger("vspider.session_drop")


# URL fragments that strongly indicate a login / auth landing page. We match
# both path-component (``/login``, ``/signin``) and host-component
# (``passport.taobao.com``, ``login.example.com``) variants.
_LOGIN_URL_NEEDLES: tuple[str, ...] = (
    "/login",
    "/signin",
    "/sign-in",
    "/sign_in",
    "/passport",
    "/sso",
    "/oauth",
    "/auth/",
    "/account/login",
    "/user/login",
    "/auth_realms",
    "/security/login",
    "/identity/login",
)

# Host *prefixes* that mean "this whole subdomain is auth". A redirect to one
# of these is almost always a session drop.
_LOGIN_HOST_PREFIXES: tuple[str, ...] = (
    "passport.",
    "login.",
    "signin.",
    "sso.",
    "auth.",
    "id.",
    "accounts.",
    "account.",
)


def looks_like_login_url(url: str) -> bool:
    """True iff the URL looks like an auth/login page by host or path."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if not host and not path:
        return False
    # Host-prefix check
    for prefix in _LOGIN_HOST_PREFIXES:
        if host.startswith(prefix):
            return True
    # Path-fragment check
    for needle in _LOGIN_URL_NEEDLES:
        if needle in path:
            return True
    return False


def _same_origin(url_a: str, url_b: str) -> bool:
    if not url_a or not url_b:
        return False
    try:
        a = urlparse(url_a)
        b = urlparse(url_b)
    except Exception:
        return False
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


@dataclass(frozen=True)
class SessionDropSignal:
    """One firing of the sniffer. Caller turns this into ask_human + goto."""
    return_url: str       # business URL the agent was working on
    current_url: str      # login-shaped URL it just landed on
    reason: str           # short human-readable summary for log + feedback


def detect_session_drop(
    *,
    current_url: str,
    last_business_url: str,
    initial_url: str = "",
    step: int = 0,
) -> SessionDropSignal | None:
    """Return a SessionDropSignal when the agent appears to have been
    bounced to a login page mid-task. Returns ``None`` otherwise.

    Args:
        current_url: URL the active page is on right now.
        last_business_url: most recent URL we've seen that did NOT look
            like a login page. The caller (main loop) is responsible for
            tracking this — usually "the URL at end of the previous step
            if it wasn't login-shaped".
        initial_url: the user-supplied start URL of the run; if the agent
            STARTED on a login page that's not a session drop (legitimate
            login-required task).
        step: current step number; we don't fire on step 1 (no "previous
            business URL" yet, by definition).
    """
    # ── early outs ──────────────────────────────────────────────────────
    if step <= 1:
        return None
    if not current_url or not last_business_url:
        return None
    if not looks_like_login_url(current_url):
        return None
    # Initial URL was already login-shaped → this is a legit login task
    if initial_url and looks_like_login_url(initial_url):
        return None
    # Don't double-fire if business URL already pointed at login
    if looks_like_login_url(last_business_url):
        return None
    # Chat hosts: /search/ etc. on yiyan.baidu.com is normal — CHAT DRIFT
    # GUARD owns this case. We stay out.
    if is_on_known_chat_domain(current_url):
        return None

    # We require an origin change OR a clearly different path; if the URL
    # changed only in query string it's likely not a session drop.
    if _same_origin(current_url, last_business_url):
        try:
            cur_path = (urlparse(current_url).path or "").lower()
            last_path = (urlparse(last_business_url).path or "").lower()
        except Exception:
            cur_path, last_path = "", ""
        if cur_path == last_path:
            # Same origin AND same path — probably just a query-param refresh
            # that happens to match a login needle by coincidence.
            return None

    reason = (
        f"session drop detected at step {step}: business URL was "
        f"{last_business_url[:80]!r}, now on login-shaped "
        f"{current_url[:80]!r}"
    )
    logger.warning("[SESSION DROP] %s", reason)
    return SessionDropSignal(
        return_url=last_business_url,
        current_url=current_url,
        reason=reason,
    )


def build_feedback(signal: SessionDropSignal) -> str:
    """Human-readable feedback string injected into VLM's next prompt."""
    return (
        "🔐 [SESSION DROP GUARD] 任务进行中被重定向到了登录页：\n"
        f"  上一个业务页 URL: {signal.return_url[:140]}\n"
        f"  当前 URL: {signal.current_url[:140]}\n"
        "这通常意味着登录态过期或被风控触发。系统已自动转为 ask_human "
        "等待你登录；登录完成后会自动 goto 回原业务页继续执行。\n"
        "你不需要为此步骤做任何决策——本步动作已被改写为 ask_human。"
    )


__all__ = [
    "SessionDropSignal",
    "detect_session_drop",
    "build_feedback",
    "looks_like_login_url",
]

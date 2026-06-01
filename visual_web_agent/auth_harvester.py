"""Automatic storage-state harvester for the ``ask_human`` HITL path.

Why this module exists
----------------------
After a user takes over the browser to complete a login (scan QR / SMS code
/ click "agree"), Playwright's persistent context keeps the resulting
cookies in ``browser_data/`` — so a re-run of the same agent process
benefits. But that's the ONLY benefit; if the user later:

  - blows away ``browser_data/`` (sandbox reset, CI clean run, ...)
  - runs from a different working tree
  - uses ``--auth-profiles foo`` to inject a curated storage_state on a
    different machine

…their freshly-earned login is gone. They have to scan/SMS again.

This module closes the loop. After each ``ask_human`` resume we ask the
context for its full storage_state, filter to just the cookies/origins
that belong to the page we're on (so we don't smear yiyan cookies into a
zhihu profile), and write them to ``.auth/<auto_named>.json``. Next time
the same site is loaded — even from a clean machine — the same login is
auto-applied by ``apply_storage_state_to_context``.

Design choices
--------------

- **Filter by host**: only cookies whose ``domain`` matches the active
  page's host (or is a parent domain of it) get saved. Without this, the
  first ``ask_human`` on any site would dump the entire third-party
  cookie jar into the profile, which is both noisy and incorrect.

- **Skip empty harvests**: if the active page has zero matching cookies
  (rare but happens — e.g. user only clicked a CAPTCHA without
  authenticating), we skip writing. An empty profile is worse than no
  profile because it shadows the manual one on next load.

- **Don't overwrite a richer profile**: if ``.auth/<name>.json`` already
  exists and contains MORE matching cookies than what we're about to
  write, skip. Avoids degrading a hand-curated profile.

- **Auto-naming**: ``yiyan.baidu.com`` → ``yiyan_baidu_com``. Mirrors
  the convention used by ``tools/manual_auth.py``.

- **Never raise**: all errors are caught and logged. The harvester is a
  best-effort nice-to-have; it must not break the HITL flow if disk is
  full or storage_state can't be serialised.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from .auth_manager import (
        _host_matches,
        _normalize_host,
        project_auth_dir,
    )
except ImportError:  # pragma: no cover
    from auth_manager import (  # type: ignore[no-redef]
        _host_matches,
        _normalize_host,
        project_auth_dir,
    )

logger = logging.getLogger("vspider.auth")


# Minimum cookies for a harvest to be worth saving. Below this we consider
# the login attempt incomplete (e.g. user just dismissed a banner).
MIN_COOKIES_TO_SAVE = 1


@dataclass(frozen=True)
class HarvestResult:
    """Outcome of one harvest attempt; never raises, always serialisable."""
    saved: bool
    path: Path | None
    profile_name: str
    cookies_saved: int
    origins_saved: int
    reason: str  # human-readable summary for log + UI toast
    cf_clearance: bool = False
    cf_clearance_expires_in_hours: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "saved": self.saved,
            "path": str(self.path) if self.path else None,
            "profile_name": self.profile_name,
            "cookies_saved": self.cookies_saved,
            "origins_saved": self.origins_saved,
            "reason": self.reason,
            "cf_clearance": self.cf_clearance,
            "cf_clearance_expires_in_hours": self.cf_clearance_expires_in_hours,
        }


def inspect_cf_clearance(state: dict, host: str) -> tuple[bool, float | None]:
    """Return (has_cf_clearance, hours_until_expiry)."""
    import time as _time

    host = _normalize_host(host or "")
    if not host:
        return False, None
    now = _time.time()
    for cookie in state.get("cookies") or []:
        name = str(cookie.get("name") or "")
        if name != "cf_clearance":
            continue
        cookie_domain = str(cookie.get("domain") or "")
        if not _host_matches(host, cookie_domain):
            continue
        expires = cookie.get("expires")
        if expires in (None, -1, 0):
            return True, None
        try:
            exp_f = float(expires)
        except (TypeError, ValueError):
            return True, None
        if exp_f <= 0:
            return True, None
        hours_left = max(0.0, (exp_f - now) / 3600.0)
        return True, round(hours_left, 2)
    return False, None


def _derive_profile_name(host: str) -> str:
    """``yiyan.baidu.com`` → ``yiyan_baidu_com``.

    Mirrors the existing naming convention from ``tools/manual_auth.py``
    so files line up alphabetically and users can pick either harvested
    or manual profiles by the same short name.
    """
    host = _normalize_host(host or "")
    if not host:
        return ""
    # Strip common prefixes that don't help disambiguate
    for prefix in ("www.", "passport.", "login.", "auth."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", host).strip("._-")
    safe = safe.replace(".", "_")
    return safe or ""


def _filter_state_to_host(state: dict, host: str) -> tuple[dict, int, int]:
    """Return a new storage_state containing only cookies/origins for ``host``.

    The match rule is the same as ``apply_storage_state_to_context``'s
    inverse: a cookie with domain ``.baidu.com`` matches ``yiyan.baidu.com``.
    """
    host = _normalize_host(host or "")
    if not host:
        return {"cookies": [], "origins": []}, 0, 0

    cookies_in: list[dict] = list(state.get("cookies") or [])
    origins_in: list[dict] = list(state.get("origins") or [])

    cookies_out: list[dict] = []
    for cookie in cookies_in:
        cookie_domain = str(cookie.get("domain") or "")
        if _host_matches(host, cookie_domain):
            cookies_out.append(cookie)

    origins_out: list[dict] = []
    for origin in origins_in:
        origin_url = str(origin.get("origin") or "")
        origin_host = _normalize_host(origin_url)
        # origin must be the same host or a subdomain we're on
        if origin_host and (origin_host == host or origin_host.endswith(f".{host}") or host.endswith(f".{origin_host}")):
            origins_out.append(origin)

    return (
        {"cookies": cookies_out, "origins": origins_out},
        len(cookies_out),
        len(origins_out),
    )


def _count_matching_cookies(path: Path, host: str) -> int:
    """How many host-matching cookies an existing profile already has.
    Used to decide whether the new harvest is an upgrade or a downgrade."""
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(existing, dict):
        return 0
    _, count, _ = _filter_state_to_host(existing, host)
    return count


async def harvest_storage_state(
    context: Any,
    *,
    hint_host: str,
    profile_name: str | None = None,
    auth_dir: Path | None = None,
    min_cookies: int = MIN_COOKIES_TO_SAVE,
) -> HarvestResult:
    """Snapshot the context's storage_state and persist the host's subset.

    Args:
        context: Playwright BrowserContext (we call ``await context.storage_state()``)
        hint_host: Host of the page the user just logged into. Drives both
            the cookie/origin filter and the auto-named profile filename.
        profile_name: Override the auto-derived name. The function will
            append ``.json`` if absent. Use this to explicitly point at
            an existing profile to refresh.
        auth_dir: Override ``.auth/`` location (used by tests).
        min_cookies: Threshold below which we don't save (default 1).

    Returns:
        ``HarvestResult`` — always returns, never raises. Inspect ``.saved``
        and ``.reason`` to know what happened.
    """
    auth_dir = auth_dir or project_auth_dir()

    # ── 1. derive profile name ──────────────────────────────────────────
    host = _normalize_host(hint_host or "")
    derived = profile_name.strip() if profile_name else _derive_profile_name(host)
    if not derived:
        return HarvestResult(
            saved=False, path=None, profile_name="",
            cookies_saved=0, origins_saved=0,
            reason="cannot derive profile name from empty host",
        )
    if not derived.endswith(".json"):
        derived = derived + ".json"

    # ── 2. snapshot full storage_state ──────────────────────────────────
    try:
        full_state = await context.storage_state()
    except Exception as e:
        logger.warning("[AUTH HARVEST] storage_state() failed: %s", e)
        return HarvestResult(
            saved=False, path=None, profile_name=derived[:-5],
            cookies_saved=0, origins_saved=0,
            reason=f"storage_state() raised {type(e).__name__}",
        )

    # ── 3. filter to just the relevant host ─────────────────────────────
    filtered, cookie_count, origin_count = _filter_state_to_host(full_state, host)
    if cookie_count < min_cookies:
        return HarvestResult(
            saved=False, path=None, profile_name=derived[:-5],
            cookies_saved=cookie_count, origins_saved=origin_count,
            reason=(
                f"only {cookie_count} matching cookie(s) for host={host}; "
                "below MIN_COOKIES_TO_SAVE — login likely incomplete"
            ),
        )

    # ── 4. don't overwrite a richer existing profile ────────────────────
    auth_dir.mkdir(parents=True, exist_ok=True)
    target = auth_dir / derived
    if target.exists():
        existing_count = _count_matching_cookies(target, host)
        if existing_count > cookie_count:
            return HarvestResult(
                saved=False, path=target, profile_name=derived[:-5],
                cookies_saved=cookie_count, origins_saved=origin_count,
                reason=(
                    f"existing profile has {existing_count} matching cookies, "
                    f"new harvest has only {cookie_count}; refusing downgrade"
                ),
            )

    # ── 5. write ─────────────────────────────────────────────────────────
    try:
        target.write_text(
            json.dumps(filtered, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("[AUTH HARVEST] write failed: %s", e)
        return HarvestResult(
            saved=False, path=target, profile_name=derived[:-5],
            cookies_saved=cookie_count, origins_saved=origin_count,
            reason=f"write failed: {type(e).__name__}: {e}",
        )

    logger.info(
        "[AUTH HARVEST] saved %s (cookies=%d origins=%d host=%s)",
        target, cookie_count, origin_count, host,
    )
    has_cf, cf_hours = inspect_cf_clearance(filtered, host)
    reason = f"saved {cookie_count} cookies + {origin_count} origins → {target.name}"
    if has_cf:
        if cf_hours is not None:
            reason += f"; cf_clearance cached (~{cf_hours}h remaining)"
        else:
            reason += "; cf_clearance cached (session cookie)"
    return HarvestResult(
        saved=True, path=target, profile_name=derived[:-5],
        cookies_saved=cookie_count, origins_saved=origin_count,
        reason=reason,
        cf_clearance=has_cf,
        cf_clearance_expires_in_hours=cf_hours,
    )


def cf_clearance_profile_hint(
    *,
    host: str,
    auth_dir: Path | None = None,
    profile_name: str | None = None,
) -> str:
    """Human-readable hint when a loaded auth profile includes cf_clearance."""
    host = _normalize_host(host or "")
    if not host:
        return ""
    auth_dir = auth_dir or project_auth_dir()
    derived = profile_name.strip() if profile_name else _derive_profile_name(host)
    if not derived:
        return ""
    path = auth_dir / (derived if derived.endswith(".json") else f"{derived}.json")
    if not path.exists():
        return ""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(state, dict):
        return ""
    has_cf, hours = inspect_cf_clearance(state, host)
    if not has_cf:
        return ""
    if hours is not None:
        return f"auth profile `{path.name}` 含 cf_clearance（约 {hours}h 内有效）"
    return f"auth profile `{path.name}` 含 cf_clearance（会话级）"


__all__ = [
    "HarvestResult",
    "MIN_COOKIES_TO_SAVE",
    "cf_clearance_profile_hint",
    "harvest_storage_state",
    "inspect_cf_clearance",
    "_derive_profile_name",
    "_filter_state_to_host",
]

"""DC-2 · Cookie/consent-wall auto guard (perception-entry).

Wires the DC-1 ``dismiss_consent`` deterministic capability into the agent loop
as an automatic, idempotent, once-per-URL guard: before the agent "looks" at a
page (perception phase), clear any cookie/GDPR consent wall so the screenshot +
SoM + AX tree describe the real, unblocked content — without spending a VLM turn
to visually find and click an "Accept all" button.

Design (mirrors the project's mission rule #1 准确 / #2 高效):
  - Reuses ``DismissConsentHandler.scan_and_dismiss`` (pure core, no side
    effects) so there is a single source of truth for CMP detection/clicking.
  - Deduplicates by normalized URL: each page is guarded at most once per run,
    so it never loops and costs at most one JS probe per frame per new URL.
  - Records an ``rpa_trail`` entry only when it actually dismisses a wall (tagged
    ``source="perception_guard"`` to distinguish it from explicit VLM actions),
    keeping the trail clean on the common no-wall path.
  - Never raises: a scan blow-up is swallowed so the perception phase (core
    loop) can never be broken by the guard.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

try:  # package import (normal run)
    from .actions import DismissConsentHandler
except ImportError:  # script-mode fallback (main.py non-package execution)
    from actions import DismissConsentHandler  # type: ignore

logger = logging.getLogger("visual_web_agent.consent_guard")


def _norm_url(url: str) -> str:
    """Stable dedup key: drop the fragment and any trailing slash on the path.

    ``https://x/a/?q=1#frag`` and ``https://x/a?q=1`` collapse to one key so the
    guard does not re-run when only the in-page anchor changes.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        path = parts.path.rstrip("/")
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
    except Exception:
        return url


async def auto_dismiss_consent(
    browser: Any,
    page: Any,
    *,
    handled_urls: set[str],
    source: str = "perception_guard",
) -> Optional[dict]:
    """Run the consent guard once for ``page``'s URL. Returns the scan result.

    Returns ``None`` when there is nothing to do (no page, or the URL was
    already guarded). Marks the URL handled up-front so a transient scan error
    does not cause the guard to re-fire every perception turn — the explicit
    ``dismiss_consent`` action remains available as a backstop.
    """
    if page is None:
        return None

    url = (getattr(page, "url", "") or "") or (getattr(browser, "current_url", "") or "")
    key = _norm_url(url)
    if key and key in handled_urls:
        return None
    if key:
        handled_urls.add(key)

    try:
        result = await DismissConsentHandler().scan_and_dismiss(page)
    except Exception as exc:  # perception must never break because of the guard
        logger.debug("[CONSENT GUARD] scan failed at %s: %s", url[:80], exc)
        return None

    if isinstance(result, dict) and result.get("dismissed"):
        result["source"] = source
        try:
            browser.rpa_trail.append(dict(result))
        except Exception as trail_err:
            logger.debug("[CONSENT GUARD] rpa_trail append skipped: %s", trail_err)
        logger.info(
            "[CONSENT GUARD] auto-dismissed consent wall at %s (cmp=%s strategy=%s)",
            url[:80], result.get("cmp"), result.get("strategy"),
        )
    return result

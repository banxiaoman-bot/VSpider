"""Slice STEALTH-1: Chromium-consistent User-Agent + Client Hints (pure + thin probe).

Cloudflare and similar WAFs cross-check the navigator User-Agent against the
low-entropy Client Hints the browser sends (Sec-CH-UA / Sec-CH-UA-Mobile /
Sec-CH-UA-Platform). A hard-coded UA whose Chrome major version no longer matches
the actual Chromium build -- or whose platform differs from what the browser
advertises -- is itself a bot signal.

This module keeps the *real* Chromium major version and a single declared
platform so the UA string, the Sec-CH-UA brand list, and Sec-CH-UA-Platform all
agree. Pure functions are fully stub-testable; the only side-effecting helper
(`detect_chromium_major`) shells out to ``<chromium> --version`` once and always
falls back to a sane default, so callers can treat it as total.

Browser wiring lives in ``browser_env.py`` (workflow section 3: keep that
oversized file thin; new capability goes in its own module).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# Fallback major used only when probing the Chromium binary fails. Kept on a
# recent stable so a failed probe still looks current instead of the legacy 124.
DEFAULT_CHROME_MAJOR = 131

# GREASE brand Chromium ships in its low-entropy brand list.
_GREASE_BRAND = "Not?A_Brand"
_GREASE_VERSION = "24"

_FULL_VERSION_RE = re.compile(r"(\d+)\.\d+\.\d+\.\d+")
_BARE_MAJOR_RE = re.compile(r"\b(\d{2,3})\b")


@dataclass(frozen=True)
class StealthProfile:
    """A consistent identity bundle: same major + platform across UA and hints."""

    major: int
    platform: str  # canonical CH token: "Windows" | "macOS" | "Linux"
    user_agent: str
    client_hints: dict[str, str]


def parse_chromium_major(
    version_text: str | None,
    *,
    fallback: int = DEFAULT_CHROME_MAJOR,
) -> int:
    """Extract the Chrome/Chromium major version from ``--version`` output.

    Accepts ``"Chromium 124.0.6367.207"``, ``"Google Chrome 131.0.6778.86"`` or a
    bare ``"131"``. Returns ``fallback`` when nothing parseable is found.
    """
    if not version_text:
        return fallback
    text = str(version_text)
    match = _FULL_VERSION_RE.search(text)
    if match:
        major = int(match.group(1))
        return major if major > 0 else fallback
    bare = _BARE_MAJOR_RE.search(text)
    if bare:
        major = int(bare.group(1))
        return major if major > 0 else fallback
    return fallback


def detect_chromium_major(
    executable_path: str | None,
    *,
    fallback: int = DEFAULT_CHROME_MAJOR,
    timeout: float = 5.0,
) -> int:
    """Best-effort probe of the real Chromium major version.

    Shells out to ``<executable> --version`` once. Any failure (missing binary,
    timeout, non-zero exit, unparseable output) yields ``fallback`` so callers
    never have to depend on the probe succeeding.
    """
    if not executable_path:
        return fallback
    try:
        proc = subprocess.run(
            [executable_path, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return fallback
    combined = f"{proc.stdout or ''} {proc.stderr or ''}"
    return parse_chromium_major(combined, fallback=fallback)


def _ua_platform_token(platform: str) -> str:
    token = (platform or "").strip().lower()
    if token in {"mac", "macos", "darwin"}:
        return "Macintosh; Intel Mac OS X 10_15_7"
    if token == "linux":
        return "X11; Linux x86_64"
    return "Windows NT 10.0; Win64; x64"


def _ch_platform_token(platform: str) -> str:
    token = (platform or "").strip().lower()
    if token in {"mac", "macos", "darwin"}:
        return "macOS"
    if token == "linux":
        return "Linux"
    return "Windows"


def build_user_agent(major: int, *, platform: str = "Windows") -> str:
    """Build a desktop Chrome UA whose major version == ``major``.

    The version tail is the reduced ``.0.0.0`` exactly like modern Chrome, and
    the string never contains "Headless" so headless runs do not leak that token.
    """
    return (
        f"Mozilla/5.0 ({_ua_platform_token(platform)}) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{int(major)}.0.0.0 Safari/537.36"
    )


def default_user_agent(*, platform: str = "Windows", major: int | None = None) -> str:
    """UA for non-browser HTTP helpers (urllib / requests) without a live
    Chromium context to probe.

    Centralises the literal so auxiliary fetch paths stay on a current, well
    formed Chrome UA (``DEFAULT_CHROME_MAJOR``) instead of drifting to a stale
    hand-written string. Pass ``major`` when a probed version is already known.
    """
    resolved = major if (major and major > 0) else DEFAULT_CHROME_MAJOR
    return build_user_agent(resolved, platform=platform)


def build_sec_ch_ua(major: int) -> str:
    """Build the ``Sec-CH-UA`` brand list, e.g.
    ``"Chromium";v="131", "Google Chrome";v="131", "Not?A_Brand";v="24"``.
    """
    value = int(major)
    return (
        f'"Chromium";v="{value}", '
        f'"Google Chrome";v="{value}", '
        f'"{_GREASE_BRAND}";v="{_GREASE_VERSION}"'
    )


def build_client_hints(major: int, *, platform: str = "Windows") -> dict[str, str]:
    """Build the low-entropy Client Hints headers that must agree with the UA."""
    return {
        "sec-ch-ua": build_sec_ch_ua(major),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{_ch_platform_token(platform)}"',
    }


def build_profile(
    executable_path: str | None = None,
    *,
    platform: str = "Windows",
    fallback: int = DEFAULT_CHROME_MAJOR,
) -> StealthProfile:
    """Probe the real Chromium version, then build a consistent UA + hints set."""
    major = detect_chromium_major(executable_path, fallback=fallback)
    return StealthProfile(
        major=major,
        platform=_ch_platform_token(platform),
        user_agent=build_user_agent(major, platform=platform),
        client_hints=build_client_hints(major, platform=platform),
    )


# --- Slice STEALTH-3: CDP Emulation.setUserAgentOverride (high-entropy) -----
#
# Header injection (STEALTH-1) only fixes the *HTTP request* Sec-CH-UA. It does
# NOT populate ``navigator.userAgentData`` (the JS-side high-entropy hints) nor
# does it reach workers / cross-origin iframes that issue their own requests.
# Cloudflare's JS challenge reads ``navigator.userAgentData.getHighEntropyValues``
# (platform, platformVersion, architecture, bitness, fullVersionList); if those
# disagree with the UA string or are empty under a spoofed UA, that itself is a
# bot tell. Driving CDP ``Emulation.setUserAgentOverride`` with a full
# ``userAgentMetadata`` makes the JS-side identity match the HTTP-side one across
# every frame and worker.


def _navigator_platform(platform: str) -> str:
    """navigator.platform value (distinct from the CH platform token).

    Desktop Chrome reports ``Win32`` even on 64-bit Windows, ``MacIntel`` on
    Apple, and ``Linux x86_64`` on Linux.
    """
    token = (platform or "").strip().lower()
    if token in {"mac", "macos", "darwin"}:
        return "MacIntel"
    if token == "linux":
        return "Linux x86_64"
    return "Win32"


def _platform_version(platform: str) -> str:
    """High-entropy ``platformVersion`` hint.

    Windows 11 is advertised as the ``"15.0.0"`` family in UA-CH; we keep a
    recent, plausible value per platform. Linux reports an empty string just
    like real Chrome.
    """
    token = (platform or "").strip().lower()
    if token in {"mac", "macos", "darwin"}:
        return "14.0.0"
    if token == "linux":
        return ""
    return "15.0.0"


def build_ua_metadata(major: int, *, platform: str = "Windows") -> dict:
    """Build the CDP ``userAgentMetadata`` object for setUserAgentOverride.

    ``brands`` carry the reduced (major-only) versions exactly like the
    Sec-CH-UA header, while ``fullVersionList`` carries the reduced full version
    (``{major}.0.0.0``) so the JS-side high-entropy list stays consistent with
    the spoofed UA string instead of leaking a real build number.
    """
    value = int(major)
    brands = [
        {"brand": "Chromium", "version": str(value)},
        {"brand": "Google Chrome", "version": str(value)},
        {"brand": _GREASE_BRAND, "version": _GREASE_VERSION},
    ]
    full = f"{value}.0.0.0"
    full_version_list = [
        {"brand": "Chromium", "version": full},
        {"brand": "Google Chrome", "version": full},
        {"brand": _GREASE_BRAND, "version": f"{_GREASE_VERSION}.0.0.0"},
    ]
    return {
        "brands": brands,
        "fullVersionList": full_version_list,
        "fullVersion": full,
        "platform": _ch_platform_token(platform),
        "platformVersion": _platform_version(platform),
        "architecture": "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False,
    }


def build_cdp_ua_override(
    profile: StealthProfile,
    *,
    accept_language: str = "",
) -> dict:
    """Params for CDP ``Emulation.setUserAgentOverride``.

    Propagates the same identity as the header-level UA + Client Hints to the
    JS side (``navigator.userAgentData``) and to workers / iframes. ``profile``
    already carries the canonical CH platform token (``Windows`` / ``macOS`` /
    ``Linux``) so the navigator.platform value and metadata stay derived from a
    single source of truth. ``acceptLanguage`` is only included when non-empty,
    so we never clobber the context's natural Accept-Language by default.
    """
    params: dict = {
        "userAgent": profile.user_agent,
        "platform": _navigator_platform(profile.platform),
        "userAgentMetadata": build_ua_metadata(profile.major, platform=profile.platform),
    }
    lang = (accept_language or "").strip()
    if lang:
        params["acceptLanguage"] = lang
    return params

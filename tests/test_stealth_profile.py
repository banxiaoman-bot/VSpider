"""Slice STEALTH-1 tests: UA <-> Client Hints version/platform consistency.

P0 contract: the navigator User-Agent string, the Sec-CH-UA brand list, and
Sec-CH-UA-Platform must all agree on the same Chrome major version and platform.
A mismatch (hard-coded UA major vs real Chromium build, or fake platform vs host
OS) is itself a Cloudflare bot signal. These tests pin that contract and the
fail-safe fallback behaviour of the version probe (no network, fully stubbable).
"""

from __future__ import annotations

from visual_web_agent.stealth_profile import (
    DEFAULT_CHROME_MAJOR,
    StealthProfile,
    build_client_hints,
    build_profile,
    build_sec_ch_ua,
    build_user_agent,
    default_user_agent,
    detect_chromium_major,
    parse_chromium_major,
)


# --- parse_chromium_major --------------------------------------------------

def test_parse_chromium_full_version() -> None:
    assert parse_chromium_major("Chromium 124.0.6367.207") == 124


def test_parse_google_chrome_full_version() -> None:
    assert parse_chromium_major("Google Chrome 131.0.6778.86") == 131


def test_parse_bare_major() -> None:
    assert parse_chromium_major("131") == 131


def test_parse_empty_uses_default_fallback() -> None:
    assert parse_chromium_major("") == DEFAULT_CHROME_MAJOR
    assert parse_chromium_major(None) == DEFAULT_CHROME_MAJOR


def test_parse_garbage_uses_custom_fallback() -> None:
    assert parse_chromium_major("no version here!", fallback=130) == 130


# --- detect_chromium_major (probe with fallback) ---------------------------

def test_detect_none_path_returns_fallback() -> None:
    assert detect_chromium_major(None) == DEFAULT_CHROME_MAJOR


def test_detect_missing_binary_returns_fallback() -> None:
    assert detect_chromium_major("/no/such/chromium/binary", fallback=129) == 129


# --- build_user_agent ------------------------------------------------------

def test_ua_has_requested_major_and_no_headless() -> None:
    ua = build_user_agent(131)
    assert "Chrome/131.0.0.0" in ua
    assert "Headless" not in ua
    assert "Windows NT 10.0" in ua


def test_ua_macos_platform_token() -> None:
    ua = build_user_agent(131, platform="macOS")
    assert "Macintosh" in ua
    assert "Windows" not in ua


def test_ua_linux_platform_token() -> None:
    ua = build_user_agent(120, platform="linux")
    assert "X11; Linux x86_64" in ua


# --- default_user_agent (auxiliary urllib/requests helpers) ----------------

def test_default_ua_uses_current_major_not_stale_124() -> None:
    ua = default_user_agent()
    assert f"Chrome/{DEFAULT_CHROME_MAJOR}.0.0.0" in ua
    # the regression this fixes: aux fetch paths used to hard-code Chrome/124
    assert "Chrome/124 " not in ua
    assert "Headless" not in ua


def test_default_ua_honors_probed_major() -> None:
    assert "Chrome/130.0.0.0" in default_user_agent(major=130)


def test_default_ua_ignores_nonpositive_major() -> None:
    ua = default_user_agent(major=0)
    assert f"Chrome/{DEFAULT_CHROME_MAJOR}.0.0.0" in ua


def test_default_ua_platform_token() -> None:
    assert "Macintosh" in default_user_agent(platform="macOS")


# --- build_sec_ch_ua / client hints ----------------------------------------

def test_sec_ch_ua_lists_version_and_grease() -> None:
    s = build_sec_ch_ua(131)
    assert '"Chromium";v="131"' in s
    assert '"Google Chrome";v="131"' in s
    assert "Not" in s  # GREASE brand present


def test_client_hints_windows_shape() -> None:
    ch = build_client_hints(131, platform="Windows")
    assert ch["sec-ch-ua-platform"] == '"Windows"'
    assert ch["sec-ch-ua-mobile"] == "?0"
    assert '"131"' in ch["sec-ch-ua"]


def test_client_hints_linux_platform() -> None:
    ch = build_client_hints(120, platform="linux")
    assert ch["sec-ch-ua-platform"] == '"Linux"'


# --- build_profile: the consistency contract -------------------------------

def test_profile_ua_and_hints_share_major_and_platform() -> None:
    prof = build_profile(None, platform="Windows", fallback=128)
    assert isinstance(prof, StealthProfile)
    assert prof.major == 128
    # the whole point of P0: UA version == Client Hints version
    assert "Chrome/128.0.0.0" in prof.user_agent
    assert '"128"' in prof.client_hints["sec-ch-ua"]
    # and platform agrees between UA token and CH platform
    assert "Windows NT 10.0" in prof.user_agent
    assert prof.client_hints["sec-ch-ua-platform"] == '"Windows"'
    assert "Headless" not in prof.user_agent


def test_profile_platform_consistency_macos() -> None:
    prof = build_profile(None, platform="macOS", fallback=127)
    assert "Macintosh" in prof.user_agent
    assert prof.client_hints["sec-ch-ua-platform"] == '"macOS"'
    assert '"127"' in prof.client_hints["sec-ch-ua"]

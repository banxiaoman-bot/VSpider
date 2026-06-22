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
    build_cdp_ua_override,
    build_client_hints,
    build_profile,
    build_sec_ch_ua,
    build_ua_metadata,
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


# --- Slice STEALTH-3: CDP userAgentMetadata high-entropy hints --------------

def test_ua_metadata_brands_are_major_only() -> None:
    meta = build_ua_metadata(131, platform="Windows")
    brands = {b["brand"]: b["version"] for b in meta["brands"]}
    assert brands["Chromium"] == "131"
    assert brands["Google Chrome"] == "131"
    # GREASE brand present so the list shape matches real Chrome
    assert any("Not" in b for b in brands)


def test_ua_metadata_full_version_list_is_reduced_not_real_build() -> None:
    meta = build_ua_metadata(131, platform="Windows")
    full = {b["brand"]: b["version"] for b in meta["fullVersionList"]}
    # reduced full version keeps JS-side consistent with the spoofed UA and
    # never leaks a real build number like 131.0.6778.86
    assert full["Chromium"] == "131.0.0.0"
    assert full["Google Chrome"] == "131.0.0.0"
    assert meta["fullVersion"] == "131.0.0.0"


def test_ua_metadata_windows_platform_shape() -> None:
    meta = build_ua_metadata(131, platform="Windows")
    assert meta["platform"] == "Windows"
    assert meta["platformVersion"] == "15.0.0"
    assert meta["mobile"] is False
    assert meta["bitness"] == "64"
    assert meta["architecture"] == "x86"
    assert meta["wow64"] is False


def test_ua_metadata_macos_platform_shape() -> None:
    meta = build_ua_metadata(127, platform="macOS")
    assert meta["platform"] == "macOS"
    assert meta["platformVersion"] == "14.0.0"


def test_ua_metadata_linux_platform_version_empty() -> None:
    meta = build_ua_metadata(120, platform="linux")
    assert meta["platform"] == "Linux"
    assert meta["platformVersion"] == ""


def test_cdp_override_matches_profile_identity() -> None:
    prof = build_profile(None, platform="Windows", fallback=131)
    params = build_cdp_ua_override(prof)
    # JS-side UA == header-side UA (single source of truth)
    assert params["userAgent"] == prof.user_agent
    # navigator.platform is Win32 on 64-bit Windows, not the CH token
    assert params["platform"] == "Win32"
    # metadata major agrees with the profile / CH header major
    brands = {b["brand"]: b["version"] for b in params["userAgentMetadata"]["brands"]}
    assert brands["Google Chrome"] == "131"
    # acceptLanguage omitted by default so we don't clobber context locale
    assert "acceptLanguage" not in params


def test_cdp_override_navigator_platform_macos() -> None:
    prof = build_profile(None, platform="macOS", fallback=128)
    params = build_cdp_ua_override(prof)
    assert params["platform"] == "MacIntel"
    assert params["userAgentMetadata"]["platform"] == "macOS"


def test_cdp_override_navigator_platform_linux() -> None:
    prof = build_profile(None, platform="linux", fallback=120)
    params = build_cdp_ua_override(prof)
    assert params["platform"] == "Linux x86_64"


def test_cdp_override_includes_accept_language_when_given() -> None:
    prof = build_profile(None, platform="Windows", fallback=131)
    params = build_cdp_ua_override(prof, accept_language="zh-CN,zh;q=0.9")
    assert params["acceptLanguage"] == "zh-CN,zh;q=0.9"

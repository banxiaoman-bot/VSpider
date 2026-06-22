"""Slice STEALTH-3 wiring: BrowserEnv._apply_cdp_ua_override drives CDP.

Header injection (STEALTH-1) only fixes the HTTP Sec-CH-UA. STEALTH-3 also
pushes the same identity to the JS side (navigator.userAgentData) and to
workers / iframes via CDP Emulation.setUserAgentOverride. These stub-frame
tests pin that wiring without launching a real browser:

  - the override is sent once per registered page with the profile identity
  - a missing profile is a no-op (no CDP session opened)
  - a CDP failure is swallowed (page registration must never break)
"""

from __future__ import annotations

import asyncio

from visual_web_agent.browser_env import BrowserEnv
from visual_web_agent.stealth_profile import build_profile


class _StubCDP:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict]] = []
        self._fail = fail

    async def send(self, method: str, params: dict | None = None) -> dict:
        if self._fail:
            raise RuntimeError("CDP boom")
        self.sent.append((method, dict(params or {})))
        return {}


class _StubContext:
    def __init__(self, cdp: _StubCDP) -> None:
        self._cdp = cdp
        self.cdp_sessions_opened = 0

    async def new_cdp_session(self, page):  # noqa: ANN001 - stub
        self.cdp_sessions_opened += 1
        return self._cdp


class _StubPage:
    def __init__(self, cdp: _StubCDP) -> None:
        self.context = _StubContext(cdp)


def _make_env(profile) -> BrowserEnv:
    # Bypass __init__ side effects (screenshot dirs etc.); we only exercise the
    # one method, so a bare instance with the profile attr is enough.
    env = BrowserEnv.__new__(BrowserEnv)
    env._stealth_profile = profile
    return env


def test_override_sent_once_with_profile_identity() -> None:
    profile = build_profile(None, platform="Windows", fallback=131)
    cdp = _StubCDP()
    page = _StubPage(cdp)
    env = _make_env(profile)

    asyncio.run(env._apply_cdp_ua_override(page))

    assert page.context.cdp_sessions_opened == 1
    assert len(cdp.sent) == 1
    method, params = cdp.sent[0]
    assert method == "Emulation.setUserAgentOverride"
    # JS-side UA must equal the header-side UA (single source of truth)
    assert params["userAgent"] == profile.user_agent
    # navigator.platform is Win32 on 64-bit Windows
    assert params["platform"] == "Win32"
    # high-entropy metadata major agrees with the profile
    brands = {b["brand"]: b["version"] for b in params["userAgentMetadata"]["brands"]}
    assert brands["Google Chrome"] == "131"
    assert params["userAgentMetadata"]["platform"] == "Windows"


def test_missing_profile_is_noop() -> None:
    cdp = _StubCDP()
    page = _StubPage(cdp)
    env = _make_env(None)

    asyncio.run(env._apply_cdp_ua_override(page))

    # No CDP session opened, nothing sent.
    assert page.context.cdp_sessions_opened == 0
    assert cdp.sent == []


def test_cdp_failure_is_swallowed() -> None:
    profile = build_profile(None, platform="Windows", fallback=131)
    cdp = _StubCDP(fail=True)
    page = _StubPage(cdp)
    env = _make_env(profile)

    # Must not raise even though cdp.send blows up.
    asyncio.run(env._apply_cdp_ua_override(page))

    assert cdp.sent == []

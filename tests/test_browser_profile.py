"""Tests for ``visual_web_agent.browser_profile`` (E1c-3b-2a).

Pure resolution of the persistent-context ``user_data_dir`` a ``BrowserEnv``
launch should use. An explicit ``override`` (cross-system session isolation,
so each system gets its own Chromium profile and avoids
``launch_persistent_context``'s single-instance lock) wins over the global
default, which wins over the packaged fallback. The function is pure -- no IO,
no ``config`` import -- so callers pass the values in and the selection logic
is unit-testable without Playwright.
"""

from __future__ import annotations

from pathlib import Path

from visual_web_agent.browser_profile import resolve_user_data_dir


_FALLBACK = Path("/pkg/browser_data")


class TestResolveUserDataDir:
    def test_override_wins_over_default(self) -> None:
        got = resolve_user_data_dir(
            "/tmp/sys_alpha", default_base="/global/profile", packaged_fallback=_FALLBACK
        )
        assert got == Path("/tmp/sys_alpha")

    def test_falls_back_to_default_base_when_no_override(self) -> None:
        got = resolve_user_data_dir(
            None, default_base="/global/profile", packaged_fallback=_FALLBACK
        )
        assert got == Path("/global/profile")

    def test_packaged_fallback_when_override_and_default_empty(self) -> None:
        got = resolve_user_data_dir("", default_base="", packaged_fallback=_FALLBACK)
        assert got == _FALLBACK

    def test_blank_override_is_ignored(self) -> None:
        got = resolve_user_data_dir(
            "   ", default_base="/global/profile", packaged_fallback=_FALLBACK
        )
        assert got == Path("/global/profile")

    def test_blank_default_base_falls_through_to_fallback(self) -> None:
        got = resolve_user_data_dir(None, default_base="   ", packaged_fallback=_FALLBACK)
        assert got == _FALLBACK

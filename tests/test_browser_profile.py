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

import os
import time
from pathlib import Path

from visual_web_agent.browser_profile import (
    gc_profile_dirs,
    resolve_user_data_dir,
    select_stale_profile_dirs,
)


_FALLBACK = Path("/pkg/browser_data")
_NOW = 1_000_000.0


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


class TestSelectStaleProfileDirs:
    """Pure selection (PROFILE-GC): from (name, mtime) entries pick the
    cross-system isolated profile dirs (sys_*) older than the TTL and not
    currently in use. Only sys_* are ever eligible -- the primary / global
    profile and any other entry are never selected. Pure: no IO."""

    def test_old_sys_dir_is_stale(self) -> None:
        entries = [("sys_alpha", _NOW - 7200)]  # 2h old, ttl 1h
        assert select_stale_profile_dirs(entries, ttl_seconds=3600, now=_NOW) == ["sys_alpha"]

    def test_fresh_sys_dir_not_stale(self) -> None:
        entries = [("sys_alpha", _NOW - 60)]
        assert select_stale_profile_dirs(entries, ttl_seconds=3600, now=_NOW) == []

    def test_non_sys_dir_never_selected(self) -> None:
        entries = [("Default", _NOW - 99999), ("cache", _NOW - 99999)]
        assert select_stale_profile_dirs(entries, ttl_seconds=3600, now=_NOW) == []

    def test_keep_excludes_active_profile(self) -> None:
        entries = [("sys_alpha", _NOW - 7200), ("sys_beta", _NOW - 7200)]
        got = select_stale_profile_dirs(entries, ttl_seconds=3600, now=_NOW, keep=["sys_beta"])
        assert got == ["sys_alpha"]

    def test_ttl_zero_disables_gc(self) -> None:
        entries = [("sys_alpha", _NOW - 99999)]
        assert select_stale_profile_dirs(entries, ttl_seconds=0, now=_NOW) == []

    def test_order_preserved_and_non_sys_skipped(self) -> None:
        entries = [("sys_b", _NOW - 7200), ("Default", _NOW - 7200), ("sys_a", _NOW - 7200)]
        assert select_stale_profile_dirs(entries, ttl_seconds=3600, now=_NOW) == ["sys_b", "sys_a"]


class TestGcProfileDirs:
    """Bounded IO sweep: gc_profile_dirs removes stale sys_* dirs under a base,
    best-effort, never touching the primary profile or non-sys_* entries. Real
    dirs via tmp_path; dry_run proves selection without deletion."""

    def _mk(self, base: Path, name: str, age_seconds: float, now: float) -> Path:
        d = base / name
        d.mkdir()
        (d / "marker.txt").write_text("x", encoding="utf-8")
        os.utime(d, (now - age_seconds, now - age_seconds))
        return d

    def test_removes_only_stale_sys_dirs(self, tmp_path: Path) -> None:
        now = time.time()
        old = self._mk(tmp_path, "sys_old", 7200, now)
        fresh = self._mk(tmp_path, "sys_new", 60, now)
        other = self._mk(tmp_path, "Default", 7200, now)
        removed = gc_profile_dirs(str(tmp_path), ttl_seconds=3600, now=now)
        assert removed == ["sys_old"]
        assert not old.exists()
        assert fresh.exists()
        assert other.exists()  # non-sys never touched even when old

    def test_dry_run_deletes_nothing(self, tmp_path: Path) -> None:
        now = time.time()
        old = self._mk(tmp_path, "sys_old", 7200, now)
        removed = gc_profile_dirs(str(tmp_path), ttl_seconds=3600, now=now, dry_run=True)
        assert removed == ["sys_old"]
        assert old.exists()

    def test_keep_protects_active(self, tmp_path: Path) -> None:
        now = time.time()
        a = self._mk(tmp_path, "sys_a", 7200, now)
        b = self._mk(tmp_path, "sys_b", 7200, now)
        removed = gc_profile_dirs(str(tmp_path), ttl_seconds=3600, now=now, keep=["sys_a"])
        assert removed == ["sys_b"]
        assert a.exists()
        assert not b.exists()

    def test_blank_or_missing_base_is_noop(self, tmp_path: Path) -> None:
        assert gc_profile_dirs("", ttl_seconds=3600) == []
        assert gc_profile_dirs(str(tmp_path / "does_not_exist"), ttl_seconds=3600) == []

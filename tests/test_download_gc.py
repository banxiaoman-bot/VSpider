"""Slice DL-GC1: stale ``.part`` / ``.meta`` garbage collection.

The resumable downloader (DL-RESUME1) leaves a stable ``.{key}.part`` + a
``.meta`` validator sidecar on disk when a download is interrupted, plus
``download.*.part`` mkstemp tempfiles that can leak on a crash. ``gc_stale_parts``
sweeps those by TTL so they do not accumulate (mission §一-B file governance),
while preserving any part still being actively appended.

Pure filesystem test (tmp dir + mtime control); no network / Playwright.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from visual_web_agent.media_harvester import gc_stale_parts


_OLD = 200_000.0  # ~55h, well past the 24h default TTL
_FRESH = 5.0


def _touch(path: Path, *, age_seconds: float = 0.0) -> Path:
    path.write_bytes(b"x")
    if age_seconds:
        t = time.time() - age_seconds
        os.utime(path, (t, t))
    return path


class TestGcStaleParts:
    def test_removes_stale_resumable_part_and_meta(self, tmp_path: Path) -> None:
        part = _touch(tmp_path / ".abc123.part", age_seconds=_OLD)
        meta = _touch(tmp_path / ".abc123.part.meta", age_seconds=_OLD)
        removed = gc_stale_parts(tmp_path)
        assert removed == 2
        assert not part.exists()
        assert not meta.exists()

    def test_keeps_fresh_part(self, tmp_path: Path) -> None:
        part = _touch(tmp_path / ".fresh.part", age_seconds=_FRESH)
        removed = gc_stale_parts(tmp_path)
        assert removed == 0
        assert part.exists()

    def test_removes_legacy_download_tempfile(self, tmp_path: Path) -> None:
        old = _touch(tmp_path / "download.xyz.part", age_seconds=_OLD)
        removed = gc_stale_parts(tmp_path)
        assert removed == 1
        assert not old.exists()

    def test_leaves_non_part_files_untouched(self, tmp_path: Path) -> None:
        art = _touch(tmp_path / "deadbeef.png", age_seconds=_OLD)  # real artifact
        part = _touch(tmp_path / ".old.part", age_seconds=_OLD)
        removed = gc_stale_parts(tmp_path)
        assert removed == 1
        assert art.exists()
        assert not part.exists()

    def test_ttl_boundary_with_now_injection(self, tmp_path: Path) -> None:
        p = _touch(tmp_path / ".b.part")  # mtime ~ now
        future = time.time() + 100_000
        removed = gc_stale_parts(tmp_path, ttl_seconds=3600, now=future)
        assert removed == 1
        assert not p.exists()

    def test_custom_ttl_keeps_within_window(self, tmp_path: Path) -> None:
        p = _touch(tmp_path / ".w.part", age_seconds=1800)  # 30 min old
        removed = gc_stale_parts(tmp_path, ttl_seconds=3600)  # 1h window
        assert removed == 0
        assert p.exists()

    def test_missing_dir_is_safe(self, tmp_path: Path) -> None:
        assert gc_stale_parts(tmp_path / "nope") == 0

    def test_mixed_stale_and_fresh_counts(self, tmp_path: Path) -> None:
        _touch(tmp_path / ".s1.part", age_seconds=_OLD)
        _touch(tmp_path / ".s1.part.meta", age_seconds=_OLD)
        fresh = _touch(tmp_path / ".f1.part", age_seconds=_FRESH)
        removed = gc_stale_parts(tmp_path)
        assert removed == 2
        assert fresh.exists()

    def test_accepts_str_path(self, tmp_path: Path) -> None:
        _touch(tmp_path / ".c.part", age_seconds=_OLD)
        removed = gc_stale_parts(str(tmp_path))
        assert removed == 1

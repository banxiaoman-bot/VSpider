"""Persistent-context profile-dir resolution for ``BrowserEnv`` launches.

Extracted from ``BrowserEnv.start`` so cross-system session isolation (E1c-3b)
can hand each system its own Chromium ``user_data_dir``: two persistent
contexts sharing one profile dir collide on ``launch_persistent_context``'s
single-instance lock. Kept pure -- no ``config`` import, no IO -- so the
selection logic is unit-testable without Playwright; callers pass the config
values in.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path


_PROFILE_PREFIX = "sys_"


def resolve_user_data_dir(
    override: str | None,
    *,
    default_base: str,
    packaged_fallback: Path,
) -> Path:
    """Pick the launch ``user_data_dir``: ``override`` > ``default_base`` > fallback.

    Blank / whitespace-only values are treated as absent, so an empty
    ``config.BROWSER_USER_DATA_DIR`` never yields ``Path('')``. The override is
    how a cross-system hop launches its target session under an isolated
    profile without contending for the primary lease's profile lock.
    """

    chosen = str(override or "").strip() or str(default_base or "").strip()
    if chosen:
        return Path(chosen)
    return Path(packaged_fallback)


def select_stale_profile_dirs(
    entries,
    *,
    ttl_seconds: float,
    now: float,
    keep=(),
) -> list[str]:
    """Pick the cross-system isolated profile dirs to garbage-collect (pure).

    ``entries`` is an iterable of ``(name, mtime_epoch)`` pairs (one per
    candidate sub-dir). A name is selected when it is a ``sys_*`` isolated
    profile (so the primary / global profile and any non-``sys_*`` entry are
    never eligible), its ``mtime`` is older than ``now - ttl_seconds``, and it
    is not in ``keep`` (the profiles a live run still needs). ``ttl_seconds <=
    0`` disables GC (returns ``[]``). Input order is preserved. No IO.
    """

    if not ttl_seconds or ttl_seconds <= 0:
        return []
    keep_set = {str(k) for k in keep}
    cutoff = now - ttl_seconds
    stale: list[str] = []
    for name, mtime in entries:
        candidate = str(name)
        if not candidate.startswith(_PROFILE_PREFIX) or candidate in keep_set:
            continue
        try:
            if float(mtime) < cutoff:
                stale.append(candidate)
        except (TypeError, ValueError):
            continue
    return stale


def gc_profile_dirs(
    base_dir,
    *,
    ttl_seconds: float,
    now: float | None = None,
    keep=(),
    dry_run: bool = False,
) -> list[str]:
    """Remove stale ``sys_*`` isolated profile dirs under ``base_dir`` (bounded IO).

    Only immediate sub-dirs named ``sys_*`` are ever considered, so the primary
    profile (``base_dir`` itself / its ``Default`` etc.) and any non-``sys_*``
    entry are never touched. Selection is delegated to the pure
    :func:`select_stale_profile_dirs`. Best-effort: a per-dir removal failure
    (Windows AV lock, perms) is swallowed so GC never aborts a run. Returns the
    dir names removed (or, with ``dry_run``, the ones that would be removed). A
    blank or non-existent ``base_dir`` is a no-op.
    """

    base = str(base_dir or "").strip()
    if not base:
        return []
    base_path = Path(base)
    if not base_path.is_dir():
        return []
    when = time.time() if now is None else now
    entries: list[tuple[str, float]] = []
    try:
        children = list(base_path.iterdir())
    except OSError:
        return []
    for child in children:
        try:
            if not child.is_dir():
                continue
            entries.append((child.name, child.stat().st_mtime))
        except OSError:
            continue
    stale = select_stale_profile_dirs(entries, ttl_seconds=ttl_seconds, now=when, keep=keep)
    removed: list[str] = []
    for name in stale:
        if dry_run:
            removed.append(name)
            continue
        try:
            shutil.rmtree(base_path / name)
            removed.append(name)
        except OSError:
            continue
    return removed


__all__ = ["resolve_user_data_dir", "select_stale_profile_dirs", "gc_profile_dirs"]

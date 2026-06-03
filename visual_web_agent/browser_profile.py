"""Persistent-context profile-dir resolution for ``BrowserEnv`` launches.

Extracted from ``BrowserEnv.start`` so cross-system session isolation (E1c-3b)
can hand each system its own Chromium ``user_data_dir``: two persistent
contexts sharing one profile dir collide on ``launch_persistent_context``'s
single-instance lock. Kept pure -- no ``config`` import, no IO -- so the
selection logic is unit-testable without Playwright; callers pass the config
values in.
"""

from __future__ import annotations

from pathlib import Path


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


__all__ = ["resolve_user_data_dir"]

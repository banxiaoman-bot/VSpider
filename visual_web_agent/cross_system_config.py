"""Single config entry for cross-system switching (mission §一-B cross_system).

Folds the cross-system feature's previously-inline environment parses into one
pure, side-effect-free module so every gate reads the *same* rule:

- ``VSPIDER_CROSS_SYSTEM_SWITCH``  -- the master on/off flag (was parsed inline
  in five places in ``main.py``). **Default ON since S11**: the M2 chain
  (input_contract systems -> graph auth_profiles -> session pre-acquire ->
  data-bus relay) is E2E-covered, so multi-system goals work with zero
  configuration; set the env var to ``0`` to opt out;
- ``VSPIDER_PROFILE_TTL_HOURS``    -- the profile-dir GC TTL (was an inline
  ``float(os.getenv(name, "24") or 24)`` parse with a 24h fallback);
- ``VSPIDER_SESSION_POOL_PER_RUN`` / ``VSPIDER_SESSION_POOL_TOTAL`` -- the
  session-pool caps (were ``_env_int`` clamped reads in
  ``browser_session_pool``).

Pure leaf module: imports only the stdlib, so ``browser_session_pool`` and
``main`` can import it without any cycle. Every accessor reads the environment
*live* (no caching) so the runtime read-timing matches the old inline parses
exactly; :class:`CrossSystemConfig` is offered for callers that want a single
immutable snapshot of all four knobs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


VERSION = "cross_system_config.v1"

# Env var names (single source of truth so call sites never typo a key).
CROSS_SYSTEM_SWITCH_ENV = "VSPIDER_CROSS_SYSTEM_SWITCH"
PROFILE_TTL_HOURS_ENV = "VSPIDER_PROFILE_TTL_HOURS"
SESSION_POOL_PER_RUN_ENV = "VSPIDER_SESSION_POOL_PER_RUN"
SESSION_POOL_TOTAL_ENV = "VSPIDER_SESSION_POOL_TOTAL"

# Defaults / clamps -- verbatim of the prior inline values.
DEFAULT_PROFILE_TTL_HOURS = 24.0
DEFAULT_POOL_PER_RUN = 4
DEFAULT_POOL_TOTAL = 16
POOL_PER_RUN_MIN, POOL_PER_RUN_MAX = 1, 16
POOL_TOTAL_MIN, POOL_TOTAL_MAX = 1, 64

_TRUTHY = ("1", "true", "yes", "on")


# ----- generic helpers (the de-dup'd inline parses) ----------------------


def env_flag(name: str, default: bool = False) -> bool:
    """True iff env ``name`` holds a truthy token (1/true/yes/on, any case).

    Exactly mirrors the previous inline parse
    ``os.getenv(name, "").strip().lower() in ("1","true","yes","on")``: an
    unset / blank / non-truthy value yields ``default`` (``False``), so an
    off flag stays off.
    """

    raw = os.getenv(name)
    if raw is None:
        return default
    stripped = str(raw).strip()
    if not stripped:
        return default
    return stripped.lower() in _TRUTHY


def env_int(name: str, default: int, *, min_value: int = 1, max_value: int = 64) -> int:
    """Clamped int env read -- a verbatim move of ``browser_session_pool._env_int``.

    Garbage / unset -> ``default``; the result is clamped into
    ``[min_value, max_value]``.
    """

    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def env_float(name: str, default: float) -> float:
    """Float env read with fallback -- mirrors ``float(os.getenv(name, str(default)) or default)``.

    A blank value falls back to ``default``; an explicit ``"0"`` is kept as
    ``0.0`` (downstream ttl<=0 disables GC); junk -> ``default``.
    """

    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return float(default)


# ----- named accessors (live env read, byte-identical to old call sites) --


def cross_system_enabled() -> bool:
    """Master gate: is cross-system switching turned on right now?

    S11: defaults to ON. The whole M2 chain (contract-declared systems ->
    graph auth_profiles -> pre-acquired sessions -> data bus relay) is
    flag-stable and E2E-covered, so a user goal that spans two systems must
    work with zero configuration (mission §简便). Set
    ``VSPIDER_CROSS_SYSTEM_SWITCH=0`` (or ``false``/``no``/``off``) to
    fall back to single-system behaviour.
    """

    return env_flag(CROSS_SYSTEM_SWITCH_ENV, default=True)


def profile_ttl_hours() -> float:
    """TTL (hours) for cross-system isolated profile-dir GC."""

    return env_float(PROFILE_TTL_HOURS_ENV, DEFAULT_PROFILE_TTL_HOURS)


def session_pool_caps() -> tuple[int, int]:
    """``(per_run, total)`` session-pool caps, each clamped to its range."""

    per_run = env_int(
        SESSION_POOL_PER_RUN_ENV,
        DEFAULT_POOL_PER_RUN,
        min_value=POOL_PER_RUN_MIN,
        max_value=POOL_PER_RUN_MAX,
    )
    total = env_int(
        SESSION_POOL_TOTAL_ENV,
        DEFAULT_POOL_TOTAL,
        min_value=POOL_TOTAL_MIN,
        max_value=POOL_TOTAL_MAX,
    )
    return per_run, total


# ----- one-shot snapshot --------------------------------------------------


@dataclass(frozen=True)
class CrossSystemConfig:
    """Immutable snapshot of the cross-system knobs (for callers wanting one object)."""

    enabled: bool = True
    profile_ttl_hours: float = DEFAULT_PROFILE_TTL_HOURS
    pool_per_run: int = DEFAULT_POOL_PER_RUN
    pool_total: int = DEFAULT_POOL_TOTAL

    @classmethod
    def from_env(cls) -> "CrossSystemConfig":
        per_run, total = session_pool_caps()
        return cls(
            enabled=cross_system_enabled(),
            profile_ttl_hours=profile_ttl_hours(),
            pool_per_run=per_run,
            pool_total=total,
        )


__all__ = [
    "VERSION",
    "CROSS_SYSTEM_SWITCH_ENV",
    "PROFILE_TTL_HOURS_ENV",
    "SESSION_POOL_PER_RUN_ENV",
    "SESSION_POOL_TOTAL_ENV",
    "env_flag",
    "env_int",
    "env_float",
    "cross_system_enabled",
    "profile_ttl_hours",
    "session_pool_caps",
    "CrossSystemConfig",
]

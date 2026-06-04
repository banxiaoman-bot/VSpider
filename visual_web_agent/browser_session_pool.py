"""Cross-system browser session pool (E1 foundation).

VSpider's existing :mod:`visual_web_agent.browser_pool` gives one
``BrowserLease`` per ``run_id`` — perfectly adequate when a goal lives on
a single host but cannot model "log in to system A, switch to system B,
come back" patterns that ``InputContract.has_cross_system`` already
detects upstream.

This module introduces :class:`BrowserSession`, a per-(run, system,
auth_profile) handle on top of a :class:`BrowserEnv`, and
:class:`BrowserSessionPool`, a registry that:

- keeps at most ``max_sessions_per_run`` browser sessions open per run
  (default 4) and ``max_total_sessions`` across the process (default 16),
- de-duplicates ``acquire_session(run_id, system_id, auth_profile)`` so
  asking for the same triple twice returns the same handle,
- knows how to release everything tied to a single run when the agent
  loop tears down a task,
- exposes a stable ``status()`` snapshot for the new
  ``GET /api/browser_sessions`` observability endpoint and for future
  Agent-loop wiring (E1b).

The pool is **pure infrastructure**: it does not yet rewrite the agent
loop in ``main.py`` (that is left for the E1b slice). The contract here
is deliberately the smallest viable surface that future code can call
without touching ``browser_pool``.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from .browser_env import BrowserEnv
except ImportError:  # pragma: no cover - script-style import fallback
    from browser_env import BrowserEnv  # type: ignore[no-redef]

try:
    from .cross_system_config import env_int, session_pool_caps
except ImportError:  # pragma: no cover - script-style import fallback
    from cross_system_config import env_int, session_pool_caps  # type: ignore[no-redef]


VERSION = "browser_session_pool.v1"


_VALID_SESSION_STATES = frozenset({"active", "idle", "released", "failed"})


def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int = 64) -> int:
    # Folded into cross_system_config.env_int (single source of truth); kept as a
    # thin alias so existing call sites / tests using this name stay valid.
    return env_int(name, default, min_value=min_value, max_value=max_value)


def _normalize_triple(run_id: str, system_id: str, auth_profile: str) -> tuple[str, str, str]:
    rid = str(run_id or "").strip()
    sid = str(system_id or "").strip() or "auto"
    profile = str(auth_profile or "").strip() or "auto"
    return rid, sid, profile


@dataclass
class BrowserSession:
    """One ``(run_id, system_id, auth_profile)`` slot in the session pool."""

    session_id: str
    run_id: str
    system_id: str
    auth_profile: str
    browser: BrowserEnv
    created_at: float
    last_used_at: float
    state: str = "active"
    released_at: float | None = None
    error: str = ""

    def touch(self) -> None:
        """Bump ``last_used_at`` whenever this session is handed back out."""

        self.last_used_at = time.time()

    def public(self) -> dict[str, Any]:
        """Lightweight serialisable snapshot for ``status()`` / API."""

        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "system_id": self.system_id,
            "auth_profile": self.auth_profile,
            "state": self.state if self.state in _VALID_SESSION_STATES else "active",
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "released_at": self.released_at,
            "age_s": round((self.released_at or time.time()) - self.created_at, 3),
            "idle_s": round(time.time() - self.last_used_at, 3) if self.released_at is None else None,
            "error": self.error,
        }


@dataclass
class BrowserSessionPool:
    """Per-run, per-system browser context registry.

    ``env_factory`` is parametrised so unit tests can inject a stub
    instead of spinning up real Playwright contexts.
    """

    max_sessions_per_run: int = 4
    max_total_sessions: int = 16
    env_factory: Callable[[], BrowserEnv] = BrowserEnv
    active: dict[str, BrowserSession] = field(default_factory=dict)
    by_run: dict[str, list[str]] = field(default_factory=dict)
    total_acquired: int = 0
    total_released: int = 0
    total_failed: int = 0
    last_error: str = ""

    # ----- internal helpers ----------------------------------------------

    def _find_existing(self, run_id: str, system_id: str, auth_profile: str) -> BrowserSession | None:
        for session_id in self.by_run.get(run_id, []):
            session = self.active.get(session_id)
            if session is None:
                continue
            if session.system_id == system_id and session.auth_profile == auth_profile:
                return session
        return None

    def _next_session_id(self, run_id: str, system_id: str) -> str:
        suffix = uuid.uuid4().hex[:6]
        rid = run_id or "run"
        sid = system_id or "sys"
        return f"session_{rid}_{sid}_{suffix}"

    # ----- public API ----------------------------------------------------

    def acquire_session(
        self,
        *,
        run_id: str,
        system_id: str,
        auth_profile: str = "auto",
    ) -> BrowserSession:
        """Get or create a browser session for the given triple.

        Returning the same handle for repeat ``(run_id, system_id,
        auth_profile)`` calls keeps the Agent loop from accidentally
        launching parallel contexts when stepping back into a system it
        already visited earlier in the run.
        """

        rid, sid, profile = _normalize_triple(run_id, system_id, auth_profile)
        if not rid:
            raise ValueError("run_id is required")

        existing = self._find_existing(rid, sid, profile)
        if existing is not None and existing.state in {"active", "idle"}:
            existing.state = "active"
            existing.touch()
            return existing

        run_sessions = self.by_run.get(rid, [])
        if len(run_sessions) >= self.max_sessions_per_run:
            self.last_error = (
                f"session pool per-run cap reached "
                f"(run_id={rid}, max={self.max_sessions_per_run})"
            )
            raise RuntimeError(self.last_error)
        if len(self.active) >= self.max_total_sessions:
            self.last_error = (
                f"session pool global cap reached "
                f"(max={self.max_total_sessions})"
            )
            raise RuntimeError(self.last_error)

        now = time.time()
        session = BrowserSession(
            session_id=self._next_session_id(rid, sid),
            run_id=rid,
            system_id=sid,
            auth_profile=profile,
            browser=self.env_factory(),
            created_at=now,
            last_used_at=now,
            state="active",
        )
        self.active[session.session_id] = session
        self.by_run.setdefault(rid, []).append(session.session_id)
        self.total_acquired += 1
        return session

    def get_active_session(
        self,
        run_id: str,
        system_id: str,
        *,
        auth_profile: str = "auto",
    ) -> BrowserSession | None:
        rid, sid, profile = _normalize_triple(run_id, system_id, auth_profile)
        if not rid:
            return None
        existing = self._find_existing(rid, sid, profile)
        if existing is None or existing.state not in {"active", "idle"}:
            return None
        return existing

    async def release_session(
        self,
        session_id: str,
        *,
        error: str = "",
    ) -> bool:
        """Close + drop one session. Returns True when something was released."""

        session = self.active.get(session_id)
        if session is None:
            return False
        err = str(error or "")
        try:
            await session.browser.close()
        except Exception as exc:
            err = err or f"{type(exc).__name__}: {exc}"
        session.released_at = time.time()
        session.error = err
        session.state = "failed" if err else "released"
        self.active.pop(session_id, None)
        run_sessions = self.by_run.get(session.run_id, [])
        if session_id in run_sessions:
            run_sessions.remove(session_id)
            if not run_sessions:
                self.by_run.pop(session.run_id, None)
        self.total_released += 1
        if err:
            self.total_failed += 1
            self.last_error = err
        return True

    async def release_run(
        self,
        run_id: str,
        *,
        error: str = "",
    ) -> list[str]:
        """Release every session attached to ``run_id``.

        Returns the list of ``session_id`` values released so callers can
        log / verify in tests.
        """

        rid = str(run_id or "").strip()
        if not rid:
            return []
        ids = list(self.by_run.get(rid, []))
        released: list[str] = []
        for session_id in ids:
            ok = await self.release_session(session_id, error=error)
            if ok:
                released.append(session_id)
        return released

    def status(self) -> dict[str, Any]:
        """Stable snapshot used by ``GET /api/browser_sessions`` (E1)."""

        by_system: dict[str, int] = {}
        per_run: dict[str, int] = {}
        for session in self.active.values():
            by_system[session.system_id] = by_system.get(session.system_id, 0) + 1
            per_run[session.run_id] = per_run.get(session.run_id, 0) + 1
        return {
            "version": VERSION,
            "max_sessions_per_run": int(self.max_sessions_per_run),
            "max_total_sessions": int(self.max_total_sessions),
            "active_count": len(self.active),
            "available_global": max(0, self.max_total_sessions - len(self.active)),
            "total_acquired": int(self.total_acquired),
            "total_released": int(self.total_released),
            "total_failed": int(self.total_failed),
            "last_error": self.last_error,
            "by_run": per_run,
            "by_system": by_system,
            "sessions": [session.public() for session in self.active.values()],
        }


# ---------------------------------------------------------------------------
# Module-level singleton (mirrors browser_pool's pattern so callers don't
# have to thread the pool object through every function signature).
# ---------------------------------------------------------------------------


def _resolve_default_pool() -> BrowserSessionPool:
    per_run, total = session_pool_caps()
    return BrowserSessionPool(
        max_sessions_per_run=per_run,
        max_total_sessions=total,
    )


_DEFAULT_SESSION_POOL = _resolve_default_pool()
_SESSION_LOCK = asyncio.Lock()


def acquire_session(
    *,
    run_id: str,
    system_id: str,
    auth_profile: str = "auto",
) -> BrowserSession:
    return _DEFAULT_SESSION_POOL.acquire_session(
        run_id=run_id,
        system_id=system_id,
        auth_profile=auth_profile,
    )


def get_active_session(
    run_id: str,
    system_id: str,
    *,
    auth_profile: str = "auto",
) -> BrowserSession | None:
    return _DEFAULT_SESSION_POOL.get_active_session(
        run_id,
        system_id,
        auth_profile=auth_profile,
    )


async def release_session(session_id: str, *, error: str = "") -> bool:
    async with _SESSION_LOCK:
        return await _DEFAULT_SESSION_POOL.release_session(session_id, error=error)


async def release_run_sessions(run_id: str, *, error: str = "") -> list[str]:
    async with _SESSION_LOCK:
        return await _DEFAULT_SESSION_POOL.release_run(run_id, error=error)


def get_browser_session_pool_status() -> dict[str, Any]:
    return _DEFAULT_SESSION_POOL.status()


def reset_default_session_pool_for_tests() -> None:
    """Test helper: rebuild the default pool with a fresh state.

    Production code should not call this; it exists so unit tests that
    exercise the module-level singleton don't bleed state across cases.
    """

    global _DEFAULT_SESSION_POOL
    _DEFAULT_SESSION_POOL = _resolve_default_pool()


__all__ = [
    "VERSION",
    "BrowserSession",
    "BrowserSessionPool",
    "acquire_session",
    "get_active_session",
    "release_session",
    "release_run_sessions",
    "get_browser_session_pool_status",
    "reset_default_session_pool_for_tests",
]

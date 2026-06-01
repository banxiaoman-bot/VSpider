"""Tests for ``visual_web_agent.browser_session_pool`` (E1 foundation).

These tests never spin up real Playwright contexts: ``BrowserSessionPool``
is parameterised with a stub ``env_factory`` that hands back lightweight
fake browser objects with the only ``close()`` method the pool needs.
"""

from __future__ import annotations

import asyncio

import pytest

from visual_web_agent.browser_session_pool import (
    VERSION,
    BrowserSession,
    BrowserSessionPool,
    acquire_session,
    get_active_session,
    get_browser_session_pool_status,
    release_run_sessions,
    release_session,
    reset_default_session_pool_for_tests,
)


# ---------------------------------------------------------------------------
# Stub browser env
# ---------------------------------------------------------------------------


class _StubBrowserEnv:
    def __init__(self) -> None:
        self.closed = False
        self.raise_on_close: Exception | None = None

    async def close(self) -> None:
        if self.raise_on_close is not None:
            raise self.raise_on_close
        self.closed = True


def _stub_factory() -> _StubBrowserEnv:
    return _StubBrowserEnv()


def _make_pool(
    *,
    per_run: int = 4,
    total: int = 16,
    factory=_stub_factory,
) -> BrowserSessionPool:
    return BrowserSessionPool(
        max_sessions_per_run=per_run,
        max_total_sessions=total,
        env_factory=factory,
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# BrowserSession.public()
# ---------------------------------------------------------------------------


class TestBrowserSessionDataclass:
    def test_public_serialisable(self) -> None:
        pool = _make_pool()
        session = pool.acquire_session(run_id="r1", system_id="system_1")
        snap = session.public()
        for key in (
            "session_id",
            "run_id",
            "system_id",
            "auth_profile",
            "state",
            "created_at",
            "last_used_at",
            "released_at",
            "age_s",
            "idle_s",
            "error",
        ):
            assert key in snap
        assert snap["run_id"] == "r1"
        assert snap["system_id"] == "system_1"
        assert snap["state"] == "active"
        assert snap["released_at"] is None
        assert snap["error"] == ""

    def test_touch_bumps_last_used(self) -> None:
        pool = _make_pool()
        session = pool.acquire_session(run_id="r1", system_id="system_1")
        first = session.last_used_at
        # Manually rewind to make the delta unambiguous.
        session.last_used_at = first - 5
        session.touch()
        assert session.last_used_at > first - 5


# ---------------------------------------------------------------------------
# acquire_session: de-dup, normalisation, validation
# ---------------------------------------------------------------------------


class TestAcquireSession:
    def test_blank_run_id_rejected(self) -> None:
        pool = _make_pool()
        with pytest.raises(ValueError):
            pool.acquire_session(run_id="", system_id="system_1")

    def test_same_triple_returns_same_handle(self) -> None:
        pool = _make_pool()
        a = pool.acquire_session(run_id="r1", system_id="system_1")
        b = pool.acquire_session(run_id="r1", system_id="system_1")
        assert a is b
        assert pool.total_acquired == 1
        assert len(pool.active) == 1

    def test_different_system_returns_new_handle(self) -> None:
        pool = _make_pool()
        a = pool.acquire_session(run_id="r1", system_id="system_1")
        b = pool.acquire_session(run_id="r1", system_id="system_2")
        assert a is not b
        assert pool.total_acquired == 2
        assert len(pool.active) == 2
        assert sorted(pool.by_run["r1"]) == sorted([a.session_id, b.session_id])

    def test_different_auth_profile_returns_new_handle(self) -> None:
        pool = _make_pool()
        a = pool.acquire_session(run_id="r1", system_id="system_1", auth_profile="default")
        b = pool.acquire_session(run_id="r1", system_id="system_1", auth_profile="logged_in")
        assert a is not b
        assert a.auth_profile == "default"
        assert b.auth_profile == "logged_in"

    def test_blank_auth_profile_normalises_to_auto(self) -> None:
        pool = _make_pool()
        session = pool.acquire_session(run_id="r1", system_id="system_1", auth_profile="   ")
        assert session.auth_profile == "auto"

    def test_blank_system_id_normalises_to_auto(self) -> None:
        pool = _make_pool()
        session = pool.acquire_session(run_id="r1", system_id="")
        assert session.system_id == "auto"

    def test_per_run_cap_enforced(self) -> None:
        pool = _make_pool(per_run=2)
        pool.acquire_session(run_id="r1", system_id="system_1")
        pool.acquire_session(run_id="r1", system_id="system_2")
        with pytest.raises(RuntimeError, match="per-run cap"):
            pool.acquire_session(run_id="r1", system_id="system_3")
        assert "per-run cap" in pool.last_error

    def test_global_cap_enforced(self) -> None:
        pool = _make_pool(per_run=10, total=2)
        pool.acquire_session(run_id="r1", system_id="system_1")
        pool.acquire_session(run_id="r2", system_id="system_1")
        with pytest.raises(RuntimeError, match="global cap"):
            pool.acquire_session(run_id="r3", system_id="system_1")
        assert "global cap" in pool.last_error

    def test_different_runs_are_independent(self) -> None:
        pool = _make_pool(per_run=1)
        a = pool.acquire_session(run_id="r1", system_id="system_1")
        b = pool.acquire_session(run_id="r2", system_id="system_1")
        assert a is not b
        assert a.run_id == "r1"
        assert b.run_id == "r2"


# ---------------------------------------------------------------------------
# get_active_session lookup
# ---------------------------------------------------------------------------


class TestGetActiveSession:
    def test_returns_known_session(self) -> None:
        pool = _make_pool()
        session = pool.acquire_session(run_id="r1", system_id="system_1")
        found = pool.get_active_session("r1", "system_1")
        assert found is session

    def test_missing_returns_none(self) -> None:
        pool = _make_pool()
        assert pool.get_active_session("r1", "system_1") is None

    def test_blank_run_id_returns_none(self) -> None:
        pool = _make_pool()
        pool.acquire_session(run_id="r1", system_id="system_1")
        assert pool.get_active_session("", "system_1") is None

    def test_after_release_returns_none(self) -> None:
        pool = _make_pool()
        session = pool.acquire_session(run_id="r1", system_id="system_1")
        _run(pool.release_session(session.session_id))
        assert pool.get_active_session("r1", "system_1") is None


# ---------------------------------------------------------------------------
# release_session / release_run
# ---------------------------------------------------------------------------


class TestReleaseSession:
    def test_release_closes_browser_and_drops_record(self) -> None:
        pool = _make_pool()
        session = pool.acquire_session(run_id="r1", system_id="system_1")
        ok = _run(pool.release_session(session.session_id))
        assert ok is True
        assert session.session_id not in pool.active
        assert session.state == "released"
        assert session.browser.closed is True
        assert session.released_at is not None
        assert pool.total_released == 1
        assert pool.total_failed == 0

    def test_release_missing_session_is_noop(self) -> None:
        pool = _make_pool()
        ok = _run(pool.release_session("nope"))
        assert ok is False
        assert pool.total_released == 0

    def test_browser_close_failure_marks_failed_state(self) -> None:
        pool = _make_pool()
        session = pool.acquire_session(run_id="r1", system_id="system_1")
        session.browser.raise_on_close = RuntimeError("boom")
        ok = _run(pool.release_session(session.session_id))
        assert ok is True
        assert session.state == "failed"
        assert "boom" in session.error
        assert pool.total_failed == 1
        assert "boom" in pool.last_error

    def test_release_with_explicit_error_overrides_state(self) -> None:
        pool = _make_pool()
        session = pool.acquire_session(run_id="r1", system_id="system_1")
        _run(pool.release_session(session.session_id, error="stopped_or_failed"))
        assert session.state == "failed"
        assert session.error == "stopped_or_failed"
        assert pool.total_failed == 1

    def test_release_removes_from_by_run_index(self) -> None:
        pool = _make_pool()
        a = pool.acquire_session(run_id="r1", system_id="system_1")
        b = pool.acquire_session(run_id="r1", system_id="system_2")
        _run(pool.release_session(a.session_id))
        remaining = pool.by_run.get("r1", [])
        assert a.session_id not in remaining
        assert b.session_id in remaining

    def test_release_empties_by_run_when_last_session_released(self) -> None:
        pool = _make_pool()
        session = pool.acquire_session(run_id="r1", system_id="system_1")
        _run(pool.release_session(session.session_id))
        assert "r1" not in pool.by_run


class TestReleaseRun:
    def test_release_run_drops_every_session(self) -> None:
        pool = _make_pool()
        a = pool.acquire_session(run_id="r1", system_id="system_1")
        b = pool.acquire_session(run_id="r1", system_id="system_2")
        released = _run(pool.release_run("r1"))
        assert set(released) == {a.session_id, b.session_id}
        assert len(pool.active) == 0
        assert "r1" not in pool.by_run

    def test_release_run_does_not_affect_other_runs(self) -> None:
        pool = _make_pool()
        a = pool.acquire_session(run_id="r1", system_id="system_1")
        b = pool.acquire_session(run_id="r2", system_id="system_1")
        _run(pool.release_run("r1"))
        assert a.session_id not in pool.active
        assert b.session_id in pool.active
        assert pool.by_run.get("r2") == [b.session_id]

    def test_release_unknown_run_returns_empty(self) -> None:
        pool = _make_pool()
        assert _run(pool.release_run("never_acquired")) == []

    def test_release_blank_run_returns_empty(self) -> None:
        pool = _make_pool()
        assert _run(pool.release_run("")) == []
        assert _run(pool.release_run("   ")) == []


# ---------------------------------------------------------------------------
# status snapshot
# ---------------------------------------------------------------------------


class TestStatusSnapshot:
    def test_empty_status(self) -> None:
        pool = _make_pool(per_run=3, total=7)
        snap = pool.status()
        assert snap["version"] == VERSION
        assert snap["max_sessions_per_run"] == 3
        assert snap["max_total_sessions"] == 7
        assert snap["active_count"] == 0
        assert snap["available_global"] == 7
        assert snap["total_acquired"] == 0
        assert snap["total_released"] == 0
        assert snap["total_failed"] == 0
        assert snap["last_error"] == ""
        assert snap["by_run"] == {}
        assert snap["by_system"] == {}
        assert snap["sessions"] == []

    def test_populated_status(self) -> None:
        pool = _make_pool(per_run=4, total=10)
        pool.acquire_session(run_id="r1", system_id="system_1")
        pool.acquire_session(run_id="r1", system_id="system_2")
        pool.acquire_session(run_id="r2", system_id="system_1")
        snap = pool.status()
        assert snap["active_count"] == 3
        assert snap["available_global"] == 7
        assert snap["total_acquired"] == 3
        assert snap["by_run"] == {"r1": 2, "r2": 1}
        assert snap["by_system"] == {"system_1": 2, "system_2": 1}
        assert len(snap["sessions"]) == 3
        assert all(item["state"] == "active" for item in snap["sessions"])


# ---------------------------------------------------------------------------
# Module-level singleton API
# ---------------------------------------------------------------------------


class TestModuleSingleton:
    def setup_method(self) -> None:
        reset_default_session_pool_for_tests()

    def teardown_method(self) -> None:
        # Pool may have leftover sessions if a test failed mid-way; clear.
        reset_default_session_pool_for_tests()

    def test_acquire_and_status_via_module_api(self, monkeypatch) -> None:
        # Inject our stub factory into the global pool by patching the
        # env factory in place.
        from visual_web_agent import browser_session_pool as bsp

        bsp._DEFAULT_SESSION_POOL.env_factory = _stub_factory
        session = acquire_session(run_id="r_singleton", system_id="system_1")
        assert isinstance(session, BrowserSession)
        snap = get_browser_session_pool_status()
        assert snap["active_count"] >= 1
        assert any(item["run_id"] == "r_singleton" for item in snap["sessions"])

    def test_release_run_sessions_via_module_api(self) -> None:
        from visual_web_agent import browser_session_pool as bsp

        bsp._DEFAULT_SESSION_POOL.env_factory = _stub_factory
        a = acquire_session(run_id="r_singleton_release", system_id="system_1")
        released = _run(release_run_sessions("r_singleton_release"))
        assert a.session_id in released
        assert get_active_session("r_singleton_release", "system_1") is None

    def test_release_session_singleton(self) -> None:
        from visual_web_agent import browser_session_pool as bsp

        bsp._DEFAULT_SESSION_POOL.env_factory = _stub_factory
        session = acquire_session(run_id="r_solo_release", system_id="system_1")
        ok = _run(release_session(session.session_id))
        assert ok is True
        assert get_active_session("r_solo_release", "system_1") is None


# ---------------------------------------------------------------------------
# Smoke: real default BrowserEnv factory should NOT be invoked during tests
# (sanity guard so we don't accidentally start Playwright in CI).
# ---------------------------------------------------------------------------


class TestNoRealBrowserAccidentallyStarted:
    def test_pool_only_uses_injected_factory(self) -> None:
        calls: list[int] = []

        def _factory() -> _StubBrowserEnv:
            calls.append(1)
            return _StubBrowserEnv()

        pool = _make_pool(factory=_factory)
        pool.acquire_session(run_id="r1", system_id="system_1")
        pool.acquire_session(run_id="r1", system_id="system_2")
        assert len(calls) == 2

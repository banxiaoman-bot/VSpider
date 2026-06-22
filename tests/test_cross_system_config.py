"""Tests for ``visual_web_agent.cross_system_config``.

The cross-system feature was gated by env parses scattered across two modules:
five inline ``os.getenv("VSPIDER_CROSS_SYSTEM_SWITCH","").strip().lower() in
("1","true","yes","on")`` truthy checks plus an inline TTL float-parse in
``main.py``, and two clamped ``_env_int`` pool-cap reads in
``browser_session_pool``. This module folds them into one pure loader; the tests
below pin the *exact* prior semantics so the fold stays byte-identical
(default-off path unchanged).
"""

import pytest

from visual_web_agent import cross_system_config as cfg


# --- env_flag: verbatim of the old inline truthy parse ---------------------


class TestEnvFlag:
    def test_unset_is_false(self, monkeypatch):
        monkeypatch.delenv("X_FLAG", raising=False)
        assert cfg.env_flag("X_FLAG") is False

    def test_blank_is_default(self, monkeypatch):
        monkeypatch.setenv("X_FLAG", "   ")
        assert cfg.env_flag("X_FLAG") is False
        assert cfg.env_flag("X_FLAG", True) is True

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", " on ", "On"])
    def test_truthy_tokens(self, monkeypatch, raw):
        monkeypatch.setenv("X_FLAG", raw)
        assert cfg.env_flag("X_FLAG") is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "maybe", "2"])
    def test_non_truthy_is_false(self, monkeypatch, raw):
        monkeypatch.setenv("X_FLAG", raw)
        assert cfg.env_flag("X_FLAG") is False


# --- env_int: verbatim of browser_session_pool._env_int (clamped) ----------


class TestEnvInt:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("X_INT", raising=False)
        assert cfg.env_int("X_INT", 4, min_value=1, max_value=16) == 4

    def test_clamped_to_max(self, monkeypatch):
        monkeypatch.setenv("X_INT", "999")
        assert cfg.env_int("X_INT", 4, min_value=1, max_value=16) == 16

    def test_clamped_to_min(self, monkeypatch):
        monkeypatch.setenv("X_INT", "-5")
        assert cfg.env_int("X_INT", 4, min_value=1, max_value=16) == 1

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("X_INT", "abc")
        assert cfg.env_int("X_INT", 7, min_value=1, max_value=16) == 7

    def test_in_range_passes_through(self, monkeypatch):
        monkeypatch.setenv("X_INT", "8")
        assert cfg.env_int("X_INT", 4, min_value=1, max_value=16) == 8


# --- env_float: verbatim of the inline TTL parse ---------------------------


class TestEnvFloat:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("X_F", raising=False)
        assert cfg.env_float("X_F", 24.0) == 24.0

    def test_override(self, monkeypatch):
        monkeypatch.setenv("X_F", "1.5")
        assert cfg.env_float("X_F", 24.0) == 1.5

    def test_zero_is_kept(self, monkeypatch):
        # ttl<=0 disables GC downstream; "0" must stay 0.0, not the default.
        monkeypatch.setenv("X_F", "0")
        assert cfg.env_float("X_F", 24.0) == 0.0

    def test_blank_returns_default(self, monkeypatch):
        monkeypatch.setenv("X_F", "")
        assert cfg.env_float("X_F", 24.0) == 24.0

    def test_garbage_returns_default(self, monkeypatch):
        monkeypatch.setenv("X_F", "nope")
        assert cfg.env_float("X_F", 24.0) == 24.0


# --- named accessors -------------------------------------------------------


class TestAccessors:
    def test_cross_system_enabled_by_default(self, monkeypatch):
        # S11: the M2 chain is stable, so multi-system goals work with zero
        # configuration; the env var is now an opt-out.
        monkeypatch.delenv("VSPIDER_CROSS_SYSTEM_SWITCH", raising=False)
        assert cfg.cross_system_enabled() is True

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "ON"])
    def test_cross_system_enabled_tokens(self, monkeypatch, raw):
        monkeypatch.setenv("VSPIDER_CROSS_SYSTEM_SWITCH", raw)
        assert cfg.cross_system_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "OFF"])
    def test_cross_system_opt_out_tokens(self, monkeypatch, raw):
        monkeypatch.setenv("VSPIDER_CROSS_SYSTEM_SWITCH", raw)
        assert cfg.cross_system_enabled() is False

    def test_profile_ttl_hours_default(self, monkeypatch):
        monkeypatch.delenv("VSPIDER_PROFILE_TTL_HOURS", raising=False)
        assert cfg.profile_ttl_hours() == 24.0

    def test_profile_ttl_hours_override(self, monkeypatch):
        monkeypatch.setenv("VSPIDER_PROFILE_TTL_HOURS", "0.5")
        assert cfg.profile_ttl_hours() == 0.5

    def test_session_pool_caps_default(self, monkeypatch):
        monkeypatch.delenv("VSPIDER_SESSION_POOL_PER_RUN", raising=False)
        monkeypatch.delenv("VSPIDER_SESSION_POOL_TOTAL", raising=False)
        assert cfg.session_pool_caps() == (4, 16)

    def test_session_pool_caps_clamped(self, monkeypatch):
        monkeypatch.setenv("VSPIDER_SESSION_POOL_PER_RUN", "999")
        monkeypatch.setenv("VSPIDER_SESSION_POOL_TOTAL", "999")
        assert cfg.session_pool_caps() == (16, 64)

    def test_session_pool_caps_floor(self, monkeypatch):
        monkeypatch.setenv("VSPIDER_SESSION_POOL_PER_RUN", "0")
        monkeypatch.setenv("VSPIDER_SESSION_POOL_TOTAL", "0")
        assert cfg.session_pool_caps() == (1, 1)


# --- CrossSystemConfig snapshot dataclass ----------------------------------


_ALL_ENV = (
    "VSPIDER_CROSS_SYSTEM_SWITCH",
    "VSPIDER_PROFILE_TTL_HOURS",
    "VSPIDER_SESSION_POOL_PER_RUN",
    "VSPIDER_SESSION_POOL_TOTAL",
)


class TestCrossSystemConfigSnapshot:
    def test_from_env_defaults(self, monkeypatch):
        for key in _ALL_ENV:
            monkeypatch.delenv(key, raising=False)
        snap = cfg.CrossSystemConfig.from_env()
        assert snap.enabled is True
        assert snap.profile_ttl_hours == 24.0
        assert snap.pool_per_run == 4
        assert snap.pool_total == 16

    def test_from_env_overrides(self, monkeypatch):
        monkeypatch.setenv("VSPIDER_CROSS_SYSTEM_SWITCH", "1")
        monkeypatch.setenv("VSPIDER_PROFILE_TTL_HOURS", "12")
        monkeypatch.setenv("VSPIDER_SESSION_POOL_PER_RUN", "2")
        monkeypatch.setenv("VSPIDER_SESSION_POOL_TOTAL", "8")
        snap = cfg.CrossSystemConfig.from_env()
        assert snap.enabled is True
        assert snap.profile_ttl_hours == 12.0
        assert snap.pool_per_run == 2
        assert snap.pool_total == 8

    def test_is_frozen(self):
        snap = cfg.CrossSystemConfig()
        with pytest.raises(Exception):
            snap.enabled = True  # type: ignore[misc]

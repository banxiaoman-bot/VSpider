"""G1: ``set_tab_notice`` / ``consume_tab_notice`` severity API.

Pure-Python tests against BrowserEnv (no Playwright runtime required).
"""

from __future__ import annotations

import pytest

from visual_web_agent.browser_env import BrowserEnv


@pytest.fixture
def env() -> BrowserEnv:
    """Construct BrowserEnv without invoking Playwright bootstrap."""
    # BrowserEnv.__init__ is pure-Python (no playwright launch),
    # only sets attributes. Safe to instantiate directly.
    return BrowserEnv()


class TestDefaults:
    def test_initial_state(self, env: BrowserEnv) -> None:
        assert env._tab_switch_notice is None
        assert env._last_notice_severity == "info"

    def test_consume_empty_returns_none_info(self, env: BrowserEnv) -> None:
        text, sev = env.consume_tab_notice()
        assert text is None
        assert sev == "info"


class TestSetNoticeBasics:
    def test_info_stamps_tag(self, env: BrowserEnv) -> None:
        env.set_tab_notice("click ok", severity="info")
        assert env._tab_switch_notice is not None
        assert "[INFO]" in env._tab_switch_notice
        assert "click ok" in env._tab_switch_notice
        assert env._last_notice_severity == "info"

    def test_warn_stamps_tag(self, env: BrowserEnv) -> None:
        env.set_tab_notice("候选不唯一", severity="warn")
        assert "[WARN]" in env._tab_switch_notice
        assert env._last_notice_severity == "warn"

    def test_error_stamps_tag(self, env: BrowserEnv) -> None:
        env.set_tab_notice("元素已消失", severity="error")
        assert "[ERROR]" in env._tab_switch_notice
        assert env._last_notice_severity == "error"

    def test_empty_text_is_noop(self, env: BrowserEnv) -> None:
        env.set_tab_notice("   ", severity="error")
        assert env._tab_switch_notice is None
        assert env._last_notice_severity == "info"

    def test_unknown_severity_falls_back_to_info(self, env: BrowserEnv) -> None:
        env.set_tab_notice("hi", severity="bogus")
        assert env._last_notice_severity == "info"
        assert "[INFO]" in env._tab_switch_notice


class TestPreStampedMarkers:
    """If the caller already brings a leading emoji or [TAG], no re-stamp."""

    @pytest.mark.parametrize(
        "lead",
        ["ℹ️", "⚠️", "🚨", "✅", "🛑", "[DRAG DONE]"],
    )
    def test_preserves_existing_marker(self, env: BrowserEnv, lead: str) -> None:
        msg = f"{lead} composed by handler"
        env.set_tab_notice(msg, severity="warn")
        # No double-stamp — caller's marker is preserved verbatim.
        assert env._tab_switch_notice == msg
        # Severity is still recorded for observability.
        assert env._last_notice_severity == "warn"


class TestCoalesceSemantics:
    def test_appends_with_blank_line(self, env: BrowserEnv) -> None:
        env.set_tab_notice("first", severity="info")
        env.set_tab_notice("second", severity="info", coalesce=True)
        assert env._tab_switch_notice is not None
        assert env._tab_switch_notice.count("\n\n") == 1
        assert "first" in env._tab_switch_notice
        assert "second" in env._tab_switch_notice

    def test_overwrite_replaces(self, env: BrowserEnv) -> None:
        env.set_tab_notice("old", severity="info")
        env.set_tab_notice("new", severity="error", coalesce=False)
        assert "old" not in env._tab_switch_notice
        assert "new" in env._tab_switch_notice
        assert env._last_notice_severity == "error"

    def test_severity_upgrades_on_coalesce(self, env: BrowserEnv) -> None:
        env.set_tab_notice("a", severity="info")
        env.set_tab_notice("b", severity="warn", coalesce=True)
        assert env._last_notice_severity == "warn"
        env.set_tab_notice("c", severity="error", coalesce=True)
        assert env._last_notice_severity == "error"

    def test_severity_does_not_downgrade_on_coalesce(self, env: BrowserEnv) -> None:
        env.set_tab_notice("a", severity="error")
        env.set_tab_notice("b", severity="info", coalesce=True)
        # error sticks even though we appended an info
        assert env._last_notice_severity == "error"

    def test_overwrite_does_downgrade(self, env: BrowserEnv) -> None:
        """coalesce=False is an explicit reset, so downgrade is intentional."""
        env.set_tab_notice("a", severity="error")
        env.set_tab_notice("b", severity="info", coalesce=False)
        assert env._last_notice_severity == "info"


class TestConsume:
    def test_consume_returns_and_clears(self, env: BrowserEnv) -> None:
        env.set_tab_notice("hello", severity="warn")
        text, sev = env.consume_tab_notice()
        assert text is not None
        assert "hello" in text
        assert sev == "warn"
        # Slot cleared, severity reset to default
        assert env._tab_switch_notice is None
        assert env._last_notice_severity == "info"

    def test_consume_twice_second_is_empty(self, env: BrowserEnv) -> None:
        env.set_tab_notice("once", severity="error")
        env.consume_tab_notice()
        text, sev = env.consume_tab_notice()
        assert text is None
        assert sev == "info"


class TestBackwardCompat:
    """Direct ``_tab_switch_notice = "..."`` still works for legacy callsites."""

    def test_direct_assignment_then_consume(self, env: BrowserEnv) -> None:
        env._tab_switch_notice = "legacy notice"
        # Severity was never set — defaults to "info"
        text, sev = env.consume_tab_notice()
        assert text == "legacy notice"
        assert sev == "info"

    def test_direct_assignment_then_set_appends(self, env: BrowserEnv) -> None:
        env._tab_switch_notice = "legacy"
        env.set_tab_notice("via helper", severity="warn", coalesce=True)
        assert "legacy" in env._tab_switch_notice
        assert "via helper" in env._tab_switch_notice
        # Severity for the slot now reflects the helper call
        assert env._last_notice_severity == "warn"

"""S3 regression tests: setup/handoff tools carved out of main.run_agent.

``SetupTools`` (visual_web_agent/phases/setup.py) hosts the 9 setup/handoff
helpers (browser action tool, targeted-probe handoffs, stop / stale-auth
guards, tool-metadata tagging, active-page recovery) as pure relocations of the
former run_agent closures; behaviour is checked here against stubs.
"""

import asyncio
import logging

import pytest

from visual_web_agent.phases.setup import SetupDeps, SetupTools


class _StubPage:
    def __init__(self, closed=False):
        self._closed = closed

    def is_closed(self):
        return self._closed


class _StubBrowser:
    def __init__(self, page=None, *, auth_stale=False, exec_result="EXEC"):
        self._page = page if page is not None else _StubPage()
        self.auth_stale_detected = auth_stale
        self.auth_stale_reason = "stale!" if auth_stale else ""
        self.current_url = "http://x"
        self._context = None
        self._exec_result = exec_result

    async def _ensure_active_page(self, reason=""):
        return self._page

    async def execute_action(self, payload, memory=None):
        return self._exec_result


class _Event:
    def __init__(self, val):
        self._v = val

    def is_set(self):
        return self._v


def _mk_setup(*, browser=None, stop_event=None, calls=None):
    calls = calls if calls is not None else {"log": [], "done": []}
    deps = SetupDeps(
        browser=browser if browser is not None else _StubBrowser(),
        goal="g",
        vlm=None,
        logger=logging.getLogger("test-setup"),
        event_stream=None,
        stop_event=stop_event,
        start_url="http://start",
        action_registry=None,
        broadcast_log_safe=lambda *a, **k: calls["log"].append((a, k)),
        broadcast_done_safe=lambda *a, **k: calls["done"].append((a, k)),
    )
    return SetupTools(deps), calls


def test_setup_tools_holds_deps():
    setup, _ = _mk_setup()
    assert setup.deps.goal == "g"
    assert setup.deps.start_url == "http://start"


def test_check_stop_raises_when_event_set():
    setup, _ = _mk_setup(stop_event=_Event(True))
    with pytest.raises(RuntimeError, match="STOP_REQUESTED"):
        setup.check_stop("ctx")


def test_check_stop_noop_when_not_set():
    setup, _ = _mk_setup(stop_event=_Event(False))
    assert setup.check_stop("ctx") is None
    setup2, _ = _mk_setup(stop_event=None)
    assert setup2.check_stop("ctx") is None


def test_abort_if_stale_auth_false_when_not_stale():
    setup, calls = _mk_setup(browser=_StubBrowser(auth_stale=False))
    assert setup.abort_if_stale_auth() is False
    assert calls["done"] == []


def test_abort_if_stale_auth_true_and_broadcasts():
    setup, calls = _mk_setup(browser=_StubBrowser(auth_stale=True))
    assert setup.abort_if_stale_auth() is True
    assert len(calls["done"]) == 1


def test_resolve_type_value_interpolates_memory():
    setup, _ = _mk_setup()
    value, display = setup.resolve_type_value_for_handoff(
        "{{name}}", workflow_memory={"name": "Tiger"}
    )
    assert value == "Tiger"
    assert display == "Tiger"


def test_resolve_type_value_passthrough_plain():
    setup, _ = _mk_setup()
    value, display = setup.resolve_type_value_for_handoff("hello")
    assert value == "hello"
    assert display == "hello"


def test_browser_action_tool_delegates():
    setup, _ = _mk_setup(browser=_StubBrowser(exec_result="PAGE"))
    assert asyncio.run(setup.browser_action_tool({"action": "x"})) == "PAGE"


def test_with_tool_metadata_none_passthrough():
    setup, _ = _mk_setup()
    assert setup.with_tool_metadata(None, []) is None


def test_recover_active_page_returns_active():
    page = _StubPage(closed=False)
    setup, _ = _mk_setup(browser=_StubBrowser(page=page))
    assert asyncio.run(setup.recover_active_page("t")) is page

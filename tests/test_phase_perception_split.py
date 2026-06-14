"""P1 regression: perception phase split out of main.py must behave identically.

Stub-browser unit tests for ``visual_web_agent.phases.perception.PerceptionPhase``:
the returned ``PerceptionSnapshot`` must carry the same fields the pre-split
main-loop block produced (url / title / screenshot_path / ax excerpt /
interactive_count), the ``observe`` event must be emitted once with the
assembled ``BrowserStateSnapshot``, and the "no active page" screenshot
recovery path must restart the browser and retry once.
"""

from __future__ import annotations

import asyncio

import pytest

from visual_web_agent.browser_state import BrowserStateSnapshot
from visual_web_agent.phases import perception as perception_mod
from visual_web_agent.phases.perception import PerceptionPhase, PerceptionSnapshot


class _StubPage:
    url = "https://alpha.example/list"

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return "Alpha List"

    async def evaluate(self, script):
        return "visible body text"


class _StubEventStream:
    def __init__(self) -> None:
        self.observe_calls: list[dict] = []

    def observe(self, **kwargs) -> None:
        self.observe_calls.append(kwargs)


class _StubBrowser:
    def __init__(self, *, screenshot_fail_first: bool = False, ax_fail: bool = False) -> None:
        self.current_url = "https://alpha.example/list"
        self._last_som_elements = [{"id": 1}, {"id": 2}, {"id": 3}]
        self._last_action_result = None
        self._page = _StubPage()
        self.restart_calls: list[tuple] = []
        self.screenshot_calls = 0
        self._screenshot_fail_first = screenshot_fail_first
        self._ax_fail = ax_fail

    async def _ensure_active_page(self, reason: str = ""):
        return self._page

    async def get_tabs_state(self) -> str:
        return "Tab 0: Alpha List (active)"

    async def get_active_page_summary(self) -> str:
        return "Alpha List — fixture page"

    async def mark_and_screenshot(self, step: int, scope="viewport"):
        self.screenshot_calls += 1
        self.last_scope = scope
        if self._screenshot_fail_first and self.screenshot_calls == 1:
            raise RuntimeError("No active page available for screenshot")
        return "b64-screenshot-data", "【可交互元素】@e1 button Submit"

    async def restart(self, start_url: str, reason: str = "") -> None:
        self.restart_calls.append((start_url, reason))

    async def extract_accessibility_tree(self) -> str:
        if self._ax_fail:
            raise RuntimeError("ax extraction broke")
        return '@e1 [button] "Submit" {enabled}\n@e2 [link] "Next"'

    async def reroute_proxy_on_block(self, url, **kwargs) -> bool:
        return False


async def _noop_recover(reason: str):
    return None


async def _noop_hitl(reason: str = "") -> None:
    return None


def _run_phase(browser: _StubBrowser, events: _StubEventStream, step: int = 3) -> PerceptionSnapshot:
    return asyncio.run(
        PerceptionPhase().run(
            browser,
            step=step,
            event_stream=events,
            start_url="https://alpha.example/list",
            recover_active_page=_noop_recover,
            wait_for_human_resume=_noop_hitl,
            bot_challenge_state=object(),
        )
    )


@pytest.fixture(autouse=True)
def _disable_a11y_enhancer(monkeypatch):
    # Keep the AX text byte-identical so excerpt assertions stay deterministic.
    monkeypatch.setattr(perception_mod, "A11Y_ENHANCER_ENABLED", False)


def test_snapshot_fields_match_pre_split_main_path():
    browser = _StubBrowser()
    events = _StubEventStream()
    snap = _run_phase(browser, events, step=3)

    assert isinstance(snap, PerceptionSnapshot)
    assert snap.screenshot_b64 == "b64-screenshot-data"
    assert snap.screenshot_path is not None and snap.screenshot_path.endswith("step_03.png")
    assert snap.reasoning_text_source == "AX_TREE"
    assert snap.tabs_state == "Tab 0: Alpha List (active)"
    assert snap.page_summary == "Alpha List — fixture page"
    assert '@e1 [button] "Submit"' in snap.ax_tree_text

    # input_descriptions = SoM text + AX block + page hint + tabs hint (same concat order)
    assert "【可交互元素】@e1 button Submit" in snap.input_descriptions
    assert "【辅助信息：页面无障碍语义树 (AX Tree)】" in snap.input_descriptions
    assert "【当前页面摘要】Alpha List — fixture page" in snap.input_descriptions
    assert "【当前标签页列表】Tab 0: Alpha List (active)" in snap.input_descriptions

    state = snap.browser_state
    assert isinstance(state, BrowserStateSnapshot)
    assert state.url == "https://alpha.example/list"
    assert state.title == "Alpha List"
    assert state.screenshot_path.endswith("step_03.png")
    assert '@e1 [button] "Submit"' in state.ax_tree_excerpt
    assert state.interactive_count == 3
    assert state.metadata.get("reasoning_text_source") == "AX_TREE"


def test_observe_event_emitted_once_with_browser_state():
    browser = _StubBrowser()
    events = _StubEventStream()
    snap = _run_phase(browser, events, step=3)

    assert len(events.observe_calls) == 1
    call = events.observe_calls[0]
    assert call["step"] == 3
    assert call["url"] == "https://alpha.example/list"
    assert call["screenshot_path"].endswith("step_03.png")
    assert call["ax_lines"] == 2
    assert call["browser_state"] is snap.browser_state
    assert call["metadata"]["reasoning_text_source"] == "AX_TREE"
    assert call["metadata"]["tabs"] == "Tab 0: Alpha List (active)"


def test_no_active_page_screenshot_restarts_and_retries_once():
    browser = _StubBrowser(screenshot_fail_first=True)
    events = _StubEventStream()
    snap = _run_phase(browser, events, step=3)

    assert browser.restart_calls == [("https://alpha.example/list", "retry hybrid screenshot")]
    assert browser.screenshot_calls == 2
    assert snap.screenshot_b64 == "b64-screenshot-data"


def test_ax_failure_falls_back_to_screenshot_only():
    browser = _StubBrowser(ax_fail=True)
    events = _StubEventStream()
    snap = _run_phase(browser, events, step=3)

    assert snap.reasoning_text_source == "SCREENSHOT_ONLY"
    assert snap.ax_tree_text == ""
    assert "【辅助信息：页面无障碍语义树 (AX Tree)】" not in snap.input_descriptions
    assert events.observe_calls[0]["ax_lines"] == 0
    assert events.observe_calls[0]["metadata"]["reasoning_text_source"] == "SCREENSHOT_ONLY"

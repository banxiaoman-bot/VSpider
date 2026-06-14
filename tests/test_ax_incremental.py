"""E3 AX incremental diff: only send added/removed AX lines to VLM prompt.

Locks the plan guarantees:
* First turn always renders full AX tree in the VLM prompt (no diff).
* Subsequent turn with changed AX shows diff format: unchanged count + added
  lines + removed lines.
* Subsequent turn with identical AX shows compact 'completely same' summary.
* E1 escape-valve forced-full turn gives full AX (resets diff baseline).
* E1 reuse turns don't shift the diff baseline.
* Empty AX produces no ax_block regardless of diff state.
* observe event metadata carries ``ax_incremental`` / ``ax_added`` /
  ``ax_removed`` / ``ax_unchanged`` counts.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from visual_web_agent.event_stream import EventStream
from visual_web_agent.phases import perception as perception_mod
from visual_web_agent.phases.perception import PerceptionPhase, PerceptionSnapshot


# ---------------------------------------------------------------------------
# AX fixtures
# ---------------------------------------------------------------------------

AX_TURN_1 = (
    '@e1 [button] "Submit" {enabled}\n'
    '@e2 [textbox] "Username" {focused}\n'
    '@e3 [link] "Help" {}'
)

AX_TURN_2 = (
    '@e1 [button] "Submit" {enabled}\n'
    '@e3 [link] "Help" {}\n'
    '@e4 [status] "Success" {}'
)

# Shared between TURN_1 and TURN_2: @e1, @e3  (2 lines)
# Added in TURN_2:   @e4                       (1 line)
# Removed in TURN_2: @e2                       (1 line)


# ---------------------------------------------------------------------------
# stubs
# ---------------------------------------------------------------------------


class _StubPage:
    url = "https://test.example/page"

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return "Test Page"

    async def evaluate(self, script):
        return "visible body text"


class _StubEventStream:
    def __init__(self) -> None:
        self.observe_calls: list[dict] = []

    def observe(self, **kwargs) -> None:
        self.observe_calls.append(kwargs)


class _StubBrowser:
    def __init__(self) -> None:
        self.current_url = "https://test.example/page"
        self._last_som_elements = [{"id": 1}]
        self._last_action_result = None
        self._page = _StubPage()
        self.signature = "sig-1"
        self.screenshot_calls = 0
        self.ax_calls = 0
        self.tabs_calls = 0
        self.ax_text = AX_TURN_1

    async def dom_signature(self) -> str:
        return self.signature

    async def _ensure_active_page(self, reason: str = ""):
        return self._page

    async def get_tabs_state(self) -> str:
        self.tabs_calls += 1
        return "Tab 0: Test Page (active)"

    async def get_active_page_summary(self) -> str:
        return "Test Page fixture"

    async def mark_and_screenshot(self, step: int, scope="viewport"):
        self.screenshot_calls += 1
        self.last_scope = scope
        return f"b64-shot-{self.screenshot_calls}", "@e1 button Submit"

    async def restart(self, start_url: str, reason: str = "") -> None:
        pass

    async def extract_accessibility_tree(self) -> str:
        self.ax_calls += 1
        return self.ax_text

    async def reroute_proxy_on_block(self, url, **kwargs) -> bool:
        return False


async def _noop_recover(reason: str):
    return None


async def _noop_hitl(reason: str = "") -> None:
    return None


def _run_turn(
    phase: PerceptionPhase,
    browser: _StubBrowser,
    events: _StubEventStream,
    step: int,
) -> PerceptionSnapshot:
    return asyncio.run(
        phase.run(
            browser,
            step=step,
            event_stream=events,
            start_url="https://test.example/page",
            recover_active_page=_noop_recover,
            wait_for_human_resume=_noop_hitl,
            bot_challenge_state=object(),
        )
    )


@pytest.fixture(autouse=True)
def _disable_a11y_enhancer(monkeypatch):
    monkeypatch.setattr(perception_mod, "A11Y_ENHANCER_ENABLED", False)


# ---------------------------------------------------------------------------
# E3 tests
# ---------------------------------------------------------------------------


def test_first_turn_full_ax():
    """First turn always renders full AX tree in VLM prompt."""
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    snap = _run_turn(phase, browser, events, step=1)

    assert snap.ax_tree_text == AX_TURN_1
    assert "【辅助信息：页面无障碍语义树 (AX Tree)】" in snap.input_descriptions
    assert "[AX 增量]" not in snap.input_descriptions

    meta = events.observe_calls[0].get("metadata", {})
    assert meta.get("ax_incremental") is False


def test_second_turn_changed_ax_gets_diff():
    """Subsequent turn with changed AX shows diff: unchanged + added + removed."""
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)

    browser.ax_text = AX_TURN_2
    browser.signature = "sig-2"
    snap2 = _run_turn(phase, browser, events, step=2)

    assert snap2.ax_tree_text == AX_TURN_2
    assert "[AX 增量]" in snap2.input_descriptions
    assert "2 行未变" in snap2.input_descriptions
    assert '@e4 [status] "Success" {}' in snap2.input_descriptions
    assert '@e2 [textbox] "Username" {focused}' in snap2.input_descriptions

    meta2 = events.observe_calls[1].get("metadata", {})
    assert meta2.get("ax_incremental") is True
    assert meta2.get("ax_added") == 1
    assert meta2.get("ax_removed") == 1
    assert meta2.get("ax_unchanged") == 2


def test_second_turn_same_ax_gets_unchanged_summary():
    """Subsequent turn with identical AX shows compact 'completely same' summary."""
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)

    browser.signature = "sig-2"
    snap2 = _run_turn(phase, browser, events, step=2)

    assert "AX Tree 未变" in snap2.input_descriptions
    assert "3 行与上一轮相同" in snap2.input_descriptions
    assert "+新增" not in snap2.input_descriptions
    assert "-消失" not in snap2.input_descriptions

    meta2 = events.observe_calls[1].get("metadata", {})
    assert meta2.get("ax_incremental") is True
    assert meta2.get("ax_added") == 0
    assert meta2.get("ax_removed") == 0


def test_escape_valve_gives_full_ax():
    """E1 escape-valve forced-full turn gives full AX (not diff)."""
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)
    for s in (2, 3, 4):
        _run_turn(phase, browser, events, step=s)  # reuse x3

    browser.ax_text = AX_TURN_2
    snap5 = _run_turn(phase, browser, events, step=5)

    assert "【辅助信息：页面无障碍语义树 (AX Tree)】" in snap5.input_descriptions
    assert "[AX 增量]" not in snap5.input_descriptions

    meta5 = events.observe_calls[4].get("metadata", {})
    assert meta5.get("ax_incremental") is False


def test_reuse_turns_preserve_diff_baseline():
    """E1 reuse turns don't shift the diff baseline."""
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)  # full, baseline = AX_TURN_1
    _run_turn(phase, browser, events, step=2)  # E1 reuse (same sig)

    browser.signature = "sig-new"
    browser.ax_text = AX_TURN_2
    snap3 = _run_turn(phase, browser, events, step=3)

    assert "[AX 增量]" in snap3.input_descriptions
    assert "2 行未变" in snap3.input_descriptions
    assert '@e4 [status] "Success" {}' in snap3.input_descriptions
    assert '@e2 [textbox] "Username" {focused}' in snap3.input_descriptions


def test_empty_ax_no_diff():
    """Empty AX tree produces no ax_block regardless of diff state."""
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    browser.ax_text = ""
    snap = _run_turn(phase, browser, events, step=1)

    assert "AX Tree" not in snap.input_descriptions
    assert "[AX 增量]" not in snap.input_descriptions


def test_diff_resets_baseline_for_next_turn():
    """After a diff turn, next diff is against the updated baseline."""
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)  # full, baseline = AX_TURN_1

    browser.ax_text = AX_TURN_2
    browser.signature = "sig-2"
    _run_turn(phase, browser, events, step=2)  # diff vs TURN_1, baseline → TURN_2

    browser.ax_text = AX_TURN_1
    browser.signature = "sig-3"
    snap3 = _run_turn(phase, browser, events, step=3)  # diff vs TURN_2

    assert "[AX 增量]" in snap3.input_descriptions
    # @e2 was absent in TURN_2 but back in TURN_1 → it's an "added" line now
    assert '@e2 [textbox] "Username" {focused}' in snap3.input_descriptions
    # @e4 was in TURN_2 but gone in TURN_1 → it's a "removed" line now
    assert '@e4 [status] "Success" {}' in snap3.input_descriptions


def test_observe_event_carries_ax_diff_metadata(tmp_path):
    """EventStream.observe passes through ax_incremental metadata fields."""
    stream = EventStream(run_id="e3_test", log_dir=tmp_path)
    stream.observe(
        step=1,
        url="https://x.example",
        metadata={
            "ax_incremental": True,
            "ax_unchanged": 10,
            "ax_added": 2,
            "ax_removed": 1,
        },
    )
    lines = [
        json.loads(line)
        for line in stream.path.read_text(encoding="utf-8").splitlines()
    ]
    meta = lines[0].get("metadata", {})
    assert meta.get("ax_incremental") is True
    assert meta.get("ax_unchanged") == 10
    assert meta.get("ax_added") == 2
    assert meta.get("ax_removed") == 1

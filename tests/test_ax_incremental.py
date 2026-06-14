"""E3 AX incremental diff: second-round AX output contains only added/removed
lines instead of the full tree, reducing observe event size and VLM token cost.

Plan guarantees tested here:
* first turn always gives full AX text (no diff);
* second turn with unchanged AX → ``[AX 无变化]`` one-liner;
* second turn with changed AX → incremental diff (added + removed only);
* E1 escape-valve forced-full round gives full AX (resets diff baseline);
* AX extraction failure (empty text) → no diff attempt;
* diff after reuse rounds is still computed against the last full extraction;
* ax_block prompt uses incremental header when diff is active.
"""

from __future__ import annotations

import asyncio

import pytest

from visual_web_agent.phases import perception as perception_mod
from visual_web_agent.phases.perception import PerceptionPhase, PerceptionSnapshot

# ---------------------------------------------------------------------------
# fixture AX trees
# ---------------------------------------------------------------------------

AX_TREE_V1 = "\n".join([
    '@e1 [button] "Submit" {enabled}',
    '@e2 [link] "Home"',
    '@e3 [textbox] "Username" {focused}',
    '@e4 [heading] "Login Page"',
    '@e5 [link] "About"',
    '@e6 [link] "Help"',
    '@e7 [link] "Contact"',
    '@e8 [link] "Privacy"',
    '@e9 [link] "Terms"',
    '@e10 [link] "FAQ"',
    '@e11 [link] "Blog"',
    '@e12 [link] "Status"',
])

AX_TREE_V2 = "\n".join([
    '@e1 [button] "Submit" {enabled}',
    '@e2 [link] "Home"',
    '@e5 [link] "About"',
    '@e6 [link] "Help"',
    '@e7 [link] "Contact"',
    '@e8 [link] "Privacy"',
    '@e9 [link] "Terms"',
    '@e10 [link] "FAQ"',
    '@e11 [link] "Blog"',
    '@e12 [link] "Status"',
    '@e15 [textbox] "Email" {focused}',
    '@e16 [link] "Register"',
])

# V1 → V2 diff:
#   unchanged: @e1, @e2, @e5-@e12 (10 lines)
#   added:     @e15, @e16          (2 lines)
#   removed:   @e3, @e4            (2 lines)


# ---------------------------------------------------------------------------
# stubs (mirror test_perception_reuse.py conventions)
# ---------------------------------------------------------------------------

class _StubPage:
    url = "https://demo.example/login"

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return "Demo Login"

    async def evaluate(self, script):
        return "visible body text"


class _StubEventStream:
    def __init__(self) -> None:
        self.observe_calls: list[dict] = []

    def observe(self, **kwargs) -> None:
        self.observe_calls.append(kwargs)


class _StubBrowser:
    """Browser stub whose ``extract_accessibility_tree`` returns successive
    AX trees from the ``ax_trees`` list."""

    def __init__(self, ax_trees: list[str]) -> None:
        self.current_url = "https://demo.example/login"
        self._last_som_elements = [{"id": 1}]
        self._last_action_result = None
        self._page = _StubPage()
        self.signature = "sig-ax"
        self.screenshot_calls = 0
        self.ax_calls = 0
        self.tabs_calls = 0
        self._ax_trees = ax_trees
        self._ax_index = 0

    async def dom_signature(self) -> str:
        return self.signature

    async def _ensure_active_page(self, reason: str = ""):
        return self._page

    async def get_tabs_state(self) -> str:
        self.tabs_calls += 1
        return "Tab 0: Demo Login (active)"

    async def get_active_page_summary(self) -> str:
        return "Demo Login — fixture"

    async def mark_and_screenshot(self, step: int, scope="viewport"):
        self.screenshot_calls += 1
        self.last_scope = scope
        return f"b64-{self.screenshot_calls}", "stub-input-desc"

    async def restart(self, start_url: str, reason: str = "") -> None:
        pass

    async def extract_accessibility_tree(self) -> str:
        self.ax_calls += 1
        if self._ax_index < len(self._ax_trees):
            text = self._ax_trees[self._ax_index]
            self._ax_index += 1
            return text
        return self._ax_trees[-1] if self._ax_trees else ""

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
            start_url="https://demo.example/login",
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


def test_first_turn_gives_full_ax_text():
    """First round always gives full AX text — no diff applied."""
    phase = PerceptionPhase()
    browser = _StubBrowser(ax_trees=[AX_TREE_V1])
    events = _StubEventStream()

    snap = _run_turn(phase, browser, events, step=1)

    assert AX_TREE_V1 in snap.ax_tree_text
    assert "[AX 增量]" not in snap.ax_tree_text
    assert "[AX 无变化]" not in snap.ax_tree_text


def test_second_turn_same_ax_gives_no_change_marker():
    """Two turns with identical AX → diff says 'no change'."""
    phase = PerceptionPhase()
    browser = _StubBrowser(ax_trees=[AX_TREE_V1, AX_TREE_V1])
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)
    # force different signature so perception is NOT reused (E1 skip)
    browser.signature = "sig-ax-2"
    snap2 = _run_turn(phase, browser, events, step=2)

    assert "[AX 无变化]" in snap2.ax_tree_text
    assert AX_TREE_V1 not in snap2.ax_tree_text


def test_second_turn_different_ax_gives_incremental_diff():
    """Two turns with changed AX → output contains only added/removed lines."""
    phase = PerceptionPhase()
    browser = _StubBrowser(ax_trees=[AX_TREE_V1, AX_TREE_V2])
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)
    browser.signature = "sig-ax-2"
    snap2 = _run_turn(phase, browser, events, step=2)

    ax = snap2.ax_tree_text
    assert "[AX 增量]" in ax
    assert "10 行不变" in ax

    # added lines present
    assert '@e15 [textbox] "Email" {focused}' in ax
    assert '@e16 [link] "Register"' in ax
    assert "新增" in ax

    # removed lines present
    assert '@e3 [textbox] "Username" {focused}' in ax
    assert '@e4 [heading] "Login Page"' in ax
    assert "已消失" in ax

    # unchanged lines NOT present (only diff output)
    assert '@e1 [button] "Submit" {enabled}' not in ax
    assert '@e2 [link] "Home"' not in ax

    # observe event size smaller than first turn
    first_ax_lines = events.observe_calls[0]["ax_lines"]
    second_ax_lines = events.observe_calls[1]["ax_lines"]
    assert second_ax_lines < first_ax_lines


def test_escape_valve_round_gives_full_ax():
    """E1 escape-valve forced-full round resets diff baseline → full AX."""
    phase = PerceptionPhase()
    browser = _StubBrowser(ax_trees=[AX_TREE_V1, AX_TREE_V2, AX_TREE_V2, AX_TREE_V2, AX_TREE_V2])
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)  # full (first round)
    # 3 reuse rounds (signature unchanged, action not mutating)
    for s in (2, 3, 4):
        _run_turn(phase, browser, events, step=s)  # reuse x3
    assert browser.ax_calls == 1  # only first round extracted AX

    # step 5: escape valve fires → forced full perception
    snap5 = _run_turn(phase, browser, events, step=5)
    assert browser.ax_calls == 2
    # forced full → full AX text, not incremental diff
    assert "[AX 增量]" not in snap5.ax_tree_text
    assert "[AX 无变化]" not in snap5.ax_tree_text
    assert AX_TREE_V2 in snap5.ax_tree_text


def test_ax_extraction_failure_no_diff():
    """AX extraction failure → empty text, no diff attempt."""
    phase = PerceptionPhase()
    browser = _StubBrowser(ax_trees=[AX_TREE_V1, ""])
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)
    browser.signature = "sig-ax-2"
    snap2 = _run_turn(phase, browser, events, step=2)

    assert snap2.ax_tree_text == ""
    assert "[AX 增量]" not in snap2.input_descriptions


def test_diff_after_reuse_rounds_uses_last_full_extraction():
    """After reuse rounds (E1 skip), diff is computed against the last full
    extraction, not the reused snapshot."""
    phase = PerceptionPhase()
    browser = _StubBrowser(ax_trees=[AX_TREE_V1, AX_TREE_V2])
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)  # full with V1
    _run_turn(phase, browser, events, step=2)  # reuse (same signature)
    assert browser.ax_calls == 1  # only first round extracted

    # now force full perception with different signature → V2
    browser.signature = "sig-ax-changed"
    snap3 = _run_turn(phase, browser, events, step=3)
    assert browser.ax_calls == 2

    # diff should be V1 → V2 (against the last full extraction, not reuse)
    ax = snap3.ax_tree_text
    assert "[AX 增量]" in ax
    assert "新增" in ax
    assert "已消失" in ax


def test_ax_block_prompt_uses_incremental_header():
    """When diff is active, the ax_block in input_descriptions uses the
    incremental header instead of the full AX header."""
    phase = PerceptionPhase()
    browser = _StubBrowser(ax_trees=[AX_TREE_V1, AX_TREE_V2])
    events = _StubEventStream()

    snap1 = _run_turn(phase, browser, events, step=1)
    assert "页面无障碍语义树" in snap1.input_descriptions

    browser.signature = "sig-ax-2"
    snap2 = _run_turn(phase, browser, events, step=2)
    assert "AX Tree 增量变化" in snap2.input_descriptions
    assert "页面无障碍语义树" not in snap2.input_descriptions

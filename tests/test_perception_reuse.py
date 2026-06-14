"""E1 perception reuse: skip screenshot/SoM when the DOM signature is unchanged.

Locks the three plan guarantees:
* two turns with the same ``dom_signature`` and a non-mutating last action →
  the second turn reuses the previous ``PerceptionSnapshot`` (no screenshot,
  no AX extraction) and the ``observe`` event carries ``perception_reused``;
* a last action with ``changed_dom=True`` (or ``changed_url=True``) forces a
  full perception even when the signature matches;
* the escape valve: after 3 consecutive reuses the 4th turn forces a full
  perception (signature-collision blindness guard).

Plus the ``BrowserEnv.dom_signature`` probe unit (sha1 of the page tuple,
quiet empty-string fallbacks) and the additive ``observe`` event field.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from visual_web_agent.event_stream import EventStream
from visual_web_agent.phases import perception as perception_mod
from visual_web_agent.phases.perception import PerceptionPhase, PerceptionSnapshot


# ---------------------------------------------------------------------------
# stubs
# ---------------------------------------------------------------------------


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
    def __init__(self) -> None:
        self.current_url = "https://alpha.example/list"
        self._last_som_elements = [{"id": 1}, {"id": 2}]
        self._last_action_result = None
        self._page = _StubPage()
        self.signature = "sig-stable"
        self.screenshot_calls = 0
        self.ax_calls = 0
        self.tabs_calls = 0

    async def dom_signature(self) -> str:
        return self.signature

    async def _ensure_active_page(self, reason: str = ""):
        return self._page

    async def get_tabs_state(self) -> str:
        self.tabs_calls += 1
        return "Tab 0: Alpha List (active)"

    async def get_active_page_summary(self) -> str:
        return "Alpha List — fixture page"

    async def mark_and_screenshot(self, step: int, scope="viewport"):
        self.screenshot_calls += 1
        self.last_scope = scope
        return f"b64-shot-{self.screenshot_calls}", "【可交互元素】@e1 button Submit"

    async def restart(self, start_url: str, reason: str = "") -> None:
        pass

    async def extract_accessibility_tree(self) -> str:
        self.ax_calls += 1
        return '@e1 [button] "Submit" {enabled}'

    async def reroute_proxy_on_block(self, url, **kwargs) -> bool:
        return False


class _ChangedAction:
    changed_url = False
    changed_dom = True


async def _noop_recover(reason: str):
    return None


async def _noop_hitl(reason: str = "") -> None:
    return None


def _run_turn(phase: PerceptionPhase, browser: _StubBrowser, events: _StubEventStream, step: int) -> PerceptionSnapshot:
    return asyncio.run(
        phase.run(
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
    monkeypatch.setattr(perception_mod, "A11Y_ENHANCER_ENABLED", False)


# ---------------------------------------------------------------------------
# reuse / skip
# ---------------------------------------------------------------------------


def test_same_signature_second_turn_skips_screenshot_and_som():
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    first = _run_turn(phase, browser, events, step=1)
    assert browser.screenshot_calls == 1
    assert first.browser_state.metadata.get("perception_reused") is not True

    second = _run_turn(phase, browser, events, step=2)
    # no second screenshot, no second AX extraction, no second tabs probe
    assert browser.screenshot_calls == 1
    assert browser.ax_calls == 1
    assert browser.tabs_calls == 1

    # reused snapshot carries the previous perception payload, marked reused
    assert second.screenshot_b64 == first.screenshot_b64
    assert second.input_descriptions == first.input_descriptions
    assert second.browser_state.metadata.get("perception_reused") is True
    assert second.browser_state.step == 2

    # observe events: first full, second reused
    assert events.observe_calls[0].get("perception_reused") is False
    assert events.observe_calls[1].get("perception_reused") is True
    assert len(events.observe_calls) == 2


def test_signature_change_forces_full_perception():
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)
    browser.signature = "sig-mutated"
    _run_turn(phase, browser, events, step=2)
    assert browser.screenshot_calls == 2
    assert events.observe_calls[1].get("perception_reused") is False


def test_changed_dom_action_forces_full_perception_despite_same_signature():
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)
    browser._last_action_result = _ChangedAction()
    _run_turn(phase, browser, events, step=2)
    assert browser.screenshot_calls == 2

    # dict-shaped action results are honoured too
    browser._last_action_result = {"changed_url": True, "changed_dom": False}
    _run_turn(phase, browser, events, step=3)
    assert browser.screenshot_calls == 3


def test_escape_valve_forces_full_perception_after_three_reuses():
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)  # full
    for step in (2, 3, 4):
        _run_turn(phase, browser, events, step=step)  # reuse x3
    assert browser.screenshot_calls == 1

    _run_turn(phase, browser, events, step=5)  # forced full
    assert browser.screenshot_calls == 2
    assert events.observe_calls[4].get("perception_reused") is False

    # streak reset: the next identical turn reuses again
    _run_turn(phase, browser, events, step=6)
    assert browser.screenshot_calls == 2
    assert events.observe_calls[5].get("perception_reused") is True


def test_browser_without_signature_probe_always_runs_full_perception():
    phase = PerceptionPhase()
    browser = _StubBrowser()
    del _StubBrowser.dom_signature  # type: ignore[attr-defined]
    try:
        events = _StubEventStream()
        _run_turn(phase, browser, events, step=1)
        _run_turn(phase, browser, events, step=2)
        assert browser.screenshot_calls == 2
    finally:
        _StubBrowser.dom_signature = _StubBrowser_dom_signature_backup  # type: ignore[attr-defined]


async def _StubBrowser_dom_signature_impl(self) -> str:
    return self.signature


_StubBrowser_dom_signature_backup = _StubBrowser_dom_signature_impl


# ---------------------------------------------------------------------------
# BrowserEnv.dom_signature probe
# ---------------------------------------------------------------------------


class _ProbePage:
    def __init__(self, raw="https://x.example|120|3456|18", fail=False) -> None:
        self._raw = raw
        self._fail = fail

    async def evaluate(self, script):
        if self._fail:
            raise RuntimeError("evaluate broke")
        return self._raw


class _ProbeSelf:
    def __init__(self, page) -> None:
        self._probe_page = page

    async def _ensure_active_page(self, reason: str = ""):
        return self._probe_page


def _call_dom_signature(stub_self):
    from visual_web_agent.browser_env import BrowserEnv

    return asyncio.run(BrowserEnv.dom_signature(stub_self))


def test_dom_signature_is_sha1_of_page_tuple():
    raw = "https://x.example|120|3456|18"
    sig = _call_dom_signature(_ProbeSelf(_ProbePage(raw=raw)))
    assert sig == hashlib.sha1(raw.encode("utf-8")).hexdigest()
    # deterministic
    assert sig == _call_dom_signature(_ProbeSelf(_ProbePage(raw=raw)))
    assert sig != _call_dom_signature(_ProbeSelf(_ProbePage(raw=raw + "|x")))


def test_dom_signature_quiet_fallbacks():
    assert _call_dom_signature(_ProbeSelf(None)) == ""
    assert _call_dom_signature(_ProbeSelf(_ProbePage(fail=True))) == ""


# ---------------------------------------------------------------------------
# event_stream observe contract (additive field)
# ---------------------------------------------------------------------------


def test_observe_event_carries_perception_reused_field(tmp_path):
    stream = EventStream(run_id="e1_test", log_dir=tmp_path)
    stream.observe(step=1, url="https://x.example", perception_reused=True)
    stream.observe(step=2, url="https://x.example")
    lines = [json.loads(line) for line in stream.path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["perception_reused"] is True
    assert lines[1]["perception_reused"] is False

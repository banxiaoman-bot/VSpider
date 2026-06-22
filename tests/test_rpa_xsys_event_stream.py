"""Stub-frame regression for session_switch evidence during RPA replay
(Slice RPA-XSYS-EVT).

RPA-XSYS's ``_replay_switch_system`` physically switches the active browser to a
step's planned system during fast-path replay, but it only *logged* the hop --
unlike the reactive-loop goto consumer, which emits ``session_switch`` +
``session_switch_activated`` into the run ``event_stream``. That left a
"留痕" gap (mission §三 human-like / §四 event_stream contract): a cross-system
hop that happened during replay produced no run evidence.

This slice threads an optional ``event_stream`` into ``_replay_switch_system``
and emits the same two events the reactive loop does, gated on ``event_stream``
being passed (``None`` -> no-op -> byte-identical when the flag is off).

No Playwright / network: deterministic fakes only, mirroring
``tests/test_rpa_cross_system_replay.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from visual_web_agent.main import _replay_switch_system


def _run(coro):
    return asyncio.run(coro)


class _StubEventStream:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, name: str, **payload: Any) -> None:
        self.events.append((name, dict(payload)))

    def names(self) -> list[str]:
        return [n for n, _ in self.events]

    def payload(self, name: str) -> dict:
        for n, p in self.events:
            if n == name:
                return p
        return {}


class _StubContext:
    async def storage_state(self) -> dict:
        return {"cookies": [], "origins": []}


class _StubPage:
    def __init__(self, url: str) -> None:
        self.url = url

    def is_closed(self) -> bool:
        return False


class _StubBrowser:
    def __init__(self, url: str) -> None:
        self.current_url = url
        self._context = _StubContext()
        self._page = _StubPage(url)


class _SwitchRouter:
    """Router whose acquire/activate force (or refuse) a cross-system rebind."""

    def __init__(self, *, target_browser, should_switch: bool = True) -> None:
        self._target = target_browser
        self._should_switch = should_switch

    def acquire_for_switch(self, *, to_system_id, from_system_id="", full_state=None):
        return {
            "should_switch": self._should_switch,
            "run_id": "r1",
            "from_system_id": from_system_id,
            "to_system_id": to_system_id,
            "to_system_name": to_system_id,
            "session_id": "sess-" + to_system_id,
        }

    async def activate_switch(self, switch, **kwargs):
        return self._target if switch.get("should_switch") else None

    def should_renavigate(self, current_url, target_url) -> bool:
        # Already there -> skip the landing goto so the test stays focused on
        # the emitted evidence rather than navigation.
        return False


class TestReplaySwitchEmitsEvidence:
    def test_cross_system_hop_emits_session_switch_events(self) -> None:
        home = _StubBrowser("https://alpha.com/home")
        target = _StubBrowser("https://beta.com/list")
        router = _SwitchRouter(target_browser=target)
        es = _StubEventStream()
        browser, _hb, _hs = _run(
            _replay_switch_system(
                home,
                router,
                to_system_id="system_2",
                from_system_id="system_1",
                url="https://beta.com/list",
                event_stream=es,
            )
        )
        assert browser is target
        assert "session_switch" in es.names()
        assert "session_switch_activated" in es.names()
        switch_evt = es.payload("session_switch")
        assert switch_evt.get("to_system_id") == "system_2"
        assert switch_evt.get("from_system_id") == "system_1"
        activated = es.payload("session_switch_activated")
        assert activated.get("via") == "rpa_replay"
        assert activated.get("to_system_id") == "system_2"
        assert activated.get("session_id") == "sess-system_2"

    def test_no_event_stream_is_noop(self) -> None:
        # event_stream omitted -> must not crash and still rebinds (the
        # byte-identical-when-off path the runtime hits with the flag set but
        # no stream wired in some call sites).
        home = _StubBrowser("https://alpha.com/home")
        target = _StubBrowser("https://beta.com/list")
        router = _SwitchRouter(target_browser=target)
        browser, _hb, _hs = _run(
            _replay_switch_system(
                home,
                router,
                to_system_id="system_2",
                from_system_id="system_1",
                url="https://beta.com/list",
            )
        )
        assert browser is target

    def test_no_switch_emits_nothing(self) -> None:
        home = _StubBrowser("https://alpha.com/home")
        target = _StubBrowser("https://beta.com/list")
        router = _SwitchRouter(target_browser=target, should_switch=False)
        es = _StubEventStream()
        browser, _hb, _hs = _run(
            _replay_switch_system(
                home,
                router,
                to_system_id="system_1",
                from_system_id="system_1",
                url="https://alpha.com/home",
                event_stream=es,
            )
        )
        assert browser is home
        assert es.events == []

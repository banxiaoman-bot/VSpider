"""Stub-frame regression for cross-system RPA replay (Slice RPA-XSYS).

Two halves, both gated on a ``session_router`` being present (the runtime only
attaches one when ``VSPIDER_CROSS_SYSTEM_SWITCH`` is on), so every assertion has
a router-absent twin proving byte-identical behaviour when the flag is off:

  A. Recording -- ``ActionContext.with_rpa_meta`` stamps the planned
     ``system_id`` of the page the step ran on, so the trail carries the
     system boundary into the cache.
  B. Compaction -- ``_compact_rpa_trail`` keeps two otherwise-identical actions
     separate when they ran in different systems (else a cross-system hop would
     be silently merged away).
  C. Replay -- ``_replay_rpa`` physically switches the active browser to a
     step's system (reusing the SessionRouter acquire->activate->rebind path)
     before replaying it; with no router it never switches.

No Playwright / network: deterministic fakes only, mirroring
``tests/test_rpa_replay_fetch_links.py``.
"""

from __future__ import annotations

import asyncio

from visual_web_agent.actions import ActionContext
from visual_web_agent.main import _compact_rpa_trail, _replay_rpa
from visual_web_agent.vlm_client import VSpiderAction


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────── A. recording stamp ───────────────────────────

class _StampRouter:
    def __init__(self, url_to_system: dict[str, str]) -> None:
        self._u2s = url_to_system

    def system_for_url(self, url: str) -> str:
        for needle, sysid in self._u2s.items():
            if needle in (url or ""):
                return sysid
        return ""


class _BrowserWithUrl:
    def __init__(self, url: str) -> None:
        self.rpa_trail: list = []
        self.current_url = url


def _make_ctx(url: str, *, session_router=None) -> ActionContext:
    action = VSpiderAction(action="click", target_id=1, type_value="", memory_key="")
    return ActionContext(
        action=action,
        browser=_BrowserWithUrl(url),
        workflow_memory={},
        page=None,
        session_router=session_router,
    )


def test_with_rpa_meta_stamps_system_id_when_router_present() -> None:
    router = _StampRouter({"b.example": "sysB"})
    ctx = _make_ctx("https://b.example/page", session_router=router)
    step = ctx.with_rpa_meta({"action": "click"})
    assert step["system_id"] == "sysB"


def test_with_rpa_meta_no_system_id_without_router() -> None:
    ctx = _make_ctx("https://b.example/page")  # flag off -> no router attached
    step = ctx.with_rpa_meta({"action": "click"})
    assert "system_id" not in step


def test_with_rpa_meta_no_system_id_when_url_unplanned() -> None:
    router = _StampRouter({"b.example": "sysB"})
    ctx = _make_ctx("https://unknown.example/", session_router=router)
    step = ctx.with_rpa_meta({"action": "click"})
    assert "system_id" not in step  # system_for_url -> "" => no stamp

    # caller override is never clobbered
    ctx2 = _make_ctx("https://b.example/", session_router=router)
    step2 = ctx2.with_rpa_meta({"action": "click", "system_id": "explicit"})
    assert step2["system_id"] == "explicit"


# ─────────────────────────── B. compaction ────────────────────────────────

def test_compact_keeps_same_action_across_systems() -> None:
    trail = [
        {"action": "click", "xpath": "//a", "system_id": "A"},
        {"action": "click", "xpath": "//a", "system_id": "B"},
    ]
    out = _compact_rpa_trail(trail)
    assert [s.get("system_id") for s in out] == ["A", "B"]


def test_compact_still_merges_identical_same_system() -> None:
    trail = [
        {"action": "click", "xpath": "//a", "system_id": "A"},
        {"action": "click", "xpath": "//a", "system_id": "A"},
    ]
    assert len(_compact_rpa_trail(trail)) == 1


# ─────────────────────────── C. replay switch ─────────────────────────────

class _FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self._closed = False

    def is_closed(self) -> bool:
        return self._closed


class _FakeContext:
    async def storage_state(self) -> dict:
        return {"cookies": [], "origins": []}


class _FakeBrowser:
    def __init__(self, system: str, url: str) -> None:
        self.system = system
        self.current_url = url
        self._page = _FakePage(url)
        self._context = _FakeContext()
        self._session_router = None
        self.stable_calls = 0

    async def _wait_for_page_stable(self) -> None:
        self.stable_calls += 1


class _FakeRouter:
    """Records the switch calls _replay_rpa makes; hands back the target browser."""

    def __init__(self, url_to_system: dict[str, str], targets: dict[str, _FakeBrowser]) -> None:
        self._u2s = url_to_system
        self._targets = targets
        self.acquire_calls: list[tuple[str, str]] = []
        self.activate_calls: list[dict] = []

    def system_for_url(self, url: str) -> str:
        for needle, sysid in self._u2s.items():
            if needle in (url or ""):
                return sysid
        return ""

    def acquire_for_switch(self, *, to_system_id, from_system_id, full_state=None):
        self.acquire_calls.append((from_system_id, to_system_id))
        should = bool(to_system_id) and to_system_id != from_system_id
        return {
            "should_switch": should,
            "run_id": "run",
            "from_system_id": from_system_id,
            "to_system_id": to_system_id,
            "to_system_name": to_system_id,
            "session_id": f"sess-{to_system_id}",
        }

    async def activate_switch(
        self,
        directive,
        *,
        url,
        user_data_dir_base="",
        full_state=None,
        home_system_id="",
        home_browser=None,
    ):
        self.activate_calls.append(dict(directive, url=url))
        target = directive.get("to_system_id")
        if home_system_id and target == home_system_id:
            return home_browser
        return self._targets.get(target)

    def should_renavigate(self, current_url, target_url) -> bool:
        return False


def test_replay_switches_browser_at_system_boundary() -> None:
    a = _FakeBrowser("A", "https://a.example/")
    b = _FakeBrowser("B", "https://b.example/")
    router = _FakeRouter(
        url_to_system={"a.example": "A", "b.example": "B"},
        targets={"B": b},
    )
    trail = [
        {"action": "remove_element", "system_id": "A"},
        {"action": "remove_element", "system_id": "B"},
    ]

    ok = _run(_replay_rpa(a, trail, {}, session_router=router))

    assert ok is True
    # exactly one A->B switch was requested + activated
    assert router.acquire_calls == [("A", "B")]
    assert len(router.activate_calls) == 1
    assert router.activate_calls[0]["to_system_id"] == "B"
    # the loop rebound onto B's browser and stamped its router back on
    assert b._session_router is router


def test_replay_no_switch_when_all_steps_same_system() -> None:
    a = _FakeBrowser("A", "https://a.example/")
    router = _FakeRouter(url_to_system={"a.example": "A"}, targets={})
    trail = [
        {"action": "remove_element", "system_id": "A"},
        {"action": "remove_element", "system_id": "A"},
    ]

    ok = _run(_replay_rpa(a, trail, {}, session_router=router))

    assert ok is True
    assert router.acquire_calls == []


def test_replay_without_router_never_switches() -> None:
    a = _FakeBrowser("A", "https://a.example/")
    trail = [
        {"action": "remove_element", "system_id": "A"},
        {"action": "remove_element", "system_id": "B"},
    ]

    # no session_router -> byte-identical to legacy single-context replay
    ok = _run(_replay_rpa(a, trail, {}))

    assert ok is True

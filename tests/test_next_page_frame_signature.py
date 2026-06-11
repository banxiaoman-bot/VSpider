"""DATA-SIG-FRAME-1: next_page sees iframe-hosted DataTables widgets.

Before this slice every next_page layer ran in the main document only:

  - ``_page_data_signature`` evaluated DATA_SIGNATURE_JS on the page, so the
    dt-info paging counter ("Showing 1 to 10 of 57 entries") of a table living
    inside an iframe was invisible - pagination_moved had no page-window
    evidence;
  - ``_wait_for_pagination_change`` returned False whenever the main-document
    url/text stayed put, which is exactly what an iframe-internal page flip
    looks like;
  - the L-1 / L3.5 JS pagination probes never looked inside child frames, so
    an iframe-hosted Next button was never clicked in the first place.

Uses deterministic stub page / frame objects (no Playwright, no network).
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from visual_web_agent import actions as actions_module
from visual_web_agent.actions import NextPageHandler


class _SigScope:
    """Stub scope: every evaluate() returns the same scripted payload."""

    def __init__(
        self,
        payload: Any,
        *,
        raises: bool = False,
        detached: bool = False,
        url: str = "",
    ) -> None:
        self.payload = payload
        self.raises = raises
        self._detached = detached
        self.url = url
        self.calls = 0

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.calls += 1
        if self.raises:
            raise RuntimeError("evaluate exploded")
        return self.payload

    def is_detached(self) -> bool:
        return self._detached


class _SigPage(_SigScope):
    def __init__(self, payload: Any, *, child_frames: list[Any] | None = None, **kw: Any) -> None:
        super().__init__(payload, **kw)
        self.main_frame = object()
        self.frames = [self.main_frame, *(child_frames or [])]

    async def wait_for_load_state(self, state: str = "load", timeout: int = 0) -> None:
        return None


_MAIN_WITH_TABLE = {
    "url": "https://host.example/",
    "bodyTextLength": 900,
    "scrollHeight": 2000,
    "tableInfo": "Showing 1 to 10 of 57 entries",
    "tablePageStart": 1,
    "tablePageEnd": 10,
    "tablePageTotal": 57,
    "tableRows": ["Tiger|Nixon"],
    "rowSignature": "mainsig",
}

_MAIN_NO_TABLE = {
    "url": "https://host.example/",
    "bodyTextLength": 300,
    "scrollHeight": 1200,
    "tableInfo": "",
    "tablePageStart": None,
    "tablePageEnd": None,
    "tablePageTotal": None,
    "tableRows": [],
    "rowSignature": "",
}

_FRAME_TABLE = {
    "url": "https://inner.example/table",
    "bodyTextLength": 700,
    "scrollHeight": 900,
    "tableInfo": "Showing 1 to 10 of 57 entries",
    "tablePageStart": 1,
    "tablePageEnd": 10,
    "tablePageTotal": 57,
    "tableRows": ["Garrett|Winters"],
    "rowSignature": "framesig",
}


def _data_signature(page: Any) -> dict:
    return asyncio.run(NextPageHandler()._page_data_signature(page))


class TestDataSignatureFrameSweep:
    def test_main_table_short_circuits_frame_sweep(self) -> None:
        frame = _SigScope(_FRAME_TABLE, url="https://inner.example/table")
        page = _SigPage(_MAIN_WITH_TABLE, child_frames=[frame])
        sig = _data_signature(page)
        assert sig["tableRows"] == ["Tiger|Nixon"]
        assert "tableFrameUrl" not in sig
        assert frame.calls == 0

    def test_frame_table_fields_are_merged(self) -> None:
        frame = _SigScope(_FRAME_TABLE, url="https://inner.example/table")
        page = _SigPage(dict(_MAIN_NO_TABLE), child_frames=[frame])
        sig = _data_signature(page)
        assert sig["url"] == "https://host.example/"  # main stays authoritative
        assert sig["tableInfo"] == "Showing 1 to 10 of 57 entries"
        assert (sig["tablePageStart"], sig["tablePageEnd"], sig["tablePageTotal"]) == (1, 10, 57)
        assert sig["tableRows"] == ["Garrett|Winters"]
        assert sig["rowSignature"] == "framesig"
        assert sig["tableFrameUrl"] == "https://inner.example/table"

    def test_raising_frame_is_skipped(self) -> None:
        bad = _SigScope({}, raises=True, url="https://bad.example/")
        good = _SigScope(_FRAME_TABLE, url="https://inner.example/table")
        page = _SigPage(dict(_MAIN_NO_TABLE), child_frames=[bad, good])
        sig = _data_signature(page)
        assert sig["tableFrameUrl"] == "https://inner.example/table"

    def test_detached_frame_is_not_probed(self) -> None:
        detached = _SigScope(_FRAME_TABLE, detached=True)
        page = _SigPage(dict(_MAIN_NO_TABLE), child_frames=[detached])
        sig = _data_signature(page)
        assert detached.calls == 0
        assert "tableFrameUrl" not in sig

    def test_no_table_anywhere_returns_main_signature(self) -> None:
        frame = _SigScope(dict(_MAIN_NO_TABLE, url="https://inner.example/"))
        page = _SigPage(dict(_MAIN_NO_TABLE), child_frames=[frame])
        sig = _data_signature(page)
        assert sig["tableRows"] == []
        assert "tableFrameUrl" not in sig


class TestWaitForPaginationChangeFrameData:
    @staticmethod
    def _run(before_data: dict, after_data: dict) -> bool:
        handler = NextPageHandler()
        page = _SigPage(dict(_MAIN_NO_TABLE))
        shell = ("https://host.example/", "text", 300, 1200)

        async def _same_shell(_page: Any) -> tuple:
            return shell

        async def _after_data(_page: Any) -> dict:
            return after_data

        handler._page_signature = _same_shell  # type: ignore[method-assign]
        handler._page_data_signature = _after_data  # type: ignore[method-assign]
        return asyncio.run(
            handler._wait_for_pagination_change(page, shell, "stub", before_data)
        )

    def test_iframe_page_window_move_confirms(self) -> None:
        before = dict(_MAIN_NO_TABLE, **{
            "tablePageStart": 1, "tablePageEnd": 10, "tablePageTotal": 57,
            "tableRows": ["a"], "rowSignature": "s1",
        })
        after = dict(before, tablePageStart=11, tablePageEnd=20, rowSignature="s2")
        assert self._run(before, after) is True

    def test_static_data_still_fails(self) -> None:
        before = dict(_MAIN_NO_TABLE, **{
            "tablePageStart": 1, "tablePageEnd": 10, "tablePageTotal": 57,
            "tableRows": ["a"], "rowSignature": "s1",
        })
        assert self._run(before, dict(before)) is False


class TestProbeFrameSweep:
    _FOUND = {"found": True, "strategy": "js_next_text", "label": "Next"}
    _MISS = {"found": False, "reason": "no candidate"}

    @staticmethod
    def _sweep(page: Any) -> tuple[dict, Any]:
        return asyncio.run(
            NextPageHandler()._js_mark_pagination_candidate_with_frames(page)
        )

    def test_main_hit_stops_sweep(self) -> None:
        frame = _SigScope(self._FOUND, url="https://inner.example/")
        page = _SigPage(self._FOUND, child_frames=[frame])
        probe, scope = self._sweep(page)
        assert scope is page
        assert probe.get("found") is True
        assert frame.calls == 0

    def test_frame_hit_returns_frame_scope(self) -> None:
        frame = _SigScope(self._FOUND, url="https://inner.example/table")
        page = _SigPage(self._MISS, child_frames=[frame])
        probe, scope = self._sweep(page)
        assert scope is frame
        assert probe["frame_url"] == "https://inner.example/table"

    def test_all_miss_returns_main_probe(self) -> None:
        frame = _SigScope(self._MISS, url="https://inner.example/")
        page = _SigPage(self._MISS, child_frames=[frame])
        probe, scope = self._sweep(page)
        assert scope is page
        assert probe.get("found") is False


class TestWiringAnchors:
    """Lock the L-1 / L3.5 call sites onto the frame-aware sweep."""

    def test_both_probe_layers_ride_the_sweep(self) -> None:
        src = inspect.getsource(actions_module.NextPageHandler)
        assert src.count("_js_mark_pagination_candidate_with_frames(page)") == 2
        assert src.count("probe_scope.locator('[data-vspider-next-page-probe=\"1\"]')") == 2

    def test_unchanged_shell_consults_data_signature(self) -> None:
        src = inspect.getsource(actions_module.NextPageHandler._wait_for_pagination_change)
        assert "main shell unchanged but data page moved" in src
        idx_data = src.index("main shell unchanged but data page moved")
        idx_fail = src.index("clicked but page did not change")
        assert idx_data < idx_fail

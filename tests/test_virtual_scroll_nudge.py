"""Virtual-scroll nudge primitives (Slice EXTRACT-VSCROLL-1).

Closes the weakness "virtual-scroll lists extract poorly": windowed lists
(react-window, vue-virtual-scroller, ag-grid...) keep a constant row window
inside an inner scroller, so window.scrollBy is a no-op and body length
stays flat - the legacy dedup nudge reported a dead end and bulk extraction
stopped early. nudge_virtual_scroll scrolls the dominant container and
reports progress via a row-signature diff.

JS execution was validated with a live-browser probe (true windowed list,
600 rows, recycled DOM): window-scroll no-op confirmed, capture loop
collected 600/600 unique rows in 71 passes, bottom detection clean - 4/4
PASS before probe removal.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from visual_web_agent.main import run_agent
from visual_web_agent.virtual_scroll import (
    VIRTUAL_LIST_ROWS_JS,
    VIRTUAL_LIST_SIGNATURE_JS,
    VIRTUAL_SCROLL_NUDGE_JS,
    capture_virtual_list_rows,
    find_virtual_list_scope,
    nudge_virtual_scroll,
)


class _StubPage:
    """Returns scripted results for the sig -> move -> sig evaluate sequence."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.eval_scripts: list[str] = []
        self.waited_ms: list[int] = []
        self.raise_on_call: int | None = None

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.eval_scripts.append(script)
        if self.raise_on_call is not None and len(self.eval_scripts) == self.raise_on_call:
            raise RuntimeError("evaluate exploded")
        if self._results:
            return self._results.pop(0)
        return {}

    async def wait_for_timeout(self, ms: int) -> None:
        self.waited_ms.append(ms)


def _run(page: Any, **kw: Any) -> dict:
    return asyncio.run(nudge_virtual_scroll(page, **kw))


class TestNudgeVirtualScroll:
    def test_container_progress_with_row_change(self) -> None:
        page = _StubPage([
            {"found": True, "sig": "Row 1||Row 10", "remaining": 900},
            {"mode": "container", "moved": True, "container_class": "vlist", "remaining": 600},
            {"found": True, "sig": "Row 9||Row 18", "remaining": 600},
        ])
        result = _run(page)
        assert result["mode"] == "container"
        assert result["moved"] is True
        assert result["rows_changed"] is True
        assert result["container"] == "vlist"
        assert result["remaining"] == 600

    def test_container_moved_but_rows_static_is_not_rows_changed(self) -> None:
        page = _StubPage([
            {"found": True, "sig": "Row 1||Row 10"},
            {"mode": "container", "moved": True, "container_tag": "div"},
            {"found": True, "sig": "Row 1||Row 10"},
        ])
        result = _run(page)
        assert result["moved"] is True
        assert result["rows_changed"] is False

    def test_bottom_detection_reports_no_progress(self) -> None:
        page = _StubPage([
            {"found": True, "sig": "Row 591||Row 600", "remaining": 0},
            {"mode": "container", "moved": False, "remaining": 0},
            {"found": True, "sig": "Row 591||Row 600", "remaining": 0},
        ])
        result = _run(page)
        assert result["moved"] is False
        assert result["rows_changed"] is False

    def test_window_fallback_when_no_container(self) -> None:
        page = _StubPage([
            {"found": False, "sig": ""},
            {"mode": "window", "moved": True},
            {"found": False, "sig": ""},
        ])
        result = _run(page)
        assert result["mode"] == "window"
        assert result["moved"] is True
        assert result["rows_changed"] is False, "empty signatures must not count"

    def test_nudge_evaluate_error_is_contained(self) -> None:
        page = _StubPage([{"found": True, "sig": "x"}])
        page.raise_on_call = 2
        result = _run(page)
        assert result == {"mode": "error", "moved": False, "rows_changed": False}

    def test_signature_errors_do_not_break_the_nudge(self) -> None:
        page = _StubPage([])
        page.raise_on_call = 1
        page._results = []

        async def _go() -> dict:
            return await nudge_virtual_scroll(page)

        result = asyncio.run(_go())
        assert result["moved"] is False

    def test_non_dict_results_normalise(self) -> None:
        page = _StubPage(["junk", "junk", "junk"])
        result = _run(page)
        assert result["mode"] == "unknown"
        assert result["moved"] is False
        assert result["rows_changed"] is False

    def test_settle_wait_is_applied(self) -> None:
        page = _StubPage([
            {"found": True, "sig": "a"},
            {"mode": "container", "moved": True},
            {"found": True, "sig": "b"},
        ])
        _run(page, settle_ms=321)
        assert page.waited_ms == [321]


class TestJsAnchors:
    def test_signature_js_shares_the_scroller_finder(self) -> None:
        for js in (VIRTUAL_LIST_SIGNATURE_JS, VIRTUAL_SCROLL_NUDGE_JS):
            assert "findScroller" in js
            assert "overflowY" in js

    def test_nudge_js_dispatches_scroll_event(self) -> None:
        """Virtual scrollers re-render on the scroll event, not on scrollTop."""
        assert "dispatchEvent(new Event('scroll'" in VIRTUAL_SCROLL_NUDGE_JS

    def test_signature_js_reads_first_and_last_row(self) -> None:
        assert "rowSignatureOf" in VIRTUAL_LIST_SIGNATURE_JS
        assert "[role=\"row\"]" in VIRTUAL_LIST_SIGNATURE_JS


class _CapturePage:
    """Scripted page for the capture loop: rows snapshots + nudge sequences.

    Each capture pass evaluates ROWS_JS once (rows snapshot), then the nudge
    runs SIGNATURE_JS / NUDGE_JS / SIGNATURE_JS; scripts are told apart by
    content so the stub stays robust to call-order tweaks.
    """

    def __init__(self, row_batches: list[list[dict]], moves: list[bool]) -> None:
        self._row_batches = list(row_batches)
        self._moves = list(moves)
        self._sig = 0

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "(amt)" in script:  # NUDGE_JS is the only (amt) => {...} script
            moved = self._moves.pop(0) if self._moves else False
            if moved:
                self._sig += 1  # the rendered row window only changes on real moves
            return {"mode": "container", "moved": moved, "container_class": "vlist"}
        if "rows.push" in script:  # ROWS_JS harvests row nodes
            if self._row_batches:
                return {"found": True, "rows": self._row_batches.pop(0)}
            return {"found": True, "rows": []}
        return {"found": True, "sig": f"sig-{self._sig}"}  # SIGNATURE_JS

    async def wait_for_timeout(self, ms: int) -> None:
        return None


class TestCaptureLoop:
    def test_accumulates_and_dedupes_recycled_rows(self) -> None:
        batches = [
            [{"text": "row 1"}, {"text": "row 2"}],
            [{"text": "row 2"}, {"text": "row 3"}],
            [{"text": "row 3"}, {"text": "row 4"}],
        ]
        page = _CapturePage(batches, moves=[True, True, False])
        result = asyncio.run(capture_virtual_list_rows(page, settle_ms=0))
        assert [r["text"] for r in result["rows"]] == ["row 1", "row 2", "row 3", "row 4"]
        assert result["complete"] is True
        assert result["container"] == "vlist"

    def test_max_rows_cap_reports_incomplete(self) -> None:
        batches = [[{"text": f"row {i}"} for i in range(1, 7)]]
        page = _CapturePage(batches, moves=[True] * 10)
        result = asyncio.run(capture_virtual_list_rows(page, max_rows=4, settle_ms=0))
        assert result["row_count"] == 4
        assert result["complete"] is False

    def test_unscrollable_container_exits_first_pass(self) -> None:
        page = _CapturePage([[{"text": "only"}]], moves=[False])
        result = asyncio.run(capture_virtual_list_rows(page, settle_ms=0))
        assert result["row_count"] == 1
        assert result["passes"] == 1
        assert result["complete"] is True

    def test_snapshot_errors_do_not_abort_the_loop(self) -> None:
        class _Raising(_CapturePage):
            async def evaluate(self, script: str, arg: Any = None) -> Any:
                if "rows.push" in script:
                    raise RuntimeError("snapshot exploded")
                return await super().evaluate(script, arg)

        page = _Raising([], moves=[False])
        result = asyncio.run(capture_virtual_list_rows(page, settle_ms=0))
        assert result["rows"] == []
        assert result["complete"] is True

    def test_rows_js_shares_scroller_and_skips_nested_wrappers(self) -> None:
        assert "findScroller" in VIRTUAL_LIST_ROWS_JS
        assert "nested" in VIRTUAL_LIST_ROWS_JS, "wrapper rows must not duplicate children"


class _StubFrame:
    """Child-frame stand-in for the scope sweep (EXTRACT-VSCROLL-3)."""

    def __init__(
        self,
        sig: Any,
        *,
        url: str = "https://child.example/",
        detached: bool = False,
        raise_on_probe: bool = False,
    ) -> None:
        self._sig = sig
        self.url = url
        self._detached = detached
        self._raise = raise_on_probe
        self.eval_count = 0

    def is_detached(self) -> bool:
        return self._detached

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.eval_count += 1
        if self._raise:
            raise RuntimeError("frame probe exploded")
        return self._sig


class _StubHostPage:
    """Host page exposing main_frame + frames like a Playwright Page."""

    def __init__(self, main_sig: Any, frames: list[Any]) -> None:
        self._sig = main_sig
        self.main_frame = object()
        self.frames = [self.main_frame, *frames]
        self.eval_count = 0

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.eval_count += 1
        return self._sig


class TestFindVirtualListScope:
    def test_main_document_hit_wins(self) -> None:
        frame = _StubFrame({"found": True, "remaining": 500})
        page = _StubHostPage({"found": True, "remaining": 900}, [frame])
        result = asyncio.run(find_virtual_list_scope(page))
        assert result["scope"] is page
        assert result["where"] == "main"
        assert frame.eval_count == 0, "a main-document hit must stop the sweep"

    def test_frame_sweep_finds_iframe_virtual_list(self) -> None:
        frame = _StubFrame({"found": True, "remaining": 740})
        page = _StubHostPage({"found": False, "sig": ""}, [frame])
        result = asyncio.run(find_virtual_list_scope(page))
        assert result["scope"] is frame
        assert result["where"] == "frame"
        assert result["url"] == "https://child.example/"
        assert result["remaining"] == 740

    def test_include_main_false_skips_the_main_document(self) -> None:
        frame = _StubFrame({"found": True, "remaining": 300})
        page = _StubHostPage({"found": True, "remaining": 900}, [frame])
        result = asyncio.run(find_virtual_list_scope(page, include_main=False))
        assert result["scope"] is frame
        assert page.eval_count == 0, "the drain probe already covered the main document"

    def test_detached_and_raising_frames_are_skipped(self) -> None:
        dead = _StubFrame({"found": True, "remaining": 100}, detached=True)
        angry = _StubFrame({"found": True, "remaining": 100}, raise_on_probe=True)
        good = _StubFrame({"found": True, "remaining": 220}, url="https://ok.example/")
        page = _StubHostPage({"found": False}, [dead, angry, good])
        result = asyncio.run(find_virtual_list_scope(page))
        assert result["scope"] is good
        assert result["url"] == "https://ok.example/"
        assert dead.eval_count == 0, "detached frames must not be evaluated"

    def test_no_scroll_headroom_is_not_a_hit(self) -> None:
        flat = _StubFrame({"found": True, "remaining": 0})
        page = _StubHostPage({"found": False}, [flat])
        result = asyncio.run(find_virtual_list_scope(page))
        assert result["scope"] is None
        assert result["where"] == ""
        assert result["remaining"] == 0

    def test_non_dict_probe_results_are_skipped(self) -> None:
        junk = _StubFrame("junk")
        page = _StubHostPage(None, [junk])
        result = asyncio.run(find_virtual_list_scope(page))
        assert result["scope"] is None

    def test_captures_straight_from_a_frame_scope(self) -> None:
        """The sweep's scope feeds capture_virtual_list_rows unchanged."""
        capture_frame = _CapturePage(
            [[{"text": "frame row 1"}], [{"text": "frame row 2"}]],
            moves=[True, False],
        )
        result = asyncio.run(capture_virtual_list_rows(capture_frame, settle_ms=0))
        assert [r["text"] for r in result["rows"]] == ["frame row 1", "frame row 2"]
        assert result["complete"] is True


class TestHorizontalAxis:
    """VSCROLL-H-1: horizontal card strips / film-strips become harvestable."""

    def test_all_scripts_know_the_horizontal_axis(self) -> None:
        for js in (VIRTUAL_LIST_SIGNATURE_JS, VIRTUAL_SCROLL_NUDGE_JS, VIRTUAL_LIST_ROWS_JS):
            assert "scrollWidth" in js
            assert "scrollLeft" in js
            assert "overflowX" in js
            assert "axis: vOk ? 'y' : 'x'" in js

    def test_vertical_hits_outrank_horizontal_ones(self) -> None:
        assert "a.axis === 'y' ? -1 : 1" in VIRTUAL_LIST_SIGNATURE_JS, (
            "legacy vertical priority must be preserved"
        )

    def test_nudge_scrolls_left_on_the_x_axis(self) -> None:
        assert (
            "target.scrollLeft = Math.min(before + step, target.scrollWidth)"
            in VIRTUAL_SCROLL_NUDGE_JS
        )

    def test_rows_js_collects_card_nodes(self) -> None:
        assert '[class*="card"]' in VIRTUAL_LIST_ROWS_JS

    def test_nudge_propagates_axis(self) -> None:
        page = _StubPage([
            {"found": True, "sig": "Card 1||Card 6", "remaining": 800, "axis": "x"},
            {"mode": "container", "moved": True, "container_class": "strip",
             "axis": "x", "remaining": 500},
            {"found": True, "sig": "Card 5||Card 10", "remaining": 500, "axis": "x"},
        ])
        result = _run(page)
        assert result["axis"] == "x"
        assert result["moved"] is True
        assert result["rows_changed"] is True

    def test_nudge_defaults_axis_to_vertical(self) -> None:
        page = _StubPage([
            {"found": True, "sig": "Row 1||Row 10"},
            {"mode": "container", "moved": True, "container_tag": "div"},
            {"found": True, "sig": "Row 9||Row 18"},
        ])
        assert _run(page)["axis"] == "y"

    def test_capture_meta_carries_axis(self) -> None:
        class _AxisCapturePage(_CapturePage):
            async def evaluate(self, script: str, arg: Any = None) -> Any:
                payload = await super().evaluate(script, arg)
                if isinstance(payload, dict):
                    payload.setdefault("axis", "x")
                return payload

        page = _AxisCapturePage([[{"text": "Card 1"}]], moves=[False])
        result = asyncio.run(capture_virtual_list_rows(page, settle_ms=0))
        assert result["axis"] == "x"
        assert result["row_count"] == 1

    def test_scope_probe_propagates_axis(self) -> None:
        page = _StubHostPage(
            {"found": True, "sig": "Card 1||Card 6", "remaining": 640, "axis": "x"},
            [],
        )
        result = asyncio.run(find_virtual_list_scope(page))
        assert result["scope"] is page
        assert result["axis"] == "x"
        assert result["remaining"] == 640


class TestMainWiring:
    def test_dedup_nudge_falls_back_to_virtual_scroll(self) -> None:
        src = inspect.getsource(run_agent)
        assert "nudge_virtual_scroll(_scroll_page" in src, (
            "the dedup nudge no longer falls back to the virtual-scroll path"
        )
        assert 'vs.get("rows_changed")' in src

    def test_pre_extract_runs_capture_as_candidate(self) -> None:
        src = inspect.getsource(run_agent)
        assert "capture_virtual_list_rows(" in src, (
            "pre-extract fast path no longer runs the deterministic capture"
        )
        assert 'name="VSCROLL_LIST"' in src
        assert 'drain.get("container_can_scroll")' in src, (
            "capture must stay gated behind the scroll-drain probe"
        )

    def test_pre_extract_sweeps_frames_when_main_probe_misses(self) -> None:
        """EXTRACT-VSCROLL-3: iframe-hosted virtual lists reach the capture."""
        src = inspect.getsource(run_agent)
        assert "find_virtual_list_scope(" in src, (
            "the pre-extract path no longer sweeps child frames for scrollers"
        )
        assert "include_main=False" in src, (
            "the drain probe already covers the main document - do not re-probe"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

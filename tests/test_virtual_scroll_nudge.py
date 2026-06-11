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
    VIRTUAL_LIST_SIGNATURE_JS,
    VIRTUAL_SCROLL_NUDGE_JS,
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


class TestMainWiring:
    def test_dedup_nudge_falls_back_to_virtual_scroll(self) -> None:
        src = inspect.getsource(run_agent)
        assert "nudge_virtual_scroll(_scroll_page" in src, (
            "the dedup nudge no longer falls back to the virtual-scroll path"
        )
        assert 'vs.get("rows_changed")' in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

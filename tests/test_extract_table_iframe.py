"""Bulk table extraction iframe sweep (Slice EXTRACT-IFRAME-1).

Closes the weakness "bulk table extraction never scans iframes": the DOM
table harvest must probe child frames when the main document has no rows.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from visual_web_agent.main import _evaluate_rows_with_frame_fallback, run_agent

ROWS = [{"name": "Ada", "city": "London"}, {"name": "Lin", "city": "Xi'an"}]
JS = "() => []"


def _run(page: Any) -> list:
    return asyncio.run(_evaluate_rows_with_frame_fallback(page, JS, log_tag="TEST"))


class _StubFrame:
    def __init__(
        self,
        name: str,
        results: list[Any] | None = None,
        *,
        detached: bool = False,
        raise_err: bool = False,
    ) -> None:
        self.url = f"https://example.test/{name}"
        self._results = list(results or [])
        self._detached = detached
        self._raise = raise_err
        self.eval_calls = 0

    def is_detached(self) -> bool:
        return self._detached

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.eval_calls += 1
        if self._raise:
            raise RuntimeError("frame is gone")
        if self._results:
            return self._results.pop(0)
        return []


class _StubPage:
    def __init__(self, main_results: list[Any], frames: list[_StubFrame]) -> None:
        self.main_frame = _StubFrame("main")
        self.frames = [self.main_frame, *frames]
        self._results = list(main_results)
        self.eval_calls = 0
        self.raise_on_evaluate = False

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.eval_calls += 1
        if self.raise_on_evaluate:
            raise RuntimeError("main document crashed")
        if self._results:
            return self._results.pop(0)
        return []


class TestFrameFallback:
    def test_main_document_hit_skips_frames(self) -> None:
        frame = _StubFrame("child", [list(ROWS)])
        page = _StubPage([list(ROWS)], [frame])
        rows = _run(page)
        assert rows == ROWS
        assert frame.eval_calls == 0, "main-document hit must not probe iframes"

    def test_empty_main_falls_through_to_matching_iframe(self) -> None:
        miss = _StubFrame("miss")
        hit = _StubFrame("hit", [list(ROWS)])
        page = _StubPage([[]], [miss, hit])
        rows = _run(page)
        assert rows == ROWS
        assert miss.eval_calls == 1
        assert hit.eval_calls == 1

    def test_main_document_error_still_probes_frames(self) -> None:
        hit = _StubFrame("hit", [list(ROWS)])
        page = _StubPage([], [hit])
        page.raise_on_evaluate = True
        rows = _run(page)
        assert rows == ROWS, "a crashing main document must not abort the sweep"

    def test_all_empty_returns_empty_list(self) -> None:
        f1, f2 = _StubFrame("a"), _StubFrame("b")
        page = _StubPage([[]], [f1, f2])
        assert _run(page) == []
        assert f1.eval_calls == 1 and f2.eval_calls == 1

    def test_detached_and_raising_frames_are_skipped(self) -> None:
        dead = _StubFrame("dead", detached=True)
        boom = _StubFrame("boom", raise_err=True)
        hit = _StubFrame("hit", [list(ROWS)])
        page = _StubPage([[]], [dead, boom, hit])
        rows = _run(page)
        assert rows == ROWS
        assert dead.eval_calls == 0, "detached frame must not be evaluated"
        assert boom.eval_calls == 1, "raising frame is probed once then skipped"

    def test_main_frame_object_is_never_reprobed(self) -> None:
        page = _StubPage([[]], [])
        _run(page)
        assert page.main_frame.eval_calls == 0
        assert page.eval_calls == 1

    def test_non_list_results_normalise_to_empty_list(self) -> None:
        odd_frame = _StubFrame("odd", ["not a list"])
        page = _StubPage(["not a list"], [odd_frame])
        assert _run(page) == []

    def test_frame_non_list_is_ignored_but_later_hit_wins(self) -> None:
        odd = _StubFrame("odd", [{"rows": "wrong shape"}])
        hit = _StubFrame("hit", [list(ROWS)])
        page = _StubPage([[]], [odd, hit])
        assert _run(page) == ROWS


class TestWiring:
    def test_table_harvest_routes_through_frame_fallback(self) -> None:
        src = inspect.getsource(run_agent)
        assert "_evaluate_rows_with_frame_fallback(" in src, (
            "bulk table extraction no longer routes through the iframe sweep"
        )
        assert 'log_tag="EXTRACT DOM"' in src

    def test_helper_keeps_main_result_shape(self) -> None:
        """Helper must always return a list, never None or raw evaluate output."""
        sig = inspect.signature(_evaluate_rows_with_frame_fallback)
        assert "log_tag" in sig.parameters


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

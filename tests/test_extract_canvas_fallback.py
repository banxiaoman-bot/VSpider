"""EXTRACT-CANVAS-1: canvas/svg-rendered grids get an explicit declaration.

Sheet engines (Luckysheet / Univer / Handsontable canvas mode,
ECharts/AntV dashboards) paint rows onto a canvas, so every deterministic
DOM harvest legitimately comes back empty. Before this slice the
pre-extract fast path just logged "no deterministic candidates" and fell
through - the planner burned VLM rounds rediscovering that there is
nothing to click. Now the empty-candidate branch probes for a dominant
canvas/svg, publishes evidence + fallback guidance (export button > view
switch > screenshot-with-caveat) into workflow_memory, and row_action's
existing failure hint stays untouched.

Source-anchor tests lock the wiring; the probe JS was validated against a
real canvas-painted grid with a live-browser probe before commit.
"""

from __future__ import annotations

import inspect

from visual_web_agent.main import run_agent


def _src() -> str:
    return inspect.getsource(run_agent)


class TestCanvasGridDeclaration:
    def test_probe_helper_exists_with_thresholds(self) -> None:
        src = _src()
        assert "async def _detect_canvas_grid(" in src
        assert "querySelectorAll('canvas, svg')" in src
        assert "r.width < 500 || r.height < 300" in src, (
            "small decorative canvases must not trigger the declaration"
        )

    def test_grid_like_class_scoring(self) -> None:
        src = _src()
        assert "grid|table|sheet|spread|cell|excel|luckysheet|univer|handsontable" in src
        assert "gridLike ? 1 : 0" in src

    def test_empty_candidates_branch_declares_before_giving_up(self) -> None:
        src = _src()
        idx_probe = src.index('_detect_canvas_grid(\n                    "pre-extract canvas grid probe"')
        idx_log = src.index('logger.info("[PRE-EXTRACT] no deterministic candidates")')
        assert idx_probe < idx_log, (
            "the canvas declaration must happen before the fast path gives up"
        )

    def test_notice_lands_in_workflow_memory_with_guidance(self) -> None:
        src = _src()
        assert 'workflow_memory["canvas_grid_notice"] = canvas_notice' in src
        assert "data_export" in src
        assert '"guidance"' in src

    def test_probe_failure_is_quiet(self) -> None:
        src = _src()
        assert "canvas grid probe failed" in src

"""auto_form bound-control iframe fallback + shadow reach (Slice FORM-IFRAME-2).

Closes the weakness "auto_form batch macro only evaluates the main document":
the bound-controls JS must descend open shadow roots, and the wrapper must
retry every child iframe when the main document resolves none of the fields.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from visual_web_agent.main import (
    _AUTO_FORM_NOT_FOUND_REASONS,
    _auto_form_fill_bound_controls_with_frames,
    _auto_form_result_found_nothing,
    _try_auto_form_fill,
    _try_auto_form_fill_bound_controls,
)

FIELDS = {"Name": "Ada", "Email": "ada@example.test"}

ALL_MISS = {
    "ok": False,
    "results": [
        {"label": "Name", "ok": False, "reason": "field_not_found"},
        {"label": "Email", "ok": False, "reason": "field_not_found"},
    ],
}
ALL_OK = {
    "ok": True,
    "submitted": True,
    "results": [
        {"label": "Name", "ok": True, "mode": "input"},
        {"label": "Email", "ok": True, "mode": "input"},
    ],
}


def _run(page: Any) -> dict:
    return asyncio.run(
        _auto_form_fill_bound_controls_with_frames(
            page, scope_title="", fields=FIELDS, require_submit=False
        )
    )


class _StubFrame:
    def __init__(
        self,
        name: str,
        results: list[dict[str, Any]] | None = None,
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
        return dict(ALL_MISS)


class _StubPage:
    def __init__(self, main_results: list[dict[str, Any]], frames: list[_StubFrame]) -> None:
        self.main_frame = _StubFrame("main")
        self.frames = [self.main_frame, *frames]
        self._results = list(main_results)
        self.eval_calls = 0

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.eval_calls += 1
        if self._results:
            return self._results.pop(0)
        return dict(ALL_MISS)


# ════════════════════════════════════════════════════════════════════
#                  frame fallback ordering / scoping
# ════════════════════════════════════════════════════════════════════


class TestAutoFormWithFrames:
    def test_main_document_hit_skips_frames(self) -> None:
        frame = _StubFrame("child", [dict(ALL_OK)])
        page = _StubPage([dict(ALL_OK)], [frame])
        result = _run(page)
        assert result["ok"] is True
        assert "frame_url" not in result
        assert frame.eval_calls == 0, "main-document hit must not probe iframes"

    def test_partial_hit_does_not_fall_back(self) -> None:
        """One field bound in the main doc: stay there, no cross-frame guessing."""
        partial = {
            "ok": False,
            "results": [
                {"label": "Name", "ok": True, "mode": "input"},
                {"label": "Email", "ok": False, "reason": "field_not_found"},
            ],
        }
        frame = _StubFrame("child", [dict(ALL_OK)])
        page = _StubPage([partial], [frame])
        result = _run(page)
        assert result is partial
        assert frame.eval_calls == 0

    def test_complex_component_failure_stays_in_main(self) -> None:
        """Found-but-complex controls are a find, not a miss."""
        complex_fail = {
            "ok": False,
            "results": [
                {
                    "label": "Name",
                    "ok": False,
                    "reason": "complex_component_requires_form_set_or_macro",
                },
                {"label": "Email", "ok": False, "reason": "field_not_found"},
            ],
        }
        frame = _StubFrame("child", [dict(ALL_OK)])
        page = _StubPage([complex_fail], [frame])
        result = _run(page)
        assert result is complex_fail
        assert frame.eval_calls == 0

    def test_falls_through_to_matching_iframe(self) -> None:
        miss = _StubFrame("miss")
        hit = _StubFrame("hit", [dict(ALL_OK)])
        page = _StubPage([dict(ALL_MISS)], [miss, hit])
        result = _run(page)
        assert result["ok"] is True
        assert result["frame_url"] == hit.url, "frame evidence must be recorded"
        assert miss.eval_calls == 1

    def test_all_not_found_returns_main_result(self) -> None:
        f1, f2 = _StubFrame("a"), _StubFrame("b")
        main_miss = dict(ALL_MISS)
        page = _StubPage([main_miss], [f1, f2])
        result = _run(page)
        assert result is main_miss
        assert "frame_url" not in result
        assert f1.eval_calls == 1 and f2.eval_calls == 1

    def test_detached_and_raising_frames_are_skipped(self) -> None:
        dead = _StubFrame("dead", detached=True)
        boom = _StubFrame("boom", raise_err=True)
        hit = _StubFrame("hit", [dict(ALL_OK)])
        page = _StubPage([dict(ALL_MISS)], [dead, boom, hit])
        result = _run(page)
        assert result["ok"] is True
        assert result["frame_url"] == hit.url
        assert dead.eval_calls == 0, "detached frame must not be evaluated"
        assert boom.eval_calls == 1, "raising frame is probed once then skipped"

    def test_main_frame_object_is_never_reprobed(self) -> None:
        page = _StubPage([dict(ALL_MISS)], [])
        _run(page)
        assert page.main_frame.eval_calls == 0
        assert page.eval_calls == 1


# ════════════════════════════════════════════════════════════════════
#                found-nothing predicate edge cases
# ════════════════════════════════════════════════════════════════════


class TestFoundNothingPredicate:
    @pytest.mark.parametrize(
        "result, expected",
        [
            ("not a dict", True),
            ({"ok": True}, False),
            ({"ok": False}, True),
            ({"ok": False, "results": []}, True),
            (ALL_MISS, True),
            (
                {
                    "ok": False,
                    "results": [{"label": "Name", "ok": False, "reason": "invalid_result"}],
                },
                True,
            ),
            (
                {
                    "ok": False,
                    "results": [
                        {"label": "Name", "ok": False, "reason": "field_not_found"},
                        {"label": "Email", "ok": False, "reason": "unsupported_control"},
                    ],
                },
                False,
            ),
            (
                {"ok": False, "reason": "submit_not_found",
                 "results": [{"label": "Name", "ok": True, "mode": "input"}]},
                False,
            ),
        ],
        ids=[
            "non_dict", "ok_result", "missing_results", "empty_results",
            "all_field_not_found", "invalid_result", "mixed_reasons", "submit_not_found",
        ],
    )
    def test_predicate(self, result: Any, expected: bool) -> None:
        assert _auto_form_result_found_nothing(result) is expected

    def test_reason_set_is_additive_and_stable(self) -> None:
        assert {"field_not_found", "invalid_result"} <= _AUTO_FORM_NOT_FOUND_REASONS


# ════════════════════════════════════════════════════════════════════
#            source-level anchors (shadow DOM + caller wiring)
# ════════════════════════════════════════════════════════════════════


class TestShadowDomAndWiring:
    def test_bound_controls_js_uses_deep_query(self) -> None:
        src = inspect.getsource(_try_auto_form_fill_bound_controls)
        assert "deepQueryAll" in src
        assert "shadowRoot" in src
        assert "Array.from(root.querySelectorAll(selector)).filter(isVisible)" not in src, (
            "bound-controls allVisible regressed to light-DOM-only querySelectorAll"
        )

    def test_auto_form_routes_through_frame_wrapper(self) -> None:
        src = inspect.getsource(_try_auto_form_fill)
        assert "_auto_form_fill_bound_controls_with_frames" in src
        assert "_try_auto_form_fill_bound_controls(" not in src, (
            "a call site bypasses the frame-fallback wrapper"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

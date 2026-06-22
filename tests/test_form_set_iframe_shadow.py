"""form_set iframe fallback + shadow-DOM reach regression (Slice FORM-IFRAME-1).

Covers the audit finding "form_set/auto_form only evaluate in the main
document": the binding JS must descend open shadow roots, and the handler
must retry every child iframe when the main document misses the label.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from visual_web_agent.actions import (
    _FORM_SET_NOT_FOUND_REASONS,
    FormSetHandler,
    _form_set_bound_control_v2,
    _form_set_with_frames,
)

NOT_FOUND = {"ok": False, "reason": "label_or_control_not_found"}


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
        return dict(NOT_FOUND)


class _StubPage:
    """Page-like stub: own evaluate result + child frames list."""

    def __init__(self, main_results: list[dict[str, Any]], frames: list[_StubFrame]) -> None:
        self.main_frame = _StubFrame("main")
        self.frames = [self.main_frame, *frames]
        self._results = list(main_results)
        self.eval_calls = 0

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.eval_calls += 1
        if self._results:
            return self._results.pop(0)
        return dict(NOT_FOUND)


# ════════════════════════════════════════════════════════════════════
#                 _form_set_with_frames fallback ordering
# ════════════════════════════════════════════════════════════════════


class TestFormSetWithFrames:
    def test_main_document_hit_skips_frames(self) -> None:
        frame = _StubFrame("child", [{"ok": True, "mode": "bound_control"}])
        page = _StubPage([{"ok": True, "mode": "bound_control", "observed": "x"}], [frame])
        result, scope = asyncio.run(_form_set_with_frames(page, "Name", "x"))
        assert result["ok"] is True
        assert scope is page
        assert frame.eval_calls == 0, "main-document hit must not probe iframes"

    def test_main_document_failure_with_binding_does_not_fall_back(self) -> None:
        """Field found in main doc but value mismatched: stay there (no cross-frame guessing)."""
        frame = _StubFrame("child", [{"ok": True, "mode": "bound_control"}])
        page = _StubPage(
            [{"ok": False, "mode": "value_mismatch", "reason": None, "observed": "y"}],
            [frame],
        )
        result, scope = asyncio.run(_form_set_with_frames(page, "Name", "x"))
        assert result["mode"] == "value_mismatch"
        assert scope is page
        assert frame.eval_calls == 0

    def test_falls_through_to_matching_iframe(self) -> None:
        miss = _StubFrame("miss")
        hit = _StubFrame("hit", [{"ok": True, "mode": "bound_control", "observed": "x"}])
        page = _StubPage([dict(NOT_FOUND)], [miss, hit])
        result, scope = asyncio.run(_form_set_with_frames(page, "Name", "x"))
        assert result["ok"] is True
        assert scope is hit
        assert result["frame_url"] == hit.url, "frame evidence must be recorded"
        assert miss.eval_calls == 1

    def test_opened_mode_in_iframe_counts_as_found(self) -> None:
        """A combobox that opened (ok=False, no not-found reason) is still a find."""
        hit = _StubFrame(
            "combo",
            [{"ok": True, "mode": "opened", "click_selector": "[data-x]"}],
        )
        page = _StubPage([dict(NOT_FOUND)], [hit])
        result, scope = asyncio.run(_form_set_with_frames(page, "Zone", "Zone one"))
        assert result["mode"] == "opened"
        assert scope is hit

    def test_all_not_found_returns_main_result_and_page_scope(self) -> None:
        f1, f2 = _StubFrame("a"), _StubFrame("b")
        page = _StubPage([dict(NOT_FOUND)], [f1, f2])
        result, scope = asyncio.run(_form_set_with_frames(page, "Ghost", "x"))
        assert result["reason"] in _FORM_SET_NOT_FOUND_REASONS
        assert scope is page
        assert f1.eval_calls == 1 and f2.eval_calls == 1

    def test_detached_and_raising_frames_are_skipped(self) -> None:
        dead = _StubFrame("dead", detached=True)
        boom = _StubFrame("boom", raise_err=True)
        hit = _StubFrame("hit", [{"ok": True, "mode": "bound_control"}])
        page = _StubPage([dict(NOT_FOUND)], [dead, boom, hit])
        result, scope = asyncio.run(_form_set_with_frames(page, "Name", "x"))
        assert result["ok"] is True
        assert scope is hit
        assert dead.eval_calls == 0, "detached frame must not be evaluated"
        assert boom.eval_calls == 1, "raising frame is probed once then skipped"

    def test_main_frame_object_is_never_reprobed(self) -> None:
        page = _StubPage([dict(NOT_FOUND)], [])
        asyncio.run(_form_set_with_frames(page, "Name", "x"))
        assert page.main_frame.eval_calls == 0
        assert page.eval_calls == 1


# ════════════════════════════════════════════════════════════════════
#            source-level anchors (shadow DOM + handler wiring)
# ════════════════════════════════════════════════════════════════════


class TestShadowDomAndWiring:
    def test_binding_js_uses_deep_query_for_controls_and_labels(self) -> None:
        src = inspect.getsource(_form_set_bound_control_v2)
        assert "deepQueryAll" in src
        assert "shadowRoot" in src
        assert "document.querySelectorAll(controlSelector)" not in src, (
            "controls lookup regressed to light-DOM-only querySelectorAll"
        )

    def test_handler_routes_through_frame_fallback(self) -> None:
        src = inspect.getsource(FormSetHandler.execute)
        assert "_form_set_with_frames" in src
        assert "frame_url" in src, "rpa_trail must carry the frame evidence field"

    def test_stub_invalid_result_normalised(self) -> None:
        """Non-dict evaluate results normalise to a retryable not-found reason."""

        class _WeirdPage:
            main_frame = None
            frames: list[Any] = []

            async def evaluate(self, script: str, arg: Any = None) -> Any:
                return "not a dict"

        result = asyncio.run(_form_set_bound_control_v2(_WeirdPage(), "Name", "x"))
        assert result["ok"] is False
        assert result["reason"] in _FORM_SET_NOT_FOUND_REASONS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

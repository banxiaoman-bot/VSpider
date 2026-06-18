"""Regression tests for the extraction runtime carved out of main.run_agent.

S1a: ``ExtractState`` groups the ~19 run-level extraction counters that the
main step loop and ``ExtractRuntime`` both read and write, so they can share a
single mutable instance instead of free closure variables.

S1b: ``ExtractDeps`` + ``ExtractRuntime`` host the 6 read-only DOM extraction
helpers moved verbatim out of ``main.run_agent`` (pure move, behaviour checked
against stub frames here).
"""

import asyncio
import logging

from visual_web_agent.extraction_engine.runtime import (
    ExtractDeps,
    ExtractRuntime,
    ExtractState,
)


def test_extract_state_scalar_defaults():
    s = ExtractState()
    assert s.extract_count == 0
    assert s.total_extracted_rows == 0
    assert s.extract_null_streak == 0
    assert s.extract_null_total_resets == 0
    assert s.pagination_kind == ""
    assert s.pagination_hint_msg == ""
    assert s.block_next_page_reason == ""
    assert s.pagination_probed is False
    assert s.first_flip_pending is False
    assert s.page_is_infinite_scroll is False
    assert s.force_next_page_pending is False
    assert s.force_extract_after_navigation_pending is False
    assert s.block_next_page_until_drained is False
    assert s.first_extract_ever_done is False
    assert s.pagination_exhausted is False


def test_extract_state_set_defaults_are_empty_sets():
    s = ExtractState()
    assert s.extracted_page_urls == set()
    assert s.extracted_page_keys == set()
    assert s.seen_extract_row_keys == set()
    assert s.tooltip_trigger_keys == set()


def test_extract_state_shared_mutation_visible_through_alias():
    # The whole point of S1a: one instance, two references, mutations visible
    # to both (mirrors closure + main-loop both touching the same counters).
    s = ExtractState()
    alias = s
    alias.total_extracted_rows += 3
    alias.extract_count += 1
    alias.seen_extract_row_keys.add("k")
    assert s.total_extracted_rows == 3
    assert s.extract_count == 1
    assert "k" in s.seen_extract_row_keys


def test_extract_state_sets_are_per_instance_not_shared_default():
    # Guards against the classic mutable-default bug: each instance must own
    # its own sets (field(default_factory=set)), not a shared class-level set.
    a = ExtractState()
    b = ExtractState()
    a.seen_extract_row_keys.add("x")
    a.extracted_page_urls.add("http://example.com")
    assert "x" not in b.seen_extract_row_keys
    assert b.extracted_page_urls == set()


# ── S1b: ExtractDeps + ExtractRuntime DOM readers ───────────────────────────


async def _async_none(*_a, **_k):
    return None


class _StubFrame:
    """Minimal page/frame stub: evaluate() returns a canned value."""

    def __init__(self, eval_result=None):
        self._eval_result = eval_result

    async def evaluate(self, _js, *_a, **_k):
        return self._eval_result

    async def wait_for_function(self, _js, *_a, **_k):
        return True


class _StubBrowser:
    def __init__(self, page):
        self._page = page

    async def _ensure_active_page(self, reason=""):
        return self._page


def _mk_runtime(*, browser=None, eval_fb=None, normalize=None):
    deps = ExtractDeps(
        browser=browser if browser is not None else _StubBrowser(_StubFrame()),
        logger=logging.getLogger("test-extract-runtime"),
        evaluate_rows_with_frame_fallback=eval_fb or _async_none,
        normalize_extracted_row_fields=normalize or (lambda rows, **_k: rows),
    )
    return ExtractRuntime(deps, ExtractState())


def test_extract_runtime_holds_deps_and_state():
    state = ExtractState()
    deps = ExtractDeps(
        browser=None,
        logger=logging.getLogger("t"),
        evaluate_rows_with_frame_fallback=_async_none,
        normalize_extracted_row_fields=lambda rows, **_k: rows,
    )
    rt = ExtractRuntime(deps, state)
    assert rt.deps is deps
    assert rt.state is state


def test_extract_list_rows_via_dom_returns_rows_and_source():
    async def fake_eval(_page, _js, **_kw):
        return {
            "rows": [{"title": "A", "url": "/a"}, {"title": "B", "url": "/b"}],
            "sourceText": "src-text",
        }

    seen = {}

    def fake_norm(rows, *, project=True):
        seen["rows"] = rows
        return rows

    rt = _mk_runtime(eval_fb=fake_eval, normalize=fake_norm)
    rows, source = asyncio.run(rt.extract_list_rows_via_dom("t"))
    assert len(rows) == 2
    assert rows[0]["title"] == "A"
    assert source == "src-text"
    assert seen["rows"][1]["url"] == "/b"


def test_extract_list_rows_via_dom_empty_on_non_dict():
    async def fake_eval(_page, _js, **_kw):
        return None

    rt = _mk_runtime(eval_fb=fake_eval)
    rows, source = asyncio.run(rt.extract_list_rows_via_dom("t"))
    assert rows == []
    assert source == ""


def test_extract_visible_table_rows_via_dom_returns_list():
    async def fake_eval(_page, _js, **_kw):
        return [{"column_1": "1"}, {"column_1": "2"}]

    rt = _mk_runtime(eval_fb=fake_eval)
    rows = asyncio.run(rt.extract_visible_table_rows_via_dom("t"))
    assert rows == [{"column_1": "1"}, {"column_1": "2"}]


def test_visible_table_signature_uses_active_page():
    rt = _mk_runtime(browser=_StubBrowser(_StubFrame(eval_result="r1|r2|r3")))
    sig = asyncio.run(rt.visible_table_signature("t"))
    assert sig == "r1|r2|r3"


def test_visible_table_signature_with_explicit_scope():
    scope = _StubFrame(eval_result="x|y")
    rt = _mk_runtime(browser=_StubBrowser(_StubFrame(eval_result="UNUSED")))
    sig = asyncio.run(rt.visible_table_signature("t", scope=scope))
    assert sig == "x|y"


class _PagerScope:
    """Page stub for autopager: tells signature JS from pager JS by content."""

    def __init__(self):
        self.main_frame = object()
        self.frames = []
        self.clicked = False

    async def evaluate(self, js, *_a, **_k):
        if "dom_next_button" in js:
            self.clicked = True
            return {"ok": True, "method": "dom_next_button", "label": "Next"}
        if "join('|')" in js:
            return "after-sig" if self.clicked else "before-sig"
        return ""

    async def wait_for_function(self, _js, *_a, **_k):
        return True


def test_auto_advance_table_page_via_dom_advances():
    rt = _mk_runtime(browser=_StubBrowser(_PagerScope()))
    advanced = asyncio.run(rt.auto_advance_table_page_via_dom("t"))
    assert advanced is True


def test_probe_scroll_drain_state_returns_dict():
    frame = _StubFrame(eval_result={"at_bottom": True, "window_remaining": 0})
    rt = _mk_runtime(browser=_StubBrowser(frame))
    drain = asyncio.run(rt.probe_scroll_drain_state("t"))
    assert drain["at_bottom"] is True


def test_detect_canvas_grid_found_adds_guidance():
    frame = _StubFrame(
        eval_result={"found": True, "tag": "canvas", "width": 800, "height": 600}
    )
    rt = _mk_runtime(browser=_StubBrowser(frame))
    notice = asyncio.run(rt.detect_canvas_grid("t"))
    assert notice["found"] is True
    assert "guidance" in notice
    assert "canvas" in notice["guidance"]


def test_detect_canvas_grid_not_found_returns_empty():
    frame = _StubFrame(eval_result={"found": False})
    rt = _mk_runtime(browser=_StubBrowser(frame))
    notice = asyncio.run(rt.detect_canvas_grid("t"))
    assert notice == {}

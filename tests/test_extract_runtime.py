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


def _mk_runtime(
    *,
    browser=None,
    eval_fb=None,
    goal="抓 10 条",
    goal_output_mode="dataset",
    requested_output_fields=None,
    data_controller=None,
    state=None,
    event_stream=None,
    run_ts="",
    snapshot_goal="",
    goal_output_contract=None,
    vlm_output="",
    enable_xhr=False,
):
    deps = ExtractDeps(
        browser=browser if browser is not None else _StubBrowser(_StubFrame()),
        logger=logging.getLogger("test-extract-runtime"),
        evaluate_rows_with_frame_fallback=eval_fb or _async_none,
        goal=goal,
        goal_output_mode=goal_output_mode,
        requested_output_fields=[] if requested_output_fields is None else requested_output_fields,
        data_controller=data_controller,
        event_stream=event_stream,
        run_ts=run_ts,
        snapshot_goal=snapshot_goal,
        goal_output_contract=goal_output_contract,
        vlm_output=vlm_output,
        enable_xhr=enable_xhr,
    )
    return ExtractRuntime(deps, state if state is not None else ExtractState())


def test_extract_runtime_holds_deps_and_state():
    state = ExtractState()
    deps = ExtractDeps(
        browser=None,
        logger=logging.getLogger("t"),
        evaluate_rows_with_frame_fallback=_async_none,
        goal="g",
        goal_output_mode="dataset",
        requested_output_fields=[],
        data_controller=None,
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

    # normalize_extracted_row_fields is now a real method; with no
    # requested fields it passes rows through unchanged (relative urls
    # are not probable, so no rewriting).
    rt = _mk_runtime(eval_fb=fake_eval)
    rows, source = asyncio.run(rt.extract_list_rows_via_dom("t"))
    assert len(rows) == 2
    assert rows[0]["title"] == "A"
    assert rows[1]["url"] == "/b"
    assert source == "src-text"


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


# ── S1c: ExtractRuntime arbiter + normalizer methods ────────────────────


class _StubDataController:
    """Minimal PageDataController stand-in for sanitize_extraction_candidate."""

    def __init__(self):
        self.signature = "sig"

    def filter_undercomplete_rows(self, data, normalize_row=None):
        return data, {"dropped": 0, "required_hits": 0, "total_fields": 0}

    def rows_signature(self, rows):
        return self.signature


def test_field_aliases_expands_synonyms():
    rt = _mk_runtime()
    url_aliases = rt.field_aliases("url")
    assert "link" in url_aliases and "href" in url_aliases
    assert "name" in rt.field_aliases("title")


def test_first_int_value_parses_or_passes_through():
    rt = _mk_runtime()
    assert rt.first_int_value("1,234 points") == 1234
    assert rt.first_int_value("no-digits") == "no-digits"


def test_classify_url_role_detail_vs_source():
    rt = _mk_runtime()
    assert rt.classify_url_role("https://x.com/item/9") == "detail"
    assert rt.classify_url_role("https://x.com/list") == "source"
    assert rt.classify_url_role("") == ""


def test_is_probable_url_scheme_only():
    rt = _mk_runtime()
    assert rt.is_probable_url("https://a.com") is True
    assert rt.is_probable_url("HTTP://a.com") is True
    assert rt.is_probable_url("/relative") is False


def test_project_row_keeps_requested_fields_only():
    rt = _mk_runtime(requested_output_fields=["title", "url"])
    row = {"title": "T", "url": "https://e.com/x", "junk": "drop me"}
    projected = rt.project_row_to_requested_fields(row)
    assert set(projected.keys()) == {"title", "url"}
    assert projected["title"] == "T"


def test_requested_field_coverage_counts_hits():
    rt = _mk_runtime(requested_output_fields=["title", "url"])
    hit, total = rt.requested_field_coverage({"title": "T", "url": "https://e.com/x"})
    assert total == 2
    assert hit == 2


def test_normalize_maps_age_to_time_and_ints_points():
    rt = _mk_runtime(requested_output_fields=[])
    out = rt.normalize_extracted_row_fields(
        [{"title": "T", "age": "3 hours ago", "points": "1,234"}], project=False
    )
    assert out[0]["time"] == "3 hours ago"
    assert "age" not in out[0]
    assert out[0]["points"] == 1234


def test_expected_rows_from_data_shape_dense_table():
    rt = _mk_runtime()
    assert rt.expected_rows_from_data_shape({"table_rows": 25, "table_cells": 5}) == 25
    assert rt.expected_rows_from_data_shape({}) == 0


def test_record_extract_progress_dataset_increments_state():
    state = ExtractState()
    rt = _mk_runtime(state=state)
    new_rows, total = rt.record_extract_progress([{"a": 1}], 5)
    assert new_rows == 5
    assert total == 5
    assert state.total_extracted_rows == 5


def test_commit_extraction_candidate_updates_seen_keys():
    state = ExtractState()
    rt = _mk_runtime(state=state)
    rows, accepted, dups, rejected, src = rt.commit_extraction_candidate(
        {
            "rows": [{"a": 1}],
            "accepted": 1,
            "duplicates": 0,
            "rejected": 0,
            "source_text": "s",
            "fingerprints": {"fp1"},
        }
    )
    assert accepted == 1
    assert rows == [{"a": 1}]
    assert "fp1" in state.seen_extract_row_keys


def test_choose_best_extraction_candidate_dataset_picks_highest_score():
    rt = _mk_runtime(goal_output_mode="dataset")
    low = {"name": "DOM_TABLE", "accepted": 2, "score": 10.0, "data_shape": {}}
    high = {"name": "DOM_CARDS", "accepted": 5, "score": 99.0, "data_shape": {}}
    assert rt.choose_best_extraction_candidate([low, high]) is high


def test_choose_best_extraction_candidate_none_when_no_accepted():
    rt = _mk_runtime()
    assert rt.choose_best_extraction_candidate([{"name": "X", "accepted": 0}]) is None


def test_sanitize_extraction_candidate_returns_scored_candidate():
    rt = _mk_runtime(requested_output_fields=[], data_controller=_StubDataController())
    candidate = rt.sanitize_extraction_candidate(
        name="dom_cards",
        data=[
            {"title": "Item one", "url": "https://e.com/1"},
            {"title": "Item two", "url": "https://e.com/2"},
        ],
        source_text="src",
    )
    assert candidate["name"] == "dom_cards"
    assert isinstance(candidate["score"], float)
    assert candidate["data_signature"] == "sig"
    assert "rows" in candidate


# ── S1d: ExtractRuntime fast-path / finalizer-adjacent methods ─────────


def test_compact_link_match_text_strips_nonalnum():
    rt = _mk_runtime()
    assert rt.compact_link_match_text("Hello, World!") == "helloworld"


def test_row_primary_link_text_prefers_title_field():
    rt = _mk_runtime()
    row = {"title": "Breaking News Headline", "score": "123"}
    assert rt.row_primary_link_text(row) == "Breaking News Headline"


def test_capture_body_text_excerpt_truncates():
    rt = _mk_runtime(browser=_StubBrowser(_StubFrame(eval_result="HELLO WORLD")))
    assert asyncio.run(rt.capture_body_text_excerpt(limit=5)) == "HELLO"


def test_inspect_click_nav_guard_non_click_returns_empty():
    rt = _mk_runtime()
    out = asyncio.run(rt.inspect_click_target_for_extract_nav_guard({"action": "type"}))
    assert out == {}


def test_inspect_click_nav_guard_no_target_id_returns_text():
    rt = _mk_runtime()
    out = asyncio.run(
        rt.inspect_click_target_for_extract_nav_guard(
            {"action": "click", "target_id": 0, "type_value": "Submit"}
        )
    )
    assert out == {"text": "Submit", "action": "click"}


def test_extract_body_text_for_semantic_cards_no_fields_returns_empty():
    rt = _mk_runtime(requested_output_fields=[])
    assert asyncio.run(rt.extract_body_text_for_semantic_cards("t")) == ""


def test_extract_body_text_for_semantic_cards_with_fields():
    rt = _mk_runtime(
        requested_output_fields=["title"],
        browser=_StubBrowser(_StubFrame(eval_result="card body text")),
    )
    assert asyncio.run(rt.extract_body_text_for_semantic_cards("t")) == "card body text"


def test_xhr_target_reached_disabled_returns_false():
    class _Br:
        intercepted_count = 5

        async def _ensure_active_page(self, reason=""):
            return _StubFrame()

    rt = _mk_runtime(browser=_Br(), enable_xhr=False)
    reached, count, _target = rt.xhr_target_reached()
    assert reached is False
    assert count == 5


def test_xhr_saved_row_count_no_filename_returns_none():
    class _Br:
        _intercept_filename = ""

    count, path = _mk_runtime(browser=_Br()).xhr_saved_row_count()
    assert count is None
    assert path == ""


def test_enrich_rows_no_anchors_returns_normalized():
    rt = _mk_runtime(
        browser=_StubBrowser(_StubFrame(eval_result=[])),
        requested_output_fields=[],
    )
    rows = asyncio.run(rt.enrich_rows_with_dom_links([{"title": "X", "url": "/a"}]))
    assert rows == [{"title": "X", "url": "/a"}]


def test_extract_compact_list_text_via_dom_returns_on_rich_list():
    payload = {"count": 6, "text": "x" * 500}
    rt = _mk_runtime(browser=_StubBrowser(_StubFrame(eval_result=payload)))
    source, text, count = asyncio.run(rt.extract_compact_list_text_via_dom("t"))
    assert source == "LIST_ITEMS_TEXT"
    assert count == 6
    assert len(text) == 500

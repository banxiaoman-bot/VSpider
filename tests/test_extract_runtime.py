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
    vlm=None,
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
        vlm=vlm,
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


# ── R2-1: compute_data_shape_with_drain (dedup of auto/explicit extract) ─────


def test_compute_data_shape_with_drain_sparse_skips_override():
    # < 10 expected rows: probe shape returned as-is, drain probe never runs.
    class _Br:
        async def probe_data_shape(self):
            return {"table_rows": 2}

        async def _ensure_active_page(self, reason=""):
            raise AssertionError("drain probe must not run for sparse shape")

    rt = _mk_runtime(browser=_Br())
    shape = asyncio.run(rt.compute_data_shape_with_drain("r"))
    assert shape == {"table_rows": 2}
    assert "drain_state" not in shape


def test_compute_data_shape_with_drain_dense_attaches_override():
    # >= 10 expected rows (table_rows>=3 and table_cells>=2): attach drain state.
    class _Frame:
        async def evaluate(self, *_a, **_k):
            return {"at_bottom": True, "window_remaining": 0}

    class _Br:
        async def probe_data_shape(self):
            return {"table_rows": 50, "table_cells": 3}

        async def _ensure_active_page(self, reason=""):
            return _Frame()

    rt = _mk_runtime(browser=_Br())
    shape = asyncio.run(rt.compute_data_shape_with_drain("r"))
    assert shape["table_rows"] == 50
    assert shape["physically_drained"] is True
    assert shape["drain_state"]["at_bottom"] is True


def test_compute_data_shape_with_drain_probe_failure_returns_empty():
    # probe_data_shape raising is swallowed; empty shape returned.
    class _Br:
        async def probe_data_shape(self):
            raise RuntimeError("probe boom")

    rt = _mk_runtime(browser=_Br())
    shape = asyncio.run(rt.compute_data_shape_with_drain("r"))
    assert shape == {}


# ── R2-2: gather_dom_list_card_candidates (dedup of auto/explicit extract) ───


def _passthrough_sanitize(rt):
    """Replace sanitize_extraction_candidate with a thin recorder so the
    gather tests assert on branching (which candidates, order, source_text),
    not on the heavy sanitize internals (data_controller/fingerprints)."""
    rt.sanitize_extraction_candidate = lambda **kw: {
        "name": kw["name"],
        "data": kw["data"],
        "source_text": kw.get("source_text", ""),
        "data_shape": kw.get("data_shape"),
    }


async def _async_empty_body(_reason):
    return ""


def test_gather_dom_list_card_candidates_empty_rows_noop():
    rt = _mk_runtime()
    candidates: list = []
    asyncio.run(
        rt.gather_dom_list_card_candidates(
            candidates,
            dom_list_rows=[],
            dom_list_text="",
            data_shape={},
            card_base_texts=("base",),
            fallback_source_text="fb",
            body_text_reason="r",
        )
    )
    assert candidates == []


def test_gather_dom_list_card_candidates_list_only_when_no_cards(monkeypatch):
    from visual_web_agent.extraction_engine import runtime as _rt_mod

    monkeypatch.setattr(_rt_mod, "extract_semantic_card_rows", lambda *a, **k: ([], ""))
    rt = _mk_runtime()
    _passthrough_sanitize(rt)
    rt.extract_body_text_for_semantic_cards = _async_empty_body
    candidates: list = []
    asyncio.run(
        rt.gather_dom_list_card_candidates(
            candidates,
            dom_list_rows=[{"a": 1}],
            dom_list_text="list-text",
            data_shape={},
            card_base_texts=("base",),
            fallback_source_text="fb",
            body_text_reason="r",
        )
    )
    assert [c["name"] for c in candidates] == ["DOM_LIST"]
    assert candidates[0]["source_text"] == "list-text"


def test_gather_dom_list_card_candidates_cards_then_list(monkeypatch):
    from visual_web_agent.extraction_engine import runtime as _rt_mod

    monkeypatch.setattr(
        _rt_mod, "extract_semantic_card_rows", lambda *a, **k: ([{"c": 1}], "card-text")
    )
    rt = _mk_runtime()
    _passthrough_sanitize(rt)
    candidates: list = []
    asyncio.run(
        rt.gather_dom_list_card_candidates(
            candidates,
            dom_list_rows=[{"a": 1}],
            dom_list_text="list-text",
            data_shape={},
            card_base_texts=("base",),
            fallback_source_text="fb",
            body_text_reason="r",
        )
    )
    assert [c["name"] for c in candidates] == ["DOM_CARDS", "DOM_LIST"]
    assert candidates[0]["source_text"] == "card-text"
    assert candidates[1]["source_text"] == "list-text"


def test_gather_dom_list_card_candidates_body_text_fallback(monkeypatch):
    calls = {"n": 0}

    def _fake_cards(_rows, *, source_text, requested_fields, goal):
        calls["n"] += 1
        if calls["n"] == 1:
            return ([], "")
        return ([{"c": 1}], "card-after-body")

    from visual_web_agent.extraction_engine import runtime as _rt_mod

    monkeypatch.setattr(_rt_mod, "extract_semantic_card_rows", _fake_cards)
    rt = _mk_runtime()
    _passthrough_sanitize(rt)

    async def _body(_reason):
        return "body-text"

    rt.extract_body_text_for_semantic_cards = _body
    candidates: list = []
    asyncio.run(
        rt.gather_dom_list_card_candidates(
            candidates,
            dom_list_rows=[{"a": 1}],
            dom_list_text="list-text",
            data_shape={},
            card_base_texts=("base",),
            fallback_source_text="fb",
            body_text_reason="r",
        )
    )
    assert calls["n"] == 2
    assert [c["name"] for c in candidates] == ["DOM_CARDS", "DOM_LIST"]
    assert candidates[0]["source_text"] == "card-after-body"


def test_gather_dom_list_card_candidates_list_source_falls_back(monkeypatch):
    from visual_web_agent.extraction_engine import runtime as _rt_mod

    monkeypatch.setattr(_rt_mod, "extract_semantic_card_rows", lambda *a, **k: ([], ""))
    rt = _mk_runtime()
    _passthrough_sanitize(rt)
    rt.extract_body_text_for_semantic_cards = _async_empty_body
    candidates: list = []
    asyncio.run(
        rt.gather_dom_list_card_candidates(
            candidates,
            dom_list_rows=[{"a": 1}],
            dom_list_text="",
            data_shape={},
            card_base_texts=("base",),
            fallback_source_text="fb-source",
            body_text_reason="r",
        )
    )
    assert candidates[0]["name"] == "DOM_LIST"
    assert candidates[0]["source_text"] == "fb-source"


# ── R2-3b: arm_pagination_after_extract (explicit-path pagination probe/arm) ──


class _ProbeBrowser:
    """Browser stub for arm_pagination_after_extract: canned probe_pagination
    result + a drain frame for the low-yield probe_scroll_drain_state call."""

    def __init__(self, probe_result, *, drain_at_bottom=True):
        self._probe_result = probe_result
        self._drain = {"at_bottom": drain_at_bottom}

    async def probe_pagination(self):
        return self._probe_result

    async def _ensure_active_page(self, reason=""):
        return _StubFrame(eval_result=self._drain)


def test_arm_pagination_first_flip_sets_pending_for_page_goal():
    from visual_web_agent.phases.pagination_helpers import (
        _should_force_first_flip_after_successful_extract,
    )

    goal = "抓取前3页数据"
    # premise: this goal must trigger the first-flip hard constraint
    assert _should_force_first_flip_after_successful_extract(goal) is True
    # pagination_probed=True + extract_count=0 isolates the first-flip branch
    state = ExtractState(pagination_probed=True, extract_count=0)
    rt = _mk_runtime(
        goal=goal,
        state=state,
        browser=_ProbeBrowser({"has_paginator": False, "kind": "", "candidates": []}),
    )
    asyncio.run(
        rt.arm_pagination_after_extract(
            new_rows=5, log_extract_text_source="DOM_LIST", data_shape={}
        )
    )
    assert state.first_extract_ever_done is True
    assert state.first_flip_pending is True


def test_arm_pagination_probe_has_paginator_arms_next_page():
    state = ExtractState(extract_count=1, total_extracted_rows=15)
    rt = _mk_runtime(
        goal="抓取50条数据",
        state=state,
        browser=_ProbeBrowser(
            {
                "has_paginator": True,
                "kind": "numeric",
                "candidates": [{"ref": "r1", "name": "2"}],
            }
        ),
    )
    asyncio.run(
        rt.arm_pagination_after_extract(
            new_rows=15, log_extract_text_source="DOM_LIST", data_shape={}
        )
    )
    assert state.pagination_probed is True
    assert state.pagination_kind == "numeric"
    assert state.force_next_page_pending is True
    assert "分页器" in state.pagination_hint_msg


def test_arm_pagination_no_paginator_marks_infinite_scroll():
    state = ExtractState(extract_count=1, total_extracted_rows=5)
    rt = _mk_runtime(
        goal="抓取50条数据",
        state=state,
        browser=_ProbeBrowser({"has_paginator": False, "kind": "", "candidates": []}),
    )
    asyncio.run(
        rt.arm_pagination_after_extract(
            new_rows=3, log_extract_text_source="DOM_LIST", data_shape={}
        )
    )
    assert state.page_is_infinite_scroll is True


def test_arm_pagination_rearm_when_paginator_known_and_target_unmet():
    # probe skipped (extract_count != 1); the re-arm branch fires.
    state = ExtractState(
        pagination_probed=True,
        pagination_kind="numeric",
        page_is_infinite_scroll=False,
        force_next_page_pending=False,
        extract_count=2,
        total_extracted_rows=5,
    )
    rt = _mk_runtime(
        goal="抓取50条数据",
        state=state,
        browser=_ProbeBrowser({"has_paginator": True, "kind": "numeric", "candidates": []}),
    )
    asyncio.run(
        rt.arm_pagination_after_extract(
            new_rows=5, log_extract_text_source="DOM_LIST", data_shape={}
        )
    )
    assert state.force_next_page_pending is True


# ── R2-3a: handle_zero_new_rows_feedback (explicit-path no-new-rows dedup) ────


class _RecVlm:
    """VLM stub recording inject_error_feedback messages."""

    def __init__(self):
        self.feedback = []

    def inject_error_feedback(self, msg):
        self.feedback.append(str(msg))


def _mk_zero_rows_runtime(vlm, state, *, goal="抓取50条数据", drain=None):
    rt = _mk_runtime(goal=goal, state=state, vlm=vlm)
    # isolate the feedback branching from the real browser scroll machinery
    _drain = drain or {"at_bottom": False, "window_remaining": 500, "container_remaining": 0}

    async def _fake_drain(_reason):
        return _drain

    rt.probe_scroll_drain_state = _fake_drain
    calls = {"nudge": 0}

    async def _fake_nudge(_reason, scroll_amount=2000):
        calls["nudge"] += 1
        return True

    rt.nudge_scroll_after_duplicate_extract = _fake_nudge
    return rt, calls


def test_zero_rows_target_already_reached_tells_done():
    vlm = _RecVlm()
    state = ExtractState(total_extracted_rows=50)
    rt, _calls = _mk_zero_rows_runtime(vlm, state, goal="抓取50条数据")
    streak = asyncio.run(
        rt.handle_zero_new_rows_feedback(
            duplicate_zero_extract_streak=0,
            dup_rows=2,
            rejected_rows=0,
            current_url="http://x",
            data_shape={},
        )
    )
    assert streak == 0  # pre_reached branch does not bump the streak
    assert any("已达成" in m for m in vlm.feedback)


def test_zero_rows_prior_page_drained_arms_first_flip():
    vlm = _RecVlm()
    state = ExtractState(total_extracted_rows=10, extracted_page_urls={"http://x"})
    rt, _calls = _mk_zero_rows_runtime(
        vlm, state, goal="抓取50条数据", drain={"at_bottom": True}
    )
    streak = asyncio.run(
        rt.handle_zero_new_rows_feedback(
            duplicate_zero_extract_streak=0,
            dup_rows=0,
            rejected_rows=0,
            current_url="http://x",
            data_shape={},
        )
    )
    assert streak == 1
    assert state.first_flip_pending is True
    assert any("物理触底" in m for m in vlm.feedback)


def test_zero_rows_prior_page_not_drained_nudges():
    vlm = _RecVlm()
    state = ExtractState(total_extracted_rows=10, extracted_page_keys={"k"})
    rt, calls = _mk_zero_rows_runtime(
        vlm,
        state,
        goal="抓取50条数据",
        drain={"at_bottom": False, "window_remaining": 800, "container_remaining": 0},
    )
    streak = asyncio.run(
        rt.handle_zero_new_rows_feedback(
            duplicate_zero_extract_streak=0,
            dup_rows=0,
            rejected_rows=0,
            current_url="http://x",
            data_shape={},
        )
    )
    assert streak == 1
    assert calls["nudge"] == 1
    assert state.first_flip_pending is False


def test_zero_rows_dense_page_blocks_next_page():
    vlm = _RecVlm()
    state = ExtractState()  # no prior page, no row-count target met
    rt, calls = _mk_zero_rows_runtime(vlm, state, goal="抓取所有数据")
    streak = asyncio.run(
        rt.handle_zero_new_rows_feedback(
            duplicate_zero_extract_streak=0,
            dup_rows=0,
            rejected_rows=0,
            current_url="http://x",
            data_shape={"table_rows": 25, "table_cells": 3},
        )
    )
    assert streak == 1
    assert state.block_next_page_until_drained is True
    assert calls["nudge"] == 1


# ── R2-3c: inject_post_extract_pagination_guidance (explicit-path) ───────────


class _PagLinksBrowser:
    """Browser stub exposing a canned (sync) find_pagination_links."""

    def __init__(self, links=None):
        self._links = links or []

    def find_pagination_links(self):
        return self._links


def test_post_extract_guidance_target_reached_says_done():
    vlm = _RecVlm()
    state = ExtractState(total_extracted_rows=50, extracted_page_urls={"a", "b"})
    rt = _mk_runtime(goal="抓取50条数据", state=state, vlm=vlm, browser=_PagLinksBrowser())
    rt.inject_post_extract_pagination_guidance()
    assert any("已达到目标" in m for m in vlm.feedback)


def test_post_extract_guidance_multi_page_target_with_links():
    vlm = _RecVlm()
    state = ExtractState(total_extracted_rows=10, extracted_page_urls={"a", "b"})
    rt = _mk_runtime(
        goal="抓取50条数据",
        state=state,
        vlm=vlm,
        browser=_PagLinksBrowser([{"id": 7, "role": "link", "name": "Next"}]),
    )
    rt.inject_post_extract_pagination_guidance()
    assert any("target_id=7" in m for m in vlm.feedback)


def test_post_extract_guidance_multi_page_no_target_asks_review():
    vlm = _RecVlm()
    state = ExtractState(total_extracted_rows=5, extracted_page_urls={"a", "b"})
    rt = _mk_runtime(goal="抓取所有数据", state=state, vlm=vlm, browser=_PagLinksBrowser())
    rt.inject_post_extract_pagination_guidance()
    assert any("判断是否需要继续翻页" in m for m in vlm.feedback)


def test_post_extract_guidance_single_page_no_links_suggests_done():
    vlm = _RecVlm()
    state = ExtractState(total_extracted_rows=3, extract_count=1)
    rt = _mk_runtime(goal="抓取所有数据", state=state, vlm=vlm, browser=_PagLinksBrowser([]))
    rt.inject_post_extract_pagination_guidance()
    assert any("可能已是最后一页" in m for m in vlm.feedback)


def test_post_extract_guidance_noop_when_nothing_extracted():
    vlm = _RecVlm()
    state = ExtractState()  # n_pages=0 and extract_count=0 -> no branch fires
    rt = _mk_runtime(goal="抓取所有数据", state=state, vlm=vlm, browser=_PagLinksBrowser())
    rt.inject_post_extract_pagination_guidance()
    assert vlm.feedback == []


# ── R2-3d: select_and_commit_extraction (explicit-path choose + commit) ───────


def _commit_candidate(name, rows, *, source_text="src"):
    return {
        "name": name,
        "rows": rows,
        "accepted": len(rows),
        "duplicates": 0,
        "rejected": 0,
        "source_text": source_text,
        "fingerprints": set(),
        "score": 50.0,
        "data_shape": {},
    }


def test_select_and_commit_chosen_non_table_keeps_prior_page_key():
    rt = _mk_runtime()
    cand = _commit_candidate("DOM_LIST", [{"a": 1}, {"a": 2}])
    res = asyncio.run(
        rt.select_and_commit_extraction(
            candidates=[cand],
            current_url="http://x",
            current_extract_page_key="PRIOR",
            source_text_for_validation="prior_src",
            log_extract_text_source="PRIOR_SRC",
        )
    )
    assert res.extracted == [{"a": 1}, {"a": 2}]
    assert res.new_rows == 2
    assert res.source_text_for_validation == "src"
    assert res.log_extract_text_source == "DOM_LIST"
    assert res.current_extract_page_key == "PRIOR"  # non-table leaves prior key


def test_select_and_commit_dom_table_sets_page_key():
    rt = _mk_runtime(browser=_StubBrowser(_StubFrame(eval_result="r1|r2|r3")))
    cand = _commit_candidate("DOM_TABLE", [{"c": 1}, {"c": 2}])
    res = asyncio.run(
        rt.select_and_commit_extraction(
            candidates=[cand],
            current_url="http://x",
            current_extract_page_key="PRIOR",
            source_text_for_validation="",
            log_extract_text_source="",
        )
    )
    assert res.log_extract_text_source == "DOM_TABLE"
    assert res.current_extract_page_key.startswith("http://x#table:")
    assert res.current_extract_page_key != "PRIOR"


def test_select_and_commit_no_candidate_returns_priors():
    rt = _mk_runtime()
    res = asyncio.run(
        rt.select_and_commit_extraction(
            candidates=[],
            current_url="http://x",
            current_extract_page_key="PRIOR",
            source_text_for_validation="prior_src",
            log_extract_text_source="PRIOR_SRC",
        )
    )
    assert res.extracted == []
    assert res.new_rows == 0
    assert res.dup_rows == 0
    assert res.rejected_rows == 0
    assert res.source_text_for_validation == "prior_src"
    assert res.log_extract_text_source == "PRIOR_SRC"
    assert res.current_extract_page_key == "PRIOR"


# ── R2-3e: persist_extracted_batch (explicit-path enrich/save/snapshot/count) ─


def _mk_persist_runtime(monkeypatch, *, goal_output_mode="dataset", api_applied=False, state=None):
    from visual_web_agent.extraction_engine import runtime as _rt_mod

    monkeypatch.setattr(_rt_mod, "save_run_dataset", lambda *a, **k: "out.xlsx")
    rt = _mk_runtime(
        goal="抓取数据", goal_output_mode=goal_output_mode, state=state or ExtractState()
    )

    async def _enrich(rows):
        return rows

    rt.enrich_rows_with_dom_links = _enrich

    def _progress(rows, accepted):
        rt.state.total_extracted_rows += accepted
        return accepted, rt.state.total_extracted_rows

    rt.record_extract_progress = _progress

    async def _api(rows, *, source):
        if api_applied:
            return {"applied": True, "fast_path": {"rows": [{"api": 1}]}}
        return {"applied": False}

    rt.try_dom_api_fast_path = _api

    async def _snap(**kw):
        return "snap/path"

    rt.save_extraction_snapshot = _snap
    return rt


def test_persist_basic_dataset(monkeypatch):
    rt = _mk_persist_runtime(monkeypatch)
    res = asyncio.run(
        rt.persist_extracted_batch(
            extracted=[{"a": 1}, {"a": 2}],
            new_rows=2,
            dup_rows=0,
            rejected_rows=0,
            log_extract_text_source="DOM_LIST",
            source_text_for_validation="src",
            candidates=[],
            data_shape={},
            current_url="http://x",
            current_extract_page_key="http://x#k",
            step=1,
        )
    )
    assert res.extracted == [{"a": 1}, {"a": 2}]
    assert res.saved_path == "out.xlsx"
    assert res.snapshot_path == "snap/path"
    assert res.progress_new_rows == 2
    assert res.progress_total_rows == 2
    assert rt.state.extract_count == 1
    assert "http://x" in rt.state.extracted_page_urls
    assert "http://x#k" in rt.state.extracted_page_keys


def test_persist_answer_mode_skips_save(monkeypatch):
    rt = _mk_persist_runtime(monkeypatch, goal_output_mode="answer")
    res = asyncio.run(
        rt.persist_extracted_batch(
            extracted=[{"a": 1}],
            new_rows=1,
            dup_rows=0,
            rejected_rows=0,
            log_extract_text_source="DOM_LIST",
            source_text_for_validation="",
            candidates=[],
            data_shape={},
            current_url="http://x",
            current_extract_page_key="http://x#k",
            step=1,
        )
    )
    assert res.saved_path == ""  # answer mode does not write a dataset artifact


def test_persist_api_fast_path_replaces_rows(monkeypatch):
    state = ExtractState(total_extracted_rows=7)
    rt = _mk_persist_runtime(monkeypatch, api_applied=True, state=state)
    res = asyncio.run(
        rt.persist_extracted_batch(
            extracted=[{"a": 1}],
            new_rows=1,
            dup_rows=0,
            rejected_rows=0,
            log_extract_text_source="DOM_LIST",
            source_text_for_validation="",
            candidates=[],
            data_shape={},
            current_url="http://x",
            current_extract_page_key="http://x#k",
            step=1,
        )
    )
    assert res.extracted == [{"api": 1}]
    assert res.progress_total_rows == 8

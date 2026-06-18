"""Regression tests for the extraction runtime carved out of main.run_agent.

S1a: ``ExtractState`` groups the ~19 run-level extraction counters that the
main step loop and (later) ``ExtractRuntime`` both read and write, so they can
share a single mutable instance instead of free closure variables.
"""

from visual_web_agent.extraction_engine.runtime import ExtractState


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

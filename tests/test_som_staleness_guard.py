"""Unit tests for som_staleness_guard."""

from __future__ import annotations

from visual_web_agent.som_staleness_guard import (
    _default_url_normalizer,
    detect_stale_som_reference,
)


# ── URL normalizer ─────────────────────────────────────────────────────────────
def test_normalizer_strips_query_and_fragment() -> None:
    assert (
        _default_url_normalizer("https://www.bing.com/search?q=x&y=1#frag")
        == "https://www.bing.com/search"
    )


def test_normalizer_trailing_slash() -> None:
    assert (
        _default_url_normalizer("https://www.bing.com/")
        == "https://www.bing.com"
    )


# ── Core staleness detection ───────────────────────────────────────────────────
def _decision(action: str, tid: int) -> dict:
    return {
        "action": action,
        "target_id": tid,
        "type_value": "",
        "thought": "",
        "status": "success",
        "memory_key": "",
    }


def test_detects_cross_page_target_id_reuse() -> None:
    """The Bing failure mode: @e21 = first result on bing, then VLM lands on
    aidaxue.com and still emits target_id=21."""
    history = [
        ("click_new_tab", 21, "", "https://www.bing.com/search"),
        ("switch_tab", 0, "", "https://www.bing.com/search"),
    ]
    msg = detect_stale_som_reference(
        _decision("click", 21),
        history,
        "https://www.aidaxue.com/course/list",
    )
    assert msg is not None
    assert "target_id=21" in msg
    assert "bing.com" in msg


def test_no_warn_when_same_url() -> None:
    """Same target_id, same page → that's regular LOOP GUARD territory; not
    our concern here."""
    history = [
        ("click", 21, "", "https://www.bing.com/search"),
    ]
    msg = detect_stale_som_reference(
        _decision("click", 21),
        history,
        "https://www.bing.com/search",
    )
    assert msg is None


def test_click_new_tab_source_page_can_reuse_same_current_id() -> None:
    """click_new_tab must be recorded against the page where @eN was seen.

    If @e19 on Bing opens aidaxue in a new tab, returning to Bing and using the
    current page's @e19 again is not stale.
    """
    history = [
        ("click_new_tab", 19, "", "https://www.bing.com/search"),
    ]
    msg = detect_stale_som_reference(
        _decision("click_new_tab", 19),
        history,
        "https://www.bing.com/search",
    )
    assert msg is None


def test_no_warn_when_target_id_zero() -> None:
    """target_id=0 means "no element" — used for switch_tab, etc."""
    history = [("click", 21, "", "https://www.aidaxue.com/")]
    msg = detect_stale_som_reference(
        _decision("click", 0),
        history,
        "https://www.bing.com/",
    )
    assert msg is None


def test_no_warn_when_target_id_not_in_history() -> None:
    history = [("click", 33, "", "https://other.example/")]
    msg = detect_stale_som_reference(
        _decision("click", 21),
        history,
        "https://www.bing.com/",
    )
    assert msg is None


def test_no_warn_for_non_element_actions() -> None:
    """switch_tab/wait/done don't act on a DOM target — exempt."""
    history = [("click", 21, "", "https://www.bing.com/")]
    for meta_action in ("switch_tab", "wait", "done", "ask_human", "goto", "extract"):
        msg = detect_stale_som_reference(
            _decision(meta_action, 21),
            history,
            "https://www.aidaxue.com/",
        )
        assert msg is None, meta_action


def test_window_bounds_history_lookback() -> None:
    """Older-than-window entries should not trigger the warning."""
    history = [
        ("click", 21, "", "https://a.example/"),  # too far back
        ("click", 7, "", "https://b.example/"),
        ("click", 8, "", "https://c.example/"),
        ("click", 9, "", "https://d.example/"),
        ("click", 10, "", "https://e.example/"),
    ]
    msg = detect_stale_som_reference(
        _decision("click", 21),
        history,
        "https://current.example/",
        window=4,
    )
    assert msg is None


def test_subsequent_decision_still_blocked_in_window() -> None:
    """The classic Bing pattern: VLM cites the same stale ID over multiple
    consecutive steps; the guard should keep firing until the stale id leaves
    the rolling window."""
    history = [
        ("click_new_tab", 21, "", "https://www.bing.com/search"),
        ("switch_tab", 0, "", "https://www.bing.com/search"),
        ("click", 21, "", "https://www.aidaxue.com/"),  # already mistakenly fired
    ]
    msg = detect_stale_som_reference(
        _decision("click", 21),
        history,
        "https://www.aidaxue.com/course",  # still on third-party land
    )
    # Bing snapshot of @e21 still in window → fires
    assert msg is not None


def test_url_with_query_string_not_false_positive() -> None:
    """Query nonce / msclkid timestamps must not split same URL into two keys."""
    history = [
        ("click", 21, "", "https://www.bing.com/search?q=x&msclkid=abc"),
    ]
    msg = detect_stale_som_reference(
        _decision("click", 21),
        history,
        "https://www.bing.com/search?q=x&msclkid=def",  # same path, diff query
    )
    assert msg is None


def test_invalid_target_id_in_decision() -> None:
    """Garbage target_id should be ignored gracefully (no crash, no warn)."""
    history = [("click", 21, "", "https://www.bing.com/")]
    msg = detect_stale_som_reference(
        {"action": "click", "target_id": "not-a-number"},
        history,
        "https://www.aidaxue.com/",
    )
    assert msg is None


def test_history_entries_missing_url_skipped() -> None:
    """Old click_point entries have empty URL; should not crash or false-trigger."""
    history = [
        ("click_point", 0, (5, 7), ""),
        ("click", 21, "", "https://www.bing.com/search"),
    ]
    msg = detect_stale_som_reference(
        _decision("click", 21),
        history,
        "https://www.aidaxue.com/",
    )
    assert msg is not None


def test_fetch_links_batch_checks_target_ids_inside_json() -> None:
    history = [
        ("click_new_tab", 21, "", "https://www.bing.com/search"),
    ]
    msg = detect_stale_som_reference(
        {
            "action": "fetch_links_batch",
            "target_id": 0,
            "type_value": '{"target_ids":[21,32],"mode":"ax"}',
        },
        history,
        "https://www.aidaxue.com/course",
    )
    assert msg is not None
    assert "target_id=21,32" in msg


def test_fetch_links_batch_target_ids_on_same_url_are_allowed() -> None:
    history = [
        ("click_new_tab", 21, "", "https://www.bing.com/search"),
    ]
    msg = detect_stale_som_reference(
        {
            "action": "fetch_links_batch",
            "target_id": 0,
            "type_value": '{"target_ids":["@e21","@e32"]}',
        },
        history,
        "https://www.bing.com/search",
    )
    assert msg is None

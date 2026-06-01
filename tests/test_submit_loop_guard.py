"""Catch redundant submit-button clicks after the URL has already changed.

This is the active-click counterpart to ``wait_loop_guard``: when the VLM
doesn't trust its own success and re-clicks the SAME ``target_id`` on the
SAME action type, but the URL between attempts has actually navigated.

The existing LOOP GUARD resets its counter on URL change, so it never
fires for the genuine "successful submit, retry anyway" pattern from
run_log_20260514_103433.
"""

from __future__ import annotations

import pytest

from visual_web_agent.submit_loop_guard import (
    _default_url_normalizer,
    detect_redundant_click_after_navigation,
)


def _decision(action: str, tid: int, type_value: str = "") -> dict:
    return {
        "action": action,
        "target_id": tid,
        "type_value": type_value,
        "thought": "",
        "memory_key": "",
        "status": "success",
    }


# ── URL normalizer keeps the navigation-detection signal intact ───────────────
def test_normalizer_keeps_path_drops_query() -> None:
    """The signal we want is path/host change; query nonces shouldn't pollute."""
    assert (
        _default_url_normalizer("https://bing.com/search?q=x&nonce=1")
        == "https://bing.com/search"
    )
    # Trailing slash dropped so home vs. home/ aren't treated as different
    assert _default_url_normalizer("https://bing.com/") == "https://bing.com"


# ── Core: same click on same target after URL changed ─────────────────────────
def test_fires_on_redundant_click_after_navigation() -> None:
    """The Bing 10:34 failure mode: click @e2 (搜索) on bing.com/ then again
    on bing.com/search?q=Python教程 → guard fires."""
    history = [
        ("click", 2, "", "https://cn.bing.com/"),
    ]
    msg = detect_redundant_click_after_navigation(
        _decision("click", 2),
        history,
        "https://cn.bing.com/search?q=Python%E6%95%99%E7%A8%8B",
    )
    assert msg is not None
    assert "SUBMIT LOOP" in msg
    assert "target_id=2" in msg
    assert "/search" in msg
    assert "查询参数" in msg  # the smoking-gun line surfaced


def test_fires_for_click_text_variant() -> None:
    history = [
        ("click_text", 5, "", "https://example.com/"),
    ]
    msg = detect_redundant_click_after_navigation(
        _decision("click_text", 5, type_value="Submit"),
        history,
        "https://example.com/results",
    )
    assert msg is not None
    assert "click_text" in msg
    assert "'Submit'" in msg


def test_fires_for_click_new_tab_variant() -> None:
    history = [
        ("click_new_tab", 8, "", "https://search.example/?q=python"),
    ]
    msg = detect_redundant_click_after_navigation(
        _decision("click_new_tab", 8),
        history,
        # Navigation post-click: URL path changed
        "https://search.example/results?q=python",
    )
    assert msg is not None
    assert "click_new_tab" in msg


# ── Negative: SAME URL → not our problem ──────────────────────────────────────
def test_does_not_fire_when_url_unchanged() -> None:
    """Same page, same click → that's LOOP GUARD's territory."""
    history = [
        ("click", 2, "", "https://example.com/page"),
    ]
    msg = detect_redundant_click_after_navigation(
        _decision("click", 2),
        history,
        "https://example.com/page",
    )
    assert msg is None


def test_does_not_fire_when_only_query_changed() -> None:
    """Query nonces (timestamps, msclkid, etc.) on the same path shouldn't
    look like a navigation."""
    history = [
        ("click", 2, "", "https://example.com/page?q=x&t=1"),
    ]
    msg = detect_redundant_click_after_navigation(
        _decision("click", 2),
        history,
        "https://example.com/page?q=x&t=2",
    )
    assert msg is None


def test_does_not_fire_for_different_target_id() -> None:
    """Each click on a different element is legitimate."""
    history = [
        ("click", 5, "", "https://example.com/a"),
    ]
    msg = detect_redundant_click_after_navigation(
        _decision("click", 7),
        history,
        "https://example.com/b",
    )
    assert msg is None


def test_does_not_fire_for_different_action_type() -> None:
    """Switching click → click_text on same id might be a legitimate fallback
    strategy, not a 'doesn't trust success' loop."""
    history = [
        ("click", 5, "", "https://example.com/a"),
    ]
    msg = detect_redundant_click_after_navigation(
        _decision("click_text", 5),
        history,
        "https://example.com/b",
    )
    assert msg is None


@pytest.mark.parametrize(
    "non_click_action",
    ["type", "hover", "scroll", "press_key", "wait", "done", "switch_tab"],
)
def test_does_not_fire_for_non_click_actions(non_click_action: str) -> None:
    """Only click variants are this guard's concern."""
    history = [
        (non_click_action, 5, "", "https://example.com/a"),
    ]
    msg = detect_redundant_click_after_navigation(
        _decision(non_click_action, 5),
        history,
        "https://example.com/b",
    )
    assert msg is None


def test_does_not_fire_with_zero_target_id() -> None:
    """target_id=0 → other guards (zero-target, click_text-by-text) own this."""
    history = [
        ("click", 0, "", "https://example.com/a"),
    ]
    msg = detect_redundant_click_after_navigation(
        _decision("click", 0),
        history,
        "https://example.com/b",
    )
    assert msg is None


def test_does_not_fire_with_empty_history() -> None:
    """First step ever — nothing to compare to."""
    msg = detect_redundant_click_after_navigation(
        _decision("click", 5),
        [],
        "https://example.com/",
    )
    assert msg is None


def test_does_not_fire_when_last_entry_is_malformed() -> None:
    """Defensive: short tuples or None entries shouldn't crash."""
    history = [
        None,
        ("click",),  # too short
        ("click", 5, ""),  # still too short (need 4 elements)
    ]
    msg = detect_redundant_click_after_navigation(
        _decision("click", 5),
        history,
        "https://example.com/",
    )
    assert msg is None


# ── Looks back only at the IMMEDIATELY PREVIOUS step ─────────────────────────
def test_inspects_only_last_action_not_full_history() -> None:
    """If the PREVIOUS step was a different click (not this target), don't
    fire — VLM is moving forward, not retrying."""
    history = [
        ("click", 2, "", "https://bing.com/"),       # 2 steps ago: same id
        ("click_text", 0, "", "https://bing.com/"),  # 1 step ago: different
    ]
    msg = detect_redundant_click_after_navigation(
        _decision("click", 2),
        history,
        "https://bing.com/search?q=x",  # URL changed but not after step 2
    )
    assert msg is None


def test_inspects_last_entry_even_after_many_history_entries() -> None:
    """Long history fine — only the tail matters."""
    history = [
        ("type", 14, "", "https://bing.com/"),
        ("press_key", 0, "", "https://bing.com/"),
        ("click", 2, "", "https://bing.com/"),  # last
    ]
    msg = detect_redundant_click_after_navigation(
        _decision("click", 2),
        history,
        "https://cn.bing.com/search?q=python",
    )
    assert msg is not None


# ── Custom URL normalizer respected ───────────────────────────────────────────
def test_custom_url_normalizer_applied() -> None:
    """Caller-provided normalizer overrides the default — main.py passes
    its own to keep behavior consistent with other guards."""
    def _always_same(url: str) -> str:
        return "X"
    history = [("click", 2, "", "https://bing.com/")]
    msg = detect_redundant_click_after_navigation(
        _decision("click", 2),
        history,
        "https://bing.com/search?q=x",
        url_normalizer=_always_same,
    )
    # Same normalized key → no fire
    assert msg is None


def test_smoking_gun_query_param_detected_for_chinese_search_engine() -> None:
    """Baidu uses ``wd``, Bing uses ``q``, Sogou uses ``query`` — guard
    surfaces the smoking gun for all of them."""
    cases = [
        ("https://baidu.com/s?wd=test", True),
        ("https://cn.bing.com/search?q=test", True),
        ("https://sogou.com/web?query=test", True),
        ("https://example.com/page?ref=email", False),  # no search-y key
    ]
    for current_url, should_have_evidence in cases:
        history = [("click", 2, "", "https://example.com/")]
        msg = detect_redundant_click_after_navigation(
            _decision("click", 2), history, current_url
        )
        assert msg is not None, f"should fire for {current_url}"
        if should_have_evidence:
            assert "查询参数" in msg or "submit" in msg.lower(), current_url
        else:
            assert "查询参数" not in msg, current_url

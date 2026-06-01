"""Switch-tab loop guard: third sibling of wait_loop_guard / submit_loop_guard.

Failure mode covered (run_log_20260514_183718, steps 10-18): VLM emits
``switch_tab(1)`` six times in a row, each one succeeds at the Playwright
layer, but VLM's mental model is wrong about *what's on tab 1* — so it
keeps re-emitting expecting a different outcome. The first switch_tab is
a legitimate retry; the second is the loop signal.
"""

from __future__ import annotations

import pytest

from visual_web_agent.switch_tab_loop_guard import (
    _read_target_id,
    detect_switch_tab_loop,
)


def _decision(action: str, target_id: int, type_value: str = "") -> dict:
    return {
        "action": action,
        "target_id": target_id,
        "type_value": type_value,
        "thought": "",
        "status": "success",
        "memory_key": "",
    }


# ── _read_target_id helper accepts both conventions ──────────────────────────
def test_read_target_id_from_target_id_field() -> None:
    assert _read_target_id({"action": "switch_tab", "target_id": 1}) == 1
    assert _read_target_id({"action": "switch_tab", "target_id": 5}) == 5


def test_read_target_id_from_type_value_when_target_id_zero() -> None:
    """The standard convention: switch_tab(target_id=0, type_value='3')."""
    assert _read_target_id({"action": "switch_tab", "target_id": 0, "type_value": "3"}) == 3


def test_read_target_id_recognizes_tab_zero() -> None:
    """target_id=0 + type_value='0' means switch to tab 0 (NOT 'no element')."""
    assert _read_target_id({"action": "switch_tab", "target_id": 0, "type_value": "0"}) == 0


def test_read_target_id_rejects_garbage() -> None:
    assert _read_target_id({"action": "switch_tab", "target_id": 0, "type_value": ""}) is None
    assert _read_target_id({"action": "switch_tab", "target_id": "abc"}) is None
    assert _read_target_id("not a dict") is None  # type: ignore[arg-type]


# ── Core: consecutive switch_tab(same N) ─────────────────────────────────────
def test_fires_on_two_consecutive_switch_tab_same_target() -> None:
    """Threshold=2 (default): one repeat is enough to be a loop signal."""
    history = [
        ("switch_tab", 1, "", "https://bing.com/"),
    ]
    msg = detect_switch_tab_loop(
        _decision("switch_tab", 1),
        history,
    )
    assert msg is not None
    assert "SWITCH TAB LOOP" in msg
    assert "switch_tab(target_id=1)" in msg
    assert "[1]" in msg


def test_fires_on_long_consecutive_run() -> None:
    """The Wenxin 18:37 failure: 6 in a row. Still fires.

    NOTE: the guard short-circuits at ``threshold`` (default 2); it does NOT
    keep counting beyond that — once you know it's a loop, no point in
    counting higher. So the message always reads "连续 2 次..." after the
    threshold is met. This is intentional (cheaper detection)."""
    history = [
        ("type", 14, "", "https://baidu.com/"),  # unrelated earlier action
        ("switch_tab", 1, "", "https://baidu.com/"),
        ("switch_tab", 1, "", "https://yiyan.baidu.com/"),
        ("switch_tab", 1, "", "https://baidu.com/?wd=x"),
    ]
    msg = detect_switch_tab_loop(_decision("switch_tab", 1), history)
    assert msg is not None
    # The guard reports the threshold-2 count; user-facing message confirms loop.
    assert "连续" in msg
    assert "switch_tab(target_id=1)" in msg


def test_fires_with_type_value_form() -> None:
    """``switch_tab(target_id=0, type_value='1')`` is the canonical form."""
    history = [
        ("switch_tab", 1, "", "https://example.com/"),
    ]
    msg = detect_switch_tab_loop(
        _decision("switch_tab", 0, type_value="1"),
        history,
    )
    assert msg is not None


# ── Negative: doesn't fire when it shouldn't ─────────────────────────────────
def test_does_not_fire_on_first_switch_tab() -> None:
    """Single switch_tab — not a loop yet."""
    msg = detect_switch_tab_loop(_decision("switch_tab", 1), [])
    assert msg is None


def test_does_not_fire_when_last_was_different_target() -> None:
    """switch_tab(1) → switch_tab(2) is normal navigation, not a loop."""
    history = [("switch_tab", 1, "", "https://example.com/")]
    msg = detect_switch_tab_loop(_decision("switch_tab", 2), history)
    assert msg is None


def test_does_not_fire_when_last_was_non_switch_action() -> None:
    """switch_tab(1) sandwiched between other actions — VLM did try
    something else in between, so it's not a stuck loop."""
    history = [
        ("switch_tab", 1, "", "https://example.com/"),
        ("click", 5, "", "https://example.com/"),  # tried something else
    ]
    msg = detect_switch_tab_loop(_decision("switch_tab", 1), history)
    assert msg is None


def test_does_not_fire_for_non_switch_tab_action() -> None:
    """Other action types are handled by other guards."""
    history = [
        ("switch_tab", 1, "", "https://example.com/"),
    ]
    msg = detect_switch_tab_loop(_decision("click", 1), history)
    assert msg is None


def test_does_not_fire_when_head_target_id_invalid() -> None:
    """Garbage decisions → safely skip."""
    history = [("switch_tab", 1, "", "https://example.com/")]
    msg = detect_switch_tab_loop(
        {"action": "switch_tab", "target_id": "garbage", "type_value": "nope"},
        history,
    )
    assert msg is None


def test_threshold_one_treated_as_disabled() -> None:
    """threshold=1 would mean 'fire on every switch_tab' — defensive: skip."""
    msg = detect_switch_tab_loop(
        _decision("switch_tab", 1), [], threshold=1
    )
    assert msg is None


def test_robust_to_malformed_history_entries() -> None:
    """Older/legacy history rows might be too short — don't crash."""
    history = [
        None,
        ("switch_tab",),  # too short
        ("switch_tab", 1, ""),  # 3 elements, still legal because we only need [0],[1]
    ]
    msg = detect_switch_tab_loop(_decision("switch_tab", 1), history)
    # The 3-element entry IS valid (we only read [0] and [1]), so it counts.
    assert msg is not None


# ── Feedback content quality ─────────────────────────────────────────────────
def test_feedback_offers_concrete_alternatives() -> None:
    """The whole point of the guard: tell VLM to STOP and do something else."""
    history = [("switch_tab", 1, "", "https://x.example/")]
    msg = detect_switch_tab_loop(_decision("switch_tab", 1), history)
    assert msg is not None
    # Concrete alternative actions named
    assert "close_tab" in msg
    assert "goto" in msg
    assert "done" in msg
    # Tells the model status=success is real
    assert "status=success" in msg


def test_feedback_includes_target_index() -> None:
    """Specific tab index makes the feedback actionable."""
    history = [("switch_tab", 3, "", "https://x.example/")]
    msg = detect_switch_tab_loop(_decision("switch_tab", 3), history)
    assert msg is not None
    assert "target_id=3" in msg
    assert "[3]" in msg

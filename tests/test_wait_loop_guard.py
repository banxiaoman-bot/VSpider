from visual_web_agent.wait_loop_guard import WaitLoopTracker, is_noop_wait


def test_is_noop_wait_short_target_zero() -> None:
    assert is_noop_wait({"action": "wait", "target_id": 0, "type_value": "1"})
    assert is_noop_wait({"action": "wait", "target_id": 0, "type_value": "3"})


def test_is_noop_wait_rejects_long_or_targeted_waits() -> None:
    assert not is_noop_wait({"action": "wait", "target_id": 0, "type_value": "5"})
    assert not is_noop_wait({"action": "wait", "target_id": 2, "type_value": "1"})
    assert not is_noop_wait({"action": "click", "target_id": 0, "type_value": "1"})


def test_tracker_triggers_on_third_same_page_short_wait() -> None:
    tracker = WaitLoopTracker(threshold=3)
    decision = {"action": "wait", "target_id": 0, "type_value": "1"}

    assert tracker.observe(decision, "https://example.com/search?q=a") is None
    assert tracker.observe(decision, "https://example.com/search?q=b") is None
    msg = tracker.observe(decision, "https://example.com/search?q=c")

    assert msg is not None
    assert "Repeated short wait loop" in msg
    assert "verify current URL" in msg or "re-verify current URL" in msg


def test_tracker_resets_on_different_page_path() -> None:
    tracker = WaitLoopTracker(threshold=3)
    decision = {"action": "wait", "target_id": 0, "type_value": "1"}

    assert tracker.observe(decision, "https://example.com/a") is None
    assert tracker.observe(decision, "https://example.com/a") is None
    assert tracker.observe(decision, "https://example.com/b") is None


def test_tracker_resets_on_non_wait_action() -> None:
    tracker = WaitLoopTracker(threshold=3)
    wait = {"action": "wait", "target_id": 0, "type_value": "1"}

    tracker.observe(wait, "https://example.com/a")
    tracker.observe(wait, "https://example.com/a")
    assert tracker.observe({"action": "click", "target_id": 1}, "https://example.com/a") is None
    assert tracker.observe(wait, "https://example.com/a") is None


def test_tracker_adds_zero_target_hint() -> None:
    tracker = WaitLoopTracker(threshold=1)
    msg = tracker.observe(
        {
            "action": "wait",
            "target_id": 0,
            "type_value": "1",
            "thought": "[ZERO_TARGET_DOWNGRADE] target_id=0",
        },
        "https://example.com/a",
    )

    assert msg is not None
    assert "zero-target" in msg.lower()

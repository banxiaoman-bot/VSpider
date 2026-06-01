"""Tests for unified task completion evaluation (COMP-1)."""

from __future__ import annotations

from visual_web_agent.completion_kernel import (
    evaluate_completion,
    maybe_short_circuit_decision,
)


def test_extract_complete_when_rows_meet_target() -> None:
    result = evaluate_completion(
        goal="提取20条评论",
        output_mode="artifact",
        output_contract={"output_kind": "dataset_rows", "container": "csv"},
        total_extracted_rows=20,
        goal_target_count=20,
    )
    assert result["status"] == "complete"
    assert result["recommended_action"] == "done"
    assert any(item["name"] == "extract_row_target" and item["passed"] for item in result["checks"])


def test_extract_complete_with_tolerance_when_pagination_exhausted() -> None:
    result = evaluate_completion(
        goal="提取100条",
        output_mode="artifact",
        total_extracted_rows=97,
        goal_target_count=100,
        pagination_exhausted=True,
    )
    assert result["status"] == "complete"
    assert result["confidence"] >= 0.8


def test_answer_complete_when_workflow_memory_has_answer() -> None:
    result = evaluate_completion(
        goal="页面上的价格是多少",
        output_mode="answer",
        output_contract={"output_kind": "answer_text", "mode": "answer"},
        workflow_memory={"final_answer": "￥1299"},
    )
    assert result["status"] == "complete"


def test_media_complete_when_manifest_has_items() -> None:
    result = evaluate_completion(
        goal="下载页面图片",
        output_mode="artifact",
        output_contract={"output_kind": "media_image", "container": "files_folder"},
        manifest_items=[{"kind": "media_image", "path": "runs/x/artifacts/a.png", "sha256": "abc"}],
    )
    assert result["status"] == "complete"


def test_continue_when_extract_below_target() -> None:
    result = evaluate_completion(
        goal="提取50条",
        output_mode="artifact",
        total_extracted_rows=12,
        goal_target_count=50,
        pagination_exhausted=False,
    )
    assert result["status"] == "continue"
    assert result["recommended_action"] == "continue"


def test_short_circuit_scroll_when_already_complete() -> None:
    decision = {"action": "scroll", "target_id": 0, "thought": "继续下滑"}
    out = maybe_short_circuit_decision(
        decision,
        goal="提取10条",
        output_mode="artifact",
        total_extracted_rows=10,
        goal_target_count=10,
    )
    assert out["short_circuit"] is True
    assert out["decision"]["action"] == "done"
    assert out["decision"].get("__completion_kernel")


def test_no_short_circuit_for_terminal_actions() -> None:
    decision = {"action": "extract", "target_id": 0}
    out = maybe_short_circuit_decision(
        decision,
        goal="提取10条",
        output_mode="artifact",
        total_extracted_rows=10,
        goal_target_count=10,
    )
    assert out["short_circuit"] is False


def test_no_progress_tracker_marks_exhaustion_after_repeated_scroll() -> None:
    from visual_web_agent.completion_kernel import NoProgressTracker

    tracker = NoProgressTracker(exhaust_threshold=2)
    tracker.observe(action="scroll", row_count=5)
    assert tracker.observe(action="scroll", row_count=5)["streak"] == 1
    out = tracker.observe(action="scroll", row_count=5)
    assert out["streak"] == 2
    assert out["pagination_exhausted"] is True


def test_no_progress_resets_when_rows_increase() -> None:
    from visual_web_agent.completion_kernel import NoProgressTracker

    tracker = NoProgressTracker()
    tracker.observe(action="scroll", row_count=3)
    tracker.observe(action="scroll", row_count=3)
    assert tracker.streak == 1
    tracker.observe(action="scroll", row_count=3)
    assert tracker.streak == 2
    tracker.observe(action="extract", row_count=8)
    assert tracker.streak == 0


def test_no_progress_complete_near_target_with_streak() -> None:
    result = evaluate_completion(
        goal="提取100条",
        output_mode="artifact",
        total_extracted_rows=98,
        goal_target_count=100,
        pagination_exhausted=False,
        no_progress_streak=2,
    )
    assert result["status"] == "complete"

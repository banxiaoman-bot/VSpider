"""Tests for unified task completion evaluation (COMP-1)."""

from __future__ import annotations

from visual_web_agent.completion_kernel import (
    evaluate_completion,
    load_manifest_items_for_run,
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


def test_dataset_manifest_complete_when_no_explicit_row_target() -> None:
    result = evaluate_completion(
        goal="导出当前表格",
        output_mode="artifact",
        output_contract={"output_kind": "dataset_records", "container": "jsonl"},
        manifest_items=[
            {"kind": "dataset_records", "path": "runs/x/artifacts/data.jsonl"},
        ],
    )
    assert result["status"] == "complete"
    assert "dataset_manifest_ready" in result["reasons"]
    assert "dataset_manifest:dataset_records" in result["evidence"]


def test_dataset_manifest_waits_for_row_target_without_count_evidence() -> None:
    result = evaluate_completion(
        goal="导出前20条数据",
        output_mode="artifact",
        output_contract={"output_kind": "dataset_rows", "container": "xlsx"},
        goal_target_count=20,
        total_extracted_rows=0,
        manifest_items=[
            {"kind": "dataset_rows", "path": "runs/x/artifacts/data.xlsx"},
        ],
    )
    assert result["status"] == "continue"
    assert "manifest_ready" in result["reasons"]
    assert "dataset_manifest_ready" not in result["reasons"]


def test_dataset_manifest_row_count_can_satisfy_explicit_target() -> None:
    result = evaluate_completion(
        goal="导出前20条数据",
        output_mode="artifact",
        output_contract={"output_kind": "dataset_rows", "container": "xlsx"},
        goal_target_count=20,
        manifest_items=[
            {
                "kind": "dataset_rows",
                "path": "runs/x/artifacts/data.xlsx",
                "extra": {"row_count": 20},
            },
        ],
    )
    assert result["status"] == "complete"
    assert "dataset_manifest_ready" in result["reasons"]


def test_page_target_complete_when_pages_meet_target() -> None:
    result = evaluate_completion(
        goal="extract first 3 pages",
        output_mode="artifact",
        output_contract={"output_kind": "dataset_records", "container": "jsonl"},
        total_pages=3,
        goal_target_pages=3,
    )

    assert result["status"] == "complete"
    assert "extract_page_target_met" in result["reasons"]
    assert "extract_pages:3/3" in result["evidence"]
    assert any(item["name"] == "extract_page_target" and item["passed"] for item in result["checks"])


def test_dataset_manifest_waits_for_page_target_until_pages_meet_target() -> None:
    result = evaluate_completion(
        goal="extract first 3 pages",
        output_mode="artifact",
        output_contract={"output_kind": "dataset_records", "container": "jsonl"},
        total_pages=1,
        goal_target_pages=3,
        manifest_items=[
            {"kind": "dataset_records", "path": "runs/x/artifacts/data.jsonl"},
        ],
    )

    assert result["status"] == "continue"
    assert "manifest_ready" in result["reasons"]
    assert "dataset_manifest_ready" not in result["reasons"]


def test_dataset_manifest_requires_contract_fields() -> None:
    result = evaluate_completion(
        goal="export title and price",
        output_mode="artifact",
        output_contract={
            "output_kind": "dataset_rows",
            "container": "csv",
            "fields": ["title", "price"],
        },
        goal_target_count=2,
        total_extracted_rows=2,
        manifest_items=[
            {
                "kind": "dataset_rows",
                "path": "runs/x/artifacts/data.csv",
                "extra": {"row_count": 2, "fields": ["title"]},
            },
        ],
    )
    assert result["status"] == "continue"
    assert "manifest_ready" in result["reasons"]
    assert "dataset_manifest_ready" not in result["reasons"]
    field_check = next(item for item in result["checks"] if item["name"] == "manifest_dataset_fields")
    assert field_check["passed"] is False
    assert field_check["missing"] == ["price"]


def test_dataset_manifest_fields_from_route_can_satisfy_completion() -> None:
    result = evaluate_completion(
        goal="export requested fields",
        output_mode="artifact",
        output_contract={"output_kind": "dataset_records", "container": "jsonl"},
        capability_route={
            "strategy_context": {
                "requested_fields": ["title", "price"],
            },
        },
        manifest_items=[
            {
                "kind": "dataset_records",
                "path": "runs/x/artifacts/data.jsonl",
                "extra": {"row_count": 2, "fields": ["Title", "price"]},
            },
        ],
    )
    assert result["status"] == "complete"
    assert "dataset_manifest_ready" in result["reasons"]
    assert "manifest_fields:title,price" in result["evidence"]


def test_load_manifest_items_for_run_reads_manifest_dataclass(tmp_path) -> None:
    from visual_web_agent.io_contract.persistence import append_manifest_item

    append_manifest_item(
        "run_load_manifest",
        kind="dataset_records",
        path=str(tmp_path / "run_load_manifest" / "artifacts" / "data.jsonl"),
        size=12,
        sha256="abc",
        extra={"row_count": 1, "fields": ["title"]},
        base_dir=tmp_path,
    )

    items = load_manifest_items_for_run("run_load_manifest", base_dir=tmp_path)
    assert len(items) == 1
    assert items[0]["kind"] == "dataset_records"
    assert items[0]["extra"]["row_count"] == 1
    assert items[0]["extra"]["fields"] == ["title"]


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


def test_short_circuit_wait_when_dataset_manifest_ready() -> None:
    decision = {"action": "wait", "target_id": 0, "thought": "等待导出完成"}
    out = maybe_short_circuit_decision(
        decision,
        goal="导出当前表格",
        output_mode="artifact",
        output_contract={"output_kind": "dataset_records", "container": "jsonl"},
        manifest_items=[
            {"kind": "dataset_records", "path": "runs/x/artifacts/data.jsonl"},
        ],
    )
    assert out["short_circuit"] is True
    assert out["decision"]["action"] == "done"
    assert "dataset_manifest:dataset_records" in out["evaluation"]["evidence"]


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

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


def test_dataset_rows_not_complete_when_subgoal_exit_passes_but_no_rows() -> None:
    """空抽取守卫：dataset_rows 任务即便子目标退出条件(page_ready)满足，
    若 0 行且 manifest 为空且未真正穷尽，则不得判定完成（防止提前 done）。"""
    result = evaluate_completion(
        goal="提取首页所有名言的文字和对应作者",
        output_mode="artifact",
        output_contract={
            "output_kind": "dataset_rows",
            "container": "xlsx",
            "artifact_required": True,
            "answer_required": False,
        },
        total_extracted_rows=0,
        manifest_items=[],
        exit_criteria=[{"type": "page_ready"}],
        current_url="https://quotes.toscrape.com",
    )
    assert result["status"] == "continue"
    assert result["recommended_action"] == "continue"


def test_dataset_rows_subgoal_exit_completes_after_rows_extracted() -> None:
    """有数据后，子目标退出条件可正常判定完成（守卫不误伤正常完成）。"""
    result = evaluate_completion(
        goal="提取首页所有名言的文字和对应作者",
        output_mode="artifact",
        output_contract={"output_kind": "dataset_rows", "container": "xlsx"},
        total_extracted_rows=10,
        manifest_items=[
            {
                "kind": "dataset_rows",
                "path": "runs/x/artifacts/data.xlsx",
                "extra": {"row_count": 10},
            }
        ],
        exit_criteria=[{"type": "page_ready"}],
        current_url="https://quotes.toscrape.com",
    )
    assert result["status"] == "complete"


def test_dataset_rows_subgoal_exit_allowed_when_genuinely_exhausted() -> None:
    """真正穷尽(分页耗尽)时，空结果可放行完成，避免死循环。"""
    result = evaluate_completion(
        goal="提取列表",
        output_mode="artifact",
        output_contract={"output_kind": "dataset_rows", "container": "xlsx"},
        total_extracted_rows=0,
        manifest_items=[],
        exit_criteria=[{"type": "page_ready"}],
        current_url="https://example.com",
        pagination_exhausted=True,
    )
    assert result["status"] == "complete"


def test_has_recorded_dataset_rows_helper() -> None:
    """确定性行证据判定：用于让 manifest 行数凌驾视觉 JUDGE 的视口计数。"""
    from visual_web_agent.completion_kernel import has_recorded_dataset_rows

    # 已抽到行 => True
    assert has_recorded_dataset_rows(
        {"output_kind": "dataset_rows"}, total_extracted_rows=10
    ) is True
    # manifest 记录了行数 => True
    assert has_recorded_dataset_rows(
        {"output_kind": "dataset_rows"},
        manifest_items=[{"kind": "dataset_rows", "extra": {"row_count": 5}}],
    ) is True
    # 0 行且 manifest 空 => False
    assert has_recorded_dataset_rows(
        {"output_kind": "dataset_rows"}, total_extracted_rows=0, manifest_items=[]
    ) is False
    # 非结构化抽取类 => False（不适用该豁免）
    assert has_recorded_dataset_rows(
        {"output_kind": "answer_text"}, total_extracted_rows=10
    ) is False


def test_structured_extraction_requirement_unmet_helper() -> None:
    """共享守卫函数的真值表（main.py plan gate 与 kernel 复用同一判定）。"""
    from visual_web_agent.completion_kernel import (
        structured_extraction_requirement_unmet,
    )

    # dataset_rows + 0 行 + 空 manifest + 未穷尽 => 未满足要求(True)
    assert structured_extraction_requirement_unmet(
        {"output_kind": "dataset_rows"}, total_extracted_rows=0, manifest_items=[]
    ) is True
    # 有行 => 满足
    assert structured_extraction_requirement_unmet(
        {"output_kind": "dataset_rows"}, total_extracted_rows=5
    ) is False
    # manifest 已有产物(无 row_count) => 视为有数据，满足
    assert structured_extraction_requirement_unmet(
        {"output_kind": "dataset_records"},
        manifest_items=[{"kind": "dataset_records", "path": "x.jsonl"}],
    ) is False
    # 非结构化抽取类(answer_text) => 不适用，满足
    assert structured_extraction_requirement_unmet(
        {"output_kind": "answer_text"}, total_extracted_rows=0
    ) is False
    # 分页穷尽 => 真空结果可放行，满足
    assert structured_extraction_requirement_unmet(
        {"output_kind": "dataset_rows"},
        total_extracted_rows=0,
        manifest_items=[],
        pagination_exhausted=True,
    ) is False
    # 连续无进展 >=3 => 真尝试过仍空，可放行，满足
    assert structured_extraction_requirement_unmet(
        {"output_kind": "dataset_rows"},
        total_extracted_rows=0,
        manifest_items=[],
        no_progress_streak=3,
    ) is False

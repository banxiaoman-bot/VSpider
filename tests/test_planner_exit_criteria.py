"""Tests for planner sub-goal exit criteria parsing (COMP-3)."""

from __future__ import annotations

from types import SimpleNamespace

from visual_web_agent.completion_kernel import evaluate_completion
from visual_web_agent.planner_exit_criteria import (
    current_subgoal_criteria,
    evaluate_exit_criteria,
    parse_exit_criteria,
    parse_subgoal_plan,
)


def test_parse_row_count_and_extract_done() -> None:
    criteria = parse_exit_criteria("提取20条评论并输出结构化 JSON")
    kinds = {item["type"] for item in criteria}
    assert "row_count" in kinds
    assert "extract_done" in kinds
    row = next(item for item in criteria if item["type"] == "row_count")
    assert row["target"] == 20


def test_parse_url_contains() -> None:
    criteria = parse_exit_criteria("URL 含 order-success 页面可见")
    assert any(item["type"] == "url_contains" for item in criteria)


def test_evaluate_exit_criteria_all_required() -> None:
    criteria = [
        {"type": "row_count", "target": 5},
        {"type": "answer_ready"},
    ]
    out = evaluate_exit_criteria(
        criteria,
        total_extracted_rows=5,
        workflow_memory={"final_answer": "ok"},
    )
    assert out["passed"] is True
    assert set(out["matched"]) == {"row_count", "answer_ready"}


def test_current_subgoal_criteria_uses_task_plan_index() -> None:
    plan = SimpleNamespace(
        current_idx=1,
        sub_goals=[
            SimpleNamespace(description="a", exit_criteria="", status="done"),
            SimpleNamespace(
                description="抓取10条",
                exit_criteria="提取10条",
                status="active",
            ),
        ],
    )
    criteria = current_subgoal_criteria(plan)
    assert any(item.get("type") == "row_count" and item.get("target") == 10 for item in criteria)


def test_parse_subgoal_plan_shapes() -> None:
    plan = SimpleNamespace(
        sub_goals=[
            SimpleNamespace(
                id="sg1",
                description="回答价格",
                exit_criteria="答案可见",
                status="active",
            ),
        ],
    )
    rows = parse_subgoal_plan(plan)
    assert len(rows) == 1
    assert rows[0]["exit_criteria_text"] == "答案可见"
    assert any(item["type"] == "answer_ready" for item in rows[0]["criteria"])


def test_completion_kernel_subgoal_exit_complete() -> None:
    result = evaluate_completion(
        goal="提取15条商品",
        output_mode="artifact",
        total_extracted_rows=15,
        exit_criteria=[{"type": "row_count", "target": 15}],
    )
    assert result["status"] == "complete"
    assert "subgoal_exit_met" in result["reasons"]

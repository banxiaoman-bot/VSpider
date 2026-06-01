from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook

from tests.agent_cases.run_agent_cases import (
    _classify_failure,
    _load_skill_lint,
    ROOT,
    build_case_diagnostics,
)


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def _write_xlsx(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["title", "author", "digg_count"])
    sheet.append(["A useful post", "alice", 12])
    sheet.append(["Another post", "bob", 3])
    workbook.save(path)


def test_build_case_diagnostics_summarizes_event_stream_and_excel() -> None:
    work_dir = ROOT / "workspace" / "tmp_case_diag"
    work_dir.mkdir(parents=True, exist_ok=True)
    event_path = work_dir / "event_stream_20260508_010101.jsonl"
    output_path = work_dir / "output.xlsx"
    _write_jsonl(
        event_path,
        [
            {
                "type": "tool_catalog",
                "selected_tools": [{"name": "list_extract"}, {"name": "xhr_extract"}],
            },
            {
                "type": "skill_match",
                "step": 1,
                "skill": {
                    "name": "internet_hovers_profile",
                    "action": "internet_hovers_macro",
                    "source": "INTERNET_HOVERS_PROFILE_MACRO",
                    "capability": "hover",
                },
                "metadata": {
                    "selection": {
                        "score": 148.0,
                        "reasons": ["match=true", "url_alias:the-internet.herokuapp.com/hovers"],
                        "history_success_rate": 1.0,
                        "strategy_context": {
                            "capabilities": ["hover"],
                            "preferred_modes": ["targeted_first"],
                        },
                    }
                },
            },
            {
                "type": "strategy_context",
                "step": 1,
                "context": {
                    "capabilities": ["extract"],
                    "preferred_modes": ["extract_fast_path"],
                    "fallback_order": ["pre_extract", "targeted_probe", "vlm_global"],
                },
            },
            {
                "type": "tool_dispatch",
                "step": 1,
                "action": "modal_dialog_macro",
                "tool": {"name": "modal_dialog_macro"},
            },
            {
                "type": "skill_replay",
                "step": 1,
                "skill": {
                    "name": "internet_hovers_profile",
                    "source": "INTERNET_HOVERS_PROFILE_MACRO",
                },
                "path": "workspace/skill_replays/sample.json",
                "metadata": {"score": 148.0},
            },
            {
                "type": "guard",
                "step": 2,
                "name": "EXTRACT_CLICK_GUARD",
                "message": "rewrote click to scroll",
            },
            {
                "type": "guard",
                "step": 2,
                "name": "extract_same_page_nav_guard",
                "message": "rewrote same-page tab to extract",
            },
            {
                "type": "guard",
                "step": 3,
                "name": "BATCH_POST_EXTRACT_GATE",
                "message": "remaining actions skipped",
                "metadata": {
                    "next_action": "click",
                    "rows": 20,
                    "row_target": 20,
                    "pages": 0,
                    "page_target": None,
                },
            },
            {
                "type": "verify",
                "step": 3,
                "name": "MODAL_DIALOG_MACRO_row_count",
                "success": True,
                "expected": 2,
                "observed": 2,
            },
            {
                "type": "extract",
                "step": 3,
                "source": "DOM_CARDS",
                "rows": 20,
                "output_file": str(output_path),
                "metadata": {
                    "mode": "pre_extract_fast_path",
                    "target": 20,
                    "snapshot_path": "workspace/extraction_snapshots/sample.json",
                },
            },
            {
                "type": "done",
                "step": 4,
                "success": True,
                "reason": "vlm_done",
                "message": "finished",
                "metadata": {
                    "total_rows": 20,
                    "row_target": 20,
                    "target_reached": True,
                },
            },
            {
                "type": "observe",
                "step": 4,
                "browser_state": {
                    "url": "https://example.test/feed",
                    "title": "Feed",
                    "interactive_count": 7,
                    "screenshot_path": "step_4.png",
                },
            },
            {
                "type": "run_end",
                "success": True,
                "reason": "completed",
                "metadata": {"html_log": "logs/run_log.html"},
            },
        ],
    )
    _write_xlsx(output_path)

    diagnostics = build_case_diagnostics(
        event_stream_path=event_path,
        output_path=output_path,
    )

    assert diagnostics["event_stream_file"] == event_path.name
    assert diagnostics["selected_tools"] == ["list_extract", "xhr_extract"]
    assert diagnostics["skill_matches"][0]["skill"] == "internet_hovers_profile"
    assert diagnostics["skill_matches"][0]["score"] == 148.0
    assert "match=true" in diagnostics["skill_matches"][0]["reasons"]
    assert diagnostics["skill_matches"][0]["strategy_context"]["capabilities"] == ["hover"]
    assert diagnostics["skill_replays"][0]["path"] == "workspace/skill_replays/sample.json"
    assert diagnostics["last_strategy_context"]["preferred_modes"] == ["extract_fast_path"]
    assert diagnostics["skill_summary"]["matched"] is True
    assert diagnostics["skill_summary"]["skills"] == ["internet_hovers_profile"]
    assert diagnostics["skill_summary"]["extracted_by_skill"] is False
    assert diagnostics["dispatched_tools"][0]["tool"] == "modal_dialog_macro"
    assert diagnostics["guards"][0]["name"] == "EXTRACT_CLICK_GUARD"
    assert diagnostics["batch_post_extract_gate"]["triggered"] is True
    assert diagnostics["batch_post_extract_gate"]["last_next_action"] == "click"
    assert diagnostics["batch_post_extract_gate"]["last_rows"] == 20
    assert diagnostics["universal_strategies"]["pre_extract"]["triggered"] is True
    assert diagnostics["universal_strategies"]["pre_extract"]["last_source"] == "DOM_CARDS"
    assert diagnostics["universal_strategies"]["same_page_nav_guard"]["triggered"] is True
    assert diagnostics["universal_strategies"]["dom_cards"]["used"] is True
    assert diagnostics["verifications"][0]["name"] == "MODAL_DIALOG_MACRO_row_count"
    assert diagnostics["extracts"][0]["source"] == "DOM_CARDS"
    assert diagnostics["last_done"]["reason"] == "vlm_done"
    assert diagnostics["last_done"]["metadata"]["target_reached"] is True
    assert diagnostics["macro_sources"] == []
    assert diagnostics["last_browser_state"]["url"] == "https://example.test/feed"
    assert diagnostics["run_end"]["success"] is True
    assert diagnostics["output"]["rows"] == 2
    assert diagnostics["output"]["fields"] == ["title", "author", "digg_count"]
    assert diagnostics["output"]["preview"][0]["title"] == "A useful post"
    assert diagnostics["download"]["exists"] is False


def test_classify_failure_detects_min_rows_and_missing_fields() -> None:
    assert (
        _classify_failure(
            returned=True,
            output_rows=1,
            error="",
            expected={"success": True, "min_output_rows": 2},
            diagnostics={"output": {"fields": ["title"]}},
        )
        == "min_rows_not_met"
    )
    assert (
        _classify_failure(
            returned=True,
            output_rows=2,
            error="",
            expected={"success": True, "required_fields": ["title", "author"]},
            diagnostics={"output": {"fields": ["title"]}},
        )
        == "field_missing"
    )


def test_load_skill_lint_gate_reports_default_registry() -> None:
    summary = _load_skill_lint(include_files=True, strict=True)

    assert summary["ok"] is True
    assert summary["checked"] >= 7
    assert summary["errors"] == 0
    assert summary["strict"] is True
    assert summary["include_files"] is True

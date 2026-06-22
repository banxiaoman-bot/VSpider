"""Tests for ``smart_batch_runner`` attachment dispatch + helpers.

These exercise the new ``_dispatch_attachment`` / ``_load_batch_rows_via_adapter``
/ ``_augment_prompt_with_attachment`` helpers without spinning up
Playwright. We never call ``run_smart_batch`` directly here because that
function reaches into ``visual_web_agent.main`` which depends on a live
browser stack.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import smart_batch_runner as sbr
from visual_web_agent.attachment_adapters.base import AdapterResult


class TestDispatchAttachment:
    def test_csv_routes_to_batch_rows(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
        spec, result = sbr._dispatch_attachment(str(f), "按行填表")
        assert spec is not None
        assert spec.intent == "batch_rows"
        assert result.kind == "rows"
        assert result.ok

    def test_pdf_routes_to_upload_to_page(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4\nfake")
        spec, result = sbr._dispatch_attachment(str(f), "请上传这个 PDF")
        assert spec is not None
        assert spec.intent == "upload_to_page"
        assert result.kind == "upload"
        assert result.upload_path == str(f)

    def test_pdf_read_routes_to_prompt_context(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4\nfake")
        spec, result = sbr._dispatch_attachment(str(f), "请阅读并总结这个 PDF")
        assert spec is not None
        assert spec.intent == "prompt_context"
        assert result.kind == "pdf"

    def test_excel_summarize_routes_to_spreadsheet_text(self, tmp_path: Path) -> None:
        f = tmp_path / "report.xlsx"
        pd.DataFrame({"a": [1, 2]}).to_excel(f, index=False)
        spec, result = sbr._dispatch_attachment(
            str(f), "阅读该 Excel 报告并总结要点"
        )
        assert spec.intent == "prompt_context"
        assert result.kind == "spreadsheet_text"

    def test_docx_default_is_prompt_context(self, tmp_path: Path) -> None:
        f = tmp_path / "memo.docx"
        f.write_bytes(b"PK\x03\x04fakedocx")
        spec, result = sbr._dispatch_attachment(str(f), "总结这份文档")
        assert spec.intent == "prompt_context"
        assert result.kind == "word"


class TestLoadBatchRowsViaAdapter:
    def test_csv_returns_dataframe(self, tmp_path: Path) -> None:
        f = tmp_path / "rows.csv"
        f.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
        df = sbr._load_batch_rows_via_adapter(str(f), "按行处理")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2

    def test_jsonl_returns_dataframe(self, tmp_path: Path) -> None:
        f = tmp_path / "rows.jsonl"
        f.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
        df = sbr._load_batch_rows_via_adapter(str(f), "")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2

    def test_xlsx_returns_dataframe(self, tmp_path: Path) -> None:
        f = tmp_path / "x.xlsx"
        pd.DataFrame({"k": [1, 2, 3]}).to_excel(f, index=False)
        df = sbr._load_batch_rows_via_adapter(str(f), "按行")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3

    def test_non_batch_intent_falls_back_to_legacy_loader(self, tmp_path: Path) -> None:
        # legacy _load_dataframe only supports csv/xlsx/...; passing a pdf
        # must raise rather than silently produce an empty frame.
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4\nfake")
        with pytest.raises(ValueError):
            sbr._load_batch_rows_via_adapter(str(f), "总结")


class TestAugmentPromptWithAttachment:
    def test_text_excerpt_appended(self) -> None:
        result = AdapterResult(kind="text", ok=True, text="hello world")
        out = sbr._augment_prompt_with_attachment("base prompt", result)
        assert "base prompt" in out
        assert "【附件】" in out
        assert "hello world" in out

    def test_upload_intent_renders_path_block(self) -> None:
        result = AdapterResult(
            kind="upload", ok=True, upload_path="temp_uploads/abc.pdf",
            metadata={"filename": "abc.pdf"},
        )
        out = sbr._augment_prompt_with_attachment("base", result)
        assert "上传至页面" in out
        assert "abc.pdf" in out
        assert "temp_uploads/abc.pdf" in out

    def test_media_source_renders_refs(self) -> None:
        result = AdapterResult(
            kind="media_source", ok=True,
            media_refs=["temp_uploads/clip.mp4"],
            metadata={"filename": "clip.mp4"},
        )
        out = sbr._augment_prompt_with_attachment("g", result)
        assert "媒体源" in out
        assert "clip.mp4" in out

    def test_empty_result_returns_prompt_unchanged(self) -> None:
        result = AdapterResult(kind="unknown", ok=False)
        assert sbr._augment_prompt_with_attachment("only-this", result) == "only-this"


class TestLegacyLoadDataframe:
    def test_csv(self, tmp_path: Path) -> None:
        f = tmp_path / "x.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        df = sbr._load_dataframe(str(f))
        assert len(df) == 1

    def test_tsv(self, tmp_path: Path) -> None:
        f = tmp_path / "x.tsv"
        f.write_text("a\tb\n1\t2\n", encoding="utf-8")
        df = sbr._load_dataframe(str(f))
        assert len(df) == 1

    def test_parquet(self, tmp_path: Path) -> None:
        try:
            import pyarrow  # type: ignore  # noqa: F401
        except Exception:
            pytest.skip("pyarrow not installed")
        f = tmp_path / "x.parquet"
        pd.DataFrame({"a": [1, 2]}).to_parquet(f)
        df = sbr._load_dataframe(str(f))
        assert len(df) == 2

    def test_unsupported_suffix_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "x.weird"
        f.write_bytes(b"data")
        with pytest.raises(ValueError):
            sbr._load_dataframe(str(f))


class TestPersistIOContractsSafe:
    def test_persists_when_run_id_set(self, tmp_path: Path, monkeypatch) -> None:
        # Redirect the runs root so the test stays hermetic.
        from visual_web_agent.io_contract import persistence as _persistence

        monkeypatch.setattr(
            _persistence,
            "default_runs_root",
            lambda: tmp_path,
        )

        f = tmp_path / "data.csv"
        f.write_text("a\n1\n", encoding="utf-8")

        sbr._persist_io_contracts_safe(
            run_id="testrun_1",
            goal="总结这个 Excel 报告",
            target_url="https://a.com",
            urls=["https://b.com"],
            file_path=str(f),
            auth_profiles="",
            vlm_options={},
        )

        ic = json.loads((tmp_path / "testrun_1" / "input_contract.json").read_text())
        oc = json.loads((tmp_path / "testrun_1" / "output_contract.json").read_text())
        manifest = json.loads((tmp_path / "testrun_1" / "manifest.json").read_text())
        assert ic["version"] == "input_contract.v1"
        assert ic["urls"][0]["url"] == "https://a.com"
        assert ic["urls"][1]["url"] == "https://b.com"
        assert ic["attachments"][0]["intent"] in {"batch_rows", "prompt_context"}
        assert oc["version"] == "output_contract.v1"
        assert manifest["version"] == "manifest.v1"

    def test_records_child_run_in_parent_manifest(self, tmp_path: Path, monkeypatch) -> None:
        from visual_web_agent.io_contract import persistence as _persistence

        monkeypatch.setattr(
            _persistence,
            "default_runs_root",
            lambda: tmp_path,
        )

        sbr._record_child_run_to_parent(
            "parent_1",
            {
                "child_run_id": "parent_1_url0002",
                "child_kind": "url",
                "index": 2,
                "total": 3,
                "start_url": "https://b.com",
                "success": True,
            },
        )

        manifest = json.loads((tmp_path / "parent_1" / "manifest.json").read_text())
        items = [
            item for item in manifest["items"]
            if item.get("extra", {}).get("entry_type") == "child_run"
        ]
        assert len(items) == 1
        assert items[0]["path"] == "runs/parent_1_url0002/manifest.json"
        assert items[0]["step_id"] == "url_0002"
        assert items[0]["extra"]["child_run_id"] == "parent_1_url0002"
        assert items[0]["extra"]["success"] is True

    def test_no_run_id_is_noop(self, tmp_path: Path, monkeypatch) -> None:
        from visual_web_agent.io_contract import persistence as _persistence

        monkeypatch.setattr(
            _persistence,
            "default_runs_root",
            lambda: tmp_path,
        )
        sbr._persist_io_contracts_safe(
            run_id="",
            goal="x",
            target_url="https://a.com",
            urls=[],
            file_path=None,
            auth_profiles="",
            vlm_options=None,
        )
        # No directory should be created when run_id is empty
        assert not any(tmp_path.iterdir())

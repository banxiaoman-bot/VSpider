"""Tests for Office / ODF / Email attachment adapters.

These tests exercise the dispatcher + stubs without requiring the heavy
parsers (``python-docx``, ``python-pptx``, ``odfpy``, ``extract_msg``).
When a parser IS installed, the test verifies the adapter returns extracted
text; when it's NOT installed, the adapter must still return a stable
placeholder ``AdapterResult`` so the agent loop can proceed.
"""

from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest

from visual_web_agent.attachment_adapters import adapt_attachment
from visual_web_agent.io_contract.input_contract import (
    AttachmentSpec,
    infer_attachment_intent,
)
from visual_web_agent.upload_store import sniff_mime_and_ext


def _spec(
    path: Path,
    *,
    intent: str,
    mime: str = "",
    filename: str = "",
) -> AttachmentSpec:
    return AttachmentSpec(
        path=str(path),
        filename=filename or path.name,
        mime=mime,
        size=path.stat().st_size if path.exists() else 0,
        sha256="",
        intent=intent,
    )


# ---------------------------------------------------------------------------
# Intent inference
# ---------------------------------------------------------------------------


class TestOfficeIntent:
    def test_docx_default_is_prompt_context(self) -> None:
        assert (
            infer_attachment_intent(filename="contract.docx")
            == "prompt_context"
        )

    def test_docx_explicit_upload(self) -> None:
        assert (
            infer_attachment_intent(filename="contract.docx", goal="上传这份合同")
            == "upload_to_page"
        )

    def test_pptx_default_is_prompt_context(self) -> None:
        assert (
            infer_attachment_intent(filename="slides.pptx")
            == "prompt_context"
        )

    def test_xlsx_summarize_routes_to_prompt_context(self) -> None:
        assert (
            infer_attachment_intent(
                filename="report.xlsx",
                goal="阅读这份 Excel 报告并总结要点",
            )
            == "prompt_context"
        )

    def test_xlsx_default_still_batch_rows(self) -> None:
        # backward-compat: row-shaped Excel still goes to smart_batch_runner
        assert infer_attachment_intent(filename="data.xlsx") == "batch_rows"

    def test_xlsx_row_goal_wins_over_read(self) -> None:
        # If the user says both "read" and "fill each row", we keep
        # batch_rows because the explicit per-row verb is stronger.
        assert (
            infer_attachment_intent(
                filename="data.xlsx",
                goal="阅读该表并按行逐条提交",
            )
            == "batch_rows"
        )

    def test_odt_routes_to_prompt_context(self) -> None:
        assert infer_attachment_intent(filename="notes.odt") == "prompt_context"

    def test_ods_default_still_batch_rows(self) -> None:
        assert infer_attachment_intent(filename="data.ods") == "batch_rows"

    def test_eml_routes_to_prompt_context(self) -> None:
        assert infer_attachment_intent(filename="msg.eml") == "prompt_context"

    def test_rtf_routes_to_prompt_context(self) -> None:
        assert infer_attachment_intent(filename="note.rtf") == "prompt_context"

    def test_mime_only_routing_for_pptx(self) -> None:
        assert (
            infer_attachment_intent(
                filename="",
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
            == "prompt_context"
        )


# ---------------------------------------------------------------------------
# MIME sniffing for OOXML-style containers
# ---------------------------------------------------------------------------


class TestSniffOfficeContainers:
    def test_pk_container_with_docx_filename(self) -> None:
        mime, ext = sniff_mime_and_ext(b"PK\x03\x04zip-like", fallback_filename="x.docx")
        assert mime == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert ext == ".docx"

    def test_pk_container_with_pptx_filename(self) -> None:
        mime, ext = sniff_mime_and_ext(b"PK\x03\x04zip-like", fallback_filename="x.pptx")
        assert mime.endswith("presentationml.presentation")
        assert ext == ".pptx"

    def test_pk_container_with_xlsx_filename(self) -> None:
        mime, ext = sniff_mime_and_ext(b"PK\x03\x04zip-like", fallback_filename="x.xlsx")
        assert mime.endswith("spreadsheetml.sheet")
        assert ext == ".xlsx"

    def test_pk_container_without_filename_falls_back_to_zip(self) -> None:
        mime, ext = sniff_mime_and_ext(b"PK\x03\x04zip-like")
        assert mime == "application/zip"

    def test_ole_compound_with_doc_filename(self) -> None:
        mime, ext = sniff_mime_and_ext(
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1tail", fallback_filename="old.doc"
        )
        assert mime == "application/msword"

    def test_ole_compound_with_xls_filename(self) -> None:
        mime, ext = sniff_mime_and_ext(
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1tail", fallback_filename="old.xls"
        )
        assert mime == "application/vnd.ms-excel"

    def test_rtf_magic(self) -> None:
        mime, ext = sniff_mime_and_ext(b"{\\rtf1\\ansi blah")
        assert mime == "application/rtf"


# ---------------------------------------------------------------------------
# Adapter behaviour - placeholders are stable even without extractors
# ---------------------------------------------------------------------------


def _has_module(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


class TestWordAdapter:
    def test_unknown_word_suffix_returns_placeholder(self, tmp_path: Path) -> None:
        f = tmp_path / "weird.docxx"
        f.write_bytes(b"\x00\x01")
        spec = _spec(f, intent="prompt_context", mime="application/octet-stream")
        result = adapt_attachment(spec)
        # No mime/suffix match -> falls back to plain text adapter, not word
        assert result.ok is True

    def test_rtf_extraction_via_regex_fallback(self, tmp_path: Path) -> None:
        f = tmp_path / "memo.rtf"
        f.write_bytes(rb"{\rtf1\ansi Hello world from RTF.}")
        spec = _spec(f, intent="prompt_context", mime="application/rtf")
        result = adapt_attachment(spec)
        assert result.kind == "word"
        assert result.ok is True
        # Either striprtf parsed it or regex fallback stripped tags
        assert "Hello world from RTF" in result.text or "Hello world" in result.text

    def test_docx_returns_text_when_parser_available(self, tmp_path: Path) -> None:
        if not _has_module("docx"):
            pytest.skip("python-docx not installed")
        import docx  # type: ignore

        f = tmp_path / "doc.docx"
        document = docx.Document()
        document.add_paragraph("Hello from a real docx file.")
        document.add_paragraph("Second paragraph here.")
        document.save(str(f))

        spec = _spec(
            f, intent="prompt_context",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        result = adapt_attachment(spec)
        assert result.kind == "word"
        assert result.ok is True
        assert "Hello from a real docx file." in result.text

    def test_docx_without_parser_returns_placeholder(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "doc.docx"
        f.write_bytes(b"PK\x03\x04fakedocx")
        spec = _spec(
            f, intent="prompt_context",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        import sys
        monkeypatch.setitem(sys.modules, "docx", None)  # force ImportError
        result = adapt_attachment(spec)
        assert result.kind == "word"
        assert result.ok is True
        assert "word document attachment" in result.text


class TestPowerPointAdapter:
    def test_unsupported_legacy_ppt_placeholder(self, tmp_path: Path) -> None:
        f = tmp_path / "deck.ppt"
        f.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy")
        spec = _spec(
            f, intent="prompt_context", mime="application/vnd.ms-powerpoint"
        )
        result = adapt_attachment(spec)
        assert result.kind == "powerpoint"
        assert result.ok is True
        assert "presentation attachment" in result.text
        assert "legacy_ppt_extractor_not_installed" in result.reasons

    def test_pptx_returns_text_when_parser_available(self, tmp_path: Path) -> None:
        if not _has_module("pptx"):
            pytest.skip("python-pptx not installed")
        from pptx import Presentation  # type: ignore
        from pptx.util import Inches  # type: ignore  # noqa: F401

        f = tmp_path / "deck.pptx"
        prs = Presentation()
        slide_layout = prs.slide_layouts[1]
        slide = prs.slides.add_slide(slide_layout)
        slide.shapes.title.text = "Quarterly Review"
        slide.placeholders[1].text = "Revenue up 12% year-over-year."
        prs.save(str(f))

        spec = _spec(
            f, intent="prompt_context",
            mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        result = adapt_attachment(spec)
        assert result.kind == "powerpoint"
        assert result.ok is True
        assert "Quarterly Review" in result.text


class TestSpreadsheetTextAdapter:
    def test_csv_summary(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("a,b\n1,2\n3,4\n5,6\n7,8\n", encoding="utf-8")
        spec = _spec(
            f,
            intent="prompt_context",
            mime="text/csv",
        )
        result = adapt_attachment(spec)
        assert result.kind == "spreadsheet_text"
        assert result.ok is True
        assert "Sheet1" in result.text
        assert "rows" in result.text
        assert result.metadata["sheets"][0]["rows"] == 4

    def test_xlsx_summary_when_pandas_available(self, tmp_path: Path) -> None:
        try:
            import pandas as pd  # type: ignore
        except Exception:
            pytest.skip("pandas not installed")

        f = tmp_path / "report.xlsx"
        df = pd.DataFrame({"city": ["BJ", "SH"], "score": [99, 88]})
        df.to_excel(f, index=False)
        spec = _spec(
            f, intent="prompt_context",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        result = adapt_attachment(spec)
        assert result.kind == "spreadsheet_text"
        assert result.ok is True
        assert "city" in result.text
        assert result.metadata["sheets"][0]["rows"] == 2


class TestEmailAdapter:
    def test_eml_extraction(self, tmp_path: Path) -> None:
        raw = textwrap.dedent(
            """\
            From: alice@example.com
            To: bob@example.com
            Subject: Hello
            Date: Mon, 1 Jan 2026 12:00:00 +0000
            Content-Type: text/plain; charset="utf-8"

            This is the body of the test message.
            """
        )
        f = tmp_path / "msg.eml"
        f.write_text(raw, encoding="utf-8")
        spec = _spec(f, intent="prompt_context", mime="message/rfc822")
        result = adapt_attachment(spec)
        assert result.kind == "email"
        assert result.ok is True
        assert "Subject: Hello" in result.text
        assert "This is the body" in result.text

    def test_msg_without_parser_placeholder(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "msg.msg"
        f.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1\x00\x00")
        spec = _spec(f, intent="prompt_context", mime="application/vnd.ms-outlook")
        import sys
        monkeypatch.setitem(sys.modules, "extract_msg", None)
        result = adapt_attachment(spec)
        assert result.kind == "email"
        assert result.ok is True
        assert "extract_msg not installed" in result.text


# ---------------------------------------------------------------------------
# Rows adapter still works for the new spreadsheet suffixes
# ---------------------------------------------------------------------------


class TestRowsAdapterCoverage:
    def test_xlsm_routed_via_rows(self, tmp_path: Path) -> None:
        try:
            import pandas as pd  # type: ignore
        except Exception:
            pytest.skip("pandas not installed")

        f = tmp_path / "data.xlsm"
        pd.DataFrame({"a": [1, 2]}).to_excel(f, index=False)
        spec = _spec(f, intent="batch_rows")
        result = adapt_attachment(spec)
        assert result.kind == "rows"
        assert result.ok is True
        assert list(result.rows or [])  # non-empty

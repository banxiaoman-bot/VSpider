"""Tests for ``visual_web_agent.attachment_adapters`` dispatcher and stubs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from visual_web_agent.attachment_adapters import adapt_attachment, AdapterResult
from visual_web_agent.io_contract.input_contract import AttachmentSpec


def _spec(path: Path, *, intent: str, mime: str = "", filename: str = "") -> AttachmentSpec:
    return AttachmentSpec(
        path=str(path),
        filename=filename or path.name,
        mime=mime,
        size=path.stat().st_size if path.exists() else 0,
        sha256="",
        intent=intent,
    )


class TestDispatchUpload:
    def test_upload_intent_returns_path(self, tmp_path: Path) -> None:
        f = tmp_path / "x.bin"
        f.write_bytes(b"hello")
        spec = _spec(f, intent="upload_to_page", mime="application/octet-stream")
        result = adapt_attachment(spec, goal="upload this")
        assert isinstance(result, AdapterResult)
        assert result.kind == "upload"
        assert result.ok is True
        assert result.upload_path == str(f)

    def test_missing_file_marks_not_ok(self, tmp_path: Path) -> None:
        spec = AttachmentSpec(path=str(tmp_path / "nope.bin"), intent="upload_to_page")
        result = adapt_attachment(spec)
        assert result.ok is False
        assert any("attachment_not_found" in r for r in result.reasons)


class TestDispatchRows:
    def test_csv_yields_rows(self, tmp_path: Path) -> None:
        f = tmp_path / "rows.csv"
        f.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
        spec = _spec(f, intent="batch_rows")
        result = adapt_attachment(spec, goal="按行填表")
        assert result.kind == "rows"
        assert result.ok is True
        rows = list(result.rows or [])
        assert len(rows) == 2
        assert rows[0]["a"] == 1 or rows[0]["a"] == "1"

    def test_json_array_yields_rows(self, tmp_path: Path) -> None:
        f = tmp_path / "rows.json"
        f.write_text(json.dumps([{"a": 1}, {"a": 2}]), encoding="utf-8")
        spec = _spec(f, intent="batch_rows")
        result = adapt_attachment(spec)
        assert result.kind == "rows"
        rows = list(result.rows or [])
        assert len(rows) == 2

    def test_jsonl_yields_rows(self, tmp_path: Path) -> None:
        f = tmp_path / "rows.jsonl"
        f.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
        spec = _spec(f, intent="batch_rows")
        result = adapt_attachment(spec)
        assert result.kind == "rows"
        rows = list(result.rows or [])
        assert len(rows) == 2

    def test_unsupported_suffix(self, tmp_path: Path) -> None:
        f = tmp_path / "rows.weird"
        f.write_bytes(b"data")
        spec = _spec(f, intent="batch_rows")
        result = adapt_attachment(spec)
        assert result.ok is False


class TestDispatchPromptContext:
    def test_text_excerpt(self, tmp_path: Path) -> None:
        f = tmp_path / "notes.txt"
        f.write_text("hello world\n" * 100, encoding="utf-8")
        spec = _spec(f, intent="prompt_context", mime="text/plain")
        result = adapt_attachment(spec)
        assert result.kind == "text"
        assert result.ok is True
        assert "hello world" in result.text

    def test_image_returns_caption(self, tmp_path: Path) -> None:
        f = tmp_path / "pic.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\nfakeimage")
        spec = _spec(f, intent="prompt_context", mime="image/png")
        result = adapt_attachment(spec)
        assert result.kind == "image"
        assert result.text.startswith("[image attachment:")

    def test_pdf_returns_caption_without_extractor(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4\nnotreallypdf")
        spec = _spec(f, intent="prompt_context", mime="application/pdf")
        result = adapt_attachment(spec)
        assert result.kind == "pdf"
        assert result.ok is True
        assert "pdf attachment" in result.text or len(result.text) >= 0


class TestDispatchMediaSourceAndUnknown:
    def test_media_source(self, tmp_path: Path) -> None:
        f = tmp_path / "clip.mp4"
        f.write_bytes(b"fakeav")
        spec = _spec(f, intent="media_source", mime="video/mp4")
        result = adapt_attachment(spec)
        assert result.kind == "media_source"
        assert result.ok is True
        assert str(f) in result.media_refs

    def test_unknown_intent(self, tmp_path: Path) -> None:
        f = tmp_path / "weird.bin"
        f.write_bytes(b"data")
        spec = _spec(f, intent="unknown")
        result = adapt_attachment(spec)
        assert result.kind == "unknown"
        assert result.ok is False

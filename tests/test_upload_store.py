"""Tests for ``visual_web_agent.upload_store`` (S4 - content-addressed uploads)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from visual_web_agent.upload_store import (
    UploadEntry,
    add_ref,
    all_refs,
    gc,
    list_entries,
    lookup,
    remove_ref,
    save_bytes,
    save_stream,
    sha256_of,
    sniff_mime_and_ext,
)


class TestSniff:
    def test_pdf_magic(self) -> None:
        mime, ext = sniff_mime_and_ext(b"%PDF-1.4\n...")
        assert mime == "application/pdf"
        assert ext == ".pdf"

    def test_png_magic(self) -> None:
        mime, ext = sniff_mime_and_ext(b"\x89PNG\r\n\x1a\nfoo")
        assert mime == "image/png"
        assert ext == ".png"

    def test_zip_magic(self) -> None:
        mime, ext = sniff_mime_and_ext(b"PK\x03\x04xxx")
        assert mime == "application/zip"

    def test_csv_falls_back_to_filename(self) -> None:
        mime, ext = sniff_mime_and_ext(b"a,b,c\n1,2,3\n", fallback_filename="x.csv")
        assert mime == "text/csv"
        assert ext == ".csv"

    def test_unknown_binary_returns_octet_stream(self) -> None:
        mime, ext = sniff_mime_and_ext(b"\x00\x01\x02\x03\x04\x05\x06\x07")
        assert mime == "application/octet-stream"

    def test_plaintext_default(self) -> None:
        mime, ext = sniff_mime_and_ext(b"hello world")
        assert mime == "text/plain"


class TestSaveBytes:
    def test_save_new_file(self, tmp_path: Path) -> None:
        entry = save_bytes(tmp_path, b"%PDF-1.4\nhello", filename="x.pdf", ref="run_a")
        assert entry.sha256 == sha256_of(b"%PDF-1.4\nhello")
        assert entry.mime == "application/pdf"
        assert entry.size == len(b"%PDF-1.4\nhello")
        assert Path(entry.path).exists()
        assert "run_a" in entry.refs

    def test_dedup_on_repeat(self, tmp_path: Path) -> None:
        e1 = save_bytes(tmp_path, b"same content", filename="a.txt", ref="run_a")
        e2 = save_bytes(tmp_path, b"same content", filename="b.txt", ref="run_b")
        assert e1.sha256 == e2.sha256
        assert e1.path == e2.path
        files = [p for p in tmp_path.iterdir() if p.is_file() and p.name != "_index.json"]
        assert len(files) == 1
        e = lookup(tmp_path, e1.sha256)
        assert e is not None
        assert "run_a" in e.refs
        assert "run_b" in e.refs

    def test_no_default_xlsx_fallback(self, tmp_path: Path) -> None:
        entry = save_bytes(tmp_path, b"%PDF-1.4\nx", filename="", ref="r")
        assert not entry.path.endswith(".xlsx")

    def test_save_stream(self, tmp_path: Path) -> None:
        data = b"%PDF-1.4\n" + b"X" * 5000
        stream = io.BytesIO(data)
        entry = save_stream(tmp_path, stream, filename="big.pdf", ref="run_x")
        assert entry.sha256 == sha256_of(data)
        assert entry.size == len(data)
        assert entry.mime == "application/pdf"


class TestRefs:
    def test_add_and_remove_ref(self, tmp_path: Path) -> None:
        entry = save_bytes(tmp_path, b"ref content", filename="a.txt")
        assert entry.refs == []
        assert add_ref(tmp_path, entry.sha256, "run_1") is True
        assert "run_1" in lookup(tmp_path, entry.sha256).refs
        assert remove_ref(tmp_path, entry.sha256, "run_1") is True
        assert lookup(tmp_path, entry.sha256).refs == []

    def test_remove_nonexistent_ref(self, tmp_path: Path) -> None:
        assert remove_ref(tmp_path, "nope", "ref") is False

    def test_all_refs_iter(self, tmp_path: Path) -> None:
        save_bytes(tmp_path, b"a", filename="a.txt", ref="r1")
        save_bytes(tmp_path, b"b", filename="b.txt", ref="r2")
        items = dict(all_refs(tmp_path))
        assert len(items) == 2


class TestGC:
    def test_no_refs_old_entry_purged(self, tmp_path: Path) -> None:
        entry = save_bytes(tmp_path, b"junk", filename="x.bin")
        path = Path(entry.path)
        assert path.exists()

        summary = gc(tmp_path, ttl_seconds=0.0)
        assert summary["removed_count"] == 1
        assert entry.sha256 in summary["removed_sha"]
        assert not path.exists()

    def test_ref_keeps_entry_alive(self, tmp_path: Path) -> None:
        entry = save_bytes(tmp_path, b"keep", filename="k.txt", ref="run_a")
        summary = gc(tmp_path, ttl_seconds=0.0)
        assert summary["removed_count"] == 0
        assert Path(entry.path).exists()

    def test_fresh_entry_not_purged(self, tmp_path: Path) -> None:
        entry = save_bytes(tmp_path, b"new", filename="n.txt")
        summary = gc(tmp_path, ttl_seconds=3600.0)
        assert summary["removed_count"] == 0
        assert Path(entry.path).exists()


class TestList:
    def test_list_entries(self, tmp_path: Path) -> None:
        save_bytes(tmp_path, b"a", filename="a.txt", ref="r1")
        save_bytes(tmp_path, b"b", filename="b.txt", ref="r2")
        entries = list_entries(tmp_path)
        assert len(entries) == 2
        assert all(isinstance(e, UploadEntry) for e in entries)

    def test_index_serialization(self, tmp_path: Path) -> None:
        save_bytes(tmp_path, b"hello", filename="x.txt", ref="r")
        idx = json.loads((tmp_path / "_index.json").read_text(encoding="utf-8"))
        assert len(idx) == 1
        sha = next(iter(idx.keys()))
        entry_dict = idx[sha]
        # the index uses sha as the dict key AND duplicates it as a field
        # value, so round-trips are unambiguous regardless of how the file
        # is read back.
        assert entry_dict["sha256"] == sha
        entry = lookup(tmp_path, sha)
        assert entry is not None and entry.sha256 == sha

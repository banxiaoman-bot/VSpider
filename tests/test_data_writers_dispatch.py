"""Tests for ``visual_web_agent.data_writers``.

Drives Slice OUT-1: a single ``save_artifact`` dispatch that routes by
``container`` to per-format writers, computes sha256 + size, and writes
each landed artifact into ``runs/<run_id>/manifest.json`` so the
``output_contract.v1`` end-to-end loop becomes verifiable.

All IO is sandboxed under ``tmp_path`` via ``base_dir=...``; no test
touches the real ``runs/`` directory.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Public API import surface (drives __init__)
# ---------------------------------------------------------------------------

def test_public_api_exports_expected_symbols() -> None:
    import visual_web_agent.data_writers as dw

    assert callable(getattr(dw, "save_artifact", None))
    assert hasattr(dw, "CONTAINER_TO_WRITER")
    for container in (
        "xlsx", "csv", "jsonl", "json",
        "markdown", "html", "files_folder", "inline_text",
    ):
        assert container in dw.CONTAINER_TO_WRITER, (
            f"container {container!r} must have a registered writer"
        )


# ---------------------------------------------------------------------------
# Common shared helpers
# ---------------------------------------------------------------------------

ROWS_SMALL = [
    {"title": "First", "url": "https://a/1", "score": 10},
    {"title": "Second", "url": "https://a/2", "score": 8},
]


def _read_manifest(tmp_path: Path, run_id: str) -> dict:
    from visual_web_agent.io_contract import MANIFEST_FILENAME

    p = tmp_path / run_id / MANIFEST_FILENAME
    assert p.exists(), f"manifest must exist at {p}"
    return json.loads(p.read_text(encoding="utf-8"))


def _read_bytes(tmp_path: Path, run_id: str, rel: str) -> bytes:
    from visual_web_agent.io_contract import ARTIFACTS_DIRNAME

    p = tmp_path / run_id / ARTIFACTS_DIRNAME / rel
    assert p.exists(), f"artifact must exist at {p}"
    return p.read_bytes()


# ---------------------------------------------------------------------------
# Dispatcher: bad container falls back safely
# ---------------------------------------------------------------------------

class TestDispatcherFallback:
    def test_unknown_container_raises_clearly(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        with pytest.raises(ValueError) as excinfo:
            save_artifact(
                ROWS_SMALL,
                run_id="run_x",
                container="wat",
                output_kind="dataset_rows",
                base_dir=tmp_path,
            )
        assert "container" in str(excinfo.value).lower()

    def test_empty_run_id_raises(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        with pytest.raises(ValueError):
            save_artifact(
                ROWS_SMALL,
                run_id="",
                container="csv",
                output_kind="dataset_rows",
                base_dir=tmp_path,
            )


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

class TestCsvWriter:
    def test_writes_csv_under_run_artifacts_and_manifest(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        item = save_artifact(
            ROWS_SMALL,
            run_id="run_csv",
            container="csv",
            output_kind="dataset_rows",
            produced_by="vlm_extract",
            step_id="3",
            base_dir=tmp_path,
        )

        # artifact landed
        rel = Path(item["path"]).name
        body = _read_bytes(tmp_path, "run_csv", rel)
        assert b"title,url,score" in body or b"title,score,url" in body
        assert b"First" in body and b"Second" in body

        # manifest registered
        manifest = _read_manifest(tmp_path, "run_csv")
        assert manifest["run_id"] == "run_csv"
        assert any(it["kind"] == "dataset_rows" for it in manifest["items"])
        target_item = next(it for it in manifest["items"] if it["kind"] == "dataset_rows")
        assert target_item["mime"] == "text/csv"
        assert target_item["produced_by"] == "vlm_extract"
        assert target_item["step_id"] == "3"
        assert target_item["size"] == len(body)
        assert target_item["sha256"] == hashlib.sha256(body).hexdigest()

    def test_csv_extension_is_csv(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        item = save_artifact(
            ROWS_SMALL,
            run_id="run_csv2",
            container="csv",
            output_kind="dataset_rows",
            base_dir=tmp_path,
        )
        assert Path(item["path"]).suffix == ".csv"


# ---------------------------------------------------------------------------
# JSONL writer
# ---------------------------------------------------------------------------

class TestJsonlWriter:
    def test_one_row_per_line(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        item = save_artifact(
            ROWS_SMALL,
            run_id="run_jsonl",
            container="jsonl",
            output_kind="dataset_records",
            base_dir=tmp_path,
        )
        body = _read_bytes(tmp_path, "run_jsonl", Path(item["path"]).name).decode("utf-8")
        lines = [ln for ln in body.splitlines() if ln.strip()]
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["title"] == "First"
        assert second["title"] == "Second"

        manifest = _read_manifest(tmp_path, "run_jsonl")
        target = next(it for it in manifest["items"] if it["mime"].startswith("application/"))
        assert target["mime"] == "application/x-ndjson"


# ---------------------------------------------------------------------------
# JSON writer
# ---------------------------------------------------------------------------

class TestJsonWriter:
    def test_writes_array_of_objects(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        item = save_artifact(
            ROWS_SMALL,
            run_id="run_json",
            container="json",
            output_kind="dataset_records",
            base_dir=tmp_path,
        )
        body = _read_bytes(tmp_path, "run_json", Path(item["path"]).name).decode("utf-8")
        parsed = json.loads(body)
        assert isinstance(parsed, list) and len(parsed) == 2
        assert parsed[0]["title"] == "First"

    def test_writes_single_record_when_given_dict(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        record = {"answer": "42", "source": "https://x"}
        item = save_artifact(
            record,
            run_id="run_json_dict",
            container="json",
            output_kind="answer_text",
            base_dir=tmp_path,
        )
        body = _read_bytes(tmp_path, "run_json_dict", Path(item["path"]).name).decode("utf-8")
        parsed = json.loads(body)
        assert parsed == record


# ---------------------------------------------------------------------------
# Markdown writer
# ---------------------------------------------------------------------------

class TestMarkdownWriter:
    def test_writes_markdown_table_for_rows(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        item = save_artifact(
            ROWS_SMALL,
            run_id="run_md",
            container="markdown",
            output_kind="dataset_rows",
            base_dir=tmp_path,
        )
        body = _read_bytes(tmp_path, "run_md", Path(item["path"]).name).decode("utf-8")
        assert "| title |" in body or "| title " in body
        assert "First" in body
        assert "---" in body

    def test_writes_plain_markdown_for_string(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        item = save_artifact(
            "# Hello\n\nbody text",
            run_id="run_md2",
            container="markdown",
            output_kind="answer_text",
            base_dir=tmp_path,
        )
        body = _read_bytes(tmp_path, "run_md2", Path(item["path"]).name).decode("utf-8")
        assert body.startswith("# Hello")
        assert "body text" in body


# ---------------------------------------------------------------------------
# HTML writer
# ---------------------------------------------------------------------------

class TestHtmlWriter:
    def test_writes_html_passthrough(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        html = "<html><body><h1>Snapshot</h1></body></html>"
        item = save_artifact(
            html,
            run_id="run_html",
            container="html",
            output_kind="html_snapshot",
            base_dir=tmp_path,
        )
        body = _read_bytes(tmp_path, "run_html", Path(item["path"]).name).decode("utf-8")
        assert "<h1>Snapshot</h1>" in body

        manifest = _read_manifest(tmp_path, "run_html")
        target = next(it for it in manifest["items"] if it["kind"] == "html_snapshot")
        assert target["mime"] == "text/html"


# ---------------------------------------------------------------------------
# Files-folder writer: writes raw bytes + records each piece
# ---------------------------------------------------------------------------

class TestFilesFolderWriter:
    def test_writes_multiple_blobs_and_appends_each(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        blobs = [
            {"filename": "a.txt", "bytes": b"hello", "source_url": "https://x/a"},
            {"filename": "b.bin", "bytes": b"\x00\x01\x02", "mime": "application/octet-stream"},
        ]
        item = save_artifact(
            blobs,
            run_id="run_ff",
            container="files_folder",
            output_kind="file_generic",
            base_dir=tmp_path,
        )

        # Returned value is a dict describing the folder + manifest summary
        assert "items" in item
        assert len(item["items"]) == 2

        manifest = _read_manifest(tmp_path, "run_ff")
        kinds_in_manifest = [it["kind"] for it in manifest["items"]]
        assert kinds_in_manifest.count("file_generic") == 2
        # Files actually exist
        a_item = next(it for it in manifest["items"] if it["path"].endswith("a.txt"))
        assert Path(a_item["path"]).exists()
        assert Path(a_item["path"]).read_bytes() == b"hello"
        assert a_item["source_url"] == ["https://x/a"]


# ---------------------------------------------------------------------------
# Inline text writer: container=inline_text MUST NOT write to disk
# ---------------------------------------------------------------------------

class TestInlineTextWriter:
    def test_inline_text_does_not_write_artifact_file(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact
        from visual_web_agent.io_contract import ARTIFACTS_DIRNAME

        item = save_artifact(
            "Quick answer: 42",
            run_id="run_inline",
            container="inline_text",
            output_kind="answer_text",
            base_dir=tmp_path,
        )
        # We still get a metadata record back
        assert item["kind"] == "answer_text"
        assert item.get("inline") is True
        assert item.get("text") == "Quick answer: 42"

        # But the artifacts dir must be empty (no spurious xlsx/csv landed)
        artifacts_dir = tmp_path / "run_inline" / ARTIFACTS_DIRNAME
        assert artifacts_dir.exists()
        assert list(artifacts_dir.iterdir()) == [], (
            "inline_text answers MUST NOT land any artifact file"
        )

        # And the manifest should still record an answer_text item for traceability
        manifest = _read_manifest(tmp_path, "run_inline")
        assert any(it["kind"] == "answer_text" for it in manifest["items"])


# ---------------------------------------------------------------------------
# XLSX writer: structural (skipped if openpyxl/pandas unavailable)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    pytest.importorskip("pandas", reason="pandas required for xlsx tests") is None,
    reason="pandas missing",
)
class TestXlsxWriter:
    def test_writes_xlsx_and_records_manifest(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        item = save_artifact(
            ROWS_SMALL,
            run_id="run_xlsx",
            container="xlsx",
            output_kind="dataset_rows",
            base_dir=tmp_path,
        )
        body = _read_bytes(tmp_path, "run_xlsx", Path(item["path"]).name)
        # OOXML magic = PK\x03\x04
        assert body[:4] == b"PK\x03\x04"
        assert Path(item["path"]).suffix == ".xlsx"

        manifest = _read_manifest(tmp_path, "run_xlsx")
        target = next(it for it in manifest["items"] if it["kind"] == "dataset_rows")
        assert target["mime"] == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )


# ---------------------------------------------------------------------------
# Dedup: writing the same payload twice dedupes by sha256 in manifest
# ---------------------------------------------------------------------------

class TestDedupAcrossSaves:
    def test_identical_csv_payload_dedupes_in_manifest(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        save_artifact(
            ROWS_SMALL,
            run_id="run_dedup",
            container="csv",
            output_kind="dataset_rows",
            base_dir=tmp_path,
        )
        save_artifact(
            ROWS_SMALL,
            run_id="run_dedup",
            container="csv",
            output_kind="dataset_rows",
            base_dir=tmp_path,
        )

        manifest = _read_manifest(tmp_path, "run_dedup")
        dataset_items = [it for it in manifest["items"] if it["kind"] == "dataset_rows"]
        assert len(dataset_items) == 1, "identical sha256 must dedupe to one manifest entry"


# ---------------------------------------------------------------------------
# Back-compat: legacy data_manager.save_to_excel must forward to save_artifact
# ---------------------------------------------------------------------------

class TestLegacyForwarding:
    def test_save_to_excel_still_works_and_returns_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``save_to_excel`` is still the public entry point used in many places.

        After Slice OUT-1 it must keep its signature/return value (str absolute
        path) so the 21+ existing callers continue to work unmodified.
        """
        pd = pytest.importorskip("pandas")
        from visual_web_agent import data_manager as dm

        # Redirect artifact_root() to tmp_path so we don't pollute workspace/
        monkeypatch.setattr(
            dm, "resolve_artifact_path",
            lambda filename, subdir="": tmp_path / Path(filename).name,
            raising=True,
        )

        out = dm.save_to_excel([{"a": 1}, {"a": 2}], "smoke.xlsx")
        assert isinstance(out, str)
        assert out.endswith("smoke.xlsx")
        assert Path(out).exists()
        # File should actually be a real xlsx (magic PK)
        assert Path(out).read_bytes()[:4] == b"PK\x03\x04"


class TestSaveRunDataset:
    def test_resolve_output_contract_merges_later_values(self) -> None:
        from visual_web_agent.data_writers.dispatch import resolve_output_contract

        merged = resolve_output_contract(
            {"container": "xlsx", "output_kind": "dataset_rows"},
            {"container": "csv"},
        )
        assert merged["container"] == "csv"
        assert merged["output_kind"] == "dataset_rows"

    def test_resolve_output_contract_media_kind_is_not_blind_xlsx(self) -> None:
        """mission §一-A: a media task must never be forced into xlsx. When the
        contract carries a media ``output_kind`` but no explicit container, the
        default must follow the kind (files_folder), not a blind xlsx."""
        from visual_web_agent.data_writers.dispatch import resolve_output_contract

        merged = resolve_output_contract({"output_kind": "media_image"})
        assert merged["container"] == "files_folder"

    def test_resolve_output_contract_answer_text_is_inline(self) -> None:
        from visual_web_agent.data_writers.dispatch import resolve_output_contract

        merged = resolve_output_contract({"output_kind": "answer_text"})
        assert merged["container"] == "inline_text"

    def test_resolve_output_contract_dataset_rows_keeps_xlsx_policy(self) -> None:
        """Pure tabular small dataset still defaults to xlsx -- this is the
        sanctioned kind->container policy (default_container_for_kind), not the
        old blind hard-coded default."""
        from visual_web_agent.data_writers.dispatch import resolve_output_contract

        merged = resolve_output_contract({"output_kind": "dataset_rows"})
        assert merged["container"] == "xlsx"

    def test_resolve_output_contract_explicit_container_wins_over_kind(self) -> None:
        from visual_web_agent.data_writers.dispatch import resolve_output_contract

        merged = resolve_output_contract({"output_kind": "media_image", "container": "zip"})
        assert merged["container"] == "zip"

    def test_save_run_dataset_honors_container(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers.dispatch import save_run_dataset

        path = save_run_dataset(
            [{"name": "alpha"}],
            run_id="run_contract",
            output_contract={"container": "csv", "output_kind": "dataset_rows"},
            produced_by="test",
            base_dir=tmp_path,
        )
        assert path.endswith(".csv")
        assert Path(path).exists()

    def test_save_run_dataset_unique_key_uses_legacy_xlsx(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("pandas")
        from visual_web_agent import data_manager as dm
        from visual_web_agent.data_writers.dispatch import save_run_dataset

        monkeypatch.setattr(
            dm,
            "resolve_artifact_path",
            lambda filename, subdir="": tmp_path / Path(filename).name,
            raising=True,
        )
        path = save_run_dataset(
            [{"tooltip": "one"}],
            run_id="run_tooltip",
            output_contract={"container": "xlsx"},
            filename_hint="tips.xlsx",
            unique_key="tooltip",
        )
        assert path.endswith("tips.xlsx")
        assert Path(path).exists()

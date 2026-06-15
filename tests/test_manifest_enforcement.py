"""C1 manifest enforcement + C2 media download contract — TDD tests.

C1: Verify that every data_writer (xlsx, csv, jsonl, json, markdown,
html, files_folder) calls finalize_file_artifact to append a manifest
entry after writing.

C2: Verify that media_harvester downloads files and writes manifest
entries (not just URL-as-row in xlsx).

Pure static + stub tests; no filesystem writes in production paths.
"""

from __future__ import annotations

import inspect
import re


class TestC1ManifestEnforcement:
    """Every writer must call finalize_file_artifact or append_manifest_item."""

    def _writer_source(self, module_name: str) -> str:
        import importlib
        mod = importlib.import_module(f"visual_web_agent.data_writers.{module_name}")
        return inspect.getsource(mod)

    def test_xlsx_writer_calls_finalize(self):
        src = self._writer_source("xlsx_writer")
        assert "finalize_file_artifact" in src or "append_manifest_item" in src

    def test_csv_writer_calls_finalize(self):
        src = self._writer_source("csv_writer")
        assert "finalize_file_artifact" in src or "append_manifest_item" in src

    def test_jsonl_writer_calls_finalize(self):
        src = self._writer_source("jsonl_writer")
        assert "finalize_file_artifact" in src or "append_manifest_item" in src

    def test_json_writer_calls_finalize(self):
        src = self._writer_source("json_writer")
        assert "finalize_file_artifact" in src or "append_manifest_item" in src

    def test_markdown_writer_calls_finalize(self):
        src = self._writer_source("markdown_writer")
        assert "finalize_file_artifact" in src or "append_manifest_item" in src

    def test_html_writer_calls_finalize(self):
        src = self._writer_source("html_writer")
        assert "finalize_file_artifact" in src or "append_manifest_item" in src

    def test_files_folder_writer_calls_finalize(self):
        src = self._writer_source("files_folder_writer")
        assert "finalize_file_artifact" in src or "append_manifest_item" in src

    def test_dispatch_routes_all_containers(self):
        from visual_web_agent.data_writers.dispatch import CONTAINER_TO_WRITER
        required = {"xlsx", "csv", "jsonl", "json", "markdown", "html", "files_folder", "inline_text"}
        assert required.issubset(set(CONTAINER_TO_WRITER.keys()))

    def test_base_finalize_calls_append_manifest(self):
        src = inspect.getsource(
            __import__("visual_web_agent.data_writers._base", fromlist=["_base"])
        )
        assert "append_manifest_item" in src


class TestC2MediaDownloadContract:
    """media_harvester must download files, not just record URLs as rows."""

    def test_harvester_calls_download_candidate(self):
        import inspect
        from visual_web_agent.media_harvester import harvester
        src = inspect.getsource(harvester)
        assert "download_candidate" in src

    def test_harvester_writes_manifest(self):
        import inspect
        from visual_web_agent.media_harvester import harvester
        src = inspect.getsource(harvester)
        assert "write_manifest" in src or "append_item" in src

    def test_harvester_has_manifest_appended_field(self):
        from visual_web_agent.media_harvester.harvester import HarvestReport
        report = HarvestReport(run_id="test")
        assert hasattr(report, "manifest_appended")
        assert report.manifest_appended == 0

    def test_agent_hook_recognizes_media_output_kinds(self):
        from visual_web_agent.media_harvester.agent_hook import _MEDIA_OUTPUT_KINDS
        required = {"media_image", "media_video", "media_audio", "media_pdf"}
        assert required.issubset(_MEDIA_OUTPUT_KINDS)

    def test_downloader_module_exists(self):
        from visual_web_agent.media_harvester.downloader import download_candidate
        assert callable(download_candidate)

    def test_harvester_report_serializes(self):
        from visual_web_agent.media_harvester.harvester import HarvestReport
        report = HarvestReport(run_id="test", manifest_appended=3)
        d = report.to_dict()
        assert d["manifest_appended"] == 3


class TestManifestDataModel:
    """Verify manifest.v1 data model contract."""

    def test_manifest_item_has_writer_field(self):
        from visual_web_agent.io_contract.manifest import ManifestItem
        item = ManifestItem(kind="dataset_rows", path="test.xlsx", produced_by="xlsx_writer")
        d = item.to_dict()
        assert d["produced_by"] == "xlsx_writer"

    def test_append_item_dedupes_on_sha256(self):
        from visual_web_agent.io_contract.manifest import Manifest, append_item, new_manifest
        m = new_manifest("test_run")
        append_item(m, kind="media_image", path="img1.png", sha256="abc123")
        append_item(m, kind="media_image", path="img1_dup.png", sha256="abc123", source_url="http://b")
        assert len(m.items) == 1
        assert "http://b" in m.items[0].source_url

    def test_manifest_roundtrip(self):
        from visual_web_agent.io_contract.manifest import Manifest, append_item, new_manifest
        m = new_manifest("roundtrip_test")
        append_item(m, kind="dataset_rows", path="data.csv", produced_by="csv_writer")
        d = m.to_dict()
        m2 = Manifest.from_dict(d)
        assert m2.run_id == "roundtrip_test"
        assert len(m2.items) == 1
        assert m2.items[0].produced_by == "csv_writer"

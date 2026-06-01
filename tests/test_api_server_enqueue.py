"""Smoke tests for ``api_server._enqueue_task`` after multi-URL / upload
integration.

We do NOT spin up the FastAPI app here; we only verify that the
``_enqueue_task`` helper accepts the new ``urls`` / ``upload_sha256`` /
``upload_mime`` kwargs and threads them into the queued task dict.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fresh_api_server(monkeypatch, tmp_path: Path):
    """Import api_server with isolated active_tasks + run_registry root.

    Each test gets a fresh in-memory queue so they don't pollute each other.
    """

    import importlib

    api_server = importlib.import_module("api_server")
    # Clear in-memory queue state
    api_server.active_tasks.clear()

    # Redirect run_registry writes to tmp
    monkeypatch.setattr(
        api_server._run_registry,
        "registry_root",
        lambda base_dir=None: tmp_path / "registry",
    )
    # Avoid filesystem persistence side-effects
    monkeypatch.setattr(api_server, "_persist_queue_snapshot_safe", lambda: None)
    return api_server


class TestEnqueueTaskAcceptsNewFields:
    def test_basic_enqueue_without_new_fields(self, fresh_api_server) -> None:
        item, _ = fresh_api_server._enqueue_task(
            target_url="https://a.com",
            prompt="hello",
            file_path="",
        )
        assert item["target_url"] == "https://a.com"
        assert item["urls"] == []
        assert item["upload_sha256"] == ""
        assert item["upload_mime"] == ""

    def test_enqueue_with_urls(self, fresh_api_server) -> None:
        item, _ = fresh_api_server._enqueue_task(
            target_url="https://a.com",
            prompt="hello",
            file_path="",
            urls=["https://b.com", "https://c.com"],
        )
        assert item["urls"] == ["https://b.com", "https://c.com"]

    def test_enqueue_with_upload_metadata(self, fresh_api_server) -> None:
        item, _ = fresh_api_server._enqueue_task(
            target_url="https://a.com",
            prompt="hello",
            file_path="temp_uploads/abc.pdf",
            urls=[],
            upload_sha256="aaa",
            upload_mime="application/pdf",
        )
        assert item["upload_sha256"] == "aaa"
        assert item["upload_mime"] == "application/pdf"
        assert item["file_path"] == "temp_uploads/abc.pdf"

    def test_enqueue_creates_task_id(self, fresh_api_server) -> None:
        item, _ = fresh_api_server._enqueue_task(
            target_url="https://a.com", prompt="x", file_path="",
        )
        assert item["task_id"]
        assert item["status"] == "queued"
        assert "queued_at" in item


class TestParseUrlsFieldImportable:
    def test_io_contract_exports(self) -> None:
        from visual_web_agent.io_contract import parse_urls_field

        out = parse_urls_field("https://a.com,https://b.com")
        assert [u.url for u in out] == ["https://a.com", "https://b.com"]

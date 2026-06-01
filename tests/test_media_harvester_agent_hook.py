"""Tests for ``visual_web_agent.media_harvester.agent_hook``.

These tests cover the Agent loop wire-up surface introduced in E2:

- :func:`is_pure_media_goal` heuristic across CN/EN goals.
- :func:`maybe_run_media_harvest` short-circuits, skip reasons, and
  end-to-end harvest dispatch via a fake Playwright page + an injected
  fake streaming HTTP client.

We never spin up Playwright or a real network: the fake page mimics the
Playwright ``Page`` surface (``async content()`` + ``url`` attribute) and
``harvest_to_run`` is steered with a fake :class:`StreamingClient` by
monkey-patching its default at the ``agent_hook`` import site.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Iterator

import pytest

from visual_web_agent.media_harvester import (
    MediaHarvestHookResult,
    is_pure_media_goal,
    maybe_run_media_harvest,
)
from visual_web_agent.media_harvester import agent_hook as agent_hook_module
from visual_web_agent.media_harvester.harvester import HarvestReport


# ---------------------------------------------------------------------------
# Fake Playwright page
# ---------------------------------------------------------------------------


class _FakePage:
    """Mimics the subset of Playwright ``Page`` the hook actually uses."""

    def __init__(self, *, html: str, url: str = "https://example.com/landing") -> None:
        self._html = html
        self.url = url

    async def content(self) -> str:
        return self._html


class _FakePageRaisingContent:
    """Page whose ``content()`` blows up — covers the ``page_html_failed`` branch."""

    url = "https://example.com/x"

    async def content(self) -> str:
        raise RuntimeError("page closed")


# ---------------------------------------------------------------------------
# Fake streaming HTTP client (reused pattern from test_media_harvester.py)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int, body: bytes, mime: str = "") -> None:
        self.status_code = status_code
        self.headers = {"content-type": mime} if mime else {}
        self._body = body

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        data = self._body
        for i in range(0, len(data), max(1, chunk_size)):
            yield data[i : i + chunk_size]

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None


class _FakeClient:
    def __init__(self, registry: dict[str, _FakeResponse]) -> None:
        self.registry = registry

    def stream(self, method: str, url: str, *, headers=None, timeout=None) -> _FakeResponse:
        response = self.registry.get(url)
        if response is None:
            return _FakeResponse(status_code=404, body=b"")
        return response

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# is_pure_media_goal heuristic
# ---------------------------------------------------------------------------


class TestIsPureMediaGoal:
    def test_pure_download_cn(self) -> None:
        assert is_pure_media_goal("下载这个页面上所有 PDF") is True

    def test_pure_download_en(self) -> None:
        assert is_pure_media_goal("download all images from this page") is True

    def test_extraction_intent_blocks_short_circuit_cn(self) -> None:
        assert is_pure_media_goal("下载所有图片并提取每张的 alt 文本") is False

    def test_extraction_intent_blocks_short_circuit_en(self) -> None:
        assert is_pure_media_goal("download all images and extract their captions") is False

    def test_summary_intent_blocks_short_circuit_cn(self) -> None:
        assert is_pure_media_goal("下载这份合同 PDF 并总结要点") is False

    def test_summary_intent_blocks_short_circuit_en(self) -> None:
        assert is_pure_media_goal("download this PDF and summarize it") is False

    def test_analysis_intent_blocks_short_circuit_cn(self) -> None:
        assert is_pure_media_goal("把所有视频抓下来再分析观看趋势") is False

    def test_read_intent_blocks_short_circuit_cn(self) -> None:
        assert is_pure_media_goal("阅读这份报告并保存附件") is False

    def test_translate_intent_blocks_short_circuit(self) -> None:
        assert is_pure_media_goal("download these PDFs and translate them") is False

    def test_blank_goal_returns_false(self) -> None:
        assert is_pure_media_goal("") is False
        assert is_pure_media_goal("   ") is False

    def test_token_must_be_word_boundary(self) -> None:
        # "readable" should NOT be treated as containing "read" -- we need
        # word boundaries for English tokens so non-instructive substrings
        # don't accidentally block the fast path.
        assert is_pure_media_goal("save all readable.txt files") is True


# ---------------------------------------------------------------------------
# maybe_run_media_harvest: skip branches
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class TestMaybeRunMediaHarvestSkipBranches:
    def test_no_route_skips(self) -> None:
        page = _FakePage(html="<html></html>")
        result = _run(
            maybe_run_media_harvest(
                page=page,
                capability_route=None,
                run_id="r1",
                goal="any",
            )
        )
        assert isinstance(result, MediaHarvestHookResult)
        assert result.triggered is False
        assert result.skip_reason == "no_route"

    def test_route_without_output_contract_skips(self) -> None:
        page = _FakePage(html="<html></html>")
        result = _run(
            maybe_run_media_harvest(
                page=page,
                capability_route={"intent": "x"},
                run_id="r1",
                goal="any",
            )
        )
        assert result.triggered is False
        assert result.skip_reason == "no_output_kind"

    def test_non_media_output_kind_skips(self) -> None:
        page = _FakePage(html="<html></html>")
        result = _run(
            maybe_run_media_harvest(
                page=page,
                capability_route={"output_contract": {"output_kind": "data_table"}},
                run_id="r1",
                goal="extract product list",
            )
        )
        assert result.triggered is False
        assert result.skip_reason == "output_kind_not_media:data_table"
        assert result.output_kind == "data_table"

    def test_none_page_skips_with_reason(self) -> None:
        result = _run(
            maybe_run_media_harvest(
                page=None,
                capability_route={"output_contract": {"output_kind": "media_pdf"}},
                run_id="r1",
                goal="download all PDFs",
            )
        )
        assert result.triggered is False
        assert result.skip_reason == "no_page"

    def test_blank_run_id_skips(self) -> None:
        page = _FakePage(html="<html></html>")
        result = _run(
            maybe_run_media_harvest(
                page=page,
                capability_route={"output_contract": {"output_kind": "media_pdf"}},
                run_id="   ",
                goal="download all PDFs",
            )
        )
        assert result.triggered is False
        assert result.skip_reason == "no_run_id"

    def test_page_content_failure_captured(self) -> None:
        page = _FakePageRaisingContent()
        result = _run(
            maybe_run_media_harvest(
                page=page,
                capability_route={"output_contract": {"output_kind": "media_pdf"}},
                run_id="r1",
                goal="download all PDFs",
            )
        )
        assert result.triggered is False
        assert result.skip_reason.startswith("page_html_failed:")


# ---------------------------------------------------------------------------
# maybe_run_media_harvest: end-to-end harvest dispatch
# ---------------------------------------------------------------------------


def _patch_default_client(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    """Force ``harvest_to_run`` (as imported by agent_hook) to use our fake."""

    import visual_web_agent.media_harvester.downloader as downloader_module

    original_download = downloader_module.download_candidate

    def patched(candidate, artifacts_dir, *, client=None, **kwargs):
        return original_download(candidate, artifacts_dir, client=client or _injected_client, **kwargs)

    _injected_client = client
    monkeypatch.setattr(downloader_module, "download_candidate", patched)
    # ``harvester`` imports download_candidate by name -> patch the rebound name.
    import visual_web_agent.media_harvester.harvester as harvester_module

    monkeypatch.setattr(harvester_module, "download_candidate", patched)


class TestMaybeRunMediaHarvestEndToEnd:
    def test_pdf_download_succeeds_and_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        html = (
            '<html><body>'
            '<a href="/docs/report.pdf">Report</a>'
            '<a href="/docs/handbook.pdf">Handbook</a>'
            '</body></html>'
        )
        page = _FakePage(html=html, url="https://example.com/landing")
        pdf_bytes = b"%PDF-1.4\n%fake pdf body\n%%EOF"
        client = _FakeClient(
            {
                "https://example.com/docs/report.pdf": _FakeResponse(
                    status_code=200, body=pdf_bytes, mime="application/pdf"
                ),
                "https://example.com/docs/handbook.pdf": _FakeResponse(
                    status_code=200, body=pdf_bytes + b" v2", mime="application/pdf"
                ),
            }
        )
        _patch_default_client(monkeypatch, client)

        result = _run(
            maybe_run_media_harvest(
                page=page,
                capability_route={"output_contract": {"output_kind": "media_pdf"}},
                run_id="run_e2_hook_01",
                goal="把这个页面所有 PDF 下载到本地",
                base_dir=tmp_path,
            )
        )

        assert result.triggered is True
        assert result.skip_reason == ""
        assert result.output_kind == "media_pdf"
        assert result.downloaded_count == 2
        assert result.failed_count == 0
        assert result.success is True
        assert result.short_circuit is True
        # Manifest dedup keeps both because bodies differ by " v2".
        assert result.manifest_appended >= 1
        # Two files should actually exist on disk.
        artifacts_dir = tmp_path / "run_e2_hook_01" / "artifacts"
        assert artifacts_dir.exists()
        files = list(artifacts_dir.iterdir())
        assert len(files) == 2
        manifest_path = tmp_path / "run_e2_hook_01" / "manifest.json"
        assert manifest_path.exists()
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest_payload["version"] == "manifest.v1"
        assert len(manifest_payload["items"]) >= 1

    def test_mixed_goal_does_not_short_circuit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        html = '<a href="/x.pdf">x</a>'
        page = _FakePage(html=html, url="https://example.com/")
        client = _FakeClient(
            {
                "https://example.com/x.pdf": _FakeResponse(
                    status_code=200, body=b"%PDF-1.4 ok", mime="application/pdf"
                )
            }
        )
        _patch_default_client(monkeypatch, client)

        result = _run(
            maybe_run_media_harvest(
                page=page,
                capability_route={"output_contract": {"output_kind": "media_pdf"}},
                run_id="run_e2_hook_02",
                goal="下载这份 PDF 并阅读后总结要点",  # contains 阅读/总结
                base_dir=tmp_path,
            )
        )

        assert result.triggered is True
        assert result.downloaded_count == 1
        assert result.success is True
        assert result.short_circuit is False  # blocked by 阅读 / 总结

    def test_empty_page_runs_but_short_circuit_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        page = _FakePage(html="<html><body></body></html>", url="https://example.com/")
        client = _FakeClient({})
        _patch_default_client(monkeypatch, client)

        result = _run(
            maybe_run_media_harvest(
                page=page,
                capability_route={"output_contract": {"output_kind": "media_pdf"}},
                run_id="run_e2_hook_03",
                goal="download all PDFs",
                base_dir=tmp_path,
            )
        )

        assert result.triggered is True
        assert result.candidate_count == 0
        assert result.downloaded_count == 0
        assert result.failed_count == 0
        assert result.success is False
        assert result.short_circuit is False

    def test_all_downloads_fail_no_short_circuit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        html = '<a href="/missing.pdf">m</a>'
        page = _FakePage(html=html, url="https://example.com/")
        client = _FakeClient({})  # every URL returns 404 via fake stub
        _patch_default_client(monkeypatch, client)

        result = _run(
            maybe_run_media_harvest(
                page=page,
                capability_route={"output_contract": {"output_kind": "media_pdf"}},
                run_id="run_e2_hook_04",
                goal="download all PDFs",
                base_dir=tmp_path,
            )
        )

        assert result.triggered is True
        assert result.candidate_count == 1
        assert result.downloaded_count == 0
        assert result.failed_count == 1
        assert result.success is False
        assert result.short_circuit is False

    def test_file_generic_output_kind_is_triggered(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # ``download`` attribute is required for the collector to treat an
        # arbitrary anchor as a downloadable file (otherwise plain
        # navigation links would flood the candidate list).
        html = '<a href="/data.bin" download>d</a>'
        page = _FakePage(html=html, url="https://example.com/")
        client = _FakeClient(
            {
                "https://example.com/data.bin": _FakeResponse(
                    status_code=200, body=b"\x00\x01\x02\x03", mime="application/octet-stream"
                )
            }
        )
        _patch_default_client(monkeypatch, client)

        result = _run(
            maybe_run_media_harvest(
                page=page,
                capability_route={"output_contract": {"output_kind": "file_generic"}},
                run_id="run_e2_hook_05",
                goal="抓取这个页面的所有文件",
                base_dir=tmp_path,
            )
        )

        assert result.triggered is True
        assert result.output_kind == "file_generic"
        assert result.downloaded_count == 1


# ---------------------------------------------------------------------------
# Result dataclass shape
# ---------------------------------------------------------------------------


class TestMediaHarvestHookResultDataclass:
    def test_default_to_dict_is_serialisable(self) -> None:
        result = MediaHarvestHookResult()
        d = result.to_dict()
        # Round-trip through JSON to make sure nothing exotic leaks in.
        encoded = json.dumps(d, ensure_ascii=False)
        decoded = json.loads(encoded)
        assert decoded["triggered"] is False
        assert decoded["short_circuit"] is False
        assert decoded["downloaded_count"] == 0
        assert decoded["report"] == {}

    def test_populated_to_dict_round_trips(self) -> None:
        report = HarvestReport(run_id="r", output_kind="media_pdf")
        result = MediaHarvestHookResult(
            triggered=True,
            short_circuit=True,
            success=True,
            output_kind="media_pdf",
            downloaded_count=3,
            failed_count=0,
            manifest_appended=3,
            candidate_count=5,
            report_dict=report.to_dict(),
        )
        d = result.to_dict()
        encoded = json.dumps(d, ensure_ascii=False)
        decoded = json.loads(encoded)
        assert decoded["triggered"] is True
        assert decoded["downloaded_count"] == 3
        assert decoded["manifest_appended"] == 3
        assert decoded["report"]["run_id"] == "r"
        assert decoded["report"]["output_kind"] == "media_pdf"

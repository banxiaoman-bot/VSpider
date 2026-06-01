"""Tests for ``visual_web_agent.media_harvester``.

Pure-Python coverage: HTML candidate collection, downloader semantics
against an injected fake ``StreamingClient``, manifest interaction.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from visual_web_agent.media_harvester import (
    MediaCandidate,
    MEDIA_KINDS,
    classify_url,
    collect_from_html,
    dedupe_candidates,
)
from visual_web_agent.media_harvester.downloader import (
    DownloadOutcome,
    download_candidate,
)
from visual_web_agent.media_harvester.harvester import (
    HarvestReport,
    harvest_to_run,
    select_candidates_for_output_kind,
)


# ---------------------------------------------------------------------------
# Fake streaming HTTP client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int, body: bytes, mime: str = "") -> None:
        self.status_code = status_code
        self.headers = {"content-type": mime} if mime else {}
        self._body = body
        self._consumed = False

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        data = self._body
        for i in range(0, len(data), max(1, chunk_size)):
            yield data[i : i + chunk_size]
        self._consumed = True

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # noqa: D401
        return None


class _FakeClient:
    def __init__(self, registry: dict[str, _FakeResponse]) -> None:
        self.registry = registry
        self.recorded_headers: list[dict[str, str]] = []

    def stream(self, method: str, url: str, *, headers=None, timeout=None) -> _FakeResponse:
        self.recorded_headers.append(dict(headers or {}))
        response = self.registry.get(url)
        if response is None:
            return _FakeResponse(status_code=404, body=b"")
        return response

    def close(self) -> None:  # pragma: no cover - parity with httpx.Client
        pass


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------


class TestClassifyUrl:
    def test_image_by_suffix(self) -> None:
        assert classify_url("https://x.com/a.png") == "media_image"

    def test_video_by_suffix(self) -> None:
        assert classify_url("https://x.com/clip.mp4") == "media_video"

    def test_pdf_by_suffix(self) -> None:
        assert classify_url("https://x.com/doc.pdf") == "media_pdf"

    def test_archive_by_suffix(self) -> None:
        assert classify_url("https://x.com/bundle.zip") == "media_archive"

    def test_mime_overrides_suffix(self) -> None:
        assert classify_url("https://x.com/file", mime_hint="image/webp") == "media_image"

    def test_unknown_falls_back_to_file_generic(self) -> None:
        assert classify_url("https://x.com/page") == "file_generic"


# ---------------------------------------------------------------------------
# HTML collector
# ---------------------------------------------------------------------------


class TestCollectFromHtml:
    BASE = "https://example.com/article/1"

    def test_collects_img(self) -> None:
        html = '<img src="/static/a.png" alt="hero">'
        out = collect_from_html(html, self.BASE)
        assert len(out) == 1
        assert out[0].url == "https://example.com/static/a.png"
        assert out[0].kind == "media_image"
        assert out[0].alt == "hero"

    def test_collects_srcset(self) -> None:
        html = '<img src="/a.png" srcset="/a@2x.png 2x, /a@3x.png 3x">'
        out = collect_from_html(html, self.BASE)
        urls = sorted(c.url for c in out)
        assert "https://example.com/a.png" in urls
        assert "https://example.com/a@2x.png" in urls
        assert "https://example.com/a@3x.png" in urls

    def test_collects_video_and_audio(self) -> None:
        html = (
            '<video src="/v.mp4"></video>'
            '<audio src="/a.mp3"></audio>'
        )
        out = collect_from_html(html, self.BASE)
        kinds = {c.kind for c in out}
        assert "media_video" in kinds
        assert "media_audio" in kinds

    def test_collects_source_inside_video(self) -> None:
        html = (
            '<video poster="/p.jpg">'
            '  <source src="/clip.webm" type="video/webm">'
            '  <source src="/clip.mp4" type="video/mp4">'
            '</video>'
        )
        out = collect_from_html(html, self.BASE)
        srcs = {c.url for c in out if c.selector == "source"}
        assert "https://example.com/clip.webm" in srcs
        assert "https://example.com/clip.mp4" in srcs
        for c in out:
            if c.selector == "source":
                assert c.kind == "media_video"

    def test_collects_anchor_download(self) -> None:
        html = '<a href="/files/report.pdf">read</a><a href="/page">navigate</a>'
        out = collect_from_html(html, self.BASE)
        assert any(c.url.endswith("/files/report.pdf") and c.kind == "media_pdf" for c in out)
        # plain navigation link must NOT be collected
        assert not any(c.url.endswith("/page") for c in out)

    def test_collects_anchor_with_download_attribute(self) -> None:
        html = '<a href="/some" download="x.bin">click</a>'
        out = collect_from_html(html, self.BASE)
        assert any(c.url.endswith("/some") and c.extra.get("download_attr") for c in out)

    def test_strips_tracking_query(self) -> None:
        html = '<img src="https://cdn.com/x.png?utm_source=tw&id=abc">'
        out = collect_from_html(html, self.BASE)
        assert out[0].url == "https://cdn.com/x.png?id=abc"

    def test_ignores_javascript_and_data_urls(self) -> None:
        html = '<img src="javascript:void(0)"><img src="data:image/png;base64,xxx">'
        out = collect_from_html(html, self.BASE)
        assert out == []

    def test_empty_input(self) -> None:
        assert collect_from_html("", self.BASE) == []

    def test_dedupe_candidates_preserves_order(self) -> None:
        a = MediaCandidate(url="https://x.com/a.png", kind="media_image")
        b = MediaCandidate(url="https://x.com/b.png", kind="media_image")
        c = MediaCandidate(url="https://x.com/a.png", kind="media_image")
        out = dedupe_candidates([a, b, c])
        assert [x.url for x in out] == [a.url, b.url]


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------


class TestDownloadCandidate:
    def test_downloads_and_hashes(self, tmp_path: Path) -> None:
        body = b"\x89PNG\r\n\x1a\nhello-world"
        client = _FakeClient({
            "https://x.com/a.png": _FakeResponse(
                status_code=200, body=body, mime="image/png"
            ),
        })
        candidate = MediaCandidate(
            url="https://x.com/a.png",
            kind="media_image",
            referrer="https://x.com/index",
        )
        outcome = download_candidate(candidate, tmp_path, client=client)
        assert outcome.ok
        assert outcome.size == len(body)
        assert outcome.sha256
        assert outcome.final_kind == "media_image"
        # Referrer is propagated
        assert client.recorded_headers[0].get("Referer") == "https://x.com/index"
        assert Path(outcome.path).exists()
        assert outcome.path.endswith(".png")

    def test_http_error_returns_failure(self, tmp_path: Path) -> None:
        client = _FakeClient({})
        candidate = MediaCandidate(url="https://x.com/missing.png", kind="media_image")
        outcome = download_candidate(candidate, tmp_path, client=client)
        assert outcome.ok is False
        assert outcome.status_code == 404
        assert "http_404" in outcome.error

    def test_dedup_same_sha_overwrites_no_file(self, tmp_path: Path) -> None:
        body = b"%PDF-1.4\nidempotent"
        client = _FakeClient({
            "https://x.com/a.pdf": _FakeResponse(
                status_code=200, body=body, mime="application/pdf"
            ),
            "https://x.com/b.pdf": _FakeResponse(
                status_code=200, body=body, mime="application/pdf"
            ),
        })
        c1 = MediaCandidate(url="https://x.com/a.pdf", kind="media_pdf")
        c2 = MediaCandidate(url="https://x.com/b.pdf", kind="media_pdf")
        o1 = download_candidate(c1, tmp_path, client=client)
        o2 = download_candidate(c2, tmp_path, client=client)
        assert o1.ok and o2.ok
        assert o1.sha256 == o2.sha256
        assert o1.path == o2.path  # same content -> same target path
        files = [p for p in tmp_path.iterdir() if p.is_file()]
        # Exactly one artifact on disk despite two downloads of same content
        assert len(files) == 1

    def test_max_bytes_aborts_oversize(self, tmp_path: Path) -> None:
        body = b"x" * 1024
        client = _FakeClient({
            "https://x.com/big.bin": _FakeResponse(
                status_code=200, body=body
            ),
        })
        candidate = MediaCandidate(url="https://x.com/big.bin", kind="file_generic")
        outcome = download_candidate(
            candidate, tmp_path, client=client, max_bytes=10
        )
        assert outcome.ok is False
        assert "size_exceeded_limit" in outcome.error

    def test_empty_response_failure(self, tmp_path: Path) -> None:
        client = _FakeClient({
            "https://x.com/empty.png": _FakeResponse(status_code=200, body=b""),
        })
        candidate = MediaCandidate(url="https://x.com/empty.png", kind="media_image")
        outcome = download_candidate(candidate, tmp_path, client=client)
        assert outcome.ok is False
        assert outcome.error == "empty_response"


# ---------------------------------------------------------------------------
# Harvester
# ---------------------------------------------------------------------------


class TestHarvestToRun:
    def test_end_to_end_writes_artifacts_and_manifest(self, tmp_path: Path) -> None:
        png = b"\x89PNG\r\n\x1a\nimage-bytes"
        pdf = b"%PDF-1.4\npdf-bytes"
        client = _FakeClient({
            "https://x.com/a.png": _FakeResponse(status_code=200, body=png, mime="image/png"),
            "https://x.com/b.pdf": _FakeResponse(status_code=200, body=pdf, mime="application/pdf"),
        })
        candidates = [
            MediaCandidate(url="https://x.com/a.png", kind="media_image"),
            MediaCandidate(url="https://x.com/b.pdf", kind="media_pdf"),
        ]
        report = harvest_to_run(
            candidates, "run_a",
            output_kind="mixed",
            base_dir=tmp_path,
            client=client,
        )
        assert isinstance(report, HarvestReport)
        assert len(report.downloaded) == 2
        assert report.failed == []
        assert report.manifest_appended == 2

        manifest_payload = json.loads(
            (tmp_path / "run_a" / "manifest.json").read_text(encoding="utf-8")
        )
        items = manifest_payload["items"]
        assert len(items) == 2
        kinds = sorted(it["kind"] for it in items)
        assert kinds == ["media_image", "media_pdf"]

        artifact_dir = tmp_path / "run_a" / "artifacts"
        assert artifact_dir.exists()
        files = sorted(p.name for p in artifact_dir.iterdir())
        assert len(files) == 2

    def test_filter_by_output_kind(self, tmp_path: Path) -> None:
        client = _FakeClient({
            "https://x.com/a.png": _FakeResponse(status_code=200, body=b"png", mime="image/png"),
            "https://x.com/b.pdf": _FakeResponse(status_code=200, body=b"%PDF-1.4 pdf", mime="application/pdf"),
        })
        candidates = [
            MediaCandidate(url="https://x.com/a.png", kind="media_image"),
            MediaCandidate(url="https://x.com/b.pdf", kind="media_pdf"),
        ]
        report = harvest_to_run(
            candidates, "run_pdf_only",
            output_kind="media_pdf",
            base_dir=tmp_path,
            client=client,
        )
        assert len(report.downloaded) == 1
        assert report.downloaded[0].final_kind == "media_pdf"
        # No image should have been fetched
        assert not any(
            "https://x.com/a.png" in h.get("Referer", "") for h in client.recorded_headers
        )

    def test_dedup_same_content_two_urls_in_manifest(self, tmp_path: Path) -> None:
        body = b"\x89PNG\r\n\x1a\ndup"
        client = _FakeClient({
            "https://x.com/a.png": _FakeResponse(status_code=200, body=body, mime="image/png"),
            "https://x.com/mirror/a.png": _FakeResponse(status_code=200, body=body, mime="image/png"),
        })
        candidates = [
            MediaCandidate(url="https://x.com/a.png", kind="media_image"),
            MediaCandidate(url="https://x.com/mirror/a.png", kind="media_image"),
        ]
        report = harvest_to_run(
            candidates, "run_dedup",
            output_kind="media_image",
            base_dir=tmp_path,
            client=client,
        )
        assert len(report.downloaded) == 2  # both completed downloads
        assert report.manifest_appended == 1  # but dedup keeps one entry
        items = json.loads(
            (tmp_path / "run_dedup" / "manifest.json").read_text(encoding="utf-8")
        )["items"]
        assert len(items) == 1
        assert sorted(items[0]["source_url"]) == sorted([
            "https://x.com/a.png",
            "https://x.com/mirror/a.png",
        ])

    def test_max_items_truncates(self, tmp_path: Path) -> None:
        client = _FakeClient({
            f"https://x.com/{i}.png": _FakeResponse(status_code=200, body=f"img-{i}".encode(), mime="image/png")
            for i in range(5)
        })
        candidates = [
            MediaCandidate(url=f"https://x.com/{i}.png", kind="media_image")
            for i in range(5)
        ]
        report = harvest_to_run(
            candidates, "run_cap",
            output_kind="media_image",
            base_dir=tmp_path,
            client=client,
            max_items=2,
        )
        assert len(report.downloaded) == 2
        assert len(report.skipped) == 3
        assert all(s.get("reason") == "max_items_limit" for s in report.skipped)

    def test_failure_recorded_separately(self, tmp_path: Path) -> None:
        client = _FakeClient({
            "https://x.com/ok.png": _FakeResponse(status_code=200, body=b"\x89PNG\r\nok", mime="image/png"),
            # missing entry => 404
        })
        candidates = [
            MediaCandidate(url="https://x.com/ok.png", kind="media_image"),
            MediaCandidate(url="https://x.com/missing.png", kind="media_image"),
        ]
        report = harvest_to_run(
            candidates, "run_mix_fail",
            output_kind="media_image",
            base_dir=tmp_path,
            client=client,
        )
        assert len(report.downloaded) == 1
        assert len(report.failed) == 1
        assert report.failed[0].status_code == 404


# ---------------------------------------------------------------------------
# select_candidates_for_output_kind helper
# ---------------------------------------------------------------------------


class TestSelectCandidatesForOutputKind:
    def test_media_pdf_keeps_only_pdf(self) -> None:
        cs = [
            MediaCandidate(url="https://x.com/a.png", kind="media_image"),
            MediaCandidate(url="https://x.com/b.pdf", kind="media_pdf"),
        ]
        kept = select_candidates_for_output_kind(cs, "media_pdf")
        assert [c.kind for c in kept] == ["media_pdf"]

    def test_mixed_keeps_all_media(self) -> None:
        cs = [
            MediaCandidate(url="https://x.com/a.png", kind="media_image"),
            MediaCandidate(url="https://x.com/b.pdf", kind="media_pdf"),
            MediaCandidate(url="https://x.com/c.mp4", kind="media_video"),
        ]
        kept = select_candidates_for_output_kind(cs, "mixed")
        assert len(kept) == 3

    def test_unknown_kind_keeps_all(self) -> None:
        cs = [MediaCandidate(url="https://x.com/a.png", kind="media_image")]
        kept = select_candidates_for_output_kind(cs, "not_a_kind")
        assert len(kept) == 1

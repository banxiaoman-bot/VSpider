"""Slice DL-RESUME1: media downloader HTTP Range / If-Range resume.

The downloader streamed to a *random* ``.part`` and deleted it on failure, so a
retry restarted from byte 0. Opt-in ``resume=True`` now uses a stable per-URL
``.part`` (+ a ``.meta`` validator sidecar), sends ``Range``/``If-Range`` to
continue, appends on ``206``, overwrites on ``200`` (server ignored range /
resource changed), and *keeps* the ``.part`` on mid-stream failure so the next
call continues. Default ``resume=False`` is byte-identical to the legacy path.

Fake StreamingClient (Range-aware); no sockets.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterator

from visual_web_agent.media_harvester.candidates import MediaCandidate
from visual_web_agent.media_harvester.downloader import (
    _url_part_key,
    download_candidate,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _RangeResponse:
    def __init__(self, *, status_code: int, body: bytes, mime: str = "", extra: dict | None = None) -> None:
        self.status_code = status_code
        self.headers = {"content-type": mime} if mime else {}
        if extra:
            self.headers.update(extra)
        self._body = body

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        for i in range(0, len(self._body), max(1, chunk_size)):
            yield self._body[i : i + chunk_size]

    def __enter__(self) -> "_RangeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _RangeClient:
    """200 full on no Range; 206 partial when a Range header is present (unless
    ``honor_range`` is False, which always replies 200 full)."""

    def __init__(self, full: bytes, *, etag: str = "abc", honor_range: bool = True, mime: str = "application/pdf") -> None:
        self.full = full
        self.etag = etag
        self.honor_range = honor_range
        self.mime = mime
        self.recorded_headers: list[dict[str, str]] = []

    def stream(self, method: str, url: str, *, headers=None, timeout=None) -> _RangeResponse:
        h = dict(headers or {})
        self.recorded_headers.append(h)
        rng = h.get("Range") or h.get("range")
        if rng and self.honor_range:
            start = int(rng.split("=", 1)[1].split("-", 1)[0])
            return _RangeResponse(
                status_code=206,
                body=self.full[start:],
                mime=self.mime,
                extra={"etag": self.etag, "content-range": f"bytes {start}-{len(self.full) - 1}/{len(self.full)}"},
            )
        return _RangeResponse(status_code=200, body=self.full, mime=self.mime, extra={"etag": self.etag})

    def close(self) -> None:
        pass


def test_fresh_resume_download_no_range_header(tmp_path) -> None:
    body = b"PDFDATA" * 100
    client = _RangeClient(body)
    cand = MediaCandidate(url="https://x.com/a.pdf", kind="media_pdf")
    out = download_candidate(cand, tmp_path, client=client, resume=True)
    assert out.ok
    assert out.size == len(body)
    assert out.sha256 == _sha(body)
    assert not any("Range" in h for h in client.recorded_headers)


def test_resume_continues_from_partial(tmp_path) -> None:
    body = b"0123456789" * 50  # 500 bytes
    part = Path(tmp_path) / f".{_url_part_key('https://x.com/big.bin')}.part"
    part.write_bytes(body[:200])
    Path(str(part) + ".meta").write_text('{"validator": "abc"}', encoding="utf-8")
    client = _RangeClient(body, etag="abc", mime="application/octet-stream")
    cand = MediaCandidate(url="https://x.com/big.bin", kind="file_generic")
    out = download_candidate(cand, tmp_path, client=client, resume=True)
    assert out.ok
    assert out.size == len(body)
    assert out.sha256 == _sha(body)  # appended, not re-downloaded
    assert any(h.get("Range") == "bytes=200-" for h in client.recorded_headers)
    assert any(h.get("If-Range") == "abc" for h in client.recorded_headers)
    assert not part.exists()  # renamed to content-addressed final, meta cleaned
    assert not Path(str(part) + ".meta").exists()


def test_resume_overwrites_when_server_ignores_range(tmp_path) -> None:
    body = b"abcdef" * 80
    part = Path(tmp_path) / f".{_url_part_key('https://x.com/c.bin')}.part"
    part.write_bytes(b"STALEPARTIAL")
    Path(str(part) + ".meta").write_text('{"validator": "v1"}', encoding="utf-8")
    client = _RangeClient(body, honor_range=False, mime="application/octet-stream")
    cand = MediaCandidate(url="https://x.com/c.bin", kind="file_generic")
    out = download_candidate(cand, tmp_path, client=client, resume=True)
    assert out.ok
    assert out.size == len(body)
    assert out.sha256 == _sha(body)  # fresh full content, no stale prefix


def test_resume_keeps_part_on_midstream_failure(tmp_path) -> None:
    class _BoomResponse:
        status_code = 200
        headers = {"content-type": "application/octet-stream", "etag": "z"}

        def iter_bytes(self, chunk_size: int = 64 * 1024):
            yield b"first-chunk"
            raise RuntimeError("connection reset")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _BoomClient:
        def __init__(self) -> None:
            self.recorded_headers: list[dict] = []

        def stream(self, method, url, *, headers=None, timeout=None):
            self.recorded_headers.append(dict(headers or {}))
            return _BoomResponse()

        def close(self):
            pass

    cand = MediaCandidate(url="https://x.com/d.bin", kind="file_generic")
    out = download_candidate(cand, tmp_path, client=_BoomClient(), resume=True)
    assert out.ok is False
    part = Path(tmp_path) / f".{_url_part_key('https://x.com/d.bin')}.part"
    assert part.exists()  # preserved for next resume
    assert part.read_bytes() == b"first-chunk"


def test_non_resume_default_sends_no_range_and_cleans_part_on_failure(tmp_path) -> None:
    class _BoomResponse:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}

        def iter_bytes(self, chunk_size: int = 64 * 1024):
            yield b"x"
            raise RuntimeError("boom")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _BoomClient:
        def __init__(self) -> None:
            self.recorded_headers: list[dict] = []

        def stream(self, method, url, *, headers=None, timeout=None):
            self.recorded_headers.append(dict(headers or {}))
            return _BoomResponse()

        def close(self):
            pass

    client = _BoomClient()
    cand = MediaCandidate(url="https://x.com/e.bin", kind="file_generic")
    out = download_candidate(cand, tmp_path, client=client)  # resume=False (default)
    assert out.ok is False
    assert not any("Range" in h for h in client.recorded_headers)
    assert not list(Path(tmp_path).glob("*.part"))

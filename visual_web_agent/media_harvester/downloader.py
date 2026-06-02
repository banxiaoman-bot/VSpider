"""Stream-download a :class:`MediaCandidate` to disk with sha256 integrity.

The downloader is intentionally minimal:

- Streaming write to a ``.part`` tempfile so partial downloads can be
  resumed or discarded.
- Computes sha256 on the fly. Final filename is content-addressed
  (``<sha>.<ext>``) which makes deduplication trivial.
- Sniffs Content-Type both from headers and (as fallback) from the first
  few bytes via :mod:`visual_web_agent.upload_store`.
- HTTP client is abstracted behind a tiny ``StreamingClient`` protocol so
  unit tests can inject a fake without spinning up sockets.

Range / If-Range resume is opt-in via ``resume=True`` (DL-RESUME1): a stable
per-URL ``.part`` + a ``.meta`` validator let an interrupted download continue
instead of restarting. Default ``resume=False`` keeps the legacy single-pass
behaviour.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Protocol, runtime_checkable

from visual_web_agent.upload_store import sniff_mime_and_ext

from .candidates import MediaCandidate, classify_url


DEFAULT_TIMEOUT_S = 30.0


# Sensible browser-shaped headers so naive hot-link protections do not 403 us.
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
        "VSpider-MediaHarvester/1.0"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.6,zh;q=0.5",
}


@runtime_checkable
class StreamingResponse(Protocol):
    status_code: int
    headers: dict[str, str] | Any

    def iter_bytes(self, chunk_size: int = ...) -> Iterator[bytes]: ...

    def __enter__(self): ...
    def __exit__(self, exc_type, exc_val, exc_tb): ...


@runtime_checkable
class StreamingClient(Protocol):
    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = ...,
        timeout: float | None = ...,
    ) -> StreamingResponse: ...


@dataclass
class DownloadOutcome:
    """Result of one media download attempt."""

    ok: bool
    candidate: MediaCandidate
    path: str = ""
    size: int = 0
    sha256: str = ""
    mime: str = ""
    final_kind: str = "file_generic"
    status_code: int = 0
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "url": self.candidate.url,
            "path": self.path,
            "size": int(self.size or 0),
            "sha256": self.sha256,
            "mime": self.mime,
            "kind": self.final_kind,
            "status_code": int(self.status_code or 0),
            "error": self.error,
            "extra": dict(self.extra or {}),
        }


def _select_extension(suggested_ext: str, candidate_url: str) -> str:
    if suggested_ext:
        return suggested_ext
    from urllib.parse import urlparse

    path = urlparse(candidate_url).path or ""
    idx = path.rfind(".")
    if idx >= 0:
        ext = path[idx:].lower()
        if 1 < len(ext) <= 6 and ext.lstrip(".").isalnum():
            return ext
    return ""


def _httpx_client(timeout: float, follow_redirects: bool = True) -> StreamingClient:
    import httpx

    return httpx.Client(
        timeout=timeout,
        follow_redirects=follow_redirects,
        headers=dict(DEFAULT_HEADERS),
    )


def _url_part_key(url: str) -> str:
    """Stable per-URL key for the resumable ``.part`` filename."""
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()[:24]


def _validator_from_headers(headers: Any) -> str:
    """ETag (preferred) or Last-Modified, used as the If-Range validator."""
    try:
        etag = headers.get("etag") or headers.get("ETag") or ""
        if etag:
            return str(etag)
        return str(headers.get("last-modified") or headers.get("Last-Modified") or "")
    except AttributeError:
        return ""


def _read_part_validator(meta_path: Path | None) -> str:
    if meta_path is None or not meta_path.exists():
        return ""
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return str(data.get("validator") or "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def _write_part_validator(meta_path: Path | None, validator: str) -> None:
    if meta_path is None:
        return
    try:
        meta_path.write_text(
            json.dumps({"validator": str(validator or "")}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _seed_hasher_from_part(part_name: str, hasher: Any, chunk_size: int) -> tuple[int, bytes]:
    """Fold an existing ``.part`` into ``hasher`` so a 206 append yields the
    same sha256 as a single-pass download. Returns ``(bytes_seeded, head_sample)``."""
    size = 0
    head_sample = b""
    try:
        with open(part_name, "rb") as existing:
            while True:
                block = existing.read(max(1, chunk_size))
                if not block:
                    break
                if not head_sample:
                    head_sample = block[:1024]
                hasher.update(block)
                size += len(block)
    except Exception:
        return 0, b""
    return size, head_sample


def _discard_part(part_name: str, meta_path: Path | None) -> None:
    try:
        Path(part_name).unlink(missing_ok=True)
    except Exception:
        pass
    if meta_path is not None:
        try:
            meta_path.unlink(missing_ok=True)
        except Exception:
            pass


def download_candidate(
    candidate: MediaCandidate,
    dest_dir: str | Path,
    *,
    client: StreamingClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    chunk_size: int = 64 * 1024,
    extra_headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
    resume: bool = False,
) -> DownloadOutcome:
    """Download ``candidate.url`` into ``dest_dir`` with sha256 dedup.

    Returns a :class:`DownloadOutcome` even on failure - the caller decides
    whether to retry. Uses ``httpx.Client`` by default, but any object
    that conforms to :class:`StreamingClient` is acceptable (great for
    tests).
    """

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    headers: dict[str, str] = dict(DEFAULT_HEADERS)
    if candidate.referrer:
        headers["Referer"] = candidate.referrer
    if extra_headers:
        headers.update(extra_headers)

    own_client = False
    if client is None:
        try:
            client = _httpx_client(timeout=timeout)
            own_client = True
        except Exception as exc:
            return DownloadOutcome(
                ok=False,
                candidate=candidate,
                final_kind=candidate.kind,
                error=f"client_init_failed: {exc}",
            )

    part_meta_path = None
    fd = None
    if resume:
        part_path = dest / f".{_url_part_key(candidate.url)}.part"
        part_meta_path = Path(str(part_path) + ".meta")
        existing_size = part_path.stat().st_size if part_path.exists() else 0
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"
            _validator = _read_part_validator(part_meta_path)
            if _validator:
                headers["If-Range"] = _validator
        tmp_name = str(part_path)
    else:
        fd, tmp_name = tempfile.mkstemp(prefix="download.", suffix=".part", dir=str(dest))

    hasher = hashlib.sha256()
    size = 0
    head_sample = b""
    status_code = 0
    response_mime = ""
    appended = False

    try:
        with client.stream("GET", candidate.url, headers=headers, timeout=timeout) as response:  # type: ignore[arg-type]
            status_code = int(getattr(response, "status_code", 0) or 0)
            response_headers = getattr(response, "headers", {}) or {}
            try:
                content_type_raw = response_headers.get("content-type") or response_headers.get("Content-Type") or ""
            except AttributeError:
                content_type_raw = ""
            response_mime = str(content_type_raw or "").split(";", 1)[0].strip().lower()

            if resume and status_code == 416:
                _discard_part(tmp_name, part_meta_path)
                if own_client:
                    try:
                        client.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                return DownloadOutcome(
                    ok=False,
                    candidate=candidate,
                    final_kind=candidate.kind,
                    status_code=status_code,
                    error="range_not_satisfiable",
                )

            if status_code and status_code >= 400:
                if fd is not None:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                if not resume:
                    Path(tmp_name).unlink(missing_ok=True)
                if own_client:
                    try:
                        client.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                return DownloadOutcome(
                    ok=False,
                    candidate=candidate,
                    final_kind=candidate.kind,
                    status_code=status_code,
                    error=f"http_{status_code}",
                )

            appended = bool(resume and status_code == 206 and Path(tmp_name).exists())
            if resume:
                _write_part_validator(part_meta_path, _validator_from_headers(response_headers))
                if appended:
                    size, head_sample = _seed_hasher_from_part(tmp_name, hasher, chunk_size)
                file_handle = open(tmp_name, "ab" if appended else "wb")
            else:
                file_handle = os.fdopen(fd, "wb")

            with file_handle as fh:
                for chunk in response.iter_bytes(chunk_size):
                    if not chunk:
                        continue
                    if not head_sample:
                        head_sample = chunk[:1024]
                    fh.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise OSError(f"size_exceeded_limit:{max_bytes}")
    except Exception as exc:
        if not resume:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass
        if own_client:
            try:
                client.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        return DownloadOutcome(
            ok=False,
            candidate=candidate,
            final_kind=candidate.kind,
            status_code=status_code,
            error=f"download_failed: {exc}",
        )

    if own_client:
        try:
            client.close()  # type: ignore[attr-defined]
        except Exception:
            pass

    if size == 0:
        if not resume:
            Path(tmp_name).unlink(missing_ok=True)
        return DownloadOutcome(
            ok=False,
            candidate=candidate,
            final_kind=candidate.kind,
            status_code=status_code,
            error="empty_response",
        )

    sniffed_mime, sniffed_ext = sniff_mime_and_ext(
        head_sample,
        fallback_filename=candidate.url,
    )
    if response_mime and response_mime != "application/octet-stream":
        final_mime = response_mime
    else:
        final_mime = sniffed_mime
    final_kind = classify_url(candidate.url, mime_hint=final_mime) or candidate.kind or "file_generic"

    final_ext = _select_extension(sniffed_ext, candidate.url)
    sha = hasher.hexdigest()
    final_name = f"{sha}{final_ext}"
    final_path = dest / final_name

    if final_path.exists():
        Path(tmp_name).unlink(missing_ok=True)
    else:
        try:
            os.replace(tmp_name, final_path)
        except Exception as exc:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass
            return DownloadOutcome(
                ok=False,
                candidate=candidate,
                final_kind=final_kind,
                status_code=status_code,
                error=f"rename_failed: {exc}",
            )
    if part_meta_path is not None:
        try:
            part_meta_path.unlink(missing_ok=True)
        except Exception:
            pass

    return DownloadOutcome(
        ok=True,
        candidate=candidate,
        path=str(final_path),
        size=size,
        sha256=sha,
        mime=final_mime,
        final_kind=final_kind,
        status_code=status_code,
        extra={"response_mime": response_mime, "sniffed_mime": sniffed_mime, "resumed": bool(appended)},
    )

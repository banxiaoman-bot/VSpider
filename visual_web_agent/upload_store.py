"""Content-addressed storage for ``temp_uploads/`` (S4).

Replaces the legacy "use original filename, overwrite on conflict" behaviour
with sha256-based deduplication, MIME / magic-byte detection, and a small
GC routine.

This module is intentionally **decoupled from FastAPI**. It accepts already
loaded ``bytes`` (or a file-like object) and returns a metadata dict, so
``api_server.py`` and CLI tooling can both use it without sharing state.

Layout::

    temp_uploads/
      <sha256>.<ext>             # the actual files
      _index.json                # { sha256 -> {filename, mime, size, refs[]} }

Background:

- The legacy code writes ``temp_uploads/<original_filename>`` and overwrites
  silently on conflict, which loses data when two users upload the same name.
- The default filename fallback was ``upload.xlsx`` which embeds an Excel
  bias incompatible with the I/O contract.

Goals:

1. Same content uploaded twice never produces two files.
2. Original filename + MIME survive as metadata.
3. Background GC can purge entries with no live ``refs`` after a TTL.
4. No silent overwrite, no default ``.xlsx`` extension.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterable


_INDEX_FILENAME = "_index.json"
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Magic-byte sniffing
# ---------------------------------------------------------------------------


_MAGIC_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    # (prefix bytes, mime, suggested extension)
    (b"%PDF-",                "application/pdf",                                              ".pdf"),
    (b"\x89PNG\r\n\x1a\n",    "image/png",                                                    ".png"),
    (b"\xff\xd8\xff",         "image/jpeg",                                                   ".jpg"),
    (b"GIF87a",               "image/gif",                                                    ".gif"),
    (b"GIF89a",               "image/gif",                                                    ".gif"),
    (b"RIFF",                 "image/webp",                                                   ".webp"),
    # OLE Compound File header is shared by legacy MS Office (.doc/.xls/.ppt)
    # and other binary formats. We treat the magic as a generic compound-doc
    # signal and rely on the filename extension to pin down the exact app.
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage",                        ""),
    # Zip-based OOXML / OpenDocument all start with PK; we keep the generic
    # zip signature below but suffix-based mime lookup steers OOXML / ODF to
    # their canonical mime when the filename is present.
    (b"PK\x03\x04",           "application/zip",                                              ".zip"),
    (b"\x1f\x8b",             "application/gzip",                                             ".gz"),
    (b"Rar!\x1a\x07",         "application/vnd.rar",                                          ".rar"),
    (b"7z\xbc\xaf'\x1c",      "application/x-7z-compressed",                                  ".7z"),
    (b"\x00\x00\x00 ftyp",    "video/mp4",                                                    ".mp4"),
    (b"\x1aE\xdf\xa3",        "video/webm",                                                   ".webm"),
    (b"ID3",                  "audio/mpeg",                                                   ".mp3"),
    (b"OggS",                 "audio/ogg",                                                    ".ogg"),
    (b"{\\rtf",               "application/rtf",                                              ".rtf"),
)


_SUFFIX_TO_MIME: dict[str, str] = {
    ".csv":   "text/csv",
    ".tsv":   "text/tab-separated-values",
    # Microsoft Office (legacy binary)
    ".doc":   "application/msword",
    ".xls":   "application/vnd.ms-excel",
    ".ppt":   "application/vnd.ms-powerpoint",
    # Microsoft Office (OOXML)
    ".docx":  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx":  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx":  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    # Macro-enabled variants
    ".docm":  "application/vnd.ms-word.document.macroEnabled.12",
    ".xlsm":  "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".pptm":  "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
    # OpenDocument formats
    ".odt":   "application/vnd.oasis.opendocument.text",
    ".ods":   "application/vnd.oasis.opendocument.spreadsheet",
    ".odp":   "application/vnd.oasis.opendocument.presentation",
    # Rich text / WordPad / Apple Pages export
    ".rtf":   "application/rtf",
    # Email
    ".eml":   "message/rfc822",
    ".msg":   "application/vnd.ms-outlook",
    # Tabular / structured
    ".parquet": "application/parquet",
    ".json":  "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson":"application/x-ndjson",
    ".yaml":  "application/yaml",
    ".yml":   "application/yaml",
    # Plain text
    ".txt":   "text/plain",
    ".md":    "text/markdown",
    ".html":  "text/html",
    ".htm":   "text/html",
}


# PK and OLE prefixes are *containers* shared by many formats. We match
# them last so an OOXML / ODF / legacy Office filename can win.
_AMBIGUOUS_PREFIXES: frozenset[bytes] = frozenset({
    b"PK\x03\x04",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
})


def sniff_mime_and_ext(
    sample: bytes,
    *,
    fallback_filename: str = "",
) -> tuple[str, str]:
    """Return ``(mime, suggested_extension)`` for raw bytes.

    Strategy:
    1. Distinctive magic signatures (PDF, PNG, JPG, ...) win immediately.
    2. For ambiguous container signatures (PK = zip-based OOXML/ODF,
       OLE = legacy Office binary), the filename extension refines the
       answer; otherwise we fall back to the generic container mime.
    3. Plaintext / tabular formats fall through to filename extension.
    """

    if not sample:
        return "application/octet-stream", ""

    name = (fallback_filename or "").lower()

    distinctive_hit: tuple[str, str] | None = None
    ambiguous_hit: tuple[str, str] | None = None
    for prefix, mime, ext in _MAGIC_SIGNATURES:
        if sample.startswith(prefix):
            if prefix in _AMBIGUOUS_PREFIXES:
                if ambiguous_hit is None:
                    ambiguous_hit = (mime, ext)
            else:
                distinctive_hit = (mime, ext)
                break

    if distinctive_hit is not None:
        if ambiguous_hit is None or distinctive_hit[0] != "application/octet-stream":
            return distinctive_hit

    for suffix, mime in _SUFFIX_TO_MIME.items():
        if name.endswith(suffix):
            return mime, suffix

    if ambiguous_hit is not None:
        return ambiguous_hit

    head = sample[:1024]
    if b"\x00" in head:
        return "application/octet-stream", ""
    control_chars = sum(
        1 for b in head if b < 9 or (13 < b < 32)
    )
    if control_chars * 4 > len(head):
        return "application/octet-stream", ""
    try:
        head.decode("utf-8")
        return "text/plain", ".txt"
    except UnicodeDecodeError:
        pass

    return "application/octet-stream", ""


# ---------------------------------------------------------------------------
# Index entries
# ---------------------------------------------------------------------------


@dataclass
class UploadEntry:
    sha256: str
    path: str
    filename: str
    mime: str
    size: int
    created_at: float
    last_used_at: float
    refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "path": self.path,
            "filename": self.filename,
            "mime": self.mime,
            "size": int(self.size or 0),
            "created_at": float(self.created_at),
            "last_used_at": float(self.last_used_at),
            "refs": list(dict.fromkeys(str(r) for r in (self.refs or []) if r)),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UploadEntry":
        return cls(
            sha256=str(raw.get("sha256") or ""),
            path=str(raw.get("path") or ""),
            filename=str(raw.get("filename") or ""),
            mime=str(raw.get("mime") or ""),
            size=int(raw.get("size") or 0),
            created_at=float(raw.get("created_at") or 0.0),
            last_used_at=float(raw.get("last_used_at") or 0.0),
            refs=[str(r) for r in (raw.get("refs") or []) if str(r or "")],
        )


@dataclass
class StoreConfig:
    base_dir: Path
    ttl_seconds: float = 7 * 24 * 3600.0
    max_total_bytes: int = 0  # 0 = no cap

    @classmethod
    def from_path(cls, base_dir: str | Path) -> "StoreConfig":
        return cls(base_dir=Path(base_dir))


# ---------------------------------------------------------------------------
# Index IO
# ---------------------------------------------------------------------------


def _index_path(base_dir: Path) -> Path:
    return base_dir / _INDEX_FILENAME


def _load_index(base_dir: Path) -> dict[str, UploadEntry]:
    target = _index_path(base_dir)
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, UploadEntry] = {}
    if not isinstance(raw, dict):
        return out
    for sha, value in raw.items():
        if isinstance(value, dict):
            entry = UploadEntry.from_dict({**value, "sha256": sha})
            out[entry.sha256] = entry
    return out


def _save_index(base_dir: Path, index: dict[str, UploadEntry]) -> None:
    target = _index_path(base_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {sha: entry.to_dict() for sha, entry in index.items()}
    fd, tmp_name = tempfile.mkstemp(
        prefix="_index.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, target)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_bytes(
    base_dir: str | Path,
    data: bytes,
    *,
    filename: str = "",
    mime_hint: str = "",
    ref: str = "",
) -> UploadEntry:
    """Persist ``data`` under ``temp_uploads/`` by sha256, dedupe on collision.

    Returns the :class:`UploadEntry` describing the stored file. When ``ref``
    is provided it is appended to ``entry.refs`` so the GC can keep the file
    alive while the run is referencing it.
    """

    if data is None:
        raise ValueError("data is required")

    base_path = Path(base_dir)
    base_path.mkdir(parents=True, exist_ok=True)

    sha = sha256_of(data)
    mime, suggested_ext = sniff_mime_and_ext(data, fallback_filename=filename)
    if mime_hint and mime == "application/octet-stream":
        mime = mime_hint
    ext = suggested_ext or _suffix_from_filename(filename)
    stored_path = base_path / f"{sha}{ext}"

    with _LOCK:
        index = _load_index(base_path)
        existing = index.get(sha)
        now = time.time()
        if existing is None:
            stored_path.write_bytes(data)
            entry = UploadEntry(
                sha256=sha,
                path=str(stored_path),
                filename=filename or "",
                mime=mime,
                size=len(data),
                created_at=now,
                last_used_at=now,
                refs=[ref] if ref else [],
            )
            index[sha] = entry
        else:
            entry = existing
            entry.last_used_at = now
            if filename and not entry.filename:
                entry.filename = filename
            if mime and entry.mime in ("", "application/octet-stream"):
                entry.mime = mime
            if ref and ref not in entry.refs:
                entry.refs.append(ref)
        _save_index(base_path, index)
        return entry


def save_stream(
    base_dir: str | Path,
    stream: BinaryIO,
    *,
    filename: str = "",
    mime_hint: str = "",
    ref: str = "",
    chunk_size: int = 1024 * 1024,
) -> UploadEntry:
    """Persist a binary stream, streaming sha256 to avoid loading huge files."""

    hasher = hashlib.sha256()
    fd, tmp_name = tempfile.mkstemp(
        prefix="upload.", suffix=".part", dir=str(Path(base_dir))
    )
    size = 0
    head_sample = b""
    try:
        with os.fdopen(fd, "wb") as fh:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                if not head_sample:
                    head_sample = chunk[:1024]
                fh.write(chunk)
                hasher.update(chunk)
                size += len(chunk)
        sha = hasher.hexdigest()
        mime, suggested_ext = sniff_mime_and_ext(head_sample, fallback_filename=filename)
        if mime_hint and mime == "application/octet-stream":
            mime = mime_hint
        ext = suggested_ext or _suffix_from_filename(filename)
        base_path = Path(base_dir)
        stored_path = base_path / f"{sha}{ext}"
        with _LOCK:
            index = _load_index(base_path)
            existing = index.get(sha)
            now = time.time()
            if existing is None:
                os.replace(tmp_name, stored_path)
                entry = UploadEntry(
                    sha256=sha,
                    path=str(stored_path),
                    filename=filename or "",
                    mime=mime,
                    size=size,
                    created_at=now,
                    last_used_at=now,
                    refs=[ref] if ref else [],
                )
                index[sha] = entry
            else:
                Path(tmp_name).unlink(missing_ok=True)
                entry = existing
                entry.last_used_at = now
                if filename and not entry.filename:
                    entry.filename = filename
                if mime and entry.mime in ("", "application/octet-stream"):
                    entry.mime = mime
                if ref and ref not in entry.refs:
                    entry.refs.append(ref)
            _save_index(base_path, index)
            return entry
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass
        raise


def add_ref(base_dir: str | Path, sha256: str, ref: str) -> bool:
    base_path = Path(base_dir)
    with _LOCK:
        index = _load_index(base_path)
        entry = index.get(sha256)
        if not entry:
            return False
        if ref and ref not in entry.refs:
            entry.refs.append(ref)
        entry.last_used_at = time.time()
        _save_index(base_path, index)
        return True


def remove_ref(base_dir: str | Path, sha256: str, ref: str) -> bool:
    base_path = Path(base_dir)
    with _LOCK:
        index = _load_index(base_path)
        entry = index.get(sha256)
        if not entry:
            return False
        try:
            entry.refs.remove(ref)
        except ValueError:
            return False
        _save_index(base_path, index)
        return True


def lookup(base_dir: str | Path, sha256: str) -> UploadEntry | None:
    return _load_index(Path(base_dir)).get(sha256)


def list_entries(base_dir: str | Path) -> list[UploadEntry]:
    return list(_load_index(Path(base_dir)).values())


def gc(
    base_dir: str | Path,
    *,
    ttl_seconds: float = 7 * 24 * 3600.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Remove entries with no refs whose ``last_used_at`` exceeds ``ttl_seconds``.

    Returns a summary dict for logging.
    """

    base_path = Path(base_dir)
    deadline = (now if now is not None else time.time()) - max(0.0, float(ttl_seconds))
    removed: list[str] = []
    freed_bytes = 0
    with _LOCK:
        index = _load_index(base_path)
        keep: dict[str, UploadEntry] = {}
        for sha, entry in index.items():
            if not entry.refs and entry.last_used_at < deadline:
                p = Path(entry.path)
                if p.exists():
                    try:
                        freed_bytes += p.stat().st_size
                        p.unlink()
                    except Exception:
                        pass
                removed.append(sha)
                continue
            keep[sha] = entry
        if removed:
            _save_index(base_path, keep)
    return {
        "removed_sha": removed,
        "removed_count": len(removed),
        "freed_bytes": freed_bytes,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _suffix_from_filename(filename: str) -> str:
    name = (filename or "").lower()
    idx = name.rfind(".")
    if idx < 0:
        return ""
    suffix = name[idx:]
    if 1 < len(suffix) <= 6 and suffix.lstrip(".").isalnum():
        return suffix
    return ""


def all_refs(base_dir: str | Path) -> Iterable[tuple[str, list[str]]]:
    for sha, entry in _load_index(Path(base_dir)).items():
        yield sha, list(entry.refs)

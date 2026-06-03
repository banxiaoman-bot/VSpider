"""Shared building blocks for ``data_writers``.

Why this exists
===============
Each per-format writer (csv / jsonl / json / markdown / html / xlsx /
files_folder / inline_text) needs the same three things:

1. A safe destination path under ``runs/<run_id>/artifacts/<name>``.
2. A way to hash the produced bytes (sha256 + size) for the manifest.
3. A consistent way to append the item into ``runs/<run_id>/manifest.json``
   via the existing ``io_contract`` persistence helpers.

Keeping that logic here means each writer module stays small, easy to
audit, and easy to test. It also gives us a single chokepoint for adding
verification events later without touching every writer.

Public surface
==============

- :func:`run_artifacts_dir` - returns ``<runs>/<run_id>/artifacts/`` and
  ensures both directories exist.
- :func:`compute_sha256_size` - one-shot helper for bytes.
- :func:`finalize_file_artifact` - computes hashes for a freshly written
  file and appends a single manifest entry.
- :func:`finalize_inline_record` - records an item that lives in metadata
  only (no file on disk) - used by ``inline_text``.
- :func:`safe_filename` - pure helper for derived filenames.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from visual_web_agent.io_contract import (
    ARTIFACTS_DIRNAME,
    append_manifest_item,
    run_dir,
)


_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z._-]+")


def safe_filename(stem: str, *, suffix: str = "", default: str = "artifact") -> str:
    """Return a filesystem-safe filename composed of ``stem`` + ``suffix``.

    - Strips disallowed characters
    - Falls back to ``default`` when stem becomes empty
    - Always preserves the leading dot of ``suffix`` if provided
    """
    base = _SAFE_NAME_RE.sub("_", str(stem or "").strip("._- ")) or default
    base = base.strip("._-") or default
    if suffix:
        if not suffix.startswith("."):
            suffix = "." + suffix
        return base + suffix
    return base


def default_filename(*, produced_by: str, suffix: str) -> str:
    """Build a deterministic filename when caller did not pass ``filename_hint``.

    Uses ``produced_by`` (e.g. ``vlm_extract``) + timestamp so artifacts written
    in the same run are still distinguishable inside the artifacts folder.
    """
    stem = (produced_by or "artifact").strip() or "artifact"
    stamp = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S_%f")
    return safe_filename(f"{stem}_{stamp}", suffix=suffix)


def run_artifacts_dir(run_id: str, *, base_dir: str | Path | None = None) -> Path:
    """Return ``runs/<run_id>/artifacts/`` (creating it if needed)."""
    rd = run_dir(run_id, base_dir=base_dir)
    artifacts = rd / ARTIFACTS_DIRNAME
    artifacts.mkdir(parents=True, exist_ok=True)
    return artifacts


def compute_sha256_size(data: bytes) -> tuple[str, int]:
    """Return ``(sha256_hex, length_in_bytes)`` for the given payload."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"compute_sha256_size expects bytes, got {type(data).__name__}")
    return hashlib.sha256(data).hexdigest(), len(data)


def finalize_file_artifact(
    *,
    run_id: str,
    path: Path,
    kind: str,
    mime: str,
    produced_by: str = "",
    step_id: str = "",
    source_url: str | Iterable[str] = "",
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Hash a written file and append a manifest entry for it.

    Returns a dict shaped like ``ManifestItem.to_dict()`` plus a top-level
    ``path`` resolved as a string. Callers may surface this directly to the
    agent loop.
    """
    body = path.read_bytes()
    sha, size = compute_sha256_size(body)
    item = append_manifest_item(
        run_id,
        kind=kind,
        path=str(path),
        size=size,
        sha256=sha,
        mime=mime,
        source_url=source_url,
        produced_by=produced_by,
        step_id=step_id,
        extra=extra or {},
        base_dir=base_dir,
    )
    payload = item.to_dict()
    payload["path"] = str(path)
    return payload


def finalize_inline_record(
    *,
    run_id: str,
    text: str,
    kind: str = "answer_text",
    produced_by: str = "",
    step_id: str = "",
    source_url: str | Iterable[str] = "",
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Record an inline ``answer_text`` payload without writing to disk.

    The manifest still gets an entry so the run summary can show "what was
    returned" even when no file landed. ``path`` is left empty to signal
    "no file artifact"; ``extra`` carries the inline text.
    """
    inline_extra = dict(extra or {})
    inline_extra.setdefault("inline_text", text)
    item = append_manifest_item(
        run_id,
        kind=kind,
        path="",
        size=len(text.encode("utf-8")),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        mime="text/plain",
        source_url=source_url,
        produced_by=produced_by,
        step_id=step_id,
        extra=inline_extra,
        base_dir=base_dir,
    )
    payload = item.to_dict()
    payload["path"] = ""
    payload["inline"] = True
    payload["text"] = text
    return payload

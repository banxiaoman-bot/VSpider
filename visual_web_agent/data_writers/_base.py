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

# Extensions a ``filename_hint`` may legitimately carry. When the caller asks
# for a different ``suffix`` (the writer's container extension) we strip one of
# these off the stem first so the on-disk name reflects the contract container
# rather than a stale hint (mission §一-A: never bury the real container under a
# leftover ``.xlsx``).
_KNOWN_ARTIFACT_EXTS = frozenset({
    "xlsx", "xls", "xlsm", "csv", "tsv", "json", "jsonl",
    "md", "markdown", "html", "htm", "txt", "parquet",
})


def safe_filename(stem: str, *, suffix: str = "", default: str = "artifact") -> str:
    """Return a filesystem-safe filename composed of ``stem`` + ``suffix``.

    - Strips disallowed characters
    - Falls back to ``default`` when stem becomes empty
    - Always preserves the leading dot of ``suffix`` if provided
    - When ``suffix`` is given and the stem already ends with a known artifact
      extension, that extension is replaced (not doubled): ``safe_filename(
      "output_1.xlsx", suffix=".csv") -> "output_1.csv"``.
    """
    base = _SAFE_NAME_RE.sub("_", str(stem or "").strip("._- ")) or default
    base = base.strip("._-") or default
    if suffix:
        if not suffix.startswith("."):
            suffix = "." + suffix
        head, _dot, tail = base.rpartition(".")
        if head and tail.lower() in _KNOWN_ARTIFACT_EXTS:
            base = head
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


def _field_names(rows: Iterable[Any]) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            sk = str(key)
            if sk not in seen_set:
                seen.append(sk)
                seen_set.add(sk)
    return seen


def dataset_extra(
    extra: dict[str, Any] | None,
    *,
    output_kind: str,
    rows: Iterable[Any] | None = None,
    row_count: int | None = None,
    fields: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Merge dataset evidence into manifest ``extra`` without overwriting caller data."""
    merged = dict(extra or {})
    if output_kind not in {"dataset_rows", "dataset_records"}:
        return merged

    materialized_rows: list[Any] | None = None
    if rows is not None:
        materialized_rows = list(rows)

    if row_count is None and materialized_rows is not None:
        row_count = len(materialized_rows)
    if row_count is not None:
        try:
            merged.setdefault("row_count", int(row_count))
        except (TypeError, ValueError):
            pass

    field_names: list[str] = []
    if fields is not None:
        field_names = [str(f) for f in fields if str(f)]
    if not field_names and materialized_rows is not None:
        field_names = _field_names(materialized_rows)
    if field_names:
        merged.setdefault("fields", field_names)
    return merged


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

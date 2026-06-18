"""zip writer for ``container == 'zip'``.

The "downstream zipper" the dispatch table used to only reference in a
comment. It bundles a set of blobs / on-disk files into a single ``.zip``
artifact under ``runs/<id>/artifacts/`` and records ONE manifest entry
(``kind`` defaults to ``media_archive``). This lets an ``output_contract``
that resolves to ``container == 'zip'`` land a real archive instead of
raising ``unknown container``.

Input shapes (parity with the ``files_folder`` writer):

- ``list[dict]`` of ``{"filename", "bytes"|"path", "mime?", "source_url?"}``
- a single such ``dict``

The archive is written **deterministically** (fixed member timestamps,
sorted is left to the caller) so identical inputs produce identical bytes
-- which makes the sha256 manifest dedup idempotent and lets the
run-level packager rebuild ``bundle.zip`` without piling up entries.

Return shape mirrors the single-file writers: the ``finalize_file_artifact``
dict (manifest item + ``path``) for the produced ``.zip``.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any, Iterable

from ._base import (
    default_filename,
    finalize_file_artifact,
    run_artifacts_dir,
    safe_filename,
)


# 1980-01-01: the zip epoch. Fixed so the archive is reproducible.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def _coerce_blobs(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        candidates = [data]
    elif isinstance(data, list):
        candidates = [b for b in data if isinstance(b, dict)]
    else:
        candidates = []
    out: list[dict[str, Any]] = []
    for raw in candidates:
        if "bytes" in raw or "path" in raw:
            out.append(raw)
    return out


def _blob_bytes(blob: dict[str, Any]) -> bytes | None:
    if "bytes" in blob:
        return bytes(blob.get("bytes") or b"")
    src = Path(str(blob.get("path") or ""))
    if not src.exists() or not src.is_file():
        return None
    return src.read_bytes()


def _arcname(blob: dict[str, Any], used: set[str], index: int) -> str:
    raw = str(blob.get("filename") or "")
    if not raw and blob.get("path"):
        raw = Path(str(blob["path"])).name
    stem = Path(raw).stem or f"file_{index}"
    suffix = Path(raw).suffix or ".bin"
    name = safe_filename(stem, suffix=suffix)
    candidate = name
    counter = 1
    while candidate in used:
        candidate = f"{Path(name).stem}_{counter}{Path(name).suffix}"
        counter += 1
    used.add(candidate)
    return candidate


def write_zip(
    data: Any,
    *,
    run_id: str,
    output_kind: str = "media_archive",
    produced_by: str = "",
    step_id: str = "",
    source_url: str | Iterable[str] = "",
    filename_hint: str = "",
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    blobs = _coerce_blobs(data)
    artifacts = run_artifacts_dir(run_id, base_dir=base_dir)

    buffer = io.BytesIO()
    used: set[str] = set()
    members: list[str] = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for index, blob in enumerate(blobs):
            body = _blob_bytes(blob)
            if body is None:
                continue
            arcname = _arcname(blob, used, index)
            info = zipfile.ZipInfo(filename=arcname, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, body)
            members.append(arcname)

    filename = (
        safe_filename(filename_hint, suffix=".zip")
        if filename_hint
        else default_filename(produced_by=produced_by or "bundle", suffix=".zip")
    )
    target = artifacts / filename
    target.write_bytes(buffer.getvalue())

    merged_extra = dict(extra or {})
    merged_extra.setdefault("member_count", len(members))
    merged_extra.setdefault("members", members)

    return finalize_file_artifact(
        run_id=run_id,
        path=target,
        kind=output_kind or "media_archive",
        mime="application/zip",
        produced_by=produced_by,
        step_id=step_id,
        source_url=source_url,
        extra=merged_extra,
        base_dir=base_dir,
    )

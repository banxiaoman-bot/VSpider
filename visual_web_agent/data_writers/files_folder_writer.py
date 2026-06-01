"""files_folder writer for ``container == 'files_folder'``.

This is the catch-all writer for grouped binary artifacts: the media
harvester, archive downloads, screenshot bundles, mixed-mode goal output.
The input is a sequence of ``{"filename", "bytes", "mime?", "source_url?",
"kind?"}`` records; each gets written into ``runs/<id>/artifacts/`` and
gets its own ``manifest.json`` entry.

Return shape::

    {
        "kind": "<output_kind>",
        "container": "files_folder",
        "path": "<runs/<id>/artifacts/>",
        "items": [<finalized manifest dicts, one per blob>]
    }
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ._base import (
    finalize_file_artifact,
    run_artifacts_dir,
    safe_filename,
)


def _coerce_blobs(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        blobs = [data]
    elif isinstance(data, list):
        blobs = [b for b in data if isinstance(b, dict)]
    else:
        blobs = []
    out: list[dict[str, Any]] = []
    for raw in blobs:
        if "bytes" not in raw and "path" not in raw:
            continue
        out.append(raw)
    return out


def write_files_folder(
    data: Any,
    *,
    run_id: str,
    output_kind: str = "file_generic",
    produced_by: str = "",
    step_id: str = "",
    source_url: str | Iterable[str] = "",
    filename_hint: str = "",
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    blobs = _coerce_blobs(data)
    artifacts = run_artifacts_dir(run_id, base_dir=base_dir)

    item_records: list[dict[str, Any]] = []
    for blob in blobs:
        raw_name = str(blob.get("filename") or "artifact.bin")
        suffix = Path(raw_name).suffix or ""
        stem = Path(raw_name).stem or "artifact"
        filename = safe_filename(stem, suffix=suffix or ".bin")
        target = artifacts / filename
        if "bytes" in blob:
            target.write_bytes(bytes(blob["bytes"] or b""))
        else:
            src = Path(str(blob["path"]))
            if not src.exists():
                continue
            target.write_bytes(src.read_bytes())
        per_kind = str(blob.get("kind") or output_kind or "file_generic")
        per_mime = str(blob.get("mime") or "application/octet-stream")
        per_source = blob.get("source_url") or source_url
        item = finalize_file_artifact(
            run_id=run_id,
            path=target,
            kind=per_kind,
            mime=per_mime,
            produced_by=produced_by,
            step_id=step_id,
            source_url=per_source if per_source is not None else "",
            extra=blob.get("extra") or extra,
            base_dir=base_dir,
        )
        item_records.append(item)

    return {
        "kind": output_kind or "file_generic",
        "container": "files_folder",
        "path": str(artifacts),
        "items": item_records,
    }

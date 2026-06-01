"""JSON Lines writer for ``container == 'jsonl'``.

One JSON object per line, UTF-8 encoded, no BOM. Non-dict rows get
wrapped in ``{"value": ...}`` so the file remains valid NDJSON regardless
of the input shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from ._base import (
    default_filename,
    finalize_file_artifact,
    run_artifacts_dir,
    safe_filename,
)


def _coerce_record(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    return {"value": row}


def write_jsonl(
    data: Any,
    *,
    run_id: str,
    output_kind: str = "dataset_records",
    produced_by: str = "",
    step_id: str = "",
    source_url: str | Iterable[str] = "",
    filename_hint: str = "",
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    if isinstance(data, dict):
        iter_rows: list[Any] = [data]
    elif isinstance(data, list):
        iter_rows = list(data)
    elif data is None:
        iter_rows = []
    else:
        iter_rows = [data]

    lines = [
        json.dumps(_coerce_record(row), ensure_ascii=False, sort_keys=False)
        for row in iter_rows
    ]
    body = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")

    artifacts = run_artifacts_dir(run_id, base_dir=base_dir)
    filename = (
        safe_filename(filename_hint, suffix=".jsonl")
        if filename_hint
        else default_filename(produced_by=produced_by or "extract", suffix=".jsonl")
    )
    target = artifacts / filename
    target.write_bytes(body)

    return finalize_file_artifact(
        run_id=run_id,
        path=target,
        kind=output_kind or "dataset_records",
        mime="application/x-ndjson",
        produced_by=produced_by,
        step_id=step_id,
        source_url=source_url,
        extra=extra,
        base_dir=base_dir,
    )

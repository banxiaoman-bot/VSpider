"""CSV writer used when ``output_contract.container == 'csv'``.

Pure-stdlib implementation so we never pull in pandas just to land a CSV.
Accepts the same row shapes the other dispatch writers accept:

- ``list[dict]`` - column-aware, union of keys preserves first-seen order
- ``list[Iterable]`` - written verbatim (no header inferred)
- ``dict``  - degraded to a single-row list

When rows are empty the writer still produces a zero-row CSV with the
union header (or just an empty file) and registers it so the run summary
reflects "yes, the agent decided to land a CSV, just nothing in it".
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Iterable

from ._base import (
    dataset_extra,
    default_filename,
    finalize_file_artifact,
    run_artifacts_dir,
    safe_filename,
)


def _flatten_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        return [dict(data)]
    if isinstance(data, list):
        out: list[dict[str, Any]] = []
        for row in data:
            if isinstance(row, dict):
                out.append(dict(row))
            else:
                out.append({"value": row})
        return out
    if data is None:
        return []
    return [{"value": data}]


def _union_columns(rows: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for row in rows:
        for key in row.keys():
            sk = str(key)
            if sk not in seen_set:
                seen.append(sk)
                seen_set.add(sk)
    return seen


def write_csv(
    data: Any,
    *,
    run_id: str,
    output_kind: str = "dataset_rows",
    produced_by: str = "",
    step_id: str = "",
    source_url: str | Iterable[str] = "",
    filename_hint: str = "",
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    rows = _flatten_rows(data)
    columns = _union_columns(rows)

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})

    body = buffer.getvalue().encode("utf-8")

    artifacts = run_artifacts_dir(run_id, base_dir=base_dir)
    filename = (
        safe_filename(filename_hint, suffix=".csv")
        if filename_hint
        else default_filename(produced_by=produced_by or "extract", suffix=".csv")
    )
    target = artifacts / filename
    target.write_bytes(body)
    kind = output_kind or "dataset_rows"

    return finalize_file_artifact(
        run_id=run_id,
        path=target,
        kind=kind,
        mime="text/csv",
        produced_by=produced_by,
        step_id=step_id,
        source_url=source_url,
        extra=dataset_extra(extra, output_kind=kind, rows=rows, fields=columns),
        base_dir=base_dir,
    )

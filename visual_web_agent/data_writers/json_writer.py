"""JSON writer for ``container == 'json'``.

When given a ``dict`` we write the object verbatim; lists become arrays;
anything else gets wrapped in ``{"value": ...}``.
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


def write_json(
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
    if isinstance(data, (dict, list)):
        payload: Any = data
    elif data is None:
        payload = []
    else:
        payload = {"value": data}

    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    artifacts = run_artifacts_dir(run_id, base_dir=base_dir)
    filename = (
        safe_filename(filename_hint, suffix=".json")
        if filename_hint
        else default_filename(produced_by=produced_by or "extract", suffix=".json")
    )
    target = artifacts / filename
    target.write_bytes(body)

    return finalize_file_artifact(
        run_id=run_id,
        path=target,
        kind=output_kind or "dataset_records",
        mime="application/json",
        produced_by=produced_by,
        step_id=step_id,
        source_url=source_url,
        extra=extra,
        base_dir=base_dir,
    )

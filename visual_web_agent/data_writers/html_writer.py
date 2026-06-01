"""HTML writer for ``container == 'html'`` (mainly for html_snapshot)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ._base import (
    default_filename,
    finalize_file_artifact,
    run_artifacts_dir,
    safe_filename,
)


def write_html(
    data: Any,
    *,
    run_id: str,
    output_kind: str = "html_snapshot",
    produced_by: str = "",
    step_id: str = "",
    source_url: str | Iterable[str] = "",
    filename_hint: str = "",
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    if isinstance(data, bytes):
        body = data
    elif data is None:
        body = b""
    else:
        body = str(data).encode("utf-8")

    artifacts = run_artifacts_dir(run_id, base_dir=base_dir)
    filename = (
        safe_filename(filename_hint, suffix=".html")
        if filename_hint
        else default_filename(produced_by=produced_by or "snapshot", suffix=".html")
    )
    target = artifacts / filename
    target.write_bytes(body)

    return finalize_file_artifact(
        run_id=run_id,
        path=target,
        kind=output_kind or "html_snapshot",
        mime="text/html",
        produced_by=produced_by,
        step_id=step_id,
        source_url=source_url,
        extra=extra,
        base_dir=base_dir,
    )

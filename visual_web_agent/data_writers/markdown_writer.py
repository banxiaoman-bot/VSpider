"""Markdown writer for ``container == 'markdown'``.

Two cases:

- ``data`` is a string -> we write it verbatim (treated as already-formatted
  Markdown).
- ``data`` is dict / list of dict -> we render a GitHub-flavored Markdown
  table whose columns are the union of keys, preserving first-seen order.
  Cells are stringified with ``str(...)`` so the writer never crashes on
  exotic value types; pipes inside values are escaped.

Either way the file lands as UTF-8 ``.md`` and gets a manifest entry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ._base import (
    dataset_extra,
    default_filename,
    finalize_file_artifact,
    run_artifacts_dir,
    safe_filename,
)


def _escape_cell(value: Any) -> str:
    text = str("" if value is None else value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def _render_table(rows: list[dict[str, Any]]) -> str:
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            sk = str(key)
            if sk not in seen:
                columns.append(sk)
                seen.add(sk)
    if not columns:
        return ""
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    body_lines = []
    for row in rows:
        body_lines.append(
            "| " + " | ".join(_escape_cell(row.get(c, "")) for c in columns) + " |"
        )
    return "\n".join([header, separator] + body_lines) + "\n"


def _coerce_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        return [dict(data)]
    if isinstance(data, list):
        return [r if isinstance(r, dict) else {"value": r} for r in data]
    return [{"value": data}]


def write_markdown(
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
    rows: list[dict[str, Any]] | None = None
    if isinstance(data, str):
        body_text = data
    else:
        rows = _coerce_rows(data)
        body_text = _render_table(rows) or ""

    body = body_text.encode("utf-8")

    artifacts = run_artifacts_dir(run_id, base_dir=base_dir)
    filename = (
        safe_filename(filename_hint, suffix=".md")
        if filename_hint
        else default_filename(produced_by=produced_by or "report", suffix=".md")
    )
    target = artifacts / filename
    target.write_bytes(body)
    kind = output_kind or "dataset_rows"

    return finalize_file_artifact(
        run_id=run_id,
        path=target,
        kind=kind,
        mime="text/markdown",
        produced_by=produced_by,
        step_id=step_id,
        source_url=source_url,
        extra=dataset_extra(extra, output_kind=kind, rows=rows),
        base_dir=base_dir,
    )

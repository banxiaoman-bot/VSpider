"""XLSX writer for ``container == 'xlsx'``.

We DO want to keep an .xlsx path for tabular rows because Excel remains
the most common consumer for non-technical users. The implementation is
intentionally lightweight (pandas + openpyxl, same as ``data_manager``)
and falls back to a deferred ``ImportError`` only when the optional
dependency is missing, so unit tests on a slim env stay green for the
other writers.

Schema normalisation, dedupe, and tooltip upsert logic stays in the
legacy ``data_manager.save_to_excel`` for now -- this writer is the
*fresh* contract-aware entry point used by ``save_artifact``. Callers
that want the legacy behaviour can keep calling ``save_to_excel`` until
all sites are migrated.
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


_XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def _coerce_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        return [dict(data)]
    if isinstance(data, list):
        return [r if isinstance(r, dict) else {"value": r} for r in data]
    if data is None:
        return []
    return [{"value": data}]


def write_xlsx(
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
    try:
        import pandas as pd  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only on slim env
        raise RuntimeError(
            "container=xlsx requires pandas+openpyxl; install them or "
            "pick container=csv/jsonl in the output_contract."
        ) from exc

    rows = _coerce_rows(data)
    df = pd.DataFrame(rows)

    artifacts = run_artifacts_dir(run_id, base_dir=base_dir)
    filename = (
        safe_filename(filename_hint, suffix=".xlsx")
        if filename_hint
        else default_filename(produced_by=produced_by or "extract", suffix=".xlsx")
    )
    target = artifacts / filename
    df.to_excel(target, index=False, engine="openpyxl")
    kind = output_kind or "dataset_rows"

    return finalize_file_artifact(
        run_id=run_id,
        path=target,
        kind=kind,
        mime=_XLSX_MIME,
        produced_by=produced_by,
        step_id=step_id,
        source_url=source_url,
        extra=dataset_extra(extra, output_kind=kind, rows=rows),
        base_dir=base_dir,
    )

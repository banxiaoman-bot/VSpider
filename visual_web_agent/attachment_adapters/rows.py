"""``batch_rows`` adapter.

Reads csv / xlsx / xls / tsv / json / jsonl / parquet attachments and yields
row dicts compatible with ``smart_batch_runner._render_goal``.

Heavy parsers (``pandas`` / ``openpyxl``) are imported lazily so the test
suite does not pay the cost when these adapters aren't exercised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from visual_web_agent.io_contract.input_contract import AttachmentSpec

from .base import AdapterResult


_TABULAR_SUFFIXES = {
    ".csv", ".tsv",
    ".xls", ".xlsx", ".xlsm",
    ".ods",
    ".parquet",
}


def adapt(
    spec: AttachmentSpec,
    *,
    goal: str = "",
    options: dict[str, Any] | None = None,
) -> AdapterResult:
    options = options or {}
    path = Path(spec.path)
    if not path.exists():
        return AdapterResult(
            kind="rows", ok=False,
            reasons=[f"attachment_not_found:{path}"],
        )

    suffix = path.suffix.lower()
    if suffix in {".json", ".jsonl", ".ndjson"}:
        return _read_json(path)
    if suffix in _TABULAR_SUFFIXES:
        return _read_tabular(path, options=options)
    return AdapterResult(
        kind="rows", ok=False,
        reasons=[f"unsupported_rows_suffix:{suffix}"],
    )


def _read_json(path: Path) -> AdapterResult:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return AdapterResult(
            kind="rows", ok=False, reasons=[f"json_read_failed:{exc}"]
        )

    stripped = text.lstrip()
    rows: list[dict[str, Any]] = []
    if stripped.startswith("["):
        try:
            payload = json.loads(text)
        except Exception as exc:
            return AdapterResult(
                kind="rows", ok=False, reasons=[f"json_parse_failed:{exc}"]
            )
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    rows.append(item)
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)

    columns = sorted({k for row in rows for k in row.keys()})
    return AdapterResult(
        kind="rows",
        ok=True,
        rows=rows,
        metadata={"row_count": len(rows), "columns": columns, "source_suffix": path.suffix.lower()},
    )


def _read_tabular(path: Path, *, options: dict[str, Any]) -> AdapterResult:
    try:
        import pandas as pd  # type: ignore
    except Exception as exc:  # pragma: no cover - pandas is a project dep
        return AdapterResult(
            kind="rows", ok=False, reasons=[f"pandas_unavailable:{exc}"]
        )

    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            df = pd.read_csv(path)
        elif suffix == ".tsv":
            df = pd.read_csv(path, sep="\t")
        elif suffix in {".xls", ".xlsx", ".xlsm"}:
            df = pd.read_excel(path)
        elif suffix == ".ods":
            try:
                df = pd.read_excel(path, engine="odf")
            except Exception as exc:
                return AdapterResult(
                    kind="rows", ok=False,
                    reasons=[f"ods_engine_unavailable:{exc}"],
                )
        elif suffix == ".parquet":
            df = pd.read_parquet(path)
        else:  # defensive, dispatcher already filtered
            return AdapterResult(
                kind="rows", ok=False, reasons=[f"unhandled_suffix:{suffix}"]
            )
    except Exception as exc:
        return AdapterResult(
            kind="rows", ok=False, reasons=[f"read_failed:{exc}"]
        )

    rows: list[dict[str, Any]] = []
    for record in df.to_dict(orient="records"):
        normalized: dict[str, Any] = {}
        for k, v in record.items():
            key = str(k).strip()
            if v is None:
                normalized[key] = ""
            else:
                try:
                    import pandas as pd  # type: ignore

                    if pd.isna(v):
                        normalized[key] = ""
                        continue
                except Exception:
                    pass
                normalized[key] = v if not isinstance(v, float) else (v if v == v else "")
        rows.append(normalized)

    return AdapterResult(
        kind="rows",
        ok=True,
        rows=rows,
        metadata={
            "row_count": len(rows),
            "columns": [str(c) for c in df.columns],
            "source_suffix": suffix,
        },
    )

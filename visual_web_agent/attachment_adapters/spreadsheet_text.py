"""Spreadsheet-as-prompt-context adapter.

When the user says "read / summarize / analyze this Excel" instead of
"fill in each row", we route the same xlsx / xls / ods to this adapter,
which emits a compact textual digest (sheet names + header rows + first
few sample rows) suitable for the prompt without trying to flatten the
whole spreadsheet.

The full row-shaped read still lives in :mod:`rows`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from visual_web_agent.io_contract.input_contract import AttachmentSpec

from .base import AdapterResult


_DEFAULT_EXCERPT_CHARS = 4000
_DEFAULT_HEAD_ROWS = 5
_DEFAULT_TAIL_ROWS = 2


def adapt(
    spec: AttachmentSpec,
    *,
    goal: str = "",
    options: dict[str, Any] | None = None,
) -> AdapterResult:
    options = options or {}
    max_chars = int(options.get("max_chars") or _DEFAULT_EXCERPT_CHARS)
    head_rows = int(options.get("head_rows") or _DEFAULT_HEAD_ROWS)
    tail_rows = int(options.get("tail_rows") or _DEFAULT_TAIL_ROWS)
    path = Path(spec.path)
    if not path.exists():
        return AdapterResult(
            kind="spreadsheet_text", ok=False,
            reasons=[f"attachment_not_found:{path}"],
        )

    suffix = path.suffix.lower()
    try:
        import pandas as pd  # type: ignore
    except Exception as exc:  # pragma: no cover - pandas is required
        return AdapterResult(
            kind="spreadsheet_text", ok=False,
            reasons=[f"pandas_unavailable:{exc}"],
        )

    sheets: dict[str, "pd.DataFrame"] = {}
    try:
        if suffix == ".csv":
            sheets = {"Sheet1": pd.read_csv(path)}
        elif suffix == ".tsv":
            sheets = {"Sheet1": pd.read_csv(path, sep="\t")}
        elif suffix in {".xls", ".xlsx", ".xlsm"}:
            sheets = pd.read_excel(path, sheet_name=None)
        elif suffix == ".ods":
            try:
                sheets = pd.read_excel(path, sheet_name=None, engine="odf")
            except Exception as exc:
                return AdapterResult(
                    kind="spreadsheet_text", ok=False,
                    reasons=[f"ods_engine_unavailable:{exc}"],
                )
        elif suffix == ".parquet":
            sheets = {"Sheet1": pd.read_parquet(path)}
        else:
            return AdapterResult(
                kind="spreadsheet_text", ok=False,
                reasons=[f"unsupported_spreadsheet_suffix:{suffix}"],
            )
    except Exception as exc:
        return AdapterResult(
            kind="spreadsheet_text", ok=False,
            reasons=[f"read_failed:{exc}"],
        )

    blocks: list[str] = []
    metadata: dict[str, Any] = {
        "filename": spec.filename or path.name,
        "source_suffix": suffix,
        "sheets": [],
    }
    for sheet_name, df in sheets.items():
        rows, cols = df.shape
        columns = [str(c) for c in df.columns]
        head_str = df.head(head_rows).to_string(index=False) if rows else ""
        tail_str = ""
        if rows > head_rows + tail_rows:
            tail_str = df.tail(tail_rows).to_string(index=False)
        block_lines = [
            f"# Sheet: {sheet_name}",
            f"shape: {rows} rows x {cols} cols",
            f"columns: {', '.join(columns)}" if columns else "columns: <empty>",
        ]
        if head_str:
            block_lines.append("head:\n" + head_str)
        if tail_str:
            block_lines.append("tail:\n" + tail_str)
        blocks.append("\n".join(block_lines))
        metadata["sheets"].append({
            "name": sheet_name,
            "rows": int(rows),
            "cols": int(cols),
            "columns": columns,
        })
        if sum(len(b) for b in blocks) >= max_chars:
            break

    body = "\n\n".join(blocks)
    excerpt = body[:max_chars]
    metadata["total_chars"] = len(body)
    metadata["excerpt_chars"] = len(excerpt)
    return AdapterResult(
        kind="spreadsheet_text",
        ok=True,
        text=excerpt,
        metadata=metadata,
    )

"""inline_text writer for ``container == 'inline_text'``.

The whole point is to NOT write to disk: ``output_kind == 'answer_text'``
goals want a short reply that flows through the chat / event_stream, not
a 1-row Excel file. We still record a manifest item so the run summary
can show "the agent did produce an answer".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ._base import finalize_inline_record


def write_inline_text(
    data: Any,
    *,
    run_id: str,
    output_kind: str = "answer_text",
    produced_by: str = "",
    step_id: str = "",
    source_url: str | Iterable[str] = "",
    filename_hint: str = "",  # accepted for signature parity; unused
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    if isinstance(data, str):
        text = data
    elif data is None:
        text = ""
    elif isinstance(data, (dict, list)):
        # Single-record inline answers are common (e.g. {"answer": "42"})
        try:
            import json as _json

            text = _json.dumps(data, ensure_ascii=False)
        except Exception:
            text = str(data)
    else:
        text = str(data)

    return finalize_inline_record(
        run_id=run_id,
        text=text,
        kind=output_kind or "answer_text",
        produced_by=produced_by,
        step_id=step_id,
        source_url=source_url,
        extra=extra,
        base_dir=base_dir,
    )

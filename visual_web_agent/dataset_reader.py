"""Read a prior run's dataset artifact back into ``list[dict]`` (RUN-RESUME1 step 3a-wire-1).

A run persists its dataset under ``runs/<run_id>/artifacts/`` in whatever
container ``output_contract`` chose (xlsx / csv / jsonl / json). To *resume* a
task across launches we must load that artifact back into row dicts and feed
them to :func:`visual_web_agent.data_sanitizer.rebuild_seen_fingerprints`, which
reconstructs the dedup seen-set so the resumed extraction skips rows already on
disk and only appends genuinely new ones.

Two contracts matter:

* **Tolerant** -- a missing / corrupt / unsupported artifact degrades to ``[]``
  so a damaged dataset never aborts a resumed run (it just resumes with an empty
  seen-set, i.e. re-extracts; dedup at write time stays correct).
* **NaN hygiene** -- pandas turns blank cells into ``NaN``; ``str(NaN)`` is
  ``"nan"``, which would leak a bogus token into a fingerprint and silently
  break resume dedup. Missing / NaN cells are coerced to ``""`` (which
  ``_non_empty_values`` then drops), matching ``attachment_adapters/rows.py``.

Pure I/O, no global state. Heavy parsers (pandas / openpyxl) are imported lazily
so importing this module stays cheap for the non-tabular paths and for callers
that never resume.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["read_dataset_rows", "SUPPORTED_SUFFIXES"]


_JSONL_SUFFIXES = {".jsonl", ".ndjson"}
_JSON_SUFFIXES = {".json"}
_TABULAR_SUFFIXES = {".csv", ".tsv", ".xls", ".xlsx", ".xlsm"}
SUPPORTED_SUFFIXES = frozenset(_JSONL_SUFFIXES | _JSON_SUFFIXES | _TABULAR_SUFFIXES)


def read_dataset_rows(path: str | Path | None) -> list[dict[str, Any]]:
    """Return the dataset at ``path`` as a list of row dicts (``[]`` on any failure).

    Dispatches on file suffix: ``.jsonl``/``.ndjson`` (line-delimited JSON),
    ``.json`` (array or single object), and tabular ``.csv``/``.tsv``/``.xls*``
    (via pandas). Non-dict items are skipped; an unknown suffix or unreadable
    file yields ``[]``.
    """

    if not path:
        return []
    target = Path(path)
    try:
        if not target.is_file():
            return []
    except OSError:
        return []

    suffix = target.suffix.lower()
    try:
        if suffix in _JSONL_SUFFIXES:
            return _read_jsonl(target)
        if suffix in _JSON_SUFFIXES:
            return _read_json(target)
        if suffix in _TABULAR_SUFFIXES:
            return _read_tabular(target)
    except Exception:
        return []
    return []


def _read_jsonl(target: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _read_json(target: Path) -> list[dict[str, Any]]:
    payload = json.loads(target.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _read_tabular(target: Path) -> list[dict[str, Any]]:
    import pandas as pd  # lazy: pandas/openpyxl are heavy project deps

    suffix = target.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(target)
    elif suffix == ".tsv":
        frame = pd.read_csv(target, sep="\t")
    else:  # .xls / .xlsx / .xlsm
        frame = pd.read_excel(target)

    rows: list[dict[str, Any]] = []
    for record in frame.to_dict(orient="records"):
        clean: dict[str, Any] = {}
        for key, value in record.items():
            name = str(key).strip()
            if value is None:
                clean[name] = ""
                continue
            if isinstance(value, float) and value != value:  # NaN
                clean[name] = ""
                continue
            try:
                if pd.isna(value):
                    clean[name] = ""
                    continue
            except (TypeError, ValueError):
                pass
            clean[name] = value
        rows.append(clean)
    return rows

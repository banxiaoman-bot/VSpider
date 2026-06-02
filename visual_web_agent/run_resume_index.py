"""Cross-run resume locator (RUN-RESUME1 step 3-pre).

``run_checkpoint`` persists a checkpoint under ``runs/<run_id>/`` keyed by the
*per-run* id (a fresh timestamp every launch). That alone can never bridge two
launches of the same task: a new run gets a new ``run_id`` and so never finds
the previous run's checkpoint. This module supplies the missing piece -- a
**stable key** derived deterministically from ``goal`` + ``start_url`` and a
tiny index, ``runs/_resume_index.json``, mapping that key to the most recent
run for the same task.

Design mirrors the rest of the RUN-RESUME slice: pure helpers, atomic
tmp-file + ``os.replace`` writes, tolerant reads (missing / corrupt -> empty),
and ``base_dir`` injection so unit tests stay hermetic under ``tmp_path``.

Default-off / byte-equivalent contract: nothing in the agent loop calls this
yet; it is wired by a later slice. Writing an index entry is the only side
effect and it lives beside ``runs/`` (never inside an existing run dir).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io_contract.persistence import default_runs_root

__all__ = [
    "RESUME_INDEX_FILENAME",
    "compute_resume_key",
    "resume_index_path",
    "record_run",
    "lookup_last_run",
]


RESUME_INDEX_FILENAME = "_resume_index.json"

_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compute_resume_key(goal: str, start_url: str) -> str:
    """Return a stable 16-hex key for a ``(goal, start_url)`` task.

    Normalization keeps the key invariant under cosmetic differences a user is
    unlikely to mean as a *different* task: surrounding / collapsed whitespace
    and case in the goal, and case / a single trailing slash in the URL.
    """

    goal_norm = " ".join(str(goal or "").strip().lower().split())
    url_norm = str(start_url or "").strip().lower().rstrip("/")
    digest = hashlib.sha256(f"{goal_norm}\x00{url_norm}".encode("utf-8")).hexdigest()
    return digest[:16]


def resume_index_path(*, base_dir: str | Path | None = None) -> Path:
    """Return ``<runs_root>/_resume_index.json`` (``base_dir`` overrides root)."""

    root = Path(base_dir) if base_dir is not None else default_runs_root()
    return root / RESUME_INDEX_FILENAME


def _load_index(target: Path) -> dict[str, Any]:
    if not target.exists() or not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_index(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix="_resume_index.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, target)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass
        raise


def record_run(
    key: str,
    run_id: str,
    *,
    status: str = "in_progress",
    item_count: int = 0,
    goal: str = "",
    start_url: str = "",
    dataset_path: str = "",
    base_dir: str | Path | None = None,
) -> Path:
    """Upsert (last-wins) the latest run for ``key`` into the resume index.

    Returns the index path. A corrupt existing index is silently replaced with
    a fresh mapping so a damaged file never blocks recording the new run.
    """

    target = resume_index_path(base_dir=base_dir)
    entry = {
        "run_id": str(run_id or ""),
        "status": str(status or "in_progress"),
        "item_count": int(item_count or 0),
        "goal": str(goal or ""),
        "start_url": str(start_url or ""),
        "dataset_path": str(dataset_path or ""),
        "updated_at": _now_iso(),
    }
    with _LOCK:
        index = _load_index(target)
        index[str(key or "")] = entry
        _atomic_write_index(target, index)
    return target


def lookup_last_run(
    key: str,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return the most recent run entry for ``key``, or ``None`` if absent."""

    target = resume_index_path(base_dir=base_dir)
    index = _load_index(target)
    entry = index.get(str(key or ""))
    return entry if isinstance(entry, dict) else None

"""Process-local run context for io_contract.

Why this exists
===============
``data_manager.save_to_excel`` and ``artifact_manager.register_artifact``
have 21+ call sites scattered across ``main.py`` / ``actions.py``. Most
of those call sites do not pass ``run_id`` because they predate the
``runs/<run_id>/manifest.json`` contract.

Instead of churning 21 surgical edits (risky in an 850 KB CRLF file),
we let the main agent loop publish "the run we are currently inside"
to a module-level slot at run-start, and let the writer modules read
that slot when they need a manifest target. The slot is reset in the
``finally`` block so a crash in one run never leaks into the next.

Design notes
============
- ``ContextVar`` keeps concurrent asyncio sub-runs isolated while retaining the
  old "call current_run_id()" API for legacy writers.
- Helpers always swallow caller mistakes (e.g. setting an invalid run_id
  silently degrades to "no current run") so they never raise into the agent loop.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any


_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")  # no "." => blocks ./.. path traversal


_RUN_ID: ContextVar[str] = ContextVar("vspider_io_contract_run_id", default="")
_BASE_DIR: ContextVar[str | Path | None] = ContextVar(
    "vspider_io_contract_base_dir",
    default=None,
)


def set_current_run(run_id: str, *, base_dir: str | Path | None = None) -> None:
    """Publish the active run id so contract-aware writers can find it.

    Invalid / empty ids silently clear the slot rather than raise -- this
    keeps the agent loop's ``try/except`` blocks simple.
    """
    rid = str(run_id or "").strip()
    if not rid or not _RUN_ID_RE.fullmatch(rid):
        clear_current_run()
        return
    _RUN_ID.set(rid)
    _BASE_DIR.set(base_dir)


def clear_current_run() -> None:
    """Reset the slot. Always safe to call (including from ``finally``)."""
    _RUN_ID.set("")
    _BASE_DIR.set(None)


def current_run_id() -> str:
    """Return the published run id, or ``""`` when no run is active."""
    return str(_RUN_ID.get() or "")


def current_base_dir() -> str | Path | None:
    """Return the optional base_dir override used by tests."""
    return _BASE_DIR.get()


def current_run_context() -> dict[str, Any]:
    """Snapshot the entire current context."""
    return {"run_id": current_run_id(), "base_dir": current_base_dir()}


__all__ = [
    "set_current_run",
    "clear_current_run",
    "current_run_id",
    "current_base_dir",
    "current_run_context",
]

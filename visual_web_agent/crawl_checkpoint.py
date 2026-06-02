"""Crawl checkpoint persistence for ``spider_lite`` resumable deep crawls.

Borrowed (re-implemented, stdlib only) from crawl4ai's resumable deep crawl.
A checkpoint is a plain JSON document holding the crawl progress
(``seen`` / pending frontier / collected ``pages`` / ``items``). Writes are
atomic (tmp file + ``os.replace``) so a crash mid-write never corrupts an
existing checkpoint; reads are tolerant (missing / corrupt → ``None``) so a
fresh or damaged checkpoint just degrades to a normal crawl.

Pure I/O helpers with no crawl logic, so they are trivially unit-testable.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

__all__ = ["save_checkpoint", "load_checkpoint"]


def save_checkpoint(path: str, state: dict[str, Any]) -> str:
    """Atomically write ``state`` as JSON to ``path``; return the path.

    Parent directories are created as needed. The write goes to a sibling temp
    file first and is then ``os.replace``-d into place (atomic on Win/POSIX).
    """
    target = str(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".crawl_cp_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def load_checkpoint(path: str) -> dict[str, Any] | None:
    """Return the checkpoint dict at ``path``, or ``None`` if missing/corrupt."""
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None

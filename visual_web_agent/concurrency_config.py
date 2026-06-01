"""Concurrency knobs for batch rows, multi start URLs, and browser pool sizing."""

from __future__ import annotations

import os
from typing import Any


def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int = 16) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _constraints_dict(constraints: dict[str, Any] | None) -> dict[str, Any]:
    return dict(constraints or {}) if isinstance(constraints, dict) else {}


def resolve_batch_row_concurrency(constraints: dict[str, Any] | None = None) -> int:
    """How many spreadsheet rows may run ``run_agent`` concurrently."""

    cons = _constraints_dict(constraints)
    env_value = _env_int("VSPIDER_BATCH_ROW_CONCURRENCY", 1, min_value=1, max_value=16)
    max_runs = int(cons.get("max_runs") or 0)
    if max_runs > 0:
        return max(1, min(env_value, max_runs))
    return env_value


def resolve_start_url_concurrency(constraints: dict[str, Any] | None = None) -> int:
    """How many ``role=start`` URLs may run as parallel sub-runs."""

    cons = _constraints_dict(constraints)
    env_value = _env_int("VSPIDER_START_URL_CONCURRENCY", 1, min_value=1, max_value=16)
    max_runs = int(cons.get("max_runs") or 0)
    if max_runs > 0:
        return max(1, min(env_value, max_runs))
    return env_value


def collect_start_urls(target_url: str, urls: list[str] | None) -> list[str]:
    """De-duplicated ordered start URL list (``target_url`` first)."""

    out: list[str] = []
    for raw in [target_url, *(urls or [])]:
        norm = str(raw or "").strip()
        if norm and norm not in out:
            out.append(norm)
    return out

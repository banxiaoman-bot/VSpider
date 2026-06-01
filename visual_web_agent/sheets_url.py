"""Backwards-compatibility shim.

The Sheets-specific URL converter has moved into the generic
:mod:`visual_web_agent.data_export` registry — a one-rule entry among many.
Existing imports keep working through these re-exports; new code should call
``data_export.find_data_export_url`` so it picks up Sheets *and* OneDrive *and*
any future transformer with no per-site branching.
"""
from __future__ import annotations

try:  # package import path
    from .data_export import (  # noqa: F401
        convert_google_sheets_url_to_csv,
        is_google_sheets_url,
    )
except ImportError:  # pragma: no cover - direct script import fallback
    from data_export import (  # noqa: F401  type: ignore[no-redef]
        convert_google_sheets_url_to_csv,
        is_google_sheets_url,
    )

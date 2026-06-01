"""Catch-all adapters for ``upload_to_page`` / ``media_source`` / ``unknown``.

These produce a stable :class:`AdapterResult` so callers can branch on
``kind`` without special-casing missing intents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from visual_web_agent.io_contract.input_contract import AttachmentSpec

from .base import AdapterResult


def adapt_upload(
    spec: AttachmentSpec,
    *,
    goal: str = "",
    options: dict[str, Any] | None = None,
) -> AdapterResult:
    path = Path(spec.path)
    if not path.exists():
        return AdapterResult(
            kind="upload", ok=False, reasons=[f"attachment_not_found:{path}"]
        )
    return AdapterResult(
        kind="upload",
        ok=True,
        upload_path=str(path),
        metadata={
            "filename": spec.filename or path.name,
            "mime": spec.mime,
            "size": spec.size or path.stat().st_size,
        },
    )


def adapt_media_source(
    spec: AttachmentSpec,
    *,
    goal: str = "",
    options: dict[str, Any] | None = None,
) -> AdapterResult:
    path = Path(spec.path)
    if not path.exists():
        return AdapterResult(
            kind="media_source", ok=False, reasons=[f"attachment_not_found:{path}"]
        )
    return AdapterResult(
        kind="media_source",
        ok=True,
        media_refs=[str(path)],
        metadata={
            "filename": spec.filename or path.name,
            "mime": spec.mime,
            "size": spec.size or path.stat().st_size,
        },
    )


def adapt_unknown(
    spec: AttachmentSpec,
    *,
    goal: str = "",
    options: dict[str, Any] | None = None,
) -> AdapterResult:
    path = Path(spec.path)
    metadata = {
        "filename": spec.filename or (path.name if path.exists() else ""),
        "mime": spec.mime,
        "size": spec.size,
    }
    return AdapterResult(
        kind="unknown",
        ok=False,
        metadata=metadata,
        reasons=["attachment_intent_unknown"],
    )

"""Shared types for attachment adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


class AdapterError(RuntimeError):
    """Raised when an adapter fails in an expected, recoverable way."""


class UnsupportedAttachmentError(AdapterError):
    """Raised when no adapter can consume the attachment."""


@dataclass
class AdapterResult:
    """Uniform return shape for every adapter.

    Fields:
      ``kind``        - canonical adapter family: ``rows`` / ``text`` /
                        ``image`` / ``pdf`` / ``archive`` / ``upload`` /
                        ``media_source`` / ``unknown``.
      ``ok``          - did the adapter produce usable output?
      ``rows``        - present for ``kind=rows``; iterable of dict rows.
      ``text``        - present for ``kind=text/pdf/image`` (caption); short
                        excerpt for prompt context, NOT the full document.
      ``upload_path`` - present for ``kind=upload``; local path Playwright can
                        feed into ``set_input_files``.
      ``media_refs``  - present for ``kind=media_source``; list of URI hints.
      ``image_b64``   - present for ``kind=image``; base64 data-URL(s) fed
                        to the VLM as extra multimodal images.
      ``metadata``    - free-form info (page_count, columns, dims, ...).
      ``reasons``     - explanation strings, mostly for debugging.
    """

    kind: str
    ok: bool = True
    rows: Iterable[dict[str, Any]] | None = None
    text: str = ""
    upload_path: str = ""
    media_refs: list[str] = field(default_factory=list)
    image_b64: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ok": bool(self.ok),
            "row_count": (
                None if self.rows is None
                else (len(self.rows) if hasattr(self.rows, "__len__") else -1)
            ),
            "text_excerpt": (self.text or "")[:500],
            "upload_path": self.upload_path,
            "media_refs": list(self.media_refs or []),
            "image_count": len(self.image_b64 or []),
            "metadata": dict(self.metadata or {}),
            "reasons": list(self.reasons or []),
        }

"""Attachment adapters dispatch by ``AttachmentSpec.intent``.

The adapters are responsible for turning a stored attachment
(``temp_uploads/<sha>.<ext>``) into something the agent can actually
*use*:

- ``batch_rows``      -> iterable of row dicts for ``smart_batch_runner``
- ``prompt_context``  -> short text snippet / image reference for the prompt
- ``upload_to_page``  -> a local file path passed to Playwright ``set_input_files``
- ``media_source``    -> a reference for media-extraction skills
- ``unknown``         -> opaque blob, recorded but not consumed

This package only ships the dispatcher + stubs for each kind. Real PDF
text extraction, image multimodal feeding, etc. land in follow-up slices
behind these stable interfaces.
"""

from __future__ import annotations

from typing import Any

from visual_web_agent.io_contract.input_contract import AttachmentSpec

from . import rows as _rows
from . import text as _text
from . import image as _image
from . import pdf as _pdf
from . import archive as _archive
from . import generic as _generic
from . import word as _word
from . import powerpoint as _powerpoint
from . import spreadsheet_text as _spreadsheet_text
from . import email as _email
from .base import (
    AdapterResult,
    AdapterError,
    UnsupportedAttachmentError,
)


_DISPATCH = {
    "batch_rows":     _rows.adapt,
    "prompt_context": _text.adapt,
    "upload_to_page": _generic.adapt_upload,
    "media_source":   _generic.adapt_media_source,
    "unknown":        _generic.adapt_unknown,
}


_WORD_MIMES = frozenset({
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-word.document.macroEnabled.12",
    "application/vnd.oasis.opendocument.text",
    "application/rtf",
})
_WORD_SUFFIXES = frozenset({".doc", ".docx", ".docm", ".odt", ".rtf"})

_POWERPOINT_MIMES = frozenset({
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
    "application/vnd.oasis.opendocument.presentation",
})
_POWERPOINT_SUFFIXES = frozenset({".ppt", ".pptx", ".pptm", ".odp"})

_SPREADSHEET_MIMES = frozenset({
    "text/csv",
    "text/tab-separated-values",
    "application/vnd.ms-excel",
    "application/vnd.ms-excel.sheet.macroEnabled.12",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/parquet",
})
_SPREADSHEET_SUFFIXES = frozenset({
    ".csv", ".tsv", ".xls", ".xlsx", ".xlsm", ".ods", ".parquet"
})

_EMAIL_MIMES = frozenset({"message/rfc822", "application/vnd.ms-outlook"})
_EMAIL_SUFFIXES = frozenset({".eml", ".msg"})

_IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"
})


def _suffix_of(spec: AttachmentSpec) -> str:
    name = (spec.filename or spec.path or "").lower()
    idx = name.rfind(".")
    return name[idx:] if idx >= 0 else ""


def adapt_attachment(
    spec: AttachmentSpec,
    *,
    goal: str = "",
    options: dict[str, Any] | None = None,
) -> AdapterResult:
    """Dispatch a stored attachment to its intent-specific adapter.

    The dispatcher first looks at ``spec.intent``. For ``prompt_context`` we
    further branch by mime / suffix: image / pdf / word / powerpoint /
    spreadsheet-text / email / archive / fall-back-text. Anything we cannot
    handle returns a stub :class:`AdapterResult` with ``ok=False`` so
    callers can still proceed.
    """

    intent = spec.intent or "unknown"

    if intent == "prompt_context":
        mime = (spec.mime or "").lower()
        suffix = _suffix_of(spec)
        if mime.startswith("image/") or suffix in _IMAGE_SUFFIXES:
            return _image.adapt(spec, goal=goal, options=options or {})
        if mime == "application/pdf" or suffix == ".pdf":
            return _pdf.adapt(spec, goal=goal, options=options or {})
        if mime in _WORD_MIMES or suffix in _WORD_SUFFIXES:
            return _word.adapt(spec, goal=goal, options=options or {})
        if mime in _POWERPOINT_MIMES or suffix in _POWERPOINT_SUFFIXES:
            return _powerpoint.adapt(spec, goal=goal, options=options or {})
        if mime in _SPREADSHEET_MIMES or suffix in _SPREADSHEET_SUFFIXES:
            return _spreadsheet_text.adapt(spec, goal=goal, options=options or {})
        if mime in _EMAIL_MIMES or suffix in _EMAIL_SUFFIXES:
            return _email.adapt(spec, goal=goal, options=options or {})
        if mime in {
            "application/zip", "application/vnd.rar",
            "application/x-7z-compressed", "application/gzip",
        }:
            return _archive.adapt(spec, goal=goal, options=options or {})
        return _text.adapt(spec, goal=goal, options=options or {})

    handler = _DISPATCH.get(intent)
    if handler is None:
        return _generic.adapt_unknown(spec, goal=goal, options=options or {})
    return handler(spec, goal=goal, options=options or {})


__all__ = [
    "AdapterResult",
    "AdapterError",
    "UnsupportedAttachmentError",
    "adapt_attachment",
]

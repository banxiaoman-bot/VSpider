"""Word / RTF / ODT document adapter.

Returns a short text excerpt suitable for the system prompt. Heavy parsers
(``python-docx``, ``odfpy``) are imported lazily; when no extractor is
available we emit a placeholder caption instead of crashing.

Supported suffixes:

- ``.docx``                via ``python-docx``
- ``.docm``                via ``python-docx`` (macro-enabled treated as docx)
- ``.doc`` (legacy binary) via ``olefile`` + ``antiword``-style heuristic, or
                           placeholder when no extractor is available
- ``.odt``                 via ``odfpy``
- ``.rtf``                 via ``striprtf`` or simple control-word strip
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from visual_web_agent.io_contract.input_contract import AttachmentSpec

from .base import AdapterResult


_DEFAULT_EXCERPT_CHARS = 4000


def adapt(
    spec: AttachmentSpec,
    *,
    goal: str = "",
    options: dict[str, Any] | None = None,
) -> AdapterResult:
    options = options or {}
    max_chars = int(options.get("max_chars") or _DEFAULT_EXCERPT_CHARS)
    path = Path(spec.path)
    if not path.exists():
        return AdapterResult(
            kind="word", ok=False, reasons=[f"attachment_not_found:{path}"]
        )

    suffix = path.suffix.lower()
    if suffix in {".docx", ".docm"}:
        return _read_docx(path, max_chars=max_chars, spec=spec)
    if suffix == ".doc":
        return _read_legacy_doc(path, max_chars=max_chars, spec=spec)
    if suffix == ".odt":
        return _read_odt(path, max_chars=max_chars, spec=spec)
    if suffix == ".rtf":
        return _read_rtf(path, max_chars=max_chars, spec=spec)

    # mime-based fallback when suffix is missing / wrong
    mime = (spec.mime or "").lower()
    if mime in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-word.document.macroEnabled.12",
    }:
        return _read_docx(path, max_chars=max_chars, spec=spec)
    if mime == "application/msword":
        return _read_legacy_doc(path, max_chars=max_chars, spec=spec)
    if mime == "application/vnd.oasis.opendocument.text":
        return _read_odt(path, max_chars=max_chars, spec=spec)
    if mime == "application/rtf":
        return _read_rtf(path, max_chars=max_chars, spec=spec)

    return _placeholder(path, spec, reason="unsupported_word_suffix")


def _read_docx(path: Path, *, max_chars: int, spec: AttachmentSpec) -> AdapterResult:
    try:
        import docx  # type: ignore  # python-docx
    except Exception:
        return _placeholder(path, spec, reason="python_docx_unavailable")

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        return AdapterResult(
            kind="word", ok=False, reasons=[f"docx_open_failed:{exc}"]
        )

    chunks: list[str] = []
    paragraph_count = 0
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").rstrip()
        if not text:
            continue
        chunks.append(text)
        paragraph_count += 1
        if sum(len(c) for c in chunks) >= max_chars:
            break

    table_text: list[str] = []
    for table in document.tables:
        for row in table.rows:
            cells = [(cell.text or "").strip() for cell in row.cells]
            line = " | ".join(c for c in cells if c)
            if line:
                table_text.append(line)
        if sum(len(c) for c in table_text) >= max_chars:
            break

    body = "\n".join(chunks)
    if table_text:
        body = (body + "\n\n[tables]\n" + "\n".join(table_text)).strip()

    excerpt = body[:max_chars]
    return AdapterResult(
        kind="word",
        ok=True,
        text=excerpt,
        metadata={
            "filename": spec.filename or path.name,
            "paragraph_count": paragraph_count,
            "table_count": len(document.tables),
            "total_chars": len(body),
            "excerpt_chars": len(excerpt),
            "source_suffix": path.suffix.lower(),
        },
    )


def _read_legacy_doc(path: Path, *, max_chars: int, spec: AttachmentSpec) -> AdapterResult:
    """Legacy ``.doc`` (OLE compound) extraction.

    We do NOT bundle a heavyweight Office reader. If ``olefile`` or external
    ``antiword`` is installed users can extend this; otherwise we return a
    placeholder caption so the run does not crash.
    """

    return _placeholder(
        path, spec,
        reason="legacy_doc_extractor_not_installed",
        hint="convert .doc to .docx via Word / LibreOffice for full text extraction",
    )


def _read_odt(path: Path, *, max_chars: int, spec: AttachmentSpec) -> AdapterResult:
    try:
        from odf import text as _odf_text  # type: ignore
        from odf.opendocument import load as _odf_load  # type: ignore
        from odf.element import Element  # type: ignore  # noqa: F401
    except Exception:
        return _placeholder(path, spec, reason="odfpy_unavailable")

    try:
        document = _odf_load(str(path))
    except Exception as exc:
        return AdapterResult(
            kind="word", ok=False, reasons=[f"odt_open_failed:{exc}"]
        )

    chunks: list[str] = []
    for paragraph in document.getElementsByType(_odf_text.P):
        text = _odf_node_text(paragraph)
        if text:
            chunks.append(text)
        if sum(len(c) for c in chunks) >= max_chars:
            break

    body = "\n".join(chunks)
    excerpt = body[:max_chars]
    return AdapterResult(
        kind="word",
        ok=True,
        text=excerpt,
        metadata={
            "filename": spec.filename or path.name,
            "paragraph_count": len(chunks),
            "total_chars": len(body),
            "excerpt_chars": len(excerpt),
            "source_suffix": ".odt",
        },
    )


def _odf_node_text(node: Any) -> str:
    parts: list[str] = []
    for child in node.childNodes:
        if child.nodeType == 3:  # TEXT_NODE
            parts.append(child.data)
        elif hasattr(child, "childNodes"):
            parts.append(_odf_node_text(child))
    return "".join(parts).strip()


_RTF_CONTROL_RE = re.compile(r"\\[a-zA-Z]+-?\d*\s?|\\\*?\\[^\s]+|[{}]")


def _read_rtf(path: Path, *, max_chars: int, spec: AttachmentSpec) -> AdapterResult:
    try:
        raw = path.read_bytes()
    except Exception as exc:
        return AdapterResult(
            kind="word", ok=False, reasons=[f"rtf_read_failed:{exc}"]
        )

    text: str = ""
    try:
        from striprtf.striprtf import rtf_to_text  # type: ignore

        text = rtf_to_text(raw.decode("latin-1", errors="ignore"))
    except Exception:
        body = raw.decode("latin-1", errors="ignore")
        text = _RTF_CONTROL_RE.sub("", body)
        text = re.sub(r"\s+", " ", text).strip()

    excerpt = text[:max_chars]
    return AdapterResult(
        kind="word",
        ok=True,
        text=excerpt,
        metadata={
            "filename": spec.filename or path.name,
            "total_chars": len(text),
            "excerpt_chars": len(excerpt),
            "source_suffix": ".rtf",
        },
        reasons=["striprtf_unavailable_fell_back_to_regex"] if not text else [],
    )


def _placeholder(
    path: Path,
    spec: AttachmentSpec,
    *,
    reason: str,
    hint: str = "",
) -> AdapterResult:
    caption = f"[word document attachment: {spec.filename or path.name}, {reason}]"
    reasons = [reason]
    if hint:
        reasons.append(hint)
    return AdapterResult(
        kind="word",
        ok=True,
        text=caption,
        metadata={
            "filename": spec.filename or path.name,
            "size": spec.size or (path.stat().st_size if path.exists() else 0),
        },
        reasons=reasons,
    )

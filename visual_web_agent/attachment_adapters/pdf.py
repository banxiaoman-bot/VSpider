"""PDF-as-prompt-context adapter (stub-with-fallback).

If ``pypdf`` (or ``pdfplumber``) is available it returns a real text excerpt;
otherwise it returns a placeholder caption so the agent can still proceed.
OCR remains opt-in and is **not** wired here.
"""

from __future__ import annotations

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
            kind="pdf", ok=False, reasons=[f"attachment_not_found:{path}"]
        )

    text = ""
    page_count = 0
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        chunks: list[str] = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                chunks.append("")
            if sum(len(c) for c in chunks) >= max_chars:
                break
        text = "\n".join(chunks)
    except Exception:
        try:
            import pdfplumber  # type: ignore

            with pdfplumber.open(str(path)) as pdf:
                page_count = len(pdf.pages)
                chunks: list[str] = []
                for page in pdf.pages:
                    try:
                        chunks.append(page.extract_text() or "")
                    except Exception:
                        chunks.append("")
                    if sum(len(c) for c in chunks) >= max_chars:
                        break
                text = "\n".join(chunks)
        except Exception:
            return AdapterResult(
                kind="pdf",
                ok=True,
                text=f"[pdf attachment: {spec.filename or path.name}, text extractor not installed]",
                metadata={
                    "filename": spec.filename or path.name,
                    "size": spec.size or path.stat().st_size,
                },
                reasons=["no_pdf_extractor_available"],
            )

    excerpt = text[:max_chars]
    return AdapterResult(
        kind="pdf",
        ok=True,
        text=excerpt,
        metadata={
            "filename": spec.filename or path.name,
            "page_count": page_count,
            "total_chars": len(text),
            "excerpt_chars": len(excerpt),
        },
    )

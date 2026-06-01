"""Text / Markdown / JSON-as-context adapter.

Returns a short excerpt suitable for stuffing into the system prompt. The
excerpt length is bounded so prompts stay within token budget; full content
remains on disk and can be referenced via ``spec.path``.
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
            kind="text", ok=False, reasons=[f"attachment_not_found:{path}"]
        )

    try:
        raw = path.read_bytes()
    except Exception as exc:
        return AdapterResult(
            kind="text", ok=False, reasons=[f"read_failed:{exc}"]
        )

    text = ""
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    excerpt = text[:max_chars]
    return AdapterResult(
        kind="text",
        ok=True,
        text=excerpt,
        metadata={
            "total_chars": len(text),
            "excerpt_chars": len(excerpt),
            "source_suffix": path.suffix.lower(),
        },
    )

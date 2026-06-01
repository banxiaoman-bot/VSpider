"""Email (.eml / .msg) adapter.

``.eml`` uses the stdlib ``email`` package, which is always available.
``.msg`` (Outlook compound) needs ``extract-msg``; without it we fall back
to a placeholder caption.
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
            kind="email", ok=False, reasons=[f"attachment_not_found:{path}"]
        )

    suffix = path.suffix.lower()
    if suffix == ".eml" or (spec.mime or "").lower() == "message/rfc822":
        return _read_eml(path, max_chars=max_chars, spec=spec)
    if suffix == ".msg" or (spec.mime or "").lower() == "application/vnd.ms-outlook":
        return _read_msg(path, max_chars=max_chars, spec=spec)
    return AdapterResult(
        kind="email", ok=False, reasons=[f"unsupported_email_suffix:{suffix}"]
    )


def _read_eml(path: Path, *, max_chars: int, spec: AttachmentSpec) -> AdapterResult:
    import email
    from email import policy

    try:
        with open(path, "rb") as fh:
            message = email.message_from_binary_file(fh, policy=policy.default)
    except Exception as exc:
        return AdapterResult(
            kind="email", ok=False, reasons=[f"eml_parse_failed:{exc}"]
        )

    headers = {
        "subject": str(message.get("subject") or ""),
        "from": str(message.get("from") or ""),
        "to": str(message.get("to") or ""),
        "date": str(message.get("date") or ""),
    }
    body_text = ""
    try:
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() == "text/plain":
                    body_text = part.get_content()
                    break
            if not body_text:
                for part in message.walk():
                    if part.get_content_type() == "text/html":
                        body_text = part.get_content()
                        break
        else:
            body_text = message.get_content()
    except Exception:
        body_text = ""

    body_text = str(body_text or "")
    block = (
        f"Subject: {headers['subject']}\n"
        f"From: {headers['from']}\n"
        f"To: {headers['to']}\n"
        f"Date: {headers['date']}\n\n"
        f"{body_text}"
    )
    excerpt = block[:max_chars]
    return AdapterResult(
        kind="email",
        ok=True,
        text=excerpt,
        metadata={
            "filename": spec.filename or path.name,
            "headers": headers,
            "total_chars": len(block),
            "excerpt_chars": len(excerpt),
            "source_suffix": ".eml",
        },
    )


def _read_msg(path: Path, *, max_chars: int, spec: AttachmentSpec) -> AdapterResult:
    try:
        import extract_msg  # type: ignore
    except Exception:
        return AdapterResult(
            kind="email",
            ok=True,
            text=f"[outlook .msg attachment: {spec.filename or path.name}, extract_msg not installed]",
            metadata={
                "filename": spec.filename or path.name,
                "size": spec.size or path.stat().st_size,
            },
            reasons=["extract_msg_unavailable"],
        )

    try:
        message = extract_msg.Message(str(path))
        message.parse()
    except Exception as exc:
        return AdapterResult(
            kind="email", ok=False, reasons=[f"msg_parse_failed:{exc}"]
        )

    block = (
        f"Subject: {message.subject or ''}\n"
        f"From: {message.sender or ''}\n"
        f"To: {message.to or ''}\n"
        f"Date: {message.date or ''}\n\n"
        f"{message.body or ''}"
    )
    excerpt = block[:max_chars]
    return AdapterResult(
        kind="email",
        ok=True,
        text=excerpt,
        metadata={
            "filename": spec.filename or path.name,
            "headers": {
                "subject": message.subject or "",
                "from": message.sender or "",
                "to": message.to or "",
                "date": str(message.date or ""),
            },
            "total_chars": len(block),
            "excerpt_chars": len(excerpt),
            "source_suffix": ".msg",
        },
    )

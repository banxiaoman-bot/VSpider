"""Deterministic input-clarification detector (data-level human-in-the-loop).

Inspects a normalized :class:`InputContract` and surfaces structured questions
a human should answer when the inputs are incomplete or ambiguous -- e.g. no
entry point, an attachment whose intent could not be inferred, a batch-rows
spreadsheet without a URL column, or an empty upload.

Pure + rule-based (no LLM, no FS). Each :class:`ClarificationRequest` carries a
human-readable ``message`` that can be passed straight to the existing
``api_server.broadcast_human_intervention(reason)`` channel via
:meth:`ClarificationRequest.as_reason`. The runtime "pause the run and wait for
the answer" wiring lives in the agent loop and is intentionally out of scope
here so this layer stays trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .input_contract import InputContract


_URL_COLUMN_HINTS = {
    "url", "urls", "link", "links", "uri", "href",
    "\u7f51\u5740", "\u94fe\u63a5", "\u5730\u5740", "\u7f51\u7ad9", "\u94fe\u63a5\u5730\u5740",
}


@dataclass
class ClarificationRequest:
    """One thing the agent needs a human to confirm before it can proceed.

    ``severity``: ``block`` (cannot proceed without an answer) or ``warn``
    (can proceed with a sensible default, but the result may be off).
    """

    target: str
    code: str
    message: str
    options: list[str] = field(default_factory=list)
    severity: str = "warn"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "code": self.code,
            "message": self.message,
            "options": list(self.options or []),
            "severity": self.severity,
        }

    def as_reason(self) -> str:
        if self.options:
            return f"{self.message}\uff08\u53ef\u9009\uff1a{' / '.join(self.options)}\uff09"
        return self.message


def _has_url_column(schema: dict[str, Any]) -> bool:
    explicit = str(schema.get("url_column") or "").strip().lower()
    if explicit and explicit not in {"auto", "none"}:
        return True
    for col in schema.get("columns") or []:
        name = str(col).strip().lower()
        if not name:
            continue
        if name in _URL_COLUMN_HINTS or "url" in name or "\u7f51\u5740" in name or "\u94fe\u63a5" in name:
            return True
    return False


def detect_input_clarifications(contract: InputContract) -> list[ClarificationRequest]:
    """Return the deterministic clarification questions for ``contract``.

    Empty list means the inputs are unambiguous enough to start.
    """

    out: list[ClarificationRequest] = []

    if not contract.urls and not contract.attachments:
        out.append(ClarificationRequest(
            target="urls",
            code="no_entry_point",
            message="\u6ca1\u6709\u63d0\u4f9b\u7f51\u5740\uff0c\u4e5f\u6ca1\u6709\u4e0a\u4f20\u9644\u4ef6\u2014\u2014\u6211\u4e0d\u77e5\u9053\u4ece\u54ea\u91cc\u5f00\u59cb\u3002",
            options=["\u63d0\u4f9b\u5165\u53e3 URL", "\u7528\u641c\u7d22\u5f15\u64ce\u4ece\u76ee\u6807\u91cc\u627e", "\u53d6\u6d88\u4efb\u52a1"],
            severity="block",
        ))

    for att in contract.attachments:
        label = att.filename or att.path or "(\u672a\u547d\u540d\u9644\u4ef6)"

        if att.intent == "unknown":
            out.append(ClarificationRequest(
                target=f"attachment:{label}",
                code="attachment_intent_unknown",
                message=f"\u4e0d\u786e\u5b9a\u9644\u4ef6 {label} \u8be5\u600e\u4e48\u7528\u3002",
                options=["\u6279\u91cf\u6570\u636e(batch_rows)", "\u4e0a\u4f20\u5230\u9875\u9762(upload_to_page)", "\u4f5c\u4e3a\u53c2\u8003(prompt_context)"],
                severity="block",
            ))

        if int(att.size or 0) <= 0:
            out.append(ClarificationRequest(
                target=f"attachment:{label}",
                code="attachment_empty",
                message=f"\u9644\u4ef6 {label} \u5927\u5c0f\u4e3a 0\uff0c\u53ef\u80fd\u4e0a\u4f20\u4e0d\u5b8c\u6574\u3002",
                options=["\u91cd\u65b0\u4e0a\u4f20", "\u5ffd\u7565\u8be5\u9644\u4ef6"],
                severity="warn",
            ))

        if att.intent == "batch_rows":
            schema = att.schema or {}
            cols = schema.get("columns") or []
            if cols and not _has_url_column(schema):
                col_list = ", ".join(str(c) for c in cols)
                out.append(ClarificationRequest(
                    target=f"attachment:{label}",
                    code="batch_rows_no_url_column",
                    message=f"\u8868 {label} \u6ca1\u8bc6\u522b\u5230 URL \u5217\uff08\u5217\uff1a{col_list}\uff09\u3002",
                    options=["\u6307\u5b9a\u54ea\u5217\u662f\u7f51\u5740", "\u786e\u8ba4\u9010\u884c\u586b\u8868(\u4e0d\u9700 URL \u5217)"],
                    severity="warn",
                ))

    return out

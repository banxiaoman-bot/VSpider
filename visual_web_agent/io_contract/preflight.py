"""Pre-run input preflight -- the testable core of the runtime input gate.

Builds the :class:`InputContract`, resolves a usable start URL, and classifies
clarification requests into ``blocking`` / ``warnings``. Pure: no network (the
LLM is injected as a ``prompt -> text`` callable) and the only disk touch is
reading an attachment's size.

The agent loop (``main.py``) calls :func:`build_preflight` once and then does
the side effects itself: navigate to ``resolved_start_url`` and, for any
``blocking`` request, broadcast a human-intervention prompt and wait.

Decision A (chosen): a missing entry URL is auto-resolved via
:func:`suggest_entry_url` and never blocks; only genuinely ambiguous inputs
(e.g. an attachment whose intent could not be inferred) block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .input_contract import (
    InputContract,
    EntrySuggestion,
    build_input_contract,
    suggest_entry_url,
)
from .clarification import ClarificationRequest, detect_input_clarifications


@dataclass
class Preflight:
    contract: InputContract
    resolved_start_url: str = ""
    entry_suggestion: EntrySuggestion | None = None
    blocking: list[ClarificationRequest] = field(default_factory=list)
    warnings: list[ClarificationRequest] = field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return bool(self.blocking)

    def blocking_reason(self) -> str:
        return "\uff1b".join(r.as_reason() for r in self.blocking)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved_start_url": self.resolved_start_url,
            "entry_suggestion": (
                self.entry_suggestion.to_dict() if self.entry_suggestion else None
            ),
            "blocking": [r.to_dict() for r in self.blocking],
            "warnings": [r.to_dict() for r in self.warnings],
        }


def build_preflight(
    goal: str,
    *,
    start_url: str = "",
    urls: Any = None,
    upload_file: str = "",
    auth_profiles: str | list[str] = "",
    constraints: dict[str, Any] | None = None,
    source: str = "cli",
    llm: Callable[[str], str] | None = None,
) -> Preflight:
    """Build a :class:`Preflight` from loose ``run_agent``-style parameters."""

    attachments: list[dict[str, Any]] = []
    if upload_file:
        fp = Path(upload_file)
        attachments.append({
            "path": str(fp),
            "filename": fp.name,
            "size": fp.stat().st_size if fp.exists() else 0,
        })

    contract = build_input_contract(
        goal=goal or "",
        target_url=start_url or "",
        urls=urls or [],
        attachments=attachments,
        auth_profiles=auth_profiles or "",
        constraints=constraints or None,
        source=source,
    )

    entry_suggestion: EntrySuggestion | None = None
    resolved = contract.primary_start_url
    if not resolved:
        entry_suggestion = suggest_entry_url(goal or "", llm=llm)
        if entry_suggestion:
            resolved = entry_suggestion.url

    blocking: list[ClarificationRequest] = []
    warnings: list[ClarificationRequest] = []
    for req in detect_input_clarifications(contract):
        if req.code == "no_entry_point" and resolved:
            # decision A: a suggestion covered the missing entry -> informational
            warnings.append(req)
        elif req.severity == "block":
            blocking.append(req)
        else:
            warnings.append(req)

    return Preflight(
        contract=contract,
        resolved_start_url=resolved,
        entry_suggestion=entry_suggestion,
        blocking=blocking,
        warnings=warnings,
    )

"""Structured action execution result.

This module is intentionally small and dependency-free. It gives the agent a
single shape for "what happened after an action" without forcing every action
handler to change at once.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


@dataclass
class ActionResult:
    """Normalized result for one browser action."""

    action: str = ""
    success: bool = True
    message: str = ""
    error: str = ""
    target_id: int = 0
    type_value: str = ""
    memory_key: str = ""
    before_url: str = ""
    after_url: str = ""
    before_pages: int = 0
    after_pages: int = 0
    changed_url: bool = False
    changed_pages: bool = False
    changed_dom: bool = False
    clicked_text: str = ""
    value_readbacks: list[dict[str, Any]] = field(default_factory=list)
    extracted_rows: int = 0
    output_file: str = ""
    output_kind: str = ""
    output_mime: str = ""
    output_size: int = 0
    output_sha256: str = ""
    output_container: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_utc_now_iso)

    @classmethod
    def from_action(
        cls,
        action: Any,
        *,
        success: bool = True,
        message: str = "",
        error: str = "",
        before_url: str = "",
        after_url: str = "",
        before_pages: int = 0,
        after_pages: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> "ActionResult":
        if isinstance(action, dict):
            action_name = str(action.get("action") or "")
            target_id = _safe_int(action.get("target_id"))
            type_value = str(action.get("type_value") or "")
            memory_key = str(action.get("memory_key") or "")
        else:
            action_name = str(getattr(action, "action", "") or "")
            target_id = _safe_int(getattr(action, "target_id", 0))
            type_value = str(getattr(action, "type_value", "") or "")
            memory_key = str(getattr(action, "memory_key", "") or "")

        merged_meta = dict(metadata or {})
        clicked_text = str(merged_meta.pop("clicked_text", "") or "")
        output_file = str(merged_meta.pop("output_file", "") or "")
        output_kind = str(merged_meta.pop("output_kind", "") or "")
        output_mime = str(merged_meta.pop("output_mime", "") or "")
        output_size = _safe_int(merged_meta.pop("output_size", 0))
        output_sha256 = str(merged_meta.pop("output_sha256", "") or "")
        output_container = str(merged_meta.pop("output_container", "") or "")
        extracted_rows = _safe_int(merged_meta.pop("extracted_rows", 0))
        readbacks = merged_meta.pop("value_readbacks", None)
        if not isinstance(readbacks, list):
            readbacks = []

        return cls(
            action=action_name,
            success=bool(success),
            message=str(message or ""),
            error=str(error or ""),
            target_id=target_id,
            type_value=type_value,
            memory_key=memory_key,
            before_url=str(before_url or ""),
            after_url=str(after_url or ""),
            before_pages=int(before_pages or 0),
            after_pages=int(after_pages or 0),
            changed_url=bool(before_url and after_url and before_url != after_url),
            changed_pages=bool(before_pages != after_pages),
            clicked_text=clicked_text,
            value_readbacks=readbacks,
            extracted_rows=extracted_rows,
            output_file=output_file,
            output_kind=output_kind,
            output_mime=output_mime,
            output_size=output_size,
            output_sha256=output_sha256,
            output_container=output_container,
            metadata=merged_meta,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def short_message(self) -> str:
        if self.success:
            detail = self.message or "ok"
        else:
            detail = self.error or self.message or "failed"
        target = f" target_id={self.target_id}" if self.target_id else ""
        return f"{self.action}{target}: {detail}"

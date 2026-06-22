"""Append-only JSONL event stream for agent runs."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .action_result import ActionResult
    from .browser_state import BrowserStateSnapshot
except ImportError:
    from action_result import ActionResult
    from browser_state import BrowserStateSnapshot

logger = logging.getLogger("vspider.events")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


class EventStream:
    """Small best-effort JSONL writer.

    Event logging must never break the browser agent, so every write is guarded.
    """

    def __init__(
        self,
        *,
        run_id: str,
        log_dir: str | Path = "logs",
        enabled: bool = True,
    ) -> None:
        self.run_id = str(run_id or datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.enabled = bool(enabled)
        self.path = Path(log_dir) / f"event_stream_{self.run_id}.jsonl"
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.enabled = False
            logger.debug("[EVENT STREAM] disabled: %s", exc)

    def emit(self, event_type: str, **payload: Any) -> None:
        if not self.enabled:
            return
        record = {
            "ts": _utc_now_iso(),
            "run_id": self.run_id,
            "type": str(event_type or "event"),
            **payload,
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            logger.debug("[EVENT STREAM] write failed: %s", exc)

    def run_start(self, *, goal: str, start_url: str) -> None:
        self.emit("run_start", goal=goal, start_url=start_url)

    def observe(
        self,
        *,
        step: int,
        url: str = "",
        screenshot_path: str = "",
        ax_lines: int = 0,
        browser_state: BrowserStateSnapshot | dict[str, Any] | None = None,
        perception_reused: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if isinstance(browser_state, BrowserStateSnapshot):
            state_payload = browser_state.to_dict()
        elif isinstance(browser_state, dict):
            state_payload = dict(browser_state)
        else:
            state_payload = None
        self.emit(
            "observe",
            step=step,
            url=url,
            screenshot_path=screenshot_path,
            ax_lines=ax_lines,
            browser_state=state_payload,
            perception_reused=bool(perception_reused),
            metadata=dict(metadata or {}),
        )

    def decide(
        self,
        *,
        step: int,
        decisions: Any,
        url: str = "",
        source: str = "vlm",
    ) -> None:
        self.emit(
            "decide",
            step=step,
            url=url,
            source=source,
            decisions=decisions,
        )

    def act(self, *, step: int, result: ActionResult | dict[str, Any]) -> None:
        if isinstance(result, ActionResult):
            payload = result.to_dict()
        else:
            payload = dict(result)
        self.emit("act", step=step, result=payload)

    def guard(
        self,
        *,
        step: int,
        name: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.emit(
            "guard",
            step=step,
            name=name,
            message=message,
            metadata=dict(metadata or {}),
        )

    def verify(
        self,
        *,
        step: int,
        name: str,
        success: bool,
        expected: Any = "",
        observed: Any = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.emit(
            "verify",
            step=step,
            name=name,
            success=bool(success),
            expected=expected,
            observed=observed,
            metadata=dict(metadata or {}),
        )

    def extract(
        self,
        *,
        step: int,
        source: str,
        rows: int = 0,
        output_file: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.emit(
            "extract",
            step=step,
            source=source,
            rows=int(rows or 0),
            output_file=output_file,
            metadata=dict(metadata or {}),
        )

    def done(
        self,
        *,
        step: int,
        success: bool,
        reason: str = "",
        message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.emit(
            "done",
            step=step,
            success=bool(success),
            reason=reason,
            message=message,
            metadata=dict(metadata or {}),
        )

    def run_end(
        self,
        *,
        success: bool,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.emit(
            "run_end",
            success=bool(success),
            reason=reason,
            metadata=dict(metadata or {}),
        )

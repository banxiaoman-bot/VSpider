"""VSpider broadcasting infrastructure.

Extracted from api_server.py (Slice 3) — provides WebSocket broadcasting,
phase event persistence, and human-in-the-loop coordination.

All broadcast_* functions are thread-safe: they schedule coroutines
onto the FastAPI event loop via ``_schedule()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Coroutine

from fastapi import WebSocket

from visual_web_agent.artifact_manager import artifact_url
from visual_web_agent.secret_redaction import redact_event_payload

logger = logging.getLogger("vspider.api")

# ── Global event-loop binding ────────────────────────────────────────
_API_LOOP: asyncio.AbstractEventLoop | None = None
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def set_api_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Called by ``lifespan`` to bind/unbind the FastAPI event loop."""
    global _API_LOOP
    _API_LOOP = loop


def get_api_loop() -> asyncio.AbstractEventLoop | None:
    return _API_LOOP


# ── ConnectionManager ────────────────────────────────────────────────

class ConnectionManager:
    """Maintain WebSocket connections and broadcast to all clients."""

    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)
        logger.info(f"[WS] 客户端已连接，当前连接数: {len(self.active)}")

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)
        logger.info(f"[WS] 客户端已断开，剩余连接数: {len(self.active)}")

    async def broadcast(self, message: dict) -> None:
        payload = json.dumps(redact_event_payload(message), ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send_log(self, content: str, level: str = "info") -> None:
        await self.broadcast({"type": "log", "level": level, "content": content})

    async def send_screenshot(self, step: int, b64_data: str) -> None:
        await self.broadcast({"type": "screenshot", "step": step, "data": b64_data})

    async def send_done(
        self,
        success: bool,
        message: str = "",
        *,
        answer_type: str | None = None,
        answer: str | None = None,
        answer_domain: str | None = None,
    ) -> None:
        """Broadcast task-done event with optional final-answer fields.

        ``answer_type`` / ``answer`` drive the frontend Final Answer panel:
          * ``"text"`` — pure text/Markdown answer
          * ``"file"`` — structured data export (artifacts tab)
        """
        payload: dict[str, Any] = {"type": "done", "success": success, "message": message}
        if isinstance(answer_type, str) and answer_type:
            payload["answer_type"] = answer_type
        if isinstance(answer, str):
            payload["answer"] = answer
        if isinstance(answer_domain, str) and answer_domain and answer_domain != "generic":
            payload["answer_domain"] = answer_domain
        await self.broadcast(payload)

    async def send_status(self, status: str, **payload: Any) -> None:
        data = {"type": "status", "status": status}
        data.update(payload)
        await self.broadcast(data)


manager = ConnectionManager()


# ── Thread-safe scheduling ───────────────────────────────────────────

_HITL_RESUME_EVENT = threading.Event()


def _schedule(coro: Coroutine[Any, Any, Any]) -> None:
    """Post a coroutine onto the FastAPI event loop (thread-safe).

    - Same loop → ``create_task``
    - Other thread → ``run_coroutine_threadsafe``
    - API not started → silently close the coroutine
    """
    loop = _API_LOOP
    if loop is None or loop.is_closed():
        coro.close()
        return

    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None

    if current is loop:
        task = loop.create_task(coro)
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
    else:
        asyncio.run_coroutine_threadsafe(coro, loop)


# ── Public broadcast helpers ─────────────────────────────────────────

def broadcast_log(content: str, level: str = "info") -> None:
    _schedule(manager.send_log(content, level))


def broadcast_image(b64_data: str, step: int = 0) -> None:
    _schedule(manager.send_screenshot(step, b64_data))


def broadcast_done(
    success: bool,
    message: str = "",
    *,
    answer_type: str | None = None,
    answer: str | None = None,
    answer_domain: str | None = None,
) -> None:
    """Thread-safe broadcast of done event with optional final-answer fields."""
    _schedule(
        manager.send_done(
            success,
            message,
            answer_type=answer_type,
            answer=answer,
            answer_domain=answer_domain,
        )
    )


def broadcast_new_artifact(path: str | Path) -> None:
    try:
        p = Path(path).resolve()
        broadcast_status(
            "new_artifact",
            filename=p.name,
            path=str(p),
            url=artifact_url(p),
        )
    except Exception as exc:
        logger.debug("[ARTIFACT] Failed to broadcast artifact %s: %s", path, exc)


def broadcast_status(status: str, **payload: Any) -> None:
    _schedule(manager.send_status(status, **payload))


# ── Phase event persistence ──────────────────────────────────────────
_PHASE_LOG_LOCK = threading.Lock()
_PHASE_LOG_PATH: Path | None = None
_PHASE_LOG_CONTEXT: ContextVar[Path | None] = ContextVar(
    "vspider_phase_log_path",
    default=None,
)


def set_phase_log_run_id(run_id: str | None) -> None:
    """Open ``logs/phase_<run_id>.jsonl`` for append-only phase event writes.

    Call once per run. Pass ``None`` to disable.
    """
    global _PHASE_LOG_PATH
    with _PHASE_LOG_LOCK:
        if not run_id:
            _PHASE_LOG_PATH = None
            _PHASE_LOG_CONTEXT.set(None)
            return
        try:
            log_dir = Path("logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"phase_{run_id}.jsonl"
            _PHASE_LOG_PATH = path
            _PHASE_LOG_CONTEXT.set(path)
            if not path.exists():
                path.touch()
        except Exception as e:
            logging.getLogger("api_server").warning(
                "set_phase_log_run_id failed: %s", e,
            )
            _PHASE_LOG_PATH = None
            _PHASE_LOG_CONTEXT.set(None)


def _persist_phase_event(payload: dict[str, Any]) -> None:
    """Append one phase event to the active jsonl (best-effort)."""
    path = _PHASE_LOG_CONTEXT.get() or _PHASE_LOG_PATH
    if path is None:
        return
    try:
        line = json.dumps(
            redact_event_payload(payload), ensure_ascii=False, separators=(",", ":")
        )
        with _PHASE_LOG_LOCK:
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
    except Exception as e:
        logging.getLogger("api_server").debug(
            "phase log persist failed (non-fatal): %s", e,
        )


def broadcast_phase(
    phase: str,
    *,
    severity: str = "info",
    message: str = "",
    step: int | None = None,
    duration_ms: int | None = None,
    notice_severity: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Thread-safe broadcast of a structured phase event.

    ``notice_severity`` carries the BrowserEnv ``_last_notice_severity``
    snapshot. The frontend Timeline uses ``max(severity, notice_severity)``
    to color chips.
    """
    if severity not in ("info", "warn", "error"):
        severity = "info"
    payload: dict[str, Any] = {
        "type": "phase",
        "phase": str(phase or "unknown"),
        "severity": severity,
        "message": str(message or ""),
        "ts": time.time(),
    }
    if step is not None:
        payload["step"] = int(step)
    if duration_ms is not None:
        payload["duration_ms"] = int(duration_ms)
    if notice_severity is not None:
        if notice_severity not in ("info", "warn", "error"):
            notice_severity = "info"
        payload["notice_severity"] = notice_severity
    if extra:
        for k, v in extra.items():
            if k not in payload:
                payload[k] = v
    _persist_phase_event(payload)
    _schedule(manager.broadcast(payload))


# ── Human-in-the-loop ────────────────────────────────────────────────

def broadcast_human_intervention(reason: str = "") -> bool:
    if _API_LOOP is None or _API_LOOP.is_closed():
        return False
    _HITL_RESUME_EVENT.clear()
    broadcast_status("human_intervention", reason=reason)
    broadcast_log(f"[HITL] Agent 已挂起，等待人工处理：{reason or 'manual intervention required'}", level="warn")
    return True


async def wait_for_human_resume() -> None:
    await asyncio.to_thread(_HITL_RESUME_EVENT.wait)
    _HITL_RESUME_EVENT.clear()


def resume_human() -> None:
    """Signal that human intervention is complete."""
    _HITL_RESUME_EVENT.set()


# ── Async wrappers ───────────────────────────────────────────────────

async def broadcast_log_async(msg: str, level: str = "info") -> None:
    await manager.send_log(msg, level)


async def broadcast_image_async(b64_string: str, step: int = 0) -> None:
    await manager.send_screenshot(step, b64_string)

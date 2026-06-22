"""Failed-runs API routes — extracted from ``api_server.py``.

Houses ``/api/failed_runs/*`` endpoints (K2 / K6).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

logger = logging.getLogger("api_routes.failed_runs_api")


def register_failed_runs_routes(
    app: FastAPI,
    *,
    failure_archive: Any,
) -> None:
    """Mount all failed-runs routes on *app*.

    Parameters
    ----------
    failure_archive:
        The ``failure_archive`` module (provides ``list_failed_runs``).
    """

    @app.get("/api/failed_runs", summary="列出最近的失败 run（K2）")
    async def get_failed_runs(limit: int = 50) -> dict:
        try:
            records = failure_archive.list_failed_runs(limit=int(limit or 50))
        except Exception as exc:
            logger.warning("[FAILED RUNS] list error: %s", exc)
            records = []
        return {"status": "success", "count": len(records), "items": records}

    @app.get(
        "/api/failed_runs/{run_id}/log",
        summary="下载失败 run 的 HTML 轨迹日志（K2）",
    )
    async def get_failed_run_html_log(run_id: str):
        rid = (run_id or "").strip()
        if not rid or not all(c.isalnum() or c == "_" for c in rid):
            raise HTTPException(status_code=400, detail="invalid run_id")
        log_path = Path("logs") / f"run_log_{rid}.html"
        if not log_path.exists() or not log_path.is_file():
            raise HTTPException(status_code=404, detail="log not found")
        return FileResponse(
            path=str(log_path),
            media_type="text/html; charset=utf-8",
            filename=log_path.name,
        )

    @app.get(
        "/api/failed_runs/{run_id}/phase_events",
        summary="返回失败 run 的 phase 事件列表（K6）",
    )
    async def get_failed_run_phase_events(run_id: str, limit: int = 200) -> dict:
        rid = (run_id or "").strip()
        if not rid or not all(c.isalnum() or c == "_" for c in rid):
            raise HTTPException(status_code=400, detail="invalid run_id")
        phase_path = Path("logs") / f"phase_{rid}.jsonl"
        if not phase_path.exists() or not phase_path.is_file():
            raise HTTPException(status_code=404, detail="phase log not found")

        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 200
        if n <= 0:
            n = 200

        events: list[dict] = []
        total = 0
        try:
            with phase_path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    total += 1
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict):
                        events.append(obj)
        except Exception as exc:
            logger.warning(
                "[FAILED RUNS] phase log read error %s: %s",
                phase_path,
                exc,
            )
            raise HTTPException(
                status_code=500,
                detail="phase log read failed",
            ) from exc

        truncated = total > n
        if truncated:
            events = events[-n:]

        return {
            "status": "success",
            "count": len(events),
            "total": total,
            "truncated": truncated,
            "events": events,
        }

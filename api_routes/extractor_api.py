"""Extractor engine API routes — extracted from ``api_server.py``.

Houses ``/api/extractor/run`` (Y6 generic structured extraction) and
``/api/extractor/select`` (Y22 Scrapling-style selector parsing).
Behaviour is a straight lift-and-delegate from api_server; no logic changes.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Body, FastAPI, HTTPException

logger = logging.getLogger("api_routes.extractor_api")


def register_extractor_routes(
    app: FastAPI,
    *,
    extractor_engine: Any,
) -> None:
    """Mount all extractor routes on *app*.

    Parameters
    ----------
    extractor_engine:
        The shared extraction engine instance (provides ``extract`` /
        ``export_jsonl`` / ``select``).
    """

    @app.post("/api/extractor/run", summary="运行通用结构化抽取引擎（Y6）")
    async def run_extractor(payload: dict[str, Any] = Body(...)) -> dict:
        source = payload.get("source")
        if source in (None, ""):
            raise HTTPException(status_code=400, detail="source is required")
        try:
            result = extractor_engine.extract(
                source,
                source_type=str(payload.get("source_type") or "auto"),
                requested_fields=payload.get("requested_fields") or None,
                max_rows=int(payload.get("max_rows") or 1000),
                all_tables=bool(payload.get("all_tables")),
            )
            artifact = None
            if bool(payload.get("export")):
                artifact = extractor_engine.export_jsonl(
                    result,
                    run_id=str(payload.get("run_id") or "manual"),
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("[EXTRACTOR] run failed: %s", exc)
            raise HTTPException(status_code=500, detail="extractor run failed") from exc
        return {
            "status": "success",
            "result": result,
            "artifact": artifact,
        }

    @app.post("/api/extractor/select", summary="运行 Scrapling 风格 Selector 解析（Y22）")
    async def select_extractor(payload: dict[str, Any] = Body(...)) -> dict:
        source = payload.get("source")
        if source in (None, ""):
            raise HTTPException(status_code=400, detail="source is required")
        try:
            result = extractor_engine.select(
                str(source),
                selector=str(payload.get("selector") or ""),
                selector_type=str(payload.get("selector_type") or payload.get("type") or "css"),
                mode=str(payload.get("mode") or "all"),
                output=str(payload.get("output") or "text"),
                attr=str(payload.get("attr") or ""),
                text=str(payload.get("text") or ""),
                regex=str(payload.get("regex") or ""),
                tag=str(payload.get("tag") or ""),
                max_results=int(payload.get("max_results") or 100),
                case_sensitive=bool(payload.get("case_sensitive")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("[EXTRACTOR] select failed: %s", exc)
            raise HTTPException(status_code=500, detail="extractor select failed") from exc
        return {
            "status": "success",
            "result": result,
        }

"""Spider API routes — extracted from ``api_server.py`` (slice G7).

Houses all ``/api/spider/*`` endpoints. Behaviour is a straight
lift-and-delegate from api_server; no logic changes.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Body, FastAPI, HTTPException

logger = logging.getLogger("api_routes.spider_api")


def register_spider_routes(app: FastAPI, *, spider_lite: Any) -> None:
    """Mount all spider routes on *app*.

    Parameters
    ----------
    spider_lite:
        The ``SpiderLiteManager`` instance (shared with api_server).
    """

    @app.post("/api/spider/run", summary="运行轻量 Spider 爬取（Y24）")
    async def run_spider_lite(payload: dict[str, Any] = Body(...)) -> dict:
        try:
            run_payload = dict(payload or {})
            run_payload.setdefault("persist_run_contracts", True)
            run_payload.setdefault("source", "api")
            result = spider_lite.run(run_payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("[SPIDER] run failed: %s", exc)
            raise HTTPException(status_code=500, detail="spider run failed") from exc
        return {"status": "success", "result": result}

    @app.get("/api/spider/runs", summary="列出轻量 Spider 运行记录（Y24）")
    async def list_spider_lite_runs() -> dict:
        return {"status": "success", "runs": spider_lite.list_runs()}

    @app.get(
        "/api/spider/page_cache/{session_id}",
        summary="读取 Spider 页面响应缓存状态（Y25）",
    )
    async def get_spider_page_cache(session_id: str) -> dict:
        return {"status": "success", "result": spider_lite.cache_state(session_id)}

    @app.get(
        "/api/spider/page_cache/{session_id}/entries",
        summary="列出 Spider 页面响应缓存条目（Y25）",
    )
    async def list_spider_page_cache_entries(session_id: str) -> dict:
        return {"status": "success", "entries": spider_lite.cache_entries(session_id)}

    @app.post(
        "/api/spider/{run_id}/export",
        summary="导出轻量 Spider items feed（Y27）",
    )
    async def export_spider_lite_feed(
        run_id: str, payload: dict[str, Any] = Body(default_factory=dict)
    ) -> dict:
        try:
            artifact = spider_lite.export_feed(
                run_id,
                format=str(
                    payload.get("format") or payload.get("export_format") or "jsonl"
                ),
                filename=str(
                    payload.get("filename") or payload.get("export_filename") or ""
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("[SPIDER] export failed: %s", exc)
            raise HTTPException(
                status_code=500, detail="spider export failed"
            ) from exc
        return {"status": "success", "artifact": artifact}

    @app.get(
        "/api/spider/{run_id}/items",
        summary="查询轻量 Spider items 数据（Y28）",
    )
    async def get_spider_lite_items(
        run_id: str, fields: str = "", offset: int = 0, limit: int = 1000
    ) -> dict:
        try:
            result = spider_lite.items(
                run_id,
                fields=[
                    part.strip()
                    for part in str(fields or "").split(",")
                    if part.strip()
                ],
                offset=int(offset or 0),
                limit=int(limit or 1000),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "success", "result": result}

    @app.get(
        "/api/spider/{run_id}",
        summary="读取轻量 Spider 运行详情（Y24）",
    )
    async def get_spider_lite_run(run_id: str) -> dict:
        result = spider_lite.get_run(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="spider run not found")
        return {"status": "success", "result": result}

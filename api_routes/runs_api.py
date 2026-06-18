"""Runs API routes — extracted from ``api_server.py``.

Houses ``/api/runs/*`` endpoints including network intelligence
and API replay.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import BackgroundTasks, FastAPI, HTTPException

logger = logging.getLogger("api_routes.runs_api")


def register_runs_routes(
    app: FastAPI,
    *,
    manager: Any,
    run_registry: Any,
    network_intelligence: Any,
    api_replay: Any,
    load_run_contract_bundle: Callable,
    queue_snapshot: Callable,
    queue_worker_config: Callable,
    queue_state: Any,
    retry_run_as_queued_task: Callable,
    start_queue_workers: Callable,
) -> None:

    @app.get("/api/runs", summary="列出任务运行记录")
    async def list_runs(limit: int = 50, status: str = "") -> dict:
        try:
            items = run_registry.list_runs(limit=limit, status=status or None)
        except Exception as exc:
            logger.warning("[RUN REGISTRY] list_runs failed: %s", exc)
            raise HTTPException(
                status_code=500, detail="run registry read failed"
            ) from exc
        return {
            "status": "success",
            "count": len(items),
            "runs": items,
        }

    @app.get(
        "/api/runs/{run_id}/network",
        summary="列出 run 捕获到的候选网络数据接口",
    )
    async def get_run_network_candidates(
        run_id: str,
        limit: int = 100,
        min_score: int = 0,
    ) -> dict:
        items = network_intelligence.list_candidates(
            run_id,
            limit=limit,
            min_score=min_score,
        )
        summary = network_intelligence.summarize_candidates(items)
        return {
            "status": "success",
            "run_id": run_id,
            "count": len(items),
            "items": items,
            "summary": summary,
        }

    @app.post(
        "/api/runs/{run_id}/network/replay",
        summary="基于候选网络接口生成或执行 API Replay",
    )
    async def replay_run_network_candidate(
        run_id: str,
        endpoint: str = "",
        page: int = 1,
        page_size: int = 50,
        execute: bool = False,
        paginate: bool = False,
        max_pages: int = 20,
    ) -> dict:
        candidates = network_intelligence.list_candidates(run_id, limit=200)
        candidate = api_replay.choose_candidate(candidates, endpoint=endpoint)
        if candidate is None:
            raise HTTPException(
                status_code=404, detail="network candidate not found"
            )
        plan = api_replay.build_replay_plan(
            candidate,
            page=max(1, int(page or 1)),
            page_size=max(1, min(int(page_size or 50), 500)),
        )
        if not execute:
            return {
                "status": "success",
                "run_id": run_id,
                "dry_run": True,
                "candidate": candidate,
                "plan": plan,
            }
        try:
            if paginate:
                result = api_replay.paginate_replay(
                    run_id=run_id,
                    candidate=candidate,
                    page_size=max(1, min(int(page_size or 50), 500)),
                    start_page=max(1, int(page or 1)),
                    max_pages=max(1, min(int(max_pages or 20), 100)),
                )
            else:
                result = api_replay.replay_candidate(
                    run_id=run_id,
                    candidate=candidate,
                    page=max(1, int(page or 1)),
                    page_size=max(1, min(int(page_size or 50), 500)),
                )
        except Exception as exc:
            logger.warning(
                "[API REPLAY] replay failed for %s: %s", run_id, exc
            )
            raise HTTPException(
                status_code=502,
                detail=f"api replay failed: {type(exc).__name__}",
            ) from exc
        return result

    @app.get("/api/runs/{run_id}", summary="读取单个任务运行记录")
    async def get_run(run_id: str) -> dict:
        rec = run_registry.load_run(run_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {
            "status": "success",
            "run": rec,
            "contracts": load_run_contract_bundle(run_id),
        }

    @app.post(
        "/api/runs/{run_id}/retry",
        summary="手动重试失败/停止的任务（Y16）",
    )
    async def retry_run(run_id: str, background_tasks: BackgroundTasks) -> dict:
        ok, message, retry = retry_run_as_queued_task(run_id)
        started_workers: list[str] = []
        worker_config = queue_worker_config()
        if ok and retry.get("should_start_worker"):
            started_workers, worker_config = start_queue_workers(
                background_tasks
            )
        await manager.send_log(
            f"[QUEUE RETRY] {message}: {run_id}",
            level="info" if ok else "warn",
        )
        return {
            "status": "success" if ok else "error",
            "message": message,
            "retry": retry,
            "queue": queue_snapshot(),
            "started_workers": started_workers,
            "worker_config": worker_config,
            "persisted": queue_state.load_public_snapshot(),
        }

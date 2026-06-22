"""Task-queue API routes — extracted from ``api_server.py``.

Houses ``/api/task_queue/*``, ``/api/browser_pool``,
``/api/browser_sessions`` endpoints.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import BackgroundTasks, FastAPI

logger = logging.getLogger("api_routes.task_queue_api")


def register_task_queue_routes(
    app: FastAPI,
    *,
    manager: Any,
    queue_state: Any,
    queue_snapshot: Callable,
    queue_metrics: Callable,
    queue_worker_config: Callable,
    cancel_queued_task: Callable,
    pause_task_queue: Callable,
    resume_task_queue: Callable,
    recover_queued_tasks: Callable,
    scan_stale_queue_workers: Callable,
    start_queue_workers: Callable,
    get_browser_pool_status: Callable,
    get_browser_runtime_status: Callable,
    get_browser_session_pool_status: Callable,
    browser_control: Any,
) -> None:

    @app.get("/api/task_queue", summary="查看任务队列")
    async def get_task_queue() -> dict:
        return {
            "status": "success",
            "queue": queue_snapshot(),
            "persisted": queue_state.load_public_snapshot(),
        }

    @app.get("/api/task_queue/metrics", summary="查看任务队列指标（Y17）")
    async def get_task_queue_metrics(run_limit: int = 200) -> dict:
        return {
            "status": "success",
            "metrics": queue_metrics(run_limit=run_limit),
        }

    @app.get("/api/browser_pool", summary="查看浏览器资源池状态（Y8-A）")
    async def get_browser_pool_status_endpoint() -> dict:
        pool = get_browser_pool_status()
        backend = browser_control.backend_status()
        return {
            "status": "success",
            "pool": pool,
            "runtime": get_browser_runtime_status(
                pool_status=pool, backend_status=backend
            ),
        }

    @app.get("/api/browser_sessions", summary="查看跨系统 Session Pool 状态（E1）")
    async def get_browser_sessions() -> dict:
        return {
            "status": "success",
            "result": get_browser_session_pool_status(),
        }

    @app.delete("/api/task_queue/{task_id}", summary="取消尚未开始的排队任务")
    async def delete_queued_task_endpoint(task_id: str) -> dict:
        ok, message = cancel_queued_task(task_id)
        await manager.send_log(
            f"[QUEUE] {message}: {task_id}",
            level="warn" if ok else "info",
        )
        return {
            "status": "success" if ok else "error",
            "message": message,
            "queue": queue_snapshot(),
            "persisted": queue_state.load_public_snapshot(),
        }

    @app.post("/api/task_queue/pause", summary="暂停队列领取新任务（Y15）")
    async def pause_queue(reason: str = "") -> dict:
        queue = pause_task_queue(reason)
        await manager.send_log(
            f"[QUEUE] 队列已暂停{': ' + reason if reason else ''}",
            level="warn",
        )
        return {
            "status": "success",
            "queue": queue,
            "persisted": queue_state.load_public_snapshot(),
        }

    @app.post("/api/task_queue/resume", summary="恢复队列领取新任务（Y15）")
    async def resume_queue(background_tasks: BackgroundTasks) -> dict:
        queue, should_start_worker = resume_task_queue()
        started_workers: list[str] = []
        worker_config = queue_worker_config()
        if should_start_worker:
            started_workers, worker_config = start_queue_workers(background_tasks)
            queue = queue_snapshot()
        await manager.send_log("[QUEUE] 队列已恢复", level="info")
        return {
            "status": "success",
            "queue": queue,
            "started_workers": started_workers,
            "worker_config": worker_config,
            "persisted": queue_state.load_public_snapshot(),
        }

    @app.post("/api/task_queue/recover", summary="从持久化快照恢复队列（Y10）")
    async def recover_task_queue() -> dict:
        result = recover_queued_tasks()
        await manager.send_log(
            (
                f"[QUEUE RECOVERY] recovered={result['recovered_count']} "
                f"interrupted={result['interrupted_count']}"
            ),
            level="warn" if result["interrupted_count"] else "info",
        )
        return {
            "status": "success",
            "recovery": result,
            "queue": queue_snapshot(),
            "persisted": queue_state.load_public_snapshot(),
        }

    @app.post("/api/task_queue/watchdog", summary="扫描并标记 stale 队列 worker（Y12）")
    async def run_task_queue_watchdog() -> dict:
        result = scan_stale_queue_workers()
        await manager.send_log(
            (
                f"[QUEUE WATCHDOG] stale_workers={result['stale_worker_count']} "
                f"affected_tasks={result['affected_task_count']}"
            ),
            level="warn" if result["stale_worker_count"] else "info",
        )
        return {
            "status": "success",
            "watchdog": result,
            "queue": queue_snapshot(),
            "persisted": queue_state.load_public_snapshot(),
        }

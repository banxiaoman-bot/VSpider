"""
VSpider FastAPI 后端服务。

提供三类能力：
1. WebSocket `/ws/logs`
   向前端实时推送日志、截图和任务完成状态。
2. POST `/api/start_batch`
   接收目标 URL、Prompt 和可选上传文件，在后台启动任务。
3. POST `/api/stop_batch`
   给当前运行中的任务发送停止信号。

设计要点：
- `broadcast_log()` / `broadcast_image()` / `broadcast_done()` 可被其他模块直接导入调用。
- `_API_LOOP` 在 `lifespan` 中捕获 FastAPI 事件循环，支持跨线程安全广播。
- 单任务执行通过后台线程调度，避免 FastAPI 主循环与 Playwright 子进程冲突。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Coroutine
from urllib.parse import urlparse

import uvicorn
from fastapi import (
    BackgroundTasks,
    Body,
    FastAPI,
    File,
    HTTPException,
    Form,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from visual_web_agent.artifact_manager import artifact_root, artifact_url, register_artifact, resolve_artifact_path
from visual_web_agent import failure_archive as _failure_archive
from visual_web_agent import run_registry as _run_registry
from visual_web_agent import network_intelligence as _network_intelligence
from visual_web_agent import api_replay as _api_replay
from visual_web_agent import queue_state as _queue_state
from visual_web_agent.secret_redaction import redact_event_payload
from visual_web_agent.capability_api import CapabilityApiDeps, capability_crawl_efficiency_plan as _capability_crawl_efficiency_plan, capability_efficiency_correlation_report as _capability_efficiency_correlation_report, create_capability_router
from visual_web_agent.extraction_engine import generic as _extractor_engine
from visual_web_agent.browser_pool import build_browser_runtime_drift as _build_browser_runtime_drift
from visual_web_agent.browser_pool import build_browser_runtime_issue_summary as _build_browser_runtime_issue_summary
from visual_web_agent.browser_pool import build_browser_runtime_preflight as _build_browser_runtime_preflight
from visual_web_agent.browser_pool import get_browser_pool_status as _get_browser_pool_status
from visual_web_agent.browser_pool import get_browser_runtime_status as _get_browser_runtime_status
from visual_web_agent.browser_session_pool import get_browser_session_pool_status as _get_browser_session_pool_status
from visual_web_agent.browser_control_api import BrowserControlApiDeps, create_browser_control_router
from visual_web_agent.browser_control import BrowserControlManager
from visual_web_agent.capability_failure_fixture_api import CapabilityFailureFixtureApiDeps, create_capability_failure_fixture_router
from visual_web_agent.capability_failure_fixture import build_capability_failure_regression_fixture
from visual_web_agent.capability_failure_replay import replay_capability_failure_fixture, replay_capability_failure_fixtures
from visual_web_agent.efficiency_feedback_replay import evaluate_planner_feedback_shadow, efficiency_feedback_replay_source_kind, replay_efficiency_feedback, replay_efficiency_feedback_batch
from visual_web_agent.route_executor import execute_route
from visual_web_agent.robots_policy import RobotsPolicyManager
from visual_web_agent.spider_lite import SpiderLiteManager


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("vspider.api")
logging.getLogger("watchfiles").setLevel(logging.WARNING)
logging.getLogger("watchfiles.main").setLevel(logging.WARNING)

if sys.platform == "win32" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


ARTIFACT_DIR = artifact_root()
_robots_policy = RobotsPolicyManager()
_spider_lite = SpiderLiteManager(robots_policy=_robots_policy)


def _harvest_urls_from_text(text: str) -> list[str]:
    """Best-effort: pull bare http(s) URLs from a free-form prompt.

    Used by ``start_batch`` when the caller omits ``target_url`` -- the
    Mission §一-B contract says "goal is the only mandatory field" and
    URLs should be inferable from the goal whenever possible.
    """
    if not text:
        return []
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s)\]}>\"'，。；、]+", text):
        candidate = match.group(0).rstrip(".,;:!?'\"")
        if candidate and candidate not in urls:
            urls.append(candidate)
    return urls


def _normalize_target_url(raw_url: str) -> tuple[str, str]:
    """Return (url, error). Adds https:// for common bare host input."""
    url = (raw_url or "").strip()
    if not url:
        return "", "目标 URL 不能为空。"
    if "://" not in url:
        host_hint = url.split("/", 1)[0].split(":", 1)[0].lower()
        is_private_hint = (
            host_hint in {"localhost", "127.0.0.1", "0.0.0.0"}
            or host_hint.startswith(("10.", "192.168."))
            or any(host_hint.startswith(f"172.{i}.") for i in range(16, 32))
        )
        url = ("http://" if is_private_hint else "https://") + url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "", (
            "目标 URL 格式不正确，请填写完整网址，例如 "
            "https://quotes.toscrape.com/ 或 https://example.com/path"
        )
    return url, ""


from broadcast import (  # noqa: E402 — extracted Slice 3
    ConnectionManager,
    manager,
    broadcast_log,
    broadcast_image,
    broadcast_done,
    broadcast_new_artifact,
    broadcast_status,
    set_phase_log_run_id,
    broadcast_phase,
    broadcast_human_intervention,
    wait_for_human_resume,
    broadcast_log_async,
    broadcast_image_async,
    set_api_loop,
    get_api_loop,
    resume_human as _broadcast_resume_human,
    submit_hitl_form as _broadcast_submit_hitl_form,
    _HITL_RESUME_EVENT,
)
from queue_core import (  # noqa: E402 — extracted Slice 3
    _TASK_LOCK,
    active_tasks,
    TEMP_UPLOAD_DIR,
    _TASK_COOLDOWN_SECONDS,
    _RETRYABLE_RUN_STATUS,
    _env_int,
    _env_float,
    _queue_heartbeat_config,
    _queue_watchdog_scheduler_config,
    _queue_worker_config,
    _task_snapshot,
    _next_task_id,
    _public_task_item,
    _active_queue_worker_count_locked,
    _refresh_queue_worker_running_locked,
    _public_worker_item,
    _queue_snapshot,
    _age_seconds,
    queue_metrics,
    _persist_queue_snapshot_safe,
    _create_task_control,
    _enqueue_task,
    cancel_queued_task,
    pause_task_queue,
    resume_task_queue,
    request_stop_current_task,
    _retry_vlm_options_from_run,
    _drop_redacted_secret_values,
    _read_json_file_if_present,
    _load_run_contract_bundle,
    retry_run_as_queued_task,
    recover_queued_tasks,
    scan_stale_queue_workers,
)
_browser_control = BrowserControlManager()

def _http_exception_detail(exc: Exception) -> Any:
    action_trace = getattr(exc, "action_trace", None)
    if isinstance(action_trace, dict) and str(action_trace.get("version") or "") == "browser_action_trace.v1":
        detail: dict[str, Any] = {
            "message": str(exc),
            "action_trace": dict(action_trace),
        }
        action_issue_summary = getattr(exc, "action_issue_summary", None) or action_trace.get("issue_summary")
        if isinstance(action_issue_summary, dict):
            detail["action_issue_summary"] = dict(action_issue_summary)
        return detail
    return str(exc)


app_browser_control_router = create_browser_control_router(BrowserControlApiDeps(
    browser_control=lambda: _browser_control,
    http_exception_detail=_http_exception_detail,
))


_AUTH_SESSION_LOCK = asyncio.Lock()
_AUTH_SESSION: dict[str, Any] | None = None


def _auth_dir() -> Path:
    try:
        from visual_web_agent import config

        return Path(getattr(config, "AUTH_DIR", "") or ".auth").resolve()
    except Exception:
        return (Path(__file__).resolve().parent / ".auth").resolve()


def _sanitize_profile_name(value: str) -> str:
    name = (value or "").strip()
    if name.endswith(".json"):
        name = name[:-5]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._-")
    if not name:
        raise ValueError("profile name is required")
    return name


def _default_profile_name(url: str) -> str:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "site").lower()
    for prefix in ("www.", "passport.", "login."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return _sanitize_profile_name(f"{host.split('.')[0] or 'site'}_default")


async def _close_auth_session(session: dict[str, Any]) -> None:
    for key in ("context", "browser"):
        obj = session.get(key)
        if obj:
            try:
                await obj.close()
            except Exception:
                pass
    playwright = session.get("playwright")
    if playwright:
        try:
            await playwright.stop()
        except Exception:
            pass


def _start_queue_workers(background_tasks: BackgroundTasks) -> tuple[list[str], dict[str, Any]]:
    config = _queue_worker_config()
    started: list[str] = []
    with _TASK_LOCK:
        workers = active_tasks.setdefault("workers", {})
        target = int(config.get("effective_max_workers") or 1)
        active_count = _active_queue_worker_count_locked()
        for _idx in range(max(0, target - active_count)):
            worker_id = f"worker_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            workers[worker_id] = {
                "worker_id": worker_id,
                "status": "starting",
                "task_id": "",
                "started_at": time.time(),
                "last_seen_at": time.time(),
                "stopped_at": None,
                "error": "",
            }
            started.append(worker_id)
        _refresh_queue_worker_running_locked()
    for worker_id in started:
        background_tasks.add_task(_queue_worker, worker_id)
    _persist_queue_snapshot_safe()
    return started, config


async def _run_batch_task(
    task_id: str,
    target_url: str,
    prompt: str,
    file_path: str | None,
    stop_event: threading.Event,
    auth_profiles: str = "",
    vlm_options: dict[str, Any] | None = None,
    urls: list[str] | None = None,
    run_constraints: dict[str, Any] | None = None,
    attachment_intent: str = "",
) -> None:
    """
    后台任务执行器。

    真正的批处理运行放在独立线程里，通过 `asyncio.to_thread(...)`
    调度同步包装器，避免 FastAPI 主循环与 Playwright 冲突。
    """
    from smart_batch_runner import run_smart_batch_sync

    try:
        logger.info(
            "[TASK] Dispatching batch runner in isolated worker thread "
            "(avoids FastAPI event-loop / Playwright subprocess conflicts)."
        )
        task_ok = await asyncio.to_thread(
            run_smart_batch_sync,
            target_url,
            prompt,
            file_path or "",
            stop_event,
            auth_profiles,
            vlm_options or {},
            run_id=task_id,
            urls=list(urls or []),
            run_constraints=run_constraints or None,
            attachment_intent=attachment_intent,
        )

        with _TASK_LOCK:
            task = active_tasks.get("current_task")
            if task and task.get("task_id") == task_id:
                task["status"] = (
                    "stopped"
                    if stop_event.is_set()
                    else ("completed" if task_ok else "failed")
                )
                task["running"] = False
        try:
            _run_registry.complete_run(
                task_id,
                success=bool(task_ok),
                stopped=stop_event.is_set(),
            )
        except Exception as exc:
            logger.debug("[RUN REGISTRY] complete_run failed for %s: %s", task_id, exc)

        if stop_event.is_set():
            broadcast_log("[TASK] 任务已被强制终止", level="warn")
            broadcast_done(False, "任务已强制停止")

    except Exception as exc:
        logger.exception("[TASK] Background task crashed")
        with _TASK_LOCK:
            task = active_tasks.get("current_task")
            if task and task.get("task_id") == task_id:
                task["status"] = "error"
                task["running"] = False
                task["error"] = f"{type(exc).__name__}: {exc}"

        err_text = f"{type(exc).__name__}: {exc}"
        try:
            _run_registry.update_run(task_id, status="error", error=err_text)
        except Exception as reg_exc:
            logger.debug("[RUN REGISTRY] update error failed for %s: %s", task_id, reg_exc)
        broadcast_log(f"[TASK] 后台任务异常: {err_text}", level="error")
        broadcast_done(False, f"后台任务异常: {err_text}")
    finally:
        with _TASK_LOCK:
            task = active_tasks.get("current_task")
            if task and task.get("task_id") == task_id:
                task["finished_at"] = time.time()
        _persist_queue_snapshot_safe()


async def _queue_worker_heartbeat(worker_id: str, stop_signal: asyncio.Event) -> None:
    interval = float(_queue_heartbeat_config().get("interval_s") or 5.0)
    while not stop_signal.is_set():
        with _TASK_LOCK:
            worker = active_tasks.setdefault("workers", {}).get(worker_id)
            if worker:
                worker["last_seen_at"] = time.time()
        _persist_queue_snapshot_safe()
        try:
            await asyncio.wait_for(stop_signal.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def _queue_worker(worker_id: str = "worker_default") -> None:
    try:
        while True:
            task_item: dict[str, Any] | None = None
            should_stop = False
            with _TASK_LOCK:
                worker = active_tasks.setdefault("workers", {}).setdefault(
                    worker_id,
                    {
                        "worker_id": worker_id,
                        "status": "starting",
                        "task_id": "",
                        "started_at": time.time(),
                        "last_seen_at": time.time(),
                        "stopped_at": None,
                        "error": "",
                    },
                )
                worker["status"] = "idle"
                worker["task_id"] = ""
                worker["last_seen_at"] = time.time()
                current = active_tasks.get("current_task")
                if current and current.get("running"):
                    task_item = None
                elif active_tasks.get("queue_paused"):
                    worker["status"] = "paused"
                    worker["last_seen_at"] = time.time()
                    _refresh_queue_worker_running_locked()
                    task_item = None
                else:
                    queue = active_tasks.get("queue") or []
                    if queue:
                        task_item = queue.pop(0)
                        worker["status"] = "running"
                        worker["task_id"] = str(task_item.get("task_id") or "")
                        worker["last_seen_at"] = time.time()
                    else:
                        worker["status"] = "stopped"
                        worker["stopped_at"] = time.time()
                        _refresh_queue_worker_running_locked()
                        should_stop = True
            if should_stop:
                _persist_queue_snapshot_safe()
                return
            if task_item is None:
                await asyncio.sleep(0.25)
                continue
            _persist_queue_snapshot_safe()

            task_id, stop_event = _create_task_control(
                str(task_item.get("target_url") or ""),
                str(task_item.get("prompt") or ""),
                str(task_item.get("file_path") or ""),
                str(task_item.get("auth_profiles") or ""),
                str(task_item.get("vlm_model") or ""),
                str(task_item.get("semantic_model") or ""),
                bool(task_item.get("vlm_text_only")),
                mode=str(task_item.get("mode") or "single"),
                filename=str(task_item.get("filename") or ""),
                file_size_kb=float(task_item.get("file_size_kb") or 0.0),
                vlm_model_type=str(task_item.get("vlm_model_type") or "vl"),
                vlm_options=task_item.get("vlm_options") or {},
                task_id=str(task_item.get("task_id") or ""),
            )
            with _TASK_LOCK:
                current_public = _public_task_item(active_tasks["current_task"])
            await manager.send_status("task_started", task=current_public)
            await manager.send_log(
                f"[QUEUE] 开始执行任务 {task_id}，剩余队列 {_queue_snapshot().get('queue_length', 0)}",
                level="info",
            )
            heartbeat_stop = asyncio.Event()
            heartbeat_task = asyncio.create_task(_queue_worker_heartbeat(worker_id, heartbeat_stop))
            try:
                _task_urls_raw = task_item.get("urls") or []
                _task_urls = [str(u) for u in _task_urls_raw if str(u or "")]
                _run_batch_kwargs: dict[str, Any] = {}
                if _task_urls:
                    _run_batch_kwargs["urls"] = _task_urls
                if task_item.get("constraints"):
                    _run_batch_kwargs["run_constraints"] = task_item.get("constraints")
                if task_item.get("attachment_intent"):
                    _run_batch_kwargs["attachment_intent"] = str(task_item.get("attachment_intent") or "")
                await _run_batch_task(
                    task_id,
                    str(task_item.get("target_url") or ""),
                    str(task_item.get("prompt") or ""),
                    str(task_item.get("file_path") or "") or None,
                    stop_event,
                    str(task_item.get("auth_profiles") or ""),
                    task_item.get("vlm_options") or {},
                    **_run_batch_kwargs,
                )
            finally:
                heartbeat_stop.set()
                try:
                    await heartbeat_task
                except Exception as hb_exc:
                    logger.debug("[QUEUE] heartbeat stop failed: %s", hb_exc)
            _persist_queue_snapshot_safe()
    except Exception as exc:
        logger.exception("[QUEUE] Worker crashed: %s", exc)
        with _TASK_LOCK:
            worker = active_tasks.setdefault("workers", {}).setdefault(
                worker_id,
                {"worker_id": worker_id},
            )
            worker["status"] = "error"
            worker["error"] = f"{type(exc).__name__}: {exc}"
            worker["stopped_at"] = time.time()
            _refresh_queue_worker_running_locked()
        _persist_queue_snapshot_safe()


async def _queue_watchdog_scheduler(stop_signal: asyncio.Event) -> None:
    interval = float(_queue_watchdog_scheduler_config().get("interval_s") or 15.0)
    while not stop_signal.is_set():
        try:
            result = scan_stale_queue_workers()
            if result.get("stale_worker_count"):
                logger.warning(
                    "[QUEUE WATCHDOG] stale_workers=%s affected_tasks=%s",
                    result.get("stale_worker_count"),
                    result.get("affected_task_count"),
                )
        except Exception as exc:
            logger.debug("[QUEUE WATCHDOG] scheduler scan failed: %s", exc)
        try:
            await asyncio.wait_for(stop_signal.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    """捕获 FastAPI 事件循环，供跨线程广播使用。"""
    # Agent 内「连续失败按回车继续」在无 TTY 时会 EOF 并拖垮进程；API 模式默认非交互。
    os.environ.setdefault("VSPIDER_NON_INTERACTIVE", "1")
    set_api_loop(asyncio.get_running_loop())
    watchdog_stop = asyncio.Event()
    watchdog_task: asyncio.Task | None = None
    watchdog_config = _queue_watchdog_scheduler_config()
    if watchdog_config.get("enabled"):
        watchdog_task = asyncio.create_task(_queue_watchdog_scheduler(watchdog_stop))
        logger.info(
            "Queue watchdog scheduler enabled; interval=%ss",
            watchdog_config.get("interval_s"),
        )
    logger.info("VSpider API server started; event loop captured for broadcasts.")
    try:
        yield
    finally:
        watchdog_stop.set()
        if watchdog_task is not None:
            try:
                await watchdog_task
            except Exception as exc:
                logger.debug("[QUEUE WATCHDOG] scheduler stop failed: %s", exc)
        set_api_loop(None)
        logger.info("VSpider API 服务关闭")


app = FastAPI(
    title="VSpider API",
    description="VSpider backend service API",
    version="0.2.0",
    lifespan=lifespan,
)

_DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
)


def _build_cors_kwargs(origins_raw: str) -> dict[str, Any]:
    # Resolve CORS allow_origins/credentials from VSPIDER_CORS_ORIGINS. Default =
    # localhost dev (5173) + same-origin API (8000). A comma list is an explicit
    # allowlist. Literal "*" re-enables wildcard but forces allow_credentials=False,
    # since browsers reject "*" together with credentials.
    raw = (origins_raw or "").strip()
    if raw == "*":
        return {"allow_origins": ["*"], "allow_credentials": False}
    if raw:
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        return {"allow_origins": origins, "allow_credentials": True}
    return {"allow_origins": list(_DEFAULT_CORS_ORIGINS), "allow_credentials": True}


app.add_middleware(
    CORSMiddleware,
    allow_methods=["*"],
    allow_headers=["*"],
    **_build_cors_kwargs(os.getenv("VSPIDER_CORS_ORIGINS", "")),
)

_DEFAULT_TRUSTED_HOSTS = ("localhost", "127.0.0.1", "::1", "testserver")


def _build_trusted_hosts(hosts_raw: str) -> list[str]:
    # Host-header allowlist from VSPIDER_ALLOWED_HOSTS blocks DNS rebinding:
    # once attacker.com resolves to 127.0.0.1 the request becomes same-origin
    # and CORS no longer applies, so the Host header is the last gate. Default
    # covers local dev + TestClient; literal "*" disables the check for
    # LAN / reverse-proxy deployments.
    raw = (hosts_raw or "").strip()
    if raw == "*":
        return ["*"]
    if raw:
        return [h.strip() for h in raw.split(",") if h.strip()]
    return list(_DEFAULT_TRUSTED_HOSTS)


# Added after CORSMiddleware so it runs before CORS (middleware is LIFO).
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=_build_trusted_hosts(os.getenv("VSPIDER_ALLOWED_HOSTS", "")),
)


@app.get("/download/runs/{run_id}/artifacts/{filename:path}", summary="下载 run 作用域产物")
async def download_run_artifact(run_id: str, filename: str):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    from visual_web_agent.io_contract.persistence import ARTIFACTS_DIRNAME, run_dir

    safe_name = str(filename or "").replace("\\", "/").lstrip("/")
    if not safe_name or ".." in safe_name.split("/"):
        raise HTTPException(status_code=400, detail="invalid artifact path")
    path = run_dir(run_id) / ARTIFACTS_DIRNAME / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(path)


app.mount("/download", StaticFiles(directory=str(ARTIFACT_DIR)), name="artifacts")
app.include_router(app_browser_control_router)
app.include_router(create_capability_router(CapabilityApiDeps(
    get_browser_pool_status=_get_browser_pool_status,
    get_browser_runtime_status=_get_browser_runtime_status,
    browser_backend_status=lambda: _browser_control.backend_status(),
)))


def _ws_origin_allowed(origin: str) -> bool:
    # WebSockets bypass CORS, so guard /ws/logs with the same allowlist as the
    # HTTP CORS layer (VSPIDER_CORS_ORIGINS). A missing Origin header indicates a
    # non-browser client (tests / native ws), which is allowed.
    allow_origins = _build_cors_kwargs(os.getenv("VSPIDER_CORS_ORIGINS", ""))["allow_origins"]
    if "*" in allow_origins:
        return True
    if not origin:
        return True
    return origin in allow_origins


@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket) -> None:
    """
    前端通过此 WebSocket 接收实时日志、截图和 done 事件。
    客户端发送的任何消息都忽略，仅用于保活。
    """
    origin = websocket.headers.get("origin", "")
    if not _ws_origin_allowed(origin):
        logger.warning("[WS] reject cross-origin /ws/logs: origin=%r", origin)
        await websocket.close(code=1008)
        return
    await manager.connect(websocket)
    try:
        await manager.send_log("✅ VSpider 已连接，等待任务下发...", level="info")
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.get("/api/auth/profiles", summary="列出 Auth Matrix profiles")
async def list_auth_profiles() -> dict:
    auth_root = _auth_dir()
    auth_root.mkdir(parents=True, exist_ok=True)
    profiles: list[dict[str, Any]] = []

    for path in sorted(auth_root.glob("*.json")):
        if not path.is_file():
            continue
        item: dict[str, Any] = {
            "name": path.stem,
            "filename": path.name,
            "size": path.stat().st_size,
            "modified_at": path.stat().st_mtime,
            "cookies": 0,
            "origins": 0,
            "cf_clearance": False,
            "cf_clearance_expires_in_hours": None,
        }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            item["cookies"] = len(data.get("cookies") or [])
            item["origins"] = len(data.get("origins") or [])
            try:
                from visual_web_agent.auth_harvester import inspect_cf_clearance

                host_guess = path.stem.replace("_", ".")
                has_cf, cf_hours = inspect_cf_clearance(data, host_guess)
                item["cf_clearance"] = has_cf
                item["cf_clearance_expires_in_hours"] = cf_hours
            except Exception:
                pass
        except Exception as exc:
            item["warning"] = f"{type(exc).__name__}: {exc}"
        profiles.append(item)

    return {"status": "success", "auth_dir": str(auth_root), "profiles": profiles}


@app.get("/api/runtime/captcha_solver", summary="Captcha Solver 配置状态")
async def captcha_solver_status() -> dict:
    import os

    provider = (os.getenv("VSPIDER_CAPTCHA_SOLVER") or "capsolver").strip().lower()
    api_key = (os.getenv("VSPIDER_CAPTCHA_API_KEY") or "").strip()
    enabled = bool(api_key) and provider in {"capsolver", "2captcha"}
    return {
        "status": "success",
        "enabled": enabled,
        "provider": provider,
        "configured": bool(api_key),
    }


@app.get("/api/output_contract/preview", summary="推断任务输出契约预览")
async def output_contract_preview(goal: str = "") -> dict:
    text = (goal or "").strip()
    if not text:
        return {"status": "error", "message": "goal is required"}
    try:
        from visual_web_agent.io_contract import infer_output_contract

        oc = infer_output_contract(text)
        return {"status": "success", "output_contract": oc.to_dict()}
    except Exception as exc:
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}


@app.post("/api/auth/manual/start", summary="打开人工登录浏览器窗口")
async def start_manual_auth(
    target_url: str = Form(..., description="需要人工登录的网站 URL"),
    profile: str = Form("", description="保存到 .auth/<profile>.json"),
) -> dict:
    global _AUTH_SESSION

    url = target_url.strip()
    if not url:
        return {"status": "error", "message": "target_url is required"}

    try:
        profile_name = _sanitize_profile_name(profile or _default_profile_name(url))
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    async with _AUTH_SESSION_LOCK:
        if _AUTH_SESSION is not None:
            return {
                "status": "error",
                "message": (
                    "已有人工登录窗口正在进行；请先保存或取消当前登录态录制。"
                ),
            }

        try:
            from playwright.async_api import async_playwright

            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=False)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                ignore_https_errors=True,
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            _AUTH_SESSION = {
                "playwright": playwright,
                "browser": browser,
                "context": context,
                "page": page,
                "profile": profile_name,
                "target_url": url,
                "started_at": time.time(),
            }
        except Exception as exc:
            if "context" in locals():
                await _close_auth_session({
                    "context": locals().get("context"),
                    "browser": locals().get("browser"),
                    "playwright": locals().get("playwright"),
                })
            logger.exception("[AUTH UI] Failed to start manual auth")
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    await manager.send_log(
        f"[AUTH] 已打开人工登录窗口，登录完成后点击保存：{profile_name}",
        level="info",
    )
    return {
        "status": "success",
        "message": "人工登录窗口已打开。登录完成后点击保存登录态。",
        "profile": profile_name,
    }


@app.post("/api/auth/manual/save", summary="保存人工登录状态")
async def save_manual_auth() -> dict:
    global _AUTH_SESSION

    async with _AUTH_SESSION_LOCK:
        if _AUTH_SESSION is None:
            return {"status": "error", "message": "没有正在进行的人工登录会话"}

        session = _AUTH_SESSION
        _AUTH_SESSION = None

    profile_name = session["profile"]
    output_path = _auth_dir() / f"{profile_name}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        state = await session["context"].storage_state()
        output_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        cookie_count = len(state.get("cookies") or [])
        origin_count = len(state.get("origins") or [])
        message = (
            f"已保存 auth profile {profile_name} "
            f"(cookies={cookie_count}, origins={origin_count})"
        )
        await manager.send_log(f"[AUTH] {message}", level="info")
        return {
            "status": "success",
            "message": message,
            "profile": profile_name,
            "path": str(output_path),
            "cookies": cookie_count,
            "origins": origin_count,
        }
    except Exception as exc:
        logger.exception("[AUTH UI] Failed to save manual auth")
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    finally:
        await _close_auth_session(session)


@app.post("/api/auth/manual/cancel", summary="取消人工登录状态录制")
async def cancel_manual_auth() -> dict:
    global _AUTH_SESSION

    async with _AUTH_SESSION_LOCK:
        if _AUTH_SESSION is None:
            return {"status": "success", "message": "没有正在进行的人工登录会话"}
        session = _AUTH_SESSION
        _AUTH_SESSION = None

    await _close_auth_session(session)
    await manager.send_log("[AUTH] 已取消人工登录态录制", level="warn")
    return {"status": "success", "message": "已取消人工登录态录制"}


@app.post("/api/human/resume", summary="人工处理完成后恢复 Agent")
async def resume_human_intervention() -> dict:
    _broadcast_resume_human()
    await manager.send_status("human_resumed")
    await manager.send_log("[HITL] 操作员确认完成，Agent 恢复执行", level="info")
    return {"status": "success", "message": "Agent resume signal sent"}


@app.post("/api/human/form_submit", summary="前端 HITL 表单提交")
async def submit_hitl_form_endpoint(payload: dict) -> dict:
    fields = payload.get("fields", {})
    if not fields:
        return {"status": "error", "message": "No fields provided"}
    _broadcast_submit_hitl_form(fields)
    await manager.send_status("human_resumed")
    filled = ", ".join(f"{k}=***" for k in fields)
    await manager.send_log(f"[HITL] 用户通过前端表单提交了 {len(fields)} 个字段: {filled}", level="info")
    return {"status": "success", "message": f"Form submitted with {len(fields)} fields"}


@app.get("/api/artifacts", summary="列出 VSpider 产出文件")
async def get_artifacts_list() -> dict:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for path in sorted(ARTIFACT_DIR.rglob("*")):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
            stat = resolved.stat()
            rel = resolved.relative_to(ARTIFACT_DIR.resolve()).as_posix()
            files.append({
                "name": rel,
                "filename": resolved.name,
                "size_kb": round(stat.st_size / 1024, 2),
                "created_at": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(stat.st_ctime),
                ),
                "modified_at": stat.st_mtime,
                "url": artifact_url(resolved),
            })
        except Exception as exc:
            logger.debug("[ARTIFACT] Skip %s: %s", path, exc)
    files.sort(key=lambda item: item["modified_at"], reverse=True)
    return {"status": "success", "root": str(ARTIFACT_DIR), "files": files}




# Capability artifact persistence helpers (extracted to own module)
from capability_artifact_persistence import (
    _write_capability_execute_artifact,
    _capability_payload_text,
    _register_capability_report_artifact,
    _register_capability_execute_trace_artifact,
    _capability_failure_fixture_source,
    _capability_failure_fixture_replay_source,
    _efficiency_feedback_replay_source,
    _write_efficiency_feedback_replay_artifact,
    _write_efficiency_feedback_replay_batch_artifact,
    _efficiency_feedback_replay_artifact_dir,
    _read_efficiency_feedback_replay_artifact,
    _efficiency_feedback_replay_summary,
    _list_efficiency_feedback_replay_artifacts,
    _list_efficiency_feedback_replay_sources,
    _capability_failure_fixture_artifact_dir,
    _read_capability_failure_fixture_artifact,
    _capability_failure_fixture_summary,
    _list_capability_failure_fixture_artifacts,
    _write_capability_failure_fixture_artifact,
    _write_capability_failure_fixture_replay_artifact,
    _write_capability_failure_fixture_replay_batch_artifact,
    _capability_failure_fixture_replay_batch_artifact_dir,
    _read_capability_failure_fixture_replay_batch_artifact,
    _capability_failure_fixture_replay_batch_summary,
    _list_capability_failure_fixture_replay_batch_artifacts,
    _capability_failure_fixture_replay_batch_pass_rate,
    _capability_failure_fixture_replay_batch_history_trend,
)




app.include_router(create_capability_failure_fixture_router(CapabilityFailureFixtureApiDeps(
    build_fixture=build_capability_failure_regression_fixture,
    replay_fixture=replay_capability_failure_fixture,
    replay_fixtures=replay_capability_failure_fixtures,
    fixture_source=_capability_failure_fixture_source,
    replay_source=_capability_failure_fixture_replay_source,
    list_fixture_artifacts=_list_capability_failure_fixture_artifacts,
    write_fixture_artifact=_write_capability_failure_fixture_artifact,
    write_replay_artifact=_write_capability_failure_fixture_replay_artifact,
    write_batch_artifact=_write_capability_failure_fixture_replay_batch_artifact,
    list_batch_artifacts=_list_capability_failure_fixture_replay_batch_artifacts,
    batch_history_trend=_capability_failure_fixture_replay_batch_history_trend,
)))


@app.post("/api/capabilities/efficiency_feedback/replay")
async def replay_efficiency_feedback_route(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
    source = _efficiency_feedback_replay_source(payload)
    if not efficiency_feedback_replay_source_kind(source):
        raise HTTPException(status_code=400, detail="source does not contain efficiency_correlation_report.v1 or efficiency planner_feedback.v1")
    report = replay_efficiency_feedback(source)
    artifact = None
    if bool(payload.get("save") or payload.get("write_artifact")):
        artifact = _write_efficiency_feedback_replay_artifact(report, payload)
    return {"status": "success", "result": {"report": report, "artifact": artifact}}


@app.post("/api/capabilities/efficiency_feedback/shadow")
async def shadow_efficiency_feedback_route(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
    source = _efficiency_feedback_replay_source(payload)
    if not efficiency_feedback_replay_source_kind(source):
        raise HTTPException(status_code=400, detail="source does not contain efficiency_correlation_report.v1 or efficiency planner_feedback.v1")
    report = evaluate_planner_feedback_shadow(source)
    return {"status": "success", "result": {"report": report}}


@app.get("/api/capabilities/efficiency_feedback/replays")
async def list_efficiency_feedback_replays_route(limit: int = 50) -> dict:
    reports = _list_efficiency_feedback_replay_artifacts(limit=limit)
    return {"status": "success", "result": {"report_count": len(reports), "reports": reports}}


@app.post("/api/capabilities/efficiency_feedback/replay_batch")
async def replay_efficiency_feedback_batch_route(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
    direct_sources = payload.get("sources")
    if isinstance(direct_sources, list):
        sources = [
            _efficiency_feedback_replay_source(dict(item))
            for item in direct_sources
            if isinstance(item, dict)
        ]
    else:
        sources = _list_efficiency_feedback_replay_sources(limit=int(payload.get("limit") or 100))
    report = replay_efficiency_feedback_batch(sources)
    for item, source in zip(report.get("items") or [], sources):
        if isinstance(item, dict) and isinstance(source, dict) and isinstance(source.get("artifact"), dict):
            item["artifact"] = dict(source.get("artifact") or {})
    artifact = None
    if bool(payload.get("save") or payload.get("write_artifact")):
        artifact = _write_efficiency_feedback_replay_batch_artifact(report, payload)
    return {"status": "success", "result": {"report": report, "artifact": artifact}}



# Capability execute runtime helpers (extracted to own module)
from capability_execute_helpers import (
    _capability_execute_runtime_context,
    _capability_execute_runtime_summary,
    _capability_execute_action_trace,
    _capability_execute_exception_action_trace,
    _capability_execute_failed_action_result,
    _capability_execute_failure_bundle,
    _capability_execute_phase_event,
    _capability_execute_phase_event_extra,
    _capability_execute_failure_detail,
)



@app.post("/api/capabilities/execute", summary="执行低风险确定性能力路由（Y31）")
async def execute_capability_route(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
    t0 = time.time()
    runtime_before = _capability_execute_runtime_context(_browser_control.backend_status())
    try:
        result = execute_route(payload, spider_lite=_spider_lite)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("[CAPABILITY EXECUTOR] failed: %s", exc)
        action_trace = _capability_execute_exception_action_trace(exc)
        if action_trace:
            runtime_after = _capability_execute_runtime_context(_browser_control.backend_status())
            result = _capability_execute_failed_action_result(exc, action_trace, runtime_before, runtime_after)
            result["crawl_efficiency_plan"] = _capability_crawl_efficiency_plan(
                payload,
                result.get("route") if isinstance(result.get("route"), dict) else {},
                runtime_status=runtime_after.get("runtime_snapshot"),
            )
            failure_bundle = _capability_execute_failure_bundle(result)
            if failure_bundle:
                result["failure_bundle"] = failure_bundle
            result["efficiency_correlation_report"] = _capability_efficiency_correlation_report(result)
            if bool(payload.get("trace_artifact") or payload.get("save_trace_artifact")):
                try:
                    result["trace_artifact"] = _write_capability_execute_artifact(payload, result)
                except Exception as artifact_exc:
                    logger.debug("[CAPABILITY EXECUTOR] trace artifact skipped: %s", artifact_exc)
                else:
                    result["failure_bundle"] = _capability_execute_failure_bundle(result)
            result["efficiency_correlation_report"] = _capability_efficiency_correlation_report(result)
            phase_event = _capability_execute_phase_event(
                result,
                severity="error",
                message=str(exc) or "capability route execution failed",
                completed=False,
            )
            try:
                broadcast_phase(
                    "capability_execute",
                    severity=str(phase_event.get("severity") or "error"),
                    message=str(phase_event.get("message") or ""),
                    duration_ms=int((time.time() - t0) * 1000),
                    extra=_capability_execute_phase_event_extra(phase_event),
                )
            except Exception as telemetry_exc:
                logger.debug("[CAPABILITY EXECUTOR] telemetry skipped: %s", telemetry_exc)
            raise HTTPException(status_code=500, detail=_capability_execute_failure_detail(result, phase_event=phase_event)) from exc
        raise HTTPException(status_code=500, detail="capability route execution failed") from exc
    runtime_after = _capability_execute_runtime_context(_browser_control.backend_status())
    result["runtime_context"] = {
        "version": "capability_execute_runtime_context.v1",
        "before": runtime_before,
        "after": runtime_after,
    }
    result["runtime_summary"] = {
        "before": _capability_execute_runtime_summary(runtime_before),
        "after": _capability_execute_runtime_summary(runtime_after),
    }
    result["runtime_drift"] = _build_browser_runtime_drift(
        runtime_before.get("runtime_snapshot"),
        runtime_after.get("runtime_snapshot"),
    )
    result["runtime_issue_summary"] = _build_browser_runtime_issue_summary(
        before_preflight=runtime_before.get("runtime_preflight"),
        after_preflight=runtime_after.get("runtime_preflight"),
        drift=result.get("runtime_drift"),
    )
    result["crawl_efficiency_plan"] = _capability_crawl_efficiency_plan(
        payload,
        result.get("route") if isinstance(result.get("route"), dict) else {},
        runtime_status=runtime_after.get("runtime_snapshot"),
    )
    action_trace = _capability_execute_action_trace(result)
    if action_trace:
        result["action_trace"] = action_trace
        if isinstance(action_trace.get("issue_summary"), dict):
            result["action_issue_summary"] = dict(action_trace.get("issue_summary") or {})
    failure_bundle = _capability_execute_failure_bundle(result)
    if failure_bundle:
        result["failure_bundle"] = failure_bundle
    result["efficiency_correlation_report"] = _capability_efficiency_correlation_report(result)
    if bool(payload.get("trace_artifact") or payload.get("save_trace_artifact")):
        try:
            result["trace_artifact"] = _write_capability_execute_artifact(payload, result)
        except Exception as exc:
            logger.debug("[CAPABILITY EXECUTOR] trace artifact skipped: %s", exc)
        else:
            result["failure_bundle"] = _capability_execute_failure_bundle(result)
    result["efficiency_correlation_report"] = _capability_efficiency_correlation_report(result)
    try:
        completed = bool(result.get("completed"))
        status = str(result.get("status") or "unknown")
        capability = str(result.get("capability") or "")
        fallback_reason = str(result.get("fallback_reason") or "")
        broadcast_phase(
            "capability_execute",
            severity="info" if completed else "warn",
            message=capability or fallback_reason or status,
            duration_ms=int((time.time() - t0) * 1000),
            extra={
                "execution_status": status,
                "completed": completed,
                "capability": capability,
                "attempts": result.get("attempts") or [],
                "verification": result.get("verification"),
                "fallback_reason": fallback_reason,
                "artifact": result.get("artifact"),
                "trace_artifact": result.get("trace_artifact"),
                "runtime_summary": result.get("runtime_summary"),
                "runtime_drift": result.get("runtime_drift"),
                "runtime_issue_summary": result.get("runtime_issue_summary"),
                "action_trace": result.get("action_trace"),
                "action_issue_summary": result.get("action_issue_summary"),
                "failure_bundle": result.get("failure_bundle"),
                "crawl_efficiency_plan": result.get("crawl_efficiency_plan"),
                "efficiency_correlation_report": result.get("efficiency_correlation_report"),
                "route_intent": (result.get("route") or {}).get("intent"),
            },
        )
    except Exception as exc:
        logger.debug("[CAPABILITY EXECUTOR] telemetry skipped: %s", exc)
    return {"status": "success", "result": result}


@app.post("/api/extractor/run", summary="运行通用结构化抽取引擎（Y6）")
async def run_extractor(payload: dict[str, Any] = Body(...)) -> dict:
    source = payload.get("source")
    if source in (None, ""):
        raise HTTPException(status_code=400, detail="source is required")
    try:
        result = _extractor_engine.extract(
            source,
            source_type=str(payload.get("source_type") or "auto"),
            requested_fields=payload.get("requested_fields") or None,
            max_rows=int(payload.get("max_rows") or 1000),
            all_tables=bool(payload.get("all_tables")),
        )
        artifact = None
        if bool(payload.get("export")):
            artifact = _extractor_engine.export_jsonl(
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
        result = _extractor_engine.select(
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


@app.post("/api/robots/set", summary="设置 robots.txt 规则缓存（Y23）")
async def set_robots_policy(payload: dict[str, Any] = Body(...)) -> dict:
    try:
        result = _robots_policy.set_robots(
            str(payload.get("domain") or payload.get("url") or ""),
            str(payload.get("robots_txt") or payload.get("text") or ""),
            user_agent=str(payload.get("user_agent") or "*"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "result": result}


@app.post("/api/robots/check", summary="检查 URL 的 robots/throttle 状态（Y23）")
async def check_robots_policy(payload: dict[str, Any] = Body(...)) -> dict:
    try:
        result = _robots_policy.check_url(
            str(payload.get("url") or ""),
            obey=bool(payload.get("obey", True)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "result": result}


@app.post("/api/robots/reserve", summary="预约 URL 抓取并更新域名节流（Y23）")
async def reserve_robots_policy(payload: dict[str, Any] = Body(...)) -> dict:
    try:
        result = _robots_policy.reserve_url(
            str(payload.get("url") or ""),
            obey=bool(payload.get("obey", True)),
            default_delay=float(payload.get("default_delay") or 0.0),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "result": result}


@app.get("/api/robots/{domain}", summary="读取域名 robots/throttle 规则（Y23）")
async def get_robots_policy(domain: str) -> dict:
    return {"status": "success", "result": _robots_policy.public_rules(domain)}


@app.post("/api/spider/run", summary="运行轻量 Spider 爬取（Y24）")
async def run_spider_lite(payload: dict[str, Any] = Body(...)) -> dict:
    try:
        run_payload = dict(payload or {})
        run_payload.setdefault("persist_run_contracts", True)
        run_payload.setdefault("source", "api")
        result = _spider_lite.run(run_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("[SPIDER] run failed: %s", exc)
        raise HTTPException(status_code=500, detail="spider run failed") from exc
    return {"status": "success", "result": result}


@app.get("/api/spider/runs", summary="列出轻量 Spider 运行记录（Y24）")
async def list_spider_lite_runs() -> dict:
    return {"status": "success", "runs": _spider_lite.list_runs()}


@app.get("/api/spider/page_cache/{session_id}", summary="读取 Spider 页面响应缓存状态（Y25）")
async def get_spider_page_cache(session_id: str) -> dict:
    return {"status": "success", "result": _spider_lite.cache_state(session_id)}


@app.get("/api/spider/page_cache/{session_id}/entries", summary="列出 Spider 页面响应缓存条目（Y25）")
async def list_spider_page_cache_entries(session_id: str) -> dict:
    return {"status": "success", "entries": _spider_lite.cache_entries(session_id)}


@app.post("/api/spider/{run_id}/export", summary="导出轻量 Spider items feed（Y27）")
async def export_spider_lite_feed(run_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
    try:
        artifact = _spider_lite.export_feed(
            run_id,
            format=str(payload.get("format") or payload.get("export_format") or "jsonl"),
            filename=str(payload.get("filename") or payload.get("export_filename") or ""),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("[SPIDER] export failed: %s", exc)
        raise HTTPException(status_code=500, detail="spider export failed") from exc
    return {"status": "success", "artifact": artifact}


@app.get("/api/spider/{run_id}/items", summary="查询轻量 Spider items 数据（Y28）")
async def get_spider_lite_items(run_id: str, fields: str = "", offset: int = 0, limit: int = 1000) -> dict:
    try:
        result = _spider_lite.items(
            run_id,
            fields=[part.strip() for part in str(fields or "").split(",") if part.strip()],
            offset=int(offset or 0),
            limit=int(limit or 1000),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "result": result}


@app.get("/api/spider/{run_id}", summary="读取轻量 Spider 运行详情（Y24）")
async def get_spider_lite_run(run_id: str) -> dict:
    result = _spider_lite.get_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="spider run not found")
    return {"status": "success", "result": result}


@app.get("/api/failed_runs", summary="列出最近的失败 run（K2）")
async def get_failed_runs(limit: int = 50) -> dict:
    """返回最新的 ``limit`` 条失败 run 记录。

    每条记录是 ``failure_archive.list_failed_runs`` 输出的字典，
    包含 schema_version/run_id/ts/reason/goal/duration_s 等字段，
    以及一个 ``paths_exist`` 子字典指明 HTML 日志 / phase jsonl /
    event jsonl 是否仍存在，前端据此决定按钮是否可用。
    """
    try:
        # ``base_dir`` / ``project_root`` 默认即可：模块自己解析项目根。
        records = _failure_archive.list_failed_runs(limit=int(limit or 50))
    except Exception as exc:
        logger.warning("[FAILED RUNS] list error: %s", exc)
        records = []
    return {"status": "success", "count": len(records), "items": records}


@app.get("/api/failed_runs/{run_id}/log", summary="下载失败 run 的 HTML 轨迹日志（K2）")
async def get_failed_run_html_log(run_id: str):
    """返回 ``logs/run_log_<run_id>.html``。run_id 必须仅含 ASCII
    字母/数字/下划线，避免 path traversal；不存在则 404。
    """
    rid = (run_id or '').strip()
    # 仅允许 _0-9A-Za-z 的运行 ID（HtmlLogger 用 strftime 生成，
    # 形如 20260524_191800，所以这条白名单足够保守）。
    if not rid or not all(c.isalnum() or c == '_' for c in rid):
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
    """K6: read ``logs/phase_<run_id>.jsonl`` and return parsed events.

    Used by the failed-runs detail dialog so users can re-inspect the
    Timeline of a historic run even after the in-memory ``phaseEvents``
    buffer has been cleared by a newer task.

    Response shape::

        {
            "status":    "success",
            "count":     N,        # events RETURNED (after limit)
            "total":     M,        # events SEEN on disk (>= count)
            "truncated": bool,     # True when total > limit
            "events":    [<event>, ...]   # parsed JSON dicts
        }

    Behavior:
        * Invalid run_id (anything outside [0-9A-Za-z_]) -> 400
        * phase_<id>.jsonl missing                       -> 404
        * limit <= 0 is treated as the default (200)
        * Malformed lines are skipped silently but counted in `total`,
          so a mismatch between `total` and `len(events)` flags corruption.
        * When truncated, we keep the LAST N events (the failure tail
          is more diagnostically useful than the head).
    """
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
                    # Malformed line: skipped from `events` but still
                    # contributes to `total` so the caller can detect drift.
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
    except Exception as exc:
        logger.warning(
            "[FAILED RUNS] phase log read error %s: %s", phase_path, exc,
        )
        raise HTTPException(
            status_code=500, detail="phase log read failed",
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


@app.post("/api/start_batch", summary="启动批处理任务（后台执行）")
async def start_batch(
    background_tasks: BackgroundTasks,
    target_url: str = Form(
        "",
        description=(
            "目标系统 URL（可选）。留空时先尝试从 prompt/goal 中提取 http(s) URL；"
            "提取不到再由 io_contract preflight 推断入口（抽象目标退化为搜索引擎入口）。"
            "响应中的 target_url_auto_entry 标记入口来源，供前端确认/覆盖。"
        ),
    ),
    prompt: str = Form("", description="自然语言 Prompt 指令"),
    goal: str = Form("", description="兼容旧字段：自然语言 Prompt 指令"),
    auth_profiles: str = Form("", description="Auth Matrix profile names, comma-separated"),
    vlm_model: str = Form("", description="Runtime VLM model override"),
    semantic_model: str = Form("", description="Runtime semantic/text model override"),
    vlm_model_type: str = Form("vl", description="vl or text"),
    vlm_temperature: str = Form("", description="Runtime VLM temperature override"),
    vlm_max_tokens: str = Form("", description="Runtime VLM max_tokens override"),
    vlm_base_url: str = Form("", description="Runtime VLM base URL override"),
    vlm_api_key: str = Form("", description="Runtime VLM API key override"),
    semantic_base_url: str = Form("", description="Runtime semantic/text base URL override"),
    semantic_api_key: str = Form("", description="Runtime semantic/text API key override"),
    urls: str = Form(
        "",
        description="Optional authoritative start URLs (JSON array, or comma/newline separated). "
                    "When supplied, urls overrides target_url.",
    ),
    file: UploadFile | None = File(
        None,
        description="Optional attachment. Any supported document/media format "
                    "(csv/xlsx/json/txt/pdf/docx/pptx/eml/img/video/zip ...); "
                    "intent is auto-inferred from filename + mime + goal.",
    ),
    attachment_intent: str = Form(
        "",
        description="Optional explicit attachment intent override "
                    "(batch_rows | upload_to_page | prompt_context | media_source). "
                    "Empty or 'auto' keeps automatic inference.",
    ),
    constraints: str = Form(
        "",
        description="Optional JSON object for input_contract.constraints (proxy, max_steps, ...).",
    ),
) -> dict:
    """
    接收任务参数和可选上传文件。

    流程：
    1. 将文件落盘到 `temp_uploads/`
    2. 创建后台任务控制对象
    3. 通过 `BackgroundTasks` 异步启动真正执行
    4. 立即返回，由前端通过 WebSocket 观察执行进度
    """
    if not isinstance(constraints, str):
        constraints = ""
    if not isinstance(urls, str):
        urls = ""
    if not isinstance(attachment_intent, str):
        attachment_intent = ""
    current = _task_snapshot()
    if not current.get("running") and current.get("in_cooldown"):
        # 任务刚刚完成，短时间内再次下发大概率是前端双击 / 自动重试 / done 广播抖动
        # 拒绝本次请求，让前端显示提示或等待冷静期结束
        remain = current.get("cooldown_remaining", 0.0)
        logger.warning(
            f"[start_batch] Rejected duplicate submission during cooldown "
            f"(remaining={remain}s, last_status={current.get('status')})"
        )
        return {
            "status": "error",
            "message": (
                f"上一个任务刚结束 ({current.get('status')}), "
                f"请 {remain:.1f}s 后再提交，防止重复触发。"
            ),
            "task": current,
        }

    merged_prompt = (prompt or goal or "").strip()
    if not merged_prompt:
        return {
            "status": "error",
            "message": "Missing prompt parameter (or compatible field goal).",
        }

    # §一-B: the user may explicitly override the attachment intent. "auto"
    # (or empty) keeps inference; anything else must be a real intent —
    # reject early instead of silently degrading ("unknown" is an inference
    # result, never a user choice).
    user_attachment_intent = (attachment_intent or "").strip().lower()
    if user_attachment_intent == "auto":
        user_attachment_intent = ""
    if user_attachment_intent:
        try:
            from visual_web_agent.io_contract import ATTACHMENT_INTENTS as _ATTACHMENT_INTENTS
        except Exception:
            _ATTACHMENT_INTENTS = ("batch_rows", "upload_to_page", "prompt_context", "media_source", "unknown")
        allowed_user_intents = tuple(i for i in _ATTACHMENT_INTENTS if i != "unknown")
        if user_attachment_intent not in allowed_user_intents:
            return {
                "status": "error",
                "message": (
                    f"Invalid attachment_intent {attachment_intent!r}; "
                    f"expected one of {', '.join(allowed_user_intents)} or 'auto'."
                ),
            }

    inferred_from_prompt = False
    auto_entry_source = ""
    urls_raw = urls if isinstance(urls, str) else ""
    explicit_urls: list[str] = []
    explicit_url_errors: list[str] = []
    if urls_raw.strip():
        try:
            from visual_web_agent.io_contract import parse_urls_field as _parse_urls_field
        except Exception:
            _parse_urls_field = None  # type: ignore[assignment]
        if _parse_urls_field is None:
            explicit_url_errors.append("urls parser unavailable")
        else:
            for spec in _parse_urls_field(urls_raw):
                norm, err = _normalize_target_url(spec.url)
                if err:
                    explicit_url_errors.append(f"{spec.url}: {err}")
                    continue
                if norm and norm not in explicit_urls:
                    explicit_urls.append(norm)
        if not explicit_urls and not explicit_url_errors:
            explicit_url_errors.append("no valid URLs supplied")
        if explicit_url_errors:
            return {
                "status": "error",
                "message": "Invalid urls: " + "; ".join(explicit_url_errors),
            }

    if explicit_urls:
        target_url = explicit_urls[0]

    if not explicit_urls and not (target_url or "").strip():
        harvested = _harvest_urls_from_text(merged_prompt)
        if harvested:
            target_url = harvested[0]
            inferred_from_prompt = True
            logger.info(
                "[start_batch] target_url 缺省，从 prompt 自动提取首个 URL: %s",
                target_url,
            )
        else:
            resolved_entry = ""
            try:
                from visual_web_agent.io_contract import build_preflight as _build_preflight
                from visual_web_agent.io_contract.entry_llm import entry_llm_from_config

                # §一-B #2: inject the semantic LLM so a URL-less goal resolves to a
                # real site the model picks (entry_suggestion.source == "llm"),
                # not just the deterministic search fallback. Run off the event
                # loop since the sync client makes a blocking completion call.
                _entry_llm = entry_llm_from_config()
                _pf_entry = await asyncio.to_thread(
                    _build_preflight, merged_prompt, llm=_entry_llm
                )
                resolved_entry = (_pf_entry.resolved_start_url or "").strip()
                auto_entry_source = (
                    _pf_entry.entry_suggestion.source
                    if _pf_entry.entry_suggestion else "inferred"
                )
            except Exception as _pf_exc:
                logger.warning("[start_batch] preflight 入口推断失败: %s", _pf_exc)
                resolved_entry = ""
                auto_entry_source = ""
            if resolved_entry:
                target_url = resolved_entry
                logger.info(
                    "[start_batch] target_url 缺省且 prompt 无链接，preflight 推断入口: %s (%s)",
                    target_url, auto_entry_source or "inferred",
                )
            else:
                return {
                    "status": "error",
                    "message": (
                        "缺少 target_url，且无法从目标推断入口。"
                        "请在表单中填写目标 URL，或在自然语言指令里直接写出网址。"
                    ),
                }

    normalized_target_url, target_url_error = _normalize_target_url(target_url)
    if target_url_error:
        return {
            "status": "error",
            "message": target_url_error,
        }
    target_url = normalized_target_url

    extra_urls: list[str] = list(explicit_urls[1:]) if explicit_urls else []
    if not explicit_urls and inferred_from_prompt:
        prompt_urls = _harvest_urls_from_text(merged_prompt)
        for candidate in prompt_urls[1:]:
            norm, err = _normalize_target_url(candidate)
            if not err and norm and norm != target_url and norm not in extra_urls:
                extra_urls.append(norm)

    vlm_options: dict[str, Any] = {
        "model": vlm_model.strip(),
        "semantic_model": semantic_model.strip(),
        "model_type": vlm_model_type.strip().lower() or "vl",
        "base_url": vlm_base_url.strip(),
        "api_key": vlm_api_key.strip(),
        "semantic_base_url": semantic_base_url.strip(),
        "semantic_api_key": semantic_api_key.strip(),
    }
    if vlm_temperature.strip():
        try:
            vlm_options["temperature"] = float(vlm_temperature)
        except ValueError:
            return {"status": "error", "message": "Invalid vlm_temperature"}
    if vlm_max_tokens.strip():
        try:
            vlm_options["max_tokens"] = int(vlm_max_tokens)
        except ValueError:
            return {"status": "error", "message": "Invalid vlm_max_tokens"}
    vlm_options = {k: v for k, v in vlm_options.items() if v not in ("", None)}

    run_constraints: dict[str, Any] = {}
    constraints_raw = (constraints or "").strip()
    if constraints_raw:
        try:
            parsed_constraints = json.loads(constraints_raw)
            if isinstance(parsed_constraints, dict):
                run_constraints = parsed_constraints
            else:
                return {"status": "error", "message": "constraints must be a JSON object"}
        except json.JSONDecodeError as exc:
            return {"status": "error", "message": f"Invalid constraints JSON: {exc}"}

    saved_path: str | None = None
    file_size_kb = 0.0
    safe_name = ""
    existed = False
    upload_sha256 = ""
    upload_mime = ""

    if file is not None:
        content = await file.read()
        file_size_kb = len(content) / 1024
        original_name = Path(file.filename or "upload").name
        try:
            from visual_web_agent.upload_store import save_bytes as _upload_save_bytes
            entry = _upload_save_bytes(
                TEMP_UPLOAD_DIR,
                content,
                filename=original_name,
                mime_hint=getattr(file, "content_type", "") or "",
            )
            safe_name = original_name
            saved_path = entry.path
            upload_sha256 = entry.sha256
            upload_mime = entry.mime
            existed = bool(entry.refs)
        except Exception as _save_exc:
            logger.warning(
                "[start_batch] upload_store.save_bytes failed (%s); falling back to legacy write_bytes",
                _save_exc,
            )
            safe_name = original_name
            saved_path_obj = TEMP_UPLOAD_DIR / safe_name
            existed = saved_path_obj.exists()
            saved_path_obj.write_bytes(content)
            saved_path = str(saved_path_obj)

    effective_attachment_intent = ""
    attachment_intent_source = ""
    if saved_path:
        if user_attachment_intent:
            effective_attachment_intent = user_attachment_intent
            attachment_intent_source = "user"
        else:
            try:
                from visual_web_agent.io_contract import infer_attachment_intent as _infer_attachment_intent

                effective_attachment_intent = _infer_attachment_intent(
                    filename=safe_name,
                    mime=upload_mime,
                    goal=merged_prompt,
                )
                attachment_intent_source = "inferred" if effective_attachment_intent else ""
            except Exception as _intent_exc:
                logger.debug("[start_batch] attachment intent inference failed: %s", _intent_exc)
                effective_attachment_intent = ""

    mode_label = "batch" if saved_path else "single"
    logger.info(
        f"[start_batch] 任务入队 ({mode_label})\n"
        f"  target_url : {target_url!r}\n"
        f"  prompt     : {merged_prompt[:120]!r}{'...' if len(merged_prompt) > 120 else ''}\n"
        f"  auth       : {auth_profiles.strip() or '<auto/default>'}\n"
        f"  vlm        : {vlm_options.get('model') or '<default>'}"
        f" ({vlm_options.get('model_type') or 'vl'}), "
        f"semantic={vlm_options.get('semantic_model') or '<default>'}, "
        f"semantic_base={vlm_options.get('semantic_base_url') or '<default>'}\n"
        f"  file       : {safe_name!r}  ({file_size_kb:.1f} KB)\n"
        f"  intent     : {effective_attachment_intent or '<none>'}"
        f"{' (user override)' if attachment_intent_source == 'user' else ''}\n"
        f"  saved_to   : {saved_path or '<none>'}\n"
        f"  overwritten: {existed}"
    )

    if saved_path:
        await manager.send_log(
            f"[UPLOAD] Task queued with file: {safe_name} ({file_size_kb:.1f} KB) -> {target_url}",
            level="info",
        )
    else:
        await manager.send_log(
            f"[UPLOAD] Task queued in single-task mode -> {target_url}",
            level="info",
        )

    task_item, should_start_worker = _enqueue_task(
        target_url=target_url,
        prompt=merged_prompt,
        file_path=saved_path or "",
        auth_profiles=auth_profiles.strip(),
        vlm_model=str(vlm_options.get("model") or ""),
        semantic_model=str(vlm_options.get("semantic_model") or ""),
        vlm_text_only=vlm_options.get("model_type") == "text",
        mode="batch" if saved_path else "single",
        filename=safe_name,
        file_size_kb=file_size_kb,
        vlm_model_type=str(vlm_options.get("model_type") or "vl"),
        vlm_options=vlm_options,
        urls=extra_urls,
        upload_sha256=upload_sha256,
        upload_mime=upload_mime,
        constraints=run_constraints,
        attachment_intent=user_attachment_intent if saved_path else "",
    )
    if upload_sha256:
        try:
            from visual_web_agent.upload_store import add_ref as _upload_add_ref
            _upload_add_ref(TEMP_UPLOAD_DIR, upload_sha256, str(task_item.get("task_id") or ""))
        except Exception as _ref_exc:
            logger.debug("[start_batch] upload_store.add_ref failed: %s", _ref_exc)
    started_workers: list[str] = []
    worker_config = _queue_worker_config()
    if should_start_worker:
        started_workers, worker_config = _start_queue_workers(background_tasks)

    return {
        "status": "success",
        "message": "任务已入队，请通过 WebSocket /ws/logs 监听进度",
        "task_id": task_item["task_id"],
        "queued": True,
        "position": _queue_snapshot()["queue_length"],
        "queue": _queue_snapshot(),
        "started_workers": started_workers,
        "worker_config": worker_config,
        "mode": "batch" if saved_path else "single",
        "filename": safe_name,
        "overwritten": existed,
        "file_size_kb": round(file_size_kb, 1),
        "target_url": target_url,
        "target_url_auto_entry": auto_entry_source,
        "urls": extra_urls,
        "auth_profiles": auth_profiles.strip(),
        "vlm_model": vlm_options.get("model", ""),
        "semantic_model": vlm_options.get("semantic_model", ""),
        "vlm_model_type": vlm_options.get("model_type", "vl"),
        "upload_sha256": upload_sha256,
        "upload_mime": upload_mime,
        "attachment_intent": effective_attachment_intent,
        "attachment_intent_source": attachment_intent_source,
    }


@app.post("/api/stop_batch", summary="停止当前批处理任务")
async def stop_batch() -> dict:
    ok, message = request_stop_current_task()
    level = "warn" if ok else "info"
    await manager.send_log(f"[STOP] {message}", level=level)
    return {
        "status": "success" if ok else "error",
        "message": message,
        "task": _task_snapshot(),
    }


@app.get("/api/task_queue", summary="查看任务队列")
async def get_task_queue() -> dict:
    return {
        "status": "success",
        "queue": _queue_snapshot(),
        "persisted": _queue_state.load_public_snapshot(),
    }


@app.get("/api/task_queue/metrics", summary="查看任务队列指标（Y17）")
async def get_task_queue_metrics(run_limit: int = 200) -> dict:
    return {
        "status": "success",
        "metrics": queue_metrics(run_limit=run_limit),
    }


@app.get("/api/browser_pool", summary="查看浏览器资源池状态（Y8-A）")
async def get_browser_pool_status() -> dict:
    pool = _get_browser_pool_status()
    backend = _browser_control.backend_status()
    return {
        "status": "success",
        "pool": pool,
        "runtime": _get_browser_runtime_status(pool_status=pool, backend_status=backend),
    }


@app.get("/api/browser_sessions", summary="查看跨系统 Session Pool 状态（E1）")
async def get_browser_sessions() -> dict:
    return {
        "status": "success",
        "result": _get_browser_session_pool_status(),
    }


@app.delete("/api/task_queue/{task_id}", summary="取消尚未开始的排队任务")
async def delete_queued_task(task_id: str) -> dict:
    ok, message = cancel_queued_task(task_id)
    await manager.send_log(f"[QUEUE] {message}: {task_id}", level="warn" if ok else "info")
    return {
        "status": "success" if ok else "error",
        "message": message,
        "queue": _queue_snapshot(),
        "persisted": _queue_state.load_public_snapshot(),
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
        "persisted": _queue_state.load_public_snapshot(),
    }


@app.post("/api/task_queue/resume", summary="恢复队列领取新任务（Y15）")
async def resume_queue(background_tasks: BackgroundTasks) -> dict:
    queue, should_start_worker = resume_task_queue()
    started_workers: list[str] = []
    worker_config = _queue_worker_config()
    if should_start_worker:
        started_workers, worker_config = _start_queue_workers(background_tasks)
        queue = _queue_snapshot()
    await manager.send_log("[QUEUE] 队列已恢复", level="info")
    return {
        "status": "success",
        "queue": queue,
        "started_workers": started_workers,
        "worker_config": worker_config,
        "persisted": _queue_state.load_public_snapshot(),
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
        "queue": _queue_snapshot(),
        "persisted": _queue_state.load_public_snapshot(),
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
        "queue": _queue_snapshot(),
        "persisted": _queue_state.load_public_snapshot(),
    }


@app.get("/api/runs", summary="列出任务运行记录")
async def list_runs(limit: int = 50, status: str = "") -> dict:
    try:
        items = _run_registry.list_runs(limit=limit, status=status or None)
    except Exception as exc:
        logger.warning("[RUN REGISTRY] list_runs failed: %s", exc)
        raise HTTPException(status_code=500, detail="run registry read failed") from exc
    return {
        "status": "success",
        "count": len(items),
        "runs": items,
    }


@app.get("/api/runs/{run_id}/network", summary="列出 run 捕获到的候选网络数据接口")
async def get_run_network_candidates(
    run_id: str,
    limit: int = 100,
    min_score: int = 0,
) -> dict:
    items = _network_intelligence.list_candidates(
        run_id,
        limit=limit,
        min_score=min_score,
    )
    summary = _network_intelligence.summarize_candidates(items)
    return {
        "status": "success",
        "run_id": run_id,
        "count": len(items),
        "items": items,
        "summary": summary,
    }


@app.post("/api/runs/{run_id}/network/replay", summary="基于候选网络接口生成或执行 API Replay")
async def replay_run_network_candidate(
    run_id: str,
    endpoint: str = "",
    page: int = 1,
    page_size: int = 50,
    execute: bool = False,
    paginate: bool = False,
    max_pages: int = 20,
) -> dict:
    candidates = _network_intelligence.list_candidates(run_id, limit=200)
    candidate = _api_replay.choose_candidate(candidates, endpoint=endpoint)
    if candidate is None:
        raise HTTPException(status_code=404, detail="network candidate not found")
    plan = _api_replay.build_replay_plan(
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
            result = _api_replay.paginate_replay(
                run_id=run_id,
                candidate=candidate,
                page_size=max(1, min(int(page_size or 50), 500)),
                start_page=max(1, int(page or 1)),
                max_pages=max(1, min(int(max_pages or 20), 100)),
            )
        else:
            result = _api_replay.replay_candidate(
                run_id=run_id,
                candidate=candidate,
                page=max(1, int(page or 1)),
                page_size=max(1, min(int(page_size or 50), 500)),
            )
    except Exception as exc:
        logger.warning("[API REPLAY] replay failed for %s: %s", run_id, exc)
        raise HTTPException(status_code=502, detail=f"api replay failed: {type(exc).__name__}") from exc
    return result


@app.get("/api/runs/{run_id}", summary="读取单个任务运行记录")
async def get_run(run_id: str) -> dict:
    rec = _run_registry.load_run(run_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "status": "success",
        "run": rec,
        "contracts": _load_run_contract_bundle(run_id),
    }


@app.post("/api/runs/{run_id}/retry", summary="手动重试失败/停止的任务（Y16）")
async def retry_run(run_id: str, background_tasks: BackgroundTasks) -> dict:
    ok, message, retry = retry_run_as_queued_task(run_id)
    started_workers: list[str] = []
    worker_config = _queue_worker_config()
    if ok and retry.get("should_start_worker"):
        started_workers, worker_config = _start_queue_workers(background_tasks)
    await manager.send_log(
        f"[QUEUE RETRY] {message}: {run_id}",
        level="info" if ok else "warn",
    )
    return {
        "status": "success" if ok else "error",
        "message": message,
        "retry": retry,
        "queue": _queue_snapshot(),
        "started_workers": started_workers,
        "worker_config": worker_config,
        "persisted": _queue_state.load_public_snapshot(),
    }


@app.get("/health", summary="健康检查")
async def health() -> dict:
    return {
        "status": "ok",
        "ws_connections": len(manager.active),
        "loop_ready": _API_LOOP is not None,
        "task": _task_snapshot(),
    }


def _resolve_api_bind() -> tuple[str, int]:
    # Bind localhost-only by default; opt into network exposure via env so the
    # single-machine flow keeps working without implicit 0.0.0.0 exposure.
    host = (os.getenv("VSPIDER_API_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int((os.getenv("VSPIDER_API_PORT") or "8000").strip() or "8000")
    except ValueError:
        port = 8000
    return host, port


if __name__ == "__main__":
    reload_enabled = _env_flag("VSPIDER_API_RELOAD", default=False)
    _reload_pattern = lambda *parts: Path(*parts).as_posix()
    reload_excludes = [
        _reload_pattern("temp_uploads"),
        _reload_pattern("__pycache__"),
        _reload_pattern("browser_data"),
        _reload_pattern("downloads"),
        _reload_pattern("logs"),
        _reload_pattern("rpa_cache"),
        _reload_pattern("screenshots"),
        _reload_pattern("visual_web_agent", "__pycache__"),
        _reload_pattern("visual_web_agent", "browser_data"),
        _reload_pattern("visual_web_agent", "downloads"),
        _reload_pattern("visual_web_agent", "logs"),
        _reload_pattern("visual_web_agent", "rpa_cache"),
        _reload_pattern("visual_web_agent", "screenshots"),
        _reload_pattern("output_*.xlsx"),
    ]

    _bind_host, _bind_port = _resolve_api_bind()
    logger.info(
        "[startup] API binding to %s:%s (override via VSPIDER_API_HOST / VSPIDER_API_PORT)",
        _bind_host,
        _bind_port,
    )
    uvicorn_kwargs: dict[str, Any] = {
        "host": _bind_host,
        "port": _bind_port,
        "log_level": "info",
        "reload": reload_enabled,
    }

    if reload_enabled:
        uvicorn_kwargs["reload_excludes"] = reload_excludes
        logger.info(
            "[startup] API reload enabled via VSPIDER_API_RELOAD=true; "
            "runtime dirs are excluded from watch."
        )
    else:
        logger.info(
            "[startup] API reload disabled by default to avoid runtime file writes "
            "restarting active tasks."
        )

    uvicorn.run("api_server:app", **uvicorn_kwargs)

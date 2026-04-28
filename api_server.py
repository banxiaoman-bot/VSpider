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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Coroutine
from urllib.parse import urlparse

import uvicorn
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from visual_web_agent.artifact_manager import artifact_root, artifact_url


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


TEMP_UPLOAD_DIR = Path(__file__).resolve().parent / "temp_uploads"
TEMP_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR = artifact_root()


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


class ConnectionManager:
    """维护 WebSocket 连接并向所有客户端广播消息。"""

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
        payload = json.dumps(message, ensure_ascii=False)
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

    async def send_done(self, success: bool, message: str = "") -> None:
        await self.broadcast({"type": "done", "success": success, "message": message})

    async def send_status(self, status: str, **payload: Any) -> None:
        data = {"type": "status", "status": status}
        data.update(payload)
        await self.broadcast(data)


manager = ConnectionManager()


_TASK_LOCK = threading.Lock()
active_tasks: dict[str, Any] = {"current_task": None}
_AUTH_SESSION_LOCK = asyncio.Lock()
_AUTH_SESSION: dict[str, Any] | None = None
_HITL_RESUME_EVENT = threading.Event()

# 任务完成后冷静期（秒）：防止前端抖动 / 双击 / 重复广播触发的二次下发
# 在这个窗口内，新的 /api/start_batch 会被拒绝并返回 cooldown 提示。
_TASK_COOLDOWN_SECONDS = 8.0


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


def _task_snapshot() -> dict[str, Any]:
    with _TASK_LOCK:
        task = active_tasks.get("current_task")
        if not task:
            return {
                "running": False,
                "task_id": None,
                "status": "idle",
                "stop_requested": False,
                "in_cooldown": False,
                "cooldown_remaining": 0.0,
            }
        finished_at = task.get("finished_at") or 0.0
        running = bool(task.get("running", False))
        if running or finished_at <= 0:
            cooldown_remaining = 0.0
        else:
            cooldown_remaining = max(
                0.0, _TASK_COOLDOWN_SECONDS - (time.time() - finished_at)
            )
        return {
            "running": running,
            "task_id": task.get("task_id"),
            "status": task.get("status", "unknown"),
            "stop_requested": bool(
                task.get("stop_event") and task["stop_event"].is_set()
            ),
            "in_cooldown": cooldown_remaining > 0,
            "cooldown_remaining": round(cooldown_remaining, 2),
        }


def _create_task_control(
    target_url: str,
    prompt: str,
    file_path: str,
    auth_profiles: str = "",
    vlm_model: str = "",
    semantic_model: str = "",
    vlm_text_only: bool = False,
) -> tuple[str, threading.Event]:
    task_id = str(int(time.time() * 1000))
    stop_event = threading.Event()
    with _TASK_LOCK:
        active_tasks["current_task"] = {
            "task_id": task_id,
            "target_url": target_url,
            "prompt": prompt,
            "file_path": file_path,
            "auth_profiles": auth_profiles,
            "vlm_model": vlm_model,
            "semantic_model": semantic_model,
            "vlm_text_only": vlm_text_only,
            "status": "running",
            "running": True,
            "created_at": time.time(),
            "stop_event": stop_event,
        }
    return task_id, stop_event


def request_stop_current_task() -> tuple[bool, str]:
    with _TASK_LOCK:
        task = active_tasks.get("current_task")
        if not task or not task.get("running"):
            return False, "当前没有运行中的任务"

        stop_event = task.get("stop_event")
        if stop_event and not stop_event.is_set():
            stop_event.set()
            task["status"] = "stopping"
            return True, "停止信号已发送"

        return True, "停止信号已存在，任务正在终止"


_API_LOOP: asyncio.AbstractEventLoop | None = None


def _schedule(coro: Coroutine[Any, Any, Any]) -> None:
    """
    把协程安全地投递到 FastAPI 事件循环。

    - 当前就在 API 事件循环中：直接 `create_task`
    - 其他线程：用 `run_coroutine_threadsafe`
    - API 未启动：静默关闭协程，允许 CLI 独立运行
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
        loop.create_task(coro)
    else:
        asyncio.run_coroutine_threadsafe(coro, loop)


def broadcast_log(content: str, level: str = "info") -> None:
    _schedule(manager.send_log(content, level))


def broadcast_image(b64_data: str, step: int = 0) -> None:
    _schedule(manager.send_screenshot(step, b64_data))


def broadcast_done(success: bool, message: str = "") -> None:
    _schedule(manager.send_done(success, message))


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


async def broadcast_log_async(msg: str, level: str = "info") -> None:
    await manager.send_log(msg, level)


async def broadcast_image_async(b64_string: str, step: int = 0) -> None:
    await manager.send_screenshot(step, b64_string)


async def _run_batch_task(
    task_id: str,
    target_url: str,
    prompt: str,
    file_path: str | None,
    stop_event: threading.Event,
    auth_profiles: str = "",
    vlm_options: dict[str, Any] | None = None,
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
        await asyncio.to_thread(
            run_smart_batch_sync,
            target_url,
            prompt,
            file_path or "",
            stop_event,
            auth_profiles,
            vlm_options or {},
        )

        with _TASK_LOCK:
            task = active_tasks.get("current_task")
            if task and task.get("task_id") == task_id:
                task["status"] = "stopped" if stop_event.is_set() else "completed"
                task["running"] = False

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
        broadcast_log(f"[TASK] 后台任务异常: {err_text}", level="error")
        broadcast_done(False, f"后台任务异常: {err_text}")
    finally:
        with _TASK_LOCK:
            task = active_tasks.get("current_task")
            if task and task.get("task_id") == task_id:
                task["finished_at"] = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """捕获 FastAPI 事件循环，供跨线程广播使用。"""
    global _API_LOOP
    _API_LOOP = asyncio.get_running_loop()
    logger.info("VSpider API server started; event loop captured for broadcasts.")
    try:
        yield
    finally:
        _API_LOOP = None
        logger.info("VSpider API 服务关闭")


app = FastAPI(
    title="VSpider API",
    description="VSpider backend service API",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/download", StaticFiles(directory=str(ARTIFACT_DIR)), name="artifacts")


@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket) -> None:
    """
    前端通过此 WebSocket 接收实时日志、截图和 done 事件。
    客户端发送的任何消息都忽略，仅用于保活。
    """
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
        }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            item["cookies"] = len(data.get("cookies") or [])
            item["origins"] = len(data.get("origins") or [])
        except Exception as exc:
            item["warning"] = f"{type(exc).__name__}: {exc}"
        profiles.append(item)

    return {"status": "success", "auth_dir": str(auth_root), "profiles": profiles}


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
    _HITL_RESUME_EVENT.set()
    await manager.send_status("human_resumed")
    await manager.send_log("[HITL] 操作员确认完成，Agent 恢复执行", level="info")
    return {"status": "success", "message": "Agent resume signal sent"}


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


@app.post("/api/start_batch", summary="启动批处理任务（后台执行）")
async def start_batch(
    background_tasks: BackgroundTasks,
    target_url: str = Form(..., description="目标系统 URL"),
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
    file: UploadFile | None = File(
        None,
        description="Excel (.xlsx) or CSV file, optional",
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
    current = _task_snapshot()
    if current["running"]:
        return {
            "status": "error",
            "message": "A task is already running; stop it before starting a new one.",
            "task": current,
        }
    if current.get("in_cooldown"):
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
    normalized_target_url, target_url_error = _normalize_target_url(target_url)
    if target_url_error:
        return {
            "status": "error",
            "message": target_url_error,
        }
    target_url = normalized_target_url

    vlm_options: dict[str, Any] = {
        "model": vlm_model.strip(),
        "semantic_model": semantic_model.strip(),
        "model_type": vlm_model_type.strip().lower() or "vl",
        "base_url": vlm_base_url.strip(),
        "api_key": vlm_api_key.strip(),
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

    saved_path: str | None = None
    file_size_kb = 0.0
    safe_name = ""
    existed = False

    if file is not None:
        content = await file.read()
        file_size_kb = len(content) / 1024
        safe_name = Path(file.filename or "upload.xlsx").name
        saved_path_obj = TEMP_UPLOAD_DIR / safe_name
        existed = saved_path_obj.exists()
        saved_path_obj.write_bytes(content)
        saved_path = str(saved_path_obj)

    mode_label = "batch" if saved_path else "single"
    logger.info(
        f"[start_batch] 任务入队 ({mode_label})\n"
        f"  target_url : {target_url!r}\n"
        f"  prompt     : {merged_prompt[:120]!r}{'...' if len(merged_prompt) > 120 else ''}\n"
        f"  auth       : {auth_profiles.strip() or '<auto/default>'}\n"
        f"  vlm        : {vlm_options.get('model') or '<default>'}"
        f" ({vlm_options.get('model_type') or 'vl'}), "
        f"semantic={vlm_options.get('semantic_model') or '<default>'}\n"
        f"  file       : {safe_name!r}  ({file_size_kb:.1f} KB)\n"
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

    task_id, stop_event = _create_task_control(
        target_url,
        merged_prompt,
        saved_path or "",
        auth_profiles.strip(),
        str(vlm_options.get("model") or ""),
        str(vlm_options.get("semantic_model") or ""),
        vlm_options.get("model_type") == "text",
    )
    background_tasks.add_task(
        _run_batch_task,
        task_id,
        target_url,
        merged_prompt,
        saved_path,
        stop_event,
        auth_profiles.strip(),
        vlm_options,
    )

    return {
        "status": "success",
        "message": "任务已在后台启动，请通过 WebSocket /ws/logs 监听进度",
        "task_id": task_id,
        "mode": "batch" if saved_path else "single",
        "filename": safe_name,
        "overwritten": existed,
        "file_size_kb": round(file_size_kb, 1),
        "target_url": target_url,
        "auth_profiles": auth_profiles.strip(),
        "vlm_model": vlm_options.get("model", ""),
        "semantic_model": vlm_options.get("semantic_model", ""),
        "vlm_model_type": vlm_options.get("model_type", "vl"),
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


@app.get("/health", summary="健康检查")
async def health() -> dict:
    return {
        "status": "ok",
        "ws_connections": len(manager.active),
        "loop_ready": _API_LOOP is not None,
        "task": _task_snapshot(),
    }


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

    uvicorn_kwargs: dict[str, Any] = {
        "host": "0.0.0.0",
        "port": 8000,
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

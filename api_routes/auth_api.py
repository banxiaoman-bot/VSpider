"""Auth & HITL API routes — extracted from ``api_server.py``.

Houses ``/api/auth/*``, ``/api/human/*``,
``/api/runtime/captcha_solver``, ``/api/output_contract/preview``
endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Form

logger = logging.getLogger("api_routes.auth_api")


_AUTH_SESSION_LOCK = asyncio.Lock()
_AUTH_SESSION: dict[str, Any] | None = None


def _auth_dir() -> Path:
    try:
        from visual_web_agent import config

        return Path(getattr(config, "AUTH_DIR", "") or ".auth").resolve()
    except Exception:
        return (Path(__file__).resolve().parent.parent / ".auth").resolve()


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


def register_auth_routes(
    app: FastAPI,
    *,
    manager: Any,
    broadcast_resume_human: Callable,
    broadcast_submit_hitl_form: Callable,
) -> None:
    """Mount auth, HITL, runtime and output-contract routes on *app*."""

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
        broadcast_resume_human()
        await manager.send_status("human_resumed")
        await manager.send_log("[HITL] 操作员确认完成，Agent 恢复执行", level="info")
        return {"status": "success", "message": "Agent resume signal sent"}

    @app.post("/api/human/form_submit", summary="前端 HITL 表单提交")
    async def submit_hitl_form_endpoint(payload: dict) -> dict:
        fields = payload.get("fields", {})
        if not fields:
            return {"status": "error", "message": "No fields provided"}
        broadcast_submit_hitl_form(fields)
        await manager.send_status("human_resumed")
        filled = ", ".join(f"{k}=***" for k in fields)
        await manager.send_log(
            f"[HITL] 用户通过前端表单提交了 {len(fields)} 个字段: {filled}",
            level="info",
        )
        return {"status": "success", "message": f"Form submitted with {len(fields)} fields"}

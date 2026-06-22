"""Robots/throttle policy API routes — extracted from ``api_server.py``.

Houses ``/api/robots/*`` endpoints (Y23): set / check / reserve / read.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Body, FastAPI, HTTPException

logger = logging.getLogger("api_routes.robots_api")


def register_robots_routes(
    app: FastAPI,
    *,
    robots_policy: Any,
) -> None:
    """Mount all robots/throttle routes on *app*.

    Parameters
    ----------
    robots_policy:
        The shared ``RobotsPolicyManager`` instance (provides
        ``set_robots`` / ``check_url`` / ``reserve_url`` / ``public_rules``).
    """

    @app.post("/api/robots/set", summary="设置 robots.txt 规则缓存（Y23）")
    async def set_robots_policy(payload: dict[str, Any] = Body(...)) -> dict:
        try:
            result = robots_policy.set_robots(
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
            result = robots_policy.check_url(
                str(payload.get("url") or ""),
                obey=bool(payload.get("obey", True)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "success", "result": result}

    @app.post("/api/robots/reserve", summary="预约 URL 抓取并更新域名节流（Y23）")
    async def reserve_robots_policy(payload: dict[str, Any] = Body(...)) -> dict:
        try:
            result = robots_policy.reserve_url(
                str(payload.get("url") or ""),
                obey=bool(payload.get("obey", True)),
                default_delay=float(payload.get("default_delay") or 0.0),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "success", "result": result}

    @app.get("/api/robots/{domain}", summary="读取域名 robots/throttle 规则（Y23）")
    async def get_robots_policy(domain: str) -> dict:
        return {"status": "success", "result": robots_policy.public_rules(domain)}

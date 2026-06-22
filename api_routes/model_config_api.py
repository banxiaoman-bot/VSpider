"""Model config persistence API routes — extracted from ``api_server.py``.

MC-1: 单套全局模型配置服务端持久化。
- ``GET  /api/model_config``  -> 脱敏配置（apiKey 仅掩码 + has_api_key，回填用）
- ``POST /api/model_config``  -> 保存 {vlm:{...}, semantic:{...}}（空 apiKey 保留旧值），回脱敏配置

Spec: docs/superpowers/specs/2026-06-22-model-config-server-persistence-design.md
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Body, FastAPI

import model_config_store

logger = logging.getLogger("api_routes.model_config_api")


def register_model_config_routes(app: FastAPI) -> None:
    """Mount GET/POST /api/model_config on *app* (reads store at call time)."""

    @app.get("/api/model_config", summary="读取模型配置（apiKey 脱敏回填）")
    async def get_model_config() -> dict:
        return {"status": "success", "result": model_config_store.masked_config()}

    @app.post("/api/model_config", summary="保存模型配置（覆盖；空 apiKey 保留旧值）")
    async def post_model_config(payload: dict[str, Any] = Body(default={})) -> dict:
        model_config_store.save_model_config(payload or {})
        return {"status": "success", "result": model_config_store.masked_config()}

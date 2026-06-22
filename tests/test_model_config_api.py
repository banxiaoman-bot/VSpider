"""MC-1: /api/model_config GET(掩码) + POST(保存) 路由测试。

镜像 tests/test_robots_api.py 的 TestClient 装配；monkeypatch 存储模块的
MODEL_CONFIG_PATH 指向 tmp_path 隔离磁盘。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

import api_server
import model_config_store as store


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(store, "MODEL_CONFIG_PATH", tmp_path / "model_config.json")
    return TestClient(api_server.app, raise_server_exceptions=False)


class TestRoutesRegistered:
    def test_routes_present(self) -> None:
        paths = {getattr(r, "path", "") for r in api_server.app.routes}
        assert "/api/model_config" in paths


class TestGetModelConfig:
    def test_get_empty_returns_masked_skeleton(self, client: TestClient) -> None:
        body = client.get("/api/model_config").json()
        assert body["status"] == "success"
        assert body["result"]["vlm"]["has_api_key"] is False
        assert body["result"]["vlm"]["api_key"] == ""


class TestPostModelConfig:
    def test_post_saves_and_returns_masked(self, client: TestClient) -> None:
        body = client.post("/api/model_config", json={
            "vlm": {"base_url": "https://vlm/v1", "api_key": "sk-abcdefgh1234", "model": "m1"},
        }).json()
        assert body["status"] == "success"
        assert body["result"]["vlm"]["has_api_key"] is True
        assert body["result"]["vlm"]["api_key"] == "sk-****1234"   # masked, no plaintext
        # round-trip: GET reflects saved base_url/model, key still masked
        got = client.get("/api/model_config").json()["result"]
        assert got["vlm"]["base_url"] == "https://vlm/v1"
        assert got["vlm"]["model"] == "m1"
        assert got["vlm"]["has_api_key"] is True

    def test_post_blank_key_preserves(self, client: TestClient) -> None:
        client.post("/api/model_config", json={"vlm": {"api_key": "sk-abcdefgh1234"}})
        client.post("/api/model_config", json={"vlm": {"api_key": "", "model": "m2"}})
        got = client.get("/api/model_config").json()["result"]
        assert got["vlm"]["has_api_key"] is True   # preserved
        assert got["vlm"]["model"] == "m2"

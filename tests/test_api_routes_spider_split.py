"""G7 api_routes/spider_api.py split — TDD tests.

Verify that ``register_spider_routes()`` mounts all 7 spider endpoints
on the FastAPI app, and that they delegate to the spider_lite manager.
Pure unit tests; no real spider, no network.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app_with_spider(spider_lite=None):
    from api_routes.spider_api import register_spider_routes

    app = FastAPI()
    if spider_lite is None:
        spider_lite = MagicMock()
        spider_lite.run.return_value = {"run_id": "test123"}
        spider_lite.list_runs.return_value = []
        spider_lite.cache_state.return_value = {}
        spider_lite.cache_entries.return_value = []
        spider_lite.export_feed.return_value = {"path": "/tmp/test.jsonl"}
        spider_lite.items.return_value = {"rows": [], "total": 0}
        spider_lite.get_run.return_value = {"run_id": "test123", "status": "done"}

    register_spider_routes(app, spider_lite=spider_lite)
    return app, spider_lite


class TestSpiderRouteRegistration:
    def test_all_seven_routes_registered(self):
        app, _ = _make_app_with_spider()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/spider/run" in paths
        assert "/api/spider/runs" in paths
        assert "/api/spider/page_cache/{session_id}" in paths
        assert "/api/spider/page_cache/{session_id}/entries" in paths
        assert "/api/spider/{run_id}/export" in paths
        assert "/api/spider/{run_id}/items" in paths
        assert "/api/spider/{run_id}" in paths


class TestSpiderRunEndpoint:
    def test_run_success(self):
        app, sl = _make_app_with_spider()
        client = TestClient(app)
        resp = client.post("/api/spider/run", json={"url": "https://example.com"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"
        sl.run.assert_called_once()

    def test_run_value_error_returns_400(self):
        sl = MagicMock()
        sl.run.side_effect = ValueError("bad url")
        app, _ = _make_app_with_spider(sl)
        client = TestClient(app)
        resp = client.post("/api/spider/run", json={"url": "bad"})
        assert resp.status_code == 400


class TestSpiderListEndpoint:
    def test_list_runs(self):
        app, sl = _make_app_with_spider()
        client = TestClient(app)
        resp = client.get("/api/spider/runs")
        assert resp.status_code == 200
        assert "runs" in resp.json()
        sl.list_runs.assert_called_once()


class TestSpiderGetRunEndpoint:
    def test_get_run_found(self):
        app, sl = _make_app_with_spider()
        client = TestClient(app)
        resp = client.get("/api/spider/test123")
        assert resp.status_code == 200

    def test_get_run_not_found(self):
        sl = MagicMock()
        sl.get_run.return_value = None
        app, _ = _make_app_with_spider(sl)
        client = TestClient(app)
        resp = client.get("/api/spider/notexist")
        assert resp.status_code == 404


class TestSpiderExportEndpoint:
    def test_export_success(self):
        app, sl = _make_app_with_spider()
        client = TestClient(app)
        resp = client.post("/api/spider/test123/export", json={"format": "csv"})
        assert resp.status_code == 200
        sl.export_feed.assert_called_once()


class TestSpiderItemsEndpoint:
    def test_items_success(self):
        app, sl = _make_app_with_spider()
        client = TestClient(app)
        resp = client.get("/api/spider/test123/items")
        assert resp.status_code == 200
        sl.items.assert_called_once()

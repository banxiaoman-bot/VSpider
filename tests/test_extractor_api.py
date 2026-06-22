"""Extractor API routes (``/api/extractor/run`` Y6, ``/api/extractor/select`` Y22).

Tests use ``starlette.testclient.TestClient`` to exercise the routes
registered via ``api_routes.extractor_api``.  The shared ``_extractor_engine``
instance on ``api_server`` is monkeypatched per-method because the route
handlers capture that same instance reference (injected at register time).
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import api_server


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_server.app, raise_server_exceptions=False)


# ── /api/extractor/run ────────────────────────────────────────────────


class TestRunExtractor:
    def test_success_passes_fields_through(self, client, monkeypatch) -> None:
        captured: dict = {}

        def _extract(source, *, source_type="auto", requested_fields=None, max_rows=1000, all_tables=False):
            captured.update(
                source=source,
                source_type=source_type,
                requested_fields=requested_fields,
                max_rows=max_rows,
                all_tables=all_tables,
            )
            return {"rows": [{"a": 1}]}

        monkeypatch.setattr(api_server._extractor_engine, "extract", _extract)
        resp = client.post(
            "/api/extractor/run",
            json={
                "source": "<table></table>",
                "source_type": "html",
                "requested_fields": ["a"],
                "max_rows": 5,
                "all_tables": True,
            },
        ).json()
        assert resp["status"] == "success"
        assert resp["result"] == {"rows": [{"a": 1}]}
        assert resp["artifact"] is None
        assert captured == {
            "source": "<table></table>",
            "source_type": "html",
            "requested_fields": ["a"],
            "max_rows": 5,
            "all_tables": True,
        }

    def test_export_calls_export_jsonl(self, client, monkeypatch) -> None:
        captured: dict = {}

        monkeypatch.setattr(api_server._extractor_engine, "extract", lambda *a, **k: {"rows": []})

        def _export(result, *, run_id="manual"):
            captured.update(result=result, run_id=run_id)
            return {"path": "x.jsonl"}

        monkeypatch.setattr(api_server._extractor_engine, "export_jsonl", _export)
        resp = client.post(
            "/api/extractor/run",
            json={"source": "x", "export": True, "run_id": "r1"},
        ).json()
        assert resp["artifact"] == {"path": "x.jsonl"}
        assert captured == {"result": {"rows": []}, "run_id": "r1"}

    def test_missing_source_returns_400(self, client) -> None:
        assert client.post("/api/extractor/run", json={}).status_code == 400
        assert client.post("/api/extractor/run", json={"source": ""}).status_code == 400

    def test_value_error_returns_400(self, client, monkeypatch) -> None:
        def _boom(*a, **k):
            raise ValueError("bad source")

        monkeypatch.setattr(api_server._extractor_engine, "extract", _boom)
        assert client.post("/api/extractor/run", json={"source": "x"}).status_code == 400

    def test_unexpected_error_returns_500(self, client, monkeypatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(api_server._extractor_engine, "extract", _boom)
        assert client.post("/api/extractor/run", json={"source": "x"}).status_code == 500


# ── /api/extractor/select ─────────────────────────────────────────────


class TestSelectExtractor:
    def test_success_passes_fields_through(self, client, monkeypatch) -> None:
        captured: dict = {}

        def _select(
            source,
            *,
            selector="",
            selector_type="css",
            mode="all",
            output="text",
            attr="",
            text="",
            regex="",
            tag="",
            max_results=100,
            case_sensitive=False,
        ):
            captured.update(
                source=source,
                selector=selector,
                selector_type=selector_type,
                mode=mode,
                output=output,
                max_results=max_results,
                case_sensitive=case_sensitive,
            )
            return {"matches": ["a", "b"]}

        monkeypatch.setattr(api_server._extractor_engine, "select", _select)
        resp = client.post(
            "/api/extractor/select",
            json={
                "source": "<p>a</p>",
                "selector": "p",
                "type": "css",
                "mode": "first",
                "output": "html",
                "max_results": 3,
                "case_sensitive": True,
            },
        ).json()
        assert resp["status"] == "success"
        assert resp["result"] == {"matches": ["a", "b"]}
        assert captured["source"] == "<p>a</p>"
        assert captured["selector"] == "p"
        assert captured["mode"] == "first"
        assert captured["output"] == "html"
        assert captured["max_results"] == 3
        assert captured["case_sensitive"] is True

    def test_missing_source_returns_400(self, client) -> None:
        assert client.post("/api/extractor/select", json={}).status_code == 400

    def test_value_error_returns_400(self, client, monkeypatch) -> None:
        def _boom(*a, **k):
            raise ValueError("bad selector")

        monkeypatch.setattr(api_server._extractor_engine, "select", _boom)
        assert client.post("/api/extractor/select", json={"source": "x"}).status_code == 400

    def test_unexpected_error_returns_500(self, client, monkeypatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(api_server._extractor_engine, "select", _boom)
        assert client.post("/api/extractor/select", json={"source": "x"}).status_code == 500


# ── Sanity: routes registered on the FastAPI app ──────────────────────


class TestRoutesRegistered:
    def test_all_extractor_routes_present(self) -> None:
        paths = {getattr(r, "path", "") for r in api_server.app.routes}
        assert "/api/extractor/run" in paths
        assert "/api/extractor/select" in paths

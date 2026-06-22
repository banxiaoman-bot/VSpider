"""Y23: ``/api/robots/*`` routes (set / check / reserve / read).

Tests use ``starlette.testclient.TestClient`` to exercise the routes
registered via ``api_routes.robots_api``.  The shared ``_robots_policy``
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


# ── /api/robots/set ───────────────────────────────────────────────────


class TestSetRobots:
    def test_success_passes_fields_through(self, client, monkeypatch) -> None:
        captured: dict = {}

        def _set(domain, text, *, user_agent="*"):
            captured.update(domain=domain, text=text, user_agent=user_agent)
            return {"domain": domain}

        monkeypatch.setattr(api_server._robots_policy, "set_robots", _set)
        resp = client.post(
            "/api/robots/set",
            json={"domain": "x.com", "robots_txt": "User-agent: *", "user_agent": "bot"},
        ).json()
        assert resp["status"] == "success"
        assert resp["result"] == {"domain": "x.com"}
        assert captured == {"domain": "x.com", "text": "User-agent: *", "user_agent": "bot"}

    def test_value_error_returns_400(self, client, monkeypatch) -> None:
        def _boom(*a, **k):
            raise ValueError("domain is required")

        monkeypatch.setattr(api_server._robots_policy, "set_robots", _boom)
        assert client.post("/api/robots/set", json={}).status_code == 400


# ── /api/robots/check ─────────────────────────────────────────────────


class TestCheckRobots:
    def test_success_passes_obey(self, client, monkeypatch) -> None:
        captured: dict = {}

        def _check(url, *, obey=True):
            captured.update(url=url, obey=obey)
            return {"allowed": True}

        monkeypatch.setattr(api_server._robots_policy, "check_url", _check)
        resp = client.post(
            "/api/robots/check", json={"url": "http://x.com/a", "obey": False}
        ).json()
        assert resp["status"] == "success"
        assert resp["result"] == {"allowed": True}
        assert captured == {"url": "http://x.com/a", "obey": False}

    def test_value_error_returns_400(self, client, monkeypatch) -> None:
        def _boom(*a, **k):
            raise ValueError("url required")

        monkeypatch.setattr(api_server._robots_policy, "check_url", _boom)
        assert client.post("/api/robots/check", json={}).status_code == 400


# ── /api/robots/reserve ───────────────────────────────────────────────


class TestReserveRobots:
    def test_success_coerces_default_delay(self, client, monkeypatch) -> None:
        captured: dict = {}

        def _reserve(url, *, obey=True, default_delay=0.0):
            captured.update(url=url, obey=obey, default_delay=default_delay)
            return {"wait": 1.5}

        monkeypatch.setattr(api_server._robots_policy, "reserve_url", _reserve)
        resp = client.post(
            "/api/robots/reserve", json={"url": "http://x.com", "default_delay": 2}
        ).json()
        assert resp["status"] == "success"
        assert resp["result"] == {"wait": 1.5}
        assert captured["default_delay"] == 2.0

    def test_value_error_returns_400(self, client, monkeypatch) -> None:
        def _boom(*a, **k):
            raise ValueError("bad")

        monkeypatch.setattr(api_server._robots_policy, "reserve_url", _boom)
        assert client.post("/api/robots/reserve", json={}).status_code == 400


# ── GET /api/robots/{domain} ──────────────────────────────────────────


class TestReadRobots:
    def test_returns_public_rules(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            api_server._robots_policy,
            "public_rules",
            lambda d: {"domain": d, "rules": []},
        )
        resp = client.get("/api/robots/example.com").json()
        assert resp["status"] == "success"
        assert resp["result"] == {"domain": "example.com", "rules": []}


# ── Sanity: routes registered on the FastAPI app ──────────────────────


class TestRoutesRegistered:
    def test_all_robots_routes_present(self) -> None:
        paths = {getattr(r, "path", "") for r in api_server.app.routes}
        assert "/api/robots/set" in paths
        assert "/api/robots/check" in paths
        assert "/api/robots/reserve" in paths
        assert "/api/robots/{domain}" in paths

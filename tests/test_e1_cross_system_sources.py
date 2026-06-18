"""E1 cross-system execution layer: source-pin + API wiring guards.

These tests pin the *existence* of the E1 surfaces so a future refactor
cannot silently drop them:

- ``visual_web_agent/browser_session_pool.py`` module and its public
  symbols,
- ``workflow_graph._resolve_node_system`` system-attribution helper,
- ``route_executor`` per-step system helpers + result fields,
- the ``GET /api/browser_sessions`` endpoint (verified live through a
  FastAPI ``TestClient``).

They are intentionally lightweight (string + import + one HTTP call) and
do not start a browser.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Module / symbol existence
# ---------------------------------------------------------------------------


class TestBrowserSessionPoolModule:
    def test_module_exports_public_api(self) -> None:
        from visual_web_agent import browser_session_pool as bsp

        for symbol in (
            "VERSION",
            "BrowserSession",
            "BrowserSessionPool",
            "acquire_session",
            "get_active_session",
            "release_session",
            "release_run_sessions",
            "get_browser_session_pool_status",
            "reset_default_session_pool_for_tests",
        ):
            assert hasattr(bsp, symbol), f"browser_session_pool missing {symbol}"

    def test_version_string(self) -> None:
        from visual_web_agent.browser_session_pool import VERSION

        assert VERSION == "browser_session_pool.v1"

    def test_status_snapshot_shape(self) -> None:
        from visual_web_agent.browser_session_pool import get_browser_session_pool_status

        snap = get_browser_session_pool_status()
        assert snap["version"] == "browser_session_pool.v1"
        for key in (
            "max_sessions_per_run",
            "max_total_sessions",
            "active_count",
            "by_run",
            "by_system",
            "sessions",
        ):
            assert key in snap


# ---------------------------------------------------------------------------
# Source pins (string-level so they survive without importing heavy deps)
# ---------------------------------------------------------------------------


class TestE1SourcePins:
    def test_browser_session_pool_source(self) -> None:
        src = (_ROOT / "visual_web_agent" / "browser_session_pool.py").read_text(encoding="utf-8")
        assert "class BrowserSession" in src
        assert "class BrowserSessionPool" in src
        assert "def acquire_session(" in src
        assert "def release_run(" in src
        assert "def release_run_sessions(" in src
        assert "def get_browser_session_pool_status(" in src
        assert "max_sessions_per_run" in src
        assert "max_total_sessions" in src

    def test_workflow_graph_source(self) -> None:
        src = (_ROOT / "visual_web_agent" / "workflow_graph.py").read_text(encoding="utf-8")
        assert "def _resolve_node_system(" in src
        assert "_BROWSER_BOUND_CAPABILITIES" in src
        assert "_NETWORK_BOUND_CAPABILITIES" in src
        assert "_LOGICAL_CAPABILITIES" in src
        # _build_nodes now receives systems + sessions rather than defaults.
        assert "def _build_nodes(" in src
        assert "systems: list[WorkflowSystem]" in src

    def test_route_executor_source(self) -> None:
        src = (_ROOT / "visual_web_agent" / "route_executor.py").read_text(encoding="utf-8")
        assert "def _normalise_per_step_systems(" in src
        assert "def _stamp_system_metadata(" in src
        assert "per_step_systems" in src
        assert '"systems_involved"' in src
        assert '"system_attempts"' in src

    def test_api_server_source(self) -> None:
        src = (_ROOT / "api_server.py").read_text(encoding="utf-8")
        assert (
            "from visual_web_agent.browser_session_pool import get_browser_session_pool_status"
            in src
        )
        tq_src = (_ROOT / "api_routes" / "task_queue_api.py").read_text(encoding="utf-8")
        assert '@app.get("/api/browser_sessions"' in tq_src


# ---------------------------------------------------------------------------
# Live API endpoint
# ---------------------------------------------------------------------------


class TestBrowserSessionsEndpoint:
    def test_endpoint_returns_pool_status(self) -> None:
        import api_server

        client = TestClient(api_server.app)
        resp = client.get("/api/browser_sessions")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "success"
        result = payload["result"]
        assert result["version"] == "browser_session_pool.v1"
        assert "active_count" in result
        assert "by_run" in result
        assert "by_system" in result
        assert isinstance(result["sessions"], list)

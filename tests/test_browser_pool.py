from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from visual_web_agent.browser_pool import BrowserPool, build_browser_runtime_drift, build_browser_runtime_issue_summary, build_browser_runtime_preflight, get_browser_runtime_status, resolve_browser_pool_config


class FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FailingBrowser:
    async def close(self) -> None:
        raise RuntimeError("close failed")


def test_browser_pool_acquire_release() -> None:
    created: list[FakeBrowser] = []

    def factory() -> FakeBrowser:
        browser = FakeBrowser()
        created.append(browser)
        return browser

    pool = BrowserPool(max_contexts=1, env_factory=factory)
    lease = pool.acquire(run_id="run_1")

    assert lease.run_id == "run_1"
    assert pool.status()["active_count"] == 1
    assert pool.status()["available_count"] == 0

    asyncio.run(pool.release(lease))

    assert created[0].closed is True
    status = pool.status()
    assert status["active_count"] == 0
    assert status["available_count"] == 1
    assert status["total_acquired"] == 1
    assert status["total_released"] == 1
    assert status["total_failed"] == 0
    assert lease.status == "released"


def test_browser_pool_exhaustion() -> None:
    pool = BrowserPool(max_contexts=1, env_factory=FakeBrowser)
    pool.acquire(run_id="one")

    try:
        pool.acquire(run_id="two")
    except RuntimeError as exc:
        assert "exhausted" in str(exc)
    else:
        raise AssertionError("expected pool exhaustion")


def test_browser_pool_release_failure_is_recorded() -> None:
    pool = BrowserPool(max_contexts=1, env_factory=FailingBrowser)
    lease = pool.acquire(run_id="bad")

    asyncio.run(pool.release(lease))

    status = pool.status()
    assert status["active_count"] == 0
    assert status["total_failed"] == 1
    assert "RuntimeError" in status["last_error"]
    assert lease.status == "failed"


def test_browser_pool_config_workload_contexts_without_experimental(monkeypatch) -> None:
    monkeypatch.setenv("VSPIDER_BROWSER_MAX_CONTEXTS", "4")
    monkeypatch.delenv("VSPIDER_EXPERIMENTAL_PARALLEL_RUNS", raising=False)

    config = resolve_browser_pool_config(workload_contexts=3)

    assert config["requested_max_contexts"] == 4
    assert config["effective_max_contexts"] == 3
    assert config["parallel_enabled"] is True
    assert config["safety_cap_active"] is True


def test_browser_pool_config_has_safe_default(monkeypatch) -> None:
    monkeypatch.setenv("VSPIDER_BROWSER_MAX_CONTEXTS", "4")
    monkeypatch.delenv("VSPIDER_EXPERIMENTAL_PARALLEL_RUNS", raising=False)

    config = resolve_browser_pool_config()

    assert config["requested_max_contexts"] == 4
    assert config["effective_max_contexts"] == 1
    assert config["parallel_enabled"] is False
    assert config["safety_cap_active"] is True


def test_browser_pool_config_allows_experimental_parallel(monkeypatch) -> None:
    monkeypatch.setenv("VSPIDER_BROWSER_MAX_CONTEXTS", "3")
    monkeypatch.setenv("VSPIDER_EXPERIMENTAL_PARALLEL_RUNS", "true")

    config = resolve_browser_pool_config()

    assert config["requested_max_contexts"] == 3
    assert config["effective_max_contexts"] == 3
    assert config["parallel_enabled"] is True
    assert config["safety_cap_active"] is False


def test_browser_pool_status_exposes_safety_fields() -> None:
    pool = BrowserPool(
        max_contexts=1,
        requested_max_contexts=4,
        parallel_enabled=False,
        safety_note="guarded",
        env_factory=FakeBrowser,
    )

    status = pool.status()

    assert status["max_contexts"] == 1
    assert status["requested_max_contexts"] == 4
    assert status["parallel_enabled"] is False
    assert status["safety_cap_active"] is True
    assert status["safety_note"] == "guarded"


def test_browser_runtime_status_combines_pool_and_backend() -> None:
    runtime = get_browser_runtime_status(
        pool_status={
            "max_contexts": 2,
            "active_count": 1,
            "available_count": 1,
            "parallel_enabled": True,
            "safety_cap_active": False,
        },
        backend_status={
            "active": {
                "name": "remote_playwright",
                "kind": "playwright_remote",
                "transport": "ws",
                "supports_remote": True,
                "status": "available",
            },
            "available": [
                {"name": "playwright_chromium", "status": "available", "supports_remote": False},
                {"name": "remote_playwright", "status": "available", "supports_remote": True},
            ],
            "health": {
                "status": "healthy",
                "reachable": True,
                "check_kind": "tcp_probe",
                "latency_ms": 12.5,
                "cache": {"hit": True, "stale": False, "age_s": 1.25, "ttl_s": 5.0},
            },
            "session_count": 3,
        },
    )

    assert runtime["version"] == "browser_runtime.v1"
    assert runtime["status"] == "available"
    assert runtime["capacity"]["active_contexts"] == 1
    assert runtime["capacity"]["backend_session_count"] == 3
    assert runtime["backend_summary"]["active_name"] == "remote_playwright"
    assert runtime["backend_summary"]["remote_available"] is True
    assert runtime["backend_summary"]["health_status"] == "healthy"
    assert runtime["backend_summary"]["health_reachable"] is True
    assert runtime["backend_summary"]["health_check_kind"] == "tcp_probe"
    assert runtime["backend_summary"]["health_latency_ms"] == 12.5
    assert runtime["backend_summary"]["health_cache_hit"] is True
    assert runtime["backend_summary"]["health_cache_stale"] is False
    assert runtime["backend_summary"]["health_cache_age_s"] == 1.25
    assert runtime["backend_summary"]["health_cache_ttl_s"] == 5.0


def test_browser_runtime_status_detects_pool_exhaustion() -> None:
    runtime = get_browser_runtime_status(
        pool_status={"max_contexts": 1, "active_count": 1, "available_count": 0},
        backend_status={"active": {"name": "playwright_chromium", "status": "available"}, "available": [], "session_count": 0},
    )

    assert runtime["status"] == "pool_exhausted"


def test_browser_runtime_status_detects_backend_unhealthy() -> None:
    runtime = get_browser_runtime_status(
        pool_status={"max_contexts": 2, "active_count": 0, "available_count": 2},
        backend_status={
            "active": {"name": "remote_playwright", "status": "available"},
            "health": {"status": "unhealthy", "reachable": False, "check_kind": "tcp_probe", "error": "refused"},
            "available": [],
            "session_count": 0,
        },
    )

    assert runtime["status"] == "backend_unhealthy"
    assert runtime["backend_summary"]["health_error"] == "refused"


def test_browser_runtime_preflight_warns_without_blocking() -> None:
    runtime = get_browser_runtime_status(
        pool_status={"max_contexts": 2, "active_count": 0, "available_count": 2},
        backend_status={
            "active": {"name": "remote_playwright", "status": "available"},
            "health": {
                "status": "unhealthy",
                "reachable": False,
                "check_kind": "tcp_probe",
                "cache": {"hit": True, "stale": True, "age_s": 6.0, "ttl_s": 5.0},
            },
            "available": [],
            "session_count": 0,
        },
    )

    preflight = build_browser_runtime_preflight(runtime)

    assert preflight["version"] == "browser_runtime_preflight.v1"
    assert preflight["status"] == "warn"
    assert preflight["blocking"] is False
    assert preflight["recommended_action"] == "check_browser_backend_health"
    assert "backend_health_unhealthy" in preflight["warnings"]
    assert "backend_health_cache_stale" in preflight["warnings"]
    assert preflight["runtime_snapshot"]["version"] == "browser_runtime.v1"


def test_browser_runtime_drift_summarizes_non_blocking_regressions() -> None:
    before = get_browser_runtime_status(
        pool_status={"max_contexts": 2, "active_count": 0, "available_count": 2},
        backend_status={
            "active": {"name": "playwright", "status": "available"},
            "health": {"status": "healthy", "cache": {"stale": False}},
            "available": [],
            "session_count": 0,
        },
    )
    after = get_browser_runtime_status(
        pool_status={"max_contexts": 2, "active_count": 2, "available_count": 0},
        backend_status={
            "active": {"name": "playwright", "status": "available"},
            "health": {"status": "healthy", "cache": {"stale": True}},
            "available": [],
            "session_count": 2,
        },
    )

    drift = build_browser_runtime_drift(before, after)

    assert drift["version"] == "browser_runtime_drift.v1"
    assert drift["status"] == "warn"
    assert drift["blocking"] is False
    assert drift["deltas"]["available_contexts"] == -2
    assert drift["recommended_action"] == "queue_or_wait_for_browser_context"
    assert "pool_capacity_exhausted_during_execute" in drift["warnings"]
    assert any(change["field"] == "cache_stale" for change in drift["changes"])


def test_browser_runtime_issue_summary_prioritizes_after_and_drift_actions() -> None:
    before = {"version": "browser_runtime_preflight.v1", "status": "pass", "warnings": [], "recommended_action": "continue"}
    after = {
        "version": "browser_runtime_preflight.v1",
        "status": "warn",
        "warnings": ["backend_health_cache_stale"],
        "recommended_action": "refresh_browser_runtime_status",
    }
    drift = {
        "version": "browser_runtime_drift.v1",
        "status": "warn",
        "warnings": ["pool_capacity_exhausted_during_execute"],
        "recommended_action": "queue_or_wait_for_browser_context",
    }

    summary = build_browser_runtime_issue_summary(before_preflight=before, after_preflight=after, drift=drift)

    assert summary["version"] == "browser_runtime_issue_summary.v1"
    assert summary["status"] == "warn"
    assert summary["blocking"] is False
    assert summary["issue_count"] == 2
    assert summary["recommended_action"] == "refresh_browser_runtime_status"
    assert summary["recommended_actions"] == ["refresh_browser_runtime_status", "queue_or_wait_for_browser_context"]
    assert summary["warnings_by_source"]["runtime_drift"] == ["pool_capacity_exhausted_during_execute"]


def test_browser_pool_api_status() -> None:
    import api_server

    client = TestClient(api_server.app)
    resp = client.get("/api/browser_pool")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "success"
    assert "active_count" in payload["pool"]
    assert "max_contexts" in payload["pool"]
    assert payload["runtime"]["version"] == "browser_runtime.v1"
    assert "backend_summary" in payload["runtime"]


def test_browser_pool_source_wiring() -> None:
    root = Path(__file__).resolve().parent.parent
    main_src = (root / "visual_web_agent" / "main.py").read_text(encoding="utf-8")
    api_src = (root / "api_server.py").read_text(encoding="utf-8")
    pool_src = (root / "visual_web_agent" / "browser_pool.py").read_text(encoding="utf-8")
    app_src = (root / "vspider-ui" / "src" / "App.vue").read_text(encoding="utf-8")
    runtime_panel_src = (root / "vspider-ui" / "src" / "components" / "CapabilityRuntimePanel.vue").read_text(encoding="utf-8")

    assert "from .browser_pool import acquire_browser, release_browser" in main_src
    assert "browser_lease = acquire_browser(run_id=_run_ts)" in main_src
    assert "browser = browser_lease.browser" in main_src
    assert "await release_browser(" in main_src
    assert "from visual_web_agent.browser_pool import get_browser_pool_status as _get_browser_pool_status" in api_src
    assert "from visual_web_agent.browser_pool import get_browser_runtime_status as _get_browser_runtime_status" in api_src
    assert '@app.get("/api/browser_pool"' in api_src
    assert '"runtime": _get_browser_runtime_status(pool_status=pool, backend_status=backend)' in api_src
    assert "class BrowserPool" in pool_src
    assert "max_contexts: int = 1" in pool_src
    assert "def resolve_browser_pool_config(*, workload_contexts: int = 1)" in pool_src
    assert "def get_browser_runtime_status(" in pool_src
    assert "def build_browser_runtime_preflight(" in pool_src
    assert "def build_browser_runtime_drift(" in pool_src
    assert "def build_browser_runtime_issue_summary(" in pool_src
    assert '"version": "browser_runtime_preflight.v1"' in pool_src
    assert '"version": "browser_runtime_drift.v1"' in pool_src
    assert '"version": "browser_runtime_issue_summary.v1"' in pool_src
    assert '"version": "browser_runtime.v1"' in pool_src
    assert "remote_available" in pool_src
    assert "health_status" in pool_src
    assert "health_cache_hit" in pool_src
    assert "backend_unhealthy" in pool_src
    assert "VSPIDER_BROWSER_MAX_CONTEXTS" in pool_src
    assert "VSPIDER_EXPERIMENTAL_PARALLEL_RUNS" in pool_src
    assert "const browserRuntimeStatus = ref(null)" in app_src
    assert "const fetchBrowserRuntimeStatus = async () => {" in app_src
    assert "/api/browser_pool" in app_src
    assert "const browserRuntimeStatusClass = computed(" in app_src
    assert "const browserRuntimeHealthLabel = computed(" in app_src
    assert "const browserRuntimeHealthCacheLabel = computed(" in app_src
    assert "import CapabilityRuntimePanel from './components/CapabilityRuntimePanel.vue'" in app_src
    assert "<CapabilityRuntimePanel" in app_src
    assert "Browser Runtime" in runtime_panel_src
    assert "<span>health</span>" in runtime_panel_src
    assert "browser-runtime-card" in runtime_panel_src

from __future__ import annotations

from visual_web_agent.browser_pool import get_browser_runtime_status
from visual_web_agent.browser_runtime_doctor import build_browser_runtime_doctor


def _runtime(
    *,
    max_contexts: int = 2,
    active_count: int = 0,
    available_count: int = 2,
    backend_name: str = "playwright_chromium",
    backend_status: str = "available",
    health_status: str = "healthy",
    reachable: bool | None = True,
    cache_stale: bool = False,
    session_count: int = 0,
) -> dict:
    return get_browser_runtime_status(
        pool_status={
            "max_contexts": max_contexts,
            "active_count": active_count,
            "available_count": available_count,
            "parallel_enabled": max_contexts > 1,
            "safety_cap_active": False,
            "total_failed": 0,
            "last_error": "",
        },
        backend_status={
            "active": {"name": backend_name, "status": backend_status, "supports_remote": backend_name == "remote_playwright"},
            "available": [
                {"name": "playwright_chromium", "status": "available", "supports_remote": False},
                {"name": "remote_playwright", "status": "available", "supports_remote": True},
            ],
            "health": {
                "status": health_status,
                "reachable": reachable,
                "check_kind": "tcp_probe" if backend_name == "remote_playwright" else "local_metadata",
                "error": "connection refused" if health_status == "unhealthy" else "",
                "cache": {"hit": True, "stale": cache_stale, "age_s": 7.0 if cache_stale else 1.0, "ttl_s": 5.0},
            },
            "session_count": session_count,
        },
    )


def test_browser_runtime_doctor_marks_healthy_runtime_ready() -> None:
    report = build_browser_runtime_doctor({"runtime_status": _runtime()})

    assert report["version"] == "browser_runtime_doctor_report.v1"
    assert report["source"] == "browser_runtime_doctor"
    assert report["status"] == "healthy"
    assert report["blocking"] is False
    assert report["runtime_status"] == "available"
    assert report["readiness"]["ready_for_browser_actions"] is True
    assert report["readiness"]["can_start_new_context"] is True
    assert report["readiness"]["ready_for_parallel"] is True
    assert report["summary"]["backend_health"] == "healthy"
    assert report["recommended_actions"] == ["continue"]
    assert all(check["passed"] for check in report["checks"])


def test_browser_runtime_doctor_blocks_unhealthy_exhausted_runtime() -> None:
    runtime = _runtime(
        max_contexts=1,
        active_count=1,
        available_count=0,
        backend_name="remote_playwright",
        health_status="unhealthy",
        reachable=False,
        cache_stale=True,
        session_count=2,
    )

    report = build_browser_runtime_doctor({"runtime_status": runtime})

    assert report["status"] == "blocked"
    assert report["blocking"] is True
    assert report["readiness"]["ready_for_browser_actions"] is False
    assert report["readiness"]["can_start_new_context"] is False
    assert "backend_health_unhealthy" in report["error_codes"]
    assert "browser_pool_exhausted" in report["error_codes"]
    assert "backend_health_cache_stale" in report["warning_codes"]
    assert "backend_session_count_exceeds_pool_capacity" in report["warning_codes"]
    assert report["recommended_action"] == "check_browser_backend_health"
    assert "queue_or_wait_for_browser_context" in report["recommended_actions"]
    assert report["summary"]["error_count"] == 2
    assert report["summary"]["warning_count"] >= 2
    assert report["recovery_plan"][0]["source"] == "runtime"


def test_browser_runtime_doctor_uses_drift_and_issue_summary() -> None:
    before = _runtime(max_contexts=2, active_count=0, available_count=2)
    after = _runtime(max_contexts=2, active_count=2, available_count=0, cache_stale=True, session_count=2)

    report = build_browser_runtime_doctor({"before_status": before, "after_status": after})

    assert report["status"] == "blocked"
    assert "runtime_drift_regressed" in report["error_codes"]
    assert "runtime_issue_summary_warn" in report["warning_codes"]
    assert "queue_or_wait_for_browser_context" in report["recommended_actions"]
    assert report["evidence"]["drift"]["version"] == "browser_runtime_drift.v1"
    assert "pool_capacity_exhausted_during_execute" in report["evidence"]["drift"]["warnings"]
    assert report["evidence"]["issue_summary"]["version"] == "browser_runtime_issue_summary.v1"


def test_browser_runtime_doctor_can_build_runtime_from_pool_and_backend() -> None:
    report = build_browser_runtime_doctor({
        "pool_status": {"max_contexts": 1, "active_count": 0, "available_count": 1},
        "backend_status": {
            "active": {"name": "playwright_chromium", "status": "available"},
            "health": {"status": "healthy", "reachable": True},
            "available": [],
            "session_count": 0,
        },
    })

    assert report["runtime_status"] == "available"
    assert report["readiness"]["can_start_new_context"] is True
    assert report["evidence"]["runtime"]["version"] == "browser_runtime.v1"


def test_browser_runtime_doctor_api_post_and_get() -> None:
    import api_server
    from fastapi.testclient import TestClient

    client = TestClient(api_server.app)
    post_resp = client.post("/api/capabilities/browser_runtime_doctor", json={"runtime_status": _runtime()})
    get_resp = client.get("/api/capabilities/browser_runtime_doctor")

    assert post_resp.status_code == 200
    assert post_resp.json()["result"]["report"]["version"] == "browser_runtime_doctor_report.v1"
    assert post_resp.json()["result"]["report"]["status"] == "healthy"
    assert get_resp.status_code == 200
    assert get_resp.json()["result"]["report"]["version"] == "browser_runtime_doctor_report.v1"
    assert "readiness" in get_resp.json()["result"]["report"]

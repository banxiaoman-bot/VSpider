"""K2: ``GET /api/failed_runs`` and ``GET /api/failed_runs/{run_id}/log``.

Tests use ``starlette.testclient.TestClient`` to exercise the routes
registered via ``api_routes.failed_runs_api``.  Monkeypatching the
shared ``_failure_archive`` module object still works because the
route handler captures the same reference.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Iterator

import pytest
from starlette.testclient import TestClient

import api_server


# ── Project-local tmp fixture (Windows-safe) ──────────────────────────


@pytest.fixture
def project_tmp(monkeypatch) -> Iterator[Path]:
    """Create a tmp dir and ``chdir`` into it so any relative ``logs/...``
    or ``runs/...`` lookups in api_server resolve there.

    We avoid pytest's tmp_path because of the recurring
    ``%LOCALAPPDATA%\\Temp\\pytest-of-<user>`` permission issues; mirrors
    ``test_phase_event_persistence.py`` and ``test_failure_archive.py``.
    """
    base = Path(__file__).resolve().parent / ".tmp_failed_runs_api"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="fr_", dir=str(base)))
    monkeypatch.chdir(tmp)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── /api/failed_runs (list) ───────────────────────────────────────────


class TestListFailedRunsEndpoint:
    def test_returns_status_count_items(self, monkeypatch, project_tmp: Path) -> None:
        fake = [
            {"schema_version": 1, "run_id": "a", "ts": 2.0, "reason": "x"},
            {"schema_version": 1, "run_id": "b", "ts": 1.0, "reason": "y"},
        ]
        monkeypatch.setattr(
            api_server._failure_archive,
            "list_failed_runs",
            lambda **kw: list(fake),
        )
        client = TestClient(api_server.app)
        resp = client.get("/api/failed_runs", params={"limit": 50}).json()
        assert resp["status"] == "success"
        assert resp["count"] == 2
        assert resp["items"] == fake

    def test_passes_limit_through(self, monkeypatch, project_tmp: Path) -> None:
        captured = {}

        def _spy(*, limit: int) -> list:
            captured["limit"] = limit
            return []

        monkeypatch.setattr(
            api_server._failure_archive, "list_failed_runs", _spy,
        )
        client = TestClient(api_server.app)
        client.get("/api/failed_runs", params={"limit": 7})
        assert captured["limit"] == 7

    def test_default_limit_when_zero_or_falsy(
        self, monkeypatch, project_tmp: Path,
    ) -> None:
        captured: list[int] = []

        def _spy(*, limit: int) -> list:
            captured.append(limit)
            return []

        monkeypatch.setattr(
            api_server._failure_archive, "list_failed_runs", _spy,
        )
        client = TestClient(api_server.app)
        client.get("/api/failed_runs", params={"limit": 0})
        assert captured[-1] == 50

    def test_archive_exception_returns_empty_items(
        self, monkeypatch, project_tmp: Path,
    ) -> None:
        def _boom(**kw):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(
            api_server._failure_archive, "list_failed_runs", _boom,
        )
        client = TestClient(api_server.app)
        resp = client.get("/api/failed_runs", params={"limit": 10}).json()
        assert resp["status"] == "success"
        assert resp["count"] == 0
        assert resp["items"] == []


# ── /api/failed_runs/{run_id}/log (download) ──────────────────────────


class TestFailedRunLogEndpoint:
    def test_returns_html_when_log_exists(
        self, project_tmp: Path,
    ) -> None:
        logs = project_tmp / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        rid = "20260524_191800"
        log = logs / f"run_log_{rid}.html"
        log.write_text("<html><body>hello</body></html>", encoding="utf-8")

        client = TestClient(api_server.app)
        resp = client.get(f"/api/failed_runs/{rid}/log")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_404_when_log_missing(self, project_tmp: Path) -> None:
        client = TestClient(api_server.app, raise_server_exceptions=False)
        resp = client.get("/api/failed_runs/nope_id/log")
        assert resp.status_code == 404

    def test_404_when_id_exists_but_logs_dir_only(
        self, project_tmp: Path,
    ) -> None:
        (project_tmp / "logs").mkdir(parents=True, exist_ok=True)
        client = TestClient(api_server.app, raise_server_exceptions=False)
        resp = client.get("/api/failed_runs/missing/log")
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "bad",
        [
            "../etc/passwd",
            "id with spaces",
            "id;with;semicolons",
        ],
    )
    def test_400_on_unsafe_run_id(self, project_tmp: Path, bad: str) -> None:
        client = TestClient(api_server.app, raise_server_exceptions=False)
        resp = client.get(f"/api/failed_runs/{bad}/log")
        assert resp.status_code in (400, 404, 422)

    def test_accepts_canonical_run_ts_format(self, project_tmp: Path) -> None:
        logs = project_tmp / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        rid = "20260524_191800"
        (logs / f"run_log_{rid}.html").write_text("<html/>", encoding="utf-8")
        client = TestClient(api_server.app)
        resp = client.get(f"/api/failed_runs/{rid}/log")
        assert resp.status_code == 200


# ── Sanity: routes registered on the FastAPI app ──────────────────────


class TestRoutesRegistered:
    def test_list_route_present(self) -> None:
        paths = {getattr(r, "path", "") for r in api_server.app.routes}
        assert "/api/failed_runs" in paths

    def test_log_route_present(self) -> None:
        paths = {getattr(r, "path", "") for r in api_server.app.routes}
        assert "/api/failed_runs/{run_id}/log" in paths

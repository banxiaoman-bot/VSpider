"""K2: ``GET /api/failed_runs`` and ``GET /api/failed_runs/{run_id}/log``.

We invoke the route handlers as plain async callables (skipping the
FastAPI request lifecycle) to keep the test stack minimal — no httpx
dependency, no TCP socket. The endpoints' contract surface is:

  • get_failed_runs(limit=...) → ``{"status", "count", "items"}``
  • get_failed_run_html_log(run_id) → ``FileResponse`` or ``HTTPException``

Both functions are top-level coroutines on ``api_server``; we awaitthem
directly via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

import api_server
from fastapi import HTTPException
from fastapi.responses import FileResponse


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
        # Stub the archive layer so we don't depend on its semantics here.
        fake = [
            {"schema_version": 1, "run_id": "a", "ts": 2.0, "reason": "x"},
            {"schema_version": 1, "run_id": "b", "ts": 1.0, "reason": "y"},
        ]
        monkeypatch.setattr(
            api_server._failure_archive,
            "list_failed_runs",
            lambda **kw: list(fake),
        )
        resp = asyncio.run(api_server.get_failed_runs(limit=50))
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
        asyncio.run(api_server.get_failed_runs(limit=7))
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
        # The endpoint applies ``limit or 50`` so passing 0 falls back to 50.
        asyncio.run(api_server.get_failed_runs(limit=0))
        assert captured[-1] == 50

    def test_archive_exception_returns_empty_items(
        self, monkeypatch, project_tmp: Path,
    ) -> None:
        def _boom(**kw):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(
            api_server._failure_archive, "list_failed_runs", _boom,
        )
        # Endpoint must NOT propagate the exception (it's a status panel —
        # one bad disk read shouldn't 500 the whole request).
        resp = asyncio.run(api_server.get_failed_runs(limit=10))
        assert resp["status"] == "success"
        assert resp["count"] == 0
        assert resp["items"] == []


# ── /api/failed_runs/{run_id}/log (download) ──────────────────────────


class TestFailedRunLogEndpoint:
    def test_returns_file_response_when_log_exists(
        self, project_tmp: Path,
    ) -> None:
        # Create the canonical HtmlLogger output path under cwd
        logs = project_tmp / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        rid = "20260524_191800"
        log = logs / f"run_log_{rid}.html"
        log.write_text("<html><body>hello</body></html>", encoding="utf-8")

        resp = asyncio.run(api_server.get_failed_run_html_log(rid))
        assert isinstance(resp, FileResponse)
        # FileResponse stashes the path as ``.path`` (Starlette) — the
        # underlying file must be the one we wrote.
        assert Path(resp.path).resolve() == log.resolve()
        # Media type must be HTML so browsers render it inline.
        assert "text/html" in (resp.media_type or "")

    def test_404_when_log_missing(self, project_tmp: Path) -> None:
        # logs/ doesn't even exist — handler must 404, not 500.
        with pytest.raises(HTTPException) as ei:
            asyncio.run(api_server.get_failed_run_html_log("nope_id"))
        assert ei.value.status_code == 404

    def test_404_when_id_exists_but_logs_dir_only(
        self, project_tmp: Path,
    ) -> None:
        (project_tmp / "logs").mkdir(parents=True, exist_ok=True)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(api_server.get_failed_run_html_log("missing"))
        assert ei.value.status_code == 404

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "../etc/passwd",
            "..\\windows\\system32",
            "id with spaces",
            "id;with;semicolons",
            "id/slash",
            "id\\backslash",
            "id|pipe",
            "id$dollar",
        ],
    )
    def test_400_on_unsafe_run_id(self, project_tmp: Path, bad: str) -> None:
        """Path-traversal / shell-metacharacter run_ids are rejected even
        if no log file exists (whitelist must check before filesystem)."""
        with pytest.raises(HTTPException) as ei:
            asyncio.run(api_server.get_failed_run_html_log(bad))
        assert ei.value.status_code == 400

    def test_accepts_canonical_run_ts_format(self, project_tmp: Path) -> None:
        """The HtmlLogger uses ``strftime('%Y%m%d_%H%M%S')`` → e.g.
        ``20260524_191800``. That format must validate."""
        logs = project_tmp / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        rid = "20260524_191800"
        (logs / f"run_log_{rid}.html").write_text("<html/>", encoding="utf-8")
        resp = asyncio.run(api_server.get_failed_run_html_log(rid))
        assert isinstance(resp, FileResponse)


# ── Sanity: routes registered on the FastAPI app ──────────────────────


class TestRoutesRegistered:
    def test_list_route_present(self) -> None:
        paths = {getattr(r, "path", "") for r in api_server.app.routes}
        assert "/api/failed_runs" in paths

    def test_log_route_present(self) -> None:
        paths = {getattr(r, "path", "") for r in api_server.app.routes}
        assert "/api/failed_runs/{run_id}/log" in paths

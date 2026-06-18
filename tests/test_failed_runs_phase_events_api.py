"""K6: ``GET /api/failed_runs/{run_id}/phase_events`` endpoint tests.

The endpoint reads ``logs/phase_<run_id>.jsonl`` and returns parsed
events as JSON. We test it as a plain async callable (same approach as
``test_failed_runs_api.py``) — no FastAPI request lifecycle / TCP needed.

Contract under test:
  * Valid id + present file -> ``{status, count, total, truncated, events}``
  * Invalid run_id (path traversal / metacharacters)   -> HTTPException(400)
  * Missing phase log file                              -> HTTPException(404)
  * Empty/blank lines in the JSONL are ignored
  * Malformed lines are counted in `total` but NOT returned in `events`
  * `limit` <= 0 falls back to the default (200)
  * When `total > limit`, the LAST `limit` events are returned (tail
    is the most diagnostically useful slice near the failure point)
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

import pytest
from starlette.testclient import TestClient

import api_server


# ── Project-local tmp fixture (Windows-safe; mirrors K2 tests) ────────


@pytest.fixture
def project_tmp(monkeypatch) -> Iterator[Path]:
    base = Path(__file__).resolve().parent / ".tmp_phase_events_api"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pe_", dir=str(base)))
    monkeypatch.chdir(tmp)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _seed_phase_log(
    project_tmp: Path,
    run_id: str,
    events: list[dict],
    *,
    extra_lines: list[str] | None = None,
) -> Path:
    """Write a logs/phase_<run_id>.jsonl file mixing valid + extra lines.

    ``extra_lines`` lets a test inject blank lines or malformed JSON to
    verify the parser's tolerance.
    """
    logs = project_tmp / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"phase_{run_id}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for evt in events:
            fh.write(json.dumps(evt, ensure_ascii=False) + "\n")
        if extra_lines:
            for line in extra_lines:
                fh.write(line + "\n")
    return path


# ── Happy path ────────────────────────────────────────────────────────


class TestPhaseEventsHappyPath:
    def test_returns_status_count_total_events(self, project_tmp: Path) -> None:
        events = [
            {"type": "phase", "phase": "som_inject", "step": 1, "ts": 1.0},
            {"type": "phase", "phase": "vlm_call", "step": 1, "ts": 1.5},
            {"type": "phase", "phase": "action", "step": 2, "ts": 2.0},
        ]
        _seed_phase_log(project_tmp, "20260524_120000", events)

        client = TestClient(api_server.app)
        resp = client.get("/api/failed_runs/20260524_120000/phase_events").json()
        assert resp["status"] == "success"
        assert resp["count"] == 3
        assert resp["total"] == 3
        assert resp["truncated"] is False
        assert [e["phase"] for e in resp["events"]] == [
            "som_inject", "vlm_call", "action",
        ]

    def test_empty_file_returns_empty_events(self, project_tmp: Path) -> None:
        _seed_phase_log(project_tmp, "empty_run", events=[])
        client = TestClient(api_server.app)
        resp = client.get("/api/failed_runs/empty_run/phase_events").json()
        assert resp == {
            "status": "success",
            "count": 0,
            "total": 0,
            "truncated": False,
            "events": [],
        }


# ── Tolerance: blank + malformed lines ───────────────────────────────


class TestPhaseEventsTolerance:
    def test_skips_blank_lines(self, project_tmp: Path) -> None:
        events = [{"type": "phase", "phase": "p1"}]
        _seed_phase_log(
            project_tmp, "blanks", events,
            extra_lines=["", "   ", "\t"],
        )
        client = TestClient(api_server.app)
        resp = client.get("/api/failed_runs/blanks/phase_events").json()
        assert resp["count"] == 1
        assert resp["total"] == 1

    def test_malformed_lines_counted_in_total_but_not_events(
        self, project_tmp: Path,
    ) -> None:
        events = [
            {"type": "phase", "phase": "good1"},
            {"type": "phase", "phase": "good2"},
        ]
        _seed_phase_log(
            project_tmp, "malformed", events,
            extra_lines=["{this is not json", "[1, 2, 3", "not even close"],
        )
        client = TestClient(api_server.app)
        resp = client.get("/api/failed_runs/malformed/phase_events").json()
        assert resp["count"] == 2
        assert resp["total"] == 5
        assert {e["phase"] for e in resp["events"]} == {"good1", "good2"}

    def test_non_dict_json_lines_are_dropped(
        self, project_tmp: Path,
    ) -> None:
        good = [{"type": "phase", "phase": "p"}]
        _seed_phase_log(
            project_tmp, "non_dict", good,
            extra_lines=["[1, 2, 3]", "42", '"hello"', "null"],
        )
        client = TestClient(api_server.app)
        resp = client.get("/api/failed_runs/non_dict/phase_events").json()
        assert resp["count"] == 1
        assert resp["total"] == 5


# ── Limit / truncation ────────────────────────────────────────────────


class TestPhaseEventsLimit:
    def test_returns_tail_when_truncated(self, project_tmp: Path) -> None:
        events = [
            {"type": "phase", "phase": f"p{i}", "step": i, "ts": float(i)}
            for i in range(10)
        ]
        _seed_phase_log(project_tmp, "trunc", events)

        client = TestClient(api_server.app)
        resp = client.get(
            "/api/failed_runs/trunc/phase_events", params={"limit": 3}
        ).json()
        assert resp["count"] == 3
        assert resp["total"] == 10
        assert resp["truncated"] is True
        assert [e["phase"] for e in resp["events"]] == ["p7", "p8", "p9"]

    def test_limit_zero_falls_back_to_default(self, project_tmp: Path) -> None:
        events = [{"type": "phase", "phase": f"p{i}"} for i in range(5)]
        _seed_phase_log(project_tmp, "lzero", events)
        client = TestClient(api_server.app)
        resp = client.get(
            "/api/failed_runs/lzero/phase_events", params={"limit": 0}
        ).json()
        assert resp["count"] == 5
        assert resp["truncated"] is False

    def test_limit_negative_falls_back_to_default(
        self, project_tmp: Path,
    ) -> None:
        events = [{"type": "phase", "phase": "p"}]
        _seed_phase_log(project_tmp, "lneg", events)
        client = TestClient(api_server.app)
        resp = client.get(
            "/api/failed_runs/lneg/phase_events", params={"limit": -99}
        ).json()
        assert resp["count"] == 1

    def test_limit_larger_than_total_does_not_set_truncated(
        self, project_tmp: Path,
    ) -> None:
        events = [{"type": "phase", "phase": "p"} for _ in range(3)]
        _seed_phase_log(project_tmp, "lover", events)
        client = TestClient(api_server.app)
        resp = client.get(
            "/api/failed_runs/lover/phase_events", params={"limit": 999}
        ).json()
        assert resp["count"] == 3
        assert resp["total"] == 3
        assert resp["truncated"] is False


# ── Error paths ───────────────────────────────────────────────────────


class TestPhaseEventsErrors:
    def test_404_when_file_missing(self, project_tmp: Path) -> None:
        client = TestClient(api_server.app, raise_server_exceptions=False)
        resp = client.get("/api/failed_runs/does_not_exist/phase_events")
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
        resp = client.get(f"/api/failed_runs/{bad}/phase_events")
        assert resp.status_code in (400, 404, 422)

    def test_accepts_canonical_run_ts_format(self, project_tmp: Path) -> None:
        _seed_phase_log(
            project_tmp,
            "20260524_191800",
            events=[{"type": "phase", "phase": "p"}],
        )
        client = TestClient(api_server.app)
        resp = client.get("/api/failed_runs/20260524_191800/phase_events").json()
        assert resp["count"] == 1


# ── Route registration ────────────────────────────────────────────────


class TestPhaseEventsRouteRegistered:
    def test_route_present(self) -> None:
        paths = {getattr(r, "path", "") for r in api_server.app.routes}
        assert "/api/failed_runs/{run_id}/phase_events" in paths

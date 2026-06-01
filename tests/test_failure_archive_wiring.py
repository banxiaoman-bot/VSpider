"""K4: integration between ``main.py`` run lifecycle and
``failure_archive.record_failure``.

We don't spin up Playwright or FastAPI here. The tests exercise the
public hooks on ``visual_web_agent.main`` directly:

  • ``_record_run_start(run_id=..., goal=..., started_at=...)``
  • ``_record_run_step(N)``
  • ``_reset_run_answer()`` clears K4 state along with answer state
  • ``_broadcast_done_safe(False, ...)`` → writes the archive file
  • ``_broadcast_done_safe(True, ...)`` → does NOT write
  • ``_broadcast_done_safe(False, ...)`` with no run_id → silently skipped
  • archive disk failure does NOT break the broadcast path

Side effects we have to mute:
  • ``broadcast_done`` / ``broadcast_phase`` ping FastAPI — replace with no-ops.
  • The archive writes under ``<project_root>/runs/failed/`` by default;
    we monkey-patch ``failure_archive._project_root`` to a tmp dir.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterator

import pytest

from visual_web_agent import failure_archive as fa
from visual_web_agent import main as vmain


# ── Project-local tmp + module-state reset fixture ────────────────────


@pytest.fixture
def isolated_run(monkeypatch) -> Iterator[Path]:
    """Project-local tmp dir + clean K4 state at entry and exit.

    Why project-local? pytest's tmp_path hits ``%LOCALAPPDATA%\\Temp``
    permission denials on this Windows host (same reason
    ``test_failure_archive.py`` rolls its own).

    Why monkeypatch ``_project_root``? ``record_failure`` defaults its
    base_dir to ``<project_root>/runs``. Pointing _project_root at our
    tmp means we don't pollute the real repo's runs/ tree.
    """
    base = Path(__file__).resolve().parent / ".tmp_fa_wiring"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="wire_", dir=str(base)))
    monkeypatch.setattr(fa, "_project_root", lambda: tmp)

    # Silence the WS broadcast layer — no FastAPI loop here.
    import api_server

    monkeypatch.setattr(api_server, "broadcast_done", lambda *a, **kw: None)
    monkeypatch.setattr(api_server, "broadcast_phase", lambda *a, **kw: None)

    # Reset module state before the test
    vmain._reset_run_answer()
    try:
        yield tmp
    finally:
        # Reset again so other tests in the session don't see leftovers
        vmain._reset_run_answer()
        shutil.rmtree(tmp, ignore_errors=True)


# ── Pure-state helpers ────────────────────────────────────────────────


class TestRecordRunStart:
    def test_sets_module_state(self, isolated_run: Path) -> None:
        vmain._record_run_start(
            run_id="20260101_120000", goal="hello", started_at=1_000_000.0,
        )
        assert vmain._RUN_ID == "20260101_120000"
        assert vmain._RUN_GOAL == "hello"
        assert vmain._RUN_STARTED_AT == 1_000_000.0

    def test_blank_run_id_stored_as_none(self, isolated_run: Path) -> None:
        vmain._record_run_start(run_id="   ", goal="x")
        assert vmain._RUN_ID is None

    def test_default_started_at_is_now(self, isolated_run: Path) -> None:
        before = time.time()
        vmain._record_run_start(run_id="rid")
        after = time.time()
        assert vmain._RUN_STARTED_AT is not None
        assert before - 0.5 <= vmain._RUN_STARTED_AT <= after + 0.5


class TestRecordRunStep:
    def test_updates_step(self, isolated_run: Path) -> None:
        vmain._record_run_step(7)
        assert vmain._RUN_LAST_STEP == 7
        vmain._record_run_step(42)
        assert vmain._RUN_LAST_STEP == 42

    def test_bad_step_silently_ignored(self, isolated_run: Path) -> None:
        vmain._record_run_step(3)
        vmain._record_run_step("not-a-number")  # type: ignore[arg-type]
        # Bad value left previous value intact
        assert vmain._RUN_LAST_STEP == 3


class TestResetClearsK4State:
    def test_all_fields_cleared(self, isolated_run: Path) -> None:
        vmain._record_run_start(run_id="rid", goal="g", started_at=1.0)
        vmain._record_run_step(5)
        vmain._reset_run_answer()
        assert vmain._RUN_ID is None
        assert vmain._RUN_GOAL is None
        assert vmain._RUN_STARTED_AT is None
        assert vmain._RUN_LAST_STEP is None


# ── _broadcast_done_safe wiring ───────────────────────────────────────


class TestBroadcastDoneFailureArchive:
    def test_writes_failure_record(self, isolated_run: Path) -> None:
        vmain._record_run_start(
            run_id="rid_ok",
            goal="导出余额表",
            started_at=time.time() - 12.0,
        )
        vmain._record_run_step(4)
        vmain._broadcast_done_safe(
            False, "Max steps reached (40)",
        )
        target = isolated_run / "runs" / "failed" / "rid_ok.json"
        assert target.exists(), f"expected archive at {target}"
        rec = json.loads(target.read_text(encoding="utf-8"))
        assert rec["run_id"] == "rid_ok"
        assert rec["reason"] == "Max steps reached (40)"
        assert rec["goal"] == "导出余额表"
        assert rec["step_count"] == 4
        # duration derived from started_at (12s ± slack)
        assert 5.0 <= rec["duration_s"] <= 120.0
        # canonical paths block
        assert rec["paths"]["html_log"] == "logs/run_log_rid_ok.html"

    def test_success_does_not_archive(self, isolated_run: Path) -> None:
        vmain._record_run_start(run_id="rid_succ", goal="g")
        vmain._broadcast_done_safe(True, "done")
        target = isolated_run / "runs" / "failed" / "rid_succ.json"
        assert not target.exists()

    def test_skipped_when_run_id_missing(self, isolated_run: Path) -> None:
        # No _record_run_start was called — _RUN_ID stays None after reset.
        vmain._reset_run_answer()
        assert vmain._RUN_ID is None
        vmain._broadcast_done_safe(False, "boom")
        failed_dir = isolated_run / "runs" / "failed"
        # Either dir was never created, or is empty
        if failed_dir.exists():
            assert not any(failed_dir.iterdir())

    def test_archive_disk_error_does_not_propagate(
        self, isolated_run: Path, monkeypatch,
    ) -> None:
        """If record_failure raises, the broadcast path must still finish.

        We stub record_failure to raise and check that
        _broadcast_done_safe returns normally without re-raising.
        """
        def _boom(**kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr(fa, "record_failure", _boom)
        vmain._record_run_start(run_id="rid_err", goal="g")
        # Must NOT raise
        vmain._broadcast_done_safe(False, "any reason")

    def test_empty_message_becomes_no_message_placeholder(
        self, isolated_run: Path,
    ) -> None:
        vmain._record_run_start(run_id="rid_blank", goal="g")
        vmain._broadcast_done_safe(False, "")
        rec = json.loads(
            (isolated_run / "runs" / "failed" / "rid_blank.json").read_text(
                encoding="utf-8"
            )
        )
        # Empty / falsy message is replaced with a stable placeholder so the
        # frontend always has something to display in the drawer row.
        assert rec["reason"] == "(no message)"


class TestRoundTripWithListFailedRuns:
    """After ``_broadcast_done_safe(False, ...)`` runs, the file we
    wrote must be visible via ``list_failed_runs`` (the same data source
    the ``/api/failed_runs`` endpoint uses)."""

    def test_record_then_list(self, isolated_run: Path) -> None:
        for i, reason in enumerate(["first failure", "second failure"]):
            vmain._record_run_start(
                run_id=f"rid_seq_{i}",
                goal=f"goal {i}",
                started_at=time.time() - (10 - i),
            )
            vmain._broadcast_done_safe(False, reason)
        # base_dir = isolated_run/runs ; project_root = isolated_run
        out = fa.list_failed_runs(
            base_dir=isolated_run / "runs",
            project_root=isolated_run,
        )
        assert len(out) == 2
        run_ids = {r["run_id"] for r in out}
        assert run_ids == {"rid_seq_0", "rid_seq_1"}
        reasons = {r["reason"] for r in out}
        assert reasons == {"first failure", "second failure"}

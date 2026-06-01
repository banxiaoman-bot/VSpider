"""K5: retention / cleanup of ``runs/failed/<id>.json`` archive entries.

Three layers covered:

1. ``parse_duration`` — duration token grammar (Ns/Nm/Nh/Nd/Nw).
2. ``select_for_deletion`` — pure function deciding which records die,
   covering keep_last alone, older_than alone, the AND combination, and
   the missing-ts safety net.
3. ``cleanup_failed_runs`` — orchestrator, including dry-run, purge-related,
   and best-effort error handling.
4. CLI smoke — ``scripts/cleanup_failed_runs.py`` end-to-end via subprocess.

We avoid pytest's ``tmp_path`` for the same reason ``test_failure_archive``
does — Windows ``%LOCALAPPDATA%`` permission denials. All on-disk tests
write under ``tests/.tmp_k5/``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator

import pytest

from visual_web_agent import failure_archive as fa


ROOT = Path(__file__).resolve().parents[1]


# ── tmp dir fixture (project-local; pytest tmp_path is unreliable here) ─


@pytest.fixture
def tmp_runs() -> Iterator[Path]:
    base = Path(__file__).resolve().parent / ".tmp_k5"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="cleanup_", dir=str(base)))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _seed_record(
    base_dir: Path,
    *,
    run_id: str,
    ts: float,
    reason: str = "auto",
    paths: dict | None = None,
) -> Path:
    """Write one ``runs/failed/<run_id>.json`` file with controlled ts.

    Direct write (not via ``record_failure``) so we can pin ts to any
    arbitrary value — ``record_failure`` always uses ``time.time()``.
    """
    target_dir = base_dir / "runs" / "failed"
    target_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "schema_version": fa.SCHEMA_VERSION,
        "run_id": run_id,
        "ts": ts,
        "reason": reason,
        "paths": paths if paths is not None else {
            "html_log": f"logs/run_log_{run_id}.html",
            "phase_jsonl": f"logs/phase_{run_id}.jsonl",
            "event_jsonl": f"logs/event_stream_{run_id}.jsonl",
        },
    }
    path = target_dir / f"{run_id}.json"
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ── 1. parse_duration ────────────────────────────────────────────────


class TestParseDuration:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("30s", 30.0),
            ("15m", 15 * 60.0),
            ("2h", 2 * 3600.0),
            ("1d", 86400.0),
            ("2w", 2 * 7 * 86400.0),
            ("0d", 0.0),
            ("1.5h", 1.5 * 3600.0),
            ("12H", 12 * 3600.0),     # case-insensitive unit
            (" 30d ", 30 * 86400.0),  # surrounding whitespace OK
        ],
    )
    def test_valid_inputs(self, text: str, expected: float) -> None:
        assert fa.parse_duration(text) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "text",
        ["", None, "30", "30x", "abc", "-5d", "d", "10dh"],
    )
    def test_invalid_inputs_raise(self, text) -> None:
        with pytest.raises(ValueError):
            fa.parse_duration(text)


# ── 2. select_for_deletion ───────────────────────────────────────────


class TestSelectForDeletion:
    def _make(self, n: int, *, base_ts: float = 1_000_000.0, gap: float = 100.0) -> list[dict]:
        """Build n records, newest first in the input list. The ts gap
        ensures each record is distinguishable for keep-last cuts."""
        return [
            {"run_id": f"r{i}", "ts": base_ts - i * gap, "reason": "x"}
            for i in range(n)
        ]

    def test_no_policy_returns_empty(self) -> None:
        recs = self._make(10)
        assert fa.select_for_deletion(recs) == []

    def test_keep_last_only_drops_tail(self) -> None:
        recs = self._make(5)
        # ts ordering: r0 newest .. r4 oldest. keep_last=2 → drop r2,r3,r4.
        out = fa.select_for_deletion(recs, keep_last=2)
        run_ids = {r["run_id"] for r in out}
        assert run_ids == {"r2", "r3", "r4"}

    def test_keep_last_zero_drops_everything(self) -> None:
        recs = self._make(3)
        out = fa.select_for_deletion(recs, keep_last=0)
        assert {r["run_id"] for r in out} == {"r0", "r1", "r2"}

    def test_keep_last_larger_than_list_drops_nothing(self) -> None:
        recs = self._make(3)
        out = fa.select_for_deletion(recs, keep_last=99)
        assert out == []

    def test_older_than_only(self) -> None:
        now = 2_000_000.0
        recs = [
            {"run_id": "fresh", "ts": now - 10.0, "reason": "x"},
            {"run_id": "stale", "ts": now - 3600.0, "reason": "x"},
            {"run_id": "ancient", "ts": now - 86_400.0, "reason": "x"},
        ]
        out = fa.select_for_deletion(recs, older_than_s=1800.0, now=now)
        # 30-minute window → stale + ancient go, fresh stays.
        assert {r["run_id"] for r in out} == {"stale", "ancient"}

    def test_combined_and_semantics(self) -> None:
        """Both keep_last AND older_than must agree a record dies.

        Layout (newest first by ts):
          r0  (ts = now - 10s)
          r1  (ts = now - 20s)
          r2  (ts = now - 30s)
          r3  (ts = now - 86400s)   ← 1 day old
          r4  (ts = now - 172800s)  ← 2 days old

        keep_last=3, older_than=12h:
          • r0, r1, r2 → within keep_last (idx < 3) → KEEP
          • r3        → idx=3 (beyond keep_last) AND 24h > 12h → DELETE
          • r4        → idx=4 (beyond keep_last) AND 48h > 12h → DELETE
        """
        now = 3_000_000.0
        recs = [
            {"run_id": "r0", "ts": now - 10.0, "reason": "x"},
            {"run_id": "r1", "ts": now - 20.0, "reason": "x"},
            {"run_id": "r2", "ts": now - 30.0, "reason": "x"},
            {"run_id": "r3", "ts": now - 86_400.0, "reason": "x"},
            {"run_id": "r4", "ts": now - 172_800.0, "reason": "x"},
        ]
        out = fa.select_for_deletion(
            recs, keep_last=3, older_than_s=43_200.0, now=now,
        )
        assert {r["run_id"] for r in out} == {"r3", "r4"}

    def test_combined_keep_last_protects_old_records(self) -> None:
        """If a record is OLD but still inside keep_last, it stays.
        (AND semantics: idx < keep_n alone is enough to save it.)"""
        now = 4_000_000.0
        recs = [
            {"run_id": "old_but_kept", "ts": now - 1_000_000.0, "reason": "x"},
        ]
        out = fa.select_for_deletion(
            recs, keep_last=10, older_than_s=60.0, now=now,
        )
        assert out == []

    def test_missing_ts_never_deleted(self) -> None:
        """Conservative: records without a parseable ts must NEVER be
        selected — we can't tell their age, so we keep them around for
        manual inspection."""
        recs = [
            {"run_id": "no_ts", "reason": "x"},
            {"run_id": "bad_ts", "ts": "yesterday", "reason": "x"},
            {"run_id": "fresh", "ts": time.time(), "reason": "x"},
        ]
        out = fa.select_for_deletion(
            recs, keep_last=0, older_than_s=0.0, now=time.time(),
        )
        # keep_last=0 + older_than=0 would normally nuke everyone, but
        # the ts-less records get the safety pass.
        assert {r["run_id"] for r in out} == {"fresh"}

    def test_input_not_mutated(self) -> None:
        """The function must be pure — caller's list/order should be
        unchanged after the call."""
        recs = self._make(4)
        snapshot = list(recs)
        fa.select_for_deletion(recs, keep_last=1)
        assert recs == snapshot

    def test_records_sorted_internally(self) -> None:
        """Even if caller passes records in arbitrary order, the result
        must be the same as feeding them sorted newest-first."""
        now = 5_000_000.0
        recs_a = [
            {"run_id": "newest", "ts": now - 1.0, "reason": "x"},
            {"run_id": "middle", "ts": now - 100.0, "reason": "x"},
            {"run_id": "oldest", "ts": now - 10_000.0, "reason": "x"},
        ]
        recs_b = list(reversed(recs_a))
        out_a = fa.select_for_deletion(recs_a, keep_last=1, now=now)
        out_b = fa.select_for_deletion(recs_b, keep_last=1, now=now)
        assert (
            {r["run_id"] for r in out_a}
            == {r["run_id"] for r in out_b}
            == {"middle", "oldest"}
        )


# ── 3. cleanup_failed_runs orchestrator ──────────────────────────────


class TestCleanupOrchestrator:
    def test_dry_run_does_not_touch_disk(self, tmp_runs: Path) -> None:
        now = time.time()
        for i in range(5):
            _seed_record(tmp_runs, run_id=f"r{i}", ts=now - i * 100)
        summary = fa.cleanup_failed_runs(
            keep_last=2,
            dry_run=True,
            base_dir=tmp_runs / "runs",
            project_root=tmp_runs,
            now=now,
        )
        assert summary["scanned"] == 5
        assert summary["deleted"] == 3  # would be deleted
        assert summary["kept"] == 2
        assert summary["dry_run"] is True
        # Disk untouched
        survivors = list((tmp_runs / "runs" / "failed").iterdir())
        assert len(survivors) == 5
        # Every dry-run detail entry is flagged
        assert all(d.get("would_delete") for d in summary["details"])

    def test_real_run_removes_archive_files(self, tmp_runs: Path) -> None:
        now = time.time()
        for i in range(4):
            _seed_record(tmp_runs, run_id=f"r{i}", ts=now - i * 100)
        summary = fa.cleanup_failed_runs(
            keep_last=2,
            base_dir=tmp_runs / "runs",
            project_root=tmp_runs,
            now=now,
        )
        assert summary["scanned"] == 4
        assert summary["deleted"] == 2
        assert summary["kept"] == 2
        survivors = sorted(p.name for p in (tmp_runs / "runs" / "failed").iterdir())
        # r0 (newest) + r1 survive; r2, r3 (oldest) deleted
        assert survivors == ["r0.json", "r1.json"]

    def test_purge_related_deletes_referenced_files(self, tmp_runs: Path) -> None:
        now = time.time()
        # Create a record AND the referenced HTML + phase JSONL files
        logs_dir = tmp_runs / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        html_path = logs_dir / "run_log_r_old.html"
        phase_path = logs_dir / "phase_r_old.jsonl"
        event_path = logs_dir / "event_stream_r_old.jsonl"
        html_path.write_text("<html>old</html>", encoding="utf-8")
        phase_path.write_text("{}\n", encoding="utf-8")
        event_path.write_text("{}\n", encoding="utf-8")

        _seed_record(tmp_runs, run_id="r_old", ts=now - 1_000_000.0)
        _seed_record(tmp_runs, run_id="r_new", ts=now - 1.0)

        summary = fa.cleanup_failed_runs(
            keep_last=1,
            purge_related=True,
            base_dir=tmp_runs / "runs",
            project_root=tmp_runs,
            now=now,
        )
        assert summary["deleted"] == 1
        # The old run's referenced logs must be gone
        assert not html_path.exists()
        assert not phase_path.exists()
        assert not event_path.exists()
        # The newer run's archive survives (no logs were created for it
        # in this test, but the archive itself must remain)
        assert (tmp_runs / "runs" / "failed" / "r_new.json").exists()
        assert not (tmp_runs / "runs" / "failed" / "r_old.json").exists()

    def test_no_policy_returns_zero_deletion(self, tmp_runs: Path) -> None:
        """Sanity: orchestrator inherits select_for_deletion's contract."""
        _seed_record(tmp_runs, run_id="r0", ts=time.time())
        summary = fa.cleanup_failed_runs(
            base_dir=tmp_runs / "runs",
            project_root=tmp_runs,
        )
        assert summary["scanned"] == 1
        assert summary["deleted"] == 0
        assert summary["kept"] == 1

    def test_missing_directory_is_clean_noop(self, tmp_runs: Path) -> None:
        # Note: runs/failed dir doesn't exist
        summary = fa.cleanup_failed_runs(
            keep_last=0,
            base_dir=tmp_runs / "runs",
            project_root=tmp_runs,
        )
        assert summary == {
            "scanned": 0,
            "deleted": 0,
            "kept": 0,
            "dry_run": False,
            "details": [],
            "errors": [],
        }

    def test_purge_related_handles_missing_referenced_files(
        self, tmp_runs: Path,
    ) -> None:
        """A common case: the HTML log was rotated away externally
        before cleanup ran. The pass should still remove the archive
        and NOT flag this as an error."""
        now = time.time()
        _seed_record(tmp_runs, run_id="r_orphan", ts=now - 1_000_000.0)
        summary = fa.cleanup_failed_runs(
            keep_last=0,
            purge_related=True,
            base_dir=tmp_runs / "runs",
            project_root=tmp_runs,
            now=now,
        )
        assert summary["deleted"] == 1
        assert summary["errors"] == []


# ── 4. CLI subprocess smoke ──────────────────────────────────────────


CLI = ROOT / "scripts" / "cleanup_failed_runs.py"


class TestCli:
    def test_no_args_exits_nonzero(self) -> None:
        """argparse.error → exit code 2."""
        proc = subprocess.run(
            [sys.executable, str(CLI)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        assert "must specify at least one" in proc.stderr

    def test_dry_run_with_json_output(self, tmp_runs: Path) -> None:
        now = time.time()
        for i in range(3):
            _seed_record(tmp_runs, run_id=f"r{i}", ts=now - i * 100)
        proc = subprocess.run(
            [
                sys.executable, str(CLI),
                "--keep-last", "1",
                "--dry-run",
                "--json",
                "--base-dir", str(tmp_runs / "runs"),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        summary = json.loads(proc.stdout)
        assert summary["scanned"] == 3
        assert summary["deleted"] == 2
        assert summary["dry_run"] is True
        # Disk untouched
        assert len(list((tmp_runs / "runs" / "failed").iterdir())) == 3

    def test_real_run_prunes_and_reports(self, tmp_runs: Path) -> None:
        now = time.time()
        for i in range(4):
            _seed_record(tmp_runs, run_id=f"r{i}", ts=now - i * 100)
        proc = subprocess.run(
            [
                sys.executable, str(CLI),
                "--keep-last", "2",
                "--base-dir", str(tmp_runs / "runs"),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        # Text summary contains the canonical lines
        assert "APPLIED" in proc.stdout
        assert "scanned: 4" in proc.stdout
        assert "deleted: 2" in proc.stdout
        # Disk: only 2 newest survive
        survivors = sorted(p.name for p in (tmp_runs / "runs" / "failed").iterdir())
        assert survivors == ["r0.json", "r1.json"]

    def test_bad_duration_token_errors_out(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(CLI), "--older-than", "30x"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        assert "--older-than" in proc.stderr

    def test_negative_keep_last_errors_out(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(CLI), "--keep-last", "-1"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        assert "--keep-last" in proc.stderr

    def test_verbose_lists_per_record_detail(self, tmp_runs: Path) -> None:
        now = time.time()
        for i in range(3):
            _seed_record(tmp_runs, run_id=f"vid_{i}", ts=now - i * 100)
        proc = subprocess.run(
            [
                sys.executable, str(CLI),
                "--keep-last", "1",
                "--dry-run",
                "--verbose",
                "--base-dir", str(tmp_runs / "runs"),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        # The 2 doomed run IDs must appear in the verbose output
        assert "vid_1" in proc.stdout
        assert "vid_2" in proc.stdout
        # The kept one must NOT appear in the per-record list (it's not
        # in `details`). Defensive: we check it's not next to a marker.
        assert "[would-delete] vid_0" not in proc.stdout

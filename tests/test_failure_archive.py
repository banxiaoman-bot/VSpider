"""K1: ``failure_archive.record_failure`` + ``list_failed_runs`` contract.

Pure-Python tests — no agent runtime, no FastAPI. We exercise the module
directly with a project-local tmp directory (NOT pytest's ``tmp_path``)
to avoid the recurring ``%LOCALAPPDATA%\\Temp\\pytest-of-<user>``
permission denials seen on this Windows host (same workaround used in
``tests/test_phase_event_persistence.py``).
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


# ── Project-local tmp fixture ─────────────────────────────────────────


@pytest.fixture
def archive_tmp() -> Iterator[Path]:
    """Yield a fresh tmp directory under ``<repo>/.tmp_failure_archive``.

    Mirrors the pattern in ``test_phase_event_persistence.py``. Cleanup
    is best-effort so a stuck handle on Windows doesn't fail the test.
    """
    base = Path(__file__).resolve().parent / ".tmp_failure_archive"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="fa_", dir=str(base)))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── record_failure ────────────────────────────────────────────────────


class TestRecordFailureHappyPath:
    def test_writes_file_with_canonical_name(self, archive_tmp: Path) -> None:
        out = fa.record_failure(
            run_id="20260101_120000",
            reason="Max steps reached (40)",
            base_dir=archive_tmp,
        )
        assert out is not None
        assert out.exists()
        assert out.name == "20260101_120000.json"
        # Lives under runs/failed/<id>.json
        assert out.parent.name == fa.FAILED_SUBDIR_NAME

    def test_payload_schema(self, archive_tmp: Path) -> None:
        fa.record_failure(
            run_id="rid_a",
            reason="oops",
            goal="登录后导出余额",
            started_at=1_000_000.0,
            duration_s=12.5,
            exception_type="TimeoutError",
            step_count=7,
            base_dir=archive_tmp,
        )
        rec = json.loads(
            (archive_tmp / fa.FAILED_SUBDIR_NAME / "rid_a.json").read_text(
                encoding="utf-8"
            )
        )
        assert rec["schema_version"] == fa.SCHEMA_VERSION
        assert rec["run_id"] == "rid_a"
        assert rec["reason"] == "oops"
        assert rec["goal"] == "登录后导出余额"
        assert rec["started_at"] == 1_000_000.0
        assert rec["duration_s"] == 12.5
        assert rec["exception_type"] == "TimeoutError"
        assert rec["step_count"] == 7
        # ts is auto-added (epoch seconds)
        assert isinstance(rec["ts"], (int, float))
        # paths block uses the conventional names
        assert rec["paths"]["html_log"] == "logs/run_log_rid_a.html"
        assert rec["paths"]["phase_jsonl"] == "logs/phase_rid_a.jsonl"
        assert rec["paths"]["event_jsonl"] == "logs/event_stream_rid_a.jsonl"

    def test_minimal_payload_only_required_fields(self, archive_tmp: Path) -> None:
        fa.record_failure(
            run_id="bare", reason="exception", base_dir=archive_tmp,
        )
        rec = json.loads(
            (archive_tmp / fa.FAILED_SUBDIR_NAME / "bare.json").read_text(
                encoding="utf-8"
            )
        )
        # Optional keys absent when not provided
        for k in ("goal", "started_at", "duration_s", "exception_type", "step_count"):
            assert k not in rec
        # Required keys always present
        assert {"schema_version", "run_id", "ts", "reason", "paths"} <= rec.keys()

    def test_duration_derived_from_started_at(self, archive_tmp: Path) -> None:
        # 60s ago, no explicit duration
        start = time.time() - 60.0
        fa.record_failure(
            run_id="derived", reason="x", started_at=start, base_dir=archive_tmp,
        )
        rec = json.loads(
            (archive_tmp / fa.FAILED_SUBDIR_NAME / "derived.json").read_text(
                encoding="utf-8"
            )
        )
        # Should be ~60s, allow generous slack for slow CI
        assert 50.0 <= rec["duration_s"] <= 200.0

    def test_explicit_duration_wins_over_derivation(self, archive_tmp: Path) -> None:
        fa.record_failure(
            run_id="explicit",
            reason="x",
            started_at=time.time() - 100.0,
            duration_s=42.0,
            base_dir=archive_tmp,
        )
        rec = json.loads(
            (archive_tmp / fa.FAILED_SUBDIR_NAME / "explicit.json").read_text(
                encoding="utf-8"
            )
        )
        assert rec["duration_s"] == 42.0


class TestRecordFailureSafety:
    def test_blank_run_id_returns_none(self, archive_tmp: Path) -> None:
        assert fa.record_failure(
            run_id="", reason="oops", base_dir=archive_tmp,
        ) is None
        assert fa.record_failure(
            run_id="   ", reason="oops", base_dir=archive_tmp,
        ) is None
        # No file written
        assert not (archive_tmp / fa.FAILED_SUBDIR_NAME).exists() or not list(
            (archive_tmp / fa.FAILED_SUBDIR_NAME).iterdir()
        )

    def test_long_reason_is_truncated(self, archive_tmp: Path) -> None:
        long = "x" * (fa.REASON_MAX_LEN + 500)
        fa.record_failure(run_id="trunc_r", reason=long, base_dir=archive_tmp)
        rec = json.loads(
            (archive_tmp / fa.FAILED_SUBDIR_NAME / "trunc_r.json").read_text(
                encoding="utf-8"
            )
        )
        # Truncated to cap (with one-char ellipsis added)
        assert len(rec["reason"]) <= fa.REASON_MAX_LEN
        assert rec["reason"].endswith("…")

    def test_long_goal_is_truncated(self, archive_tmp: Path) -> None:
        long = "g" * (fa.GOAL_MAX_LEN + 100)
        fa.record_failure(
            run_id="trunc_g", reason="x", goal=long, base_dir=archive_tmp,
        )
        rec = json.loads(
            (archive_tmp / fa.FAILED_SUBDIR_NAME / "trunc_g.json").read_text(
                encoding="utf-8"
            )
        )
        assert len(rec["goal"]) <= fa.GOAL_MAX_LEN
        assert rec["goal"].endswith("…")

    def test_invalid_numeric_fields_silently_dropped(self, archive_tmp: Path) -> None:
        fa.record_failure(
            run_id="bad_nums",
            reason="x",
            duration_s="not-a-number",  # type: ignore[arg-type]
            started_at="also-bad",  # type: ignore[arg-type]
            step_count="wat",  # type: ignore[arg-type]
            base_dir=archive_tmp,
        )
        rec = json.loads(
            (archive_tmp / fa.FAILED_SUBDIR_NAME / "bad_nums.json").read_text(
                encoding="utf-8"
            )
        )
        # The bad fields are not present (silently dropped, not raised)
        assert "duration_s" not in rec
        assert "started_at" not in rec
        assert "step_count" not in rec

    def test_overwrite_on_same_run_id(self, archive_tmp: Path) -> None:
        """Re-recording the same run_id overwrites (atomic). This matters
        if a run gets retried and we re-archive its failure."""
        fa.record_failure(run_id="rid", reason="first", base_dir=archive_tmp)
        fa.record_failure(run_id="rid", reason="second", base_dir=archive_tmp)
        rec = json.loads(
            (archive_tmp / fa.FAILED_SUBDIR_NAME / "rid.json").read_text(
                encoding="utf-8"
            )
        )
        assert rec["reason"] == "second"

    def test_record_never_raises_on_bad_dir(self, monkeypatch) -> None:
        """If the target directory is unwritable, return None — never raise.

        We force this by pointing base_dir at a non-existent device path
        on Windows ("Z:/nope/nope/nope"). On POSIX where this resolves
        unexpectedly, we just check it didn't raise.
        """
        out = fa.record_failure(
            run_id="rid",
            reason="x",
            base_dir="Z:/nope/__never_exists__/nope",
        )
        # Either the write failed silently (returns None) — that's the
        # contract — OR it succeeded by accident on a permissive
        # filesystem; both outcomes satisfy "must not raise".
        assert out is None or isinstance(out, Path)


# ── list_failed_runs ──────────────────────────────────────────────────


class TestListFailedRunsBasics:
    def test_empty_when_dir_missing(self, archive_tmp: Path) -> None:
        # archive_tmp is empty — no runs/failed/ subdir yet
        assert fa.list_failed_runs(base_dir=archive_tmp) == []

    def test_returns_records_newest_first(self, archive_tmp: Path) -> None:
        # Write 3 records with explicit ts values via direct file writes
        target = archive_tmp / fa.FAILED_SUBDIR_NAME
        target.mkdir(parents=True, exist_ok=True)
        for i, ts in enumerate([100.0, 300.0, 200.0]):
            (target / f"r{i}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": f"r{i}",
                        "ts": ts,
                        "reason": f"reason-{i}",
                    }
                ),
                encoding="utf-8",
            )
        out = fa.list_failed_runs(base_dir=archive_tmp)
        assert [r["run_id"] for r in out] == ["r1", "r2", "r0"]  # 300, 200, 100

    def test_limit_caps_result(self, archive_tmp: Path) -> None:
        target = archive_tmp / fa.FAILED_SUBDIR_NAME
        target.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            (target / f"r{i}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": f"r{i}",
                        "ts": float(i),
                        "reason": "x",
                    }
                ),
                encoding="utf-8",
            )
        out = fa.list_failed_runs(base_dir=archive_tmp, limit=3)
        assert len(out) == 3
        assert [r["run_id"] for r in out] == ["r9", "r8", "r7"]

    def test_skips_malformed_files(self, archive_tmp: Path) -> None:
        target = archive_tmp / fa.FAILED_SUBDIR_NAME
        target.mkdir(parents=True, exist_ok=True)
        # Good record
        (target / "ok.json").write_text(
            json.dumps(
                {"schema_version": 1, "run_id": "ok", "ts": 1.0, "reason": "x"}
            ),
            encoding="utf-8",
        )
        # Bad: not JSON
        (target / "bad.json").write_text("not json {{{", encoding="utf-8")
        # Bad: JSON but list, not dict
        (target / "list.json").write_text("[1,2,3]", encoding="utf-8")
        # Bad: missing required key
        (target / "incomplete.json").write_text(
            json.dumps({"run_id": "x"}), encoding="utf-8"
        )
        # Non-json file, ignored
        (target / "README.txt").write_text("hi", encoding="utf-8")

        out = fa.list_failed_runs(base_dir=archive_tmp)
        assert len(out) == 1
        assert out[0]["run_id"] == "ok"


class TestListFailedRunsPathsExist:
    """``paths_exist`` sub-dict reflects real filesystem state under
    ``project_root``. Used by the frontend to grey out open-log buttons."""

    def test_all_missing_when_no_files(self, archive_tmp: Path) -> None:
        target = archive_tmp / fa.FAILED_SUBDIR_NAME
        target.mkdir(parents=True, exist_ok=True)
        (target / "r.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "r",
                    "ts": 1.0,
                    "reason": "x",
                    "paths": {
                        "html_log": "logs/run_log_r.html",
                        "phase_jsonl": "logs/phase_r.jsonl",
                        "event_jsonl": "logs/event_stream_r.jsonl",
                    },
                }
            ),
            encoding="utf-8",
        )
        # project_root points at archive_tmp (no logs/ inside)
        out = fa.list_failed_runs(base_dir=archive_tmp, project_root=archive_tmp)
        assert out[0]["paths_exist"] == {
            "html_log": False,
            "phase_jsonl": False,
            "event_jsonl": False,
        }

    def test_exists_when_file_present(self, archive_tmp: Path) -> None:
        target = archive_tmp / fa.FAILED_SUBDIR_NAME
        target.mkdir(parents=True, exist_ok=True)
        # Create a fake "logs/run_log_r.html" under archive_tmp so
        # project_root=archive_tmp finds it.
        logs = archive_tmp / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "run_log_r.html").write_text("<html/>", encoding="utf-8")
        (target / "r.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "r",
                    "ts": 1.0,
                    "reason": "x",
                    "paths": {
                        "html_log": "logs/run_log_r.html",
                        "phase_jsonl": "logs/phase_r.jsonl",  # missing
                        "event_jsonl": "logs/event_stream_r.jsonl",  # missing
                    },
                }
            ),
            encoding="utf-8",
        )
        out = fa.list_failed_runs(base_dir=archive_tmp, project_root=archive_tmp)
        assert out[0]["paths_exist"]["html_log"] is True
        assert out[0]["paths_exist"]["phase_jsonl"] is False
        assert out[0]["paths_exist"]["event_jsonl"] is False


# ── Integration: record then list ─────────────────────────────────────


class TestRoundTrip:
    def test_record_then_list(self, archive_tmp: Path) -> None:
        for i in range(3):
            fa.record_failure(
                run_id=f"rid_{i}",
                reason=f"reason {i}",
                goal=f"goal {i}",
                started_at=time.time() - (10 - i),
                base_dir=archive_tmp,
            )
        out = fa.list_failed_runs(
            base_dir=archive_tmp, project_root=archive_tmp,
        )
        assert len(out) == 3
        run_ids = {r["run_id"] for r in out}
        assert run_ids == {"rid_0", "rid_1", "rid_2"}
        # Newest first (rid_2 has the largest started_at and was written last)
        for r in out:
            assert "paths_exist" in r
            assert "ts" in r

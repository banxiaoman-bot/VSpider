"""L-scanner: ``scripts/scan_failed_runs.py`` reads phase jsonl pairs.

The scanner walks ``logs/run_log_<id>.html`` and now also opens
``logs/phase_<id>.jsonl`` for matching ``id``. These tests pin:

1. **`parse_phase_jsonl`** robustness — bad rows skipped, missing file → {}
2. **Aggregate stats** — count / warn / error / percentile fields populated
3. **Run pairing** — ``parse_run`` attaches ``phase_stats`` keyed off the
   run-id derived from the html filename
4. **Report rendering** — ``build_report`` adds a ``Phase timings`` table
   when at least one paired jsonl exists, and silently skips it otherwise
"""

from __future__ import annotations

import importlib
import json
import shutil
import tempfile
from pathlib import Path

import pytest

# Re-import via spec so we don't pollute sys.modules with relative paths
SCAN = importlib.import_module("scripts.scan_failed_runs")


@pytest.fixture
def tmp_path(monkeypatch):
    """Project-local tmp dir override.

    Pytest's built-in ``tmp_path`` writes into
    ``%LOCALAPPDATA%\\Temp\\pytest-of-<user>`` which is permission-denied
    on this Windows host (matches the workaround used by
    ``test_phase_event_persistence.py``).
    """
    base = Path(__file__).resolve().parent.parent / ".tmp_phase_tests"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="scan_phase_", dir=str(base)))
    try:
        yield tmp
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


# ── helpers ─────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _minimal_html(content: str = "") -> str:
    """Return an html log with one done step-card and a tiny custom body."""
    return (
        '<!doctype html><html><body>'
        '<div class="step-card is-done">'
        f'  <div class="step-body">{content}</div>'
        '</div></body></html>'
    )


@pytest.fixture
def scanner_logs(tmp_path, monkeypatch):
    """Point the scanner at a clean tmp logs dir."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(SCAN, "LOGS_DIR", logs)
    return logs


# ════════════════════════════════════════════════════════════════════════
# parse_phase_jsonl: low-level robustness
# ════════════════════════════════════════════════════════════════════════


class TestParsePhaseJsonl:
    def test_missing_file_returns_empty(self, tmp_path):
        assert SCAN.parse_phase_jsonl(tmp_path / "nope.jsonl") == {}

    def test_aggregates_count_warn_error(self, tmp_path):
        path = tmp_path / "phase_x.jsonl"
        _write_jsonl(path, [
            {"phase": "vlm_call", "severity": "info", "duration_ms": 1000, "step": 1},
            {"phase": "vlm_call", "severity": "warn", "duration_ms": 2000, "step": 2},
            {"phase": "vlm_call", "severity": "error", "duration_ms": 3000, "step": 3},
            {"phase": "som_inject", "severity": "info", "duration_ms": 500, "step": 1},
        ])
        stats = SCAN.parse_phase_jsonl(path)
        assert set(stats.keys()) == {"vlm_call", "som_inject"}
        v = stats["vlm_call"]
        assert v["count"] == 3
        assert v["warn"] == 1
        assert v["error"] == 1
        assert v["avg_ms"] == 2000.0
        assert v["p50_ms"] == 2000.0
        assert v["max_ms"] == 3000.0
        # som_inject only has 1 row — percentiles must still be sensible
        s = stats["som_inject"]
        assert s["count"] == 1
        assert s["max_ms"] == 500.0

    def test_skips_rows_without_duration_for_timing_only(self, tmp_path):
        """``finalize`` typically has no duration_ms — must still be counted
        but excluded from timing aggregates."""
        path = tmp_path / "phase_x.jsonl"
        _write_jsonl(path, [
            {"phase": "finalize", "severity": "info"},
            {"phase": "finalize", "severity": "info"},
        ])
        stats = SCAN.parse_phase_jsonl(path)
        assert stats["finalize"]["count"] == 2
        assert stats["finalize"]["avg_ms"] is None
        assert stats["finalize"]["p95_ms"] is None
        assert stats["finalize"]["max_ms"] is None

    def test_malformed_rows_silently_skipped(self, tmp_path):
        path = tmp_path / "phase_x.jsonl"
        path.write_text(
            '\n'  # empty line
            'not-json-at-all\n'
            '{"phase":"good","severity":"info","duration_ms":42}\n'
            '{partially-broken\n'
            '\n',
            encoding="utf-8",
        )
        stats = SCAN.parse_phase_jsonl(path)
        assert "good" in stats and stats["good"]["count"] == 1
        # No "unknown" or junk-named phases leaked through
        assert set(stats.keys()) == {"good"}

    def test_phase_with_blank_value_falls_back_to_unknown(self, tmp_path):
        path = tmp_path / "phase_x.jsonl"
        _write_jsonl(path, [{"phase": "", "duration_ms": 10}])
        stats = SCAN.parse_phase_jsonl(path)
        assert "unknown" in stats


class TestPercentile:
    def test_empty(self):
        assert SCAN._percentile([], 95) == 0.0

    def test_single(self):
        assert SCAN._percentile([100.0], 95) == 100.0

    def test_p50_is_median_for_odd_count(self):
        assert SCAN._percentile([1.0, 2.0, 3.0], 50) == 2.0

    def test_p95_interpolates(self):
        # values 1..10, p95 should be 9.55 with linear interpolation
        vals = [float(x) for x in range(1, 11)]
        assert SCAN._percentile(vals, 95) == pytest.approx(9.55)


# ════════════════════════════════════════════════════════════════════════
# parse_run: pairs html with jsonl by run_id
# ════════════════════════════════════════════════════════════════════════


class TestParseRunPairing:
    def test_attaches_phase_stats_when_jsonl_present(self, scanner_logs):
        run_id = "20260601_120000"
        html_path = scanner_logs / f"run_log_{run_id}.html"
        html_path.write_text(_minimal_html(), encoding="utf-8")
        _write_jsonl(scanner_logs / f"phase_{run_id}.jsonl", [
            {"phase": "vlm_call", "severity": "info", "duration_ms": 1500},
            {"phase": "action", "severity": "info", "duration_ms": 800},
        ])

        run = SCAN.parse_run(html_path)
        assert run["outcome"] == "ok"
        ps = run["phase_stats"]
        assert "vlm_call" in ps and "action" in ps
        assert ps["vlm_call"]["count"] == 1
        assert ps["action"]["avg_ms"] == 800.0

    def test_empty_phase_stats_when_jsonl_missing(self, scanner_logs):
        run_id = "20260601_120000"
        html_path = scanner_logs / f"run_log_{run_id}.html"
        html_path.write_text(_minimal_html(), encoding="utf-8")
        # No jsonl created → phase_stats must be empty dict, not None
        run = SCAN.parse_run(html_path)
        assert run["phase_stats"] == {}

    def test_run_id_must_match_filename_pattern(self, scanner_logs):
        """A run_log_*.html with a non-standard name must not crash; just
        produce empty phase_stats."""
        html_path = scanner_logs / "run_log_wonkyformat.html"
        html_path.write_text(_minimal_html(), encoding="utf-8")
        run = SCAN.parse_run(html_path)
        assert run["phase_stats"] == {}


# ════════════════════════════════════════════════════════════════════════
# build_report: emits "Phase timings" section
# ════════════════════════════════════════════════════════════════════════


class TestBuildReport:
    def test_phase_timings_section_present(self, scanner_logs):
        run_id = "20260601_120000"
        (scanner_logs / f"run_log_{run_id}.html").write_text(
            _minimal_html(), encoding="utf-8",
        )
        _write_jsonl(scanner_logs / f"phase_{run_id}.jsonl", [
            {"phase": "vlm_call", "severity": "info", "duration_ms": 1000},
            {"phase": "vlm_call", "severity": "warn", "duration_ms": 3000},
            {"phase": "som_inject", "severity": "info", "duration_ms": 500},
        ])
        report = SCAN.build_report(limit=10)
        assert "## Phase timings" in report
        # Table header rendered
        assert "| phase | count | warn | error | avg ms |" in report
        # Both phases appear
        assert "`vlm_call`" in report
        assert "`som_inject`" in report

    def test_phase_section_skipped_when_no_jsonl(self, scanner_logs):
        run_id = "20260601_120000"
        (scanner_logs / f"run_log_{run_id}.html").write_text(
            _minimal_html(), encoding="utf-8",
        )
        report = SCAN.build_report(limit=10)
        # The section is only included when at least one paired jsonl exists
        assert "## Phase timings" not in report

    def test_phase_table_sorts_by_count_desc(self, scanner_logs):
        run_id = "20260601_120000"
        (scanner_logs / f"run_log_{run_id}.html").write_text(
            _minimal_html(), encoding="utf-8",
        )
        _write_jsonl(scanner_logs / f"phase_{run_id}.jsonl", [
            {"phase": "rare", "severity": "info", "duration_ms": 100},
            *[{"phase": "common", "severity": "info", "duration_ms": 50}
              for _ in range(5)],
        ])
        report = SCAN.build_report(limit=10)
        # `common` row must appear before `rare` row in the table
        common_pos = report.find("`common`")
        rare_pos = report.find("`rare`")
        assert 0 < common_pos < rare_pos, (
            "phase rows must be sorted by count descending"
        )

    def test_aggregates_across_multiple_runs(self, scanner_logs):
        """A phase that fires in 2 runs must show count=2_runs_total in the
        cross-run table."""
        for ts in ("20260601_120000", "20260602_120000"):
            (scanner_logs / f"run_log_{ts}.html").write_text(
                _minimal_html(), encoding="utf-8",
            )
            _write_jsonl(scanner_logs / f"phase_{ts}.jsonl", [
                {"phase": "vlm_call", "severity": "info", "duration_ms": 100},
                {"phase": "vlm_call", "severity": "warn", "duration_ms": 200},
            ])
        report = SCAN.build_report(limit=10)
        # vlm_call should report count=4 (2 rows × 2 runs) and warn=2
        # The exact line: "| `vlm_call` | 4 | 2 | 0 | ..."
        import re
        m = re.search(r"\|\s*`vlm_call`\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", report)
        assert m, f"vlm_call row not found in report:\n{report}"
        count, warn, err = (int(x) for x in m.groups())
        assert count == 4
        assert warn == 2
        assert err == 0

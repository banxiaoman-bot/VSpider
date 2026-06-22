"""Tests for ``resume_seed`` orchestration (RUN-RESUME1 step 3a-wire-2a).

The façade main.py uses to make a *resumed* run skip rows the prior run for the
same task already wrote. It ties together:

* ``run_resume_index`` (cross-run locator keyed by goal+url),
* ``manifest.json`` (to find the prior run's dataset artifact path),
* ``dataset_reader`` (read that artifact back to rows),
* ``data_sanitizer.rebuild_seen_fingerprints`` (rebuild the dedup seen-set).

All hermetic via ``base_dir=tmp_path``. The contract that matters is the
end-to-end round trip: record run A's dataset, then run B seeds a seen-set that
suppresses re-extraction of A's rows.
"""

from __future__ import annotations

import json
from pathlib import Path

from visual_web_agent.data_sanitizer import sanitize_extracted_rows
from visual_web_agent.io_contract.persistence import append_manifest_item, run_dir
from visual_web_agent.run_resume_index import lookup_last_run
from visual_web_agent.resume_seed import (
    ResumeSeed,
    latest_dataset_path,
    record_run_for_resume,
    seed_seen_from_last_run,
)


_GOAL = "scrape all products"
_URL = "https://shop.example.com/list"
_ROWS = [
    {"title": "Hello World Product", "url": "https://shop.example.com/p/1"},
    {"title": "Second Great Item", "url": "https://shop.example.com/p/2"},
    {"title": "Third Amazing Thing", "url": "https://shop.example.com/p/3"},
]
_SOURCE = (
    "Hello World Product https://shop.example.com/p/1 "
    "Second Great Item https://shop.example.com/p/2 "
    "Third Amazing Thing https://shop.example.com/p/3"
)


def _write_dataset(base: Path, run_id: str, rows: list[dict], *, name: str = "data.jsonl") -> str:
    """Write ``rows`` as a jsonl artifact and register it in the run manifest."""
    artifacts = run_dir(run_id, base_dir=base) / "artifacts"
    target = artifacts / name
    target.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    append_manifest_item(
        run_id, kind="dataset_rows", path=str(target), base_dir=base
    )
    return str(target)


class TestLatestDatasetPath:
    def test_no_manifest_returns_empty(self, tmp_path: Path) -> None:
        assert latest_dataset_path("run_missing", base_dir=tmp_path) == ""

    def test_returns_dataset_item_path(self, tmp_path: Path) -> None:
        path = _write_dataset(tmp_path, "run_a", _ROWS)
        assert latest_dataset_path("run_a", base_dir=tmp_path) == path

    def test_ignores_non_dataset_kinds(self, tmp_path: Path) -> None:
        append_manifest_item(
            "run_b", kind="screenshot", path="shot.png", base_dir=tmp_path
        )
        path = _write_dataset(tmp_path, "run_b", _ROWS)
        assert latest_dataset_path("run_b", base_dir=tmp_path) == path

    def test_returns_latest_when_multiple(self, tmp_path: Path) -> None:
        _write_dataset(tmp_path, "run_c", _ROWS[:1], name="first.jsonl")
        second = _write_dataset(tmp_path, "run_c", _ROWS, name="second.jsonl")
        assert latest_dataset_path("run_c", base_dir=tmp_path) == second


class TestRecordRunForResume:
    def test_records_run_with_dataset_path_from_manifest(self, tmp_path: Path) -> None:
        dataset = _write_dataset(tmp_path, "run_rec", _ROWS)
        key = record_run_for_resume(
            _GOAL, _URL, "run_rec", item_count=3, status="completed", base_dir=tmp_path
        )
        assert key
        entry = lookup_last_run(key, base_dir=tmp_path)
        assert entry is not None
        assert entry["run_id"] == "run_rec"
        assert entry["dataset_path"] == dataset
        assert entry["item_count"] == 3
        assert entry["status"] == "completed"

    def test_explicit_dataset_path_overrides_manifest(self, tmp_path: Path) -> None:
        run_dir("run_x", base_dir=tmp_path)
        key = record_run_for_resume(
            _GOAL, _URL, "run_x", dataset_path="/explicit/p.jsonl", base_dir=tmp_path
        )
        entry = lookup_last_run(key, base_dir=tmp_path)
        assert entry is not None
        assert entry["dataset_path"] == "/explicit/p.jsonl"


class TestSeedSeenFromLastRun:
    def test_no_prior_returns_empty(self, tmp_path: Path) -> None:
        seed = seed_seen_from_last_run(_GOAL, _URL, "run_now", base_dir=tmp_path)
        assert isinstance(seed, ResumeSeed)
        assert seed.seen == set()
        assert seed.count == 0
        assert seed.reason == "no_prior"

    def test_same_run_returns_empty(self, tmp_path: Path) -> None:
        _write_dataset(tmp_path, "run_same", _ROWS)
        record_run_for_resume(_GOAL, _URL, "run_same", item_count=3, base_dir=tmp_path)
        seed = seed_seen_from_last_run(_GOAL, _URL, "run_same", base_dir=tmp_path)
        assert seed.seen == set()
        assert seed.reason == "same_run"

    def test_no_dataset_returns_empty(self, tmp_path: Path) -> None:
        run_dir("run_nodata", base_dir=tmp_path)
        record_run_for_resume(_GOAL, _URL, "run_nodata", base_dir=tmp_path)
        seed = seed_seen_from_last_run(_GOAL, _URL, "run_new", base_dir=tmp_path)
        assert seed.seen == set()
        assert seed.reason == "no_dataset"

    def test_seeds_from_prior_dataset(self, tmp_path: Path) -> None:
        _write_dataset(tmp_path, "run_prior", _ROWS)
        record_run_for_resume(_GOAL, _URL, "run_prior", item_count=3, base_dir=tmp_path)
        seed = seed_seen_from_last_run(_GOAL, _URL, "run_resumed", base_dir=tmp_path)
        assert seed.prior_run_id == "run_prior"
        assert seed.count == 3
        assert len(seed.seen) >= 3
        assert seed.reason == "seeded"


class TestEndToEndResumeDedup:
    """The contract that matters: a resumed run skips the prior run's rows."""

    def test_resumed_run_suppresses_reextraction(self, tmp_path: Path) -> None:
        run1 = sanitize_extracted_rows(_ROWS, _SOURCE, seen_fingerprints=set())
        assert run1.accepted == 3
        _write_dataset(tmp_path, "run_1", run1.rows)
        record_run_for_resume(_GOAL, _URL, "run_1", item_count=3, base_dir=tmp_path)

        seed = seed_seen_from_last_run(_GOAL, _URL, "run_2", base_dir=tmp_path)
        run2 = sanitize_extracted_rows(_ROWS, _SOURCE, seen_fingerprints=seed.seen)
        assert run2.accepted == 0
        assert run2.duplicates == 3

    def test_resumed_run_still_accepts_new_rows(self, tmp_path: Path) -> None:
        run1 = sanitize_extracted_rows(_ROWS, _SOURCE, seen_fingerprints=set())
        _write_dataset(tmp_path, "run_1b", run1.rows)
        record_run_for_resume(_GOAL, _URL, "run_1b", item_count=3, base_dir=tmp_path)

        seed = seed_seen_from_last_run(_GOAL, _URL, "run_2b", base_dir=tmp_path)
        new_rows = [{"title": "Fourth Fresh Entry", "url": "https://shop.example.com/p/4"}]
        new_source = "Fourth Fresh Entry https://shop.example.com/p/4"
        run2 = sanitize_extracted_rows(new_rows, new_source, seen_fingerprints=seed.seen)
        assert run2.accepted == 1
        assert run2.duplicates == 0


class TestTolerance:
    def test_corrupt_manifest_degrades_to_empty(self, tmp_path: Path) -> None:
        target = run_dir("run_corrupt", base_dir=tmp_path) / "manifest.json"
        target.write_text("{ broken json", encoding="utf-8")
        assert latest_dataset_path("run_corrupt", base_dir=tmp_path) == ""

    def test_missing_dataset_file_seeds_empty(self, tmp_path: Path) -> None:
        # Manifest points at a dataset path that does not exist on disk.
        append_manifest_item(
            "run_ghost", kind="dataset_rows",
            path=str(tmp_path / "gone.jsonl"), base_dir=tmp_path,
        )
        record_run_for_resume(_GOAL, _URL, "run_ghost", base_dir=tmp_path)
        seed = seed_seen_from_last_run(_GOAL, _URL, "run_after", base_dir=tmp_path)
        assert seed.seen == set()
        assert seed.count == 0

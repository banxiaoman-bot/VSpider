"""Tests for ``visual_web_agent.run_resume_index`` (RUN-RESUME1 step 3-pre).

The cross-run locator that unblocks resume: a stable key derived from
``goal`` + ``start_url`` and a small ``runs/_resume_index.json`` mapping that
key to the most recent run for the same task. Pure I/O + a pure key function,
kept hermetic with ``tmp_path`` so no test writes under the real ``runs/``.
Mirrors the ``run_checkpoint`` / ``batch_resume`` slice test style.
"""

from __future__ import annotations

import json
from pathlib import Path

from visual_web_agent.run_resume_index import (
    RESUME_INDEX_FILENAME,
    compute_resume_key,
    lookup_last_run,
    record_run,
    resume_index_path,
)


class TestComputeResumeKey:
    def test_deterministic(self) -> None:
        a = compute_resume_key("抓取商品列表", "https://shop.example.com")
        b = compute_resume_key("抓取商品列表", "https://shop.example.com")
        assert a == b
        assert isinstance(a, str) and a

    def test_stable_across_whitespace_and_case(self) -> None:
        a = compute_resume_key("Grab  All Items", "https://A.com/")
        b = compute_resume_key("  grab all items ", "https://a.com")
        assert a == b

    def test_distinct_goal_or_url_differs(self) -> None:
        base = compute_resume_key("g", "https://a.com")
        assert compute_resume_key("g2", "https://a.com") != base
        assert compute_resume_key("g", "https://b.com") != base

    def test_empty_inputs_are_stable(self) -> None:
        assert compute_resume_key("", "") == compute_resume_key("", "")


class TestIndexRoundtrip:
    def test_lookup_missing_returns_none(self, tmp_path: Path) -> None:
        assert lookup_last_run("nope", base_dir=tmp_path) is None

    def test_record_then_lookup(self, tmp_path: Path) -> None:
        key = compute_resume_key("g", "https://a.com")
        record_run(
            key,
            "run_001",
            status="in_progress",
            item_count=12,
            goal="g",
            start_url="https://a.com",
            dataset_path="runs/run_001/artifacts/data.xlsx",
            base_dir=tmp_path,
        )
        entry = lookup_last_run(key, base_dir=tmp_path)
        assert entry is not None
        assert entry["run_id"] == "run_001"
        assert entry["status"] == "in_progress"
        assert entry["item_count"] == 12
        assert entry["dataset_path"].endswith("data.xlsx")
        assert "updated_at" in entry

    def test_record_is_last_wins_per_key(self, tmp_path: Path) -> None:
        key = compute_resume_key("g", "https://a.com")
        record_run(key, "run_001", item_count=5, base_dir=tmp_path)
        record_run(key, "run_002", item_count=9, base_dir=tmp_path)
        entry = lookup_last_run(key, base_dir=tmp_path)
        assert entry is not None
        assert entry["run_id"] == "run_002"
        assert entry["item_count"] == 9

    def test_distinct_keys_isolated(self, tmp_path: Path) -> None:
        k1 = compute_resume_key("g1", "https://a.com")
        k2 = compute_resume_key("g2", "https://a.com")
        record_run(k1, "run_a", base_dir=tmp_path)
        record_run(k2, "run_b", base_dir=tmp_path)
        assert lookup_last_run(k1, base_dir=tmp_path)["run_id"] == "run_a"
        assert lookup_last_run(k2, base_dir=tmp_path)["run_id"] == "run_b"


class TestRobustness:
    def test_index_lives_at_runs_root(self, tmp_path: Path) -> None:
        assert resume_index_path(base_dir=tmp_path) == tmp_path / RESUME_INDEX_FILENAME

    def test_corrupt_index_degrades_to_none(self, tmp_path: Path) -> None:
        (tmp_path / RESUME_INDEX_FILENAME).write_text("{ not json", encoding="utf-8")
        assert lookup_last_run("k", base_dir=tmp_path) is None

    def test_corrupt_index_is_recoverable_on_write(self, tmp_path: Path) -> None:
        (tmp_path / RESUME_INDEX_FILENAME).write_text("garbage", encoding="utf-8")
        key = compute_resume_key("g", "https://a.com")
        record_run(key, "run_x", base_dir=tmp_path)
        entry = lookup_last_run(key, base_dir=tmp_path)
        assert entry is not None and entry["run_id"] == "run_x"

    def test_atomic_write_leaves_no_tmp(self, tmp_path: Path) -> None:
        key = compute_resume_key("g", "https://a.com")
        record_run(key, "run_x", base_dir=tmp_path)
        leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []

    def test_index_is_valid_json_mapping(self, tmp_path: Path) -> None:
        key = compute_resume_key("g", "https://a.com")
        record_run(key, "run_x", base_dir=tmp_path)
        raw = json.loads((tmp_path / RESUME_INDEX_FILENAME).read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        assert key in raw
        assert raw[key]["run_id"] == "run_x"

import json
import os
import shutil
from pathlib import Path

from visual_web_agent.extraction_engine.snapshots import (
    build_snapshot,
    compare_latest_snapshots,
    compare_snapshots,
    maybe_save_snapshot,
    replay_snapshot,
    save_snapshot,
    should_capture_snapshot,
)


def test_snapshot_replay_reports_field_coverage() -> None:
    snapshot = build_snapshot(
        url="https://example.com/table",
        goal="extract Name and Office",
        source="DOM_TABLE",
        rows=[
            {"Name": "Alice", "Office": "Tokyo"},
            {"Name": "Bob", "Office": ""},
        ],
        requested_fields=["Name", "Office"],
        candidates=[{"name": "DOM_TABLE", "accepted": 2, "score": 200}],
    )

    replay = replay_snapshot(snapshot.to_dict())

    assert replay["row_count"] == 2
    assert replay["unique_row_count"] == 2
    assert replay["field_coverage"]["complete_rows"] == 1
    assert replay["field_coverage"]["missing_by_field"]["Office"] == 1
    assert replay["selected_candidate"]["name"] == "DOM_TABLE"
    assert replay["selector_fingerprints"]["source_family"] == "DOM_TABLE"
    assert replay["selector_fingerprints"]["fields"] == ["Name", "Office"]


def test_maybe_save_snapshot_writes_json() -> None:
    out_dir = Path("workspace") / "test_snapshots"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    path = maybe_save_snapshot(
        url="https://example.com/table",
        goal="extract rows",
        source="DOM_TABLE",
        rows=[{"Name": "Alice"}],
        requested_fields=["Name"],
        directory=out_dir,
    )

    try:
        assert path is not None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["source"] == "DOM_TABLE"
        assert data["row_count"] == 1
        assert data["field_coverage"]["complete_rows"] == 1
        assert data["selector_fingerprints"]["source_family"] == "DOM_TABLE"
    finally:
        if out_dir.exists():
            shutil.rmtree(out_dir)


def test_snapshot_capture_skips_sensitive_targets() -> None:
    assert should_capture_snapshot("https://example.com/login", "extract rows") is False
    assert should_capture_snapshot("https://example.com/data", "输入密码后提取") is False


def test_compare_snapshots_scores_matching_surfaces() -> None:
    baseline = build_snapshot(
        url="https://example.com/table",
        goal="extract employees",
        source="DOM_TABLE",
        rows=[
            {"Name": "Alice", "Position": "Engineer", "Office": "Tokyo"},
            {"Name": "Bob", "Position": "Designer", "Office": "London"},
        ],
        requested_fields=["Name", "Position", "Office"],
        data_shape={"table_rows": 2, "table_cells": 3},
    ).to_dict()
    candidate = build_snapshot(
        url="https://example.com/table",
        goal="extract employees",
        source="DOM_TABLE",
        rows=[
            {"Name": "Carol", "Position": "Manager", "Office": "Paris"},
            {"Name": "Dan", "Position": "Engineer", "Office": "Berlin"},
        ],
        requested_fields=["Name", "Position", "Office"],
        data_shape={"table_rows": 2, "table_cells": 3},
    ).to_dict()

    result = compare_snapshots(baseline, candidate)

    assert result["match"] is True
    assert result["score"] >= 0.9
    assert result["row_delta"] == 0
    assert result["field_delta"]["added"] == []
    assert result["field_delta"]["removed"] == []


def test_compare_snapshots_reports_field_drift() -> None:
    baseline = build_snapshot(
        url="https://example.com/table",
        goal="extract employees",
        source="DOM_TABLE",
        rows=[{"Name": "Alice", "Office": "Tokyo"}],
        requested_fields=["Name", "Office"],
    ).to_dict()
    candidate = build_snapshot(
        url="https://example.com/table",
        goal="extract movies",
        source="DOM_LIST",
        rows=[{"Title": "Movie", "Rating": "9.0"}],
        requested_fields=["Title", "Rating"],
    ).to_dict()

    result = compare_snapshots(baseline, candidate)

    assert result["match"] is False
    assert "Title" in result["field_delta"]["added"]
    assert "Office" in result["field_delta"]["removed"]


def test_compare_latest_snapshots_groups_recent_pairs() -> None:
    out_dir = Path("workspace") / "test_snapshot_compare_latest"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    try:
        first = save_snapshot(
            build_snapshot(
                url="https://example.com/table",
                goal="extract employees",
                source="DOM_TABLE",
                rows=[{"Name": "Alice", "Office": "Tokyo"}],
                requested_fields=["Name", "Office"],
            ),
            directory=out_dir,
        )
        second = save_snapshot(
            build_snapshot(
                url="https://example.com/table",
                goal="extract employees",
                source="DOM_TABLE",
                rows=[{"Name": "Bob", "Office": "London"}],
                requested_fields=["Name", "Office"],
            ),
            directory=out_dir,
        )
        os.utime(first, (1, 1))
        os.utime(second, (2, 2))

        results = compare_latest_snapshots(out_dir)

        assert len(results) == 1
        assert results[0]["match"] is True
        assert results[0]["baseline_path"] == str(first)
        assert results[0]["candidate_path"] == str(second)
    finally:
        if out_dir.exists():
            shutil.rmtree(out_dir)


def test_compare_latest_snapshots_respects_zero_limit() -> None:
    assert compare_latest_snapshots(Path("workspace") / "missing_snapshot_dir", limit=0) == []

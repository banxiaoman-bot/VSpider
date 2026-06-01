import os
import shutil
from pathlib import Path

from visual_web_agent.extraction_engine.recovery import (
    load_recovery_baselines,
    rank_extraction_candidates_with_history,
)
from visual_web_agent.extraction_engine.snapshots import build_snapshot, save_snapshot


def test_loads_baselines_for_same_url_and_fields() -> None:
    out_dir = Path("workspace") / "test_recovery_baselines"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    try:
        matched = save_snapshot(
            build_snapshot(
                url="https://example.com/table?page=1",
                goal="extract employees",
                source="DOM_TABLE",
                rows=[{"Name": "Alice", "Office": "Tokyo"}],
                requested_fields=["Name", "Office"],
            ),
            directory=out_dir,
        )
        other = save_snapshot(
            build_snapshot(
                url="https://example.com/table",
                goal="extract movies",
                source="DOM_LIST",
                rows=[{"Title": "Movie"}],
                requested_fields=["Title"],
            ),
            directory=out_dir,
        )
        os.utime(matched, (2, 2))
        os.utime(other, (3, 3))

        baselines = load_recovery_baselines(
            url="https://example.com/table?page=2",
            requested_fields=["Office", "Name"],
            directory=out_dir,
        )

        assert len(baselines) == 1
        assert baselines[0]["_snapshot_path"] == str(matched)
    finally:
        if out_dir.exists():
            shutil.rmtree(out_dir)


def test_history_boosts_matching_candidate() -> None:
    out_dir = Path("workspace") / "test_recovery_rank"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    try:
        save_snapshot(
            build_snapshot(
                url="https://example.com/table",
                goal="extract employees",
                source="DOM_TABLE",
                rows=[
                    {"Name": "Alice", "Position": "Engineer", "Office": "Tokyo"},
                    {"Name": "Bob", "Position": "Designer", "Office": "London"},
                ],
                requested_fields=["Name", "Position", "Office"],
                data_shape={"table_rows": 2, "table_cells": 3},
            ),
            directory=out_dir,
        )
        candidates = [
            {
                "name": "DOM_TABLE",
                "rows": [{"Name": "Carol", "Position": "Manager", "Office": "Paris"}],
                "accepted": 1,
                "score": 10.0,
                "data_shape": {"table_rows": 1, "table_cells": 3},
            },
            {
                "name": "DOM_LIST",
                "rows": [{"Title": "Movie", "Rating": "9.0"}],
                "accepted": 1,
                "score": 10.0,
                "data_shape": {"repeated_class_count": 1},
            },
        ]

        ranked = rank_extraction_candidates_with_history(
            candidates,
            url="https://example.com/table",
            requested_fields=["Name", "Position", "Office"],
            directory=out_dir,
        )

        table = next(candidate for candidate in ranked if candidate["name"] == "DOM_TABLE")
        listing = next(candidate for candidate in ranked if candidate["name"] == "DOM_LIST")
        assert table["score"] > 10.0
        assert table["recovery"]["boost"] > 0
        assert table["recovery"]["match"] is True
        assert listing["score"] == 10.0
    finally:
        if out_dir.exists():
            shutil.rmtree(out_dir)


def test_history_absent_leaves_candidates_unchanged() -> None:
    ranked = rank_extraction_candidates_with_history(
        [{"name": "DOM_TABLE", "rows": [{"Name": "Alice"}], "accepted": 1, "score": 7.0}],
        url="https://example.com/missing",
        requested_fields=["Name"],
        directory=Path("workspace") / "missing_recovery_dir",
    )

    assert ranked[0]["score"] == 7.0
    assert "recovery" not in ranked[0]

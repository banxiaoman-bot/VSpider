from visual_web_agent.extraction_engine.selectors import (
    build_selector_fingerprints,
    score_fingerprint_match,
)


ROWS = [
    {"Name": "Alice", "Position": "Engineer", "Office": "Tokyo"},
    {"Name": "Bob", "Position": "Designer", "Office": "London"},
]


def test_builds_table_fingerprint() -> None:
    fp = build_selector_fingerprints(
        rows=ROWS,
        requested_fields=["Name", "Position", "Office"],
        source="DOM_TABLE",
        data_shape={"table_rows": 2, "table_cells": 3, "table_count": 1},
    )

    assert fp["source_family"] == "DOM_TABLE"
    assert fp["fields"] == ["Name", "Position", "Office"]
    assert fp["selector_like"] == ["table -> tbody/tr -> cells"]
    assert fp["signature"]
    assert fp["row_widths"] == {3: 2}


def test_scores_matching_fingerprints() -> None:
    baseline = build_selector_fingerprints(
        rows=ROWS,
        requested_fields=["Name", "Position", "Office"],
        source="DOM_TABLE",
        data_shape={"table_rows": 2, "table_cells": 3},
    )
    candidate = build_selector_fingerprints(
        rows=[
            {"Name": "Carol", "Position": "Manager", "Office": "Paris"},
            {"Name": "Dan", "Position": "Engineer", "Office": "Berlin"},
        ],
        requested_fields=["Name", "Position", "Office"],
        source="DOM_TABLE",
        data_shape={"table_rows": 2, "table_cells": 3},
    )

    result = score_fingerprint_match(baseline, candidate)

    assert result["match"] is True
    assert result["score"] >= 0.9
    assert "fields" in result["reasons"]


def test_scores_different_fingerprints_lower() -> None:
    baseline = build_selector_fingerprints(
        rows=ROWS,
        requested_fields=["Name", "Position", "Office"],
        source="DOM_TABLE",
        data_shape={"table_rows": 2, "table_cells": 3},
    )
    candidate = build_selector_fingerprints(
        rows=[{"Title": "Movie", "Rating": "9.0"}],
        requested_fields=["Title", "Rating"],
        source="DOM_LIST",
        data_shape={"repeated_class_count": 1},
    )

    result = score_fingerprint_match(baseline, candidate)

    assert result["match"] is False
    assert result["score"] < 0.5

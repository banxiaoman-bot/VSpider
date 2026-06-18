"""C1: extraction selector recovery — gated baseline re-selection.

The existing ``rank_extraction_candidates_with_history`` only *boosts* a
matching candidate's score; when the live page drifts so hard that the current
extraction collapses (no accepted rows / required fields missing), boosting does
nothing because there is nothing good to boost. C1 adds a strictly-gated
recovery hint: when the current extraction is weak AND a strong historical
baseline exists for the same URL/field group, surface the baseline's
source-family + expected fields so the caller can re-attempt extraction biased
to the known-good surface. It is advisory only — it never fabricates rows.
"""

from __future__ import annotations

from pathlib import Path

from visual_web_agent.extraction_engine.recovery import recover_extraction_selectors
from visual_web_agent.extraction_engine.snapshots import build_snapshot, save_snapshot


_URL = "https://example.com/employees"
_FIELDS = ["Name", "Position", "Office"]


def _save_strong_baseline(directory: Path) -> None:
    save_snapshot(
        build_snapshot(
            url=_URL,
            goal="extract employees",
            source="DOM_TABLE",
            rows=[
                {"Name": "Alice", "Position": "Engineer", "Office": "Tokyo"},
                {"Name": "Bob", "Position": "Designer", "Office": "London"},
            ],
            requested_fields=_FIELDS,
            data_shape={"table_rows": 2, "table_cells": 3},
        ),
        directory=directory,
    )


def test_collapsed_extraction_recovers_from_strong_baseline(tmp_path: Path) -> None:
    _save_strong_baseline(tmp_path)
    result = recover_extraction_selectors(
        [],  # extraction produced nothing usable
        url=_URL,
        requested_fields=_FIELDS,
        directory=tmp_path,
    )
    assert result["recovered"] is True
    assert result["source_family"] == "DOM_TABLE"
    assert result["selector_like"]
    assert set(result["expected_fields"]) == set(_FIELDS)
    assert result["current_coverage"] == 0.0
    assert result["baseline_coverage"] >= 0.8


def test_weak_partial_extraction_recovers(tmp_path: Path) -> None:
    _save_strong_baseline(tmp_path)
    candidates = [
        {
            "name": "DOM_LIST",
            "rows": [{"Name": "Carol"}],  # only 1/3 fields covered
            "accepted": 1,
            "score": 10.0,
        }
    ]
    result = recover_extraction_selectors(
        candidates,
        url=_URL,
        requested_fields=_FIELDS,
        directory=tmp_path,
    )
    assert result["recovered"] is True
    assert result["current_coverage"] <= 0.5


def test_healthy_extraction_is_not_recovered(tmp_path: Path) -> None:
    _save_strong_baseline(tmp_path)
    candidates = [
        {
            "name": "DOM_TABLE",
            "rows": [{"Name": "Carol", "Position": "Manager", "Office": "Paris"}],
            "accepted": 1,
            "score": 12.0,
        }
    ]
    result = recover_extraction_selectors(
        candidates,
        url=_URL,
        requested_fields=_FIELDS,
        directory=tmp_path,
    )
    assert result["recovered"] is False
    assert result["reason"] == "current_extraction_healthy"


def test_no_baseline_means_no_recovery(tmp_path: Path) -> None:
    result = recover_extraction_selectors(
        [],
        url="https://example.com/nothing-here",
        requested_fields=_FIELDS,
        directory=tmp_path,
    )
    assert result["recovered"] is False
    assert result["reason"] == "no_baseline"


def test_weak_baseline_does_not_trigger_recovery(tmp_path: Path) -> None:
    url = "https://example.com/weak"
    save_snapshot(
        build_snapshot(
            url=url,
            goal="extract employees",
            source="DOM_TABLE",
            rows=[{"Name": "Alice"}],  # baseline itself only covers 1/3 fields
            requested_fields=_FIELDS,
        ),
        directory=tmp_path,
    )
    result = recover_extraction_selectors(
        [],
        url=url,
        requested_fields=_FIELDS,
        directory=tmp_path,
    )
    assert result["recovered"] is False
    assert result["reason"] == "no_strong_baseline"

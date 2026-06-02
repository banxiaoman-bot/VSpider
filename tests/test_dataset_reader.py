"""Tests for ``read_dataset_rows`` (RUN-RESUME1 step 3a-wire-1).

The deterministic bridge that lets a *resumed* run load the prior run's dataset
artifact back into ``list[dict]`` so :func:`rebuild_seen_fingerprints` can
reconstruct the dedup seen-set. A run may write its dataset as xlsx / csv /
jsonl / json (chosen by ``output_contract.container``); the reader must read any
of them back uniformly and -- critically -- coerce missing/NaN cells to ``""``
so they never leak a ``"nan"`` token into a fingerprint (which would silently
break resume dedup).

Tolerant by contract: a missing / corrupt / unsupported file degrades to ``[]``
so a damaged artifact never aborts a resumed run. Mirrors the
``run_resume_index`` / ``rebuild_seen_fingerprints`` slice test style.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from visual_web_agent.data_sanitizer import (
    rebuild_seen_fingerprints,
    sanitize_extracted_rows,
)
from visual_web_agent.dataset_reader import read_dataset_rows


_ROWS = [
    {"title": "Hello World Product", "url": "https://shop.example.com/p/1"},
    {"title": "Second Great Item", "url": "https://shop.example.com/p/2"},
    {"title": "Third Amazing Thing", "url": "https://shop.example.com/p/3"},
]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


class TestReadFormats:
    def test_read_jsonl(self, tmp_path: Path) -> None:
        target = tmp_path / "dataset.jsonl"
        _write_jsonl(target, _ROWS)
        assert read_dataset_rows(target) == _ROWS

    def test_read_json_array(self, tmp_path: Path) -> None:
        target = tmp_path / "dataset.json"
        target.write_text(json.dumps(_ROWS, ensure_ascii=False), encoding="utf-8")
        assert read_dataset_rows(target) == _ROWS

    def test_read_csv(self, tmp_path: Path) -> None:
        pd = pytest.importorskip("pandas")
        target = tmp_path / "dataset.csv"
        pd.DataFrame(_ROWS).to_csv(target, index=False)
        rows = read_dataset_rows(target)
        assert [r["url"] for r in rows] == [r["url"] for r in _ROWS]

    def test_read_xlsx(self, tmp_path: Path) -> None:
        pytest.importorskip("pandas")
        pytest.importorskip("openpyxl")
        import pandas as pd

        target = tmp_path / "dataset.xlsx"
        pd.DataFrame(_ROWS).to_excel(target, index=False)
        rows = read_dataset_rows(target)
        assert [r["url"] for r in rows] == [r["url"] for r in _ROWS]

    def test_accepts_str_path(self, tmp_path: Path) -> None:
        target = tmp_path / "dataset.jsonl"
        _write_jsonl(target, _ROWS)
        assert read_dataset_rows(str(target)) == _ROWS


class TestTolerance:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert read_dataset_rows(tmp_path / "nope.jsonl") == []

    def test_empty_path_returns_empty(self) -> None:
        assert read_dataset_rows("") == []
        assert read_dataset_rows(None) == []  # type: ignore[arg-type]

    def test_unsupported_suffix_returns_empty(self, tmp_path: Path) -> None:
        target = tmp_path / "dataset.bin"
        target.write_bytes(b"\x00\x01\x02")
        assert read_dataset_rows(target) == []

    def test_corrupt_jsonl_skips_bad_lines(self, tmp_path: Path) -> None:
        target = tmp_path / "dataset.jsonl"
        target.write_text(
            json.dumps(_ROWS[0]) + "\n"
            + "{not valid json\n"
            + json.dumps(_ROWS[1]) + "\n",
            encoding="utf-8",
        )
        rows = read_dataset_rows(target)
        assert rows == [_ROWS[0], _ROWS[1]]

    def test_corrupt_json_returns_empty(self, tmp_path: Path) -> None:
        target = tmp_path / "dataset.json"
        target.write_text("{ broken", encoding="utf-8")
        assert read_dataset_rows(target) == []

    def test_non_dict_json_items_skipped(self, tmp_path: Path) -> None:
        target = tmp_path / "dataset.json"
        target.write_text(json.dumps([_ROWS[0], 7, "x", None, _ROWS[1]]), encoding="utf-8")
        assert read_dataset_rows(target) == [_ROWS[0], _ROWS[1]]


class TestNaNHygiene:
    """Blank cells must not become ``"nan"`` -- that would corrupt fingerprints."""

    def test_xlsx_blank_cells_become_empty(self, tmp_path: Path) -> None:
        pytest.importorskip("pandas")
        pytest.importorskip("openpyxl")
        import pandas as pd

        target = tmp_path / "dataset.xlsx"
        pd.DataFrame(
            [
                {"title": "Has Both", "note": "present"},
                {"title": "Missing Note", "note": None},
            ]
        ).to_excel(target, index=False)
        rows = read_dataset_rows(target)
        assert rows[1]["note"] == ""
        # No value anywhere should serialise to the pandas NaN token.
        assert all("nan" != str(v).lower() for r in rows for v in r.values())

    def test_blank_cells_do_not_pollute_fingerprints(self, tmp_path: Path) -> None:
        pytest.importorskip("pandas")
        pytest.importorskip("openpyxl")
        import pandas as pd

        target = tmp_path / "dataset.xlsx"
        pd.DataFrame(_ROWS).to_excel(target, index=False)
        rows = read_dataset_rows(target)
        seen, _ = rebuild_seen_fingerprints(rows)
        assert not any("nan" in fp.lower() for fp in seen)


class TestResumeRoundTrip:
    """End-to-end: a stored dataset, read back, suppresses re-extraction."""

    @pytest.mark.parametrize("ext", ["jsonl", "json", "csv", "xlsx"])
    def test_reader_rebuild_dedups_reextracted_rows(self, tmp_path: Path, ext: str) -> None:
        source = (
            "Hello World Product https://shop.example.com/p/1 "
            "Second Great Item https://shop.example.com/p/2 "
            "Third Amazing Thing https://shop.example.com/p/3"
        )
        run1 = sanitize_extracted_rows(_ROWS, source, seen_fingerprints=set())
        assert run1.accepted == 3

        target = tmp_path / f"dataset.{ext}"
        if ext == "jsonl":
            _write_jsonl(target, run1.rows)
        elif ext == "json":
            target.write_text(json.dumps(run1.rows, ensure_ascii=False), encoding="utf-8")
        else:
            pd = pytest.importorskip("pandas")
            if ext == "xlsx":
                pytest.importorskip("openpyxl")
                pd.DataFrame(run1.rows).to_excel(target, index=False)
            else:
                pd.DataFrame(run1.rows).to_csv(target, index=False)

        restored = read_dataset_rows(target)
        seen, count = rebuild_seen_fingerprints(restored)
        assert count == 3

        run2 = sanitize_extracted_rows(_ROWS, source, seen_fingerprints=seen)
        assert run2.accepted == 0
        assert run2.duplicates == 3

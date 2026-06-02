"""Tests for ``rebuild_seen_fingerprints`` (RUN-RESUME1 step 3a-core).

The deterministic bridge that lets a *resumed* run skip rows already written to
the prior run's dataset: rebuild the dedup seen-fingerprint set + accepted count
from previously-extracted rows, reusing the exact same fingerprint primitives
the live sanitizer uses, so re-extracted rows are recognised as duplicates and
only genuinely new rows are appended. Pure function, no I/O.

Mirrors the ``run_resume_index`` / ``run_checkpoint`` slice test style.
"""

from __future__ import annotations

from visual_web_agent.data_sanitizer import (
    rebuild_seen_fingerprints,
    sanitize_extracted_rows,
)


# Rows carrying a URL identity key (``link``); at write time the sanitizer
# canonicalises ``link`` -> ``url``, so rebuilding from the stored form
# exercises identity fingerprint stability across that rename.
_ROWS = [
    {"title": "Hello World Product", "link": "https://shop.example.com/p/1"},
    {"title": "Second Great Item", "link": "https://shop.example.com/p/2"},
    {"title": "Third Amazing Thing", "link": "https://shop.example.com/p/3"},
]
_SOURCE = (
    "Hello World Product https://shop.example.com/p/1 "
    "Second Great Item https://shop.example.com/p/2 "
    "Third Amazing Thing https://shop.example.com/p/3"
)


class TestRebuildBasics:
    def test_empty_input(self) -> None:
        seen, count = rebuild_seen_fingerprints([])
        assert seen == set()
        assert count == 0

    def test_none_input(self) -> None:
        seen, count = rebuild_seen_fingerprints(None)
        assert seen == set()
        assert count == 0

    def test_skips_non_dict_and_empty_rows(self) -> None:
        seen, count = rebuild_seen_fingerprints([None, 7, "x", {}, {"  ": ""}])
        assert seen == set()
        assert count == 0

    def test_count_matches_valid_rows(self) -> None:
        seen, count = rebuild_seen_fingerprints(_ROWS)
        assert count == 3
        assert len(seen) >= 3  # at least one fingerprint per row

    def test_deterministic(self) -> None:
        assert rebuild_seen_fingerprints(_ROWS) == rebuild_seen_fingerprints(_ROWS)


class TestResumeDedupInvariant:
    """The contract that matters: a rebuilt seen set suppresses re-extraction."""

    def test_rebuilt_set_dedups_reextracted_rows(self) -> None:
        # Run 1: fresh extraction populates the dataset (canonical rows).
        run1 = sanitize_extracted_rows(_ROWS, _SOURCE, seen_fingerprints=set())
        assert run1.accepted == 3

        # Rebuild the seen set from the stored *canonical* rows (link -> url).
        seen, count = rebuild_seen_fingerprints(run1.rows)
        assert count == 3

        # Run 2 (resume): the same rows re-extracted are all duplicates.
        run2 = sanitize_extracted_rows(_ROWS, _SOURCE, seen_fingerprints=seen)
        assert run2.accepted == 0
        assert run2.duplicates == 3

    def test_new_rows_still_accepted_after_resume(self) -> None:
        run1 = sanitize_extracted_rows(_ROWS, _SOURCE, seen_fingerprints=set())
        seen, _ = rebuild_seen_fingerprints(run1.rows)

        new_rows = [
            {"title": "Fourth Fresh Entry", "link": "https://shop.example.com/p/4"},
        ]
        new_source = "Fourth Fresh Entry https://shop.example.com/p/4"
        run2 = sanitize_extracted_rows(new_rows, new_source, seen_fingerprints=seen)
        assert run2.accepted == 1
        assert run2.duplicates == 0

    def test_rebuilt_set_superset_of_sanitizer_fingerprints(self) -> None:
        run1 = sanitize_extracted_rows(_ROWS, _SOURCE, seen_fingerprints=set())
        seen, _ = rebuild_seen_fingerprints(run1.rows)
        # Every fingerprint the sanitizer recorded for the accepted rows must be
        # present in the rebuilt set, or resume dedup would silently miss rows.
        assert run1.fingerprints <= seen

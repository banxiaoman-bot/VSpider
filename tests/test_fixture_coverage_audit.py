"""FIXTURE-AUDIT-1: capability failure-fixture coverage, locked.

Audit findings (2026-06-11, all verified by running the chain):

  - every mapped failure code (9 in ``_FAILURE_REPAIR_HINTS``), the
    ``action_error`` producer fallback, AND a completely unmapped code all
    build a ``capability_failure_regression_fixture.v1`` and replay GREEN
    through the real planner_feedback -> route_task -> execution_plan
    chain (13 checks each);
  - ``_FAILURE_REPAIR_HINTS`` (repair layer) and the planner-feedback
    mappings agree: ``preferred_capabilities`` are identical per code, and
    every hint code has a SPECIFIC avoid list in both layers (the wording
    differs by design - route_task overlays the repair hints on top of the
    feedback, so the repair vocabulary wins in the final route);
  - the disk round-trip (write fixture -> read back -> replay) holds.

These tests freeze that state: adding a failure code to one table but not
the other, breaking the unmapped-code fallback, or bending the replay
checks now fails loudly here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from visual_web_agent.capability_failure_fixture import (
    build_capability_failure_regression_fixture,
    write_capability_failure_regression_fixture,
)
from visual_web_agent.capability_failure_replay import (
    replay_capability_failure_fixture,
    replay_capability_failure_fixtures,
)
from visual_web_agent.capability_router import (
    _FAILURE_REPAIR_HINTS,
    _planner_feedback_avoid_actions,
    _planner_feedback_preferred_capabilities,
    build_failure_repair_feedback,
)


def _bundle(code: str, *, action: str = "click") -> dict:
    return {
        "version": "capability_execute_failure_bundle.v1",
        "source": "capability_execute",
        "status": "error",
        "primary_failure": code,
        "failure_category": "browser_action",
        "action": action,
        "capability": "browser_control",
        "recommended_actions": ["inspect_browser_action"],
    }


_ALL_CODES = sorted(_FAILURE_REPAIR_HINTS) + ["action_error", "totally_unmapped_code"]


class TestEveryFailureCodeRepaysGreen:
    """Mapped, fallback, and unmapped codes all close the fixture loop."""

    @pytest.mark.parametrize("code", _ALL_CODES)
    def test_fixture_builds_and_replays(self, code: str) -> None:
        fixture = build_capability_failure_regression_fixture(
            {"failure_bundle": _bundle(code)}
        )
        assert fixture.get("version") == "capability_failure_regression_fixture.v1"
        assert fixture["expected"]["primary_failure"] == code
        report = replay_capability_failure_fixture(fixture)
        failed = [c["name"] for c in report["checks"] if not c["passed"]]
        assert report["passed"] is True, f"{code}: failed checks {failed}"

    def test_known_hint_codes_are_exactly_nine(self) -> None:
        # A new failure code must be added to BOTH mapping layers and get a
        # parametrized loop run above - bump this list consciously.
        assert sorted(_FAILURE_REPAIR_HINTS) == [
            "backend_unavailable",
            "click_intercepted",
            "context_closed",
            "element_disabled",
            "element_not_visible",
            "input_rejected",
            "navigation_failed",
            "selector_missing",
            "timeout",
        ]


class TestRepairAndFeedbackLayersStayAligned:
    def test_preferred_capabilities_identical_per_code(self) -> None:
        for code, hints in _FAILURE_REPAIR_HINTS.items():
            assert _planner_feedback_preferred_capabilities(code, []) == list(
                hints["preferred_capabilities"]
            ), f"{code}: repair layer and feedback layer disagree"

    def test_every_hint_code_has_specific_avoid_lists_in_both_layers(self) -> None:
        default_avoid = _planner_feedback_avoid_actions("__nope__")
        for code, hints in _FAILURE_REPAIR_HINTS.items():
            assert hints.get("avoid_actions"), f"{code}: repair layer avoid empty"
            assert _planner_feedback_avoid_actions(code) != default_avoid, (
                f"{code}: feedback layer fell back to the default avoid list"
            )

    def test_unmapped_code_still_yields_actionable_defaults(self) -> None:
        feedback = build_failure_repair_feedback("weird_new_failure")
        assert feedback["repair_actions"]
        assert feedback["avoid_actions"]
        assert feedback["preferred_capabilities"]


class TestDiskRoundTrip:
    def test_write_then_replay_from_disk(self, tmp_path: Path) -> None:
        path = write_capability_failure_regression_fixture(
            {"failure_bundle": _bundle("timeout")}, tmp_path, name="audit_timeout"
        )
        fixture = json.loads(path.read_text(encoding="utf-8"))
        report = replay_capability_failure_fixture(fixture)
        assert report["passed"] is True

    def test_bundleless_source_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            write_capability_failure_regression_fixture(
                {"no_bundle": True}, tmp_path, name="nope"
            )


class TestBatchReportAggregation:
    def test_batch_counts_codes_and_passes(self) -> None:
        fixtures = [
            build_capability_failure_regression_fixture(
                {"failure_bundle": _bundle(code)}
            )
            for code in ("timeout", "selector_missing", "timeout")
        ]
        batch = replay_capability_failure_fixtures(fixtures)
        assert batch["passed"] is True
        assert batch["fixture_count"] == 3
        top = {row["name"]: row["count"] for row in batch["summary"]["top_primary_failures"]}
        assert top == {"timeout": 2, "selector_missing": 1}

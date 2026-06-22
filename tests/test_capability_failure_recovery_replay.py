"""C2: failure-fixture replay loop deepened with a recovery-decision replay.

The existing ``capability_failure_replay`` only verified the planner-feedback
contract wiring (route_task -> execution_plan). C2 adds an *offline* check that
the captured failure would actually drive a self-healing recovery decision
(``self_healing_policy.v1``), closing the loop from "we detected the failure"
to "we would actually recover". The new section is purely additive and must NOT
change the locked ``report['passed']`` planner-feedback gate (FIXTURE-AUDIT-1).
"""

from __future__ import annotations

from visual_web_agent.capability_failure_fixture import (
    build_capability_failure_regression_fixture,
)
from visual_web_agent.capability_failure_replay import (
    replay_capability_failure_fixture,
    replay_capability_failure_fixtures,
    replay_recovery_decision,
)


def _rich_bundle(code: str = "selector_missing", *, action: str = "click") -> dict:
    """A failure bundle that carries an action_trace (real captured failures do)."""
    return {
        "version": "capability_execute_failure_bundle.v1",
        "source": "capability_execute",
        "status": "error",
        "primary_failure": code,
        "failure_category": "browser_action",
        "action": action,
        "capability": "browser_control",
        "recommended_actions": ["inspect_browser_action"],
        "action_trace": {
            "version": "browser_action_trace.v1",
            "action": action,
            "status": "error",
            "warning_codes": ["action_failed"],
            "result_summary": {
                "failure_code": code,
                "failure_category": "target_resolution",
            },
        },
    }


def _minimal_bundle(code: str = "timeout", *, action: str = "click") -> dict:
    """A failure bundle with no action_trace (e.g. early/coarse failures)."""
    return {
        "version": "capability_execute_failure_bundle.v1",
        "status": "error",
        "primary_failure": code,
        "failure_category": "browser_action",
        "action": action,
        "capability": "browser_control",
        "recommended_actions": ["inspect_browser_action"],
    }


class TestRecoveryDecisionReplay:
    def test_rich_bundle_yields_recovery_decision(self) -> None:
        fixture = build_capability_failure_regression_fixture(
            {"failure_bundle": _rich_bundle()}
        )
        report = replay_capability_failure_fixture(fixture)
        rd = report["recovery_decision"]
        assert rd["version"] == "capability_failure_recovery_decision_replay.v1"
        assert rd["has_action_trace"] is True
        assert rd["has_recovery_decision"] is True
        assert rd["risk_level"] in {"medium", "high"}
        assert rd["recommended_actions"] and rd["recommended_actions"] != ["continue"]
        # selector_missing must steer toward a similar-selector recovery
        assert "use_similar_selector" in rd["recommended_actions"]

    def test_recovery_decision_does_not_break_planner_feedback_gate(self) -> None:
        # Backward-compat guard: FIXTURE-AUDIT-1 locks report['passed'].
        rich = replay_capability_failure_fixture(
            build_capability_failure_regression_fixture(
                {"failure_bundle": _rich_bundle()}
            )
        )
        minimal = replay_capability_failure_fixture(
            build_capability_failure_regression_fixture(
                {"failure_bundle": _minimal_bundle()}
            )
        )
        assert rich["passed"] is True
        assert minimal["passed"] is True

    def test_minimal_bundle_has_no_recovery_decision(self) -> None:
        fixture = build_capability_failure_regression_fixture(
            {"failure_bundle": _minimal_bundle()}
        )
        report = replay_capability_failure_fixture(fixture)
        rd = report["recovery_decision"]
        assert rd["has_action_trace"] is False
        assert rd["has_recovery_decision"] is False

    def test_direct_replay_recovery_decision(self) -> None:
        rd = replay_recovery_decision(_rich_bundle())
        assert rd["has_action_trace"] is True
        assert rd["has_recovery_decision"] is True
        assert rd["requires_planner_replan"] is True

    def test_direct_replay_handles_empty_bundle(self) -> None:
        rd = replay_recovery_decision({})
        assert rd["has_action_trace"] is False
        assert rd["has_recovery_decision"] is False


class TestBatchRecoveryDecisionAggregation:
    def test_batch_counts_recovery_decisions(self) -> None:
        fixtures = [
            build_capability_failure_regression_fixture(
                {"failure_bundle": _rich_bundle()}
            ),
            build_capability_failure_regression_fixture(
                {"failure_bundle": _rich_bundle(action="type")}
            ),
            build_capability_failure_regression_fixture(
                {"failure_bundle": _minimal_bundle()}
            ),
        ]
        batch = replay_capability_failure_fixtures(fixtures)
        assert batch["passed"] is True
        assert batch["fixture_count"] == 3
        assert batch["summary"]["recovery_decision_count"] == 2

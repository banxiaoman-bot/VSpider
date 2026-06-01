"""I1/I3/I4: pin the ``broadcast_phase`` emit sites in main.py.

The G2 channel is only useful if main.py actually emits events. Source-level
invariants confirm the wiring exists at the key checkpoints:

* **action** — after every VLM-driven action execution (I1)
* **finalize** — alongside ``broadcast_done`` (I3)
* **guard** — when ``session_drop_guard`` rewrites the head action (I4)

Each emit must be:
  - wrapped in try/except (so CLI-only runs without api_server stay safe);
  - call ``broadcast_phase`` from ``api_server``;
  - pass the correct phase name and severity rules.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


MAIN_PATH = Path(__file__).resolve().parent.parent / "visual_web_agent" / "main.py"


@pytest.fixture(scope="module")
def src() -> str:
    return MAIN_PATH.read_text(encoding="utf-8")


# ── I1: action phase emit ──────────────────────────────────────────────


class TestActionPhaseEmit:
    def test_action_t0_recorded_before_exec(self, src: str) -> None:
        # Pre-action timestamp must be set so duration_ms is always valid
        assert "_action_t0 = time.time()" in src

    def test_emit_calls_broadcast_phase_action(self, src: str) -> None:
        m = re.search(
            r'broadcast_phase\(\s*"action",\s*severity=', src,
        )
        assert m, "expected broadcast_phase('action', severity=...) emit"

    def test_severity_error_when_action_failed(self, src: str) -> None:
        m = re.search(
            r'severity="error" if _act_err else "info"',
            src,
        )
        assert m, "action phase severity must flip to 'error' when handler errored"

    def test_duration_ms_uses_action_t0(self, src: str) -> None:
        m = re.search(
            r"duration_ms=int\(\(time\.time\(\) - _action_t0\) \* 1000\)",
            src,
        )
        assert m

    def test_action_phase_carries_action_name_extra(self, src: str) -> None:
        # extras["action_name"] is what the frontend tooltip displays
        assert '"action_name": action or ""' in src

    def test_action_phase_carries_notice_severity(self, src: str) -> None:
        """U: action site must thread browser._last_notice_severity into the
        phase payload so the Timeline chip color reflects the agent-visible
        notice severity (not just whether the action raised)."""
        # Match within the action emit block — anchor on the action_t0
        # duration_ms expression that's unique to this site.
        m = re.search(
            r"duration_ms=int\(\(time\.time\(\) - _action_t0\) \* 1000\),"
            r"\s*notice_severity=getattr\(browser, [\"']_last_notice_severity[\"'], None\)",
            src,
            flags=re.S,
        )
        assert m, (
            "action emit must pass "
            "notice_severity=getattr(browser, '_last_notice_severity', None) "
            "right after duration_ms (U migration)"
        )


# ── I3: finalize phase emit ────────────────────────────────────────────


class TestFinalizePhaseEmit:
    def test_emit_calls_broadcast_phase_finalize(self, src: str) -> None:
        m = re.search(
            r'_bp_finalize\(\s*"finalize",\s*severity="info" if success else "error"',
            src, flags=re.S,
        )
        assert m, "expected finalize phase emit with success-based severity"

    def test_finalize_extras_carry_answer_metadata(self, src: str) -> None:
        # The frontend uses these to colour-code finished runs
        assert '"answer_type": eff_type or ""' in src
        assert '"answer_domain": eff_domain or ""' in src
        assert '"has_answer_text": bool(eff_text)' in src

    def test_finalize_is_after_broadcast_done(self, src: str) -> None:
        # The phase MUST fire AFTER the done broadcast so any frontend listener
        # using "phase=finalize" to gate state knows the done was already sent.
        done_pos = src.find('broadcast_done(\n            success,')
        finalize_pos = src.find('"finalize"')
        assert 0 < done_pos < finalize_pos, (
            "finalize phase must be emitted after broadcast_done, not before"
        )


# ── I4: guard phase emit (session drop) ────────────────────────────────


class TestSessionDropGuardPhase:
    def test_emit_calls_broadcast_phase_guard(self, src: str) -> None:
        m = re.search(
            r'_bp_guard\(\s*"guard",\s*severity="warn"',
            src, flags=re.S,
        )
        assert m, "expected guard phase emit with severity=warn for session_drop"

    def test_guard_extras_tag_subtype(self, src: str) -> None:
        # extras["guard"] distinguishes session_drop from future loop/tab guards
        assert '"guard": "session_drop"' in src
        assert '"orig_action": _orig_sd_action' in src
        assert '"return_url": _sd_signal.return_url' in src

    def test_guard_phase_carries_notice_severity(self, src: str) -> None:
        """U: guard site must also surface the agent's current notice
        severity. session_drop is itself a warn-level event, but if the
        notice channel already escalated to 'error' the Timeline should
        keep showing red, not downgrade to amber."""
        m = re.search(
            r'_bp_guard\(\s*"guard".*?'
            r'notice_severity=getattr\(browser, [\"\']_last_notice_severity[\"\'], None\)',
            src,
            flags=re.S,
        )
        assert m, (
            "guard emit must pass "
            "notice_severity=getattr(browser, '_last_notice_severity', None) "
            "(U migration)"
        )


# ── Safety: all emits wrapped in try/except ────────────────────────────


class TestEmitSafetyWrapping:
    @pytest.mark.parametrize(
        "phase_pattern, label",
        [
            (r'broadcast_phase\(\s*"action",\s*severity=', "action (I1)"),
            (r'_bp_finalize\(\s*"finalize"', "finalize (I3)"),
            (r'_bp_guard\(\s*"guard"', "guard (I4)"),
        ],
    )
    def test_each_emit_inside_try_except(
        self, src: str, phase_pattern: str, label: str,
    ) -> None:
        m = re.search(phase_pattern, src, flags=re.S)
        assert m is not None, f"emit site not found for {label}"
        idx = m.start()
        # The emit must have a `try:` immediately above (within 6 lines)
        # and an `except Exception:` shortly after (within 30 lines).
        head = src.rfind("try:", 0, idx)
        tail = src.find("except Exception:", idx)
        assert head != -1 and head < idx, f"{label}: no try: above"
        assert tail != -1 and idx < tail, f"{label}: no except below"
        before = src[head:idx]
        after = src[idx:tail]
        assert before.count("\n") < 6, (
            f"{label}: try: is {before.count(chr(10))} lines above emit — "
            "likely a different (outer) try block, not the emit's wrapper"
        )
        assert after.count("\n") < 30, (
            f"{label}: except is {after.count(chr(10))} lines after emit — "
            "wrapper boundary check failed"
        )

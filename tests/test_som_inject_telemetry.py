"""H1: pin SoM-inject performance telemetry.

Background
----------
On heavy pages (>150 interactive elements, or pages with slow rendering
frameworks), ``frame.evaluate(self._som_js)`` can take 2-5 seconds. Without
visibility into this cost, the agent appears "stuck" between actions and
users can't tell whether it's the VLM or the SoM injection that's slow.

Contract
--------
The mark_and_screenshot path must:

1. **Time** the SoM injection loop end-to-end (cumulative across frames).
2. **Log INFO with duration** on normal pages.
3. **Log WARNING** when ``total_elements > 150`` OR ``duration_ms > 2000``.
4. **Emit a ``broadcast_phase("som_inject", ...)``** event with:
   - ``severity`` upgraded to "warn" when heavy;
   - ``duration_ms`` field;
   - ``extra`` containing ``element_count``, ``frame_count``, ``heavy``.

These three signals together feed the G2 frontend timeline and let us
spot performance regressions before they hit user-visible runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


BE_PATH = Path(__file__).resolve().parent.parent / "visual_web_agent" / "browser_env.py"


@pytest.fixture(scope="module")
def src() -> str:
    return BE_PATH.read_text(encoding="utf-8")


class TestTimingScaffold:
    def test_records_t0_before_loop(self, src: str) -> None:
        assert "_som_t0 = time.time()" in src

    def test_records_duration_after_loop(self, src: str) -> None:
        # The duration_ms calc must use the same t0 sentinel.
        assert "_som_dur_ms = int((time.time() - _som_t0) * 1000)" in src

    def test_heavy_threshold_correct(self, src: str) -> None:
        """Heavy = >150 elements OR >2000ms. Either condition must trip."""
        assert "_som_heavy = total_elements > 150 or _som_dur_ms > 2000" in src


class TestLoggingTier:
    def test_heavy_path_warns(self, src: str) -> None:
        # logger.warning text mentions "SoM HEAVY" so ops can grep
        assert "SoM HEAVY" in src
        # The warning path counts both element + duration
        warn_block = src[src.find("SoM HEAVY"): src.find("SoM HEAVY") + 400]
        assert "total_elements" in warn_block
        assert "_som_dur_ms" in warn_block

    def test_normal_path_still_logs_duration(self, src: str) -> None:
        # Existing INFO line was preserved (regression check) AND now
        # includes the duration suffix
        m = re.search(r"marked \{total_elements\} interactive elements in \{_som_dur_ms\}ms", src)
        assert m, "normal-path INFO log must include duration_ms"


class TestPhaseEvent:
    def test_calls_broadcast_phase_with_som_inject_name(self, src: str) -> None:
        # Use the G2 phase channel
        assert 'broadcast_phase(' in src
        assert '"som_inject"' in src

    def test_severity_upgrades_to_warn_when_heavy(self, src: str) -> None:
        m = re.search(
            r"broadcast_phase\(\s*\"som_inject\",\s*severity=\"warn\" if _som_heavy else \"info\"",
            src,
            flags=re.S,
        )
        assert m, "phase severity must be 'warn' when _som_heavy else 'info'"

    def test_extras_carry_element_and_frame_counts(self, src: str) -> None:
        # The extras dict must surface the structured stats the frontend
        # needs to render a meaningful tooltip
        m = re.search(
            r'extra=\{\s*"element_count":\s*int\(total_elements\)',
            src,
        )
        assert m
        assert '"frame_count": int(injected_frames)' in src
        assert '"heavy": bool(_som_heavy)' in src

    def test_best_effort_no_raise(self, src: str) -> None:
        """The phase emit must be wrapped in try/except so CLI runs (where
        api_server isn't imported) don't crash on the broadcast."""
        # Find the broadcast_phase call site and confirm it's inside a try
        idx = src.find('broadcast_phase(\n                "som_inject"')
        # The block above the call must be a `try:` and the block below
        # must include `except Exception:`
        head = src.rfind("try:", 0, idx)
        tail = src.find("except Exception:", idx)
        assert head != -1 and tail != -1 and head < idx < tail


class TestStepNumberPropagated:
    def test_step_arg_passed_to_phase(self, src: str) -> None:
        # The frontend timeline keys events by step number — must be present
        m = re.search(
            r'broadcast_phase\(\s*"som_inject",\s*severity=.*?step=step',
            src,
            flags=re.S,
        )
        assert m, "broadcast_phase must include step=step so timeline keys correctly"


class TestNoticeSeverityPropagated:
    """U: som_inject runs inside BrowserEnv methods so ``self`` is the
    BrowserEnv. The emit must surface ``self._last_notice_severity`` so a
    pre-existing warn/error notice from the agent loop keeps the chip
    coloured even when the SoM injection itself is technically info."""

    def test_notice_severity_arg_present(self, src: str) -> None:
        m = re.search(
            r'broadcast_phase\(\s*"som_inject",.*?'
            r'notice_severity=self\._last_notice_severity',
            src,
            flags=re.S,
        )
        assert m, (
            "som_inject emit must pass notice_severity=self._last_notice_severity "
            "(U migration); without it a heavy-page warn would still show "
            "info-blue when a prior action's notice was warn/error."
        )

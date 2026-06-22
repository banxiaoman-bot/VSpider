"""Startup phase — extracted from ``main.py`` (slice G1).

Houses ``RunContext`` (the dataclass that flows through every phase),
``prepare_run_identity`` (run-id / output filenames), and
``init_loop_guards`` (Judge / LoopDetector / FailureStats / counters).

Behavior is a straight lift-and-delegate from run_agent; no logic changes.
"""

from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger("visual_web_agent.phases.startup")


# ---------------------------------------------------------------------------
# RunContext — the single data object that threads through every phase
# ---------------------------------------------------------------------------

@dataclass
class RunContext:
    """Core state produced during startup and consumed by every later phase."""

    run_ts: str
    vlm_output: str
    xhr_output: str

    goal_output_mode: str = "default"
    goal_output_contract: dict = field(default_factory=dict)
    initial_output_contract: dict = field(default_factory=dict)
    prompt_images: list[str] = field(default_factory=list)
    prompt_image_policy: str = "adaptive"
    registry_record_owned: bool = False


# ---------------------------------------------------------------------------
# LoopGuards — guard objects created once per run, consumed in the step loop
# ---------------------------------------------------------------------------

@dataclass
class LoopGuards:
    """All guard / detector objects needed before the main step loop."""

    judge: Any
    loop_detector: Any
    failure_stats: Any
    judge_rejections: int = 0
    consecutive_errors: int = 0
    max_consecutive_errors: int = 3


# ---------------------------------------------------------------------------
# prepare_run_identity — run_ts + output filenames
# ---------------------------------------------------------------------------

def prepare_run_identity(
    *,
    run_id: str = "",
    prompt_images: list[str] | None = None,
    prompt_image_policy: str = "adaptive",
) -> RunContext:
    """Process caller-supplied run_id into a sanitised run_ts, derive output
    filenames, and return a seed ``RunContext``.

    Lifted verbatim from ``run_agent`` lines 7677-7698.
    """
    caller_run_id = re.sub(r"[^0-9A-Za-z_-]+", "_", str(run_id or "").strip()).strip("_")
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if caller_run_id:
        run_ts = caller_run_id

    vlm_output = f"output_{run_ts}.xlsx"
    xhr_output = f"xhr_{run_ts}.xlsx"

    return RunContext(
        run_ts=run_ts,
        vlm_output=vlm_output,
        xhr_output=xhr_output,
        prompt_images=list(prompt_images or []),
        prompt_image_policy=prompt_image_policy,
    )


# ---------------------------------------------------------------------------
# init_loop_guards — Judge / LoopDetector / FailureStats / counters
# ---------------------------------------------------------------------------

def init_loop_guards(vlm: Any) -> LoopGuards:
    """Create the guard objects that live for the duration of a run.

    Lifted verbatim from ``run_agent`` lines 11486-11506.
    """
    try:
        from ..judge import TaskJudge, JudgeConfig
        from ..loop_detector import ActionLoopDetector, LoopDetectorConfig
        from ..failure_classifier import FailureStats
        from ..config import JUDGE_ENABLED
    except ImportError:
        from judge import TaskJudge, JudgeConfig  # type: ignore[no-redef]
        from loop_detector import ActionLoopDetector, LoopDetectorConfig  # type: ignore[no-redef]
        from failure_classifier import FailureStats  # type: ignore[no-redef]
        from config import JUDGE_ENABLED  # type: ignore[no-redef]

    judge = TaskJudge(vlm_client=vlm, config=JudgeConfig(
        enabled=JUDGE_ENABLED,
        max_retries_after_fail=2,
    ))

    loop_detector = ActionLoopDetector(config=LoopDetectorConfig(
        window_size=8,
        action_repeat_threshold=3,
        stagnation_threshold=4,
    ))

    failure_stats = FailureStats()

    try:
        vlm.reset_element_tracker()
    except Exception as _trk_err:
        logger.debug("[TRACKER] reset failed: %s", _trk_err)

    return LoopGuards(
        judge=judge,
        loop_detector=loop_detector,
        failure_stats=failure_stats,
    )

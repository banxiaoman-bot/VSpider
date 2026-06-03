"""Consume a resume decision in the agent loop (RUN-RESUME1 step 3).

Step 1/2 shipped the *write* side: a per-turn ``run_checkpoint.json`` plus a
pure :func:`decide_resume` policy and ``manifest.resumed_from`` provenance.
What was deliberately deferred (the "non-deterministic VLM replay" caveat) is
the *consume* side — actually using ``decision.completed_steps`` / ``from_turn``
so a resumed run does not redo finished work.

Because the VLM loop is non-deterministic you cannot replay turns
byte-for-byte. The honest, human-like way to "skip already-done steps" is:

1. **Surface** the resume state into ``workflow_memory`` + a natural-language
   directive so the VLM/planner *knows* what was already accomplished and
   continues toward the remaining goal (re-establishing preconditions such as
   navigation/login when needed) instead of restarting.
2. **Best-effort skip** the leading planner sub-goals whose descriptions match
   the recorded completed steps, advancing the plan past them. This is
   conservative (exact normalized match, never skips the final sub-goal) and
   self-healing — the agent re-observes each turn, so a wrong skip degrades to a
   re-navigation rather than data loss.

The recorded ``completed_steps`` ledger itself is derived from the planner's
``sub_goals`` via :func:`plan_completed_step_labels` (the record side).

Pure module: stdlib only, no agent/VLM/IO. Duck-typed over the
``vlm_client.SubGoal`` / ``TaskPlan`` and ``run_checkpoint.ResumeDecision``
shapes so it stays trivially unit-testable and decoupled.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

__all__ = [
    "plan_completed_step_labels",
    "resume_memory_payload",
    "build_resume_note",
    "apply_completed_steps_to_plan",
]


def _norm(value: Any) -> str:
    """Lowercase + whitespace-collapse a label for tolerant matching."""

    return " ".join(str(value or "").split()).strip().lower()


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _norm2(value: Any) -> str:
    """Aggressive normalize for fuzzy matching: NFKC (full-width -> half-width),
    lowercase, punctuation stripped, whitespace collapsed. Keeps word chars
    (incl. CJK) so a re-worded plan still matches its recorded step labels.
    """

    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = _PUNCT_RE.sub(" ", text)
    return " ".join(text.split()).strip()


def _fuzzy_match(a: Any, b: Any) -> bool:
    """Tolerant label match (RUN-RESUME1 step 5).

    Conservative by design — exact normalized equality first (the step-3
    behaviour), then full-width / case / punctuation / spacing-insensitive
    equality, then a whitespace-squashed leading-prefix match so a slightly
    re-worded or extended sub-goal ("搜索 python" vs "搜索 Python 并点开") still
    resumes. Reordered or merely token-overlapping phrases never match, so a
    wrong skip stays unlikely (and the agent re-observes each turn anyway).
    """

    if _norm(a) and _norm(a) == _norm(b):
        return True
    na, nb = _norm2(a), _norm2(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    sa, sb = na.replace(" ", ""), nb.replace(" ", "")
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return len(shorter) >= 4 and longer.startswith(shorter)


def _get(decision: Any, key: str, default: Any) -> Any:
    """Read ``key`` from a ResumeDecision object *or* a plain dict."""

    if isinstance(decision, dict):
        return decision.get(key, default)
    return getattr(decision, key, default)


def plan_completed_step_labels(task_plan: Any) -> list[str]:
    """Return the descriptions of already-``done`` sub-goals, in order.

    This is the *record* side: the agent loop passes the result to
    ``RunCheckpointer.record(completed_steps=...)`` each turn so the checkpoint
    accumulates a meaningful, plan-anchored ledger. Tolerant of ``None`` and of
    plans without sub-goals.
    """

    if task_plan is None:
        return []
    sub_goals = list(getattr(task_plan, "sub_goals", None) or [])
    labels: list[str] = []
    for sg in sub_goals:
        if str(getattr(sg, "status", "") or "").strip().lower() != "done":
            continue
        desc = str(getattr(sg, "description", "") or "").strip()
        if desc:
            labels.append(desc)
    return labels


def resume_memory_payload(decision: Any) -> dict[str, Any]:
    """Build the ``workflow_memory["__resume_state"]`` payload from a decision.

    Surfaces the resume state to the prompt's memory section and the planner.
    A non-resuming decision yields an inert payload (``resumed=False``,
    empty note) so a fresh run stays unaffected.
    """

    resumed = bool(_get(decision, "should_resume", False))
    completed = [str(s) for s in (_get(decision, "completed_steps", None) or [])]
    resumed_from = _get(decision, "resumed_from", None)
    prior_status = ""
    if isinstance(resumed_from, dict):
        prior_status = str(resumed_from.get("status") or "")
    return {
        "resumed": resumed,
        "from_turn": int(_get(decision, "from_turn", 0) or 0),
        "completed_steps": completed,
        "item_count": int(_get(decision, "item_count", 0) or 0),
        "prior_status": prior_status,
        "note": build_resume_note(decision),
    }


def build_resume_note(decision: Any, *, goal: str = "") -> str:
    """Natural-language directive injected so the VLM continues, not restarts.

    Returns ``""`` for a non-resuming decision (nothing to say).
    """

    if not bool(_get(decision, "should_resume", False)):
        return ""

    from_turn = int(_get(decision, "from_turn", 0) or 0)
    item_count = int(_get(decision, "item_count", 0) or 0)
    completed = [str(s) for s in (_get(decision, "completed_steps", None) or [])]

    parts = [
        "【断点续跑】本次为续跑：上次运行已进行到第 "
        f"{from_turn} 轮、已抓取 {item_count} 条数据（已去重，请勿重复输出已抓内容）。"
    ]
    if completed:
        shown = "、".join(completed[:6])
        parts.append(f"已完成步骤：{shown}。")
    parts.append(
        "请从尚未完成的剩余目标继续推进；必要时可重新导航/登录以恢复页面前置条件，"
        "但不要重复执行已完成的提取或操作。"
    )
    return " ".join(parts)


def apply_completed_steps_to_plan(task_plan: Any, completed_steps: list[str] | None) -> int:
    """Best-effort skip leading completed sub-goals on a resumed plan.

    Advances ``current_idx`` over the longest *leading* run of sub-goals whose
    normalized description matches a recorded completed step, marking them
    ``done`` and the next one ``active``. Conservative by design:

    - exact normalized match only (no fuzzy matching);
    - never skips the final sub-goal (there must always be work left to do);
    - leaves the plan untouched when the first sub-goal does not match.

    Returns the number of sub-goals skipped. Tolerant of ``None`` inputs.
    """

    if task_plan is None or not completed_steps:
        return 0
    sub_goals = list(getattr(task_plan, "sub_goals", None) or [])
    if len(sub_goals) <= 1:
        return 0

    done = [s for s in completed_steps if _norm(s)]
    if not done:
        return 0

    cap = len(sub_goals) - 1  # always keep at least the last sub-goal to execute
    skip = 0
    while skip < cap and any(
        _fuzzy_match(getattr(sub_goals[skip], "description", ""), label) for label in done
    ):
        skip += 1

    if skip == 0:
        return 0

    for idx in range(skip):
        try:
            sub_goals[idx].status = "done"
        except Exception:  # pragma: no cover - defensive on exotic stubs
            pass
    try:
        sub_goals[skip].status = "active"
        task_plan.current_idx = skip
    except Exception:  # pragma: no cover - defensive
        pass
    return skip

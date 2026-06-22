"""Run-level checkpoint persistence + resume decision (RUN-RESUME1 step 1).

The deterministic foundation for "agent mid-task checkpoint" (断点重跑 gap #3).
A *run checkpoint* is a small JSON document under
``runs/<run_id>/run_checkpoint.json`` recording how far an agent run got
(turn / phase / completed steps / item count). Writes are atomic
(tmp file + ``os.replace``) so a crash mid-write never corrupts an existing
checkpoint; reads are tolerant (missing / corrupt -> ``None``) so a fresh or
damaged checkpoint degrades to a normal run. This mirrors
``crawl_checkpoint`` for the deep-crawl layer.

Scope (deliberate): this module is pure I/O plus a pure :func:`decide_resume`
policy. It does **not** drive the VLM agent loop. Writing the checkpoint
turn-by-turn inside ``main.py`` and actually re-executing from it is
RUN-RESUME1 step 2 (out of scope here). Default off -> a run that never opts
into resume is byte-identical to today (no ``run_checkpoint.json`` is written
unless a caller explicitly saves one, and ``manifest.resumed_from`` is omitted
unless a resume actually happened).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io_contract.persistence import run_dir

__all__ = [
    "RUN_CHECKPOINT_FILENAME",
    "CHECKPOINT_VERSION",
    "build_checkpoint_state",
    "run_checkpoint_path",
    "save_run_checkpoint",
    "load_run_checkpoint",
    "clear_run_checkpoint",
    "ResumeDecision",
    "decide_resume",
    "mark_manifest_resumed",
    "RunCheckpointer",
]


RUN_CHECKPOINT_FILENAME = "run_checkpoint.json"
CHECKPOINT_VERSION = "run_checkpoint.v1"

# A checkpoint in one of these states has nothing left to resume.
_TERMINAL_STATUSES = ("done", "completed", "success")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_checkpoint_state(
    run_id: str,
    *,
    goal: str = "",
    start_url: str = "",
    turn: int = 0,
    phase: str = "",
    status: str = "in_progress",
    completed_steps: list[str] | None = None,
    item_count: int = 0,
    last_action: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``run_checkpoint.v1`` state dict (pure; no IO)."""

    return {
        "version": CHECKPOINT_VERSION,
        "run_id": str(run_id or ""),
        "goal": str(goal or ""),
        "start_url": str(start_url or ""),
        "turn": int(turn or 0),
        "phase": str(phase or ""),
        "status": str(status or "in_progress"),
        "completed_steps": [str(s) for s in (completed_steps or [])],
        "item_count": int(item_count or 0),
        "last_action": str(last_action or ""),
        "updated_at": _now_iso(),
        "extra": dict(extra or {}),
    }


def run_checkpoint_path(run_id: str, *, base_dir: str | Path | None = None) -> Path:
    """Return ``runs/<run_id>/run_checkpoint.json`` (creating the run dir)."""

    return run_dir(run_id, base_dir=base_dir) / RUN_CHECKPOINT_FILENAME


def save_run_checkpoint(
    run_id: str,
    state: dict[str, Any],
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """Atomically persist ``state`` to ``runs/<run_id>/run_checkpoint.json``."""

    target = run_checkpoint_path(run_id, base_dir=base_dir)
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".run_cp_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def load_run_checkpoint(
    run_id: str,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return the checkpoint dict for ``run_id``, or ``None`` if missing/corrupt."""

    target = run_checkpoint_path(run_id, base_dir=base_dir)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_run_checkpoint(
    run_id: str,
    *,
    base_dir: str | Path | None = None,
) -> bool:
    """Remove the checkpoint (call on success). Returns ``True`` if a file was removed."""

    target = run_checkpoint_path(run_id, base_dir=base_dir)
    try:
        target.unlink()
        return True
    except OSError:
        return False


@dataclass
class ResumeDecision:
    """Outcome of :func:`decide_resume` -- whether/where a run should continue."""

    should_resume: bool
    from_turn: int = 0
    completed_steps: list[str] = field(default_factory=list)
    item_count: int = 0
    resumed_from: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_resume": self.should_resume,
            "from_turn": self.from_turn,
            "completed_steps": list(self.completed_steps),
            "item_count": self.item_count,
            "resumed_from": dict(self.resumed_from),
            "reason": self.reason,
        }


def decide_resume(
    prior: dict[str, Any] | None,
    *,
    resume: bool,
    goal: str = "",
    start_url: str = "",
) -> ResumeDecision:
    """Pure policy: should this run resume from ``prior``?

    - ``resume`` off -> never (the default path stays untouched).
    - no / empty / corrupt checkpoint -> fresh.
    - prior already terminal (done / completed / success) -> fresh.
    - prior goal/url present and differing from the current run -> fresh
      (never resume a *different* task into this run).
    - otherwise -> resume from the recorded turn / completed_steps, exposing a
      ``resumed_from`` provenance payload for the manifest.
    """

    if not resume:
        return ResumeDecision(False, reason="resume_disabled")
    if not isinstance(prior, dict) or not prior:
        return ResumeDecision(False, reason="no_checkpoint")

    status = str(prior.get("status") or "").lower()
    if status in _TERMINAL_STATUSES:
        return ResumeDecision(False, reason="prior_completed")

    prior_goal = str(prior.get("goal") or "")
    if goal and prior_goal and goal != prior_goal:
        return ResumeDecision(False, reason="goal_mismatch")

    prior_url = str(prior.get("start_url") or "")
    if start_url and prior_url and start_url != prior_url:
        return ResumeDecision(False, reason="url_mismatch")

    from_turn = int(prior.get("turn") or 0)
    completed = [str(s) for s in (prior.get("completed_steps") or [])]
    item_count = int(prior.get("item_count") or 0)
    resumed_from = {
        "turn": from_turn,
        "phase": str(prior.get("phase") or ""),
        "completed_steps": len(completed),
        "item_count": item_count,
        "status": status or "in_progress",
        "updated_at": str(prior.get("updated_at") or ""),
    }
    return ResumeDecision(
        True,
        from_turn=from_turn,
        completed_steps=completed,
        item_count=item_count,
        resumed_from=resumed_from,
        reason="resumed",
    )


def mark_manifest_resumed(
    run_id: str,
    resumed_from: dict[str, Any],
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """Persist ``resumed_from`` provenance onto ``runs/<run_id>/manifest.json``.

    Read-modify-write via the io_contract persistence helpers so the manifest's
    atomic-write semantics are preserved. No-op-safe: an empty payload leaves the
    manifest byte-identical (``set_resumed_from`` clears the marker, which
    ``Manifest.to_dict`` then omits).
    """

    from .io_contract.manifest import set_resumed_from
    from .io_contract.persistence import read_manifest, write_manifest

    manifest = read_manifest(run_id, base_dir=base_dir)
    set_resumed_from(manifest, resumed_from)
    return write_manifest(run_id, manifest, base_dir=base_dir)


class RunCheckpointer:
    """Stateful lifecycle glue around the run-checkpoint primitives (step 2).

    Wraps begin / per-turn record / finish so the agent loop only needs a few
    guarded one-liners. Opt-in: when ``resume`` is falsy the checkpointer is
    *inert* (no file written, manifest untouched), preserving the default-off /
    byte-identical contract. Every method swallows its own I/O errors so a
    checkpoint failure can never abort the agent run.

    Note: step 2 wires the *write* side (durable per-turn checkpoint) and resume
    *provenance* (``manifest.resumed_from`` on a detected resume). Actually
    consuming ``decision.completed_steps`` to skip already-done work in the VLM
    loop is deferred to step 3 (non-deterministic replay).
    """

    def __init__(
        self,
        run_id: str,
        *,
        enabled: bool,
        goal: str = "",
        start_url: str = "",
        base_dir: str | Path | None = None,
        decision: ResumeDecision | None = None,
    ) -> None:
        self.run_id = str(run_id or "")
        self.enabled = bool(enabled)
        self.goal = str(goal or "")
        self.start_url = str(start_url or "")
        self.base_dir = base_dir
        self.decision = decision if decision is not None else ResumeDecision(False, reason="resume_disabled")
        self._completed: list[str] = list(self.decision.completed_steps or [])
        self._last_turn: int = int(self.decision.from_turn or 0)
        self._item_count: int = int(self.decision.item_count or 0)

    @classmethod
    def begin(
        cls,
        run_id: str,
        *,
        resume: bool,
        goal: str = "",
        start_url: str = "",
        base_dir: str | Path | None = None,
    ) -> "RunCheckpointer":
        """Load any prior checkpoint, decide resume, and mark manifest provenance.

        Returns an inert checkpointer when ``resume`` is falsy.
        """

        if not resume:
            return cls(
                run_id, enabled=False, goal=goal, start_url=start_url, base_dir=base_dir,
                decision=ResumeDecision(False, reason="resume_disabled"),
            )

        try:
            prior = load_run_checkpoint(run_id, base_dir=base_dir)
        except Exception:
            prior = None
        decision = decide_resume(prior, resume=True, goal=goal, start_url=start_url)
        inst = cls(
            run_id, enabled=True, goal=goal, start_url=start_url, base_dir=base_dir,
            decision=decision,
        )
        if decision.should_resume:
            try:
                mark_manifest_resumed(run_id, decision.resumed_from, base_dir=base_dir)
            except Exception:
                pass
        return inst

    def record(
        self,
        turn: int,
        *,
        phase: str = "",
        item_count: int | None = None,
        last_action: str = "",
        completed_steps: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Path | None:
        """Persist an ``in_progress`` checkpoint for ``turn``; no-op when disabled."""

        if not self.enabled:
            return None
        try:
            self._last_turn = int(turn or 0)
            if completed_steps is not None:
                self._completed = [str(s) for s in completed_steps]
            if item_count is not None:
                self._item_count = int(item_count or 0)
            state = build_checkpoint_state(
                self.run_id,
                goal=self.goal,
                start_url=self.start_url,
                turn=self._last_turn,
                phase=phase,
                status="in_progress",
                completed_steps=self._completed,
                item_count=self._item_count,
                last_action=last_action,
                extra=extra,
            )
            return save_run_checkpoint(self.run_id, state, base_dir=self.base_dir)
        except Exception:
            return None

    def finish(self, success: bool) -> Path | None:
        """Clear the checkpoint on success; persist a ``failed`` one otherwise."""

        if not self.enabled:
            return None
        try:
            if success:
                clear_run_checkpoint(self.run_id, base_dir=self.base_dir)
                return None
            state = build_checkpoint_state(
                self.run_id,
                goal=self.goal,
                start_url=self.start_url,
                turn=self._last_turn,
                status="failed",
                completed_steps=self._completed,
                item_count=self._item_count,
            )
            return save_run_checkpoint(self.run_id, state, base_dir=self.base_dir)
        except Exception:
            return None

    @property
    def should_resume(self) -> bool:
        return bool(self.decision.should_resume)

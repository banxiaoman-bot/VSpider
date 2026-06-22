"""``resume_run`` capability handler (RUN-RESUME1 step 3).

Shipped as its own module (not appended to the oversized ``actions.py``) per the
workflow rule §三. A deterministic, read-only control action that lets the agent
*consult where a prior/interrupted run left off* and surface it into
``workflow_memory`` so it continues instead of restarting from scratch.

Resolution order:

1. ``workflow_memory["__resume_state"]`` — the resume the loop already decided
   for *this* run (written by :mod:`run_resume_consume`). Preferred.
2. ``runs/<active_run_id>/run_checkpoint.json`` — load the active run's
   checkpoint directly when the loop hasn't published a resume state.
3. Nothing resumable -> a clean "fresh task" status.

The result is written to ``workflow_memory[memory_key or "resume_status"]`` and
mirrored onto the RPA trail as evidence. No page mutation (works without a live
page).
"""

from __future__ import annotations

from typing import Any, Optional

try:
    from .actions import ActionContext, ActionHandler, ActionRegistry
    from .run_resume_consume import build_resume_note
except ImportError:  # pragma: no cover - flat-layout fallback, mirrors actions.py
    from actions import (  # type: ignore[no-redef]
        ActionContext,
        ActionHandler,
        ActionRegistry,
    )
    from run_resume_consume import build_resume_note  # type: ignore[no-redef]


_TERMINAL_STATUSES = ("done", "completed", "success")
_FRESH_NOTE = "未发现可续跑的进度记录，将按全新任务从头执行。"


@ActionRegistry.register("resume_run")
class ResumeRunHandler(ActionHandler):
    """Read back where a prior/interrupted run left off into memory."""

    async def execute(self, ctx: ActionContext) -> Optional[Any]:
        mem_key = (ctx.action.memory_key or "").strip() or "resume_status"
        payload = self._resolve(ctx)

        try:
            ctx.workflow_memory[mem_key] = payload
        except Exception:  # pragma: no cover - memory is a plain dict in practice
            pass

        try:
            ctx.browser.rpa_trail.append(
                ctx.with_rpa_meta(
                    {
                        "action": "resume_run",
                        "resumed": bool(payload.get("resumed")),
                        "from_turn": int(payload.get("from_turn") or 0),
                        "completed_steps": len(payload.get("completed_steps") or []),
                        "item_count": int(payload.get("item_count") or 0),
                        "prior_status": str(payload.get("prior_status") or ""),
                        "source": str(payload.get("source") or ""),
                    }
                )
            )
        except Exception:  # pragma: no cover - trail is best-effort evidence
            pass

        return None

    @staticmethod
    def _resolve(ctx: ActionContext) -> dict[str, Any]:
        mem = ctx.workflow_memory if isinstance(ctx.workflow_memory, dict) else {}

        # 1. resume state already decided for THIS run by the loop
        state = mem.get("__resume_state")
        if isinstance(state, dict) and state:
            resumed = bool(state.get("resumed"))
            note = str(state.get("note") or "")
            if not note:
                note = build_resume_note(state) if resumed else _FRESH_NOTE
            return {
                "resumed": resumed,
                "from_turn": int(state.get("from_turn") or 0),
                "completed_steps": [str(s) for s in (state.get("completed_steps") or [])],
                "item_count": int(state.get("item_count") or 0),
                "prior_status": str(state.get("prior_status") or ""),
                "note": note,
                "source": "workflow_memory",
            }

        # 2. load the active run's checkpoint directly
        checkpoint = _load_active_checkpoint()
        if isinstance(checkpoint, dict) and checkpoint:
            status = str(checkpoint.get("status") or "").strip().lower()
            resumable = status not in _TERMINAL_STATUSES
            completed = [str(s) for s in (checkpoint.get("completed_steps") or [])]
            decision_like = {
                "should_resume": resumable,
                "from_turn": int(checkpoint.get("turn") or 0),
                "completed_steps": completed,
                "item_count": int(checkpoint.get("item_count") or 0),
            }
            return {
                "resumed": resumable,
                "from_turn": int(checkpoint.get("turn") or 0),
                "completed_steps": completed,
                "item_count": int(checkpoint.get("item_count") or 0),
                "prior_status": status,
                "note": build_resume_note(decision_like) if resumable else _FRESH_NOTE,
                "source": "checkpoint",
            }

        # 3. nothing resumable
        return {
            "resumed": False,
            "from_turn": 0,
            "completed_steps": [],
            "item_count": 0,
            "prior_status": "",
            "note": _FRESH_NOTE,
            "source": "none",
        }


def _load_active_checkpoint() -> Optional[dict[str, Any]]:
    """Load ``run_checkpoint.json`` for the published active run (or ``None``)."""

    try:
        from .io_contract import current_base_dir, current_run_id
        from .run_checkpoint import load_run_checkpoint
    except ImportError:  # pragma: no cover - flat-layout fallback
        from io_contract import current_base_dir, current_run_id  # type: ignore[no-redef]
        from run_checkpoint import load_run_checkpoint  # type: ignore[no-redef]

    try:
        run_id = current_run_id()
    except Exception:  # pragma: no cover - defensive
        run_id = ""
    if not run_id:
        return None
    try:
        return load_run_checkpoint(run_id, base_dir=current_base_dir())
    except Exception:  # pragma: no cover - tolerant load
        return None

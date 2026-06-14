"""E5: per-run VLM call budget + metering (vlm_budget.v1).

Every real VLM/LLM call routed through ``VLMClient`` (ask / extract / judge /
plan / reflect) is metered here. The meter is **observe-only** by default: it
counts calls per kind, tracks the primary "turn" budget (``ask`` calls), and
flags when usage crosses the warn ratio or exceeds the budget. It never raises
and never blocks a call -- the agent loop reads :meth:`VlmBudget.snapshot` to
feed efficiency / planner feedback and decide whether to switch to a cheaper
path (api_replay, deterministic actions). Accuracy is never traded for the
budget; the budget only informs efficiency.

Opt-out: ``VSPIDER_VLM_BUDGET=0`` (metering becomes a no-op).
Tune via env:
  - ``VSPIDER_VLM_MAX_CALLS``  total real VLM calls before over-budget (default 120)
  - ``VSPIDER_VLM_MAX_TURNS``  ask() turns before over-budget (default 60)
  - ``VSPIDER_VLM_BUDGET_WARN_RATIO``  warn threshold 0..1 (default 0.8)
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

BUDGET_VERSION = "vlm_budget.v1"

# A VLM "turn" is one ask() call (the per-step perception/action decision);
# extract/judge/plan/reflect are auxiliary calls that still consume budget.
TURN_KIND = "ask"
KNOWN_KINDS = ("ask", "extract", "judge", "plan", "reflect", "other")

_DEFAULT_MAX_CALLS = 120
_DEFAULT_MAX_TURNS = 60
_DEFAULT_WARN_RATIO = 0.8


def _env_int(name: str, default: int) -> int:
    try:
        raw = str(os.environ.get(name, "")).strip()
        return int(raw) if raw else int(default)
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = str(os.environ.get(name, "")).strip()
        return float(raw) if raw else float(default)
    except Exception:
        return float(default)


def budget_enabled() -> bool:
    return str(os.environ.get("VSPIDER_VLM_BUDGET", "1")).strip().lower() not in {
        "0", "false", "no", "off",
    }


def normalize_kind(kind: str) -> str:
    k = str(kind or "other").strip().lower()
    return k if k in KNOWN_KINDS else "other"


class VlmBudget:
    """Thread-safe per-run VLM call meter with a *soft* budget.

    :meth:`record` returns a status dict each call; callers log / surface it.
    The budget is soft: ``over_budget`` is a flag, never an exception, so a
    runaway loop is reported (and can be cut short by the agent loop) without
    a hard failure that would abort a legitimately long task mid-flight.
    """

    def __init__(
        self,
        *,
        max_calls: int | None = None,
        max_turns: int | None = None,
        warn_ratio: float | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.enabled = budget_enabled() if enabled is None else bool(enabled)
        self.max_calls = max(
            0,
            int(max_calls if max_calls is not None else _env_int("VSPIDER_VLM_MAX_CALLS", _DEFAULT_MAX_CALLS)),
        )
        self.max_turns = max(
            0,
            int(max_turns if max_turns is not None else _env_int("VSPIDER_VLM_MAX_TURNS", _DEFAULT_MAX_TURNS)),
        )
        ratio = warn_ratio if warn_ratio is not None else _env_float(
            "VSPIDER_VLM_BUDGET_WARN_RATIO", _DEFAULT_WARN_RATIO
        )
        self.warn_ratio = min(1.0, max(0.0, float(ratio)))
        self._lock = threading.Lock()
        self.total = 0
        self.turns = 0
        self.by_kind: dict[str, int] = {}
        self.started_at = time.time()
        self.last_step = 0
        self._warned = False
        self._warned_over = False

    def _over_locked(self) -> bool:
        over_calls = self.max_calls > 0 and self.total > self.max_calls
        over_turns = self.max_turns > 0 and self.turns > self.max_turns
        return bool(over_calls or over_turns)

    def record(self, kind: str, step: int = 0) -> dict[str, Any]:
        k = normalize_kind(kind)
        if not self.enabled:
            return {"enabled": False, "kind": k, "warn": False, "over_budget": False}
        with self._lock:
            self.total += 1
            self.by_kind[k] = self.by_kind.get(k, 0) + 1
            if k == TURN_KIND:
                self.turns += 1
            try:
                self.last_step = max(self.last_step, int(step or 0))
            except Exception:
                pass
            over = self._over_locked()
            call_ratio = (self.total / self.max_calls) if self.max_calls > 0 else 0.0
            turn_ratio = (self.turns / self.max_turns) if self.max_turns > 0 else 0.0
            warn = bool(max(call_ratio, turn_ratio) >= self.warn_ratio) if self.warn_ratio > 0 else False
            status: dict[str, Any] = {
                "enabled": True,
                "version": BUDGET_VERSION,
                "kind": k,
                "step": int(step or 0),
                "total": self.total,
                "turns": self.turns,
                "max_calls": self.max_calls,
                "max_turns": self.max_turns,
                "remaining_calls": (max(0, self.max_calls - self.total) if self.max_calls > 0 else -1),
                "remaining_turns": (max(0, self.max_turns - self.turns) if self.max_turns > 0 else -1),
                "warn": warn,
                "over_budget": over,
                "first_warn": False,
                "first_over": False,
            }
            # Edge-trigger the one-shot flags so callers log exactly once.
            if over and not self._warned_over:
                self._warned_over = True
                status["first_over"] = True
            if warn and not self._warned:
                self._warned = True
                status["first_warn"] = True
            return status

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": BUDGET_VERSION,
                "enabled": self.enabled,
                "total": self.total,
                "turns": self.turns,
                "by_kind": dict(self.by_kind),
                "max_calls": self.max_calls,
                "max_turns": self.max_turns,
                "remaining_calls": (max(0, self.max_calls - self.total) if self.max_calls > 0 else -1),
                "remaining_turns": (max(0, self.max_turns - self.turns) if self.max_turns > 0 else -1),
                "warn_ratio": self.warn_ratio,
                "over_budget": self._over_locked(),
                "elapsed_s": round(time.time() - self.started_at, 3),
                "last_step": self.last_step,
            }

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self.turns = 0
            self.by_kind = {}
            self.started_at = time.time()
            self.last_step = 0
            self._warned = False
            self._warned_over = False

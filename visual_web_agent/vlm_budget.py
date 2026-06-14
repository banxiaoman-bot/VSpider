"""vlm_budget.v1: VLM round / token budget tracker with soft & hard limits.

Provides a lightweight meter that VLMClient hooks into at every ``ask()``
call.  The main loop (or API caller) can set a per-run budget; once the
soft limit is hit the tracker emits a warning event; at the hard limit it
raises ``VlmBudgetExhausted`` so the run terminates gracefully instead of
burning unlimited tokens.

Design constraints (mission 高效):
- Zero overhead when no budget is configured (all checks are O(1) ints).
- Thread-safe via simple atomics (no locks needed for single-writer).
- Emits structured events for the event_stream so the UI can show spend.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("vspider.vlm_budget")


class VlmBudgetExhausted(Exception):
    """Raised when the hard budget limit is reached."""


@dataclass
class BudgetConfig:
    """Per-run VLM budget knobs.

    Set ``max_calls=0`` or ``max_tokens=0`` to disable that axis.
    ``soft_ratio`` (0..1) controls when the warning fires relative to the
    hard limit (default 0.8 = warn at 80%).
    """
    max_calls: int = 0
    max_tokens: int = 0
    soft_ratio: float = 0.8


@dataclass
class VlmBudget:
    """Accumulates VLM usage for one run and enforces limits."""

    config: BudgetConfig = field(default_factory=BudgetConfig)

    # counters
    total_calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_latency_ms: int = 0

    # per-step log (kept lean: step + tokens + latency)
    _log: list[dict[str, Any]] = field(default_factory=list)

    # internal
    _soft_warned_calls: bool = False
    _soft_warned_tokens: bool = False

    # -- recording ---------------------------------------------------------

    def record(
        self,
        step: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
    ) -> None:
        """Record one VLM call.  Raises ``VlmBudgetExhausted`` on hard limit."""
        self.total_calls += 1
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_tokens += prompt_tokens + completion_tokens
        self.total_latency_ms += latency_ms

        self._log.append({
            "step": step,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": latency_ms,
            "ts": time.time(),
        })

        self._check_soft()
        self._check_hard()

    # -- checks ------------------------------------------------------------

    def _check_soft(self) -> None:
        cfg = self.config
        ratio = max(0.0, min(1.0, cfg.soft_ratio))
        if cfg.max_calls > 0 and not self._soft_warned_calls:
            threshold = int(cfg.max_calls * ratio)
            if self.total_calls >= threshold:
                self._soft_warned_calls = True
                logger.warning(
                    "[VLM_BUDGET] soft limit: %d/%d calls (%.0f%%)",
                    self.total_calls, cfg.max_calls,
                    100 * self.total_calls / cfg.max_calls,
                )
                self._emit_event("vlm_budget_soft", {
                    "axis": "calls",
                    "current": self.total_calls,
                    "limit": cfg.max_calls,
                })
        if cfg.max_tokens > 0 and not self._soft_warned_tokens:
            threshold = int(cfg.max_tokens * ratio)
            if self.total_tokens >= threshold:
                self._soft_warned_tokens = True
                logger.warning(
                    "[VLM_BUDGET] soft limit: %d/%d tokens (%.0f%%)",
                    self.total_tokens, cfg.max_tokens,
                    100 * self.total_tokens / cfg.max_tokens,
                )
                self._emit_event("vlm_budget_soft", {
                    "axis": "tokens",
                    "current": self.total_tokens,
                    "limit": cfg.max_tokens,
                })

    def _check_hard(self) -> None:
        cfg = self.config
        if cfg.max_calls > 0 and self.total_calls >= cfg.max_calls:
            msg = (
                f"VLM call budget exhausted: {self.total_calls}/{cfg.max_calls} calls, "
                f"{self.total_tokens} tokens total"
            )
            logger.error("[VLM_BUDGET] HARD LIMIT: %s", msg)
            self._emit_event("vlm_budget_hard", {
                "axis": "calls",
                "current": self.total_calls,
                "limit": cfg.max_calls,
            })
            raise VlmBudgetExhausted(msg)
        if cfg.max_tokens > 0 and self.total_tokens >= cfg.max_tokens:
            msg = (
                f"VLM token budget exhausted: {self.total_tokens}/{cfg.max_tokens} tokens, "
                f"{self.total_calls} calls total"
            )
            logger.error("[VLM_BUDGET] HARD LIMIT: %s", msg)
            self._emit_event("vlm_budget_hard", {
                "axis": "tokens",
                "current": self.total_tokens,
                "limit": cfg.max_tokens,
            })
            raise VlmBudgetExhausted(msg)

    # -- event bridge ------------------------------------------------------

    @staticmethod
    def _emit_event(event_type: str, data: dict[str, Any]) -> None:
        try:
            from api_server import broadcast_phase
            broadcast_phase(event_type, severity="warn", **data)
        except Exception:
            pass

    # -- query -------------------------------------------------------------

    def remaining_calls(self) -> int | None:
        if self.config.max_calls <= 0:
            return None
        return max(0, self.config.max_calls - self.total_calls)

    def remaining_tokens(self) -> int | None:
        if self.config.max_tokens <= 0:
            return None
        return max(0, self.config.max_tokens - self.total_tokens)

    def summary(self) -> dict[str, Any]:
        return {
            "total_calls": self.total_calls,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "total_latency_ms": self.total_latency_ms,
            "avg_latency_ms": (
                self.total_latency_ms // self.total_calls
                if self.total_calls > 0 else 0
            ),
            "budget_max_calls": self.config.max_calls,
            "budget_max_tokens": self.config.max_tokens,
            "remaining_calls": self.remaining_calls(),
            "remaining_tokens": self.remaining_tokens(),
        }

    def step_log(self) -> list[dict[str, Any]]:
        return list(self._log)

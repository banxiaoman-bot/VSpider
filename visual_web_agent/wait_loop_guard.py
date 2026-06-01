"""Detect repeated no-op waits caused by guard or zero-target downgrades."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


def _norm_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return str(url)[:160]


def _wait_seconds(decision: dict[str, Any]) -> int | None:
    try:
        return int(str(decision.get("type_value") or "").strip() or "1")
    except (TypeError, ValueError):
        return None


def is_noop_wait(decision: dict[str, Any]) -> bool:
    if str(decision.get("action") or "") != "wait":
        return False
    try:
        target_id = int(decision.get("target_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    seconds = _wait_seconds(decision)
    return target_id == 0 and seconds in {1, 2, 3}


@dataclass
class WaitLoopTracker:
    threshold: int = 3
    count: int = 0
    signature: tuple[str, int] | None = None

    def observe(self, decision: dict[str, Any], current_url: str) -> str | None:
        """Return feedback when the same short wait repeats too often."""
        if not is_noop_wait(decision):
            self.count = 0
            self.signature = None
            return None

        sig = (_norm_url(current_url), _wait_seconds(decision) or 1)
        if sig == self.signature:
            self.count += 1
        else:
            self.signature = sig
            self.count = 1

        if self.count < self.threshold:
            return None

        self.count = 0
        text = " ".join(
            str(decision.get(k) or "")
            for k in ("thought", "progress_review", "current_state")
        )
        hint = ""
        if "ZERO_TARGET" in text or "target_id=0" in text:
            hint = (
                "\nThis wait was produced after a target_id=0/zero-target downgrade; "
                "recover the real @eN from the current AX Tree instead of waiting again."
            )
        return (
            "Repeated short wait loop detected on the same page. Stop waiting and "
            "re-verify current URL, searchbox value, visible results, and active tab. "
            "If the previous click/Enter already succeeded, mark the subgoal completed "
            "or move to the next required action; otherwise choose a real current @eN "
            "or a semantic action such as press_key Enter, click_text, next_page, or "
            "fetch_link_content."
            + hint
        )

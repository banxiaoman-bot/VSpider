"""Progressive navigation retry — degrade wait_until before giving up."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NavAttempt:
    strategy: str
    error: str = ""


@dataclass
class NavResult:
    success: bool
    final_url: str = ""
    final_strategy: str = ""
    degraded: bool = False
    attempt_count: int = 0
    attempts: list[NavAttempt] = field(default_factory=list)


async def navigate_with_retry(
    page: Any,
    url: str,
    *,
    base_timeout_ms: int = 30_000,
    strategies: list[str] | None = None,
) -> NavResult:
    """Try ``page.goto`` with progressively looser ``wait_until`` strategies."""
    chain = strategies or ["domcontentloaded", "load", "commit"]
    attempts: list[NavAttempt] = []
    for idx, strategy in enumerate(chain):
        timeout = base_timeout_ms + idx * 5_000
        try:
            await page.goto(url, wait_until=strategy, timeout=timeout)
            final_url = ""
            try:
                final_url = str(page.url or url)
            except Exception:
                final_url = str(url)
            return NavResult(
                success=True,
                final_url=final_url,
                final_strategy=strategy,
                degraded=idx > 0,
                attempt_count=idx + 1,
                attempts=attempts,
            )
        except Exception as exc:
            attempts.append(NavAttempt(strategy=strategy, error=str(exc)))
    return NavResult(success=False, attempt_count=len(attempts), attempts=attempts)


async def check_page_health(page: Any) -> dict[str, Any]:
    issues: list[str] = []
    try:
        if page.is_closed():
            issues.append("page_closed")
    except Exception:
        issues.append("page_unreachable")
    return {"healthy": not issues, "issues": issues}

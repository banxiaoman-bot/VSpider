"""SoM injector — extracted from ``browser_env.py`` (slice G6).

Houses ``SomInjector`` which encapsulates the Set-of-Mark (SoM) injection
loop: evaluate the SoM JS across page frames, collect element maps,
handle zero-element retries, and emit performance telemetry.

Behavior is a straight lift-and-delegate from BrowserEnv.mark_and_screenshot;
no logic changes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("visual_web_agent.som_injector")


@dataclass
class SomResult:
    """Outcome of injecting SoM across all frames."""
    elements: list[dict] = field(default_factory=list)
    total_elements: int = 0
    injected_frames: int = 0
    duration_ms: int = 0
    is_heavy: bool = False
    retried: bool = False


class SomInjector:
    """Set-of-Mark injection engine.

    Lifted from BrowserEnv.mark_and_screenshot lines 2648-2770.
    """

    def __init__(self, som_js: str) -> None:
        self._som_js = som_js

    async def inject_across_frames(
        self,
        frames: list[Any],
        *,
        start_index: int = 1,
        scope: str | dict = "full",
        step: int = 0,
        format_element: Any | None = None,
    ) -> tuple[SomResult, list[str]]:
        """Evaluate SoM JS on each frame, collect element maps.

        Returns (SomResult, input_descriptions list).
        """
        all_elements: list[dict] = []
        input_descriptions: list[str] = []
        current_id = start_index
        total_elements = 0
        injected_frames = 0

        t0 = time.time()
        for frame in frames:
            try:
                result = await frame.evaluate(
                    self._som_js, {"startIndex": current_id, "scope": scope}
                )
                if result and isinstance(result, dict):
                    injected_frames += 1
                    current_id = result.get("nextId", current_id)
                    element_map = result.get("resultMap", [])
                    total_elements += len(element_map)
                    all_elements.extend(element_map)
                    if format_element is not None:
                        for el in element_map:
                            input_descriptions.append(format_element(el))
            except Exception as err:
                logger.warning("Frame evaluation failed: %s", err)

        dur_ms = int((time.time() - t0) * 1000)
        is_heavy = total_elements > 150 or dur_ms > 2000

        if is_heavy:
            logger.warning(
                "[Step %d] SoM HEAVY: %d elements across %d frames in %dms",
                step, total_elements, injected_frames, dur_ms,
            )
        else:
            logger.info(
                "[Step %d] SoM injected across %d frames, marked %d elements in %dms",
                step, injected_frames, total_elements, dur_ms,
            )

        self._broadcast_phase(
            total_elements, injected_frames, dur_ms, is_heavy, step
        )

        som_result = SomResult(
            elements=all_elements,
            total_elements=total_elements,
            injected_frames=injected_frames,
            duration_ms=dur_ms,
            is_heavy=is_heavy,
        )
        return som_result, input_descriptions

    async def retry_injection(
        self,
        page: Any,
        *,
        step: int = 0,
        clear_overlays: Any | None = None,
        format_element: Any | None = None,
    ) -> tuple[SomResult, list[str]] | None:
        """Retry SoM injection after zero-element result.

        Returns None if the page is closed or about:blank.
        Lifted from BrowserEnv.mark_and_screenshot lines 2710-2770.
        """
        if page.is_closed():
            return None

        page_url = (page.url or "").strip()
        if not page_url or page_url.startswith("about:"):
            return None

        try:
            diag = await page.evaluate("""() => ({
                url: location.href,
                title: document.title,
                bodyChildren: document.body ? document.body.childElementCount : -1,
                bodyTextLen: document.body ? (document.body.innerText || '').length : -1,
                readyState: document.readyState,
                visibility: document.visibilityState,
            })""")
            logger.warning("[SoM RETRY] 0 elements, diagnostics: %s", diag)
        except Exception:
            logger.warning("[SoM RETRY] 0 elements, diagnostics unavailable")

        logger.info("[SoM RETRY] waiting 3s then retrying...")
        await asyncio.sleep(3)

        if clear_overlays is not None:
            await clear_overlays()

        frames = list(page.frames)
        result, descriptions = await self.inject_across_frames(
            frames,
            start_index=1,
            scope="full",
            step=step,
            format_element=format_element,
        )
        result.retried = True

        if result.total_elements > 0:
            logger.info(
                "[SoM RETRY] recovered %d elements across %d frames in %dms",
                result.total_elements, result.injected_frames, result.duration_ms,
            )
        else:
            logger.warning("[SoM RETRY] still 0 elements after retry")

        return result, descriptions

    @staticmethod
    def _broadcast_phase(
        total_elements: int,
        injected_frames: int,
        dur_ms: int,
        is_heavy: bool,
        step: int,
    ) -> None:
        """Emit SoM performance phase event. Swallows all errors."""
        try:
            from api_server import broadcast_phase
            broadcast_phase(
                "som_inject",
                severity="warn" if is_heavy else "info",
                message=f"{total_elements} elements / {injected_frames} frames",
                step=step,
                duration_ms=dur_ms,
                extra={
                    "element_count": int(total_elements),
                    "frame_count": int(injected_frames),
                    "heavy": bool(is_heavy),
                },
            )
        except Exception:
            pass

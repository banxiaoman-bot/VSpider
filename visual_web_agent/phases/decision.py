"""Decision phase — extracted from ``main.py`` (slice G3).

Houses ``make_vlm_decision()`` which wraps the VLM ask call with prompt
image policy and returns a decision result.

Behavior is a straight lift-and-delegate from run_agent; no logic changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("visual_web_agent.phases.decision")

try:
    from ..prompt_image_policy import should_include_prompt_images as _should_include
except ImportError:
    try:
        from prompt_image_policy import should_include_prompt_images as _should_include
    except ImportError:
        def _should_include(step, *, policy="adaptive", last_sent_url=None, current_url=None):
            return step == 1 and policy != "off"


@dataclass
class VlmDecisionResult:
    """Result of a VLM decision call."""
    decisions: list[dict]
    prompt_images_sent: bool


async def make_vlm_decision(
    *,
    vlm: Any,
    screenshot_b64: Any,
    goal: str,
    step: int,
    input_descriptions: str,
    workflow_memory: dict,
    task_plan: Any | None,
    max_steps: int,
    som_elements: Any | None,
    capability_route: dict | None,
    prompt_images: list[str],
    prompt_image_policy: str,
    current_url: str,
    last_prompt_image_url: str | None,
) -> VlmDecisionResult:
    """Wrap the VLM ask() call with prompt image policy.

    Lifted from run_agent lines 12672-12693.
    """
    send_imgs = bool(prompt_images) and _should_include(
        step,
        policy=prompt_image_policy,
        last_sent_url=last_prompt_image_url,
        current_url=current_url,
    )

    decisions = await vlm.ask(
        screenshot_b64,
        goal,
        step,
        input_descriptions,
        workflow_memory,
        task_plan=task_plan,
        max_steps=max_steps,
        som_elements=som_elements,
        capability_route=capability_route,
        extra_images=(prompt_images if send_imgs else None),
    )

    return VlmDecisionResult(
        decisions=decisions,
        prompt_images_sent=send_imgs,
    )

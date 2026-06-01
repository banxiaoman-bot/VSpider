"""Deterministic schedule for (re)sending prompt_context reference images.

Phase 2 of the image-attachment multimodal slice (MM-2). The agent loop calls
:func:`should_include_prompt_images` before every ``vlm.ask`` to decide whether
to attach the uploaded reference image(s) on this step. The decision is purely
deterministic (no VLM guessing, per the "准确" rule) so token cost stays
predictable:

  * ``always``   - attach on every step.
  * ``first``    - attach only during the first ``early_steps`` step(s).
  * ``off``      - never attach.
  * ``adaptive`` - (default) attach during the early-step window AND whenever
                   the page URL changed since the last attach (re-ground on
                   navigation), skipping redundant repeats on the same page.

Unknown policy strings fall back to ``adaptive``.
"""

from __future__ import annotations


def should_include_prompt_images(
    step: int,
    *,
    policy: str = "adaptive",
    last_sent_url: str | None = None,
    current_url: str | None = None,
    early_steps: int = 1,
) -> bool:
    """Return ``True`` when reference image(s) should ride along this step."""

    pol = (policy or "adaptive").strip().lower()
    if pol == "off":
        return False
    if pol == "always":
        return True

    window = max(1, int(early_steps))
    if pol == "first":
        return step <= window

    # adaptive (default + unknown fallback): early window, then on page change.
    if step <= window:
        return True
    if current_url and current_url != last_sent_url:
        return True
    return False

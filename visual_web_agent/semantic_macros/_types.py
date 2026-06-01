"""Macro framework data types.

A *semantic macro* is a goal-driven shortcut that bypasses the VLM main loop
for tasks the VLM is provably bad at (multi-step calendar selection, cascader
drilling, multi-tab search-and-open). It captures the **intent** declaratively
(``{action, anchor, ...params}``) and replays via a single ``page.evaluate``
JS body using shared primitives.

Three responsibilities, three callbacks:

  1. **parse**: ``goal text → step dict | None``
     Decide whether this macro applies to the user's goal and extract its
     params. Returning ``None`` means "not my job; let another macro try".

  2. **js_body**: a JS source fragment (NOT a complete function)
     Concatenated after the primitives bundle and wrapped in
     ``async (step) => {{ ... }}``. Must return
     ``{ok: bool, reason?: str, ...details}``.

  3. **postcheck_question** (optional): ``(step, replay_result) → question``
     When the JS body returns ``ok=False`` with a reason in
     ``retryable_with_vl_reasons``, this question is sent to
     ``vlm.judge_screenshot`` as a second-chance visual oracle. Returning
     ``None`` from the callback (or leaving the callback ``None``) disables
     the VL fallback for this macro.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

ParseFn = Callable[[str], Optional[dict[str, Any]]]
"""goal → step dict or None."""

PostcheckQuestionFn = Callable[[dict[str, Any], dict[str, Any]], Optional[str]]
"""(step, replay_result) → VL judge question or None."""


@dataclass(frozen=True)
class Macro:
    """A semantic macro registered with the framework.

    Attributes:
        action: Unique identifier matching ``step["action"]`` in RPA trails.
            Used as both registry key and dispatch key in the replay loop.
        parse: Goal parser. The parser SHOULD set
            ``result["action"] = self.action`` for round-trip integrity.
        js_body: JavaScript fragment (statements + expressions) run inside
            ``async (step) => {{ <PRIMITIVES> ; <BODY> }}``. Receives the step
            dict as the ``step`` parameter. Must return
            ``{ok: bool, reason?: str, ...details}``.
        postcheck_question: Builds the VL-judge question when JS reports
            ``ok=False`` with a retryable reason. Optional.
        retryable_with_vl_reasons: Subset of failure reasons (string values)
            for which the VL judge should be consulted. Other failures bubble
            up as hard errors.
        priority: Lower runs first when ``registry.parse_goal`` tries parsers
            in order. Useful when two parsers might both match a goal
            (e.g. "calendar date" + generic "click first non-ad").
    """

    action: str
    parse: ParseFn
    js_body: str
    postcheck_question: PostcheckQuestionFn | None = None
    retryable_with_vl_reasons: frozenset[str] = field(
        default_factory=lambda: frozenset({"postcheck_failed"})
    )
    priority: int = 100

    def __post_init__(self) -> None:
        if not self.action or not self.action.strip():
            raise ValueError("Macro.action must be a non-empty string")
        if not callable(self.parse):
            raise TypeError("Macro.parse must be callable")
        if not isinstance(self.js_body, str) or not self.js_body.strip():
            raise ValueError("Macro.js_body must be a non-empty string")

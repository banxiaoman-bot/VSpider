"""Semantic macro registry.

Public API:
  - ``register(macro)``         -- add a macro to the registry
  - ``unregister(action)``      -- remove a macro (mainly for tests)
  - ``get(action)``             -- look up by action name
  - ``actions()``               -- frozenset of registered action names
  - ``parse_goal(goal)``        -- try every parser in priority order; return
                                   the first non-None step dict (or None)
  - ``build_macro_js(body)``    -- wrap a macro JS body with the primitives
                                   bundle and the ``async (step) => {{…}}``
                                   shell, ready for ``page.evaluate``
  - ``replay_step(...)``        -- async unified dispatcher: runs the JS body
                                   for ``step["action"]`` and falls back to
                                   ``vlm.judge_screenshot`` on retryable
                                   failures
  - ``MACRO_PRIMITIVES``        -- re-exported JS primitives bundle constant

The registry is module-level state. Tests should ``unregister`` what they
``register`` (a ``snapshot_registry()`` context manager helps).
"""

from __future__ import annotations

import base64
import logging
from contextlib import contextmanager
from typing import Any, Iterator

from ._primitives import MACRO_PRIMITIVES
from ._types import Macro

logger = logging.getLogger(__name__)

__all__ = [
    "MACRO_PRIMITIVES",
    "Macro",
    "actions",
    "build_macro_js",
    "get",
    "parse_goal",
    "register",
    "replay_step",
    "snapshot_registry",
    "unregister",
]

_REGISTRY: dict[str, Macro] = {}


def register(macro: Macro) -> None:
    """Add a macro to the registry. Duplicate ``action`` overwrites silently
    (re-registration is convenient when re-importing during development).
    """
    if not isinstance(macro, Macro):  # defensive — catches accidental dict pass
        raise TypeError(f"register() expected Macro, got {type(macro).__name__}")
    _REGISTRY[macro.action] = macro


def unregister(action: str) -> Macro | None:
    """Remove a macro by action name. Returns the removed entry or None."""
    return _REGISTRY.pop(action, None)


def get(action: str) -> Macro | None:
    """Look up a macro by action name."""
    return _REGISTRY.get(action)


def actions() -> frozenset[str]:
    """Names of all currently registered macros."""
    return frozenset(_REGISTRY.keys())


def parse_goal(goal: str) -> dict | None:
    """Try every registered parser in priority order.

    Returns the first non-None step dict. Parsers that raise are skipped
    silently — a buggy parser must not prevent other macros from matching.

    Ties on priority are broken by action name (deterministic).
    """
    if not goal:
        return None
    ordered = sorted(_REGISTRY.values(), key=lambda m: (m.priority, m.action))
    for macro in ordered:
        try:
            result = macro.parse(goal)
        except Exception:
            continue
        if result:
            # Defensive: enforce the action contract so callers don't get a
            # mis-routed step dict if a parser forgot to set it.
            if isinstance(result, dict):
                result.setdefault("action", macro.action)
            return result
    return None


def build_macro_js(body: str) -> str:
    """Concatenate primitives + body into an ``async (step) => {{…}}`` shell.

    The result is suitable for ``page.evaluate(JS, step_dict)``. The JS body
    receives the step dict as the ``step`` parameter and primitives as
    in-scope ``const`` declarations.
    """
    if not isinstance(body, str) or not body.strip():
        raise ValueError("build_macro_js: body must be a non-empty string")
    return f"async (step) => {{\n{MACRO_PRIMITIVES}\n\n{body}\n}}"


@contextmanager
def snapshot_registry() -> Iterator[None]:
    """Context manager that saves and restores the registry. Tests should
    wrap their ``register()`` calls so they don't leak between cases."""
    saved = dict(_REGISTRY)
    try:
        yield
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(saved)


async def replay_step(
    page: Any,
    step: dict[str, Any],
    *,
    vlm: Any = None,
) -> dict[str, Any]:
    """Run a registered macro step.

    Args:
        page: Playwright ``Page`` (anything with ``async evaluate`` and
            ``async screenshot``).
        step: Step dict; ``step["action"]`` must match a registered macro.
        vlm: Optional VLM client exposing ``async judge_screenshot``. When
            provided, retryable failures (``reason`` in
            ``macro.retryable_with_vl_reasons``) trigger one visual second
            opinion before bubbling up.

    Returns:
        The JS body's result dict, possibly with ``ok`` flipped to ``True``
        by the VL judge (``vl_judge`` recorded on the dict either way).

        On non-dict JS returns we synthesize a failure dict so callers always
        get the same shape.

    Raises:
        ValueError: if ``step["action"]`` has no registered macro.
    """
    if not isinstance(step, dict):
        raise TypeError(f"replay_step: step must be a dict, got {type(step).__name__}")
    action = str(step.get("action") or "")
    macro = get(action)
    if macro is None:
        raise ValueError(f"replay_step: no macro registered for action {action!r}")

    js = build_macro_js(macro.js_body)
    raw = await page.evaluate(js, step)
    if not isinstance(raw, dict):
        return {"ok": False, "reason": "macro_returned_non_dict", "raw": raw}

    if raw.get("ok"):
        return raw

    reason = str(raw.get("reason") or "")
    if (
        vlm is None
        or macro.postcheck_question is None
        or reason not in macro.retryable_with_vl_reasons
    ):
        return raw

    try:
        question = macro.postcheck_question(step, raw)
    except Exception as q_err:
        logger.warning("[%s] postcheck_question raised: %s", action, q_err)
        return raw
    if not question:
        return raw

    try:
        ss_bytes = await page.screenshot(type="jpeg", quality=70, full_page=False)
        ss_b64 = base64.b64encode(ss_bytes).decode("utf-8")
    except Exception as ss_err:
        logger.warning("[%s] judge screenshot failed: %s", action, ss_err)
        ss_b64 = None

    judge = await vlm.judge_screenshot(
        ss_b64,
        question,
        context=f"action={action}; js_reason={reason}; observed={raw.get('observed','')}",
    )
    raw["vl_judge"] = judge
    if isinstance(judge, dict) and judge.get("verdict") == "yes":
        logger.info(
            "[%s] JS reason=%s but VL judge=yes (%s); accepting",
            action, reason, str(judge.get("reason", ""))[:120],
        )
        raw["ok"] = True
        raw["vl_recovered"] = True
    return raw


# ── Bootstrap: import each macro module so it self-registers. Keep this at
# the bottom to avoid circular-import surprises (macro modules import from
# ``__init__`` to call ``register``). ──────────────────────────────────────────
from . import cascader_pick as _cascader_pick  # noqa: E402,F401
from . import date_pick as _date_pick  # noqa: E402,F401

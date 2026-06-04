"""Sync ``goal -> URL`` semantic callable for the preflight entry gate.

``suggest_entry_url(goal, llm=...)`` / ``build_preflight(..., llm=...)`` accept a
**synchronous** ``prompt -> text`` callable so the io_contract layer stays pure
and testable. The runtime semantic client (``vlm_client.VLMClient``) is built on
``AsyncOpenAI`` and cannot be handed to that sync seam directly. This module
bridges the gap: it builds a small one-shot **synchronous** OpenAI chat
completion bound to the project's semantic config, which the main entry points
(``run_agent`` / ``start_batch``) inject into ``build_preflight`` so a URL-less
goal is resolved by the model picking a real site (source ``llm``) instead of
silently degrading to the Bing search fallback.

No network IO happens at import time; the callable performs a single blocking
completion only when actually invoked (i.e. only when no URL was supplied).
"""

from __future__ import annotations

from typing import Callable


def make_entry_llm(
    *,
    api_base: str = "",
    api_key: str = "",
    model: str = "",
    timeout: float = 20.0,
) -> Callable[[str], str] | None:
    """Build a sync ``prompt -> text`` callable backed by a one-shot completion.

    Returns ``None`` when ``model`` is empty (no semantic model configured) or
    when the ``openai`` SDK / client cannot be constructed -- callers then fall
    back to ``suggest_entry_url``'s deterministic search entry. The returned
    callable swallows its own errors and yields ``""`` on failure so a flaky
    semantic endpoint never aborts a run.
    """

    model = (model or "").strip()
    if not model:
        return None

    try:
        from openai import OpenAI
    except Exception:
        return None

    try:
        client = OpenAI(
            base_url=api_base or None,
            api_key=api_key or "EMPTY",
            timeout=timeout or 20.0,
        )
    except Exception:
        return None

    def _call(prompt: str) -> str:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception:
            return ""

    return _call


def entry_llm_from_config() -> Callable[[str], str] | None:
    """Build :func:`make_entry_llm` from the project's semantic VLM config.

    Sources ``VLM_SEMANTIC_*`` (falling back to ``VLM_*``) the same way
    ``vlm_client.VLMClient`` does, so the entry-inference model matches the
    planner the agent already uses. Best-effort: any missing config / import
    error yields ``None`` and the deterministic search fallback takes over.
    """

    try:
        from .. import config as cfg
    except Exception:
        try:
            import config as cfg  # type: ignore
        except Exception:
            return None

    model = (
        getattr(cfg, "VLM_SEMANTIC_MODEL_NAME", "")
        or getattr(cfg, "VLM_MODEL_NAME", "")
    )
    api_base = (
        getattr(cfg, "VLM_SEMANTIC_API_BASE", "")
        or getattr(cfg, "VLM_API_BASE", "")
    )
    api_key = (
        getattr(cfg, "VLM_SEMANTIC_API_KEY", "")
        or getattr(cfg, "VLM_API_KEY", "")
    )
    try:
        timeout = float(getattr(cfg, "VLM_TIMEOUT", 20) or 20)
    except (TypeError, ValueError):
        timeout = 20.0

    return make_entry_llm(
        api_base=str(api_base or ""),
        api_key=str(api_key or ""),
        model=str(model or ""),
        timeout=timeout,
    )

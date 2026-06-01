"""Wire ``media_harvester`` into the agent loop as a deterministic fast path.

This module is the *only* place that knows how to bridge the agent loop
(``main.py``) with the ``media_harvester`` package. ``main.py`` calls
:func:`maybe_run_media_harvest` once after the browser has navigated to
the start URL; the hook decides whether to run, whether to short-circuit
the VLM loop, and reports a structured :class:`MediaHarvestHookResult`.

Design constraints:

- The hook MUST be safe to call even when ``capability_route`` is absent
  or malformed; it returns a no-op result with ``skip_reason``.
- The hook MUST NOT raise. Internal failures are captured into
  ``skip_reason`` so the caller can ``logger.debug`` and continue.
- The hook MUST NOT depend on Playwright types at import time so the
  module is unit-testable with a fake page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .candidates import collect_from_html
from .harvester import HarvestReport, harvest_to_run


_MEDIA_OUTPUT_KINDS: frozenset[str] = frozenset({
    "media_image",
    "media_video",
    "media_audio",
    "media_pdf",
    "media_archive",
    "file_generic",
})


# Goal tokens that indicate the user wants MORE than just downloading the
# media (extraction, summarisation, analysis, etc.). When any of these
# appear we do not short-circuit: we let the VLM loop run so it can also
# fulfil the secondary intent. Matched case-insensitively against the
# normalised goal text. Includes both English and Chinese tokens because
# VSpider runs against bilingual user goals.
_NON_PURE_MEDIA_TOKENS: tuple[str, ...] = (
    "extract",
    "extraction",
    "summarize",
    "summarise",
    "summary",
    "analyse",
    "analyze",
    "analysis",
    "report",
    "translate",
    "compare",
    "review",
    "read",
    "提取",
    "抽取",
    "总结",
    "概括",
    "归纳",
    "分析",
    "翻译",
    "对比",
    "比较",
    "阅读",
    "审阅",
    "审查",
    "评估",
)


@dataclass
class MediaHarvestHookResult:
    """Structured outcome of one ``maybe_run_media_harvest`` invocation."""

    triggered: bool = False
    short_circuit: bool = False
    success: bool = False
    output_kind: str = ""
    downloaded_count: int = 0
    failed_count: int = 0
    manifest_appended: int = 0
    candidate_count: int = 0
    skip_reason: str = ""
    report_dict: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered": bool(self.triggered),
            "short_circuit": bool(self.short_circuit),
            "success": bool(self.success),
            "output_kind": str(self.output_kind or ""),
            "downloaded_count": int(self.downloaded_count),
            "failed_count": int(self.failed_count),
            "manifest_appended": int(self.manifest_appended),
            "candidate_count": int(self.candidate_count),
            "skip_reason": str(self.skip_reason or ""),
            "report": dict(self.report_dict or {}),
        }


def _extract_output_kind(capability_route: dict[str, Any] | None) -> str:
    if not isinstance(capability_route, dict):
        return ""
    output_contract = capability_route.get("output_contract")
    if not isinstance(output_contract, dict):
        return ""
    return str(output_contract.get("output_kind") or "")


def _should_trigger(capability_route: dict[str, Any] | None) -> tuple[bool, str, str]:
    """Return ``(triggered, output_kind, skip_reason)``.

    ``triggered`` is True only when capability_route has an
    ``output_contract.output_kind`` that media_harvester knows how to
    handle.
    """

    if capability_route is None:
        return False, "", "no_route"
    output_kind = _extract_output_kind(capability_route)
    if not output_kind:
        return False, "", "no_output_kind"
    if output_kind not in _MEDIA_OUTPUT_KINDS:
        return False, output_kind, f"output_kind_not_media:{output_kind}"
    return True, output_kind, ""


def is_pure_media_goal(goal: str) -> bool:
    """Heuristic: True when the goal is just "download / fetch media".

    Returns False when the goal also asks for extraction, summarisation,
    analysis, translation, comparison, or reading — those require the
    VLM loop to actually inspect content, so we must not short-circuit.
    """

    text = (goal or "").strip().lower()
    if not text:
        return False
    for token in _NON_PURE_MEDIA_TOKENS:
        key = token.strip().lower()
        if not key:
            continue
        if re.search(r"[\u4e00-\u9fff]", key):
            if key in text:
                return False
        else:
            pattern = rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])"
            if re.search(pattern, text):
                return False
    return True


async def _read_page_html(page: Any) -> tuple[str, str, str]:
    """Best-effort fetch of ``(html, page_url, error)``.

    Tries common Playwright Page shapes (``content()`` / ``url``). When a
    test injects a fake page exposing ``html`` / ``current_url``
    attributes those are honoured too. Any exception returns
    ``("", "", reason)``.
    """

    if page is None:
        return "", "", "no_page"
    html = ""
    page_url = ""
    try:
        if hasattr(page, "content"):
            content_attr = page.content
            html_raw = content_attr() if callable(content_attr) else content_attr
            if hasattr(html_raw, "__await__"):
                html_raw = await html_raw
            html = str(html_raw or "")
        elif hasattr(page, "html"):
            html = str(getattr(page, "html", "") or "")
    except Exception as exc:
        return "", "", f"page_html_failed:{exc}"
    try:
        if hasattr(page, "url"):
            page_url = str(getattr(page, "url", "") or "")
        elif hasattr(page, "current_url"):
            page_url = str(getattr(page, "current_url", "") or "")
    except Exception:
        page_url = ""
    return html, page_url, ""


async def maybe_run_media_harvest(
    *,
    page: Any,
    capability_route: dict[str, Any] | None,
    run_id: str,
    goal: str,
    base_dir: str | Path | None = None,
    max_items: int | None = 200,
    max_bytes_per_item: int | None = 100_000_000,
) -> MediaHarvestHookResult:
    """Run media_harvester as a deterministic fast path when applicable.

    Returns a :class:`MediaHarvestHookResult`. Never raises: any internal
    error is captured into ``skip_reason`` and ``triggered`` stays False.
    """

    triggered, output_kind, skip_reason = _should_trigger(capability_route)
    if not triggered:
        return MediaHarvestHookResult(
            triggered=False,
            output_kind=output_kind,
            skip_reason=skip_reason,
        )

    if page is None:
        return MediaHarvestHookResult(
            triggered=False,
            output_kind=output_kind,
            skip_reason="no_page",
        )

    if not (run_id or "").strip():
        return MediaHarvestHookResult(
            triggered=False,
            output_kind=output_kind,
            skip_reason="no_run_id",
        )

    html, page_url, html_err = await _read_page_html(page)
    if html_err:
        return MediaHarvestHookResult(
            triggered=False,
            output_kind=output_kind,
            skip_reason=html_err,
        )

    try:
        candidates = collect_from_html(html, base_url=page_url or "")
    except Exception as exc:
        return MediaHarvestHookResult(
            triggered=False,
            output_kind=output_kind,
            skip_reason=f"collect_failed:{exc}",
        )

    try:
        report: HarvestReport = harvest_to_run(
            candidates,
            run_id=str(run_id),
            output_kind=output_kind,
            base_dir=base_dir,
            max_items=max_items,
            max_bytes_per_item=max_bytes_per_item,
        )
    except Exception as exc:
        return MediaHarvestHookResult(
            triggered=True,
            output_kind=output_kind,
            candidate_count=len(candidates or []),
            skip_reason=f"harvest_failed:{exc}",
        )

    downloaded_count = len(report.downloaded or [])
    failed_count = len(report.failed or [])
    success = downloaded_count > 0 and failed_count == 0
    short_circuit = bool(success and is_pure_media_goal(goal))

    return MediaHarvestHookResult(
        triggered=True,
        short_circuit=short_circuit,
        success=success,
        output_kind=output_kind,
        downloaded_count=downloaded_count,
        failed_count=failed_count,
        manifest_appended=int(report.manifest_appended),
        candidate_count=len(candidates or []),
        report_dict=report.to_dict(),
    )


__all__ = [
    "MediaHarvestHookResult",
    "is_pure_media_goal",
    "maybe_run_media_harvest",
]

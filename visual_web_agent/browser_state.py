"""Compact browser state snapshot for logs, prompts, and debugging."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

try:
    from .action_result import ActionResult
except ImportError:
    from action_result import ActionResult


def _clip(text: Any, limit: int = 1200) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated {len(value) - limit} chars]..."


def _action_result_dict(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    if isinstance(result, ActionResult):
        return result.to_dict()
    if isinstance(result, dict):
        return dict(result)
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"repr": repr(result)}


_BROWSER_STATE_V2_VERSION = "browser_state.v2"


def _compact_som_element(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"repr": repr(item)}
    rect = item.get("rect") if isinstance(item.get("rect"), dict) else {}
    return {
        "id": item.get("id"),
        "ref": f"@e{item.get('id')}" if item.get("id") not in (None, "") else item.get("ref", ""),
        "role": item.get("role") or item.get("tag") or "",
        "name": _clip(item.get("name") or item.get("text") or "", 160),
        "state": _clip(item.get("state") or "", 120),
        "input_desc": _clip(item.get("inputDesc") or "", 160),
        "bbox": {
            "x": rect.get("x"),
            "y": rect.get("y"),
            "width": rect.get("width"),
            "height": rect.get("height"),
        },
    }


def _action_refs_from_mapping(element_mapping: Any) -> list[dict[str, Any]]:
    if not isinstance(element_mapping, dict):
        return []
    refs: list[dict[str, Any]] = []
    for ref, data in sorted(element_mapping.items(), key=lambda item: str(item[0])):
        value = dict(data or {}) if isinstance(data, dict) else {}
        value.setdefault("ref", str(ref))
        refs.append(value)
    return refs


def _snapshot_dict(snapshot: Any) -> dict[str, Any]:
    if snapshot is None:
        return {}
    if isinstance(snapshot, BrowserStateSnapshot):
        return snapshot.to_dict()
    if isinstance(snapshot, dict):
        return dict(snapshot)
    to_dict = getattr(snapshot, "to_dict", None)
    if callable(to_dict):
        try:
            value = to_dict()
            if isinstance(value, dict):
                return dict(value)
        except Exception:
            pass
    return {}


def build_browser_state_v2(
    snapshot: Any = None,
    *,
    som_elements: list[dict[str, Any]] | None = None,
    element_mapping: dict[str, Any] | None = None,
    action_refs: list[dict[str, Any]] | None = None,
    network_candidates: list[dict[str, Any]] | None = None,
    runtime_status: dict[str, Any] | None = None,
    extraction: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = _snapshot_dict(snapshot)
    som = [_compact_som_element(item) for item in (som_elements or [])[:200]]
    refs = list(action_refs or []) or _action_refs_from_mapping(element_mapping or {})
    network = list(network_candidates or [])[:50]
    interactive_count = int(data.get("interactive_count") or len(som) or len(refs) or 0)
    ax_line_count = int(data.get("ax_line_count") or 0)
    return {
        "version": _BROWSER_STATE_V2_VERSION,
        "source": "browser_state",
        "page": {
            "url": str(data.get("url") or ""),
            "title": str(data.get("title") or ""),
            "summary": _clip(data.get("page_summary") or "", 1200),
        },
        "artifacts": {
            "screenshot_path": str(data.get("screenshot_path") or ""),
        },
        "perception": {
            "visible_text_excerpt": _clip(data.get("visible_text_excerpt") or "", 1200),
            "ax_tree_excerpt": _clip(data.get("ax_tree_excerpt") or "", 1800),
            "ax_line_count": ax_line_count,
            "dom_shape": dict(data.get("dom_shape") or {}),
            "som": {
                "interactive_count": interactive_count,
                "elements": som,
            },
        },
        "interaction": {
            "action_refs": refs,
            "action_ref_count": len(refs),
            "last_action_result": _action_result_dict(data.get("last_action_result")),
        },
        "navigation": {
            "tabs": str(data.get("tabs") or ""),
        },
        "runtime": dict(runtime_status or {}),
        "data": {
            "network_candidates": network,
            "network_candidate_count": len(network),
            "extraction": dict(extraction or {}),
        },
        "metrics": {
            "interactive_count": interactive_count,
            "ax_line_count": ax_line_count,
            "network_candidate_count": len(network),
            "action_ref_count": len(refs),
        },
        "metadata": dict(metadata or data.get("metadata") or {}),
    }


async def build_browser_state_v2_from_browser(
    browser: Any,
    *,
    step: int = 0,
    screenshot_path: str = "",
    ax_tree_text: str = "",
    tabs: str = "",
    page_summary: str = "",
    dom_shape: dict[str, Any] | None = None,
    last_action_result: Any = None,
    network_candidates: list[dict[str, Any]] | None = None,
    runtime_status: dict[str, Any] | None = None,
    extraction: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = await BrowserStateSnapshot.from_browser(
        browser,
        step=step,
        screenshot_path=screenshot_path,
        ax_tree_text=ax_tree_text,
        tabs=tabs,
        page_summary=page_summary,
        dom_shape=dom_shape,
        last_action_result=last_action_result,
        metadata=metadata,
    )
    return build_browser_state_v2(
        snapshot,
        som_elements=getattr(browser, "_last_som_elements", None) or [],
        element_mapping=getattr(browser, "element_mapping", None) or {},
        network_candidates=network_candidates,
        runtime_status=runtime_status,
        extraction=extraction,
        metadata=metadata,
    )


@dataclass
class BrowserStateSnapshot:
    """Serializable summary of the current active browser page."""

    step: int = 0
    url: str = ""
    title: str = ""
    screenshot_path: str = ""
    ax_tree_excerpt: str = ""
    ax_line_count: int = 0
    interactive_count: int = 0
    dom_shape: dict[str, Any] = field(default_factory=dict)
    visible_text_excerpt: str = ""
    tabs: str = ""
    page_summary: str = ""
    last_action_result: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    async def from_browser(
        cls,
        browser: Any,
        *,
        step: int = 0,
        screenshot_path: str = "",
        ax_tree_text: str = "",
        tabs: str = "",
        page_summary: str = "",
        dom_shape: dict[str, Any] | None = None,
        last_action_result: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> "BrowserStateSnapshot":
        page = None
        url = ""
        title = ""
        visible_text = ""
        try:
            page = await browser._ensure_active_page(reason="browser state snapshot")
        except Exception:
            page = getattr(browser, "_page", None)

        if page is not None:
            try:
                url = page.url or ""
            except Exception:
                url = ""
            try:
                title = (await page.title()) or ""
            except Exception:
                title = ""
            try:
                visible_text = await page.evaluate(
                    """() => {
                        const text = document.body ? document.body.innerText || '' : '';
                        return String(text).replace(/\\s+/g, ' ').trim().slice(0, 1600);
                    }"""
                )
            except Exception:
                visible_text = ""

        if not url:
            try:
                url = browser.current_url or ""
            except Exception:
                url = ""
        if not tabs:
            try:
                tabs = await browser.get_tabs_state()
            except Exception:
                tabs = ""
        if not page_summary:
            try:
                page_summary = await browser.get_active_page_summary()
            except Exception:
                page_summary = ""

        interactive_count = 0
        try:
            interactive_count = len(getattr(browser, "_last_som_elements", []) or [])
        except Exception:
            interactive_count = 0
        if not interactive_count:
            try:
                interactive_count = len(getattr(browser, "element_mapping", {}) or {})
            except Exception:
                interactive_count = 0

        ax_lines = ax_tree_text.splitlines() if ax_tree_text else []
        return cls(
            step=int(step or 0),
            url=url,
            title=title,
            screenshot_path=str(screenshot_path or ""),
            ax_tree_excerpt=_clip(ax_tree_text, 1800),
            ax_line_count=len(ax_lines),
            interactive_count=int(interactive_count or 0),
            dom_shape=dict(dom_shape or {}),
            visible_text_excerpt=_clip(visible_text, 1200),
            tabs=str(tabs or ""),
            page_summary=_clip(page_summary, 1200),
            last_action_result=_action_result_dict(last_action_result),
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

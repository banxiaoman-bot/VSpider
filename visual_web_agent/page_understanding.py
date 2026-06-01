"""Generic page understanding — prefer local perception over site-specific macros."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


SITE_SPECIFIC_MACROS = frozenset({
    "round_form_challenge",
    "reactrouter_docs_macro",
    "internet_hovers_macro",
    "demoqa_droppable_slider_macro",
    "demoqa_slider_macro",
    "selectorshub_shadow_iframe_macro",
    "wikipedia_new_tab_macro",
})

GENERIC_FIRST_TOOLS = (
    "targeted_probe",
    "table_extract",
    "list_extract",
    "auto_form_fill",
    "modal_dialog_macro",
)

_GENERIC_GOAL_RE = re.compile(
    r"\b(fill|click|extract|find|open|submit|search|download|upload|navigate)\b"
    r"|填写|点击|提取|查找|打开|提交|搜索|下载|上传|导航",
    re.I,
)


def _host(url: str) -> str:
    try:
        return (urlparse(str(url or "")).netloc or "").lower()
    except Exception:
        return ""


def _tool_url_match(tool: dict[str, Any], url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    blob = " ".join(
        str(tool.get(key) or "")
        for key in ("name", "description", *tool.get("aliases", ()))
    ).lower()
    aliases = tool.get("aliases") or []
    if isinstance(aliases, (list, tuple)):
        blob += " " + " ".join(str(a) for a in aliases)
    return host in blob or any(host in str(a).lower() for a in aliases)


def is_generic_surface_goal(goal: str, url: str = "") -> bool:
    text = str(goal or "").strip()
    if not text:
        return False
    if _GENERIC_GOAL_RE.search(text):
        return True
    return not _host(url)


def reprioritize_agent_tools(
    selected_tools: list[dict[str, Any]],
    *,
    goal: str,
    url: str = "",
) -> list[dict[str, Any]]:
    """Boost generic perception tools; demote site macros unless URL matches."""

    if not selected_tools:
        return selected_tools
    generic = is_generic_surface_goal(goal, url)
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, tool in enumerate(selected_tools):
        name = str(tool.get("name") or "")
        score = int(tool.get("match_score") or 0)
        if name in GENERIC_FIRST_TOOLS:
            score += 6 if generic else 2
        if name in SITE_SPECIFIC_MACROS:
            if _tool_url_match(tool, url):
                score += 4
            elif generic:
                score = max(0, score - 8)
        ranked.append((score, index, tool))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    out: list[dict[str, Any]] = []
    for score, _index, tool in ranked:
        item = dict(tool)
        item["match_score"] = score
        reasons = list(item.get("strategy_reasons") or [])
        if generic and str(item.get("name") or "") in GENERIC_FIRST_TOOLS:
            reasons.append("page_understanding:generic_surface_boost")
        if str(item.get("name") or "") in SITE_SPECIFIC_MACROS and not _tool_url_match(item, url):
            reasons.append("page_understanding:site_macro_demoted")
        if reasons:
            item["strategy_reasons"] = reasons
        out.append(item)
    return out


def page_understanding_policy(goal: str, url: str = "") -> dict[str, Any]:
    generic = is_generic_surface_goal(goal, url)
    return {
        "version": "page_understanding.v1",
        "generic_surface": generic,
        "preferred_tools": list(GENERIC_FIRST_TOOLS) if generic else ["targeted_probe"],
        "demote_site_macros": generic,
        "first_action": "targeted_probe" if generic else "",
    }

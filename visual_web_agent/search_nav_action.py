"""``open_top_search_result`` deterministic capability (Slice SEARCH-NAV).

When a goal carries no usable URL, the entry preflight drops the agent on a
search-engine results page (``EntrySuggestion.source == "search_fallback"``).
Picking the right destination off a SERP is hostile to coordinate-only VLM
clicks: ads, sponsored cards, "people also ask", news strips and sidebars all
mimic real result links. This handler makes that step deterministic and
ad-safe:

    open_top_search_result:
        probe ranked organic results (ads / nav / sidebars / same-engine
        internal links excluded in-page)
        -> navigate to the first candidate
        -> landing-page 2nd pass: drop sponsored / paid-click / tracking
           redirects (``is_ad_redirect_url``) and same-search-host dead ends
        -> rotate to the next candidate until a real destination is reached

Shipped as its own module (workflow rule "新功能拆模块") instead of being
appended to the oversized ``actions.py``. The selection / navigation core lives
in ``search_result_guards.open_top_organic_result`` (stub-frame testable); this
module is the thin :class:`ActionHandler` that records evidence + memory +
rpa_trail.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

try:
    from .actions import (
        ActionContext,
        ActionExecutionError,
        ActionHandler,
        ActionRegistry,
    )
    from .search_result_guards import open_top_organic_result
except ImportError:  # pragma: no cover - flat-layout fallback, mirrors actions.py
    from actions import (  # type: ignore[no-redef]
        ActionContext,
        ActionExecutionError,
        ActionHandler,
        ActionRegistry,
    )
    from search_result_guards import open_top_organic_result  # type: ignore[no-redef]


_SEARCH_PARAM_NAMES = ("q", "query", "wd", "word", "keyword", "text", "p")


def _query_from_url(url: str) -> str:
    """Pull the search term off a SERP URL (``?q=`` / ``?wd=`` / ...)."""
    try:
        params = parse_qs(urlparse(url or "").query or "")
    except Exception:
        return ""
    for name in _SEARCH_PARAM_NAMES:
        values = params.get(name)
        if values and str(values[0]).strip():
            return str(values[0]).strip()
    return ""


@ActionRegistry.register("open_top_search_result")
class OpenTopSearchResultHandler(ActionHandler):
    """Open the first non-ad organic result on the current search-results page."""

    async def execute(self, ctx: ActionContext) -> Optional[Any]:
        browser = ctx.browser
        page = ctx.page
        if page is None:
            raise ActionExecutionError("open_top_search_result: 无活动页面。")

        # Query resolution: explicit type_value wins; otherwise read it off the
        # current SERP URL (?q= / ?wd= ...). No goal text is threaded into a
        # handler, so the search-page URL is the deterministic fallback source.
        query = (ctx.action.type_value or "").strip()
        current_url = str(getattr(browser, "current_url", "") or "")
        if not query:
            query = _query_from_url(current_url)
        if not query:
            raise ActionExecutionError(
                "open_top_search_result: 无法确定搜索关键词；请在 type_value 填写，"
                "或确保当前页是带 ?q= 查询参数的搜索结果页。"
            )

        try:
            want = int(getattr(ctx.action, "target_id", 0) or 0)
        except (TypeError, ValueError):
            want = 0
        want = want if want > 1 else 1
        result = await open_top_organic_result(browser, query=query, want=want)
        if result is None:
            raise ActionExecutionError(
                "open_top_search_result: 当前页未找到任何有机结果候选；"
                "可能不是搜索结果页或结果尚未加载。"
            )
        if result.get("failed") or not result.get("url"):
            skipped = result.get("skipped") or []
            raise ActionExecutionError(
                "open_top_search_result: "
                f"{int(result.get('candidates') or 0)} 个候选全部为广告/跳转死链"
                f"（已跳过 {len(skipped)} 条），未能导航到真实结果。请改用其它动作。"
            )

        landing = str(result.get("url") or "")
        title = str(result.get("title") or "")
        skipped = result.get("skipped") or []
        mem_key = (ctx.action.memory_key or "").strip() or "search_top_result"
        try:
            ctx.workflow_memory[mem_key] = {
                "url": landing,
                "title": title,
                "query": query,
                "ads_skipped": len(skipped),
                "results": result.get("results") or [],
            }
        except Exception:  # pragma: no cover - memory is a plain dict in practice
            pass
        try:
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "open_top_search_result",
                    "type_value": query,
                    "source_url": current_url,
                    "result_url": landing,
                    "result_title": title,
                    "candidates": int(result.get("candidates") or 0),
                    "ads_skipped": len(skipped),
                    "results_count": len(result.get("results") or []),
                    "verified": True,
                })
            )
        except Exception:  # pragma: no cover - trail is best-effort evidence
            pass
        return None

"""Generic guards for opening the first organic search result.

The VLM is good at reading intent, but search result pages are hostile to
coordinate-only decisions: ads, news cards, sidebars, and suggestions all reuse
"result-like" visual patterns. This module keeps the intent generic while using
DOM structure and query relevance to choose the first main result link.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlparse

try:
    from .search_reentry_guards import parse_first_search_query
except ImportError:  # pragma: no cover - direct script import fallback
    from search_reentry_guards import parse_first_search_query

logger = logging.getLogger(__name__)

_FIRST_RESULT_RE = re.compile(
    r"(?:click|open|打开|点击).{0,40}(?:first|第一个|首个|第一条).{0,40}"
    r"(?:result|结果|link|链接|条目)",
    re.I,
)
_NEW_TAB_RE = re.compile(r"(?:new\s*tab|新标签|新窗口|新页面|中键|后台)", re.I)

# Canonical ad-flavoured token regex — single source of truth for both:
#   * the in-page first-result picker (JS template literal at line ~128 below)
#   * the Python-side ``is_ad_like_text`` helper used by extraction filters
# Keep the two in sync whenever a new locale / vocabulary is added.
_AD_TEXT_RE = re.compile(
    r"(广告|廣告|赞助|贊助|推广|推廣|商业推广|品牌广告|"
    r"\bsponsored\b|\bpromoted?\b|\bads?\b|"
    r"\bpromotion\b|\bpartnership\b)",
    re.I,
)


def is_ad_like_text(text: str | None) -> bool:
    """Heuristic: does the snippet look like a sponsored / promoted card?

    Used by feed-style extractors (Juejin / Weibo / Zhihu / Toutiao) to drop
    promo cards from "前 N 条真实文章" style goals. Mirrors the JS regex
    embedded in ``first_result_picker_js`` so a candidate that looks like an
    ad in the in-page picker also looks like one to the Python extraction
    filter — no double-counting, no leak-through.

    Empty / falsy input → False (don't drop blanks here; that's a separate
    filter). Whitespace-only → False.
    """
    if not text:
        return False
    snippet = str(text).strip()
    if not snippet:
        return False
    return bool(_AD_TEXT_RE.search(snippet))


# Ad-redirect / paid-click URL signatures for the landing-page second pass. A
# result that looked organic in-page but whose href (or final landing URL)
# matches these is a sponsored / tracking redirect, not an organic destination
# -- open_top_organic_result drops it and rotates to the next candidate.
_AD_REDIRECT_HOST_RE = re.compile(
    r"(doubleclick\.net|googleadservices\.com|googlesyndication\.com|"
    r"adservice\.|adnxs\.com|adsystem\.|2mdn\.net|adform\.net|"
    r"taboola\.com|outbrain\.com|criteo\.com|zedo\.com)",
    re.I,
)
_AD_REDIRECT_PATH_RE = re.compile(
    r"/(aclk|aclick|pagead|adclick|adserver|adservice)(/|$|\?|=)", re.I
)
_AD_REDIRECT_PARAM_RE = re.compile(
    r"(?:^|[?&])(adurl|gclid|msclkid|dclid|gclsrc|wbraid|gbraid|fbclid)=", re.I
)
_AD_REDIRECT_CPC_RE = re.compile(
    r"(?:^|[?&])utm_(?:medium|source)=(?:cpc|ppc|ads?|sponsored|paid)", re.I
)


def is_ad_redirect_url(url: str | None) -> bool:
    """Heuristic: is this URL a sponsored / paid-click / tracking redirect?

    Inspects host (ad networks), path (``/aclk``, ``/aclick``, ``/pagead`` ...)
    and query (``gclid`` / ``msclkid`` / ``adurl`` / ``utm_medium=cpc`` ...).
    Used by :func:`open_top_organic_result` as a landing-page second pass so a
    candidate that *looked* organic in-page but resolves to an ad redirect is
    dropped and the next candidate is tried. Empty / falsy input -> False.
    """
    if not url:
        return False
    text = str(url).strip()
    if not text:
        return False
    try:
        parsed = urlparse(text)
        host = parsed.netloc.split("@")[-1].split(":")[0].lower()
        path = parsed.path or ""
        query = parsed.query or ""
    except Exception:
        host, path, query = "", "", text
    if host and _AD_REDIRECT_HOST_RE.search(host):
        return True
    if _AD_REDIRECT_PATH_RE.search(path):
        return True
    if _AD_REDIRECT_PARAM_RE.search(query) or _AD_REDIRECT_CPC_RE.search(query):
        return True
    return False


_SEARCH_PARAM_NAMES = ("q", "query", "wd", "word", "keyword", "text", "p")
_TAB_META_ACTIONS = {"switch_tab", "close_tab", "done", "ask_human", "error"}


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", unquote_plus(value or "").casefold())


def _query_from_url(url: str) -> str:
    try:
        parsed = urlparse(url or "")
    except Exception:
        return ""
    params = parse_qs(parsed.query or "")
    for name in _SEARCH_PARAM_NAMES:
        values = params.get(name)
        if values and str(values[0]).strip():
            return str(values[0]).strip()
    return ""


def _queries_match(current_query: str, expected_query: str) -> bool:
    cur = _compact(current_query)
    exp = _compact(expected_query)
    if not cur or not exp:
        return False
    return cur == exp or cur in exp or exp in cur


def goal_requests_first_result_new_tab(goal: str, decision: dict[str, Any] | None = None) -> bool:
    """Return True when the goal/head decision asks to open the first result.

    Site names are intentionally ignored. We key on operation semantics:
    "open/click" + "first" + "result/link" + "new tab".
    """
    text_parts = [goal or ""]
    if decision:
        text_parts.extend(
            str(decision.get(k) or "")
            for k in ("thought", "progress_review", "current_state", "type_value")
        )
        if str(decision.get("action") or "") == "click_new_tab":
            text_parts.append("new tab")
    text = " ".join(text_parts)
    return bool(_FIRST_RESULT_RE.search(text) and _NEW_TAB_RE.search(text))


def _decision_requests_first_result(decision: dict[str, Any]) -> bool:
    text = " ".join(
        str(decision.get(k) or "")
        for k in ("thought", "progress_review", "current_state", "type_value")
    )
    return bool(_FIRST_RESULT_RE.search(text))


def _has_non_current_open_tab(browser: Any) -> bool:
    ctx = getattr(browser, "_context", None)
    page = getattr(browser, "_page", None)
    if not ctx or not page:
        return False
    try:
        pages = [p for p in getattr(ctx, "pages", []) if not p.is_closed()]
    except Exception:
        return False
    return any(p is not page for p in pages)


def _probe_script(expected_query: str, limit: int = 1) -> str:
    # The script is generated with a literal query to keep the Playwright call
    # compatible with the simple fake frames used in unit tests.
    query_json = json.dumps(expected_query or "", ensure_ascii=False)
    return f"""
() => {{
    const expectedQuery = {query_json};
    const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
    const low = (s) => norm(s).toLowerCase();
    const present = (el) => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        if (!r || r.width < 24 || r.height < 12) return false;
        const cs = window.getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || 1) <= 0.01) return false;
        return r.bottom > 0 && r.right > 0;
    }};
    const hostOf = (href) => {{
        try {{ return new URL(href, location.href).hostname.replace(/^www\\./, '').toLowerCase(); }}
        catch (_) {{ return ''; }}
    }};
    const cleanHref = (href) => {{
        try {{ return new URL(href, location.href).href; }}
        catch (_) {{ return String(href || ''); }}
    }};
    const searchHost = hostOf(location.href);
    const queryTokens = Array.from(new Set([
        ...low(expectedQuery).match(/[a-z0-9][a-z0-9._-]{{2,}}/g) || [],
        ...norm(expectedQuery).match(/[\\u4e00-\\u9fff]{{2,}}/g) || [],
    ])).filter(Boolean);
    const bannedContainer = 'nav, header, footer, form, [role="navigation"], [role="banner"], [role="contentinfo"], aside';
    const adRe = /(广告|廣告|赞助|贊助|推广|推廣|sponsored|\\bad\\b|\\bads\\b)/i;
    const resultHint = 'li, article, section, .g, .b_algo, .result, .results, [data-sokoban-container], [data-testid*="result" i]';
    const anchors = Array.from(document.querySelectorAll('a[href]'));
    const candidates = [];
    for (const a of anchors) {{
        if (!present(a)) continue;
        const href = cleanHref(a.getAttribute('href') || a.href || '');
        if (!href || /^(javascript:|mailto:|tel:|#)/i.test(href)) continue;
        const h = hostOf(href);
        if (!h) continue;
        const r = a.getBoundingClientRect();
        if (r.top < 80) continue;
        if (a.closest(bannedContainer)) continue;
        const card = a.closest(resultHint) || a.parentElement || a;
        const title = norm(a.innerText || a.textContent || a.getAttribute('aria-label') || a.title || '');
        const cardText = norm(card.innerText || card.textContent || '');
        const hay = low([title, href, cardText].join(' '));
        if (title.length < 4 && !queryTokens.some(t => hay.includes(t.toLowerCase()))) continue;
        if (adRe.test(cardText.slice(0, 300))) continue;
        const url = new URL(href, location.href);
        const path = (url.pathname || '').toLowerCase();
        const sameSearchHost = h === searchHost || h.endsWith('.' + searchHost) || searchHost.endsWith('.' + h);
        if (sameSearchHost && /\\/(search|images|videos|news|maps|shop|travel|ck|aclick|url)(\\/|$)/i.test(path)) continue;
        if (queryTokens.length && !queryTokens.some(t => hay.includes(String(t).toLowerCase()))) continue;
        let score = 10000 - Math.max(0, r.top);
        if (card.matches && card.matches(resultHint)) score += 250;
        if (/h[1-3]/i.test(a.parentElement ? a.parentElement.tagName : '')) score += 120;
        if (r.left > (window.innerWidth || 1000) * 0.62) score -= 700;
        if (sameSearchHost) score -= 500;
        if (/\\b(wiki|docs|developer|learn|tutorial|guide|教程|文档)\\b/i.test(hay)) score += 40;
        candidates.push({{
            target_id: Number(a.getAttribute('data-som-id') || 0),
            text: title,
            href,
            host: h,
            top: r.top,
            left: r.left,
            score,
        }});
    }}
    candidates.sort((a, b) => b.score - a.score || a.top - b.top || a.left - b.left);
    const _limit = {max(1, int(limit or 1))}; if (_limit <= 1) return candidates[0] || null; return candidates.slice(0, _limit);
}}
"""


async def _probe_first_organic_result(browser: Any, expected_query: str) -> dict[str, Any] | None:
    page = await browser._ensure_active_page(reason="search result guard")
    if not page:
        return None
    script = _probe_script(expected_query)
    for frame in getattr(page, "frames", []) or [getattr(page, "main_frame", None)]:
        if frame is None:
            continue
        try:
            result = await frame.evaluate(script)
        except Exception:
            continue
        if isinstance(result, dict):
            return result
    return None


def _host(url: str) -> str:
    try:
        return urlparse(url or "").netloc.split("@")[-1].split(":")[0].strip().lower()
    except Exception:
        return ""


async def _probe_organic_results(
    browser: Any, expected_query: str, *, limit: int = 6
) -> list[dict[str, Any]]:
    """Return up to ``limit`` ranked organic (ad-filtered) result candidates."""
    page = await browser._ensure_active_page(reason="search result guard")
    if not page:
        return []
    script = _probe_script(expected_query, max(1, int(limit or 1)))
    for frame in getattr(page, "frames", []) or [getattr(page, "main_frame", None)]:
        if frame is None:
            continue
        try:
            result = await frame.evaluate(script)
        except Exception:
            continue
        if isinstance(result, list) and result:
            return [r for r in result if isinstance(r, dict)]
        if isinstance(result, dict):
            return [result]
    return []


_MAX_BROWSE = 5  # hard cap on how many top results to surface (avoid "clicking too many")


async def open_top_organic_result(
    browser: Any,
    *,
    query: str,
    max_candidates: int = 6,
    want: int = 1,
) -> dict[str, Any] | None:
    """Deterministically open the first non-ad organic result and navigate to it.

    Probes the current search-results page for ranked organic candidates (ads /
    nav / sidebars / same-engine internal links already excluded in-page), then
    walks them in order: navigate to each candidate and run a landing-page second
    pass (:func:`is_ad_redirect_url` + same-search-host check). The first
    candidate that resolves to a real, non-ad destination wins; the rest are
    recorded as ``skipped``. Returns a result dict (``url`` empty + ``failed``
    True when every candidate was an ad / dead end), or ``None`` when the page
    had no organic candidates at all. ``want`` (capped at ``_MAX_BROWSE``)
    also surfaces the top-N clean results in ``results`` for browsing without
    extra navigation. ``browser`` is duck-typed
    (``_ensure_active_page`` / ``current_url`` / ``page.goto`` /
    ``_wait_for_page_stable``) so this stays unit-testable with stub frames.
    """
    q = (query or "").strip()
    candidates = await _probe_organic_results(browser, q, limit=max_candidates)
    if not candidates:
        return None
    want = max(1, min(int(want or 1), _MAX_BROWSE))
    results: list[dict[str, Any]] = []
    for _cand in candidates:
        _href = str(_cand.get("href") or "").strip()
        if not _href or is_ad_redirect_url(_href):
            continue
        results.append({
            "rank": len(results) + 1,
            "title": str(_cand.get("text") or "").strip(),
            "url": _href,
        })
        if len(results) >= want:
            break
    page = await browser._ensure_active_page(reason="open top organic result")
    if not page:
        return None
    search_host = _host(str(getattr(browser, "current_url", "") or ""))
    tried: list[dict[str, Any]] = []
    for cand in candidates:
        href = str(cand.get("href") or "").strip()
        title = str(cand.get("text") or "").strip()
        if not href:
            continue
        if is_ad_redirect_url(href):
            tried.append({"href": href, "title": title, "skipped": "ad_redirect_href"})
            continue
        try:
            await page.goto(href, wait_until="domcontentloaded", timeout=30000)
            try:
                await browser._wait_for_page_stable()
            except Exception:
                pass
        except Exception as exc:
            tried.append({"href": href, "title": title, "skipped": f"nav_error:{exc}"[:120]})
            continue
        landing = str(getattr(browser, "current_url", "") or "").strip() or href
        landing_host = _host(landing)
        if (
            is_ad_redirect_url(landing)
            or not landing_host
            or (search_host and landing_host == search_host)
        ):
            tried.append(
                {"href": href, "title": title, "landing": landing, "skipped": "ad_or_search_host"}
            )
            continue
        logger.info(
            "[SEARCH NAV] opened first clean organic result: %s (%s) after %d skip(s)",
            landing, title[:60], len(tried),
        )
        return {
            "url": landing,
            "href": href,
            "title": title,
            "query": q,
            "candidates": len(candidates),
            "skipped": tried,
            "results": results,
        }
    logger.info("[SEARCH NAV] all %d organic candidates were ads / dead ends", len(candidates))
    return {
        "url": "",
        "href": "",
        "title": "",
        "query": q,
        "candidates": len(candidates),
        "skipped": tried,
        "results": results,
        "failed": True,
    }


async def apply_search_result_open_guard(
    browser: Any,
    decisions: list[dict[str, Any]],
    *,
    goal: str,
) -> bool:
    """Rewrite a fuzzy first-result click into ``click_new_tab`` on the organic link."""
    if not decisions:
        return False
    head = decisions[0]
    action = str(head.get("action") or "")
    if action in _TAB_META_ACTIONS:
        return False
    if _has_non_current_open_tab(browser) and not _decision_requests_first_result(head):
        return False
    expected_query = parse_first_search_query(goal)
    if not expected_query:
        return False
    try:
        current_url = str(getattr(browser, "current_url", "") or "")
    except Exception:
        current_url = ""
    current_query = _query_from_url(current_url)
    if not _queries_match(current_query, expected_query):
        return False
    if not goal_requests_first_result_new_tab(goal, head):
        return False

    result = await _probe_first_organic_result(browser, expected_query)
    if not result:
        return False
    try:
        target_id = int(result.get("target_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    if target_id <= 0:
        logger.info("[SEARCH RESULT] first organic result found but has no SoM id: %s", result)
        return False

    text = str(result.get("text") or "").strip()
    href = str(result.get("href") or "").strip()
    logger.info(
        "[SEARCH RESULT] rewriting %s#%s -> click_new_tab#%s (%s)",
        action,
        head.get("target_id"),
        target_id,
        href[:100],
    )
    decisions[:] = [
        {
            **head,
            "action": "click_new_tab",
            "target_id": target_id,
            "type_value": "",
            "thought": (
                "[SEARCH RESULT GUARD] User asked to open the first search result in a new tab; "
                "selected the first relevant main-result link by DOM structure/query relevance "
                f"(query={expected_query!r}, text={text!r}, href={href!r})."
            ),
        }
    ]
    return True

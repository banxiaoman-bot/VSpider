"""URL Seeder: discover crawl seed URLs from ``sitemap.xml`` / sitemap-index /
``robots.txt``.

Borrowed (re-implemented, no external deps) from crawl4ai's ``AsyncUrlSeeder``.
The parsing is pure stdlib (``html.parser``, mirroring ``spider_lite`` and
``extraction_engine/generic.py``) and the discovery walk takes an injected
``fetcher`` identical in shape to ``spider_lite.Fetcher``, so the whole module
is stub-testable with no network / browser.

Seeds feed ``spider_lite`` ``start_urls`` and, with ``crawl_strategy=best_first``
(CRAWL-BF1/BF2), the relevance frontier — letting a run start from a site's own
URL inventory instead of crawling blindly from one page (mission §一 "高效 /
通用 / 最少回合").

Public surface:

    parse_sitemap_locs(xml_text) -> list[str]
    is_sitemap_index(xml_text) -> bool
    sitemap_urls_from_robots(robots_text) -> list[str]
    UrlSeeder(fetcher).seed_from_sitemap(url, *, max_urls, max_sitemaps,
                                         allowed_domains, keywords) -> list[str]
    UrlSeeder(fetcher).seed_from_robots(url, **kw) -> list[str]
"""

from __future__ import annotations

import re
from collections import deque
from html.parser import HTMLParser
from typing import Any, Callable, Iterable
from urllib.parse import urljoin

from visual_web_agent.crawl_frontier import normalize_keywords, score_url
from visual_web_agent.spider_lite import domain_of, normalize_url

__all__ = [
    "parse_sitemap_locs",
    "is_sitemap_index",
    "sitemap_urls_from_robots",
    "UrlSeeder",
]

Fetcher = Callable[[str], Any]

_INDEX_RE = re.compile(r"<\s*sitemapindex", re.IGNORECASE)
_ROBOTS_SITEMAP_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


class _LocParser(HTMLParser):
    """Capture the text of every ``<loc>`` element (urlset *and* sitemapindex).

    Whitespace is trimmed on flush; a dangling unclosed ``<loc>`` is flushed by
    :meth:`close`, so callers must ``feed`` then ``close``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.locs: list[str] = []
        self._in_loc = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if str(tag or "").lower() == "loc":
            self._in_loc = True
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_loc and data:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if str(tag or "").lower() == "loc" and self._in_loc:
            self._flush()

    def _flush(self) -> None:
        if self._in_loc:
            self.locs.append("".join(self._buf).strip())
        self._in_loc = False
        self._buf = []

    def close(self) -> None:
        super().close()
        self._flush()


def parse_sitemap_locs(xml_text: str) -> list[str]:
    """Return every ``<loc>`` URL in document order (empty entries dropped)."""
    parser = _LocParser()
    parser.feed(str(xml_text or ""))
    parser.close()
    return [loc for loc in parser.locs if loc]


def is_sitemap_index(xml_text: str) -> bool:
    """True when the document is a ``<sitemapindex>`` (locs are child sitemaps)."""
    return bool(_INDEX_RE.search(str(xml_text or "")))


def sitemap_urls_from_robots(robots_text: str) -> list[str]:
    """Extract ``Sitemap:`` directive URLs from a ``robots.txt`` (deduped)."""
    out: list[str] = []
    seen: set[str] = set()
    for match in _ROBOTS_SITEMAP_RE.finditer(str(robots_text or "")):
        url = match.group(1).strip()
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _response_text(fetched: Any) -> str:
    """Coerce a ``Fetcher`` return (FetchResult / dict / str) into body text."""
    if fetched is None:
        return ""
    if isinstance(fetched, str):
        return fetched
    if isinstance(fetched, dict):
        return str(fetched.get("html") or fetched.get("text") or fetched.get("body") or "")
    return str(getattr(fetched, "html", "") or "")


def _normalize_head(resp: Any) -> tuple[int, str]:
    """Coerce a ``head_fetcher`` return into ``(status_code, content_type)``."""
    if resp is None:
        return 0, ""
    if isinstance(resp, dict):
        status = resp.get("status_code") or resp.get("status") or 0
        headers = resp.get("headers") if isinstance(resp.get("headers"), dict) else {}
        content_type = (
            resp.get("content_type")
            or headers.get("content-type")
            or headers.get("Content-Type")
            or ""
        )
    else:
        status = getattr(resp, "status_code", 0) or 0
        content_type = getattr(resp, "content_type", "") or ""
        if not content_type:
            headers = getattr(resp, "headers", {}) or {}
            if isinstance(headers, dict):
                content_type = headers.get("content-type") or headers.get("Content-Type") or ""
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 0
    return status, str(content_type or "")


class UrlSeeder:
    """Discover seed URLs by walking sitemaps with an injected ``fetcher``.

    An optional ``head_fetcher`` (HEAD-style: returns status + content-type, no
    body) enables :meth:`probe_url` liveness checks and ``live_only`` filtering.
    """

    def __init__(self, fetcher: Fetcher, *, head_fetcher: Fetcher | None = None) -> None:
        self.fetcher = fetcher
        self.head_fetcher = head_fetcher

    def _fetch_text(self, url: str) -> str:
        try:
            return _response_text(self.fetcher(url))
        except Exception:
            return ""

    def probe_url(self, url: str) -> dict[str, Any]:
        """HEAD-probe ``url`` → ``{url, status_code, content_type, live}``.

        ``live`` is ``200 <= status < 400``. With no ``head_fetcher`` (or on any
        error) the URL is reported not-live with status ``0``.
        """
        out: dict[str, Any] = {"url": str(url), "status_code": 0, "content_type": "", "live": False}
        if not self.head_fetcher:
            return out
        try:
            resp = self.head_fetcher(url)
        except Exception:
            return out
        status, content_type = _normalize_head(resp)
        out["status_code"] = status
        out["content_type"] = content_type
        out["live"] = 200 <= status < 400
        return out

    def seed_from_sitemap(
        self,
        sitemap_url: str,
        *,
        max_urls: int = 1000,
        max_sitemaps: int = 50,
        allowed_domains: Iterable[str] | None = None,
        keywords: object | None = None,
        live_only: bool = False,
    ) -> list[str]:
        """BFS over ``sitemap_url`` (recursing into sitemap indexes) → page URLs.

        ``allowed_domains`` keeps only seeds on those hosts; ``keywords`` keeps
        only seeds whose URL scores > 0 (``crawl_frontier.score_url``). Result is
        deduped, in document order, capped at ``max_urls``. ``live_only`` drops
        URLs that fail a :meth:`probe_url` HEAD check (no-op without a
        ``head_fetcher``).
        """
        allow = {domain_of(str(d)) for d in (allowed_domains or [])}
        allow.discard("")
        kw = normalize_keywords(keywords) if keywords else []

        start = normalize_url(sitemap_url) or str(sitemap_url or "")
        queue: deque[str] = deque([start] if start else [])
        seen_sitemaps: set[str] = set()
        pages: list[str] = []
        seen_pages: set[str] = set()

        while queue and len(pages) < max_urls and len(seen_sitemaps) < max_sitemaps:
            current = queue.popleft()
            if not current or current in seen_sitemaps:
                continue
            seen_sitemaps.add(current)
            text = self._fetch_text(current)
            if not text:
                continue
            locs = parse_sitemap_locs(text)
            if is_sitemap_index(text):
                for loc in locs:
                    child = normalize_url(urljoin(current, loc))
                    if child and child not in seen_sitemaps:
                        queue.append(child)
                continue
            for loc in locs:
                page = normalize_url(urljoin(current, loc))
                if not page or page in seen_pages:
                    continue
                if allow and domain_of(page) not in allow:
                    continue
                if kw and score_url(page, kw) <= 0:
                    continue
                seen_pages.add(page)
                pages.append(page)
                if len(pages) >= max_urls:
                    break
        if live_only and self.head_fetcher:
            pages = [p for p in pages if self.probe_url(p)["live"]]
        return pages

    def seed_from_robots(
        self,
        robots_url: str,
        *,
        max_urls: int = 1000,
        max_sitemaps: int = 50,
        allowed_domains: Iterable[str] | None = None,
        keywords: object | None = None,
        live_only: bool = False,
    ) -> list[str]:
        """Read ``robots.txt`` ``Sitemap:`` directives, then seed from each."""
        text = self._fetch_text(robots_url)
        pages: list[str] = []
        seen: set[str] = set()
        for sitemap_url in sitemap_urls_from_robots(text):
            for page in self.seed_from_sitemap(
                urljoin(robots_url, sitemap_url),
                max_urls=max_urls,
                max_sitemaps=max_sitemaps,
                allowed_domains=allowed_domains,
                keywords=keywords,
                live_only=live_only,
            ):
                if page not in seen:
                    seen.add(page)
                    pages.append(page)
                    if len(pages) >= max_urls:
                        return pages
        return pages

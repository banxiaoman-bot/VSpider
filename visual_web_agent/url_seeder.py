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
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from typing import Any, Callable, Iterable
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from visual_web_agent.crawl_frontier import normalize_keywords, score_url
from visual_web_agent.spider_lite import domain_of, normalize_url
from visual_web_agent.url_guard import UrlGuardError, check_url

__all__ = [
    "parse_sitemap_locs",
    "is_sitemap_index",
    "sitemap_urls_from_robots",
    "default_head_fetch",
    "content_type_to_output_kind",
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


_HEAD_REJECTED_STATUSES = (405, 501)

# Upper bound for parallel HEAD probing (SEED-PARALLEL). Probes are cheap I/O so
# this is higher than the agent-run cap in ``concurrency_config`` (16), but still
# bounded so a large seed set can't spawn an unbounded thread pool.
_MAX_PROBE_CONCURRENCY = 32

# Default parallelism for batch HEAD probing when a caller doesn't specify one.
_DEFAULT_PROBE_CONCURRENCY = 8


def _urllib_probe(
    url: str,
    *,
    method: str,
    timeout: float,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Issue ``method`` to ``url`` and read status + content-type (body unread)."""
    try:
        check_url(url)  # SSRF guard: a blocked host probes as not-live (status 0)
    except UrlGuardError:
        return {"status_code": 0, "content_type": ""}
    headers = {"User-Agent": "VSpider-UrlSeeder/1.0"}
    if extra_headers:
        headers.update(extra_headers)
    req = Request(str(url), method=method, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 0) or getattr(resp, "code", 0) or 200)
            resp_headers = getattr(resp, "headers", None)
            content_type = resp_headers.get("content-type", "") if resp_headers else ""
            return {"status_code": status, "content_type": str(content_type or "")}
    except HTTPError as exc:
        content_type = ""
        try:
            if exc.headers:
                content_type = exc.headers.get("content-type", "") or ""
        except Exception:
            content_type = ""
        return {"status_code": int(getattr(exc, "code", 0) or 0), "content_type": str(content_type)}
    except Exception:
        return {"status_code": 0, "content_type": ""}


def default_head_fetch(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Real stdlib HEAD probe → ``{"status_code", "content_type"}``.

    Mirrors :func:`spider_lite.default_fetch` but issues an HTTP ``HEAD`` so a
    URL's liveness + content-type are checked without downloading the body
    (mission §一 "高效 — 能不重抓就不重抓"). HTTP error responses (404/500…)
    keep their real status via :class:`urllib.error.HTTPError`; network / DNS
    failures collapse to status ``0`` so :meth:`UrlSeeder.probe_url` treats them
    as not-live.

    SEED-NEXT: servers that reject ``HEAD`` (405 / 501) are retried with a
    ``Range: bytes=0-0`` ``GET`` so liveness + content-type are still resolved
    without downloading the body. Pass as
    ``UrlSeeder(fetcher, head_fetcher=default_head_fetch)``.
    """
    result = _urllib_probe(url, method="HEAD", timeout=timeout)
    if int(result.get("status_code") or 0) in _HEAD_REJECTED_STATUSES:
        return _urllib_probe(
            url, method="GET", timeout=timeout, extra_headers={"Range": "bytes=0-0"}
        )
    return result


_CONTENT_TYPE_OUTPUT_KIND = {
    "application/pdf": "media_pdf",
    "application/zip": "media_archive",
    "application/x-zip-compressed": "media_archive",
    "application/x-7z-compressed": "media_archive",
    "application/x-rar-compressed": "media_archive",
    "application/gzip": "media_archive",
    "application/x-tar": "media_archive",
    "text/csv": "dataset_rows",
    "application/csv": "dataset_rows",
    "application/vnd.ms-excel": "dataset_rows",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "dataset_rows",
    "application/json": "dataset_records",
    "application/jsonl": "dataset_records",
    "application/x-ndjson": "dataset_records",
    "text/html": "html_snapshot",
    "application/xhtml+xml": "html_snapshot",
}


def content_type_to_output_kind(content_type: Any) -> str:
    """Map a response ``Content-Type`` to an ``output_contract`` output_kind.

    Pure + tolerant (strips ``;charset=…`` params, lowercases). ``image/* ->
    media_image``, ``video/* -> media_video``, ``audio/* -> media_audio``; known
    document / data MIMEs map per :data:`_CONTENT_TYPE_OUTPUT_KIND`; other
    ``text/* -> code_or_text``; everything else -> ``file_generic``; empty -> "".
    """
    ct = str(content_type or "").split(";", 1)[0].strip().lower()
    if not ct:
        return ""
    main = ct.split("/", 1)[0]
    if main == "image":
        return "media_image"
    if main == "video":
        return "media_video"
    if main == "audio":
        return "media_audio"
    mapped = _CONTENT_TYPE_OUTPUT_KIND.get(ct)
    if mapped:
        return mapped
    if main == "text":
        return "code_or_text"
    return "file_generic"


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
        """HEAD-probe ``url`` → ``{url, status_code, content_type, live, output_kind}``.

        ``live`` is ``200 <= status < 400``; ``output_kind`` is inferred from the
        content-type (output_contract.v1). With no ``head_fetcher`` (or on any
        error) the URL is reported not-live with status ``0`` and empty kind.
        """
        out: dict[str, Any] = {
            "url": str(url), "status_code": 0, "content_type": "", "live": False, "output_kind": "",
        }
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
        out["output_kind"] = content_type_to_output_kind(content_type)
        return out

    def probe_urls(
        self,
        urls: Iterable[str],
        *,
        concurrency: int = _DEFAULT_PROBE_CONCURRENCY,
    ) -> list[dict[str, Any]]:
        """Probe many URLs via :meth:`probe_url`, returning metadata in input order.

        With ``concurrency <= 1`` the URLs are probed serially; otherwise up to
        ``concurrency`` HEAD probes (capped at :data:`_MAX_PROBE_CONCURRENCY` and
        the URL count) run on a thread pool. Output order always matches input
        order regardless of completion order, and a per-URL failure is swallowed
        (that URL reported not-live) so one dead probe never aborts the batch
        (mission §一 "高效 / 准确").
        """
        items = [str(u) for u in urls]
        if not items:
            return []
        workers = min(int(concurrency or 0), _MAX_PROBE_CONCURRENCY, len(items))
        if workers <= 1:
            return [self.probe_url(u) for u in items]
        results: list[dict[str, Any]] = [
            {"url": u, "status_code": 0, "content_type": "", "live": False, "output_kind": ""}
            for u in items
        ]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_idx = {pool.submit(self.probe_url, u): i for i, u in enumerate(items)}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception:
                    pass  # keep the pre-seeded not-live placeholder
        return results

    def seed_from_sitemap(
        self,
        sitemap_url: str,
        *,
        max_urls: int = 1000,
        max_sitemaps: int = 50,
        allowed_domains: Iterable[str] | None = None,
        keywords: object | None = None,
        live_only: bool = False,
        probe_concurrency: int = _DEFAULT_PROBE_CONCURRENCY,
    ) -> list[str]:
        """BFS over ``sitemap_url`` (recursing into sitemap indexes) → page URLs.

        ``allowed_domains`` keeps only seeds on those hosts; ``keywords`` keeps
        only seeds whose URL scores > 0 (``crawl_frontier.score_url``). Result is
        deduped, in document order, capped at ``max_urls``. ``live_only`` drops
        URLs that fail a :meth:`probe_url` HEAD check (no-op without a
        ``head_fetcher``), probed up to ``probe_concurrency`` at a time.
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
            metas = self.probe_urls(pages, concurrency=probe_concurrency)
            pages = [page for page, meta in zip(pages, metas) if meta["live"]]
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
        probe_concurrency: int = _DEFAULT_PROBE_CONCURRENCY,
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
                probe_concurrency=probe_concurrency,
            ):
                if page not in seen:
                    seen.add(page)
                    pages.append(page)
                    if len(pages) >= max_urls:
                        return pages
        return pages

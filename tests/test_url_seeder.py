"""Slice CRAWL-SEED1: URL Seeder (sitemap / sitemap-index / robots discovery).

Borrowed from crawl4ai's AsyncUrlSeeder, re-implemented with stdlib only and a
fetcher-injected discovery walk so the whole module is stub-testable (no
network). Pins:

1. pure parsers: ``parse_sitemap_locs`` / ``is_sitemap_index`` /
   ``sitemap_urls_from_robots``;
2. ``UrlSeeder`` discovery: flat urlset, sitemap-index recursion, max_urls cap,
   allowed-domain filter, keyword relevance filter, robots.txt entrypoint;
3. ``spider_lite`` opt-in wiring: ``seed_sitemap`` payload merges seeds into
   start_urls + allowed_domains; default (no ``seed_sitemap``) is unchanged and
   still requires ``start_urls``.
"""

from __future__ import annotations

import pytest

from visual_web_agent.spider_lite import FetchResult, SpiderLiteManager
from visual_web_agent.url_seeder import (
    UrlSeeder,
    is_sitemap_index,
    parse_sitemap_locs,
    sitemap_urls_from_robots,
)


URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc> https://example.com/python-guide </loc></url>
  <url><loc>https://other.com/x</loc></url>
</urlset>
"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-a.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap-b.xml</loc></sitemap>
</sitemapindex>
"""

CHILD_A = """<urlset><url><loc>https://example.com/a1</loc></url></urlset>"""
CHILD_B = """<urlset><url><loc>https://example.com/b1</loc></url></urlset>"""

ROBOTS = """User-agent: *
Disallow: /private
Sitemap: https://example.com/sitemap.xml
sitemap: https://example.com/news.xml
"""


# --- pure parsers ----------------------------------------------------------

def test_parse_sitemap_locs_urlset_trims_whitespace() -> None:
    assert parse_sitemap_locs(URLSET) == [
        "https://example.com/a",
        "https://example.com/python-guide",
        "https://other.com/x",
    ]


def test_parse_sitemap_locs_ignores_non_loc_tags() -> None:
    xml = '<urlset><url><loc>https://e.com/a</loc><lastmod>2020</lastmod></url></urlset>'
    assert parse_sitemap_locs(xml) == ["https://e.com/a"]


def test_is_sitemap_index() -> None:
    assert is_sitemap_index(INDEX) is True
    assert is_sitemap_index(URLSET) is False
    assert is_sitemap_index("") is False


def test_sitemap_urls_from_robots_case_insensitive_dedup() -> None:
    assert sitemap_urls_from_robots(ROBOTS) == [
        "https://example.com/sitemap.xml",
        "https://example.com/news.xml",
    ]


# --- UrlSeeder discovery ---------------------------------------------------

def _seeder(mapping: dict[str, str]) -> UrlSeeder:
    def fetch(url: str) -> FetchResult:
        return FetchResult(url=url, status_code=200, html=mapping.get(url, ""))

    return UrlSeeder(fetch)


def test_seed_from_sitemap_flat() -> None:
    seeder = _seeder({"https://example.com/sitemap.xml": URLSET})
    seeds = seeder.seed_from_sitemap("https://example.com/sitemap.xml")
    assert seeds == [
        "https://example.com/a",
        "https://example.com/python-guide",
        "https://other.com/x",
    ]


def test_seed_from_sitemap_index_recurses() -> None:
    seeder = _seeder({
        "https://example.com/sitemap.xml": INDEX,
        "https://example.com/sitemap-a.xml": CHILD_A,
        "https://example.com/sitemap-b.xml": CHILD_B,
    })
    seeds = seeder.seed_from_sitemap("https://example.com/sitemap.xml")
    assert seeds == ["https://example.com/a1", "https://example.com/b1"]


def test_seed_from_sitemap_max_urls_cap() -> None:
    seeder = _seeder({"https://example.com/sitemap.xml": URLSET})
    seeds = seeder.seed_from_sitemap("https://example.com/sitemap.xml", max_urls=2)
    assert seeds == ["https://example.com/a", "https://example.com/python-guide"]


def test_seed_from_sitemap_allowed_domains_filter() -> None:
    seeder = _seeder({"https://example.com/sitemap.xml": URLSET})
    seeds = seeder.seed_from_sitemap(
        "https://example.com/sitemap.xml", allowed_domains=["example.com"]
    )
    assert seeds == ["https://example.com/a", "https://example.com/python-guide"]


def test_seed_from_sitemap_keywords_filter() -> None:
    seeder = _seeder({"https://example.com/sitemap.xml": URLSET})
    seeds = seeder.seed_from_sitemap(
        "https://example.com/sitemap.xml", keywords=["python"]
    )
    assert seeds == ["https://example.com/python-guide"]


def test_seed_from_robots() -> None:
    seeder = _seeder({
        "https://example.com/robots.txt": ROBOTS,
        "https://example.com/sitemap.xml": URLSET,
        "https://example.com/news.xml": "<urlset><url><loc>https://example.com/n1</loc></url></urlset>",
    })
    seeds = seeder.seed_from_robots("https://example.com/robots.txt")
    assert "https://example.com/a" in seeds
    assert "https://example.com/n1" in seeds


# --- spider_lite opt-in wiring ---------------------------------------------

SPIDER_SITE = {
    "https://example.com/sitemap.xml": (
        "<urlset>"
        "<url><loc>https://example.com/p1</loc></url>"
        "<url><loc>https://example.com/p2</loc></url>"
        "</urlset>"
    ),
    "https://example.com/p1": "<html><body><p>page one</p></body></html>",
    "https://example.com/p2": "<html><body><p>page two</p></body></html>",
}


def _spider_fetch(url: str) -> FetchResult:
    return FetchResult(url=url, status_code=200, html=SPIDER_SITE[url])


def test_spider_run_seeds_from_sitemap_without_start_urls() -> None:
    manager = SpiderLiteManager(fetcher=_spider_fetch)
    result = manager.run({
        "run_id": "seed_run",
        "seed_sitemap": "https://example.com/sitemap.xml",
        "max_pages": 2,
        "follow_links": False,
    })
    assert result["status"] == "success"
    visited = {page["url"] for page in result["pages"]}
    assert visited == {"https://example.com/p1", "https://example.com/p2"}
    assert "example.com" in result["allowed_domains"]


def test_config_still_requires_start_urls_when_no_seed() -> None:
    manager = SpiderLiteManager(fetcher=_spider_fetch)
    with pytest.raises(ValueError):
        manager.run({"run_id": "no_starts", "follow_links": False})

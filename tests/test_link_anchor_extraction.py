"""Slice CRAWL-BF2: anchor-text-enriched link extraction.

``crawl_frontier.score_url`` already weights anchor text at 0.5, but the legacy
``spider_lite`` link extraction threw the anchor text away (it only kept the
``href``), so the best-first frontier never actually saw that signal. These
tests pin:

1. the new pure ``extract_links_with_anchors`` (href + collapsed anchor text),
2. ``extract_links`` staying byte-identical (list[str], dedup, document order),
3. the integration win: a page whose *URL* has no keyword but whose *anchor
   text* does is crawled first under ``best_first`` — proving the anchor signal
   is now wired through ``run()``.

Same deterministic stub-fetcher style as ``test_spider_lite_best_first.py``;
no network, no browser.
"""

from __future__ import annotations

from visual_web_agent.spider_lite import (
    FetchResult,
    SpiderLiteManager,
    extract_links,
    extract_links_with_anchors,
)


# --- pure extraction -------------------------------------------------------

def test_extract_links_backward_compatible() -> None:
    html = '<a href="/a#frag">A</a><a href="https://example.com/b">B</a>'
    assert extract_links(html, "https://example.com/root") == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_extract_links_with_anchors_captures_text() -> None:
    html = '<a href="/a">Alpha</a><a href="/b">Beta</a>'
    assert extract_links_with_anchors(html, "https://example.com/root") == [
        ("https://example.com/a", "Alpha"),
        ("https://example.com/b", "Beta"),
    ]


def test_extract_links_with_anchors_collapses_whitespace_and_nested_tags() -> None:
    html = '<a href="/p">  Python   <b>Deep</b>\n  Guide </a>'
    assert extract_links_with_anchors(html, "https://example.com/") == [
        ("https://example.com/p", "Python Deep Guide"),
    ]


def test_extract_links_with_anchors_empty_anchor() -> None:
    html = '<a href="/p"></a>'
    assert extract_links_with_anchors(html, "https://example.com/") == [
        ("https://example.com/p", ""),
    ]


def test_extract_links_with_anchors_dedup_keeps_first_anchor() -> None:
    html = '<a href="/p">first</a><a href="/p">second</a>'
    assert extract_links_with_anchors(html, "https://example.com/") == [
        ("https://example.com/p", "first"),
    ]


def test_extract_links_with_anchors_captures_unclosed_trailing_anchor() -> None:
    # No closing </a>; feed() alone would never flush it without close().
    html = '<a href="/x">dangling text'
    assert extract_links_with_anchors(html, "https://example.com/") == [
        ("https://example.com/x", "dangling text"),
    ]


# --- integration: anchor signal reorders the best-first frontier ------------

HUB = "https://example.com/"
ALPHA = "https://example.com/alpha"  # URL has NO keyword token
BETA = "https://example.com/beta"    # URL has NO keyword token

# keyword "python" appears ONLY in BETA's anchor text, never in any URL path.
PAGES = {
    HUB: """
    <html><body>
      <a href="/alpha">boring filler link</a>
      <a href="/beta">python tutorial</a>
    </body></html>
    """,
    ALPHA: "<html><body><p>unrelated</p></body></html>",
    BETA: "<html><body><p>on topic body</p></body></html>",
}


def _fake_fetch(url: str) -> FetchResult:
    return FetchResult(url=url, status_code=200, html=PAGES[url])


def _payload(**overrides) -> dict:
    payload = {
        "start_urls": [HUB],
        "max_depth": 1,
        "max_pages": 2,
        "follow_links": True,
    }
    payload.update(overrides)
    return payload


def test_best_first_uses_anchor_text_signal() -> None:
    manager = SpiderLiteManager(fetcher=_fake_fetch)
    result = manager.run(
        _payload(run_id="bf_anchor", crawl_strategy="best_first", keywords=["python"])
    )
    assert result["status"] == "success"
    # URL paths carry no keyword; only BETA's anchor does. With the anchor
    # signal wired in, BETA is popped before the document-order-earlier ALPHA.
    assert result["pages"][1]["url"] == BETA


def test_default_bfs_ignores_anchor_and_keeps_document_order() -> None:
    manager = SpiderLiteManager(fetcher=_fake_fetch)
    result = manager.run(_payload(run_id="bfs_anchor"))
    assert result["config"]["crawl_strategy"] == "bfs"
    # BFS is FIFO -> document order -> ALPHA (first in HTML) is visited first.
    assert result["pages"][1]["url"] == ALPHA

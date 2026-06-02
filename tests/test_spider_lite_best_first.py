"""Integration regression: opt-in best-first crawl strategy in ``spider_lite``.

Uses the same deterministic stub-fetcher style as ``test_spider_lite.py`` (a
``PAGES`` dict + ``fake_fetch``; no network). The hub links to an *irrelevant*
page first and a *relevant* page second, so:

- default BFS visits them in document order (irrelevant first), proving the
  legacy ordering is untouched;
- ``crawl_strategy="best_first"`` with keywords visits the relevant page first,
  proving the frontier reorders toward on-topic pages in fewer rounds.
"""

from __future__ import annotations

from visual_web_agent.spider_lite import FetchResult, SpiderLiteManager


HUB = "https://example.com/"
RANDOM = "https://example.com/zzz-random"
RELEVANT = "https://example.com/python-guide"

PAGES = {
    HUB: """
    <html><body>
      <a href="/zzz-random">Random stuff</a>
      <a href="/python-guide">Py</a>
    </body></html>
    """,
    RANDOM: "<html><body><p>unrelated</p></body></html>",
    RELEVANT: "<html><body><p>python tutorial body</p></body></html>",
}


def fake_fetch(url: str) -> FetchResult:
    return FetchResult(url=url, status_code=200, html=PAGES[url])


def _base_payload(**overrides) -> dict:
    payload = {
        "start_urls": [HUB],
        "max_depth": 1,
        "max_pages": 2,
        "follow_links": True,
    }
    payload.update(overrides)
    return payload


def test_default_bfs_visits_in_document_order() -> None:
    manager = SpiderLiteManager(fetcher=fake_fetch)
    result = manager.run(_base_payload(run_id="bfs_default"))
    assert result["status"] == "success"
    assert result["config"]["crawl_strategy"] == "bfs"
    # BFS: links followed in document order -> the first (random) page is fetched
    assert result["pages"][1]["url"] == RANDOM


def test_best_first_visits_relevant_page_first() -> None:
    manager = SpiderLiteManager(fetcher=fake_fetch)
    result = manager.run(
        _base_payload(run_id="bf_run", crawl_strategy="best_first", keywords=["python"])
    )
    assert result["status"] == "success"
    assert result["config"]["crawl_strategy"] == "best_first"
    assert result["config"]["keywords"] == ["python"]
    # best-first: relevant page is popped before the document-order-earlier one
    assert result["pages"][1]["url"] == RELEVANT


def test_best_first_full_order_when_budget_allows() -> None:
    manager = SpiderLiteManager(fetcher=fake_fetch)
    result = manager.run(
        _base_payload(
            run_id="bf_full", max_pages=3, crawl_strategy="best_first", keywords="python guide"
        )
    )
    visited = [page["url"] for page in result["pages"]]
    assert visited == [HUB, RELEVANT, RANDOM]


def test_best_first_relevance_query_alias() -> None:
    manager = SpiderLiteManager(fetcher=fake_fetch)
    result = manager.run(
        _base_payload(run_id="bf_alias", crawl_strategy="best_first", relevance_query="Python")
    )
    assert result["config"]["keywords"] == ["python"]
    assert result["pages"][1]["url"] == RELEVANT


def test_unknown_strategy_falls_back_to_bfs() -> None:
    manager = SpiderLiteManager(fetcher=fake_fetch)
    result = manager.run(_base_payload(run_id="bf_unknown", crawl_strategy="nonsense"))
    assert result["config"]["crawl_strategy"] == "bfs"
    assert result["pages"][1]["url"] == RANDOM

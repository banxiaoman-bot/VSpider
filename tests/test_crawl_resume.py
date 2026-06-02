"""Slice CRAWL-RESUME1: resumable deep-crawl checkpointing (opt-in).

Borrowed from crawl4ai's resumable deep crawl. A run with ``resume_state_path``
periodically persists ``{seen, pending frontier, pages, items}`` to an atomic
JSON checkpoint; a later run with the same path reloads it, skips already-seen
URLs, and continues from the saved frontier instead of re-fetching. Default
(no ``resume_state_path``) is byte-identical.

Deterministic stub-fetcher style (counting fetcher proves no re-fetch); no
network.
"""

from __future__ import annotations

import json
from pathlib import Path

from visual_web_agent.crawl_checkpoint import load_checkpoint, save_checkpoint
from visual_web_agent.crawl_frontier import build_frontier
from visual_web_agent.spider_lite import FetchResult, SpiderLiteManager


# --- checkpoint module -----------------------------------------------------

def test_save_then_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = {"seen": ["https://e.com/a"], "pending": [{"url": "https://e.com/b", "depth": 1}]}
    save_checkpoint(str(path), state)
    assert path.exists()
    assert load_checkpoint(str(path)) == state


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert load_checkpoint(str(tmp_path / "nope.json")) is None


def test_load_corrupt_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{ not json", encoding="utf-8")
    assert load_checkpoint(str(path)) is None


# --- frontier snapshot / restore ------------------------------------------

def test_bfs_frontier_snapshot_and_restore_order() -> None:
    frontier = build_frontier("bfs", seeds=["https://e.com/1", "https://e.com/2"])
    snap = frontier.snapshot()
    assert snap == [
        {"url": "https://e.com/1", "depth": 0},
        {"url": "https://e.com/2", "depth": 0},
    ]
    restored = build_frontier("bfs", pending=snap)
    assert restored.pop() == ("https://e.com/1", 0)
    assert restored.pop() == ("https://e.com/2", 0)


def test_best_first_frontier_snapshot_and_restore_rescores() -> None:
    frontier = build_frontier(
        "best_first",
        keywords=["python"],
        seeds=["https://e.com/other", "https://e.com/python-guide"],
    )
    snap = frontier.snapshot()
    assert {item["url"] for item in snap} == {
        "https://e.com/other",
        "https://e.com/python-guide",
    }
    restored = build_frontier("best_first", keywords=["python"], pending=snap)
    # re-scored by URL keyword -> the python page is popped first
    assert restored.pop()[0] == "https://e.com/python-guide"


def test_best_first_snapshot_preserves_anchor_signal() -> None:
    # keyword only in the ANCHOR text, never in the URL path.
    frontier = build_frontier("best_first", keywords=["python"])
    frontier.push("https://e.com/aaa", 1, anchor_text="boring filler")
    frontier.push("https://e.com/bbb", 1, anchor_text="python tutorial")
    snap = frontier.snapshot()
    # snapshot carries the anchor text so a resume can re-apply the 0.5 weight
    assert any(item.get("anchor_text") == "python tutorial" for item in snap)
    restored = build_frontier("best_first", keywords=["python"], pending=snap)
    # anchor-scored page (bbb) is popped before the URL-only zero-score page
    assert restored.pop()[0] == "https://e.com/bbb"


# --- spider_lite opt-in wiring ---------------------------------------------

HUB = "https://example.com/"
PAGE_A = "https://example.com/a"
PAGE_B = "https://example.com/b"

SITE = {
    HUB: f'<html><body><a href="{PAGE_A}">A</a><a href="{PAGE_B}">B</a></body></html>',
    PAGE_A: "<html><body><p>leaf a</p></body></html>",
    PAGE_B: "<html><body><p>leaf b</p></body></html>",
}


class _CountingFetcher:
    def __init__(self) -> None:
        self.fetched: list[str] = []

    def __call__(self, url: str) -> FetchResult:
        self.fetched.append(url)
        return FetchResult(url=url, status_code=200, html=SITE[url])


def test_run_writes_checkpoint(tmp_path: Path) -> None:
    state_path = tmp_path / "crawl.json"
    manager = SpiderLiteManager(fetcher=_CountingFetcher())
    result = manager.run({
        "run_id": "cp_write",
        "start_urls": [HUB],
        "max_depth": 1,
        "max_pages": 3,
        "follow_links": True,
        "resume_state_path": str(state_path),
    })
    assert result["status"] == "success"
    assert state_path.exists()
    saved = load_checkpoint(str(state_path))
    assert HUB in set(saved["seen"])


def test_resume_skips_seen_and_continues_from_pending(tmp_path: Path) -> None:
    state_path = tmp_path / "crawl.json"
    # Pretend a prior run already fetched HUB and queued A + B.
    save_checkpoint(str(state_path), {
        "run_id": "prior",
        "strategy": "bfs",
        "keywords": [],
        "seen": [HUB],
        "pending": [{"url": PAGE_A, "depth": 1}, {"url": PAGE_B, "depth": 1}],
        "pages": [{"url": HUB, "final_url": HUB, "status_code": 200, "depth": 0}],
        "items": [],
        "errors": [],
    })
    fetcher = _CountingFetcher()
    manager = SpiderLiteManager(fetcher=fetcher)
    result = manager.run({
        "run_id": "cp_resume",
        "start_urls": [HUB],
        "max_depth": 1,
        "max_pages": 5,
        "follow_links": False,
        "resume_state_path": str(state_path),
    })
    assert result.get("resumed") is True
    visited = {page["url"] for page in result["pages"]}
    assert visited == {HUB, PAGE_A, PAGE_B}
    # HUB was already seen -> never re-fetched; only the pending leaves are.
    assert HUB not in fetcher.fetched
    assert set(fetcher.fetched) == {PAGE_A, PAGE_B}


def test_default_run_writes_no_checkpoint_and_has_no_resumed_flag(tmp_path: Path) -> None:
    manager = SpiderLiteManager(fetcher=_CountingFetcher())
    result = manager.run({
        "run_id": "no_cp",
        "start_urls": [HUB],
        "max_depth": 0,
        "max_pages": 1,
        "follow_links": False,
    })
    assert result["status"] == "success"
    assert "resumed" not in result
    assert list(tmp_path.iterdir()) == []

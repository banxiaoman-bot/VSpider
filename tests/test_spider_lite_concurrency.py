"""S14: spider_lite concurrent fetching + cache interplay.

Concurrency is opt-in (``concurrency`` payload key, default 1 = serial path
unchanged). Only network fetches run on the thread pool; cache lookups,
stores, extraction, link expansion and checkpoints stay on the main thread.
"""
from __future__ import annotations

import shutil
import threading
import time
import uuid
from pathlib import Path

import pytest

from visual_web_agent.spider_lite import FetchResult, SpiderLiteManager


PAGES = {
    "https://example.com/": """
    <html><body>
      <a href="/p1">P1</a><a href="/p2">P2</a><a href="/p3">P3</a><a href="/p4">P4</a>
      <div class="quote"><span class="text">root</span></div>
    </body></html>
    """,
    "https://example.com/p1": '<html><body><div class="quote"><span class="text">one</span></div></body></html>',
    "https://example.com/p2": '<html><body><div class="quote"><span class="text">two</span></div></body></html>',
    "https://example.com/p3": '<html><body><div class="quote"><span class="text">three</span></div></body></html>',
    "https://example.com/p4": '<html><body><div class="quote"><span class="text">four</span></div></body></html>',
}


def fake_fetch(url: str) -> FetchResult:
    return FetchResult(url=url, status_code=200, html=PAGES[url])


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_spider_concurrency_tests"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _payload(**extra: object) -> dict:
    base = {
        "start_urls": ["https://example.com/"],
        "max_depth": 1,
        "max_pages": 10,
        "extract": {"selector": ".quote .text::text"},
    }
    base.update(extra)
    return base


def test_concurrent_run_matches_serial_results() -> None:
    serial = SpiderLiteManager(fetcher=fake_fetch).run(_payload(run_id="serial"))
    concurrent = SpiderLiteManager(fetcher=fake_fetch).run(_payload(run_id="conc", concurrency=4))

    assert concurrent["status"] == "success"
    assert concurrent["page_count"] == serial["page_count"] == 5
    assert [i["value"] for i in concurrent["items"]] == [i["value"] for i in serial["items"]]
    assert [p["url"] for p in concurrent["pages"]] == [p["url"] for p in serial["pages"]]


def test_concurrency_actually_overlaps_fetches() -> None:
    lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0

    def slow_fetch(url: str) -> FetchResult:
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return fake_fetch(url)

    result = SpiderLiteManager(fetcher=slow_fetch).run(_payload(run_id="overlap", concurrency=4))

    assert result["page_count"] == 5
    # root page fetches alone (only frontier entry), then 4 children overlap
    assert max_in_flight >= 2
    assert result["fetch_stats"]["concurrency"] == 4
    assert result["fetch_stats"]["network"] == 5


def test_concurrency_respects_max_pages_budget() -> None:
    result = SpiderLiteManager(fetcher=fake_fetch).run(
        _payload(run_id="budget", concurrency=4, max_pages=3)
    )

    assert result["page_count"] == 3


def test_concurrent_record_then_replay_uses_cache(local_tmp_path: Path) -> None:
    SpiderLiteManager(fetcher=fake_fetch).run(_payload(
        run_id="rec",
        concurrency=4,
        cache_mode="record",
        cache_dir=str(local_tmp_path),
        cache_session_id="s14",
    ))

    def fail_fetch(url: str) -> FetchResult:
        raise AssertionError(f"network fetch must not run in replay mode: {url}")

    replay = SpiderLiteManager(fetcher=fail_fetch).run(_payload(
        run_id="rep",
        concurrency=4,
        cache_mode="replay",
        cache_dir=str(local_tmp_path),
        cache_session_id="s14",
    ))

    assert replay["page_count"] == 5
    assert all(p["fetch_source"] == "cache" for p in replay["pages"])
    assert replay["fetch_stats"]["cache"] == 5
    assert replay["fetch_stats"]["network"] == 0


def test_concurrent_batch_error_does_not_kill_run() -> None:
    def flaky_fetch(url: str) -> FetchResult:
        if url.endswith("/p2"):
            raise RuntimeError("boom")
        return fake_fetch(url)

    result = SpiderLiteManager(fetcher=flaky_fetch).run(_payload(run_id="flaky", concurrency=4))

    assert result["status"] == "success"
    assert result["page_count"] == 4
    assert any("boom" in str(e.get("error")) for e in result["errors"])


def test_concurrency_config_default_and_clamp() -> None:
    manager = SpiderLiteManager(fetcher=fake_fetch)

    assert manager._config(_payload())["concurrency"] == 1
    assert manager._config(_payload(concurrency=99))["concurrency"] == 16
    assert manager._config(_payload(concurrency=0))["concurrency"] == 1
    assert manager._config(_payload(max_concurrency=3))["concurrency"] == 3


def test_serial_path_has_fetch_stats_too() -> None:
    result = SpiderLiteManager(fetcher=fake_fetch).run(_payload(run_id="serial_stats"))

    assert result["fetch_stats"] == {"network": 5, "cache": 0, "concurrency": 1}

"""S13: cross-run incremental crawl state store.

IncrementalStore unit coverage (signatures, key_fields, eviction, corrupt
state recovery) plus spider_lite integration: a second run over the same
scope only emits never-delivered items.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from visual_web_agent.incremental_state import IncrementalStore, item_signature, scope_slug
from visual_web_agent.spider_lite import FetchResult, SpiderLiteManager


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_incremental_state"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# store unit tests
# ---------------------------------------------------------------------------


def test_scope_slug_is_filesystem_safe() -> None:
    assert scope_slug("shop.example.com") == "shop.example.com"
    assert scope_slug("https://shop.example.com/路径?q=1") == "https_shop.example.com_q_1"
    assert scope_slug("") == "default"


def test_item_signature_respects_key_fields() -> None:
    a = {"id": 1, "title": "A", "views": 100}
    b = {"id": 1, "title": "A", "views": 999}
    assert item_signature(a, ["id", "title"]) == item_signature(b, ["id", "title"])
    assert item_signature(a) != item_signature(b)


def test_filter_new_and_commit_roundtrip(local_tmp_path: Path) -> None:
    store = IncrementalStore("example.com", base_dir=local_tmp_path)
    items = [{"id": 1}, {"id": 2}, {"id": 1}]  # in-batch duplicate collapses

    fresh, skipped = store.filter_new(items)
    assert [r["id"] for r in fresh] == [1, 2]
    assert skipped == 1
    assert store.commit(fresh) == 2

    # A brand-new store instance over the same scope sees the same index.
    store2 = IncrementalStore("example.com", base_dir=local_tmp_path)
    fresh2, skipped2 = store2.filter_new([{"id": 2}, {"id": 3}])
    assert [r["id"] for r in fresh2] == [3]
    assert skipped2 == 1
    assert store2.size == 2


def test_max_keys_evicts_oldest(local_tmp_path: Path) -> None:
    store = IncrementalStore("cap.example.com", base_dir=local_tmp_path, max_keys=2)
    store.commit([{"id": 1}])
    store.commit([{"id": 2}])
    store.commit([{"id": 3}])  # evicts id=1 on save

    store2 = IncrementalStore("cap.example.com", base_dir=local_tmp_path, max_keys=2)
    fresh, skipped = store2.filter_new([{"id": 1}, {"id": 2}, {"id": 3}])
    assert [r["id"] for r in fresh] == [1]
    assert skipped == 2


def test_corrupt_state_file_starts_fresh(local_tmp_path: Path) -> None:
    store = IncrementalStore("bad.example.com", base_dir=local_tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    fresh, skipped = store.filter_new([{"id": 1}])
    assert len(fresh) == 1 and skipped == 0
    store.commit(fresh)
    assert json.loads(store.path.read_text(encoding="utf-8"))["scope"] == "bad.example.com"


# ---------------------------------------------------------------------------
# spider_lite integration
# ---------------------------------------------------------------------------


def _make_spider(pages: dict[str, list[dict]]) -> SpiderLiteManager:
    def fetcher(url: str) -> FetchResult:
        names = pages.get(url, [])
        html = "<html><body>" + "".join(f'<span class="name">{n["name"]}</span>' for n in names) + "</body></html>"
        return FetchResult(url=url, status_code=200, html=html)

    return SpiderLiteManager(fetcher=fetcher)


def _payload(state_dir: Path, run_id: str) -> dict:
    return {
        "run_id": run_id,
        "start_urls": ["https://inc.example.com/list"],
        "follow_links": False,
        "extract": {"selector": ".name", "type": "css"},
        "incremental": True,
        "incremental_key_fields": ["value"],
        "incremental_dir": str(state_dir),
    }


def test_spider_second_run_only_emits_new_items(local_tmp_path: Path) -> None:
    url = "https://inc.example.com/list"
    pages = {url: [{"name": "alpha"}, {"name": "beta"}]}
    spider = _make_spider(pages)

    first = spider.run(_payload(local_tmp_path, "inc_run_1"))
    assert first["status"] == "success"
    assert first["item_count"] == 2
    assert first["incremental"]["new_count"] == 2
    assert first["incremental"]["skipped_known"] == 0

    # Site gains one new row; rerun must deliver only the delta.
    pages[url] = [{"name": "alpha"}, {"name": "beta"}, {"name": "gamma"}]
    second = _make_spider(pages).run(_payload(local_tmp_path, "inc_run_2"))
    assert second["item_count"] == 1
    assert second["items"][0]["value"] == "gamma"
    assert second["incremental"]["skipped_known"] == 2
    assert second["incremental"]["known_total"] == 3


def test_spider_incremental_off_keeps_legacy_shape(local_tmp_path: Path) -> None:
    url = "https://inc.example.com/list"
    spider = _make_spider({url: [{"name": "alpha"}]})
    result = spider.run({
        "run_id": "inc_off",
        "start_urls": [url],
        "follow_links": False,
        "extract": {"selector": ".name", "type": "css"},
    })
    assert result["status"] == "success"
    assert "incremental" not in result

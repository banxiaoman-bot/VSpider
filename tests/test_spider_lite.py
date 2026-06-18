from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visual_web_agent.page_cache import PageCacheMissError, PageResponseCache
from visual_web_agent.robots_policy import RobotsPolicyManager
from visual_web_agent.spider_lite import FetchResult, SpiderLiteManager, domain_of, extract_links, normalize_url


PAGES = {
    "https://example.com/": """
    <html><body>
      <a href="/page2">Next</a>
      <div class="quote"><span class="text">Hello world</span></div>
    </body></html>
    """,
    "https://example.com/page2": """
    <html><body>
      <a href="https://other.example/skip">Skip</a>
      <div class="quote"><span class="text">Second page</span></div>
    </body></html>
    """,
}


def fake_fetch(url: str) -> FetchResult:
    return FetchResult(url=url, status_code=200, html=PAGES[url])


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_spider_lite_tests"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_spider_lite_follows_links_and_extracts_selector_items() -> None:
    manager = SpiderLiteManager(fetcher=fake_fetch)

    result = manager.run({
        "run_id": "spider_test",
        "start_urls": ["https://example.com/"],
        "max_depth": 1,
        "max_pages": 5,
        "extract": {"selector": ".quote .text::text"},
    })

    assert result["status"] == "success"
    assert result["page_count"] == 2
    assert result["item_count"] == 2
    assert [item["value"] for item in result["items"]] == ["Hello world", "Second page"]
    assert manager.get_run("spider_test") is result
    assert manager.list_runs()[0]["run_id"] == "spider_test"


def test_page_response_cache_record_replay_roundtrip(local_tmp_path: Path) -> None:
    record = PageResponseCache(mode="record", cache_dir=local_tmp_path, session_id="dev")
    stored = record.store("https://example.com/", final_url="https://example.com/", status_code=200, html="<p>Hello</p>")
    replay = PageResponseCache(mode="replay", cache_dir=local_tmp_path, session_id="dev")
    cached = replay.lookup("https://example.com/")

    assert stored is not None
    assert record.public_state()["records"] == 1
    assert cached is not None
    assert cached.html == "<p>Hello</p>"
    assert replay.public_state()["hits"] == 1
    assert len(replay.list_entries()) == 1

    miss = PageResponseCache(mode="replay", cache_dir=local_tmp_path, session_id="dev", replay_fallback_on_miss=True)
    assert miss.lookup("https://example.com/missing") is None
    with pytest.raises(PageCacheMissError):
        replay.lookup("https://example.com/missing")


def test_spider_lite_page_cache_record_then_replay(local_tmp_path: Path) -> None:
    record_manager = SpiderLiteManager(fetcher=fake_fetch)
    record = record_manager.run({
        "run_id": "record_run",
        "start_urls": ["https://example.com/"],
        "max_depth": 0,
        "cache_mode": "record",
        "cache_dir": str(local_tmp_path),
        "cache_session_id": "session_a",
        "extract": {"selector": ".quote .text::text"},
    })

    def fail_fetch(url: str) -> FetchResult:
        raise AssertionError(f"network fetch should not run in replay mode: {url}")

    replay_manager = SpiderLiteManager(fetcher=fail_fetch)
    replay = replay_manager.run({
        "run_id": "replay_run",
        "start_urls": ["https://example.com/"],
        "max_depth": 0,
        "cache_mode": "replay",
        "cache_dir": str(local_tmp_path),
        "cache_session_id": "session_a",
        "extract": {"selector": ".quote .text::text"},
    })

    assert record["pages"][0]["fetch_source"] == "network"
    assert record["page_cache"]["records"] == 1
    assert replay["pages"][0]["fetch_source"] == "cache"
    assert replay["items"][0]["value"] == "Hello world"
    assert replay["page_cache"]["hits"] == 1


def test_spider_lite_exports_items_feed(monkeypatch, local_tmp_path: Path) -> None:
    import visual_web_agent.spider_lite as spider_lite

    artifact_root = local_tmp_path / "artifacts"
    monkeypatch.setattr(spider_lite, "resolve_artifact_path", lambda filename, subdir="": artifact_root / subdir / filename)
    monkeypatch.setattr(spider_lite, "register_artifact", lambda path: None)
    monkeypatch.setattr(spider_lite, "artifact_url", lambda path: "/download/" + Path(path).name)

    manager = SpiderLiteManager(fetcher=fake_fetch)
    result = manager.run({
        "run_id": "feed_run",
        "start_urls": ["https://example.com/"],
        "max_depth": 0,
        "extract": {"selector": ".quote .text::text"},
        "export": True,
        "export_filename": "feed.jsonl",
    })
    auto = dict(result["artifact"])
    manual = manager.export_feed("feed_run", format="json", filename="feed.json")

    auto_path = Path(auto["path"])
    manual_path = Path(manual["path"])
    assert auto["format"] == "jsonl"
    assert auto_path.read_text(encoding="utf-8").strip() == '{"url":"https://example.com/","value":"Hello world"}'
    data = json.loads(manual_path.read_text(encoding="utf-8"))
    assert data["run_id"] == "feed_run"
    assert data["item_count"] == 1
    assert data["items"][0]["value"] == "Hello world"


def test_spider_lite_persists_contracts_registry_and_manifest(
    monkeypatch,
    local_tmp_path: Path,
) -> None:
    import visual_web_agent.spider_lite as spider_lite
    from visual_web_agent import run_registry
    from visual_web_agent.io_contract import persistence as _persistence

    artifact_root = local_tmp_path / "artifacts"
    runs_root = local_tmp_path / "runs"
    registry_root = local_tmp_path / "registry"
    monkeypatch.setattr(spider_lite, "resolve_artifact_path", lambda filename, subdir="": artifact_root / subdir / filename)
    monkeypatch.setattr(spider_lite, "artifact_url", lambda path: "/download/" + Path(path).name)
    monkeypatch.setattr(_persistence, "default_runs_root", lambda: runs_root)
    monkeypatch.setattr(run_registry, "registry_root", lambda base_dir=None: registry_root)
    monkeypatch.setattr(run_registry, "_run_path", lambda run_id, base_dir=None: registry_root / f"{run_id}.json")

    manager = SpiderLiteManager(fetcher=fake_fetch)
    result = manager.run({
        "run_id": "spider_contract",
        "goal": "爬取 quote 文本并导出 JSONL",
        "start_urls": ["https://example.com/"],
        "max_depth": 0,
        "extract": {"selector": ".quote .text::text"},
        "export": True,
        "export_filename": "spider_contract.jsonl",
        "persist_run_contracts": True,
    })

    input_contract = json.loads((runs_root / "spider_contract" / "input_contract.json").read_text(encoding="utf-8"))
    output_contract = json.loads((runs_root / "spider_contract" / "output_contract.json").read_text(encoding="utf-8"))
    manifest = json.loads((runs_root / "spider_contract" / "manifest.json").read_text(encoding="utf-8"))
    rec = run_registry.load_run("spider_contract", base_dir=registry_root)

    assert result["status"] == "success"
    assert input_contract["version"] == "input_contract.v1"
    assert input_contract["source"] == "spider_lite"
    assert input_contract["urls"][0]["url"] == "https://example.com/"
    assert input_contract["constraints"]["max_pages"] == 10
    assert output_contract["output_kind"] == "dataset_records"
    assert output_contract["container"] == "jsonl"
    assert manifest["items"][0]["kind"] == "dataset_records"
    assert manifest["items"][0]["produced_by"] == "spider_lite"
    assert manifest["items"][0]["extra"]["row_count"] == 1
    assert manifest["items"][0]["extra"]["fields"] == ["url", "value"]
    assert rec is not None
    assert rec["mode"] == "spider_lite"
    assert rec["status"] == "succeeded"
    assert rec["target_url"] == "https://example.com/"


def test_spider_lite_item_pipeline_dedupe_fields_and_required() -> None:
    pages = {
        "https://example.com/": """
        <div class="quote"><span class="text"> Alpha </span></div>
        <div class="quote"><span class="text">Alpha</span></div>
        <div class="quote"><span class="text"></span></div>
        """
    }

    def fetch(url: str) -> FetchResult:
        return FetchResult(url=url, status_code=200, html=pages[url])

    manager = SpiderLiteManager(fetcher=fetch)
    result = manager.run({
        "run_id": "pipeline_run",
        "start_urls": ["https://example.com/"],
        "max_depth": 0,
        "extract": {"selector": ".quote .text::text"},
        "item_pipeline": {
            "fields": ["value"],
            "required_fields": ["value"],
            "dedupe_by": ["value"],
            "max_items": 10,
        },
    })
    items_page = manager.items("pipeline_run", fields=["value"], offset=0, limit=1)

    assert result["item_count"] == 1
    assert result["items"] == [{"value": "Alpha"}]
    assert result["item_pipeline"]["dropped_duplicate"] == 1
    assert result["item_pipeline"]["dropped_required"] == 1
    assert items_page["total"] == 1
    assert items_page["fields"] == ["value"]
    assert items_page["items"] == [{"value": "Alpha"}]


def test_spider_lite_respects_allowed_domains_and_robots() -> None:
    robots = RobotsPolicyManager()
    robots.set_robots("example.com", "User-agent: *\nDisallow: /blocked\nCrawl-delay: 2")
    pages = {
        "https://example.com/": '<a href="/blocked">Blocked</a><a href="/ok">OK</a><p class="item">Root</p>',
        "https://example.com/ok": '<p class="item">OK</p>',
    }

    def fetch(url: str) -> FetchResult:
        return FetchResult(url=url, status_code=200, html=pages[url])

    manager = SpiderLiteManager(robots_policy=robots, fetcher=fetch)
    result = manager.run({
        "start_urls": ["https://example.com/"],
        "max_depth": 1,
        "max_pages": 5,
        "robots_txt_obey": True,
        "extract": {"selector": ".item::text"},
    })

    urls = [page["url"] for page in result["pages"]]
    assert "https://example.com/" in urls
    assert "https://example.com/ok" in urls
    assert "https://example.com/blocked" not in urls
    assert any(error["error"] == "blocked by robots.txt" for error in result["errors"])


def test_spider_lite_utility_functions() -> None:
    html = '<a href="/a#frag">A</a><a href="https://example.com/b">B</a>'

    assert normalize_url("https://example.com/a#frag") == "https://example.com/a"
    assert normalize_url("javascript:void(0)") == ""
    assert domain_of("https://example.com:443/path") == "example.com"
    assert extract_links(html, "https://example.com/root") == ["https://example.com/a", "https://example.com/b"]


def test_spider_lite_api_wiring(monkeypatch, local_tmp_path: Path) -> None:
    import api_server
    import visual_web_agent.spider_lite as spider_lite
    from visual_web_agent import run_registry
    from visual_web_agent.io_contract import persistence as _persistence

    artifact_root = local_tmp_path / "artifacts"
    runs_root = local_tmp_path / "runs"
    registry_root = local_tmp_path / "registry"
    monkeypatch.setattr(spider_lite, "resolve_artifact_path", lambda filename, subdir="": artifact_root / subdir / filename)
    monkeypatch.setattr(spider_lite, "artifact_url", lambda path: "/download/" + Path(path).name)
    monkeypatch.setattr(_persistence, "default_runs_root", lambda: runs_root)
    monkeypatch.setattr(run_registry, "registry_root", lambda base_dir=None: registry_root)
    monkeypatch.setattr(run_registry, "_run_path", lambda run_id, base_dir=None: registry_root / f"{run_id}.json")

    manager = SpiderLiteManager(fetcher=fake_fetch)
    monkeypatch.setattr(api_server, "_spider_lite", manager)
    client = TestClient(api_server.app)

    run_resp = client.post(
        "/api/spider/run",
        json={
            "run_id": "api_spider",
            "start_urls": ["https://example.com/"],
            "max_depth": 1,
            "max_pages": 3,
            "cache_mode": "record",
            "cache_dir": str(local_tmp_path),
            "cache_session_id": "api_cache",
            "extract": {"selector": ".quote .text::text"},
            "export": True,
            "export_filename": "api_feed.jsonl",
        },
    )
    list_resp = client.get("/api/spider/runs")
    cache_resp = client.get("/api/spider/page_cache/api_cache")
    entries_resp = client.get("/api/spider/page_cache/api_cache/entries")
    export_resp = client.post("/api/spider/api_spider/export", json={"format": "json", "filename": "api_feed.json"})
    items_resp = client.get("/api/spider/api_spider/items?fields=value&limit=1")
    detail_resp = client.get("/api/spider/api_spider")

    assert run_resp.status_code == 200
    assert run_resp.json()["result"]["item_count"] == 2
    assert run_resp.json()["result"]["artifact"]["count"] == 2
    assert list_resp.json()["runs"][0]["run_id"] == "api_spider"
    assert cache_resp.json()["result"]["entry_count"] == 2
    assert len(entries_resp.json()["entries"]) == 2
    assert export_resp.json()["artifact"]["format"] == "json"
    assert items_resp.json()["result"]["count"] == 1
    assert items_resp.json()["result"]["items"] == [{"value": "Hello world"}]
    assert detail_resp.json()["result"]["run_id"] == "api_spider"
    assert (runs_root / "api_spider" / "input_contract.json").exists()
    assert run_registry.load_run("api_spider", base_dir=registry_root)["status"] == "succeeded"


def test_spider_lite_source_wiring() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    api_src = (root / "api_server.py").read_text(encoding="utf-8")
    spider_src = (root / "visual_web_agent" / "spider_lite.py").read_text(encoding="utf-8")

    assert "from visual_web_agent.spider_lite import SpiderLiteManager" in api_src
    assert "_spider_lite = SpiderLiteManager(robots_policy=_robots_policy)" in api_src
    spider_api_src = (root / "api_routes" / "spider_api.py").read_text(encoding="utf-8")
    assert '@app.post("/api/spider/run"' in spider_api_src
    assert '@app.get("/api/spider/runs"' in spider_api_src
    assert '@app.get(' in spider_api_src and "page_cache/{session_id}" in spider_api_src
    assert '@app.get(' in spider_api_src and "page_cache/{session_id}/entries" in spider_api_src
    assert '@app.post(' in spider_api_src and "{run_id}/export" in spider_api_src
    assert '@app.get(' in spider_api_src and "{run_id}/items" in spider_api_src
    assert '@app.get(' in spider_api_src and "spider/{run_id}" in spider_api_src
    assert "class SpiderLiteManager:" in spider_src
    assert "PageResponseCache" in spider_src
    assert "def cache_entries(" in spider_src
    assert "def export_feed(" in spider_src
    assert "def items(" in spider_src
    assert "def _apply_item_pipeline(" in spider_src
    assert "register_artifact" in spider_src
    assert "def extract_links(" in spider_src

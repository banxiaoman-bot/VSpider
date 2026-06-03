"""Slice CRAWL-SEED2: URL Seeder HEAD liveness probe + content-type metadata.

Borrowed from crawl4ai's URL-seeder ``live_check`` / metadata pass. An optional
``head_fetcher`` lets the seeder cheaply probe candidate URLs (HEAD-style:
status + content-type, no body) so dead links can be dropped before they ever
enter the crawl frontier (mission §一 "高效 / 准确 — 能不重抓就不重抓").

Stub head fetcher (a plain dict map); no network.
"""

from __future__ import annotations

import threading
import time

from visual_web_agent import url_seeder
from visual_web_agent.spider_lite import FetchResult
from visual_web_agent.url_seeder import (
    UrlSeeder,
    content_type_to_output_kind,
    default_head_fetch,
)


URLSET = (
    "<urlset>"
    "<url><loc>https://e.com/live</loc></url>"
    "<url><loc>https://e.com/dead</loc></url>"
    "</urlset>"
)

HEAD_MAP = {
    "https://e.com/live": {"status_code": 200, "content_type": "text/html"},
    "https://e.com/dead": {"status_code": 404},
}


def _get_fetch(url: str) -> FetchResult:
    bodies = {"https://e.com/sitemap.xml": URLSET}
    return FetchResult(url=url, status_code=200, html=bodies.get(url, ""))


def _head_fetch(url: str) -> dict:
    return HEAD_MAP.get(url, {"status_code": 0})


def _seeder(*, with_head: bool = True) -> UrlSeeder:
    return UrlSeeder(_get_fetch, head_fetcher=_head_fetch if with_head else None)


def test_probe_url_live_200() -> None:
    meta = _seeder().probe_url("https://e.com/live")
    assert meta == {
        "url": "https://e.com/live",
        "status_code": 200,
        "content_type": "text/html",
        "live": True,
        "output_kind": "html_snapshot",
    }


def test_probe_url_dead_404() -> None:
    meta = _seeder().probe_url("https://e.com/dead")
    assert meta["status_code"] == 404
    assert meta["live"] is False


def test_probe_url_without_head_fetcher_is_not_live() -> None:
    meta = _seeder(with_head=False).probe_url("https://e.com/live")
    assert meta["status_code"] == 0
    assert meta["live"] is False


def test_probe_url_swallows_head_fetcher_errors() -> None:
    def boom(url: str) -> dict:
        raise RuntimeError("network down")

    seeder = UrlSeeder(_get_fetch, head_fetcher=boom)
    meta = seeder.probe_url("https://e.com/live")
    assert meta["live"] is False


def test_seed_from_sitemap_live_only_drops_dead() -> None:
    seeder = _seeder()
    seeds = seeder.seed_from_sitemap("https://e.com/sitemap.xml", live_only=True)
    assert seeds == ["https://e.com/live"]


def test_seed_from_sitemap_without_live_only_keeps_all() -> None:
    seeder = _seeder()
    seeds = seeder.seed_from_sitemap("https://e.com/sitemap.xml")
    assert seeds == ["https://e.com/live", "https://e.com/dead"]


def test_live_only_is_noop_without_head_fetcher() -> None:
    seeder = _seeder(with_head=False)
    seeds = seeder.seed_from_sitemap("https://e.com/sitemap.xml", live_only=True)
    assert seeds == ["https://e.com/live", "https://e.com/dead"]


# --- default_head_fetch: real HEAD via urllib (urlopen monkeypatched) ------

class _FakeHeadResp:
    def __init__(self, status: int, content_type: str = "") -> None:
        self.status = status
        self.headers = {"content-type": content_type} if content_type else {}

    def __enter__(self) -> "_FakeHeadResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_default_head_fetch_success(monkeypatch) -> None:
    monkeypatch.setattr(url_seeder, "urlopen", lambda req, timeout=10.0: _FakeHeadResp(200, "text/html"))
    assert default_head_fetch("https://e.com/x") == {"status_code": 200, "content_type": "text/html"}


def test_default_head_fetch_keeps_http_error_status(monkeypatch) -> None:
    from urllib.error import HTTPError

    def _raise(req, timeout=10.0):
        raise HTTPError("https://e.com/x", 404, "Not Found", {"content-type": "text/plain"}, None)

    monkeypatch.setattr(url_seeder, "urlopen", _raise)
    assert default_head_fetch("https://e.com/x")["status_code"] == 404


def test_default_head_fetch_network_error_is_status_0(monkeypatch) -> None:
    def _boom(req, timeout=10.0):
        raise OSError("dns down")

    monkeypatch.setattr(url_seeder, "urlopen", _boom)
    assert default_head_fetch("https://e.com/x") == {"status_code": 0, "content_type": ""}


def test_default_head_fetch_wires_into_probe_url(monkeypatch) -> None:
    monkeypatch.setattr(url_seeder, "urlopen", lambda req, timeout=10.0: _FakeHeadResp(200, "application/pdf"))
    seeder = UrlSeeder(_get_fetch, head_fetcher=default_head_fetch)
    meta = seeder.probe_url("https://e.com/file.pdf")
    assert meta["live"] is True
    assert meta["content_type"] == "application/pdf"
    assert meta["output_kind"] == "media_pdf"


# --- SEED-NEXT: HEAD->GET fallback for servers that reject HEAD -------------

def test_head_405_falls_back_to_get(monkeypatch) -> None:
    calls: list[str] = []

    def fake_urlopen(req, timeout=10.0):
        calls.append(req.get_method())
        if req.get_method() == "HEAD":
            return _FakeHeadResp(405)
        return _FakeHeadResp(200, "application/pdf")  # GET succeeds

    monkeypatch.setattr(url_seeder, "urlopen", fake_urlopen)
    result = default_head_fetch("https://e.com/no-head")
    assert result == {"status_code": 200, "content_type": "application/pdf"}
    assert calls == ["HEAD", "GET"]


def test_head_501_falls_back_to_get(monkeypatch) -> None:
    def fake_urlopen(req, timeout=10.0):
        if req.get_method() == "HEAD":
            return _FakeHeadResp(501)
        return _FakeHeadResp(206, "video/mp4")  # Range honored -> 206

    monkeypatch.setattr(url_seeder, "urlopen", fake_urlopen)
    result = default_head_fetch("https://e.com/x")
    assert result["status_code"] == 206
    assert result["content_type"] == "video/mp4"


def test_head_200_does_not_fall_back(monkeypatch) -> None:
    calls: list[str] = []

    def fake_urlopen(req, timeout=10.0):
        calls.append(req.get_method())
        return _FakeHeadResp(200, "text/html")

    monkeypatch.setattr(url_seeder, "urlopen", fake_urlopen)
    default_head_fetch("https://e.com/ok")
    assert calls == ["HEAD"]  # no GET fallback on 200


def test_get_fallback_sends_range_header(monkeypatch) -> None:
    seen: dict = {}

    def fake_urlopen(req, timeout=10.0):
        if req.get_method() == "GET":
            seen.update({k.lower(): v for k, v in req.header_items()})
            return _FakeHeadResp(200, "image/png")
        return _FakeHeadResp(405)

    monkeypatch.setattr(url_seeder, "urlopen", fake_urlopen)
    default_head_fetch("https://e.com/x")
    assert seen.get("range") == "bytes=0-0"


# --- SEED-NEXT: content_type -> output_kind routing ------------------------

class TestContentTypeToOutputKind:
    def test_media_families(self) -> None:
        assert content_type_to_output_kind("image/jpeg") == "media_image"
        assert content_type_to_output_kind("video/mp4") == "media_video"
        assert content_type_to_output_kind("audio/mpeg") == "media_audio"

    def test_documents_and_data(self) -> None:
        assert content_type_to_output_kind("application/pdf") == "media_pdf"
        assert content_type_to_output_kind("application/zip") == "media_archive"
        assert content_type_to_output_kind("text/csv") == "dataset_rows"
        assert content_type_to_output_kind("application/json") == "dataset_records"
        assert content_type_to_output_kind("text/html") == "html_snapshot"

    def test_strips_charset_param_and_case(self) -> None:
        assert content_type_to_output_kind("TEXT/HTML; charset=UTF-8") == "html_snapshot"

    def test_other_text_is_code_or_text(self) -> None:
        assert content_type_to_output_kind("text/plain") == "code_or_text"

    def test_unknown_is_file_generic(self) -> None:
        assert content_type_to_output_kind("application/octet-stream") == "file_generic"

    def test_empty_is_blank(self) -> None:
        assert content_type_to_output_kind("") == ""
        assert content_type_to_output_kind(None) == ""


def test_probe_url_surfaces_output_kind() -> None:
    head_map = {"https://e.com/pic": {"status_code": 200, "content_type": "image/png"}}
    seeder = UrlSeeder(_get_fetch, head_fetcher=lambda u: head_map.get(u, {"status_code": 0}))
    meta = seeder.probe_url("https://e.com/pic")
    assert meta["output_kind"] == "media_image"
    assert meta["live"] is True


# --- SEED-PARALLEL: probe_urls batch + parallel live_only -------------------

def test_probe_urls_empty_returns_empty() -> None:
    assert _seeder().probe_urls([]) == []


def test_probe_urls_serial_preserves_order() -> None:
    urls = ["https://e.com/live", "https://e.com/dead", "https://e.com/live"]
    metas = _seeder().probe_urls(urls, concurrency=1)
    assert [m["url"] for m in metas] == urls
    assert [m["live"] for m in metas] == [True, False, True]


def test_probe_urls_concurrency_below_one_runs_serially() -> None:
    metas = _seeder().probe_urls(["https://e.com/live"], concurrency=0)
    assert [m["live"] for m in metas] == [True]


def test_probe_urls_parallel_preserves_input_order() -> None:
    urls = [f"https://e.com/p{i}" for i in range(5)]
    # Reverse delay: later URLs finish first, so order is preserved only if the
    # implementation maps results back to the input index (not completion order).
    delays = {u: (len(urls) - i) * 0.02 for i, u in enumerate(urls)}

    def head(url: str) -> dict:
        time.sleep(delays[url])
        return {"status_code": 200, "content_type": "text/html"}

    seeder = UrlSeeder(_get_fetch, head_fetcher=head)
    metas = seeder.probe_urls(urls, concurrency=5)
    assert [m["url"] for m in metas] == urls
    assert all(m["live"] for m in metas)


def test_probe_urls_actually_runs_in_parallel() -> None:
    n = 4
    # A Barrier of n only releases when all n probes are in flight at once; a
    # serial implementation would block on the first wait() and time out.
    barrier = threading.Barrier(n, timeout=5)

    def head(url: str) -> dict:
        barrier.wait()
        return {"status_code": 200, "content_type": "text/html"}

    seeder = UrlSeeder(_get_fetch, head_fetcher=head)
    urls = [f"https://e.com/c{i}" for i in range(n)]
    metas = seeder.probe_urls(urls, concurrency=n)
    assert [m["live"] for m in metas] == [True] * n


def test_probe_urls_parallel_swallows_errors() -> None:
    def boom(url: str) -> dict:
        raise RuntimeError("network down")

    seeder = UrlSeeder(_get_fetch, head_fetcher=boom)
    metas = seeder.probe_urls(["https://e.com/a", "https://e.com/b"], concurrency=2)
    assert [m["live"] for m in metas] == [False, False]
    assert [m["url"] for m in metas] == ["https://e.com/a", "https://e.com/b"]


def test_seed_from_sitemap_live_only_parallel_drops_dead() -> None:
    seeder = _seeder()
    seeds = seeder.seed_from_sitemap(
        "https://e.com/sitemap.xml", live_only=True, probe_concurrency=4
    )
    assert seeds == ["https://e.com/live"]


def test_seed_from_robots_live_only_parallel_drops_dead() -> None:
    def get_fetch(url: str) -> FetchResult:
        bodies = {
            "https://e.com/robots.txt": "Sitemap: https://e.com/sitemap.xml",
            "https://e.com/sitemap.xml": URLSET,
        }
        return FetchResult(url=url, status_code=200, html=bodies.get(url, ""))

    seeder = UrlSeeder(get_fetch, head_fetcher=_head_fetch)
    seeds = seeder.seed_from_robots(
        "https://e.com/robots.txt", live_only=True, probe_concurrency=4
    )
    assert seeds == ["https://e.com/live"]

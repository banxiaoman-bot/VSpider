"""Slice CRAWL-SEED2: URL Seeder HEAD liveness probe + content-type metadata.

Borrowed from crawl4ai's URL-seeder ``live_check`` / metadata pass. An optional
``head_fetcher`` lets the seeder cheaply probe candidate URLs (HEAD-style:
status + content-type, no body) so dead links can be dropped before they ever
enter the crawl frontier (mission §一 "高效 / 准确 — 能不重抓就不重抓").

Stub head fetcher (a plain dict map); no network.
"""

from __future__ import annotations

from visual_web_agent import url_seeder
from visual_web_agent.spider_lite import FetchResult
from visual_web_agent.url_seeder import UrlSeeder, default_head_fetch


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

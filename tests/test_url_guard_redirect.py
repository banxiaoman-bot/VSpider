"""Redirect-hop SSRF: re-check *every* redirect target, not just the first URL.

All three transports auto-follow 3xx -- urllib (``urlopen``/opener), httpx
(``follow_redirects=True``) and requests (``allow_redirects=True``). Without
re-validation a public URL can bounce the server onto an internal host
(``302 -> http://169.254.169.254/``).

Part 1 pins the url_guard primitives with no network (urllib via injected
``resolver``; httpx hook via IP-literal URLs that skip DNS). Part 2 pins that
every redirect-following fetch site is actually wired to a guard (source-level,
matching the repo's structural-wiring test style).
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from visual_web_agent.url_guard import (
    UrlGuardError,
    _GuardedRedirectHandler,
    build_guarded_opener,
    guard_httpx_request,
)

ROOT = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ---- Part 1: url_guard redirect primitives ----------------------------------

def _handler(resolver):
    return _GuardedRedirectHandler(resolver=resolver, allow_private=False)


def test_redirect_hop_to_internal_is_blocked():
    handler = _handler(lambda host: ["127.0.0.1"])
    req = urllib.request.Request("http://public.example.com/")
    with pytest.raises(UrlGuardError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://internal.example.com/")


def test_redirect_hop_to_metadata_literal_is_blocked():
    handler = _handler(None)  # IP literal -> no DNS needed
    req = urllib.request.Request("http://public.example.com/")
    with pytest.raises(UrlGuardError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/latest/")


def test_redirect_hop_to_public_is_allowed():
    handler = _handler(lambda host: ["93.184.216.34"])
    req = urllib.request.Request("http://public.example.com/")
    newreq = handler.redirect_request(req, None, 302, "Found", {}, "http://other.example.com/")
    assert newreq is not None
    assert newreq.full_url == "http://other.example.com/"


def test_build_guarded_opener_installs_redirect_guard():
    opener = build_guarded_opener()
    assert any(isinstance(h, _GuardedRedirectHandler) for h in opener.handlers)


class _FakeReq:
    def __init__(self, url: str) -> None:
        self.url = url


def test_httpx_hook_blocks_internal_hop():
    with pytest.raises(UrlGuardError):
        guard_httpx_request(_FakeReq("http://127.0.0.1:9000/"))


def test_httpx_hook_blocks_metadata_hop():
    with pytest.raises(UrlGuardError):
        guard_httpx_request(_FakeReq("http://169.254.169.254/latest/meta-data/"))


def test_httpx_hook_allows_public_hop():
    guard_httpx_request(_FakeReq("http://93.184.216.34/media.mp4"))  # public literal, no raise


# ---- Part 2: every redirect-following fetch site is wired --------------------

def test_spider_lite_default_fetch_uses_guarded_opener():
    assert "build_guarded_opener().open(" in _src("visual_web_agent/spider_lite.py")


def test_url_seeder_probe_uses_guarded_opener():
    assert "build_guarded_opener().open(" in _src("visual_web_agent/url_seeder.py")


def test_api_replay_uses_guarded_opener():
    assert "build_guarded_opener().open(" in _src("visual_web_agent/api_replay.py")


def test_media_downloader_registers_httpx_request_guard():
    src = _src("visual_web_agent/media_harvester/downloader.py")
    assert "guard_httpx_request" in src and "event_hooks" in src


def test_fetch_articles_follows_redirects_through_guard():
    src = _src("visual_web_agent/fetch_articles.py")
    assert "_guarded_get(" in src
    # the raw auto-follow must be gone (replaced by the manual guarded follow)
    assert "allow_redirects=True" not in src

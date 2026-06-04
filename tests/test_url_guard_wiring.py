"""SSRF guard wiring: every server-side fetch site must consult ``url_guard``.

These pin that the guard is invoked *before* any socket work at each entry
point, so a blocked host fails closed instead of letting the server connect to
an internal address. Blocking a loopback / link-local literal proves the guard
fired (without it the call would attempt a real connection, not a guard error).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from visual_web_agent import spider_lite, url_seeder
from visual_web_agent.media_harvester.candidates import MediaCandidate
from visual_web_agent.media_harvester.downloader import download_candidate
from visual_web_agent.url_guard import UrlGuardError


class TestSpiderLiteWiring:
    def test_default_fetch_rejects_loopback(self):
        with pytest.raises(UrlGuardError):
            spider_lite.default_fetch("http://127.0.0.1/secret")

    def test_default_fetch_rejects_metadata(self):
        with pytest.raises(UrlGuardError):
            spider_lite.default_fetch("http://169.254.169.254/latest/meta-data/")

    def test_default_fetch_rejects_non_http_scheme(self):
        with pytest.raises(UrlGuardError):
            spider_lite.default_fetch("file:///etc/passwd")


class TestUrlSeederWiring:
    def test_head_fetch_blocked_reports_not_live(self):
        # default_head_fetch -> _urllib_probe: a blocked host must come back as
        # status 0 (not-live) with no network call.
        out = url_seeder.default_head_fetch("http://169.254.169.254/latest/meta-data/")
        assert out == {"status_code": 0, "content_type": ""}

    def test_probe_url_blocked_not_live(self):
        seeder = url_seeder.UrlSeeder(fetcher=lambda u: "", head_fetcher=url_seeder.default_head_fetch)
        meta = seeder.probe_url("http://10.0.0.1/x")
        assert meta["live"] is False
        assert meta["status_code"] == 0


class TestDownloaderWiring:
    # No tmp_path fixture: the guard short-circuits before any mkdir, so the
    # dest dir must never be created -- which we assert directly. (Also dodges
    # the Windows pytest-tmp permission lock noted in the repo workflow rules.)
    def test_download_candidate_blocks_private(self):
        dest = Path("_ssrf_guard_unused_dest_private")
        candidate = MediaCandidate(url="http://10.0.0.1/video.mp4", kind="media_video")
        outcome = download_candidate(candidate, dest)
        assert outcome.ok is False
        assert outcome.error.startswith("blocked_url:")
        assert not dest.exists()

    def test_download_candidate_blocks_metadata(self):
        dest = Path("_ssrf_guard_unused_dest_metadata")
        candidate = MediaCandidate(url="http://169.254.169.254/latest/meta-data/")
        outcome = download_candidate(candidate, dest)
        assert outcome.ok is False
        assert "blocked_url" in outcome.error
        assert not dest.exists()

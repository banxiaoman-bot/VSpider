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


class TestApiReplayWiring:
    # replay_candidate(fetcher=None) takes the real-network path; a blocked
    # literal IP must fail closed (no socket, no DNS) as a clean blocked result.
    def test_replay_blocks_metadata_url(self):
        from visual_web_agent import api_replay

        result = api_replay.replay_candidate(
            run_id="ssrf_probe",
            candidate={"endpoint": "http://169.254.169.254/latest/meta-data/", "method": "GET"},
        )
        assert result["http_ok"] is False
        assert result["status"] == "blocked_url"

    def test_replay_blocks_private_url(self):
        from visual_web_agent import api_replay

        result = api_replay.replay_candidate(
            run_id="ssrf_probe2",
            candidate={"endpoint": "http://10.0.0.1/admin", "method": "GET"},
        )
        assert result["http_ok"] is False
        assert result["status"] == "blocked_url"


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


class TestMainFetchWiring:
    # main.py pulls a page-supplied RPA xlsx (download_url) and a user-supplied
    # Google Sheet export server-side; both must route through url_guard.
    # ``urljoin`` lets an absolute download_url escape the default base onto an
    # internal host -- a textbook SSRF -- so the guard must fire before any socket.
    def test_rpa_challenge_rows_blocks_internal_download_url(self):
        from visual_web_agent import main as main_mod

        with pytest.raises(UrlGuardError):
            main_mod._load_rpa_challenge_rows("http://169.254.169.254/x.xlsx", 1)

    def test_rpa_challenge_rows_blocks_non_http_scheme(self):
        from visual_web_agent import main as main_mod

        with pytest.raises(UrlGuardError):
            main_mod._load_rpa_challenge_rows("file:///etc/passwd", 1)

    def test_main_fetch_sites_use_guarded_opener(self):
        # Pin the wiring: a refactor must not silently revert to raw urlopen
        # (which would also drop the redirect-hop guard).
        src = Path("visual_web_agent/main.py").read_text(encoding="utf-8")
        assert src.count("build_guarded_opener().open(") >= 2
        assert "with urlopen(" not in src


class TestVlmEndpointOverrideGuard:
    # The OpenAI client fetches the VLM/semantic ``base_url`` server-side WITH the
    # configured API key in the Authorization header. A user-supplied override
    # (api_server Form ``vlm_base_url`` / ``semantic_base_url`` -> vlm_options ->
    # runtime_config.VLM_API_BASE) is therefore both an SSRF and a credential-exfil
    # vector. ``allow_private=True`` keeps local-LLM endpoints usable (the shipped
    # default is ``http://localhost:8000/v1``) while still blocking the
    # cloud-metadata endpoint / link-local / non-http(s) schemes.
    def test_blocks_metadata_endpoint(self):
        from visual_web_agent import main as main_mod

        with pytest.raises(UrlGuardError):
            main_mod._guard_vlm_endpoint_override("http://169.254.169.254/v1", label="base_url")

    def test_blocks_non_http_scheme(self):
        from visual_web_agent import main as main_mod

        with pytest.raises(UrlGuardError):
            main_mod._guard_vlm_endpoint_override("file:///etc/passwd", label="base_url")

    def test_allows_local_llm_default(self):
        # The shipped default endpoint is a loopback local-LLM server; it must
        # stay usable (allow_private=True) or every default install breaks.
        from visual_web_agent import main as main_mod

        url = "http://localhost:8000/v1"
        assert main_mod._guard_vlm_endpoint_override(url, label="base_url") == url

    def test_allows_public_endpoint(self):
        from visual_web_agent import main as main_mod

        url = "https://api.openai.com/v1"
        assert main_mod._guard_vlm_endpoint_override(url, label="semantic_base_url") == url

    def test_override_application_is_guarded(self):
        # Pin the wiring: both the base_url and semantic_base_url overrides must
        # route through the guard so a refactor can't reintroduce a raw assignment.
        src = Path("visual_web_agent/main.py").read_text(encoding="utf-8")
        assert "_cfg.VLM_API_BASE = _guard_vlm_endpoint_override(" in src
        assert "_cfg.VLM_SEMANTIC_API_BASE = _guard_vlm_endpoint_override(" in src

"""Resume-wiring regression for RUN-RESUME1 step 4 (unified resume switch).

Proves the ``resume`` flag threads end-to-end through the media-download path:
``maybe_run_media_harvest(resume=...)`` -> ``harvest_to_run(resume=...)`` ->
``download_candidate(resume=...)``. The main.py call site passes
``bool((run_constraints or {}).get("resume"))`` into the hook; here we verify
the hook + harvester forward it faithfully and default to ``False`` (so a run
without ``constraints.resume`` stays byte-identical to the legacy single-pass).

Pure: a fake page + a captured (monkeypatched) ``download_candidate``; no
network, no Playwright. Mirrors ``test_media_harvester_agent_hook.py`` stubs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from visual_web_agent.media_harvester import maybe_run_media_harvest
from visual_web_agent.media_harvester.candidates import collect_from_html
from visual_web_agent.media_harvester.harvester import harvest_to_run


class _FakePage:
    """Subset of the Playwright ``Page`` surface the hook reads."""

    def __init__(self, *, html: str, url: str = "https://example.com/landing") -> None:
        self._html = html
        self.url = url

    async def content(self) -> str:
        return self._html


class _StubOutcome:
    """Minimal DownloadOutcome stand-in; ``ok=False`` skips the manifest write."""

    ok = False
    sha256 = ""
    final_kind = "media_pdf"
    path = ""
    size = 0

    def to_dict(self) -> dict:
        return {"ok": False}


def _capture_download(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch ``download_candidate`` (both bind sites) to record the resume kwarg."""

    seen: dict = {"calls": [], "resume": None}

    def fake(candidate, artifacts_dir, *, client=None, resume=False, **kwargs):
        seen["calls"].append(resume)
        seen["resume"] = resume
        return _StubOutcome()

    import visual_web_agent.media_harvester.downloader as downloader_module
    import visual_web_agent.media_harvester.harvester as harvester_module

    monkeypatch.setattr(downloader_module, "download_candidate", fake)
    monkeypatch.setattr(harvester_module, "download_candidate", fake)
    return seen


_HTML = '<html><body><a href="/docs/a.pdf">a</a></body></html>'
_ROUTE = {"output_contract": {"output_kind": "media_pdf"}}


def _run(coro):
    return asyncio.run(coro)


class TestHarvestToRunThreadsResume:
    def test_direct_resume_true(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        seen = _capture_download(monkeypatch)
        candidates = collect_from_html(_HTML, base_url="https://example.com/")
        harvest_to_run(
            candidates, run_id="rr_h1", output_kind="media_pdf",
            base_dir=tmp_path, resume=True,
        )
        assert seen["calls"] and all(r is True for r in seen["calls"])

    def test_direct_default_is_false(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        seen = _capture_download(monkeypatch)
        candidates = collect_from_html(_HTML, base_url="https://example.com/")
        harvest_to_run(candidates, run_id="rr_h2", output_kind="media_pdf", base_dir=tmp_path)
        assert seen["calls"] and all(r is False for r in seen["calls"])


class TestHookThreadsResume:
    def test_hook_resume_true(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        seen = _capture_download(monkeypatch)
        result = _run(maybe_run_media_harvest(
            page=_FakePage(html=_HTML), capability_route=_ROUTE,
            run_id="rr_hook1", goal="download all PDFs", base_dir=tmp_path, resume=True,
        ))
        assert result.triggered is True
        assert seen["resume"] is True

    def test_hook_resume_false_explicit(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        seen = _capture_download(monkeypatch)
        _run(maybe_run_media_harvest(
            page=_FakePage(html=_HTML), capability_route=_ROUTE,
            run_id="rr_hook2", goal="download all PDFs", base_dir=tmp_path, resume=False,
        ))
        assert seen["resume"] is False

    def test_hook_default_is_no_resume(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        seen = _capture_download(monkeypatch)
        _run(maybe_run_media_harvest(
            page=_FakePage(html=_HTML), capability_route=_ROUTE,
            run_id="rr_hook3", goal="download all PDFs", base_dir=tmp_path,
        ))
        assert seen["resume"] is False

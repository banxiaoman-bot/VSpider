"""Slice FITMD-3: page_to_markdown emits a chunks.jsonl artifact.

Wires FITMD-2's chunking into the page_to_markdown handler: every run now also
splits the produced fit-markdown into RAG-ready chunks, exposes a chunk count +
path in workflow_memory / rpa_trail, and (when a run context is active) persists
a ``markdown_chunks`` jsonl artifact + manifest entry. Markdown behaviour is
unchanged; chunks are purely additive.

Deterministic stub page / browser (no Playwright, no network), mirroring
``test_page_to_markdown_handler.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from visual_web_agent.actions import ActionContext
from visual_web_agent.io_contract.runtime import clear_current_run, set_current_run
from visual_web_agent.page_to_markdown_action import PageToMarkdownHandler
from visual_web_agent.vlm_client import VSpiderAction


SAMPLE = """
<html><head><title>Doc</title></head>
<body>
<article>
  <h1>Alpha</h1>
  <p>First section paragraph with enough words to count.</p>
  <h2>Beta</h2>
  <p>Second section paragraph with enough words to count.</p>
</article>
</body></html>
"""


class _StubPage:
    def __init__(self, html: str, url: str) -> None:
        self._html = html
        self.url = url

    async def content(self) -> str:
        return self._html


class _StubBrowser:
    def __init__(self) -> None:
        self.rpa_trail: list = []


def _make_ctx(html: str, url: str, *, memory_key: str = "") -> ActionContext:
    action = VSpiderAction(action="page_to_markdown", target_id=0, type_value="", memory_key=memory_key)
    return ActionContext(action=action, browser=_StubBrowser(), workflow_memory={}, page=_StubPage(html, url))


def test_chunk_count_in_memory() -> None:
    ctx = _make_ctx(SAMPLE, "https://example.com", memory_key="doc_md")
    asyncio.run(PageToMarkdownHandler().execute(ctx))
    mem = ctx.workflow_memory["doc_md"]
    # two headings -> two chunks
    assert mem["chunk_count"] == 2
    # markdown itself is unchanged / still present
    assert "# Alpha" in mem["markdown"]


def test_chunk_count_present_without_run_context() -> None:
    ctx = _make_ctx(SAMPLE, "https://example.com", memory_key="doc_md")
    asyncio.run(PageToMarkdownHandler().execute(ctx))
    mem = ctx.workflow_memory["doc_md"]
    assert mem["chunk_count"] == 2
    # no active run -> nothing persisted
    assert mem["chunks_path"] == ""


def test_rpa_trail_records_chunk_count() -> None:
    ctx = _make_ctx(SAMPLE, "https://example.com")
    asyncio.run(PageToMarkdownHandler().execute(ctx))
    entry = ctx.browser.rpa_trail[-1]
    assert entry["action"] == "page_to_markdown"
    assert entry["chunk_count"] == 2


def test_persists_chunks_jsonl_when_run_active(tmp_path: Path) -> None:
    ctx = _make_ctx(SAMPLE, "https://example.com", memory_key="doc_md")
    set_current_run("p2m_chunks", base_dir=str(tmp_path))
    try:
        asyncio.run(PageToMarkdownHandler().execute(ctx))
    finally:
        clear_current_run()
    chunks_path = ctx.workflow_memory["doc_md"]["chunks_path"]
    assert chunks_path
    artifact = Path(chunks_path)
    assert artifact.exists()
    assert artifact.suffix == ".jsonl"
    lines = [line for line in artifact.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["heading"] == "Alpha"
    assert "index" in first and "word_count" in first and "text" in first


def test_markdown_artifact_still_written(tmp_path: Path) -> None:
    # the markdown_doc path must keep working alongside the new chunks artifact
    ctx = _make_ctx(SAMPLE, "https://example.com", memory_key="doc_md")
    set_current_run("p2m_both", base_dir=str(tmp_path))
    try:
        asyncio.run(PageToMarkdownHandler().execute(ctx))
    finally:
        clear_current_run()
    mem = ctx.workflow_memory["doc_md"]
    assert Path(mem["markdown_path"]).suffix == ".md"
    assert Path(mem["chunks_path"]).suffix == ".jsonl"

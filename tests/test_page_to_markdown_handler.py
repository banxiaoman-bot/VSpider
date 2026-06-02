"""Regression tests for the ``page_to_markdown`` agent capability.

Covers:
  - ``VSpiderAction`` schema accepts the new action literal
  - handler is registered in ``ActionRegistry``
  - handler reads the live tab HTML, writes denoised Markdown into
    ``workflow_memory``, and persists a ``markdown_doc`` artifact + manifest
    entry when a run context is active
  - optional ``type_value`` acts as a BM25 focus query
  - missing page raises ``ActionExecutionError``

Uses deterministic stub page / browser objects (no Playwright, no network),
mirroring the stub-frame style of ``test_extract_row_and_tree_check.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from visual_web_agent.actions import ActionContext, ActionExecutionError, ActionRegistry
from visual_web_agent.io_contract.runtime import clear_current_run, set_current_run
from visual_web_agent.page_to_markdown_action import PageToMarkdownHandler
from visual_web_agent.vlm_client import VSpiderAction


SAMPLE = """
<html><head><title>Doc</title><script>var t=1;</script></head>
<body>
<nav class="nav"><a href="/a">Home</a><a href="/b">About</a></nav>
<article>
  <h1>Main Title</h1>
  <p>This is the first real paragraph with plenty of words for the body.</p>
  <p>Weather coastline breeze sunny day by the quiet shore.</p>
</article>
<footer>footer junk</footer>
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


def _make_ctx(html: str, url: str, *, type_value: str = "", memory_key: str = "") -> ActionContext:
    action = VSpiderAction(
        action="page_to_markdown",
        target_id=0,
        type_value=type_value,
        memory_key=memory_key,
    )
    return ActionContext(
        action=action,
        browser=_StubBrowser(),
        workflow_memory={},
        page=_StubPage(html, url),
    )


class TestSchemaAndRegistration:
    def test_schema_accepts_action(self) -> None:
        v = VSpiderAction(action="page_to_markdown", target_id=0, type_value="", memory_key="")
        assert v.action == "page_to_markdown"

    def test_handler_registered(self) -> None:
        assert ActionRegistry.is_registered("page_to_markdown")
        assert isinstance(ActionRegistry.get("page_to_markdown"), PageToMarkdownHandler)


class TestHandlerBehavior:
    def test_writes_markdown_into_memory(self) -> None:
        ctx = _make_ctx(SAMPLE, "https://example.com", memory_key="doc_md")
        asyncio.run(PageToMarkdownHandler().execute(ctx))
        mem = ctx.workflow_memory["doc_md"]
        assert "# Main Title" in mem["markdown"]
        assert "footer junk" not in mem["markdown"]
        assert mem["word_count"] > 0
        assert mem["source_url"] == "https://example.com"

    def test_default_memory_key(self) -> None:
        ctx = _make_ctx(SAMPLE, "https://example.com")
        asyncio.run(PageToMarkdownHandler().execute(ctx))
        assert "page_markdown" in ctx.workflow_memory

    def test_records_rpa_trail(self) -> None:
        ctx = _make_ctx(SAMPLE, "https://example.com")
        asyncio.run(PageToMarkdownHandler().execute(ctx))
        trail = ctx.browser.rpa_trail
        assert trail and trail[-1]["action"] == "page_to_markdown"

    def test_query_focus_filters(self) -> None:
        ctx = _make_ctx(SAMPLE, "https://example.com", type_value="paragraph body words")
        asyncio.run(PageToMarkdownHandler().execute(ctx))
        md = ctx.workflow_memory["page_markdown"]["markdown"]
        assert "first real paragraph" in md
        assert "coastline" not in md

    def test_persists_artifact_when_run_active(self, tmp_path: Path) -> None:
        ctx = _make_ctx(SAMPLE, "https://example.com", memory_key="doc_md")
        set_current_run("p2m_test", base_dir=str(tmp_path))
        try:
            asyncio.run(PageToMarkdownHandler().execute(ctx))
        finally:
            clear_current_run()
        path = ctx.workflow_memory["doc_md"]["markdown_path"]
        assert path
        artifact = Path(path)
        assert artifact.exists()
        assert artifact.suffix == ".md"
        assert "# Main Title" in artifact.read_text(encoding="utf-8")

    def test_missing_page_raises(self) -> None:
        action = VSpiderAction(action="page_to_markdown", target_id=0, type_value="", memory_key="")
        ctx = ActionContext(action=action, browser=_StubBrowser(), workflow_memory={}, page=None)
        with pytest.raises(ActionExecutionError):
            asyncio.run(PageToMarkdownHandler().execute(ctx))

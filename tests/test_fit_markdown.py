"""Regression tests for the Fit Markdown engine (crawl4ai borrow-point #1).

``extraction_engine.fit_markdown.html_to_fit_markdown`` turns a full HTML page
into LLM-friendly Markdown:

* boilerplate denoise (nav / header / footer / aside / script / style / ads)
* density-based pruning of residual link-farm blocks
* heading / paragraph / list mapping to Markdown
* numbered link references resolved against ``base_url``
* optional BM25 query filtering to keep only relevant blocks

All cases use deterministic stub HTML (no network, no browser).
"""

from __future__ import annotations

import pytest

from visual_web_agent.extraction_engine.fit_markdown import (
    FitMarkdownResult,
    html_to_fit_markdown,
)


SAMPLE = """
<html><head><title>Doc</title>
<style>.x{color:red}</style>
<script>var tracker=1;</script>
</head>
<body>
<nav class="nav"><a href="/a">Home</a><a href="/b">About</a><a href="/c">Contact</a></nav>
<header>site masthead junk</header>
<article>
  <h1>Main Title</h1>
  <p>This is the first real paragraph with enough words to be considered the main content body.</p>
  <p>Second paragraph has meaningful text and a <a href="/docs">documentation link</a> inside it.</p>
  <ul><li>Alpha point</li><li>Beta point</li></ul>
</article>
<aside class="sidebar"><a href="/x">x</a><a href="/y">y</a></aside>
<footer>footer disclaimer junk</footer>
</body></html>
"""


class TestDenoise:
    def test_removes_script_and_style(self) -> None:
        res = html_to_fit_markdown(SAMPLE, base_url="https://example.com")
        assert "var tracker" not in res.markdown
        assert "color:red" not in res.markdown

    def test_removes_nav_header_footer_aside(self) -> None:
        res = html_to_fit_markdown(SAMPLE, base_url="https://example.com")
        assert "site masthead junk" not in res.markdown
        assert "footer disclaimer junk" not in res.markdown
        # nav / aside link texts should be denoised away
        assert "About" not in res.markdown
        assert "Contact" not in res.markdown

    def test_keeps_main_content(self) -> None:
        res = html_to_fit_markdown(SAMPLE, base_url="https://example.com")
        assert "first real paragraph" in res.markdown
        assert "meaningful text" in res.markdown


class TestStructureMapping:
    def test_heading_mapped_to_hash(self) -> None:
        res = html_to_fit_markdown(SAMPLE, base_url="https://example.com")
        assert "# Main Title" in res.markdown

    def test_list_items_mapped_to_dashes(self) -> None:
        res = html_to_fit_markdown(SAMPLE, base_url="https://example.com")
        assert "- Alpha point" in res.markdown
        assert "- Beta point" in res.markdown


class TestLinkReferences:
    def test_inline_link_gets_numbered_marker(self) -> None:
        res = html_to_fit_markdown(SAMPLE, base_url="https://example.com")
        assert "documentation link [1]" in res.markdown

    def test_reference_section_resolves_relative_url(self) -> None:
        res = html_to_fit_markdown(SAMPLE, base_url="https://example.com")
        assert "[1]: https://example.com/docs" in res.markdown

    def test_links_payload_structured(self) -> None:
        res = html_to_fit_markdown(SAMPLE, base_url="https://example.com")
        assert res.links
        first = res.links[0]
        assert first["index"] == 1
        assert first["url"] == "https://example.com/docs"
        assert first["text"] == "documentation link"


QUERY_SAMPLE = """
<article>
<p>Python is a programming language widely used for data science and machine learning workflows.</p>
<p>The weather today is sunny with a gentle breeze drifting along the quiet coastline.</p>
</article>
"""


class TestQueryFilter:
    def test_query_keeps_relevant_drops_irrelevant(self) -> None:
        res = html_to_fit_markdown(
            QUERY_SAMPLE, base_url="https://example.com", query="python programming language"
        )
        assert "programming language" in res.markdown
        assert "coastline" not in res.markdown

    def test_no_query_keeps_everything(self) -> None:
        res = html_to_fit_markdown(QUERY_SAMPLE, base_url="https://example.com")
        assert "programming language" in res.markdown
        assert "coastline" in res.markdown


class TestEdgeCases:
    def test_empty_html_no_crash(self) -> None:
        res = html_to_fit_markdown("", base_url="https://example.com")
        assert isinstance(res, FitMarkdownResult)
        assert res.markdown.strip() == ""
        assert res.links == []

    def test_none_html_no_crash(self) -> None:
        res = html_to_fit_markdown(None, base_url="https://example.com")  # type: ignore[arg-type]
        assert res.markdown.strip() == ""

    def test_word_count_positive_for_content(self) -> None:
        res = html_to_fit_markdown(SAMPLE, base_url="https://example.com")
        assert res.word_count > 0

"""Slice FITMD-2: markdown chunking + BM25 relevance filtering.

Borrowed from crawl4ai's chunking strategies, re-implemented with stdlib only
and reusing fit_markdown's BM25 so a fit-markdown page can be split into
RAG-ready chunks and ranked/filtered against a query. Pure functions, no
network — fully stub-testable.
"""

from __future__ import annotations

from visual_web_agent.extraction_engine.chunking import (
    Chunk,
    chunk_markdown,
    filter_chunks,
    rank_chunks,
    score_chunks,
)


HEADING_DOC = """Intro paragraph before any heading.

# Title

Body of title section.

## Section A

Para a1.

Para a2.

## Section B

Para b1.
"""


def test_heading_strategy_splits_into_sections() -> None:
    chunks = chunk_markdown(HEADING_DOC, strategy="heading")
    assert [c.heading for c in chunks] == ["", "Title", "Section A", "Section B"]
    assert [c.index for c in chunks] == [0, 1, 2, 3]
    assert "Para a1." in chunks[2].text
    assert "Para a2." in chunks[2].text


def test_heading_strategy_splits_oversized_section_keeping_heading() -> None:
    doc = "## S\n\nalpha beta\n\ngamma delta"
    chunks = chunk_markdown(doc, strategy="heading", max_words=3, overlap=0)
    assert len(chunks) == 2
    assert all(c.heading == "S" for c in chunks)
    assert chunks[0].text == "## S\n\nalpha beta"
    assert chunks[1].text == "gamma delta"


def test_window_strategy_packs_to_word_budget() -> None:
    doc = "one two three\n\nfour five six\n\nseven eight nine"
    chunks = chunk_markdown(doc, strategy="window", max_words=3, overlap=0)
    assert [c.text for c in chunks] == [
        "one two three",
        "four five six",
        "seven eight nine",
    ]


def test_window_strategy_overlap_repeats_tail_words() -> None:
    doc = "a b c d e f g h"
    chunks = chunk_markdown(doc, strategy="window", max_words=4, overlap=2)
    assert [c.text for c in chunks] == ["a b c d", "c d e f", "e f g h"]


def test_paragraph_strategy_one_chunk_each() -> None:
    doc = "first para.\n\nsecond para.\n\nthird para."
    chunks = chunk_markdown(doc, strategy="paragraph")
    assert [c.text for c in chunks] == ["first para.", "second para.", "third para."]


def test_min_words_drops_tiny_chunks_and_reindexes() -> None:
    doc = "hi\n\nthis paragraph has several words in it"
    chunks = chunk_markdown(doc, strategy="paragraph", min_words=3)
    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].word_count >= 3


def test_empty_markdown_yields_no_chunks() -> None:
    assert chunk_markdown("", strategy="heading") == []
    assert chunk_markdown("   \n\n   ", strategy="window") == []


TOPIC_DOC = """# Python

Python is a high level programming language for scripting.

# Cooking

Recipes for soup and fresh bread.
"""


def test_score_chunks_ranks_relevant_higher() -> None:
    chunks = chunk_markdown(TOPIC_DOC, strategy="heading")
    scores = score_chunks(chunks, "python programming language")
    assert len(scores) == 2
    assert scores[0] > scores[1]


def test_rank_chunks_orders_by_relevance_with_top_k() -> None:
    chunks = chunk_markdown(TOPIC_DOC, strategy="heading")
    ranked = rank_chunks(chunks, "python programming", top_k=1)
    assert len(ranked) == 1
    assert ranked[0].heading == "Python"


def test_filter_chunks_drops_irrelevant() -> None:
    chunks = chunk_markdown(TOPIC_DOC, strategy="heading")
    kept = filter_chunks(chunks, "python programming")
    assert len(kept) == 1
    assert kept[0].heading == "Python"


def test_filter_chunks_no_query_returns_all() -> None:
    chunks = chunk_markdown(TOPIC_DOC, strategy="heading")
    assert filter_chunks(chunks, "") == chunks

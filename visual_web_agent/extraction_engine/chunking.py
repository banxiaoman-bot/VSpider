"""Markdown chunking + BM25 relevance filtering (crawl4ai borrow).

Splits :mod:`fit_markdown` output into RAG-ready chunks and optionally ranks /
filters them against a query using the *same* stdlib BM25 as ``fit_markdown``
(reused, not re-implemented, so scoring never drifts between the two). Pure
functions, zero external deps, fully stub-testable.

Strategies:

- ``heading`` (default) — split at Markdown heading lines (``#``..``######``);
  each section is its heading + following paragraphs, further window-split when
  it exceeds ``max_words``. Text before the first heading is its own section.
- ``window`` — ignore headings; greedily pack paragraphs into ``max_words``
  chunks with an optional ``overlap`` (in words) between neighbours.
- ``paragraph`` — one chunk per blank-line-separated paragraph.

Chunks below ``min_words`` are dropped and the survivors re-indexed from 0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from visual_web_agent.extraction_engine.fit_markdown import _bm25_scores, _tokenize

__all__ = [
    "Chunk",
    "chunk_markdown",
    "score_chunks",
    "rank_chunks",
    "filter_chunks",
]

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}\s")


@dataclass
class Chunk:
    text: str
    index: int
    word_count: int
    heading: str = ""


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(str(text or "")))


def _paragraphs(markdown: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", str(markdown or "")) if p.strip()]


def _hard_split(text: str, max_words: int, overlap: int) -> list[str]:
    words = text.split()
    if len(words) <= max_words:
        return [text]
    step = max(1, max_words - overlap)
    out: list[str] = []
    i = 0
    while i < len(words):
        out.append(" ".join(words[i : i + max_words]))
        if i + max_words >= len(words):
            break
        i += step
    return out


def _pack(paragraphs: list[str], max_words: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    cur: list[str] = []
    cur_wc = 0
    for para in paragraphs:
        wc = _word_count(para)
        if wc > max_words:
            if cur:
                chunks.append("\n\n".join(cur))
                cur, cur_wc = [], 0
            chunks.extend(_hard_split(para, max_words, overlap))
            continue
        if cur and cur_wc + wc > max_words:
            chunks.append("\n\n".join(cur))
            if overlap > 0:
                carried: list[str] = []
                cwc = 0
                for prev in reversed(cur):
                    pc = _word_count(prev)
                    if cwc + pc > overlap:
                        break
                    carried.insert(0, prev)
                    cwc += pc
                cur, cur_wc = carried, cwc
            else:
                cur, cur_wc = [], 0
        cur.append(para)
        cur_wc += wc
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def _sections(paragraphs: list[str]) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    head = ""
    cur: list[str] = []
    for para in paragraphs:
        first_line = para.split("\n", 1)[0]
        if _HEADING_LINE.match(first_line):
            if cur:
                sections.append((head, cur))
            head = first_line.lstrip().lstrip("#").strip()
            cur = [para]
        else:
            cur.append(para)
    if cur:
        sections.append((head, cur))
    return sections


def chunk_markdown(
    markdown: str,
    *,
    strategy: str = "heading",
    max_words: int = 300,
    overlap: int = 0,
    min_words: int = 1,
) -> list[Chunk]:
    """Split ``markdown`` into :class:`Chunk` objects per ``strategy``."""
    max_words = max(1, int(max_words))
    overlap = max(0, min(int(overlap), max_words - 1))
    paragraphs = _paragraphs(markdown)

    raw: list[tuple[str, str]] = []
    strat = str(strategy or "heading").strip().lower()
    if strat == "paragraph":
        raw = [(p, "") for p in paragraphs]
    elif strat == "window":
        raw = [(t, "") for t in _pack(paragraphs, max_words, overlap)]
    else:  # heading (default)
        for head, sec_paras in _sections(paragraphs):
            for text in _pack(sec_paras, max_words, overlap):
                raw.append((text, head))

    chunks: list[Chunk] = []
    for text, head in raw:
        wc = _word_count(text)
        if wc < min_words:
            continue
        chunks.append(Chunk(text=text, index=len(chunks), word_count=wc, heading=head))
    return chunks


def score_chunks(chunks: list[Chunk], query: str) -> list[float]:
    """BM25 score of every chunk against ``query`` (0.0 when query is empty)."""
    docs = [_tokenize(c.text) for c in chunks]
    return _bm25_scores(docs, _tokenize(query))


def rank_chunks(chunks: list[Chunk], query: str, *, top_k: int | None = None) -> list[Chunk]:
    """Chunks sorted by descending relevance (stable on ties); cut to ``top_k``."""
    scores = score_chunks(chunks, query)
    order = sorted(range(len(chunks)), key=lambda i: (-scores[i], i))
    ranked = [chunks[i] for i in order]
    return ranked[:top_k] if top_k else ranked


def filter_chunks(
    chunks: list[Chunk],
    query: str,
    *,
    threshold: float = 0.0,
    top_k: int | None = None,
) -> list[Chunk]:
    """Keep chunks scoring above ``threshold`` for ``query`` (document order).

    An empty / blank ``query`` is a no-op (returns all chunks). ``top_k`` keeps
    only the highest-scoring survivors, restored to document order.
    """
    if not str(query or "").strip():
        return list(chunks)
    scores = score_chunks(chunks, query)
    survivors = [(c, s) for c, s in zip(chunks, scores) if s > threshold]
    if top_k:
        survivors.sort(key=lambda cs: -cs[1])
        survivors = survivors[:top_k]
        survivors.sort(key=lambda cs: cs[0].index)
    return [c for c, _ in survivors]

"""Crawl frontier strategies for ``spider_lite`` (BFS default + opt-in best-first).

Borrowed (re-implemented, no external deps) from crawl4ai's best-first deep
crawl idea. The legacy crawl is a FIFO BFS over a ``deque``; this module keeps
that behaviour byte-identical as :class:`BFSFrontier` and adds an opt-in
:class:`BestFirstFrontier` that pops the most *relevant* link first so the
crawler reaches on-topic pages in fewer rounds (mission §一 "高效 / 最少回合 /
智能").

Relevance is a deterministic keyword score over the URL (path + query tokens)
plus optional anchor text — pure functions with no network / browser, so the
whole module is stub-testable.

Frontier protocol (duck-typed, used by ``spider_lite.run``):

    push(url: str, depth: int, *, anchor_text: str = "") -> None
    pop() -> tuple[str, int]
    __len__() -> int
"""

from __future__ import annotations

import itertools
import re
from collections import deque
from heapq import heappop, heappush
from typing import Iterable
from urllib.parse import urlsplit

__all__ = [
    "BFS",
    "BEST_FIRST",
    "normalize_keywords",
    "score_url",
    "BFSFrontier",
    "BestFirstFrontier",
    "build_frontier",
]

BFS = "bfs"
BEST_FIRST = "best_first"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text or "").lower())


def normalize_keywords(value: object) -> list[str]:
    """Coerce ``str`` / iterable input into a de-duped, lowercased token list.

    ``"Python Tutorial"`` and ``["python", "tutorial"]`` both normalize to
    ``["python", "tutorial"]``. Non-iterables fall back to ``str(value)``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw = _tokenize(value)
    elif isinstance(value, (list, tuple, set)):
        raw = []
        for item in value:
            raw.extend(_tokenize(item if isinstance(item, str) else str(item)))
    else:
        raw = _tokenize(str(value))
    out: list[str] = []
    for token in raw:
        if token and token not in out:
            out.append(token)
    return out


def score_url(url: str, keywords: object, *, anchor_text: str = "") -> float:
    """Relevance score of a candidate link given the crawl ``keywords``.

    Counts distinct keyword hits in the URL path + query tokens (weight 1.0)
    and the anchor text (weight 0.5, since human-readable but noisier). Pure
    and deterministic; returns ``0.0`` when there are no keywords / no matches.
    """
    kw = keywords if isinstance(keywords, (list, tuple, set)) else normalize_keywords(keywords)
    kw_set = {str(k).lower() for k in kw if k}
    if not kw_set:
        return 0.0
    parts = urlsplit(str(url or ""))
    url_tokens = set(_tokenize(parts.path)) | set(_tokenize(parts.query))
    anchor_tokens = set(_tokenize(anchor_text))
    score = 0.0
    for keyword in kw_set:
        if keyword in url_tokens:
            score += 1.0
        if keyword in anchor_tokens:
            score += 0.5
    return score


class BFSFrontier:
    """FIFO frontier — behaviour-identical to the legacy ``deque`` crawl.

    ``push`` appends, ``pop`` removes from the front; ``anchor_text`` is
    accepted (for a uniform protocol) but ignored.
    """

    def __init__(self, seeds: Iterable[str] = ()) -> None:
        self._queue: deque[tuple[str, int]] = deque((url, 0) for url in seeds)

    def push(self, url: str, depth: int, *, anchor_text: str = "") -> None:
        self._queue.append((url, depth))

    def pop(self) -> tuple[str, int]:
        return self._queue.popleft()

    def __len__(self) -> int:
        return len(self._queue)

    def snapshot(self) -> list[dict]:
        """Pending ``[{"url", "depth"}]`` in FIFO order (for resume_state)."""
        return [{"url": url, "depth": depth} for url, depth in self._queue]


class BestFirstFrontier:
    """Max-heap frontier: highest relevance first.

    Tie-break order is (1) higher score, (2) shallower depth, (3) insertion
    order (stable FIFO) — so with no/zero-scoring keywords it gracefully
    degrades to a depth-first-then-FIFO traversal rather than crashing.
    """

    def __init__(self, seeds: Iterable[str] = (), *, keywords: object = ()) -> None:
        self.keywords = normalize_keywords(keywords)
        self._heap: list[tuple[float, int, int, str]] = []
        self._counter = itertools.count()
        for url in seeds:
            self.push(url, 0)

    def push(self, url: str, depth: int, *, anchor_text: str = "") -> None:
        score = score_url(url, self.keywords, anchor_text=anchor_text)
        heappush(self._heap, (-score, int(depth), next(self._counter), str(url)))

    def pop(self) -> tuple[str, int]:
        _neg_score, depth, _seq, url = heappop(self._heap)
        return url, depth

    def __len__(self) -> int:
        return len(self._heap)

    def snapshot(self) -> list[dict]:
        """Pending ``[{"url", "depth"}]`` (heap order; re-scored on restore)."""
        return [{"url": url, "depth": depth} for _neg, depth, _seq, url in self._heap]


def build_frontier(
    strategy: str,
    *,
    keywords: object = (),
    seeds: Iterable[str] = (),
    pending: Iterable[dict] = (),
):
    """Factory: return the frontier for ``strategy`` seeded at depth 0.

    Unknown strategies fall back to :class:`BFSFrontier` so callers never get
    a surprise crawl-ordering change from a typo. ``pending`` restores a
    snapshot (``[{"url", "depth"}]`` from :meth:`snapshot`) after the seeds —
    used by ``spider_lite`` resume_state to rebuild an interrupted frontier.
    """
    strat = str(strategy or BFS).strip().lower()
    if strat == BEST_FIRST:
        frontier: BFSFrontier | BestFirstFrontier = BestFirstFrontier(seeds, keywords=keywords)
    else:
        frontier = BFSFrontier(seeds)
    for item in pending or ():
        url = str(item.get("url") or "").strip()
        if url:
            frontier.push(url, int(item.get("depth") or 0))
    return frontier

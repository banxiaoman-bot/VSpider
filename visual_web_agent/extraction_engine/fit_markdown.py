"""Fit Markdown engine — full HTML page → LLM-friendly Markdown.

Borrowed (re-implemented, no external deps) from crawl4ai's "fit markdown"
idea, adapted to VSpider's stdlib-only extraction style (mirrors the
``html.parser`` tree used in ``extraction_engine/generic.py``).

Pipeline:

1. **Parse** HTML into a lightweight node tree.
2. **Denoise** — drop non-content / boilerplate subtrees by tag
   (``script``/``style``/``nav``/``header``/``footer``/``aside``/...) and by
   ``class`` / ``id`` / ``role`` keyword (``sidebar``/``cookie``/``ad``/...).
3. **Prune** — density filter that drops residual short link-farm blocks
   (high link density + few words).
4. **Render** — walk in document order emitting Markdown: headings ``#``,
   paragraphs, lists, blockquotes, code, and inline emphasis. Links become
   numbered markers ``text [n]`` with a trailing reference section; relative
   URLs are resolved against ``base_url``.
5. **Query filter (optional)** — BM25 over body blocks keeps only blocks
   relevant to ``query`` (headings / references always retained).

The public entry point :func:`html_to_fit_markdown` is a pure function with no
network / browser dependency, so it is fully stub-testable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Whole subtree dropped: non-content + common boilerplate containers.
_DROP_TAGS = {
    "head", "title", "meta", "base", "link", "script", "style", "noscript",
    "template", "svg", "canvas", "iframe", "object", "embed", "audio", "video",
    "nav", "header", "footer", "aside", "form", "button", "select", "input",
    "textarea", "dialog", "menu",
}

# Treated as inline during rendering (do not force a block recurse).
_INLINE_TAGS = {
    "a", "span", "strong", "b", "em", "i", "code", "small", "sub", "sup",
    "mark", "u", "abbr", "time", "cite", "q", "s", "kbd", "var", "br", "img",
    "label", "del", "ins", "bdi", "bdo", "wbr",
}

_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

# class / id / role tokens that mark a subtree as boilerplate.
_BOILERPLATE_WORDS = {
    "nav", "navbar", "navigation", "menu", "sidebar", "footer", "header",
    "masthead", "comment", "comments", "cookie", "cookies", "consent", "gdpr",
    "banner", "advert", "advertisement", "ad", "ads", "promo", "sponsor",
    "social", "share", "sharing", "related", "breadcrumb", "breadcrumbs",
    "pagination", "pager", "widget", "popup", "modal", "overlay", "newsletter",
    "subscribe", "signup", "toolbar", "skip", "skiplink", "offcanvas",
}

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass
class FitMarkdownResult:
    markdown: str
    links: list[dict] = field(default_factory=list)
    word_count: int = 0
    kept_blocks: int = 0
    removed_blocks: int = 0
    query: str = ""


class _Node:
    __slots__ = ("tag", "attrs", "parent", "children")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None, parent: "_Node | None" = None) -> None:
        self.tag = str(tag or "").lower()
        self.attrs = attrs or {}
        self.parent = parent
        self.children: list[object] = []


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if node.tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag_l = str(tag or "").lower()
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == tag_l:
                del self.stack[idx:]
                break


def _parse(html: str) -> _Node:
    parser = _TreeParser()
    parser.feed(str(html or ""))
    return parser.root


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or ""))


def _text_of(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        else:
            parts.append(_text_of(child))
    return _norm_ws("".join(parts)).strip()


def _link_text_of(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            continue
        if child.tag == "a":
            parts.append(_text_of(child))
        else:
            parts.append(_link_text_of(child))
    return _norm_ws(" ".join(p for p in parts if p)).strip()


def _is_boilerplate(node: _Node) -> bool:
    if node.tag in _DROP_TAGS:
        return True
    raw = " ".join(
        [node.attrs.get("class", ""), node.attrs.get("id", ""), node.attrs.get("role", "")]
    ).lower()
    if not raw.strip():
        return False
    tokens = set(re.split(r"[^a-z0-9]+", raw)) - {""}
    return bool(tokens & _BOILERPLATE_WORDS)


def _density_drop(node: _Node) -> bool:
    if node.tag in _INLINE_TAGS or node.tag in _HEADING_TAGS or node.tag in {"ul", "ol", "li", "table"}:
        return False
    text = _text_of(node)
    word_count = len(_WORD_RE.findall(text))
    if word_count == 0:
        return False
    link_len = len(_link_text_of(node))
    density = link_len / max(len(text), 1)
    return density >= 0.6 and word_count < 12


def _denoise(node: _Node, prune: bool) -> int:
    removed = 0
    kept: list[object] = []
    for child in node.children:
        if isinstance(child, str):
            kept.append(child)
            continue
        if _is_boilerplate(child):
            removed += 1
            continue
        removed += _denoise(child, prune)
        if prune and _density_drop(child):
            removed += 1
            continue
        kept.append(child)
    node.children = kept
    return removed


class _Ctx:
    def __init__(self, base_url: str) -> None:
        self.base = base_url or ""
        self.links: list[dict] = []
        self._by_url: dict[str, int] = {}

    def add_link(self, text: str, href: str) -> int:
        href = (href or "").strip()
        if not href or href.lower().startswith(("javascript:", "data:", "#")):
            return 0
        url = urljoin(self.base, href) if self.base else href
        if url in self._by_url:
            return self._by_url[url]
        idx = len(self.links) + 1
        self.links.append({"index": idx, "url": url, "text": text})
        self._by_url[url] = idx
        return idx


def _inline(node: _Node, ctx: _Ctx) -> str:
    out: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            out.append(_norm_ws(child))
            continue
        tag = child.tag
        if tag in _HEADING_TAGS or tag not in _INLINE_TAGS:
            # block-level descendant: not part of this inline run
            continue
        if tag == "a":
            text = _inline(child, ctx).strip()
            href = child.attrs.get("href", "")
            idx = ctx.add_link(text or href, href)
            if idx:
                out.append(f"{text or href} [{idx}]")
            elif text:
                out.append(text)
        elif tag in {"strong", "b"}:
            inner = _inline(child, ctx).strip()
            if inner:
                out.append(f"**{inner}**")
        elif tag in {"em", "i"}:
            inner = _inline(child, ctx).strip()
            if inner:
                out.append(f"*{inner}*")
        elif tag in {"code", "kbd", "var"}:
            inner = _inline(child, ctx).strip()
            if inner:
                out.append(f"`{inner}`")
        elif tag == "br":
            out.append("\n")
        elif tag == "img":
            continue
        else:
            out.append(_inline(child, ctx))
    return "".join(out)


def _render_list(node: _Node, ctx: _Ctx, ordered: bool, indent: int = 0) -> list[str]:
    lines: list[str] = []
    n = 1
    for child in node.children:
        if isinstance(child, str) or child.tag != "li":
            continue
        marker = f"{n}." if ordered else "-"
        text = _inline(child, ctx).strip()
        lines.append(("  " * indent + f"{marker} {text}").rstrip())
        for sub in child.children:
            if not isinstance(sub, str) and sub.tag in {"ul", "ol"}:
                lines.extend(_render_list(sub, ctx, sub.tag == "ol", indent + 1))
        n += 1
    return lines


def _has_block_child(node: _Node) -> bool:
    return any(
        (not isinstance(c, str)) and (c.tag in _HEADING_TAGS or c.tag not in _INLINE_TAGS)
        for c in node.children
    )


def _walk_blocks(node: _Node, ctx: _Ctx) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for child in node.children:
        if isinstance(child, str):
            text = _norm_ws(child).strip()
            if text:
                blocks.append(("body", text))
            continue
        tag = child.tag
        if tag in _HEADING_TAGS:
            text = _inline(child, ctx).strip()
            if text:
                blocks.append(("heading", "#" * _HEADING_TAGS[tag] + " " + text))
        elif tag == "p":
            text = _inline(child, ctx).strip()
            if text:
                blocks.append(("body", text))
        elif tag in {"ul", "ol"}:
            lines = _render_list(child, ctx, ordered=(tag == "ol"))
            if lines:
                blocks.append(("body", "\n".join(lines)))
        elif tag == "blockquote":
            for _, inner in _walk_blocks(child, ctx):
                quoted = "\n".join("> " + line for line in inner.split("\n"))
                blocks.append(("body", quoted))
        elif tag == "pre":
            code = _text_of(child)
            if code:
                blocks.append(("body", "```\n" + code + "\n```"))
        elif tag in {"hr"}:
            blocks.append(("body", "---"))
        elif tag in _INLINE_TAGS:
            continue
        else:
            if _has_block_child(child):
                blocks.extend(_walk_blocks(child, ctx))
            else:
                text = _inline(child, ctx).strip()
                if text:
                    blocks.append(("body", text))
    return blocks


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text or "").lower())


def _bm25_scores(
    docs: list[list[str]], query: list[str], *, k1: float = 1.5, b: float = 0.75
) -> list[float]:
    total = len(docs)
    if total == 0:
        return []
    avgdl = sum(len(d) for d in docs) / total or 1.0
    df: dict[str, int] = {}
    for doc in docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1
    q_terms = set(query)
    scores: list[float] = []
    for doc in docs:
        dl = len(doc) or 1
        freq: dict[str, int] = {}
        for word in doc:
            freq[word] = freq.get(word, 0) + 1
        score = 0.0
        for term in q_terms:
            tf = freq.get(term, 0)
            if not tf:
                continue
            n = df.get(term, 0)
            idf = math.log(1 + (total - n + 0.5) / (n + 0.5))
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
        scores.append(score)
    return scores


def html_to_fit_markdown(
    html: str,
    *,
    base_url: str = "",
    query: str = "",
    prune: bool = True,
    bm25_threshold: float = 0.0,
) -> FitMarkdownResult:
    """Convert ``html`` to denoised, LLM-friendly Markdown.

    Args:
        html: Raw page HTML (``None`` / empty is tolerated).
        base_url: Used to resolve relative link hrefs in the reference section.
        query: When non-empty, BM25-filters body blocks to those relevant.
        prune: Enable density-based pruning of residual link-farm blocks.
        bm25_threshold: Minimum BM25 score for a body block to survive ``query``.
    """
    root = _parse(html)
    removed = _denoise(root, prune)
    ctx = _Ctx(base_url)
    blocks = _walk_blocks(root, ctx)

    if ctx.links:
        ref_lines = ["## References"] + [f"[{l['index']}]: {l['url']}" for l in ctx.links]
        blocks.append(("refs", "\n".join(ref_lines)))

    if query.strip():
        body_positions = [i for i, (kind, _) in enumerate(blocks) if kind == "body"]
        body_docs = [_tokenize(blocks[i][1]) for i in body_positions]
        scores = _bm25_scores(body_docs, _tokenize(query))
        keep: set[int] = set()
        for rank, pos in enumerate(body_positions):
            if scores[rank] > bm25_threshold:
                keep.add(pos)
            else:
                removed += 1
        blocks = [b for i, b in enumerate(blocks) if b[0] != "body" or i in keep]

    texts = [text for _, text in blocks if text.strip()]
    markdown = ("\n\n".join(texts).strip() + "\n") if texts else ""
    word_count = sum(
        len(_WORD_RE.findall(text)) for kind, text in blocks if kind in {"heading", "body"}
    )
    kept_blocks = sum(1 for kind, _ in blocks if kind in {"heading", "body"})
    return FitMarkdownResult(
        markdown=markdown,
        links=ctx.links,
        word_count=word_count,
        kept_blocks=kept_blocks,
        removed_blocks=removed,
        query=query,
    )

"""Collect :class:`MediaCandidate` records from HTML or DOM snapshots.

Pure-Python HTML walker (no BeautifulSoup, no Playwright). The collector
recognises ``<img>``, ``<picture>/<source>``, ``<video>``, ``<audio>``,
``<source>`` inside media, and ``<a download>`` / ``<a href="*.pdf|zip|...">``.

URL classification is conservative: when the suffix is ambiguous we tag
``file_generic`` and let the downloader's response Content-Type pin down
the real kind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse


MEDIA_KINDS = (
    "media_image",
    "media_video",
    "media_audio",
    "media_pdf",
    "media_archive",
    "file_generic",
)


_IMAGE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".tiff",
)
_VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v")
_AUDIO_SUFFIXES = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac")
_ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".tgz")
_DOCUMENT_SUFFIXES = (
    ".pdf",
    ".doc", ".docx", ".docm",
    ".xls", ".xlsx", ".xlsm",
    ".ppt", ".pptx", ".pptm",
    ".odt", ".ods", ".odp",
)


_IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "image/bmp", "image/svg+xml", "image/avif", "image/tiff",
}
_VIDEO_MIMES = {
    "video/mp4", "video/quicktime", "video/x-matroska", "video/webm",
    "video/x-msvideo", "video/x-flv",
}
_AUDIO_MIMES = {
    "audio/mpeg", "audio/wav", "audio/ogg", "audio/flac",
    "audio/x-m4a", "audio/aac",
}
_ARCHIVE_MIMES = {
    "application/zip", "application/vnd.rar", "application/x-7z-compressed",
    "application/x-tar", "application/gzip",
}
_PDF_MIMES = {"application/pdf"}


_TRACKING_QUERY_KEYS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "yclid", "_ga",
})


@dataclass
class MediaCandidate:
    """One downloadable media reference discovered on a page."""

    url: str
    kind: str = "file_generic"
    referrer: str = ""
    alt: str = ""
    mime_hint: str = ""
    selector: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind if self.kind in MEDIA_KINDS else "file_generic",
            "referrer": self.referrer,
            "alt": self.alt,
            "mime_hint": self.mime_hint,
            "selector": self.selector,
            "extra": dict(self.extra or {}),
        }


def classify_url(url: str, *, mime_hint: str = "") -> str:
    """Return one of ``MEDIA_KINDS`` for ``url``."""

    mime = (mime_hint or "").lower().split(";", 1)[0].strip()
    if mime in _IMAGE_MIMES or mime.startswith("image/"):
        return "media_image"
    if mime in _VIDEO_MIMES or mime.startswith("video/"):
        return "media_video"
    if mime in _AUDIO_MIMES or mime.startswith("audio/"):
        return "media_audio"
    if mime in _PDF_MIMES:
        return "media_pdf"
    if mime in _ARCHIVE_MIMES:
        return "media_archive"

    path = urlparse(url or "").path.lower()
    if path.endswith(_IMAGE_SUFFIXES):
        return "media_image"
    if path.endswith(_VIDEO_SUFFIXES):
        return "media_video"
    if path.endswith(_AUDIO_SUFFIXES):
        return "media_audio"
    if path.endswith(_ARCHIVE_SUFFIXES):
        return "media_archive"
    if path.endswith(_DOCUMENT_SUFFIXES):
        return "media_pdf" if path.endswith(".pdf") else "file_generic"
    return "file_generic"


def _strip_tracking(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    pairs = []
    for chunk in parsed.query.split("&"):
        if not chunk:
            continue
        key = chunk.split("=", 1)[0]
        if key.lower() in _TRACKING_QUERY_KEYS:
            continue
        pairs.append(chunk)
    cleaned = "&".join(pairs)
    new_parts = parsed._replace(query=cleaned)
    return new_parts.geturl()


def _resolve(base_url: str, raw: str) -> str:
    raw = (raw or "").strip()
    if not raw or raw.startswith(("javascript:", "data:", "#", "mailto:", "tel:")):
        return ""
    try:
        return _strip_tracking(urljoin(base_url, raw))
    except Exception:
        return raw


class _MediaParser(HTMLParser):
    """Collect candidate elements while keeping a parent stack so we can
    attribute ``<source>`` tags to their owning ``<video>`` / ``<audio>``."""

    _MEDIA_PARENTS = {"video", "audio", "picture"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.candidates: list[MediaCandidate] = []
        self._stack: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        self._stack.append((tag, attr_map))
        self._maybe_collect(tag, attr_map)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        self._maybe_collect(tag, attr_map)
        # self-closing - don't push onto stack

    def handle_endtag(self, tag: str) -> None:
        while self._stack:
            top_tag, _ = self._stack.pop()
            if top_tag == tag:
                break

    def _parent_media_kind(self) -> str:
        for tag, _ in reversed(self._stack):
            if tag == "video":
                return "media_video"
            if tag == "audio":
                return "media_audio"
            if tag == "picture":
                return "media_image"
        return ""

    def _maybe_collect(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "img":
            src = _resolve(self.base_url, attrs.get("src") or attrs.get("data-src") or "")
            if not src:
                return
            self.candidates.append(MediaCandidate(
                url=src,
                kind="media_image",
                referrer=self.base_url,
                alt=attrs.get("alt") or "",
                mime_hint="",
                selector="img",
            ))
            srcset = attrs.get("srcset") or ""
            for part in srcset.split(","):
                token = part.strip().split(" ", 1)[0]
                if not token:
                    continue
                resolved = _resolve(self.base_url, token)
                if resolved and resolved != src:
                    self.candidates.append(MediaCandidate(
                        url=resolved,
                        kind="media_image",
                        referrer=self.base_url,
                        alt=attrs.get("alt") or "",
                        mime_hint="",
                        selector="img[srcset]",
                    ))
        elif tag == "video":
            src = _resolve(self.base_url, attrs.get("src") or "")
            if src:
                self.candidates.append(MediaCandidate(
                    url=src,
                    kind="media_video",
                    referrer=self.base_url,
                    alt=attrs.get("title") or "",
                    mime_hint="",
                    selector="video",
                ))
        elif tag == "audio":
            src = _resolve(self.base_url, attrs.get("src") or "")
            if src:
                self.candidates.append(MediaCandidate(
                    url=src,
                    kind="media_audio",
                    referrer=self.base_url,
                    alt=attrs.get("title") or "",
                    mime_hint="",
                    selector="audio",
                ))
        elif tag == "source":
            src = _resolve(self.base_url, attrs.get("src") or "")
            if not src:
                return
            mime_hint = attrs.get("type") or ""
            kind = self._parent_media_kind() or classify_url(src, mime_hint=mime_hint)
            self.candidates.append(MediaCandidate(
                url=src,
                kind=kind,
                referrer=self.base_url,
                alt="",
                mime_hint=mime_hint,
                selector="source",
            ))
        elif tag == "a":
            href = _resolve(self.base_url, attrs.get("href") or "")
            if not href:
                return
            has_download_attr = "download" in attrs
            kind = classify_url(href)
            if not has_download_attr and kind == "file_generic":
                return  # ordinary navigation link, not a media reference
            self.candidates.append(MediaCandidate(
                url=href,
                kind=kind,
                referrer=self.base_url,
                alt=attrs.get("download") or attrs.get("title") or "",
                mime_hint="",
                selector="a",
                extra={"download_attr": has_download_attr},
            ))


def collect_from_html(html: str, base_url: str) -> list[MediaCandidate]:
    """Walk ``html`` and return all media candidates rooted at ``base_url``.

    Empty input returns ``[]``. URLs are absolutised against ``base_url``
    and stripped of common tracking params.
    """

    if not html:
        return []
    parser = _MediaParser(base_url=base_url or "")
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # HTMLParser is forgiving but extremely malformed inputs may raise.
        pass
    return parser.candidates


def dedupe_candidates(candidates: Iterable[MediaCandidate]) -> list[MediaCandidate]:
    """Return candidates with unique ``url`` preserving first-occurrence."""

    seen: set[str] = set()
    out: list[MediaCandidate] = []
    for c in candidates or []:
        if not c or not c.url:
            continue
        if c.url in seen:
            continue
        seen.add(c.url)
        out.append(c)
    return out

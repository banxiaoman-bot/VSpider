"""Generic data-export-URL transformer registry.

Why this exists
===============
Many web apps render tabular data via Canvas / SVG / virtual scrolling, so
the SoM overlay and the AX Tree can't see the actual values. Trying to
extract from the rendered view is fundamentally hopeless — the agent will
spend dozens of steps clicking phantom rows.

But every such app **always** ships an export endpoint:

    Google Sheets  →  /export?format=csv&gid={GID}
    OneDrive .xlsx →  + ?download=1
    SharePoint     →  + ?download=1
    Direct .csv    →  already downloadable, no transform needed

This module is a tiny **registry** that maps any URL → its CSV/Excel
export URL through a list of registered transformers. The agent's runtime
just calls ``find_data_export_url(current_url)``; if a transformer
matches, it gets the export URL back. Adding a new data source = one
``register_export_transform`` call. **No prompt edits, no per-site
branches in the agent loop, no skill renaming.**

Public API
==========

    @dataclass ExportTransform
        name:           "Google Sheets" | "OneDrive" | ...
        host_pattern:   regex matched against urlsplit().hostname
        transform:      Callable[[str], str] returning export URL or ""

    register_export_transform(t: ExportTransform) -> None
    list_registered_hosts() -> list[str]
    find_data_export_url(url: str | None) -> tuple[name, export_url]
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Tuple
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class ExportTransform:
    """A registered transformer mapping URLs of a host family → export URL."""

    name: str
    host_pattern: str
    transform: Callable[[str], str]

    def matches_host(self, host: str) -> bool:
        if not host:
            return False
        return bool(re.search(self.host_pattern, host, re.I))


_REGISTRY: List[ExportTransform] = []


def register_export_transform(t: ExportTransform) -> None:
    """Append a transformer to the registry. Last registered wins on overlap.

    Use ``unregister`` (not exported — call ``_REGISTRY.clear()`` in tests)
    if you need a clean slate for unit tests.
    """
    _REGISTRY.append(t)


def list_registered_hosts() -> List[str]:
    """Diagnostic: list all registered ``host_pattern`` strings."""
    return [t.host_pattern for t in _REGISTRY]


def find_data_export_url(url: str | None) -> Tuple[str, str]:
    """Return ``(transformer_name, export_url)`` for any recognised data URL.

    * No URL or unparseable URL  → ``("", "")``
    * Non-http(s) scheme         → ``("", "")``
    * No registered transformer matches  → ``("", "")``
    * Transformer raises         → ``("", "")``
    * Transformer returns empty (e.g. URL is already an export URL or
      doesn't have the right path shape) → ``("", "")``
    """
    if not url:
        return "", ""
    try:
        parts = urlsplit(str(url))
    except Exception:
        return "", ""
    if (parts.scheme or "").lower() not in ("http", "https"):
        return "", ""
    host = (parts.hostname or "").lower()
    if not host:
        return "", ""
    # Iterate registry in insertion order; the first match wins. Test fixtures
    # can clear/re-register to control priority.
    for t in _REGISTRY:
        if not t.matches_host(host):
            continue
        try:
            export = t.transform(str(url))
        except Exception:
            continue
        if export:
            return t.name, export
    return "", ""


# ════════════════════════════════════════════════════════════════════════════
# Built-in transformers
# ════════════════════════════════════════════════════════════════════════════

# ── Google Sheets ──────────────────────────────────────────────────────────

_SHEETS_PATH_RE = re.compile(
    r"^/spreadsheets/d/(?P<sheet_id>[A-Za-z0-9_-]+)(?:/.*)?$"
)


def _extract_gid(url: str) -> str:
    """Pull ``gid`` from a Sheets URL — query param wins over fragment."""
    parts = urlsplit(url)
    qs = parse_qs(parts.query or "")
    if (gid_values := qs.get("gid")):
        gid = (gid_values[0] or "").strip()
        if gid:
            return gid
    fragment = parts.fragment or ""
    if fragment:
        for chunk in fragment.split("&"):
            if "=" not in chunk:
                continue
            key, _, value = chunk.partition("=")
            if key.strip().lower() == "gid":
                value = value.strip()
                if value:
                    return value
    return ""


def _google_sheets_transform(url: str) -> str:
    """edit / view / preview / htmlview → /export?format=csv&gid={N}.

    Returns ``""`` for already-export URLs (so caller treats as "no transform
    needed") and for URLs that don't match the Sheets path shape.
    """
    parts = urlsplit(url)
    match = _SHEETS_PATH_RE.match(parts.path or "")
    if not match:
        return ""
    sheet_id = match.group("sheet_id")
    if not sheet_id:
        return ""
    if "/export" in (parts.path or "").lower():
        return ""  # already exported
    gid = _extract_gid(url) or "0"
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={gid}"
    )


register_export_transform(
    ExportTransform(
        name="Google Sheets",
        host_pattern=r"^docs\.google\.com$",
        transform=_google_sheets_transform,
    )
)


# ── OneDrive / SharePoint Excel preview → direct download ──────────────────
# Pattern: shared .xlsx / .xlsm / .xls / .csv links open in the Office Online
# preview. Adding ``?download=1`` (or merging it into the existing query)
# forces the browser to download the underlying file instead of rendering
# it. This bypasses the canvas-based preview entirely.

_OFFICE_FILE_EXT = re.compile(r"\.(xlsx|xlsm|xls|csv|tsv|ods)(?:$|[\?#])", re.I)


def _office_online_transform(url: str) -> str:
    """Append/merge ``?download=1`` so the file downloads instead of previews.

    Only fires when the URL points at a known Office file extension —
    avoids touching arbitrary OneDrive/SharePoint navigation pages.
    """
    if not _OFFICE_FILE_EXT.search(url):
        return ""
    parts = urlsplit(url)
    qs = parse_qs(parts.query or "", keep_blank_values=True)
    if "download" in qs:
        return ""  # already requesting download — no transform needed
    new_query = parts.query + ("&" if parts.query else "") + "download=1"
    return f"{parts.scheme}://{parts.netloc}{parts.path}?{new_query}" + (
        f"#{parts.fragment}" if parts.fragment else ""
    )


register_export_transform(
    ExportTransform(
        name="OneDrive",
        host_pattern=r"(?:^|\.)(onedrive\.live\.com|1drv\.ms)$",
        transform=_office_online_transform,
    )
)
register_export_transform(
    ExportTransform(
        name="SharePoint",
        host_pattern=r"\.sharepoint\.com$",
        transform=_office_online_transform,
    )
)


# ── Backwards-compatibility shim ───────────────────────────────────────────
# `sheets_url.py` exposes thin wrappers that delegate here; this lets older
# tests and call sites keep working without churn.

def is_google_sheets_url(url: str | None) -> bool:
    """Return True iff the URL is a docs.google.com/spreadsheets... URL.

    Pure URL check — does NOT call any transformer (so already-exported
    URLs still come back True).
    """
    if not url:
        return False
    try:
        parts = urlsplit(str(url))
    except Exception:
        return False
    if (parts.scheme or "").lower() not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if host != "docs.google.com" and not host.endswith(".docs.google.com"):
        return False
    return bool(_SHEETS_PATH_RE.match(parts.path or ""))


def convert_google_sheets_url_to_csv(url: str | None) -> str:
    """Convenience: returns the Sheets-specific export URL or ``""``.

    Identical to ``find_data_export_url(url)[1]`` when the URL is a Sheets
    URL — preserved as a public name because tests + other modules import it.
    """
    if not is_google_sheets_url(url):
        return ""
    return _google_sheets_transform(str(url))

"""E4: cross-run action selector cache (action_selector_cache.v1).

A successful deterministic locate (today: click_text's tier funnel) derives a
stable CSS selector for the clicked element and persists it under
``workspace/selector_cache/<host>.json`` keyed by ``action + normalised
semantic key`` (the visible text). The next run on the same host tries the
cached selector first; the hit is only accepted when the element is visible
AND its innerText still matches the requested text (the fingerprint check),
so accuracy never trades for speed. Validation failure invalidates the entry
and the original funnel runs unchanged.

Opt-out: ``VSPIDER_SELECTOR_CACHE=0``.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

CACHE_VERSION = "action_selector_cache.v1"
_MAX_ENTRIES = 200
_KEY_MAX_CHARS = 80
_SLUG_RE = re.compile(r"[^0-9A-Za-z._-]+")


def default_base_dir() -> Path:
    override = str(os.environ.get("VSPIDER_SELECTOR_CACHE_DIR", "")).strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "workspace" / "selector_cache"


def selector_cache_enabled() -> bool:
    return str(os.environ.get("VSPIDER_SELECTOR_CACHE", "1")).strip().lower() not in {
        "0", "false", "no", "off",
    }


def cache_host(url: str) -> str:
    from urllib.parse import urlparse

    text = str(url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"http://{text}")
    return parsed.netloc.split("@")[-1].split(":", 1)[0].lower()


def normalize_key(value: str) -> str:
    text = " ".join(str(value or "").split()).lower()
    return text[:_KEY_MAX_CHARS]


# Derives a short, *verified-unique* CSS selector for an element:
# id -> stable attributes -> nth-of-type path (max 6 levels). Returns ''
# when no unique selector exists, so callers simply skip the cache write.
DERIVE_SELECTOR_JS = """el => {
    const esc = (v) => (window.CSS && CSS.escape) ? CSS.escape(v) : String(v).replace(/([^a-zA-Z0-9_-])/g, '\\\\$1');
    const unique = (sel) => {
        try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; }
    };
    if (el.id) {
        const sel = '#' + esc(el.id);
        if (unique(sel)) return sel;
    }
    for (const attr of ['data-testid', 'data-test', 'data-qa', 'name', 'aria-label']) {
        const v = el.getAttribute && el.getAttribute(attr);
        if (v) {
            const sel = el.tagName.toLowerCase() + '[' + attr + '="' + String(v).replace(/"/g, '\\\\"') + '"]';
            if (unique(sel)) return sel;
        }
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
        if (node.id) {
            parts.unshift('#' + esc(node.id));
            break;
        }
        let part = node.tagName.toLowerCase();
        const parent = node.parentElement;
        if (parent) {
            const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
            if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
        }
        parts.unshift(part);
        node = parent;
    }
    const sel = parts.join(' > ');
    return unique(sel) ? sel : '';
}"""


class SelectorCache:
    """One JSON file per host; entries keyed ``<action>::<normalised key>``."""

    def __init__(self, host: str, *, base_dir: str | Path | None = None) -> None:
        self.host = str(host or "").strip().lower()
        if not self.host:
            raise ValueError("selector cache requires a host")
        root = Path(base_dir) if base_dir is not None else default_base_dir()
        slug = _SLUG_RE.sub("_", self.host).strip("._-") or "host"
        self.path = root / f"{slug}.json"
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and isinstance(obj.get("entries"), dict):
                return obj
        except Exception:
            pass
        return {"version": CACHE_VERSION, "host": self.host, "entries": {}}

    def _save(self) -> None:
        entries: dict[str, Any] = self._data.get("entries", {})
        if len(entries) > _MAX_ENTRIES:
            ranked = sorted(
                entries.items(),
                key=lambda kv: float(kv[1].get("last_ok_at") or kv[1].get("created_at") or 0),
                reverse=True,
            )
            self._data["entries"] = dict(ranked[:_MAX_ENTRIES])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception:
            pass

    @staticmethod
    def _entry_key(action: str, key: str) -> str:
        return f"{str(action or '').strip()}::{normalize_key(key)}"

    def lookup(self, action: str, key: str) -> dict[str, Any] | None:
        entry = self._data["entries"].get(self._entry_key(action, key))
        return dict(entry) if isinstance(entry, dict) else None

    def store(self, action: str, key: str, selector: str, *, signature: str = "") -> dict[str, Any]:
        sel = str(selector or "").strip()
        if not sel:
            raise ValueError("selector must be non-empty")
        now = time.time()
        existing = self._data["entries"].get(self._entry_key(action, key)) or {}
        entry = {
            "selector": sel,
            "signature": normalize_key(signature or key),
            "hits": int(existing.get("hits") or 0),
            "created_at": float(existing.get("created_at") or now),
            "last_ok_at": now,
        }
        self._data["entries"][self._entry_key(action, key)] = entry
        self._save()
        return dict(entry)

    def record_hit(self, action: str, key: str) -> None:
        entry = self._data["entries"].get(self._entry_key(action, key))
        if isinstance(entry, dict):
            entry["hits"] = int(entry.get("hits") or 0) + 1
            entry["last_ok_at"] = time.time()
            self._save()

    def invalidate(self, action: str, key: str) -> None:
        if self._data["entries"].pop(self._entry_key(action, key), None) is not None:
            self._save()

    def entries(self) -> dict[str, Any]:
        return dict(self._data.get("entries") or {})


def som_cache_key(som_elements: list, target_id: int) -> str:
    """Build a normalised cache key from SoM element's role + name."""
    for el in (som_elements or []):
        try:
            if int(el.get("id", -1)) == target_id:
                role = str(el.get("role") or el.get("tag") or "")
                name = str(el.get("name") or el.get("text") or "")
                raw = f"{role}::{name}"
                result = normalize_key(raw)
                return result if result and result != "::" else ""
        except (ValueError, TypeError):
            continue
    return ""


async def validate_cached_target(
    page: Any,
    entry: dict[str, Any],
    expected_name: str,
) -> Any | None:
    """Validate a cached selector for click/type by visibility + text match.

    Returns the first-matching Playwright locator on success, ``None`` on any
    validation failure (caller should invalidate the entry).
    """
    selector = str((entry or {}).get("selector") or "").strip()
    if not selector:
        return None
    try:
        loc = page.locator(selector)
        if await loc.count() < 1:
            return None
        candidate = loc.first
        if not await candidate.is_visible():
            return None
        if expected_name:
            try:
                observed = normalize_key(await candidate.inner_text())
            except Exception:
                observed = ""
            if expected_name not in observed:
                try:
                    label = normalize_key(
                        await candidate.get_attribute("aria-label") or ""
                    )
                except Exception:
                    label = ""
                if expected_name not in label:
                    return None
        return candidate
    except Exception:
        return None


async def derive_selector(locator: Any) -> str:
    """Best-effort unique-CSS derivation for a Playwright locator/handle."""
    try:
        return str(await locator.evaluate(DERIVE_SELECTOR_JS) or "").strip()
    except Exception:
        return ""


async def click_cached_selector(
    page: Any,
    entry: dict[str, Any],
    text: str,
    click_fn: Callable[[Any, str], Awaitable[str]],
) -> str:
    """Validate a cached selector, then click through ``click_fn``.

    Fingerprint gate: the element must exist, be visible, and its innerText
    must still contain the requested text (case/whitespace-insensitive).
    Returns the used-label on success, '' on any miss (caller invalidates).
    """
    selector = str((entry or {}).get("selector") or "").strip()
    if not selector:
        return ""
    try:
        loc = page.locator(selector)
        if await loc.count() < 1:
            return ""
        candidate = loc.first
        if not await candidate.is_visible():
            return ""
        observed = normalize_key(await candidate.inner_text())
        wanted = normalize_key(text)
        if not wanted or (wanted not in observed and observed != wanted):
            return ""
        mode = await click_fn(candidate, f"selector_cache {selector}")
        return f"selector_cache {selector} ({mode})"
    except Exception:
        return ""

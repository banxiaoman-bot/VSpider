"""selector_action_cache.v1: cross-run selector→action replay index.

When a batch-fill or repeated-visit task needs the same form/table on the
same host, the first run discovers selectors via the full Agent loop (VLM +
DOM probing).  This module persists the *successful* selector→action pairs
so subsequent runs can replay them deterministically, skipping expensive
VLM rounds entirely.

Design constraints (mission 高效 + 准确):
- Only **verified** actions (rpa_trail entries with ``verified=True``) are
  cached.  Unverified or failed actions never enter the index.
- Keys are (host, page_path_pattern, selector_fingerprint) so the cache
  generalises across URL query-params but not across different pages.
- The cache is scoped per host (one JSON file per host).
- Staleness: entries older than ``max_age_s`` are evicted on load.
- Thread-safety: load/save are atomic (tmp + os.replace), same pattern as
  ``incremental_state.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_VERSION = "selector_action_cache.v1"
_SAFE_RE = re.compile(r"[^0-9A-Za-z._-]+")
_DEFAULT_BASE_DIR = ".cache/selector_actions"
_DEFAULT_MAX_ENTRIES = 5_000
_DEFAULT_MAX_AGE_S = 30 * 24 * 3600  # 30 days


def _host_slug(host: str) -> str:
    text = str(host or "").strip().lower()
    slug = _SAFE_RE.sub("_", text).strip("._")
    return slug or "unknown"


def _path_pattern(url: str) -> str:
    """Normalise URL path to a reusable pattern (strip query/fragment)."""
    parsed = urlparse(str(url or ""))
    path = (parsed.path or "/").rstrip("/") or "/"
    return path


def selector_fingerprint(
    selector: str,
    tag: str = "",
    role: str = "",
    label: str = "",
) -> str:
    """Stable fingerprint for a selector + element identity."""
    parts = [
        str(selector or "").strip(),
        str(tag or "").strip().lower(),
        str(role or "").strip().lower(),
        str(label or "").strip()[:80],
    ]
    blob = "|".join(parts)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


class SelectorActionCache:
    """Per-host cross-run cache of verified selector→action mappings."""

    def __init__(
        self,
        host: str,
        *,
        base_dir: str | Path = _DEFAULT_BASE_DIR,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_age_s: float = _DEFAULT_MAX_AGE_S,
    ) -> None:
        self.host = str(host or "").strip().lower() or "unknown"
        self.base_dir = Path(base_dir or _DEFAULT_BASE_DIR)
        self.max_entries = max(1, int(max_entries or _DEFAULT_MAX_ENTRIES))
        self.max_age_s = float(max_age_s or _DEFAULT_MAX_AGE_S)
        self._state: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        return self.base_dir / f"{_host_slug(self.host)}.json"

    # -- I/O ---------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state
        state: dict[str, Any] = {"version": _VERSION, "host": self.host, "entries": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
                state["entries"] = raw["entries"]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        now = time.time()
        if self.max_age_s > 0:
            state["entries"] = {
                k: v
                for k, v in state["entries"].items()
                if now - float(v.get("ts", 0)) < self.max_age_s
            }
        self._state = state
        return state

    def save(self) -> None:
        if self._state is None:
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(self.path))
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # -- API ---------------------------------------------------------------

    def _entry_key(self, page_path: str, fp: str) -> str:
        return f"{page_path}|{fp}"

    def lookup(
        self,
        url: str,
        selector: str,
        tag: str = "",
        role: str = "",
        label: str = "",
    ) -> dict[str, Any] | None:
        """Return cached action dict if a verified hit exists, else None."""
        state = self._load()
        pp = _path_pattern(url)
        fp = selector_fingerprint(selector, tag, role, label)
        key = self._entry_key(pp, fp)
        entry = state["entries"].get(key)
        if entry and isinstance(entry, dict):
            entry["hit_count"] = int(entry.get("hit_count", 0)) + 1
            return dict(entry.get("action") or {})
        return None

    def store(
        self,
        url: str,
        selector: str,
        action: dict[str, Any],
        *,
        tag: str = "",
        role: str = "",
        label: str = "",
        run_id: str = "",
    ) -> None:
        """Persist a verified action for future replay.

        Only call this for actions whose rpa_trail entry has
        ``verified=True``.
        """
        state = self._load()
        pp = _path_pattern(url)
        fp = selector_fingerprint(selector, tag, role, label)
        key = self._entry_key(pp, fp)
        state["entries"][key] = {
            "action": {
                k: v
                for k, v in action.items()
                if k in (
                    "action", "type_value", "selector", "xpath",
                    "method", "tag", "role", "label",
                )
            },
            "selector": str(selector or ""),
            "tag": str(tag or ""),
            "role": str(role or ""),
            "label": str(label or "")[:120],
            "fp": fp,
            "page_path": pp,
            "run_id": str(run_id or ""),
            "ts": time.time(),
            "hit_count": 0,
        }
        if len(state["entries"]) > self.max_entries:
            sorted_keys = sorted(
                state["entries"],
                key=lambda k: float(state["entries"][k].get("ts", 0)),
            )
            for old_key in sorted_keys[: len(sorted_keys) - self.max_entries]:
                state["entries"].pop(old_key, None)

    def stats(self) -> dict[str, int]:
        state = self._load()
        entries = state.get("entries", {})
        total_hits = sum(int(e.get("hit_count", 0)) for e in entries.values())
        return {"entries": len(entries), "total_hits": total_hits}

    def clear(self) -> None:
        self._state = {"version": _VERSION, "host": self.host, "entries": {}}
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def get_cache(url: str, **kwargs: Any) -> SelectorActionCache:
    """Factory: returns a cache scoped to the URL's host."""
    host = urlparse(str(url or "")).netloc.split("@")[-1].split(":", 1)[0].lower()
    return SelectorActionCache(host or "unknown", **kwargs)

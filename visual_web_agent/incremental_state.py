"""incremental_state.v1: persistent cross-run dedup index for incremental crawls.

A crawl that runs daily over the same listing should only emit rows it has
never delivered before. This module keeps a small on-disk index of item
signatures per *scope* (typically the site domain or a user-chosen task key)
so any later run can subtract already-known items before exporting.

Design constraints (mission 高效/通用):
- The index stores only signatures + first-seen metadata, never payloads.
- Saves are atomic (tmp + os.replace) so a crashed run cannot corrupt state.
- The index is capped; oldest signatures are evicted first once full.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

_VERSION = "incremental_state.v1"
_SCOPE_SAFE_RE = re.compile(r"[^0-9A-Za-z._-]+")
_DEFAULT_BASE_DIR = ".cache/incremental"
_DEFAULT_MAX_KEYS = 100_000


def scope_slug(scope: str) -> str:
    """Filesystem-safe slug for a scope key (domain, task name, ...)."""
    text = str(scope or "").strip().lower()
    slug = _SCOPE_SAFE_RE.sub("_", text).strip("._")
    return slug or "default"


def item_signature(item: Any, key_fields: list[str] | None = None) -> str:
    """Stable signature for one item.

    With ``key_fields`` only those fields participate (so volatile columns
    like timestamps or view counts do not break dedup); otherwise the whole
    item is canonicalised.
    """
    if isinstance(item, dict) and key_fields:
        basis: Any = {field: item.get(field, "") for field in key_fields}
    else:
        basis = item
    blob = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


class IncrementalStore:
    """Cross-run signature index for one scope."""

    def __init__(
        self,
        scope: str,
        *,
        base_dir: str | Path = _DEFAULT_BASE_DIR,
        max_keys: int = _DEFAULT_MAX_KEYS,
    ) -> None:
        self.scope = str(scope or "").strip() or "default"
        self.base_dir = Path(base_dir or _DEFAULT_BASE_DIR)
        self.max_keys = max(1, int(max_keys or _DEFAULT_MAX_KEYS))
        self._state: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        return self.base_dir / f"{scope_slug(self.scope)}.json"

    # -- state io ----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state
        state: dict[str, Any] = {"version": _VERSION, "scope": self.scope, "seen": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("seen"), dict):
                state["seen"] = {str(k): v for k, v in raw["seen"].items()}
        except FileNotFoundError:
            pass
        except Exception:
            # A corrupt index must not kill the crawl: start fresh, the only
            # cost is re-delivering rows once.
            state["seen"] = {}
        self._state = state
        return state

    def _save(self) -> None:
        state = self._load()
        seen: dict[str, Any] = state["seen"]
        if len(seen) > self.max_keys:
            ordered = sorted(seen.items(), key=lambda kv: float((kv[1] or {}).get("ts") or 0.0))
            for key, _meta in ordered[: len(seen) - self.max_keys]:
                seen.pop(key, None)
        payload = {
            "version": _VERSION,
            "scope": self.scope,
            "updated_at": time.time(),
            "seen": seen,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- public api ---------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._load()["seen"])

    def known(self, signature: str) -> bool:
        return str(signature) in self._load()["seen"]

    def filter_new(
        self,
        items: list[Any],
        *,
        key_fields: list[str] | None = None,
    ) -> tuple[list[Any], int]:
        """Split ``items`` into (never-seen items, count of known ones).

        Duplicates *within* the batch are also collapsed to the first hit.
        """
        seen = self._load()["seen"]
        batch: set[str] = set()
        fresh: list[Any] = []
        skipped = 0
        for item in items:
            sig = item_signature(item, key_fields)
            if sig in seen or sig in batch:
                skipped += 1
                continue
            batch.add(sig)
            fresh.append(item)
        return fresh, skipped

    def commit(
        self,
        items: list[Any],
        *,
        key_fields: list[str] | None = None,
        run_id: str = "",
    ) -> int:
        """Record ``items`` as delivered and persist the index."""
        seen = self._load()["seen"]
        added = 0
        now = time.time()
        for item in items:
            sig = item_signature(item, key_fields)
            if sig in seen:
                continue
            seen[sig] = {"ts": now, "run_id": str(run_id or "")}
            added += 1
        self._save()
        return added

    def stats(self) -> dict[str, Any]:
        return {
            "version": _VERSION,
            "scope": self.scope,
            "state_path": str(self.path),
            "known_total": self.size,
            "max_keys": self.max_keys,
        }

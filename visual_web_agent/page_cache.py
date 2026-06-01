from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SAFE_ID_RE = re.compile(r"[^0-9A-Za-z_.-]+")


class PageCacheMissError(LookupError):
    pass


@dataclass
class PageCacheEntry:
    url: str
    final_url: str
    status_code: int
    html: str
    timestamp: float
    content_hash: str

    def to_dict(self, *, include_html: bool = True) -> dict[str, Any]:
        data = {
            "url": self.url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "timestamp": self.timestamp,
            "content_hash": self.content_hash,
            "bytes": len(self.html.encode("utf-8", errors="ignore")),
        }
        if include_html:
            data["html"] = self.html
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PageCacheEntry":
        html = str(data.get("html") or "")
        return cls(
            url=str(data.get("url") or ""),
            final_url=str(data.get("final_url") or data.get("url") or ""),
            status_code=int(data.get("status_code") or 200),
            html=html,
            timestamp=float(data.get("timestamp") or 0.0),
            content_hash=str(data.get("content_hash") or _hash_text(html)),
        )


class PageResponseCache:
    def __init__(
        self,
        *,
        mode: str = "off",
        cache_dir: str | Path = ".cache/page_responses",
        session_id: str = "default",
        replay_fallback_on_miss: bool = False,
    ) -> None:
        self.mode = normalize_mode(mode)
        self.cache_dir = Path(cache_dir)
        self.session_id = safe_id(session_id or "default")
        self.replay_fallback_on_miss = bool(replay_fallback_on_miss)
        self.hits = 0
        self.misses = 0
        self.records = 0
        if self.mode == "record":
            self.session_dir.mkdir(parents=True, exist_ok=True)

    @property
    def session_dir(self) -> Path:
        return self.cache_dir / self.session_id

    def key_for_url(self, url: str) -> str:
        return hashlib.sha256(str(url or "").encode("utf-8", errors="replace")).hexdigest()[:20]

    def path_for_url(self, url: str) -> Path:
        return self.session_dir / f"{self.key_for_url(url)}.json"

    def lookup(self, url: str) -> PageCacheEntry | None:
        if self.mode != "replay":
            return None
        path = self.path_for_url(url)
        if not path.exists():
            self.misses += 1
            if self.replay_fallback_on_miss:
                return None
            raise PageCacheMissError(f"page cache miss for {url}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.misses += 1
            if self.replay_fallback_on_miss:
                return None
            raise PageCacheMissError(f"page cache read failed for {url}: {exc}") from exc
        self.hits += 1
        return PageCacheEntry.from_dict(data)

    def store(self, url: str, *, final_url: str, status_code: int, html: str) -> PageCacheEntry | None:
        if self.mode != "record":
            return None
        entry = PageCacheEntry(
            url=str(url or ""),
            final_url=str(final_url or url or ""),
            status_code=int(status_code or 200),
            html=str(html or ""),
            timestamp=time.time(),
            content_hash=_hash_text(str(html or "")),
        )
        path = self.path_for_url(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry.to_dict(), ensure_ascii=False), encoding="utf-8")
        self.records += 1
        return entry

    def list_entries(self) -> list[dict[str, Any]]:
        if not self.session_dir.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(self.session_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entry = PageCacheEntry.from_dict(data)
                item = entry.to_dict(include_html=False)
                item["key"] = path.stem
                item["path"] = str(path)
                items.append(item)
            except Exception:
                continue
        items.sort(key=lambda item: item.get("timestamp") or 0, reverse=True)
        return items

    def public_state(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "cache_dir": str(self.cache_dir),
            "session_id": self.session_id,
            "session_dir": str(self.session_dir),
            "hits": self.hits,
            "misses": self.misses,
            "records": self.records,
            "entry_count": len(self.list_entries()),
        }


def normalize_mode(mode: str) -> str:
    value = str(mode or "off").strip().lower()
    return value if value in {"off", "record", "replay"} else "off"


def safe_id(value: str) -> str:
    text = _SAFE_ID_RE.sub("_", str(value or "default").strip()).strip("._-")
    return text or "default"


def _hash_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest()[:20]

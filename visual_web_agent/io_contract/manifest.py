"""``manifest.v1`` - append-only artifact registry.

A manifest tracks every real produced artifact for a run so the consumer
(user, downstream pipeline, replay tool) can find files without guessing.

Design notes:

- Append-only. ``append_item`` returns a **new** ``Manifest`` if you want
  immutability, but for the common case it mutates in place and returns
  ``self`` for chaining.
- Deduplicates on ``sha256``: if an item with the same hash is appended
  again, the original entry is preserved and ``source_url`` is upgraded to
  a deduplicated list so we can keep all upstreams.
- Pure data; no FS IO. Persistence (``write_atomic``) lives outside this
  module so unit tests stay hermetic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


VERSION = "manifest.v1"


_ITEM_KINDS = (
    "answer_text",
    "dataset_rows",
    "dataset_records",
    "media_image",
    "media_video",
    "media_audio",
    "media_pdf",
    "media_archive",
    "file_generic",
    "html_snapshot",
    "screenshot",
    "log",
    "other",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ManifestItem:
    kind: str
    path: str
    size: int = 0
    sha256: str = ""
    mime: str = ""
    source_url: list[str] = field(default_factory=list)
    produced_by: str = ""
    step_id: str = ""
    created_at: str = field(default_factory=_now_iso)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind if self.kind in _ITEM_KINDS else "other",
            "path": self.path,
            "size": int(self.size or 0),
            "sha256": self.sha256,
            "mime": self.mime,
            "source_url": list(dict.fromkeys(str(u) for u in (self.source_url or []) if u)),
            "produced_by": self.produced_by,
            "step_id": self.step_id,
            "created_at": self.created_at,
            "extra": dict(self.extra or {}),
        }


@dataclass
class Manifest:
    run_id: str
    items: list[ManifestItem] = field(default_factory=list)
    updated_at: str = field(default_factory=_now_iso)
    version: str = VERSION
    resumed_from: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": self.version,
            "run_id": self.run_id,
            "items": [it.to_dict() for it in self.items],
            "updated_at": self.updated_at,
        }
        if self.resumed_from:
            payload["resumed_from"] = dict(self.resumed_from)
        return payload

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Manifest":
        if not isinstance(payload, dict):
            return cls(run_id="")
        items: list[ManifestItem] = []
        for raw in payload.get("items") or []:
            if not isinstance(raw, dict):
                continue
            items.append(
                ManifestItem(
                    kind=str(raw.get("kind") or "other"),
                    path=str(raw.get("path") or ""),
                    size=int(raw.get("size") or 0),
                    sha256=str(raw.get("sha256") or ""),
                    mime=str(raw.get("mime") or ""),
                    source_url=[
                        str(u) for u in (raw.get("source_url") or [])
                        if str(u or "")
                    ] if isinstance(raw.get("source_url"), list)
                    else ([str(raw.get("source_url"))] if raw.get("source_url") else []),
                    produced_by=str(raw.get("produced_by") or ""),
                    step_id=str(raw.get("step_id") or ""),
                    created_at=str(raw.get("created_at") or _now_iso()),
                    extra=dict(raw.get("extra") or {}),
                )
            )
        raw_resumed = payload.get("resumed_from")
        return cls(
            run_id=str(payload.get("run_id") or ""),
            items=items,
            updated_at=str(payload.get("updated_at") or _now_iso()),
            version=str(payload.get("version") or VERSION),
            resumed_from=dict(raw_resumed) if isinstance(raw_resumed, dict) else {},
        )

    def find_by_sha(self, sha256: str) -> ManifestItem | None:
        if not sha256:
            return None
        for item in self.items:
            if item.sha256 and item.sha256 == sha256:
                return item
        return None

    def find_by_path(self, path: str) -> ManifestItem | None:
        if not path:
            return None
        for item in self.items:
            if item.path == path:
                return item
        return None


# ---------------------------------------------------------------------------
# Factory + mutators
# ---------------------------------------------------------------------------


def new_manifest(run_id: str) -> Manifest:
    return Manifest(run_id=str(run_id or ""))


def set_resumed_from(manifest: Manifest, resumed_from: dict[str, Any] | None) -> Manifest:
    """Record run-resume provenance on the manifest (RUN-RESUME1).

    Additive + idempotent: an empty / falsy payload clears the marker so an
    unused-resume run stays byte-identical (``resumed_from`` is then omitted
    from :meth:`Manifest.to_dict`). Returns the manifest for chaining.
    """

    manifest.resumed_from = dict(resumed_from or {})
    manifest.updated_at = _now_iso()
    return manifest


def merge_source_url(existing: ManifestItem, new_urls: Iterable[str]) -> None:
    """Upgrade ``existing.source_url`` to keep every distinct upstream URL."""

    bucket = list(existing.source_url or [])
    seen = {u for u in bucket if u}
    for u in new_urls or []:
        s = str(u or "")
        if s and s not in seen:
            bucket.append(s)
            seen.add(s)
    existing.source_url = bucket


def append_item(
    manifest: Manifest,
    *,
    kind: str,
    path: str,
    size: int = 0,
    sha256: str = "",
    mime: str = "",
    source_url: str | Iterable[str] = "",
    produced_by: str = "",
    step_id: str = "",
    extra: dict[str, Any] | None = None,
) -> ManifestItem:
    """Append a new item, or dedupe onto an existing same-sha entry.

    Returns the canonical :class:`ManifestItem` for the artifact (which may
    be a pre-existing one when deduping).
    """

    sources: list[str] = []
    if isinstance(source_url, str):
        if source_url:
            sources = [source_url]
    else:
        sources = [str(u) for u in (source_url or []) if str(u or "")]

    if sha256:
        existing = manifest.find_by_sha(sha256)
        if existing is not None:
            merge_source_url(existing, sources)
            if extra:
                existing.extra.update(extra)
            manifest.updated_at = _now_iso()
            return existing

    item = ManifestItem(
        kind=kind if kind in _ITEM_KINDS else "other",
        path=path,
        size=int(size or 0),
        sha256=sha256,
        mime=mime,
        source_url=sources,
        produced_by=produced_by,
        step_id=step_id,
        created_at=_now_iso(),
        extra=dict(extra or {}),
    )
    manifest.items.append(item)
    manifest.updated_at = _now_iso()
    return item

"""High-level orchestration: candidates -> download -> manifest entry.

Public entry: :func:`harvest_to_run`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from visual_web_agent.io_contract.persistence import (
    ARTIFACTS_DIRNAME,
    read_manifest,
    run_dir,
    write_manifest,
)
from visual_web_agent.io_contract.manifest import (
    Manifest,
    append_item,
)

from .candidates import MediaCandidate, MEDIA_KINDS, dedupe_candidates
from .downloader import (
    DEFAULT_TIMEOUT_S,
    DownloadOutcome,
    StreamingClient,
    download_candidate,
)


_OUTPUT_KIND_FILTER: dict[str, frozenset[str]] = {
    "media_image":   frozenset({"media_image"}),
    "media_video":   frozenset({"media_video"}),
    "media_audio":   frozenset({"media_audio"}),
    "media_pdf":     frozenset({"media_pdf"}),
    "media_archive": frozenset({"media_archive"}),
    "file_generic":  frozenset({"file_generic"}),
    "mixed":         frozenset(MEDIA_KINDS),
}


@dataclass
class HarvestReport:
    """Summary of one harvest run."""

    run_id: str
    output_kind: str = ""
    downloaded: list[DownloadOutcome] = field(default_factory=list)
    failed: list[DownloadOutcome] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    manifest_appended: int = 0

    @property
    def total(self) -> int:
        return len(self.downloaded) + len(self.failed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "output_kind": self.output_kind,
            "downloaded": [outcome.to_dict() for outcome in self.downloaded],
            "failed": [outcome.to_dict() for outcome in self.failed],
            "skipped": list(self.skipped or []),
            "manifest_appended": int(self.manifest_appended),
            "total": int(self.total),
        }


def select_candidates_for_output_kind(
    candidates: Iterable[MediaCandidate],
    output_kind: str,
) -> list[MediaCandidate]:
    """Filter candidates by the desired ``output_kind``.

    ``mixed`` keeps everything in ``MEDIA_KINDS``. Unknown ``output_kind``
    values fall back to ``MEDIA_KINDS`` so we never accidentally drop a
    candidate.
    """

    allowed = _OUTPUT_KIND_FILTER.get(output_kind) or frozenset(MEDIA_KINDS)
    return [c for c in (candidates or []) if c and c.kind in allowed]


def harvest_to_run(
    candidates: Iterable[MediaCandidate],
    run_id: str,
    *,
    output_kind: str = "mixed",
    base_dir: str | Path | None = None,
    client: StreamingClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_items: int | None = None,
    max_bytes_per_item: int | None = None,
    produced_by: str = "media_harvester",
    step_id: str = "",
    extra_headers: dict[str, str] | None = None,
) -> HarvestReport:
    """Download every candidate matching ``output_kind`` and append to manifest.

    ``base_dir`` overrides the default ``runs/`` root (useful for tests).
    ``client`` lets callers inject a custom streaming HTTP client (or a
    fake) instead of the default ``httpx.Client``.
    """

    run_path = run_dir(run_id, base_dir=base_dir)
    artifacts_dir = run_path / ARTIFACTS_DIRNAME
    selected = select_candidates_for_output_kind(
        dedupe_candidates(candidates), output_kind
    )

    if max_items is not None and max_items > 0:
        if len(selected) > max_items:
            skipped_records = [
                {"url": c.url, "kind": c.kind, "reason": "max_items_limit"}
                for c in selected[max_items:]
            ]
            selected = selected[:max_items]
        else:
            skipped_records = []
    else:
        skipped_records = []

    manifest: Manifest = read_manifest(run_id, base_dir=base_dir)

    downloaded: list[DownloadOutcome] = []
    failed: list[DownloadOutcome] = []
    appended = 0

    for candidate in selected:
        outcome = download_candidate(
            candidate,
            artifacts_dir,
            client=client,
            timeout=timeout,
            extra_headers=extra_headers,
            max_bytes=max_bytes_per_item,
        )
        if not outcome.ok:
            failed.append(outcome)
            continue
        try:
            existing = manifest.find_by_sha(outcome.sha256) if outcome.sha256 else None
            append_item(
                manifest,
                kind=outcome.final_kind,
                path=outcome.path,
                size=outcome.size,
                sha256=outcome.sha256,
                mime=outcome.mime,
                source_url=candidate.url,
                produced_by=produced_by,
                step_id=step_id,
                extra={
                    "referrer": candidate.referrer,
                    "alt": candidate.alt,
                    "selector": candidate.selector,
                },
            )
            if existing is None:
                appended += 1
        except Exception as exc:
            outcome.extra["manifest_append_error"] = str(exc)
        downloaded.append(outcome)

    if appended:
        try:
            write_manifest(run_id, manifest, base_dir=base_dir)
        except Exception:
            pass

    return HarvestReport(
        run_id=run_id,
        output_kind=output_kind,
        downloaded=downloaded,
        failed=failed,
        skipped=skipped_records,
        manifest_appended=appended,
    )

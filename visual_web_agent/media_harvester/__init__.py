"""Deterministic media harvesting capability.

Turns ``output_kind=media_*`` runs from "the agent answered with URLs in a
list" into "the files are actually on disk under ``runs/<id>/artifacts/``,
deduplicated, with a manifest entry per item".

This package is **pure logic + thin HTTP**:

- :mod:`candidates` parses HTML / Playwright snapshots into
  :class:`MediaCandidate` records.
- :mod:`downloader` runs the actual HTTP fetch, stream-hashing to sha256
  while writing to disk under a content-addressed name.
- :mod:`harvester` is the orchestrator: it consumes candidates, dispatches
  them to the downloader, appends to ``runs/<id>/manifest.json``, and
  returns a :class:`HarvestReport`.

The capability is metadata-registered in ``action_registry`` so the
capability router can recommend it for ``output_kind=media_*`` goals; the
actual ``handler=`` binding is left for the agent loop to wire up once it
has a Playwright frame in hand.
"""

from __future__ import annotations

from .candidates import (
    MediaCandidate,
    MEDIA_KINDS,
    classify_url,
    collect_from_html,
    dedupe_candidates,
)
from .downloader import (
    DownloadOutcome,
    download_candidate,
    DEFAULT_TIMEOUT_S,
    DEFAULT_HEADERS,
    gc_stale_parts,
)
from .harvester import (
    HarvestReport,
    harvest_to_run,
    select_candidates_for_output_kind,
)
from .agent_hook import (
    MediaHarvestHookResult,
    is_pure_media_goal,
    maybe_run_media_harvest,
)


__all__ = [
    "MediaCandidate",
    "MEDIA_KINDS",
    "classify_url",
    "collect_from_html",
    "dedupe_candidates",
    "DownloadOutcome",
    "download_candidate",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_HEADERS",
    "gc_stale_parts",
    "HarvestReport",
    "harvest_to_run",
    "select_candidates_for_output_kind",
    "MediaHarvestHookResult",
    "is_pure_media_goal",
    "maybe_run_media_harvest",
]

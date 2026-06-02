"""Resume seeding orchestration (RUN-RESUME1 step 3a-wire-2).

A thin façade so ``main.py`` needs only two guarded calls to make a *resumed*
run skip rows the prior run for the same task already wrote:

* :func:`record_run_for_resume` -- register this run in
  ``runs/_resume_index.json`` keyed by ``goal``+``start_url``, pointing at its
  latest dataset artifact (located from ``manifest.json``).
* :func:`seed_seen_from_last_run` -- find the prior run for this task, read its
  dataset back, and rebuild the dedup seen-set + accepted count.

Ties together :mod:`run_resume_index` (cross-run locator), the manifest registry
(:func:`io_contract.persistence.read_manifest`), :mod:`dataset_reader`, and
:func:`data_sanitizer.rebuild_seen_fingerprints`.

Default-off / byte-equivalent: callers gate on ``constraints.resume``; with
resume off nothing here is invoked and behaviour is identical to today. Every
public function swallows its own errors so a resume mishap degrades to a *fresh
run* and can never abort the agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .data_sanitizer import rebuild_seen_fingerprints
from .dataset_reader import read_dataset_rows
from .run_resume_index import compute_resume_key, lookup_last_run, record_run

__all__ = [
    "DATASET_KINDS",
    "ResumeSeed",
    "latest_dataset_path",
    "record_run_for_resume",
    "seed_seen_from_last_run",
]


# Manifest item kinds that represent a re-readable row dataset.
DATASET_KINDS = ("dataset_rows", "dataset_records")


@dataclass
class ResumeSeed:
    """Outcome of :func:`seed_seen_from_last_run`."""

    seen: set[str] = field(default_factory=set)
    count: int = 0
    prior_run_id: str = ""
    dataset_path: str = ""
    reason: str = ""


def latest_dataset_path(run_id: str, *, base_dir: str | Path | None = None) -> str:
    """Return the path of the most recent dataset artifact in ``run_id``'s manifest.

    Manifest items are append-only, so the last dataset-kind entry is the newest.
    Returns ``""`` when there is no manifest / no dataset item / on any error.
    """

    try:
        from .io_contract.persistence import read_manifest

        manifest = read_manifest(run_id, base_dir=base_dir)
    except Exception:
        return ""
    try:
        for item in reversed(list(getattr(manifest, "items", []) or [])):
            if getattr(item, "kind", "") in DATASET_KINDS and getattr(item, "path", ""):
                return str(item.path)
    except Exception:
        return ""
    return ""


def record_run_for_resume(
    goal: str,
    start_url: str,
    run_id: str,
    *,
    item_count: int = 0,
    status: str = "in_progress",
    dataset_path: str | None = None,
    base_dir: str | Path | None = None,
) -> str:
    """Upsert this run into the resume index. Returns the resume key (``""`` on error).

    ``dataset_path`` defaults to the run's latest dataset artifact (from the
    manifest) so the next launch can read it back; pass an explicit path to
    override. Safe to call repeatedly (last-wins) -- e.g. at run start and again
    at run end with the final ``item_count``.
    """

    try:
        key = compute_resume_key(goal, start_url)
        path = (
            dataset_path
            if dataset_path is not None
            else latest_dataset_path(run_id, base_dir=base_dir)
        )
        record_run(
            key,
            run_id,
            status=status,
            item_count=int(item_count or 0),
            goal=goal,
            start_url=start_url,
            dataset_path=str(path or ""),
            base_dir=base_dir,
        )
        return key
    except Exception:
        return ""


def seed_seen_from_last_run(
    goal: str,
    start_url: str,
    current_run_id: str,
    *,
    base_dir: str | Path | None = None,
) -> ResumeSeed:
    """Rebuild a dedup seen-set from the prior run for this ``(goal, start_url)``.

    Returns an empty :class:`ResumeSeed` (with a ``reason``) when there is no
    prior run, the prior run is this run, the prior run has no dataset, or
    anything fails -- so the caller transparently falls back to a fresh run.
    """

    try:
        key = compute_resume_key(goal, start_url)
        prior = lookup_last_run(key, base_dir=base_dir)
    except Exception:
        return ResumeSeed(reason="lookup_failed")

    if not isinstance(prior, dict) or not prior:
        return ResumeSeed(reason="no_prior")

    prior_run_id = str(prior.get("run_id") or "")
    if not prior_run_id or prior_run_id == str(current_run_id or ""):
        return ResumeSeed(prior_run_id=prior_run_id, reason="same_run")

    dataset_path = str(prior.get("dataset_path") or "")
    if not dataset_path:
        dataset_path = latest_dataset_path(prior_run_id, base_dir=base_dir)
    if not dataset_path:
        return ResumeSeed(prior_run_id=prior_run_id, reason="no_dataset")

    try:
        rows = read_dataset_rows(dataset_path)
        seen, count = rebuild_seen_fingerprints(rows)
    except Exception:
        return ResumeSeed(
            prior_run_id=prior_run_id, dataset_path=dataset_path, reason="rebuild_failed"
        )

    return ResumeSeed(
        seen=seen,
        count=count,
        prior_run_id=prior_run_id,
        dataset_path=dataset_path,
        reason="seeded" if count else "empty_dataset",
    )

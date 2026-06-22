"""Run-level artifact packaging policy for ``output_contract.v1``.

Implements the user-facing delivery rule on top of the per-container
writers:

- downloadable files  >  ``FILE_BUNDLE_THRESHOLD``  -> bundle into one ``.zip``
- downloadable files  <= ``FILE_BUNDLE_THRESHOLD``  -> keep individual files
- pure ``answer_text``                                -> stays inline (no files)
- mixed (answer + files)                              -> answer inline + files
                                                         per the threshold rule

The threshold is a policy knob (default 5). ``package_run_artifacts`` is the
**automatic** completion-time hook (only bundles when above threshold).
``build_run_bundle`` is the **explicit** on-demand path (used by the
``/download/runs/{id}/bundle.zip`` endpoint and the frontend "打包下载"
button) and bundles whenever there is at least one downloadable file.

All functions accept ``base_dir`` so they are hermetic under ``tmp_path``
in tests, mirroring the rest of ``data_writers`` / ``io_contract``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._base import run_artifacts_dir
from .zip_writer import write_zip


FILE_BUNDLE_THRESHOLD = 5
BUNDLE_FILENAME_HINT = "bundle"  # -> bundle.zip

# Kinds that represent a real, individually downloadable file artifact.
_FILE_KINDS = frozenset({
    "media_image",
    "media_video",
    "media_audio",
    "media_pdf",
    "media_archive",
    "file_generic",
    "screenshot",
})

_INLINE_KINDS = frozenset({"answer_text"})
_DATASET_KINDS = frozenset({"dataset_rows", "dataset_records"})


def _item_extra(item: dict[str, Any]) -> dict[str, Any]:
    extra = item.get("extra") if isinstance(item, dict) else None
    return extra if isinstance(extra, dict) else {}


def is_bundle_item(item: dict[str, Any]) -> bool:
    """True for a packager-produced ``bundle.zip`` manifest entry."""
    if not isinstance(item, dict):
        return False
    if _item_extra(item).get("bundle"):
        return True
    return Path(str(item.get("path") or "")).name == "bundle.zip"


def is_downloadable_file_item(item: dict[str, Any]) -> bool:
    """True when an item is a real file the user could download.

    Excludes inline answers (no file on disk), datasets handled by their own
    container, and the packager's own ``bundle.zip`` (so re-bundling is
    idempotent and never nests a bundle inside a bundle).
    """
    if not isinstance(item, dict):
        return False
    if item.get("inline"):
        return False
    if str(item.get("kind") or "") not in _FILE_KINDS:
        return False
    if not str(item.get("path") or ""):
        return False
    if is_bundle_item(item):
        return False
    return True


def collect_downloadable_files(manifest_items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [it for it in (manifest_items or []) if is_downloadable_file_item(it)]


def has_bundle(manifest_items: list[dict[str, Any]] | None) -> bool:
    return any(is_bundle_item(it) for it in (manifest_items or []) if isinstance(it, dict))


def should_bundle(file_count: int, *, threshold: int = FILE_BUNDLE_THRESHOLD) -> bool:
    """Policy: strictly more than ``threshold`` files -> bundle."""
    try:
        return int(file_count) > int(threshold)
    except (TypeError, ValueError):
        return False


def decide_delivery(
    manifest_items: list[dict[str, Any]] | None,
    *,
    threshold: int = FILE_BUNDLE_THRESHOLD,
) -> dict[str, Any]:
    """Recommend how to deliver a run's outputs (the "灵活分配" policy).

    Returns a plan describing inline answers (-> frontend), downloadable
    files, datasets, and whether the files should be zipped.
    """
    items = [it for it in (manifest_items or []) if isinstance(it, dict)]

    inline_answers: list[str] = []
    for it in items:
        if str(it.get("kind") or "") not in _INLINE_KINDS:
            continue
        text = str(_item_extra(it).get("inline_text") or it.get("text") or "").strip()
        if text:
            inline_answers.append(text)

    files = collect_downloadable_files(items)
    datasets = [it for it in items if str(it.get("kind") or "") in _DATASET_KINDS]
    file_count = len(files)
    bundle = should_bundle(file_count, threshold=threshold)

    if inline_answers and (files or datasets):
        recommended = "mixed"
    elif bundle:
        recommended = "zip"
    elif files:
        recommended = "files_folder"
    elif inline_answers:
        recommended = "inline"
    elif datasets:
        recommended = "dataset"
    else:
        recommended = "none"

    return {
        "recommended": recommended,
        "inline_answers": inline_answers,
        "file_count": file_count,
        "files": files,
        "datasets": datasets,
        "bundle": bundle,
        "threshold": int(threshold),
    }


def _manifest_item_dicts(run_id: str, base_dir: str | Path | None) -> list[dict[str, Any]]:
    try:
        from ..io_contract.persistence import read_manifest
    except (ImportError, ValueError):
        from visual_web_agent.io_contract.persistence import read_manifest  # type: ignore[no-redef]
    manifest = read_manifest(run_id, base_dir=base_dir)
    out: list[dict[str, Any]] = []
    for item in getattr(manifest, "items", None) or []:
        if hasattr(item, "to_dict"):
            payload = item.to_dict()
            if isinstance(payload, dict):
                out.append(payload)
        elif isinstance(item, dict):
            out.append(dict(item))
    return out


def _resolve_file_blobs(
    run_id: str,
    files: list[dict[str, Any]],
    *,
    base_dir: str | Path | None,
) -> list[dict[str, Any]]:
    """Resolve manifest items to ``{"path", "filename"}`` blobs on disk.

    Paths in the manifest may be absolute (production) or relative; either
    way the real file lives in ``runs/<id>/artifacts/<name>``, so we resolve
    by name inside the artifacts dir first and fall back to the raw path.
    """
    artifacts = run_artifacts_dir(run_id, base_dir=base_dir)
    blobs: list[dict[str, Any]] = []
    for item in files:
        raw = str(item.get("path") or "")
        if not raw:
            continue
        name = Path(raw).name
        candidate = artifacts / name
        src = candidate if candidate.exists() else Path(raw)
        if not src.exists() or not src.is_file():
            continue
        blobs.append({
            "path": str(src),
            "filename": name,
            "source_url": item.get("source_url") or "",
        })
    return blobs


def _create_bundle(
    run_id: str,
    files: list[dict[str, Any]],
    *,
    base_dir: str | Path | None,
    threshold: int,
) -> dict[str, Any]:
    blobs = _resolve_file_blobs(run_id, files, base_dir=base_dir)
    if not blobs:
        return {"bundled": False, "reason": "no_files_on_disk", "file_count": 0}
    record = write_zip(
        blobs,
        run_id=run_id,
        output_kind="media_archive",
        produced_by="run_packager",
        filename_hint=BUNDLE_FILENAME_HINT,
        extra={"bundle": True, "member_count": len(blobs), "threshold": int(threshold)},
        base_dir=base_dir,
    )
    return {
        "bundled": True,
        "path": record.get("path"),
        "file_count": len(blobs),
        "member_count": len(blobs),
        "threshold": int(threshold),
        "item": record,
    }


def package_run_artifacts(
    run_id: str,
    *,
    threshold: int = FILE_BUNDLE_THRESHOLD,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Completion-time auto-packaging.

    Bundles the run's downloadable files into ``bundle.zip`` only when the
    count strictly exceeds ``threshold``. Idempotent: a no-op once a bundle
    already exists. Never raises -- returns a decision dict.
    """
    items = _manifest_item_dicts(run_id, base_dir)
    files = collect_downloadable_files(items)
    file_count = len(files)
    if has_bundle(items):
        return {"bundled": False, "reason": "already_bundled", "file_count": file_count, "threshold": int(threshold)}
    if not should_bundle(file_count, threshold=threshold):
        return {"bundled": False, "reason": "below_threshold", "file_count": file_count, "threshold": int(threshold)}
    return _create_bundle(run_id, files, base_dir=base_dir, threshold=threshold)


def build_run_bundle(
    run_id: str,
    *,
    threshold: int = FILE_BUNDLE_THRESHOLD,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """On-demand packaging for an explicit "download everything" action.

    Bundles whenever there is at least one downloadable file (ignores the
    threshold, since the user asked for it). Rebuilds deterministically, so
    repeat calls dedupe to the same manifest entry by sha256.
    """
    items = _manifest_item_dicts(run_id, base_dir)
    files = collect_downloadable_files(items)
    if not files:
        return {"bundled": False, "reason": "no_files", "file_count": 0, "threshold": int(threshold)}
    return _create_bundle(run_id, files, base_dir=base_dir, threshold=threshold)

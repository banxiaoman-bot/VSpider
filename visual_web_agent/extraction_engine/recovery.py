"""History-guided extraction candidate recovery.

The recovery layer is intentionally advisory: it never creates rows and never
overrides extraction validation. It only adds a bounded score boost to current
candidate sources that resemble recent successful snapshots for the same
URL/field group.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from .selectors import build_selector_fingerprints, score_fingerprint_match
    from .snapshots import DEFAULT_SNAPSHOT_DIR, list_snapshots, load_snapshot
except ImportError:
    from extraction_engine.selectors import build_selector_fingerprints, score_fingerprint_match
    from extraction_engine.snapshots import DEFAULT_SNAPSHOT_DIR, list_snapshots, load_snapshot


def _safe_slug(value: str, *, limit: int = 60) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", value or "").strip("._-")
    return (slug or "snapshot")[:limit]


def _group_key(url: str, fields: list[str] | None = None, goal: str = "") -> str:
    parsed = urlparse(str(url or ""))
    host_path = f"{parsed.netloc}{parsed.path}".strip("/") or str(url or "")
    field_key = ",".join(
        sorted(str(field).strip().lower() for field in fields or [] if str(field).strip())
    )
    if not field_key:
        field_key = _safe_slug(str(goal or ""), limit=60)
    return f"{host_path}|{field_key}"


def _snapshot_fingerprint(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("selector_fingerprints") or build_selector_fingerprints(
        rows=data.get("rows") if isinstance(data.get("rows"), list) else [],
        requested_fields=data.get("requested_fields")
        if isinstance(data.get("requested_fields"), list) else [],
        source=str(data.get("source") or ""),
        data_shape=data.get("data_shape") if isinstance(data.get("data_shape"), dict) else {},
    )


def _candidate_fingerprint(
    candidate: dict[str, Any],
    requested_fields: list[str] | None,
) -> dict[str, Any]:
    return build_selector_fingerprints(
        rows=candidate.get("rows") if isinstance(candidate.get("rows"), list) else [],
        requested_fields=requested_fields or [],
        source=str(candidate.get("name") or ""),
        data_shape=candidate.get("data_shape") if isinstance(candidate.get("data_shape"), dict) else {},
    )


def load_recovery_baselines(
    *,
    url: str,
    requested_fields: list[str] | None = None,
    goal: str = "",
    directory: str | Path = DEFAULT_SNAPSHOT_DIR,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Load recent snapshots for the same URL path and requested field group."""

    if int(limit or 0) <= 0:
        return []
    target_key = _group_key(url, requested_fields, goal)
    baselines: list[dict[str, Any]] = []
    for path in list_snapshots(directory):
        try:
            data = load_snapshot(path)
        except Exception:
            continue
        fields = data.get("requested_fields") if isinstance(data.get("requested_fields"), list) else []
        if _group_key(str(data.get("url") or ""), fields, str(data.get("goal") or "")) != target_key:
            continue
        data = dict(data)
        data["_snapshot_path"] = str(path)
        baselines.append(data)
        if len(baselines) >= int(limit):
            break
    return baselines


def rank_extraction_candidates_with_history(
    candidates: list[dict[str, Any]],
    *,
    url: str,
    requested_fields: list[str] | None = None,
    goal: str = "",
    directory: str | Path = DEFAULT_SNAPSHOT_DIR,
    max_baselines: int = 5,
    min_match: float = 0.72,
    max_boost: float = 45.0,
) -> list[dict[str, Any]]:
    """Return candidates with history-based recovery metadata and score boosts."""

    if not candidates:
        return []
    ranked = [dict(candidate) for candidate in candidates]
    baselines = load_recovery_baselines(
        url=url,
        requested_fields=requested_fields,
        goal=goal,
        directory=directory,
        limit=max_baselines,
    )
    if not baselines:
        return ranked

    baseline_fps = [
        (baseline, _snapshot_fingerprint(baseline))
        for baseline in baselines
    ]
    for candidate in ranked:
        if int(candidate.get("accepted") or 0) <= 0:
            continue
        candidate_fp = _candidate_fingerprint(candidate, requested_fields)
        best: dict[str, Any] | None = None
        for baseline, baseline_fp in baseline_fps:
            match = score_fingerprint_match(baseline_fp, candidate_fp)
            if best is None or float(match.get("score") or 0) > float(best.get("score") or 0):
                best = {
                    "score": float(match.get("score") or 0),
                    "match": bool(match.get("match")),
                    "reasons": list(match.get("reasons") or []),
                    "baseline_path": str(baseline.get("_snapshot_path") or ""),
                    "baseline_source": str(baseline.get("source") or ""),
                    "baseline_signature": str(match.get("baseline_signature") or ""),
                    "candidate_signature": str(match.get("candidate_signature") or ""),
                }
        if not best:
            continue
        candidate["recovery"] = best
        if bool(best.get("match")) and float(best.get("score") or 0) >= float(min_match):
            boost = min(float(max_boost), round(float(best["score"]) * float(max_boost), 2))
            candidate["recovery"] = dict(best, boost=boost)
            candidate["score"] = float(candidate.get("score") or 0) + boost
    return ranked

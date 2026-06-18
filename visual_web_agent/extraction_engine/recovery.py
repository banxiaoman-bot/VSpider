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


_EXTRACTION_SELECTOR_RECOVERY_VERSION = "extraction_selector_recovery.v1"


def _required_field_coverage(rows: Any, requested_fields: list[str] | None) -> float:
    """Fraction of requested fields that have at least one non-empty value."""

    row_dicts = [row for row in (rows or []) if isinstance(row, dict)]
    if not row_dicts:
        return 0.0
    fields = [str(field).strip() for field in (requested_fields or []) if str(field).strip()]
    if not fields:
        return 1.0
    covered = 0
    for field in fields:
        field_lower = field.lower()
        present = False
        for row in row_dicts:
            value = row.get(field)
            if value is None:
                lower_map = {str(key).strip().lower(): val for key, val in row.items()}
                value = lower_map.get(field_lower)
            if value is not None and str(value).strip():
                present = True
                break
        if present:
            covered += 1
    return round(covered / len(fields), 4)


def _best_candidate_coverage(
    candidates: list[dict[str, Any]] | None,
    requested_fields: list[str] | None,
) -> float:
    best = 0.0
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        if int(candidate.get("accepted") or 0) <= 0:
            continue
        coverage = _required_field_coverage(candidate.get("rows"), requested_fields)
        if coverage > best:
            best = coverage
    return best


def recover_extraction_selectors(
    candidates: list[dict[str, Any]] | None,
    *,
    url: str,
    requested_fields: list[str] | None = None,
    goal: str = "",
    directory: str | Path = DEFAULT_SNAPSHOT_DIR,
    max_baselines: int = 5,
    min_baseline_coverage: float = 0.8,
    max_current_coverage: float = 0.5,
) -> dict[str, Any]:
    """Return a strictly-gated selector-recovery hint when extraction collapsed.

    Recovery only fires when the *current* extraction is weak (no accepted rows
    or required-field coverage at/below ``max_current_coverage``) AND a *strong*
    historical baseline (coverage >= ``min_baseline_coverage``) exists for the
    same URL/field group. The hint surfaces the baseline's source-family,
    selector-like signal, and expected fields so the caller can re-attempt
    extraction biased to the known-good surface. It never fabricates rows.
    """

    current_coverage = _best_candidate_coverage(candidates, requested_fields)
    if current_coverage > float(max_current_coverage):
        return {
            "version": _EXTRACTION_SELECTOR_RECOVERY_VERSION,
            "recovered": False,
            "reason": "current_extraction_healthy",
            "current_coverage": current_coverage,
        }

    baselines = load_recovery_baselines(
        url=url,
        requested_fields=requested_fields,
        goal=goal,
        directory=directory,
        limit=max_baselines,
    )
    if not baselines:
        return {
            "version": _EXTRACTION_SELECTOR_RECOVERY_VERSION,
            "recovered": False,
            "reason": "no_baseline",
            "current_coverage": current_coverage,
        }

    best: tuple[float, int, dict[str, Any]] | None = None
    for baseline in baselines:
        rows = baseline.get("rows") if isinstance(baseline.get("rows"), list) else []
        baseline_fields = (
            baseline.get("requested_fields")
            if isinstance(baseline.get("requested_fields"), list)
            else (requested_fields or [])
        )
        coverage = _required_field_coverage(rows, requested_fields or baseline_fields)
        row_count = len([row for row in rows if isinstance(row, dict)])
        if coverage < float(min_baseline_coverage) or row_count <= 0:
            continue
        if best is None or (coverage, row_count) > (best[0], best[1]):
            best = (coverage, row_count, baseline)

    if best is None:
        return {
            "version": _EXTRACTION_SELECTOR_RECOVERY_VERSION,
            "recovered": False,
            "reason": "no_strong_baseline",
            "current_coverage": current_coverage,
        }

    baseline_coverage, baseline_row_count, baseline = best
    fingerprint = _snapshot_fingerprint(baseline)
    expected_fields = [
        str(field)
        for field in (baseline.get("requested_fields") or requested_fields or [])
        if str(field).strip()
    ]
    return {
        "version": _EXTRACTION_SELECTOR_RECOVERY_VERSION,
        "recovered": True,
        "reason": "current_extraction_weak_baseline_available",
        "current_coverage": current_coverage,
        "baseline_coverage": baseline_coverage,
        "baseline_row_count": baseline_row_count,
        "source_family": str(fingerprint.get("source_family") or baseline.get("source") or ""),
        "selector_like": list(fingerprint.get("selector_like") or []),
        "expected_fields": expected_fields,
        "baseline_signature": str(fingerprint.get("signature") or ""),
        "baseline_source": str(baseline.get("source") or ""),
        "baseline_path": str(baseline.get("_snapshot_path") or ""),
    }

"""Selector and rowset fingerprints for extraction resilience.

This first slice records stable semantic/data-shape signals that can be replayed
offline from snapshots. Later slices can add concrete CSS/XPath recovery using
the same scoring surface.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _clip(value: Any, limit: int = 80) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _source_family(source: str) -> str:
    text = str(source or "").upper()
    if "DOM_TABLE" in text:
        return "DOM_TABLE"
    if "DOM_LIST" in text:
        return "DOM_LIST"
    if "AX_TREE" in text:
        return "AX_TREE"
    if "FULL_PAGE" in text:
        return "FULL_PAGE"
    if "XHR" in text:
        return "XHR"
    if "VIEWPORT" in text or "VLM" in text:
        return "VIEWPORT_VLM"
    return text or "UNKNOWN"


def _value_pattern(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "empty"
    lowered = text.lower()
    if re.match(r"^https?://", lowered):
        return "url"
    if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", lowered):
        return "email"
    if re.match(r"^\+?\d[\d\s().-]{5,}$", text):
        return "phone_or_number"
    if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$", text):
        return "date"
    if re.match(r"^\d+(?:\.\d+)?%?$", text):
        return "number"
    if len(text) > 120:
        return "long_text"
    if re.search(r"\d", text) and re.search(r"[A-Za-z\u4e00-\u9fff]", text):
        return "mixed_text_number"
    return "short_text"


def _field_order(rows: list[Any]) -> list[str]:
    for row in rows:
        if isinstance(row, dict):
            return [str(key) for key in row.keys()]
    return []


def _all_fields(rows: list[Any], requested_fields: list[str] | None = None) -> list[str]:
    fields: list[str] = []
    for field in requested_fields or []:
        text = str(field or "").strip()
        if text and text not in fields:
            fields.append(text)
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            text = str(key or "").strip()
            if text and text not in fields:
                fields.append(text)
    return fields


def _field_fingerprint(rows: list[Any], field: str) -> dict[str, Any]:
    values = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(field)
        if value is None:
            lower_map = {str(k).strip().lower(): v for k, v in row.items()}
            value = lower_map.get(field.lower())
        if value is not None and str(value).strip():
            values.append(str(value).strip())

    patterns = Counter(_value_pattern(value) for value in values)
    normalized_samples = [_norm(value) for value in values[:5]]
    signature_src = json.dumps(
        {
            "field": field.lower(),
            "patterns": sorted(patterns.items()),
            "samples": normalized_samples[:3],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "field": field,
        "coverage": len(values),
        "patterns": dict(sorted(patterns.items())),
        "samples": [_clip(value) for value in values[:3]],
        "signature": hashlib.sha1(signature_src.encode("utf-8")).hexdigest()[:16],
    }


def build_selector_fingerprints(
    *,
    rows: list[Any],
    requested_fields: list[str] | None = None,
    source: str = "",
    data_shape: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build compact extraction fingerprints from rows and page shape."""

    row_dicts = [row for row in rows or [] if isinstance(row, dict)]
    fields = _all_fields(row_dicts, requested_fields)
    field_order = _field_order(row_dicts)
    field_fingerprints = [_field_fingerprint(row_dicts, field) for field in fields]
    width_counts = Counter(len(row.keys()) for row in row_dicts)
    source_family = _source_family(source)
    shape = dict(data_shape or {})
    signature_src = json.dumps(
        {
            "source_family": source_family,
            "fields": [field.lower() for field in fields],
            "field_order": [field.lower() for field in field_order],
            "field_patterns": [
                (fp["field"].lower(), sorted(fp["patterns"].items()))
                for fp in field_fingerprints
            ],
            "shape": {
                "table_rows": shape.get("table_rows"),
                "table_cells": shape.get("table_cells"),
                "repeated_class_count": shape.get("repeated_class_count"),
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    selector_like = []
    if source_family == "DOM_TABLE":
        selector_like.append("table -> tbody/tr -> cells")
    elif source_family == "DOM_LIST":
        selector_like.append("repeated list/card items")
    elif source_family in {"AX_TREE", "FULL_PAGE"}:
        selector_like.append("semantic text rows")

    return {
        "source_family": source_family,
        "row_count": len(rows or []),
        "dict_row_count": len(row_dicts),
        "fields": fields,
        "field_order": field_order,
        "field_fingerprints": field_fingerprints,
        "row_widths": dict(sorted(width_counts.items())),
        "selector_like": selector_like,
        "data_shape": {
            key: shape.get(key)
            for key in (
                "table_count",
                "table_rows",
                "table_cells",
                "repeated_list_items",
                "repeated_class_count",
                "repeated_avg_text",
                "body_text_length",
            )
            if key in shape
        },
        "signature": hashlib.sha1(signature_src.encode("utf-8")).hexdigest()[:20],
    }


def score_fingerprint_match(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Score whether two fingerprints look like the same extraction surface."""

    reasons: list[str] = []
    score = 0.0
    if baseline.get("source_family") and baseline.get("source_family") == candidate.get("source_family"):
        score += 0.2
        reasons.append("source_family")

    base_fields = {_norm(field) for field in baseline.get("fields") or [] if _norm(field)}
    cand_fields = {_norm(field) for field in candidate.get("fields") or [] if _norm(field)}
    if base_fields or cand_fields:
        overlap = len(base_fields & cand_fields)
        union = len(base_fields | cand_fields) or 1
        field_score = overlap / union
        score += 0.45 * field_score
        if field_score >= 0.8:
            reasons.append("fields")

    base_patterns = {
        _norm(fp.get("field")): set((fp.get("patterns") or {}).keys())
        for fp in baseline.get("field_fingerprints") or []
        if isinstance(fp, dict)
    }
    cand_patterns = {
        _norm(fp.get("field")): set((fp.get("patterns") or {}).keys())
        for fp in candidate.get("field_fingerprints") or []
        if isinstance(fp, dict)
    }
    pattern_hits = 0
    pattern_total = 0
    for field, patterns in base_patterns.items():
        if not patterns:
            continue
        pattern_total += 1
        if patterns & cand_patterns.get(field, set()):
            pattern_hits += 1
    if pattern_total:
        pattern_score = pattern_hits / pattern_total
        score += 0.25 * pattern_score
        if pattern_score >= 0.8:
            reasons.append("value_patterns")

    base_shape = baseline.get("data_shape") or {}
    cand_shape = candidate.get("data_shape") or {}
    shape_hits = 0
    shape_total = 0
    for key in ("table_rows", "table_cells", "repeated_class_count"):
        if base_shape.get(key) is None or cand_shape.get(key) is None:
            continue
        shape_total += 1
        try:
            b = float(base_shape.get(key) or 0)
            c = float(cand_shape.get(key) or 0)
        except Exception:
            continue
        if b == c or (max(b, c) and abs(b - c) / max(b, c) <= 0.25):
            shape_hits += 1
    if shape_total:
        shape_score = shape_hits / shape_total
        score += 0.1 * shape_score
        if shape_score >= 0.8:
            reasons.append("data_shape")

    score = max(0.0, min(1.0, score))
    return {
        "score": round(score, 3),
        "match": score >= 0.72,
        "reasons": reasons,
        "baseline_signature": baseline.get("signature", ""),
        "candidate_signature": candidate.get("signature", ""),
    }

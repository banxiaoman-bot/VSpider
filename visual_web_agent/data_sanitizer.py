"""Schema-agnostic cleanup for VLM extracted rows.

The sanitizer deliberately avoids business-field assumptions such as
``title``/``url``/``author``. It audits extracted data by shape, grounding,
and dynamic fingerprints so it can be reused for forums, e-commerce, GitHub,
ERP tables, and numeric grid/energy ledgers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


EMPTY_MARKERS = {
    "", "-", "--", "---", "—", "–", "/", "\\", "n/a", "na", "nan",
    "none", "null", "undefined", "无", "暂无", "未填", "未填写", "未知",
}

RUNTIME_KEYS = {
    "_extracted_at", "page_url", "url_source", "source_page", "source",
}

ROW_COLLECTION_KEYS = {
    "visible_rows", "rows", "row", "data", "items", "records", "results",
    "list", "table", "table_rows", "entries", "products", "articles",
    "employees",
}

WRAPPER_META_KEYS = {
    "page", "current_page", "page_index", "page_no", "page_number",
    "total", "total_count", "total_entries", "total_pages",
    "entries_per_page", "page_size", "per_page", "count", "query",
    "keyword", "has_next", "next_page",
}

IDENTITY_KEY_MARKERS = (
    "id", "uuid", "url", "link", "href", "sku", "code", "no", "sn",
    "bh", "编号", "序号", "序列号", "编码", "设备编码", "工单号", "单号",
    "资产编号", "表计号", "户号",
)

VOLATILE_EVIDENCE_KEY_MARKERS = (
    "point", "score", "vote", "comment", "reply", "time", "date",
    "age", "author", "user", "by", "热度", "评论", "时间", "日期",
    "作者", "用户",
)

URL_ALIAS_KEYS = {"link", "href"}

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {
    "spm", "fbclid", "gclid", "yclid", "mc_cid", "mc_eid",
}

TOOLTIP_UNIQUE_KEY = "__tooltip_trigger__"
TOOLTIP_INTERNAL_KEY = "__tooltip_trigger_key"

TOOLTIP_TRIGGER_KEY_MARKERS = (
    "direction", "placement", "button", "btn", "label", "target", "name",
    "element", "trigger", "control", "widget", "anchor", "item", "field",
    "column", "column_name", "header", "heading", "caption", "selector",
)

TOOLTIP_PAYLOAD_KEY_MARKERS = (
    "tooltip", "tool_tip", "tip", "popover", "content", "message",
    "description", "reaction", "result", "text",
)


@dataclass
class SanitizeResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    accepted: int = 0
    duplicates: int = 0
    rejected_sparse: int = 0
    rejected_ungrounded: int = 0
    rejected_low_quality: int = 0
    fingerprints: set[str] = field(default_factory=set)

    @property
    def raw(self) -> int:
        return (
            self.accepted
            + self.duplicates
            + self.rejected_sparse
            + self.rejected_ungrounded
            + self.rejected_low_quality
        )

    @property
    def rejected_total(self) -> int:
        return (
            self.rejected_sparse
            + self.rejected_ungrounded
            + self.rejected_low_quality
        )

    def summary_log(self, target_remaining: int | None = None) -> str:
        suffix = "" if target_remaining is None else f" remaining={target_remaining}"
        return (
            f"[SANITIZER] raw={self.raw} accepted={self.accepted} "
            f"duplicate={self.duplicates} sparse={self.rejected_sparse} "
            f"ungrounded={self.rejected_ungrounded} "
            f"low_quality={self.rejected_low_quality}{suffix}"
        )


def sanitize_extracted_rows(
    raw_data: Any,
    source_text: str,
    seen_fingerprints: set[str],
    target_remaining: int | None = None,
) -> SanitizeResult:
    """Normalize, validate, deduplicate, and cap VLM extracted rows."""
    result = SanitizeResult()
    if target_remaining is not None and target_remaining <= 0:
        return result

    rows = _normalise_rows(raw_data)
    if not rows:
        return result

    schema_width = _infer_schema_width(rows)
    source_norm = _normalise_for_match(source_text)

    for row in rows:
        if target_remaining is not None and result.accepted >= target_remaining:
            break

        clean = _non_empty_values(row)
        numeric_dense = _is_numeric_dense_row(clean, schema_width)

        if _is_sparse_row(clean, schema_width, numeric_dense):
            result.rejected_sparse += 1
            continue

        if _is_low_quality_row(clean, numeric_dense):
            result.rejected_low_quality += 1
            continue

        if not _has_source_evidence(clean, source_norm, numeric_dense):
            result.rejected_ungrounded += 1
            continue

        fingerprints = _row_fingerprints(clean)
        if any(fingerprint in seen_fingerprints for fingerprint in fingerprints):
            result.duplicates += 1
            continue

        for fingerprint in fingerprints:
            seen_fingerprints.add(fingerprint)
            result.fingerprints.add(fingerprint)
        result.rows.append(_canonical_output_row(row))
        result.accepted += 1

    return result


def extract_tooltip_primary_key(row_dict: dict[str, Any]) -> str:
    """Return a stable trigger key for hover/tooltip mapping rows.

    Tooltip extraction is an upsert-style task: the trigger identifies the row,
    and the tooltip/popover text is the payload that may improve in later steps.
    """
    clean = _non_empty_values(row_dict)
    if not clean:
        return ""

    for key, value in sorted(clean.items()):
        key_norm = _normalise_collection_key(key)
        is_trigger_key = any(marker in key_norm for marker in TOOLTIP_TRIGGER_KEY_MARKERS)
        is_payload_key = any(marker in key_norm for marker in TOOLTIP_PAYLOAD_KEY_MARKERS)
        if is_trigger_key and not (is_payload_key and "trigger" not in key_norm):
            normalized = _normalize_tooltip_key(value)
            if normalized:
                return f"trigger|{normalized}"

    valid_values = [_normalize_tooltip_key(value) for value in clean.values()]
    valid_values = [value for value in valid_values if value]
    if valid_values:
        return "value|" + max(valid_values, key=len)

    return ""


def _normalise_rows(raw_data: Any) -> list[dict[str, Any]]:
    if raw_data is None:
        return []
    if isinstance(raw_data, list):
        items = raw_data
    else:
        items = [raw_data]

    rows: list[dict[str, Any]] = []
    for item in items:
        rows.extend(_flatten_row_item(item))
    return rows


def _flatten_row_item(
    item: Any,
    parent_meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    parent_meta = parent_meta or {}
    if not isinstance(item, dict):
        row = _merge_parent_meta(parent_meta, {"value": item})
        return [row] if row else []

    row = {
        str(k).strip(): v
        for k, v in item.items()
        if str(k).strip() and str(k).strip() not in RUNTIME_KEYS
    }
    if not row:
        return []

    collection_key = _find_row_collection_key(row)
    if collection_key:
        children = row.get(collection_key) or []
        local_meta = {
            key: value
            for key, value in row.items()
            if key != collection_key and _is_scalar_meta_value(value)
        }
        merged_meta = _merge_parent_meta(parent_meta, local_meta)
        flattened: list[dict[str, Any]] = []
        for child in children:
            flattened.extend(_flatten_row_item(child, merged_meta))
        if flattened:
            return flattened

    merged_row = _merge_parent_meta(parent_meta, row)
    return [merged_row] if merged_row else []


def _find_row_collection_key(row: dict[str, Any]) -> str | None:
    candidates: list[tuple[int, str]] = []
    for key, value in row.items():
        if not _is_list_of_dicts(value):
            continue
        key_norm = _normalise_collection_key(key)
        if key_norm in ROW_COLLECTION_KEYS:
            candidates.append((100 + len(value), key))
        elif _looks_like_wrapper_row(row, key):
            candidates.append((50 + len(value), key))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _normalise_collection_key(key: str) -> str:
    return re.sub(r"[\s\-]+", "_", str(key or "").strip().lower())


def _is_list_of_dicts(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(isinstance(child, dict) for child in value)
    )


def _looks_like_wrapper_row(row: dict[str, Any], collection_key: str) -> bool:
    scalar_keys = [
        key for key, value in row.items()
        if key != collection_key and _is_scalar_meta_value(value)
    ]
    non_scalar_keys = [
        key for key, value in row.items()
        if key != collection_key and not _is_scalar_meta_value(value)
    ]
    if non_scalar_keys:
        return False
    if not scalar_keys:
        return True
    meta_hits = sum(
        1 for key in scalar_keys
        if _normalise_collection_key(key) in WRAPPER_META_KEYS
    )
    return meta_hits >= max(1, len(scalar_keys) - 1)


def _is_scalar_meta_value(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _merge_parent_meta(
    parent_meta: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in parent_meta.items():
        key_text = str(key).strip()
        if not key_text or key_text in RUNTIME_KEYS:
            continue
        if key_text in row:
            merged[f"parent_{key_text}"] = value
        else:
            merged[key_text] = value
    merged.update(row)
    return merged


def _canonical_output_row(row: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {}
    for key, value in row.items():
        key_text = str(key).strip()
        if not key_text or key_text in RUNTIME_KEYS:
            continue
        if key_text.lower() in URL_ALIAS_KEYS:
            canonical.setdefault("url", value)
            continue
        canonical[key_text] = value
    return canonical


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    text = str(value).replace("\u3000", " ").strip()
    return text.lower() in EMPTY_MARKERS


def _value_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).replace("\u3000", " ").strip())


def _non_empty_values(row: dict[str, Any]) -> dict[str, str]:
    return {
        str(k).strip(): _value_text(v)
        for k, v in row.items()
        if str(k).strip() and not _is_empty(v)
    }


def _infer_schema_width(rows: list[dict[str, Any]]) -> int:
    widths = sorted(len(_non_empty_values(row)) for row in rows)
    widths = [w for w in widths if w > 0]
    if not widths:
        return 1
    # P75 is more robust than max: one very wide hallucinated row should not
    # make all valid narrow table rows look sparse.
    idx = min(len(widths) - 1, int(len(widths) * 0.75))
    return max(1, widths[idx])


def _looks_numeric_or_time(text: str) -> bool:
    text = _value_text(text)
    if not text:
        return False
    if re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?%?(?:\s*(?:kv|v|a|kw|kwh|mw|℃|hz))?", text, re.I):
        return True
    if re.fullmatch(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?", text):
        return True
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", text):
        return True
    return False


def _is_numeric_dense_row(clean: dict[str, str], schema_width: int) -> bool:
    if len(clean) < 3:
        return False
    numeric_like = sum(1 for value in clean.values() if _looks_numeric_or_time(value))
    return numeric_like >= 3 and len(clean) >= max(3, int(schema_width * 0.5))


def _is_sparse_row(
    clean: dict[str, str], schema_width: int, numeric_dense: bool
) -> bool:
    if not clean:
        return True
    if numeric_dense:
        return False
    if schema_width >= 4:
        return len(clean) < max(2, math.ceil(schema_width * 0.45))
    if schema_width >= 3:
        return len(clean) < 2
    return False


def _is_low_quality_row(clean: dict[str, str], numeric_dense: bool) -> bool:
    if numeric_dense or len(clean) <= 1:
        return False
    lengths = [len(v) for v in clean.values() if v]
    total_len = sum(lengths)
    if total_len <= 150:
        return False
    return max(lengths) / total_len > 0.85


def _normalise_for_match(text: str) -> str:
    text = str(text or "").replace("\u3000", " ").lower()
    return re.sub(r"\s+", " ", text).strip()


def _compact_stable_text(text: str) -> str:
    return re.sub(r"\W+", "", str(text or "").lower(), flags=re.UNICODE)


def _is_identity_key(key: str) -> bool:
    key_lower = str(key or "").lower()
    return any(marker in key_lower for marker in IDENTITY_KEY_MARKERS)


def _is_volatile_evidence_key(key: str) -> bool:
    key_lower = str(key or "").lower()
    return any(marker in key_lower for marker in VOLATILE_EVIDENCE_KEY_MARKERS)


def _has_source_evidence(
    clean: dict[str, str], source_norm: str, numeric_dense: bool
) -> bool:
    if not source_norm:
        return True

    source_compact = _compact_stable_text(source_norm)

    if not numeric_dense:
        for key, value in clean.items():
            value_norm = _normalise_for_match(value)
            if not value_norm or _looks_numeric_or_time(value_norm):
                continue
            compact = _compact_stable_text(value_norm)
            exact_match = value_norm in source_norm
            compact_match = len(compact) >= 8 and compact in source_compact
            if not (exact_match or compact_match):
                continue
            if _is_identity_key(key) and len(compact) >= 4:
                return True
            if not _is_volatile_evidence_key(key) and len(compact) >= 4:
                return True
        return False

    evidence = 0
    for value in clean.values():
        value_norm = _normalise_for_match(value)
        if not value_norm:
            continue
        if value_norm in source_norm:
            if numeric_dense and _looks_numeric_or_time(value_norm):
                evidence += 1
            elif len(value_norm) >= 10:
                evidence += 2
            elif len(value_norm) >= 4:
                evidence += 1
        elif len(value_norm) >= 10:
            compact = _compact_stable_text(value_norm)
            if compact and compact in source_compact:
                evidence += 1

    return evidence >= 2


def _row_fingerprint(clean: dict[str, str]) -> str:
    identity_values: list[str] = []
    for key, value in sorted(clean.items()):
        key_lower = key.lower()
        if any(marker in key_lower for marker in IDENTITY_KEY_MARKERS):
            normalized = _normalize_identity_value(value)
            if normalized:
                identity_values.append(normalized)

    if identity_values:
        raw_key = "id|" + "|".join(identity_values)
    else:
        stable_values = []
        for _, value in sorted(clean.items()):
            if _looks_numeric_or_time(value):
                continue
            compact = _compact_stable_text(value)
            if len(compact) >= 8:
                stable_values.append(compact)
        stable_values = sorted(stable_values, key=len, reverse=True)[:3]
        raw_key = "text|" + "|".join(stable_values) if stable_values else "row|" + str(sorted(clean.items()))

    return hashlib.md5(raw_key.encode("utf-8", errors="ignore")).hexdigest()


def _row_fingerprints(clean: dict[str, str]) -> list[str]:
    raw_keys = [_row_fingerprint(clean)]
    stable_values = []
    for key, value in sorted(clean.items()):
        if _is_identity_key(key) or _is_volatile_evidence_key(key):
            continue
        if _looks_numeric_or_time(value):
            continue
        compact = _compact_stable_text(value)
        if len(compact) >= 30:
            stable_values.append(compact)

    for value in sorted(set(stable_values), key=len, reverse=True)[:2]:
        raw_keys.append(f"text|{value}")

    return [
        hashlib.md5(raw_key.encode("utf-8", errors="ignore")).hexdigest()
        for raw_key in raw_keys
    ]


def _normalize_identity_value(value: str) -> str:
    text = _value_text(value)
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        query = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in TRACKING_QUERY_KEYS
            and not k.lower().startswith(TRACKING_QUERY_PREFIXES)
        ]
        parsed = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            query=urlencode(query, doseq=True),
            fragment="",
        )
        return urlunparse(parsed).rstrip("/")
    return _compact_stable_text(text)


def _normalize_tooltip_key(value: Any) -> str:
    text = _value_text(value)
    if not text:
        return ""
    compact = _compact_stable_text(text)
    return compact[:160]

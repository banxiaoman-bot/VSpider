from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse


_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")  # no "." => blocks ./.. path traversal
_VOLATILE_QUERY_KEYS = {
    "_",
    "t",
    "ts",
    "timestamp",
    "time",
    "r",
    "rand",
    "random",
    "callback",
}
_PAGE_QUERY_KEYS = {"page", "p", "current", "offset", "limit", "size", "pageSize", "cursor"}

# Request headers worth persisting so an API fast-path replay can reproduce the
# same permission level the logged-in browser session had. Cookies are handled
# separately (forwarded from the live context); these cover token/header auth
# that lives outside cookies (e.g. Bearer tokens kept in localStorage).
_REPLAY_HEADER_WHITELIST = (
    "authorization",
    "x-csrf-token",
    "x-xsrf-token",
    "x-requested-with",
    "x-api-key",
    "x-auth-token",
    "x-access-token",
    "x-token",
    "x-tt-token",
    "x-app-id",
    "x-client-id",
    "x-tenant-id",
    "referer",
    "origin",
)

# Bounds for optionally persisting the full captured payload so a severely
# truncated DOM can be upgraded from the data captured "at the time" without a
# second network round-trip. Kept small to avoid bloating the JSONL index.
_FULL_ROWS_MAX_ROWS = 120
_FULL_ROWS_MAX_FIELD_CHARS = 8000
_FULL_ROWS_MAX_TOTAL_CHARS = 96000


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def network_root(base_dir: str | Path | None = None) -> Path:
    root = Path(base_dir) if base_dir is not None else project_root() / "runs" / "network"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_run_id(run_id: str) -> str:
    rid = str(run_id or "").strip()
    if not rid or not _RUN_ID_RE.fullmatch(rid):
        raise ValueError("invalid run_id")
    return rid


def network_path(run_id: str, base_dir: str | Path | None = None) -> Path:
    return network_root(base_dir) / f"{_safe_run_id(run_id)}.jsonl"


def _atomic_append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


def normalize_endpoint(url: str) -> str:
    parsed = urlparse(str(url or ""))
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        k_lower = key.lower()
        if k_lower in _VOLATILE_QUERY_KEYS:
            continue
        if key in _PAGE_QUERY_KEYS or k_lower in {x.lower() for x in _PAGE_QUERY_KEYS}:
            kept.append((key, "*"))
        else:
            kept.append((key, value))
    query = "&".join(f"{k}={v}" for k, v in kept)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))


def schema_fingerprint(rows: list[dict[str, Any]]) -> str:
    keys: set[str] = set()
    for row in rows[:8]:
        if isinstance(row, dict):
            keys.update(str(k) for k in row.keys())
    return "|".join(sorted(keys))


def sample_rows(rows: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[: max(0, limit)]:
        if not isinstance(row, dict):
            continue
        clean: dict[str, Any] = {}
        for key, value in list(row.items())[:20]:
            if isinstance(value, (str, int, float, bool)) or value is None:
                text = value
                if isinstance(text, str) and len(text) > 180:
                    text = text[:177] + "..."
                clean[str(key)] = text
            elif isinstance(value, list):
                clean[str(key)] = f"<list:{len(value)}>"
            elif isinstance(value, dict):
                clean[str(key)] = f"<dict:{len(value)}>"
            else:
                clean[str(key)] = str(type(value).__name__)
        out.append(clean)
    return out


def score_candidate(url: str, rows: list[dict[str, Any]], *, base_score: int = 0) -> int:
    row_count = len(rows)
    fp = schema_fingerprint(rows)
    keys = set(fp.split("|")) if fp else set()
    lower_url = str(url or "").lower()
    score = int(base_score or 0)

    if row_count >= 5:
        score += 2
    if row_count >= 20:
        score += 2
    if row_count >= 50:
        score += 1
    if any(x in lower_url for x in ("api", "ajax", "xhr", "search", "query", "list", "page", "data", "result", "export")):
        score += 2
    if keys & {"id", "uuid", "uid", "url", "link", "title", "name", "price", "amount", "date", "time", "status", "code", "order_id"}:
        score += 2
    if keys & {"next", "cursor", "has_more", "total", "page", "page_size"}:
        score += 1
    if len(keys) >= 3:
        score += 1
    if keys & {"children", "menus", "permissions", "routes", "config", "schema"} and row_count < 20:
        score -= 3
    return score


def filter_replay_headers(headers: Any) -> dict[str, str]:
    """Keep only auth-bearing request headers safe to replay (whitelist)."""
    out: dict[str, str] = {}
    if not isinstance(headers, dict):
        return out
    for key, value in headers.items():
        name = str(key or "").strip().lower()
        if name in _REPLAY_HEADER_WHITELIST and value not in (None, ""):
            out[name] = str(value)
    return out


def field_max_lengths(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Max length per (lowercased) string field across the full untruncated rows."""
    lengths: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if isinstance(value, str):
                text = value.strip()
                if text:
                    k = str(key).lower()
                    lengths[k] = max(lengths.get(k, 0), len(text))
    return lengths


def max_text_len(rows: list[dict[str, Any]]) -> int:
    lengths = field_max_lengths(rows)
    return max(lengths.values()) if lengths else 0


def capped_full_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bounded copy of the full payload for offline upgrade without re-replay."""
    out: list[dict[str, Any]] = []
    total = 0
    for row in rows[:_FULL_ROWS_MAX_ROWS]:
        if not isinstance(row, dict):
            continue
        clean: dict[str, Any] = {}
        for key, value in list(row.items())[:40]:
            if isinstance(value, str):
                if len(value) > _FULL_ROWS_MAX_FIELD_CHARS:
                    value = value[:_FULL_ROWS_MAX_FIELD_CHARS]
                total += len(value)
            elif isinstance(value, (int, float, bool)) or value is None:
                total += 8
            else:
                continue
            clean[str(key)] = value
        if clean:
            out.append(clean)
        if total >= _FULL_ROWS_MAX_TOTAL_CHARS:
            break
    return out


def build_candidate(
    *,
    run_id: str,
    url: str,
    method: str = "GET",
    status: int = 200,
    resource_type: str = "xhr",
    rows: list[dict[str, Any]] | None = None,
    score: int = 0,
    content_type: str = "",
    request_headers: Any | None = None,
    request_body: str | None = None,
) -> dict[str, Any]:
    rid = _safe_run_id(run_id)
    data_rows = [r for r in (rows or []) if isinstance(r, dict)]
    body_text = str(request_body or "")
    if len(body_text) > _FULL_ROWS_MAX_FIELD_CHARS:
        body_text = body_text[:_FULL_ROWS_MAX_FIELD_CHARS]
    return {
        "run_id": rid,
        "ts": time.time(),
        "url": str(url or ""),
        "endpoint": normalize_endpoint(url),
        "method": str(method or "GET").upper(),
        "status": int(status or 0),
        "resource_type": str(resource_type or ""),
        "content_type": str(content_type or ""),
        "row_count": len(data_rows),
        "schema": schema_fingerprint(data_rows),
        "score": score_candidate(url, data_rows, base_score=score),
        "sample": sample_rows(data_rows),
        "max_text_len": max_text_len(data_rows),
        "field_max_lengths": field_max_lengths(data_rows),
        "request_headers": filter_replay_headers(request_headers),
        "request_body": body_text,
        "full_rows": capped_full_rows(data_rows),
    }


def record_candidate(
    *,
    run_id: str,
    url: str,
    method: str = "GET",
    status: int = 200,
    resource_type: str = "xhr",
    rows: list[dict[str, Any]] | None = None,
    score: int = 0,
    content_type: str = "",
    request_headers: Any | None = None,
    request_body: str | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    candidate = build_candidate(
        run_id=run_id,
        url=url,
        method=method,
        status=status,
        resource_type=resource_type,
        rows=rows,
        score=score,
        content_type=content_type,
        request_headers=request_headers,
        request_body=request_body,
    )
    if candidate["row_count"] <= 0:
        return None
    _atomic_append_jsonl(network_path(run_id, base_dir), candidate)
    return candidate


def list_candidates(
    run_id: str,
    *,
    limit: int = 100,
    min_score: int = 0,
    base_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    try:
        path = network_path(run_id, base_dir)
    except ValueError:
        return []
    if not path.exists():
        return []
    try:
        n = max(1, min(int(limit), 500))
    except Exception:
        n = 100
    try:
        threshold = int(min_score)
    except Exception:
        threshold = 0
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if int(obj.get("score") or 0) < threshold:
                continue
            out.append(obj)
    out.sort(key=lambda x: (int(x.get("score") or 0), float(x.get("ts") or 0)), reverse=True)
    return out[:n]


def summarize_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    endpoints: dict[str, dict[str, Any]] = {}
    for c in candidates:
        ep = str(c.get("endpoint") or c.get("url") or "")
        if not ep:
            continue
        cur = endpoints.get(ep)
        if cur is None:
            endpoints[ep] = {
                "endpoint": ep,
                "hits": 1,
                "max_score": int(c.get("score") or 0),
                "max_rows": int(c.get("row_count") or 0),
                "schema": c.get("schema") or "",
                "sample": c.get("sample") or [],
                "latest_ts": c.get("ts"),
            }
            continue
        cur["hits"] += 1
        cur["max_score"] = max(int(cur.get("max_score") or 0), int(c.get("score") or 0))
        cur["max_rows"] = max(int(cur.get("max_rows") or 0), int(c.get("row_count") or 0))
        if float(c.get("ts") or 0) >= float(cur.get("latest_ts") or 0):
            cur["latest_ts"] = c.get("ts")
            cur["sample"] = c.get("sample") or cur.get("sample") or []
    ranked = sorted(
        endpoints.values(),
        key=lambda x: (int(x.get("max_score") or 0), int(x.get("hits") or 0), int(x.get("max_rows") or 0)),
        reverse=True,
    )
    return {
        "endpoint_count": len(ranked),
        "top_endpoints": ranked[:20],
    }

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .artifact_manager import artifact_url, register_artifact, resolve_artifact_path
from .url_guard import UrlGuardError, build_guarded_opener, check_url


_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")  # no "." => blocks ./.. path traversal
_PAGE_KEYS = {"page", "p", "current", "offset", "cursor"}
_LIMIT_KEYS = {"limit", "size", "page_size", "pageSize", "per_page"}
_CURSOR_KEYS = {"cursor", "after", "page_token", "pageToken", "next_cursor", "scroll_id"}
_NEXT_CURSOR_KEYS = (
    "next_cursor",
    "nextCursor",
    "next_page_token",
    "nextPageToken",
    "after",
    "scroll_id",
)
_NEXT_URL_KEYS = ("next", "next_url", "nextUrl", "next_page_url", "nextPageUrl")


def _safe_run_id(run_id: str) -> str:
    rid = str(run_id or "").strip()
    if not rid or not _RUN_ID_RE.fullmatch(rid):
        raise ValueError("invalid run_id")
    return rid


def _extract_rows(value: Any) -> list[dict[str, Any]]:
    best: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        nonlocal best
        if isinstance(node, list):
            dict_rows = [x for x in node if isinstance(x, dict)]
            if len(dict_rows) > len(best):
                best = dict_rows
            for item in node[:20]:
                if isinstance(item, (dict, list)):
                    visit(item)
            return
        if isinstance(node, dict):
            preferred = (
                "data",
                "items",
                "list",
                "rows",
                "records",
                "results",
                "result",
                "content",
            )
            for key in preferred:
                if key in node:
                    visit(node[key])
            for val in node.values():
                if isinstance(val, (dict, list)):
                    visit(val)

    visit(value)
    return best


def build_replay_url(
    endpoint: str,
    *,
    page: int | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
) -> str:
    parsed = urlparse(str(endpoint or ""))
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    out: list[tuple[str, str]] = []
    page_set = False
    size_set = False
    cursor_set = False
    cursor_keys_l = {x.lower() for x in _CURSOR_KEYS}
    for key, value in pairs:
        key_l = key.lower()
        if cursor is not None and key_l in cursor_keys_l:
            out.append((key, str(cursor)))
            cursor_set = True
        elif value == "*" and (key in _PAGE_KEYS or key_l in {x.lower() for x in _PAGE_KEYS}):
            out.append((key, str(page if page is not None else 1)))
            page_set = True
        elif value == "*" and (key in _LIMIT_KEYS or key_l in {x.lower() for x in _LIMIT_KEYS}):
            out.append((key, str(page_size if page_size is not None else 50)))
            size_set = True
        else:
            out.append((key, value))
    if cursor is not None and not cursor_set:
        out.append(("cursor", str(cursor)))
    if page is not None and not page_set and cursor is None:
        out.append(("page", str(page)))
    if page_size is not None and not size_set:
        out.append(("limit", str(page_size)))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(out), ""))


def build_replay_plan(
    candidate: dict[str, Any],
    *,
    page: int | None = 1,
    page_size: int | None = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    endpoint = str(candidate.get("endpoint") or candidate.get("url") or "")
    method = str(candidate.get("method") or "GET").upper()
    method = method if method in {"GET", "POST"} else "GET"
    body = str(candidate.get("request_body") or "")
    return {
        "method": method,
        "endpoint": endpoint,
        "url": build_replay_url(endpoint, page=page, page_size=page_size, cursor=cursor),
        "schema": candidate.get("schema") or "",
        "score": int(candidate.get("score") or 0),
        "page": page,
        "page_size": page_size,
        "cursor": cursor or "",
        "body": body if method == "POST" else "",
    }


def _extract_pagination_hints(parsed: Any) -> tuple[str, str]:
    """Return (next_url, next_cursor) advertised by a JSON payload.

    Only the document root and one nested dict level are inspected: that is
    where real-world APIs (DRF, GitHub, Elastic, OpenAPI cursor styles) put
    their pagination block, and a deep scan would risk picking row data.
    """

    def probe(node: dict[str, Any]) -> tuple[str, str]:
        next_url = ""
        next_cursor = ""
        for key in _NEXT_URL_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
                next_url = value
                break
        for key in _NEXT_CURSOR_KEYS:
            value = node.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                next_cursor = str(value).strip()
                break
        return next_url, next_cursor

    if not isinstance(parsed, dict):
        return "", ""
    next_url, next_cursor = probe(parsed)
    if next_url or next_cursor:
        return next_url, next_cursor
    for value in parsed.values():
        if isinstance(value, dict):
            next_url, next_cursor = probe(value)
            if next_url or next_cursor:
                return next_url, next_cursor
    return "", ""


def _write_jsonl_artifact(run_id: str, rows: list[dict[str, Any]], *, source_url: str = "") -> Path:
    rid = _safe_run_id(run_id)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = resolve_artifact_path(f"api_replay_{rid}_{ts}.jsonl", subdir="api_replay")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    extra: dict[str, Any] = {"row_count": len(rows)}
    fields = _row_fields(rows)
    if fields:
        extra["fields"] = fields
    try:
        register_artifact(
            path,
            run_id=rid,
            kind="dataset_records",
            mime="application/x-ndjson",
            source_url=source_url,
            produced_by="api_replay",
            step_id="network_replay",
            extra=extra,
        )
    except TypeError:
        register_artifact(path)
    return path


def _row_fields(rows: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            text = str(key or "").strip()
            if text and text not in out:
                out.append(text)
    return out


def _blocked_replay_result(rid: str, plan: dict[str, Any], method: str, reason: str) -> dict[str, Any]:
    """SSRF-blocked replay result, shaped like a clean http failure (no socket)."""
    return {
        "status": "blocked_url",
        "http_ok": False,
        "run_id": rid,
        "http_status": 0,
        "method": method,
        "content_type": "",
        "plan": plan,
        "row_count": 0,
        "rows": [],
        "error": reason,
        "artifact": {"path": "", "url": ""},
        "sample": [],
    }


def replay_candidate(
    *,
    run_id: str,
    candidate: dict[str, Any],
    page: int | None = 1,
    page_size: int | None = 50,
    cursor: str | None = None,
    timeout_s: float = 15.0,
    headers: dict[str, str] | None = None,
    fetcher: Any | None = None,
    write_artifact: bool = True,
) -> dict[str, Any]:
    rid = _safe_run_id(run_id)
    plan = build_replay_plan(candidate, page=page, page_size=page_size, cursor=cursor)
    url = str(plan["url"])
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("candidate endpoint must be http(s)")

    method = str(plan.get("method") or "GET").upper()
    body = str(plan.get("body") or "")

    req_headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": "VSpider-API-Replay/1.0",
    }
    req_headers.update(headers or {})

    body_bytes: bytes | None = None
    if method == "POST" and body:
        body_bytes = body.encode("utf-8")
        if not any(k.lower() == "content-type" for k in req_headers):
            req_headers["content-type"] = "application/json"

    if fetcher is not None:
        raw_status, raw_headers, raw_body = fetcher(url, req_headers, timeout_s, method, body)
    else:
        # SSRF guard: validate the target host before opening any socket so a
        # captured candidate URL can't point the server at an internal /
        # cloud-metadata address (mission §一-3 通用: one guard, every fetch).
        try:
            check_url(url)
        except UrlGuardError as exc:
            return _blocked_replay_result(rid, plan, method, f"blocked_url: {exc}")
        request = urllib.request.Request(
            url,
            data=body_bytes if method == "POST" else None,
            headers=req_headers,
            method=method,
        )
        try:
            with build_guarded_opener().open(request, timeout=timeout_s) as resp:
                raw_status = int(getattr(resp, "status", 0) or 0)
                raw_headers = dict(resp.headers.items())
                raw_body = resp.read()
        except urllib.error.HTTPError as exc:
            raw_status = int(exc.code or 0)
            raw_headers = dict(exc.headers.items()) if exc.headers else {}
            raw_body = exc.read()

    if isinstance(raw_body, bytes):
        text = raw_body.decode("utf-8", errors="replace")
    else:
        text = str(raw_body or "")

    http_ok = 200 <= int(raw_status or 0) < 300
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    rows = _extract_rows(parsed) if parsed is not None else []
    next_url, next_cursor = _extract_pagination_hints(parsed)
    artifact_path = (
        _write_jsonl_artifact(rid, rows, source_url=url)
        if (rows and http_ok and write_artifact)
        else None
    )
    return {
        "status": "success" if http_ok else "http_error",
        "http_ok": http_ok,
        "run_id": rid,
        "http_status": raw_status,
        "method": method,
        "content_type": raw_headers.get("content-type") or raw_headers.get("Content-Type") or "",
        "plan": plan,
        "row_count": len(rows),
        "rows": rows,
        "next_url": next_url,
        "next_cursor": next_cursor,
        "artifact": {
            "path": str(artifact_path) if artifact_path else "",
            "url": artifact_url(artifact_path) if artifact_path else "",
        },
        "sample": rows[:3],
    }


def paginate_replay(
    *,
    run_id: str,
    candidate: dict[str, Any],
    page_size: int | None = 50,
    start_page: int = 1,
    max_pages: int = 20,
    target_rows: int | None = None,
    timeout_s: float = 15.0,
    headers: dict[str, str] | None = None,
    fetcher: Any | None = None,
) -> dict[str, Any]:
    """Replay a captured API across pages until the dataset is exhausted.

    Pagination strategy is auto-detected per page, in priority order:
    server-advertised absolute ``next`` URL > cursor token > numeric page
    increment. Stop conditions: empty page, all-duplicate page, short page
    (numeric mode only), HTTP failure, ``target_rows`` reached, or the
    ``max_pages`` safety cap (reported as ``truncated``).
    """

    rid = _safe_run_id(run_id)
    endpoint = str(candidate.get("endpoint") or candidate.get("url") or "")
    seen: set[str] = set()
    all_rows: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    stop_reason = "max_pages"
    last_result: dict[str, Any] = {}
    cursor: str | None = None
    next_url = ""
    page = max(1, int(start_page or 1))

    for _ in range(max(1, int(max_pages or 1))):
        if next_url:
            # Server-advertised absolute URL already carries its own params:
            # pass it through untouched (no page/limit/cursor rewriting).
            step_candidate = dict(candidate)
            step_candidate["endpoint"] = next_url
            step_candidate.pop("url", None)
            step_page: int | None = None
            step_size: int | None = None
            step_cursor: str | None = None
        else:
            step_candidate = candidate
            step_page = None if cursor is not None else page
            step_size = page_size
            step_cursor = cursor
        result = replay_candidate(
            run_id=rid,
            candidate=step_candidate,
            page=step_page,
            page_size=step_size,
            cursor=step_cursor,
            timeout_s=timeout_s,
            headers=headers,
            fetcher=fetcher,
            write_artifact=False,
        )
        last_result = result
        rows = result.get("rows") or []
        new_rows = 0
        for row in rows:
            sig = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if sig in seen:
                continue
            seen.add(sig)
            all_rows.append(row)
            new_rows += 1
        pages.append({
            "url": str((result.get("plan") or {}).get("url") or ""),
            "http_status": result.get("http_status"),
            "row_count": len(rows),
            "new_rows": new_rows,
        })
        if not result.get("http_ok"):
            stop_reason = str(result.get("status") or "http_error")
            break
        if not rows:
            stop_reason = "empty_page"
            break
        if new_rows == 0:
            stop_reason = "duplicate_page"
            break
        if target_rows and len(all_rows) >= int(target_rows):
            stop_reason = "target_reached"
            break
        step_next_url = str(result.get("next_url") or "")
        step_next_cursor = str(result.get("next_cursor") or "")
        if step_next_url:
            if step_next_url == next_url:
                stop_reason = "repeated_next_url"
                break
            next_url = step_next_url
            cursor = None
        elif step_next_cursor:
            if cursor is not None and step_next_cursor == cursor:
                stop_reason = "repeated_cursor"
                break
            cursor = step_next_cursor
            next_url = ""
        else:
            if page_size and len(rows) < int(page_size):
                stop_reason = "short_page"
                break
            next_url = ""
            cursor = None
            page += 1

    http_ok = bool(last_result.get("http_ok"))
    success = bool(all_rows) and (http_ok or len(pages) > 1)
    artifact_path = _write_jsonl_artifact(rid, all_rows, source_url=endpoint) if all_rows else None
    return {
        "status": "success" if success else str(last_result.get("status") or "http_error"),
        "http_ok": http_ok,
        "run_id": rid,
        "http_status": last_result.get("http_status"),
        "method": str(last_result.get("method") or ""),
        "content_type": str(last_result.get("content_type") or ""),
        "plan": last_result.get("plan") or build_replay_plan(candidate, page=start_page, page_size=page_size),
        "row_count": len(all_rows),
        "rows": all_rows,
        "pages": pages,
        "page_count": len(pages),
        "stop_reason": stop_reason,
        "truncated": stop_reason == "max_pages",
        "artifact": {
            "path": str(artifact_path) if artifact_path else "",
            "url": artifact_url(artifact_path) if artifact_path else "",
        },
        "sample": all_rows[:3],
    }


def choose_candidate(candidates: list[dict[str, Any]], endpoint: str = "") -> dict[str, Any] | None:
    if endpoint:
        for c in candidates:
            if endpoint in {str(c.get("endpoint") or ""), str(c.get("url") or "")}:
                return c
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda c: (int(c.get("score") or 0), int(c.get("row_count") or 0), float(c.get("ts") or 0)),
        reverse=True,
    )[0]

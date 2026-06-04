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
from .url_guard import UrlGuardError, check_url


_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_.-]+$")
_PAGE_KEYS = {"page", "p", "current", "offset", "cursor"}
_LIMIT_KEYS = {"limit", "size", "page_size", "pageSize", "per_page"}


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
) -> str:
    parsed = urlparse(str(endpoint or ""))
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    out: list[tuple[str, str]] = []
    page_set = False
    size_set = False
    for key, value in pairs:
        key_l = key.lower()
        if value == "*" and (key in _PAGE_KEYS or key_l in {x.lower() for x in _PAGE_KEYS}):
            out.append((key, str(page if page is not None else 1)))
            page_set = True
        elif value == "*" and (key in _LIMIT_KEYS or key_l in {x.lower() for x in _LIMIT_KEYS}):
            out.append((key, str(page_size if page_size is not None else 50)))
            size_set = True
        else:
            out.append((key, value))
    if page is not None and not page_set:
        out.append(("page", str(page)))
    if page_size is not None and not size_set:
        out.append(("limit", str(page_size)))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(out), ""))


def build_replay_plan(
    candidate: dict[str, Any],
    *,
    page: int | None = 1,
    page_size: int | None = 50,
) -> dict[str, Any]:
    endpoint = str(candidate.get("endpoint") or candidate.get("url") or "")
    method = str(candidate.get("method") or "GET").upper()
    method = method if method in {"GET", "POST"} else "GET"
    body = str(candidate.get("request_body") or "")
    return {
        "method": method,
        "endpoint": endpoint,
        "url": build_replay_url(endpoint, page=page, page_size=page_size),
        "schema": candidate.get("schema") or "",
        "score": int(candidate.get("score") or 0),
        "page": page,
        "page_size": page_size,
        "body": body if method == "POST" else "",
    }


def _write_jsonl_artifact(run_id: str, rows: list[dict[str, Any]]) -> Path:
    rid = _safe_run_id(run_id)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = resolve_artifact_path(f"api_replay_{rid}_{ts}.jsonl", subdir="api_replay")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    register_artifact(path)
    return path


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
    timeout_s: float = 15.0,
    headers: dict[str, str] | None = None,
    fetcher: Any | None = None,
) -> dict[str, Any]:
    rid = _safe_run_id(run_id)
    plan = build_replay_plan(candidate, page=page, page_size=page_size)
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
            with urllib.request.urlopen(request, timeout=timeout_s) as resp:
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
    artifact_path = _write_jsonl_artifact(rid, rows) if (rows and http_ok) else None
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
        "artifact": {
            "path": str(artifact_path) if artifact_path else "",
            "url": artifact_url(artifact_path) if artifact_path else "",
        },
        "sample": rows[:3],
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

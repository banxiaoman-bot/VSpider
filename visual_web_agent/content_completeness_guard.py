"""Detect DOM-truncated content and prefer richer API/XHR payloads (NET-6)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from visual_web_agent.api_replay import choose_candidate, replay_candidate
from visual_web_agent.network_intelligence import list_candidates

_COMPLETENESS_VERSION = "dom_api_completeness.v1"

_TRUNCATION_MARKERS = re.compile(
    r"(开通会员|会员专享|VIP|付费|解锁全文|查看全部|展开全部|仅展示|部分内容|"
    r"登录后查看|subscribe|premium|paywall|unlock|read more|show more|"
    r"…|\.\.\.)",
    re.I,
)
_FULL_CONTENT_GOAL_RE = re.compile(
    r"完整|全文|详情|正文|原文|会员|unlock|vip|premium",
    re.I,
)
_TEXT_FIELD_KEYS = (
    "content",
    "body",
    "text",
    "desc",
    "description",
    "summary",
    "detail",
    "article",
    "answer",
    "reply",
    "comment",
    "title",
    "name",
)


def _as_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, dict)]


def _string_fields(row: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in row.items():
        if isinstance(value, str):
            text = value.strip()
            if text:
                out[str(key).lower()] = text
    return out


def _max_text_len(rows: list[dict[str, Any]]) -> int:
    best = 0
    for row in rows:
        for text in _string_fields(row).values():
            best = max(best, len(text))
    return best


def _field_max_lengths(rows: list[dict[str, Any]]) -> dict[str, int]:
    lengths: dict[str, int] = {}
    for row in rows:
        for key, text in _string_fields(row).items():
            lengths[key] = max(lengths.get(key, 0), len(text))
    return lengths


def detect_dom_truncation(
    dom_text: str = "",
    dom_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    text = str(dom_text or "")
    markers: list[str] = []
    if _TRUNCATION_MARKERS.search(text):
        markers.append("page_text")
    row_hits = 0
    for row in _as_rows(dom_rows):
        blob = " ".join(_string_fields(row).values())
        if _TRUNCATION_MARKERS.search(blob):
            row_hits += 1
    if row_hits:
        markers.append(f"rows:{row_hits}")
    truncated = bool(markers)
    return {
        "truncated": truncated,
        "markers": markers,
        "max_text_len": _max_text_len(_as_rows(dom_rows)),
    }


def _candidate_field_lengths(candidate: dict[str, Any]) -> tuple[dict[str, int], int]:
    """Prefer the real (untruncated) lengths captured at record time; the
    180-char ``sample`` would otherwise blind the richness comparison."""
    stored = candidate.get("field_max_lengths")
    if isinstance(stored, dict) and stored:
        api_lens = {str(k).lower(): int(v) for k, v in stored.items() if int(v or 0) > 0}
        api_max = int(candidate.get("max_text_len") or (max(api_lens.values()) if api_lens else 0))
        return api_lens, api_max
    sample = _as_rows(candidate.get("sample"))
    return _field_max_lengths(sample), _max_text_len(sample)


def _richer_than_dom(
    dom_rows: list[dict[str, Any]],
    candidate: dict[str, Any],
    *,
    ratio: float = 1.35,
) -> dict[str, Any]:
    api_lens, api_max = _candidate_field_lengths(candidate)
    if not api_lens and api_max <= 0:
        return {"richer": False, "fields": [], "dom_max": 0, "api_max": 0}

    dom_lens = _field_max_lengths(dom_rows)
    richer_fields: list[str] = []
    for key in _TEXT_FIELD_KEYS:
        dom_len = dom_lens.get(key, 0)
        api_len = api_lens.get(key, 0)
        if api_len <= 0:
            continue
        if dom_len <= 0 and api_len >= 80:
            richer_fields.append(f"{key}:0→{api_len}")
            continue
        if dom_len > 0 and api_len >= int(dom_len * ratio) and (api_len - dom_len) >= 40:
            richer_fields.append(f"{key}:{dom_len}→{api_len}")

    dom_max = _max_text_len(dom_rows)
    global_richer = api_max >= int(max(dom_max, 1) * ratio) and (api_max - dom_max) >= 60
    return {
        "richer": bool(richer_fields or global_richer),
        "fields": richer_fields,
        "dom_max": dom_max,
        "api_max": api_max,
    }


def rank_candidates_for_dom(
    dom_rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    goal: str = "",
) -> list[dict[str, Any]]:
    rows = _as_rows(dom_rows)
    ranked: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        richness = _richer_than_dom(rows, candidate)
        bonus = 3 if richness.get("richer") else 0
        if _FULL_CONTENT_GOAL_RE.search(str(goal or "")):
            bonus += 1
        score = int(candidate.get("score") or 0) + bonus
        ranked.append((score, candidate, richness))
    ranked.sort(
        key=lambda item: (
            item[0],
            int(item[1].get("row_count") or 0),
            float(item[1].get("ts") or 0),
        ),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for score, candidate, richness in ranked:
        item = dict(candidate)
        item["completeness_score"] = score
        item["richness"] = richness
        out.append(item)
    return out


def evaluate_dom_api_completeness(
    *,
    dom_rows: list[dict[str, Any]] | None = None,
    dom_text: str = "",
    candidates: list[dict[str, Any]] | None = None,
    run_id: str = "",
    goal: str = "",
    min_candidate_score: int = 5,
) -> dict[str, Any]:
    rows = _as_rows(dom_rows)
    pool = list(candidates or [])
    if not pool and run_id:
        pool = list_candidates(run_id, limit=50, min_score=min_candidate_score)

    truncation = detect_dom_truncation(dom_text, rows)
    ranked = rank_candidates_for_dom(rows, pool, goal=goal)
    best = ranked[0] if ranked else None
    richness = (best or {}).get("richness") or {}
    api_richer = bool(richness.get("richer"))
    dom_truncated = bool(truncation.get("truncated"))
    goal_wants_full = bool(_FULL_CONTENT_GOAL_RE.search(str(goal or "")))

    should_fast_path = bool(
        best
        and int(best.get("completeness_score") or best.get("score") or 0) >= min_candidate_score
        and (dom_truncated or api_richer or (goal_wants_full and int(best.get("row_count") or 0) >= 3))
    )

    reasons: list[str] = []
    if dom_truncated:
        reasons.append("dom_truncated")
    if api_richer:
        reasons.append("api_richer_than_dom")
    if goal_wants_full:
        reasons.append("goal_prefers_full_content")

    return {
        "version": _COMPLETENESS_VERSION,
        "should_fast_path": should_fast_path,
        "dom_truncated": dom_truncated,
        "api_richer": api_richer,
        "goal_wants_full": goal_wants_full,
        "truncation": truncation,
        "candidate": best,
        "candidate_count": len(pool),
        "reasons": reasons,
        "endpoint": str((best or {}).get("endpoint") or (best or {}).get("url") or ""),
        "richness": richness,
    }


def build_cookie_header(cookies: list[dict[str, Any]] | None, *, url: str = "") -> str:
    if not cookies:
        return ""
    host = (urlparse(str(url or "")).hostname or "").lower()
    parts: list[str] = []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if not name:
            continue
        domain = str(item.get("domain") or "").lstrip(".").lower()
        if host and domain and host != domain and not host.endswith("." + domain) and not domain.endswith(host):
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _is_detail_candidate(verdict: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Single-record / full-content endpoints must not get list pagination params."""
    return bool(verdict.get("goal_wants_full")) and int(candidate.get("row_count") or 0) <= 2


def execute_api_fast_path(
    *,
    run_id: str,
    verdict: dict[str, Any],
    cookies: list[dict[str, Any]] | None = None,
    page: int = 1,
    page_size: int = 50,
    timeout_s: float = 15.0,
    fetcher: Any | None = None,
) -> dict[str, Any]:
    if not verdict.get("should_fast_path"):
        return {"applied": False, "reason": "fast_path_not_recommended", "verdict": verdict}

    candidate = verdict.get("candidate")
    if not isinstance(candidate, dict):
        chosen = choose_candidate(list_candidates(run_id, limit=50, min_score=5))
        candidate = chosen
    if not isinstance(candidate, dict):
        return {"applied": False, "reason": "no_candidate", "verdict": verdict}

    endpoint = str(candidate.get("endpoint") or candidate.get("url") or "")
    headers: dict[str, str] = {}
    # Replay captured auth headers (Bearer token / CSRF / etc.) so the fast path
    # reproduces the same permission level even when auth is not cookie-based.
    captured_headers = candidate.get("request_headers")
    if isinstance(captured_headers, dict):
        for key, value in captured_headers.items():
            if key and value not in (None, ""):
                headers[str(key)] = str(value)
    cookie_header = build_cookie_header(cookies, url=endpoint)
    if cookie_header:
        headers["Cookie"] = cookie_header

    detail = _is_detail_candidate(verdict, candidate)
    rep_page = None if detail else page
    rep_size = None if detail else page_size

    replay = replay_candidate(
        run_id=run_id,
        candidate=candidate,
        page=rep_page,
        page_size=rep_size,
        timeout_s=timeout_s,
        headers=headers or None,
        fetcher=fetcher,
    )
    rows = replay.get("rows") or []
    row_count = int(replay.get("row_count") or len(rows))
    http_ok = bool(replay.get("http_ok", replay.get("status") == "success"))
    applied = row_count > 0 and replay.get("status") == "success" and http_ok

    source = "api_replay"
    # Fallback to the payload captured "at the time" when a live replay is not
    # possible (auth/replay failure) but the captured full rows are usable.
    if not applied:
        captured = [r for r in (candidate.get("full_rows") or []) if isinstance(r, dict)]
        if captured:
            rows = captured
            row_count = len(captured)
            applied = True
            source = "captured_payload"

    return {
        "applied": applied,
        "source": source,
        "verdict": verdict,
        "replay": replay,
        "row_count": row_count,
        "endpoint": endpoint,
        "rows": rows,
        "detail": detail,
        "cookie_header_used": bool(cookie_header),
        "auth_headers_used": sorted(headers.keys()),
    }

"""Cross-run experience reuse — RPA cache + failure fixture hints for routing."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_RPA_CACHE_DIR = Path(__file__).resolve().parent / "rpa_cache"
_FIXTURE_SUBDIR = "capability/failure_fixtures"


def _norm_host(url: str) -> str:
    try:
        return (urlsplit(str(url or "").strip()).netloc or "").lower()
    except Exception:
        return ""


def _norm_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or "").strip())
        path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") or "/"
        return urlunsplit(((parsed.scheme or "https").lower(), (parsed.netloc or "").lower(), path, "", ""))
    except Exception:
        return str(url or "").strip().rstrip("/")


def _norm_goal(goal: str) -> str:
    text = re.split(r"\n\s*\n【[^】]+】", str(goal or ""), maxsplit=1)[0].strip().lower()
    text = re.sub(r"\{\{[^}]+\}\}", "{{var}}", text)
    return re.sub(r"\s+", " ", text).strip()


def _rpa_cache_key(url: str, goal: str) -> str:
    payload = f"{_norm_url(url)}||{_norm_goal(goal)}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _fixture_root() -> Path:
    try:
        from visual_web_agent.artifact_manager import resolve_artifact_path

        return resolve_artifact_path("_probe", subdir=_FIXTURE_SUBDIR).parent
    except Exception:
        return Path("artifacts") / _FIXTURE_SUBDIR


def lookup_rpa_cache_hint(url: str, goal: str) -> dict[str, Any]:
    key = _rpa_cache_key(url, goal)
    path = _RPA_CACHE_DIR / f"{key}.json"
    if not path.exists():
        return {"hit": False}
    payload = _read_json(path)
    if not payload:
        return {"hit": False}
    return {
        "hit": True,
        "path": str(path),
        "replayable": bool(payload.get("replayable", True)),
        "trail_steps": len(list(payload.get("trail") or [])),
        "fail_count": int(payload.get("fail_count") or 0),
        "reason": str(payload.get("reason") or ""),
    }


def scan_similar_rpa_caches(url: str, goal: str, *, limit: int = 3) -> list[dict[str, Any]]:
    if not _RPA_CACHE_DIR.exists():
        return []
    host = _norm_host(url)
    goal_tokens = {t for t in re.split(r"\W+", _norm_goal(goal)) if len(t) >= 2}
    matches: list[tuple[int, dict[str, Any]]] = []
    for path in _RPA_CACHE_DIR.glob("*.json"):
        payload = _read_json(path)
        if not payload:
            continue
        meta = payload.get("match_metadata") if isinstance(payload.get("match_metadata"), dict) else {}
        cache_url = str(meta.get("normalized_url") or meta.get("url") or "")
        if host and _norm_host(cache_url) != host:
            continue
        cache_goal = _norm_goal(str(meta.get("normalized_goal") or meta.get("goal") or ""))
        overlap = len(goal_tokens & {t for t in re.split(r"\W+", cache_goal) if len(t) >= 2})
        if overlap <= 0 and not host:
            continue
        matches.append((overlap, {
            "path": str(path),
            "overlap_tokens": overlap,
            "replayable": bool(payload.get("replayable", True)),
            "trail_steps": len(list(payload.get("trail") or [])),
        }))
    matches.sort(key=lambda item: -item[0])
    return [item for _score, item in matches[: max(1, limit)]]


def scan_failure_fixture_hints(url: str, goal: str, *, limit: int = 3) -> list[dict[str, Any]]:
    root = _fixture_root()
    if not root.exists():
        return []
    host = _norm_host(url)
    goal_lower = _norm_goal(goal)
    hints: list[tuple[int, dict[str, Any]]] = []
    for path in sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        data = _read_json(path)
        if not data or str(data.get("version") or "") != "capability_failure_regression_fixture.v1":
            continue
        expected = data.get("expected") if isinstance(data.get("expected"), dict) else {}
        fixture_url = str(expected.get("url") or data.get("url") or "")
        if host and _norm_host(fixture_url) != host:
            continue
        name = str(data.get("name") or path.stem)
        score = 0
        if any(tok in goal_lower for tok in re.split(r"\W+", name.lower()) if len(tok) >= 3):
            score += 2
        if host:
            score += 1
        hints.append((score, {
            "name": name,
            "path": str(path),
            "primary_failure": str(expected.get("primary_failure") or ""),
            "capability": str(expected.get("capability") or ""),
            "repair_actions": list(expected.get("repair_actions") or []),
        }))
    hints.sort(key=lambda item: -item[0])
    return [item for _score, item in hints[: max(1, limit)]]


def build_experience_hints(goal: str, url: str = "") -> dict[str, Any]:
    rpa = lookup_rpa_cache_hint(url, goal)
    similar = scan_similar_rpa_caches(url, goal)
    fixtures = scan_failure_fixture_hints(url, goal)
    recommended: list[str] = []
    if rpa.get("hit") and rpa.get("replayable"):
        recommended.append("rpa_cache_replay")
    if fixtures:
        recommended.append("failure_fixture_replay")
        for item in fixtures:
            cap = str(item.get("capability") or "").strip()
            if cap and cap not in recommended:
                recommended.append(cap)
    return {
        "version": "experience_reuse.v1",
        "rpa_cache": rpa,
        "similar_rpa_caches": similar,
        "failure_fixtures": fixtures,
        "recommended_capabilities": recommended,
    }

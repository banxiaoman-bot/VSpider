"""Skill replay snapshots for offline diagnostics."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from .base import SkillResult, SkillVerification
from .registry import build_default_skill_registry


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SKILL_REPLAY_DIR = ROOT / "workspace" / "skill_replays"


def _safe_slug(value: str, *, limit: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    slug = slug.strip("._")
    return (slug or "skill")[:limit]


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return str(value)


def _verification_payload(items: list[SkillVerification] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, SkillVerification):
            payload.append(asdict(item))
        elif isinstance(item, dict):
            payload.append(dict(item))
    return payload


def save_skill_replay_snapshot(
    *,
    skill_match: Any,
    goal: str,
    url: str,
    result: SkillResult,
    run_id: str = "",
    output_file: str = "",
    directory: Path | str = DEFAULT_SKILL_REPLAY_DIR,
) -> Path:
    """Persist a compact skill replay snapshot.

    The snapshot is intentionally browser-free: it captures the deterministic
    contract around matching, rows, required fields, and verifications so a
    later code change can be checked offline.
    """
    skill = skill_match.skill
    rows = list(result.rows or [])
    fields = sorted({
        str(key)
        for row in rows
        if isinstance(row, dict)
        for key, value in row.items()
        if value not in (None, "")
    })
    row_count = len(rows)
    expected_rows = int(result.expected_rows or getattr(skill, "expected_rows", 1) or 1)
    required_fields = list(result.required_fields or getattr(skill, "required_fields", ()) or [])
    missing_fields = [
        field for field in required_fields
        if any(not str((row or {}).get(field) or "").strip() for row in rows)
    ]
    verifications = _verification_payload(result.verifications)
    verifications.extend([
        {
            "name": f"{result.source}_row_count",
            "success": row_count >= max(1, expected_rows),
            "expected": max(1, expected_rows),
            "observed": row_count,
            "metadata": {"fields": fields},
        },
        {
            "name": f"{result.source}_required_fields",
            "success": not missing_fields,
            "expected": required_fields,
            "observed": {"missing": missing_fields, "fields": fields},
            "metadata": {"rows": row_count},
        },
    ])
    payload = {
        "schema_version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": str(run_id or ""),
        "input": {
            "url": str(url or ""),
            "goal": str(goal or ""),
        },
        "skill": {
            "name": skill.name,
            "action": skill.action,
            "source": skill.source,
            "capability": skill.capability,
            "aliases": list(skill.aliases),
            "required_fields": required_fields,
            "expected_rows": expected_rows,
        },
        "selection": {
            "score": getattr(skill_match, "score", None),
            "reasons": list(getattr(skill_match, "reasons", []) or []),
            "history_runs": getattr(skill_match, "history_runs", 0),
            "history_success_rate": getattr(skill_match, "history_success_rate", None),
        },
        "result": {
            "source": result.source,
            "row_count": row_count,
            "fields": fields,
            "rows": rows,
            "metadata": dict(result.metadata or {}),
            "output_file": str(output_file or ""),
        },
        "verifications": verifications,
    }
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{datetime.now():%Y%m%d_%H%M%S_%f}_"
        f"{_safe_slug(skill.name)}_"
        f"{_safe_slug(result.source)}.json"
    )
    path = folder / filename
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return path


def load_skill_replay_snapshot(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def list_skill_replays(
    directory: Path | str = DEFAULT_SKILL_REPLAY_DIR,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    folder = Path(directory)
    if not folder.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:max(0, int(limit))]:
        try:
            payload = load_skill_replay_snapshot(path)
        except Exception:
            continue
        items.append({
            "path": str(path),
            "created_at": payload.get("created_at", ""),
            "skill": (payload.get("skill") or {}).get("name", ""),
            "source": (payload.get("skill") or {}).get("source", ""),
            "row_count": (payload.get("result") or {}).get("row_count", 0),
            "score": (payload.get("selection") or {}).get("score"),
            "verification_failures": sum(
                1 for item in payload.get("verifications") or []
                if item.get("success") is False
            ),
        })
    return items


def replay_skill_snapshot(path: Path | str) -> dict[str, Any]:
    payload = load_skill_replay_snapshot(path)
    input_payload = payload.get("input") or {}
    expected_skill = (payload.get("skill") or {}).get("name", "")
    registry = build_default_skill_registry(load_history=False)
    ranked = registry.ranked_matches(
        url=str(input_payload.get("url") or ""),
        goal=str(input_payload.get("goal") or ""),
    )
    current_first = ranked[0] if ranked else None
    rows = (payload.get("result") or {}).get("rows") or []
    required_fields = (payload.get("skill") or {}).get("required_fields") or []
    expected_rows = int((payload.get("skill") or {}).get("expected_rows") or 1)
    missing_fields = [
        field for field in required_fields
        if any(not str((row or {}).get(field) or "").strip() for row in rows)
    ]
    verification_failures = [
        item for item in payload.get("verifications") or []
        if item.get("success") is False
    ]
    match_ok = bool(current_first and current_first.skill.name == expected_skill)
    row_ok = len(rows) >= max(1, expected_rows)
    field_ok = not missing_fields
    replay_ok = match_ok and row_ok and field_ok and not verification_failures
    return {
        "path": str(path),
        "ok": replay_ok,
        "expected_skill": expected_skill,
        "current_first_match": current_first.skill.name if current_first else "",
        "current_score": current_first.score if current_first else None,
        "current_reasons": current_first.reasons if current_first else [],
        "row_count": len(rows),
        "expected_rows": expected_rows,
        "missing_fields": missing_fields,
        "verification_failures": verification_failures,
    }


def check_skill_replays(
    directory: Path | str = DEFAULT_SKILL_REPLAY_DIR,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    items = list_skill_replays(directory, limit=limit)
    results: list[dict[str, Any]] = []
    for item in items:
        path = item.get("path", "")
        if not path:
            continue
        try:
            result = replay_skill_snapshot(path)
        except Exception as exc:
            result = {
                "path": str(path),
                "ok": False,
                "expected_skill": str(item.get("skill") or ""),
                "current_first_match": "",
                "current_score": None,
                "current_reasons": [],
                "row_count": int(item.get("row_count") or 0),
                "expected_rows": 0,
                "missing_fields": [],
                "verification_failures": [],
                "error": repr(exc),
            }
        results.append(result)
    by_skill: dict[str, dict[str, Any]] = {}
    for result in results:
        skill_name = str(result.get("expected_skill") or "unknown")
        item = by_skill.setdefault(skill_name, {"checked": 0, "passed": 0, "failed": 0})
        item["checked"] += 1
        if result.get("ok"):
            item["passed"] += 1
        else:
            item["failed"] += 1
    failed = [item for item in results if not item.get("ok")]
    return {
        "checked": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "ok": not failed,
        "by_skill": dict(sorted(by_skill.items())),
        "results": results,
    }


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect and replay skill snapshots.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list skill replay snapshots")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--json", action="store_true")

    show_parser = subparsers.add_parser("show", help="show a skill replay snapshot")
    show_parser.add_argument("path")

    replay_parser = subparsers.add_parser("replay", help="offline-check a skill replay snapshot")
    replay_parser.add_argument("path")
    replay_parser.add_argument("--json", action="store_true")

    check_parser = subparsers.add_parser("check", help="offline-check recent skill replay snapshots")
    check_parser.add_argument("--limit", type=int, default=20)
    check_parser.add_argument("--json", action="store_true")
    check_parser.add_argument("--show-passed", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "list":
        items = list_skill_replays(limit=args.limit)
        if args.json:
            _print_json({"count": len(items), "items": items})
        else:
            if not items:
                print("No skill replay snapshots.")
            for item in items:
                print(
                    f"{item['created_at']} {item['skill']} rows={item['row_count']} "
                    f"score={item['score']} failures={item['verification_failures']} {item['path']}"
                )
        return 0
    if args.command == "show":
        _print_json(load_skill_replay_snapshot(args.path))
        return 0
    if args.command == "replay":
        result = replay_skill_snapshot(args.path)
        if args.json:
            _print_json(result)
        else:
            print(
                f"ok={result['ok']} skill={result['expected_skill']} "
                f"current={result['current_first_match']} rows={result['row_count']}/{result['expected_rows']} "
                f"missing={result['missing_fields']} failures={len(result['verification_failures'])}"
            )
        return 0 if result["ok"] else 1
    if args.command == "check":
        summary = check_skill_replays(limit=args.limit)
        if args.json:
            _print_json(summary)
        else:
            print(
                f"ok={summary['ok']} checked={summary['checked']} "
                f"passed={summary['passed']} failed={summary['failed']}"
            )
            for skill_name, item in summary.get("by_skill", {}).items():
                print(
                    f"  {skill_name}: checked={item['checked']} "
                    f"passed={item['passed']} failed={item['failed']}"
                )
            for result in summary.get("results", []):
                if result.get("ok") and not args.show_passed:
                    continue
                status = "PASS" if result.get("ok") else "FAIL"
                print(
                    f"  [{status}] {result.get('expected_skill')} "
                    f"current={result.get('current_first_match')} "
                    f"rows={result.get('row_count')}/{result.get('expected_rows')} "
                    f"missing={result.get('missing_fields') or []} "
                    f"verify_failures={len(result.get('verification_failures') or [])} "
                    f"path={result.get('path')}"
                )
        return 0 if summary["ok"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

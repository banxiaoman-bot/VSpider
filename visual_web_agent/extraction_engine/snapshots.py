"""Compact extraction snapshots and offline replay summaries."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from .selectors import build_selector_fingerprints, score_fingerprint_match
except ImportError:
    from selectors import build_selector_fingerprints, score_fingerprint_match


DEFAULT_SNAPSHOT_DIR = Path("workspace") / "extraction_snapshots"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clip(value: Any, limit: int = 6000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]..."


def _safe_slug(value: str, *, limit: int = 80) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", value or "").strip("._-")
    return (slug or "snapshot")[:limit]


def _row_key(row: Any) -> str:
    if isinstance(row, dict):
        return json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    return str(row)


def _required_field_coverage(
    rows: list[Any],
    requested_fields: list[str],
) -> dict[str, Any]:
    fields = [str(f).strip() for f in requested_fields or [] if str(f).strip()]
    if not fields:
        return {"required_fields": [], "complete_rows": len(rows), "missing_by_field": {}}
    missing_by_field = {field: 0 for field in fields}
    complete_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            for field in fields:
                missing_by_field[field] += 1
            continue
        row_complete = True
        lower_map = {str(k).strip().lower(): v for k, v in row.items()}
        for field in fields:
            value = row.get(field, lower_map.get(field.lower()))
            if value is None or not str(value).strip():
                missing_by_field[field] += 1
                row_complete = False
        if row_complete:
            complete_rows += 1
    return {
        "required_fields": fields,
        "complete_rows": complete_rows,
        "missing_by_field": missing_by_field,
    }


def should_capture_snapshot(url: str = "", goal: str = "") -> bool:
    flag = os.getenv("VSPIDER_EXTRACTION_SNAPSHOTS", "").strip().lower()
    if flag in {"0", "false", "off", "no"}:
        return False
    if flag in {"force", "1", "true", "on", "yes"}:
        return True

    text = f"{url} {goal}".lower()
    if re.search(r"login|signin|auth|password|token|captcha|验证码|登录|密码|认证", text):
        return False
    host = (urlparse(url).hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return False
    if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[0-1])\.)", host):
        return False
    return True


@dataclass
class ExtractionSnapshot:
    url: str
    goal: str
    source: str
    rows: list[Any]
    requested_fields: list[str] = field(default_factory=list)
    output_file: str = ""
    run_id: str = ""
    step: int = 0
    total_rows: int = 0
    accepted_rows: int = 0
    duplicate_rows: int = 0
    rejected_rows: int = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    data_shape: dict[str, Any] = field(default_factory=dict)
    source_text_excerpt: str = ""
    body_text_excerpt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    selector_fingerprints: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["row_count"] = len(self.rows or [])
        data["field_coverage"] = _required_field_coverage(
            self.rows or [],
            self.requested_fields or [],
        )
        data["unique_row_count"] = len({_row_key(row) for row in self.rows or []})
        return data


def build_snapshot(
    *,
    url: str,
    goal: str,
    source: str,
    rows: list[Any],
    requested_fields: list[str] | None = None,
    output_file: str = "",
    run_id: str = "",
    step: int = 0,
    total_rows: int = 0,
    accepted_rows: int = 0,
    duplicate_rows: int = 0,
    rejected_rows: int = 0,
    candidates: list[dict[str, Any]] | None = None,
    data_shape: dict[str, Any] | None = None,
    source_text: str = "",
    body_text: str = "",
    metadata: dict[str, Any] | None = None,
) -> ExtractionSnapshot:
    compact_candidates = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        compact_candidates.append({
            "name": candidate.get("name"),
            "accepted": candidate.get("accepted", 0),
            "duplicates": candidate.get("duplicates", 0),
            "rejected": candidate.get("rejected", 0),
            "score": candidate.get("score", 0),
            "data_signature": candidate.get("data_signature", ""),
            "data_shape": candidate.get("data_shape") or {},
            "recovery": candidate.get("recovery") or {},
        })
    return ExtractionSnapshot(
        url=str(url or ""),
        goal=str(goal or ""),
        source=str(source or ""),
        rows=list(rows or []),
        requested_fields=list(requested_fields or []),
        output_file=str(output_file or ""),
        run_id=str(run_id or ""),
        step=int(step or 0),
        total_rows=int(total_rows or 0),
        accepted_rows=int(accepted_rows or len(rows or [])),
        duplicate_rows=int(duplicate_rows or 0),
        rejected_rows=int(rejected_rows or 0),
        candidates=compact_candidates,
        data_shape=dict(data_shape or {}),
        source_text_excerpt=_clip(source_text, 8000),
        body_text_excerpt=_clip(body_text, 3000),
        metadata=dict(metadata or {}),
        selector_fingerprints=build_selector_fingerprints(
            rows=list(rows or []),
            requested_fields=list(requested_fields or []),
            source=str(source or ""),
            data_shape=dict(data_shape or {}),
        ),
    )


def save_snapshot(
    snapshot: ExtractionSnapshot,
    *,
    directory: str | Path = DEFAULT_SNAPSHOT_DIR,
) -> Path:
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    host = _safe_slug(urlparse(snapshot.url).netloc or "page", limit=40)
    source = _safe_slug(snapshot.source, limit=36)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = out_dir / f"{stamp}_{host}_{source}.json"
    path.write_text(
        json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def maybe_save_snapshot(
    *,
    url: str,
    goal: str,
    source: str,
    rows: list[Any],
    requested_fields: list[str] | None = None,
    output_file: str = "",
    run_id: str = "",
    step: int = 0,
    total_rows: int = 0,
    accepted_rows: int = 0,
    duplicate_rows: int = 0,
    rejected_rows: int = 0,
    candidates: list[dict[str, Any]] | None = None,
    data_shape: dict[str, Any] | None = None,
    source_text: str = "",
    body_text: str = "",
    metadata: dict[str, Any] | None = None,
    directory: str | Path = DEFAULT_SNAPSHOT_DIR,
) -> Path | None:
    if not rows:
        return None
    if not should_capture_snapshot(url, goal):
        return None
    snapshot = build_snapshot(
        url=url,
        goal=goal,
        source=source,
        rows=rows,
        requested_fields=requested_fields,
        output_file=output_file,
        run_id=run_id,
        step=step,
        total_rows=total_rows,
        accepted_rows=accepted_rows,
        duplicate_rows=duplicate_rows,
        rejected_rows=rejected_rows,
        candidates=candidates,
        data_shape=data_shape,
        source_text=source_text,
        body_text=body_text,
        metadata=metadata,
    )
    return save_snapshot(snapshot, directory=directory)


def load_snapshot(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def list_snapshots(directory: str | Path = DEFAULT_SNAPSHOT_DIR) -> list[Path]:
    root = Path(directory)
    if not root.exists():
        return []
    return sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def _snapshot_group_key(data: dict[str, Any]) -> str:
    url = str(data.get("url") or "")
    parsed = urlparse(url)
    host_path = f"{parsed.netloc}{parsed.path}".strip("/") or url
    fields = data.get("requested_fields") if isinstance(data.get("requested_fields"), list) else []
    field_key = ",".join(sorted(str(field).strip().lower() for field in fields if str(field).strip()))
    if not field_key:
        field_key = _safe_slug(str(data.get("goal") or ""), limit=60)
    return f"{host_path}|{field_key}"


def replay_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    requested = data.get("requested_fields") if isinstance(data.get("requested_fields"), list) else []
    candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
    selected = str(data.get("source") or "")
    selected_candidate = next(
        (c for c in candidates if str(c.get("name") or "") == selected),
        None,
    )
    fingerprints = data.get("selector_fingerprints") or build_selector_fingerprints(
        rows=rows,
        requested_fields=requested,
        source=selected,
        data_shape=data.get("data_shape") if isinstance(data.get("data_shape"), dict) else {},
    )
    return {
        "url": data.get("url", ""),
        "source": selected,
        "row_count": len(rows),
        "unique_row_count": len({_row_key(row) for row in rows}),
        "requested_fields": requested,
        "field_coverage": _required_field_coverage(rows, requested),
        "selected_candidate": selected_candidate,
        "candidate_count": len(candidates),
        "selector_fingerprints": {
            "source_family": fingerprints.get("source_family"),
            "signature": fingerprints.get("signature"),
            "fields": fingerprints.get("fields") or [],
            "selector_like": fingerprints.get("selector_like") or [],
            "row_widths": fingerprints.get("row_widths") or {},
        },
        "output_file": data.get("output_file", ""),
    }


def compare_snapshots(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    baseline_replay = replay_snapshot(baseline)
    candidate_replay = replay_snapshot(candidate)

    baseline_fp = baseline.get("selector_fingerprints") or build_selector_fingerprints(
        rows=baseline.get("rows") if isinstance(baseline.get("rows"), list) else [],
        requested_fields=baseline.get("requested_fields")
        if isinstance(baseline.get("requested_fields"), list) else [],
        source=str(baseline.get("source") or ""),
        data_shape=baseline.get("data_shape") if isinstance(baseline.get("data_shape"), dict) else {},
    )
    candidate_fp = candidate.get("selector_fingerprints") or build_selector_fingerprints(
        rows=candidate.get("rows") if isinstance(candidate.get("rows"), list) else [],
        requested_fields=candidate.get("requested_fields")
        if isinstance(candidate.get("requested_fields"), list) else [],
        source=str(candidate.get("source") or ""),
        data_shape=candidate.get("data_shape") if isinstance(candidate.get("data_shape"), dict) else {},
    )
    fingerprint_match = score_fingerprint_match(baseline_fp, candidate_fp)

    baseline_fields = set(baseline_replay.get("requested_fields") or [])
    candidate_fields = set(candidate_replay.get("requested_fields") or [])
    baseline_missing = (baseline_replay.get("field_coverage") or {}).get("missing_by_field") or {}
    candidate_missing = (candidate_replay.get("field_coverage") or {}).get("missing_by_field") or {}

    return {
        "match": bool(fingerprint_match.get("match")),
        "score": fingerprint_match.get("score", 0),
        "reasons": fingerprint_match.get("reasons", []),
        "baseline": {
            "url": baseline_replay.get("url", ""),
            "source": baseline_replay.get("source", ""),
            "row_count": baseline_replay.get("row_count", 0),
            "unique_row_count": baseline_replay.get("unique_row_count", 0),
            "fingerprint_signature": baseline_fp.get("signature", ""),
        },
        "candidate": {
            "url": candidate_replay.get("url", ""),
            "source": candidate_replay.get("source", ""),
            "row_count": candidate_replay.get("row_count", 0),
            "unique_row_count": candidate_replay.get("unique_row_count", 0),
            "fingerprint_signature": candidate_fp.get("signature", ""),
        },
        "row_delta": int(candidate_replay.get("row_count", 0) or 0)
        - int(baseline_replay.get("row_count", 0) or 0),
        "field_delta": {
            "added": sorted(candidate_fields - baseline_fields),
            "removed": sorted(baseline_fields - candidate_fields),
            "baseline_missing_by_field": baseline_missing,
            "candidate_missing_by_field": candidate_missing,
        },
    }


def compare_latest_snapshots(
    directory: str | Path = DEFAULT_SNAPSHOT_DIR,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if int(limit or 0) <= 0:
        return []
    grouped: dict[str, list[Path]] = {}
    for path in list_snapshots(directory):
        try:
            data = load_snapshot(path)
        except Exception:
            continue
        grouped.setdefault(_snapshot_group_key(data), []).append(path)

    results: list[dict[str, Any]] = []
    for group_key, paths in grouped.items():
        if len(paths) < 2:
            continue
        paths = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
        latest, previous = paths[0], paths[1]
        comparison = compare_snapshots(load_snapshot(previous), load_snapshot(latest))
        comparison["group_key"] = group_key
        comparison["baseline_path"] = str(previous)
        comparison["candidate_path"] = str(latest)
        results.append(comparison)

    results.sort(key=lambda item: item.get("candidate_path", ""), reverse=True)
    return results[: int(limit)]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m visual_web_agent.extraction_engine.snapshots",
        description="List, inspect, and replay compact extraction snapshots.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    common_dir = argparse.ArgumentParser(add_help=False)
    common_dir.add_argument("--dir", default=str(DEFAULT_SNAPSHOT_DIR), help="Snapshot directory.")

    sub.add_parser("list", parents=[common_dir], help="List saved snapshots.")

    show = sub.add_parser("show", help="Print one snapshot JSON.")
    show.add_argument("path")

    replay = sub.add_parser("replay", help="Print replay summary for one snapshot.")
    replay.add_argument("path")

    compare = sub.add_parser("compare", help="Compare two snapshots by fingerprint.")
    compare.add_argument("baseline")
    compare.add_argument("candidate")

    latest = sub.add_parser("compare-latest", parents=[common_dir], help="Compare latest pairs per URL/field group.")
    latest.add_argument("--limit", type=int, default=10, help="Maximum comparisons to print.")
    latest.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "list":
        for path in list_snapshots(args.dir):
            data = load_snapshot(path)
            print(
                f"{path} | rows={len(data.get('rows') or [])} "
                f"source={data.get('source')} url={data.get('url')}"
            )
        return 0
    if args.command == "show":
        print(json.dumps(load_snapshot(args.path), ensure_ascii=False, indent=2))
        return 0
    if args.command == "replay":
        print(json.dumps(replay_snapshot(load_snapshot(args.path)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "compare":
        print(json.dumps(
            compare_snapshots(load_snapshot(args.baseline), load_snapshot(args.candidate)),
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    if args.command == "compare-latest":
        results = compare_latest_snapshots(args.dir, limit=args.limit)
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for item in results:
                print(
                    f"{item['group_key']} | match={item['match']} score={item['score']} "
                    f"rows={item['baseline']['row_count']}->{item['candidate']['row_count']} "
                    f"fields+={item['field_delta']['added']} fields-={item['field_delta']['removed']}"
                )
                print(f"  baseline: {item['baseline_path']}")
                print(f"  candidate: {item['candidate_path']}")
        return 0
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

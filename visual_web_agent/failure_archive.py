"""K1: Failure-run archive — persistent record of every run that ended
with ``success=False``, paired with the trajectory artifacts that exist
on disk for that run (HTML log, phase events jsonl, event stream jsonl).

Layout (relative to the project root, alongside the existing ``logs/``
directory):

    runs/
      failed/
        <run_id>.json     # one file per failed run, atomic write

Each ``<run_id>.json`` is a self-contained record with a stable schema:

    {
      "schema_version": 1,
      "run_id":         "20260524_191800",
      "ts":             1748113080.123,        # archive timestamp (epoch s)
      "reason":         "Max steps reached (40)",
      "goal":           "登录后导出余额…",        # truncated to ~400 chars
      "started_at":     1748113000.0,          # run start timestamp (epoch s)
      "duration_s":     80.123,                # convenience derived field
      "exception_type": "TimeoutError",        # None when not from an exception
      "step_count":     40,                    # last step reached, when known
      "paths": {                               # all paths are project-relative
        "html_log":     "logs/run_log_20260524_191800.html",
        "phase_jsonl":  "logs/phase_20260524_191800.jsonl",
        "event_jsonl":  "logs/event_stream_20260524_191800.jsonl"
      }
    }

The ``paths`` block is computed by name convention from ``run_id`` —
``record_failure`` does NOT verify that the files exist on disk. The
frontend / API layer is responsible for filtering the ``paths`` dict by
existence before serving links (``list_failed_runs`` does this).

Why per-run files instead of a single ``failed.jsonl``?
  • Atomic write per run (no half-line tail on crash).
  • Per-run cleanup is a single ``rm`` instead of a rewrite.
  • Easier to inspect manually (``cat runs/failed/<id>.json | jq .``).
  • Listing is ``os.listdir`` + parse, which is fast for the cap we set.

Retention is intentionally NOT implemented here — runs/failed/ may grow
indefinitely. ``list_failed_runs(limit=N)`` only returns the newest N
files but does not delete the rest. A separate cleanup job (cron / CLI
``scripts/cleanup_failed_runs.py``) is the right place for that policy.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Schema constants ──────────────────────────────────────────────────
SCHEMA_VERSION = 1
GOAL_MAX_LEN = 400  # truncation cap for the persisted goal field
REASON_MAX_LEN = 2000  # safety cap; some Python tracebacks are huge
DEFAULT_BASE_DIR_NAME = "runs"
FAILED_SUBDIR_NAME = "failed"


def _project_root() -> Path:
    """Project root = parent of this package (matches artifact_manager.py)."""
    return Path(__file__).resolve().parents[1]


def failed_runs_dir(base_dir: str | Path | None = None) -> Path:
    """Resolve the directory holding ``<run_id>.json`` files.

    ``base_dir`` defaults to ``<project_root>/runs/failed``. Tests pass
    a project-local tmp path here to dodge the Windows ``%LOCALAPPDATA%``
    permission denials we hit elsewhere with pytest's tmp_path fixture.
    """
    if base_dir is None:
        return _project_root() / DEFAULT_BASE_DIR_NAME / FAILED_SUBDIR_NAME
    return Path(base_dir) / FAILED_SUBDIR_NAME


def _safe_truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    s = str(text)
    if len(s) <= limit:
        return s
    # Append a marker so consumers know the field was truncated.
    return s[: max(0, limit - 1)] + "…"


def _derive_paths(run_id: str) -> dict[str, str]:
    """Return canonical (project-relative) paths for the trajectory
    artifacts associated with ``run_id``.

    These mirror the names used by ``HtmlLogger``, ``EventStream``, and
    ``api_server.set_phase_log_run_id``. The strings are returned even
    when the underlying files do not exist; ``list_failed_runs`` filters
    by existence before returning to callers.
    """
    rid = str(run_id or "").strip()
    return {
        "html_log": f"logs/run_log_{rid}.html" if rid else "",
        "phase_jsonl": f"logs/phase_{rid}.jsonl" if rid else "",
        "event_jsonl": f"logs/event_stream_{rid}.jsonl" if rid else "",
    }


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> bool:
    """Write ``payload`` to ``target`` via a temp-file + rename.

    Returns True on success, False on any I/O failure. Best-effort: we
    swallow exceptions because failing to archive a failure should NEVER
    cause a cascading error in the run-end path.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # NamedTemporaryFile is too restrictive on Windows (can't reopen
        # while the handle is held). Use mkstemp + manual write/rename.
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.stem + ".",
            suffix=".tmp",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            # os.replace is atomic on POSIX and Windows when source/dest
            # are on the same filesystem (which they are here).
            os.replace(tmp_name, target)
        except Exception:
            # Best-effort cleanup of the half-written tmp file.
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass
            raise
        return True
    except Exception as exc:
        logger.debug("[FAILURE ARCHIVE] write failed for %s: %s", target, exc)
        return False


def record_failure(
    *,
    run_id: str,
    reason: str,
    goal: str | None = None,
    started_at: float | None = None,
    duration_s: float | None = None,
    exception_type: str | None = None,
    step_count: int | None = None,
    base_dir: str | Path | None = None,
) -> Path | None:
    """Append one failure record to the on-disk archive.

    Required:
      ``run_id`` — same timestamp identifier used for the HTML log /
        phase jsonl / event stream. Empty / blank → return ``None`` (we
        cannot pair an unidentified run with its artifacts).
      ``reason`` — short human-readable failure message. Truncated to
        ~2 KB; can be the message from ``_broadcast_done_safe(False, msg)``
        or a Python traceback summary.

    Optional:
      ``goal`` — the user-visible task description (truncated to ~400 c).
      ``started_at`` — epoch seconds at run start; enables ``duration_s``
        derivation when ``duration_s`` itself is omitted.
      ``duration_s`` — pre-computed run duration; preferred over deriving
        from ``started_at`` because it captures the agent's own clock.
      ``exception_type`` — class name when the failure was an exception.
      ``step_count`` — the last step reached.
      ``base_dir`` — override the runs/ root (tests).

    Returns the ``Path`` of the written record on success, ``None`` on
    bad input or any I/O error (best-effort; never raises).
    """
    rid = str(run_id or "").strip()
    if not rid:
        logger.debug("[FAILURE ARCHIVE] missing run_id, skipping record")
        return None

    now = time.time()
    eff_duration: float | None = None
    if duration_s is not None:
        try:
            eff_duration = float(duration_s)
        except (TypeError, ValueError):
            eff_duration = None
    if eff_duration is None and started_at is not None:
        try:
            eff_duration = max(0.0, now - float(started_at))
        except (TypeError, ValueError):
            eff_duration = None

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": rid,
        "ts": now,
        "reason": _safe_truncate(reason or "", REASON_MAX_LEN),
    }
    if goal is not None:
        payload["goal"] = _safe_truncate(goal, GOAL_MAX_LEN)
    if started_at is not None:
        try:
            payload["started_at"] = float(started_at)
        except (TypeError, ValueError):
            pass
    if eff_duration is not None:
        payload["duration_s"] = eff_duration
    if exception_type is not None:
        payload["exception_type"] = str(exception_type)[:120]
    if step_count is not None:
        try:
            payload["step_count"] = int(step_count)
        except (TypeError, ValueError):
            pass
    payload["paths"] = _derive_paths(rid)

    target_dir = failed_runs_dir(base_dir)
    target = target_dir / f"{rid}.json"
    ok = _atomic_write_json(target, payload)
    return target if ok else None


def _read_one(path: Path) -> dict[str, Any] | None:
    """Parse one ``<run_id>.json`` file. Returns ``None`` on any error
    (including malformed JSON or missing required keys)."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        if "run_id" not in data or "reason" not in data:
            return None
        return data
    except Exception as exc:
        logger.debug("[FAILURE ARCHIVE] read failed for %s: %s", path, exc)
        return None


def list_failed_runs(
    *,
    limit: int = 50,
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` failure records, newest first.

    Each record is the JSON dict from disk, plus an injected
    ``paths_exist`` sub-dict mapping each ``paths`` key to a bool
    indicating whether the file currently exists. The frontend uses this
    to grey out "open HTML log" buttons when the log was deleted.

    ``base_dir`` overrides the ``runs/`` root (tests). ``project_root``
    overrides the path-existence anchor so callers can validate against
    a different filesystem layout (also tests).

    The function is tolerant of:
      • non-JSON files in the directory (skipped)
      • truncated / malformed JSON (skipped, logged at DEBUG)
      • a missing directory (returns empty list)
    """
    target_dir = failed_runs_dir(base_dir)
    if not target_dir.exists() or not target_dir.is_dir():
        return []

    root = Path(project_root) if project_root is not None else _project_root()

    entries: list[tuple[float, dict[str, Any]]] = []
    for entry in target_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() != ".json":
            continue
        rec = _read_one(entry)
        if rec is None:
            continue
        ts = rec.get("ts")
        # Use mtime as a fallback ordering key when ts is missing or non-numeric.
        try:
            sort_key = float(ts) if isinstance(ts, (int, float)) else entry.stat().st_mtime
        except Exception:
            sort_key = 0.0
        # Annotate with file existence under the project root.
        paths = rec.get("paths") or {}
        exist_map: dict[str, bool] = {}
        if isinstance(paths, dict):
            for k, v in paths.items():
                if not isinstance(v, str) or not v:
                    exist_map[k] = False
                    continue
                try:
                    exist_map[k] = (root / v).exists()
                except Exception:
                    exist_map[k] = False
        rec["paths_exist"] = exist_map
        entries.append((sort_key, rec))

    entries.sort(key=lambda t: t[0], reverse=True)
    n = max(0, int(limit or 0))
    return [rec for _ts, rec in entries[:n]] if n else [rec for _ts, rec in entries]


# ── K5: retention / cleanup ───────────────────────────────────────────
# Two-policy model: caller can specify ``keep_last`` (cap newest N) and/or
# ``older_than_s`` (drop entries with ts older than the threshold). When
# both are set, BOTH must allow deletion for a record to be removed —
# i.e. a record survives if EITHER it is among the N newest OR it is
# within the age window. This matches what most "logrotate"-style users
# expect: "keep at least 20 records AND keep anything newer than 30 days".


# Duration tokens accepted by ``parse_duration``. Kept short so the CLI
# call site reads tersely ("--older-than 30d") and so the regex stays
# trivial to maintain.
_DURATION_UNITS_SECONDS = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 7 * 86400.0,
}


def parse_duration(text: str) -> float:
    """Parse a human-friendly duration string into seconds.

    Accepted forms: ``"90s"``, ``"15m"``, ``"12h"``, ``"30d"``, ``"2w"``.
    The unit is case-insensitive; the numeric portion may be a positive
    integer or float.

    Raises ``ValueError`` on any unparseable input. ``"0d"`` is allowed
    and means "delete everything" — callers who don't want that should
    range-check before calling cleanup.
    """
    if text is None:
        raise ValueError("duration is None")
    s = str(text).strip().lower()
    if not s:
        raise ValueError("duration is empty")
    if s[-1] not in _DURATION_UNITS_SECONDS:
        raise ValueError(
            f"duration unit must be one of {sorted(_DURATION_UNITS_SECONDS)!r}, got {s!r}"
        )
    unit = s[-1]
    try:
        value = float(s[:-1])
    except ValueError as exc:
        raise ValueError(f"duration value is not a number: {s!r}") from exc
    if value < 0:
        raise ValueError(f"duration must be non-negative: {s!r}")
    return value * _DURATION_UNITS_SECONDS[unit]


def _record_age_seconds(rec: dict[str, Any], now: float) -> float | None:
    """How many seconds ago was this record archived? ``None`` when the
    record's ``ts`` field is missing or unparseable — callers treat that
    as 'cannot decide on age, keep the record'."""
    ts = rec.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    try:
        return max(0.0, float(now) - float(ts))
    except (TypeError, ValueError):
        return None


def select_for_deletion(
    records: list[dict[str, Any]],
    *,
    keep_last: int | None = None,
    older_than_s: float | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Pure function — given a list of failure records, decide which
    ones the cleanup pass should delete.

    Inputs are NOT mutated. The input order is irrelevant; we sort by
    ``ts`` internally (newest first).

    Policy:
      • If both ``keep_last`` and ``older_than_s`` are ``None`` → return
        the empty list (nothing to delete; saves the caller from having
        to range-check).
      • If only ``keep_last`` is set → delete everything beyond the N
        newest.
      • If only ``older_than_s`` is set → delete everything with
        ``age > older_than_s``.
      • If both are set → a record dies iff it's BOTH beyond the N
        newest AND older than the age window. (AND semantics — see the
        module docstring above.)

    Records with missing/unparseable ``ts`` are NEVER selected for
    deletion. This is conservative: corrupt entries should stick around
    so the operator can inspect them.
    """
    if keep_last is None and older_than_s is None:
        return []

    eff_now = float(now) if now is not None else time.time()

    # Build a stable sort key — newest first. Records with no ts go to
    # the END so they don't crowd out real-data records at the front
    # when keep_last is small.
    def _sort_key(r: dict[str, Any]) -> tuple[int, float]:
        ts = r.get("ts")
        if isinstance(ts, (int, float)):
            return (0, -float(ts))  # 0 → "has ts", negate for newest-first
        return (1, 0.0)  # 1 → "missing ts", goes last

    sorted_records = sorted(records, key=_sort_key)

    # Cap keep_last at the list size; treat None as infinity.
    if keep_last is not None:
        keep_n = max(0, int(keep_last))
    else:
        keep_n = len(sorted_records)  # everyone is in the "kept by count" set

    to_delete: list[dict[str, Any]] = []
    for idx, rec in enumerate(sorted_records):
        # Never delete records with unparseable ts (see docstring).
        age = _record_age_seconds(rec, eff_now)
        if age is None:
            continue

        beyond_keep_last = idx >= keep_n
        older_than_window = (
            older_than_s is not None and age > float(older_than_s)
        )

        if keep_last is not None and older_than_s is not None:
            # AND semantics: must fail BOTH guards to die.
            if beyond_keep_last and older_than_window:
                to_delete.append(rec)
        elif keep_last is not None:
            if beyond_keep_last:
                to_delete.append(rec)
        elif older_than_s is not None:
            if older_than_window:
                to_delete.append(rec)

    return to_delete


def _delete_record_files(
    rec: dict[str, Any],
    *,
    base_dir: str | Path | None,
    project_root: str | Path | None,
    purge_related: bool,
) -> dict[str, Any]:
    """Delete one record's archive JSON, optionally also the referenced
    HTML / phase / event logs. Returns a per-file outcome dict useful for
    the CLI summary.

    Best-effort: every unlink is wrapped in try/except so a single locked
    file (e.g. Windows handle held open elsewhere) doesn't abort the
    whole cleanup pass.
    """
    rid = str(rec.get("run_id") or "").strip()
    out: dict[str, Any] = {
        "run_id": rid,
        "archive": False,
        "related": {},
        "errors": [],
    }
    if not rid:
        out["errors"].append("missing_run_id")
        return out

    target_dir = failed_runs_dir(base_dir)
    archive_path = target_dir / f"{rid}.json"
    try:
        archive_path.unlink(missing_ok=True)
        out["archive"] = True
    except Exception as exc:
        out["errors"].append(f"unlink_archive: {exc}")

    if purge_related:
        root = Path(project_root) if project_root is not None else _project_root()
        paths = rec.get("paths") or {}
        if isinstance(paths, dict):
            for k, v in paths.items():
                if not isinstance(v, str) or not v:
                    out["related"][k] = False
                    continue
                p = root / v
                try:
                    if p.exists():
                        p.unlink()
                        out["related"][k] = True
                    else:
                        out["related"][k] = False
                except Exception as exc:
                    out["related"][k] = False
                    out["errors"].append(f"unlink_{k}: {exc}")
    return out


def cleanup_failed_runs(
    *,
    keep_last: int | None = None,
    older_than_s: float | None = None,
    purge_related: bool = False,
    dry_run: bool = False,
    base_dir: str | Path | None = None,
    project_root: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply the retention policy and (unless ``dry_run``) delete files.

    Returns a summary dict::

        {
          "scanned":   12,            # records read from disk
          "deleted":   4,             # records selected for removal
          "kept":      8,             # records still present after the pass
          "dry_run":   False,
          "details":   [ {run_id, archive, related, errors}, ... ],
          "errors":    [ "..."],      # any per-record error message strings
        }

    ``dry_run=True`` runs the full selection logic but skips ``unlink``
    calls. The ``details`` block still describes WHAT WOULD be deleted
    (each ``archive: False`` because we didn't touch disk).
    """
    # We read the full list (no limit) so retention sees every record.
    records = list_failed_runs(
        limit=0, base_dir=base_dir, project_root=project_root,
    )
    scanned = len(records)

    to_delete = select_for_deletion(
        records,
        keep_last=keep_last,
        older_than_s=older_than_s,
        now=now,
    )

    details: list[dict[str, Any]] = []
    errors: list[str] = []
    deleted = 0

    for rec in to_delete:
        if dry_run:
            # Synthesize a details entry without touching disk so the
            # caller's summary still shows WHAT would be deleted.
            related_plan = {}
            if purge_related:
                paths = rec.get("paths") or {}
                if isinstance(paths, dict):
                    related_plan = {
                        k: bool((rec.get("paths_exist") or {}).get(k))
                        for k in paths.keys()
                    }
            details.append({
                "run_id": str(rec.get("run_id") or ""),
                "archive": False,
                "related": related_plan,
                "errors": [],
                "would_delete": True,
            })
            deleted += 1
            continue

        outcome = _delete_record_files(
            rec,
            base_dir=base_dir,
            project_root=project_root,
            purge_related=purge_related,
        )
        if outcome["archive"]:
            deleted += 1
        details.append(outcome)
        errors.extend(outcome.get("errors") or [])

    return {
        "scanned": scanned,
        "deleted": deleted,
        "kept": max(0, scanned - deleted),
        "dry_run": bool(dry_run),
        "details": details,
        "errors": errors,
    }

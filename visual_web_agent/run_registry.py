from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .secret_redaction import redact_secret_mapping


_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")  # no "." => blocks ./.. path traversal
_RUN_STATUS = {"queued", "running", "paused", "succeeded", "failed", "stopped", "error"}
_TERMINAL_STATUS = {"succeeded", "failed", "stopped", "error"}


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def registry_root(base_dir: str | Path | None = None) -> Path:
    root = Path(base_dir) if base_dir is not None else project_root() / "runs" / "registry"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_run_id(run_id: str) -> str:
    rid = str(run_id or "").strip()
    if not rid or not _RUN_ID_RE.fullmatch(rid):
        raise ValueError("invalid run_id")
    return rid


def _run_path(run_id: str, base_dir: str | Path | None = None) -> Path:
    return registry_root(base_dir) / f"{_safe_run_id(run_id)}.json"


def _now() -> float:
    return time.time()


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.stem + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, target)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _sanitize_vlm_options(vlm_options: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(vlm_options or {}).items():
        if value in (None, ""):
            continue
        if "api_key" in str(key).lower():
            out[key] = "***"
        else:
            out[key] = value
    return out


def _sanitize_secret_mapping(value: dict[str, Any] | None) -> dict[str, Any]:
    return redact_secret_mapping(value, drop_empty=True)


def _derive_paths(run_id: str) -> dict[str, str]:
    rid = str(run_id or "").strip()
    return {
        "html_log": f"logs/run_log_{rid}.html" if rid else "",
        "phase_jsonl": f"logs/phase_{rid}.jsonl" if rid else "",
        "event_jsonl": f"logs/event_stream_{rid}.jsonl" if rid else "",
        "registry_json": f"runs/registry/{rid}.json" if rid else "",
    }


def _paths_exist(paths: dict[str, str]) -> dict[str, bool]:
    root = project_root()
    out: dict[str, bool] = {}
    for key, rel in paths.items():
        out[key] = bool(rel and (root / rel).exists())
    return out


def create_run(
    *,
    run_id: str,
    target_url: str,
    prompt: str,
    mode: str = "single",
    filename: str = "",
    file_size_kb: float | int = 0.0,
    auth_profiles: str = "",
    vlm_model: str = "",
    semantic_model: str = "",
    vlm_model_type: str = "vl",
    vlm_options: dict[str, Any] | None = None,
    urls: list[str] | None = None,
    constraints: dict[str, Any] | None = None,
    upload_sha256: str = "",
    upload_mime: str = "",
    status: str = "running",
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    rid = _safe_run_id(run_id)
    st = str(status or "running").strip().lower()
    if st not in _RUN_STATUS:
        raise ValueError("invalid run status")
    ts = _now()
    rec: dict[str, Any] = {
        "schema_version": 1,
        "run_id": rid,
        "task_id": rid,
        "status": st,
        "mode": str(mode or "single"),
        "target_url": str(target_url or ""),
        "prompt": str(prompt or ""),
        "filename": str(filename or ""),
        "file_size_kb": round(float(file_size_kb or 0.0), 1),
        "auth_profiles": str(auth_profiles or ""),
        "vlm_model": str(vlm_model or ""),
        "semantic_model": str(semantic_model or ""),
        "vlm_model_type": str(vlm_model_type or "vl"),
        "vlm_options": _sanitize_vlm_options(vlm_options),
        "urls": [str(u) for u in (urls or []) if str(u or "")],
        "constraints": _sanitize_secret_mapping(constraints),
        "upload_sha256": str(upload_sha256 or ""),
        "upload_mime": str(upload_mime or ""),
        "created_at": ts,
        "started_at": ts if st == "running" else None,
        "finished_at": None,
        "duration_s": None,
        "stop_requested": False,
        "error": "",
        "paths": _derive_paths(rid),
    }
    _atomic_write_json(_run_path(rid, base_dir), rec)
    return rec


def load_run(run_id: str, base_dir: str | Path | None = None) -> dict[str, Any] | None:
    try:
        path = _run_path(run_id, base_dir)
    except ValueError:
        return None
    if not path.exists() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    paths = data.get("paths") if isinstance(data.get("paths"), dict) else _derive_paths(str(data.get("run_id") or run_id))
    data["paths"] = paths
    data["paths_exist"] = _paths_exist(paths)
    return data


def update_run(
    run_id: str,
    *,
    status: str | None = None,
    stop_requested: bool | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    try:
        rid = _safe_run_id(run_id)
    except ValueError:
        return None
    rec = load_run(rid, base_dir)
    if not rec:
        return None
    if status is not None:
        st = str(status or "").strip().lower()
        if st not in _RUN_STATUS:
            raise ValueError("invalid run status")
        rec["status"] = st
        if st in _TERMINAL_STATUS and not rec.get("finished_at"):
            rec["finished_at"] = _now()
    if stop_requested is not None:
        rec["stop_requested"] = bool(stop_requested)
    if error is not None:
        rec["error"] = str(error or "")
    if extra:
        for key, value in extra.items():
            if key not in {"run_id", "task_id", "schema_version"}:
                rec[key] = value
    started = rec.get("started_at") or rec.get("created_at")
    finished = rec.get("finished_at")
    if started and finished:
        try:
            rec["duration_s"] = round(float(finished) - float(started), 3)
        except Exception:
            rec["duration_s"] = None
    rec["paths"] = rec.get("paths") if isinstance(rec.get("paths"), dict) else _derive_paths(rid)
    rec.pop("paths_exist", None)
    _atomic_write_json(_run_path(rid, base_dir), rec)
    return load_run(rid, base_dir)


def mark_stop_requested(run_id: str, base_dir: str | Path | None = None) -> dict[str, Any] | None:
    return update_run(run_id, status="stopping" if False else None, stop_requested=True, base_dir=base_dir)


def complete_run(
    run_id: str,
    *,
    success: bool,
    stopped: bool = False,
    error: str = "",
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    if stopped:
        status = "stopped"
    else:
        status = "succeeded" if success else "failed"
    return update_run(run_id, status=status, error=error, base_dir=base_dir)


def list_runs(
    *,
    limit: int = 50,
    status: str | None = None,
    base_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    try:
        n = int(limit)
    except Exception:
        n = 50
    n = max(1, min(n, 500))
    wanted = str(status or "").strip().lower()
    if wanted and wanted not in _RUN_STATUS:
        return []
    items: list[dict[str, Any]] = []
    for path in registry_root(base_dir).glob("*.json"):
        if not path.is_file():
            continue
        rec = load_run(path.stem, base_dir)
        if not rec:
            continue
        if wanted and rec.get("status") != wanted:
            continue
        items.append(rec)
    items.sort(key=lambda r: float(r.get("created_at") or 0), reverse=True)
    return items[:n]


def delete_run(run_id: str, *, base_dir: str | Path | None = None) -> bool:
    """Delete a run's registry record + ``runs/<id>/`` dir + ``logs/*_<id>.*``.

    Pure file removal — NO status guard (callers enforce active-run protection).
    Returns True iff the registry record existed. Raises ValueError on bad id.
    """
    rid = _safe_run_id(run_id)
    reg = registry_root(base_dir)          # .../runs/registry
    runs_root = reg.parent                  # .../runs
    proj = runs_root.parent                 # project root (or tmp in tests)
    reg_path = reg / f"{rid}.json"
    existed = reg_path.exists()
    reg_path.unlink(missing_ok=True)
    run_dir = runs_root / rid
    if run_dir.is_dir():
        shutil.rmtree(run_dir, ignore_errors=True)
    for rel in (f"run_log_{rid}.html", f"phase_{rid}.jsonl", f"event_stream_{rid}.jsonl"):
        try:
            (proj / "logs" / rel).unlink(missing_ok=True)
        except OSError:
            pass
    return existed


def prune_runs(*, keep: int = 100, base_dir: str | Path | None = None) -> list[str]:
    """Keep newest *keep* runs by created_at; delete older **terminal** ones.

    Never deletes non-terminal (running/queued/paused) runs even if old.
    Returns deleted run_ids.
    """
    try:
        k = max(0, int(keep))
    except (TypeError, ValueError):
        k = 100
    recs: list[dict[str, Any]] = []
    for path in registry_root(base_dir).glob("*.json"):
        if not path.is_file():
            continue
        rec = load_run(path.stem, base_dir)
        if rec:
            recs.append(rec)
    recs.sort(key=lambda r: float(r.get("created_at") or 0), reverse=True)
    deleted: list[str] = []
    for rec in recs[k:]:
        if rec.get("status") in _TERMINAL_STATUS:
            rid = str(rec.get("run_id") or rec.get("task_id") or "")
            try:
                if rid and delete_run(rid, base_dir=base_dir):
                    deleted.append(rid)
            except ValueError:
                continue
    return deleted

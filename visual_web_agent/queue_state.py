from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def queue_root(base_dir: str | Path | None = None) -> Path:
    root = Path(base_dir) if base_dir is not None else project_root() / "runs" / "queue"
    root.mkdir(parents=True, exist_ok=True)
    return root


def queue_state_path(base_dir: str | Path | None = None) -> Path:
    return queue_root(base_dir) / "state.json"


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


def _public_task(task: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(task, dict):
        return None
    item = {
        "task_id": task.get("task_id"),
        "status": task.get("status", "unknown"),
        "target_url": task.get("target_url", ""),
        "prompt": task.get("prompt", ""),
        "mode": task.get("mode", "single"),
        "filename": task.get("filename", ""),
        "file_size_kb": task.get("file_size_kb", 0.0),
        "created_at": task.get("created_at"),
        "queued_at": task.get("queued_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
    }
    if "position" in task:
        item["position"] = task.get("position")
    return item


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def _execution_task(task: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(task, dict):
        return None
    return {
        "task_id": task.get("task_id"),
        "status": task.get("status", "unknown"),
        "running": bool(task.get("running")),
        "target_url": task.get("target_url", ""),
        "prompt": task.get("prompt", ""),
        "file_path": task.get("file_path", ""),
        "auth_profiles": task.get("auth_profiles", ""),
        "vlm_model": task.get("vlm_model", ""),
        "semantic_model": task.get("semantic_model", ""),
        "vlm_text_only": bool(task.get("vlm_text_only")),
        "mode": task.get("mode", "single"),
        "filename": task.get("filename", ""),
        "file_size_kb": task.get("file_size_kb", 0.0),
        "vlm_model_type": task.get("vlm_model_type", "vl"),
        "vlm_options": _json_safe(task.get("vlm_options") or {}),
        "created_at": task.get("created_at"),
        "queued_at": task.get("queued_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
    }


def public_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    public = dict(snapshot)
    public.pop("execution_queue", None)
    public["current"] = _public_task(snapshot.get("current"))
    queue = snapshot.get("queue") or []
    public["queue"] = [
        item
        for item in (_public_task(task) for task in queue if isinstance(task, dict))
        if item is not None
    ]
    return public


def sanitize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    queue = snapshot.get("queue") or []
    execution_queue = snapshot.get("execution_queue")
    if execution_queue is None:
        execution_queue = queue
    workers = snapshot.get("workers") or []
    return {
        "schema_version": SCHEMA_VERSION,
        "saved_at": time.time(),
        "running": bool(snapshot.get("running")),
        "worker_running": bool(snapshot.get("worker_running")),
        "queue_paused": bool(snapshot.get("queue_paused")),
        "queue_paused_at": snapshot.get("queue_paused_at"),
        "queue_pause_reason": snapshot.get("queue_pause_reason") or "",
        "worker_config": dict(snapshot.get("worker_config") or {}),
        "heartbeat_config": dict(snapshot.get("heartbeat_config") or {}),
        "watchdog_config": dict(snapshot.get("watchdog_config") or {}),
        "active_worker_count": int(snapshot.get("active_worker_count") or 0),
        "stale_worker_count": int(snapshot.get("stale_worker_count") or 0),
        "workers": [dict(w) for w in workers if isinstance(w, dict)],
        "current": _public_task(snapshot.get("current")),
        "queue_length": int(snapshot.get("queue_length") or len(queue)),
        "queue": [
            public
            for public in (_public_task(item) for item in queue if isinstance(item, dict))
            if public is not None
        ],
        "execution_queue": [
            task
            for task in (_execution_task(item) for item in execution_queue if isinstance(item, dict))
            if task is not None
        ],
    }


def save_snapshot(snapshot: dict[str, Any], *, base_dir: str | Path | None = None) -> dict[str, Any]:
    payload = sanitize_snapshot(snapshot)
    path = queue_state_path(base_dir)
    _atomic_write_json(path, payload)
    return payload


def load_snapshot(*, base_dir: str | Path | None = None) -> dict[str, Any] | None:
    path = queue_state_path(base_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def load_public_snapshot(*, base_dir: str | Path | None = None) -> dict[str, Any] | None:
    return public_snapshot(load_snapshot(base_dir=base_dir))

"""VSpider queue management core.

Extracted from api_server.py (Slice 3) — task queue state, enqueueing,
scheduling, retry, recovery, and metrics logic.

This module owns the ``active_tasks`` dict and ``_TASK_LOCK``; callers in
api_server.py import them when needed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from visual_web_agent import run_registry as _run_registry
from visual_web_agent import queue_state as _queue_state

logger = logging.getLogger("vspider.api")

# ── Shared upload directory (same as api_server.py) ──────────────────
TEMP_UPLOAD_DIR = Path(__file__).resolve().parent / "temp_uploads"
TEMP_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── Global task state ────────────────────────────────────────────────
_TASK_LOCK = threading.Lock()
active_tasks: dict[str, Any] = {
    "current_task": None,
    "queue": [],
    "queue_worker_running": False,
    "queue_paused": False,
    "queue_paused_at": None,
    "queue_pause_reason": "",
    "workers": {},
}

_TASK_COOLDOWN_SECONDS = 8.0
_RETRYABLE_RUN_STATUS = {"failed", "stopped", "error"}


# ── Environment helpers ──────────────────────────────────────────────

def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int = 16) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _env_float(name: str, default: float, *, min_value: float = 0.01, max_value: float = 3600.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


# ── Queue configuration ─────────────────────────────────────────────

def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _queue_heartbeat_config() -> dict[str, Any]:
    interval = _env_float("VSPIDER_QUEUE_WORKER_HEARTBEAT_INTERVAL", 5.0, min_value=0.01, max_value=60.0)
    stale_after = _env_float("VSPIDER_QUEUE_WORKER_STALE_SECONDS", 30.0, min_value=interval, max_value=3600.0)
    return {
        "interval_s": round(interval, 3),
        "stale_after_s": round(stale_after, 3),
    }


def _queue_watchdog_scheduler_config() -> dict[str, Any]:
    interval = _env_float("VSPIDER_QUEUE_WATCHDOG_INTERVAL", 15.0, min_value=0.1, max_value=3600.0)
    return {
        "enabled": _env_flag("VSPIDER_QUEUE_WATCHDOG_ENABLED", False),
        "interval_s": round(interval, 3),
    }


def _queue_worker_config() -> dict[str, Any]:
    requested = _env_int("VSPIDER_QUEUE_MAX_WORKERS", 1, min_value=1, max_value=16)
    parallel_enabled = _env_flag("VSPIDER_EXPERIMENTAL_PARALLEL_RUNS", False)
    effective = requested if parallel_enabled else 1
    return {
        "requested_max_workers": requested,
        "effective_max_workers": effective,
        "parallel_enabled": parallel_enabled,
        "safety_cap_active": requested > effective,
    }


# ── Task snapshot / view helpers ─────────────────────────────────────

def _task_snapshot() -> dict[str, Any]:
    with _TASK_LOCK:
        task = active_tasks.get("current_task")
        queue = list(active_tasks.get("queue") or [])
        if not task:
            return {
                "running": False,
                "task_id": None,
                "status": "idle",
                "stop_requested": False,
                "in_cooldown": False,
                "cooldown_remaining": 0.0,
                "queue_length": len(queue),
                "queue_worker_running": bool(active_tasks.get("queue_worker_running")),
                "queue_paused": bool(active_tasks.get("queue_paused")),
            }
        finished_at = task.get("finished_at") or 0.0
        running = bool(task.get("running", False))
        if running or finished_at <= 0:
            cooldown_remaining = 0.0
        else:
            cooldown_remaining = max(
                0.0, _TASK_COOLDOWN_SECONDS - (time.time() - finished_at)
            )
        return {
            "running": running,
            "task_id": task.get("task_id"),
            "status": task.get("status", "unknown"),
            "stop_requested": bool(
                task.get("stop_event") and task["stop_event"].is_set()
            ),
            "in_cooldown": cooldown_remaining > 0,
            "cooldown_remaining": round(cooldown_remaining, 2),
            "queue_length": len(queue),
            "queue_worker_running": bool(active_tasks.get("queue_worker_running")),
            "queue_paused": bool(active_tasks.get("queue_paused")),
        }


def _next_task_id() -> str:
    return f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"


def _public_task_item(task: dict[str, Any], *, position: int | None = None) -> dict[str, Any]:
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
    if position is not None:
        item["position"] = position
    return item


def _active_queue_worker_count_locked() -> int:
    workers = active_tasks.get("workers") or {}
    return sum(
        1
        for worker in workers.values()
        if str(worker.get("status") or "") in {"starting", "idle", "running", "paused"}
    )


def _refresh_queue_worker_running_locked() -> None:
    active_tasks["queue_worker_running"] = _active_queue_worker_count_locked() > 0


def _public_worker_item(worker: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    now_ts = time.time() if now is None else float(now)
    item = dict(worker or {})
    try:
        age = max(0.0, now_ts - float(item.get("last_seen_at") or 0.0))
    except Exception:
        age = 0.0
    status = str(item.get("status") or "")
    stale_after = float(_queue_heartbeat_config().get("stale_after_s") or 30.0)
    item["heartbeat_age_s"] = round(age, 3)
    item["stale"] = status in {"starting", "idle", "running", "paused"} and age > stale_after
    return item


def _queue_snapshot() -> dict[str, Any]:
    with _TASK_LOCK:
        current = active_tasks.get("current_task")
        queue = list(active_tasks.get("queue") or [])
        workers = list((active_tasks.get("workers") or {}).values())
        worker_config = _queue_worker_config()
        heartbeat_config = _queue_heartbeat_config()
        watchdog_config = _queue_watchdog_scheduler_config()
        now_ts = time.time()
        public_workers = sorted(
            [_public_worker_item(worker, now=now_ts) for worker in workers],
            key=lambda item: str(item.get("worker_id") or ""),
        )
        return {
            "running": bool(current and current.get("running")),
            "worker_running": bool(active_tasks.get("queue_worker_running")),
            "queue_paused": bool(active_tasks.get("queue_paused")),
            "queue_paused_at": active_tasks.get("queue_paused_at"),
            "queue_pause_reason": active_tasks.get("queue_pause_reason") or "",
            "worker_config": worker_config,
            "heartbeat_config": heartbeat_config,
            "watchdog_config": watchdog_config,
            "active_worker_count": _active_queue_worker_count_locked(),
            "stale_worker_count": sum(1 for worker in public_workers if worker.get("stale")),
            "workers": public_workers,
            "current": _public_task_item(current) if current else None,
            "queue_length": len(queue),
            "queue": [
                _public_task_item(task, position=idx + 1)
                for idx, task in enumerate(queue)
            ],
        }


def _age_seconds(now_ts: float, value: Any) -> float | None:
    try:
        ts = float(value)
    except Exception:
        return None
    if ts <= 0:
        return None
    return round(max(0.0, now_ts - ts), 3)


# ── Queue metrics ────────────────────────────────────────────────────

def queue_metrics(*, run_limit: int = 200) -> dict[str, Any]:
    now_ts = time.time()
    snapshot = _queue_snapshot()
    queue_items = [item for item in (snapshot.get("queue") or []) if isinstance(item, dict)]
    workers = [item for item in (snapshot.get("workers") or []) if isinstance(item, dict)]
    queued_ages = [
        age
        for age in (_age_seconds(now_ts, item.get("queued_at") or item.get("created_at")) for item in queue_items)
        if age is not None
    ]
    current = snapshot.get("current") if isinstance(snapshot.get("current"), dict) else None
    worker_status_counts: dict[str, int] = {}
    for worker in workers:
        status = str(worker.get("status") or "unknown")
        worker_status_counts[status] = worker_status_counts.get(status, 0) + 1

    try:
        runs = _run_registry.list_runs(limit=run_limit)
    except Exception as exc:
        logger.debug("[QUEUE METRICS] list_runs failed: %s", exc)
        runs = []
    run_status_counts: dict[str, int] = {}
    retry_count = 0
    retryable_count = 0
    for run in runs:
        status = str(run.get("status") or "unknown")
        run_status_counts[status] = run_status_counts.get(status, 0) + 1
        if run.get("retry_of"):
            retry_count += 1
        if status in _RETRYABLE_RUN_STATUS:
            retryable_count += 1

    return {
        "generated_at": now_ts,
        "queue": {
            "length": int(snapshot.get("queue_length") or 0),
            "paused": bool(snapshot.get("queue_paused")),
            "pause_reason": snapshot.get("queue_pause_reason") or "",
            "worker_running": bool(snapshot.get("worker_running")),
            "running": bool(snapshot.get("running")),
        },
        "workers": {
            "active_count": int(snapshot.get("active_worker_count") or 0),
            "stale_count": int(snapshot.get("stale_worker_count") or 0),
            "status_counts": worker_status_counts,
        },
        "timing": {
            "current_runtime_s": _age_seconds(now_ts, current.get("started_at")) if current else None,
            "oldest_queued_age_s": max(queued_ages) if queued_ages else None,
            "average_queued_age_s": round(sum(queued_ages) / len(queued_ages), 3) if queued_ages else None,
        },
        "registry": {
            "sample_size": len(runs),
            "status_counts": run_status_counts,
            "retry_count": retry_count,
            "retryable_count": retryable_count,
        },
    }


# ── Persistence ──────────────────────────────────────────────────────

def _persist_queue_snapshot_safe() -> None:
    try:
        snapshot = _queue_snapshot()
        with _TASK_LOCK:
            snapshot["execution_queue"] = [
                dict(task)
                for task in (active_tasks.get("queue") or [])
                if isinstance(task, dict)
            ]
        _queue_state.save_snapshot(snapshot)
    except Exception as exc:
        logger.debug("[QUEUE STATE] persist failed: %s", exc)


# ── Task creation ────────────────────────────────────────────────────

def _create_task_control(
    target_url: str,
    prompt: str,
    file_path: str,
    auth_profiles: str = "",
    vlm_model: str = "",
    semantic_model: str = "",
    vlm_text_only: bool = False,
    *,
    mode: str = "single",
    filename: str = "",
    file_size_kb: float = 0.0,
    vlm_model_type: str = "vl",
    vlm_options: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> tuple[str, threading.Event]:
    task_id = task_id or _next_task_id()
    stop_event = threading.Event()
    now = time.time()
    with _TASK_LOCK:
        active_tasks["current_task"] = {
            "task_id": task_id,
            "target_url": target_url,
            "prompt": prompt,
            "file_path": file_path,
            "auth_profiles": auth_profiles,
            "vlm_model": vlm_model,
            "semantic_model": semantic_model,
            "vlm_text_only": vlm_text_only,
            "status": "running",
            "running": True,
            "created_at": now,
            "started_at": now,
            "stop_event": stop_event,
            "mode": mode,
            "filename": filename,
            "file_size_kb": round(float(file_size_kb or 0.0), 1),
            "vlm_model_type": vlm_model_type,
            "vlm_options": vlm_options or {},
        }
    try:
        if _run_registry.load_run(task_id) is None:
            _run_registry.create_run(
                run_id=task_id,
                target_url=target_url,
                prompt=prompt,
                mode=mode,
                filename=filename,
                file_size_kb=file_size_kb,
                auth_profiles=auth_profiles,
                vlm_model=vlm_model,
                semantic_model=semantic_model,
                vlm_model_type=vlm_model_type,
                vlm_options=vlm_options,
            )
        else:
            _run_registry.update_run(task_id, status="running", extra={"started_at": now})
    except Exception as exc:
        logger.debug("[RUN REGISTRY] create_run failed for %s: %s", task_id, exc)
    return task_id, stop_event


# ── Enqueue ──────────────────────────────────────────────────────────

def _enqueue_task(
    *,
    target_url: str,
    prompt: str,
    file_path: str,
    auth_profiles: str = "",
    vlm_model: str = "",
    semantic_model: str = "",
    vlm_text_only: bool = False,
    mode: str = "single",
    filename: str = "",
    file_size_kb: float = 0.0,
    vlm_model_type: str = "vl",
    vlm_options: dict[str, Any] | None = None,
    urls: list[str] | None = None,
    upload_sha256: str = "",
    upload_mime: str = "",
    constraints: dict[str, Any] | None = None,
    attachment_intent: str = "",
) -> tuple[dict[str, Any], bool]:
    task_id = _next_task_id()
    now = time.time()
    item = {
        "task_id": task_id,
        "target_url": target_url,
        "prompt": prompt,
        "file_path": file_path,
        "auth_profiles": auth_profiles,
        "vlm_model": vlm_model,
        "semantic_model": semantic_model,
        "vlm_text_only": vlm_text_only,
        "mode": mode,
        "filename": filename,
        "file_size_kb": round(float(file_size_kb or 0.0), 1),
        "vlm_model_type": vlm_model_type,
        "vlm_options": vlm_options or {},
        "urls": list(urls or []),
        "upload_sha256": upload_sha256,
        "upload_mime": upload_mime,
        "constraints": dict(constraints or {}),
        "attachment_intent": str(attachment_intent or "").strip(),
        "status": "queued",
        "running": False,
        "created_at": now,
        "queued_at": now,
    }
    with _TASK_LOCK:
        active_tasks.setdefault("queue", []).append(item)
        should_start_worker = _active_queue_worker_count_locked() <= 0
        if should_start_worker:
            active_tasks["queue_worker_running"] = True
    try:
        _run_registry.create_run(
            run_id=task_id,
            target_url=target_url,
            prompt=prompt,
            mode=mode,
            filename=filename,
            file_size_kb=file_size_kb,
            auth_profiles=auth_profiles,
            vlm_model=vlm_model,
            semantic_model=semantic_model,
            vlm_model_type=vlm_model_type,
            vlm_options=vlm_options,
            urls=list(urls or []),
            constraints=dict(constraints or {}),
            upload_sha256=upload_sha256,
            upload_mime=upload_mime,
            status="queued",
        )
    except Exception as exc:
        logger.debug("[RUN REGISTRY] queued create_run failed for %s: %s", task_id, exc)
    _persist_queue_snapshot_safe()
    return item, should_start_worker


# ── Queue operations ─────────────────────────────────────────────────

def cancel_queued_task(task_id: str) -> tuple[bool, str]:
    tid = str(task_id or "").strip()
    if not tid:
        return False, "task_id is required"
    cancelled = False
    with _TASK_LOCK:
        queue = active_tasks.get("queue") or []
        for idx, task in enumerate(list(queue)):
            if str(task.get("task_id") or "") == tid:
                queue.pop(idx)
                task["status"] = "stopped"
                task["finished_at"] = time.time()
                try:
                    _run_registry.update_run(tid, status="stopped", error="cancelled before start")
                except Exception as exc:
                    logger.debug("[RUN REGISTRY] cancel queued update failed: %s", exc)
                cancelled = True
                break
    if cancelled:
        _persist_queue_snapshot_safe()
        return True, "队列任务已取消"
    return False, "队列中未找到该任务"


def pause_task_queue(reason: str = "") -> dict[str, Any]:
    now_ts = time.time()
    with _TASK_LOCK:
        active_tasks["queue_paused"] = True
        active_tasks["queue_paused_at"] = now_ts
        active_tasks["queue_pause_reason"] = str(reason or "").strip()
        for worker in (active_tasks.get("workers") or {}).values():
            if str(worker.get("status") or "") in {"starting", "idle"}:
                worker["status"] = "paused"
                worker["last_seen_at"] = now_ts
        _refresh_queue_worker_running_locked()
    _persist_queue_snapshot_safe()
    return _queue_snapshot()


def resume_task_queue() -> tuple[dict[str, Any], bool]:
    with _TASK_LOCK:
        active_tasks["queue_paused"] = False
        active_tasks["queue_paused_at"] = None
        active_tasks["queue_pause_reason"] = ""
        for worker in (active_tasks.get("workers") or {}).values():
            if str(worker.get("status") or "") == "paused":
                worker["status"] = "idle"
                worker["last_seen_at"] = time.time()
        should_start_worker = bool(active_tasks.get("queue")) and _active_queue_worker_count_locked() <= 0
        if should_start_worker:
            active_tasks["queue_worker_running"] = True
        else:
            _refresh_queue_worker_running_locked()
    _persist_queue_snapshot_safe()
    return _queue_snapshot(), should_start_worker


def request_stop_current_task() -> tuple[bool, str]:
    with _TASK_LOCK:
        task = active_tasks.get("current_task")
        if not task or not task.get("running"):
            return False, "当前没有运行中的任务"

        stop_event = task.get("stop_event")
        if stop_event and not stop_event.is_set():
            stop_event.set()
            task["status"] = "stopping"
            try:
                _run_registry.mark_stop_requested(str(task.get("task_id") or ""))
            except Exception as exc:
                logger.debug("[RUN REGISTRY] mark_stop_requested failed: %s", exc)
            return True, "停止信号已发送"

        return True, "停止信号已存在，任务正在终止"


# ── Retry helpers ────────────────────────────────────────────────────

def _retry_vlm_options_from_run(run: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for key, value in dict(run.get("vlm_options") or {}).items():
        if value in (None, ""):
            continue
        if "api_key" in str(key).lower() and str(value) == "***":
            continue
        options[str(key)] = value
    return options


def _drop_redacted_secret_values(mapping: Any) -> dict[str, Any]:
    restored: dict[str, Any] = {}
    for key, value in dict(mapping or {}).items():
        lowered = str(key).lower()
        is_secret = any(part in lowered for part in ("api_key", "password", "secret", "token"))
        if is_secret and str(value) == "***":
            continue
        restored[str(key)] = value
    return restored


def _read_json_file_if_present(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _load_run_contract_bundle(run_id: str) -> dict[str, Any]:
    """Read run-scoped contracts without creating missing run directories."""
    empty = {
        "input_contract": None,
        "output_contract": None,
        "manifest": None,
        "summary": {
            "has_input_contract": False,
            "has_output_contract": False,
            "has_manifest": False,
            "manifest_items": 0,
            "child_runs": 0,
        },
        "paths": {},
    }
    try:
        rid = _run_registry._safe_run_id(run_id)  # type: ignore[attr-defined]
    except Exception:
        return dict(empty)

    try:
        from visual_web_agent.io_contract import persistence as _io_persistence

        runs_root = _io_persistence.default_runs_root()
        run_path = runs_root / rid
        read_paths = {
            "input_contract": run_path / _io_persistence.INPUT_CONTRACT_FILENAME,
            "output_contract": run_path / _io_persistence.OUTPUT_CONTRACT_FILENAME,
            "manifest": run_path / _io_persistence.MANIFEST_FILENAME,
            "artifacts_dir": run_path / _io_persistence.ARTIFACTS_DIRNAME,
        }
        paths = {
            "input_contract": f"runs/{rid}/{_io_persistence.INPUT_CONTRACT_FILENAME}",
            "output_contract": f"runs/{rid}/{_io_persistence.OUTPUT_CONTRACT_FILENAME}",
            "manifest": f"runs/{rid}/{_io_persistence.MANIFEST_FILENAME}",
            "artifacts_dir": f"runs/{rid}/{_io_persistence.ARTIFACTS_DIRNAME}",
        }
    except Exception:
        return dict(empty)

    input_contract = _read_json_file_if_present(read_paths["input_contract"])
    output_contract = _read_json_file_if_present(read_paths["output_contract"])
    manifest = _read_json_file_if_present(read_paths["manifest"])

    items = manifest.get("items") if isinstance(manifest, dict) else []
    item_list = [item for item in (items or []) if isinstance(item, dict)] if isinstance(items, list) else []
    child_runs = [
        item for item in item_list
        if isinstance(item.get("extra"), dict)
        and item.get("extra", {}).get("entry_type") == "child_run"
    ]
    artifacts_dir = read_paths["artifacts_dir"]
    artifact_count = 0
    if artifacts_dir.exists() and artifacts_dir.is_dir():
        try:
            artifact_count = sum(1 for p in artifacts_dir.rglob("*") if p.is_file())
        except Exception:
            artifact_count = 0

    return {
        "input_contract": input_contract,
        "output_contract": output_contract,
        "manifest": manifest,
        "summary": {
            "has_input_contract": input_contract is not None,
            "has_output_contract": output_contract is not None,
            "has_manifest": manifest is not None,
            "manifest_items": len(item_list),
            "child_runs": len(child_runs),
            "artifacts": artifact_count,
        },
        "paths": paths,
    }


# ── Input contract helpers (for retry) ───────────────────────────────

def _retry_input_contract(run_id: str) -> dict[str, Any]:
    try:
        bundle = _load_run_contract_bundle(run_id)
    except Exception:
        return {}
    contract = bundle.get("input_contract") if isinstance(bundle, dict) else None
    return contract if isinstance(contract, dict) else {}


def _input_contract_urls(contract: dict[str, Any]) -> list[str]:
    urls = contract.get("urls") if isinstance(contract, dict) else []
    out: list[str] = []
    for item in urls or []:
        if isinstance(item, dict):
            value = str(item.get("url") or "").strip()
        else:
            value = str(item or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def _url_identity(value: str) -> str:
    return str(value or "").strip().lower().rstrip("/")


def _input_contract_extra_urls(contract_urls: list[str], target_url: str) -> list[str]:
    target_key = _url_identity(target_url)
    out: list[str] = []
    for url in contract_urls:
        if not url:
            continue
        if target_key and _url_identity(url) == target_key:
            continue
        if url not in out:
            out.append(url)
    return out


def _input_contract_auth_profiles(contract: dict[str, Any]) -> str:
    profiles = contract.get("auth_profiles") if isinstance(contract, dict) else []
    if isinstance(profiles, str):
        return profiles.strip()
    if isinstance(profiles, list):
        return ",".join(str(p).strip() for p in profiles if str(p).strip())
    return ""


def _input_contract_primary_attachment(contract: dict[str, Any]) -> dict[str, Any]:
    attachments = contract.get("attachments") if isinstance(contract, dict) else []
    for item in attachments or []:
        if isinstance(item, dict) and (item.get("path") or item.get("filename")):
            return item
    return {}


# ── Retry as queued task ─────────────────────────────────────────────

def retry_run_as_queued_task(run_id: str) -> tuple[bool, str, dict[str, Any]]:
    rid = str(run_id or "").strip()
    if not rid:
        return False, "run_id is required", {}
    run = _run_registry.load_run(rid)
    if not run:
        return False, "run not found", {}
    source_status = str(run.get("status") or "").lower()
    if source_status not in _RETRYABLE_RUN_STATUS:
        return False, f"run status is not retryable: {source_status or 'unknown'}", {"source": run}
    input_contract = _retry_input_contract(rid)
    contract_urls = _input_contract_urls(input_contract)
    target_url = str(run.get("target_url") or "").strip() or (contract_urls[0] if contract_urls else "")
    prompt = str(run.get("prompt") or "").strip() or str(input_contract.get("goal") or "").strip()
    if not target_url or not prompt:
        return False, "run is missing target_url or prompt", {"source": run}

    contract_attachment = _input_contract_primary_attachment(input_contract)
    attachment_path = str(contract_attachment.get("path") or "").strip()
    mode = str(run.get("mode") or ("batch" if contract_attachment else "single"))
    filename = (
        str(run.get("filename") or "").strip()
        or str(contract_attachment.get("filename") or "").strip()
        or (Path(attachment_path).name if attachment_path else "")
    )
    file_size_kb = float(run.get("file_size_kb") or 0.0)
    if not file_size_kb and contract_attachment.get("size"):
        try:
            file_size_kb = float(contract_attachment.get("size") or 0.0) / 1024.0
        except Exception:
            file_size_kb = 0.0
    upload_sha256 = str(run.get("upload_sha256") or contract_attachment.get("sha256") or "")
    upload_mime = str(run.get("upload_mime") or contract_attachment.get("mime") or "")
    retry_attachment_intent = str(
        run.get("attachment_intent") or contract_attachment.get("intent") or ""
    ).strip()
    if retry_attachment_intent == "unknown":
        retry_attachment_intent = ""
    file_path = ""
    if mode == "batch" and (filename or attachment_path):
        candidates: list[Path] = []
        if filename:
            candidates.append(TEMP_UPLOAD_DIR / filename)
        if attachment_path:
            candidates.append(Path(attachment_path))
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                file_path = str(candidate)
                break
        if not file_path:
            return False, "batch retry file is missing", {"source": run}

    retry_urls = [str(u) for u in (run.get("urls") or []) if str(u or "")]
    if not retry_urls:
        retry_urls = _input_contract_extra_urls(contract_urls, target_url)
    retry_constraints = _drop_redacted_secret_values(run.get("constraints"))
    if not retry_constraints:
        retry_constraints = _drop_redacted_secret_values(input_contract.get("constraints"))

    item, should_start_worker = _enqueue_task(
        target_url=target_url,
        prompt=prompt,
        file_path=file_path,
        auth_profiles=str(run.get("auth_profiles") or "").strip() or _input_contract_auth_profiles(input_contract),
        vlm_model=str(run.get("vlm_model") or ""),
        semantic_model=str(run.get("semantic_model") or ""),
        vlm_text_only=str(run.get("vlm_model_type") or "").lower() == "text",
        mode=mode,
        filename=filename,
        file_size_kb=file_size_kb,
        vlm_model_type=str(run.get("vlm_model_type") or "vl"),
        vlm_options=_retry_vlm_options_from_run(run),
        urls=retry_urls,
        constraints=retry_constraints,
        upload_sha256=upload_sha256,
        upload_mime=upload_mime,
        attachment_intent=retry_attachment_intent,
    )
    try:
        _run_registry.update_run(
            str(item.get("task_id") or ""),
            extra={
                "retry_of": rid,
                "retry_source_status": source_status,
                "retry_queued_at": time.time(),
            },
        )
    except Exception as exc:
        logger.debug("[RUN REGISTRY] retry metadata update failed for %s: %s", item.get("task_id"), exc)
    return True, "retry task queued", {
        "source_run_id": rid,
        "source_status": source_status,
        "task": _public_task_item(item),
        "task_id": item.get("task_id"),
        "should_start_worker": should_start_worker,
    }


# ── Recovery ─────────────────────────────────────────────────────────

def recover_queued_tasks() -> dict[str, Any]:
    snapshot = _queue_state.load_snapshot()
    if not snapshot:
        return {
            "recovered_count": 0,
            "interrupted_count": 0,
            "recovered_task_ids": [],
            "interrupted_task_ids": [],
            "message": "no persisted queue snapshot",
        }

    recovered: list[dict[str, Any]] = []
    for item in snapshot.get("execution_queue") or snapshot.get("queue") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").lower() != "queued":
            continue
        tid = str(item.get("task_id") or "").strip()
        if not tid:
            continue
        recovered.append({
            "task_id": tid,
            "target_url": str(item.get("target_url") or ""),
            "prompt": str(item.get("prompt") or ""),
            "file_path": str(item.get("file_path") or ""),
            "auth_profiles": str(item.get("auth_profiles") or ""),
            "vlm_model": str(item.get("vlm_model") or ""),
            "semantic_model": str(item.get("semantic_model") or ""),
            "vlm_text_only": bool(item.get("vlm_text_only")),
            "mode": str(item.get("mode") or "single"),
            "filename": str(item.get("filename") or ""),
            "file_size_kb": float(item.get("file_size_kb") or 0.0),
            "vlm_model_type": str(item.get("vlm_model_type") or "vl"),
            "vlm_options": _drop_redacted_secret_values(item.get("vlm_options")),
            "urls": [str(u) for u in (item.get("urls") or []) if str(u or "")],
            "upload_sha256": str(item.get("upload_sha256") or ""),
            "upload_mime": str(item.get("upload_mime") or ""),
            "constraints": _drop_redacted_secret_values(item.get("constraints")),
            "attachment_intent": str(item.get("attachment_intent") or ""),
            "status": "queued",
            "running": False,
            "created_at": item.get("created_at") or time.time(),
            "queued_at": item.get("queued_at") or time.time(),
        })

    interrupted: list[str] = []
    current = snapshot.get("current")
    if isinstance(current, dict):
        current_status = str(current.get("status") or "").lower()
        current_id = str(current.get("task_id") or "").strip()
        if current_id and current_status in {"running", "stopping"}:
            interrupted.append(current_id)
            try:
                _run_registry.update_run(
                    current_id,
                    status="error",
                    error="interrupted before queue recovery",
                )
            except Exception as exc:
                logger.debug("[QUEUE RECOVERY] interrupted update failed: %s", exc)

    with _TASK_LOCK:
        existing = {
            str(item.get("task_id") or "")
            for item in (active_tasks.get("queue") or [])
            if isinstance(item, dict)
        }
        added = []
        for item in recovered:
            if item["task_id"] in existing:
                continue
            active_tasks.setdefault("queue", []).append(item)
            existing.add(item["task_id"])
            added.append(item)
        active_tasks["current_task"] = None
        active_tasks["workers"] = {}
        active_tasks["queue_worker_running"] = False

    _persist_queue_snapshot_safe()
    return {
        "recovered_count": len(added),
        "interrupted_count": len(interrupted),
        "recovered_task_ids": [item["task_id"] for item in added],
        "interrupted_task_ids": interrupted,
        "message": "queue snapshot recovered",
    }


# ── Watchdog ─────────────────────────────────────────────────────────

def scan_stale_queue_workers() -> dict[str, Any]:
    now_ts = time.time()
    stale_workers: list[dict[str, Any]] = []
    affected_task_ids: list[str] = []
    stale_error = "worker heartbeat stale"
    with _TASK_LOCK:
        workers = active_tasks.get("workers") or {}
        current = active_tasks.get("current_task")
        for worker_id, worker in workers.items():
            public = _public_worker_item(worker, now=now_ts)
            if not public.get("stale"):
                continue
            task_id = str(worker.get("task_id") or "").strip()
            worker["status"] = "error"
            worker["error"] = (
                f"{stale_error}: heartbeat_age_s={public.get('heartbeat_age_s')}"
            )
            worker["stopped_at"] = now_ts
            worker["last_seen_at"] = worker.get("last_seen_at") or now_ts
            public["status"] = "error"
            public["error"] = worker["error"]
            public["stopped_at"] = now_ts
            stale_workers.append(public)
            if task_id:
                affected_task_ids.append(task_id)
                if current and str(current.get("task_id") or "") == task_id:
                    current["status"] = "error"
                    current["running"] = False
                    current["finished_at"] = now_ts
                    current["error"] = stale_error
        _refresh_queue_worker_running_locked()

    unique_task_ids = sorted(set(affected_task_ids))
    for task_id in unique_task_ids:
        try:
            _run_registry.update_run(task_id, status="error", error=stale_error)
        except Exception as exc:
            logger.debug("[QUEUE WATCHDOG] stale task update failed for %s: %s", task_id, exc)

    _persist_queue_snapshot_safe()
    return {
        "scanned_at": now_ts,
        "stale_worker_count": len(stale_workers),
        "affected_task_count": len(unique_task_ids),
        "affected_task_ids": unique_task_ids,
        "workers": stale_workers,
        "message": "stale workers marked" if stale_workers else "no stale workers",
    }

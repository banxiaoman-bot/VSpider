from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from .browser_env import BrowserEnv
except ImportError:
    from browser_env import BrowserEnv

try:
    from .browser_backend import browser_backend_health_from_info, build_default_browser_backend, list_browser_backends
except ImportError:
    from browser_backend import browser_backend_health_from_info, build_default_browser_backend, list_browser_backends


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int = 16) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def resolve_browser_pool_config(*, workload_contexts: int = 1) -> dict[str, Any]:
    requested = _env_int("VSPIDER_BROWSER_MAX_CONTEXTS", 1, min_value=1, max_value=16)
    experimental = _env_flag("VSPIDER_EXPERIMENTAL_PARALLEL_RUNS", False)
    need = max(1, int(workload_contexts or 1))
    if experimental:
        effective = requested
        parallel_enabled = requested > 1
        safety_note = ""
    elif need > 1:
        effective = min(requested, need)
        parallel_enabled = effective > 1
        safety_note = ""
        if effective < need:
            safety_note = "workload concurrency capped by VSPIDER_BROWSER_MAX_CONTEXTS"
    else:
        effective = 1
        parallel_enabled = False
        safety_note = (
            "parallel browser contexts require workload concurrency > 1 "
            "or VSPIDER_EXPERIMENTAL_PARALLEL_RUNS=true"
        )
    return {
        "requested_max_contexts": requested,
        "effective_max_contexts": effective,
        "parallel_enabled": parallel_enabled,
        "safety_cap_active": requested > effective,
        "safety_note": safety_note,
    }


def apply_pool_capacity_for_workload(required_contexts: int) -> int:
    """Raise the process-wide pool cap for batch / multi-URL parallel work."""

    need = max(1, int(required_contexts or 1))
    config = resolve_browser_pool_config(workload_contexts=need)
    effective = int(config["effective_max_contexts"])
    pool = get_browser_pool()
    pool.max_contexts = effective
    pool.requested_max_contexts = int(config["requested_max_contexts"])
    pool.parallel_enabled = bool(config["parallel_enabled"])
    pool.safety_note = str(config.get("safety_note") or "")
    return effective


@dataclass
class BrowserLease:
    lease_id: str
    run_id: str
    browser: BrowserEnv
    acquired_at: float
    status: str = "active"
    released_at: float | None = None
    error: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "status": self.status,
            "acquired_at": self.acquired_at,
            "released_at": self.released_at,
            "age_s": round((self.released_at or time.time()) - self.acquired_at, 3),
            "error": self.error,
        }


@dataclass
class BrowserPool:
    max_contexts: int = 1
    env_factory: Callable[[], BrowserEnv] = BrowserEnv
    requested_max_contexts: int | None = None
    parallel_enabled: bool = False
    safety_note: str = ""
    active: dict[str, BrowserLease] = field(default_factory=dict)
    total_acquired: int = 0
    total_released: int = 0
    total_failed: int = 0
    last_error: str = ""
    # POOL-LOCK: guards active + counters across worker threads. acquire() is
    # sync (called from threadpool / sync paths), so an asyncio.Lock cannot
    # protect it; a threading.Lock covers both sync acquire and the sync
    # mutation tail of async release. Never held across an await.
    _state_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def acquire(self, *, run_id: str = "") -> BrowserLease:
        with self._state_lock:
            if len(self.active) >= self.max_contexts:
                raise RuntimeError("browser pool exhausted")
            lease = BrowserLease(
                lease_id=f"lease_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}",
                run_id=str(run_id or ""),
                browser=self.env_factory(),
                acquired_at=time.time(),
            )
            self.active[lease.lease_id] = lease
            self.total_acquired += 1
            return lease

    async def release(self, lease: BrowserLease | None, *, error: str = "") -> None:
        if lease is None:
            return
        err = str(error or "")
        try:
            await lease.browser.close()
        except Exception as exc:
            err = err or f"{type(exc).__name__}: {exc}"
        with self._state_lock:
            lease.released_at = time.time()
            lease.error = err
            lease.status = "failed" if err else "released"
            self.active.pop(lease.lease_id, None)
            self.total_released += 1
            if err:
                self.total_failed += 1
                self.last_error = err

    def status(self) -> dict[str, Any]:
        requested = self.requested_max_contexts or self.max_contexts
        with self._state_lock:
            return {
                "max_contexts": self.max_contexts,
                "requested_max_contexts": requested,
                "parallel_enabled": self.parallel_enabled,
                "safety_cap_active": requested > self.max_contexts,
                "safety_note": self.safety_note,
                "active_count": len(self.active),
                "available_count": max(0, self.max_contexts - len(self.active)),
                "total_acquired": self.total_acquired,
                "total_released": self.total_released,
                "total_failed": self.total_failed,
                "last_error": self.last_error,
                "active": [lease.public() for lease in self.active.values()],
            }


_DEFAULT_CONFIG = resolve_browser_pool_config()
_DEFAULT_POOL = BrowserPool(
    max_contexts=int(_DEFAULT_CONFIG["effective_max_contexts"]),
    requested_max_contexts=int(_DEFAULT_CONFIG["requested_max_contexts"]),
    parallel_enabled=bool(_DEFAULT_CONFIG["parallel_enabled"]),
    safety_note=str(_DEFAULT_CONFIG["safety_note"] or ""),
)
_POOL_LOCK = asyncio.Lock()


def get_browser_pool() -> BrowserPool:
    return _DEFAULT_POOL


def get_browser_pool_status() -> dict[str, Any]:
    return _DEFAULT_POOL.status()


def get_browser_runtime_status(
    pool_status: dict[str, Any] | None = None,
    backend_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pool = dict(pool_status or get_browser_pool_status())
    backend = dict(backend_status or {})
    if not backend:
        active_backend = build_default_browser_backend().info().to_dict()
        backend = {
            "active": active_backend,
            "health": browser_backend_health_from_info(active_backend, check_kind="metadata"),
            "available": list_browser_backends(),
            "session_count": 0,
        }
    active_backend = dict(backend.get("active") or {})
    backend_health = dict(backend.get("health") or browser_backend_health_from_info(active_backend, check_kind="metadata"))
    backend_health_cache = dict(backend_health.get("cache") or {})
    available_backends = list(backend.get("available") or [])
    active_count = int(pool.get("active_count") or 0)
    max_contexts = int(pool.get("max_contexts") or 0)
    available_count = int(pool.get("available_count") or 0)
    remote_backends = [
        item for item in available_backends
        if isinstance(item, dict) and bool(item.get("supports_remote"))
    ]
    remote_available = any(str(item.get("status") or "") == "available" for item in remote_backends)
    backend_available = str(active_backend.get("status") or "available") == "available"
    backend_healthy = str(backend_health.get("status") or "healthy") not in {"unhealthy", "not_configured"}
    pool_available = available_count > 0
    status = "available" if backend_available and backend_healthy and pool_available else "limited"
    if not backend_available:
        status = "backend_unavailable"
    elif not backend_healthy:
        status = "backend_unhealthy"
    elif max_contexts > 0 and active_count >= max_contexts:
        status = "pool_exhausted"
    return {
        "version": "browser_runtime.v1",
        "status": status,
        "pool": pool,
        "backend": backend,
        "capacity": {
            "max_contexts": max_contexts,
            "active_contexts": active_count,
            "available_contexts": available_count,
            "backend_session_count": int(backend.get("session_count") or 0),
            "parallel_enabled": bool(pool.get("parallel_enabled")),
            "safety_cap_active": bool(pool.get("safety_cap_active")),
        },
        "backend_summary": {
            "active_name": str(active_backend.get("name") or ""),
            "active_kind": str(active_backend.get("kind") or ""),
            "active_transport": str(active_backend.get("transport") or ""),
            "supports_remote": bool(active_backend.get("supports_remote")),
            "remote_available": remote_available,
            "available_backend_count": len(available_backends),
            "health_status": str(backend_health.get("status") or ""),
            "health_reachable": backend_health.get("reachable"),
            "health_check_kind": str(backend_health.get("check_kind") or ""),
            "health_latency_ms": backend_health.get("latency_ms"),
            "health_error": str(backend_health.get("error") or ""),
            "health_cache_hit": bool(backend_health_cache.get("hit")),
            "health_cache_stale": bool(backend_health_cache.get("stale")),
            "health_cache_age_s": backend_health_cache.get("age_s"),
            "health_cache_ttl_s": backend_health_cache.get("ttl_s"),
        },
    }


def build_browser_runtime_preflight(runtime_status: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = dict(runtime_status or {})
    summary = dict(runtime.get("backend_summary") or {})
    capacity = dict(runtime.get("capacity") or {})
    runtime_state = str(runtime.get("status") or "unknown")
    health_state = str(summary.get("health_status") or "unknown")
    available_contexts = _safe_int(capacity.get("available_contexts"), default=-1)
    max_contexts = _safe_int(capacity.get("max_contexts"), default=0)
    pool_available = available_contexts > 0 if available_contexts >= 0 else None
    backend_available = runtime_state not in {"backend_unavailable"} if runtime_state != "unknown" else None
    backend_healthy = health_state not in {"unhealthy", "not_configured"} if health_state != "unknown" else None
    cache_stale = bool(summary.get("health_cache_stale"))
    warnings: list[str] = []
    if not runtime:
        warnings.append("runtime_snapshot_missing")
    if runtime_state == "backend_unavailable":
        warnings.append("active_backend_unavailable")
    if runtime_state == "backend_unhealthy" or backend_healthy is False:
        warnings.append("backend_health_unhealthy")
    if runtime_state == "pool_exhausted":
        warnings.append("browser_pool_exhausted")
    if pool_available is False:
        warnings.append("no_available_browser_contexts")
    if cache_stale:
        warnings.append("backend_health_cache_stale")
    status = "pass" if runtime_state == "available" and not warnings else "warn"
    return {
        "version": "browser_runtime_preflight.v1",
        "status": status,
        "blocking": False,
        "runtime_status": runtime_state,
        "pool_available": pool_available,
        "backend_available": backend_available,
        "backend_healthy": backend_healthy,
        "cache_stale": cache_stale,
        "warnings": warnings,
        "recommended_action": _browser_runtime_preflight_action(runtime_state, warnings),
        "summary": {
            "active_backend": str(summary.get("active_name") or ""),
            "backend_health": health_state,
            "available_contexts": available_contexts if available_contexts >= 0 else None,
            "max_contexts": max_contexts,
            "health_cache_hit": bool(summary.get("health_cache_hit")),
            "health_cache_stale": cache_stale,
        },
        "runtime_snapshot": runtime,
    }


def build_browser_runtime_drift(
    before_status: dict[str, Any] | None = None,
    after_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before = dict(before_status or {})
    after = dict(after_status or {})
    before_values = _runtime_drift_values(before)
    after_values = _runtime_drift_values(after)
    fields = ("runtime_status", "active_backend", "backend_health", "available_contexts", "active_contexts", "max_contexts", "cache_stale")
    changes = [
        {"field": field, "before": before_values.get(field), "after": after_values.get(field)}
        for field in fields
        if before_values.get(field) != after_values.get(field)
    ]
    warnings: list[str] = []
    if not before:
        warnings.append("before_runtime_snapshot_missing")
    if not after:
        warnings.append("after_runtime_snapshot_missing")
    if before_values["runtime_status"] not in {"unknown", "backend_unavailable", "backend_unhealthy", "pool_exhausted"} and after_values["runtime_status"] in {"backend_unavailable", "backend_unhealthy", "pool_exhausted"}:
        warnings.append("runtime_status_regressed")
    if before_values["backend_health"] not in {"unknown", "unhealthy", "not_configured"} and after_values["backend_health"] in {"unhealthy", "not_configured"}:
        warnings.append("backend_health_regressed")
    if before_values["available_contexts"] not in (None, 0) and after_values["available_contexts"] == 0:
        warnings.append("pool_capacity_exhausted_during_execute")
    if before_values["active_backend"] and after_values["active_backend"] and before_values["active_backend"] != after_values["active_backend"]:
        warnings.append("active_backend_changed")
    if not before_values["cache_stale"] and after_values["cache_stale"]:
        warnings.append("backend_health_cache_became_stale")
    status = "warn" if warnings else ("changed" if changes else "stable")
    return {
        "version": "browser_runtime_drift.v1",
        "status": status,
        "blocking": False,
        "warnings": warnings,
        "changes": changes,
        "recommended_action": _browser_runtime_drift_action(warnings, changes),
        "deltas": {
            "available_contexts": _runtime_delta(before_values.get("available_contexts"), after_values.get("available_contexts")),
            "active_contexts": _runtime_delta(before_values.get("active_contexts"), after_values.get("active_contexts")),
        },
        "before": before_values,
        "after": after_values,
    }


def build_browser_runtime_issue_summary(
    *,
    before_preflight: dict[str, Any] | None = None,
    after_preflight: dict[str, Any] | None = None,
    drift: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before = dict(before_preflight or {})
    after = dict(after_preflight or {})
    drift_data = dict(drift or {})
    sources = (
        ("before_preflight", before),
        ("after_preflight", after),
        ("runtime_drift", drift_data),
    )
    warnings_by_source: dict[str, list[str]] = {}
    issues: list[dict[str, str]] = []
    for source, data in sources:
        warnings = [str(item) for item in list(data.get("warnings") or []) if str(item or "")]
        warnings_by_source[source] = warnings
        issues.extend({"source": source, "code": warning, "severity": "warn"} for warning in warnings)
        if not warnings and str(data.get("status") or "") == "warn":
            issues.append({"source": source, "code": f"{source}_warn", "severity": "warn"})
    actions = _unique_runtime_actions(
        after.get("recommended_action"),
        drift_data.get("recommended_action"),
        before.get("recommended_action"),
    )
    drift_status = str(drift_data.get("status") or "unknown")
    status = "warn" if issues or drift_status == "warn" else ("watch" if drift_status == "changed" else "ok")
    return {
        "version": "browser_runtime_issue_summary.v1",
        "status": status,
        "blocking": False,
        "issue_count": len(issues),
        "issues": issues,
        "warnings_by_source": warnings_by_source,
        "recommended_action": actions[0] if actions else "continue",
        "recommended_actions": actions or ["continue"],
        "sources": {
            "before_preflight_status": str(before.get("status") or "unknown"),
            "after_preflight_status": str(after.get("status") or "unknown"),
            "drift_status": drift_status,
        },
    }


def _unique_runtime_actions(*values: Any) -> list[str]:
    actions: list[str] = []
    for value in values:
        action = str(value or "")
        if not action or action == "continue" or action in actions:
            continue
        actions.append(action)
    return actions


def _runtime_drift_values(runtime_status: dict[str, Any]) -> dict[str, Any]:
    summary = dict(runtime_status.get("backend_summary") or {})
    capacity = dict(runtime_status.get("capacity") or {})
    return {
        "runtime_status": str(runtime_status.get("status") or "unknown"),
        "active_backend": str(summary.get("active_name") or ""),
        "backend_health": str(summary.get("health_status") or "unknown"),
        "available_contexts": _safe_int_or_none(capacity.get("available_contexts")),
        "active_contexts": _safe_int_or_none(capacity.get("active_contexts")),
        "max_contexts": _safe_int_or_none(capacity.get("max_contexts")),
        "cache_stale": bool(summary.get("health_cache_stale")),
    }


def _runtime_delta(before: Any, after: Any) -> int | None:
    if isinstance(before, int) and isinstance(after, int):
        return after - before
    return None


def _browser_runtime_drift_action(warnings: list[str], changes: list[dict[str, Any]]) -> str:
    warning_set = set(warnings)
    if "before_runtime_snapshot_missing" in warning_set or "after_runtime_snapshot_missing" in warning_set:
        return "fetch_browser_runtime_status"
    if "pool_capacity_exhausted_during_execute" in warning_set:
        return "queue_or_wait_for_browser_context"
    if "backend_health_regressed" in warning_set or "runtime_status_regressed" in warning_set:
        return "check_browser_backend_health"
    if "active_backend_changed" in warning_set:
        return "reconcile_browser_backend_sessions"
    if "backend_health_cache_became_stale" in warning_set:
        return "refresh_browser_runtime_status"
    if changes:
        return "continue_with_monitoring"
    return "continue"


def _browser_runtime_preflight_action(runtime_state: str, warnings: list[str]) -> str:
    warning_set = set(warnings)
    if "runtime_snapshot_missing" in warning_set:
        return "fetch_browser_runtime_status"
    if "active_backend_unavailable" in warning_set:
        return "check_browser_backend_configuration"
    if "backend_health_unhealthy" in warning_set:
        return "check_browser_backend_health"
    if "browser_pool_exhausted" in warning_set or "no_available_browser_contexts" in warning_set:
        return "queue_or_wait_for_browser_context"
    if "backend_health_cache_stale" in warning_set:
        return "refresh_browser_runtime_status"
    if runtime_state == "limited":
        return "continue_with_monitoring"
    return "continue"


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def acquire_browser(*, run_id: str = "") -> BrowserLease:
    return _DEFAULT_POOL.acquire(run_id=run_id)


async def release_browser(lease: BrowserLease | None, *, error: str = "") -> None:
    async with _POOL_LOCK:
        await _DEFAULT_POOL.release(lease, error=error)

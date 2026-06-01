from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

from visual_web_agent.action_ref import normalize_action_ref, summarize_action_ref_sources
from visual_web_agent.browser_backend import BrowserBackend, browser_backend_health_from_info, build_default_browser_backend, list_browser_backends


_INTERACTIVE_SELECTOR = "button,a[href],input,textarea,select,[role=button],[role=link],[contenteditable=true]"
_BROWSER_ACTION_TRACE_VERSION = "browser_action_trace.v1"
_BROWSER_ACTION_ISSUE_SUMMARY_VERSION = "browser_action_issue_summary.v1"


def build_browser_action_issue_summary(action_trace: dict[str, Any] | None = None) -> dict[str, Any]:
    trace = dict(action_trace or {})
    action = str(trace.get("action") or "unknown")
    trace_status = str(trace.get("status") or "ok").lower()
    if trace_status not in {"ok", "warn", "error"}:
        trace_status = "ok"
    warnings = [
        str(item)
        for item in (trace.get("warning_codes") or [])
        if str(item or "")
    ]
    target = dict(trace.get("target") or {})
    action_ref = dict(trace.get("action_ref") or {})
    result_summary = dict(trace.get("result_summary") or {})
    selector = str(action_ref.get("selector") or target.get("selector") or "")
    ref = str(action_ref.get("ref") or target.get("ref") or "")
    issues: list[dict[str, Any]] = []
    for code in warnings:
        issues.append({"source": "warning_codes", "code": code, "action": action})
    if trace_status == "error":
        issues.append({"source": "action_trace", "code": "action_error", "action": action})
    elif trace_status == "warn" and not warnings:
        issues.append({"source": "action_trace", "code": "action_warn", "action": action})
    if ref and not action_ref:
        issues.append({"source": "action_ref", "code": "action_ref_missing", "action": action})
    if ref and not selector:
        issues.append({"source": "selector", "code": "selector_missing", "action": action})
    actions: list[str] = []
    recommended_action = str(trace.get("recommended_action") or "")
    if recommended_action and recommended_action != "continue":
        actions.append(recommended_action)
    for item in result_summary.get("recovery_actions") or []:
        action_item = str(item or "")
        if action_item and action_item != "continue":
            actions.append(action_item)
    issue_codes = {str(item.get("code") or "") for item in issues}
    if "selector_missing" in issue_codes or "action_ref_missing" in issue_codes:
        actions.append("refresh_browser_snapshot")
    if issues and not actions:
        actions.append("inspect_browser_action")
    summary_status = "error" if trace_status == "error" else ("warn" if issues or trace_status == "warn" else "ok")
    warnings_by_source: dict[str, list[str]] = {}
    for item in issues:
        source = str(item.get("source") or "unknown")
        warnings_by_source.setdefault(source, []).append(str(item.get("code") or ""))
    deduped_actions = list(dict.fromkeys(actions))
    return {
        "version": _BROWSER_ACTION_ISSUE_SUMMARY_VERSION,
        "source": "browser_control",
        "status": summary_status,
        "blocking": False,
        "action": action,
        "issue_count": len(issues),
        "issues": issues,
        "warnings_by_source": warnings_by_source,
        "recommended_action": deduped_actions[0] if deduped_actions else ("continue" if summary_status == "ok" else "inspect_browser_action"),
        "recommended_actions": deduped_actions,
        "source_status": {
            "action_trace": trace_status,
            "action_ref": "present" if action_ref else "missing",
            "selector": "present" if selector else "missing",
        },
    }


def build_browser_action_trace(
    action: str,
    *,
    session_id: str = "",
    status: str = "ok",
    started_at: float | None = None,
    ended_at: float | None = None,
    target: dict[str, Any] | None = None,
    action_ref: dict[str, Any] | None = None,
    result_summary: dict[str, Any] | None = None,
    backend: dict[str, Any] | None = None,
    warning_codes: list[str] | None = None,
    recommended_action: str = "",
) -> dict[str, Any]:
    started = float(started_at or 0.0)
    ended = float(ended_at if ended_at is not None else time.time())
    duration_ms = int(max(0.0, round((ended - started) * 1000))) if started else 0
    warnings = [str(item) for item in (warning_codes or []) if str(item or "")]
    normalized_status = str(status or "ok").lower()
    if normalized_status not in {"ok", "warn", "error"}:
        normalized_status = "ok"
    if warnings and normalized_status == "ok":
        normalized_status = "warn"
    trace = {
        "version": _BROWSER_ACTION_TRACE_VERSION,
        "source": "browser_control",
        "action": str(action or "unknown"),
        "status": normalized_status,
        "blocking": False,
        "session_id": str(session_id or ""),
        "duration_ms": duration_ms,
        "target": dict(target or {}),
        "action_ref": dict(action_ref or {}),
        "result_summary": dict(result_summary or {}),
        "backend": dict(backend or {}),
        "warning_codes": warnings,
        "recommended_action": str(recommended_action or ""),
    }
    trace["issue_summary"] = build_browser_action_issue_summary(trace)
    return trace


@dataclass
class BrowserControlSession:
    session_id: str
    playwright: Any | None = None
    browser: Any | None = None
    context: Any | None = None
    page: Any | None = None
    headed: bool = False
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    refs: dict[str, str] = field(default_factory=dict)
    console_messages: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[dict[str, Any]] = field(default_factory=list)
    network_requests: list[dict[str, Any]] = field(default_factory=list)
    backend_info: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.last_used_at = time.time()


def _browser_action_arg(args: tuple[Any, ...], kwargs: dict[str, Any], name: str, index: int, default: Any = "") -> Any:
    if name in kwargs:
        return kwargs.get(name)
    if len(args) > index:
        return args[index]
    return default


def _browser_action_session_id(manager: Any, kwargs: dict[str, Any]) -> str:
    raw = str(kwargs.get("session_id") or "default")
    try:
        return str(manager._session_id(raw))
    except Exception:
        return raw or "default"


def _browser_action_selector(session: Any, ref: str) -> str:
    if not ref or session is None:
        return ""
    try:
        return str((getattr(session, "refs", {}) or {}).get(ref) or "")
    except Exception:
        return ""


def _browser_action_failure_target(action: str, args: tuple[Any, ...], kwargs: dict[str, Any], session: Any = None) -> dict[str, Any]:
    name = str(action or "unknown")
    if name == "open":
        return {"url": str(_browser_action_arg(args, kwargs, "url", 0, "")), "headed": bool(kwargs.get("headed"))}
    if name == "snapshot":
        return {"interactive": bool(kwargs.get("interactive", True))}
    if name in {"click", "hover", "selector", "similar"}:
        ref = str(_browser_action_arg(args, kwargs, "ref", 0, ""))
        target = {"ref": ref, "selector": _browser_action_selector(session, ref)}
        if name == "similar":
            target["limit"] = int(kwargs.get("limit") or 20)
        return target
    if name in {"fill", "type"}:
        ref = str(_browser_action_arg(args, kwargs, "ref", 0, ""))
        text = str(_browser_action_arg(args, kwargs, "text", 1, ""))
        return {"ref": ref, "selector": _browser_action_selector(session, ref), "value_length": len(text)}
    if name == "press":
        key = str(_browser_action_arg(args, kwargs, "key", 0, ""))
        ref = str(kwargs.get("ref") or "")
        return {"ref": ref, "selector": _browser_action_selector(session, ref), "key": key, "target_type": "element" if ref else "page"}
    if name == "scroll":
        return {
            "direction": str(_browser_action_arg(args, kwargs, "direction", 0, "down") or "down").lower(),
            "amount": int(_browser_action_arg(args, kwargs, "amount", 1, 500) or 0),
        }
    if name == "wait":
        ref = str(kwargs.get("ref") or "")
        text = str(kwargs.get("text") or "")
        load_state = str(kwargs.get("load_state") or "")
        wait_kind = "ref" if ref else ("text" if text else ("load_state" if load_state else "timeout"))
        return {
            "kind": wait_kind,
            "ref": ref,
            "selector": _browser_action_selector(session, ref),
            "text": text,
            "load_state": load_state,
            "timeout": max(1, int(kwargs.get("ms") or 10000)),
        }
    if name == "navigate":
        return {"action": str(_browser_action_arg(args, kwargs, "action", 0, "") or "").lower()}
    if name == "get":
        ref = str(kwargs.get("ref") or "")
        selector = str(kwargs.get("selector") or "") or _browser_action_selector(session, ref)
        return {"kind": str(_browser_action_arg(args, kwargs, "kind", 0, "") or "").lower(), "ref": ref, "selector": selector, "attr": str(kwargs.get("attr") or "")}
    if name == "find":
        return {
            "strategy": str(_browser_action_arg(args, kwargs, "strategy", 0, "") or "").lower(),
            "query": str(_browser_action_arg(args, kwargs, "query", 1, "")),
            "action": str(kwargs.get("action") or "text").lower(),
            "index": int(kwargs.get("index") or 0),
            "exact": bool(kwargs.get("exact", True)),
        }
    if name == "screenshot":
        return {"path": str(kwargs.get("path") or ""), "full_page": bool(kwargs.get("full_page"))}
    return {}


def _browser_action_failure_action_ref(target: dict[str, Any], session_id: str) -> dict[str, Any]:
    ref = str(target.get("ref") or "")
    selector = str(target.get("selector") or "")
    if not ref and not selector:
        return {}
    return normalize_action_ref(
        {"ref": ref, "selector": selector},
        default_source="browser_ref" if ref else "selector",
        session_id=session_id,
    )


def _classify_browser_action_failure(exc: Exception, action: str = "") -> dict[str, str]:
    text = f"{type(exc).__name__}: {exc}".lower()
    name = str(action or "").lower()
    if "timeout" in text or type(exc).__name__.lower().endswith("timeouterror"):
        return {"code": "timeout", "category": "timing", "recommended_action": "increase_wait_or_check_runtime"}
    if "unknown ref" in text or ("selector" in text and any(token in text for token in ("missing", "not found", "resolved to 0", "strict mode violation"))):
        return {"code": "selector_missing", "category": "target_resolution", "recommended_action": "refresh_snapshot_or_use_similar_selector"}
    if "not visible" in text or "hidden" in text or "visible" in text and "not" in text:
        return {"code": "element_not_visible", "category": "element_state", "recommended_action": "scroll_into_view_or_wait_visible"}
    if "disabled" in text or "not enabled" in text:
        return {"code": "element_disabled", "category": "element_state", "recommended_action": "wait_until_enabled_or_choose_another_target"}
    if "intercept" in text or "receives pointer events" in text or "covered by" in text or "overlay" in text:
        return {"code": "click_intercepted", "category": "element_state", "recommended_action": "close_overlay_or_try_alternate_click"}
    if ("closed" in text or "has been closed" in text) and any(token in text for token in ("page", "context", "browser", "target")):
        return {"code": "context_closed", "category": "browser_context", "recommended_action": "reopen_browser_session"}
    if "connection refused" in text or "connect_over_cdp" in text or "websocket" in text or "backend" in text and "unavailable" in text:
        return {"code": "backend_unavailable", "category": "browser_backend", "recommended_action": "check_browser_backend_health"}
    if name in {"open", "navigate"} or "navigation" in text or "net::" in text or "goto" in text:
        return {"code": "navigation_failed", "category": "navigation", "recommended_action": "check_url_or_retry_navigation"}
    if name in {"fill", "type", "press"} and any(token in text for token in ("editable", "input", "fill", "type", "keyboard")):
        return {"code": "input_rejected", "category": "input", "recommended_action": "verify_input_target_or_use_alternate_entry"}
    return {"code": "unknown_action_error", "category": "unknown", "recommended_action": "inspect_browser_action"}


def _browser_action_recovery_actions(failure_code: str, action: str = "") -> list[str]:
    code = str(failure_code or "unknown_action_error").lower()
    name = str(action or "").lower()
    actions_by_code = {
        "timeout": ["increase_wait_timeout", "check_browser_runtime", "retry_action_once"],
        "selector_missing": ["refresh_browser_snapshot", "use_similar_selector", "retry_action_with_new_ref"],
        "element_not_visible": ["scroll_into_view", "wait_until_visible", "retry_action_once"],
        "element_disabled": ["wait_until_enabled", "choose_another_target"],
        "click_intercepted": ["close_overlay", "try_alternate_click", "retry_action_once"],
        "context_closed": ["reopen_browser_session", "refresh_browser_snapshot"],
        "backend_unavailable": ["check_browser_backend_health", "switch_browser_backend"],
        "navigation_failed": ["check_url", "retry_navigation", "fallback_to_extract_source"],
        "input_rejected": ["verify_input_target", "use_type_instead_of_fill"],
        "unknown_action_error": ["inspect_browser_action", "capture_screenshot"],
    }
    actions = list(actions_by_code.get(code) or actions_by_code["unknown_action_error"])
    if name == "screenshot" and "capture_screenshot" in actions:
        actions.remove("capture_screenshot")
    return list(dict.fromkeys(actions))


def _attach_browser_action_failure_trace(exc: Exception, action: str, *, session_id: str, started_at: float, target: dict[str, Any] | None = None, action_ref: dict[str, Any] | None = None, backend: dict[str, Any] | None = None) -> dict[str, Any]:
    failure = _classify_browser_action_failure(exc, action)
    failure_code = str(failure.get("code") or "unknown_action_error")
    failure_category = str(failure.get("category") or "unknown")
    recommended_action = str(failure.get("recommended_action") or "inspect_browser_action")
    recovery_actions = _browser_action_recovery_actions(failure_code, action)
    warning_codes = list(dict.fromkeys(["action_failed", failure_code]))
    trace = build_browser_action_trace(
        action,
        session_id=session_id,
        status="error",
        started_at=started_at,
        target=target,
        action_ref=action_ref,
        result_summary={"failed": True, "error_type": type(exc).__name__, "error": str(exc), "failure_code": failure_code, "failure_category": failure_category, "recovery_actions": recovery_actions},
        backend=backend,
        warning_codes=warning_codes,
        recommended_action=recommended_action,
    )
    setattr(exc, "action_trace", trace)
    setattr(exc, "action_issue_summary", trace.get("issue_summary") or {})
    return trace


def _browser_action_failure_traced(action: str):
    def decorate(func):
        @wraps(func)
        async def wrapped(self, *args, **kwargs):
            started_at = time.time()
            try:
                return await func(self, *args, **kwargs)
            except Exception as exc:
                if not isinstance(getattr(exc, "action_trace", None), dict):
                    sid = _browser_action_session_id(self, kwargs)
                    session = getattr(self, "sessions", {}).get(sid)
                    target = _browser_action_failure_target(action, args, kwargs, session)
                    action_ref = _browser_action_failure_action_ref(target, sid)
                    backend = dict(getattr(session, "backend_info", {}) or {}) if session is not None else {}
                    _attach_browser_action_failure_trace(exc, action, session_id=sid, started_at=started_at, target=target, action_ref=action_ref, backend=backend)
                raise
        return wrapped
    return decorate


class BrowserControlManager:
    def __init__(self, backend: BrowserBackend | None = None) -> None:
        self.sessions: dict[str, BrowserControlSession] = {}
        self.backend = backend or build_default_browser_backend()
        self._backend_health_cache: dict[str, Any] = {}
        self._backend_health_cache_key = ""
        self._backend_health_cache_at = 0.0

    def backend_status(self) -> dict[str, Any]:
        info = self.backend.info().to_dict()
        health = self._backend_health(info)
        return {
            "active": info,
            "health": health,
            "available": list_browser_backends(),
            "session_count": len(self.sessions),
        }

    def _backend_health(self, info: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        ttl_s = _backend_health_cache_ttl_s()
        cache_key = _backend_health_cache_key(info)
        cached = self._backend_health_cache if self._backend_health_cache_key == cache_key else {}
        cache_age_s = now - self._backend_health_cache_at if cached else 0.0
        if cached and ttl_s > 0 and cache_age_s <= ttl_s:
            health = dict(cached)
            health["cache"] = _backend_health_cache_meta(
                hit=True,
                stale=False,
                age_s=cache_age_s,
                ttl_s=ttl_s,
                cached_at=self._backend_health_cache_at,
            )
            return health
        health = {}
        health_func = getattr(self.backend, "health", None)
        if callable(health_func):
            try:
                health = dict(health_func() or {})
            except Exception as exc:
                if cached:
                    health = dict(cached)
                    health["cache"] = _backend_health_cache_meta(
                        hit=True,
                        stale=True,
                        age_s=cache_age_s,
                        ttl_s=ttl_s,
                        cached_at=self._backend_health_cache_at,
                        refresh_error=f"{type(exc).__name__}: {exc}",
                    )
                    return health
                health = browser_backend_health_from_info(info, reachable=False, check_kind="metadata")
                health["status"] = "unhealthy"
                health["error"] = f"{type(exc).__name__}: {exc}"
        if not health:
            health = browser_backend_health_from_info(info, check_kind="metadata")
        self._backend_health_cache = dict(health)
        self._backend_health_cache_key = cache_key
        self._backend_health_cache_at = now
        health = dict(health)
        health["cache"] = _backend_health_cache_meta(
            hit=False,
            stale=False,
            age_s=0.0,
            ttl_s=ttl_s,
            cached_at=now,
        )
        return health

    def get(self, session_id: str = "default") -> BrowserControlSession | None:
        return self.sessions.get(self._session_id(session_id))

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self._public_session(item) for item in sorted(self.sessions.values(), key=lambda s: s.session_id)]

    @_browser_action_failure_traced("open")
    async def open(self, url: str, *, session_id: str = "default", headed: bool = False) -> dict[str, Any]:
        started_at = time.time()
        sid = self._session_id(session_id)
        session = self.sessions.get(sid)
        if session is None:
            session = await self._create_session(sid, headed=headed)
            self.sessions[sid] = session
        elif session.page is None or self._is_closed(session.page):
            await self.close(sid)
            session = await self._create_session(sid, headed=headed)
            self.sessions[sid] = session
        response = await session.page.goto(str(url), wait_until="domcontentloaded", timeout=30000)
        try:
            await session.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        session.touch()
        status_code = getattr(response, "status", None) if response is not None else None
        return {
            "session_id": sid,
            "url": session.page.url,
            "status_code": status_code,
            "action_trace": build_browser_action_trace(
                "open",
                session_id=sid,
                started_at=started_at,
                target={"url": str(url or ""), "headed": bool(headed)},
                result_summary={"url": session.page.url, "status_code": status_code},
                backend=session.backend_info,
            ),
        }

    @_browser_action_failure_traced("snapshot")
    async def snapshot(self, *, session_id: str = "default", interactive: bool = True) -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = _INTERACTIVE_SELECTOR if interactive else "body *"
        elements = await page.evaluate(
            r"""(selector) => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const label = (el) => (
                    el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') ||
                    el.innerText || el.value || el.textContent || el.tagName || ''
                ).replace(/\s+/g, ' ').trim();
                const cssPath = (el) => {
                    if (el.id) return '#' + CSS.escape(el.id);
                    const parts = [];
                    let node = el;
                    while (node && node.nodeType === 1 && node !== document.body && parts.length < 6) {
                        let part = node.tagName.toLowerCase();
                        if (node.classList && node.classList.length) part += '.' + Array.from(node.classList).slice(0, 2).map(CSS.escape).join('.');
                        const parent = node.parentElement;
                        if (parent) {
                            const same = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
                            if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
                        }
                        parts.unshift(part);
                        node = parent;
                    }
                    return parts.join(' > ');
                };
                return Array.from(document.querySelectorAll(selector)).filter(visible).slice(0, 200).map((el) => ({
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || '',
                    name: label(el).slice(0, 160),
                    selector: cssPath(el),
                    type: el.getAttribute('type') || '',
                    href: el.getAttribute('href') || '',
                }));
            }""",
            selector,
        )
        session.refs = {}
        public_items: list[dict[str, Any]] = []
        action_refs: list[dict[str, Any]] = []
        for idx, item in enumerate(elements or [], start=1):
            ref = f"@e{idx}"
            selector_value = str(item.get("selector") or "")
            if not selector_value:
                continue
            session.refs[ref] = selector_value
            public = dict(item)
            public.pop("selector", None)
            public["ref"] = ref
            public_items.append(public)
            action_refs.append(normalize_action_ref(
                {
                    "ref": ref,
                    "selector": selector_value,
                    "role": public.get("role") or "",
                    "name": public.get("name") or "",
                    "tag": public.get("tag") or "",
                    "type": public.get("type") or "",
                    "href": public.get("href") or "",
                },
                default_source="browser_ref",
                session_id=session.session_id,
            ))
        session.touch()
        return {
            "session_id": session.session_id,
            "url": page.url,
            "count": len(public_items),
            "items": public_items,
            "action_refs": action_refs,
            "action_ref_summary": summarize_action_ref_sources(action_refs),
            "action_trace": build_browser_action_trace(
                "snapshot",
                session_id=session.session_id,
                started_at=started_at,
                target={"interactive": bool(interactive)},
                result_summary={
                    "url": page.url,
                    "count": len(public_items),
                    "action_ref_summary": summarize_action_ref_sources(action_refs),
                },
                backend=session.backend_info,
            ),
        }

    @_browser_action_failure_traced("click")
    async def click(self, ref: str, *, session_id: str = "default") -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = self._resolve_ref(session, ref)
        await page.locator(selector).first.click(timeout=10000)
        session.touch()
        action_ref = normalize_action_ref({"ref": str(ref or ""), "selector": selector}, default_source="browser_ref", session_id=session.session_id)
        return {
            "session_id": session.session_id,
            "ref": ref,
            "url": page.url,
            "action_trace": build_browser_action_trace(
                "click",
                session_id=session.session_id,
                started_at=started_at,
                target={"ref": str(ref or ""), "selector": selector},
                action_ref=action_ref,
                result_summary={"url": page.url},
                backend=session.backend_info,
            ),
        }

    async def dblclick(self, ref: str, *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = self._resolve_ref(session, ref)
        await page.locator(selector).first.dblclick(timeout=10000)
        session.touch()
        return {"session_id": session.session_id, "ref": ref, "url": page.url}

    async def focus(self, ref: str, *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = self._resolve_ref(session, ref)
        await page.locator(selector).first.focus(timeout=10000)
        session.touch()
        return {"session_id": session.session_id, "ref": ref}

    @_browser_action_failure_traced("hover")
    async def hover(self, ref: str, *, session_id: str = "default") -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = self._resolve_ref(session, ref)
        await page.locator(selector).first.hover(timeout=10000)
        session.touch()
        action_ref = normalize_action_ref({"ref": str(ref or ""), "selector": selector}, default_source="browser_ref", session_id=session.session_id)
        return {
            "session_id": session.session_id,
            "ref": ref,
            "action_trace": build_browser_action_trace(
                "hover",
                session_id=session.session_id,
                started_at=started_at,
                target={"ref": str(ref or ""), "selector": selector},
                action_ref=action_ref,
                result_summary={"url": page.url},
                backend=session.backend_info,
            ),
        }

    @_browser_action_failure_traced("fill")
    async def fill(self, ref: str, text: str, *, session_id: str = "default") -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = self._resolve_ref(session, ref)
        await page.locator(selector).first.fill(str(text), timeout=10000)
        session.touch()
        value = str(text)
        action_ref = normalize_action_ref({"ref": str(ref or ""), "selector": selector}, default_source="browser_ref", session_id=session.session_id)
        return {
            "session_id": session.session_id,
            "ref": ref,
            "value": value,
            "action_trace": build_browser_action_trace(
                "fill",
                session_id=session.session_id,
                started_at=started_at,
                target={"ref": str(ref or ""), "selector": selector},
                action_ref=action_ref,
                result_summary={"value_length": len(value)},
                backend=session.backend_info,
            ),
        }

    async def check(self, ref: str, *, session_id: str = "default", checked: bool = True) -> dict[str, Any]:
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = self._resolve_ref(session, ref)
        locator = page.locator(selector).first
        if checked:
            await locator.check(timeout=10000)
        else:
            await locator.uncheck(timeout=10000)
        session.touch()
        return {"session_id": session.session_id, "ref": ref, "checked": bool(checked)}

    async def select(self, ref: str, value: str, *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = self._resolve_ref(session, ref)
        selected = await page.locator(selector).first.select_option(str(value), timeout=10000)
        session.touch()
        return {"session_id": session.session_id, "ref": ref, "value": str(value), "selected": selected}

    async def scroll_into_view(self, ref: str, *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = self._resolve_ref(session, ref)
        await page.locator(selector).first.scroll_into_view_if_needed(timeout=10000)
        session.touch()
        return {"session_id": session.session_id, "ref": ref}

    async def upload(self, ref: str, paths: str | list[str], *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = self._resolve_ref(session, ref)
        raw_paths = paths if isinstance(paths, list) else [paths]
        file_paths = [str(Path(str(item))) for item in raw_paths if str(item or "").strip()]
        if not file_paths:
            raise ValueError("upload paths are required")
        await page.locator(selector).first.set_input_files(file_paths if len(file_paths) > 1 else file_paths[0], timeout=10000)
        session.touch()
        return {"session_id": session.session_id, "ref": ref, "paths": file_paths}

    @_browser_action_failure_traced("type")
    async def type(self, ref: str, text: str, *, session_id: str = "default") -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = self._resolve_ref(session, ref)
        await page.locator(selector).first.type(str(text), timeout=10000)
        session.touch()
        value = str(text)
        action_ref = normalize_action_ref({"ref": str(ref or ""), "selector": selector}, default_source="browser_ref", session_id=session.session_id)
        return {
            "session_id": session.session_id,
            "ref": ref,
            "value": value,
            "action_trace": build_browser_action_trace(
                "type",
                session_id=session.session_id,
                started_at=started_at,
                target={"ref": str(ref or ""), "selector": selector},
                action_ref=action_ref,
                result_summary={"value_length": len(value)},
                backend=session.backend_info,
            ),
        }

    @_browser_action_failure_traced("press")
    async def press(self, key: str, *, session_id: str = "default", ref: str = "") -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = ""
        action_ref: dict[str, Any] = {}
        if ref:
            selector = self._resolve_ref(session, ref)
            await page.locator(selector).first.press(str(key), timeout=10000)
            action_ref = normalize_action_ref({"ref": str(ref or ""), "selector": selector}, default_source="browser_ref", session_id=session.session_id)
        else:
            await page.keyboard.press(str(key))
        session.touch()
        return {
            "session_id": session.session_id,
            "ref": ref,
            "key": str(key),
            "action_trace": build_browser_action_trace(
                "press",
                session_id=session.session_id,
                started_at=started_at,
                target={"ref": str(ref or ""), "selector": selector, "key": str(key), "target_type": "element" if ref else "page"},
                action_ref=action_ref,
                result_summary={"key": str(key)},
                backend=session.backend_info,
            ),
        }

    @_browser_action_failure_traced("scroll")
    async def scroll(self, direction: str = "down", amount: int = 500, *, session_id: str = "default") -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        delta = abs(int(amount or 0))
        direction_value = str(direction or "").lower()
        if direction_value in {"up", "left"}:
            delta = -delta
        if direction_value in {"left", "right"}:
            await page.mouse.wheel(delta, 0)
        else:
            await page.mouse.wheel(0, delta)
        session.touch()
        wheel_delta = [delta, 0] if direction_value in {"left", "right"} else [0, delta]
        return {
            "session_id": session.session_id,
            "direction": direction_value or "down",
            "amount": amount,
            "action_trace": build_browser_action_trace(
                "scroll",
                session_id=session.session_id,
                started_at=started_at,
                target={"direction": direction_value or "down", "amount": int(amount or 0)},
                result_summary={"wheel_delta": wheel_delta},
                backend=session.backend_info,
            ),
        }

    @_browser_action_failure_traced("wait")
    async def wait(
        self,
        *,
        session_id: str = "default",
        ms: int = 0,
        ref: str = "",
        text: str = "",
        load_state: str = "",
    ) -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        timeout = max(1, int(ms or 10000))
        selector = ""
        wait_kind = "timeout"
        action_ref: dict[str, Any] = {}
        if ref:
            selector = self._resolve_ref(session, ref)
            await page.locator(selector).first.wait_for(timeout=timeout)
            wait_kind = "ref"
            action_ref = normalize_action_ref({"ref": str(ref or ""), "selector": selector}, default_source="browser_ref", session_id=session.session_id)
        elif text:
            await page.get_by_text(str(text)).first.wait_for(timeout=timeout)
            wait_kind = "text"
        elif load_state:
            await page.wait_for_load_state(str(load_state), timeout=timeout)
            wait_kind = "load_state"
        else:
            await page.wait_for_timeout(max(0, int(ms or 0)))
        session.touch()
        return {
            "session_id": session.session_id,
            "waited": True,
            "action_trace": build_browser_action_trace(
                "wait",
                session_id=session.session_id,
                started_at=started_at,
                target={"kind": wait_kind, "ref": str(ref or ""), "selector": selector, "text": str(text or ""), "load_state": str(load_state or ""), "timeout": timeout},
                action_ref=action_ref,
                result_summary={"waited": True},
                backend=session.backend_info,
            ),
        }

    async def evaluate(self, script: str, *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        page = self._require_page(session)
        value = await page.evaluate(str(script or ""))
        session.touch()
        return {"session_id": session.session_id, "value": value}

    @_browser_action_failure_traced("navigate")
    async def navigate(self, action: str, *, session_id: str = "default") -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        op = str(action or "").lower()
        if op == "back":
            response = await page.go_back(wait_until="domcontentloaded")
        elif op == "forward":
            response = await page.go_forward(wait_until="domcontentloaded")
        elif op == "reload":
            response = await page.reload(wait_until="domcontentloaded")
        else:
            raise ValueError("unsupported navigation action")
        session.touch()
        status_code = getattr(response, "status", None) if response is not None else None
        return {
            "session_id": session.session_id,
            "action": op,
            "url": page.url,
            "status_code": status_code,
            "action_trace": build_browser_action_trace(
                "navigate",
                session_id=session.session_id,
                started_at=started_at,
                target={"action": op},
                result_summary={"url": page.url, "status_code": status_code},
                backend=session.backend_info,
            ),
        }

    @_browser_action_failure_traced("get")
    async def get(self, kind: str, *, ref: str = "", attr: str = "", selector: str = "", session_id: str = "default") -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        k = str(kind or "").lower()
        query = ""
        selector_value = ""
        action_ref: dict[str, Any] = {}
        if k == "url":
            value: Any = page.url
        elif k == "title":
            value = await page.title()
        elif k == "count":
            query = selector or (self._resolve_ref(session, ref) if ref.startswith("@") else ref)
            value = await page.locator(str(query or "body")).count()
            if ref:
                action_ref = normalize_action_ref({"ref": str(ref or ""), "selector": str(query or "")}, default_source="browser_ref", session_id=session.session_id)
            elif query:
                action_ref = normalize_action_ref({"selector": str(query or "")}, default_source="selector", session_id=session.session_id)
        elif k in {"text", "html", "value", "attr"}:
            selector_value = self._resolve_ref(session, ref)
            action_ref = normalize_action_ref({"ref": str(ref or ""), "selector": selector_value}, default_source="browser_ref", session_id=session.session_id)
            locator = page.locator(selector_value).first
            if k == "text":
                value = await locator.inner_text(timeout=10000)
            elif k == "html":
                value = await locator.inner_html(timeout=10000)
            elif k == "value":
                value = await locator.input_value(timeout=10000)
            else:
                value = await locator.get_attribute(str(attr or ""), timeout=10000)
        elif k == "box":
            selector_value = self._resolve_ref(session, ref)
            action_ref = normalize_action_ref({"ref": str(ref or ""), "selector": selector_value}, default_source="browser_ref", session_id=session.session_id)
            value = await page.locator(selector_value).first.bounding_box(timeout=10000)
        else:
            raise ValueError("unsupported get kind")
        session.touch()
        return {
            "session_id": session.session_id,
            "kind": k,
            "value": value,
            "action_trace": build_browser_action_trace(
                "get",
                session_id=session.session_id,
                started_at=started_at,
                target={"kind": k, "ref": str(ref or ""), "selector": selector_value or str(query or ""), "attr": str(attr or "")},
                action_ref=action_ref,
                result_summary={"value_type": type(value).__name__, "has_value": value is not None},
                backend=session.backend_info,
            ),
        }

    async def is_state(self, kind: str, ref: str, *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector = self._resolve_ref(session, ref)
        locator = page.locator(selector).first
        k = str(kind or "").lower()
        if k == "visible":
            value = await locator.is_visible(timeout=10000)
        elif k == "enabled":
            value = await locator.is_enabled(timeout=10000)
        elif k == "checked":
            value = await locator.is_checked(timeout=10000)
        else:
            raise ValueError("unsupported state kind")
        session.touch()
        return {"session_id": session.session_id, "kind": k, "ref": ref, "value": bool(value)}

    @_browser_action_failure_traced("find")
    async def find(
        self,
        strategy: str,
        query: str,
        *,
        session_id: str = "default",
        action: str = "text",
        name: str = "",
        value: str = "",
        attr: str = "",
        index: int = 0,
        exact: bool = True,
    ) -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        locator = self._find_locator(
            page,
            str(strategy or ""),
            str(query or ""),
            name=str(name or ""),
            index=int(index or 0),
            exact=bool(exact),
        )
        result = await self._run_locator_action(
            locator,
            str(action or "text"),
            value=str(value or ""),
            attr=str(attr or ""),
        )
        session.touch()
        return {
            "session_id": session.session_id,
            "strategy": str(strategy or "").lower(),
            "query": str(query or ""),
            "action": str(action or "text").lower(),
            "value": result,
            "action_trace": build_browser_action_trace(
                "find",
                session_id=session.session_id,
                started_at=started_at,
                target={
                    "strategy": str(strategy or "").lower(),
                    "query": str(query or ""),
                    "action": str(action or "text").lower(),
                    "index": int(index or 0),
                    "exact": bool(exact),
                },
                result_summary={"value_type": type(result).__name__, "has_value": result is not None},
                backend=session.backend_info,
            ),
        }

    @_browser_action_failure_traced("selector")
    async def selector(self, ref: str, *, session_id: str = "default") -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector_value = self._resolve_ref(session, ref)
        data = await page.evaluate(
            r"""(selector) => {
                const esc = (v) => window.CSS && CSS.escape ? CSS.escape(String(v)) : String(v).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
                const label = (el) => (
                    el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') ||
                    el.innerText || el.value || el.textContent || el.tagName || ''
                ).replace(/\s+/g, ' ').trim();
                const roleOf = (el) => {
                    const explicit = el.getAttribute('role') || '';
                    if (explicit) return explicit;
                    const tag = el.tagName.toLowerCase();
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    if (tag === 'button') return 'button';
                    if (tag === 'a' && el.getAttribute('href')) return 'link';
                    if (tag === 'select') return 'combobox';
                    if (tag === 'textarea') return 'textbox';
                    if (tag === 'input' && ['checkbox', 'radio'].includes(type)) return type;
                    if (tag === 'input') return 'textbox';
                    return '';
                };
                const cssPath = (el) => {
                    if (el.id) return '#' + esc(el.id);
                    const parts = [];
                    let node = el;
                    while (node && node.nodeType === 1 && node !== document.body && parts.length < 8) {
                        let part = node.tagName.toLowerCase();
                        if (node.classList && node.classList.length) part += '.' + Array.from(node.classList).slice(0, 2).map(esc).join('.');
                        const parent = node.parentElement;
                        if (parent) {
                            const same = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
                            if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
                        }
                        parts.unshift(part);
                        node = parent;
                    }
                    return parts.join(' > ');
                };
                const xpath = (el) => {
                    const parts = [];
                    let node = el;
                    while (node && node.nodeType === 1) {
                        const tag = node.tagName.toLowerCase();
                        if (node.id) {
                            parts.unshift(`*[@id="${node.id.replace(/"/g, '\\"')}"]`);
                            break;
                        }
                        const parent = node.parentElement;
                        const same = parent ? Array.from(parent.children).filter((child) => child.tagName === node.tagName) : [];
                        const idx = same.length > 1 ? `[${same.indexOf(node) + 1}]` : '';
                        parts.unshift(tag + idx);
                        node = parent;
                    }
                    return '/' + parts.join('/');
                };
                const el = document.querySelector(selector);
                if (!el) return null;
                const attrs = {};
                for (const name of ['id', 'class', 'name', 'type', 'href', 'role', 'aria-label', 'title', 'placeholder', 'data-testid']) {
                    const value = el.getAttribute(name);
                    if (value !== null && value !== '') attrs[name] = value;
                }
                const role = roleOf(el);
                const name = label(el).slice(0, 160);
                return {
                    css: cssPath(el),
                    xpath: xpath(el),
                    role: role,
                    role_selector: role ? `role=${role}${name ? '[name="' + name.replace(/"/g, '\\"') + '"]' : ''}` : '',
                    text: name,
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute('type') || '',
                    href: el.getAttribute('href') || '',
                    attributes: attrs,
                };
            }""",
            selector_value,
        )
        if not isinstance(data, dict):
            data = {}
        selectors = {
            "css": str(data.get("css") or selector_value),
            "xpath": str(data.get("xpath") or ""),
            "role": str(data.get("role_selector") or ""),
            "text": str(data.get("text") or ""),
        }
        element = {
            "tag": str(data.get("tag") or ""),
            "role": str(data.get("role") or ""),
            "name": str(data.get("text") or ""),
            "type": str(data.get("type") or ""),
            "href": str(data.get("href") or ""),
            "attributes": dict(data.get("attributes") or {}),
        }
        action_ref = normalize_action_ref(
            {
                "ref": str(ref or ""),
                "selector": selectors["css"],
                "selectors": selectors,
                "element": element,
            },
            default_source="browser_ref",
            session_id=session.session_id,
        )
        session.touch()
        return {
            "session_id": session.session_id,
            "ref": str(ref or ""),
            "selectors": selectors,
            "element": element,
            "action_ref": action_ref,
            "action_trace": build_browser_action_trace(
                "selector",
                session_id=session.session_id,
                started_at=started_at,
                target={"ref": str(ref or ""), "selector": selector_value},
                action_ref=action_ref,
                result_summary={
                    "selector_kinds": [key for key, value in selectors.items() if value],
                    "element_tag": element["tag"],
                    "element_role": element["role"],
                },
                backend=session.backend_info,
            ),
        }

    @_browser_action_failure_traced("similar")
    async def similar(self, ref: str, *, session_id: str = "default", limit: int = 20) -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        selector_value = self._resolve_ref(session, ref)
        items = await page.evaluate(
            r"""([selector, limit]) => {
                const esc = (v) => window.CSS && CSS.escape ? CSS.escape(String(v)) : String(v).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const label = (el) => (
                    el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') ||
                    el.innerText || el.value || el.textContent || el.tagName || ''
                ).replace(/\s+/g, ' ').trim();
                const roleOf = (el) => {
                    const explicit = el.getAttribute('role') || '';
                    if (explicit) return explicit;
                    const tag = el.tagName.toLowerCase();
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    if (tag === 'button') return 'button';
                    if (tag === 'a' && el.getAttribute('href')) return 'link';
                    if (tag === 'select') return 'combobox';
                    if (tag === 'textarea') return 'textbox';
                    if (tag === 'input' && ['checkbox', 'radio'].includes(type)) return type;
                    if (tag === 'input') return 'textbox';
                    return '';
                };
                const cssPath = (el) => {
                    if (el.id) return '#' + esc(el.id);
                    const parts = [];
                    let node = el;
                    while (node && node.nodeType === 1 && node !== document.body && parts.length < 8) {
                        let part = node.tagName.toLowerCase();
                        if (node.classList && node.classList.length) part += '.' + Array.from(node.classList).slice(0, 2).map(esc).join('.');
                        const parent = node.parentElement;
                        if (parent) {
                            const same = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
                            if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
                        }
                        parts.unshift(part);
                        node = parent;
                    }
                    return parts.join(' > ');
                };
                const classes = (el) => new Set(Array.from(el.classList || []));
                const overlap = (a, b) => {
                    if (!a.size && !b.size) return 0;
                    let same = 0;
                    for (const item of a) if (b.has(item)) same += 1;
                    return same / Math.max(a.size, b.size, 1);
                };
                const target = document.querySelector(selector);
                if (!target) return [];
                const t = {
                    tag: target.tagName.toLowerCase(),
                    role: roleOf(target),
                    type: target.getAttribute('type') || '',
                    cls: classes(target),
                    parent: target.parentElement ? target.parentElement.tagName.toLowerCase() : '',
                };
                return Array.from(document.querySelectorAll('body *'))
                    .filter((el) => el !== target && visible(el))
                    .map((el) => {
                        const score =
                            (el.tagName.toLowerCase() === t.tag ? 0.35 : 0) +
                            (roleOf(el) && roleOf(el) === t.role ? 0.2 : 0) +
                            ((el.getAttribute('type') || '') === t.type ? 0.15 : 0) +
                            (overlap(classes(el), t.cls) * 0.2) +
                            ((el.parentElement ? el.parentElement.tagName.toLowerCase() : '') === t.parent ? 0.1 : 0);
                        return {
                            score: Number(score.toFixed(3)),
                            tag: el.tagName.toLowerCase(),
                            role: roleOf(el),
                            name: label(el).slice(0, 160),
                            selector: cssPath(el),
                            type: el.getAttribute('type') || '',
                            href: el.getAttribute('href') || '',
                        };
                    })
                    .filter((item) => item.selector && item.score >= 0.35)
                    .sort((a, b) => b.score - a.score)
                    .slice(0, Math.max(1, Math.min(Number(limit) || 20, 100)));
            }""",
            [selector_value, int(limit or 20)],
        )
        public_items: list[dict[str, Any]] = []
        action_refs: list[dict[str, Any]] = []
        for item in items or []:
            selector_item = str(item.get("selector") or "") if isinstance(item, dict) else ""
            if not selector_item:
                continue
            new_ref = f"@e{len(session.refs) + 1}"
            session.refs[new_ref] = selector_item
            public = dict(item)
            public.pop("selector", None)
            public["ref"] = new_ref
            public_items.append(public)
            action_refs.append(normalize_action_ref(
                {
                    "ref": new_ref,
                    "selector": selector_item,
                    "role": public.get("role") or "",
                    "name": public.get("name") or "",
                    "tag": public.get("tag") or "",
                    "type": public.get("type") or "",
                    "href": public.get("href") or "",
                    "confidence": public.get("score", 1.0),
                },
                default_source="browser_ref",
                session_id=session.session_id,
            ))
        session.touch()
        action_ref = normalize_action_ref({"ref": str(ref or ""), "selector": selector_value}, default_source="browser_ref", session_id=session.session_id)
        return {
            "session_id": session.session_id,
            "ref": str(ref or ""),
            "count": len(public_items),
            "items": public_items,
            "action_refs": action_refs,
            "action_ref_summary": summarize_action_ref_sources(action_refs),
            "action_trace": build_browser_action_trace(
                "similar",
                session_id=session.session_id,
                started_at=started_at,
                target={"ref": str(ref or ""), "selector": selector_value, "limit": int(limit or 20)},
                action_ref=action_ref,
                result_summary={
                    "count": len(public_items),
                    "action_ref_summary": summarize_action_ref_sources(action_refs),
                },
                backend=session.backend_info,
            ),
        }

    @_browser_action_failure_traced("screenshot")
    async def screenshot(self, *, session_id: str = "default", path: str = "", full_page: bool = False) -> dict[str, Any]:
        started_at = time.time()
        session = self._require_session(session_id)
        page = self._require_page(session)
        options: dict[str, Any] = {"full_page": bool(full_page)}
        if path:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(target), **options)
            value = str(target)
        else:
            raw = await page.screenshot(**options)
            value = base64.b64encode(raw).decode("ascii")
        session.touch()
        return {
            "session_id": session.session_id,
            "path": str(path or ""),
            "data": value if not path else "",
            "action_trace": build_browser_action_trace(
                "screenshot",
                session_id=session.session_id,
                started_at=started_at,
                target={"path": str(path or ""), "full_page": bool(full_page)},
                result_summary={"inline_data": not bool(path), "data_length": len(value) if not path else 0},
                backend=session.backend_info,
            ),
        }

    async def pdf(self, *, session_id: str = "default", path: str = "") -> dict[str, Any]:
        session = self._require_session(session_id)
        page = self._require_page(session)
        if not path:
            raise ValueError("pdf path is required")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await page.pdf(path=str(target))
        session.touch()
        return {"session_id": session.session_id, "path": str(target)}

    async def tabs(self, *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        pages = self._open_pages(session)
        items = [
            {"index": idx, "url": str(getattr(page, "url", "") or ""), "active": page is session.page}
            for idx, page in enumerate(pages)
        ]
        session.touch()
        return {"session_id": session.session_id, "count": len(items), "tabs": items}

    async def tab_new(self, url: str = "", *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        if session.context is None:
            raise RuntimeError("browser control context is closed")
        page = await session.context.new_page()
        self._bind_page_observers(session, page)
        session.page = page
        if url:
            await page.goto(str(url), wait_until="domcontentloaded", timeout=30000)
        pages = self._open_pages(session)
        session.touch()
        return {"session_id": session.session_id, "index": pages.index(page), "url": str(getattr(page, "url", "") or "")}

    async def tab_switch(self, index: int, *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        pages = self._open_pages(session)
        idx = int(index)
        if idx < 0 or idx >= len(pages):
            raise IndexError("tab index out of range")
        session.page = pages[idx]
        session.touch()
        return {"session_id": session.session_id, "index": idx, "url": str(getattr(session.page, "url", "") or "")}

    async def tab_close(self, index: int | None = None, *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        pages = self._open_pages(session)
        if not pages:
            return {"session_id": session.session_id, "closed": False, "remaining": 0}
        page = session.page if index is None else pages[int(index)]
        if page is None:
            return {"session_id": session.session_id, "closed": False, "remaining": len(pages)}
        await page.close()
        remaining = self._open_pages(session)
        session.page = remaining[-1] if remaining else None
        session.touch()
        return {"session_id": session.session_id, "closed": True, "remaining": len(remaining)}

    async def cookies(self, *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        if session.context is None:
            raise RuntimeError("browser control context is closed")
        items = await session.context.cookies()
        session.touch()
        return {"session_id": session.session_id, "cookies": items}

    async def cookies_set(self, name: str, value: str, *, session_id: str = "default", url: str = "") -> dict[str, Any]:
        session = self._require_session(session_id)
        if session.context is None:
            raise RuntimeError("browser control context is closed")
        page = self._require_page(session)
        cookie = {"name": str(name or ""), "value": str(value or ""), "url": str(url or page.url or "about:blank")}
        if not cookie["name"]:
            raise ValueError("cookie name is required")
        await session.context.add_cookies([cookie])
        session.touch()
        return {"session_id": session.session_id, "cookie": cookie}

    async def cookies_clear(self, *, session_id: str = "default") -> dict[str, Any]:
        session = self._require_session(session_id)
        if session.context is None:
            raise RuntimeError("browser control context is closed")
        await session.context.clear_cookies()
        session.touch()
        return {"session_id": session.session_id, "cleared": True}

    async def storage_local(
        self,
        *,
        session_id: str = "default",
        key: str = "",
        value: str | None = None,
        clear: bool = False,
    ) -> dict[str, Any]:
        session = self._require_session(session_id)
        page = self._require_page(session)
        if clear:
            await page.evaluate("() => localStorage.clear()")
            result: Any = {}
        elif value is not None:
            await page.evaluate("([key, value]) => localStorage.setItem(key, value)", [str(key or ""), str(value)])
            result = str(value)
        elif key:
            result = await page.evaluate("(key) => localStorage.getItem(key)", str(key))
        else:
            result = await page.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
        session.touch()
        return {"session_id": session.session_id, "key": str(key or ""), "value": result}

    def console(self, *, session_id: str = "default", clear: bool = False) -> dict[str, Any]:
        session = self._require_session(session_id)
        items = list(session.console_messages)
        if clear:
            session.console_messages.clear()
        session.touch()
        return {"session_id": session.session_id, "count": len(items), "items": items, "cleared": bool(clear)}

    def errors(self, *, session_id: str = "default", clear: bool = False) -> dict[str, Any]:
        session = self._require_session(session_id)
        items = list(session.page_errors)
        if clear:
            session.page_errors.clear()
        session.touch()
        return {"session_id": session.session_id, "count": len(items), "items": items, "cleared": bool(clear)}

    def network_requests(self, *, session_id: str = "default", filter_text: str = "", clear: bool = False) -> dict[str, Any]:
        session = self._require_session(session_id)
        needle = str(filter_text or "").lower()
        items = [
            item for item in session.network_requests
            if not needle or needle in str(item.get("url") or "").lower()
        ]
        if clear:
            session.network_requests.clear()
        session.touch()
        return {"session_id": session.session_id, "count": len(items), "items": items, "cleared": bool(clear)}

    async def close(self, session_id: str = "default") -> dict[str, Any]:
        sid = self._session_id(session_id)
        session = self.sessions.pop(sid, None)
        if session is None:
            return {"session_id": sid, "closed": False}
        for obj in (session.context, session.browser, session.playwright):
            if obj is None:
                continue
            try:
                if obj is session.playwright:
                    await obj.stop()
                else:
                    await obj.close()
            except Exception:
                pass
        return {"session_id": sid, "closed": True}

    async def _create_session(self, session_id: str, *, headed: bool = False) -> BrowserControlSession:
        backend_session = await self.backend.create_session(headed=headed)
        session = BrowserControlSession(
            session_id=session_id,
            playwright=backend_session.playwright,
            browser=backend_session.browser,
            context=backend_session.context,
            page=backend_session.page,
            headed=headed,
            backend_info=dict(backend_session.backend_info or {}),
        )
        if session.page is not None:
            self._bind_page_observers(session, session.page)
        return session

    def _require_session(self, session_id: str) -> BrowserControlSession:
        session = self.sessions.get(self._session_id(session_id))
        if session is None:
            raise KeyError("browser control session not found")
        return session

    def _require_page(self, session: BrowserControlSession) -> Any:
        if session.page is None or self._is_closed(session.page):
            raise RuntimeError("browser control page is closed")
        return session.page

    def _resolve_ref(self, session: BrowserControlSession, ref: str) -> str:
        key = str(ref or "").strip()
        selector = session.refs.get(key)
        if not selector:
            raise KeyError("element ref not found; call snapshot first")
        return selector

    def _public_session(self, session: BrowserControlSession) -> dict[str, Any]:
        url = ""
        if session.page is not None and not self._is_closed(session.page):
            url = str(getattr(session.page, "url", "") or "")
        return {
            "session_id": session.session_id,
            "url": url,
            "headed": session.headed,
            "created_at": session.created_at,
            "last_used_at": session.last_used_at,
            "ref_count": len(session.refs),
            "console_count": len(session.console_messages),
            "error_count": len(session.page_errors),
            "network_request_count": len(session.network_requests),
            "backend": dict(session.backend_info or {}),
        }

    def _open_pages(self, session: BrowserControlSession) -> list[Any]:
        if session.context is None:
            return []
        try:
            return [page for page in list(session.context.pages) if not self._is_closed(page)]
        except Exception:
            return [session.page] if session.page is not None and not self._is_closed(session.page) else []

    def _bind_page_observers(self, session: BrowserControlSession, page: Any) -> None:
        try:
            page.on("console", lambda msg: self._record_console(session, msg))
            page.on("pageerror", lambda exc: self._record_error(session, exc))
            page.on("request", lambda req: self._record_request(session, req))
        except Exception:
            pass

    def _record_console(self, session: BrowserControlSession, msg: Any) -> None:
        item = {
            "ts": time.time(),
            "type": str(getattr(msg, "type", "") or ""),
            "text": str(getattr(msg, "text", "") or ""),
        }
        session.console_messages.append(item)
        del session.console_messages[:-200]

    def _record_error(self, session: BrowserControlSession, exc: Any) -> None:
        session.page_errors.append({"ts": time.time(), "error": str(exc)})
        del session.page_errors[:-200]

    def _record_request(self, session: BrowserControlSession, req: Any) -> None:
        item = {
            "ts": time.time(),
            "method": str(getattr(req, "method", "") or ""),
            "url": str(getattr(req, "url", "") or ""),
            "resource_type": str(getattr(req, "resource_type", "") or ""),
        }
        session.network_requests.append(item)
        del session.network_requests[:-500]

    def _find_locator(
        self,
        page: Any,
        strategy: str,
        query: str,
        *,
        name: str = "",
        index: int = 0,
        exact: bool = True,
    ) -> Any:
        kind = str(strategy or "").lower()
        if kind == "role":
            kwargs: dict[str, Any] = {}
            if name:
                kwargs["name"] = name
                kwargs["exact"] = bool(exact)
            return page.get_by_role(str(query or ""), **kwargs)
        if kind == "text":
            return page.get_by_text(str(query or ""), exact=bool(exact))
        if kind == "label":
            return page.get_by_label(str(query or ""), exact=bool(exact))
        if kind in {"css", "selector"}:
            return page.locator(str(query or "body"))
        if kind == "first":
            return page.locator(str(query or "body")).first
        if kind == "nth":
            return page.locator(str(query or "body")).nth(int(index or 0))
        raise ValueError("unsupported find strategy")

    async def _run_locator_action(self, locator: Any, action: str, *, value: str = "", attr: str = "") -> Any:
        op = str(action or "text").lower()
        if op == "count":
            return await locator.count()
        target = locator.first
        if op == "click":
            await target.click(timeout=10000)
            return True
        if op == "dblclick":
            await target.dblclick(timeout=10000)
            return True
        if op == "fill":
            await target.fill(str(value), timeout=10000)
            return str(value)
        if op == "type":
            await target.type(str(value), timeout=10000)
            return str(value)
        if op == "hover":
            await target.hover(timeout=10000)
            return True
        if op == "focus":
            await target.focus(timeout=10000)
            return True
        if op == "check":
            await target.check(timeout=10000)
            return True
        if op == "uncheck":
            await target.uncheck(timeout=10000)
            return True
        if op == "text":
            return await target.inner_text(timeout=10000)
        if op == "html":
            return await target.inner_html(timeout=10000)
        if op == "value":
            return await target.input_value(timeout=10000)
        if op == "attr":
            return await target.get_attribute(str(attr or ""), timeout=10000)
        if op == "box":
            return await target.bounding_box(timeout=10000)
        if op == "is_visible":
            return bool(await target.is_visible(timeout=10000))
        if op == "is_enabled":
            return bool(await target.is_enabled(timeout=10000))
        if op == "is_checked":
            return bool(await target.is_checked(timeout=10000))
        raise ValueError("unsupported find action")

    @staticmethod
    def _session_id(session_id: str) -> str:
        sid = str(session_id or "default").strip()
        return sid or "default"

    @staticmethod
    def _is_closed(page: Any) -> bool:
        try:
            return bool(page.is_closed())
        except Exception:
            return False


def _backend_health_cache_ttl_s() -> float:
    try:
        value = float(os.getenv("VSPIDER_BROWSER_HEALTH_CACHE_TTL_MS", "5000")) / 1000.0
    except Exception:
        value = 5.0
    return max(0.0, min(300.0, value))


def _backend_health_cache_key(info: dict[str, Any]) -> str:
    config = info.get("config") if isinstance(info.get("config"), dict) else {}
    return "|".join([
        str(info.get("name") or ""),
        str(info.get("kind") or ""),
        str(info.get("transport") or ""),
        str(info.get("status") or ""),
        str(config.get("endpoint_configured") or False),
        str(config.get("mode") or ""),
    ])


def _backend_health_cache_meta(
    *,
    hit: bool,
    stale: bool,
    age_s: float,
    ttl_s: float,
    cached_at: float,
    refresh_error: str = "",
) -> dict[str, Any]:
    return {
        "hit": bool(hit),
        "stale": bool(stale),
        "age_s": round(max(0.0, float(age_s or 0.0)), 3),
        "ttl_s": round(max(0.0, float(ttl_s or 0.0)), 3),
        "cached_at": float(cached_at or 0.0),
        "refresh_error": str(refresh_error or ""),
    }

from __future__ import annotations

from typing import Any

from visual_web_agent.capability_router import route_task
from visual_web_agent import api_replay
from visual_web_agent.extraction_engine import generic
from visual_web_agent.spider_lite import SpiderLiteManager
from visual_web_agent.success_verifier import verify_route_success


def _normalise_per_step_systems(raw: Any) -> dict[str, dict[str, str]]:
    """Index ``payload["per_step_systems"]`` by capability for fast lookup.

    Accepts the canonical list-of-dicts form (each item carrying
    ``capability``, ``system_id``, optional ``auth_profile`` and
    ``step_id``). Anything that doesn't conform is silently dropped so a
    malformed planner output cannot crash the executor.
    """

    if not isinstance(raw, list):
        return {}
    out: dict[str, dict[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        capability = str(item.get("capability") or "").strip()
        system_id = str(item.get("system_id") or "").strip()
        if not capability or not system_id:
            continue
        out[capability] = {
            "system_id": system_id,
            "auth_profile": str(item.get("auth_profile") or "auto"),
            "step_id": str(item.get("step_id") or ""),
        }
    return out


def _stamp_system_metadata(
    attempts: list[dict[str, Any]],
    capability_to_system: dict[str, dict[str, str]],
    router: Any = None,
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    """Tag each attempt with its planned ``system_id`` and bucket per system.

    The legacy "no cross-system" path is preserved as
    ``systems_involved=["system_1"]`` so downstream consumers always see
    at least one system entry.

    When a :class:`~visual_web_agent.session_router.SessionRouter` is
    supplied (E1c-2), the effective ``auth_profile`` is resolved with the
    router's precedence -- an explicit per-step profile wins, else the
    system's profile declared in ``workflow_graph.systems``, else ``auto``
    (which is left unstamped).
    """

    systems_involved: list[str] = []
    system_attempts: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        capability = str(attempt.get("capability") or "")
        info = capability_to_system.get(capability)
        system_id = (info or {}).get("system_id") or "system_1"
        attempt["system_id"] = system_id
        explicit_profile = str((info or {}).get("auth_profile") or "")
        if router is not None:
            resolved_profile = router.resolved_auth_profile(system_id, explicit_profile or "auto")
        else:
            resolved_profile = explicit_profile
        if resolved_profile and resolved_profile != "auto":
            attempt["auth_profile"] = resolved_profile
        if info and info.get("step_id"):
            attempt["step_id"] = info["step_id"]
        if system_id not in systems_involved:
            systems_involved.append(system_id)
        system_attempts.setdefault(system_id, []).append(attempt)
    if not systems_involved:
        systems_involved.append("system_1")
        system_attempts.setdefault("system_1", [])
    return systems_involved, system_attempts


def _router_from_route(route: dict[str, Any]) -> Any:
    """Build a (pool-less) SessionRouter from a route's workflow_graph.

    Returns ``None`` on any failure so the executor degrades to the legacy
    per-step-only auth handling instead of crashing.
    """

    try:
        from .session_router import build_session_router

        return build_session_router("route_executor", route)
    except Exception:
        return None


def _build_session_plan(
    systems_involved: list[str],
    router: Any,
) -> list[dict[str, Any]]:
    """Resolve, per involved system, the session a downstream executor needs.

    Each entry is ``{system_id, auth_profile, domain}``. This is plan-stepped
    output: ``route_executor`` itself stays browserless, but E1c-3's reactive
    loop can use this to pre-acquire / switch ``BrowserSession`` handles.
    """

    plan: list[dict[str, Any]] = []
    for system_id in systems_involved:
        if router is not None:
            auth_profile = router.resolved_auth_profile(system_id)
            domain = router.plan.domain_for(system_id)
        else:
            auth_profile = "auto"
            domain = ""
        plan.append({
            "system_id": system_id,
            "auth_profile": auth_profile,
            "domain": domain,
        })
    return plan


def _attempt_error(capability: str, exc: Exception) -> dict[str, Any]:
    item: dict[str, Any] = {"capability": capability, "status": "error", "reason": str(exc)}
    action_trace = getattr(exc, "action_trace", None)
    if isinstance(action_trace, dict) and str(action_trace.get("version") or "") == "browser_action_trace.v1":
        item["action_trace"] = dict(action_trace)
        action_issue_summary = getattr(exc, "action_issue_summary", None) or action_trace.get("issue_summary")
        if isinstance(action_issue_summary, dict):
            item["action_issue_summary"] = dict(action_issue_summary)
    return item


class DeterministicRouteExecutor:
    def __init__(self, *, spider_lite: SpiderLiteManager | None = None) -> None:
        self.spider_lite = spider_lite or SpiderLiteManager()

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        goal = str(payload.get("goal") or payload.get("prompt") or "")
        url = str(payload.get("url") or payload.get("target_url") or payload.get("start_url") or "")
        if not goal:
            raise ValueError("goal is required")
        route = route_task(
            goal,
            url=url,
            context=dict(payload.get("context") or {}),
            limit=int(payload.get("limit") or 12),
        )
        repair_branches = list((route.get("workflow_graph") or {}).get("repair_branches") or [])
        attempts: list[dict[str, Any]] = []
        capability_to_system = _normalise_per_step_systems(payload.get("per_step_systems"))
        source = payload.get("source")
        if source is None:
            source = payload.get("html")
        if source not in (None, ""):
            selector_result = self._try_selector(payload, source, route, attempts)
            if selector_result is not None:
                return self._completed(route, attempts, selector_result, capability_to_system)
            extract_result = self._try_extract(payload, source, route, attempts)
            if extract_result is not None:
                return self._completed(route, attempts, extract_result, capability_to_system)
        else:
            attempts.append({"capability": "generic_extractor", "status": "skipped", "reason": "source/html not provided"})
        if bool(payload.get("allow_network")):
            api_result = self._try_api_replay(payload, route, attempts)
            if api_result is not None:
                return self._completed(route, attempts, api_result, capability_to_system)
            spider_result = self._try_spider(payload, route, attempts)
            if spider_result is not None:
                return self._completed(route, attempts, spider_result, capability_to_system)
        else:
            if _api_replay_candidates(payload):
                attempts.append({"capability": "api_replay", "status": "skipped", "reason": "allow_network is false"})
            attempts.append({"capability": "spider_lite", "status": "skipped", "reason": "allow_network is false"})
        status = "fallback" if any(item.get("status") == "attempted" for item in attempts) else "skipped"
        router = _router_from_route(route)
        systems_involved, system_attempts = _stamp_system_metadata(attempts, capability_to_system, router)
        return {
            "status": status,
            "completed": False,
            "route": route,
            "attempts": attempts,
            "result": None,
            "artifact": None,
            "fallback_reason": self._fallback_reason(attempts),
            "systems_involved": systems_involved,
            "system_attempts": system_attempts,
            "session_plan": _build_session_plan(systems_involved, router),
        }

    def _try_selector(self, payload: dict[str, Any], source: Any, route: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
        extract_config = payload.get("extract") if isinstance(payload.get("extract"), dict) else {}
        selector = payload.get("selector") or extract_config.get("selector") or ""
        selector_type = payload.get("selector_type") or payload.get("type") or extract_config.get("selector_type") or extract_config.get("type") or ""
        if not selector and not selector_type:
            attempts.append({"capability": "extractor_select", "status": "skipped", "reason": "selector not provided"})
            return None
        try:
            result = generic.select(
                str(source),
                selector=str(selector or ""),
                selector_type=str(selector_type or "css"),
                mode=str(payload.get("mode") or extract_config.get("mode") or "all"),
                output=str(payload.get("output") or extract_config.get("output") or "text"),
                attr=str(payload.get("attr") or extract_config.get("attr") or ""),
                text=str(payload.get("text") or extract_config.get("text") or ""),
                regex=str(payload.get("regex") or extract_config.get("regex") or ""),
                tag=str(payload.get("tag") or extract_config.get("tag") or ""),
                max_results=int(payload.get("max_results") or extract_config.get("max_results") or 100),
                case_sensitive=bool(payload.get("case_sensitive") or extract_config.get("case_sensitive")),
            )
        except Exception as exc:
            attempts.append(_attempt_error("extractor_select", exc))
            return None
        artifact = None
        if _save_artifact_required(route, payload) or bool(payload.get("export")):
            rows = [{"value": value} for value in (result.get("results") or [])]
            artifact = generic.export_jsonl({"rows": rows}, run_id=str(payload.get("run_id") or "route_executor"))
        verification = verify_route_success(route, capability="extractor_select", result=result, artifact=artifact, payload=payload)
        attempts.append({
            "capability": "extractor_select",
            "status": "attempted",
            "count": verification.get("observed_count"),
            "target_count": verification.get("target_count"),
            "completed": verification.get("passed"),
            "verification": verification,
            "verification_summary": verification.get("verification_summary"),
            "reason": "" if verification.get("passed") else verification.get("summary"),
        })
        if not verification.get("passed"):
            return None
        return {"capability": "extractor_select", "result": result, "artifact": artifact, "verification": verification}

    def _try_extract(self, payload: dict[str, Any], source: Any, route: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
        requested_fields = payload.get("requested_fields") or (route.get("strategy_context") or {}).get("requested_fields") or None
        try:
            result = generic.extract(
                source,
                source_type=str(payload.get("source_type") or "auto"),
                requested_fields=requested_fields,
                max_rows=int(payload.get("max_rows") or 1000),
            )
            artifact = None
            if bool(payload.get("export")) or _save_artifact_required(route, payload):
                artifact = generic.export_jsonl(result, run_id=str(payload.get("run_id") or "route_executor"))
        except Exception as exc:
            attempts.append(_attempt_error("generic_extractor", exc))
            return None
        verification = verify_route_success(route, capability="generic_extractor", result=result, artifact=artifact, payload=payload)
        attempts.append({
            "capability": "generic_extractor",
            "status": "attempted",
            "row_count": verification.get("observed_count"),
            "target_count": verification.get("target_count"),
            "completed": verification.get("passed"),
            "verification": verification,
            "verification_summary": verification.get("verification_summary"),
            "reason": "" if verification.get("passed") else verification.get("summary"),
        })
        if not verification.get("passed"):
            return None
        return {"capability": "generic_extractor", "result": result, "artifact": artifact, "verification": verification}

    def _try_api_replay(self, payload: dict[str, Any], route: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates = _api_replay_candidates(payload)
        if not candidates:
            return None
        candidate = api_replay.choose_candidate(
            candidates,
            endpoint=str(payload.get("endpoint") or payload.get("api_endpoint") or ""),
        )
        if candidate is None:
            attempts.append({"capability": "api_replay", "status": "skipped", "reason": "network candidate not provided"})
            return None
        headers = payload.get("api_headers") if isinstance(payload.get("api_headers"), dict) else payload.get("headers")
        if not isinstance(headers, dict):
            headers = {}
        fetcher = payload.get("api_replay_fetcher") if callable(payload.get("api_replay_fetcher")) else None
        try:
            result = api_replay.replay_candidate(
                run_id=str(payload.get("run_id") or "route_executor"),
                candidate=candidate,
                page=_positive_int(payload.get("page"), default=1),
                page_size=_positive_int(payload.get("page_size"), default=_target_count(route) or 50, max_value=500),
                timeout_s=float(payload.get("api_timeout_s") or payload.get("timeout_s") or 15.0),
                headers=dict(headers),
                fetcher=fetcher,
            )
        except Exception as exc:
            attempts.append(_attempt_error("api_replay", exc))
            return None
        artifact = result.get("artifact") if isinstance(result.get("artifact"), dict) else None
        verification = verify_route_success(route, capability="api_replay", result=result, artifact=artifact, payload=payload)
        attempts.append({
            "capability": "api_replay",
            "status": "attempted",
            "row_count": verification.get("observed_count"),
            "target_count": verification.get("target_count"),
            "http_status": result.get("http_status"),
            "completed": verification.get("passed"),
            "verification": verification,
            "verification_summary": verification.get("verification_summary"),
            "reason": "" if verification.get("passed") else verification.get("summary"),
        })
        if not verification.get("passed"):
            return None
        return {"capability": "api_replay", "result": result, "artifact": artifact, "verification": verification}

    def _try_spider(self, payload: dict[str, Any], route: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
        url = str(payload.get("url") or payload.get("target_url") or payload.get("start_url") or "")
        start_urls = payload.get("start_urls") or payload.get("urls") or ([url] if url else [])
        if not start_urls:
            attempts.append({"capability": "spider_lite", "status": "skipped", "reason": "start_urls not provided"})
            return None
        spider_payload = dict(payload.get("spider") or {})
        spider_payload.setdefault("run_id", str(payload.get("run_id") or "route_executor"))
        spider_payload.setdefault("start_urls", start_urls)
        spider_payload.setdefault("max_depth", int(payload.get("max_depth") or 0))
        spider_payload.setdefault("max_pages", int(payload.get("max_pages") or _target_pages(route) or 10))
        if isinstance(payload.get("extract"), dict):
            spider_payload.setdefault("extract", dict(payload.get("extract") or {}))
        spider_payload.setdefault("export", bool(payload.get("export")) or _save_artifact_required(route, payload))
        if payload.get("item_pipeline") is not None:
            spider_payload.setdefault("item_pipeline", dict(payload.get("item_pipeline") or {}))
        try:
            result = self.spider_lite.run(spider_payload)
        except Exception as exc:
            attempts.append(_attempt_error("spider_lite", exc))
            return None
        verification = verify_route_success(route, capability="spider_lite", result=result, artifact=result.get("artifact"), payload=payload)
        attempts.append({
            "capability": "spider_lite",
            "status": "attempted",
            "item_count": verification.get("observed_count"),
            "target_count": verification.get("target_count"),
            "page_count": result.get("page_count"),
            "target_pages": _target_pages(route),
            "completed": verification.get("passed"),
            "verification": verification,
            "verification_summary": verification.get("verification_summary"),
            "reason": "" if verification.get("passed") else verification.get("summary"),
        })
        if not verification.get("passed"):
            return None
        return {"capability": "spider_lite", "result": result, "artifact": result.get("artifact"), "verification": verification}

    def _completed(
        self,
        route: dict[str, Any],
        attempts: list[dict[str, Any]],
        payload: dict[str, Any],
        capability_to_system: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        router = _router_from_route(route)
        systems_involved, system_attempts = _stamp_system_metadata(
            attempts, capability_to_system or {}, router
        )
        return {
            "status": "completed",
            "completed": True,
            "route": route,
            "attempts": attempts,
            "capability": payload.get("capability"),
            "result": payload.get("result"),
            "artifact": payload.get("artifact"),
            "verification": payload.get("verification"),
            "fallback_reason": "",
            "systems_involved": systems_involved,
            "system_attempts": system_attempts,
            "session_plan": _build_session_plan(systems_involved, router),
        }

    def _fallback_reason(self, attempts: list[dict[str, Any]]) -> str:
        for item in reversed(attempts):
            if item.get("status") in {"error", "attempted"}:
                return str(item.get("reason") or f"{item.get('capability')} did not meet completion criteria")
        return "no eligible deterministic executor"


def execute_route(payload: dict[str, Any], *, spider_lite: SpiderLiteManager | None = None) -> dict[str, Any]:
    return DeterministicRouteExecutor(spider_lite=spider_lite).execute(payload)


def _target_count(route: dict[str, Any]) -> int | None:
    try:
        value = int((route.get("strategy_context") or {}).get("target_count") or 0)
    except Exception:
        value = 0
    return value if value > 0 else None


def _target_pages(route: dict[str, Any]) -> int | None:
    try:
        value = int((route.get("strategy_context") or {}).get("target_pages") or 0)
    except Exception:
        value = 0
    return value if value > 0 else None


def _positive_int(value: Any, *, default: int, max_value: int | None = None) -> int:
    try:
        number = int(value or default)
    except Exception:
        number = default
    number = max(1, number)
    if max_value is not None:
        number = min(number, max_value)
    return number


def _api_replay_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(raw: Any) -> None:
        if isinstance(raw, dict):
            if raw.get("endpoint") or raw.get("url"):
                out.append(dict(raw))
            return
        if isinstance(raw, list):
            for item in raw:
                add(item)

    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    for source in (
        payload.get("network_candidate"),
        payload.get("api_candidate"),
        payload.get("candidate"),
        payload.get("network_candidates"),
        payload.get("candidates"),
        context.get("network_candidate"),
        context.get("api_candidate"),
        context.get("candidate"),
        context.get("network_candidates"),
        context.get("candidates"),
    ):
        add(source)
    return out


def _save_artifact_required(route: dict[str, Any], payload: dict[str, Any]) -> bool:
    output_contract = (route.get("strategy_context") or {}).get("output_contract") or {}
    return bool(output_contract.get("save_artifact") or payload.get("export"))

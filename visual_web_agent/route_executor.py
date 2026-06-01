from __future__ import annotations

from typing import Any

from visual_web_agent.capability_router import route_task
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
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    """Tag each attempt with its planned ``system_id`` and bucket per system.

    The legacy "no cross-system" path is preserved as
    ``systems_involved=["system_1"]`` so downstream consumers always see
    at least one system entry.
    """

    systems_involved: list[str] = []
    system_attempts: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        capability = str(attempt.get("capability") or "")
        info = capability_to_system.get(capability)
        system_id = (info or {}).get("system_id") or "system_1"
        attempt["system_id"] = system_id
        if info and info.get("auth_profile") and info["auth_profile"] != "auto":
            attempt["auth_profile"] = info["auth_profile"]
        if info and info.get("step_id"):
            attempt["step_id"] = info["step_id"]
        if system_id not in systems_involved:
            systems_involved.append(system_id)
        system_attempts.setdefault(system_id, []).append(attempt)
    if not systems_involved:
        systems_involved.append("system_1")
        system_attempts.setdefault("system_1", [])
    return systems_involved, system_attempts


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
            spider_result = self._try_spider(payload, route, attempts)
            if spider_result is not None:
                return self._completed(route, attempts, spider_result, capability_to_system)
        else:
            attempts.append({"capability": "spider_lite", "status": "skipped", "reason": "allow_network is false"})
        status = "fallback" if any(item.get("status") == "attempted" for item in attempts) else "skipped"
        systems_involved, system_attempts = _stamp_system_metadata(attempts, capability_to_system)
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
        spider_payload.setdefault("max_pages", int(payload.get("max_pages") or 10))
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
        systems_involved, system_attempts = _stamp_system_metadata(
            attempts, capability_to_system or {}
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


def _save_artifact_required(route: dict[str, Any], payload: dict[str, Any]) -> bool:
    output_contract = (route.get("strategy_context") or {}).get("output_contract") or {}
    return bool(output_contract.get("save_artifact") or payload.get("export"))

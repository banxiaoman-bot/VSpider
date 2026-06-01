from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException

from .action_ref import action_ref_schema, normalize_action_ref, normalize_action_refs, summarize_action_ref_sources
from .agent_case_benchmark import build_agent_case_benchmark_matrix, load_agent_case_benchmark_cases, summarize_agent_case_benchmark_results
from .agent_case_regression import build_agent_case_regression_report
from .browser_runtime_doctor import build_browser_runtime_doctor
from .capability_manifest import get_capability, list_capabilities
from .capability_router import model_role_report, route_task
from .crawl_efficiency import build_crawl_efficiency_plan
from .io_contract import OutputPrediction, RunOutputProtocol, build_input_contract, ensure_contract_skeleton, write_input_contract, write_output_contract, write_output_prediction, record_verification_evidence, record_template_experience
from .efficiency_correlation import build_efficiency_correlation_report
from .run_evidence_bundle import build_run_evidence_bundle


@dataclass(frozen=True)
class CapabilityApiDeps:
    get_browser_pool_status: Callable[[], dict[str, Any]]
    get_browser_runtime_status: Callable[..., dict[str, Any]]
    browser_backend_status: Callable[[], dict[str, Any]]


def capability_crawl_efficiency_plan(
    payload: dict[str, Any] | None = None,
    route: dict[str, Any] | None = None,
    *,
    runtime_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = dict(payload or {})
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    network_candidates = (
        data.get("network_candidates")
        or data.get("candidates")
        or context.get("network_candidates")
        or context.get("candidates")
    )
    browser_state = data.get("browser_state") or context.get("browser_state")
    runtime = runtime_status or data.get("runtime_status") or context.get("runtime_status") or context.get("browser_runtime")
    return build_crawl_efficiency_plan(
        data,
        route=route,
        network_candidates=network_candidates,
        browser_state=browser_state,
        runtime_status=runtime,
    )


def capability_efficiency_correlation_report(
    result: dict[str, Any],
    *,
    browser_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = browser_state or (result.get("browser_state") if isinstance(result.get("browser_state"), dict) else {})
    return build_efficiency_correlation_report(
        result,
        crawl_efficiency_plan=result.get("crawl_efficiency_plan") if isinstance(result.get("crawl_efficiency_plan"), dict) else {},
        deterministic_evaluation=result.get("deterministic_evaluation") if isinstance(result.get("deterministic_evaluation"), dict) else {},
        action_reliability=result.get("action_reliability") if isinstance(result.get("action_reliability"), dict) else {},
        browser_state=state,
    )


def create_capability_router(deps: CapabilityApiDeps) -> APIRouter:
    router = APIRouter(tags=["capabilities"])

    def current_runtime_status() -> dict[str, Any]:
        return deps.get_browser_runtime_status(
            pool_status=deps.get_browser_pool_status(),
            backend_status=deps.browser_backend_status(),
        )

    @router.post("/api/capabilities/route", summary="为任务生成全能力路由与兜底策略（Y29）")
    async def route_capabilities(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        goal = str(payload.get("goal") or payload.get("prompt") or "")
        url = str(payload.get("url") or payload.get("target_url") or "")
        if not goal:
            raise HTTPException(status_code=400, detail="goal is required")
        input_contract = build_input_contract(
            goal=goal,
            target_url=url,
            urls=payload.get("urls"),
            attachments=payload.get("attachments"),
            auth_profiles=payload.get("auth_profiles") or "",
            vlm_options=payload.get("vlm_options") or None,
            constraints=payload.get("constraints") or None,
            source=str(payload.get("source") or "api"),
        )
        output_prediction = OutputPrediction.from_dict(
            payload.get("output_prediction")
            or payload.get("model_output")
            or payload.get("model_prediction")
            or payload.get("output_contract_prediction")
            or {}
        )
        protocol = RunOutputProtocol(
            input_contract=input_contract,
            output_prediction=output_prediction,
        )
        runtime_status = current_runtime_status()
        result = route_task(
            goal,
            url=url,
            context={
                **dict(payload.get("context") or {}),
                "input_contract": input_contract.to_dict(),
                "output_prediction": output_prediction.to_dict(),
                "run_protocol": protocol,
            },
            limit=int(payload.get("limit") or 12),
            runtime_status=runtime_status,
        )
        result["crawl_efficiency_plan"] = capability_crawl_efficiency_plan(
            payload,
            result,
            runtime_status=runtime_status,
        )
        result["run_protocol"] = protocol.to_dict()
        run_id = str(payload.get("run_id") or payload.get("id") or "")
        if run_id:
            base_dir = payload.get("base_dir")
            verify_summary = (result.get("verification") or {}).get("verification_summary") if isinstance(result.get("verification"), dict) else {}
            protocol_with_verification = RunOutputProtocol(
                input_contract=input_contract,
                output_prediction=output_prediction,
                output_contract=protocol.resolved_output_contract(),
                verification_summary=dict(verify_summary or {}),
            )
            ensure_contract_skeleton(run_id, base_dir=base_dir)
            write_input_contract(run_id, input_contract, base_dir=base_dir)
            write_output_prediction(run_id, output_prediction, base_dir=base_dir)
            write_output_contract(run_id, protocol_with_verification.resolved_output_contract(), base_dir=base_dir)
            if isinstance(result.get("verification"), dict):
                record_verification_evidence(run_id, verification=result.get("verification") or {}, base_dir=base_dir)
                if isinstance(result.get("template"), dict):
                    record_template_experience(
                        run_id,
                        template=result.get("template") or {},
                        verification=result.get("verification") or {},
                        base_dir=base_dir,
                    )
            result["run_protocol"] = protocol_with_verification.to_dict()
        return {"status": "success", "result": result}

    @router.post("/api/capabilities/plan", summary="为任务生成结构化执行计划（Y44）")
    async def plan_capabilities(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        goal = str(payload.get("goal") or payload.get("prompt") or "")
        url = str(payload.get("url") or payload.get("target_url") or "")
        if not goal:
            raise HTTPException(status_code=400, detail="goal is required")
        runtime_status = current_runtime_status()
        route = route_task(
            goal,
            url=url,
            context=dict(payload.get("context") or {}),
            limit=int(payload.get("limit") or 12),
            runtime_status=runtime_status,
        )
        route["crawl_efficiency_plan"] = capability_crawl_efficiency_plan(
            payload,
            route,
            runtime_status=runtime_status,
        )
        return {
            "status": "success",
            "result": {
                "intent": route.get("intent") or {},
                "execution_plan": route.get("execution_plan") or {},
                "workflow_graph": route.get("workflow_graph") or {},
                "crawl_efficiency_plan": route.get("crawl_efficiency_plan") or {},
                "capability_manifest": route.get("capability_manifest") or [],
                "route": route if bool(payload.get("include_route")) else None,
            },
        }

    @router.post("/api/capabilities/workflow", summary="为任务生成跨系统工作流图（Y45）")
    async def workflow_capabilities(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        goal = str(payload.get("goal") or payload.get("prompt") or "")
        url = str(payload.get("url") or payload.get("target_url") or "")
        if not goal:
            raise HTTPException(status_code=400, detail="goal is required")
        route = route_task(
            goal,
            url=url,
            context=dict(payload.get("context") or {}),
            limit=int(payload.get("limit") or 12),
            runtime_status=current_runtime_status(),
        )
        return {
            "status": "success",
            "result": {
                "intent": route.get("intent") or {},
                "workflow_graph": route.get("workflow_graph") or {},
                "execution_plan": route.get("execution_plan") or {},
                "route": route if bool(payload.get("include_route")) else None,
            },
        }

    @router.get("/api/capabilities/manifest", summary="列出 VSpider 可规划能力清单（Y43）")
    async def get_capability_manifest(
        category: str = "",
        layer: str = "",
        include_disabled: bool = False,
    ) -> dict:
        capabilities = list_capabilities(
            category=category or None,
            layer=layer or None,
            include_disabled=include_disabled,
        )
        categories = sorted({str(item.get("category") or "") for item in capabilities if item.get("category")})
        layers = sorted({str(item.get("layer") or "") for item in capabilities if item.get("layer")})
        return {
            "status": "success",
            "result": {
                "count": len(capabilities),
                "categories": categories,
                "layers": layers,
                "capabilities": capabilities,
            },
        }

    @router.get("/api/capabilities/manifest/{name}", summary="查看单个 VSpider 可规划能力详情（Y43）")
    async def get_capability_manifest_item(name: str) -> dict:
        capability = get_capability(name)
        if capability is None:
            raise HTTPException(status_code=404, detail="capability not found")
        return {"status": "success", "result": capability}

    @router.get("/api/capabilities/model_roles", summary="查看视觉/语义/确定性能力职责边界（Y29）")
    async def get_capability_model_roles() -> dict:
        return {"status": "success", "result": model_role_report()}

    @router.get("/api/capabilities/agent_case_benchmark")
    async def get_agent_case_benchmark(include_cases: bool = True) -> dict:
        cases = load_agent_case_benchmark_cases()
        matrix = build_agent_case_benchmark_matrix(cases)
        if not include_cases:
            matrix = dict(matrix)
            matrix["cases"] = []
        return {"status": "success", "result": {"matrix": matrix}}

    @router.post("/api/capabilities/agent_case_benchmark")
    async def summarize_agent_case_benchmark(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        raw_cases = payload.get("cases")
        cases = [dict(item) for item in raw_cases if isinstance(item, dict)] if isinstance(raw_cases, list) else load_agent_case_benchmark_cases()
        raw_results = payload.get("results")
        results = [dict(item) for item in raw_results if isinstance(item, dict)] if isinstance(raw_results, list) else []
        matrix = build_agent_case_benchmark_matrix(cases)
        summary = summarize_agent_case_benchmark_results(cases, results)
        if not bool(payload.get("include_cases", True)):
            matrix = dict(matrix)
            matrix["cases"] = []
        return {"status": "success", "result": {"matrix": matrix, "summary": summary}}

    @router.get("/api/capabilities/agent_case_regression")
    async def get_agent_case_regression(include_cases: bool = False) -> dict:
        report = build_agent_case_regression_report({
            "include_saved_reports": True,
            "include_cases": include_cases,
        })
        return {"status": "success", "result": {"report": report}}

    @router.post("/api/capabilities/agent_case_regression")
    async def build_agent_case_regression_route(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        report = build_agent_case_regression_report(payload)
        return {"status": "success", "result": {"report": report}}

    @router.post("/api/capabilities/run_evidence_bundle")
    async def build_run_evidence_bundle_route(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        bundle = build_run_evidence_bundle(payload)
        return {"status": "success", "result": {"bundle": bundle}}

    @router.get("/api/capabilities/browser_runtime_doctor")
    async def get_browser_runtime_doctor() -> dict:
        pool = deps.get_browser_pool_status()
        backend = deps.browser_backend_status()
        runtime = deps.get_browser_runtime_status(pool_status=pool, backend_status=backend)
        report = build_browser_runtime_doctor({
            "pool_status": pool,
            "backend_status": backend,
            "runtime_status": runtime,
        })
        return {"status": "success", "result": {"report": report}}

    @router.post("/api/capabilities/browser_runtime_doctor")
    async def build_browser_runtime_doctor_route(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        report = build_browser_runtime_doctor(payload)
        return {"status": "success", "result": {"report": report}}

    @router.get("/api/action_refs/schema", summary="查看统一 ActionRef 引用契约（Y46）")
    async def get_action_ref_schema() -> dict:
        return {"status": "success", "result": action_ref_schema()}

    @router.post("/api/action_refs/normalize", summary="归一化 SoM/AX/selector/bbox/browser ref 为 ActionRef（Y46）")
    async def normalize_action_refs_api(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        session_id = str(payload.get("session_id") or "")
        default_source = str(payload.get("source") or payload.get("default_source") or "unknown")
        if isinstance(payload.get("items"), list):
            refs = normalize_action_refs(
                payload.get("items") or [],
                default_source=default_source,
                session_id=session_id,
            )
        else:
            item = payload.get("ref") if "ref" in payload and len(payload) <= 4 else payload
            refs = [normalize_action_ref(item, default_source=default_source, session_id=session_id)]
        return {
            "status": "success",
            "result": {
                "refs": refs,
                "summary": summarize_action_ref_sources(refs),
            },
        }

    return router

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from visual_web_agent.action_ref import action_ref_schema
from visual_web_agent.action_registry import build_default_action_registry
from visual_web_agent.agent_strategy import (
    parse_goal_requested_fields,
    parse_goal_target_count,
    parse_goal_target_pages,
)
from visual_web_agent.browser_pool import build_browser_runtime_preflight, get_browser_runtime_status
from visual_web_agent.capability_manifest import summarize_capabilities
from visual_web_agent.crawl_efficiency import build_crawl_efficiency_plan
from visual_web_agent.extraction_engine.strategies import infer_goal_output_contract, infer_goal_strategy_context
from visual_web_agent.planner_contract import build_execution_plan
from visual_web_agent.workflow_graph import build_workflow_graph
from visual_web_agent.task_templates import TaskTemplateRegistry
from visual_web_agent.semantic_router import inject_capabilities_into_plan

try:
    from visual_web_agent.io_contract import RunOutputProtocol
except Exception:  # pragma: no cover - import fallback for partial environments
    RunOutputProtocol = Any  # type: ignore[assignment]


_STRUCTURED_RE = re.compile(r"\b(extract|scrape|crawl|collect|dataset|records?|rows?|table|list|feed|export|csv|excel|xlsx)\b|提取|抓取|采集|爬取|表格|列表|数据|导出|保存", re.I)
_CRAWL_RE = re.compile(r"\b(crawl|spider|scrape|follow links?|multi[- ]?page|pagination|all pages?)\b|爬取|爬虫|多页|翻页|全站|所有页", re.I)
_API_RE = re.compile(r"\b(api|xhr|fetch|network|json|endpoint|replay)\b|接口|网络|请求|回放", re.I)
_FULL_CONTENT_RE = re.compile(
    r"\b(full|complete|detail|article|body|premium|vip|unlock)\b|完整|全文|详情|正文|会员|解锁",
    re.I,
)
_MARKDOWN_RE = re.compile(
    r"\b(markdown|readable|reader[ -]?mode|clean text|main content|rag|llm[ -]?friendly)\b|正文提取|转\s*markdown|网页转\s*md|可读正文|喂给?大模型",
    re.I,
)
_VSCROLL_RE = re.compile(
    r"\b(virtual\s*(?:scroll|list|table)|virtuali[sz]ed?|infinite\s*scroll|react-window|vue-virtual)\b"
    r"|虚拟滚动|虚拟列表|虚拟表格|无限滚动|滚动加载|滚动采集|全量采集",
    re.I,
)
_FORM_RE = re.compile(r"\b(form|fill|submit|register|input|textbox)\b|表单|填写|填入|提交|输入", re.I)
_CHAT_RE = re.compile(r"\b(chatgpt|claude|kimi|deepseek|gemini|copilot|chat|ai answer)\b|文心|豆包|通义|元宝|智谱|助手|聊天|对话|AI", re.I)
_FILE_RE = re.compile(r"\b(upload|download|file|excel|csv|xlsx)\b|上传|下载|文件|导入|导出", re.I)
_CACHE_RE = re.compile(r"\b(cache|record|replay|debug|dev mode)\b|缓存|录制|回放|调试", re.I)
_QUEUE_RE = re.compile(r"\b(batch|queue|retry|resume|recover|watchdog|worker|metrics|checkpoint)\b|批量|队列|重试|恢复|续跑|断点|续传|暂停|指标|监控", re.I)
_RESUME_RE = re.compile(r"\bresume\b|\bcontinue\s+(the\s+)?(last|previous|prior)\b|continue\s+where|left\s+off|pick\s+up\s+where|断点续跑|断点续传|接着上次|继续上次|上次没做完|上次没完成|接着之前|继续之前", re.I)
_BROWSER_RE = re.compile(r"\b(click|scroll|hover|tab|cookie|storage|console|screenshot|browser|locator|selector|similar)\b|点击|滚动|悬停|标签页|浏览器|选择器|相似元素", re.I)
_AUTH_RE = re.compile(r"\b(login|signin|auth|captcha|2fa|otp)\b|登录|认证|验证码|短信", re.I)

_TASK_TEMPLATE_REGISTRY = TaskTemplateRegistry()


_FAILURE_REPAIR_HINTS: dict[str, dict[str, Any]] = {
    "selector_missing": {
        "repair_actions": ["refresh_dom_snapshot", "selector_generator", "browser_control_find"],
        "avoid_actions": ["blind_repeat_click"],
        "preferred_capabilities": ["browser_control_find", "selector_generator", "action_ref_normalizer"],
    },
    "timeout": {
        "repair_actions": ["wait_and_retry", "browser_pool_health_check", "backend_check"],
        "avoid_actions": ["immediate_repeat_without_wait"],
        "preferred_capabilities": ["browser_pool", "browser_backend_abstraction", "browser_control_find"],
    },
    "element_not_visible": {
        "repair_actions": ["scroll_into_view", "refresh_layout_snapshot", "vision_fallback"],
        "avoid_actions": ["click_without_visibility_check"],
        "preferred_capabilities": ["browser_control_find", "selector_generator", "vision_agent"],
    },
    "element_disabled": {
        "repair_actions": ["wait_for_enabled_state", "refresh_state", "alternate_action_path"],
        "avoid_actions": ["submit_before_enabled_state"],
        "preferred_capabilities": ["browser_control_find", "selector_generator", "semantic_planner_reflector"],
    },
    "click_intercepted": {
        "repair_actions": ["close_overlay", "alternate_click_target", "vision_fallback"],
        "avoid_actions": ["repeat_click_without_overlay_check"],
        "preferred_capabilities": ["browser_control_find", "selector_generator", "vision_agent"],
    },
    "context_closed": {
        "repair_actions": ["reopen_browser_context", "browser_pool_reset"],
        "avoid_actions": ["reuse_closed_browser_context"],
        "preferred_capabilities": ["browser_pool", "browser_backend_abstraction", "browser_control"],
    },
    "backend_unavailable": {
        "repair_actions": ["backend_health_check", "switch_backend", "defer_browser_action"],
        "avoid_actions": ["open_new_page_without_backend_health_check"],
        "preferred_capabilities": ["browser_backend_abstraction", "browser_pool", "human_guard"],
    },
    "navigation_failed": {
        "repair_actions": ["check_url", "api_replay", "network_intelligence"],
        "avoid_actions": ["repeat_navigation_without_url_or_network_check"],
        "preferred_capabilities": ["network_intelligence", "api_replay", "spider_lite"],
    },
    "input_rejected": {
        "repair_actions": ["normalize_action_ref", "validate_target", "retry_with_clean_input"],
        "avoid_actions": ["reuse_unverified_input_target"],
        "preferred_capabilities": ["action_ref_normalizer", "browser_control_find", "selector_generator"],
    },
}


def build_failure_repair_feedback(primary_failure: str, recommended_actions: list[str] | None = None) -> dict[str, Any]:
    code = str(primary_failure or "").strip().lower()
    hints = dict(_FAILURE_REPAIR_HINTS.get(code) or {})
    repair_actions = list(dict.fromkeys((recommended_actions or []) + list(hints.get("repair_actions") or [])))
    return {
        "version": "failure_repair.v1",
        "primary_failure": code,
        "repair_actions": repair_actions or ["review_and_retry"],
        "avoid_actions": list(hints.get("avoid_actions") or ["blind_retry_without_analysis"]),
        "preferred_capabilities": list(hints.get("preferred_capabilities") or ["semantic_planner_reflector", "browser_control_find"]),
    }


def _extract_model_output_prediction(context: dict[str, Any]) -> dict[str, Any]:
    from visual_web_agent.io_contract import OutputPrediction

    raw = (
        context.get("model_output")
        or context.get("output_prediction")
        or context.get("model_prediction")
        or context.get("output_contract_prediction")
        or {}
    )
    if isinstance(raw, OutputPrediction):
        return raw.normalized().to_dict()
    return OutputPrediction.from_dict(raw).to_dict()


def planner_feedback_from_failure_bundle(failure_bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(failure_bundle or {}) if isinstance(failure_bundle, dict) else {}
    if not data:
        return {}
    primary_failure = str(data.get("primary_failure") or "")
    if not primary_failure:
        return {}
    action_trace = dict(data.get("action_trace") or {}) if isinstance(data.get("action_trace"), dict) else {}
    target = dict(action_trace.get("target") or {}) if isinstance(action_trace.get("target"), dict) else {}
    action_ref = dict(action_trace.get("action_ref") or {}) if isinstance(action_trace.get("action_ref"), dict) else {}
    recommended_actions = [
        str(item)
        for item in (data.get("recommended_actions") or [])
        if str(item or "") and str(item or "") != "continue"
    ]
    recommended_action = str(data.get("recommended_action") or "")
    if recommended_action and recommended_action != "continue":
        recommended_actions.append(recommended_action)
    preferred_capabilities = _planner_feedback_preferred_capabilities(primary_failure, recommended_actions)
    avoid_actions = _planner_feedback_avoid_actions(primary_failure)
    return {
        "version": "planner_feedback.v1",
        "source": "capability_execute_failure_bundle",
        "status": "active",
        "primary_failure": primary_failure,
        "failure_category": str(data.get("failure_category") or ""),
        "failed_capability": str(data.get("capability") or ""),
        "failed_action": str(data.get("action") or ""),
        "fallback_reason": str(data.get("fallback_reason") or ""),
        "recommended_action": recommended_action or (recommended_actions[0] if recommended_actions else ""),
        "recommended_actions": list(dict.fromkeys(recommended_actions)),
        "preferred_capabilities": preferred_capabilities,
        "avoid_actions": avoid_actions,
        "target": {
            "ref": str(action_ref.get("ref") or target.get("ref") or ""),
            "selector": str(action_ref.get("selector") or target.get("selector") or ""),
        },
    }


def _planner_feedback_from_context(context: dict[str, Any]) -> dict[str, Any]:
    for key in ("failure_bundle", "capability_execute_failure_bundle", "last_failure_bundle"):
        item = context.get(key)
        if isinstance(item, dict):
            feedback = planner_feedback_from_failure_bundle(item)
            if feedback:
                return feedback
    execute = context.get("capability_execute")
    if isinstance(execute, dict) and isinstance(execute.get("failure_bundle"), dict):
        return planner_feedback_from_failure_bundle(execute.get("failure_bundle"))
    return {}


def _planner_feedback_preferred_capabilities(primary_failure: str, recommended_actions: list[str]) -> list[str]:
    code = str(primary_failure or "").lower()
    mapping = {
        "selector_missing": ["browser_control_find", "selector_generator", "action_ref_normalizer"],
        "timeout": ["browser_pool", "browser_backend_abstraction", "browser_control_find"],
        "element_not_visible": ["browser_control_find", "selector_generator", "vision_agent"],
        "element_disabled": ["browser_control_find", "selector_generator", "semantic_planner_reflector"],
        "click_intercepted": ["browser_control_find", "selector_generator", "vision_agent"],
        "context_closed": ["browser_pool", "browser_backend_abstraction", "browser_control"],
        "backend_unavailable": ["browser_backend_abstraction", "browser_pool", "human_guard"],
        "navigation_failed": ["network_intelligence", "api_replay", "spider_lite"],
        "input_rejected": ["action_ref_normalizer", "browser_control_find", "selector_generator"],
    }
    out = list(mapping.get(code) or ["semantic_planner_reflector", "browser_control_find"])
    if any("similar" in item for item in recommended_actions) and "selector_generator" not in out:
        out.append("selector_generator")
    if any("runtime" in item or "backend" in item for item in recommended_actions) and "browser_backend_abstraction" not in out:
        out.append("browser_backend_abstraction")
    return list(dict.fromkeys(out))[:6]


def _planner_feedback_avoid_actions(primary_failure: str) -> list[str]:
    code = str(primary_failure or "").lower()
    mapping = {
        "selector_missing": ["retry_same_action_ref_without_refresh", "blind_visual_click_before_locator_refresh"],
        "timeout": ["immediate_repeat_without_runtime_check"],
        "element_not_visible": ["click_without_scroll_or_visibility_wait"],
        "element_disabled": ["submit_before_enabled_state"],
        "click_intercepted": ["repeat_click_without_overlay_check"],
        "context_closed": ["reuse_closed_browser_context"],
        "backend_unavailable": ["open_new_page_without_backend_health_check"],
        "navigation_failed": ["repeat_navigation_without_url_or_network_check"],
        "input_rejected": ["reuse_unverified_input_target"],
    }
    return list(mapping.get(code) or ["blind_retry_without_failure_review"])


def route_task(
    goal: str,
    *,
    url: str = "",
    context: dict[str, Any] | RunOutputProtocol | None = None,
    limit: int = 12,
    runtime_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = f"{goal or ''} {url or ''}"
    if isinstance(context, RunOutputProtocol):
        route_context = dict(context.to_dict())
        route_context.setdefault("input_contract", context.input_contract.to_dict())
        route_context.setdefault("output_prediction", context.output_prediction.to_dict())
        route_context.setdefault("output_contract", context.resolved_output_contract().to_dict())
        goal = goal or context.input_contract.goal
    else:
        route_context = dict(context or {})
    target_count = _parse_target_count(text)
    target_pages = _parse_target_pages(goal)
    requested_fields = _parse_requested_fields(goal)
    strategy_context = infer_goal_strategy_context(
        goal,
        url=url,
        target_count=target_count,
        target_pages=target_pages,
        requested_fields=requested_fields,
        data_shape=route_context.get("data_shape"),
    )
    registry = build_default_action_registry()
    selected_tools = registry.select_for_goal(goal, limit=limit, strategy_context=strategy_context)
    try:
        from visual_web_agent.experience_reuse import build_experience_hints
        from visual_web_agent.semantic_router import apply_semantic_route_enrichment

        _experience_hints = build_experience_hints(goal, url)
        route_context = {**route_context, "experience_reuse": _experience_hints}
        _semantic = apply_semantic_route_enrichment(
            goal=goal,
            url=url,
            signals=_signals(f"{goal or ''} {url or ''}", strategy_context),
            selected_tools=selected_tools,
            strategy_context=strategy_context,
            experience_hints=_experience_hints,
        )
        selected_tools = list(_semantic.get("selected_agent_tools") or selected_tools)
        strategy_context = dict(_semantic.get("strategy_context_patch") or strategy_context)
        route_context["semantic_route"] = _semantic.get("semantic_route") or {}
        route_context["page_understanding"] = (
            (_semantic.get("semantic_route") or {}).get("page_understanding_first")
        )
        _semantic_inject = list(_semantic.get("inject_capabilities") or [])
        _semantic_signals_patch = dict(_semantic.get("signals_patch") or {})
    except Exception:
        _experience_hints = {}
        _semantic = {}
        _semantic_inject = []
        _semantic_signals_patch = {}
    template, template_score, template_status = _TASK_TEMPLATE_REGISTRY.best_match(goal, {"url": url, **route_context})
    fallback_template = None
    strategy_mode = "template"
    template_degraded = template_status == "template_degraded"
    if template is not None:
        strategy_context["task_template"] = template.to_dict()
        if template.recommended_output_kind:
            strategy_context["template_output_kind"] = template.recommended_output_kind
        if template.recommended_container:
            strategy_context["template_container"] = template.recommended_container
    elif template_status == "template_degraded":
        fallback_template = _TASK_TEMPLATE_REGISTRY.get("crawl_pagination.v1") or _TASK_TEMPLATE_REGISTRY.get("api_replay.v1")
        strategy_mode = "template_fallback" if fallback_template is not None else "generic_planning"
        strategy_context["template_fallback_reason"] = "template_underperformed"
        strategy_context["template_fallback_mode"] = strategy_mode
        if fallback_template is not None:
            strategy_context["template_fallback_template"] = fallback_template.to_dict()
    elif template_status == "no_template_match":
        strategy_mode = "generic_planning"
    else:
        strategy_mode = "template"
    output_contract_dict: dict[str, Any] = {}
    model_output_hint = _extract_model_output_prediction(route_context)
    try:
        from visual_web_agent.io_contract import infer_output_contract as _infer_oc

        output_contract_dict = _infer_oc(
            goal,
            target_count=target_count,
            requested_fields=requested_fields,
            model_predicted_kind=model_output_hint.get("kind", ""),
            model_predicted_mode=model_output_hint.get("mode", ""),
        ).to_dict()
    except Exception:
        output_contract_dict = {}
    output_kind = str(output_contract_dict.get("output_kind") or "")
    if output_kind.startswith("media_") or output_kind == "file_generic":
        if not any(
            (isinstance(item, dict) and item.get("name") == "media_harvester")
            for item in selected_tools
        ):
            harvester_tool = registry.get("media_harvester")
            if harvester_tool is not None:
                forced = harvester_tool.to_dict()
                forced["match_score"] = 10
                forced["strategy_reasons"] = [
                    f"forced_by_output_kind:{output_kind}",
                ]
                selected_tools = [forced, *selected_tools]
    signals = _signals(text, strategy_context)
    signals.update(_semantic_signals_patch)
    planner_feedback = _planner_feedback_from_context(route_context)
    failure_repair_feedback = {}
    failure_bundle = route_context.get("failure_bundle") or route_context.get("capability_execute_failure_bundle") or route_context.get("last_failure_bundle")
    if isinstance(failure_bundle, dict):
        failure_repair_feedback = build_failure_repair_feedback(
            str(failure_bundle.get("primary_failure") or planner_feedback.get("primary_failure") or ""),
            list(planner_feedback.get("recommended_actions") or []),
        )
        if failure_repair_feedback:
            planner_feedback = {**planner_feedback, **failure_repair_feedback}
    if planner_feedback:
        signals["planner_feedback"] = True
        signals["previous_failure"] = planner_feedback.get("primary_failure")
    backend_plan = _backend_plan(signals, strategy_context, selected_tools)
    backend_plan = inject_capabilities_into_plan(backend_plan, _semantic_inject)
    fallback_chain = _fallback_chain(signals, strategy_context, backend_plan)
    runtime_preflight = {}
    if signals.get("browser_interaction"):
        runtime_snapshot = runtime_status or route_context.get("browser_runtime") or route_context.get("runtime_status")
        if not isinstance(runtime_snapshot, dict) or not runtime_snapshot:
            runtime_snapshot = get_browser_runtime_status()
        runtime_preflight = build_browser_runtime_preflight(runtime_snapshot)
    result = {
        "goal": str(goal or ""),
        "url": str(url or ""),
        "context": route_context,
        "template": template.to_dict() if template is not None else None,
        "template_fallback_template": fallback_template.to_dict() if fallback_template is not None else None,
        "template_status": template_status,
        "template_score": template_score,
        "template_degraded": template_degraded,
        "strategy_mode": strategy_mode,
        "intent": _intent(signals, strategy_context),
        "signals": signals,
        "strategy_context": strategy_context,
        "selected_agent_tools": selected_tools,
        "backend_plan": backend_plan,
        "fallback_chain": fallback_chain,
        "semantic_route": route_context.get("semantic_route") or _semantic.get("semantic_route") or {},
        "experience_reuse": route_context.get("experience_reuse") or _experience_hints,
        "page_understanding": route_context.get("page_understanding"),
        "model_roles": model_role_report(signals=signals),
        "audit": audit_model_placement(signals=signals, selected_tools=selected_tools, backend_plan=backend_plan),
    }
    if planner_feedback:
        result["planner_feedback"] = planner_feedback
    if failure_repair_feedback:
        result["failure_repair"] = failure_repair_feedback
    if runtime_preflight:
        result["runtime_preflight"] = runtime_preflight
    result["crawl_efficiency_plan"] = build_crawl_efficiency_plan(
        {"goal": goal, "url": url, **route_context},
        route=result,
        network_candidates=route_context.get("network_candidates") or route_context.get("candidates"),
        browser_state=route_context.get("browser_state"),
        runtime_status=runtime_status or route_context.get("runtime_status") or route_context.get("browser_runtime"),
    )
    if template is not None:
        _TASK_TEMPLATE_REGISTRY.record_experience(template.id, passed=bool((result.get("verification") or {}).get("passed")))
    result["capability_manifest"] = summarize_capabilities(
        [str(item.get("name") or "") for item in backend_plan if isinstance(item, dict)]
    )
    result["action_ref_schema"] = action_ref_schema()
    if output_contract_dict:
        result["output_contract"] = output_contract_dict
    result["execution_plan"] = build_execution_plan(result)
    result["workflow_graph"] = build_workflow_graph(result)
    return result


def model_role_report(*, signals: dict[str, Any] | None = None) -> dict[str, Any]:
    sig = signals or {}
    return {
        "deterministic_router": {
            "position": "before model calls and before low-level browser actions",
            "responsibilities": [
                "classify task surface",
                "rank deterministic backend capabilities across Y1-Y28",
                "choose fallback order",
                "define success and escalation conditions",
            ],
            "should_not_do": ["read screenshots", "invent page state", "solve CAPTCHA"],
        },
        "semantic_model": {
            "position": "planner and reflector layer",
            "responsibilities": [
                "decompose ambiguous goals",
                "summarize intent and constraints",
                "audit stalled runs from text signals",
                "decide whether to continue, retry, or ask human when deterministic checks are inconclusive",
            ],
            "should_not_do": [
                "pick pixel coordinates",
                "override robots/throttle policy",
                "be the only authority for completion",
            ],
        },
        "vision_model": {
            "position": "per-step browser observation only after deterministic fast paths or when visual grounding is required",
            "responsibilities": [
                "ground actions in screenshot plus AX tree",
                "resolve ambiguous visible targets",
                "handle visual-only layouts and coordinate fallback after safer selectors fail",
            ],
            "should_not_do": [
                "global capability routing",
                "bulk data post-processing",
                "queue/retry/cache policy decisions",
            ],
            "recommended_use": "required" if sig.get("browser_interaction") or sig.get("visual_required") else "fallback_or_verification",
        },
        "runtime_guards": {
            "position": "around every deterministic and model-selected action",
            "responsibilities": [
                "validate action schema",
                "guard login/captcha/session-drop/loops",
                "verify extraction counts and required fields",
                "trigger fallback chain when success criteria fail",
            ],
        },
    }


def audit_model_placement(
    *,
    signals: dict[str, Any] | None = None,
    selected_tools: list[dict[str, Any]] | None = None,
    backend_plan: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sig = signals or {}
    tools = selected_tools or []
    plan = backend_plan or []
    findings = [
        {
            "area": "existing deterministic layer",
            "status": "present",
            "detail": "ActionRegistry, strategy context, RPA cache, semantic macros, pre-extract fast path, planner, reflector, loop guards, and browser action guards already exist.",
        },
        {
            "area": "visual model placement",
            "status": "appropriate_with_guardrails",
            "detail": "VLM is used for screenshot+AX grounded browser decisions, while semantic_model is already used for Planner/Reflector. This is the right split as long as deterministic routing runs first.",
        },
        {
            "area": "backend API capability exposure",
            "status": "needs_router_or_tool_bridge" if plan else "not_applicable",
            "detail": "Y1-Y28 backend APIs are powerful but should be selected by a deterministic router or explicit tool bridge, not by asking the VLM to remember every endpoint.",
        },
    ]
    if sig.get("structured") and not any(item.get("name") in {"generic_extractor", "spider_lite"} for item in plan):
        findings.append({"area": "structured extraction", "status": "warning", "detail": "Structured task did not rank extractor/spider; review task wording or strategy signals."})
    if sig.get("browser_interaction") and not tools:
        findings.append({"area": "browser actions", "status": "warning", "detail": "No ActionRegistry tool was selected for an interaction-heavy task."})
    recommendations = [
        "Run CapabilityRouter before launching a task and attach route output to run metadata/logs.",
        "Prefer deterministic fast paths for API/HTML extraction, queue control, cache replay, and feed export before spending VLM calls.",
        "Use semantic_model for intent decomposition and reflection, not for DOM coordinates or final data validation.",
        "Use vision_model only when screenshot/AX grounding is needed or deterministic selector/semantic locators fail.",
        "Keep success checks deterministic: item_count, required_fields, artifacts, HTTP status, cache hits, queue state, and guard signals.",
    ]
    return {"findings": findings, "recommendations": recommendations}


def _signals(text: str, strategy_context: dict[str, Any]) -> dict[str, Any]:
    parsed = urlparse(_first_url(text) or "")
    structured = bool(_STRUCTURED_RE.search(text) or "extract" in strategy_context.get("capabilities", []))
    crawl = bool(_CRAWL_RE.search(text))
    api = bool(_API_RE.search(text))
    full_content = bool(_FULL_CONTENT_RE.search(text))
    markdown_doc = bool(_MARKDOWN_RE.search(text))
    vscroll = bool(_VSCROLL_RE.search(text))
    form = bool(_FORM_RE.search(text) or "form" in strategy_context.get("capabilities", []))
    chat = bool(_CHAT_RE.search(text) or "chat" in strategy_context.get("capabilities", []))
    file_io = bool(_FILE_RE.search(text))
    cache = bool(_CACHE_RE.search(text))
    queue = bool(_QUEUE_RE.search(text))
    resume = bool(_RESUME_RE.search(text))
    browser_interaction = bool(_BROWSER_RE.search(text) or form or chat or file_io)
    auth = bool(_AUTH_RE.search(text))
    output_contract = strategy_context.get("output_contract") or infer_goal_output_contract(text)
    return {
        "structured": structured,
        "crawl": crawl,
        "api_or_network": api or full_content,
        "full_content_preferred": full_content,
        "markdown_preferred": markdown_doc,
        "vscroll_capture_preferred": vscroll,
        "form": form,
        "chat": chat,
        "file_io": file_io,
        "cache_or_replay": cache,
        "queue_or_ops": queue,
        "resume_preferred": resume,
        "browser_interaction": browser_interaction,
        "auth_or_captcha": auth,
        "visual_required": browser_interaction and not (api or crawl),
        "domain": parsed.netloc.split('@')[-1].split(':', 1)[0] if parsed.netloc else "",
        "output_mode": strategy_context.get("output_mode") or output_contract.get("mode") or "default",
        "artifact_required": bool(output_contract.get("artifact_required")),
        "answer_required": bool(output_contract.get("answer_required")),
    }


def _backend_plan(signals: dict[str, Any], strategy_context: dict[str, Any], selected_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    _add(plan, "run_registry", "observability", "Y1", ["GET /api/runs", "GET /api/runs/{run_id}"], "Track task lifecycle and final status.", "runtime_guards", always=True)
    if signals.get("queue_or_ops") or signals.get("structured") or signals.get("browser_interaction"):
        _add(plan, "task_queue", "orchestration", "Y7-Y17", ["GET /api/task_queue", "POST /api/task_queue/recover", "POST /api/task_queue/watchdog", "GET /api/task_queue/metrics"], "Long-running or batch tasks need pause/resume/retry/recovery/metrics around execution.", "deterministic_router")
    if signals.get("browser_interaction"):
        _add(plan, "browser_pool", "resource_guard", "Y8/Y49/Y51/Y52/Y53", ["GET /api/browser_pool"], "Browser work should be guarded by pool limits, backend status/health, cached health freshness, and runtime preflight.", "runtime_guards")
    if signals.get("api_or_network") or signals.get("structured") or signals.get("full_content_preferred"):
        _add(plan, "network_intelligence", "data_fast_path", "Y4", ["GET /api/runs/{run_id}/network"], "Prefer captured XHR/fetch JSON candidates before visual scraping when available.", "deterministic_router")
        _add(plan, "api_replay", "data_fast_path", "Y5", ["POST /api/runs/{run_id}/network/replay"], "Replay API candidates for dry-run/execute extraction before DOM/VLM fallback.", "deterministic_router")
        if signals.get("full_content_preferred"):
            _add(plan, "content_completeness_guard", "data_fast_path", "NET-6", ["dom/api completeness guard"], "When DOM shows truncated/preview text but XHR payloads are richer, replay API with session cookies.", "deterministic_router")
    if signals.get("structured"):
        _add(plan, "generic_extractor", "extraction", "Y6/Y22", ["POST /api/extractor/run", "POST /api/extractor/select"], "Use JSON/HTML table/card/selector extraction before screenshot-based extract.", "deterministic_router")
        _add(plan, "item_pipeline", "post_processing", "Y28", ["GET /api/spider/{run_id}/items"], "Validate fields, required values, dedupe, empty rows, and pagination of extracted items.", "runtime_guards")
    if signals.get("markdown_preferred"):
        _add(plan, "page_to_markdown", "extraction", "Y-FITMD", ["ActionRegistry: page_to_markdown"], "Convert the current page into denoised LLM-friendly Markdown (readability denoise + density prune + numbered link references + optional BM25 focus query) for question-answering / RAG feeds instead of full-page screenshots.", "deterministic_router")
    if signals.get("vscroll_capture_preferred"):
        _add(plan, "vscroll_capture", "extraction", "VSCROLL-ACTION", ["ActionRegistry: vscroll_capture"], "Deterministically harvest every row of a virtualised / infinite-scroll list (main document first, then same-origin child iframes) in one mid-run call - alternate row snapshots with container nudges and dedup recycled rows - instead of one VLM round per viewport.", "deterministic_router")
    if signals.get("resume_preferred"):
        _add(plan, "resume_run", "resume", "RUN-RESUME1", ["ActionRegistry: resume_run"], "Read the prior run checkpoint / resume state and continue from where the previous run left off (dedup already-captured rows, skip already-completed sub-goals) instead of restarting from scratch.", "deterministic_router")
    if signals.get("crawl") or (signals.get("structured") and signals.get("artifact_required")):
        _add(plan, "robots_throttle", "crawl_guard", "Y23", ["POST /api/robots/check", "POST /api/robots/reserve"], "Check robots/throttle before spidering or repeated domain fetches.", "runtime_guards")
        _add(plan, "spider_lite", "crawl_extract", "Y24", ["POST /api/spider/run", "GET /api/spider/{run_id}", "GET /api/spider/{run_id}/items"], "Use Spider Lite for multi-page structured extraction with selectors and item pipeline.", "deterministic_router")
    if signals.get("cache_or_replay") or signals.get("crawl"):
        _add(plan, "page_response_cache", "dev_mode", "Y25", ["GET /api/spider/page_cache/{session_id}", "GET /api/spider/page_cache/{session_id}/entries"], "Use record/replay cache for debugging, regression, and prompt tuning without repeated network calls.", "deterministic_router")
    if signals.get("artifact_required") or signals.get("file_io"):
        _add(plan, "feed_export", "artifact", "Y27", ["POST /api/spider/{run_id}/export"], "Export validated items as JSONL/JSON artifact after extraction succeeds.", "runtime_guards")
    if signals.get("browser_interaction"):
        _add(plan, "browser_backend_abstraction", "browser_runtime", "Y47/Y48/Y51/Y52", ["GET /api/browser_control/backend"], "Select and report the active local or remote browser runtime backend and cached health before opening browser sessions.", "runtime_guards")
        _add(plan, "browser_control", "browser_actions", "Y18-Y21", ["POST /api/browser_control/open", "POST /api/browser_control/snapshot", "POST /api/browser_control/find"], "Use isolated browser sessions, semantic locators, tabs, storage, cookies, and observability for dynamic tasks.", "vision_model")
        _add(plan, "action_ref_normalizer", "locator_contract", "Y46", ["GET /api/action_refs/schema", "POST /api/action_refs/normalize"], "Normalize SoM target_id, @e refs, selectors, AX roles, bboxes, and points before choosing browser or visual actions.", "deterministic_router")
        _add(plan, "selector_generator", "locator_recovery", "Y26", ["POST /api/browser_control/selector", "POST /api/browser_control/similar"], "Generate stable selectors and similar element refs when DOM shifts or visual target needs recovery.", "deterministic_router")
    if selected_tools:
        _add(plan, "action_registry_macros", "agent_tools", "legacy+Y18", [tool.get("name", "") for tool in selected_tools[:6]], "Use registered deterministic macros/tools selected from the full task goal before generic VLM browsing.", "deterministic_router")
    if signals.get("auth_or_captcha"):
        _add(plan, "human_guard", "safety", "existing", ["ask_human", "prelogin/session guards"], "Auth walls, CAPTCHA, 2FA, and stale sessions must escalate instead of blind retries.", "runtime_guards", risk="medium")
    _add(plan, "semantic_planner_reflector", "model_reasoning", "existing", ["VLMClient.make_plan", "VLMClient.reflect"], "Use text model for task decomposition and stalled-run audit, not low-level endpoint selection.", "semantic_model")
    _add(plan, "vision_agent", "visual_grounding", "existing", ["VLMClient.ask with screenshot+AX"], "Use vision model for final visual grounding when deterministic routes cannot complete safely.", "vision_model", risk="medium")
    return plan


def _fallback_chain(signals: dict[str, Any], strategy_context: dict[str, Any], backend_plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    if signals.get("api_or_network"):
        chain.extend([
            _step("network_intelligence", "Use captured API/XHR candidates if present."),
            _step("api_replay", "Replay promising API calls and validate schema/row count."),
        ])
    if signals.get("structured"):
        chain.append(_step("generic_extractor", "Try JSON/HTML/selector extraction with deterministic validation."))
    if signals.get("crawl") or (signals.get("structured") and signals.get("artifact_required")):
        chain.extend([
            _step("robots_throttle", "Reserve domain access and obey robots before repeated fetches."),
            _step("spider_lite", "Follow links/pages and extract items with cache/pipeline support."),
        ])
    if signals.get("browser_interaction"):
        chain.extend([
            _step("browser_backend_abstraction", "Check active browser backend and configured remote/stealth backend slots."),
            _step("action_registry_macros", "Try selected deterministic browser macro/tool."),
            _step("browser_control_find", "Use semantic locator or targeted probe before visual click."),
            _step("action_ref_normalizer", "Normalize @e/SoM/selector/AX/bbox references into a single action contract."),
            _step("selector_generator", "Recover stable selector or similar elements after DOM shifts."),
        ])
    if signals.get("artifact_required"):
        chain.append(_step("feed_export", "Export only after item validation passes."))
    chain.extend([
        _step("semantic_reflector", "Ask semantic model to audit failure signals and choose continue/retry/abort."),
        _step("vision_agent", "Use screenshot+AX VLM for ambiguous visual grounding."),
        _step("human_guard", "Escalate CAPTCHA/2FA/risk barriers instead of blind retry."),
    ])
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in chain:
        key = str(item.get("capability") or "")
        if key and key not in seen:
            seen.add(key)
            item["order"] = len(out) + 1
            out.append(item)
    return out


def _intent(signals: dict[str, Any], strategy_context: dict[str, Any]) -> dict[str, Any]:
    if signals.get("chat"):
        task_type = "ai_chat_workflow"
    elif signals.get("crawl"):
        task_type = "crawl_extract"
    elif signals.get("structured"):
        task_type = "structured_extraction"
    elif signals.get("form"):
        task_type = "form_or_transaction"
    elif signals.get("browser_interaction"):
        task_type = "browser_interaction"
    elif signals.get("queue_or_ops"):
        task_type = "operations"
    else:
        task_type = "general_browser_agent"
    return {
        "task_type": task_type,
        "output_mode": signals.get("output_mode") or strategy_context.get("output_mode") or "default",
        "requires_artifact": bool(signals.get("artifact_required")),
        "requires_answer": bool(signals.get("answer_required")),
        "requires_visual_grounding": bool(signals.get("visual_required")),
    }


def _add(
    plan: list[dict[str, Any]],
    name: str,
    stage: str,
    milestone: str,
    endpoints: list[str],
    reason: str,
    model_role: str,
    *,
    always: bool = False,
    risk: str = "low",
) -> None:
    if not always and any(item.get("name") == name for item in plan):
        return
    plan.append({
        "name": name,
        "stage": stage,
        "milestone": milestone,
        "endpoints_or_actions": [item for item in endpoints if item],
        "reason": reason,
        "owner": model_role,
        "deterministic": model_role != "vision_model" and model_role != "semantic_model",
        "risk": risk,
    })


def _step(capability: str, condition: str) -> dict[str, Any]:
    return {"capability": capability, "condition": condition}


def _parse_target_count(text: str) -> int | None:
    return parse_goal_target_count(text)


def _parse_target_pages(text: str) -> int | None:
    return parse_goal_target_pages(text)


def _parse_requested_fields(text: str) -> list[str]:
    return parse_goal_requested_fields(text)[:20]


def _first_url(text: str) -> str:
    m = re.search(r"https?://[^\s)\]}>\"']+", text or "")
    return m.group(0) if m else ""

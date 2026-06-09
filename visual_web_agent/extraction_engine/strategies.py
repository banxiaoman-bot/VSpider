"""Reusable extraction strategy helpers.

These helpers are intentionally pure and small so the agent loop, diagnostics,
and future skill/tool routing can share the same policy decisions.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def _append_unique(items: list[str], value: str) -> None:
    value = str(value or "").strip()
    if value and value not in items:
        items.append(value)


_ANSWER_OUTPUT_RE = re.compile(
    r"\b("
    r"tell\s+me|answer|whether|will|should|"
    r"what(?:'s|\s+is)?|when|where|why|how\s+(?:much|many|long|far|old)|"
    r"look\s+up|find\s+out|check\s+(?:if|whether)"
    r")\b"
    r"|(?:\u5e2e\u6211)?\u67e5\u4e00\u4e0b"
    r"|\u544a\u8bc9\u6211|\u56de\u7b54|\u4f1a\u4e0d\u4f1a|\u662f\u5426"
    r"|\u591a\u5c11|\u51e0\u5ea6|\u4ec0\u4e48|\u54ea\u91cc|\u54ea\u4e2a|\u51e0\u70b9",
    re.I,
)

_SAVE_ARTIFACT_RE = re.compile(
    r"\b(save|export|download|excel|xlsx|csv|spreadsheet|file)\b"
    r"|\u4fdd\u5b58|\u5bfc\u51fa|\u4e0b\u8f7d|\u5199\u5165|\u5b58\u5165|"
    r"\u843d\u76d8|\u7ed3\u679c\u6587\u4ef6",
    re.I,
)

_MEDIA_ARTIFACT_RE = re.compile(
    r"\b(image|photo|picture|img|jpg|jpeg|png|gif|webp|thumbnail|video|mp4|mkv|webm|audio|mp3|wav|podcast|voice|sound|speech|pdf|report|archive|zip|rar|7z|tar\.gz|tgz)\b"
    r"|\u56fe\u7247|\u56fe\u50cf|\u7167\u7247|\u622a\u56fe|\u89c6\u9891|\u5f55\u50cf|\u76f4\u64ad|\u97f3\u9891|\u5f55\u97f3|\u8bed\u97f3|\u62a5\u544a|\u767d\u76ae\u4e66|\u538b\u7f29\u5305|\u6253\u5305",
    re.I,
)

_STRUCTURED_ARTIFACT_RE = re.compile(
    r"\b(extract|scrape|crawl|collect|dataset|records?|rows?|fields?|columns?|"
    r"table|list|bulk|pagination)\b"
    r"|\u63d0\u53d6|\u6293\u53d6|\u91c7\u96c6|\u722c\u53d6|\u62bd\u53d6|"
    r"\u8868\u683c|\u5217\u8868|\u5b57\u6bb5|\u6570\u636e|\u6279\u91cf|"
    r"\u5168\u91cf|\u5168\u90e8|\u6240\u6709|"
    r"\u524d\s*\d+\s*(?:\u6761|\u4e2a|\u9879|\u7bc7|\u5219|\u884c)",
    re.I,
)


def infer_goal_output_contract(
    goal: str,
    *,
    target_count: int | None = None,
    target_pages: int | None = None,
    requested_fields: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Infer whether the user primarily wants an answer or a saved dataset."""
    text = str(goal or "")
    save_artifact = bool(_SAVE_ARTIFACT_RE.search(text))
    structured_artifact = bool(_STRUCTURED_ARTIFACT_RE.search(text)) or bool(
        _MEDIA_ARTIFACT_RE.search(text)
    )
    if requested_fields:
        structured_artifact = True
    if target_count is not None and target_count > 0 and structured_artifact:
        structured_artifact = True
    if target_pages is not None and target_pages > 0:
        structured_artifact = True

    answer_required = bool(_ANSWER_OUTPUT_RE.search(text))
    artifact_required = save_artifact or structured_artifact
    if answer_required and artifact_required:
        mode = "mixed"
    elif artifact_required:
        mode = "artifact"
    elif answer_required:
        mode = "answer"
    else:
        mode = "default"

    reasons: list[str] = []
    if answer_required:
        reasons.append("goal_requests_answer")
    if save_artifact:
        reasons.append("goal_requests_saved_artifact")
    if structured_artifact:
        reasons.append("goal_requests_structured_rows")

    return {
        "mode": mode,
        "answer_required": answer_required,
        "artifact_required": artifact_required,
        "save_artifact": save_artifact,
        "structured_rows": structured_artifact,
        "reasons": reasons,
    }


def infer_goal_output_mode(
    goal: str,
    *,
    target_count: int | None = None,
    target_pages: int | None = None,
    requested_fields: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Return the high-level output mode for callers that only need a string."""
    return str(
        infer_goal_output_contract(
            goal,
            target_count=target_count,
            target_pages=target_pages,
            requested_fields=requested_fields,
        ).get("mode")
        or "default"
    )


def infer_goal_strategy_context(
    goal: str,
    *,
    url: str = "",
    target_count: int | None = None,
    target_pages: int | None = None,
    requested_fields: list[str] | tuple[str, ...] | None = None,
    data_shape: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Infer generic execution strategy hints from the task surface.

    This is deliberately advisory. It should never be the only gate for running
    a skill or fast path; concrete skill ``match()`` and runtime verification
    still decide whether an action is actually safe.
    """
    output_contract = infer_goal_output_contract(
        goal,
        target_count=target_count,
        target_pages=target_pages,
        requested_fields=requested_fields,
    )
    output_mode = str(output_contract.get("mode") or "default")
    text = f"{goal or ''} {url or ''}".lower()
    capabilities: list[str] = []
    preferred_actions: list[str] = []
    preferred_modes: list[str] = []
    reasons: list[str] = list(output_contract.get("reasons") or [])

    def add(capability: str, reason: str, *, action: str = "", mode: str = "") -> None:
        _append_unique(capabilities, capability)
        _append_unique(reasons, reason)
        if action:
            _append_unique(preferred_actions, action)
        if mode:
            _append_unique(preferred_modes, mode)

    extraction_markers = (
        "extract", "scrape", "collect", "table", "list", "feed", "article",
        "weather", "forecast", "rows", "records", "data", "excel",
        "提取", "抓取", "采集", "爬取", "表格", "列表", "天气", "预报", "数据",
        "\u63d0\u53d6", "\u6293\u53d6", "\u91c7\u96c6", "\u722c\u53d6",
        "\u8868\u683c", "\u5217\u8868", "\u5929\u6c14", "\u9884\u62a5",
        "\u6570\u636e",
    )
    if any(marker in text for marker in extraction_markers):
        if output_mode == "answer":
            add("extract", "goal_requests_target_answer", action="extract", mode="answer_first")
        else:
            add("extract", "goal_requests_structured_extraction", action="extract", mode="extract_fast_path")
        if target_count is not None and target_count > 0:
            _append_unique(reasons, "goal_has_row_target")
        if requested_fields:
            _append_unique(reasons, "goal_has_requested_fields")
    if target_pages is not None and target_pages > 0:
        add("extract", "goal_has_page_target", action="next_page", mode="extract_fast_path")

    chat_like = bool(
        re.search(
            r"\b(chat|chatgpt|claude|kimi|deepseek|gemini|copilot|ai\s*(?:answer|reply|response))\b"
            r"|文心|一言|豆包|通义|元宝|智谱|助手|聊天|对话|AI\s*回答|ai返回|返回的内容|回复|提问|发送",
            text,
            re.I,
        )
    )
    if chat_like:
        add("chat", "goal_mentions_ai_chat_workflow", action="chat_submit", mode="targeted_first")
        _append_unique(preferred_actions, "chat_extract")

    form_like = bool(
        re.search(r"\b(form|submit|register)\b|表单|提交|注册", text, re.I)
        or re.search(
            r"\b(fill|type|enter)\b.{0,40}\b(input|field|textbox|textarea)\b"
            r"|\b(input|field|textbox|textarea)\b.{0,40}\b(fill|type|enter)\b",
            text,
            re.I,
        )
        or re.search(r"填写|填入|输入.{0,12}(框|字段|内容|值)", text, re.I)
    )
    if form_like:
        add("form", "goal_mentions_form_or_input", action="auto_form_fill", mode="targeted_first")
    if re.search(r"\b(search input|find input|find button|find link|locator|probe)\b|局部感知|探针|查找", text, re.I):
        add("perception", "goal_mentions_local_element_probe", action="targeted_probe", mode="targeted_first")

    if re.search(r"\bmodal|dialog|popup\b|弹窗|对话框", text, re.I):
        add("dialog", "goal_mentions_dialog_workflow", action="modal_dialog_macro", mode="macro_first")
    if re.search(r"\bhover|tooltip|avatar|view profile|dropdown\b|悬停|悬浮|提示框|头像|下拉", text, re.I):
        add("hover", "goal_mentions_hover_or_overlay", action="hover_and_click", mode="targeted_first")
    if re.search(r"\bslider|滑块\b", text, re.I):
        add("slider", "goal_mentions_slider", action="demoqa_slider_macro", mode="macro_first")
    if re.search(r"\bdrag|drop|droppable|drag me|drop here\b|拖拽|拖动", text, re.I):
        add("drag_drop", "goal_mentions_drag_drop", action="demoqa_droppable_slider_macro", mode="macro_first")
    if re.search(r"\bshadow\s*dom|iframe\b|shadow root|影子|内嵌框架", text, re.I):
        add("shadow_dom", "goal_mentions_shadow_or_iframe", action="selectorshub_shadow_iframe_macro", mode="macro_first")
    if re.search(r"\bnew\s*tab|tab\b|新标签|新开标签", text, re.I):
        add("new_tab", "goal_mentions_new_tab_workflow", action="wikipedia_new_tab_macro", mode="macro_first")
    if re.search(r"\bdocs?|sidebar|navigation|upgrading|api/components\b|文档|侧边栏|导航", text, re.I):
        add("navigation", "goal_mentions_docs_navigation", action="reactrouter_docs_macro", mode="macro_first")
    if re.search(r"\b(upload|choose file|select file|input file)\b|上传|选择文件|选取文件", text, re.I):
        add("file_upload", "goal_mentions_file_upload", action="upload", mode="targeted_first")
    if re.search(r"\b(download|samplefile|save file)\b|下载|保存文件", text, re.I):
        add("file_download", "goal_mentions_file_download", action="file_download", mode="targeted_first")
    if re.search(r"\b404\b|\bhttp\s*error\b|\bstatus\s*code\b|错误|终止|状态码", text, re.I):
        add("http_error", "goal_mentions_http_error_handling", action="http_error_guard", mode="guard_first")

    if data_shape_exposes_target_candidate(data_shape, target_count):
        if output_mode == "answer":
            add("extract", "page_shape_exposes_answer_candidate", action="extract", mode="answer_first")
        else:
            add("extract", "page_shape_exposes_target_rows", action="extract", mode="extract_fast_path")

    if not preferred_modes:
        _append_unique(preferred_modes, "vlm_global_fallback")

    fallback_order: list[str] = []
    if "macro_first" in preferred_modes:
        fallback_order.extend(["skill_macro", "targeted_probe", "vlm_global"])
    elif "extract_fast_path" in preferred_modes:
        fallback_order.extend(["pre_extract", "targeted_probe", "vlm_global"])
    elif "answer_first" in preferred_modes:
        fallback_order.extend(["targeted_probe", "vlm_global"])
    elif "targeted_first" in preferred_modes:
        fallback_order.extend(["targeted_probe", "skill_macro", "vlm_global"])
    elif "guard_first" in preferred_modes:
        fallback_order.extend(["guard", "vlm_global"])
    else:
        fallback_order.append("vlm_global")

    return {
        "capabilities": capabilities,
        "preferred_actions": preferred_actions,
        "preferred_modes": preferred_modes,
        "fallback_order": list(dict.fromkeys(fallback_order)),
        "target_count": target_count,
        "target_pages": target_pages,
        "requested_fields": list(requested_fields or []),
        "output_mode": output_mode,
        "output_contract": output_contract,
        "reasons": reasons,
    }


def pre_extract_candidate_family_rank(name: str) -> int:
    """Lower rank wins for pre-VLM deterministic extraction handoff."""
    source = str(name or "").upper()
    if "DOM_TABLE" in source:
        return 0
    if "DOM_CARDS" in source:
        return 1
    if "DOM_LIST" in source:
        return 2
    if "FULL_PAGE" in source or "AX_TREE" in source or "INNER_TEXT" in source:
        return 3
    return 9


def choose_pre_extract_reached_candidate(
    candidates: list[dict[str, Any]],
    target_count: int,
) -> dict[str, Any] | None:
    """Choose the preferred extraction family that already satisfies the target."""
    if target_count <= 0:
        return None
    reached = [
        candidate
        for candidate in candidates
        if int(candidate.get("accepted") or 0) >= target_count
    ]
    if not reached:
        return None
    return sorted(
        reached,
        key=lambda candidate: (
            pre_extract_candidate_family_rank(str(candidate.get("name") or "")),
            -float(candidate.get("score") or 0),
            -int(candidate.get("accepted") or 0),
        ),
    )[0]


def url_without_fragment(url: str) -> str:
    try:
        parsed = urlsplit(str(url or "").strip())
    except Exception:
        return str(url or "").split("#", 1)[0]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def same_document_url(current_url: str, target_url: str) -> bool:
    if not target_url:
        return False
    return url_without_fragment(current_url) == url_without_fragment(target_url)


def data_shape_exposes_target_candidate(
    data_shape: dict[str, Any] | None,
    target_count: int | None,
) -> bool:
    """Whether current page appears to expose enough rows/cards for the target."""
    if target_count is None or target_count <= 0:
        return False
    shape = data_shape or {}
    try:
        table_rows = int(shape.get("table_rows") or 0)
        table_cells = int(shape.get("table_cells") or 0)
        repeated_items = int(shape.get("repeated_list_items") or 0)
        repeated_classes = int(shape.get("repeated_class_count") or 0)
    except (TypeError, ValueError):
        return False
    exposed = 0
    if table_cells >= 2:
        exposed = max(exposed, table_rows)
    exposed = max(exposed, repeated_items, repeated_classes)
    return exposed >= target_count


def click_target_is_same_page_extract_nav(
    inspection: dict[str, Any] | None,
    current_url: str,
) -> bool:
    """Identify tabs/self-links that should yield to extraction on data pages."""
    data = inspection or {}
    href = str(data.get("href") or "")
    role = str(data.get("role") or "").lower()
    tag = str(data.get("tag") or "").lower()
    text = str(data.get("text") or "").lower()
    nav_like = bool(data.get("nav_like"))
    tab_like = bool(data.get("tab_like")) or role == "tab"
    if href and same_document_url(current_url, href):
        return True
    if tab_like and not re.search(r"\b(next|prev|previous|page)\b|下一页|上一页|翻页", text):
        return True
    if nav_like and tag in {"a", "button", "li", "span", "div"} and re.search(
        r"7\s*(?:day|days)|3\s*(?:day|days)|forecast|weather|7日|7天|三天|预报|天气",
        text,
        re.I,
    ):
        return True
    return False


def summarize_universal_strategies(
    *,
    extracts: list[dict[str, Any]],
    guards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize generic engine strategies, independent of site-specific cases."""
    pre_extracts = [
        item
        for item in extracts
        if (item.get("metadata") or {}).get("mode") == "pre_extract_fast_path"
    ]
    same_page_nav_guards = [
        item
        for item in guards
        if str(item.get("name") or "").lower() == "extract_same_page_nav_guard"
    ]
    batch_gates = [
        item for item in guards if str(item.get("name") or "") == "BATCH_POST_EXTRACT_GATE"
    ]
    extract_click_guards = [
        item
        for item in guards
        if str(item.get("name") or "").lower() == "extract_click_guard"
    ]
    sources = [str(item.get("source") or "") for item in extracts if item.get("source")]
    source_families = sorted(
        {source.split(":", 1)[0] for source in sources if source}
    )
    last_pre = pre_extracts[-1] if pre_extracts else {}
    last_pre_meta = last_pre.get("metadata") if isinstance(last_pre, dict) else {}
    if not isinstance(last_pre_meta, dict):
        last_pre_meta = {}
    return {
        "pre_extract": {
            "triggered": bool(pre_extracts),
            "count": len(pre_extracts),
            "last_source": last_pre.get("source", "") if last_pre else "",
            "last_rows": last_pre.get("rows") if last_pre else None,
            "last_target": last_pre_meta.get("target"),
            "last_snapshot_path": last_pre_meta.get("snapshot_path", ""),
        },
        "same_page_nav_guard": {
            "triggered": bool(same_page_nav_guards),
            "count": len(same_page_nav_guards),
        },
        "batch_post_extract_gate": {
            "triggered": bool(batch_gates),
            "count": len(batch_gates),
        },
        "extract_click_guard": {
            "triggered": bool(extract_click_guards),
            "count": len(extract_click_guards),
        },
        "dom_cards": {
            "used": any(source.startswith("DOM_CARDS") for source in sources),
            "count": sum(1 for source in sources if source.startswith("DOM_CARDS")),
        },
        "full_page_or_ax": {
            "used": any(
                source.startswith("FULL_PAGE")
                or "AX_TREE" in source
                or "INNER_TEXT" in source
                for source in sources
            ),
            "count": sum(
                1
                for source in sources
                if source.startswith("FULL_PAGE")
                or "AX_TREE" in source
                or "INNER_TEXT" in source
            ),
        },
        "source_families": source_families,
    }

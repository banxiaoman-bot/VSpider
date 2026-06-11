"""Lightweight action/tool registry for deterministic agent capabilities.

The registry is intentionally metadata-first. Existing handlers can be bound
without moving their implementation out of ``main.py`` immediately, which keeps
the migration small and reversible.
"""

from __future__ import annotations

from inspect import isawaitable
from dataclasses import asdict, dataclass, field
import re
from typing import Any, Callable


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _goal_token_matches(text: str, token: Any) -> bool:
    key = _norm(token)
    if not key or not text:
        return False
    if re.search(r"[\u4e00-\u9fff]", key):
        return key in text
    if re.fullmatch(r"[a-z0-9][a-z0-9 _-]*", key):
        pattern = rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return key in text


@dataclass
class ActionTool:
    """Metadata and optional callable for one deterministic capability."""

    name: str
    capability: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    deterministic: bool = True
    changes_state: bool = True
    enabled: bool = True
    risk: str = "low"
    handler: Callable[..., Any] | None = field(default=None, repr=False, compare=False)

    def bind(self, handler: Callable[..., Any] | None) -> "ActionTool":
        self.handler = handler
        return self

    @property
    def handler_bound(self) -> bool:
        return callable(self.handler)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("handler", None)
        data["handler_bound"] = self.handler_bound
        return data

    def score_goal(self, goal: str) -> int:
        text = _norm(goal)
        if not text or not self.enabled:
            return 0
        score = 0
        for token in (self.name, *self.aliases, *self.tags):
            key = _norm(token)
            if key and _goal_token_matches(text, key):
                score += 2 if key == _norm(self.name) else 1
        return score

    def score_strategy(self, strategy_context: dict[str, Any] | None) -> tuple[int, list[str]]:
        strategy = strategy_context or {}
        if not self.enabled or not isinstance(strategy, dict):
            return 0, []
        score = 0
        reasons: list[str] = []
        capabilities = {_norm(item) for item in strategy.get("capabilities") or []}
        actions = {_norm(item) for item in strategy.get("preferred_actions") or []}
        modes = {_norm(item) for item in strategy.get("preferred_modes") or []}
        capability = _norm(self.capability)
        if capability and capability in capabilities:
            score += 6
            reasons.append(f"strategy_capability:{self.capability}")
        tool_actions = {_norm(item) for item in (self.name, *self.actions)}
        action_hits = sorted(item for item in tool_actions if item and item in actions)
        if action_hits:
            score += 8
            reasons.append(f"strategy_action:{action_hits[0]}")
        if "macro_first" in modes and any(_norm(item).endswith("_macro") for item in tool_actions):
            score += 2
            reasons.append("strategy_mode:macro_first")
        if "extract_fast_path" in modes and capability == "extract":
            score += 4
            reasons.append("strategy_mode:extract_fast_path")
        if "targeted_first" in modes and self.name == "targeted_probe":
            score += 4
            reasons.append("strategy_mode:targeted_first")
        return score, reasons

    def matches_action(self, action: str) -> bool:
        key = _norm(action)
        if not key or not self.enabled:
            return False
        return key == _norm(self.name) or key in {_norm(item) for item in self.actions}


class ActionRegistry:
    """Small registry for deterministic actions and macro tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ActionTool] = {}

    def register(self, tool: ActionTool) -> ActionTool:
        key = _norm(tool.name)
        if not key:
            raise ValueError("ActionTool.name is required")
        if key in self._tools:
            raise ValueError(f"ActionTool already registered: {tool.name}")
        self._tools[key] = tool
        return tool

    def bind(self, name: str, handler: Callable[..., Any] | None) -> ActionTool:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"Unknown action tool: {name}")
        return tool.bind(handler)

    def get(self, name: str) -> ActionTool | None:
        return self._tools.get(_norm(name))

    def list_tools(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        tools = [
            tool
            for tool in self._tools.values()
            if include_disabled or tool.enabled
        ]
        return [tool.to_dict() for tool in sorted(tools, key=lambda t: t.name)]

    def select_for_goal(
        self,
        goal: str,
        *,
        limit: int = 8,
        strategy_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        ranked: list[tuple[int, ActionTool, list[str]]] = []
        for tool in self._tools.values():
            goal_score = tool.score_goal(goal)
            strategy_score, strategy_reasons = tool.score_strategy(strategy_context)
            score = goal_score + strategy_score
            if score > 0:
                ranked.append((score, tool, strategy_reasons))
        ranked.sort(key=lambda item: (-item[0], item[1].name))
        selected: list[dict[str, Any]] = []
        for score, tool, strategy_reasons in ranked[: max(1, int(limit or 1))]:
            data = tool.to_dict()
            data["match_score"] = score
            if strategy_reasons:
                data["strategy_reasons"] = strategy_reasons
            selected.append(data)
        return selected

    def get_for_action(
        self,
        action: str,
        *,
        goal: str = "",
    ) -> ActionTool | None:
        key = _norm(action)
        if not key:
            return None
        exact_matches = [
            tool for tool in self._tools.values()
            if tool.enabled and _norm(tool.name) == key
        ]
        if exact_matches:
            return exact_matches[0]
        matches = [tool for tool in self._tools.values() if tool.matches_action(action)]
        if not matches:
            return None
        if len(matches) == 1:
            tool = matches[0]
            return tool if tool.score_goal(goal) > 0 else None
        goal_scores = [(tool.score_goal(goal), tool) for tool in matches]
        goal_scores.sort(key=lambda item: (-item[0], item[1].name))
        best_score, best_tool = goal_scores[0]
        if best_score <= 0:
            return None
        return best_tool

    def resolve_for_action(
        self,
        action: str,
        *,
        goal: str = "",
    ) -> dict[str, Any] | None:
        tool = self.get_for_action(action, goal=goal)
        return tool.to_dict() if tool else None

    async def execute_for_action(
        self,
        action: str,
        *,
        goal: str = "",
        **kwargs: Any,
    ) -> Any:
        tool = self.get_for_action(action, goal=goal)
        if tool is None:
            raise KeyError(f"No registered tool for action: {action}")
        if not callable(tool.handler):
            raise RuntimeError(f"Registered tool has no bound handler: {tool.name}")
        result = tool.handler(**kwargs)
        if isawaitable(result):
            return await result
        return result


def build_default_action_registry() -> ActionRegistry:
    """Register VSpider's current first-class deterministic capabilities."""

    registry = ActionRegistry()
    register = registry.register

    register(ActionTool(
        name="targeted_probe",
        capability="perception",
        description="Collect compact intent-matched element candidates without full-page SoM/AX.",
        actions=("targeted_probe",),
        aliases=(
            "targeted",
            "probe",
            "local perception",
            "find input",
            "find button",
            "find link",
            "search input",
            "locator",
            "局部感知",
            "探针",
            "查找输入框",
            "查找按钮",
            "查找链接",
        ),
        tags=("input", "button", "link", "table", "dialog", "iframe", "shadow_dom"),
        evidence=("candidates", "selector", "bbox", "confidence"),
        deterministic=True,
        changes_state=False,
        risk="low",
    ))
    register(ActionTool(
        name="auto_form_fill",
        capability="form",
        description="Fill scoped web forms with label/control binding and readback.",
        actions=("form_set", "auto_form"),
        aliases=(
            "form",
            "form_set",
            "submit",
            "create",
            "student registration",
            "basic form",
            "填写",
            "填报",
            "填入",
            "表单",
        ),
        tags=("input", "select", "radio", "checkbox", "textarea", "autocomplete"),
        evidence=("value_readbacks", "form_validation"),
    ))
    register(ActionTool(
        name="round_form_challenge",
        capability="form",
        description="Complete repeated shuffled-label form rounds until final state.",
        actions=("auto_form_macro",),
        aliases=(
            "rpa challenge",
            "rpachallenge",
            "rpachallenge.com",
            "https://rpachallenge.com",
            "round",
            "challenge.xlsx",
        ),
        tags=("label_binding", "multi_round", "submit"),
        evidence=("round", "row_index", "final_completion"),
    ))
    register(ActionTool(
        name="date_pick",
        capability="date_picker",
        description="Select a target date in calendar/date-picker widgets.",
        actions=("date_pick", "semantic_verify"),
        aliases=("pick a date", "pick a day", "calendar", "下个月", "日期"),
        tags=("component", "relative_date"),
        evidence=("expected_date", "observed_value"),
    ))
    register(ActionTool(
        name="cascader_pick",
        capability="cascader",
        description="Pick a path through multi-level cascader menus.",
        actions=("cascader_pick", "semantic_verify"),
        aliases=("cascader", "级联", "guide", "navigation"),
        tags=("component", "multi_level_menu"),
        evidence=("path", "observed_value"),
    ))
    register(ActionTool(
        name="hover_and_click",
        capability="hover",
        description="Hover a trigger, wait for overlay menu, then click an item.",
        actions=("hover_and_click",),
        aliases=("dropdown", "hover trigger", "menu", "action"),
        tags=("overlay", "menu_click"),
        evidence=("clicked_text", "overlay_text"),
    ))
    register(ActionTool(
        name="tooltip_extract",
        capability="tooltip",
        description="Hover tooltip triggers and extract displayed tooltip text.",
        actions=("hover",),
        aliases=("tooltip", "提示框", "top", "bottom", "left", "right"),
        tags=("hover", "extract"),
        evidence=("trigger", "tooltip_text"),
        changes_state=False,
    ))
    register(ActionTool(
        name="modal_dialog_macro",
        capability="dialog",
        description="Open requested modal/dialog buttons, extract dialog text, then close them.",
        actions=("modal_dialog_macro",),
        aliases=("modal", "dialog", "small modal", "large modal", "popup"),
        tags=("interaction", "extract", "close"),
        evidence=("trigger", "title", "content", "close_result"),
        changes_state=True,
    ))
    register(ActionTool(
        name="reactrouter_docs_macro",
        capability="navigation",
        description="Navigate React Router docs sidebar targets and extract main headings.",
        actions=("reactrouter_docs_macro",),
        aliases=("reactrouter.com", "react router", "upgrading from v6", "api/components", "sidebar", "docs"),
        tags=("docs", "sidebar", "extract"),
        evidence=("step", "title", "url"),
        changes_state=True,
    ))
    register(ActionTool(
        name="internet_hovers_macro",
        capability="hover",
        description="Hover a requested avatar, extract revealed profile text, and follow its profile link.",
        actions=("internet_hovers_macro",),
        aliases=("the-internet.herokuapp.com/hovers", "hover", "view profile", "avatar", "头像"),
        tags=("hidden_text", "profile", "navigation"),
        evidence=("username", "profile_link_text", "final_url", "final_title_or_heading"),
        changes_state=True,
    ))
    register(ActionTool(
        name="demoqa_droppable_slider_macro",
        capability="drag_drop",
        description="Complete DemoQA droppable and slider probes with verified readback.",
        actions=("demoqa_droppable_slider_macro",),
        aliases=("demoqa.com/droppable", "droppable", "drag me", "drop here", "slider"),
        tags=("drag_drop", "slider", "readback"),
        evidence=("droppable_text", "slider_value"),
        changes_state=True,
    ))
    register(ActionTool(
        name="demoqa_slider_macro",
        capability="slider",
        description="Set DemoQA slider to a target value and read back the displayed input value.",
        actions=("demoqa_slider_macro",),
        aliases=("demoqa.com/slider", "slider", "滑块", "80"),
        tags=("drag", "precision", "readback"),
        evidence=("slider_value",),
        changes_state=True,
    ))
    register(ActionTool(
        name="selectorshub_shadow_iframe_macro",
        capability="shadow_dom",
        description="Fill a Shadow DOM input, filter an iframe table, select a row, and extract cells.",
        actions=("selectorshub_shadow_iframe_macro",),
        aliases=("selectorshub.com/xpath-practice-page", "shadow dom", "iframe and table", "pizza", "Search:"),
        tags=("shadow_dom", "iframe", "table_filter", "checkbox"),
        evidence=("pizza_name", "city", "country"),
        changes_state=True,
    ))
    register(ActionTool(
        name="wikipedia_new_tab_macro",
        capability="new_tab",
        description="Open requested Wikipedia links in new tabs, extract first paragraphs, and close them.",
        actions=("wikipedia_new_tab_macro",),
        aliases=("wikipedia.org/wiki/Web_scraping", "data mining", "artificial intelligence", "new tab"),
        tags=("tab_switch", "extract", "close_tab"),
        evidence=("link_text", "target_title", "first_paragraph"),
        changes_state=True,
    ))
    register(ActionTool(
        name="file_upload",
        capability="file_upload",
        description="Upload a preconfigured local file through an input[type=file] control.",
        actions=("upload",),
        aliases=("upload", "choose file", "select file", "上传", "选择文件", "选取文件"),
        tags=("file", "input", "local_file"),
        evidence=("uploaded_file", "page_echo"),
        changes_state=True,
    ))
    register(ActionTool(
        name="file_download",
        capability="file_download",
        description="Click a download control and verify that a file is saved by the browser download interceptor.",
        actions=("click", "download_image"),
        aliases=("download", "下载", "sampleFile", "sampleFile.jpeg"),
        tags=("file", "artifact", "download"),
        evidence=("download_path", "suggested_filename"),
        changes_state=True,
    ))
    register(ActionTool(
        name="media_harvester",
        capability="media_harvester",
        description=(
            "Bulk-download media references (images / video / audio / pdf / "
            "archive / generic files) discovered on the page into "
            "runs/<id>/artifacts/, sha256-deduplicated, with one manifest "
            "entry per item. Triggered when output_contract.output_kind "
            "starts with media_*."
        ),
        actions=("media_harvest", "harvest_media", "download_all_media"),
        aliases=(
            "media",
            "media_harvester",
            "harvest",
            "download all images",
            "download all videos",
            "download all pdfs",
            "save images",
            "save videos",
            "save pdfs",
            "save all media",
            "下载图片",
            "下载视频",
            "下载音频",
            "下载 PDF",
            "下载所有图片",
            "下载所有视频",
            "下载所有 pdf",
            "批量下载",
            "媒体下载",
            "媒体采集",
        ),
        tags=(
            "media", "image", "video", "audio", "pdf", "archive",
            "download", "manifest", "sha256", "artifact",
        ),
        evidence=(
            "candidates",
            "downloaded_count",
            "failed_count",
            "manifest_appended",
            "artifact_paths",
        ),
        deterministic=True,
        changes_state=False,
        risk="low",
    ))
    register(ActionTool(
        name="http_error_guard",
        capability="http_error",
        description="Detect HTTP error pages such as 404 and end gracefully when the goal asks for error handling.",
        aliases=("404", "http error", "status", "错误", "终止"),
        tags=("guard", "network", "done"),
        evidence=("status_code", "url"),
        changes_state=False,
    ))
    register(ActionTool(
        name="table_extract",
        capability="extract",
        description="Extract structured rows from visible tables.",
        actions=("extract",),
        aliases=(
            "table",
            "datatable",
            "employee",
            "office",
            "position",
            "表格",
            "数据表",
            "员工",
        ),
        tags=("pagination", "excel"),
        evidence=("rows", "output_file", "fields"),
        changes_state=False,
    ))
    register(ActionTool(
        name="list_extract",
        capability="extract",
        description="Extract repeated list/card/search-result rows.",
        actions=("extract",),
        aliases=("抓取", "提取", "采集", "爬取", "top250", "hn.algolia", "AI Agent"),
        tags=("list", "pagination", "excel"),
        evidence=("rows", "output_file", "fields"),
        changes_state=False,
    ))
    register(ActionTool(
        name="vscroll_capture",
        capability="extract",
        description=(
            "Deterministically harvest every row of a virtualised / "
            "infinite-scroll list (react-window, vue-virtual-scroller, "
            "ag-grid, Element Plus virtual tables) by alternating row "
            "snapshots with container nudges, deduplicating recycled rows "
            "by text. Sweeps the main document first, then same-origin "
            "child iframes. type_value is an optional row cap. Writes a "
            "dataset_rows jsonl artifact + manifest entry and stores rows "
            "in memory. Replaces ~1 VLM round per viewport with a single "
            "mid-run call."
        ),
        actions=("vscroll_capture",),
        aliases=(
            "virtual list",
            "virtual scroll",
            "virtualized list",
            "infinite scroll",
            "react-window",
            "vue-virtual-scroller",
            "ag-grid",
            "虚拟列表",
            "虚拟滚动",
            "无限滚动",
            "滚动加载",
            "全量采集",
            "滚动采集",
        ),
        tags=("extract", "list", "virtual-scroll", "deterministic"),
        evidence=("row_count", "passes", "complete", "container", "output_path"),
        deterministic=True,
        changes_state=False,
    ))
    register(ActionTool(
        name="page_to_markdown",
        capability="extract",
        description=(
            "Convert the current page into denoised, LLM-friendly Markdown: "
            "strips nav/header/footer/aside/script/ads, density-prunes link "
            "farms, maps headings/lists, and numbers links into a reference "
            "section. type_value is an optional BM25 focus query that keeps "
            "only relevant blocks. Writes a markdown_doc artifact + manifest "
            "and stores the text in memory. Prefer this over a full-page "
            "screenshot for question-answering / RAG feeds (far fewer tokens)."
        ),
        actions=("page_to_markdown",),
        aliases=(
            "markdown",
            "to markdown",
            "readable",
            "reader mode",
            "clean text",
            "main content",
            "正文",
            "正文提取",
            "转markdown",
            "转 markdown",
            "网页转md",
            "可读正文",
            "喂给大模型",
            "RAG",
        ),
        tags=("extract", "markdown", "readability", "rag", "deterministic"),
        evidence=("output_path", "word_count", "link_count", "source_url"),
        deterministic=True,
        changes_state=False,
    ))
    register(ActionTool(
        name="resume_run",
        capability="resume",
        description=(
            "Read back where a prior / interrupted run left off (turn, completed "
            "steps, rows already captured) from the run checkpoint or the resume "
            "state the loop published, and surface it into memory so the agent "
            "continues toward the remaining goal instead of restarting from "
            "scratch. Already-captured rows are deduped on a resumed run. "
            "Deterministic and read-only (no page mutation). Use when the user "
            "asks to 续跑 / 断点续跑 / 接着上次 / continue the last run."
        ),
        actions=("resume_run",),
        aliases=(
            "resume",
            "resume run",
            "resume last",
            "continue last",
            "continue previous",
            "pick up where",
            "续跑",
            "断点续跑",
            "接着上次",
            "继续上次",
            "上次没做完",
            "接着之前",
            "继续之前",
        ),
        tags=("resume", "checkpoint", "control", "deterministic"),
        evidence=("resumed", "from_turn", "completed_steps", "item_count"),
        deterministic=True,
        changes_state=False,
    ))
    register(ActionTool(
        name="chat_extract",
        capability="extract",
        description=(
            "Deterministic AI-chat answer extractor. Waits for streaming, "
            "probes known answer-block selectors, falls back to largest "
            "text block. Use on yiyan.baidu.com / chat.baidu.com / "
            "chat.openai.com / claude.ai / tongyi.aliyun.com / kimi / 豆包 "
            "after submitting the user question."
        ),
        actions=("chat_extract",),
        aliases=(
            "chat",
            "ai answer",
            "chat answer",
            "聊天",
            "对话",
            "AI 回答",
            "AI回答",
            "回答",
            "提问",
            "介绍一下",
            "解释一下",
            "chatgpt",
            "claude",
            "文心",
            "通义",
            "豆包",
            "kimi",
            "deepseek",
            "智谱",
            "元宝",
            "perplexity",
            "gemini",
            "copilot",
        ),
        tags=("chat", "ai", "answer", "streaming", "deterministic"),
        evidence=("answer", "method", "selector", "wait_ms"),
        deterministic=True,
        changes_state=False,
    ))
    register(ActionTool(
        name="chat_submit",
        capability="click",
        description=(
            "Click the send button on AI-chat pages even when SoM misses it. "
            "Uses chat_send_locator heuristic (CSS selector cascade + textbox-"
            "anchored proximity) to find the send button's viewport coordinates "
            "and clicks via real mouse. Use after typing the message on yiyan/"
            "chat.baidu.com/chat.openai.com/claude.ai/etc. when the send "
            "button is an icon <div> (no SoM number) and press_key Enter "
            "is not firing the submit handler."
        ),
        actions=("chat_submit",),
        aliases=(
            "send",
            "submit chat",
            "click send",
            "发送",
            "提交聊天",
            "发送消息",
            "点击发送",
            "send button",
            "submit message",
        ),
        tags=("chat", "ai", "submit", "send", "deterministic"),
        evidence=("method", "selector", "click_point_x", "click_point_y"),
        deterministic=True,
        changes_state=True,
    ))
    register(ActionTool(
        name="fetch_links_batch",
        capability="extract",
        description="Fetch one or many visible links in background tabs and store {url,title,content}.",
        actions=("fetch_links_batch", "fetch_link_content"),
        aliases=(
            "fetch",
            "fetch_link",
            "fetch links",
            "link content",
            "article content",
            "batch links",
            "每个链接",
            "每条结果",
            "前几条",
            "抓取链接",
            "批量抓取",
        ),
        tags=("link", "content", "article", "background", "ax", "selectors"),
        evidence=("url", "title", "content", "memory_key"),
        deterministic=True,
        changes_state=False,
    ))
    register(ActionTool(
        name="next_page",
        capability="pagination",
        description="Advance paginated table/list pages with progress guards.",
        actions=("next_page",),
        aliases=("next", "下一页", "翻页"),
        tags=("pagination", "extract"),
        evidence=("before_url", "after_url", "row_count"),
    ))
    register(ActionTool(
        name="xhr_extract",
        capability="extract",
        description="Capture structured API/XHR records when pure extraction permits it.",
        actions=("xhr_extract",),
        aliases=("xhr", "api", "network"),
        tags=("interceptor", "excel"),
        evidence=("rows", "output_file", "source_url"),
        changes_state=False,
    ))
    register(ActionTool(
        name="visual_coordinate_fallback",
        capability="visual_fallback",
        description="Coordinate-first fallback for non-DOM targets after semantic paths fail.",
        aliases=("coordinate", "screenshot", "visual"),
        tags=("fallback", "som"),
        evidence=("point", "screenshot_path"),
        enabled=False,
        risk="medium",
    ))
    return registry

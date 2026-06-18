"""
VSpider 系统提示词构建器

负责动态组装 System Prompt 和 User Message。
静态模板在 prompt_templates.py，关键词/检测函数在 prompt_helpers.py。
"""

import os
import re

try:
    from .prompt_templates import SYSTEM_PROMPT, FULL_SYSTEM_PROMPT
except ImportError:
    from prompt_templates import SYSTEM_PROMPT, FULL_SYSTEM_PROMPT

try:
    from .prompt_helpers import *  # noqa: F401,F403
except ImportError:
    from prompt_helpers import *  # noqa: F401,F403

try:
    from .prompt_skills import SKILL_PROMPTS, STATIC_PROMPT_PARTS
except ImportError:
    from prompt_skills import SKILL_PROMPTS, STATIC_PROMPT_PARTS

try:
    from .extraction_engine.strategies import infer_goal_output_contract
except ImportError:
    from extraction_engine.strategies import infer_goal_output_contract


def build_system_prompt(
    goal: str = "",
    browser_state: str = "",
    workflow_memory: dict | None = None,
    *,
    force_full: bool = False,
) -> str:
    """
    Build the per-step system prompt from stable core blocks plus dynamic skills.

    Stable blocks are always first for local prefix-cache friendliness. Dynamic,
    frequently changing skills are appended after the stable prefix. Goal, browser
    state and AX Tree stay in the user message, not the system prefix.
    """
    if force_full or os.getenv("VSPIDER_USE_LEGACY_PROMPT", "").lower() in {"1", "true", "yes"}:
        return FULL_SYSTEM_PROMPT

    haystack = f"{goal}\n{browser_state}".lower()
    login_surface = _looks_like_login_surface(browser_state)
    skills: list[str] = []

    bulk_extract = _looks_like_bulk_extract(goal)

    if _text_has_any(haystack, _EXTRACT_TRIGGERS):
        skills.extend(["extract", "download"])
    if bulk_extract:
        skills.extend(["extract", "bulk_extract", "download"])
    if _text_has_any(haystack, _FORM_TRIGGERS):
        skills.append("form")
    if login_surface or _text_has_any(haystack, _LOGIN_TRIGGERS):
        skills.append("login")
    if login_surface or _text_has_any(haystack, _CREDENTIAL_TRIGGERS):
        skills.append("credential")
    if _text_has_any(haystack, _HITL_TRIGGERS):
        skills.append("hitl")
    if workflow_memory or _text_has_any(haystack, _MEMORY_TRIGGERS):
        skills.append("memory")
    if _text_has_any(haystack, _MULTI_TAB_TRIGGERS):
        skills.append("multi_tab")
    if _text_has_any(haystack, _HOVER_MENU_TRIGGERS):
        skills.append("hover_menu")
    if _text_has_any(haystack, _CASCADER_TRIGGERS):
        skills.append("cascader")
    # Row action: the system-injected `row_action` handler needs FORM_SKILL's
    # verification rules ("read row.value to confirm") + CONFIRM_DIALOG_SKILL
    # to handle the post-click modal. Inject all three together so the prompt
    # is self-contained.
    if _text_has_any(haystack, _ROW_ACTION_TRIGGERS):
        if "form" not in skills:
            skills.append("form")
        skills.append("row_action")
        if "confirm_dialog" not in skills:
            skills.append("confirm_dialog")
    if _text_has_any(haystack, _CONFIRM_DIALOG_TRIGGERS):
        if "confirm_dialog" not in skills:
            skills.append("confirm_dialog")
    if _text_has_any(haystack, _TREE_TRIGGERS):
        skills.append("tree")
    if _text_has_any(haystack, _STEPPER_TRIGGERS):
        # Stepper goals are by definition form goals, pull FORM too.
        if "form" not in skills:
            skills.append("form")
        skills.append("stepper")
    if _text_has_any(haystack, _RELATIVE_DATE_TRIGGERS) or _resolver_matches(goal):
        # Pull in FORM first if it wasn't already, so the date-picker block
        # has the broader "calendar value verification" rules to lean on.
        if "form" not in skills:
            skills.append("form")
        skills.append("relative_date")
    if _text_has_any(haystack, _FEED_AD_FILTER_TRIGGERS):
        skills.append("feed_ad_filter")
    if (
        _text_has_any(haystack, _DATA_EXPORT_TRIGGERS)
        or _url_matches_data_export_registry(browser_state)
    ):
        skills.append("data_export")
    if _text_has_any(haystack, _TOOLTIP_TRIGGERS):
        skills.append("tooltip")
    if _text_has_any(haystack, _SEMANTIC_TRIGGERS):
        skills.append("semantic")
    if _text_has_any(haystack, _CHAT_ENTRY_TRIGGERS):
        skills.append("chat_entry")
    if _text_has_any(haystack, _PAGE_TO_MARKDOWN_TRIGGERS):
        skills.append("page_to_markdown")
    if _text_has_any(haystack, _VSCROLL_CAPTURE_TRIGGERS):
        skills.append("vscroll_capture")
    if _text_has_any(haystack, _SNAPSHOT_TRIGGERS):
        skills.append("snapshot")
    if _text_has_any(haystack, _SEARCH_NAV_TRIGGERS):
        skills.append("search_nav")
    if _text_has_any(haystack, _RESUME_RUN_TRIGGERS):
        skills.append("resume_run")
    if _text_has_any(haystack, ("示例", "范例", "few-shot", "few shot")):
        skills.append("few_shot")

    # Remove duplicates while preserving trigger order.
    deduped_skills = list(dict.fromkeys(skills))
    parts = list(STATIC_PROMPT_PARTS)
    parts.extend(SKILL_PROMPTS[name] for name in deduped_skills if name in SKILL_PROMPTS)
    return "\n\n".join(part for part in parts if part).strip()


def build_user_message(
    goal: str,
    step: int,
    max_steps: int,
    history: str = "",
    input_descriptions: str = "",
    workflow_memory: dict | None = None,
    task_plan: "object | None" = None,
    capability_route: dict | None = None,
) -> str:
    """
    构建发送给 VLM 的用户消息文本部分。

    Args:
        goal: 用户的任务目标描述
        step: 当前步骤编号
        max_steps: 最大步骤数
        history: 最近操作历史摘要文本
        input_descriptions: 当前页面中输入框的详细信息描述
        workflow_memory: 跨页面记忆库，非空时注入提示让 VLM 知道可用的变量
        task_plan: Wave 2 任务计划对象（TaskPlan），非空时插入计划摘要与当前子目标

    Returns:
        格式化后的用户消息文本
    """
    parts = [
        f"🎯 ## 你的终极目标（Task）\n{goal}\n",
        f"## 进度\n当前是第 {step} 步，最多执行 {max_steps} 步。\n",
    ]
    try:
        _output_contract = infer_goal_output_contract(goal)
    except Exception:
        _output_contract = {}
    _output_mode = str(_output_contract.get("mode") or "default")
    if _output_mode == "answer":
        parts.append(
            "## Output intent\n"
            "The user is asking for a concise answer, not a dataset export. "
            "When the visible page already contains the answer, finish with action=done. "
            "If you must use extract, extract only the target answer facts; do not save "
            "generic search-result lists, recommendation chips, navigation text, or unrelated rows.\n"
        )
    elif _output_mode == "artifact":
        parts.append(
            "## Output intent\n"
            "The user is asking for structured data or a saved artifact. Use extract/download/export "
            "when the target rows or file are visible, and keep rows aligned to the requested fields. "
            "Prefer native export, network/API replay, or a structured extractor over visual row reading "
            "when available. Finish with action=done only after the action/history shows an artifact path, "
            "download_completed, manifest item, or equivalent output evidence for this run; do not invent "
            "filenames or treat a textual summary as the saved artifact.\n"
        )
    elif _output_mode == "mixed":
        parts.append(
            "## Output intent\n"
            "The user wants both an answer and a saved/structured result. Capture the requested facts "
            "cleanly and avoid unrelated page lists or search-result noise. If a saved result is required, "
            "finish only after artifact/download/manifest evidence exists for this run.\n"
        )
    # ── 历史骑脸前置：把"已完成的操作 + 结果"紧贴在终极目标下方 ─────────────
    # 旧位置（AX Tree 之后）被长文本稀释，VLM 容易忽略；
    # 新位置 + 双层 ===== 分隔符用版式权重压制后续所有视觉描述，
    # 配合 VSpiderAction.progress_review 字段形成"强制自我反思"闭环。
    if history:
        parts.append(
            "=========================================================\n"
            "🛑 【全局历史与进度复盘】（决策前必读！填写 progress_review 字段时必须引用）\n"
            f"{history}\n"
            "=========================================================\n"
        )
    route_section = _format_capability_route_guidance(capability_route)
    if route_section:
        parts.append(route_section)
    # ── Wave 2：任务计划骑脸注入（紧贴历史之下）──────────────────────────
    # 让 VLM 每步都看到"整体计划 + 当前子目标 + 退出标准"，治跨步战略盲视。
    if task_plan is not None and getattr(task_plan, "sub_goals", None):
        _cur = task_plan.current
        _total = len(task_plan.sub_goals)
        _cur_idx = task_plan.current_idx
        _lines = [f"📋 【全局任务计划】(进度 {_cur_idx + 1}/{_total})"]
        for sg in task_plan.sub_goals:
            if sg.status == "done":
                _lines.append(f"   ✅ {sg.id}. {sg.description}")
            elif sg.status == "active":
                _lines.append(f"   ▶ {sg.id}. {sg.description}   ← 当前子目标")
                _lines.append(f"       退出标准: {sg.exit_criteria}")
            elif sg.status == "failed":
                _lines.append(f"   ❌ {sg.id}. {sg.description}")
            else:
                _lines.append(f"   ⏳ {sg.id}. {sg.description}")
        if _cur is not None:
            _lines.append(
                f"\n【本步要求】只聚焦当前子目标 「{_cur.description}」。"
                f"完成该子目标（满足退出标准）时把 subgoal_status 设为 \"completed\"，"
                f"系统会自动推进计划；否则保持 \"in_progress\"。"
                f"若当前是最后一个子目标且已完成，同时把 action 设为 done。"
            )
        parts.append("\n".join(_lines) + "\n")
    # ── 记忆库注入：让 VLM 知道当前手里有哪些跨页面保存的数据 ──────────────
    if workflow_memory:
        mem_lines = [f"## 当前跨页面记忆库（可在 type 动作中用 {{{{key}}}} 引用）"]
        for k, v in workflow_memory.items():
            mem_lines.append(f"- `{k}` = \"{v}\"")
        mem_lines.append(
            "（如需使用以上值，在 type_value 中写 `{{变量名}}`，底层自动替换为真实值）"
        )
        parts.append("\n".join(mem_lines) + "\n")
    if input_descriptions:
        parts.append(input_descriptions)
    parts.append(
        f"## 截图说明（重要）\n"
        f"截图中每个可交互元素上叠加了**黑底白字的红框序号**（如 ①②③...22 23 24...）。\n"
        f"这些序号是操作用的 ID，**不是页面的实际内容**。\n"
        f"执行 extract 时，必须读取**红框内部或红框旁边的真实文字/数字**，"
        f"绝对不能把红框上的序号当作数据（如热度、排名、价格等）写入 extracted_data。\n"
    )
    parts.append(
        f"## 请求\n"
        f"请仔细观察上方的网页截图（已标注红框和数字序号），"
        f"分析当前页面状态，决定下一步操作。\n"
        f"只输出 JSON，不要输出其他内容。"
    )
    return "\n".join(parts)


def _format_capability_route_guidance(capability_route: dict | None) -> str:
    if not isinstance(capability_route, dict) or not capability_route:
        return ""
    intent = capability_route.get("intent") or {}
    backend_plan = capability_route.get("backend_plan") or []
    fallback_chain = capability_route.get("fallback_chain") or []
    selected_tools = capability_route.get("selected_agent_tools") or []
    model_roles = capability_route.get("model_roles") or {}
    task_type = str(intent.get("task_type") or "").strip()
    output_mode = str(intent.get("output_mode") or "").strip()
    plan_names = [
        str(item.get("name") or "").strip()
        for item in backend_plan
        if isinstance(item, dict) and item.get("name")
    ][:8]
    fallback_names = [
        str(item.get("capability") or "").strip()
        for item in fallback_chain
        if isinstance(item, dict) and item.get("capability")
    ][:8]
    tool_names = [
        str(item.get("name") or "").strip()
        for item in selected_tools
        if isinstance(item, dict) and item.get("name")
    ][:8]
    vision_role = model_roles.get("vision_model") or {}
    semantic_role = model_roles.get("semantic_model") or {}
    lines = ["## Capability Route Guidance（系统路由建议，决策前必读）"]
    if task_type or output_mode:
        lines.append(f"- 任务类型: {task_type or 'unknown'}；输出模式: {output_mode or 'default'}")
    if plan_names:
        lines.append("- 推荐优先能力: " + " → ".join(plan_names))
    if fallback_names:
        lines.append("- 兜底顺序: " + " → ".join(fallback_names))
    if tool_names:
        lines.append("- 可用确定性 Agent 工具: " + ", ".join(tool_names))
    lines.append(
        "- 约束: 优先使用已注册的确定性动作/宏/抽取能力；不要凭空发明新 action；"
        "当推荐能力与当前页面状态冲突时，以当前截图和 AX Tree 为准。"
    )
    if semantic_role:
        lines.append("- 语义模型职责: 理解目标、拆解计划、审计卡住原因；不要替代运行时校验。")
    if vision_role:
        recommended = str(vision_role.get("recommended_use") or "fallback_or_verification")
        lines.append(
            "- 视觉模型职责: 只负责当前截图+AX 的可视化定位与歧义消解；"
            f"本任务视觉使用建议: {recommended}。"
        )
    return "\n".join(lines) + "\n"


# ════════════════════════════════════════════════════════════════
#  Wave 2 — Planner / Reflector prompt builders
# ════════════════════════════════════════════════════════════════

def build_plan_prompts(
    goal: str,
    initial_url: str,
    workflow_memory: dict | None = None,
) -> tuple[str, str]:
    """生成 Planner LLM 的 (system, user) prompts。"""
    system = (
        "你是一个资深的网页自动化任务规划师。你的唯一工作：把用户的自然语言 goal 拆成 "
        "3-6 个**可独立验证**的有序子目标，供下游 Web Agent 顺序执行。\n\n"
        "拆分规则：\n"
        "1. 每个子目标必须同时包含 description（要做什么）与 exit_criteria（怎么算完成，"
        "   用可观察的页面状态描述，如「搜索结果页已加载，可见 >=3 条结果」或「数据已写入 Excel」）\n"
        "2. 粒度控制在 3-6 个：太少（1-2 个）= 无法切换战术；太多（>6）= 过度拆分拖慢执行\n"
        "3. 按执行顺序编号 id=1,2,3...；列表中第一个子目标由系统自动置为 active\n"
        "4. 若 goal 含「先 X 后 Y 忽略 X 的错误」这种容错描述，X 仍要单独列一个子目标，"
        "   exit_criteria 写「尝试 X 即可，无论成败后续都继续执行」\n"
        "5. 最后一个子目标的 exit_criteria 通常是「完成 goal 全部要求，准备输出 done」\n"
        "5b. 📦 提取类任务粒度收敛：若 goal 包含「提取 / 采集 / 抓取 / 下载数据 / 写入 Excel / 保存数据」"
        "   等关键词，且目标页面是**单一数据列表页**（豆瓣 Top250 / 热搜榜 / 搜索结果前 N 条等），"
        "   子目标**严格限制在 1-2 个**：\n"
        "     - 子目标 1：到达包含目标数据的页面（exit_criteria 是页面元素可见，如「榜单首项已加载」）\n"
        "     - 子目标 2（可选）：提取 N 条结构化数据（exit_criteria 是「extract 动作已输出完整 JSON」）\n"
        "   **禁止**把「保存为 Excel」「落盘」「写入文件」拆成独立子目标——系统在 extract 成功时自动落盘，"
        "   拆成独立子目标会造成 VLM 提取成功后反复尝试「再提取一次」以推进子目标计数器，浪费多步。\n"
        "5c. 🧾 表单填报任务粒度：若 goal 包含「填写 / 填报 / 表单 / form」等关键词，"
        "   禁止把「让整个表单完整出现在同一屏」作为子目标或退出标准。长表单应拆成按字段顺序填写的子目标，"
        "   exit_criteria 使用「某字段已显示指定值 / 某选项已选中 / 最终 Create 或 Submit 已点击」这类可观察状态。"
        "   如果首屏已看见目标字段（如 Activity name），第一个子目标必须是填写当前可见字段，而不是继续向下滚动。\n"
        "6. 🔒 **登录处理原则：被动响应，不主动探测**（CRITICAL — 适用于所有任务类型）\n"
        "   核心理念：**能用就用，不能用才喊人**。绝大多数站点（包括聊天页、电商、"
        "   工具页）在未登录态都有大量功能可用；主循环的运行时 guard（PRELOGIN 检测、"
        "   CHAT DRIFT GUARD、ask_human 兜底）会在真正撞到登录墙时介入。在 Planner "
        "   层凭空塞「探测登录」子目标，只会让 VLM 把第一步浪费在点登录按钮上。\n"
        "   规则：\n"
        "   (a) **只有当 goal 文本里明确出现**「登录」「先登录」「帮我登录」「log in」"
        "       「sign in」等祈使动词，**或**给出了凭证占位符（如 `{{phone}}` / "
        "       `{{password}}` / `{{verify_code}}`），才把「完成登录」列为第一个子目标，"
        "       其 exit_criteria 写「URL 已离开 /login，页面进入业务态」。\n"
        "   (b) goal **没明说**登录、也没给凭证 → **绝对不要**添加「探测登录 / "
        "       检查登录态 / 打开登录页」类子目标，哪怕你觉得"
        "       「这站点似乎需要登录才能用」。直接按 goal 字面动作拆分，第一个子目标"
        "       就是 goal 要做的第一件事（输入框输入 / 搜索 / 点击列表项 / 提取数据 等）。\n"
        "   (c) 即使没有凭证、运行时却撞上了登录墙，主循环会自动 `ask_human` 让"
        "       用户人工介入；Planner 不需要在计划层重复防御。\n"
        "6b. 📝 **goal 已经把要做的事描述完整时**（例如「打开页面，在输入框中输入 X，"
        "    回车后获取回答」「提取列表前 10 条」「点击下一页直到末页」），子目标必须"
        "    严格贴合 goal 字面动作，不要凭空插入『检查登录』『关闭弹窗』『确认页面加载』"
        "    『验证元素可见』等 goal 没要求的探测步骤 —— 主循环的运行时 guard 会处理"
        "    这些非业务态。Planner 的职责是「翻译用户意图为有序步骤」，不是「写一份"
        "    防御性 SOP」。\n\n"
        "输出要求：\n"
        "- 只输出 JSON，不要任何 markdown、不要解释文字\n"
        "- JSON 结构：{\"goal\": str, \"sub_goals\": [{id, description, exit_criteria, status}], "
        "\"current_idx\": 0}\n"
        "- 所有子目标的 status 填 \"pending\"，current_idx 填 0（系统会把第一个改为 active）\n\n"
        "示例 —— goal=\"搜索 Claude AI，点击第一个结果在新标签打开，再切回搜索页\"：\n"
        "{\n"
        '  "goal": "搜索 Claude AI，点击第一个结果在新标签打开，再切回搜索页",\n'
        '  "sub_goals": [\n'
        '    {"id":1,"description":"在搜索框输入 Claude AI 并提交","exit_criteria":"搜索结果页已加载，URL 含 q=Claude","status":"pending"},\n'
        '    {"id":2,"description":"中键点击第一条搜索结果链接","exit_criteria":"新标签页已打开并加载目标站点","status":"pending"},\n'
        '    {"id":3,"description":"切回原搜索结果页","exit_criteria":"当前活动标签回到搜索结果页","status":"pending"}\n'
        '  ],\n'
        '  "current_idx": 0\n'
        "}"
    )
    mem_block = ""
    if workflow_memory:
        mem_block = f"\n\n当前已有跨页面记忆变量：{list(workflow_memory.keys())}"
    user = (
        f"用户 goal：{goal}\n"
        f"起始 URL：{initial_url}"
        f"{mem_block}\n\n"
        f"请按上述规则输出 TaskPlan JSON。"
    )
    return system, user


def build_reflect_prompts(
    task_plan: "object",
    history_summary: str,
    signals: list[str],
    current_url: str,
) -> tuple[str, str]:
    """生成 Reflector LLM 的 (system, user) prompts。"""
    system = (
        "你是一个 Web Agent 的监督员（Reflector）。主 Agent 在执行任务时遇到了异常信号，"
        "请你结合 **任务计划 + 最近历史 + 当前现状**，给出一个决策：\n\n"
        "- continue：计划无误，Agent 只是暂时遇阻，无需干预，让它自己再试一步\n"
        "- advance：当前子目标的退出标准实际已达成，但 Agent 忘了推进，强制跳到 advance_to_idx\n"
        "- revise：计划本身有误（如起始页不对、漏了关键步骤），给出完整的 new_sub_goals 覆盖\n"
        "- abort：任务不可完成（如要求的页面不存在、要登录但没凭据），给出 abort_verdict=fail 终结\n\n"
        "决策原则：\n"
        "1. 优先 continue（最保守）；只有明确证据时才 advance / revise / abort\n"
        "2. advance 必须同时给 advance_to_idx（0-based 索引）\n"
        "3. revise 必须给完整的 new_sub_goals（列表，不是补丁），沿用 SubGoal schema\n"
        "4. 仅输出 JSON，不要 markdown、不要解释"
    )

    # 计划摘要
    plan_lines = [f"当前任务计划（goal={task_plan.goal}）："]
    for sg in task_plan.sub_goals:
        _mark = {"done": "[✅完成]", "active": "[▶ 当前]", "pending": "[⏳待做]", "failed": "[❌失败]"}.get(sg.status, "[?]")
        plan_lines.append(
            f"  {_mark} {sg.id}. {sg.description}（退出标准：{sg.exit_criteria}）"
        )
    plan_text = "\n".join(plan_lines)

    signals_text = "\n".join(f"  - {s}" for s in signals) if signals else "  (无)"

    user = (
        f"{plan_text}\n\n"
        f"触发 Reflector 的异常信号：\n{signals_text}\n\n"
        f"最近操作历史：\n{history_summary or '  (尚无历史)'}\n\n"
        f"当前 URL：{current_url}\n\n"
        f"请输出 ReflectorDecision JSON。"
    )
    return system, user

"""
VSpider VLM 结构化模型定义（从 vlm_client.py 拆出）

包含任务规划与反思审计相关的 Pydantic 模型：
- SubGoal / TaskPlan：任务分解与进度追踪
- ReflectorDecision：Reflector 审计决策
"""

from typing import Any, ClassVar, Dict, Literal, List, Optional, Union

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

import re
import logging


class SubGoal(BaseModel):
    """单个可独立验证的子目标。"""
    id: int = Field(..., description="子目标序号，从 1 开始")
    description: str = Field(..., description="子目标简述（如 '搜索 python playwright'）")
    exit_criteria: str = Field(
        ...,
        description="退出标准，一句话描述如何判断本子目标完成（如 '搜索结果页加载，可见 >=3 条结果'）"
    )
    status: Literal["pending", "active", "done", "failed"] = Field(
        default="pending",
        description="子目标状态。任务起手时首个为 active，其余 pending"
    )


class TaskPlan(BaseModel):
    """任务计划 = goal + 有序子目标列表。"""
    goal: str = Field(..., description="用户原始目标")
    sub_goals: List[SubGoal] = Field(..., description="3-6 个按序执行的子目标")
    current_idx: int = Field(default=0, description="当前活动子目标索引")

    @property
    def current(self) -> Optional[SubGoal]:
        if 0 <= self.current_idx < len(self.sub_goals):
            return self.sub_goals[self.current_idx]
        return None

    def advance(self) -> bool:
        """推进到下一子目标，返回 True；已是最后一个返回 False（仅标 done 不推进）。"""
        if self.current_idx < len(self.sub_goals) - 1:
            self.sub_goals[self.current_idx].status = "done"
            self.current_idx += 1
            self.sub_goals[self.current_idx].status = "active"
            return True
        self.sub_goals[self.current_idx].status = "done"
        return False

    def summary(self) -> str:
        """一行进度概览，用于 prompt 注入。"""
        icons = {"done": "✅", "active": "▶", "pending": "⏳", "failed": "❌"}
        return "  ".join(
            f"{icons.get(s.status, '·')} {s.id}. {s.description[:24]}"
            for s in self.sub_goals
        )


class ReflectorDecision(BaseModel):
    """Reflector 审计后的决策。"""
    decision: Literal["continue", "advance", "revise", "abort"] = Field(
        ...,
        description=(
            "审计结论："
            "continue=计划无误仅暂时遇阻；"
            "advance=当前子目标实际已完成强推进；"
            "revise=计划有误，给出新子目标列表；"
            "abort=不可完成，终结任务"
        ),
    )
    reason: str = Field(..., description="决策理由，简述现状与结论")
    advance_to_idx: Optional[int] = Field(
        default=None,
        description="advance 专用，推进到的目标索引（0-based）"
    )
    new_sub_goals: Optional[List[SubGoal]] = Field(
        default=None,
        description="revise 专用，完全覆盖的新子目标列表"
    )
    abort_verdict: Optional[Literal["success", "fail"]] = Field(
        default=None,
        description="abort 专用，最终裁决"
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_reflector_payload(cls, data):
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if not str(payload.get("reason") or "").strip():
            payload["reason"] = (
                payload.get("thought")
                or payload.get("summary")
                or payload.get("explanation")
                or "Reflector returned no reason."
            )

        status_map = {
            "todo": "pending",
            "not_started": "pending",
            "in_progress": "active",
            "current": "active",
            "completed": "done",
            "complete": "done",
            "success": "done",
            "error": "failed",
        }
        allowed = {"pending", "active", "done", "failed"}
        goals = payload.get("new_sub_goals")
        if isinstance(goals, list):
            normalized = []
            for idx, goal in enumerate(goals, start=1):
                if not isinstance(goal, dict):
                    normalized.append(goal)
                    continue
                item = dict(goal)
                item.setdefault("id", idx)
                if not str(item.get("exit_criteria") or "").strip():
                    item["exit_criteria"] = (
                        item.get("description") or "Complete this sub-goal."
                    )
                status = str(item.get("status") or "pending").strip().lower()
                item["status"] = status_map.get(
                    status,
                    status if status in allowed else "pending",
                )
                normalized.append(item)
            payload["new_sub_goals"] = normalized
        return payload


TASK_PLAN_SCHEMA: dict = TaskPlan.model_json_schema()
REFLECTOR_DECISION_SCHEMA: dict = ReflectorDecision.model_json_schema()

ERROR_DECISION = {
    "thought": "VLM 请求失败或返回格式异常",
    "action": "error",
    "target_id": 0,
    "type_value": "",
    "memory_key": "",
    "status": "error",
}

ERROR_DECISION_LIST: list = [dict(ERROR_DECISION)]



# Logger for VSpiderAction validators
logger = logging.getLogger("vspider.vlm")


class VSpiderAction(BaseModel):
    """VLM 返回的结构化操作决策。所有字段均有默认值以兼容不完整的 VLM 响应。"""

    # ★★★ progress_review 必须排在所有字段绝对第一位（Structured Outputs 会按此顺序生成）
    # 用"强制自我反思 (Forced Reflection)" 压制 VLM 的视觉脑惯性：
    # 不允许它一上来就看截图选 target_id，必须先对照历史复盘进度。
    progress_review: str = Field(
        default="",
        description=(
            "【绝对强制字段 · 第一顺位】在看当前截图之前，先对照【🛑 全局历史与进度复盘】"
            "和【当前任务】，明确列出：①✅ 已达成的子目标（标注在哪一步完成、结果 URL / 标签变化）；"
            "②⏳ 剩余未完成的子目标。如果所有子目标均已达成（例如「点击结果新标签 + 切回原页」均已在"
            "历史中显示 ✅），**必须在此字段写出『✅ 任务已完成，进入 done』**，且下方 action 只能填 done。"
            "禁止复制粘贴任务原文，必须按已观察到的历史事实做结论。"
        ),
    )
    # ★ thought 第二位：progress_review 给出进度结论后，再基于当前截图推理下一步
    thought: str = Field(
        default="",
        description=(
            "在 progress_review 结论之后，结合当前截图和 AX Tree 描述你的推理过程。"
            "如果 progress_review 已判定任务完成，此处只需重复『任务完成，输出 done』，"
            "不要再分析视觉元素。"
        ),
    )
    current_state: str = Field(default="", description="当前页面状态描述")
    action: Literal[
        "click", "type", "hover", "scroll", "select",
        "press_key", "goto", "upload",
        "extract", "extract_link", "download_image",
        "close_tab", "switch_tab", "save_to_memory", "done", "ask_human", "error",
        "captcha_detected",
        "click_point",    # 无选择器坐标点击：直接用像素坐标操控鼠标，跳过 SoM ID 定位
        "targeted_probe", # 局部元素探针：按目标查找 input/button/link/table/dialog 候选，不改变页面状态
        "click_new_tab",  # 中键点击：强制在新标签页打开链接，避免 click+switch_tab 循环
        "fetch_link_content",  # JS-driven 后台拉取：context.new_page+goto+evaluate+close，免 VLM 进环
        "fetch_links_batch",   # 批量后台拉取：type_value JSON {target_ids/urls, mode, selectors}
        "smooth_scroll",  # 平滑滚动：behavior:'smooth' 模拟人类滚轮，更易触发懒加载
        "remove_element", # 物理铲除：从 DOM 树直接删除广告遮罩/悬浮弹窗等阻挡节点
        "wait",           # 显式等待：主动暂停 N 秒，应对长动画/慢加载中间态
        "drag_and_drop",  # 拖拽：将 target_id 元素拖到 type_value 指定 ID 的元素上
        "find_text",      # 滚动定位文本/字段标签：target_id=0, type_value=可见文字
        "form_set",       # 按字段标签设置表单控件：target_id=0, type_value="label=value"
        "next_page",      # 启发式翻页：底层尝试 [Next/下一页/›/→] 等通用 locator
        "click_text",     # 文本定位点击：底层 page.get_by_text(type_value) 绕开 SoM ID 填位
        "hover_and_click",# 复合悬浮+菜单项点击（target_id=hover触发器, type_value=菜单项文字）
        "row_action",     # 按行筛选定位行内按钮：target_id=0, type_value="<行筛选文本>||<按钮文字>" 如 "张三||删除"
        "extract_row",   # 行值取用：按行筛选 + 列标题抓单值或整行，写回 memory
        "tree_check",    # 树控件复选框：按节点文本定位 + check / uncheck / toggle
        "set_prompt_response", # 预装填下一次原生 prompt() 的回答（一次性）
        "chat_extract",   # 通用 AI 聊天回答提取：等待流式完成 + 选择器级联 + 兜底大文本块，避开"搜索-然后-回答"页面的搜索结果干扰
        "chat_submit",    # 通用 AI 聊天发送按钮点击：当 SoM 漏标了图标式发送按钮（<div>+SVG）或 press_key Enter 无效时使用；底层用启发式 locator 直接点击
        "page_to_markdown", # 整页 HTML→去噪 LLM 友好 Markdown：type_value=可选聚焦 query，落 markdown_doc 产物 + 写回 memory
        "resume_run",     # 断点续跑：读取上次 run_checkpoint / resume 状态写回 memory，据此继续而非从头重来
        "vscroll_capture", # 虚拟列表一键全量采集：交替收行+推进容器滚动并按行文本去重直到触底；type_value=可选行数上限，落 dataset_rows jsonl + 写回 memory
        "html_snapshot",  # 整页 HTML 快照落盘并登记 manifest：type_value=可选文件名；路径写回 memory
        "screenshot",     # 截图落盘并登记 manifest：type_value="full" 整页截图，缺省视口；路径写回 memory
        "open_top_search_result", # 搜索结果页：确定性打开首个非广告有机结果并导航（落地二次广告校验 + 候选轮替）
    ] = Field(..., description="要执行的动作类型")
    target_id: int = Field(
        default=0,
        description=(
            "目标元素的 SoM ID（= AX Tree 中 @eN 的数字部分）。"
            "若你看到 @e5 [button] \"Submit\"，操作它就填 5。"
            "允许直接写 @e5 字符串（系统会自动解析）。"
        ),
    )
    type_value: str = Field(default="", description="输入框内容（支持 {{key}} 插值）或按键名称")
    memory_key: str = Field(
        ...,  # 绝对必填，不设默认值，强制生成引擎输出此字段
        description=(
            "【绝对必填字段】无论执行什么 action，都必须输出此字段！"
            "如果是 click、scroll 等不需要保存记忆的动作，请填入空字符串 \"\"；"
            "如果是 save_to_memory，必须填写一个英文变量名（如 'icp_number'、'local_ip'）。"
        ),
    )
    extracted_data: Optional[Union[Dict[str, Any], List[Any], str]] = Field(
        default=None,
        description=(
            "⚠️ 极度重要：当 action 为 'extract' 时，此字段【绝对不能为 null】！"
            "你必须仔细观察截图和 AX Tree，将目标数据整理为结构化 JSON 填入此字段。"
            "示例：{\"title\": \"黑神话悟空\", \"views\": \"3500万\"} 或列表 [{...}, {...}]。"
            "如果你输出 null，系统会直接拒绝并浪费一步！请参考 Few-Shot 范例 E。"
        ),
    )
    # 无选择器坐标定位专用字段：[x, y] 千分制归一化坐标（0-1000），仅 click_point 动作使用
    # 后端会自动将归一化坐标换算为当前视口的真实像素坐标再点击
    point: Optional[List[int]] = Field(default=None, description="千分制归一化坐标 [x, y]（0-1000），仅 click_point 动作使用")
    status: str = Field(default="", description="状态信息（如 captcha_detected）")
    # Wave 2：子目标自宣告完成标志。VLM 每步在 action 选定后自评：
    #   本步动作是否让【当前子目标】达到 exit_criteria？是 → "completed"；否 → "in_progress"
    # 主循环在 VSpiderAction 返回后检查此字段，若 completed 且非末子目标则推进 _task_plan.current_idx。
    subgoal_status: Literal["in_progress", "completed"] = Field(
        default="in_progress",
        description=(
            "当前子目标状态。若本步动作达成 task_plan.current 的 exit_criteria，"
            "设为 'completed'（系统会自动推进到下一子目标）；否则保持 'in_progress'。"
            "末尾子目标完成时应同时把 action 设为 done。"
        ),
    )

    @field_validator("target_id", mode="before")
    @classmethod
    def _coerce_target_id(cls, v: Any) -> int:
        # 支持 @eN 别名（Wave 3 语义快照）：VLM 可能回显 "@e5" / "e5" / "@E5"
        # 或带空格 "@e 5"；structured-output 强制 int 时 VLM 通常直接回数字。
        # 畸形字符串（"@efoo"/"@e"/""/"foo"）**必须抛 ValueError** → Pydantic
        # ValidationError → _validate_decision 自愈反馈路径，让 VLM 下一步修正，
        # 而不是静默吞成 0 再被 click+0→wait 机制二次降级（掩盖真实输入）。
        if isinstance(v, str):
            raw = v.strip()
            if not raw:
                raise ValueError(
                    "target_id 为空字符串。请给出具体数字 ID（如 3）或 @eN 别名（如 @e5）。"
                )
            s = raw.lstrip("@").lstrip()
            if s.lower().startswith("e"):
                s = s[1:].strip()
            try:
                return int(s)
            except (TypeError, ValueError):
                raise ValueError(
                    f"target_id={raw!r} 无法解析。允许格式：整数（3）、@eN（@e5）、eN（e5）。"
                    "若当前页面没有合适目标，请改用 smooth_scroll / press_key / wait，不要编造 ID。"
                )
        # 非字符串分支保留旧行为：None / 布尔 / 其它 → 0（default 语义）
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    @field_validator("type_value", mode="before")
    @classmethod
    def _coerce_type_value(cls, v: Any) -> str:
        return str(v) if v is not None else ""

    @field_validator("memory_key", mode="before")
    @classmethod
    def _coerce_memory_key(cls, v: Any) -> str:
        """将 null/None 统一转为空字符串，避免 model_validator 收到 None。"""
        return str(v).strip() if v is not None else ""

    # ── 凭证捏造黑名单（Fix 1：CRITICAL 安全红线）──
    # 仅对 action="type" 生效；action="press_key"/"goto"/"extract" 的 type_value
    # 语义完全不同（按键名 / URL / 数据），不做此校验。
    # 命中任一模式 → raise ValueError → Pydantic ValidationError → 自愈反馈。
    _FABRICATED_CREDENTIAL_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        # 中国大陆手机号（严格 11 位 1x 开头）
        re.compile(r"^1[3-9]\d{9}$"),
        # 弱密码 / 占位密码
        re.compile(r"^(password|passwd|pwd|pass)\d*$", re.IGNORECASE),
        re.compile(r"^(123456|1234567|12345678|123456789|111111|000000|888888|666666)$"),
        re.compile(r"^(qwerty|qwerty123|abcdef|abc123|admin|admin123|root|root123)$", re.IGNORECASE),
        # 测试邮箱
        re.compile(r"^(test|admin|demo|foo|bar|example|user|root|qa)\d*@", re.IGNORECASE),
        re.compile(r"@(example\.com|test\.com|foo\.bar|test\.test)$", re.IGNORECASE),
        # 常见假身份
        re.compile(r"^(张三|李四|王五|testuser|demouser)$", re.IGNORECASE),
    ]

    @model_validator(mode="after")
    def _reject_fabricated_credentials(self) -> "VSpiderAction":
        """
        拦截 VLM 凭空捏造的凭证串。两道过滤：
          1. 仅 action="type" 触发（press_key/goto/extract 的 type_value 语义不同）
          2. 允许 {{memory_key}} 插值串透传（未解析前就是字面量 "{{phone}}"，不匹配黑名单）
        """
        if self.action != "type":
            return self
        v = (self.type_value or "").strip()
        if not v or v.startswith("{{"):
            return self
        for _pat in self._FABRICATED_CREDENTIAL_PATTERNS:
            if _pat.match(v):
                raise ValueError(
                    f"🔒 凭证安全红线：type_value={v!r} 匹配凭证捏造黑名单"
                    f"（模式：{_pat.pattern}）。"
                    "你不允许凭空捏造手机号/密码/邮箱/用户名并提交到真实表单。"
                    "正确做法："
                    "(a) 若 workflow_memory 有用户预置的凭证，用 {{变量名}} 引用；"
                    "(b) 否则立即 action=ask_human 或 action=done 裁定任务 blocked。"
                )
        return self

    @model_validator(mode="after")
    def _require_done_thought(self) -> "VSpiderAction":
        """
        action=done 必须带非空 thought，说明裁定依据（任务完成 / blocked / abort）。

        修复 Task C 式的静默 done：VLM 在登录墙前 1 步 done、thought 为空，
        用户看日志完全不知道为什么结束。

        ⚠️ 关键改造（2026-05-06）：原先 raise ValueError 会让整个 batch 校验失败 →
        全 batch 降级为 _ERROR_DECISION → action=error → 反复 done 被全 batch 罚成
        error → 3 步熔断（DataTables 任务 step 1-3 死循环复现）。
        改为**软标记**：写入兜底 thought + 标记 __empty_done_thought=True，
        让上层 PLAN GATE / 主循环 dispatch 看到此标记后决定如何处理（注入反馈让
        VLM 补理由而不是直接全 batch 死）。
        """
        if self.action == "done" and not (self.thought or "").strip():
            self.thought = (
                "[EMPTY DONE THOUGHT] 系统检测到你输出 action=done 但未在 thought 中"
                "说明裁定依据。可能是任务真已完成（应说明哪些子目标 ✅、累计提取 N 条），"
                "或 VLM 误判提前 done（应改为继续 extract / next_page 等动作）。"
                "请下一轮明确说明：是真完成还是误判？"
            )
        return self

    @model_validator(mode="after")
    def _enforce_progress_action_consistency(self) -> "VSpiderAction":
        """
        强制一致性：thought / progress_review 宣告任务完成时，action 必须 = done。

        修复 VLM "思想-动作分离" 幻觉：
          progress_review / thought 写"✅ 任务已完成"，但 action 仍输出 click/extract。
        触发规则：
          - 弱触发：progress_review + thought 双字段宽匹配（任意意向词命中）
          - 强触发：thought 单字段强匹配（行动后置完成词，补 progress_review 缺失盲点）
        """
        if self.action == "done":
            return self
        _done_markers_wide = (
            "✅ 任务已完成", "任务已完成", "进入 done",
            "任务完成，输出 done", "直接输出 done", "应输出 done",
        )
        # 强词表锁定"数据提取类任务的终态"，避免误杀子任务完成场景
        _done_markers_strong = (
            "已按要求提取", "已完成提取", "已提取所有", "已全部提取",
            "已提取 3 条", "已提取 5 条", "已提取 10 条",
        )
        _pr_done = any(m in (self.progress_review or "") for m in _done_markers_wide)
        _thought_done_wide = any(m in (self.thought or "") for m in _done_markers_wide)
        _thought_done_strong = any(m in (self.thought or "") for m in _done_markers_strong)
        _trigger = (_pr_done and _thought_done_wide) or _thought_done_strong
        if _trigger:
            import logging as _logging
            _why = "thought 强完成词" if _thought_done_strong else "progress_review+thought 双宣告"
            _logging.getLogger(__name__).warning(
                f"[ACTION FIX] {_why}任务完成但 action={self.action}，"
                f"强制纠偏为 done。thought={self.thought[:80]!r}"
            )
            self.action = "done"  # type: ignore[assignment]
            self.target_id = 0
            self.type_value = ""
        return self

    @model_validator(mode="after")
    def _require_memory_key_for_save(self) -> "VSpiderAction":
        """
        强制校验：执行 save_to_memory 时 memory_key 必须非空。

        Pydantic 校验失败会抛出 ValidationError，被 _validate_decision() 捕获并
        降级为 error 决策，同时触发自愈反馈，让 VLM 下一步重新提供正确的 memory_key。
        """
        if self.action == "save_to_memory" and not self.memory_key:
            raise ValueError(
                "执行 save_to_memory 必须提供具体的 memory_key（如 'local_ip'、'order_id'），"
                "请在返回的 JSON 中补全该字段后重试。"
            )
        return self

    @model_validator(mode="after")
    def _validate_action_consistency(self) -> "VSpiderAction":
        """
        校验动作与参数的一致性，拦截 VLM 的"手脑不一致"幻觉：
          - click + 非空 type_value（URL 形式）→ 自动纠偏为 goto
          - click + 非空 type_value（非 URL）→ VLM 实际上想 type，但选错了 action
          - type + 空 type_value   → VLM 忘记填写要输入的文字
        """
        reasoning_text = " ".join(
            str(part or "")
            for part in (
                self.progress_review,
                self.thought,
                self.current_state,
            )
        )
        _scroll_intent_direction = ""
        if re.search(
            r"scroll\s*(?:up|to top)|smooth_scroll\s*(?:up|to top)|向上滚动|往上滚|上滚|回到顶部|滚到顶部",
            reasoning_text,
            re.IGNORECASE,
        ):
            _scroll_intent_direction = "up"
        elif re.search(
            r"scroll\s*(?:down|to bottom)|smooth_scroll\s*(?:down|to bottom)|向下滚动|往下滚|下滚|继续向下|滚到下方|滚到表单|滚到按钮|不在当前视口|未在当前视口|视口.*下方|按钮未.*可见|submit.*未.*可见|submit button.*not visible|not in (?:the )?viewport|below the viewport",
            reasoning_text,
            re.IGNORECASE,
        ):
            _scroll_intent_direction = "down"
        _has_scroll_intent = bool(_scroll_intent_direction)

        _tab_switch_intent = bool(
            re.search(
                r"\bswitch_tab\b|切换.{0,8}(?:标签|tab|页)|切回.{0,10}(?:标签|tab|必应|搜索|原页|起始|首页)",
                reasoning_text,
                re.IGNORECASE,
            )
        )
        if self.action in {"click", "click_new_tab", "hover", "hover_and_click"} and _tab_switch_intent:
            tab_idx: int | None = None
            for pat in (
                r"(?:标签页?|tab)\s*(?:索引|index)?\s*[#:]?\s*(\d+)",
                r"switch_tab\s*\(\s*(\d+)",
                r"target_id[:：= ]+(\d+).{0,40}(?:标签|tab)",
                r"第\s*(\d+)\s*(?:个)?标签",
            ):
                m = re.search(pat, reasoning_text, re.IGNORECASE)
                if m:
                    tab_idx = int(m.group(1))
                    break
            if tab_idx is None and re.search(
                r"第一个|最初|索引\s*0|标签\s*0|起始标签|原标签",
                reasoning_text,
                re.I,
            ):
                tab_idx = 0
            if tab_idx is not None:
                logging.getLogger(__name__).warning(
                    "[ACTION FIX] %s with switch_tab intent in thought -> switch_tab(%s)",
                    self.action,
                    tab_idx,
                )
                self.action = "switch_tab"  # type: ignore[assignment]
                self.target_id = tab_idx
                self.type_value = str(tab_idx)
                return self

        if (
            self.action == "click"
            and self.target_id == 0
            and not (self.type_value and self.type_value.strip())
            and _has_scroll_intent
        ):
            logging.getLogger(__name__).info(
                "[ACTION FIX] click target_id=0 with scroll intent -> scroll(%s)",
                _scroll_intent_direction,
            )
            self.action = "scroll"  # type: ignore[assignment]
            self.target_id = 0
            self.type_value = _scroll_intent_direction
            return self

        # VLM 常见“手脑分裂”：thought 明确写了 @e28 / 红框 28，
        # 但结构化字段 target_id 却填 0。先从推理文本里捞回 ID，
        # 避免 hover/click 直接撞到 Element #0。
        if self.target_id == 0 and self.action in {
            "click", "click_new_tab", "hover", "select", "upload",
            "extract_link", "download_image", "remove_element",
            "drag_and_drop", "hover_and_click",
        } and not _has_scroll_intent and not _tab_switch_intent:
            id_match = re.search(
                r"(?:@e|红框\s*|ID[:：= ]+|target_id[:：= ]+)(\d{1,4})",
                reasoning_text,
                re.IGNORECASE,
            )
            if id_match:
                recovered_id = int(id_match.group(1))
                if recovered_id > 0:
                    logging.getLogger(__name__).warning(
                        "[ACTION FIX] %s target_id=0 but thought mentions target #%s; recovered target_id",
                        self.action,
                        recovered_id,
                    )
                    self.target_id = recovered_id

        if self.action == "click" and self.type_value and self.type_value.strip():
            _tv = self.type_value.strip()
            _tv_lower = _tv.lower()
            _input_like_intent = bool(
                re.search(
                    r"输入框|输入|搜索框|搜索|textbox|searchbox|combobox|textarea|\binput\b",
                    reasoning_text,
                    re.IGNORECASE,
                )
            )
            _looks_like_long_explanation = bool(
                len(_tv) > 24
                and re.search(r"\s|,|，|。|；|;|\.", _tv)
                and re.search(
                    r"click|result|link|tab|switch|open|点击|结果|链接|标签|切换|打开",
                    _tv_lower,
                    re.IGNORECASE,
                )
            )
            if _tv_lower in {"down", "up", "top", "bottom"} and (
                self.target_id == 0 or _has_scroll_intent
            ):
                import logging as _logging
                _logging.getLogger(__name__).info(
                    "[ACTION FIX] click + type_value=%r 自动纠偏为 scroll",
                    _tv,
                )
                self.action = "scroll"  # type: ignore[assignment]
                self.target_id = 0
                self.type_value = _tv_lower
                return self
            # ── 自动纠偏：click + URL → goto ────────────────────────────
            # VLM 常见幻觉：想导航到某个 URL，但错用了 click 动作并将 URL 填入 type_value
            if _tv.startswith(("http://", "https://", "www.")):
                import logging as _logging
                _logging.getLogger(__name__).info(
                    f"[ACTION FIX] click + URL 自动纠偏为 goto: {_tv[:80]}"
                )
                self.action = "goto"       # type: ignore[assignment]
                self.target_id = 0
            # ── 自动纠偏：click + 按键名 → press_key ────────────────────
            # VLM 常见幻觉：想按回车提交搜索，但错用了 click 动作
            elif _tv in (
                "Enter", "Escape", "Tab", "Backspace", "Delete", "Space",
                "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
                "PageUp", "PageDown", "Home", "End",
            ):
                import logging as _logging
                _logging.getLogger(__name__).info(
                    f"[ACTION FIX] click + type_value='{_tv}' 自动纠偏为 press_key"
                )
                self.action = "press_key"  # type: ignore[assignment]
                self.target_id = 0
            elif _tv_lower in {"click_new_tab", "new_tab", "open_new_tab", "open in new tab"}:
                import logging as _logging
                _logging.getLogger(__name__).info(
                    "[ACTION FIX] click + type_value=%r 自动纠偏为 click_new_tab",
                    _tv,
                )
                self.action = "click_new_tab"  # type: ignore[assignment]
                self.type_value = ""
            elif self.target_id > 0 and _input_like_intent and not _looks_like_long_explanation:
                import logging as _logging
                _logging.getLogger(__name__).info(
                    f"[ACTION FIX] click + 输入文本 type_value={_tv!r} 自动纠偏为 type"
                )
                self.action = "type"       # type: ignore[assignment]
                self.type_value = _tv
                return self
            # ── Fix 5：click + 短文本/标签字 → 静默擦掉 type_value，保留 click ─
            # VLM 常见 schema 错位：想点击「页码 2」「下一页」「>」按钮，
            # 错把按钮可见文字塞进 type_value（type_value 实际只用于 type 动作输入）。
            # 判定：长度 ≤ 3 且非纯字母词 → 几乎可断定是按钮 label，不是输入文本。
            # 精准条件命中时静默擦除（不抛警告），剩下的真·长文本才认为是 click→type 错选动作。
            elif (
                len(_tv) <= 3
                or _tv in ("下一页", "上一页", "Next", "Prev", "Previous", "More", "更多")
                or (len(_tv) <= 6 and (_tv.isdigit() or all(c in "<>«»‹›←→▲▼▶◀" for c in _tv)))
            ):
                import logging as _logging
                _looks_like_navigation_label = (
                    _tv.isdigit()
                    or _tv_lower in {"next", "prev", "previous", "more"}
                    or (len(_tv) <= 6 and all(c in "<>«»‹›←→▲▼▶◀" for c in _tv))
                )
                if self.target_id > 0 and _input_like_intent and not _looks_like_navigation_label:
                    _logging.getLogger(__name__).info(
                        f"[ACTION FIX] click + 短输入文本 type_value={_tv!r} 自动纠偏为 type"
                    )
                    self.action = "type"       # type: ignore[assignment]
                    self.type_value = _tv
                    return self
                _logging.getLogger(__name__).info(
                    f"[ACTION FIX] click + 短标签 type_value={_tv!r} 静默擦除"
                    f"（保留 click，target_id 应为该按钮的 @eN 数字）"
                )
                if self.target_id == 0:
                    _logging.getLogger(__name__).warning(
                        f"[ACTION FIX] click + target_id=0 + label type_value={_tv!r} "
                        "-> click_text"
                    )
                    self.action = "click_text"  # type: ignore[assignment]
                    self.target_id = 0
                    self.type_value = _tv
                else:
                    self.type_value = ""
                # action 保持 click 不变；如果 target_id 仍为 0 会进入下一道 ZERO_TARGET_DOWNGRADE 守卫
            # ── 自动纠偏：click + 长文本 → type ───────────────────────
            # 真·schema 错选：长文本几乎一定是 VLM 想 type 但选成 click
            else:
                import logging as _logging
                _logging.getLogger(__name__).info(
                    f"[ACTION FIX] click + misplaced long type_value={_tv[:40]!r}; preserving click"
                )
                self.type_value = ""
        if self.action == "type" and not (self.type_value and self.type_value.strip()):
            raise ValueError(
                "动作缺陷！你选择了 'type' 动作，但 type_value 是空的。"
                "请在 type_value 中填入你想输入的文字。"
            )
        if self.action in ("click", "click_new_tab", "type") and self.target_id == 0:
            # ── 自动纠偏：click/type + target_id=0 → wait(2s) ────────────
            # 避免硬 raise 引发连续 N 步 action=error 死循环（同 validator 内
            # 其他分支一致采用 auto-correct，这里也统一到降级模式）。
            import logging as _logging
            _prev_action = self.action
            _prev_tv = (self.type_value or "").strip()
            # 诊断：click + target_id=0 + type_value 非空且短（像元素可见文字）→
            # 极可能是 VLM 把"点击标签为 X 的按钮"误填成"click + type_value=X"，
            # 应当把对应 @eN 数字部分填到 target_id，而非 type_value
            # 也覆盖 click→type 旁路改写：上游 _validate_action_consistency 把
            # `click + type_value="2"` 当 VLM 选错 action 改成 `type`，到我们这里
            # _prev_action 就成了 type。无论 click 还是 type，target_id=0 + 短文本 type_value
            # 都强烈暗示"把元素可见文字塞错位"。
            _likely_label_misfill = (
                _prev_action in ("click", "click_new_tab", "type")
                and len(_prev_tv) <= 20
                and _prev_tv != ""
            )
            _logging.getLogger(__name__).warning(
                f"[ACTION FIX] {_prev_action} + target_id=0 (type_value={_prev_tv!r}) "
                f"自动纠偏为 wait(2s)，等 VLM 下一轮重新选有效红框"
            )
            _tv_lower = _prev_tv.lower()
            _looks_like_click_text = (
                _prev_tv
                and (
                    _prev_tv.isdigit()
                    or _tv_lower in {"next", "prev", "previous", "more"}
                    or (
                        len(_prev_tv) <= 3
                        and all(c in "<>/-+|[](){}.,:;!?" for c in _prev_tv)
                    )
                )
            )
            if _prev_action == "type" and _prev_tv and not _looks_like_click_text:
                _logging.getLogger(__name__).info(
                    "[ACTION FIX] type + target_id=0 preserved for targeted input handoff "
                    "(type_value=%r)",
                    _prev_tv[:80],
                )
                self.thought = (
                    "[TARGETED_TYPE_PENDING] type target_id=0 will be resolved by "
                    "targeted input handoff before falling back. "
                ) + (self.thought or "")
                return self
            if _looks_like_click_text:
                _logging.getLogger(__name__).warning(
                    f"[ACTION FIX] {_prev_action} + target_id=0 + label "
                    f"type_value={_prev_tv!r} -> click_text"
                )
                self.action = "click_text"  # type: ignore[assignment]
                self.target_id = 0
                self.type_value = _prev_tv
                self.thought = (
                    f"[ZERO_TARGET_AUTOFIX] target_id=0 with label {_prev_tv!r} "
                    "was converted to click_text. "
                ) + (self.thought or "")
                return self

            self.action = "wait"  # type: ignore[assignment]
            self.target_id = 0
            self.type_value = "2"
            _diag_extra = ""
            if _likely_label_misfill:
                _diag_extra = (
                    f"\n\n🚨【绝对硬指令】你刚才输出 {_prev_action} target_id=0 type_value={_prev_tv!r}，"
                    f"系统判定为「字段错位幻觉」。\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"【最快路径 — 推荐】使用 `click_text` 文本定位绕开 target_id 填位：\n"
                    f"  {{\"action\":\"click_text\",\"target_id\":0,\"type_value\":{_prev_tv!r}}}\n"
                    f"  底层引擎会用 page.get_by_text({_prev_tv!r}) 直接定位并点击，\n"
                    f"  不依赖你填对 SoM 红框 ID。**对密集分页器极强**。\n\n"
                    f"【或者 — 如果是翻页场景】用 `next_page` 启发式翻页：\n"
                    f"  {{\"action\":\"next_page\",\"target_id\":0,\"type_value\":\"\"}}\n"
                    f"  引擎会自动找 [Next/下一页/›/→] 等通用控件并点击。\n\n"
                    f"【常规路径 — 如果你确定 ID】：\n"
                    f"  1. 在【可交互元素 @eN 语义快照】里找到 Name 等于 {_prev_tv!r} 的元素（如 `@e43 [button] {_prev_tv!r}`）\n"
                    f"  2. 输出 action=\"click\"\n"
                    f"  3. target_id 必须填 @eN 的**真实非零数字**（如 43）\n"
                    f"  4. type_value 必须为空字符串 \"\"\n\n"
                    f"【绝对禁止】：\n"
                    f"  · 禁止再次把 {_prev_tv!r} 写进 type_value\n"
                    f"  · 禁止 target_id=0\n"
                    f"  · 禁止用 click_point 估算坐标（已经有红框 ID 就不要降维）\n\n"
                    f"语义口诀：type_value 只用于 type 动作的「键盘输入文本」（搜索关键词、表单内容），"
                    f"**绝对不是**按钮上的可见文字。按钮可见文字是用来在 @eN 清单里**查 ID** 的，"
                    f"查到的 ID 才填进 target_id。"
                )
            self.thought = (
                f"[ZERO_TARGET_DOWNGRADE] 上一步你想执行 {_prev_action} 但 target_id=0，"
                f"这会使校验失败并卡死循环。系统已代为 wait 2 秒。下一步请仔细看截图里可见的"
                f"红框数字，挑一个**非 0 的编号**；若屏幕没有合适元素，改用 smooth_scroll 或 press_key。"
                + _diag_extra
            ) + (self.thought or "")
            return self
        # ── click_text type_value 长度防呆 ──────────────────────────
        # VLM 偶发把推理段落塞进 type_value（应只放可见按钮/链接文字）
        if self.action == "click_text" and self.type_value and len(self.type_value) > 30:
            _raw_tv = self.type_value
            # 尝试从引号内提取真实目标文本
            _quote_match = re.search(r"['\"]([^'\"]{1,30})['\"]" , _raw_tv)
            if _quote_match:
                _fixed_tv = _quote_match.group(1).strip()
            else:
                # 取第一个空格分隔的短词或前 20 字符
                _first_word = _raw_tv.split()[0] if _raw_tv.split() else _raw_tv[:20]
                _fixed_tv = _first_word if len(_first_word) <= 20 else _raw_tv[:20]
            logging.getLogger(__name__).warning(
                f"[ACTION FIX] click_text type_value 过长({len(_raw_tv)}字), "
                f"截取修复: {_raw_tv[:50]!r}... → {_fixed_tv!r}"
            )
            self.type_value = _fixed_tv
        if self.action == "press_key" and not (self.type_value and self.type_value.strip()):
            raise ValueError(
                "动作缺陷！你选择了 'press_key' 动作，但未提供按键名称。"
                "请在 type_value 中填入按键名（如 'Enter'、'Escape'、'Tab'、'PageDown'）。"
            )
        if self.action == "switch_tab" and self.target_id < 0:
            raise ValueError(
                f"switch_tab 的 target_id 必须是非负整数（标签页索引），"
                f"收到了 {self.target_id}。请从系统提供的标签页清单中选择正确的索引。"
            )
        if self.action == "click_point" and (
            not self.point
            or len(self.point) != 2
            or not all(isinstance(v, int) for v in self.point)
        ):
            raise ValueError(
                "动作缺陷！你选择了 'click_point' 动作，但 point 字段无效。\n"
                "point 必须是包含两个整数的千分制归一化坐标列表，例如：point: [500, 300]。\n"
                "规则：左上角 [0, 0]，右下角 [1000, 1000]，正中心 [500, 500]。\n"
                "请仔细观察截图中目标元素的相对位置，换算为 0-1000 范围后重新提交。"
            )
        # Check the ACTUAL current state of extracted_data (not just serialized payload)
        # This avoids false-positive downgrade when SCHEMA RESCUE has already populated data.
        if isinstance(self.extracted_data, (list, dict)):
            _has_real_data = len(self.extracted_data) > 0
        elif isinstance(self.extracted_data, str):
            _has_real_data = self.extracted_data.strip().lower() not in (
                "", "[]", "{}", "null", "none", "undefined"
            )
        else:
            _has_real_data = self.extracted_data is not None

        if self.action == "extract" and not _has_real_data:
            logger.warning(
                "[ACTION FIX] extract + empty extracted_data → 自动降级为 wait(2s) + "
                "AX/全文自动提取兜底"
            )
            # 降级为 wait 2 秒：让主循环重新截图后 VLM 再次尝试提取
            self.action = "wait"           # type: ignore[assignment]
            self.type_value = "2"
            self.target_id = 0
            # 在 thought 中加入显式标记，供 _validate_batch / main.py 精确识别
            self.thought = "[EXTRACT_NULL_DOWNGRADE] " + (self.thought or "")
        elif self.action == "extract" and _has_real_data:
            _data_len = len(self.extracted_data) if isinstance(self.extracted_data, (list, dict)) else len(str(self.extracted_data))
            logger.info(f"[ACTION FIX] extract + extracted_data 非空 ({_data_len} items)，跳过 NULL_DOWNGRADE")
        return self

    @model_validator(mode="after")
    def _final_completion_arbiter(self) -> "VSpiderAction":
        """所有降级 validator 跑完后的最后裁决：thought 强完成词 + action 仍非 done → 强改 done"""
        if self.action == "done":
            return self  # 已是 done，不动

        _thought = self.thought or ""
        _final_done_markers = (
            "任务已完成", "✅ 任务", "进入 done", "可以 done",
            "所有数据已提取", "目标已达成", "全部完成",
        )
        _thought_implies_done = any(m in _thought for m in _final_done_markers)

        if _thought_implies_done:
            # Only force done if extracted_data has real data (avoid false positive on empty extract)
            _has_data = (
                self.extracted_data is not None
                and self.extracted_data != []
                and self.extracted_data != {}
            )
            if self.action in ("wait", "extract") and _has_data:
                logger.info(
                    f"[FINAL ARBITER] thought 含完成标记但 action={self.action!r}，"
                    f"extracted_data 非空 → 强制 action=done"
                )
                self.action = "done"

        return self

    def to_dict(self) -> dict:
        return self.model_dump()


# JSON Schema 用于 response_format（按需，仅在模型支持时启用）
_VSPIDER_ACTION_SCHEMA: dict = VSpiderAction.model_json_schema()


class VSpiderActionBatch(BaseModel):
    """VLM 返回的连招批次：一次包含一个或多个顺序执行的动作。"""

    actions: List[VSpiderAction] = Field(
        ...,
        description=(
            "要依次执行的动作列表（至少包含一个动作）。"
            "触发页面跳转或刷新的动作（如 click 提交按钮、goto）必须放在列表末尾。"
        ),
    )


# 批次 JSON Schema（连招模式下用于 response_format）
_VSPIDER_BATCH_SCHEMA: dict = VSpiderActionBatch.model_json_schema()


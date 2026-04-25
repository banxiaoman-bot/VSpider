"""
VSpider VLM 异步客户端模块

封装对本地 72B VLM 的异步请求，负责：
- 图片 Base64 编码传输
- 构造 OpenAI 兼容的多模态消息
- 解析返回的 JSON 决策
"""

import asyncio
import json
import re
import logging
from typing import Any, ClassVar, Dict, Literal, List, Optional, Union

from openai import AsyncOpenAI, BadRequestError
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

# ── 静默 httpx AsyncClient GC 噪音 ──────────────────────────────
# httpx.AsyncClient.__del__ 在事件循环关闭后尝试调度 aclose()，
# 导致 "Event loop is closed" 错误日志。Patch 使其静默跳过。
import httpx as _httpx

_orig_del = getattr(_httpx.AsyncClient, "__del__", None)
if _orig_del:
    def _silent_del(self: _httpx.AsyncClient) -> None:  # type: ignore[misc]
        try:
            _orig_del(self)
        except Exception:
            pass
    _httpx.AsyncClient.__del__ = _silent_del  # type: ignore[method-assign]

try:
    from .config import (
        VLM_API_BASE,
        VLM_API_KEY,
        VLM_MODEL_NAME,
        VLM_TIMEOUT,
        VLM_MAX_TOKENS,
        VLM_TEMPERATURE,
        VLM_HISTORY_WINDOW,
        MAX_STEPS,
    )
    from .prompts import build_system_prompt, build_user_message
except ImportError:
    from config import (
        VLM_API_BASE,
        VLM_API_KEY,
        VLM_MODEL_NAME,
        VLM_TIMEOUT,
        VLM_MAX_TOKENS,
        VLM_TEMPERATURE,
        VLM_HISTORY_WINDOW,
        MAX_STEPS,
    )
    from prompts import build_system_prompt, build_user_message

logger = logging.getLogger("vspider.vlm")


def _broadcast_log_safe(message: str, level: str = "info") -> None:
    """向 API WebSocket 广播日志；未运行 API 时静默降级。"""
    try:
        from api_server import broadcast_log

        broadcast_log(message, level=level)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════
#  Pydantic 结构化输出模型
# ════════════════════════════════════════════════════════════════

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
        "click_new_tab",  # 中键点击：强制在新标签页打开链接，避免 click+switch_tab 循环
        "smooth_scroll",  # 平滑滚动：behavior:'smooth' 模拟人类滚轮，更易触发懒加载
        "remove_element", # 物理铲除：从 DOM 树直接删除广告遮罩/悬浮弹窗等阻挡节点
        "wait",           # 显式等待：主动暂停 N 秒，应对长动画/慢加载中间态
        "drag_and_drop",  # 拖拽：将 target_id 元素拖到 type_value 指定 ID 的元素上
        "next_page",      # 启发式翻页：底层尝试 [Next/下一页/›/→] 等通用 locator
        "click_text",     # 文本定位点击：底层 page.get_by_text(type_value) 绕开 SoM ID 填位
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
        用户看日志完全不知道为什么结束。强制 VLM 把理由写出来。
        """
        if self.action == "done" and not (self.thought or "").strip():
            raise ValueError(
                "action=done 必须在 thought 中说明裁定依据："
                "是所有子目标已完成？还是遇到登录墙/风控/验证码无法继续？"
                "或是 Reflector 判定不可完成？"
                "空 thought 的 done 被拒绝，用户看不到任务为何结束。"
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
        if self.action == "click" and self.type_value and self.type_value.strip():
            _tv = self.type_value.strip()
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
                    f"[ACTION FIX] click + type_value='{_tv[:40]}' 自动纠偏为 type"
                )
                self.action = "type"       # type: ignore[assignment]
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
        if self.action == "extract" and self.extracted_data is None:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "[ACTION FIX] extract + extracted_data=null → 自动降级为 wait(2s) + "
                "自愈提示，下一步 VLM 将重新观察截图提取数据"
            )
            # 降级为 wait 2 秒：让主循环重新截图后 VLM 再次尝试提取
            self.action = "wait"           # type: ignore[assignment]
            self.type_value = "2"
            self.target_id = 0
            # 在 thought 中加入显式标记，供 _validate_batch / main.py 精确识别
            self.thought = "[EXTRACT_NULL_DOWNGRADE] " + (self.thought or "")
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


# ════════════════════════════════════════════════════════════════
#  Wave 2 — Planner / Reflector 数据模型
# ════════════════════════════════════════════════════════════════

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


_TASK_PLAN_SCHEMA: dict = TaskPlan.model_json_schema()
_REFLECTOR_DECISION_SCHEMA: dict = ReflectorDecision.model_json_schema()

# 默认的错误回退决策
_ERROR_DECISION = {
    "thought": "VLM 请求失败或返回格式异常",
    "action": "error",
    "target_id": 0,
    "type_value": "",
    "memory_key": "",
    "status": "error",
}

# 默认的错误回退决策列表（连招格式）
_ERROR_DECISION_LIST: list = [dict(_ERROR_DECISION)]


class VLMClient:
    """
    视觉大模型异步客户端。

    通过 OpenAI 兼容 API 与本地部署的 VLM 通信，
    发送带 SoM 标记的网页截图，获取 JSON 格式的操作决策。

    特性：
    - 维护最近 N 轮的操作历史摘要，帮助 VLM 避免重复操作
    - 自动重试（最多 2 次）以应对偶发网络/模型错误
    """

    # 历史窗口大小：保留最近几轮的 thought+action 摘要
    _HISTORY_WINDOW = 5
    # 最大重试次数
    _MAX_RETRIES = 2
    # 重试间隔（秒）
    _RETRY_DELAY = 2.0

    def __init__(self):
        import httpx
        try:
            from . import config as runtime_config
        except ImportError:
            import config as runtime_config

        self.api_base = getattr(runtime_config, "VLM_API_BASE", VLM_API_BASE)
        self.api_key = getattr(runtime_config, "VLM_API_KEY", VLM_API_KEY)
        self.model = getattr(runtime_config, "VLM_MODEL_NAME", VLM_MODEL_NAME)
        self.semantic_model = (
            getattr(runtime_config, "VLM_SEMANTIC_MODEL_NAME", "") or self.model
        )
        self.timeout = getattr(runtime_config, "VLM_TIMEOUT", VLM_TIMEOUT)
        self.max_tokens = getattr(runtime_config, "VLM_MAX_TOKENS", VLM_MAX_TOKENS)
        self.temperature = getattr(runtime_config, "VLM_TEMPERATURE", VLM_TEMPERATURE)
        self.text_only = bool(getattr(runtime_config, "VLM_TEXT_ONLY", False))

        # 使用自定义 httpx 客户端，禁止 GC 时自动 aclose()
        # 避免 event loop 关闭后 "Event loop is closed" 噪音日志
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
        )
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.api_base,
            timeout=self.timeout,
            http_client=_http_client,
        )
        # 操作历史：存储每轮批次中各动作的 (step, thought, action, target_id, type_value) 摘要
        self._history: list[dict] = []
        self._history_window: int = max(2, int(VLM_HISTORY_WINDOW or self._HISTORY_WINDOW))
        # 结构化输出：首次尝试 json_schema 模式，若模型不支持则自动降级
        self._use_structured: bool = True
        # 自愈反馈：下一轮 ask() 会将上一轮的执行错误注入提示
        self._pending_error: str | None = None
        logger.info(
            f"VLM client initialized | model={self.model} | base={self.api_base} | "
            f"semantic_model={self.semantic_model} | text_only={self.text_only} | "
            f"max_tokens={self.max_tokens} | temperature={self.temperature}"
        )

    def _build_history_summary(self) -> str:
        """构建最近 N 轮操作的历史摘要文本（含执行结果）。"""
        if not self._history:
            return ""

        lines = ["## 最近操作历史（含执行结果，避免重复）"]
        older_count = max(0, len(self._history) - self._history_window)
        if older_count:
            lines.append(f"- Earlier {older_count} history item(s) omitted; rely on current goal and recent results.")
        for h in self._history[-self._history_window:]:
            _result = h.get("result")
            _result_tag = f" → {_result}" if _result else " → (执行中)"
            lines.append(
                f"- 第{h['step']}步: action={h['action']}, "
                f"target_id={h['target_id']}(已失效), "
                f"type_value={h.get('type_value', '')!r}{_result_tag} "
                f"| {h.get('thought', '')[:80]}"
            )
        lines.append(
            "\n**⚠️ 严重警告**: "
            "1. 历史中的 target_id 已全部失效！红框序号每一步都会重新分配，"
            "绝对不能复用历史中的旧 ID。你必须根据**本步截图**中的红框数字重新选择目标。"
            "\n2. **务必先读每一行末尾的 `→ 结果` 字段**：若上一次点击已成功触发新标签/跳转/完成输入，"
            "切勿重复同一操作；结合用户目标判断任务是否已完成，若已达成直接输出 action=done。\n"
        )
        return "\n".join(lines)

    def _record_history(self, step: int, decisions: list[dict]) -> None:
        """记录本轮批次所有决策到历史（result 字段由 annotate_last_result 回填）。"""
        for decision in decisions:
            self._history.append({
                "step": step,
                "thought": decision.get("thought", ""),
                "action": decision.get("action", ""),
                "target_id": decision.get("target_id", 0),
                "type_value": decision.get("type_value", ""),
                "result": None,
            })
        # 只保留窗口大小的历史
        if len(self._history) > self._history_window * 2:
            self._history = self._history[-self._history_window:]

    def annotate_last_result(self, note: str) -> None:
        """
        为最早一条尚未回填结果的历史记录写入执行结果摘要。

        Args:
            note: 简短结果描述，例如 "✅ url=https://... | 标签 1→2 | 触发TabGuard" 或 "❌ 失败: click #21 超时"
        """
        if not note:
            return
        for h in self._history:
            if h.get("result") is None:
                h["result"] = note[:160]
                return

    def inject_error_feedback(self, error_msg: str) -> None:
        """
        将上一步的执行错误注入下一轮 ask() 的提示中（自愈反馈机制）。

        当 execute_action 抛出 ActionExecutionError 时，main.py 调用此方法
        把错误信息传递给 VLM，让它在下一步决策时知道上一步失败了，
        从而主动换一个策略（如换 target_id、换 action 或回滚操作）。

        Args:
            error_msg: 错误描述字符串（来自 ActionExecutionError.args[0]）
        """
        self._pending_error = error_msg
        logger.info(f"[SELF-HEAL] Error feedback injected for next ask(): {error_msg[:120]}")
        _broadcast_log_safe(f"[SELF-HEAL] Error feedback injected: {error_msg[:120]}", level="warn")

    async def ask(
        self,
        screenshot_b64: str | None,
        goal: str,
        step: int,
        input_descriptions: str = "",
        workflow_memory: dict | None = None,
        task_plan: Optional["TaskPlan"] = None,
        max_steps: int | None = None,
    ) -> list[dict]:
        """
        向 VLM/LLM 发送当前状态，请求下一批次动作（连招模式）。

        图文双模态融合 (Hybrid Modality)：
          - 默认每次都携带 SoM 截图 + 无障碍语义树 (AX Tree)，VLM 用图文两路信息综合决策。
          - 仅当 screenshot_b64 为空 / None（截图模块失败等极端情况）时，才优雅降级
            为纯文本 payload，保证任务不中断。

        Args:
            screenshot_b64: 网页截图的 Base64 编码字符串；空值触发纯文本降级
            goal: 用户的任务目标描述
            step: 当前步骤编号（从 1 开始）
            input_descriptions: 融合后的辅助文本（输入框描述 + AX Tree + 标签页/页面摘要）
            workflow_memory: 当前跨页面记忆库，非空时注入提示

        Returns:
            经过验证的决策字典列表（连招批次），每个元素包含
            thought/action/target_id/type_value/memory_key/status 等字段
        """
        history_text = self._build_history_summary()
        system_prompt = build_system_prompt(
            goal=goal,
            browser_state=input_descriptions,
            workflow_memory=workflow_memory,
        )
        logger.debug(
            "[PROMPT] dynamic system prompt chars=%s (goal=%r)",
            len(system_prompt),
            goal[:80],
        )
        user_text = build_user_message(
            goal, step, max_steps or MAX_STEPS, history_text, input_descriptions, workflow_memory,
            task_plan=task_plan,
        )
        if self.text_only and screenshot_b64:
            screenshot_b64 = None
            user_text += (
                "\n\n[TEXT_ONLY_MODEL]\n"
                "Current selected model is text-only. Strip image payload and rely only on "
                "AX Tree, element roles/names/states, URL and page text. Choose target_id "
                "strictly from the current @eN element snapshot."
            )
            _broadcast_log_safe(
                f"[MODEL] Text-only model active: image payload stripped for step {step}",
                level="warn",
            )

        # ── 图文双模态：若截图失败（极端情况）才切换纯文本降级 ───────────────
        # 常规路径保留原始"截图说明"，因为此时图文并行，VLM 依旧需要解释红框语义。
        if not screenshot_b64:
            user_text = user_text.replace(
                "## 截图说明（重要）\n"
                "截图中每个可交互元素上叠加了**黑底白字的红框序号**（如 ①②③...22 23 24...）。\n"
                "这些序号是操作用的 ID，**不是页面的实际内容**。\n"
                "执行 extract 时，必须读取**红框内部或红框旁边的真实文字/数字**，"
                "绝对不能把红框上的序号当作数据（如热度、排名、价格等）写入 extracted_data。\n",
                "## 当前模式：纯文本应急降级（截图失败，仅此一轮）\n"
                "本轮截图采集失败，只能依赖上方【可交互元素 @eN 语义快照】中的 @eN 编号来识别并操作页面元素。\n"
                "输出的 target_id 必须来自 @eN 快照段（@e5 → target_id=5）；这些编号仅本轮有效，禁止复用。\n"
                "页面语义快照段不含 @eN，只能用作理解上下文，不可用作点击目标。\n",
            )
            user_text = user_text.replace(
                "请仔细观察上方的网页截图（已标注红框和数字序号），"
                "分析当前页面状态，决定下一步操作。\n"
                "只输出 JSON，不要输出其他内容。",
                "本轮仅有 AX Tree（截图缺失），请综合元素的 Role / Name / Value / State 判断页面状态并决定下一步动作。"
                "如果你发现页面已达到用户目标状态，请直接输出 done。只输出 JSON，不要输出其他内容。",
            )
            logger.warning(f"[步骤 {step}] 截图缺失，本轮降级为纯文本 payload")
            _broadcast_log_safe(
                f"[步骤 {step}] 截图缺失，本轮降级为纯文本 payload", level="warn"
            )

        # ── 自愈反馈：将上一步的执行错误注入提示，强制 VLM 修正而非逃避 ────
        if self._pending_error:
            error_msg = self._pending_error
            error_note = (
                f"\n\n🚨 严重警告：你上一步决定的动作执行失败或校验不通过！\n"
                f"底层报错信息：{error_msg}\n\n"
                f"【系统强制指令】：请仔细阅读上述报错并反思。如果是缺少字段或格式错误，"
                f"请在本次输出中**直接修复该动作并重新提交**。"
                f"绝对不允许逃避错误去执行其他无关动作（如盲目 scroll 或 click）！"
            )
            user_text = user_text + error_note
            logger.info(f"[SELF-HEAL] Injected error context into step {step} prompt")
            _broadcast_log_safe(f"[SELF-HEAL] Injected error context into step {step} prompt", level="warn")
            self._pending_error = None  # 消费后清空，避免下轮重复注入

        # ── 尾部 JSON 强约束：AX Tree 较长时注意力容易被带偏，在最末尾再钉一遍 ───
        user_text = (
            user_text
            + "\n\n🚨【系统强制指令】：请务必结合上述图文信息进行决策，"
              "并严格按照前文要求的 JSON 格式输出你的 actions！"
              "绝对不要输出任何其他废话或解释文本！"
        )

        # ── 图文双模态 Payload 组装：默认图 + 文，image 缺失时优雅降级 ─────────
        # 兼容 OpenAI / 通义千问 Qwen-VL 的 multimodal messages 协议：
        #   content = [{"type": "image_url", ...}, {"type": "text", ...}]
        if screenshot_b64:
            # SoM 截图与 AX Tree 已经在 input_descriptions 中融合到 user_text 里，
            # 下方 image_url 提供视觉通道，VLM 会结合两路信息做综合决策。
            #
            # 安全校验：识别 "data:image" 前缀（覆盖 jpeg/png/webp 等所有 image/* 子类型），
            # 避免拼出 `data:image/jpeg;base64,data:image/...` 这种双重前缀引发 400。
            image_url = screenshot_b64
            if screenshot_b64 and not screenshot_b64.startswith("data:image"):
                image_url = f"data:image/jpeg;base64,{screenshot_b64}"

            user_content = [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": user_text},
            ]
        else:
            # 极端降级：截图缺失时仅发文本，确保任务不中断
            user_content = user_text

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": user_content,
            },
        ]

        last_error = None
        for attempt in range(1, self._MAX_RETRIES + 2):  # 1 + MAX_RETRIES 次
            try:
                if attempt > 1:
                    logger.info(f"[步骤 {step}] VLM 第 {attempt} 次重试...")
                    await asyncio.sleep(self._RETRY_DELAY)
                else:
                    logger.info(f"[步骤 {step}] 正在请求 VLM 决策...")
                    _broadcast_log_safe(f"[步骤 {step}] 正在请求 VLM 决策...")

                # ── 结构化输出：首次尝试 json_schema 模式 ────────────────────
                # 部分本地模型不支持 response_format=json_schema，
                # 遇到 BadRequestError 时自动降级为普通模式（仅降级一次，全程生效）
                api_kwargs: dict = {
                    "model": self.model,
                    "messages": messages,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                }
                if self._use_structured:
                    api_kwargs["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "VSpiderActionBatch",
                            "strict": False,
                            "schema": _VSPIDER_BATCH_SCHEMA,
                        },
                    }

                try:
                    response = await self.client.chat.completions.create(**api_kwargs)
                except BadRequestError as bre:
                    bre_str = str(bre)
                    # ── 区分"模型不支持 json_schema"与"内容审核拦截" ──────────
                    # data_inspection_failed 是 DashScope 安全审核拒绝截图，
                    # 不应该误触发 json_schema 降级，必须抛到外层走纯文本降级链路。
                    if "data_inspection_failed" in bre_str:
                        raise  # 抛到外层 except Exception 走纯文本降级
                    if self._use_structured:
                        # 模型不支持 json_schema → 永久降级并立即重试
                        self._use_structured = False
                        logger.info(
                            f"[STRUCTURED] Model does not support json_schema response_format "
                            f"({bre}), disabling for this session and retrying..."
                        )
                        api_kwargs.pop("response_format", None)
                        response = await self.client.chat.completions.create(**api_kwargs)
                    else:
                        raise

                raw_content = response.choices[0].message.content
                logger.debug(f"[步骤 {step}] VLM 原始返回:\n{raw_content}")

                # ── Pydantic 验证：parse → validate_batch → list[dict] ────────
                # _parse_json 提取 JSON 对象（兼容 markdown fence / 嵌套 JSON），
                # _validate_batch 支持新格式 {"actions":[...]} 和旧格式 flat dict。
                raw_dict = self._parse_json(raw_content)
                decisions = self._validate_batch(raw_dict, step)

                # 打印思考过程（取批次第一个动作的 thought）
                first_pr = decisions[0].get("progress_review", "") if decisions else ""
                if first_pr:
                    logger.info(f"[步骤 {step}] VLM 进度复盘: {first_pr}")
                    _broadcast_log_safe(f"[步骤 {step}] VLM 进度复盘: {first_pr}")
                first_thought = decisions[0].get("thought", "") if decisions else ""
                if first_thought:
                    logger.info(f"[步骤 {step}] VLM 思考: {first_thought}")
                    _broadcast_log_safe(f"[步骤 {step}] VLM 思考: {first_thought}")

                for i, d in enumerate(decisions):
                    logger.info(
                        f"[步骤 {step}] VLM 决策[{i + 1}/{len(decisions)}]: "
                        f"action={d.get('action')} "
                        f"target_id={d.get('target_id')} "
                        f"type_value={d.get('type_value', '')!r} "
                        f"status={d.get('status')}"
                    )
                    _broadcast_log_safe(
                        f"[步骤 {step}] VLM 决策[{i + 1}/{len(decisions)}]: "
                        f"action={d.get('action')} "
                        f"target_id={d.get('target_id')} "
                        f"type_value={d.get('type_value', '')!r} "
                        f"status={d.get('status')}"
                    )

                # 记录到历史
                self._record_history(step, decisions)

                return decisions

            except Exception as e:
                last_error = e
                err_str = str(e)

                # ── 内容审核拒绝：截图被 DashScope 安全过滤器拦截 ────────────
                # 典型场景：新闻页面含敏感图片/标题，重试发同一张图毫无意义。
                # 策略：去掉图片，降级为纯文本模式再发一次请求。
                if "data_inspection_failed" in err_str:
                    logger.warning(
                        f"[步骤 {step}] 截图被 DashScope 内容审核拦截，"
                        f"降级为纯文本模式重试..."
                    )
                    _broadcast_log_safe(
                        f"[步骤 {step}] 截图被内容审核拦截，降级为纯文本模式...",
                        level="warn",
                    )
                    # 将 messages 中的图片剥离，只保留文本
                    _text_only_content = user_text
                    _text_only_messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": _text_only_content},
                    ]
                    _text_api_kwargs: dict = {
                        "model": self.model,
                        "messages": _text_only_messages,
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                    }
                    try:
                        response = await self.client.chat.completions.create(
                            **_text_api_kwargs
                        )
                        raw_content = response.choices[0].message.content
                        logger.debug(
                            f"[步骤 {step}] VLM 纯文本降级返回:\n{raw_content}"
                        )
                        raw_dict = self._parse_json(raw_content)
                        decisions = self._validate_batch(raw_dict, step)
                        first_thought = (
                            decisions[0].get("thought", "") if decisions else ""
                        )
                        if first_thought:
                            logger.info(
                                f"[步骤 {step}] VLM 思考(纯文本): {first_thought}"
                            )
                            _broadcast_log_safe(
                                f"[步骤 {step}] VLM 思考(纯文本): {first_thought}"
                            )
                        for i, d in enumerate(decisions):
                            logger.info(
                                f"[步骤 {step}] VLM 决策(纯文本)[{i + 1}/{len(decisions)}]: "
                                f"action={d.get('action')} "
                                f"target_id={d.get('target_id')} "
                                f"type_value={d.get('type_value', '')!r}"
                            )
                        self._record_history(step, decisions)
                        return decisions
                    except Exception as _fallback_err:
                        logger.error(
                            f"[步骤 {step}] 纯文本降级也失败: {_fallback_err}"
                        )
                        last_error = _fallback_err
                    # 不再继续重试，直接跳出循环
                    break

                logger.warning(
                    f"[步骤 {step}] VLM 请求异常 (尝试 {attempt}/"
                    f"{self._MAX_RETRIES + 1}): {type(e).__name__}: {e}"
                )
                _broadcast_log_safe(
                    f"[步骤 {step}] VLM 请求异常 (尝试 {attempt}/{self._MAX_RETRIES + 1}): "
                    f"{type(e).__name__}: {e}",
                    level="warn",
                )

        logger.error(f"[步骤 {step}] VLM 请求最终失败: {last_error}")
        _broadcast_log_safe(f"[步骤 {step}] VLM 请求最终失败: {last_error}", level="error")
        return list(_ERROR_DECISION_LIST)

    # ════════════════════════════════════════════════════════════════
    #  全页结构化数据提取（DOM 全文 + 纯文本 VLM 结构化调用）
    # ════════════════════════════════════════════════════════════════

    async def extract_structured_data(
        self,
        page_text: str,
        goal: str,
        example_data: Any = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        从页面 DOM 全文中提取结构化数据（纯文本 LLM 调用，无需截图）。

        借鉴 browser-use 的 extract_clean_markdown 架构：
        VLM 只负责发出 extract 意图，数据由系统从 DOM 获取全页文本后，
        用一次纯文本 LLM 调用完成结构化，突破视口截图只能看到 ~10 条的限制。

        Args:
            page_text: document.body.innerText 获取的完整页面文本
            goal: 用户的任务目标描述
            example_data: VLM 之前提取的数据样本（用于推断字段名和格式）

        Returns:
            结构化数据列表（list[dict]），失败时返回 None
        """
        page_text = (page_text or "").strip()
        if not page_text:
            logger.warning("[EXTRACT FULL] 页面文本为空，跳过结构化提取")
            return None

        # 截断防止 token 溢出（纯文本 token 效率高，给 16K 字符）
        page_text = page_text[:16000]

        # ── 解析用户目标里的数量需求（如"前 3 条"），指导后端 LLM 一次返回够数 ──
        _count_match = re.search(
            r"(?:前|取|抓|提取|爬)\s*(\d+)\s*(?:条|个|项|篇|则)", goal or ""
        )
        _hint_count = int(_count_match.group(1)) if _count_match else None
        count_hint = (
            f"\n\n⚠️ 用户明确要求提取【{_hint_count} 条】数据。"
            f"请在页面中按原始出现顺序找出前 {_hint_count} 条符合条件的条目，"
            f"**一次性全部**放入返回的 JSON 数组，不要只返回 1 条或少于 {_hint_count} 条。"
            if _hint_count else ""
        )

        # ── 根据 VLM 已提取的样本推断字段格式 ──
        format_hint = ""
        if example_data:
            try:
                if isinstance(example_data, list) and len(example_data) > 0:
                    sample = example_data[0] if isinstance(example_data[0], dict) else {"value": example_data[0]}
                    format_hint = f"\n期望的数据字段和格式示例（请严格遵循）：\n{json.dumps(sample, ensure_ascii=False, indent=2)}"
                elif isinstance(example_data, dict):
                    format_hint = f"\n期望的数据字段和格式示例（请严格遵循）：\n{json.dumps(example_data, ensure_ascii=False, indent=2)}"
            except Exception:
                pass

        system_prompt = (
            "你是一个专业的网页数据提取助手。\n"
            "规则：\n"
            "1. 只输出 JSON 数组 [...]，不要输出任何解释文字、markdown 代码块或其他格式\n"
            "2. 提取页面中**所有**符合用户需求的数据项，一条都不要遗漏\n"
            "3. 每个数据项是一个字典，字段名称应清晰且与用户需求匹配\n"
            "4. 如果有格式示例，严格遵循示例的字段名和结构\n"
            "5. 如果页面中没有匹配的数据，输出空数组 []\n"
            "6. 数值字段保持原始格式（如 '3500万' 而非 35000000）\n"
            + count_hint
        )

        user_prompt = (
            f"用户任务：{goal}\n"
            f"{format_hint}{count_hint}\n\n"
            f"以下是网页的完整文本内容（通过 DOM 提取，包含页面上所有数据，"
            f"不受视口限制），请从中提取**全部**符合用户任务要求的数据项：\n\n"
            f"---页面文本开始---\n{page_text}\n---页面文本结束---\n\n"
            f"请输出 JSON 数组，包含页面中所有匹配项。只输出 JSON，不要输出其他内容。"
        )

        try:
            logger.info(
                f"[EXTRACT FULL] 开始全页结构化提取，"
                f"页面文本长度={len(page_text)}，目标={goal[:60]}"
            )
            _broadcast_log_safe(
                "[EXTRACT FULL] 启动全页 AX Tree 结构化提取（纯文本 LLM 调用）..."
            )

            response = await self.client.chat.completions.create(
                model=self.semantic_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=self.max_tokens,
                temperature=0.1,  # 低温度确保数据提取准确
            )

            raw = (response.choices[0].message.content or "").strip()

            # 清理 markdown code fence
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
                raw = re.sub(r"\n?\s*```$", "", raw)

            data = json.loads(raw)

            if isinstance(data, list):
                logger.info(
                    f"[EXTRACT FULL] 全页结构化提取成功，共 {len(data)} 条数据"
                )
                _broadcast_log_safe(
                    f"[EXTRACT FULL] 全页提取完成：{len(data)} 条数据"
                )
                print(
                    f"\033[1;36m🔍 [EXTRACT FULL]\033[0m "
                    f"AX Tree 全页提取 \033[36m{len(data)}\033[0m 条数据"
                    f"（VLM 视口内仅 ~10 条）"
                )
                return data
            elif isinstance(data, dict):
                logger.info("[EXTRACT FULL] 返回单个字典，包装为列表")
                return [data]
            else:
                logger.warning(f"[EXTRACT FULL] 意外的返回类型: {type(data)}")
                return None

        except json.JSONDecodeError as e:
            logger.warning(f"[EXTRACT FULL] JSON 解析失败: {e}")
            return None
        except Exception as e:
            logger.warning(
                f"[EXTRACT FULL] 结构化提取失败，将降级使用 VLM 原始数据: {e}"
            )
            return None

    # ════════════════════════════════════════════════════════════════
    #  Wave 2 — Planner / Reflector（纯文本 LLM，无截图）
    # ════════════════════════════════════════════════════════════════

    async def make_plan(
        self,
        goal: str,
        initial_url: str,
        workflow_memory: Optional[dict] = None,
    ) -> TaskPlan:
        """
        任务起手调一次，把 goal 拆成 3-6 个可独立验证的子目标。

        失败（网络 / JSON / schema）时返回只含单一子目标的 TaskPlan，
        等价于关闭 Planner —— 保证零回归。
        """
        fallback_plan = TaskPlan(
            goal=goal,
            sub_goals=[SubGoal(
                id=1,
                description=goal[:80],
                exit_criteria="完成用户全部需求",
                status="active",
            )],
            current_idx=0,
        )

        try:
            from .prompts import build_plan_prompts  # type: ignore
        except ImportError:
            from prompts import build_plan_prompts   # type: ignore

        system_prompt, user_prompt = build_plan_prompts(goal, initial_url, workflow_memory)

        try:
            logger.info(f"[PLANNER] 生成任务计划 goal={goal[:60]}")
            _broadcast_log_safe("[PLANNER] 调用 Planner LLM 生成任务计划...")

            api_kwargs: dict = {
                "model": self.semantic_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": self.max_tokens,
                "temperature": 0.2,
            }
            if self._use_structured:
                api_kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "TaskPlan",
                        "strict": False,
                        "schema": _TASK_PLAN_SCHEMA,
                    },
                }

            try:
                response = await self.client.chat.completions.create(**api_kwargs)
            except BadRequestError:
                api_kwargs.pop("response_format", None)
                response = await self.client.chat.completions.create(**api_kwargs)

            raw = (response.choices[0].message.content or "").strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
                raw = re.sub(r"\n?\s*```$", "", raw)

            data = json.loads(raw)
            plan = TaskPlan.model_validate(data)

            if not plan.sub_goals:
                logger.warning("[PLANNER] LLM 返回空子目标列表，降级为单子目标计划")
                return fallback_plan

            # 首个子目标默认置为 active
            if plan.sub_goals[0].status != "active":
                plan.sub_goals[0].status = "active"
            plan.current_idx = 0

            logger.info(
                f"[PLANNER] 计划生成成功：{len(plan.sub_goals)} 个子目标 | "
                + plan.summary()
            )
            _broadcast_log_safe(
                f"[PLANNER] 计划生成：{len(plan.sub_goals)} 个子目标"
            )
            return plan

        except Exception as e:
            logger.warning(f"[PLANNER] 生成失败降级为单子目标：{type(e).__name__}: {e}")
            return fallback_plan

    async def reflect(
        self,
        plan: TaskPlan,
        history_summary: str,
        signals: List[str],
        current_url: str,
    ) -> ReflectorDecision:
        """
        失败信号或兜底兜到时调，审计"计划 vs 现状"。

        失败时返回 decision="continue"，等价于不介入，保持零回归。
        """
        fallback = ReflectorDecision(decision="continue", reason="reflector call failed")

        try:
            from .prompts import build_reflect_prompts  # type: ignore
        except ImportError:
            from prompts import build_reflect_prompts   # type: ignore

        system_prompt, user_prompt = build_reflect_prompts(
            plan, history_summary, signals, current_url
        )

        try:
            logger.info(
                f"[REFLECTOR] 触发审计，信号={signals}；当前子目标 "
                f"{plan.current_idx + 1}/{len(plan.sub_goals)}"
            )
            _broadcast_log_safe(
                f"[REFLECTOR] 调用 Reflector LLM 审计，信号：{'; '.join(signals)}"
            )

            api_kwargs: dict = {
                "model": self.semantic_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": self.max_tokens,
                "temperature": 0.2,
            }
            if self._use_structured:
                api_kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "ReflectorDecision",
                        "strict": False,
                        "schema": _REFLECTOR_DECISION_SCHEMA,
                    },
                }

            try:
                response = await self.client.chat.completions.create(**api_kwargs)
            except BadRequestError:
                api_kwargs.pop("response_format", None)
                response = await self.client.chat.completions.create(**api_kwargs)

            raw = (response.choices[0].message.content or "").strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
                raw = re.sub(r"\n?\s*```$", "", raw)

            data = json.loads(raw)
            rd = ReflectorDecision.model_validate(data)
            logger.info(f"[REFLECTOR] 决策={rd.decision} 理由={rd.reason[:80]}")
            _broadcast_log_safe(f"[REFLECTOR] 决策={rd.decision} | {rd.reason[:60]}")
            return rd

        except Exception as e:
            logger.warning(f"[REFLECTOR] 调用失败降级为 continue：{type(e).__name__}: {e}")
            return fallback

    def _validate_batch(self, raw: dict, step: int) -> list[dict]:
        """
        验证并规范化 VLM 返回的批次数据，返回 list[dict]。

        支持两种输入格式：
        1. 新格式（连招）：{"actions": [...]} → 批次 Pydantic 校验
        2. 旧格式（单动作）：{"action": "click", ...} → 包装为单元素列表

        Args:
            raw:  _parse_json() 返回的原始字典
            step: 当前步骤编号（仅用于日志）

        Returns:
            经过校验的决策字典列表
        """
        if isinstance(raw, dict) and "actions" in raw:
            # 新格式：批次校验
            try:
                batch = VSpiderActionBatch.model_validate(raw)
                decisions = [a.to_dict() for a in batch.actions]
                # ── 检查 extract→wait / zero-target→wait 降级，注入自愈提示 ──
                _EXTRACT_MARKER = "[EXTRACT_NULL_DOWNGRADE]"
                _ZERO_MARKER = "[ZERO_TARGET_DOWNGRADE]"
                for idx, a in enumerate(batch.actions):
                    thought = a.thought or ""
                    if thought.startswith(_EXTRACT_MARKER):
                        decisions[idx]["__extract_downgraded"] = True
                        self.inject_error_feedback(
                            "⚠️ 你上一步想执行 extract 动作，但 extracted_data 是 null，"
                            "已被系统自动降级为 wait。\n"
                            "【本步要求】请重新执行 extract 动作，这次必须仔细观察截图，"
                            "将目标数据整理为结构化 JSON 写入 extracted_data 字段。\n"
                            "例如：extracted_data: {\"rank\": 1, \"title\": \"...\", \"views\": \"...\"}\n"
                            "extracted_data 绝对不能是 null！"
                        )
                        break
                    if _ZERO_MARKER in thought:
                        decisions[idx]["__zero_target_downgraded"] = True
                        self.inject_error_feedback(thought)
                        break
                return decisions
            except ValidationError as ve:
                logger.warning(
                    f"[步骤 {step}] Pydantic 批次校验失败，降级为 error 决策: {ve}"
                )
                _feedback = (
                    f"🚨 严重警告：你上一步输出的 JSON 格式校验失败！\n"
                    f"Pydantic 报错详情：\n{ve}\n\n"
                    f"【系统强制指令】：请仔细阅读上述报错"
                    f"（特别是 actions 数组中缺失或错误的字段，如 memory_key）。"
                    f"请直接在本次输出中严格按照 Schema 修正你的 JSON 格式，绝对不允许逃避！"
                )
                self.inject_error_feedback(_feedback)
                return list(_ERROR_DECISION_LIST)

        # 旧格式兼容：flat dict → 单元素列表
        return [self._validate_single(raw, step)]

    def _validate_single(self, raw: dict, step: int) -> dict:
        """
        验证单个动作字典（旧格式兼容 + 批次内部元素校验的入口）。

        将 target_id 强制转为 int、为缺失字段填充默认值、
        检测非法 action 值并降级为 "error"，同时注入自愈反馈。
        """
        try:
            action_obj = VSpiderAction.model_validate(raw)
            return action_obj.to_dict()
        except ValidationError as ve:
            logger.warning(
                f"[步骤 {step}] Pydantic 校验失败，降级为 error 决策: {ve}"
            )
            # 将校验报错注入自愈反馈队列
            _feedback = (
                f"🚨 严重警告：你上一步输出的 JSON 格式校验失败！\n"
                f"Pydantic 报错详情：\n{ve}\n\n"
                f"【系统强制指令】：请仔细阅读上述报错（特别是缺失或错误的字段，如 memory_key）。"
                f"请直接在本次输出中严格按照 Schema 修正你的 JSON 格式，绝对不允许逃避！"
            )
            self.inject_error_feedback(_feedback)
            fallback = dict(_ERROR_DECISION)
            if isinstance(raw, dict):
                fallback["thought"] = raw.get("thought", fallback["thought"])
            return fallback

    def _parse_json(self, raw: str) -> dict:
        """
        从 VLM 的原始返回文本中提取 JSON 对象。

        处理以下情况：
        1. 纯 JSON 字符串
        2. 被 ```json ... ``` 包裹的 JSON
        3. 文本中夹杂的 JSON 块

        Args:
            raw: VLM 的原始返回文本

        Returns:
            解析后的字典
        """
        if not raw or not raw.strip():
            logger.warning("VLM 返回内容为空")
            return dict(_ERROR_DECISION)

        text = raw.strip()

        # 尝试 1：直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试 2：提取 markdown code fence 中的 JSON
        code_fence_match = re.search(
            r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL
        )
        if code_fence_match:
            try:
                return json.loads(code_fence_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 尝试 3：提取文本中第一个 {...} 块
        brace_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        # 尝试 4：提取嵌套的 {...} 块（贪婪匹配）
        brace_match_greedy = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match_greedy:
            try:
                return json.loads(brace_match_greedy.group(0))
            except json.JSONDecodeError:
                pass

        logger.warning(f"无法从 VLM 返回中解析 JSON:\n{text[:500]}")
        return dict(_ERROR_DECISION)

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
from typing import Any, Literal, List, Optional

from openai import AsyncOpenAI, BadRequestError
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

try:
    from .config import (
        VLM_API_BASE,
        VLM_API_KEY,
        VLM_MODEL_NAME,
        VLM_TIMEOUT,
        VLM_MAX_TOKENS,
        VLM_TEMPERATURE,
        MAX_STEPS,
    )
    from .prompts import SYSTEM_PROMPT, build_user_message
except ImportError:
    from config import (
        VLM_API_BASE,
        VLM_API_KEY,
        VLM_MODEL_NAME,
        VLM_TIMEOUT,
        VLM_MAX_TOKENS,
        VLM_TEMPERATURE,
        MAX_STEPS,
    )
    from prompts import SYSTEM_PROMPT, build_user_message

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

    current_state: str = Field(default="", description="当前页面状态描述")
    thought: str = Field(default="", description="VLM 的思考过程")
    action: Literal[
        "click", "type", "hover", "scroll", "select",
        "press_key", "goto", "upload",
        "extract", "extract_link", "download_image",
        "close_tab", "switch_tab", "save_to_memory", "done", "ask_human", "error",
        "captcha_detected",
        "click_point",    # 无选择器坐标点击：直接用像素坐标操控鼠标，跳过 SoM ID 定位
        "smooth_scroll",  # 平滑滚动：behavior:'smooth' 模拟人类滚轮，更易触发懒加载
        "remove_element", # 物理铲除：从 DOM 树直接删除广告遮罩/悬浮弹窗等阻挡节点
        "wait",           # 显式等待：主动暂停 N 秒，应对长动画/慢加载中间态
    ] = Field(..., description="要执行的动作类型")
    target_id: int = Field(default=0, description="目标元素的 SoM ID")
    type_value: str = Field(default="", description="输入框内容（支持 {{key}} 插值）或按键名称")
    memory_key: str = Field(
        ...,  # 绝对必填，不设默认值，强制生成引擎输出此字段
        description=(
            "【绝对必填字段】无论执行什么 action，都必须输出此字段！"
            "如果是 click、scroll 等不需要保存记忆的动作，请填入空字符串 \"\"；"
            "如果是 save_to_memory，必须填写一个英文变量名（如 'icp_number'、'local_ip'）。"
        ),
    )
    extracted_data: Any = Field(default=None, description="提取的结构化数据")
    # 无选择器坐标定位专用字段：[x, y] 千分制归一化坐标（0-1000），仅 click_point 动作使用
    # 后端会自动将归一化坐标换算为当前视口的真实像素坐标再点击
    point: Optional[List[int]] = Field(default=None, description="千分制归一化坐标 [x, y]（0-1000），仅 click_point 动作使用")
    status: str = Field(default="", description="状态信息（如 captcha_detected）")

    @field_validator("target_id", mode="before")
    @classmethod
    def _coerce_target_id(cls, v: Any) -> int:
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
          - click + 非空 type_value → VLM 实际上想 type，但选错了 action
          - type + 空 type_value   → VLM 忘记填写要输入的文字
        """
        if self.action == "click" and self.type_value and self.type_value.strip():
            raise ValueError(
                f"动作冲突！你选择了 'click'，但却在 type_value 中填入了 '{self.type_value}'。"
                f"如果你想在输入框中打字，请务必将 action 改为 'type'！"
                f"如果你真的只是想点击，请将 type_value 留空 \"\"。"
            )
        if self.action == "type" and not (self.type_value and self.type_value.strip()):
            raise ValueError(
                "动作缺陷！你选择了 'type' 动作，但 type_value 是空的。"
                "请在 type_value 中填入你想输入的文字。"
            )
        if self.action in ("click", "type") and self.target_id == 0:
            raise ValueError(
                f"🚨 严重错误！执行 '{self.action}' 动作必须指定页面上真实的红框序号作为 target_id，"
                f"绝对不能为 0！请仔细观察截图，找到你要操作的元素对应的数字序号。"
            )
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
            raise ValueError(
                "动作缺陷！你选择了 'extract' 动作，但 extracted_data 是 null。\n"
                "extract 动作的核心职责就是从页面截图中读取数据并输出到 extracted_data 字段。\n"
                "请仔细观察截图，将你看到的目标数据整理为结构化 JSON 填入 extracted_data。\n"
                "例如：extracted_data: {\"rank\": 1, \"title\": \"AI技术突破\", \"hot\": \"523万\"}"
            )
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
        self.client = AsyncOpenAI(
            api_key=VLM_API_KEY,
            base_url=VLM_API_BASE,
            timeout=VLM_TIMEOUT,
        )
        self.model = VLM_MODEL_NAME
        # 操作历史：存储每轮批次中各动作的 (step, thought, action, target_id, type_value) 摘要
        self._history: list[dict] = []
        # 结构化输出：首次尝试 json_schema 模式，若模型不支持则自动降级
        self._use_structured: bool = True
        # 自愈反馈：下一轮 ask() 会将上一轮的执行错误注入提示
        self._pending_error: str | None = None
        logger.info(f"VLM 客户端初始化完成 | 模型: {self.model} | 端点: {VLM_API_BASE}")

    def _build_history_summary(self) -> str:
        """构建最近 N 轮操作的历史摘要文本。"""
        if not self._history:
            return ""

        lines = ["## 最近操作历史（避免重复）"]
        for h in self._history[-self._HISTORY_WINDOW:]:
            lines.append(
                f"- 第{h['step']}步: action={h['action']}, "
                f"target_id={h['target_id']}, "
                f"type_value={h.get('type_value', '')!r} "
                f"| {h.get('thought', '')[:80]}"
            )
        lines.append(
            "\n**重要**: 如果上面的历史显示你连续多轮执行了相同的操作但页面没有变化，"
            "请务必换一个不同的策略！不要重复同样的操作。\n"
        )
        return "\n".join(lines)

    def _record_history(self, step: int, decisions: list[dict]) -> None:
        """记录本轮批次所有决策到历史。"""
        for decision in decisions:
            self._history.append({
                "step": step,
                "thought": decision.get("thought", ""),
                "action": decision.get("action", ""),
                "target_id": decision.get("target_id", 0),
                "type_value": decision.get("type_value", ""),
            })
        # 只保留窗口大小的历史
        if len(self._history) > self._HISTORY_WINDOW * 2:
            self._history = self._history[-self._HISTORY_WINDOW:]

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
    ) -> list[dict]:
        """
        向 VLM/LLM 发送当前状态，请求下一批次动作（连招模式）。

        图文双模态融合 (Hybrid Modality)：
          - 默认每次都携带 SoM 截图 + 精简 DOM 树，VLM 用图文两路信息综合决策。
          - 仅当 screenshot_b64 为空 / None（截图模块失败等极端情况）时，才优雅降级
            为纯文本 payload，保证任务不中断。

        Args:
            screenshot_b64: 网页截图的 Base64 编码字符串；空值触发纯文本降级
            goal: 用户的任务目标描述
            step: 当前步骤编号（从 1 开始）
            input_descriptions: 融合后的辅助文本（输入框描述 + 精简 DOM 树 + 标签页/页面摘要）
            workflow_memory: 当前跨页面记忆库，非空时注入提示

        Returns:
            经过验证的决策字典列表（连招批次），每个元素包含
            thought/action/target_id/type_value/memory_key/status 等字段
        """
        history_text = self._build_history_summary()
        user_text = build_user_message(
            goal, step, MAX_STEPS, history_text, input_descriptions, workflow_memory
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
                "本轮截图采集失败，只能依赖上方【精简 DOM 树】中的 [ID: N] 序号来识别并操作页面元素。\n"
                "输出的 target_id 必须来自 DOM 列表的 [ID: N]；这些 ID 仅本轮有效，禁止复用。\n"
                "如果看到 [TEXT] 开头的行，仅能用于 extract/save_to_memory(target_id=0)，不能点击。\n",
            )
            user_text = user_text.replace(
                "请仔细观察上方的网页截图（已标注红框和数字序号），"
                "分析当前页面状态，决定下一步操作。\n"
                "只输出 JSON，不要输出其他内容。",
                "本轮仅有精简 DOM 树（截图缺失），请综合 placeholder、文本、类型判断页面状态并决定下一步动作。"
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

        # ── 尾部 JSON 强约束：DOM 树很长时注意力容易被带偏，在最末尾再钉一遍 ───
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
            # SoM 截图与精简 DOM 树已经在 input_descriptions 中融合到 user_text 里，
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
            {"role": "system", "content": SYSTEM_PROMPT},
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
                    "max_tokens": VLM_MAX_TOKENS,
                    "temperature": VLM_TEMPERATURE,
                }
                if self._use_structured:
                    api_kwargs["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "VSpiderActionBatch",
                            "strict": True,
                            "schema": _VSPIDER_BATCH_SCHEMA,
                        },
                    }

                try:
                    response = await self.client.chat.completions.create(**api_kwargs)
                except BadRequestError as bre:
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
                return [a.to_dict() for a in batch.actions]
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

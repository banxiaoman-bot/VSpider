"""
VSpider VLM Response Parser Mixin (from vlm_client.py Phase 2 extraction)

Parser/validation methods for VLM JSON responses:
- _parse_json: Extract JSON from raw VLM text
- _validate_batch: Validate batch action format
- _validate_single: Validate single action format
- _rescue_schema_collapse: Auto-rescue malformed VLM outputs
"""

import json
import logging
import re
from typing import Any, ClassVar

from pydantic import ValidationError

try:
    from .vlm_models import (
        VSpiderAction,
        VSpiderActionBatch,
        ERROR_DECISION as _ERROR_DECISION,
        ERROR_DECISION_LIST as _ERROR_DECISION_LIST,
    )
except ImportError:
    from vlm_models import (
        VSpiderAction,
        VSpiderActionBatch,
        ERROR_DECISION as _ERROR_DECISION,
        ERROR_DECISION_LIST as _ERROR_DECISION_LIST,
    )

logger = logging.getLogger("vspider.vlm")


class VLMParserMixin:
    """Mixin providing VLM response parsing/validation for VLMClient."""

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
        # ── Schema-collapse 救援：检测 VLM 把 extracted_data 单行直接当根对象返回 ──
        # 触发模型：弱模型（qwen3-vl-flash 等）在长 prompt 下偶发把 schema 包裹层吃掉，
        # 直接吐出 {title, url, points, ...} 数据行，导致缺 action/memory_key 校验失败。
        # 救援策略：识别"裸数据行"特征 → 自动包裹成合法 extract action，**不打断流程**。
        raw = self._rescue_schema_collapse(raw, step)
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
                            "⚠️ 你上一步想执行 extract 动作，但 extracted_data 是 null/空数组/空对象。\n"
                            "系统已自动尝试提取并向下滚动加载新内容。\n"
                            "【本步要求】如果页面有新内容可见，请执行 extract 并填充数据；\n"
                            "如果视口内容未变化，请执行 smooth_scroll(type_value='down') 向下滚动加载更多。"
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
        # 同 _validate_batch：进入校验前先尝试救援 schema collapse
        rescued = self._rescue_schema_collapse(raw, step)
        if isinstance(rescued, dict) and "actions" in rescued:
            # 救援把单 dict 升格成了 batch 形式（含一个 extract action）
            actions_list = rescued.get("actions") or []
            if actions_list:
                raw = actions_list[0]
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

    # 数据行的"特征字段"：出现 ≥2 个就视为 extracted_data 单行而非 VSpiderAction
    _DATA_ROW_HINTS: ClassVar[set[str]] = {
        "title", "name", "url", "link", "href", "rank", "score", "rating",
        "price", "points", "votes", "comments", "author", "uploader", "summary",
        "description", "time", "date", "views", "count", "category",
    }
    _ACTION_REQUIRED_KEYS: ClassVar[set[str]] = {"action", "actions"}

    def _rescue_schema_collapse(self, raw: Any, step: int) -> dict:
        """救援 VLM schema-collapse：把裸数据行/裸数据列表包装成合法 extract action。

        典型故障（qwen3-vl-flash 等弱模型在长 prompt 下偶发）：
          {"title": "...", "url": "...", "points": "..."}   ← 单行 extract data
        或：
          [{"title": "..."}, {"title": "..."}]              ← extract data list
        缺 action/memory_key → Pydantic 校验失败 → 任务卡死在 error 重试循环。

        识别规则：
          1. 顶层是 list[dict] 且每项含 _DATA_ROW_HINTS 中 ≥2 字段 → 包装
          2. 顶层是 dict 且不含 action/actions 但含 _DATA_ROW_HINTS ≥2 字段 → 包装

        包装后返回：{"actions": [{"action":"extract","target_id":0,"type_value":"",
                                  "memory_key":"","extracted_data":<orig>,"thought":...}]}
        """
        def _looks_like_data_row(obj: Any) -> bool:
            if not isinstance(obj, dict):
                return False
            keys = {str(k).lower() for k in obj.keys()}
            if keys & self._ACTION_REQUIRED_KEYS:
                return False
            return len(keys & self._DATA_ROW_HINTS) >= 2

        # case 1: list[dict] of data rows
        if isinstance(raw, list) and raw and all(_looks_like_data_row(r) for r in raw):
            logger.warning(
                f"[步骤 {step}] [SCHEMA RESCUE] VLM 返回了裸数据列表（{len(raw)} 行），"
                "自动包装为 extract action"
            )
            self.inject_error_feedback(
                "🩹【系统救援】上一步你直接返回了 extract 数据行而没有 action 包裹层。"
                "系统已自动救援为 {actions:[{action:'extract', extracted_data:<你的数据>, ...}]}。"
                "**下一步必须严格按 Schema 输出**：JSON 顶层是 {actions:[{action,target_id,"
                "type_value,memory_key,extracted_data,thought,...}]}，不要省略包裹层。"
            )
            return {
                "actions": [{
                    "action": "extract",
                    "target_id": 0,
                    "type_value": "",
                    "memory_key": "",
                    "extracted_data": raw,
                    "thought": "[SCHEMA RESCUE] VLM 返回裸数据列表，系统自动包装",
                    "progress_review": "（系统救援包装）",
                    "current_state": "VLM schema-collapse rescued",
                    "status": "success",
                }]
            }

        # case 2: dict that looks like a single data row
        if _looks_like_data_row(raw):
            logger.warning(
                f"[步骤 {step}] [SCHEMA RESCUE] VLM 返回了裸数据单行，自动包装为 extract action"
            )
            self.inject_error_feedback(
                "🩹【系统救援】上一步你直接返回了 extract 数据行而没有 action 包裹层。"
                "系统已自动救援。**下一步必须严格按 Schema 输出**：JSON 顶层是 "
                "{actions:[{action,target_id,type_value,memory_key,extracted_data,...}]}，"
                "不要省略包裹层。"
            )
            return {
                "actions": [{
                    "action": "extract",
                    "target_id": 0,
                    "type_value": "",
                    "memory_key": "",
                    "extracted_data": [raw],
                    "thought": "[SCHEMA RESCUE] VLM 返回裸数据单行，系统自动包装",
                    "progress_review": "（系统救援包装）",
                    "current_state": "VLM schema-collapse rescued",
                    "status": "success",
                }]
            }

        return raw if isinstance(raw, dict) else {}

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


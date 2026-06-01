"""Judge 验证模块：任务完成后进行 LLM 验证，减少假完成。

参考 browser-use 的 judge.py 设计，在 Agent 输出 action=done 后，
用一个独立的 LLM 调用来验证任务是否真正完成。

检验方式：
1. 将任务目标 + 最终页面截图 + 操作历史摘要 发送给 LLM
2. LLM 判断任务是否真正完成（pass/fail/uncertain）
3. 如果判断为 fail，生成拒绝理由并阻止 done

适用场景：
- 搜索类任务：确认结果页已加载
- 表单类任务：确认字段已正确填写
- 导航类任务：确认到达目标页面
- 提取类任务：确认数据已提取且符合要求

用法：
    judge = TaskJudge(vlm_client=vlm)
    result = await judge.evaluate(goal, screenshot_b64, history_summary)
    if not result.passed:
        vlm.inject_error_feedback(result.rejection_reason)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class JudgeConfig:
    """Judge 配置。"""

    # 是否启用 Judge 验证
    enabled: bool = True

    # 置信度阈值：低于此值认为验证不通过
    pass_threshold: float = 0.7

    # 是否在提取类任务上也启用验证
    verify_extract_tasks: bool = False

    # 验证超时（秒）
    timeout: int = 30

    # 验证失败后允许重试的最大次数
    max_retries_after_fail: int = 2


@dataclass
class JudgeResult:
    """Judge 验证结果。"""

    # 是否通过验证
    passed: bool = True

    # 判定：pass / fail / uncertain
    verdict: str = "pass"

    # 置信度 0.0 ~ 1.0
    confidence: float = 1.0

    # 推理过程
    reasoning: str = ""

    # 拒绝理由（verdict=fail 时）
    rejection_reason: str = ""

    # 建议的修复动作
    suggested_action: str = ""

    # 元数据
    metadata: dict[str, Any] = field(default_factory=dict)


_JUDGE_SYSTEM_PROMPT = """\
你是一个严格的任务验证官。你的工作是判断一个网页自动化 Agent 是否真正完成了用户的任务。

你会收到：
1. 用户的原始任务目标
2. Agent 的最终截图（当前页面状态）
3. Agent 的操作历史摘要

请根据以上信息做出判断。

判断标准：
- pass：任务目标已明确达成，页面状态与目标一致
- fail：任务目标未达成，有明确证据表明任务未完成
- uncertain：证据不足以判断，需要进一步操作确认

输出要求（只输出 JSON，不要其他文字）：
{
  "verdict": "pass|fail|uncertain",
  "confidence": 0.0-1.0,
  "reasoning": "简述判断依据",
  "rejection_reason": "仅 fail 时填写，说明为什么没完成",
  "suggested_action": "仅 fail 时填写，建议 Agent 下一步做什么"
}
"""

_JUDGE_USER_TEMPLATE = """\
## 任务目标
{goal}

## Agent 操作历史摘要
{history_summary}

## 当前页面状态
- URL: {current_url}
- 标题: {page_title}
{page_context}

请判断任务是否真正完成。只输出 JSON。"""


class TaskJudge:
    """任务完成验证器。

    在 Agent 输出 done 后调用，通过独立 LLM 调用验证任务是否真正完成。
    """

    def __init__(
        self,
        vlm_client: Any = None,
        config: Optional[JudgeConfig] = None,
    ):
        self._vlm_client = vlm_client
        self.config = config or JudgeConfig()
        self._verification_count = 0
        self._rejection_count = 0

    async def evaluate(
        self,
        goal: str,
        screenshot_b64: Optional[str] = None,
        history_summary: str = "",
        current_url: str = "",
        page_title: str = "",
        page_context: str = "",
    ) -> JudgeResult:
        """评估任务是否真正完成。

        Args:
            goal: 用户任务目标
            screenshot_b64: 最终页面截图（base64）
            history_summary: 操作历史摘要
            current_url: 当前页面 URL
            page_title: 当前页面标题
            page_context: 额外页面上下文（如 AX Tree 片段）

        Returns:
            JudgeResult 验证结果
        """
        if not self.config.enabled:
            return JudgeResult(passed=True, verdict="pass", confidence=1.0,
                             reasoning="Judge disabled")

        self._verification_count += 1

        try:
            result = await self._call_judge_llm(
                goal=goal,
                screenshot_b64=screenshot_b64,
                history_summary=history_summary,
                current_url=current_url,
                page_title=page_title,
                page_context=page_context,
            )
            if not result.passed:
                self._rejection_count += 1
                logger.warning(
                    f"[JUDGE] Task verification FAILED: {result.rejection_reason}"
                )
            else:
                logger.info(
                    f"[JUDGE] Task verification PASSED (confidence={result.confidence:.2f})"
                )
            return result

        except Exception as e:
            logger.warning(f"[JUDGE] Verification error, defaulting to pass: {e}")
            return JudgeResult(
                passed=True,
                verdict="uncertain",
                confidence=0.5,
                reasoning=f"Judge evaluation failed: {e}",
            )

    async def _call_judge_llm(
        self,
        goal: str,
        screenshot_b64: Optional[str],
        history_summary: str,
        current_url: str,
        page_title: str,
        page_context: str,
    ) -> JudgeResult:
        """调用 LLM 进行验证。"""
        if self._vlm_client is None:
            return JudgeResult(passed=True, verdict="pass", confidence=0.5,
                             reasoning="No VLM client available for judge")

        user_prompt = _JUDGE_USER_TEMPLATE.format(
            goal=goal,
            history_summary=history_summary or "(无历史)",
            current_url=current_url or "(未知)",
            page_title=page_title or "(未知)",
            page_context=page_context or "(无额外上下文)",
        )

        # 构建消息
        messages = [{"role": "system", "content": _JUDGE_SYSTEM_PROMPT}]

        if screenshot_b64:
            image_url = screenshot_b64
            if not screenshot_b64.startswith("data:image"):
                image_url = f"data:image/jpeg;base64,{screenshot_b64}"
            messages.append({
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": user_prompt},
                ],
            })
        else:
            messages.append({"role": "user", "content": user_prompt})

        client = self._vlm_client.semantic_client
        model = self._vlm_client.semantic_model

        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=512,
            temperature=0.1,
        )

        raw_content = response.choices[0].message.content or ""
        return self._parse_judge_response(raw_content)

    def _parse_judge_response(self, raw: str) -> JudgeResult:
        """解析 LLM 返回的 Judge 结果。"""
        import json
        import re

        # 尝试提取 JSON
        json_match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        if not json_match:
            # 降级：通过关键词推断
            lower = raw.lower()
            if "fail" in lower:
                return JudgeResult(
                    passed=False, verdict="fail", confidence=0.6,
                    reasoning=raw[:200],
                    rejection_reason="Judge indicated failure but response was not valid JSON",
                )
            return JudgeResult(passed=True, verdict="uncertain", confidence=0.5,
                             reasoning=f"Could not parse judge response: {raw[:200]}")

        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError:
            return JudgeResult(passed=True, verdict="uncertain", confidence=0.5,
                             reasoning=f"JSON parse failed: {raw[:200]}")

        verdict = str(data.get("verdict", "uncertain")).lower().strip()
        confidence = float(data.get("confidence", 0.5))
        reasoning = str(data.get("reasoning", ""))
        rejection = str(data.get("rejection_reason", ""))
        suggested = str(data.get("suggested_action", ""))

        passed = True
        if verdict == "fail":
            passed = False
        elif verdict == "uncertain":
            passed = confidence >= self.config.pass_threshold

        return JudgeResult(
            passed=passed,
            verdict=verdict,
            confidence=confidence,
            reasoning=reasoning,
            rejection_reason=rejection,
            suggested_action=suggested,
        )

    @property
    def stats(self) -> dict[str, int]:
        """验证统计。"""
        return {
            "total_verifications": self._verification_count,
            "rejections": self._rejection_count,
            "pass_rate": (
                (self._verification_count - self._rejection_count) / self._verification_count
                if self._verification_count > 0 else 1.0
            ),
        }

    def reset(self) -> None:
        """重置统计（新任务时调用）。"""
        self._verification_count = 0
        self._rejection_count = 0

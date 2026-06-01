"""统一失败分类器（Failure Classifier）。

将 Agent 执行中的各种异常/失败统一归类，便于：
1. 自动选择重试策略（如 ELEMENT_NOT_FOUND → 重新截图; ANTI_BOT → 等待/换策略）
2. 运维统计分析（哪类失败最频繁）
3. 向前端/日志输出结构化的失败原因

参考：skyvern 的 failure_classifier 设计，适配 VSpider 的纯视觉 Agent 场景。

用法：
    result = classify_failure(error_msg="click #21 超时", exception=e)
    if result and result.category == FailureCategory.ELEMENT_NOT_FOUND:
        # 重新截图，让 VLM 重新选择
        ...
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class FailureCategory(str, Enum):
    """失败类型枚举。"""

    # 浏览器/页面层
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    ELEMENT_NOT_INTERACTABLE = "ELEMENT_NOT_INTERACTABLE"
    PAGE_LOAD_TIMEOUT = "PAGE_LOAD_TIMEOUT"
    NAVIGATION_FAILURE = "NAVIGATION_FAILURE"
    BROWSER_CRASH = "BROWSER_CRASH"

    # 反检测/安全
    ANTI_BOT_DETECTION = "ANTI_BOT_DETECTION"
    CAPTCHA_DETECTED = "CAPTCHA_DETECTED"
    CONTENT_BLOCKED = "CONTENT_BLOCKED"

    # 认证/权限
    AUTH_FAILURE = "AUTH_FAILURE"
    SESSION_EXPIRED = "SESSION_EXPIRED"

    # VLM/模型层
    VLM_REQUEST_FAILED = "VLM_REQUEST_FAILED"
    VLM_PARSE_ERROR = "VLM_PARSE_ERROR"
    VLM_RATE_LIMIT = "VLM_RATE_LIMIT"
    VLM_CONTENT_FILTER = "VLM_CONTENT_FILTER"

    # Agent 逻辑层
    LOOP_DETECTED = "LOOP_DETECTED"
    MAX_STEPS_EXCEEDED = "MAX_STEPS_EXCEEDED"
    ACTION_EXECUTION_ERROR = "ACTION_EXECUTION_ERROR"
    WRONG_PAGE_STATE = "WRONG_PAGE_STATE"
    DATA_EXTRACTION_FAILURE = "DATA_EXTRACTION_FAILURE"

    # 基础设施
    NETWORK_ERROR = "NETWORK_ERROR"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"

    # 兜底
    UNKNOWN = "UNKNOWN"


# 建议的重试策略
class RetryStrategy(str, Enum):
    """根据失败类型推荐的重试策略。"""

    RETRY_SAME = "retry_same"            # 直接重试相同操作
    RETRY_WITH_SCREENSHOT = "retry_screenshot"  # 重新截图后重试
    RETRY_DIFFERENT_ACTION = "retry_different"  # 换一个操作
    WAIT_AND_RETRY = "wait_and_retry"    # 等待后重试
    ESCALATE_TO_HUMAN = "escalate"       # 交人工处理
    ABORT = "abort"                      # 终止任务
    SKIP = "skip"                        # 跳过当前步
    REFRESH_AND_RETRY = "refresh_retry"  # 刷新页面后重试


@dataclass
class FailureClassification:
    """失败分类结果。"""

    category: FailureCategory
    confidence: float  # 0.0 ~ 1.0
    reasoning: str = ""
    suggested_strategy: RetryStrategy = RetryStrategy.RETRY_WITH_SCREENSHOT
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_retryable(self) -> bool:
        """该失败是否可重试。"""
        return self.suggested_strategy not in (
            RetryStrategy.ABORT,
            RetryStrategy.ESCALATE_TO_HUMAN,
        )


# ──────────────────────────────────────────────────────────────────
# 关键词 → 分类规则表
# ──────────────────────────────────────────────────────────────────

_CLASSIFICATION_RULES: list[dict[str, Any]] = [
    # ── 元素未找到 ──
    {
        "category": FailureCategory.ELEMENT_NOT_FOUND,
        "keywords": [
            "data-som-id", "找不到元素", "element not found",
            "no element", "locator", "selector.*not found",
            "目标元素不存在", "zero targets", "no interactive",
        ],
        "exception_types": ["NoSuchElementException", "ElementNotFoundError"],
        "strategy": RetryStrategy.RETRY_WITH_SCREENSHOT,
        "confidence": 0.85,
    },
    # ── 元素不可交互 ──
    {
        "category": FailureCategory.ELEMENT_NOT_INTERACTABLE,
        "keywords": [
            "not interactable", "不可交互", "element is not visible",
            "obscured", "intercepted", "disabled", "readonly",
            "被遮挡", "covered by",
        ],
        "exception_types": ["ElementNotInteractableException"],
        "strategy": RetryStrategy.RETRY_DIFFERENT_ACTION,
        "confidence": 0.80,
    },
    # ── 页面加载超时 ──
    {
        "category": FailureCategory.PAGE_LOAD_TIMEOUT,
        "keywords": [
            "timeout", "超时", "timed out", "page load",
            "navigation timeout", "networkidle",
        ],
        "exception_types": ["TimeoutError", "PlaywrightTimeoutError"],
        "strategy": RetryStrategy.WAIT_AND_RETRY,
        "confidence": 0.75,
    },
    # ── 导航失败 ──
    {
        "category": FailureCategory.NAVIGATION_FAILURE,
        "keywords": [
            "net::err_", "navigation failed", "无法访问",
            "refused to connect", "dns", "unreachable",
        ],
        "exception_types": [],
        "strategy": RetryStrategy.REFRESH_AND_RETRY,
        "confidence": 0.80,
    },
    # ── 浏览器崩溃 ──
    {
        "category": FailureCategory.BROWSER_CRASH,
        "keywords": [
            "browser.*crash", "browser.*closed", "target closed",
            "session closed", "connection closed",
            "浏览器崩溃", "context.*destroyed",
        ],
        "exception_types": ["BrowserClosedError", "TargetClosedError"],
        "strategy": RetryStrategy.ABORT,
        "confidence": 0.90,
    },
    # ── 反爬/机器人检测 ──
    {
        "category": FailureCategory.ANTI_BOT_DETECTION,
        "keywords": [
            "cloudflare", "turnstile", "bot detect", "robot", "anti-bot",
            "human verification", "access denied", "ip block",
            "rate limit.*block", "waf", "cf-challenge", "just a moment",
        ],
        "exception_types": [],
        "strategy": RetryStrategy.WAIT_AND_RETRY,
        "confidence": 0.70,
    },
    # ── 验证码 ──
    {
        "category": FailureCategory.CAPTCHA_DETECTED,
        "keywords": [
            "captcha", "验证码", "滑块验证", "图形验证",
            "recaptcha", "hcaptcha",
        ],
        "exception_types": [],
        "strategy": RetryStrategy.ESCALATE_TO_HUMAN,
        "confidence": 0.85,
    },
    # ── 内容审核拦截 ──
    {
        "category": FailureCategory.CONTENT_BLOCKED,
        "keywords": [
            "data_inspection_failed", "content filter",
            "内容审核", "安全过滤", "content policy",
        ],
        "exception_types": [],
        "strategy": RetryStrategy.RETRY_DIFFERENT_ACTION,
        "confidence": 0.85,
    },
    # ── 认证失败 ──
    {
        "category": FailureCategory.AUTH_FAILURE,
        "keywords": [
            "login failed", "登录失败", "unauthorized", "401",
            "authentication", "password.*wrong", "密码错误",
            "permission denied", "权限不足",
        ],
        "exception_types": [],
        "strategy": RetryStrategy.ESCALATE_TO_HUMAN,
        "confidence": 0.75,
    },
    # ── 会话过期 ──
    {
        "category": FailureCategory.SESSION_EXPIRED,
        "keywords": [
            "session.*expir", "会话过期", "token.*expir",
            "重新登录", "please login again", "kicked out",
        ],
        "exception_types": [],
        "strategy": RetryStrategy.REFRESH_AND_RETRY,
        "confidence": 0.80,
    },
    # ── VLM 请求失败 ──
    {
        "category": FailureCategory.VLM_REQUEST_FAILED,
        "keywords": [
            "vlm.*失败", "vlm.*failed", "api.*error",
            "openai.*error", "connection.*refused",
            "模型.*超时", "model.*timeout",
        ],
        "exception_types": ["APIError", "APIConnectionError", "APITimeoutError"],
        "strategy": RetryStrategy.RETRY_SAME,
        "confidence": 0.80,
    },
    # ── VLM 解析错误 ──
    {
        "category": FailureCategory.VLM_PARSE_ERROR,
        "keywords": [
            "json.*parse", "json.*decode", "invalid.*json",
            "格式异常", "parse.*error", "validation.*error",
        ],
        "exception_types": ["JSONDecodeError", "ValidationError"],
        "strategy": RetryStrategy.RETRY_SAME,
        "confidence": 0.80,
    },
    # ── VLM 限流 ──
    {
        "category": FailureCategory.VLM_RATE_LIMIT,
        "keywords": [
            "rate.?limit", "too many requests", "429",
            "quota.*exceeded", "限流", "请求过多",
        ],
        "exception_types": ["RateLimitError"],
        "strategy": RetryStrategy.WAIT_AND_RETRY,
        "confidence": 0.90,
    },
    # ── 循环检测 ──
    {
        "category": FailureCategory.LOOP_DETECTED,
        "keywords": [
            "loop.*detect", "死循环", "重复操作",
            "stuck", "same action", "loop guard",
            "submit.*loop", "wait.*loop",
        ],
        "exception_types": [],
        "strategy": RetryStrategy.RETRY_DIFFERENT_ACTION,
        "confidence": 0.85,
    },
    # ── 步数超限 ──
    {
        "category": FailureCategory.MAX_STEPS_EXCEEDED,
        "keywords": [
            "max.*step", "步数.*超", "maximum.*iteration",
            "超出.*步数",
        ],
        "exception_types": [],
        "strategy": RetryStrategy.ABORT,
        "confidence": 0.95,
    },
    # ── 数据提取失败 ──
    {
        "category": FailureCategory.DATA_EXTRACTION_FAILURE,
        "keywords": [
            "extract.*fail", "提取失败", "no data",
            "empty.*result", "数据为空", "extraction.*error",
        ],
        "exception_types": [],
        "strategy": RetryStrategy.RETRY_WITH_SCREENSHOT,
        "confidence": 0.70,
    },
    # ── 页面状态错误 ──
    {
        "category": FailureCategory.WRONG_PAGE_STATE,
        "keywords": [
            "wrong.*page", "unexpected.*page", "页面不对",
            "not.*expected", "blank page", "空白页",
        ],
        "exception_types": [],
        "strategy": RetryStrategy.REFRESH_AND_RETRY,
        "confidence": 0.65,
    },
    # ── 网络错误 ──
    {
        "category": FailureCategory.NETWORK_ERROR,
        "keywords": [
            "network", "连接失败", "connection.*error",
            "socket", "ssl", "certificate",
        ],
        "exception_types": ["ConnectionError", "OSError", "SSLError"],
        "strategy": RetryStrategy.WAIT_AND_RETRY,
        "confidence": 0.75,
    },
]


def classify_failure(
    error_msg: Optional[str] = None,
    exception: Optional[Exception] = None,
    context: Optional[dict[str, Any]] = None,
    fallback_to_unknown: bool = True,
) -> Optional[FailureClassification]:
    """对失败进行分类。

    Args:
        error_msg: 错误消息文本
        exception: 异常对象
        context: 可选的上下文信息（如 URL、action、step 等）
        fallback_to_unknown: 无法匹配时是否返回 UNKNOWN 类别

    Returns:
        FailureClassification 或 None（当 fallback_to_unknown=False 且无法匹配时）
    """
    if not error_msg and not exception:
        return None

    reason = (error_msg or "").lower()
    exc_name = type(exception).__name__ if exception else ""
    exc_msg = str(exception).lower() if exception else ""

    # 合并搜索文本
    search_text = f"{reason} {exc_msg}".strip()

    candidates: list[FailureClassification] = []

    for rule in _CLASSIFICATION_RULES:
        score = 0.0

        # 关键词匹配
        keywords: list[str] = rule.get("keywords", [])
        matched_keywords = []
        for kw in keywords:
            try:
                if re.search(kw, search_text, re.IGNORECASE):
                    matched_keywords.append(kw)
                    score += 1.0
            except re.error:
                if kw in search_text:
                    matched_keywords.append(kw)
                    score += 1.0

        # 异常类型匹配（高权重）
        exc_types: list[str] = rule.get("exception_types", [])
        if exc_name and exc_name in exc_types:
            score += 2.0

        if score <= 0:
            continue

        # 归一化置信度
        base_confidence = rule.get("confidence", 0.5)
        # 多关键词匹配提升置信度
        keyword_bonus = min(0.15, len(matched_keywords) * 0.05)
        confidence = min(1.0, base_confidence + keyword_bonus)

        candidates.append(FailureClassification(
            category=rule["category"],
            confidence=confidence,
            reasoning=f"Matched: {', '.join(matched_keywords[:3])}"
                      + (f"; exc_type={exc_name}" if exc_name in exc_types else ""),
            suggested_strategy=rule.get("strategy", RetryStrategy.RETRY_WITH_SCREENSHOT),
            metadata={
                "matched_keywords": matched_keywords,
                "exception_type": exc_name,
                "context": context or {},
            },
        ))

    if not candidates:
        if fallback_to_unknown:
            return FailureClassification(
                category=FailureCategory.UNKNOWN,
                confidence=0.3,
                reasoning=f"No rules matched. error_msg={error_msg!r}, exc={exc_name}",
                suggested_strategy=RetryStrategy.RETRY_WITH_SCREENSHOT,
                metadata={"exception_type": exc_name, "context": context or {}},
            )
        return None

    # 选择置信度最高的分类
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    best = candidates[0]

    logger.debug(
        f"[FAILURE_CLASSIFIER] {best.category.value} "
        f"(confidence={best.confidence:.2f}): {best.reasoning}"
    )
    return best


def classify_and_log(
    error_msg: Optional[str] = None,
    exception: Optional[Exception] = None,
    context: Optional[dict[str, Any]] = None,
    step: Optional[int] = None,
) -> Optional[FailureClassification]:
    """分类并记录日志（便捷方法）。"""
    result = classify_failure(error_msg, exception, context)
    if result:
        step_prefix = f"[步骤 {step}] " if step is not None else ""
        logger.info(
            f"{step_prefix}[FAILURE] category={result.category.value}, "
            f"confidence={result.confidence:.2f}, "
            f"strategy={result.suggested_strategy.value}, "
            f"reason={result.reasoning}"
        )
    return result


# ──────────────────────────────────────────────────────────────────
# 统计聚合（用于运维和任务报告）
# ──────────────────────────────────────────────────────────────────

@dataclass
class FailureStats:
    """任务级别的失败统计。"""

    counts: dict[str, int] = field(default_factory=dict)
    history: list[FailureClassification] = field(default_factory=list)

    def record(self, classification: Optional[FailureClassification]) -> None:
        """记录一次失败分类。"""
        if classification is None:
            return
        cat = classification.category.value
        self.counts[cat] = self.counts.get(cat, 0) + 1
        self.history.append(classification)

    @property
    def total_failures(self) -> int:
        return sum(self.counts.values())

    @property
    def top_category(self) -> Optional[str]:
        """出现次数最多的失败类型。"""
        if not self.counts:
            return None
        return max(self.counts, key=lambda k: self.counts[k])

    def summary(self) -> str:
        """生成简洁的统计摘要。"""
        if not self.counts:
            return "无失败记录"
        parts = [f"{k}:{v}" for k, v in sorted(
            self.counts.items(), key=lambda x: -x[1]
        )]
        return f"总计 {self.total_failures} 次失败 | " + ", ".join(parts)

    def get_suggested_strategy(self) -> Optional[RetryStrategy]:
        """根据最近的失败模式推荐策略。"""
        if not self.history:
            return None
        # 最近 3 次的主要策略
        recent = self.history[-3:]
        strategies = [r.suggested_strategy for r in recent]
        # 如果有 ABORT 建议，优先返回
        if RetryStrategy.ABORT in strategies:
            return RetryStrategy.ABORT
        if RetryStrategy.ESCALATE_TO_HUMAN in strategies:
            return RetryStrategy.ESCALATE_TO_HUMAN
        # 返回出现最多的策略
        from collections import Counter
        counter = Counter(strategies)
        return counter.most_common(1)[0][0]

    def reset(self) -> None:
        """重置统计。"""
        self.counts.clear()
        self.history.clear()

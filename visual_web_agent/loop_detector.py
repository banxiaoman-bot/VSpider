"""统一循环检测器（Action Loop Detector）。

基于 browser-use 的 ActionLoopDetector 设计，对 VSpider 做适配。
通过 action hash + 页面指纹双重检测来识别 Agent 陷入的行为循环。

检测维度：
1. **Action 重复检测**：同一 (action, target_id, type_value) 在窗口内超过阈值。
2. **页面指纹停滞检测**：连续多步页面指纹不变（URL + 标题 + 元素数 hash）。
3. **输出模式检测**：VLM 反复输出相同/相似 thought，暗示陷入死循环。

与 main.py 现有的 LOOP GUARD 系统互补：
- 现有 LOOP GUARD 基于 (action, target_id, point, url) tuple 计数
- 本模块额外引入页面指纹停滞 + thought 相似度，捕获更隐蔽的循环

用法：
    detector = ActionLoopDetector()
    result = detector.check(decision, page_fingerprint, step)
    if result.loop_detected:
        vlm.inject_error_feedback(result.nudge_message)
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class PageFingerprint:
    """页面状态轻量指纹。"""

    url: str = ""
    title: str = ""
    element_count: int = 0
    scroll_position: int = 0

    @property
    def hash(self) -> str:
        """生成指纹 hash（用于快速比较）。"""
        raw = f"{self.url}|{self.title}|{self.element_count}|{self.scroll_position}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PageFingerprint):
            return NotImplemented
        return self.hash == other.hash


@dataclass
class LoopDetectorConfig:
    """循环检测器配置。"""

    # 检测窗口大小（最近 N 步）
    window_size: int = 8

    # 同一 action hash 重复次数阈值（超过则认为循环）
    action_repeat_threshold: int = 3

    # 页面指纹连续相同的步数阈值
    stagnation_threshold: int = 4

    # thought 相似度阈值（0.0~1.0，超过此值认为相似）
    thought_similarity_threshold: float = 0.8

    # 是否启用 thought 相似度检测
    enable_thought_detection: bool = True


@dataclass
class LoopCheckResult:
    """循环检测结果。"""

    loop_detected: bool = False
    loop_type: str = ""  # "action_repeat" | "page_stagnation" | "thought_loop"
    confidence: float = 0.0
    nudge_message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ActionEntry:
    """内部：单步动作记录。"""
    step: int
    action_hash: str
    thought_hash: str
    thought_text: str
    page_fingerprint_hash: str


class ActionLoopDetector:
    """Agent 行为循环检测器。

    每步调用 check() 传入决策和页面指纹，返回是否检测到循环。
    """

    def __init__(self, config: Optional[LoopDetectorConfig] = None):
        self.config = config or LoopDetectorConfig()
        self._history: list[_ActionEntry] = []
        self._nudge_count: int = 0  # 已发出的 nudge 次数

    def check(
        self,
        decision: dict[str, Any],
        page_fingerprint: Optional[PageFingerprint] = None,
        step: int = 0,
    ) -> LoopCheckResult:
        """检测当前决策是否构成循环。

        Args:
            decision: VLM 输出的决策 dict
            page_fingerprint: 当前页面指纹
            step: 当前步数

        Returns:
            LoopCheckResult 描述检测结果
        """
        # 计算当前动作 hash
        action_hash = self._hash_action(decision)
        thought_text = (decision.get("thought") or "")[:200]
        thought_hash = hashlib.md5(thought_text.encode()).hexdigest()[:8]
        fp_hash = page_fingerprint.hash if page_fingerprint else ""

        entry = _ActionEntry(
            step=step,
            action_hash=action_hash,
            thought_hash=thought_hash,
            thought_text=thought_text,
            page_fingerprint_hash=fp_hash,
        )
        self._history.append(entry)

        # 保持窗口大小
        if len(self._history) > self.config.window_size * 2:
            self._history = self._history[-self.config.window_size:]

        window = self._history[-self.config.window_size:]
        if len(window) < 3:
            return LoopCheckResult()

        # ── 检测 1：Action 重复 ──
        result = self._check_action_repeat(window, action_hash)
        if result.loop_detected:
            return result

        # ── 检测 2：页面指纹停滞 ──
        if fp_hash:
            result = self._check_page_stagnation(window, fp_hash)
            if result.loop_detected:
                return result

        # ── 检测 3：Thought 重复 ──
        if self.config.enable_thought_detection:
            result = self._check_thought_loop(window, thought_hash, thought_text)
            if result.loop_detected:
                return result

        return LoopCheckResult()

    def _check_action_repeat(
        self, window: list[_ActionEntry], current_hash: str
    ) -> LoopCheckResult:
        """检测同一 action hash 在窗口内重复次数。"""
        repeat_count = sum(1 for e in window if e.action_hash == current_hash)
        if repeat_count >= self.config.action_repeat_threshold:
            self._nudge_count += 1
            return LoopCheckResult(
                loop_detected=True,
                loop_type="action_repeat",
                confidence=min(1.0, 0.6 + repeat_count * 0.1),
                nudge_message=(
                    f"⚠️ 循环检测：你在最近 {len(window)} 步中重复了相同操作 "
                    f"{repeat_count} 次。请立即换一个不同的策略 —— "
                    f"尝试不同的元素、不同的动作类型、或者重新评估当前页面状态。"
                    f"如果任务已完成，请直接 action=done。"
                ),
                details={
                    "repeat_count": repeat_count,
                    "action_hash": current_hash,
                    "nudge_number": self._nudge_count,
                },
            )
        return LoopCheckResult()

    def _check_page_stagnation(
        self, window: list[_ActionEntry], current_fp_hash: str
    ) -> LoopCheckResult:
        """检测页面指纹连续不变（停滞）。"""
        # 统计最近连续相同指纹的步数
        consecutive = 0
        for entry in reversed(window):
            if entry.page_fingerprint_hash == current_fp_hash:
                consecutive += 1
            else:
                break

        if consecutive >= self.config.stagnation_threshold:
            self._nudge_count += 1
            return LoopCheckResult(
                loop_detected=True,
                loop_type="page_stagnation",
                confidence=min(1.0, 0.5 + consecutive * 0.1),
                nudge_message=(
                    f"⚠️ 页面停滞检测：最近 {consecutive} 步操作后，"
                    f"页面状态（URL/标题/元素数量）没有任何变化。"
                    f"你的操作可能没有生效。请尝试：\n"
                    f"1. 确认目标元素是否真正可交互（检查 AX Tree 的 state）\n"
                    f"2. 尝试 scroll 到新区域\n"
                    f"3. 如果反复失败，考虑用 goto 或 press_key 换一种途径\n"
                    f"4. 如果目标已达成但页面没显式变化，直接 done"
                ),
                details={
                    "consecutive_steps": consecutive,
                    "fingerprint_hash": current_fp_hash,
                    "nudge_number": self._nudge_count,
                },
            )
        return LoopCheckResult()

    def _check_thought_loop(
        self, window: list[_ActionEntry], current_hash: str, current_text: str
    ) -> LoopCheckResult:
        """检测 VLM 反复输出相似 thought（思维循环）。"""
        similar_count = 0
        for entry in window[:-1]:  # 排除当前条目
            if entry.thought_hash == current_hash:
                similar_count += 1
            elif self._text_similarity(entry.thought_text, current_text) >= self.config.thought_similarity_threshold:
                similar_count += 1

        threshold = max(2, self.config.action_repeat_threshold - 1)
        if similar_count >= threshold:
            self._nudge_count += 1
            return LoopCheckResult(
                loop_detected=True,
                loop_type="thought_loop",
                confidence=min(1.0, 0.5 + similar_count * 0.15),
                nudge_message=(
                    f"⚠️ 思维循环检测：你在最近 {len(window)} 步中反复表达相同/相似的 thought，"
                    f"说明你可能陷入了固定思路。请跳出当前思维框架：\n"
                    f"- 仔细重新阅读截图上的所有元素\n"
                    f"- 考虑之前忽略的操作路径\n"
                    f"- 如果页面状态已经满足目标，直接 done"
                ),
                details={
                    "similar_count": similar_count,
                    "nudge_number": self._nudge_count,
                },
            )
        return LoopCheckResult()

    @staticmethod
    def _hash_action(decision: dict[str, Any]) -> str:
        """生成动作的可比较 hash。"""
        action = str(decision.get("action") or "")
        target_id = str(decision.get("target_id") or "0")
        type_value = str(decision.get("type_value") or "")[:50]
        raw = f"{action}|{target_id}|{type_value}"
        return hashlib.md5(raw.encode()).hexdigest()[:10]

    @staticmethod
    def _text_similarity(text1: str, text2: str) -> float:
        """简单的文本相似度（基于字符 bigram Jaccard）。"""
        if not text1 or not text2:
            return 0.0
        if text1 == text2:
            return 1.0

        def bigrams(s: str) -> set[str]:
            return {s[i:i+2] for i in range(len(s) - 1)} if len(s) > 1 else {s}

        b1 = bigrams(text1)
        b2 = bigrams(text2)
        intersection = len(b1 & b2)
        union = len(b1 | b2)
        return intersection / union if union > 0 else 0.0

    @property
    def total_nudges(self) -> int:
        """总共发出的循环警告次数。"""
        return self._nudge_count

    def reset(self) -> None:
        """重置检测器状态（新任务开始时调用）。"""
        self._history.clear()
        self._nudge_count = 0

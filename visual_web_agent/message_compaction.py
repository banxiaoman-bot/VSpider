"""Message Compaction: 对话历史智能压缩模块。

当 Agent 执行长任务时，操作历史会不断膨胀，最终超出 VLM 的 token 窗口。
本模块实现两层压缩策略：

1. **摘要压缩（Summary Compaction）**:
   当历史记录超过阈值时，将较早的历史用 LLM 生成一段简洁摘要，
   仅保留最近 N 条原始记录，避免 token 溢出的同时保留关键上下文。

2. **渐进式遗忘（Progressive Forgetting）**:
   摘要本身也有长度上限，超长时做二次截断，确保总开销可控。

参考：browser-use 的 MessageCompactionSettings 设计。

用法：
    compactor = MessageCompactor(vlm_client=vlm)
    # 每步结束后调用
    compacted_history = await compactor.maybe_compact(history_items)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class CompactionConfig:
    """历史压缩配置参数。"""

    # 是否启用压缩
    enabled: bool = True

    # 触发压缩的历史条目数阈值（超过此值时压缩旧条目）
    trigger_count: int = 12

    # 触发压缩的字符数阈值（历史文本超过此长度时也触发）
    trigger_char_count: int = 8000

    # 压缩后保留的最近 N 条原始记录（不会被摘要覆盖）
    keep_last_items: int = 6

    # 摘要最大字符数
    summary_max_chars: int = 2000

    # 两次压缩之间的最小步数间隔（避免频繁压缩）
    compact_cooldown_steps: int = 5


@dataclass
class CompactionState:
    """压缩状态跟踪。"""

    # 累积的摘要文本（每次压缩后追加/替换）
    summary: str = ""

    # 上次压缩时的步数
    last_compact_step: int = 0

    # 总共压缩过多少次
    compact_count: int = 0

    # 被压缩掉的历史条目总数
    compacted_item_count: int = 0


_COMPACT_SYSTEM_PROMPT = """\
你是一个专业的操作日志摘要助手。请将以下网页自动化 Agent 的操作历史浓缩为一段简洁摘要。

要求：
1. 保留关键信息：哪些页面被访问了、哪些重要操作成功/失败、当前进度。
2. 丢弃冗余细节：重复的滚动、失败后的重试细节只保留结论。
3. 输出纯文本，不超过 {max_chars} 字符。
4. 使用中文。
5. 格式：一段连续文本，不用列表。
"""

_COMPACT_USER_TEMPLATE = """\
任务目标：{goal}

{existing_summary_section}

需要压缩的操作历史（共 {count} 条）：
{history_text}

请输出一段不超过 {max_chars} 字符的摘要："""


class MessageCompactor:
    """对话历史压缩器。

    与 VLMClient 配合使用，在历史记录过长时自动触发 LLM 摘要压缩。
    """

    def __init__(
        self,
        config: Optional[CompactionConfig] = None,
        vlm_client: Any = None,
    ):
        self.config = config or CompactionConfig()
        self._vlm_client = vlm_client
        self.state = CompactionState()

    def needs_compaction(self, history: list[dict], current_step: int) -> bool:
        """判断当前历史是否需要压缩。"""
        if not self.config.enabled:
            return False

        # 冷却期检查
        if (current_step - self.state.last_compact_step) < self.config.compact_cooldown_steps:
            return False

        # 条目数阈值
        if len(history) > self.config.trigger_count:
            return True

        # 字符数阈值
        total_chars = sum(
            len(h.get("thought", "")) + len(h.get("action", ""))
            + len(str(h.get("result", "") or ""))
            for h in history
        )
        if total_chars > self.config.trigger_char_count:
            return True

        return False

    async def compact(
        self,
        history: list[dict],
        current_step: int,
        goal: str = "",
    ) -> list[dict]:
        """执行压缩：将旧历史摘要化，返回压缩后的历史列表。

        Args:
            history: 完整历史记录列表
            current_step: 当前步数
            goal: 任务目标（用于生成更精准的摘要）

        Returns:
            压缩后的历史列表（最近 keep_last_items 条保留原样）
        """
        if not self.config.enabled or len(history) <= self.config.keep_last_items:
            return history

        keep = self.config.keep_last_items
        to_compact = history[:-keep] if keep > 0 else history
        to_keep = history[-keep:] if keep > 0 else []

        # 生成摘要
        summary = await self._generate_summary(to_compact, goal)
        if summary:
            self.state.summary = summary
            self.state.last_compact_step = current_step
            self.state.compact_count += 1
            self.state.compacted_item_count += len(to_compact)
            logger.info(
                f"[COMPACTION] 压缩完成: {len(to_compact)} 条历史 → "
                f"{len(summary)} 字摘要, 保留最近 {len(to_keep)} 条"
            )

        return to_keep

    async def _generate_summary(self, items: list[dict], goal: str) -> str:
        """调用 LLM 生成历史摘要。若 LLM 不可用则降级为规则压缩。"""
        history_text = self._format_items_for_summary(items)
        max_chars = self.config.summary_max_chars

        # 尝试用 VLM 生成摘要
        if self._vlm_client is not None:
            try:
                summary = await self._llm_summarize(history_text, goal, max_chars)
                if summary:
                    return summary[:max_chars]
            except Exception as e:
                logger.warning(f"[COMPACTION] LLM 摘要失败，降级为规则压缩: {e}")

        # 降级：规则压缩（提取关键信息）
        return self._rule_based_compress(items, max_chars)

    async def _llm_summarize(self, history_text: str, goal: str, max_chars: int) -> str:
        """使用 VLM 的 semantic_client 生成摘要。"""
        existing_section = ""
        if self.state.summary:
            existing_section = f"此前已有的摘要（请整合）：\n{self.state.summary}\n"

        system_prompt = _COMPACT_SYSTEM_PROMPT.format(max_chars=max_chars)
        user_prompt = _COMPACT_USER_TEMPLATE.format(
            goal=goal or "（未指定）",
            existing_summary_section=existing_section,
            count=history_text.count("\n") + 1,
            history_text=history_text[:6000],  # 防止输入过长
            max_chars=max_chars,
        )

        client = self._vlm_client.semantic_client
        model = self._vlm_client.semantic_model

        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=1024,
            temperature=0.3,
        )

        content = response.choices[0].message.content or ""
        return content.strip()

    def _rule_based_compress(self, items: list[dict], max_chars: int) -> str:
        """规则降级压缩：保留每条的关键一行。"""
        lines = []
        if self.state.summary:
            lines.append(f"[早期摘要] {self.state.summary[:max_chars // 3]}")

        for h in items:
            result = h.get("result", "") or ""
            action = h.get("action", "")
            step = h.get("step", "?")
            thought_short = (h.get("thought", "") or "")[:40]
            line = f"步{step}:{action}"
            if result:
                # 只保留结果的成功/失败标识
                if "✅" in result or "成功" in result:
                    line += " ✅"
                elif "❌" in result or "失败" in result:
                    line += " ❌"
            if thought_short:
                line += f" ({thought_short})"
            lines.append(line)

        compressed = "\n".join(lines)
        if len(compressed) > max_chars:
            # 保留头尾
            half = max_chars // 2
            compressed = compressed[:half] + "\n...(中间省略)...\n" + compressed[-half:]
        return compressed[:max_chars]

    def _format_items_for_summary(self, items: list[dict]) -> str:
        """将历史条目格式化为适合 LLM 阅读的文本。"""
        lines = []
        for h in items:
            result = h.get("result") or "(未记录)"
            lines.append(
                f"步骤{h.get('step', '?')}: "
                f"action={h.get('action', '?')}, "
                f"target_id={h.get('target_id', 0)}, "
                f"type_value={h.get('type_value', '')!r}, "
                f"结果={result}, "
                f"思考={h.get('thought', '')[:100]}"
            )
        return "\n".join(lines)

    def get_summary_prefix(self) -> str:
        """返回当前累积摘要文本，供注入到 history_summary 中。"""
        if not self.state.summary:
            return ""
        return (
            f"## 早期操作摘要（共 {self.state.compacted_item_count} 步已压缩）\n"
            f"{self.state.summary}\n\n"
        )

    def reset(self) -> None:
        """重置压缩状态（新任务开始时调用）。"""
        self.state = CompactionState()

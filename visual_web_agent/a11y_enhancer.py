"""Accessibility Tree 增强辅助模块。

在 VSpider 现有的 SoM 截图 + AX Tree 基础上，提供额外的无障碍信息增强，
帮助 VLM 做出更准确的决策。

增强功能：
1. **元素状态标注**：为 AX Tree 中的元素补充状态信息（disabled/checked/expanded 等）
2. **语义分组**：将元素按逻辑分组（导航栏/主内容/侧边栏/表单区）
3. **焦点追踪**：标注当前焦点元素
4. **隐藏元素提示**：提示 iframe/shadow DOM 中可能遗漏的可交互元素数量
5. **元素可见性过滤**：过滤掉视口外或不可见的元素，减少噪音

参考：browser-use 的 DomService 和 WebVoyager 的 accessibility tree 融合设计。

用法：
    enhancer = A11yEnhancer()
    enhanced_text = enhancer.enhance(ax_tree_text, page_metadata)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class A11yEnhancerConfig:
    """增强器配置。"""

    # 是否启用语义分组
    enable_grouping: bool = True

    # 是否标注元素状态
    enable_state_annotation: bool = True

    # 是否提示隐藏元素数量
    enable_hidden_hints: bool = True

    # 是否过滤视口外元素
    filter_offscreen: bool = False

    # 最大输出字符数（防止 AX Tree 过长）
    max_output_chars: int = 12000

    # 是否压缩连续空白节点
    compress_whitespace: bool = True


@dataclass
class PageMetadata:
    """页面元数据（辅助增强判断）。"""

    url: str = ""
    title: str = ""
    viewport_width: int = 1280
    viewport_height: int = 800
    total_elements: int = 0
    visible_elements: int = 0
    iframe_count: int = 0
    shadow_dom_count: int = 0
    focused_element_id: Optional[int] = None
    scroll_position: int = 0
    page_height: int = 0


@dataclass
class EnhancedA11yResult:
    """增强后的 AX Tree 结果。"""

    text: str = ""
    summary: str = ""
    element_count: int = 0
    hidden_element_count: int = 0
    groups: dict[str, int] = field(default_factory=dict)


# 常见导航关键词（用于语义分组）
_NAV_KEYWORDS = frozenset({
    "navigation", "nav", "menu", "menubar", "toolbar",
    "header", "banner", "breadcrumb",
})

_MAIN_KEYWORDS = frozenset({
    "main", "article", "content", "region",
})

_FORM_KEYWORDS = frozenset({
    "form", "textbox", "searchbox", "combobox", "spinbutton",
    "checkbox", "radio", "switch", "slider",
})

_FOOTER_KEYWORDS = frozenset({
    "contentinfo", "footer", "complementary",
})


class A11yEnhancer:
    """无障碍树增强器。"""

    def __init__(self, config: Optional[A11yEnhancerConfig] = None):
        self.config = config or A11yEnhancerConfig()

    def enhance(
        self,
        ax_tree_text: str,
        page_metadata: Optional[PageMetadata] = None,
    ) -> EnhancedA11yResult:
        """增强 AX Tree 文本。

        Args:
            ax_tree_text: 原始 AX Tree 文本
            page_metadata: 页面元数据

        Returns:
            EnhancedA11yResult 增强结果
        """
        if not ax_tree_text:
            return EnhancedA11yResult()

        lines = ax_tree_text.split("\n")
        enhanced_lines: list[str] = []
        groups: dict[str, int] = {}
        element_count = 0

        for line in lines:
            processed_line = line

            # 统计元素
            if re.search(r"\[ID:\s*\d+\]|@e\d+", line):
                element_count += 1

            # 语义分组标注
            if self.config.enable_grouping:
                group = self._classify_line(line)
                if group:
                    groups[group] = groups.get(group, 0) + 1

            # 状态标注增强
            if self.config.enable_state_annotation:
                processed_line = self._annotate_states(processed_line)

            # 压缩空白
            if self.config.compress_whitespace and not processed_line.strip():
                if enhanced_lines and not enhanced_lines[-1].strip():
                    continue  # 跳过连续空行

            enhanced_lines.append(processed_line)

        # 构建最终输出
        result_text = "\n".join(enhanced_lines)

        # 添加隐藏元素提示
        hidden_count = 0
        if self.config.enable_hidden_hints and page_metadata:
            hidden_count = max(
                0, page_metadata.total_elements - page_metadata.visible_elements
            )
            if hidden_count > 0 or page_metadata.iframe_count > 0:
                hints = self._build_hidden_hints(page_metadata, hidden_count)
                result_text = result_text + "\n" + hints

        # 截断过长文本
        if len(result_text) > self.config.max_output_chars:
            result_text = self._smart_truncate(result_text, self.config.max_output_chars)

        # 生成摘要
        summary = self._build_summary(element_count, groups, page_metadata)

        return EnhancedA11yResult(
            text=result_text,
            summary=summary,
            element_count=element_count,
            hidden_element_count=hidden_count,
            groups=groups,
        )

    def build_viewport_context(
        self,
        page_metadata: PageMetadata,
    ) -> str:
        """生成视口上下文提示（告知 VLM 当前可见区域的位置）。

        帮助 VLM 理解页面的滚动状态，判断是否需要 scroll。
        """
        if not page_metadata.page_height:
            return ""

        scroll_pct = 0
        if page_metadata.page_height > page_metadata.viewport_height:
            scroll_pct = int(
                page_metadata.scroll_position
                / (page_metadata.page_height - page_metadata.viewport_height)
                * 100
            )
            scroll_pct = max(0, min(100, scroll_pct))

        at_top = page_metadata.scroll_position <= 10
        at_bottom = (
            page_metadata.scroll_position + page_metadata.viewport_height
            >= page_metadata.page_height - 10
        )

        if at_top:
            position = "顶部"
        elif at_bottom:
            position = "底部"
        else:
            position = f"中部 ({scroll_pct}%)"

        return (
            f"📍 视口位置: {position} | "
            f"页面高度: {page_metadata.page_height}px | "
            f"可见区域: {page_metadata.viewport_height}px"
        )

    def _classify_line(self, line: str) -> str:
        """将 AX Tree 行分类到语义组。"""
        lower = line.lower()
        role_match = re.search(r"role:\s*(\w+)", lower)
        role = role_match.group(1) if role_match else ""

        if role in _NAV_KEYWORDS or any(kw in lower for kw in ("navigation", "nav", "menu")):
            return "navigation"
        if role in _MAIN_KEYWORDS or any(kw in lower for kw in ("main", "article")):
            return "main_content"
        if role in _FORM_KEYWORDS or any(kw in lower for kw in ("textbox", "input", "form")):
            return "form"
        if role in _FOOTER_KEYWORDS or "footer" in lower:
            return "footer"
        return ""

    def _annotate_states(self, line: str) -> str:
        """为元素行添加状态标注。"""
        annotations = []

        # 检测 disabled 状态
        if re.search(r"\bdisabled\b", line, re.IGNORECASE):
            annotations.append("🚫disabled")

        # 检测 checked 状态
        if re.search(r"\bchecked\b", line, re.IGNORECASE):
            annotations.append("☑checked")

        # 检测 expanded 状态
        if re.search(r"\bexpanded\b", line, re.IGNORECASE):
            annotations.append("▼expanded")

        # 检测 selected 状态
        if re.search(r"\bselected\b", line, re.IGNORECASE):
            annotations.append("★selected")

        # 检测 required 状态
        if re.search(r"\brequired\b", line, re.IGNORECASE):
            annotations.append("*required")

        if annotations and line.strip():
            return f"{line}  [{' '.join(annotations)}]"
        return line

    def _build_hidden_hints(self, meta: PageMetadata, hidden_count: int) -> str:
        """构建隐藏元素提示。"""
        hints = []
        if hidden_count > 5:
            hints.append(
                f"💡 提示: 当前视口外还有约 {hidden_count} 个可交互元素未显示。"
                f"如果在可见区域找不到目标，尝试 scroll 查看更多内容。"
            )
        if meta.iframe_count > 0:
            hints.append(
                f"📦 页面包含 {meta.iframe_count} 个 iframe，"
                f"部分内容可能需要切换 frame 才能操作。"
            )
        if meta.shadow_dom_count > 0:
            hints.append(
                f"🔒 页面包含 {meta.shadow_dom_count} 个 Shadow DOM 组件。"
            )
        return "\n".join(hints)

    def _build_summary(
        self,
        element_count: int,
        groups: dict[str, int],
        meta: Optional[PageMetadata],
    ) -> str:
        """生成结构摘要。"""
        parts = [f"可交互元素: {element_count}"]
        if groups:
            group_parts = [f"{k}({v})" for k, v in sorted(groups.items())]
            parts.append(f"分组: {', '.join(group_parts)}")
        if meta and meta.focused_element_id is not None:
            parts.append(f"焦点: @e{meta.focused_element_id}")
        return " | ".join(parts)

    def _smart_truncate(self, text: str, max_chars: int) -> str:
        """智能截断：保留头部和尾部，中间省略。"""
        if len(text) <= max_chars:
            return text

        # 保留 60% 头部 + 40% 尾部
        head_size = int(max_chars * 0.6)
        tail_size = max_chars - head_size - 50  # 留 50 字给省略提示

        head = text[:head_size]
        tail = text[-tail_size:]

        # 在行边界截断
        head_end = head.rfind("\n")
        if head_end > head_size * 0.8:
            head = head[:head_end]

        tail_start = tail.find("\n")
        if tail_start > 0 and tail_start < tail_size * 0.2:
            tail = tail[tail_start + 1:]

        omitted = len(text) - len(head) - len(tail)
        return (
            f"{head}\n"
            f"... (省略约 {omitted} 字符的中间内容) ...\n"
            f"{tail}"
        )

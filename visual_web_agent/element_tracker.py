"""自适应元素重定位（Adaptive Element Tracker）。

灵感来自 Scrapling 的 Smart Element Tracking：DOM 改版 / 重新渲染后，仅凭原始
CSS/XPath 经常找不回原来那个元素；本模块基于**结构特征签名 + 加权相似度**
重新定位。

为什么 VSpider 需要这个？
------------------------
VSpider 的 SoM ID 每步都重新分配——上一轮 ``target_id=5`` 是 "登录按钮"，
下一轮同一个 DOM 元素可能变成 ``target_id=12``，或者页面局部刷新后整个 SoM
重排。当前 VSpider 在 prompt 中明确告诉 VLM "**不要引用历史 target_id**"，
治标不治本：

- VLM 推理过程经常需要 "再回到刚才那个按钮"
- workflow_memory 只能存数据，存不了元素引用
- 长任务 / 多步切换页面后，靠 VLM 重新视觉识别同一控件，浪费 token + 出错

本模块提供 **"按名字追踪元素"** 的能力：

1. 当 Agent 成功操作某元素时，调用 ``tracker.track(name="login_button", element=...)``
   把它的结构签名（tag / text / role / aria-label / class / bbox / 父链）落到内存。
2. 后续任意一步，调用 ``tracker.relocate("login_button", current_elements)``
   在当前 SoM 快照里按相似度找回该元素的**新** ``som_id``，附带置信度。
3. 置信度足够高就可以直接复用，无需 VLM 重新选 target_id。

签名包含的特征（按权重排序，权重见 ``TrackerConfig.weights``）：
- ``text``（可见文本 / aria-name）：bigram Jaccard 相似度
- ``tag``（HTML 标签）：完全匹配
- ``role``（无障碍角色）：完全匹配
- ``aria_label``：bigram Jaccard 相似度
- ``class_list``：集合 Jaccard
- ``bbox``：归一化欧氏距离倒数
- ``parent_chain``：序列相似度（最近 N 层）

权重总和默认归一化到 1.0；候选元素得分 ≥ ``match_threshold`` 视为匹配。

用法示例：
    tracker = ElementTracker()

    # 第 3 步操作了一个 login 按钮 → 注册它
    tracker.track(
        name="login_button",
        element=current_elements[5],   # SoM id=5 那个元素的 dict
        step=3,
    )

    # 第 10 步页面重排了，想再点 login_button
    result = tracker.relocate("login_button", current_elements_now, current_step=10)
    if result.found:
        # 直接用 result.new_som_id 触发 click
        ...
    else:
        # 让 VLM 重新视觉识别
        ...

入参 ``element`` / ``current_elements`` 的格式（与 VSpider SoM 输出对齐）：
    {
        "som_id": int,             # 必填
        "tag": str,                # 可选：HTML 标签名（小写）
        "text": str,               # 可选：可见文本（visibleName）
        "role": str,               # 可选：a11y 角色
        "aria_label": str,         # 可选：aria-label
        "class_list": list[str],   # 可选：CSS class
        "bbox": [x, y, w, h] 或 {"x":..,"y":..,"w":..,"h":..},  # 可选
        "parent_chain": list[str], # 可选：从外到内的祖先描述
        ...
    }

缺失字段不会让追踪失败，只是该维度不参与评分。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# 配置与数据类
# ──────────────────────────────────────────────────────────────────────


@dataclass
class TrackerConfig:
    """追踪器配置。"""

    # 各维度权重（不需要和为 1.0，会内部归一化）
    weights: dict[str, float] = field(default_factory=lambda: {
        "text": 0.30,
        "tag": 0.15,
        "role": 0.15,
        "aria_label": 0.15,
        "class_list": 0.10,
        "bbox": 0.10,
        "parent_chain": 0.05,
    })

    # 视为匹配的总分阈值（0.0~1.0）
    match_threshold: float = 0.55

    # "高置信度"门槛：超过此值认为强匹配，可直接使用
    high_confidence_threshold: float = 0.80

    # bbox 距离归一化时使用的页面参考尺寸（避免一个边缘像素跳变拉低分）
    bbox_reference_size: float = 1500.0

    # parent chain 比对的最近层数
    parent_chain_depth: int = 4


@dataclass
class ElementSignature:
    """某一刻捕获的元素结构指纹。"""

    name: str
    som_id: int
    tag: str = ""
    text: str = ""
    role: str = ""
    aria_label: str = ""
    class_list: tuple[str, ...] = ()
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    parent_chain: tuple[str, ...] = ()
    captured_step: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "som_id": self.som_id,
            "tag": self.tag,
            "text": self.text,
            "role": self.role,
            "aria_label": self.aria_label,
            "class_list": list(self.class_list),
            "bbox": list(self.bbox),
            "parent_chain": list(self.parent_chain),
            "captured_step": self.captured_step,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ElementSignature":
        bbox_raw = d.get("bbox") or [0, 0, 0, 0]
        if isinstance(bbox_raw, dict):
            bbox_tup = (
                float(bbox_raw.get("x", 0)), float(bbox_raw.get("y", 0)),
                float(bbox_raw.get("w", 0)), float(bbox_raw.get("h", 0)),
            )
        else:
            seq = list(bbox_raw) + [0.0] * (4 - len(bbox_raw))
            bbox_tup = (float(seq[0]), float(seq[1]), float(seq[2]), float(seq[3]))
        return cls(
            name=str(d.get("name", "")),
            som_id=int(d.get("som_id", 0)),
            tag=str(d.get("tag", "")),
            text=str(d.get("text", "")),
            role=str(d.get("role", "")),
            aria_label=str(d.get("aria_label", "")),
            class_list=tuple(str(c) for c in (d.get("class_list") or [])),
            bbox=bbox_tup,
            parent_chain=tuple(str(p) for p in (d.get("parent_chain") or [])),
            captured_step=int(d.get("captured_step", 0)),
            metadata=dict(d.get("metadata") or {}),
        )


@dataclass
class RelocateResult:
    """重定位结果。"""

    found: bool = False
    name: str = ""
    new_som_id: int = 0
    confidence: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    candidate: Optional[dict[str, Any]] = None
    reason: str = ""

    @property
    def is_high_confidence(self) -> bool:
        return self.found and self.confidence >= 0.80


# ──────────────────────────────────────────────────────────────────────
# 主类
# ──────────────────────────────────────────────────────────────────────


class ElementTracker:
    """按名字追踪元素并在 DOM 变化后重新定位。"""

    def __init__(self, config: Optional[TrackerConfig] = None):
        self.config = config or TrackerConfig()
        self._tracked: dict[str, ElementSignature] = {}

    # ── Tracking API ───────────────────────────────────────────────────

    def track(
        self,
        name: str,
        element: dict[str, Any],
        step: int = 0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ElementSignature:
        """注册一个元素到 ``name`` 名下，覆盖同名旧记录。"""
        if not name:
            raise ValueError("tracker name must be non-empty")
        sig = self._element_to_signature(name, element, step, metadata or {})
        self._tracked[name] = sig
        logger.debug(
            f"[TRACKER] track name={name!r} som_id={sig.som_id} "
            f"tag={sig.tag!r} text={sig.text[:30]!r}"
        )
        return sig

    def untrack(self, name: str) -> bool:
        """删除一个已追踪元素，返回是否真的存在过。"""
        return self._tracked.pop(name, None) is not None

    def list_tracked(self) -> list[str]:
        """所有已追踪的名字（按字母序）。"""
        return sorted(self._tracked.keys())

    def get(self, name: str) -> Optional[ElementSignature]:
        """获取签名（用于调试 / 序列化）。"""
        return self._tracked.get(name)

    def reset(self) -> None:
        """清空所有追踪条目（新任务开始时调用）。"""
        self._tracked.clear()

    # ── Relocate API ───────────────────────────────────────────────────

    def relocate(
        self,
        name: str,
        current_elements: list[dict[str, Any]],
        current_step: int = 0,
    ) -> RelocateResult:
        """在当前 SoM 快照里重新定位 ``name``。"""
        sig = self._tracked.get(name)
        if sig is None:
            return RelocateResult(found=False, name=name, reason="not_tracked")
        if not current_elements:
            return RelocateResult(found=False, name=name, reason="empty_snapshot")

        best: Optional[RelocateResult] = None
        for el in current_elements:
            try:
                score, breakdown = self._score(sig, el)
            except Exception as e:  # pragma: no cover
                logger.debug(f"[TRACKER] score error: {e}")
                continue
            if best is None or score > best.confidence:
                best = RelocateResult(
                    found=False,
                    name=name,
                    new_som_id=int(el.get("som_id") or 0),
                    confidence=score,
                    score_breakdown=breakdown,
                    candidate=el,
                )

        if best is None:
            return RelocateResult(found=False, name=name, reason="no_candidates")

        best.found = best.confidence >= self.config.match_threshold
        best.reason = (
            "matched" if best.found
            else f"below_threshold({best.confidence:.2f}<{self.config.match_threshold:.2f})"
        )
        logger.debug(
            f"[TRACKER] relocate name={name!r} → som_id={best.new_som_id} "
            f"score={best.confidence:.2f} found={best.found}"
        )
        return best

    def relocate_all(
        self,
        current_elements: list[dict[str, Any]],
        current_step: int = 0,
    ) -> dict[str, RelocateResult]:
        """批量重定位所有追踪条目。"""
        return {
            name: self.relocate(name, current_elements, current_step)
            for name in self._tracked
        }

    # ── 内部辅助 ───────────────────────────────────────────────────────

    def _element_to_signature(
        self,
        name: str,
        element: dict[str, Any],
        step: int,
        metadata: dict[str, Any],
    ) -> ElementSignature:
        bbox = element.get("bbox") or element.get("rect") or [0, 0, 0, 0]
        if isinstance(bbox, dict):
            bbox_tup = (
                float(bbox.get("x", 0)), float(bbox.get("y", 0)),
                float(bbox.get("w", bbox.get("width", 0))),
                float(bbox.get("h", bbox.get("height", 0))),
            )
        else:
            seq = list(bbox) + [0.0] * (4 - len(bbox))
            bbox_tup = (float(seq[0]), float(seq[1]), float(seq[2]), float(seq[3]))

        text = (
            element.get("text")
            or element.get("name")          # AX Tree 的可访问名
            or element.get("visibleName")
            or ""
        )

        return ElementSignature(
            name=name,
            som_id=int(element.get("som_id") or element.get("id") or 0),
            tag=str(element.get("tag") or "").lower(),
            text=str(text).strip()[:200],
            role=str(element.get("role") or "").lower(),
            aria_label=str(element.get("aria_label") or "").strip()[:200],
            class_list=tuple(str(c) for c in (element.get("class_list") or [])),
            bbox=bbox_tup,
            parent_chain=tuple(
                str(p) for p in (element.get("parent_chain") or [])
            )[-self.config.parent_chain_depth:],
            captured_step=step,
            metadata=dict(metadata),
        )

    def _score(
        self,
        sig: ElementSignature,
        candidate: dict[str, Any],
    ) -> tuple[float, dict[str, float]]:
        """对单个候选元素打分。返回 (总分, 各维度得分明细)。"""
        weights = self.config.weights
        weight_sum = sum(weights.values()) or 1.0

        # 把 candidate 走一遍 _element_to_signature 拿到同结构（不存入字典）
        cand_sig = self._element_to_signature(
            "__candidate__", candidate, step=0, metadata={},
        )

        breakdown: dict[str, float] = {}

        # text 相似度（bigram Jaccard）
        breakdown["text"] = _text_similarity(sig.text, cand_sig.text)
        # tag 完全匹配
        breakdown["tag"] = 1.0 if (sig.tag and sig.tag == cand_sig.tag) else 0.0
        # role 完全匹配
        breakdown["role"] = 1.0 if (sig.role and sig.role == cand_sig.role) else 0.0
        # aria-label 相似度
        breakdown["aria_label"] = _text_similarity(sig.aria_label, cand_sig.aria_label)
        # class_list Jaccard
        breakdown["class_list"] = _set_jaccard(
            set(sig.class_list), set(cand_sig.class_list)
        )
        # bbox 邻近度
        breakdown["bbox"] = _bbox_proximity(
            sig.bbox, cand_sig.bbox, self.config.bbox_reference_size,
        )
        # parent_chain 序列相似度
        breakdown["parent_chain"] = _sequence_similarity(
            sig.parent_chain, cand_sig.parent_chain,
        )

        total = 0.0
        for key, score in breakdown.items():
            total += score * weights.get(key, 0.0)
        total /= weight_sum

        return total, breakdown


# ──────────────────────────────────────────────────────────────────────
# 相似度算法（模块级，便于单测）
# ──────────────────────────────────────────────────────────────────────


def _text_similarity(a: str, b: str) -> float:
    """基于字符 bigram 的 Jaccard 相似度。"""
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a and not b:
        # 双方都没有该字段 → 信息中性，给中等分而不是 0（避免拉低总分）
        return 0.5
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) <= 1 or len(b) <= 1:
        return 1.0 if a == b else 0.0

    def bigrams(s: str) -> set[str]:
        return {s[i:i + 2] for i in range(len(s) - 1)}

    ba, bb = bigrams(a), bigrams(b)
    inter = len(ba & bb)
    union = len(ba | bb)
    return inter / union if union else 0.0


def _set_jaccard(a: set[str], b: set[str]) -> float:
    """集合 Jaccard 相似度。空集 vs 空集 → 0.5（中性）。"""
    if not a and not b:
        return 0.5
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _bbox_proximity(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    reference: float = 1500.0,
) -> float:
    """两矩形中心点距离归一化的接近度（1.0 = 完全重合，0.0 = 距离 ≥ reference）。"""
    if reference <= 0:
        reference = 1.0
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if aw <= 0 and ah <= 0 and bw <= 0 and bh <= 0:
        # 双方都没有 bbox 信息 → 中性
        return 0.5
    cax, cay = ax + aw / 2, ay + ah / 2
    cbx, cby = bx + bw / 2, by + bh / 2
    dist = ((cax - cbx) ** 2 + (cay - cby) ** 2) ** 0.5
    return max(0.0, 1.0 - dist / reference)


def _sequence_similarity(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    """两个有序短序列的相似度（基于末尾对齐的元素重合率）。"""
    if not a and not b:
        return 0.5
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    matches = sum(1 for x, y in zip(a[-n:], b[-n:]) if x == y)
    return matches / max(len(a), len(b))

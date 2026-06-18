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
import time
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
        VLM_SEMANTIC_API_BASE,
        VLM_SEMANTIC_API_KEY,
        VLM_TIMEOUT,
        VLM_MAX_TOKENS,
        VLM_TEMPERATURE,
        VLM_HISTORY_WINDOW,
        MAX_STEPS,
        COMPACTION_ENABLED,
        COMPACTION_TRIGGER_COUNT,
        COMPACTION_TRIGGER_CHARS,
        COMPACTION_KEEP_LAST,
        COMPACTION_COOLDOWN,
        CACHE_MODE,
        CACHE_DIR,
        CACHE_SESSION_ID,
        CACHE_REPLAY_STRICT,
        CACHE_REPLAY_FALLBACK,
        ELEMENT_TRACKER_ENABLED,
        ELEMENT_TRACKER_MATCH_THRESHOLD,
        ELEMENT_TRACKER_HIGH_CONFIDENCE,
    )
    from .prompts import build_system_prompt, build_user_message
    from .response_cache import ResponseCache, CacheMode, CacheReplayMissError
    from .element_tracker import ElementTracker, TrackerConfig, RelocateResult
except ImportError:
    from config import (
        VLM_API_BASE,
        VLM_API_KEY,
        VLM_MODEL_NAME,
        VLM_SEMANTIC_API_BASE,
        VLM_SEMANTIC_API_KEY,
        VLM_TIMEOUT,
        VLM_MAX_TOKENS,
        VLM_TEMPERATURE,
        VLM_HISTORY_WINDOW,
        MAX_STEPS,
        COMPACTION_ENABLED,
        COMPACTION_TRIGGER_COUNT,
        COMPACTION_TRIGGER_CHARS,
        COMPACTION_KEEP_LAST,
        COMPACTION_COOLDOWN,
        CACHE_MODE,
        CACHE_DIR,
        CACHE_SESSION_ID,
        CACHE_REPLAY_STRICT,
        CACHE_REPLAY_FALLBACK,
        ELEMENT_TRACKER_ENABLED,
        ELEMENT_TRACKER_MATCH_THRESHOLD,
        ELEMENT_TRACKER_HIGH_CONFIDENCE,
    )
    from prompts import build_system_prompt, build_user_message
    from response_cache import ResponseCache, CacheMode, CacheReplayMissError
    from element_tracker import ElementTracker, TrackerConfig, RelocateResult

logger = logging.getLogger("vspider.vlm")


def _broadcast_log_safe(message: str, level: str = "info") -> None:
    """向 API WebSocket 广播日志；未运行 API 时静默降级。"""
    try:
        from api_server import broadcast_log

        broadcast_log(message, level=level)
    except Exception:
        pass


def _looks_like_auth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "401" in text
        or "invalid_api_key" in text
        or "incorrect api key" in text
        or "authenticationerror" in text
    )


# ════════════════════════════════════════════════════════════════
#  Pydantic 结构化输出模型
# ════════════════════════════════════════════════════════════════

# VSpiderAction / VSpiderActionBatch moved to vlm_models.py (Phase 1 extraction)
try:
    from .vlm_models import (
        VSpiderAction,
        VSpiderActionBatch,
        _VSPIDER_ACTION_SCHEMA,
        _VSPIDER_BATCH_SCHEMA,
    )
except ImportError:
    from vlm_models import (
        VSpiderAction,
        VSpiderActionBatch,
        _VSPIDER_ACTION_SCHEMA,
        _VSPIDER_BATCH_SCHEMA,
    )

try:
    from .vlm_parser import VLMParserMixin
except ImportError:
    from vlm_parser import VLMParserMixin

# ════════════════════════════════════════════════════════════════
#  Wave 2 — Planner / Reflector 数据模型
# ════════════════════════════════════════════════════════════════

try:
    from .vlm_models import (
        SubGoal, TaskPlan, ReflectorDecision,
        TASK_PLAN_SCHEMA as _TASK_PLAN_SCHEMA,
        REFLECTOR_DECISION_SCHEMA as _REFLECTOR_DECISION_SCHEMA,
        ERROR_DECISION as _ERROR_DECISION,
        ERROR_DECISION_LIST as _ERROR_DECISION_LIST,
    )
except ImportError:
    from vlm_models import (
        SubGoal, TaskPlan, ReflectorDecision,
        TASK_PLAN_SCHEMA as _TASK_PLAN_SCHEMA,
        REFLECTOR_DECISION_SCHEMA as _REFLECTOR_DECISION_SCHEMA,
        ERROR_DECISION as _ERROR_DECISION,
        ERROR_DECISION_LIST as _ERROR_DECISION_LIST,
    )


class VLMClient(VLMParserMixin):
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
        self.semantic_api_base = (
            getattr(runtime_config, "VLM_SEMANTIC_API_BASE", "")
            or self.api_base
        )
        self.semantic_api_key = (
            getattr(runtime_config, "VLM_SEMANTIC_API_KEY", "")
            or self.api_key
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
        self.semantic_client = self.client
        if self.semantic_api_base != self.api_base or self.semantic_api_key != self.api_key:
            self.semantic_client = AsyncOpenAI(
                api_key=self.semantic_api_key,
                base_url=self.semantic_api_base,
                timeout=self.timeout,
                http_client=httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)),
            )
        # 操作历史：存储每轮批次中各动作的 (step, thought, action, target_id, type_value) 摘要
        self._history: list[dict] = []
        self._history_window: int = max(2, int(VLM_HISTORY_WINDOW or self._HISTORY_WINDOW))
        # 结构化输出：首次尝试 json_schema 模式，若模型不支持则自动降级
        self._use_structured: bool = True
        # 自愈反馈：下一轮 ask() 会将上一轮的执行错误注入提示
        self._pending_error: str | None = None
        # ── Message Compaction：长任务历史智能压缩 ─────────────────────
        try:
            from .message_compaction import MessageCompactor, CompactionConfig
        except ImportError:
            from message_compaction import MessageCompactor, CompactionConfig
        self._compactor = MessageCompactor(
            config=CompactionConfig(
                enabled=COMPACTION_ENABLED,
                trigger_count=COMPACTION_TRIGGER_COUNT,
                trigger_char_count=COMPACTION_TRIGGER_CHARS,
                keep_last_items=COMPACTION_KEEP_LAST,
                summary_max_chars=2000,
                compact_cooldown_steps=COMPACTION_COOLDOWN,
            ),
            vlm_client=self,
        )
        # ── Response Cache：record / replay 模式 ────────────────────────
        # off 时构造一个透传实例，所有 lookup 返回 None / store 是 no-op；
        # record 时把每步的 (输入哈希, 输出) 落盘到 JSONL；
        # replay 时按步骤号从 JSONL 返回缓存输出，跳过真实 VLM 调用。
        self._response_cache = ResponseCache(
            mode=CacheMode.from_str(CACHE_MODE),
            cache_dir=CACHE_DIR,
            session_id=CACHE_SESSION_ID or None,
            replay_strict=CACHE_REPLAY_STRICT,
            replay_fallback_on_miss=CACHE_REPLAY_FALLBACK,
        )
        if self._response_cache.is_active():
            logger.info(
                f"[CACHE] mode={self._response_cache.mode.value} "
                f"session={self._response_cache.session_id} "
                f"path={self._response_cache.session_path()}"
            )
        # ── Element Tracker：自适应元素重定位 ──────────────────────────
        # 启用后通过 vlm.track_element() / vlm.relocate_element() 在
        # DOM 重排后按结构签名找回元素。开关关闭时这些方法是 no-op，
        # 不影响任何现有调用路径。
        self._element_tracker_enabled = bool(ELEMENT_TRACKER_ENABLED)
        self._element_tracker = ElementTracker(
            config=TrackerConfig(
                match_threshold=ELEMENT_TRACKER_MATCH_THRESHOLD,
                high_confidence_threshold=ELEMENT_TRACKER_HIGH_CONFIDENCE,
            ),
        ) if self._element_tracker_enabled else None
        if self._element_tracker_enabled:
            logger.info(
                f"[TRACKER] enabled | match≥{ELEMENT_TRACKER_MATCH_THRESHOLD} "
                f"high≥{ELEMENT_TRACKER_HIGH_CONFIDENCE}"
            )
        # ── VLM Budget：per-run call/token metering ──────────────────────
        try:
            from .vlm_budget import VlmBudget, BudgetConfig
        except ImportError:
            from vlm_budget import VlmBudget, BudgetConfig
        self._budget = VlmBudget(config=BudgetConfig())
        logger.info(
            f"VLM client initialized | model={self.model} | base={self.api_base} | "
            f"semantic_model={self.semantic_model} | semantic_base={self.semantic_api_base} | "
            f"text_only={self.text_only} | "
            f"max_tokens={self.max_tokens} | temperature={self.temperature}"
        )

    def configure_budget(self, max_calls: int = 0, max_tokens: int = 0, soft_ratio: float = 0.8) -> None:
        """Set per-run VLM budget limits.  0 = unlimited on that axis."""
        try:
            from .vlm_budget import BudgetConfig
        except ImportError:
            from vlm_budget import BudgetConfig
        self._budget.config = BudgetConfig(
            max_calls=max_calls, max_tokens=max_tokens, soft_ratio=soft_ratio,
        )
        if max_calls or max_tokens:
            logger.info("[BUDGET] configured: max_calls=%d max_tokens=%d soft=%.0f%%",
                        max_calls, max_tokens, soft_ratio * 100)

    @property
    def budget_summary(self) -> dict:
        """Return current VLM budget/usage snapshot."""
        return self._budget.summary()

    def _build_history_summary(self) -> str:
        """构建最近 N 轮操作的历史摘要文本（含执行结果）。

        当历史较长时，前缀包含 MessageCompactor 生成的压缩摘要，
        后跟最近 N 条原始记录，保证 VLM 既有全局上下文又有精确近况。
        """
        if not self._history:
            return self._compactor.get_summary_prefix() or ""

        lines = []
        # 注入早期历史的 LLM 压缩摘要（若有）
        compaction_prefix = self._compactor.get_summary_prefix()
        if compaction_prefix:
            lines.append(compaction_prefix)
        lines.append("## 最近操作历史（含执行结果，避免重复）")
        older_count = max(0, len(self._history) - self._history_window)
        if older_count and not compaction_prefix:
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
        # ── Message Compaction：智能压缩替代硬截断 ─────────────────────
        # 旧逻辑：if len > window*2: 硬截断到 window 条
        # 新逻辑：超阈值时异步压缩旧记录为摘要，保留最近 window 条原始
        if len(self._history) > self._history_window * 2:
            if self._compactor.needs_compaction(self._history, step):
                # 标记需要压缩（实际压缩在 maybe_compact_history 中异步执行）
                self._pending_compaction_step = step
            else:
                # 降级：若压缩冷却中则仍做硬截断
                self._history = self._history[-self._history_window:]

    async def maybe_compact_history(self, goal: str = "") -> None:
        """在每步结束后调用，若有待压缩任务则执行异步摘要。

        设计为异步方法，因为 LLM 摘要需要网络请求。
        main.py 在每步 action 执行完毕后调用此方法。
        """
        pending_step = getattr(self, "_pending_compaction_step", None)
        if pending_step is None:
            return
        self._pending_compaction_step = None
        try:
            self._history = await self._compactor.compact(
                self._history, pending_step, goal=goal
            )
        except Exception as e:
            logger.warning(f"[COMPACTION] 压缩执行异常，降级为硬截断: {e}")
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

    def reset_decision_history(self, reason: str = "") -> None:
        """Clear VLM decision history after AGENT_STUCK — force fresh screenshot reasoning."""
        try:
            from .stuck_recovery_guard import build_recovery_feedback
        except ImportError:
            from stuck_recovery_guard import build_recovery_feedback

        self._history.clear()
        self._pending_error = build_recovery_feedback(reason)
        logger.warning("[STUCK RECOVERY] VLM decision history cleared: %s", reason[:160])

    # ════════════════════════════════════════════════════════════════
    #  Element Tracker —— 自适应元素重定位（按结构签名找回元素）
    # ════════════════════════════════════════════════════════════════
    #
    # 这一组方法把 element_tracker.ElementTracker 暴露为 VLMClient 的 public API。
    # 设计原则：**开关关闭时所有方法都是 no-op**，不抛错、不影响调用方逻辑。
    # 这样 main.py 的 guard 可以无脑调用，业务侧不需要做 try/except 包裹。
    #
    # 典型用法：
    #   # main.py：成功执行 click 后注册元素
    #   vlm.track_element("submit_btn", element=som_elements[5], step=current_step)
    #
    #   # 后续任意一步：DOM 重排后想再点 submit_btn
    #   result = vlm.relocate_element("submit_btn", som_elements_now)
    #   if result and result.is_high_confidence:
    #       new_target_id = result.new_som_id
    #       # 直接复用 new_target_id 触发动作，无需 VLM 重新视觉定位
    # ════════════════════════════════════════════════════════════════

    def track_element(
        self,
        name: str,
        element: dict[str, Any],
        step: int = 0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        """注册一个元素到追踪器。返回是否真的写入（开关关闭时返回 False）。"""
        if not self._element_tracker_enabled or self._element_tracker is None:
            return False
        try:
            self._element_tracker.track(
                name=name, element=element, step=step, metadata=metadata,
            )
            logger.debug(f"[TRACKER] tracked name={name!r}")
            return True
        except Exception as e:
            logger.warning(f"[TRACKER] track failed name={name!r}: {e}")
            return False

    def untrack_element(self, name: str) -> bool:
        """删除一个追踪条目。返回是否真的存在过。"""
        if not self._element_tracker_enabled or self._element_tracker is None:
            return False
        return self._element_tracker.untrack(name)

    def relocate_element(
        self,
        name: str,
        current_elements: list[dict[str, Any]],
        current_step: int = 0,
    ) -> Optional["RelocateResult"]:
        """在当前 SoM 快照里重新定位。开关关闭时返回 None。"""
        if not self._element_tracker_enabled or self._element_tracker is None:
            return None
        try:
            return self._element_tracker.relocate(
                name=name,
                current_elements=current_elements,
                current_step=current_step,
            )
        except Exception as e:
            logger.warning(f"[TRACKER] relocate failed name={name!r}: {e}")
            return None

    def relocate_all_tracked(
        self,
        current_elements: list[dict[str, Any]],
        current_step: int = 0,
    ) -> dict[str, "RelocateResult"]:
        """批量重定位所有追踪条目。开关关闭时返回空 dict。"""
        if not self._element_tracker_enabled or self._element_tracker is None:
            return {}
        try:
            return self._element_tracker.relocate_all(
                current_elements=current_elements,
                current_step=current_step,
            )
        except Exception as e:
            logger.warning(f"[TRACKER] relocate_all failed: {e}")
            return {}

    def list_tracked_elements(self) -> list[str]:
        """列出当前所有追踪名。开关关闭时返回空列表。"""
        if not self._element_tracker_enabled or self._element_tracker is None:
            return []
        return self._element_tracker.list_tracked()

    def reset_element_tracker(self) -> None:
        """清空追踪条目（新任务开始时调用）。开关关闭时是 no-op。"""
        if self._element_tracker is not None:
            self._element_tracker.reset()

    def _build_named_anchor_section(
        self,
        som_elements: Optional[list[dict[str, Any]]],
        current_step: int,
    ) -> str:
        """构建 Named Anchors 提示段落：在当前快照中重新定位所有追踪元素。

        VLM 看到这段话后能够：
          1) 知道之前操作过的元素现在的 SoM ID
          2) 识别 "已经操作过" → 避免重复
          3) 在 thought 中按名字（last_click 等）描述意图，main.py
             可在事后日志检索

        返回空字符串表示无追踪条目 / 未启用 / 无候选。
        """
        if not self._element_tracker_enabled or self._element_tracker is None:
            return ""
        if not som_elements:
            return ""
        tracked_names = self._element_tracker.list_tracked()
        if not tracked_names:
            return ""

        # 把 SoM 元素映射到 tracker 期望的格式（兼容 _last_som_elements 的 'id' 字段）
        normalized: list[dict[str, Any]] = []
        for el in som_elements:
            normalized.append({
                "som_id": el.get("som_id") or el.get("id") or 0,
                "tag": el.get("tag", ""),
                "text": el.get("text") or el.get("name", ""),
                "role": el.get("role", ""),
                "aria_label": el.get("aria_label", ""),
                "class_list": el.get("class_list", []),
                "bbox": el.get("bbox") or el.get("rect") or {},
                "parent_chain": el.get("parent_chain") or (
                    [el.get("parentContext") or ""] if el.get("parentContext") else []
                ),
            })

        try:
            results = self._element_tracker.relocate_all(
                normalized, current_step=current_step,
            )
        except Exception as e:
            logger.debug(f"[TRACKER] anchor section relocate_all failed: {e}")
            return ""

        if not results:
            return ""

        # 拼装可读的 markdown 段落
        lines: list[str] = [
            "## 📍 Named Anchors (元素追踪锚点)",
            "",
            "你在之前的步骤里操作过这些元素。系统已用结构签名在本次快照里重新定位：",
            "",
        ]
        for name in sorted(results.keys()):
            r = results[name]
            sig = self._element_tracker.get(name)
            ago = (current_step - sig.captured_step) if sig else 0
            text_preview = (sig.text or "")[:40] if sig else ""
            tag_str = sig.tag if sig else ""
            if r.found:
                conf_tag = "✓" if r.is_high_confidence else "?"
                lines.append(
                    f"- **`{name}`** {conf_tag} → 当前 som_id=**{r.new_som_id}** "
                    f"(置信度 {r.confidence:.2f}) | "
                    f"text={text_preview!r} tag={tag_str} | 上次操作于 step {sig.captured_step if sig else '?'}（{ago} 步前）"
                )
            else:
                lines.append(
                    f"- **`{name}`** ✗ 未找到匹配元素（最高分 {r.confidence:.2f}） | "
                    f"text={text_preview!r} tag={tag_str} | 该元素可能已被替换/移除"
                )
        lines.append("")
        lines.append(
            "⚠️ 锚点只是**只读上下文**，不是新字段。需要操作元素时仍然在 JSON 里写 "
            "`target_id=<数字>`。"
        )
        lines.append(
            "- 看到 `✓` 高置信锚点时：可直接复用该 som_id，不必再视觉识别"
        )
        lines.append(
            "- 看到 `?` 中等置信锚点时：先在截图上确认该 ID 确实是你要的元素"
        )
        lines.append(
            "- 看到 `✗` 未找到时：原元素已不存在，请按当前页面状态重新选择策略"
        )
        lines.append(
            "- **避免循环**：若某锚点显示你 1~2 步前刚操作过，且任务目标已达成（按 progress_review），优先 done"
        )
        return "\n".join(lines)

    @staticmethod
    def _build_user_content(
        screenshot_b64: str | None,
        user_text: str,
        extra_images: list[str] | None = None,
    ) -> list[dict] | str:
        """Assemble OpenAI / Qwen-VL multimodal user content.

        Order: primary screenshot -> attachment ``extra_images`` -> text.
        Raw base64 is normalized to a ``data:image`` URL (idempotent). When no
        image is available the plain text string is returned so the request
        still succeeds (graceful degrade).
        """
        def _norm(b64: str) -> str:
            if b64.startswith("data:image"):
                return b64
            return f"data:image/jpeg;base64,{b64}"

        image_urls: list[str] = []
        if screenshot_b64:
            image_urls.append(_norm(screenshot_b64))
        for img in (extra_images or []):
            if img:
                image_urls.append(_norm(img))
        if not image_urls:
            return user_text
        content: list[dict] = [
            {"type": "image_url", "image_url": {"url": u}} for u in image_urls
        ]
        content.append({"type": "text", "text": user_text})
        return content

    async def ask(
        self,
        screenshot_b64: str | None,
        goal: str,
        step: int,
        input_descriptions: str = "",
        workflow_memory: dict | None = None,
        task_plan: Optional["TaskPlan"] = None,
        max_steps: int | None = None,
        som_elements: Optional[list[dict[str, Any]]] = None,
        capability_route: Optional[dict[str, Any]] = None,
        extra_images: list[str] | None = None,
    ) -> list[dict]:
        """
        向 VLM/LLM 发送当前状态，请求下一批次动作（连招模式）。

        Response Cache 集成（Replay / Record 模式）：
          - REPLAY 模式：跳过实际 LLM 调用，按 step 从 JSONL 缓存返回；
            缺失时按配置抛错或回落到真实调用。
          - RECORD 模式：正常调用 LLM，但额外把 (输入哈希, 输出) 落盘。
          - OFF（默认）：完全透传，无任何额外开销。

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
        self._ask_t0 = time.time()
        history_text = self._build_history_summary()
        # ── Cache Replay：命中即返回，绕过真实 LLM 调用 ───────────────────
        if self._response_cache.mode == CacheMode.REPLAY:
            try:
                cached = self._response_cache.lookup(
                    step=step,
                    goal=goal,
                    screenshot_b64=screenshot_b64,
                    history_summary=history_text,
                )
                if cached is not None:
                    # 仍要把缓存输出写入历史，保证后续步骤的 history 摘要一致
                    self._record_history(step, cached)
                    logger.info(
                        f"[CACHE] step={step} replay hit "
                        f"({len(cached)} cached actions)"
                    )
                    return cached
                # 走到这里说明 fallback_on_miss=True 且 lookup 返回 None
                logger.warning(
                    f"[CACHE] step={step} replay miss → falling back to real LLM call"
                )
            except CacheReplayMissError:
                # 严格模式 / 默认行为：缺失即抛错，由调用方处理
                raise
        # 真实路径继续执行（OFF / RECORD / REPLAY-fallback）
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
            capability_route=capability_route,
        )
        # ── Named Anchors：注入元素追踪锚点 ────────────────────────────
        # 把所有已 track 的元素在当前快照里 relocate，作为 read-only 上下文
        # 附在 user_text 后。VLM 看到后可以：
        #   1) 知道之前操作过的元素当前的 SoM ID（可能已变）
        #   2) 避免对同一逻辑元素重复操作
        #   3) 决定下一步是该回到旧元素还是切换到新元素
        # 不修改 schema、不要求 VLM 用新字段，最大向后兼容。
        anchor_section = self._build_named_anchor_section(som_elements, step)
        if anchor_section:
            user_text = user_text + "\n\n" + anchor_section
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
        # 截图 + 附件图片(extra_images) + 文本；全缺图时降级为纯文本。
        user_content = self._build_user_content(
            screenshot_b64, user_text, extra_images
        )

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

                # ── Budget Meter：记录本步 VLM 消耗 ───────────────────────
                _usage = getattr(response, "usage", None)
                _prompt_tk = int(getattr(_usage, "prompt_tokens", 0) or 0)
                _compl_tk = int(getattr(_usage, "completion_tokens", 0) or 0)
                _vlm_latency = int((time.time() - (self._ask_t0 or time.time())) * 1000)
                self._budget.record(
                    step=step,
                    prompt_tokens=_prompt_tk,
                    completion_tokens=_compl_tk,
                    latency_ms=_vlm_latency,
                )

                # ── Cache Record：把本步 (输入哈希, 输出) 落盘 ────────────
                self._response_cache.store(
                    step=step,
                    goal=goal,
                    screenshot_b64=screenshot_b64,
                    input_descriptions=input_descriptions,
                    output=decisions,
                    history_summary=history_text,
                )
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
                        # ── Cache Record：纯文本降级路径也要落盘 ────────────
                        self._response_cache.store(
                            step=step,
                            goal=goal,
                            screenshot_b64=screenshot_b64,
                            input_descriptions=input_descriptions,
                            output=decisions,
                            history_summary=history_text,
                        )
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
        # ── Surface the real exception in the fallback decision ───────────
        # Without this, the trajectory just shows "VLM 请求失败或返回格式异常"
        # for every retry exhaustion and there's no way to tell auth /
        # rate-limit / parse / network issues apart. We keep the original
        # action="error" / status="error" contract so main.py's consecutive-
        # failure counter still trips, but stuff the exception type + message
        # into thought so it surfaces in the HTML log and event stream.
        _err_kind = type(last_error).__name__ if last_error else "Unknown"
        _err_msg = (str(last_error) if last_error else "no exception captured")[:400]
        _fallback = dict(_ERROR_DECISION)
        _fallback["thought"] = (
            f"VLM 请求最终失败（{_err_kind}）: {_err_msg}"
        )
        return [_fallback]

    # ════════════════════════════════════════════════════════════════
    #  全页结构化数据提取（页面文本 + 纯文本 VLM 结构化调用）
    # ════════════════════════════════════════════════════════════════

    async def extract_structured_data(
        self,
        page_text: str,
        goal: str,
        example_data: Any = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        从页面文本中提取结构化数据（纯文本 LLM 调用，无需截图）。

        借鉴 browser-use 的 extract_clean_markdown 架构：
        VLM 只负责发出 extract 意图，数据由系统从 AX Tree、DOM innerText
        或压缩后的列表条目文本获取页面内容后，
        用一次纯文本 LLM 调用完成结构化，突破视口截图只能看到 ~10 条的限制。

        Args:
            page_text: AX Tree / DOM innerText / compact list items 等页面文本
            goal: 用户的任务目标描述
            example_data: VLM 之前提取的数据样本（用于推断字段名和格式）

        Returns:
            结构化数据列表（list[dict]），失败时返回 None
        """
        page_text = (page_text or "").strip()
        if not page_text:
            logger.warning("[EXTRACT FULL] 页面文本为空，跳过结构化提取")
            return None

        # 截断防止 token 溢出（纯文本 token 效率高，给 16K 字符）。
        # 压缩列表文本以 "\n\nItem N:" 分隔，优先在条目边界截断，避免最后一行半截。
        if len(page_text) > 16000:
            clipped = page_text[:16000]
            marker = clipped.rfind("\n\nItem ")
            if marker >= 12000:
                clipped = clipped[:marker].rstrip()
            page_text = clipped

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
            f"以下是网页文本内容（来源可能是 AX Tree、DOM innerText 或压缩列表条目，"
            f"通常不受当前截图视口限制），请从中提取**全部**符合用户任务要求的数据项：\n\n"
            f"---页面文本开始---\n{page_text}\n---页面文本结束---\n\n"
            f"请输出 JSON 数组，包含页面中所有匹配项。只输出 JSON，不要输出其他内容。"
        )

        try:
            logger.info(
                f"[EXTRACT FULL] 开始全页结构化提取，"
                f"页面文本长度={len(page_text)}，目标={goal[:60]}"
            )
            _broadcast_log_safe(
                "[EXTRACT FULL] 启动页面文本结构化提取（纯文本 LLM 调用）..."
            )

            response = await self.semantic_client.chat.completions.create(
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
    #  Visual Judge —— 语义宏（date_pick / form_set 等）的慢路径兜底
    # ════════════════════════════════════════════════════════════════

    async def judge_screenshot(
        self,
        screenshot_b64: Optional[str],
        question: str,
        context: str = "",
        timeout: float = 12.0,
    ) -> dict:
        """
        视觉断言：JS 自检失败时调一次 VLM 看截图判 yes/no/unclear。

        设计：
        - 严格 yes/no/unclear 输出，避免开放式发挥
        - 截图缺失时返回 unclear，让调用方按"快路径已失败 + 兜底无答案"处理
        - 单独超时（默认 12s）避免拖慢 RPA 回放

        Returns:
            {"verdict": "yes"|"no"|"unclear", "reason": str}
        """
        if not screenshot_b64:
            return {"verdict": "unclear", "reason": "screenshot unavailable"}

        image_url = screenshot_b64
        if not image_url.startswith("data:image"):
            image_url = f"data:image/jpeg;base64,{image_url}"

        system_prompt = (
            "你是网页 UI 视觉裁判。只看截图，回答关于页面状态的 yes/no 问题。\n"
            "规则：\n"
            "1. 只输出 JSON：{\"verdict\":\"yes\"|\"no\"|\"unclear\",\"reason\":\"...\"}\n"
            "2. yes = 完全确定问题描述的状态已达成\n"
            "3. no = 看到证据反驳（值不对 / 状态相反 / 元素未变）\n"
            "4. unclear = 截图被遮挡、信息不足、或两种解释都说得通\n"
            "5. reason 用一句话引用截图里的具体可见证据（输入框文本 / 选中态 / 标签等）"
        )
        user_text = f"问题：{question}"
        if context:
            user_text += f"\n\n上下文：{context}"
        user_text += "\n\n请只输出 JSON。"

        try:
            response = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": image_url}},
                                {"type": "text", "text": user_text},
                            ],
                        },
                    ],
                    max_tokens=200,
                    temperature=0.0,
                ),
                timeout=timeout,
            )
            raw = (response.choices[0].message.content or "").strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
                raw = re.sub(r"\n?\s*```$", "", raw)
            data = json.loads(raw)
            verdict = str(data.get("verdict") or "").strip().lower()
            if verdict not in {"yes", "no", "unclear"}:
                return {"verdict": "unclear", "reason": f"invalid verdict: {raw[:120]}"}
            reason = str(data.get("reason") or "").strip() or "(no reason)"
            logger.info(f"[VL JUDGE] verdict={verdict} reason={reason[:120]}")
            return {"verdict": verdict, "reason": reason}
        except asyncio.TimeoutError:
            logger.warning(f"[VL JUDGE] timed out after {timeout}s")
            return {"verdict": "unclear", "reason": f"vlm timeout {timeout}s"}
        except Exception as e:
            logger.warning(f"[VL JUDGE] call failed: {e}")
            return {"verdict": "unclear", "reason": f"vlm call failed: {e}"}

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

            async def _create_plan_response(client, model_name: str):
                call_kwargs = dict(api_kwargs)
                call_kwargs["model"] = model_name
                try:
                    return await client.chat.completions.create(**call_kwargs)
                except BadRequestError:
                    call_kwargs.pop("response_format", None)
                    return await client.chat.completions.create(**call_kwargs)

            try:
                response = await _create_plan_response(
                    self.semantic_client,
                    self.semantic_model,
                )
            except Exception as semantic_exc:
                semantic_is_primary = (
                    self.semantic_client is self.client
                    and self.semantic_model == self.model
                )
                if semantic_is_primary or not _looks_like_auth_error(semantic_exc):
                    raise
                logger.warning(
                    "[PLANNER] semantic client auth failed; retrying with primary VLM client/model=%s",
                    self.model,
                )
                _broadcast_log_safe(
                    "[PLANNER] semantic key failed; retrying planner with primary VLM model",
                    level="warning",
                )
                response = await _create_plan_response(self.client, self.model)

            raw = (response.choices[0].message.content or "").strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
                raw = re.sub(r"\n?\s*```$", "", raw)

            data = json.loads(raw)
            plan = TaskPlan.model_validate(data)

            if not plan.sub_goals:
                logger.warning("[PLANNER] LLM 返回空子目标列表，降级为单子目标计划")
                return fallback_plan

            # ── Strip login-probe subgoals when user did NOT request login ─
            # Universal "use-it-if-you-can, ask-human-only-when-blocked"
            # principle: the Planner LLM frequently prefixes tasks with a
            # defensive "探测登录 / 检查是否已登录" subgoal even when the goal
            # never mentioned authentication. This forces the VLM to click
            # "登录" on its first step and derails the run.
            #
            # Drop login-probe subgoals UNLESS the goal text itself committed
            # to logging in (explicit verbs or credential placeholders).
            # Real login walls at runtime are handled reactively by the
            # PRELOGIN system + ask_human fallback — Planner doesn't need to
            # repeat that defence at planning time.
            try:
                try:
                    from .main import _goal_has_explicit_login_intent  # type: ignore
                except ImportError:  # pragma: no cover
                    from main import _goal_has_explicit_login_intent  # type: ignore[no-redef]
                if not _goal_has_explicit_login_intent(goal):
                    _login_needles = (
                        "登录", "登陆", "登入", "认证", "验证页",
                        "login", "logged", "signin", "sign in", "signed in",
                        "signed-in", "auth", "authenticated",
                    )
                    _probe_verbs = (
                        "探测", "检查", "确认", "判断", "校验", "检测",
                        "是否", "probe", "check", "verify", "detect",
                        "ensure", "确保",
                    )
                    _kept = []
                    _dropped = []
                    for sg in plan.sub_goals:
                        _desc = (sg.description or "").lower()
                        _is_login_probe = (
                            any(n in _desc for n in _login_needles)
                            and any(v in _desc for v in _probe_verbs)
                        )
                        if _is_login_probe:
                            _dropped.append(sg.description)
                        else:
                            _kept.append(sg)
                    # Edge: if filter would empty the plan (LLM only emitted
                    # a login probe), keep the original to avoid an empty
                    # plan crash downstream.
                    if _dropped and _kept:
                        logger.info(
                            "[PLANNER] no explicit login intent — dropped %d "
                            "login-probe subgoal(s): %s",
                            len(_dropped),
                            [d[:60] for d in _dropped],
                        )
                        # Re-number IDs sequentially after stripping
                        for new_id, sg in enumerate(_kept, start=1):
                            sg.id = new_id
                            sg.status = "pending"
                        plan.sub_goals = _kept
            except Exception as _filter_err:
                logger.debug(
                    "[PLANNER] login-probe filter skipped: %s",
                    _filter_err,
                )

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

            async def _create_reflect_response(client, model_name: str):
                call_kwargs = dict(api_kwargs)
                call_kwargs["model"] = model_name
                try:
                    return await client.chat.completions.create(**call_kwargs)
                except BadRequestError:
                    call_kwargs.pop("response_format", None)
                    return await client.chat.completions.create(**call_kwargs)

            try:
                response = await _create_reflect_response(
                    self.semantic_client,
                    self.semantic_model,
                )
            except Exception as semantic_exc:
                semantic_is_primary = (
                    self.semantic_client is self.client
                    and self.semantic_model == self.model
                )
                if semantic_is_primary or not _looks_like_auth_error(semantic_exc):
                    raise
                logger.warning(
                    "[REFLECTOR] semantic client auth failed; retrying with primary VLM client/model=%s",
                    self.model,
                )
                response = await _create_reflect_response(self.client, self.model)

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

    # Parser methods moved to vlm_parser.py (VLMParserMixin)
    # _validate_batch, _validate_single, _rescue_schema_collapse, _parse_json
    # are inherited via VLMParserMixin

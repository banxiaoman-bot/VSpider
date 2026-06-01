"""响应缓存 / Replay 模式（Response Cache / Replay）。

灵感来自 Scrapling 的 dev-mode response caching：第一次跑真实抓取并缓存到磁盘，
后续相同输入直接从磁盘 replay，**不再发送真实请求**。

VSpider 的应用场景：
1. **Prompt 调优**：改完 prompt 想看新提示对决策的影响；但不想再耗 API 配额、
   也不想等真实浏览器跑一遍。Record 一次，后续 Replay。
2. **回归测试**：固定输入序列 + 固定输出 → 跑 CI 时不需要 LLM 凭证。
3. **本地复现 bug**：用户发来一个失败 session 的 cache，本地直接 Replay 复现。

三种模式：
- ``off``：透传，不做任何事（默认）。
- ``record``：实际调用 VLM，把每步的 (输入, 输出) 写入 JSONL session 文件。
- ``replay``：拦截 VLM 调用，按步骤号从 session 文件返回缓存输出；
  缓存缺失时根据 ``replay_strict`` 决定是 fail 还是回落到真实调用。

文件格式（一个 session 一个 .jsonl 文件，每行一条 step 记录）：

    {
        "step": 1,
        "key": "abc123def456",
        "goal_hash": "...",
        "screenshot_hash": "...",
        "history_hash": "...",
        "timestamp": "2026-01-15T10:30:00",
        "input_summary": {
            "goal": "..." (前 200 字符),
            "screenshot_chars": 12345,
            "input_descriptions_chars": 567
        },
        "output": [{"action": "...", "target_id": ..., ...}, ...]
    }

用法：
    from response_cache import ResponseCache, CacheMode

    cache = ResponseCache(mode=CacheMode.RECORD, cache_dir=".cache/vlm",
                          session_id="task_20260115_103000")

    # 在 vlm.ask() 内：
    cached = cache.lookup(step=step, goal=goal, screenshot_b64=screenshot_b64)
    if cached is not None:
        return cached
    # ...真实调用 LLM 拿到 decisions...
    cache.store(step=step, goal=goal, screenshot_b64=screenshot_b64,
                input_descriptions=input_descriptions, output=decisions)
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class CacheMode(str, enum.Enum):
    """缓存模式枚举。"""

    OFF = "off"
    RECORD = "record"
    REPLAY = "replay"

    @classmethod
    def from_str(cls, value: Optional[str]) -> "CacheMode":
        """从字符串解析（容忍大小写 / None）。"""
        if not value:
            return cls.OFF
        try:
            return cls(value.lower().strip())
        except ValueError:
            logger.warning(f"[CACHE] Unknown mode {value!r}, falling back to OFF")
            return cls.OFF


@dataclass
class CacheEntry:
    """单条缓存记录。"""

    step: int
    key: str
    goal_hash: str
    screenshot_hash: str
    history_hash: str
    timestamp: str
    input_summary: dict[str, Any]
    output: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "key": self.key,
            "goal_hash": self.goal_hash,
            "screenshot_hash": self.screenshot_hash,
            "history_hash": self.history_hash,
            "timestamp": self.timestamp,
            "input_summary": self.input_summary,
            "output": self.output,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CacheEntry":
        return cls(
            step=int(data.get("step", 0)),
            key=str(data.get("key", "")),
            goal_hash=str(data.get("goal_hash", "")),
            screenshot_hash=str(data.get("screenshot_hash", "")),
            history_hash=str(data.get("history_hash", "")),
            timestamp=str(data.get("timestamp", "")),
            input_summary=dict(data.get("input_summary") or {}),
            output=list(data.get("output") or []),
        )


@dataclass
class CacheStats:
    """缓存统计信息。"""

    hits: int = 0          # replay 命中次数
    misses: int = 0        # replay 未命中次数
    records: int = 0       # record 写入次数
    mismatches: int = 0    # replay 命中但输入哈希不一致

    def summary(self) -> str:
        return (
            f"hits={self.hits} misses={self.misses} "
            f"records={self.records} mismatches={self.mismatches}"
        )


class ResponseCache:
    """VLM 响应缓存器。

    采用 **step number** 作为主键：replay 时按步骤号顺序返回缓存输出。
    输入哈希作为完整性校验副信号：哈希不一致时记录警告但仍返回缓存
    （除非 strict=True）。
    """

    def __init__(
        self,
        mode: CacheMode | str = CacheMode.OFF,
        cache_dir: str | Path = ".cache/vlm_responses",
        session_id: Optional[str] = None,
        replay_strict: bool = False,
        replay_fallback_on_miss: bool = False,
    ):
        """
        Args:
            mode: 缓存模式 off/record/replay
            cache_dir: 缓存目录（自动创建）
            session_id: 会话 ID；None 时自动用时间戳生成
            replay_strict: replay 模式下输入哈希不一致是否抛错（默认仅警告）
            replay_fallback_on_miss: replay 缺失时是否回落到真实调用（默认抛错）
        """
        self.mode = CacheMode.from_str(mode) if isinstance(mode, str) else mode
        self.cache_dir = Path(cache_dir)
        self.session_id = session_id or self._auto_session_id()
        self.replay_strict = bool(replay_strict)
        self.replay_fallback_on_miss = bool(replay_fallback_on_miss)
        self.stats = CacheStats()

        # 仅 replay 模式预加载，record 模式追加写
        self._entries: dict[int, CacheEntry] = {}
        if self.mode in (CacheMode.REPLAY,):
            self._load_session()
        elif self.mode == CacheMode.RECORD:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info(
                f"[CACHE] RECORD mode → {self._session_file()} "
                f"(session_id={self.session_id})"
            )

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def lookup(
        self,
        step: int,
        goal: str = "",
        screenshot_b64: Optional[str] = None,
        history_summary: str = "",
    ) -> Optional[list[dict[str, Any]]]:
        """Replay 模式：按 step 返回缓存输出；其他模式返回 None。"""
        if self.mode != CacheMode.REPLAY:
            return None

        entry = self._entries.get(step)
        if entry is None:
            self.stats.misses += 1
            msg = f"[CACHE] REPLAY miss step={step} session={self.session_id}"
            if self.replay_fallback_on_miss:
                logger.warning(f"{msg} → fallback to real call")
                return None
            else:
                logger.error(msg)
                # 严格 replay 但找不到记录 → 抛错
                raise CacheReplayMissError(
                    f"No cache entry for step {step} in session {self.session_id}"
                )

        # 校验输入哈希（仅警告 / 严格抛错）
        cur_goal_hash = self._hash_text(goal)
        cur_screenshot_hash = self._hash_screenshot(screenshot_b64)
        cur_history_hash = self._hash_text(history_summary)

        mismatches: list[str] = []
        if entry.goal_hash and cur_goal_hash != entry.goal_hash:
            mismatches.append(f"goal({entry.goal_hash[:6]}→{cur_goal_hash[:6]})")
        if entry.screenshot_hash and cur_screenshot_hash != entry.screenshot_hash:
            mismatches.append(
                f"screenshot({entry.screenshot_hash[:6]}→{cur_screenshot_hash[:6]})"
            )
        if entry.history_hash and cur_history_hash != entry.history_hash:
            mismatches.append(
                f"history({entry.history_hash[:6]}→{cur_history_hash[:6]})"
            )

        if mismatches:
            self.stats.mismatches += 1
            msg = f"[CACHE] REPLAY step={step} hash mismatch: {', '.join(mismatches)}"
            if self.replay_strict:
                raise CacheInputMismatchError(msg)
            logger.warning(msg)

        self.stats.hits += 1
        logger.debug(f"[CACHE] REPLAY hit step={step} (cached at {entry.timestamp})")
        return entry.output

    def store(
        self,
        step: int,
        goal: str,
        screenshot_b64: Optional[str],
        input_descriptions: str,
        output: list[dict[str, Any]],
        history_summary: str = "",
    ) -> None:
        """Record 模式：把当前步骤的输入摘要 + 输出落盘。"""
        if self.mode != CacheMode.RECORD:
            return

        try:
            entry = CacheEntry(
                step=step,
                key=self._make_key(step, goal, screenshot_b64, history_summary),
                goal_hash=self._hash_text(goal),
                screenshot_hash=self._hash_screenshot(screenshot_b64),
                history_hash=self._hash_text(history_summary),
                timestamp=datetime.now().isoformat(timespec="seconds"),
                input_summary={
                    "goal": goal[:200],
                    "screenshot_chars": len(screenshot_b64) if screenshot_b64 else 0,
                    "input_descriptions_chars": len(input_descriptions or ""),
                    "history_chars": len(history_summary or ""),
                },
                output=output or [],
            )
            self._append_entry(entry)
            self.stats.records += 1
            logger.debug(
                f"[CACHE] RECORD step={step} key={entry.key[:10]} "
                f"({len(output) if output else 0} actions)"
            )
        except Exception as e:
            logger.warning(f"[CACHE] Failed to record step={step}: {e}")

    def is_active(self) -> bool:
        """是否处于工作状态（record 或 replay）。"""
        return self.mode in (CacheMode.RECORD, CacheMode.REPLAY)

    def list_steps(self) -> list[int]:
        """已缓存的步骤号（replay 模式时可用）。"""
        return sorted(self._entries.keys())

    def session_path(self) -> Path:
        """获取当前 session 文件路径（外部调试用）。"""
        return self._session_file()

    # ──────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────

    def _session_file(self) -> Path:
        return self.cache_dir / f"{self.session_id}.jsonl"

    @staticmethod
    def _auto_session_id() -> str:
        return f"vlm_session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    @staticmethod
    def _hash_text(text: str) -> str:
        if not text:
            return ""
        return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()[:12]

    @staticmethod
    def _hash_screenshot(b64: Optional[str]) -> str:
        """对截图取部分哈希（前 4KB）以容忍 base64 末尾微小差异。"""
        if not b64:
            return ""
        sample = b64[:4096]
        return hashlib.md5(sample.encode("utf-8", errors="replace")).hexdigest()[:12]

    @staticmethod
    def _make_key(
        step: int,
        goal: str,
        screenshot_b64: Optional[str],
        history: str,
    ) -> str:
        """生成完整缓存键（用于完整性校验，非主索引）。"""
        parts = [
            str(step),
            ResponseCache._hash_text(goal),
            ResponseCache._hash_screenshot(screenshot_b64),
            ResponseCache._hash_text(history),
        ]
        return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]

    def _append_entry(self, entry: CacheEntry) -> None:
        """追加一条记录到 session JSONL 文件。"""
        path = self._session_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        # 同步内存索引（供后续 list_steps）
        self._entries[entry.step] = entry

    def _load_session(self) -> None:
        """Replay 模式：加载整个 session 文件到内存。"""
        path = self._session_file()
        if not path.exists():
            logger.warning(
                f"[CACHE] REPLAY session file not found: {path}; "
                f"all lookups will miss."
            )
            return
        loaded = 0
        try:
            with path.open("r", encoding="utf-8") as f:
                for line_no, raw in enumerate(f, 1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                        entry = CacheEntry.from_dict(data)
                        self._entries[entry.step] = entry
                        loaded += 1
                    except Exception as e:
                        logger.warning(
                            f"[CACHE] Skip malformed line {line_no} in {path}: {e}"
                        )
        except Exception as e:
            logger.error(f"[CACHE] Failed to load session {path}: {e}")
            return

        logger.info(
            f"[CACHE] REPLAY loaded {loaded} entries from {path} "
            f"(steps={self.list_steps()[:10]}{'...' if loaded > 10 else ''})"
        )


# ──────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────


class CacheError(Exception):
    """缓存相关基类异常。"""


class CacheReplayMissError(CacheError):
    """Replay 模式下找不到指定步骤的缓存记录。"""


class CacheInputMismatchError(CacheError):
    """Replay 模式严格校验下输入哈希不一致。"""

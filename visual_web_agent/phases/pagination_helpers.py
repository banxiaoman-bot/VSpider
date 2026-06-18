"""Pagination, goal-step, and RPA cache lookup helpers — extracted from ``main.py``."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("vspider.main")

try:
    from .goal_parser import (
        _goal_is_tooltip_extract, _parse_goal_target_count,
        _parse_goal_target_pages, _goal_is_form_fill,
    )
except ImportError:
    _goal_is_tooltip_extract = lambda g: False  # type: ignore
    _parse_goal_target_count = lambda g: None  # type: ignore
    _parse_goal_target_pages = lambda g: None  # type: ignore
    _goal_is_form_fill = lambda g: False  # type: ignore

_RPA_CACHE_DIR = Path(__file__).resolve().parent.parent / "rpa_cache"

try:
    from ..config import MAX_STEPS
except ImportError:
    MAX_STEPS = 30

try:
    from .rpa_cache import _build_rpa_match_metadata, _rpa_cache_path, _load_exact_rpa_cache
except ImportError:
    pass

def _goal_needs_pagination_probe(goal: str) -> bool:
    """Whether the task likely needs pagination-related hints/probes."""
    text = str(goal or "").lower()
    if _goal_is_tooltip_extract(text):
        return False
    target_count = _parse_goal_target_count(text)
    if target_count is not None and target_count >= 20:
        return True
    if _parse_goal_target_pages(text):
        return True
    return any(
        kw in text
        for kw in (
            "批量", "全量", "所有", "全部", "多页", "翻页", "分页", "下一页",
            "逐页", "pagination", "paginate", "next page", "all pages",
        )
    )


def _should_force_first_flip_after_successful_extract(goal: str) -> bool:
    """Whether a successful extract may immediately force next_page.

    Row-count goals (for example "前26条/50条") must be governed by data delta:
    only a zero-new-row extract proves the current page/viewport is drained.
    Forcing next_page right after a successful extract can skip lazy-loaded rows
    still hidden lower on the same page.
    """
    text = str(goal or "")
    if _goal_is_tooltip_extract(text):
        return False
    if _parse_goal_target_count(text) is not None:
        return False
    return _parse_goal_target_pages(text) is not None


def _should_schedule_next_page_after_extract(
    goal: str,
    *,
    new_rows: int,
    total_rows: int,
    extract_source: str = "",
    pagination_kind: str = "",
    expected_rows: int = 0,
    physically_drained: bool = False,
) -> tuple[bool, str]:
    """Decide whether a successful extract can safely arm next_page immediately.

    This is intentionally stricter than "target not met + paginator exists".
    We only short-circuit when the current extraction looks like a complete page
    batch, so lazy-loaded rows lower on the same page are not skipped.
    """
    if _goal_is_tooltip_extract(goal):
        return False, "tooltip extraction is key-value mapping, not page traversal"

    page_target = _parse_goal_target_pages(goal)
    if page_target is not None and new_rows > 0:
        return True, "explicit page-count goal"

    target_count = _parse_goal_target_count(goal)
    if target_count is None:
        return False, "no row-count target"
    if total_rows >= target_count:
        return False, "target already met"

    source = str(extract_source or "").upper()
    kind = str(pagination_kind or "").lower()
    try:
        expected = int(expected_rows or 0)
    except (TypeError, ValueError):
        expected = 0

    if physically_drained and new_rows > 0:
        return True, f"current page physically drained after {new_rows} new rows"

    if expected >= 10:
        page_floor = max(10, int(expected * 0.7))
        remaining = max(0, target_count - (total_rows - new_rows))
        required = min(expected, page_floor, remaining or page_floor)
        if new_rows >= required:
            return True, (
                f"page batch sufficiently extracted: {new_rows}/{expected} "
                f"(required {required})"
            )
        return False, (
            f"only {new_rows}/{expected} expected rows; "
            "allow scroll/drain before next_page"
        )

    if "DOM_TABLE" in source and new_rows >= 5:
        return True, f"DOM table page extracted {new_rows} rows"
    if kind == "numeric" and new_rows >= 10:
        return True, f"numeric paginator with {new_rows} new rows"
    if new_rows >= 20:
        return True, f"large page batch extracted {new_rows} rows"

    return False, f"only {new_rows} new rows; allow scroll/drain before next_page"


def _derive_effective_max_steps(goal: str) -> int:
    """Raise step budget for bulk extraction / multi-page traversal goals.

    Triggers (any of):
      - explicit count >= 50 (e.g. "抓取 200 条"): budget = max(MAX_STEPS, count//5 + 30)
      - explicit page traversal "前 N 页 / 共 N 页 / N 页数据": budget = max(50, N*5 + 10)
      - generic "翻页 / 分页 / 下一页 / 多页 / 逐页 / pagination" + count<50:
        budget = max(50, MAX_STEPS)
    """
    target_count = _parse_goal_target_count(goal)
    if target_count is not None and target_count >= 50:
        return min(500, max(MAX_STEPS, target_count // 5 + 30))

    # 显式翻页计数：「前 N 页」「共 N 页」「N 页 数据/列表」
    page_match = re.search(
        r'(?:前|共|抓取|采集|提取|遍历|爬|browse|first)\s*(\d+)\s*(?:页|pages?)',
        goal, re.IGNORECASE,
    )
    if page_match:
        n_pages = int(page_match.group(1))
        # 每页约 3-5 步（提取+滚动+点击下一页+缓冲），加 10 步前后开销
        return min(500, max(50, n_pages * 5 + 10))

    # 通用翻页关键词（不带数量但意图明确）
    page_keywords = ('翻页', '分页', '下一页', '多页', '逐页', 'pagination', 'paginate', 'next page')
    if any(kw in goal.lower() for kw in (k.lower() for k in page_keywords)):
        return max(50, MAX_STEPS)

    return MAX_STEPS


def _goal_should_skip_rpa(goal: str) -> tuple[bool, str]:
    """
    Some goals should not use cached RPA replay.

    This includes explicit recovery/error tests. Relative date-picker tasks are
    handled by semantic RPA macros instead of being disabled here. Cascader /
    multi-level popup tasks also use semantic macros and should not be blocked
    by this physical-replay gate.
    """
    text = str(goal or "").strip()
    if not text:
        return False, ""

    suspicious_patterns: list[tuple[str, str]] = [
        (r"target_id\s*=\s*9999", "goal explicitly injects an invalid target_id"),
        (r"点击.*不存在的元素", "goal explicitly asks to click a nonexistent element"),
        (r"绝对不存在的元素", "goal explicitly asks for a guaranteed-missing element"),
        (r"不存在的(?:按钮|链接|控件|元素)", "goal contains an explicit nonexistent control step"),
        (r"无效的?(?:按钮|链接|控件|元素|target_id)", "goal contains an explicit invalid control step"),
        (r"故意.*(?:错误|失败|异常|无效)", "goal explicitly injects an error/failure step"),
        (r"(?:错误|异常|失败).*(?:步骤|动作|链路|流程|场景)", "goal is testing an error-handling path"),
        (r"(?:容错|恢复|鲁棒|自愈|报错)测试", "goal is explicitly testing recovery / robustness"),
        (r"测试.*(?:错误|异常|失败|容错|恢复|自愈)", "goal is explicitly testing error recovery"),
    ]
    for pattern, reason in suspicious_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True, reason
    return False, ""


def _load_rpa_cache_payload(path: Path) -> dict | None:
    try:
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
        normalized_payload = _normalize_rpa_cache_payload(raw_payload)
        if normalized_payload != raw_payload:
            try:
                _write_rpa_cache_payload(path, normalized_payload)
            except Exception as refresh_exc:
                logger.debug("[RPA] Failed to refresh normalized cache %s: %s", path.name, refresh_exc)
        return normalized_payload
    except Exception as exc:
        logger.warning(f"[RPA] Failed to load cache {path.name}: {exc}")
        return None


def _find_similar_rpa_cache(url: str, goal: str, exclude_path: Path | None = None) -> tuple[Path, dict, str] | None:
    if not _RPA_CACHE_DIR.exists():
        return None

    target_meta = _build_rpa_match_metadata(url, goal)
    target_url = target_meta.get("normalized_url") or ""
    target_goal = target_meta.get("normalized_goal") or ""
    if not target_url or not target_goal:
        return None

    best: tuple[float, int, float, Path, dict, str] | None = None
    for path in _RPA_CACHE_DIR.glob("*.json"):
        if exclude_path and path == exclude_path:
            continue

        payload = _load_rpa_cache_payload(path)
        if not payload or not payload.get("replayable", True):
            continue

        candidate_url = payload.get("normalized_url") or ""
        candidate_goal = payload.get("normalized_goal") or ""
        if not candidate_url or not candidate_goal:
            continue
        if candidate_url != target_url:
            continue

        ratio = SequenceMatcher(None, target_goal, candidate_goal).ratio()
        exact_core = candidate_goal == target_goal
        if not exact_core and ratio < 0.88:
            continue

        fail_count = int(payload.get("fail_count", 0) or 0)
        mtime = path.stat().st_mtime
        score = ratio + (0.08 if exact_core else 0.0) - min(fail_count, 3) * 0.05
        reason = "normalized core goal exact match" if exact_core else f"similarity={ratio:.3f}"
        candidate_key = (score, -fail_count, mtime, path, payload, reason)
        if best is None or candidate_key[:3] > best[:3]:
            best = candidate_key

    if not best:
        return None

    return best[3], best[4], best[5]


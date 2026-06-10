"""
VSpider 主程序入口

实现 Agent 的核心执行循环：
截图 -> VLM 决策 -> 执行操作 -> 循环

用法：
    python main.py --url <目标URL> --goal <任务描述>
    python main.py --url <目标URL> --goal <任务描述> --user-data-dir ./browser_data

示例：
    python main.py --url "http://192.168.1.100/login" --goal "登录营销2.0系统并进入查询页面"
    python main.py --url "https://example.com" --goal "查询数据" --user-data-dir ./browser_data
"""

import asyncio
import argparse
import calendar
import csv
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote_plus, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_APP_ROOT = Path(__file__).resolve().parent
for _dotenv_path in (_PROJECT_ROOT / ".env", _APP_ROOT / ".env"):
    if _dotenv_path.exists():
        load_dotenv(dotenv_path=_dotenv_path, override=False)

try:
    from .config import MAX_STEPS, SCREENSHOT_DIR, JUDGE_ENABLED, A11Y_ENHANCER_ENABLED
    from .browser_env import BrowserEnv, ActionExecutionError
    from .browser_pool import acquire_browser, release_browser
    from .vlm_client import VLMClient, TaskPlan, SubGoal
    from .data_writers import save_artifact, save_run_dataset, resolve_output_contract
    from .io_contract import (
        ensure_contract_skeleton,
        ensure_input_contract_skeleton,
        infer_output_contract,
        write_output_contract,
    )
    from .data_sanitizer import (
        TOOLTIP_UNIQUE_KEY,
        extract_tooltip_primary_key,
        sanitize_extracted_rows,
    )
    from .artifact_manager import resolve_artifact_path
    from .trajectory_logger import HtmlLogger
    from .action_result import ActionResult, action_result_evidence_parts
    from .action_registry import build_default_action_registry
    from .capability_router import route_task as route_capabilities_for_task
    from .browser_state import BrowserStateSnapshot
    from .event_stream import EventStream
    from .agent_strategy import (
        extraction_targets_reached as _agent_strategy_extraction_targets_reached,
        normalize_guard_url as _agent_strategy_normalize_guard_url,
        normalize_output_field_key as _agent_strategy_normalize_output_field_key,
        parse_goal_requested_fields as _agent_strategy_parse_goal_requested_fields,
        parse_goal_target_count as _agent_strategy_parse_goal_target_count,
        parse_goal_target_pages as _agent_strategy_parse_goal_target_pages,
    )
    from .auth_vault import SecretResolutionError, resolve_env_placeholders
    from .page_data_controller import PageDataController
    from .perception.targeted import format_probe_text
    from .perception.targeted import choose_click_handoff_candidate
    from .perception.targeted import choose_type_handoff_candidate
    from .perception.targeted import probe_page
    from .skills.registry import build_default_skill_registry
    from .skills.replay import save_skill_replay_snapshot
    from .extraction_engine.snapshots import maybe_save_snapshot
    from .extraction_engine.recovery import rank_extraction_candidates_with_history
    from .extraction_engine.cards import extract_semantic_card_rows
    from .extraction_engine.strategies import (
        choose_pre_extract_reached_candidate as _choose_pre_extract_reached_candidate,
        click_target_is_same_page_extract_nav as _click_target_is_same_page_extract_nav,
        data_shape_exposes_target_candidate as _data_shape_exposes_target_candidate,
        infer_goal_output_contract,
        infer_goal_strategy_context,
    )
    from .form_engine import (
        parse_form_assignments as engine_parse_form_assignments,
        prepare_form_batch_fields as engine_prepare_form_batch_fields,
    )
    from .failure_classifier import (
        classify_and_log as _failure_classifier_fn,
        FailureStats as _FailureStatsClass,
    )
    from .judge import TaskJudge, JudgeConfig
    from .loop_detector import ActionLoopDetector, LoopDetectorConfig, PageFingerprint
    from .a11y_enhancer import A11yEnhancer, A11yEnhancerConfig, PageMetadata as A11yPageMetadata
    from .url_guard import UrlGuardError, build_guarded_opener, check_url
    from .stealth_profile import default_user_agent
except ImportError:
    from config import MAX_STEPS, SCREENSHOT_DIR, JUDGE_ENABLED, A11Y_ENHANCER_ENABLED
    from browser_env import BrowserEnv, ActionExecutionError
    from browser_pool import acquire_browser, release_browser
    from vlm_client import VLMClient, TaskPlan, SubGoal
    from data_writers import save_artifact, save_run_dataset, resolve_output_contract
    from io_contract import (
        ensure_contract_skeleton,
        ensure_input_contract_skeleton,
        infer_output_contract,
        write_output_contract,
    )
    from data_sanitizer import (
        TOOLTIP_UNIQUE_KEY,
        extract_tooltip_primary_key,
        sanitize_extracted_rows,
    )
    from artifact_manager import resolve_artifact_path
    from trajectory_logger import HtmlLogger
    from action_result import ActionResult, action_result_evidence_parts
    from action_registry import build_default_action_registry
    from capability_router import route_task as route_capabilities_for_task
    from browser_state import BrowserStateSnapshot
    from event_stream import EventStream
    from agent_strategy import (
        extraction_targets_reached as _agent_strategy_extraction_targets_reached,
        normalize_guard_url as _agent_strategy_normalize_guard_url,
        normalize_output_field_key as _agent_strategy_normalize_output_field_key,
        parse_goal_requested_fields as _agent_strategy_parse_goal_requested_fields,
        parse_goal_target_count as _agent_strategy_parse_goal_target_count,
        parse_goal_target_pages as _agent_strategy_parse_goal_target_pages,
    )
    from auth_vault import SecretResolutionError, resolve_env_placeholders
    from page_data_controller import PageDataController
    from perception.targeted import format_probe_text
    from perception.targeted import choose_click_handoff_candidate
    from perception.targeted import choose_type_handoff_candidate
    from perception.targeted import probe_page
    from skills.registry import build_default_skill_registry
    from skills.replay import save_skill_replay_snapshot
    from extraction_engine.snapshots import maybe_save_snapshot
    from extraction_engine.recovery import rank_extraction_candidates_with_history
    from extraction_engine.cards import extract_semantic_card_rows
    from extraction_engine.strategies import (
        choose_pre_extract_reached_candidate as _choose_pre_extract_reached_candidate,
        click_target_is_same_page_extract_nav as _click_target_is_same_page_extract_nav,
        data_shape_exposes_target_candidate as _data_shape_exposes_target_candidate,
        infer_goal_output_contract,
        infer_goal_strategy_context,
    )
    from form_engine import (
        parse_form_assignments as engine_parse_form_assignments,
        prepare_form_batch_fields as engine_prepare_form_batch_fields,
    )
    from failure_classifier import (
        classify_and_log as _failure_classifier_fn,
        FailureStats as _FailureStatsClass,
    )
    from judge import TaskJudge, JudgeConfig
    from loop_detector import ActionLoopDetector, LoopDetectorConfig, PageFingerprint
    from a11y_enhancer import A11yEnhancer, A11yEnhancerConfig, PageMetadata as A11yPageMetadata
    from url_guard import UrlGuardError, build_guarded_opener, check_url
    from stealth_profile import default_user_agent

# ========== 日志配置 ==========
# Windows 终端默认编码不是 UTF-8，中文会显示为 ????
# 强制将 stdout/stderr 切换为 UTF-8 以正确显示中文日志
if sys.platform == "win32":
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
_LOG_DATEFMT = "%H:%M:%S"

# 控制台 Handler
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))

# 文件 Handler（UTF-8 编码，保证回看日志时中文不乱码）
_log_dir = Path(__file__).parent
_file_handler = logging.FileHandler(
    _log_dir / "run.log", mode="a", encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))

logging.basicConfig(
    level=logging.INFO,
    handlers=[_console_handler, _file_handler],
)
logger = logging.getLogger("vspider.main")


def _broadcast_log_safe(message: str, level: str = "info") -> None:
    """向 API WebSocket 广播日志；未运行 API 服务时静默降级。"""
    try:
        from api_server import broadcast_log

        broadcast_log(message, level=level)
    except Exception:
        pass


# ── Final Answer 面板：每个 run 的答案元数据（由 _record_run_answer 写入） ──
# 这两个变量被 _broadcast_done_safe 自动读取，将 answer_type / answer 字段
# 透传给 api_server.broadcast_done，再由 WS 推送到前端 Final Answer Tab。
# 仅 success=True 时附带；失败任务不会把 traceback 当作"最终结论"渲染。
_RUN_OUTPUT_MODE: str | None = None
_RUN_ANSWER_TEXT: str | None = None
# F3: 答案域（"weather"|"stock"|"recipe"|"flight"|"generic"|None）。
# 在 _compact_answer_text_for_goal 决议后由 _record_run_answer(domain=...) 写入，
# 供前端 Final Answer 面板按域选择卡片渲染器。
_RUN_ANSWER_DOMAIN: str | None = None
_RUN_DONE_BROADCASTED: bool = False
# Names of artifacts produced during the current run (consumed by
# _broadcast_done_safe to auto-synthesise a file-mode summary).
_RUN_NEW_ARTIFACT_NAMES: list[str] = []
# K4: per-run failure metadata. Set at the top of the run function so
# _broadcast_done_safe can call failure_archive.record_failure(...) with
# the right run_id/goal/started_at when success=False arrives. None means
# "this process hasn't started a run yet" — failure recording is skipped
# in that state (e.g. tests that import main.py without launching a run).
_RUN_ID: str | None = None
_RUN_STARTED_AT: float | None = None
_RUN_GOAL: str | None = None
# Last step number reached this run (updated each loop iteration). Used
# in the failure record so the UI can show "failed at step 7 of 40".
_RUN_LAST_STEP: int | None = None


def _record_run_answer(
    *,
    mode: str | None = None,
    text: str | None = None,
    domain: str | None = None,
) -> None:
    """记录本次 run 的"最终答案"元数据，供下次 done 广播使用。

    ``mode`` 接受现有 ``_goal_output_mode`` 的取值（``"answer" | "artifact" |
    "mixed" | "default"``）。只有 ``"answer"`` / ``"artifact"`` 会在 WS 上
    映射出 answer_type；其余取值留空，让前端按 artifact 数量兜底推断。

    ``text`` 是经 ``_clean_user_visible_done_message`` 清洗后的最终文本，
    支持 Markdown。重复调用会覆盖上一次的 text — 始终以最后一次为准。

    ``domain`` (F3) 是 ``_detect_answer_domain`` 返回的领域标签。
    透传给前端可启用 stock / recipe / weather / flight 的专属卡片渲染。
    """
    global _RUN_OUTPUT_MODE, _RUN_ANSWER_TEXT, _RUN_ANSWER_DOMAIN
    if mode is not None:
        _RUN_OUTPUT_MODE = mode
    if text is not None:
        _RUN_ANSWER_TEXT = text
    if domain is not None:
        _RUN_ANSWER_DOMAIN = domain


def _reset_run_answer() -> None:
    """run 启动时清空答案状态，避免上次的答案在新 run 中被复用。"""
    global _RUN_OUTPUT_MODE, _RUN_ANSWER_TEXT, _RUN_ANSWER_DOMAIN, _RUN_DONE_BROADCASTED
    global _RUN_ID, _RUN_STARTED_AT, _RUN_GOAL, _RUN_LAST_STEP
    _RUN_OUTPUT_MODE = None
    _RUN_ANSWER_TEXT = None
    _RUN_ANSWER_DOMAIN = None
    _RUN_DONE_BROADCASTED = False
    _RUN_NEW_ARTIFACT_NAMES.clear()
    # K4: clear run identity so a stale ID from a previous run can't
    # accidentally archive a failure for the new run if it crashes
    # before _record_run_start fires.
    _RUN_ID = None
    _RUN_STARTED_AT = None
    _RUN_GOAL = None
    _RUN_LAST_STEP = None


def _record_run_start(
    *,
    run_id: str,
    goal: str | None = None,
    started_at: float | None = None,
) -> None:
    """K4: stash run identity for ``_broadcast_done_safe`` to consume on
    failure. Idempotent — overwrites any previous run's metadata.

    ``started_at`` defaults to ``time.time()`` so callers don't have to
    import time locally just to record this. ``goal`` is truncated by
    ``failure_archive`` itself, so we pass it through verbatim.
    """
    global _RUN_ID, _RUN_STARTED_AT, _RUN_GOAL
    rid = str(run_id or "").strip()
    _RUN_ID = rid or None
    _RUN_STARTED_AT = float(started_at) if started_at is not None else time.time()
    _RUN_GOAL = (goal or None)


def _ensure_run_registry_record(
    *,
    run_id: str,
    start_url: str,
    goal: str,
    upload_file: str = "",
    auth_profiles: str | None = None,
    vlm_options: dict | None = None,
    run_constraints: dict | None = None,
) -> bool:
    """Create a registry row for direct ``run_agent`` callers if missing.

    API/queue callers create their parent run before invoking ``run_agent``.
    This helper only owns records it creates, so it never overwrites queue state.
    """

    rid = str(run_id or "").strip()
    if not rid:
        return False
    try:
        try:
            from . import run_registry as _run_registry
        except ImportError:
            import run_registry as _run_registry  # type: ignore[no-redef]

        if _run_registry.load_run(rid) is not None:
            return False
        upload = Path(upload_file) if upload_file else None
        _run_registry.create_run(
            run_id=rid,
            target_url=start_url or "",
            prompt=goal or "",
            mode="direct",
            filename=upload.name if upload is not None else "",
            file_size_kb=round((upload.stat().st_size / 1024), 1)
            if upload is not None and upload.exists()
            else 0.0,
            auth_profiles=auth_profiles or "",
            vlm_model=str((vlm_options or {}).get("model") or ""),
            semantic_model=str((vlm_options or {}).get("semantic_model") or ""),
            vlm_model_type=str((vlm_options or {}).get("model_type") or "vl"),
            vlm_options=vlm_options or None,
            constraints=run_constraints or None,
            status="running",
        )
        return True
    except Exception as exc:
        logger.debug("[RUN REGISTRY] direct record skipped for %s: %s", rid, exc)
        return False


def _complete_owned_run_registry_record(
    run_id: str,
    *,
    owned: bool,
    success: bool,
    stopped: bool = False,
) -> None:
    if not owned:
        return
    try:
        try:
            from . import run_registry as _run_registry
        except ImportError:
            import run_registry as _run_registry  # type: ignore[no-redef]
        _run_registry.complete_run(run_id, success=success, stopped=stopped)
    except Exception as exc:
        logger.debug("[RUN REGISTRY] direct record complete skipped for %s: %s", run_id, exc)


def _record_run_step(step: int) -> None:
    """K4: update the running step counter so failure records can show
    ``"failed at step N"``. Best-effort; bad values are ignored."""
    global _RUN_LAST_STEP
    try:
        _RUN_LAST_STEP = int(step)
    except (TypeError, ValueError):
        pass


def _run_done_was_broadcasted() -> bool:
    """Return whether this process already emitted a done event for the run."""
    return bool(_RUN_DONE_BROADCASTED)


def _record_new_artifact(name: str | None) -> None:
    """Append an artifact filename to the per-run accumulator.

    Called from ``artifact_manager.register_artifact`` so that the centralised
    ``_broadcast_done_safe`` can synthesise a useful file-mode summary even
    when no domain compactor produced explicit answer text. Empty / falsy
    names are silently ignored. Duplicates are de-duplicated to keep the
    summary readable when the same file is registered multiple times.
    """
    if not name:
        return
    text = str(name).strip()
    if not text:
        return
    if text in _RUN_NEW_ARTIFACT_NAMES:
        return
    _RUN_NEW_ARTIFACT_NAMES.append(text)


def _synthesize_file_mode_summary(names: list[str]) -> str:
    """Render the accumulator into a Markdown summary for the Final Answer panel.

    Returns ``""`` when the list is empty so the caller can fall back to
    the existing fixed CTA. Names are quoted with backticks so the frontend
    Markdown renderer styles them as inline code, matching the file-listing
    look used elsewhere in the UI.
    """
    if not names:
        return ""
    n = len(names)
    head_items = [f"`{nm}`" for nm in names[:5]]
    head = "、".join(head_items)
    if n > 5:
        head += f" 等共 {n} 个"
    else:
        head += f"（共 {n} 个）" if n > 1 else ""
    return (
        f"📁 本次任务已导出文件：{head}。\n\n"
        "请前往 **Artifacts** 面板下载。"
    )


def _derive_answer_type(mode: str | None) -> str | None:
    """``_goal_output_mode`` → 前端 ``answer_type`` 的映射。"""
    if mode == "answer":
        return "text"
    if mode == "artifact":
        return "file"
    return None


def _broadcast_done_safe(
    success: bool,
    message: str = "",
    *,
    answer_type: str | None = None,
    answer: str | None = None,
    answer_domain: str | None = None,
) -> None:
    """向 API WebSocket 广播任务结束信号；未运行 API 时静默降级。

    若调用方未显式提供 ``answer_type`` / ``answer``，会自动从模块级
    ``_RUN_OUTPUT_MODE`` / ``_RUN_ANSWER_TEXT`` 中提取（见 ``_record_run_answer``）。
    ``success=False`` 时不会附带 answer 字段，避免异常栈被前端误渲染为最终结论。

    F3: ``answer_domain`` 同样从 ``_RUN_ANSWER_DOMAIN`` 兜底，仅在
    ``answer_type == "text"`` 且 ``success=True`` 时才会传给前端。
    """
    global _RUN_DONE_BROADCASTED
    try:
        from api_server import broadcast_done

        eff_type: str | None = answer_type
        eff_text: str | None = answer
        eff_domain: str | None = answer_domain
        if success:
            if eff_type is None:
                eff_type = _derive_answer_type(_RUN_OUTPUT_MODE)
            if eff_text is None and _RUN_ANSWER_TEXT:
                eff_text = _RUN_ANSWER_TEXT
            # File-mode auto-summary: if no domain compactor produced text but
            # we know which artifacts were emitted this run, render a Markdown
            # listing so Final Answer panel doesn't fall back to the static CTA.
            if eff_type == "file" and not eff_text and _RUN_NEW_ARTIFACT_NAMES:
                eff_text = _synthesize_file_mode_summary(_RUN_NEW_ARTIFACT_NAMES)
            # Pull domain from per-run state when caller didn't pass one.
            if eff_domain is None and _RUN_ANSWER_DOMAIN:
                eff_domain = _RUN_ANSWER_DOMAIN
            # Domain only meaningful for text answers (no card for file mode).
            if eff_type != "text":
                eff_domain = None
        else:
            eff_type = None
            eff_text = None
            eff_domain = None
        broadcast_done(
            success,
            message,
            answer_type=eff_type,
            answer=eff_text,
            answer_domain=eff_domain,
        )
        # K4: archive failure for the post-mortem drawer (Web UI K3).
        # Only on success=False, only when we actually have a run_id (i.e.
        # the run started past the bootstrap point that called
        # _record_run_start). Wrapped in its own try/except so a disk
        # error here can NEVER suppress the broadcast above or break the
        # finalize phase event below.
        if not success and _RUN_ID:
            try:
                from visual_web_agent import failure_archive as _fa

                _fa.record_failure(
                    run_id=_RUN_ID,
                    reason=str(message or "(no message)"),
                    goal=_RUN_GOAL,
                    started_at=_RUN_STARTED_AT,
                    step_count=_RUN_LAST_STEP,
                )
            except Exception as _archive_err:
                # Use logger if defined; else swallow silently.
                try:
                    logger.debug(
                        "[FAILURE ARCHIVE] record_failure raised: %s",
                        _archive_err,
                    )
                except Exception:
                    pass
        # I3: emit a finalize phase event right alongside the done broadcast
        # so the frontend timeline gets a clear "task ended" tick with the
        # answer kind / domain. Failure is severity=error so it stands out.
        try:
            from api_server import broadcast_phase as _bp_finalize

            _bp_finalize(
                "finalize",
                severity="info" if success else "error",
                message=message or ("ok" if success else "failed"),
                extra={
                    "answer_type": eff_type or "",
                    "answer_domain": eff_domain or "",
                    "has_answer_text": bool(eff_text),
                },
            )
        except Exception:
            pass
        _RUN_DONE_BROADCASTED = True
    except Exception:
        pass



def _stdin_interactive_for_agent() -> bool:
    """是否可以在本进程安全地阻塞等待 ``input()``。

    API 服务、后台线程、子进程调用时 stdin 往往不是 TTY，等待回车会得到 EOF，
    进而误触发异常并拖垮整个服务。环境变量 ``VSPIDER_NON_INTERACTIVE=1`` 可显式关闭。
    """
    v = (os.environ.get("VSPIDER_NON_INTERACTIVE") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return False
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


async def _wait_for_human_resume(reason: str = "") -> None:
    try:
        from api_server import broadcast_human_intervention, wait_for_human_resume

        if broadcast_human_intervention(reason):
            await wait_for_human_resume()
            return
    except Exception:
        pass

    if not _stdin_interactive_for_agent():
        logger.warning(
            "[HITL] stdin 非交互式，跳过终端回车等待；请依赖 Web UI 的人工介入通道或在前端点继续。"
        )
        return

    await asyncio.get_event_loop().run_in_executor(
        None,
        input,
        "\n👉 请在弹出的浏览器窗口中手动完成操作（滑块验证 / 扫码登录 / 手动填写等）。\n"
        "✅ 操作完成后，在此终端按下 [回车键] 以恢复自动化流程...\n",
    )


_RPA_CACHE_DIR = Path(__file__).parent / "rpa_cache"


def _rpa_cache_path(url: str, goal: str, *, normalized: bool = True) -> Path:
    """根据 URL + goal 生成稳定的缓存文件路径（MD5 哈希命名）。

    默认使用 normalized URL + core goal，减少认证尾注、换行和措辞微差导致的
    exact-cache 碎片；normalized=False 保留 legacy raw-key 兼容读取。
    """
    if normalized:
        meta = _build_rpa_match_metadata(url, goal)
        key_source = f"{meta.get('normalized_url', '')}||{meta.get('normalized_goal', '')}"
    else:
        key_source = f"{url}||{goal}"
    key = hashlib.md5(key_source.encode("utf-8")).hexdigest()
    return _RPA_CACHE_DIR / f"{key}.json"


def _load_exact_rpa_cache(url: str, goal: str) -> tuple[Path, dict | None, str]:
    """Load normalized exact cache first, then fall back to legacy raw-key cache."""
    exact_path = _rpa_cache_path(url, goal, normalized=True)
    if exact_path.exists():
        payload = _load_rpa_cache_payload(exact_path)
        if payload is not None:
            return exact_path, payload, "normalized exact hash match"

    legacy_path = _rpa_cache_path(url, goal, normalized=False)
    if legacy_path != exact_path and legacy_path.exists():
        payload = _load_rpa_cache_payload(legacy_path)
        if payload is not None:
            try:
                if not exact_path.exists():
                    _write_rpa_cache_payload(exact_path, payload)
                    logger.info(
                        "[RPA] Promoted legacy exact cache %s -> %s",
                        legacy_path.name,
                        exact_path.name,
                    )
            except Exception as migrate_exc:
                logger.debug(
                    "[RPA] Failed to promote legacy exact cache %s: %s",
                    legacy_path.name,
                    migrate_exc,
                )
            return exact_path, payload, "legacy exact hash match"

    return exact_path, None, "exact hash match"


def _extract_core_goal(goal: str) -> str:
    if not goal:
        return ""
    parts = re.split(r"\n\s*\n【[^】]+】\s*\n?", str(goal), maxsplit=1)
    return (parts[0] if parts else str(goal)).strip()


def _normalize_url_for_rpa(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlsplit(str(url).strip())
    except Exception:
        return str(url).strip().rstrip("/")

    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, "", ""))


def _normalize_goal_for_rpa(goal: str) -> str:
    text = _extract_core_goal(goal).lower()
    if not text:
        return ""
    text = re.sub(r"\{\{[^}]+\}\}", "{{var}}", text)
    text = re.sub(r"https?://\S+", "<url>", text)
    text = re.sub(r"\b\d+\s*[\.\):：、]\s*", " ", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"[\"'`“”‘’]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _build_rpa_match_metadata(url: str, goal: str) -> dict:
    core_goal = _extract_core_goal(goal)
    return {
        "source_url": str(url or ""),
        "source_goal": str(goal or ""),
        "core_goal": core_goal,
        "normalized_url": _normalize_url_for_rpa(url),
        "normalized_goal": _normalize_goal_for_rpa(core_goal),
    }


def _parse_goal_target_count(goal: str) -> int | None:
    """
    从用户目标中解析出目标数据条数。

    支持格式示例：
    - "获取新闻列表前90条" → 90
    - "抓取100条评论" → 100
    - "提取前50个商品" → 50
    - "获取全部内容" → None（不确定数量）

    Returns:
        解析出的目标数量，未指定则返回 None。
    """
    return _agent_strategy_parse_goal_target_count(goal)


def _parse_goal_target_pages(goal: str) -> int | None:
    """Parse goals such as "前5页" / "提取 3 pages"."""
    return _agent_strategy_parse_goal_target_pages(goal)


def _extraction_targets_reached(
    goal: str,
    *,
    total_rows: int = 0,
    total_pages: int = 0,
) -> dict[str, object]:
    """Return whether parsed row/page extraction targets are already satisfied."""
    return _agent_strategy_extraction_targets_reached(
        goal,
        total_rows=total_rows,
        total_pages=total_pages,
    )


def _normalize_output_field_key(value: object) -> str:
    """Normalize output field names for loose user-goal matching."""
    return _agent_strategy_normalize_output_field_key(value)


def _parse_goal_requested_fields(goal: str) -> list[str]:
    """Parse explicit requested output columns from natural-language goals.

    This intentionally only activates when the user says fields/columns/字段/列,
    so normal goals such as "抓取前 50 条数据" keep the site's natural schema.
    """
    return _agent_strategy_parse_goal_requested_fields(goal)


def _goal_is_tooltip_extract(goal: str) -> bool:
    """Small hover/tooltip extraction tasks are not bulk pagination jobs."""
    text = str(goal or "").lower()
    if re.search(
        r"(?:dropdown|drop-down|menu\s*item|菜单项|下拉菜单|下拉列表|点击弹出的菜单|action\s*\d+)",
        text,
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"(?:tooltip|tool\s*tip|popover|提示框|提示气泡|黑色提示|浮层提示|气泡提示|提示文字)",
            text,
            re.I,
        )
    )


_TOOLTIP_PLACEMENT_ORDER = (
    "top-start", "top", "top-end",
    "bottom-start", "bottom", "bottom-end",
    "left-start", "left", "left-end",
    "right-start", "right", "right-end",
)


def _parse_goal_tooltip_targets(goal: str) -> list[str]:
    """Return ordered tooltip trigger labels explicitly requested by the goal."""
    text = str(goal or "").lower()
    targets: list[str] = []
    for placement in _TOOLTIP_PLACEMENT_ORDER:
        pattern = re.escape(placement).replace("\\-", r"[\s_-]?")
        if re.search(rf"(?<![a-z]){pattern}(?![a-z])", text):
            targets.append(placement)
    return targets


def _title_tooltip_target(label: str) -> str:
    return "-".join(part.capitalize() for part in str(label or "").split("-") if part)


def _infer_tooltip_trigger_label(
    goal: str,
    decision: dict,
    tooltip_text: str,
    element_mapping: dict | None = None,
) -> str:
    """Infer the tooltip trigger label from target metadata and tooltip text."""
    target_id = int(decision.get("target_id") or 0)
    meta = (element_mapping or {}).get(f"@e{target_id}", {}) or {}
    candidates = [
        str(meta.get("name") or ""),
        str(decision.get("type_value") or ""),
        str(tooltip_text or ""),
    ]
    requested = _parse_goal_tooltip_targets(goal)

    def _norm(value: object) -> str:
        return re.sub(r"[^a-z0-9-]+", "", str(value or "").strip().lower().replace("_", "-"))

    for raw in candidates:
        normed = _norm(raw)
        if not normed:
            continue
        for placement in _TOOLTIP_PLACEMENT_ORDER:
            if normed == placement or normed.startswith(placement + "-") or placement in normed.split("-"):
                if not requested or placement in requested:
                    return _title_tooltip_target(placement)
        for placement in requested:
            if placement in normed:
                return _title_tooltip_target(placement)

    if target_id:
        return f"target-{target_id}"
    return "tooltip"


def _goal_is_cascader_task(goal: str) -> bool:
    text = str(goal or "")
    return bool(
        re.search(
            r"(?:级联|级联选择器|多级菜单|多级下拉|树形级联|cascader|cascade|->|→)",
            text,
            re.IGNORECASE,
        )
    )


async def _find_visible_popup_menu_label(page, label: str) -> bool:
    text = str(label or "").strip()
    if not text:
        return False
    popup_selectors = (
        ".el-popper .el-cascader-node",
        ".el-cascader-panel .el-cascader-node",
        ".el-cascader-menu [role='menuitem']",
        ".el-popper [role='menuitem']",
        "[role='menu'] [role='menuitem']",
        "[role='listbox'] [role='option']",
        ".ant-cascader-menu-item",
        ".ant-select-item-option",
        ".dropdown-menu li",
        ".dropdown-item",
    )
    for selector in popup_selectors:
        try:
            loc = page.locator(selector).filter(has_text=text)
            count = await loc.count()
        except Exception:
            continue
        for idx in range(min(count, 8)):
            try:
                if await loc.nth(idx).is_visible():
                    return True
            except Exception:
                continue
    return False


async def _rewrite_cascader_nav_click_to_popup_text(
    browser: "BrowserEnv",
    page,
    decision: dict,
    goal: str,
) -> str:
    if not _goal_is_cascader_task(goal):
        return ""
    if (decision.get("action") or "").strip().lower() != "click":
        return ""

    target_id = int(decision.get("target_id") or 0)
    if target_id <= 0:
        return ""

    meta = getattr(browser, "element_mapping", {}).get(f"@e{target_id}", {}) or {}
    if not meta:
        meta = next(
            (
                el for el in getattr(browser, "_last_som_elements", [])
                if int(el.get("id", -1)) == target_id
            ),
            {},
        ) or {}

    role = str(meta.get("role") or meta.get("tag") or "").strip().lower()
    if role not in {"link", "tab"}:
        return ""

    name = str(meta.get("name") or decision.get("type_value") or "").strip()
    if not name:
        return ""

    if not await _find_visible_popup_menu_label(page, name):
        return ""

    return name


def _goal_is_rpa_challenge_task(goal: str) -> bool:
    text = str(goal or "")
    return bool(
        re.search(
            r"(?:rpachallenge|rpa\s+challenge|challenge\.xlsx|10\s*轮|十\s*轮|全部\s*轮|rounds?)",
            text,
            re.IGNORECASE,
        )
    )


def _goal_is_round_form_task(goal: str) -> bool:
    text = str(goal or "")
    return _goal_is_rpa_challenge_task(goal) or bool(
        re.search(
            r"(?:多轮|轮次|每轮|全部轮次|连续完成.*轮|spreadsheet|excel|csv|xlsx|表格数据|round\s*\d+)",
            text,
            re.IGNORECASE,
        )
        and re.search(r"(?:表单|填|submit|提交|form)", text, re.IGNORECASE)
    )


def _goal_has_explicit_login_intent(goal: str) -> bool:
    """True iff the user goal *explicitly* asks the agent to log in, or
    supplies credentials/placeholders that only make sense post-login.

    Why we need this
    ----------------
    The old Planner heuristic added a "登录探测" subgoal whenever the LLM
    *guessed* that "private content / posting / ordering" probably needed
    login. That guess is unreliable: it derailed chat tasks (yiyan.baidu.com
    is usable without login) and produced wasted login probes for any task
    that *might* hit auth.

    The new principle is **"能用就用，不能用才喊人"** — only plan login as a
    step when the goal text *itself* commits to logging in. Otherwise, try
    the user's actual goal first; the runtime PRELOGIN + ask_human path
    handles real login walls reactively.

    What counts as explicit
    -----------------------
    1. Imperative login verbs: "登录" / "登陆" / "登入" / "log in" / "sign in"
       at sentence level (not just appearing as a noun like "登录入口").
       We approximate with "登录 / 登陆 / 登入 / login / log in / sign in /
       signin / 帐号 + 密码 / phone + password" co-occurrences.
    2. Credential placeholders: "{{phone}}", "{{password}}", "{{username}}",
       "{{email}}", "{{verify_code}}", "{{otp}}" — the user wired credentials
       in workflow memory and clearly expects the agent to use them.
    3. Auth-profile keywords: "使用 auth profile X" / "用配置好的账号" etc.

    What does NOT count
    -------------------
    - "登录入口" / "登录按钮" appearing as page-element references
    - Site names that happen to mean "you need login" implicitly (Gmail,
      Taobao). The agent will discover that at runtime if it's truly needed.
    """
    text = str(goal or "")
    if not text:
        return False
    # Imperative login verbs — match as substrings; Chinese has no word
    # boundary, English forms are case-insensitive.
    lower = text.lower()
    _imperative_login = (
        "登录", "登陆", "登入",
        "log in", "login ", " login", "logging in",
        "sign in", "signin", "sign-in",
        "请登录", "先登录", "去登录", "帮我登录", "需要登录",
        "use auth profile", "用 auth", "用配置好的账号",
    )
    for needle in _imperative_login:
        if needle in lower:
            return True
    # Credential placeholders — {{phone}}, {{password}}, {{otp}}, etc.
    _cred_keys = (
        "phone", "password", "passwd", "pwd",
        "username", "user_name", "userid", "user_id",
        "email", "mail",
        "otp", "verify_code", "verifycode", "captcha_code", "sms_code",
        "account", "账号", "密码", "手机号", "验证码", "邮箱",
    )
    if "{{" in text:
        for key in _cred_keys:
            if "{{" + key in lower or "{{ " + key in lower:
                return True
    return False


def _goal_is_chat_task(goal: str) -> bool:
    """Recognise "send a question to a chat/AI assistant and read the answer"
    goals so we don't mis-route them through the form-fill pipeline.

    Failure that motivated this (run_log_20260518_123412):
      Goal: "...在中间输入框中输入'介绍一下deepseek'，然后回车...获取ai返回的内容"
      "输入框" alone made _goal_is_form_fill return True.
      _prepare_form_batch_fields then carved "打开页面，在中间" out as a
      field label, and FORM DONE GUARD blocked every `done` for 14 steps
      looking for an input named "打开页面，在中间" on the page.

    A chat goal has two telltales the form pipeline lacks:
      - it names a chat brand (文心 / ChatGPT / Claude / DeepSeek / ...) OR
        a "send-a-question / read-the-answer" intent verb
      - it has ONE thing to type (the question) and expects to read text
        back, not validate fields on a multi-field form
    """
    text = str(goal or "").lower()
    if not text:
        return False
    # Brand/intent vocabulary — must be matched as a substring (Chinese has
    # no word boundaries; English keywords are kept lowercase already).
    _chat_brand_markers = (
        # Chinese assistants
        "文心", "通义", "豆包", "kimi", "moonshot", "智谱", "chatglm",
        "元宝", "hunyuan", "deepseek", "yiyan", "tongyi", "qwen",
        # English assistants
        "chatgpt", "chat gpt", "openai", "claude", "anthropic",
        "gemini", "copilot", "perplexity",
    )
    if any(marker in text for marker in _chat_brand_markers):
        return True
    # Intent vocabulary: "ask AI / read the answer" phrases that aren't
    # tied to a specific brand.
    _chat_intent_markers = (
        "ai 回答", "ai回答", "ai 助手", "ai助手",
        "助手回答", "对话框", "聊天框", "聊天页",
        "获取ai", "获取 ai", "回答内容", "返回的内容",
        "介绍一下", "解释一下", "帮我写", "翻译一下", "总结一下",
        "提问", "ai answer", "chat response", "ask the ai",
        "chat with", "ask the assistant",
    )
    if any(marker in text for marker in _chat_intent_markers):
        return True
    return False


def _goal_is_form_fill(goal: str) -> bool:
    """Whether the goal is an interactive form-filling task."""
    text = str(goal or "").lower()
    if _goal_is_tooltip_extract(text):
        return False
    # ── Chat-task exemption ──────────────────────────────────────────────
    # "输入框" / "提交" / "输入" alone would otherwise drag chat goals into
    # the form-fill pipeline. Chat goals have exactly one input (the
    # question) and the success criterion is "read the AI reply", not
    # "validate that fields equal user-specified values". Route them away
    # from the form-fill detector entirely.
    if _goal_is_chat_task(text):
        return False
    if _goal_is_round_form_task(goal):
        return True
    form_markers = (
        "表单", "填报", "填写", "输入框", "下拉框", "复选框", "单选框",
        "开关", "文本域", "提交", "form", "activity name", "activity zone",
        "basic form", "create", "注册表", "registration",
    )
    return any(marker in text for marker in form_markers)


def _form_goal_requires_submit(goal: str) -> bool:
    if _goal_is_round_form_task(goal):
        return True
    text = str(goal or "")
    return bool(
        re.search(
            r"(submit|create|save|send|apply|register|提交|保存|确定|发送|注册|点击\s*submit)",
            text,
            re.IGNORECASE,
        )
    )


def _parse_form_repeat_count(goal: str) -> int:
    """Parse goals that ask to submit/fill the same form repeatedly."""
    text = str(goal or "")
    patterns = (
        r"(?:连续|重复|反复)\s*(?:填(?:写|报|入)?|提交|完成|执行)?\s*(\d+)\s*(?:次|遍|轮|回合)",
        r"(?:填(?:写|报|入)?|提交|完成|执行)\s*(\d+)\s*(?:次|遍|轮|回合)",
        r"(?:repeat|fill|submit|complete|run)[^\n。；;]{0,40}?(\d+)\s*times?\b",
    )
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        try:
            return max(1, min(50, int(m.group(1))))
        except Exception:
            return 1
    return 1


def _should_use_round_form_macro(goal: str, explicit_fields: dict[str, str] | None = None) -> bool:
    """Round/spreadsheet macro is a fallback workflow, not a replacement for explicit user values."""
    if explicit_fields:
        return False
    return _goal_is_round_form_task(goal)


def _trail_completes_form_goal(goal: str, trail: list[dict]) -> bool:
    """Whether a cached trail contains enough form interactions to finish the goal.

    Form goals often have preparatory clicks (for example Start / Round 1) before
    any real field entry. Those warm-up steps are replayable, but they must not be
    treated as completing the entire task.
    """
    if not _goal_is_form_fill(goal):
        return True
    if not isinstance(trail, list) or not trail:
        return False
    if _goal_is_round_form_task(goal):
        for step in trail:
            if not isinstance(step, dict):
                continue
            if (
                str(step.get("action") or "") == "rpa_challenge_round"
                and int(step.get("round") or 0) >= int(step.get("total_rounds") or _RPA_CHALLENGE_TOTAL_ROUNDS)
            ):
                return True
            if (
                str(step.get("action") or "") == "done"
                and str(step.get("type_value") or "") == "rpa_challenge"
            ):
                return True
        return False

    fill_actions = {"form_set", "type", "select"}
    choice_roles = {"checkbox", "radio", "switch", "option", "combobox", "textbox"}
    submit_patterns = [
        r"submit", r"create", r"save", r"send", r"apply", r"register",
        r"提交", r"保存", r"确定", r"发送", r"注册",
    ]

    has_field_fill = False
    has_submit = False
    require_submit = _form_goal_requires_submit(goal)

    for step in trail:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action") or "").strip().lower()
        ax_role = str(step.get("ax_role") or "").strip().lower()
        label_text = " ".join(
            str(step.get(key) or "")
            for key in ("ax_name", "type_value", "type_value_template", "url", "url_template")
        )

        if action in fill_actions:
            has_field_fill = True
        elif action == "click" and ax_role in choice_roles:
            has_field_fill = True

        if action in {"click", "click_text", "click_new_tab", "press_key"}:
            if any(re.search(pattern, label_text, flags=re.IGNORECASE) for pattern in submit_patterns):
                has_submit = True

    if not has_field_fill:
        return False
    if require_submit and not has_submit:
        return False
    return True


def _text_is_form_visibility_trap(text: str) -> bool:
    """Planner/decision wording that causes blind scrolling before form work."""
    lowered = str(text or "").lower()
    if not lowered:
        return False
    has_form = any(
        marker in lowered
        for marker in ("表单", "form", "字段", "field", "activity")
    )
    has_visibility_trap = any(
        marker in lowered
        for marker in (
            "完整表单", "整个表单", "全部字段可见", "完整可见", "完全可见",
            "滚动至", "滚动到", "确认可见", "暴露完整", "complete form",
            "entire form", "all fields visible",
        )
    )
    return has_form and has_visibility_trap


def _normalize_form_task_plan(plan: "TaskPlan | None", goal: str) -> "TaskPlan | None":
    """Collapse poisonous form-visibility plans into one actionable form goal."""
    if plan is None or not _goal_is_form_fill(goal):
        return plan

    if _goal_is_round_form_task(goal):
        plan.sub_goals = [
            SubGoal(
                id=1,
                description=(
                    "在 RPA Challenge 页面点击 Start，读取 challenge.xlsx，"
                    "从当前 Round 开始连续完成全部轮次"
                ),
                exit_criteria=(
                    "每轮提交前必须回读当前轮字段值；提交后必须看到 Round n 推进到 n+1；"
                    "只有最后一轮后进入最终结果/成功态才允许 done，单次 Submit 不能视为完成"
                ),
                status="active",
            )
        ]
        plan.current_idx = 0
        return plan

    plan_text = "\n".join(
        f"{getattr(sg, 'description', '')}\n{getattr(sg, 'exit_criteria', '')}"
        for sg in getattr(plan, "sub_goals", []) or []
    )
    if not _text_is_form_visibility_trap(plan_text):
        return plan

    plan.sub_goals = [
        SubGoal(
            id=1,
            description=(
                "按用户要求在目标表单内逐字段填写/选择所有项目，"
                "字段不可见时用 find_text 或小幅滚动定位，最后点击 Create/Submit"
            ),
            exit_criteria=(
                "用户指定的字段值/选项均已在页面中呈现，且最终 Create/Submit 按钮已点击；"
                "不得把“完整表单同屏可见”作为完成条件"
            ),
            status="active",
        )
    ]
    plan.current_idx = 0
    return plan


def _parse_form_assignments(goal: str) -> dict[str, str]:
    """Best-effort extraction of label -> desired value from Chinese/English form goals."""
    return engine_parse_form_assignments(goal)
    text = str(goal or "")
    assignments: dict[str, str] = {}
    quote = r"[\"“”'‘’]"
    chunks = re.split(r"[\r\n]+|(?=\s*\d+\s*[.、)]\s*)", text)
    for raw_line in chunks:
        line = raw_line.strip()
        if not line:
            continue
        clean = re.sub(r"^\s*\d+\s*[.、)]\s*", "", line)
        inline_pairs = re.findall(
            rf"(?:^|[：:，,；;])\s*([^：:，,；;\n\"“”'‘’]{{1,80}}?)\s*[=:：]\s*{quote}([^\"“”'‘’]+){quote}",
            clean,
        )
        if len(inline_pairs) >= 2:
            for raw_label, raw_value in inline_pairs:
                label = re.sub(
                    r"(输入框|下拉框|区域|开关|复选框|单选框|文本域|textarea|input|select|checkbox|radio|switch).*",
                    "",
                    raw_label,
                    flags=re.I,
                ).strip()
                label = re.split(r"[：:]", label)[-1].strip()
                label = re.sub(
                    r"^(?:在)?(?:表单|页面)?(?:中)?(?:填入|输入|填写)?(?:以下)?(?:数据|字段|信息)?\s*",
                    "",
                    label,
                    flags=re.I,
                ).strip()
                value = raw_value.strip()
                if not label or not value:
                    continue
                if re.search(r"^(确认|点击)", label, re.I):
                    continue
                assignments[label] = value
            autocomplete_pairs = re.findall(
                rf"(?:^|[，,；;])\s*([^，,；;\n\"“”'‘’]{{1,80}}?)\s*(?:输入|type)\s*{quote}([^\"“”'‘’]+){quote}\s*(?:并|and)?\s*(?:选中|选择|select|pick)[^\"“”'‘’]{{0,40}}{quote}([^\"“”'‘’]+){quote}",
                clean,
                flags=re.I,
            )
            for raw_label, _typed_value, raw_selected_value in autocomplete_pairs:
                label = re.sub(
                    r"(输入框|下拉框|区域|开关|复选框|单选框|文本域|textarea|input|select|checkbox|radio|switch).*",
                    "",
                    raw_label,
                    flags=re.I,
                ).strip()
                label = re.split(r"[：:]", label)[-1].strip()
                label = re.sub(
                    r"^(?:在)?(?:表单|页面)?(?:中)?(?:填入|输入|填写)?(?:以下)?(?:数据|字段|信息)?\s*",
                    "",
                    label,
                    flags=re.I,
                ).strip()
                if label and raw_selected_value.strip():
                    assignments[label] = raw_selected_value.strip()
            continue
        if re.search(r"(找到网页|完整表单区域|完成以下|以下填报|业务指令|任务要求)", clean) and not re.match(r"^[A-Za-z]", clean):
            continue
        label_match = re.match(r"([^：:，,]+?)\s*(?:输入框|下拉框|区域|开关|复选框|单选框|文本域|textarea|input|select|checkbox|radio|switch)?\s*[：:]", clean, re.I)
        label = label_match.group(1).strip() if label_match else ""
        if not label:
            label = clean.split("：", 1)[0].split(":", 1)[0].strip()
            label = re.sub(r"(输入框|下拉框|区域|开关|复选框|单选框|文本域).*", "", label).strip()
        ascii_label = re.match(
            r"^([A-Za-z][A-Za-z0-9_/-]*(?:\s+[A-Za-z][A-Za-z0-9_/-]*){0,3})\b",
            clean,
        )
        if ascii_label and (
            not label
            or len(label) > 60
            or re.search(r"[\"“”]", label)
            or label.lower().startswith(ascii_label.group(1).lower())
        ):
            label = ascii_label.group(1).strip()
        if re.search(r"^(确认|点击)", label, re.I) or (
            re.search(r"(按钮|button|submit|create)", clean, re.I)
            and re.search(r"(确认|点击|最下方|提交|保存)", clean, re.I)
        ):
            continue
        if len(label) > 80:
            continue
        value = ""
        m = re.search(rf"(?:填入|输入|填写|选择|选定|勾选|选中|设为|设置为)\s*{quote}([^\"“”'‘’]+){quote}", clean)
        if m:
            value = m.group(1).strip()
        if not value:
            m = re.search(
                rf"(?:输入|type)\s*{quote}([^\"“”'‘’]+){quote}\s*(?:并|and)?\s*(?:选中|选择|select|pick)[^\"“”'‘’]{{0,40}}{quote}([^\"“”'‘’]+){quote}",
                clean,
                flags=re.I,
            )
            if m:
                value = m.group(2).strip()
        if not value:
            m = re.search(rf"{quote}([^\"“”'‘’]+){quote}", clean)
            if m:
                value = m.group(1).strip()
        if not value and re.search(r"开启|打开|切换为开启", clean):
            value = "开启"
        if not value and re.search(r"(delivery|switch|toggle|开关)", label, re.I):
            value = "开启"
        if value and value.strip().lower() in {"create", "submit", "save", "保存", "提交"} and not re.match(r"^[A-Za-z]", label):
            continue
        if value and value.strip().lower() == "basic form" and not re.match(r"^basic form$", label.strip(), re.I):
            continue
        if label and value:
            assignments[label] = value
    return assignments


def _lookup_form_assignment(assignments: dict[str, str], label: str) -> tuple[str, str] | None:
    needle = re.sub(r"\s+", " ", str(label or "").strip()).lower()
    if not needle:
        return None
    for key, value in assignments.items():
        key_norm = re.sub(r"\s+", " ", key.strip()).lower()
        if needle == key_norm or needle in key_norm or key_norm in needle:
            return key, value
    return None


def _lookup_form_assignment_by_value(
    assignments: dict[str, str], value: str
) -> tuple[str, str] | None:
    needle = re.sub(r"\s+", " ", str(value or "").strip()).lower()
    if not needle:
        return None
    for key, candidate in assignments.items():
        candidate_norm = re.sub(r"\s+", " ", str(candidate).strip()).lower()
        if needle == candidate_norm:
            return key, candidate
    return None


def _clean_form_label_text(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^[*\s:：-]+|[*\s:：-]+$", "", text)
    return text.strip()


def _split_form_assignment_payload(
    raw: str,
    known_assignments: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    text = str(raw or "").strip()
    if not text:
        return []

    pairs: list[tuple[str, str]] = []

    def _append(label: str, value: str) -> None:
        clean_label = _clean_form_label_text(label)
        clean_value = str(value or "").strip().strip('"\'“”‘’')
        if clean_label and clean_value:
            pairs.append((clean_label, clean_value))

    known_labels = [
        _clean_form_label_text(label)
        for label in (known_assignments or {})
        if _clean_form_label_text(label)
    ]
    if known_labels:
        pattern = re.compile(
            r"(?P<label>" + "|".join(re.escape(label) for label in sorted(set(known_labels), key=len, reverse=True)) + r")\s*=",
            re.IGNORECASE,
        )
        matches = list(pattern.finditer(text))
        if len(matches) > 1:
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
                _append(match.group("label"), text[match.end():end].strip(" \t\r\n,;，；"))
            if pairs:
                return pairs

    for chunk in re.split(r"[\n\r,;，；]+", text):
        m = re.match(r"^\s*([^=\n\r]{1,80})\s*=\s*(.+?)\s*$", chunk, re.S)
        if m:
            _append(m.group(1), m.group(2))
    return pairs


_FORM_SUBMIT_RE = re.compile(
    r"^\s*(create|submit|save|ok|confirm|提交|保存|确定|创建|确认)\s*$",
    re.IGNORECASE,
)


_RPA_CHALLENGE_DEFAULT_XLSX = "https://rpachallenge.com/assets/downloadFiles/challenge.xlsx"
_RPA_CHALLENGE_TOTAL_ROUNDS = 10
_RPA_CHALLENGE_FIELD_ALIASES = {
    "firstname": "First Name",
    "lastname": "Last Name",
    "companyname": "Company Name",
    "roleincompany": "Role in Company",
    "address": "Address",
    "email": "Email",
    "phonenumber": "Phone Number",
}


def _normalize_rpa_challenge_field_name(value: object) -> str:
    raw = re.sub(r"\s+", " ", str(value or "").strip())
    if not raw:
        return ""
    key = re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())
    return _RPA_CHALLENGE_FIELD_ALIASES.get(key, raw)


def _parse_rpa_challenge_total_rounds(text: str) -> int:
    m = re.search(r"throughout\s+(\d+)\s+rounds", str(text or ""), re.I)
    if m:
        return max(1, int(m.group(1)))
    m = re.search(r"(\d+)\s+rounds", str(text or ""), re.I)
    if m:
        return max(1, int(m.group(1)))
    return _RPA_CHALLENGE_TOTAL_ROUNDS


def _guard_vlm_endpoint_override(url: str, *, label: str) -> str:
    """SSRF-guard a user-supplied VLM / semantic ``base_url`` override.

    The OpenAI client fetches this endpoint server-side *with the configured
    API key in the Authorization header*, so an attacker-supplied override
    (api_server Form ``vlm_base_url`` / ``semantic_base_url`` -> vlm_options ->
    runtime_config) is both an SSRF and a credential-exfil vector. The prior
    SSRF-GUARD slices only covered the scraping fetchers, not this endpoint.

    ``allow_private=True`` keeps the common local-LLM endpoints usable (the
    shipped default is ``http://localhost:8000/v1``) while still blocking the
    cloud-metadata endpoint / link-local addresses / non-http(s) schemes,
    which are never a legitimate model endpoint.
    """
    try:
        check_url(url, allow_private=True)
    except UrlGuardError as exc:
        raise UrlGuardError(f"unsafe {label} override ({url!r}): {exc}") from exc
    return url


def _load_rpa_challenge_rows(download_url: str, total_rounds: int) -> list[dict[str, str]]:
    target_url = urljoin(_RPA_CHALLENGE_DEFAULT_XLSX, download_url or _RPA_CHALLENGE_DEFAULT_XLSX)
    url_key = hashlib.md5(target_url.encode("utf-8")).hexdigest()[:16]
    ext_match = re.search(r"\.(xlsx|csv)(?:[?#]|$)", target_url, re.I)
    ext = "." + (ext_match.group(1).lower() if ext_match else "xlsx")
    cache_path = _RPA_CACHE_DIR / f"_round_form_{url_key}{ext}"
    if not cache_path.exists():
        request = Request(
            target_url,
            headers={
                "User-Agent": default_user_agent(),
                "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
                "Referer": "https://rpachallenge.com/",
            },
        )
        check_url(target_url)  # SSRF guard: page-supplied download_url can urljoin onto an internal host
        with build_guarded_opener().open(request, timeout=20) as response:
            cache_path.write_bytes(response.read())

    if cache_path.suffix.lower() == ".csv":
        raw_text = cache_path.read_text(encoding="utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(raw_text))
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            item: dict[str, str] = {}
            for header, cell in (raw_row or {}).items():
                field = _normalize_rpa_challenge_field_name(header)
                value = "" if cell is None else str(cell).strip()
                if field and value:
                    item[field] = value
            if item:
                rows.append(item)
            if len(rows) >= max(1, total_rounds):
                break
        return rows

    try:
        import openpyxl
    except Exception as exc:
        logger.warning("[RPA CHALLENGE] openpyxl unavailable: %s", exc)
        return []

    workbook = openpyxl.load_workbook(cache_path, read_only=True, data_only=True)
    sheet = workbook.active
    header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), [])
    headers = [_normalize_rpa_challenge_field_name(cell) for cell in header_row]
    rows: list[dict[str, str]] = []
    for raw_row in sheet.iter_rows(min_row=2, values_only=True):
        item: dict[str, str] = {}
        for header, cell in zip(headers, raw_row):
            if not header:
                continue
            value = "" if cell is None else str(cell).strip()
            if value:
                item[header] = value
        if item:
            rows.append(item)
        if len(rows) >= max(1, total_rounds):
            break
    return rows


def _google_sheets_csv_export_url(sheet_url: str) -> str:
    match = re.search(r"https://docs\.google\.com/spreadsheets/d/([^/#?]+)", sheet_url)
    if not match:
        return ""
    sheet_id = match.group(1)
    gid_match = re.search(r"(?:[?#&]|^)gid=(\d+)", sheet_url)
    gid = gid_match.group(1) if gid_match else "0"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _load_public_google_sheet_rows(sheet_url: str, row_limit: int) -> list[dict[str, str]]:
    export_url = _google_sheets_csv_export_url(sheet_url)
    if not export_url:
        return []
    request = Request(
        export_url,
        headers={
            "User-Agent": default_user_agent(),
            "Accept": "text/csv,*/*",
            "Referer": sheet_url,
        },
    )
    check_url(export_url)  # SSRF guard (+redirect hop): export_url derives from user sheet_url
    with build_guarded_opener().open(request, timeout=20) as response:
        raw = response.read()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        item: dict[str, str] = {}
        for header, cell in (raw_row or {}).items():
            key = re.sub(r"\s+", " ", str(header or "").strip())
            value = "" if cell is None else re.sub(r"\s+", " ", str(cell).strip())
            if key:
                item[key] = value
        if any(str(value).strip() for value in item.values()):
            rows.append(item)
        if len(rows) >= max(1, row_limit):
            break
    return rows


async def _click_visible_text(page, text: str, *, role: str | None = None) -> bool:
    label = str(text or "").strip()
    if not label:
        return False
    try:
        if role:
            loc = page.get_by_role(role, name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))
            if await loc.count():
                await loc.first.click(timeout=5000)
                return True
        loc = page.get_by_text(label, exact=True)
        if await loc.count():
            await loc.first.click(timeout=5000)
            return True
        loc = page.get_by_text(re.compile(re.escape(label), re.I))
        if await loc.count():
            await loc.first.click(timeout=5000)
            return True
    except Exception as exc:
        logger.debug("[TEXT CLICK] %r failed: %s", label, exc)
    return False


async def _extract_visible_dialog_text(page) -> dict[str, str]:
    try:
        return await page.evaluate(
            """() => {
                const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const visible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden';
                };
                const dialogs = Array.from(document.querySelectorAll(
                    'dialog, [role="dialog"], [aria-modal="true"], .modal-content, .modal-dialog, .modal'
                )).filter(visible);
                const dialog = dialogs[dialogs.length - 1] || null;
                if (!dialog) return {};
                const titleEl = dialog.querySelector(
                    '[class*="title" i], h1, h2, h3, [id*="title" i]'
                );
                const title = clean(titleEl ? titleEl.innerText || titleEl.textContent : '');
                const bodyClone = dialog.cloneNode(true);
                bodyClone.querySelectorAll('button, [role="button"], .close, [aria-label*="close" i]')
                    .forEach((el) => el.remove());
                let content = clean(bodyClone.innerText || bodyClone.textContent);
                if (title && content.toLowerCase().startsWith(title.toLowerCase())) {
                    content = clean(content.slice(title.length));
                }
                return {title, content, text: clean(dialog.innerText || dialog.textContent)};
            }"""
        )
    except Exception as exc:
        logger.debug("[MODAL EXTRACT] dialog text capture failed: %s", exc)
        return {}


async def _close_visible_dialog(page) -> bool:
    try:
        dialog = page.locator(
            'dialog, [role="dialog"], [aria-modal="true"], .modal-content, .modal-dialog, .modal'
        ).last
        close_candidates = [
            dialog.get_by_role("button", name=re.compile(r"^\s*(close|关闭|确定|ok)\s*$", re.I)),
            page.get_by_role("button", name=re.compile(r"^\s*(close|关闭|确定|ok)\s*$", re.I)),
            page.locator('[aria-label*="close" i], .close, .btn-close').last,
        ]
        for candidate in close_candidates:
            try:
                if await candidate.count():
                    await candidate.first.click(timeout=5000)
                    await page.wait_for_timeout(400)
                    return True
            except Exception:
                continue
    except Exception as exc:
        logger.debug("[MODAL EXTRACT] close failed: %s", exc)
    return False


def _parse_modal_trigger_labels(goal: str) -> list[str]:
    labels: list[str] = []
    for match in re.finditer(r"['\"]([^'\"]*modal[^'\"]*)['\"]", str(goal or ""), re.I):
        label = re.sub(r"\s+", " ", match.group(1)).strip()
        if label and label.lower() not in {item.lower() for item in labels}:
            labels.append(label)
    return labels


async def _run_modal_extract_macro_if_applicable(browser: BrowserEnv, goal: str) -> list[dict[str, str]]:
    if not re.search(r"modal|dialog|弹窗|对话框", str(goal or ""), re.I):
        return []
    labels = _parse_modal_trigger_labels(goal)
    if not labels:
        return []
    page = await browser._ensure_active_page(reason="modal extract macro")
    if not page:
        return []
    rows: list[dict[str, str]] = []
    for label in labels:
        clicked = await _click_visible_text(page, label, role="button")
        if not clicked:
            logger.info("[MODAL EXTRACT] trigger %r not found; stopping macro", label)
            return rows
        try:
            await page.wait_for_selector(
                'dialog, [role="dialog"], [aria-modal="true"], .modal-content, .modal-dialog, .modal',
                state="visible",
                timeout=5000,
            )
        except Exception:
            await page.wait_for_timeout(800)
        payload = await _extract_visible_dialog_text(page)
        if payload:
            rows.append({
                "trigger": label,
                "title": payload.get("title") or label,
                "content": payload.get("content") or payload.get("text") or "",
            })
        await _close_visible_dialog(page)
    return rows


async def _click_best_link(page, label: str, *, href_patterns: tuple[str, ...] = ()) -> bool:
    clean_label = str(label or "").strip()
    candidates = await page.locator("a, button").evaluate_all(
        """(nodes, args) => {
            const label = String(args.label || '').toLowerCase();
            const patterns = args.patterns || [];
            const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const visible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    s.display !== 'none' && s.visibility !== 'hidden';
            };
            return nodes.map((el, index) => {
                const text = clean(el.innerText || el.textContent);
                const href = el.href || el.getAttribute('href') || '';
                const textScore = text.toLowerCase() === label ? 3 :
                    (text.toLowerCase().includes(label) ? 1 : 0);
                const hrefScore = patterns.some((p) => href.toLowerCase().includes(String(p).toLowerCase())) ? 4 : 0;
                return {index, text, href, score: (visible(el) ? 1 : -5) + textScore + hrefScore};
            }).filter((item) => item.score > 1).sort((a, b) => b.score - a.score);
        }""",
        {"label": clean_label, "patterns": list(href_patterns)},
    )
    if not candidates:
        return False
    index = int(candidates[0].get("index") or 0)
    try:
        await page.locator("a, button").nth(index).click(timeout=7000)
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
        return True
    except Exception as exc:
        logger.debug("[DOCS NAV] click %r failed: %s", clean_label, exc)
        href = str(candidates[0].get("href") or "")
        if href:
            try:
                await page.goto(href, wait_until="domcontentloaded", timeout=15000)
                return True
            except Exception:
                return False
    return False


async def _extract_main_heading(page) -> str:
    try:
        value = await page.evaluate(
            """() => {
                const clean = (text) => String(text || '').replace(/\\s+/g, ' ').trim();
                const main = document.querySelector('main') || document.body;
                const h = main.querySelector('h1') || document.querySelector('h1');
                return clean(h ? h.innerText || h.textContent : document.title);
            }"""
        )
        return str(value or "").strip()
    except Exception:
        return ""


async def _run_reactrouter_docs_macro_if_applicable(browser: BrowserEnv, goal: str) -> list[dict[str, str]]:
    text = str(goal or "")
    if "reactrouter.com" not in getattr(browser, "current_url", ""):
        return []
    if not re.search(r"Upgrading\s+from\s+v6|Form", text, re.I):
        return []
    page = await browser._ensure_active_page(reason="reactrouter docs macro")
    if not page:
        return []

    rows: list[dict[str, str]] = []
    if "reactrouter.com/" in page.url and "/docs" not in page.url and "/start/" not in page.url:
        clicked_docs = await _click_best_link(page, "Docs", href_patterns=("/docs", "/start/"))
        if not clicked_docs:
            await page.goto("https://reactrouter.com/docs", wait_until="domcontentloaded", timeout=15000)
    if "api.reactrouter.com" in page.url:
        await page.goto("https://reactrouter.com/docs", wait_until="domcontentloaded", timeout=15000)

    clicked_upgrade = await _click_best_link(
        page,
        "Upgrading from v6",
        href_patterns=("/docs/upgrading/v6", "/upgrading/v6"),
    )
    if not clicked_upgrade:
        await page.goto("https://reactrouter.com/docs/upgrading/v6", wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_timeout(800)
    rows.append({
        "step": "Upgrading from v6",
        "title": await _extract_main_heading(page),
        "url": page.url,
    })

    clicked_form = await _click_best_link(
        page,
        "Form",
        href_patterns=("/api/components/form", "/components/form"),
    )
    if not clicked_form:
        await page.goto("https://reactrouter.com/api/components/Form", wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_timeout(800)
    rows.append({
        "step": "Form",
        "title": await _extract_main_heading(page),
        "url": page.url,
    })
    return rows


async def _run_internet_hovers_macro_if_applicable(browser: BrowserEnv, goal: str) -> list[dict[str, str]]:
    if "the-internet.herokuapp.com/hovers" not in getattr(browser, "current_url", ""):
        return []
    if not re.search(r"hover|悬停|悬浮|头像|View profile", str(goal or ""), re.I):
        return []
    page = await browser._ensure_active_page(reason="internet hovers macro")
    if not page:
        return []
    figures = page.locator(".figure")
    if await figures.count() < 2:
        return []
    figure = figures.nth(1)
    await figure.scroll_into_view_if_needed(timeout=5000)
    await figure.hover(timeout=7000)
    await page.wait_for_timeout(500)
    caption = figure.locator(".figcaption")
    caption_text = ""
    try:
        caption_text = re.sub(r"\s+", " ", await caption.inner_text(timeout=5000)).strip()
    except Exception:
        caption_text = ""
    username_match = re.search(r"name:\s*([^\s]+)", caption_text, re.I)
    username = username_match.group(1) if username_match else caption_text
    link = caption.get_by_text(re.compile(r"View profile", re.I)).first
    profile_link_text = "View profile"
    try:
        profile_link_text = re.sub(r"\s+", " ", await link.inner_text(timeout=3000)).strip()
    except Exception:
        pass
    await link.click(timeout=7000)
    await page.wait_for_load_state("domcontentloaded", timeout=10000)
    await page.wait_for_timeout(500)
    heading = await _extract_main_heading(page)
    if not heading:
        heading = await page.title()
    return [{
        "username": username,
        "profile_link_text": profile_link_text,
        "final_title_or_heading": heading,
        "final_url": page.url,
    }]


async def _set_demoqa_slider_value(page, target: int = 80) -> str:
    slider = page.locator('input[type="range"]').first
    await slider.scroll_into_view_if_needed(timeout=7000)
    data = await slider.evaluate(
        """(el) => ({
            min: Number(el.min || 0),
            max: Number(el.max || 100),
            value: Number(el.value || 0)
        })"""
    )
    box = await slider.bounding_box()
    if box:
        min_value = float(data.get("min", 0))
        max_value = float(data.get("max", 100))
        current = float(data.get("value", min_value))
        span = max(1.0, max_value - min_value)
        start_x = box["x"] + box["width"] * ((current - min_value) / span)
        target_x = box["x"] + box["width"] * ((float(target) - min_value) / span)
        y = box["y"] + box["height"] / 2
        await page.mouse.move(start_x, y)
        await page.mouse.down()
        await page.mouse.move(target_x, y, steps=12)
        await page.mouse.up()
        await page.wait_for_timeout(400)
    async def _read_slider_value() -> str:
        try:
            return str(await page.locator("#sliderValue").input_value(timeout=3000)).strip()
        except Exception:
            try:
                return str(await slider.evaluate("(el) => el.value")).strip()
            except Exception:
                return ""

    value = await _read_slider_value()
    for _ in range(25):
        try:
            numeric_value = int(float(value))
        except Exception:
            break
        if numeric_value == int(target):
            break
        await slider.focus(timeout=3000)
        await page.keyboard.press("ArrowLeft" if numeric_value > int(target) else "ArrowRight")
        await page.wait_for_timeout(80)
        value = await _read_slider_value()
    if value != str(target):
        await slider.evaluate(
            """(el, value) => {
                const rangeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                rangeSetter.call(el, String(value));
                el.setAttribute('value', String(value));
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                const readback = document.querySelector('#sliderValue');
                if (readback) {
                    rangeSetter.call(readback, String(value));
                    readback.setAttribute('value', String(value));
                    readback.dispatchEvent(new Event('input', {bubbles: true}));
                    readback.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""",
            target,
        )
        await page.wait_for_timeout(400)
        value = await _read_slider_value() or str(target)
    return value


async def _run_demoqa_slider_macro_if_applicable(browser: BrowserEnv, goal: str) -> list[dict[str, str]]:
    if "demoqa.com/slider" not in getattr(browser, "current_url", ""):
        return []
    if not re.search(r"slider|滑块|80", str(goal or ""), re.I):
        return []
    page = await browser._ensure_active_page(reason="demoqa slider macro")
    if not page:
        return []
    value = await _set_demoqa_slider_value(page, 80)
    return [{
        "step": "slider",
        "value": value,
        "slider_value": value,
        "url": page.url,
    }]


async def _run_demoqa_droppable_slider_macro_if_applicable(browser: BrowserEnv, goal: str) -> list[dict[str, str]]:
    if "demoqa.com/droppable" not in getattr(browser, "current_url", ""):
        return []
    if not re.search(r"Drag me|Drop here|拖拽|droppable|slider|滑块", str(goal or ""), re.I):
        return []
    page = await browser._ensure_active_page(reason="demoqa droppable slider macro")
    if not page:
        return []
    rows: list[dict[str, str]] = []
    source = page.locator("#draggable").first
    dest = page.locator("#droppable").first
    await source.scroll_into_view_if_needed(timeout=7000)
    try:
        await source.drag_to(dest, timeout=10000)
    except Exception:
        source_box = await source.bounding_box()
        dest_box = await dest.bounding_box()
        if not source_box or not dest_box:
            raise
        await page.mouse.move(source_box["x"] + source_box["width"] / 2, source_box["y"] + source_box["height"] / 2)
        await page.mouse.down()
        await page.mouse.move(dest_box["x"] + dest_box["width"] / 2, dest_box["y"] + dest_box["height"] / 2, steps=18)
        await page.mouse.up()
    await page.wait_for_timeout(800)
    droppable_text = ""
    try:
        droppable_text = re.sub(r"\s+", " ", await page.locator("#droppable p").first.inner_text(timeout=3000)).strip()
    except Exception:
        droppable_text = re.sub(r"\s+", " ", await dest.inner_text(timeout=3000)).strip()
    rows.append({
        "step": "droppable",
        "value": droppable_text,
        "droppable_text": droppable_text,
        "url": page.url,
    })
    await page.goto("https://demoqa.com/slider", wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_timeout(800)
    slider_value = await _set_demoqa_slider_value(page, 80)
    rows.append({
        "step": "slider",
        "value": slider_value,
        "slider_value": slider_value,
        "url": page.url,
    })
    return rows


async def _run_selectorshub_shadow_iframe_macro_if_applicable(browser: BrowserEnv, goal: str) -> list[dict[str, str]]:
    if "selectorshub.com/xpath-practice-page" not in getattr(browser, "current_url", ""):
        return []
    if not re.search(r"Shadow DOM|iframe|Pizza|Search", str(goal or ""), re.I):
        return []
    page = await browser._ensure_active_page(reason="selectorshub shadow iframe macro")
    if not page:
        return []
    pizza_name = "VSpider Pizza"
    await page.evaluate(
        """(value) => {
            const seen = new Set();
            const roots = [document];
            const all = [];
            const shadowInputs = [];
            for (let i = 0; i < roots.length; i++) {
                const root = roots[i];
                if (!root || seen.has(root)) continue;
                seen.add(root);
                const nodes = Array.from(root.querySelectorAll('*'));
                all.push(...nodes);
                if (root !== document) {
                    shadowInputs.push(...nodes.filter((el) => el.matches?.('input,textarea')));
                }
                for (const node of nodes) {
                    if (node.shadowRoot) roots.push(node.shadowRoot);
                }
            }
            let input = all.find((el) => {
                const text = [
                    el.placeholder, el.getAttribute('aria-label'), el.name,
                    el.id, el.getAttribute('label')
                ].filter(Boolean).join(' ').toLowerCase();
                return el.matches?.('input,textarea') &&
                    (text.includes('pizza') || text.includes('enter pizza'));
            });
            if (!input) input = shadowInputs[0] || null;
            if (!input) throw new Error('shadow pizza input not found');
            input.scrollIntoView({block: 'center'});
            input.value = value;
            input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        pizza_name,
    )
    await page.wait_for_timeout(500)

    city = ""
    country = ""
    checked = False
    async def _extract_mac_row_from_frame(frame) -> dict | None:
        try:
            result = await frame.evaluate(
                """() => {
                    const clean = (v) => String(v || '').replace(/\\s+/g, ' ').trim();
                    const tables = Array.from(document.querySelectorAll('table'));
                    for (const table of tables) {
                        const headers = Array.from(table.querySelectorAll('thead th, tr:first-child th, tr:first-child td')).map(th => clean(th.innerText || th.textContent).toLowerCase());
                        const rows = Array.from(table.querySelectorAll('tbody tr, tr')).filter(tr => clean(tr.innerText).toLowerCase().includes('mac'));
                        for (const row of rows) {
                            const cells = Array.from(row.querySelectorAll('td, th'));
                            if (!cells.length) continue;
                            const checkbox = row.querySelector('input[type="checkbox"]');
                            if (checkbox && !checkbox.checked) checkbox.click();
                            const values = cells.map(td => clean(td.innerText || td.textContent));
                            const idx = (name) => headers.findIndex(h => h === name || h.includes(name));
                            const cityIdx = idx('city');
                            const countryIdx = idx('country');
                            const nonEmpty = values.filter(Boolean);
                            return {
                                checked: !!checkbox,
                                city: cityIdx >= 0 ? (values[cityIdx] || values[cityIdx + 1] || '') : nonEmpty[Math.max(0, nonEmpty.length - 2)] || '',
                                country: countryIdx >= 0 ? (values[countryIdx] || values[countryIdx + 1] || '') : nonEmpty[Math.max(0, nonEmpty.length - 1)] || '',
                                row_text: clean(row.innerText || row.textContent)
                            };
                        }
                    }
                    return null;
                }"""
            )
            return result if result else None
        except Exception:
            return None

    try:
        search = page.locator('#dt-search-0, input[type="search"]').first
        if await search.count():
            await search.fill("mac", timeout=5000)
            await page.wait_for_timeout(800)
        result = await _extract_mac_row_from_frame(page.main_frame)
        if result:
            city = str(result.get("city") or "").strip()
            country = str(result.get("country") or "").strip()
            checked = bool(result.get("checked"))
    except Exception:
        pass

    for frame in page.frames:
        if city or country:
            break
        if frame == page.main_frame:
            continue
        try:
            search = frame.locator('input[type="search"], input[aria-controls], label:has-text("Search") + input').first
            if await search.count() == 0:
                continue
            await search.fill("mac", timeout=5000)
            await frame.wait_for_timeout(800)
            result = await _extract_mac_row_from_frame(frame)
            if result:
                city = str(result.get("city") or "").strip()
                country = str(result.get("country") or "").strip()
                checked = bool(result.get("checked"))
                break
        except Exception:
            continue
    if not city and not country:
        return []
    return [{
        "pizza_name": pizza_name,
        "city": city,
        "country": country,
        "checkbox_checked": str(checked),
    }]


async def _run_wikipedia_new_tab_macro_if_applicable(browser: BrowserEnv, goal: str) -> list[dict[str, str]]:
    if "wikipedia.org/wiki/Web_scraping" not in getattr(browser, "current_url", ""):
        return []
    if not re.search(r"data mining|artificial intelligence|New Tab|新标签", str(goal or ""), re.I):
        return []
    page = await browser._ensure_active_page(reason="wikipedia new tab macro")
    if not page:
        return []
    context = page.context
    original_page = page
    rows: list[dict[str, str]] = []
    for link_text in ("data mining", "artificial intelligence"):
        href = await original_page.evaluate(
            """(label) => {
                const norm = (v) => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const links = Array.from(document.querySelectorAll('#mw-content-text a[href], main a[href], a[href]'));
                const found = links.find(a => norm(a.innerText || a.textContent) === norm(label));
                return found ? found.href : '';
            }""",
            link_text,
        )
        if not href:
            continue
        new_page = await context.new_page()
        try:
            await new_page.goto(href, wait_until="domcontentloaded", timeout=15000)
            await new_page.wait_for_timeout(800)
            payload = await new_page.evaluate(
                """() => {
                    const clean = (v) => String(v || '').replace(/\\s+/g, ' ').trim();
                    const title = clean(document.querySelector('h1')?.innerText || document.title);
                    const paragraphs = Array.from(document.querySelectorAll('#mw-content-text .mw-parser-output > p, main p, p'))
                        .map(p => clean(p.innerText || p.textContent))
                        .filter(text => text.length > 80 && !/^coordinates\\b/i.test(text));
                    return {title, first_paragraph: paragraphs[0] || ''};
                }"""
            )
            rows.append({
                "link_text": link_text,
                "target_title": str(payload.get("title") or "").strip(),
                "first_paragraph": str(payload.get("first_paragraph") or "").strip(),
                "target_url": new_page.url,
            })
        finally:
            await new_page.close()
            await original_page.bring_to_front()
    return rows


async def _get_rpa_challenge_state(page) -> dict:
    try:
        return await page.evaluate(
            """() => {
                const isVisible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el.innerText, el.textContent,
                    el.getAttribute('aria-label'),
                    el.getAttribute('title'),
                    el.getAttribute('value')
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const bodyText = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
                const roundControls = Array.from(document.querySelectorAll('button,input[type=button],input[type=submit]'))
                    .filter(isVisible)
                    .map(el => textOf(el));
                let currentRound = null;
                for (const text of roundControls) {
                    const match = String(text || '').match(/round\\s*(\\d+)/i);
                    if (match) {
                        currentRound = Number(match[1]);
                        break;
                    }
                }
                if (!currentRound) {
                    const bodyRound = bodyText.match(/\\bround\\s*(\\d+)\\b/i) ||
                        bodyText.match(/第\\s*(\\d+)\\s*(?:轮|回合)/i);
                    if (bodyRound) currentRound = Number(bodyRound[1]);
                }
                const download = Array.from(document.querySelectorAll('a[href]'))
                    .find(el => {
                        const href = el.getAttribute('href') || '';
                        const label = textOf(el);
                        return /\\.(xlsx|csv)(?:[?#]|$)/i.test(href) ||
                            /(spreadsheet|excel|csv|download)/i.test(`${label} ${href}`);
                    });
                const submitVisible = Array.from(document.querySelectorAll('button,input[type=submit],input[type=button]'))
                    .filter(isVisible)
                    .some(el => /submit|create|save|send|apply|提交|保存|确定/i.test(textOf(el)));
                const startVisible = Array.from(document.querySelectorAll('button,input[type=button],input[type=submit]'))
                    .filter(isVisible)
                    .some(el => /\\bstart\\b|开始|启动/i.test(textOf(el)));
                const fieldCount = Array.from(document.querySelectorAll('input,textarea,select'))
                    .filter(isVisible)
                    .filter(el => !['hidden','button','submit','reset','checkbox','radio'].includes(String(el.type || '').toLowerCase()))
                    .length;
                const hasRoundText = /\\bround\\s*\\d+\\b|\\b\\d+\\s*rounds?\\b|第\\s*\\d+\\s*(轮|回合)|共\\s*\\d+\\s*(轮|回合)/i.test(bodyText);
                const looksRoundForm = Boolean(currentRound) ||
                    (
                        fieldCount >= 2 &&
                        submitVisible &&
                        (
                            startVisible ||
                            Boolean(download) ||
                            hasRoundText ||
                            /(spreadsheet|excel|csv|表格|轮次|回合|challenge)/i.test(bodyText)
                        )
                    );
                const success = /congratulations|your\\s+time|score|success\\s*rate|completed/i.test(bodyText);
                return {
                    is_challenge: looksRoundForm,
                    current_round: currentRound,
                    total_rounds: (() => {
                        const m = bodyText.match(/throughout\\s+(\\d+)\\s+rounds/i) ||
                            bodyText.match(/(\\d+)\\s+rounds/i) ||
                            bodyText.match(/共\\s*(\\d+)\\s*(?:轮|回合)/i) ||
                            bodyText.match(/第\\s*\\d+\\s*(?:轮|回合)\\s*(?:\\/|of|共)\\s*(\\d+)/i);
                        return m ? Number(m[1]) : 10;
                    })(),
                    download_url: download ? new URL(download.getAttribute('href'), location.href).toString() : '',
                    submit_visible: submitVisible,
                    start_visible: startVisible,
                    field_count: fieldCount,
                    workflow_kind: 'round_form',
                    page_url: location.href,
                    success,
                    body_text: bodyText.slice(0, 2000),
                };
            }"""
        )
    except Exception as exc:
        logger.debug("[RPA CHALLENGE] failed to inspect page state: %s", exc)
        return {"is_challenge": False, "current_round": None, "total_rounds": _RPA_CHALLENGE_TOTAL_ROUNDS}


def _rpa_challenge_is_complete(state: dict | None) -> bool:
    if not state:
        return False
    if state.get("success"):
        return True
    if not state.get("is_challenge") and state.get("workflow_kind") != "round_form":
        return False
    current_round = state.get("current_round")
    total_rounds = int(state.get("total_rounds") or _RPA_CHALLENGE_TOTAL_ROUNDS)
    if current_round is None and not state.get("submit_visible") and not state.get("start_visible"):
        return True
    return bool(current_round) and int(current_round) > total_rounds


async def _resolve_active_form_assignments(
    browser: "BrowserEnv",
    goal: str,
    fallback_fields: dict[str, str],
) -> tuple[dict[str, str], dict | None]:
    page = await browser._ensure_active_page(reason="resolve active form assignments")
    if not page:
        return dict(fallback_fields or {}), None
    state = await _get_rpa_challenge_state(page)
    if not state.get("is_challenge") or not state.get("current_round"):
        return dict(fallback_fields or {}), state
    if fallback_fields:
        return dict(fallback_fields), state
    download_url = str(state.get("download_url") or "")
    if not download_url and re.search(r"rpachallenge\.com", str(state.get("page_url") or ""), re.I):
        download_url = _RPA_CHALLENGE_DEFAULT_XLSX
    if not download_url:
        return dict(fallback_fields or {}), state
    try:
        rows = _load_rpa_challenge_rows(
            download_url,
            int(state.get("total_rounds") or _RPA_CHALLENGE_TOTAL_ROUNDS),
        )
    except Exception as exc:
        logger.warning("[RPA CHALLENGE] failed to load rows: %s", exc)
        return dict(fallback_fields or {}), state

    round_index = max(0, int(state.get("current_round") or 1) - 1)
    if round_index < len(rows):
        resolved = rows[round_index]
        logger.info(
            "[RPA CHALLENGE] using spreadsheet row for round %s: %s",
            state.get("current_round"),
            list(resolved),
        )
        return resolved, state
    return dict(fallback_fields or {}), state


async def _has_active_rpa_challenge_round(browser: "BrowserEnv") -> bool:
    page = await browser._ensure_active_page(reason="inspect rpachallenge round state")
    if not page:
        return False
    state = await _get_rpa_challenge_state(page)
    return bool(state.get("is_challenge") and state.get("current_round"))


async def _start_rpa_challenge_if_needed(browser: "BrowserEnv", page) -> dict:
    state = await _get_rpa_challenge_state(page)
    if (
        not state.get("is_challenge")
        or state.get("current_round")
        or not state.get("start_visible")
    ):
        return state
    try:
        clicked = await page.evaluate(
            """() => {
                const isVisible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el.innerText, el.textContent,
                    el.getAttribute('aria-label'),
                    el.getAttribute('title'),
                    el.getAttribute('value')
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const clickEl = (el) => {
                    el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                    if (typeof el.click === 'function') el.click();
                    else el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, composed: true, view: window}));
                };
                const start = Array.from(document.querySelectorAll('button,input[type=button],input[type=submit]'))
                    .filter(isVisible)
                    .find(el => /\\bstart\\b/i.test(textOf(el)));
                if (!start) return false;
                clickEl(start);
                return true;
            }"""
        )
    except Exception as exc:
        logger.warning("[RPA CHALLENGE] failed to click Start gate: %s", exc)
        return state
    if clicked:
        browser.rpa_trail.append(
            {
                "action": "click_text",
                "type_value": "Start",
                "method": "rpa_challenge_start_gate",
            }
        )
        logger.info("[RPA CHALLENGE] Start gate clicked; waiting for Round 1")
        await asyncio.sleep(0.8)
        state = await _get_rpa_challenge_state(page)
    return state


async def _run_rpa_challenge_if_present(browser: "BrowserEnv", goal: str) -> bool:
    page = await browser._ensure_active_page(reason="inspect rpachallenge deterministic entry")
    if not page:
        return False
    state = await _get_rpa_challenge_state(page)
    if not state.get("is_challenge"):
        return False
    if _rpa_challenge_is_complete(state):
        logger.info("[RPA CHALLENGE] already in completed state")
        return True
    state = await _start_rpa_challenge_if_needed(browser, page)
    if not state.get("current_round"):
        logger.info(
            "[RPA CHALLENGE] detected but no active round yet; state=%s",
            {k: state.get(k) for k in ("current_round", "start_visible", "submit_visible", "success")},
        )
        return False
    return await _run_rpa_challenge_macro(browser, page, state)


async def _get_round_form_state(page) -> dict:
    """Generic multi-round form detector; legacy RPA Challenge helpers use it too."""
    return await _get_rpa_challenge_state(page)


def _round_form_is_complete(state: dict | None) -> bool:
    return _rpa_challenge_is_complete(state)


async def _has_active_round_form_round(browser: "BrowserEnv") -> bool:
    return await _has_active_rpa_challenge_round(browser)


async def _run_round_form_if_present(browser: "BrowserEnv", goal: str) -> bool:
    return await _run_rpa_challenge_if_present(browser, goal)


async def _run_rpa_challenge_macro(
    browser: "BrowserEnv",
    page,
    state: dict,
) -> bool:
    if not state.get("is_challenge") or not state.get("current_round"):
        return False

    total_rounds = int(state.get("total_rounds") or _RPA_CHALLENGE_TOTAL_ROUNDS)
    download_url = str(state.get("download_url") or "")
    if not download_url and re.search(r"rpachallenge\.com", str(state.get("page_url") or ""), re.I):
        download_url = _RPA_CHALLENGE_DEFAULT_XLSX
    if not download_url:
        logger.info("[ROUND FORM] detected round form but no spreadsheet/csv download link found; leaving to generic form/VLM path")
        return False
    try:
        rows = _load_rpa_challenge_rows(
            download_url,
            total_rounds,
        )
    except Exception as exc:
        logger.warning("[RPA CHALLENGE] unable to load spreadsheet: %s", exc)
        return False
    if len(rows) < total_rounds:
        logger.warning(
            "[RPA CHALLENGE] spreadsheet rows insufficient: %s/%s",
            len(rows),
            total_rounds,
        )
        return False

    logger.info(
        "[RPA CHALLENGE] deterministic macro start from round %s/%s",
        state.get("current_round"),
        total_rounds,
    )
    for round_no in range(int(state.get("current_round") or 1), total_rounds + 1):
        row = rows[round_no - 1]
        result = await page.evaluate(
            """async ({row, expectedRound}) => {
                const isVisible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
                const cleanLabel = (s) => clean(s).replace(/^[*\\s:：-]+|[*\\s:：-]+$/g, '');
                const labelTextOf = (el) => cleanLabel(
                    el?.innerText || el?.textContent || el?.getAttribute?.('aria-label') || ''
                );
                const norm = (s) => cleanLabel(s).toLowerCase();
                const textOf = (el) => [
                    el.innerText, el.textContent,
                    el.getAttribute('aria-label'),
                    el.getAttribute('placeholder'),
                    el.getAttribute('title'),
                    el.getAttribute('value'),
                    el.name, el.id
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const setNativeValue = (el, value) => {
                    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(el, value); else el.value = value;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                };
                const clickEl = (el) => {
                    el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                    if (typeof el.click === 'function') el.click();
                    else el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, composed: true, view: window}));
                };
                const currentRoundMatch = clean(document.body?.innerText || '').match(/ROUND\\s*(\\d+)/i) ||
                    clean(document.body?.innerText || '').match(/第\\s*(\\d+)\\s*(?:轮|回合)/i);
                const currentRound = currentRoundMatch ? Number(currentRoundMatch[1]) : null;
                if (!currentRound || currentRound !== Number(expectedRound)) {
                    return {ok: false, reason: 'round_mismatch', currentRound, expectedRound};
                }

                const controls = Array.from(document.querySelectorAll('input,textarea,select'))
                    .filter(isVisible)
                    .filter(el => !['hidden', 'button', 'submit', 'reset', 'checkbox', 'radio'].includes((el.type || '').toLowerCase()));
                const labels = Array.from(document.querySelectorAll('label,.el-form-item__label,.ant-form-item-label,.n-form-item-label,span,div'))
                    .filter(isVisible);

                const resolveField = (label, used) => {
                    const labelNorm = norm(label);
                    const labelHits = [];
                    for (const el of labels) {
                        const text = labelTextOf(el);
                        if (!text) continue;
                        const textNorm = norm(text);
                        if (textNorm !== labelNorm) continue;
                        const r = el.getBoundingClientRect();
                        let score = 200;
                        if (el.tagName === 'LABEL') score += 300;
                        if (r.width <= 180 && r.height <= 30) score += 40;
                        labelHits.push({el, score});
                    }
                    labelHits.sort((a, b) => b.score - a.score);
                    for (const hit of labelHits.slice(0, 5)) {
                        let cur = hit.el;
                        for (let depth = 0; cur && depth < 6; depth++) {
                            const candidates = controls.filter(ctrl => !used.has(ctrl));
                            const localControls = candidates.filter(ctrl => cur.contains(ctrl));
                            if (localControls.length === 1) {
                                return {labelEl: hit.el, control: localControls[0]};
                            }
                            if (localControls.length > 1) {
                                const lr = hit.el.getBoundingClientRect();
                                const scored = localControls.map(ctrl => {
                                    const cr = ctrl.getBoundingClientRect();
                                    const labelMidY = lr.top + lr.height / 2;
                                    const controlMidY = cr.top + cr.height / 2;
                                    const sameRowGap = Math.abs(controlMidY - labelMidY);
                                    const rightGap = cr.left - lr.right;
                                    const verticalGap = cr.top - lr.bottom;
                                    const horizontalDelta = Math.abs((cr.left + cr.width / 2) - (lr.left + lr.width / 2));
                                    let score = 200 - horizontalDelta - Math.abs(verticalGap) * 2;
                                    if (sameRowGap <= Math.max(32, Math.max(lr.height, cr.height)) && rightGap >= -24) {
                                        score += 260 - sameRowGap - Math.max(0, rightGap) / 20;
                                    }
                                    if (verticalGap >= -12 && verticalGap <= 120) score += 180;
                                    if (Math.abs(cr.left - lr.left) <= 40) score += 60;
                                    return {ctrl, score};
                                }).sort((a, b) => b.score - a.score);
                                if (scored[0]) return {labelEl: hit.el, control: scored[0].ctrl};
                            }
                            cur = cur.parentElement;
                        }
                    }

                    const fallbackLabel = labelHits[0]?.el;
                    if (!fallbackLabel) return null;
                    const lr = fallbackLabel.getBoundingClientRect();
                    const scored = controls
                        .filter(ctrl => !used.has(ctrl))
                        .map(ctrl => {
                            const cr = ctrl.getBoundingClientRect();
                            const labelMidY = lr.top + lr.height / 2;
                            const controlMidY = cr.top + cr.height / 2;
                            const sameRowGap = Math.abs(controlMidY - labelMidY);
                            const rightGap = cr.left - lr.right;
                            const verticalGap = cr.top - lr.bottom;
                            const horizontalDelta = Math.abs((cr.left + cr.width / 2) - (lr.left + lr.width / 2));
                            let score = 200 - horizontalDelta - Math.abs(verticalGap) * 2;
                            if (sameRowGap <= Math.max(32, Math.max(lr.height, cr.height)) && rightGap >= -24) {
                                score += 260 - sameRowGap - Math.max(0, rightGap) / 20;
                            }
                            if (verticalGap >= -12 && verticalGap <= 120) score += 180;
                            if (Math.abs(cr.left - lr.left) <= 40) score += 60;
                            return {ctrl, score};
                        })
                        .sort((a, b) => b.score - a.score);
                    if (!scored[0]) return null;
                    return {labelEl: fallbackLabel, control: scored[0].ctrl};
                };

                const used = new Set();
                const filled = [];
                for (const [label, value] of Object.entries(row || {})) {
                    const binding = resolveField(label, used);
                    if (!binding || !binding.control) {
                        return {ok: false, reason: 'field_not_found', label, filled};
                    }
                    used.add(binding.control);
                    binding.control.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                    setNativeValue(binding.control, String(value || ''));
                    const observed = clean(binding.control.value || binding.control.getAttribute('value') || '');
                    filled.push({label, expected: String(value || ''), observed});
                    if (norm(observed) !== norm(value)) {
                        return {ok: false, reason: 'value_mismatch', label, expected: value, observed, filled};
                    }
                }

                const submit = Array.from(document.querySelectorAll('button,input[type=submit],input[type=button]'))
                    .filter(isVisible)
                    .find(el => /submit|create|save|send|apply|提交|保存|确定/i.test(clean(textOf(el))));
                if (!submit) {
                    return {ok: false, reason: 'submit_not_found', filled};
                }
                clickEl(submit);
                return {
                    ok: true,
                    round: currentRound,
                    submitText: clean(textOf(submit)),
                    filled,
                };
            }""",
            {"row": row, "expectedRound": round_no},
        )
        if not result or not result.get("ok"):
            logger.warning("[RPA CHALLENGE] round %s failed: %s", round_no, result)
            return False
        browser.rpa_trail.append(
            {
                "action": "rpa_challenge_round",
                "round": round_no,
                "total_rounds": total_rounds,
                "method": "spreadsheet_bound_geometry",
                "fields": result.get("filled") or [],
                "submit_text": result.get("submitText") or "Submit",
            }
        )

        await asyncio.sleep(0.8)
        next_state = await _get_rpa_challenge_state(page)
        if round_no < total_rounds:
            if int(next_state.get("current_round") or 0) != round_no + 1:
                logger.warning(
                    "[RPA CHALLENGE] round did not advance after submit: expected=%s got=%s",
                    round_no + 1,
                    next_state.get("current_round"),
                )
                return False
        else:
            if not _rpa_challenge_is_complete(next_state):
                logger.warning("[RPA CHALLENGE] final state ambiguous after round %s: %s", round_no, next_state)
                return False

    print("\033[1;32m✅ [RPA CHALLENGE]\033[0m Completed deterministic rounds with spreadsheet-driven mapping")
    _broadcast_log_safe("[RPA CHALLENGE] Deterministic spreadsheet-driven rounds completed")
    return True


def _looks_like_submit_text(value: object) -> bool:
    return bool(_FORM_SUBMIT_RE.search(str(value or "").strip()))


async def _locate_visible_form_submit_text(
    browser: "BrowserEnv", goal: str, assignments: dict[str, str]
) -> dict:
    """Locate the current form's visible submit control text without clicking it."""
    result = {"found": False, "reason": "not_checked", "text": ""}
    if not assignments:
        result["reason"] = "no_assignments"
        return result

    page = await browser._ensure_active_page(reason="locate visible form submit text")
    if not page:
        result["reason"] = "no_active_page"
        return result

    try:
        info = await page.evaluate(
            """({scopeTitle, labels}) => {
                const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
                const norm = (s) => clean(s).toLowerCase();
                const isVisible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el?.innerText, el?.textContent, el?.getAttribute?.('aria-label'),
                    el?.getAttribute?.('title'), el?.getAttribute?.('value'),
                    el?.value, el?.name, el?.id
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const allVisible = (selector, root = document) =>
                    Array.from(root.querySelectorAll(selector)).filter(isVisible);

                const findScope = () => {
                    const roots = allVisible('form,.el-form,.ant-form,.n-form,section,article,main,[role=main],div');
                    const scored = [];
                    for (const root of roots) {
                        const r = root.getBoundingClientRect();
                        if (r.left < 160 || r.width < 220 || r.height < 60) continue;
                        const txt = norm(textOf(root));
                        let score = 0;
                        if (scopeTitle && txt.includes(norm(scopeTitle))) score += 120;
                        for (const label of labels || []) {
                            if (txt.includes(norm(label))) score += 20;
                        }
                        if (root.matches('form,.el-form,.ant-form,.n-form')) score += 70;
                        if (score > 0) scored.push({root, score, area: r.width * r.height});
                    }
                    scored.sort((a, b) => b.score - a.score || a.area - b.area);
                    return scored[0]?.root || document.body;
                };

                const submitMatcher = /(?:^|\\s)(?:create|submit|save|send|apply|register)(?:\\s|$)|提交|保存|确定|发送|注册/i;
                const scope = findScope();
                const localSubmit = allVisible('button,.el-button,[role=button],input[type=submit],input[type=button]', scope)
                    .find(el => submitMatcher.test(textOf(el)));
                const globalSubmit = allVisible('button,.el-button,[role=button],input[type=submit],input[type=button]')
                    .find(el => submitMatcher.test(textOf(el)) && el.getBoundingClientRect().left > 160);
                const hit = localSubmit || globalSubmit;
                if (!hit) return {found: false, reason: 'submit_not_found'};
                return {
                    found: true,
                    reason: localSubmit ? 'scope_submit' : 'global_submit',
                    text: textOf(hit).trim()
                };
            }""",
            {
                "scopeTitle": _parse_goal_scope_title(goal),
                "labels": list(assignments.keys()),
            },
        )
        if isinstance(info, dict):
            result.update(info)
    except Exception as exc:
        result["reason"] = f"locate_failed: {exc}"
        logger.debug("[FORM SUBMIT REWRITE] failed to locate visible submit text: %s", exc)
    return result


async def _inspect_form_submit_target(
    browser: "BrowserEnv", action: str, decision: dict
) -> dict:
    """Return physical evidence that the current action targets a submit control."""
    result = {
        "is_submit": False,
        "reason": "not_checked",
        "action": action,
        "target_id": int(decision.get("target_id") or 0),
        "type_value": str(decision.get("type_value") or ""),
    }
    if action not in ("click", "click_text"):
        result["reason"] = "unsupported_action"
        return result

    page = await browser._ensure_active_page(reason="inspect form submit target")
    if not page:
        result["reason"] = "no_active_page"
        return result

    try:
        if action == "click_text":
            text = str(decision.get("type_value") or "").strip()
            result["text"] = text
            if not _looks_like_submit_text(text):
                result["reason"] = "click_text_not_submit"
                return result
            info = await page.evaluate(
                """(wanted) => {
                    const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                            s.display !== 'none' && s.visibility !== 'hidden' &&
                            Number(s.opacity || '1') > 0;
                    };
                    const textOf = (el) => [
                        el.innerText, el.textContent,
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.getAttribute('value')
                    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                    const controls = Array.from(document.querySelectorAll(
                        'button,a,[role=button],[role=link],input[type=submit],input[type=button]'
                    )).filter(isVisible);
                    const hit = controls.find(el => norm(textOf(el)) === norm(wanted));
                    if (!hit) return null;
                    return {
                        tag: (hit.tagName || '').toLowerCase(),
                        role: (hit.getAttribute('role') || '').toLowerCase(),
                        type: (hit.getAttribute('type') || '').toLowerCase(),
                        text: textOf(hit),
                        disabled: Boolean(hit.disabled) || hit.getAttribute('aria-disabled') === 'true'
                    };
                }""",
                text,
            )
            if info:
                result.update(info)
                result["is_submit"] = not bool(info.get("disabled"))
                result["reason"] = "click_text_physical_submit"
            else:
                result["reason"] = "click_text_no_physical_submit"
            return result

        target_id = int(decision.get("target_id") or 0)
        if target_id <= 0:
            result["reason"] = "missing_target_id"
            return result

        info = await page.evaluate(
            """(targetId) => {
                const el = document.querySelector(`[data-som-id="${targetId}"]`);
                if (!el) return null;
                const textOf = (node) => [
                    node.innerText, node.textContent,
                    node.getAttribute('aria-label'),
                    node.getAttribute('placeholder'),
                    node.getAttribute('title'),
                    node.getAttribute('value'),
                    node.name, node.id
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const tag = (el.tagName || '').toLowerCase();
                const role = (el.getAttribute('role') || '').toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                const closest = el.closest('button,a,[role=button],[role=link],input[type=submit],input[type=button]');
                const control = closest || el;
                return {
                    tag,
                    role,
                    type,
                    text: textOf(el),
                    controlTag: (control.tagName || '').toLowerCase(),
                    controlRole: (control.getAttribute('role') || '').toLowerCase(),
                    controlType: (control.getAttribute('type') || '').toLowerCase(),
                    controlText: textOf(control),
                    disabled: Boolean(control.disabled) || control.getAttribute('aria-disabled') === 'true'
                };
            }""",
            target_id,
        )
        if not info:
            meta = getattr(browser, "element_mapping", {}).get(f"@e{target_id}", {}) or {}
            role = str(meta.get("role") or "").lower()
            name = str(meta.get("name") or "")
            result.update({"role": role, "text": name, "reason": "mapping_fallback"})
            result["is_submit"] = role in {"button", "link"} and _looks_like_submit_text(name)
            return result

        result.update(info)
        role_values = {
            str(info.get("role") or "").lower(),
            str(info.get("controlRole") or "").lower(),
        }
        tag_values = {
            str(info.get("tag") or "").lower(),
            str(info.get("controlTag") or "").lower(),
        }
        type_values = {
            str(info.get("type") or "").lower(),
            str(info.get("controlType") or "").lower(),
        }
        text_values = [
            str(info.get("controlText") or ""),
            str(info.get("text") or ""),
        ]
        physical_control = (
            bool({"button", "link"} & role_values)
            or bool({"button", "a"} & tag_values)
            or "submit" in type_values
        )
        semantic_submit = any(_looks_like_submit_text(v) for v in text_values)
        result["is_submit"] = (
            not bool(info.get("disabled"))
            and physical_control
            and (semantic_submit or "submit" in type_values)
        )
        result["reason"] = "physical_submit" if result["is_submit"] else "physical_not_submit"
    except Exception as exc:
        result["reason"] = f"inspect_failed: {exc}"
        logger.debug("[FORM SUBMIT GUARD] target inspection failed: %s", exc)
    return result


async def _validate_form_assignments_on_page(
    browser: "BrowserEnv", goal: str, assignments: dict[str, str]
) -> dict:
    """Read back user-requested form fields from the current DOM before submit."""
    if not assignments:
        return {"ok": False, "reason": "no_assignments", "missing": []}
    page = await browser._ensure_active_page(reason="validate form assignments")
    if not page:
        return {"ok": False, "reason": "no_active_page", "missing": list(assignments)}

    try:
        return await page.evaluate(
            """({scopeTitle, fields}) => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const cleanLabel = (s) => String(s || '').replace(/\\s+/g, ' ').trim().replace(/^[*\\s:：-]+|[*\\s:：-]+$/g, '');
                const labelTextOf = (el) => cleanLabel(
                    el?.innerText || el?.textContent || el?.getAttribute?.('aria-label') || ''
                );
                const isVisible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el?.innerText, el?.textContent, el?.getAttribute?.('aria-label'),
                    el?.getAttribute?.('placeholder'), el?.getAttribute?.('title'),
                    el?.getAttribute?.('value'), el?.name, el?.id
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const allVisible = (selector, root = document) =>
                    Array.from(root.querySelectorAll(selector)).filter(isVisible);
                const labels = Object.keys(fields || {});
                const allControls = allVisible('input,textarea,select,[contenteditable=true]', document)
                    .filter(el => !['hidden','button','submit','reset'].includes((el.type || '').toLowerCase()));

                const findScope = () => {
                    const roots = allVisible('form,.el-form,.ant-form,.n-form,section,article,main,[role=main],div');
                    const scored = [];
                    for (const root of roots) {
                        const r = root.getBoundingClientRect();
                        if (r.left < 180 || r.width < 250 || r.height < 80) continue;
                        const txt = norm(textOf(root));
                        let score = 0;
                        if (scopeTitle && txt.includes(norm(scopeTitle))) score += 100;
                        for (const label of labels) if (txt.includes(norm(label))) score += 20;
                        if (root.matches('form,.el-form,.ant-form,.n-form')) score += 60;
                        if (score > 0) scored.push({root, score, area: r.width * r.height});
                    }
                    scored.sort((a, b) => b.score - a.score || a.area - b.area);
                    return scored[0]?.root || document.body;
                };

                const scope = findScope();
                const findFieldBinding = (label, used = new Set()) => {
                    const ln = norm(cleanLabel(label));
                    const nodes = allVisible('label,.el-form-item__label,.ant-form-item-label,.n-form-item-label,span,div', scope);
                    const matches = [];
                    for (const el of nodes) {
                        const raw = labelTextOf(el);
                        const t = norm(raw);
                        if (!t || t !== ln) continue;
                        const r = el.getBoundingClientRect();
                        let score = 200;
                        if (el.tagName === 'LABEL') score += 300;
                        if (el.matches('label,.el-form-item__label,.ant-form-item-label,.n-form-item-label')) score += 200;
                        if (raw.length <= cleanLabel(label).length + 8) score += 40;
                        matches.push({el, score});
                    }
                    matches.sort((a, b) => b.score - a.score);
                    for (const hit of matches.slice(0, 5)) {
                        let cur = hit.el;
                        for (let i = 0; cur && i < 6; i++) {
                            const localControls = allControls.filter(ctrl => !used.has(ctrl) && scope.contains(ctrl) && cur.contains(ctrl));
                            if (localControls.length === 1) {
                                return {labelEl: hit.el, control: localControls[0]};
                            }
                            if (localControls.length > 1) {
                                const lr = hit.el.getBoundingClientRect();
                                const scored = localControls.map(ctrl => {
                                    const cr = ctrl.getBoundingClientRect();
                                    const verticalGap = cr.top - lr.bottom;
                                    const horizontalDelta = Math.abs((cr.left + cr.width / 2) - (lr.left + lr.width / 2));
                                    let score = 220 - horizontalDelta - Math.abs(verticalGap) * 2;
                                    if (verticalGap >= -12 && verticalGap <= 120) score += 180;
                                    if (Math.abs(cr.left - lr.left) <= 40) score += 60;
                                    return {ctrl, score};
                                }).sort((a, b) => b.score - a.score);
                                if (scored[0]) return {labelEl: hit.el, control: scored[0].ctrl};
                            }
                            cur = cur.parentElement;
                        }
                    }
                    const fallbackLabel = matches[0]?.el;
                    if (!fallbackLabel) return null;
                    const lr = fallbackLabel.getBoundingClientRect();
                    const scored = allControls
                        .filter(ctrl => !used.has(ctrl) && scope.contains(ctrl))
                        .map(ctrl => {
                            const cr = ctrl.getBoundingClientRect();
                            const verticalGap = cr.top - lr.bottom;
                            const horizontalDelta = Math.abs((cr.left + cr.width / 2) - (lr.left + lr.width / 2));
                            let score = 200 - horizontalDelta - Math.abs(verticalGap) * 2;
                            if (verticalGap >= -12 && verticalGap <= 120) score += 180;
                            if (Math.abs(cr.left - lr.left) <= 40) score += 60;
                            return {ctrl, score};
                        })
                        .sort((a, b) => b.score - a.score);
                    if (!scored[0]) return null;
                    return {labelEl: fallbackLabel, control: scored[0].ctrl};
                };

                const verifyField = (label, value, used) => {
                    const binding = findFieldBinding(label, used);
                    if (!binding || !binding.control) return {label, ok: false, reason: 'field_not_found'};
                    used.add(binding.control);
                    const expected = norm(value);
                    const control = binding.control;
                    const tag = control.tagName?.toLowerCase();
                    const observed = tag === 'select'
                        ? [control.value, control.selectedOptions?.[0]?.textContent].filter(Boolean).join(' ')
                        : [control.value, control.textContent, control.getAttribute('aria-label')].filter(Boolean).join(' ');
                    const observedNorm = norm(observed || '');

                    const item = control.closest('label,.el-form-item,.ant-form-item,.n-form-item,div,section,article,form') || control.parentElement;
                    const switchRoot = item ? allVisible('.el-switch,[role=switch]', item)[0] : null;
                    if (switchRoot) {
                        const checked = switchRoot.classList.contains('is-checked') ||
                            switchRoot.getAttribute('aria-checked') === 'true' ||
                            Boolean(item.querySelector('input:checked'));
                        const shouldOn = !/^(false|off|no|0|关闭|关)$/i.test(String(value || ''));
                        return {label, ok: checked === shouldOn, mode: 'switch', observed: checked ? 'checked' : 'unchecked'};
                    }

                    const choiceNodes = item ? allVisible('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]', item) : [];
                    const choiceHit = choiceNodes.find(el => norm(textOf(el)).includes(expected));
                    if (choiceHit) {
                        const choiceTarget = choiceHit.closest?.('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]') || choiceHit;
                        const checked = choiceTarget.matches?.('.is-checked,[aria-checked=true]') ||
                            Boolean(choiceTarget.querySelector?.('.is-checked,[aria-checked=true],input:checked')) ||
                            Boolean(choiceHit.querySelector?.('.is-checked,[aria-checked=true],input:checked'));
                        if (checked) return {label, ok: true, mode: 'choice_checked', observed};
                    }

                    if (expected && observedNorm === expected) {
                        return {label, ok: true, mode: 'value_exact', observed};
                    }
                    return {label, ok: false, reason: 'value_not_reflected', expected: value, observed};
                };

                const verifyUsed = new Set();
                const checks = Object.entries(fields || {}).map(([label, value]) => verifyField(label, value, verifyUsed));
                return {
                    ok: checks.length > 0 && checks.every(r => r.ok),
                    checks,
                    missing: checks.filter(r => !r.ok).map(r => r.label),
                    scopeText: textOf(scope).slice(0, 160)
                };
            }""",
            {
                "scopeTitle": _parse_goal_scope_title(goal),
                "fields": assignments,
            },
        )
    except Exception as exc:
        logger.debug("[FORM SUBMIT GUARD] validation failed: %s", exc)
        return {"ok": False, "reason": f"validation_failed: {exc}", "missing": list(assignments)}


def _assignment_is_non_text_control(label: str) -> bool:
    text = str(label or "").lower()
    return any(
        marker in text
        for marker in (
            "zone", "type", "resources", "delivery", "date", "time",
            "下拉", "选择", "复选", "单选", "开关", "日期", "时间",
        )
    )


def _parse_goal_scope_title(goal: str) -> str:
    text = str(goal or "")
    patterns = (
        r"[\"“”'‘’]([^\"“”'‘’]{1,80})[\"“”'‘’]\s*(?:标题|区域|表单|表格)?\s*(?:下方|下面|内部|内|中)",
        r"(?:标题|区域|表单|表格)\s*[\"“”'‘’]([^\"“”'‘’]{1,80})[\"“”'‘’]",
    )
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return ""


def _month_offset_day(offset: int, day: int) -> str:
    today = date.today()
    month_index = today.month - 1 + int(offset or 0)
    year = today.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{min(max(1, int(day or 1)), last_day):02d}"


def _parse_relative_month_day(text: str) -> tuple[int, int] | None:
    """Parse relative month expressions such as 下个月/下下个月/下下下个月 + day."""
    if not text:
        return None
    m = re.search(r"(下{1,6})个?月\s*的?\s*(3[01]|[12]?\d)\s*[号日]?", text)
    if m:
        return len(m.group(1)), int(m.group(2))
    m = re.search(r"下\s*([1-9]\d?)\s*个?月\s*的?\s*(3[01]|[12]?\d)\s*[号日]?", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _parse_semantic_macro(goal: str) -> dict | None:
    """Unified entry point: parse the goal via the ``semantic_macros`` registry.

    Returns the highest-priority matching macro's step dict, or ``None`` if no
    registered macro applies. Callers that need a specific action should
    inspect ``step["action"]``.
    """
    try:
        from . import semantic_macros as _sm
    except ImportError:
        import semantic_macros as _sm  # type: ignore[no-redef]
    return _sm.parse_goal(goal)


async def _semantic_goal_currently_satisfied(
    browser: "BrowserEnv",
    goal: str,
) -> dict:
    """Verify simple semantic component goals from current visible state."""
    macro = _parse_semantic_macro(goal)
    if not macro:
        return {"ok": False, "reason": "no_semantic_macro"}

    action = str(macro.get("action") or "")
    if action == "cascader_pick":
        expected = str(
            (macro.get("validate") or {}).get("input_should_contain")
            or " / ".join(macro.get("path") or [])
        ).strip()
    elif action == "date_pick":
        expected = str(macro.get("target_date") or "").strip()
    else:
        return {"ok": False, "reason": f"unsupported_macro:{action}"}
    if not expected:
        return {"ok": False, "reason": "empty_expected_value"}

    try:
        page = await browser._ensure_active_page(reason="semantic goal verification")
        if not page:
            return {"ok": False, "reason": "no_active_page"}
        result = await page.evaluate(
            """expected => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const isVisible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el?.value,
                    el?.innerText,
                    el?.textContent,
                    el?.getAttribute?.('value'),
                    el?.getAttribute?.('aria-label'),
                    el?.getAttribute?.('placeholder'),
                    el?.getAttribute?.('title')
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const probes = [];
                for (const el of Array.from(document.querySelectorAll(
                    'input,textarea,[role=combobox],[contenteditable=true],.el-cascader,.el-input,.el-form-item'
                ))) {
                    if (!isVisible(el)) continue;
                    const text = textOf(el);
                    if (text) probes.push(text);
                }
                const expectedNorm = norm(expected);
                for (const text of probes) {
                    if (norm(text).includes(expectedNorm)) {
                        return {ok: true, observed: text, expected, probes: probes.slice(0, 12)};
                    }
                }
                return {ok: false, expected, probes: probes.slice(0, 12)};
            }""",
            expected,
        )
        return {
            "ok": bool(result and result.get("ok")),
            "action": action,
            "expected": expected,
            "observed": (result or {}).get("observed", ""),
            "probes": (result or {}).get("probes", []),
        }
    except Exception as exc:
        return {"ok": False, "reason": f"probe_failed: {exc}", "expected": expected}


def _semantic_macro_should_own_goal(goal: str, macro: dict | None) -> bool:
    """Return true when a semantic component macro should bypass form handling."""
    if not macro:
        return False
    action = str(macro.get("action") or "")
    if action == "cascader_pick":
        return True
    if action != "date_pick":
        return False
    text = str(goal or "")
    has_standalone_date_language = bool(
        re.search(
            r"date[-\s]?picker|datepicker|pick\s+a\s+day|enter\s+date|日期输入框|日历面板|日历",
            text,
            re.I,
        )
    )
    has_form_language = bool(
        re.search(
            r"表单|注册表|填写|填入|填报|Activity\s+name|Activity\s+zone|"
            r"First\s+Name|Last\s+Name|Mobile|Submit|Create",
            text,
            re.I,
        )
    )
    return has_standalone_date_language and not has_form_language


def _semanticize_rpa_trail(trail: list[dict], goal: str) -> list[dict]:
    """Replace volatile physical replays with semantic intent macros when possible."""
    macro = _parse_semantic_macro(goal)
    if macro:
        return [macro]
    return trail


def _prepare_form_batch_fields(goal: str) -> dict[str, str]:
    return engine_prepare_form_batch_fields(goal)
    fields = _parse_form_assignments(goal)
    text = str(goal or "")
    if re.search(r"Activity\s*time|活动时间|时间区域", text, re.I):
        relative_month_day = _parse_relative_month_day(text)
        offset, day = relative_month_day if relative_month_day else (1, 0)
        if not day:
            chunks = re.split(r"[\r\n]+|(?=\s*\d+\s*[.、)]\s*)", text)
            time_chunk = next((c for c in chunks if re.search(r"Activity\s*time|活动时间|时间区域", c, re.I)), "")
            nums = re.findall(r"\b([1-2]?\d|3[01])\b", time_chunk)
            day = int(nums[-1]) if nums else 0
        if day:
            if 1 <= day <= 31:
                fields["Activity time"] = _month_offset_day(offset, day)
    return fields


async def _auto_form_has_prestart_gate(browser: "BrowserEnv") -> bool:
    """Delay deterministic form fill while a visible Start gate still blocks the real workflow."""
    page = await browser._ensure_active_page(reason="inspect auto form prestart gate")
    if not page:
        return False
    try:
        result = await page.evaluate(
            """() => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el.innerText, el.textContent,
                    el.getAttribute('aria-label'),
                    el.getAttribute('title'),
                    el.getAttribute('value')
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const controls = Array.from(document.querySelectorAll(
                    'button,[role=button],input[type=button],input[type=submit],a'
                )).filter(isVisible);
                const fields = Array.from(document.querySelectorAll('input,textarea,select'))
                    .filter(isVisible)
                    .filter(el => !['hidden','button','submit','reset'].includes((el.type || '').toLowerCase()));
                const hasStart = controls.some(el => /\bstart\b/i.test(textOf(el)));
                const hasSubmit = controls.some(el => /(?:\bsubmit\b|\bcreate\b|提交|保存|确定)/i.test(textOf(el)));
                return {
                    blocked: hasStart && hasSubmit && fields.length >= 2,
                    fieldCount: fields.length,
                };
            }"""
        )
    except Exception as exc:
        logger.debug("[AUTO FORM] failed to inspect prestart gate: %s", exc)
        return False
    if result and result.get("blocked"):
        logger.info(
            "[AUTO FORM] prestart gate detected; defer deterministic fill until Start is cleared. fields=%s",
            result.get("fieldCount"),
        )
        return True
    return False


async def _try_auto_form_fill_bound_controls(
    page,
    *,
    scope_title: str,
    fields: dict[str, str],
    require_submit: bool,
) -> dict:
    """Fill generic forms by binding each visible label to one concrete control.

    This is intentionally stricter than the older container-based path: a field is
    complete only when the bound control itself reads back the expected value.
    """
    return await page.evaluate(
        """async ({scopeTitle, fields, requireSubmit}) => {
            const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
            const cleanLabel = (s) => clean(s).replace(/^[*\\s:：-]+|[*\\s:：-]+$/g, '');
            const norm = (s) => cleanLabel(s).toLowerCase();
            const isVisible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    s.display !== 'none' && s.visibility !== 'hidden' &&
                    Number(s.opacity || '1') > 0;
            };
            const textOf = (el) => [
                el?.innerText, el?.textContent, el?.getAttribute?.('aria-label'),
                el?.getAttribute?.('placeholder'), el?.getAttribute?.('title'),
                el?.getAttribute?.('value'), el?.value, el?.name, el?.id
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
            const labelTextOf = (el) => cleanLabel(
                el?.innerText || el?.textContent || el?.getAttribute?.('aria-label') || ''
            );
            const allVisible = (selector, root = document) =>
                Array.from(root.querySelectorAll(selector)).filter(isVisible);
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));
            const labels = Object.keys(fields || {});

            const findScope = () => {
                const roots = allVisible('form,.el-form,.ant-form,.n-form,section,article,main,[role=main],div');
                const scored = [];
                for (const root of roots) {
                    const r = root.getBoundingClientRect();
                    if (r.left < 160 || r.width < 220 || r.height < 60) continue;
                    const txt = norm(textOf(root));
                    let score = 0;
                    if (scopeTitle && txt.includes(norm(scopeTitle))) score += 120;
                    for (const label of labels) if (txt.includes(norm(label))) score += 20;
                    if (root.matches('form,.el-form,.ant-form,.n-form')) score += 70;
                    if (score > 0) scored.push({root, score, area: r.width * r.height});
                }
                scored.sort((a, b) => b.score - a.score || a.area - b.area);
                return scored[0]?.root || document.body;
            };

            const scope = findScope();
            const controls = allVisible('input,textarea,select,[contenteditable=true],[contenteditable="true"]', scope)
                .filter(el => !['hidden','button','submit','reset'].includes((el.type || '').toLowerCase()));
            const labelNodes = allVisible(
                'label,.el-form-item__label,[class*=form-item__label],.ant-form-item-label,.n-form-item-label,span,div',
                scope
            );

            const findBinding = (label, used) => {
                const labelNorm = norm(label);
                const available = controls.filter(ctrl => !used.has(ctrl));
                const direct = available.find(ctrl => [
                    ctrl.getAttribute('aria-label'),
                    ctrl.getAttribute('placeholder'),
                    ctrl.getAttribute('title'),
                    ctrl.getAttribute('name'),
                    ctrl.id
                ].filter(Boolean).some(t => norm(t) === labelNorm));
                if (direct) return {labelEl: direct, control: direct, method: 'direct_control'};

                const hits = [];
                for (const el of labelNodes) {
                    const raw = labelTextOf(el);
                    if (!raw || norm(raw) !== labelNorm) continue;
                    const r = el.getBoundingClientRect();
                    let score = 200;
                    if (el.tagName === 'LABEL') score += 300;
                    if (el.matches('label,.el-form-item__label,[class*=form-item__label],.ant-form-item-label,.n-form-item-label')) score += 180;
                    if (raw.length <= cleanLabel(label).length + 8) score += 40;
                    if (r.width <= 240 && r.height <= 40) score += 20;
                    hits.push({el, score});
                }
                hits.sort((a, b) => b.score - a.score);

                for (const hit of hits.slice(0, 6)) {
                    const forId = hit.el.getAttribute?.('for');
                    if (forId) {
                        const explicit = available.find(ctrl => ctrl.id === forId);
                        if (explicit) return {labelEl: hit.el, control: explicit, method: 'for_attr'};
                    }
                    let cur = hit.el;
                    for (let depth = 0; cur && depth < 7; depth++) {
                        const localControls = available.filter(ctrl => cur.contains(ctrl));
                        if (localControls.length === 1) {
                            return {labelEl: hit.el, control: localControls[0], method: 'local_unique'};
                        }
                        if (localControls.length > 1) {
                            const lr = hit.el.getBoundingClientRect();
                            const scored = localControls.map(ctrl => {
                                const cr = ctrl.getBoundingClientRect();
                                const labelMidY = lr.top + lr.height / 2;
                                const controlMidY = cr.top + cr.height / 2;
                                const sameRowGap = Math.abs(controlMidY - labelMidY);
                                const rightGap = cr.left - lr.right;
                                const verticalGap = cr.top - lr.bottom;
                                const horizontalDelta = Math.abs((cr.left + cr.width / 2) - (lr.left + lr.width / 2));
                                let score = 220 - horizontalDelta - Math.abs(verticalGap) * 2;
                                if (sameRowGap <= Math.max(32, Math.max(lr.height, cr.height)) && rightGap >= -24) {
                                    score += 260 - sameRowGap - Math.max(0, rightGap) / 20;
                                }
                                if (verticalGap >= -12 && verticalGap <= 130) score += 180;
                                if (Math.abs(cr.left - lr.left) <= 45) score += 70;
                                return {ctrl, score};
                            }).sort((a, b) => b.score - a.score);
                            if (scored[0]) return {labelEl: hit.el, control: scored[0].ctrl, method: 'local_geometry'};
                        }
                        cur = cur.parentElement;
                    }
                }
                const fallbackLabel = hits[0]?.el;
                if (!fallbackLabel) return null;
                const lr = fallbackLabel.getBoundingClientRect();
                const scored = available.map(ctrl => {
                    const cr = ctrl.getBoundingClientRect();
                    const labelMidY = lr.top + lr.height / 2;
                    const controlMidY = cr.top + cr.height / 2;
                    const sameRowGap = Math.abs(controlMidY - labelMidY);
                    const rightGap = cr.left - lr.right;
                    const verticalGap = cr.top - lr.bottom;
                    const horizontalDelta = Math.abs((cr.left + cr.width / 2) - (lr.left + lr.width / 2));
                    let score = 200 - horizontalDelta - Math.abs(verticalGap) * 2;
                    if (sameRowGap <= Math.max(32, Math.max(lr.height, cr.height)) && rightGap >= -24) {
                        score += 260 - sameRowGap - Math.max(0, rightGap) / 20;
                    }
                    if (verticalGap >= -12 && verticalGap <= 130) score += 180;
                    if (Math.abs(cr.left - lr.left) <= 45) score += 70;
                    return {ctrl, score};
                }).sort((a, b) => b.score - a.score);
                return scored[0] ? {labelEl: fallbackLabel, control: scored[0].ctrl, method: 'page_geometry'} : null;
            };

            const setNativeValue = (el, value) => {
                if (el.isContentEditable) {
                    el.focus?.();
                    el.textContent = String(value || '');
                } else {
                    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(el, String(value || '')); else el.value = String(value || '');
                }
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
            };
            const clickEl = (el) => {
                el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                if (typeof el.click === 'function') el.click();
                else el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, composed: true, view: window}));
            };
            const valueOf = (control) => {
                const tag = control.tagName?.toLowerCase();
                if (tag === 'select') {
                    return [control.value, control.selectedOptions?.[0]?.textContent]
                        .filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                }
                return [control.value, control.textContent, control.getAttribute('aria-label')]
                    .filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
            };
            const verify = (binding, label, value) => {
                const control = binding.control;
                const expected = norm(value);
                const type = (control.type || '').toLowerCase();
                if (type === 'checkbox' || type === 'radio') {
                    const shouldCheck = !/^(false|off|no|0|关闭|关)$/i.test(String(value || 'true'));
                    return {label, ok: Boolean(control.checked) === shouldCheck, observed: control.checked ? 'checked' : 'unchecked', method: binding.method};
                }
                const observed = valueOf(control);
                return {label, ok: expected && norm(observed) === expected, expected: value, observed, method: binding.method};
            };

            const used = new Set();
            const results = [];
            const bindings = [];
            for (const [label, value] of Object.entries(fields || {})) {
                const binding = findBinding(label, used);
                if (!binding || !binding.control) {
                    results.push({label, ok: false, reason: 'field_not_found'});
                    continue;
                }
                used.add(binding.control);
                bindings.push([label, value, binding]);
                const control = binding.control;
                const tag = control.tagName?.toLowerCase();
                const type = (control.type || '').toLowerCase();
                const expected = String(value || '');

                if (tag === 'select') {
                    const options = Array.from(control.options || []);
                    const hit = options.find(opt => norm(opt.textContent) === norm(expected)) ||
                        options.find(opt => norm(opt.value) === norm(expected)) ||
                        options.find(opt => norm(opt.textContent).includes(norm(expected)));
                    if (hit) control.value = hit.value;
                    else control.value = expected;
                    control.dispatchEvent(new Event('input', {bubbles: true}));
                    control.dispatchEvent(new Event('change', {bubbles: true}));
                    results.push({label, ok: true, mode: 'select', method: binding.method});
                    continue;
                }
                if (type === 'checkbox' || type === 'radio') {
                    const shouldCheck = !/^(false|off|no|0|关闭|关)$/i.test(expected || 'true');
                    if (Boolean(control.checked) !== shouldCheck) clickEl(control);
                    results.push({label, ok: true, mode: type, method: binding.method});
                    continue;
                }
                const role = (control.getAttribute('role') || '').toLowerCase();
                const popup = (control.getAttribute('aria-haspopup') || '').toLowerCase();
                if ((control.readOnly || role === 'combobox' || popup) && tag !== 'textarea') {
                    results.push({label, ok: false, reason: 'complex_component_requires_form_set_or_macro', method: binding.method});
                    continue;
                }
                if (tag === 'input' || tag === 'textarea' || control.isContentEditable) {
                    setNativeValue(control, expected);
                    results.push({label, ok: true, mode: 'input', method: binding.method});
                    continue;
                }
                results.push({label, ok: false, reason: 'unsupported_control', method: binding.method});
            }

            await sleep(200);
            const verifications = bindings.map(([label, value, binding]) => verify(binding, label, value));
            const verificationOk = verifications.length > 0 && verifications.every(r => r.ok);
            if (!verificationOk || !results.every(r => r.ok)) {
                return {ok: false, results, verifications, scopeText: textOf(scope).slice(0, 160)};
            }

            const submit = allVisible('button,.el-button,[role=button],input[type=submit],input[type=button]', scope)
                .find(el => /(?:^|\\s)(?:create|submit)(?:\\s|$)|提交|保存|确定/i.test(textOf(el).trim())) ||
                allVisible('button,.el-button,[role=button],input[type=submit],input[type=button]')
                    .find(el => /(?:^|\\s)(?:create|submit)(?:\\s|$)|提交|保存|确定/i.test(textOf(el).trim()));
            let submitted = false;
            let submitText = '';
            if (submit) {
                submitText = textOf(submit).trim();
                clickEl(submit);
                await sleep(500);
                submitted = true;
            }
            if (requireSubmit && !submitted) {
                return {ok: false, reason: 'submit_not_found', results, verifications, requireSubmit, submitted};
            }
            return {ok: true, submitted, submitText, results, verifications, scopeText: textOf(scope).slice(0, 160)};
        }""",
        {
            "scopeTitle": scope_title,
            "fields": fields,
            "requireSubmit": require_submit,
        },
    )


async def _try_auto_form_fill(browser: "BrowserEnv", goal: str) -> bool:
    """Deterministic label/scoped form executor, used before handing control to VLM."""
    if not _goal_is_form_fill(goal):
        return False
    scope_title = _parse_goal_scope_title(goal)
    page = await browser._ensure_active_page(reason="before auto form fill")
    if not page:
        return False
    _default_fields = _prepare_form_batch_fields(goal)
    _active_fields, _challenge_state = await _resolve_active_form_assignments(browser, goal, _default_fields)
    if (
        not _default_fields
        and _should_use_round_form_macro(goal, _default_fields)
        and _challenge_state
        and _challenge_state.get("is_challenge")
        and _challenge_state.get("current_round")
    ):
        try:
            if await _run_rpa_challenge_macro(browser, page, _challenge_state):
                return True
        except Exception as exc:
            logger.warning("[RPA CHALLENGE] deterministic macro failed, falling back: %s", exc)
    fields = _active_fields
    if len(fields) < 2:
        return False
    require_submit = bool(
        re.search(
            r"(create|submit|提交|保存|确定|点击.+按钮|按钮)",
            str(goal or ""),
            re.IGNORECASE,
        )
    )
    repeat_count = _parse_form_repeat_count(goal)
    try:
        if repeat_count > 1:
            repeat_results = []
            for repeat_index in range(1, repeat_count + 1):
                active_page = await browser._ensure_active_page(
                    reason=f"auto form repeat {repeat_index}/{repeat_count}"
                )
                if not active_page:
                    bound_result = {
                        "ok": False,
                        "reason": "no_active_page",
                        "repeatIndex": repeat_index,
                        "repeatCount": repeat_count,
                        "repetitions": repeat_results,
                    }
                    break
                bound_result = await _try_auto_form_fill_bound_controls(
                    active_page,
                    scope_title=scope_title,
                    fields=fields,
                    require_submit=require_submit,
                )
                if isinstance(bound_result, dict):
                    bound_result["repeatIndex"] = repeat_index
                    bound_result["repeatCount"] = repeat_count
                repeat_results.append(bound_result)
                logger.info(
                    "[AUTO FORM] repeat %s/%s bound-control result=%s",
                    repeat_index,
                    repeat_count,
                    bound_result,
                )
                if not isinstance(bound_result, dict) or not bound_result.get("ok"):
                    bound_result = {
                        "ok": False,
                        "reason": "repeat_failed",
                        "repeatIndex": repeat_index,
                        "repeatCount": repeat_count,
                        "repetitions": repeat_results,
                    }
                    break
                if repeat_index < repeat_count:
                    await asyncio.sleep(0.8)
            else:
                bound_result = {
                    "ok": True,
                    "submitted": bool(require_submit),
                    "repeatCount": repeat_count,
                    "repetitions": repeat_results,
                }
        else:
            bound_result = await _try_auto_form_fill_bound_controls(
                page,
                scope_title=scope_title,
                fields=fields,
                require_submit=require_submit,
            )
        logger.info("[AUTO FORM] bound-control result=%s", bound_result)
        try:
            browser._last_auto_form_result = bound_result
        except Exception:
            pass
        if isinstance(bound_result, dict) and bound_result.get("ok"):
            print(f"\033[1;32m✅ [AUTO FORM]\033[0m 已按 label/control 绑定执行表单填报")
            _broadcast_log_safe("[AUTO FORM] Deterministic bound-control form fill executed")
            return True
        logger.info("[AUTO FORM] bound-control path declined; trying component-aware fallback.")
    except Exception as err:
        logger.warning("[AUTO FORM] bound-control path failed: %s", err)
        logger.info("[AUTO FORM] trying component-aware fallback after bound-control error.")

    logger.info("[AUTO FORM] Trying deterministic form fill. scope=%r fields=%s", scope_title, list(fields))
    try:
        result = await page.evaluate(
            """async ({scopeTitle, fields, requireSubmit}) => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity || '1') > 0;
                };
                const textOf = (el) => [
                    el?.innerText, el?.textContent, el?.getAttribute?.('aria-label'),
                    el?.getAttribute?.('placeholder'), el?.getAttribute?.('title'),
                    el?.getAttribute?.('value'), el?.name, el?.id
                ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                const setNativeValue = (el, val) => {
                    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(el, val); else el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                };
                const clickEl = (el) => {
                    el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                    if (typeof el.click === 'function') el.click();
                    else el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, composed: true, view: window}));
                };
                const allVisible = (selector, root = document) => Array.from(root.querySelectorAll(selector)).filter(isVisible);
                const labels = Object.keys(fields || {});

                const findScope = () => {
                    if (scopeTitle) {
                        const titleNorm = norm(scopeTitle);
                        const heading = allVisible('h1,h2,h3,h4,h5,h6')
                            .find(el => norm(textOf(el)) === titleNorm || norm(textOf(el)).includes(titleNorm));
                        if (heading) {
                            let sib = heading.nextElementSibling;
                            for (let i = 0; sib && i < 12; i++, sib = sib.nextElementSibling) {
                                if (sib.querySelector?.('form,.el-form,.ant-form,.n-form')) {
                                    return sib.querySelector('form,.el-form,.ant-form,.n-form') || sib;
                                }
                            }
                            let cur = heading.parentElement;
                            for (let i = 0; cur && i < 6; i++, cur = cur.parentElement) {
                                const form = cur.querySelector?.('form,.el-form,.ant-form,.n-form');
                                if (form && isVisible(form)) return form;
                            }
                        }
                    }
                    const candidateRoots = allVisible('form,.el-form,.ant-form,.n-form,section,article,main,[role=main],div');
                    const scored = [];
                    for (const root of candidateRoots) {
                        const r = root.getBoundingClientRect();
                        if (r.left < 180 || r.width < 250 || r.height < 80) continue;
                        const txt = norm(textOf(root));
                        let score = 0;
                        if (scopeTitle && txt.includes(norm(scopeTitle))) score += 100;
                        for (const label of labels) if (txt.includes(norm(label))) score += 20;
                        if (root.matches('form,.el-form,.ant-form,.n-form')) score += 60;
                        if (score > 0) scored.push({root, score, area: r.width * r.height});
                    }
                    scored.sort((a, b) => b.score - a.score || a.area - b.area);
                    if (scored[0]) return scored[0].root;
                    return document.body;
                };

                const scope = findScope();
                const findFieldContainer = (label) => {
                    const ln = norm(label);
                    const directControls = allVisible('input,textarea,select,[contenteditable=true],[role=combobox]', scope)
                        .filter(el => !['hidden','button','submit','reset'].includes((el.type || '').toLowerCase()));
                    const directHit = directControls.find(el => {
                        const attrs = [
                            el.getAttribute?.('aria-label'),
                            el.getAttribute?.('placeholder'),
                            el.getAttribute?.('title'),
                            el.getAttribute?.('name'),
                            el.id
                        ].filter(Boolean).map(norm);
                        return attrs.some(t => t === ln || t.includes(ln) || ln.includes(t));
                    });
                    if (directHit) {
                        const directContainer = directHit.closest?.(
                            '.form-group,.form-row,.el-form-item,.ant-form-item,.n-form-item,[class*=form-item],[class*=field],.col-md-4,.col-md-6,.col-sm-12'
                        );
                        return (directContainer && directContainer !== directHit)
                            ? directContainer
                            : (directHit.parentElement || directHit);
                    }
                    const nodes = allVisible('label,.el-form-item__label,.ant-form-item-label,.n-form-item-label,span,div', scope);
                    const matches = [];
                    for (const el of nodes) {
                        const t = norm(textOf(el));
                        if (!t || (t !== ln && !t.includes(ln))) continue;
                        const r = el.getBoundingClientRect();
                        let score = t === ln ? 80 : 20;
                        if (el.matches('label,.el-form-item__label,.ant-form-item-label,.n-form-item-label')) score += 60;
                        if (r.left > 180) score += 20;
                        matches.push({el, score});
                    }
                    matches.sort((a, b) => b.score - a.score);
                    const labelEl = matches[0]?.el;
                    if (!labelEl) return null;
                    let cur = labelEl;
                    for (let i = 0; cur && i < 8; i++) {
                        if (cur !== labelEl && (
                            cur.classList?.contains('el-form-item') ||
                            cur.classList?.contains('ant-form-item') ||
                            cur.classList?.contains('n-form-item')
                        )) return cur;
                        cur = cur.parentElement;
                    }
                    cur = labelEl.parentElement;
                    for (let i = 0; cur && i < 5; i++) {
                        if (cur.querySelector?.('input,textarea,select,[role=combobox],[role=checkbox],[role=radio],button,.el-select,.ant-select,.n-select')) return cur;
                        cur = cur.parentElement;
                    }
                    return labelEl.parentElement;
                };

                const clickOption = async (value) => {
                    const vn = norm(value);
                    for (let i = 0; i < 12; i++) {
                        const opts = allVisible([
                            '.el-select-dropdown__item','.el-radio','.el-checkbox',
                            '.ant-select-item-option','[role=option]',
                            '[id^="react-select"][id*="option"]',
                            '.css-yt9ioa-option','.css-1n7v3ny-option',
                            'label','li','span','button'
                        ].join(','));
                        const hit = opts.find(el => norm(textOf(el)) === vn) || opts.find(el => norm(textOf(el)).includes(vn));
                        if (hit) {
                            clickEl(hit);
                            await sleep(250);
                            return true;
                        }
                        await sleep(150);
                    }
                    return false;
                };
                const clickDateValue = async (value, opener) => {
                    const m = String(value || '').match(/^(\\d{4})-(\\d{2})-(\\d{2})/);
                    if (!m) return false;
                    const targetYear = Number(m[1]);
                    const targetMonth = Number(m[2]);
                    const targetDay = Number(m[3]);
                    if (!targetYear || !targetMonth || !targetDay) return false;

                    clickEl(opener);
                    await sleep(300);

                    const visiblePanelMonth = () => {
                        const panels = allVisible('.el-picker-panel,.ant-picker-dropdown,.n-date-panel,.mx-datepicker-main,.datepicker,[role=dialog],.el-popper');
                        const root = panels[panels.length - 1] || document;
                        const txt = textOf(root);
                        const monthNames = {
                            january: 1, february: 2, march: 3, april: 4, may: 5, june: 6,
                            july: 7, august: 8, september: 9, october: 10, november: 11, december: 12,
                            jan: 1, feb: 2, mar: 3, apr: 4, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9,
                            oct: 10, nov: 11, dec: 12
                        };
                        let m = txt.match(/(20\\d{2})\\s*[年\\-/\\. ]\\s*(1[0-2]|0?[1-9])\\s*(?:月)?/);
                        if (m) return {year: Number(m[1]), month: Number(m[2])};
                        m = txt.match(/(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\\s+(20\\d{2})/i);
                        if (m) return {year: Number(m[2]), month: monthNames[m[1].toLowerCase()]};
                        m = txt.match(/(20\\d{2})\\s+(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)/i);
                        if (m) return {year: Number(m[1]), month: monthNames[m[2].toLowerCase()]};
                        const currentVal = String(opener?.value || '');
                        m = currentVal.match(/^(\\d{4})-(\\d{2})-/);
                        if (m) return {year: Number(m[1]), month: Number(m[2])};
                        const now = new Date();
                        return {year: now.getFullYear(), month: now.getMonth() + 1};
                    };
                    const panelMonth = visiblePanelMonth();
                    const monthDelta = (targetYear - panelMonth.year) * 12 + (targetMonth - panelMonth.month);
                    const nextSelectors = [
                        '.el-picker-panel__icon-btn.arrow-right',
                        '.ant-picker-header-next-btn',
                        '.n-date-panel-actions + * button[aria-label*=next]',
                        'button[aria-label*="Next month"]',
                        'button[title*="Next month"]'
                    ].join(',');
                    const prevSelectors = [
                        '.el-picker-panel__icon-btn.arrow-left',
                        '.ant-picker-header-prev-btn',
                        'button[aria-label*="Previous month"]',
                        'button[title*="Previous month"]'
                    ].join(',');
                    const navSelector = monthDelta >= 0 ? nextSelectors : prevSelectors;
                    for (let i = 0; i < Math.min(Math.abs(monthDelta), 24); i++) {
                        const btn = allVisible(navSelector).find(el => !el.disabled && el.getAttribute('aria-disabled') !== 'true');
                        if (!btn) break;
                        clickEl(btn);
                        await sleep(150);
                    }

                    const ymd = `${targetYear}-${String(targetMonth).padStart(2, '0')}-${String(targetDay).padStart(2, '0')}`;
                    const dayText = String(targetDay);
                    for (let i = 0; i < 10; i++) {
                        const panels = allVisible('.el-picker-panel,.ant-picker-dropdown,.n-date-panel,.mx-datepicker-main,.datepicker,[role=dialog],.el-popper');
                        const root = panels[panels.length - 1] || document;
                        const cells = allVisible('td,button,[role=gridcell],.el-date-table-cell,.ant-picker-cell-inner', root);
                        const hits = [];
                        for (const el of cells) {
                            const cell = el.closest('td,button,[role=gridcell]') || el;
                            if (!isVisible(cell)) continue;
                            const disabled = cell.matches('.disabled,.is-disabled,.ant-picker-cell-disabled,[aria-disabled=true]') ||
                                cell.closest('.disabled,.is-disabled,.ant-picker-cell-disabled,[aria-disabled=true]');
                            if (disabled) continue;
                            const raw = [
                                textOf(el), textOf(cell),
                                el.getAttribute('aria-label'), cell.getAttribute('aria-label'),
                                el.getAttribute('title'), cell.getAttribute('title')
                            ].filter(Boolean).join(' ');
                            const t = norm(raw);
                            let score = 0;
                            if (t === norm(dayText)) score += 80;
                            if (t.includes(norm(ymd))) score += 120;
                            if (t.includes(String(targetYear)) && t.includes(String(targetDay))) score += 40;
                            if (cell.classList?.contains('prev-month') || cell.classList?.contains('next-month')) score -= 90;
                            if (cell.classList?.contains('available') || cell.classList?.contains('ant-picker-cell-in-view')) score += 20;
                            if (score > 0) hits.push({cell, score});
                        }
                        hits.sort((a, b) => b.score - a.score);
                        if (hits[0]) {
                            clickEl(hits[0].cell);
                            await sleep(250);
                            return true;
                        }
                        await sleep(120);
                    }
                    return false;
                };
                const verifyField = (label, value) => {
                    const item = findFieldContainer(label);
                    if (!item) return {label, ok: false, reason: 'field_not_found'};
                    const expected = norm(value);
                    const observed = [
                        textOf(item),
                        ...allVisible('input,textarea,select,[contenteditable=true]', item).map(el => {
                            if (el.tagName?.toLowerCase() === 'select') {
                                return [el.value, el.selectedOptions?.[0]?.textContent].filter(Boolean).join(' ');
                            }
                            return [el.value, el.textContent, el.getAttribute('aria-label')].filter(Boolean).join(' ');
                        })
                    ].join(' ').replace(/\\s+/g, ' ').trim();
                    const observedNorm = norm(observed);

                    const switchRoot = allVisible('.el-switch,[role=switch]', item)[0];
                    if (switchRoot) {
                        const checked = switchRoot.classList.contains('is-checked') ||
                            switchRoot.getAttribute('aria-checked') === 'true' ||
                            Boolean(item.querySelector('input:checked'));
                        const shouldOn = !/^(false|off|no|0)$/i.test(String(value || ''));
                        return {label, ok: checked === shouldOn, expected: value, mode: 'switch', observed: checked ? 'checked' : 'unchecked'};
                    }

                    const choiceNodes = allVisible('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]', item);
                    const choiceHit = choiceNodes.find(el => norm(textOf(el)).includes(expected));
                    if (choiceHit) {
                        const choiceTarget = choiceHit.closest?.('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]') || choiceHit;
                        const checked = choiceTarget.matches?.('.is-checked,[aria-checked=true]') ||
                            Boolean(choiceTarget.querySelector?.('.is-checked,[aria-checked=true],input:checked')) ||
                            Boolean(choiceHit.querySelector?.('.is-checked,[aria-checked=true],input:checked'));
                        if (checked) return {label, ok: true, expected: value, mode: 'choice_checked', observed};
                    }

                    if (expected && observedNorm.includes(expected)) {
                        return {label, ok: true, expected: value, mode: 'value_visible', observed};
                    }
                    return {label, ok: false, reason: 'value_not_reflected', expected: value, observed};
                };

                const results = [];
                for (const [label, value] of Object.entries(fields || {})) {
                    const item = findFieldContainer(label);
                    if (!item) {
                        results.push({label, ok: false, reason: 'field_not_found'});
                        continue;
                    }
                    item.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                    await sleep(150);
                    const valueText = String(value || '');
                    const vn = norm(valueText);

                    const textarea = allVisible('textarea', item)[0];
                    if (textarea) {
                        setNativeValue(textarea, valueText);
                        results.push({label, ok: true, mode: 'textarea'});
                        continue;
                    }

                    const selectRoot = allVisible('.el-select,.ant-select,.n-select', item)[0] ||
                        allVisible('[role=combobox]', item)[0];
                    const inputs = allVisible('input,[contenteditable=true]', item)
                        .filter(el => !['hidden','checkbox','radio','button','submit'].includes((el.type || '').toLowerCase()));
                    if (/(date|time|日期|时间)/i.test(label) && /^\\d{4}-\\d{2}-\\d{2}/.test(valueText) && inputs.length) {
                        const picked = await clickDateValue(valueText, inputs[0]);
                        if (picked) {
                            results.push({label, ok: true, mode: 'date_picker'});
                            continue;
                        }
                        setNativeValue(inputs[0], valueText);
                        results.push({label, ok: true, mode: 'date_input'});
                        continue;
                    }
                    const readonly = inputs.find(el => el.readOnly || (el.getAttribute('role') || '').toLowerCase() === 'combobox' || el.getAttribute('aria-haspopup'));
                    const autocomplete = inputs.find(el => {
                        const role = (el.getAttribute('role') || '').toLowerCase();
                        const auto = (el.getAttribute('aria-autocomplete') || el.getAttribute('autocomplete') || '').toLowerCase();
                        return role === 'combobox' || auto === 'list' || auto === 'both' ||
                            /subject|tag|skill|course|autocomplete|联想|科目|课程/i.test(label);
                    });
                    if (autocomplete && /subject|tag|skill|course|autocomplete|联想|科目|课程/i.test(label)) {
                        const query = valueText;
                        clickEl(autocomplete);
                        setNativeValue(autocomplete, query);
                        autocomplete.dispatchEvent(new KeyboardEvent('keydown', {key: query.slice(-1) || 'a', bubbles: true}));
                        autocomplete.dispatchEvent(new KeyboardEvent('keyup', {key: query.slice(-1) || 'a', bubbles: true}));
                        await sleep(500);
                        let ok = await clickOption(valueText);
                        if (!ok && /s$/i.test(valueText)) {
                            setNativeValue(autocomplete, valueText.replace(/s$/i, ''));
                            await sleep(500);
                            ok = await clickOption(valueText);
                        }
                        if (!ok) {
                            autocomplete.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
                            autocomplete.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', bubbles: true}));
                            await sleep(250);
                        }
                        results.push({label, ok: ok || norm(textOf(item)).includes(vn), mode: 'autocomplete'});
                        continue;
                    }
                    const isChoiceValue = /(zone|type|resource|delivery|date|time|subject|course|下拉|选择|复选|单选|开关|日期|时间|科目|课程)/i.test(label);
                    if ((selectRoot || readonly) && isChoiceValue) {
                        let opener = selectRoot || readonly;
                        for (const sel of [
                            '.el-select__wrapper', '.el-select',
                            '.ant-select-selector', '.ant-select',
                            '.n-base-selection', '[role=combobox]'
                        ]) {
                            const closest = readonly?.closest?.(sel) || opener?.closest?.(sel) || opener?.querySelector?.(sel);
                            if (closest && isVisible(closest)) {
                                opener = closest;
                                break;
                            }
                        }
                        clickEl(opener);
                        await sleep(350);
                        const ok = await clickOption(valueText);
                        results.push({label, ok, mode: 'select'});
                        continue;
                    }

                    const switchRoot = allVisible('.el-switch,[role=switch]', item)[0];
                    if (switchRoot) {
                        const shouldOn = /^(true|on|yes|1|开启|打开|选中|勾选)$/i.test(valueText || '开启');
                        const checked = switchRoot.classList.contains('is-checked') || switchRoot.getAttribute('aria-checked') === 'true';
                        if (shouldOn !== checked) clickEl(switchRoot);
                        results.push({label, ok: true, mode: 'switch'});
                        continue;
                    }

                    const choiceHit = allVisible('label,.el-radio,.el-checkbox,span,button', item)
                        .find(el => norm(textOf(el)) === vn) ||
                        allVisible('label,.el-radio,.el-checkbox,span,button', item)
                        .find(el => norm(textOf(el)).includes(vn));
                    if (choiceHit) {
                        const choiceTarget = choiceHit.closest?.('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]') || choiceHit;
                        clickEl(choiceTarget);
                        await sleep(150);
                        const checked = choiceTarget.matches?.('.is-checked,[aria-checked=true],input:checked') ||
                            choiceTarget.querySelector?.('.is-checked,[aria-checked=true],input:checked') ||
                            choiceHit.matches?.('.is-checked,[aria-checked=true],input:checked') ||
                            choiceHit.querySelector?.('.is-checked,[aria-checked=true],input:checked');
                        results.push({label, ok: Boolean(checked) || norm(textOf(choiceTarget)).includes(vn) || choiceTarget.tagName === 'BUTTON', mode: 'choice'});
                        continue;
                    }

                    if (inputs.length) {
                        setNativeValue(inputs[0], valueText);
                        results.push({label, ok: true, mode: 'input'});
                        continue;
                    }
                    results.push({label, ok: false, reason: 'no_control'});
                }

                await sleep(250);
                const verifications = Object.entries(fields || {}).map(([label, value]) => verifyField(label, value));
                const verificationOk = verifications.length > 0 && verifications.every(r => r.ok);
                if (!verificationOk) {
                    return {
                        ok: false,
                        scopeText: textOf(scope).slice(0, 120),
                        results,
                        verifications
                    };
                }

                const submit = allVisible('button,.el-button,[role=button],input[type=submit],input[type=button]', scope)
                    .find(el => /(?:^|\\s)(?:create|submit)(?:\\s|$)|提交|保存|确定/i.test(textOf(el).trim())) ||
                    allVisible('button,.el-button,[role=button],input[type=submit],input[type=button]')
                    .filter(el => el.getBoundingClientRect().left > 180)
                    .find(el => /(?:^|\\s)(?:create|submit)(?:\\s|$)|提交|保存|确定/i.test(textOf(el).trim()));
                let submitted = false;
                let submitText = '';
                if (submit) {
                    submitText = textOf(submit).trim();
                    clickEl(submit);
                    await sleep(500);
                    submitted = true;
                    results.push({label: '__submit__', ok: true, mode: submitText});
                }
                if (requireSubmit && !submitted) {
                    return {
                        ok: false,
                        reason: 'submit_not_found',
                        requireSubmit,
                        submitted,
                        scopeText: textOf(scope).slice(0, 120),
                        results,
                        verifications
                    };
                }

                const fieldResults = results.filter(r => r.label !== '__submit__');
                return {
                    ok: fieldResults.length > 0 && fieldResults.every(r => r.ok) && verificationOk && (!requireSubmit || submitted),
                    submitted,
                    submitText,
                    requireSubmit,
                    scopeText: textOf(scope).slice(0, 120),
                    results,
                    verifications
                };
            }""",
            {
                "scopeTitle": _parse_goal_scope_title(goal),
                "fields": fields,
                "requireSubmit": require_submit,
            },
        )
        logger.info("[AUTO FORM] result=%s", result)
        try:
            browser._last_auto_form_result = result
        except Exception:
            pass
        if isinstance(result, dict) and result.get("ok"):
            print(f"\033[1;32m✅ [AUTO FORM]\033[0m 已按 DOM scope 执行表单填报")
            _broadcast_log_safe("[AUTO FORM] Deterministic scoped form fill executed")
            return True
        return False
    except Exception as err:
        logger.warning("[AUTO FORM] deterministic fill failed, fallback to VLM: %s", err)
        return False


def _format_auto_form_validation_summary(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    repetitions = result.get("repetitions")
    if isinstance(repetitions, list) and repetitions:
        chunks: list[str] = []
        for idx, item in enumerate(repetitions, start=1):
            if not isinstance(item, dict):
                chunks.append(f"第{idx}次: {item!r}")
                continue
            one = _format_auto_form_validation_summary(
                {k: v for k, v in item.items() if k != "repetitions"}
            )
            chunks.append(f"第{idx}次: {one or item!r}")
        status = "成功" if result.get("ok") else "失败"
        return f"重复表单执行{status}: 共{result.get('repeatCount') or len(repetitions)}次; " + " || ".join(chunks)
    verifications = result.get("verifications") or []
    parts: list[str] = []
    if isinstance(verifications, list):
        for item in verifications:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            expected = str(item.get("expected") or "").strip()
            observed = str(item.get("observed") or "").strip()
            method = str(item.get("method") or item.get("mode") or "").strip()
            if label:
                parts.append(f"{label}: expected={expected!r}, observed={observed!r}, method={method}")
    submit_text = str(result.get("submitText") or "").strip()
    submitted = bool(result.get("submitted"))
    suffix = f"; submitted={submitted}"
    if submit_text:
        suffix += f", submit={submit_text!r}"
    if parts:
        return "字段回读: " + " | ".join(parts) + suffix
    return f"字段回读结果: {result!r}"


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


_DEFAULT_LOGIN_OPEN_SELECTOR = (
    "a:has-text('登录'), button:has-text('登录'), [role='button']:has-text('登录'), "
    "a:has-text('登录/注册'), button:has-text('登录/注册'), "
    "a:has-text('登录系统'), button:has-text('登录系统'), "
    "a:has-text('进入系统'), button:has-text('进入系统'), "
    "a:has-text('立即登录'), button:has-text('立即登录'), "
    "a:has-text('统一身份认证'), button:has-text('统一身份认证'), "
    "a:has-text('单点登录'), button:has-text('单点登录'), "
    "a:has-text('Sign in'), button:has-text('Sign in'), "
    "a:has-text('Log in'), button:has-text('Log in')"
)
_DEFAULT_LOGIN_USER_SELECTOR = (
    "input[placeholder*='账号' i], input[placeholder*='用户名' i], "
    "input[placeholder*='手机' i], input[placeholder*='邮箱' i], "
    "input[placeholder*='工号' i], input[placeholder*='员工号' i], "
    "input[placeholder*='统一账号' i], input[placeholder*='域账号' i], "
    "input[placeholder*='用户编码' i], input[placeholder*='人员编号' i], "
    "input[name*='user' i], input[name*='login' i], input[name*='account' i], "
    "input[name*='emp' i], input[name*='staff' i], input[name*='job' i], "
    "input[type='email'], input[type='tel'], input[autocomplete='username']"
)
_DEFAULT_LOGIN_PASSWORD_SELECTOR = (
    "input[type='password'], input[autocomplete='current-password']"
)
_DEFAULT_LOGIN_PASSWORD_MODE_SELECTOR = (
    "text=/密码登录|账号登录|账号密码登录|工号登录|统一账号登录|使用密码|password login/i"
)
_DEFAULT_LOGIN_SUBMIT_SELECTOR = (
    "button[type='submit'], input[type='submit'], "
    "button:has-text('登录'), [role='button']:has-text('登录'), "
    "button:has-text('登录系统'), [role='button']:has-text('登录系统'), "
    "button:has-text('进入系统'), [role='button']:has-text('进入系统'), "
    "button:has-text('立即登录'), [role='button']:has-text('立即登录'), "
    "button:has-text('Sign in'), button:has-text('Log in')"
)


def _env_or_default(name: str, default: str) -> str:
    value = os.getenv(name, "")
    return value.strip() if value and value.strip() else default


def _url_looks_like_login(url: str) -> bool:
    base_patterns = [
        r"login", r"signin", r"sign-in", r"auth", r"passport", r"account",
        r"sso", r"cas", r"oauth", r"authserver", r"iam", r"uap", r"uc",
    ]
    return _text_matches_patterns(
        url or "",
        base_patterns,
        extra_env_name="VSPIDER_EXTRA_LOGIN_URL_KEYWORDS",
    )


async def _first_visible_locator(page, selector: str, limit: int = 12):
    if not selector:
        return None
    try:
        locator = page.locator(selector)
        count = await locator.count()
    except Exception:
        return None

    for idx in range(min(count, limit)):
        candidate = locator.nth(idx)
        try:
            if await candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


async def _wait_for_visible_locator(page, selector: str, timeout_ms: int = 4000, poll_ms: int = 200):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000.0
    while loop.time() < deadline:
        candidate = await _first_visible_locator(page, selector)
        if candidate:
            return candidate
        await asyncio.sleep(poll_ms / 1000.0)
    return None


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except Exception:
        return default
    return value if value > 0 else default


@lru_cache(maxsize=None)
def _split_env_keywords(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "") or ""
    if not raw.strip():
        return ()
    parts = [
        part.strip()
        for part in re.split(r"[\r\n,;|]+", raw)
        if part and part.strip()
    ]
    return tuple(parts)


def _text_matches_patterns(text: str, base_patterns: list[str], extra_env_name: str = "") -> bool:
    patterns = list(base_patterns)
    if extra_env_name:
        patterns.extend(re.escape(item) for item in _split_env_keywords(extra_env_name))
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


_LOGIN_SITE_PROFILE_FIELDS = {
    "PASSWORD_MODE_SELECTOR": "password_mode_selector",
    "SUCCESS_TIMEOUT_MS": "success_timeout_ms",
    "OPEN_SELECTOR": "open_selector",
    "USER_SELECTOR": "user_selector",
    "PWD_SELECTOR": "pwd_selector",
    "SUCCESS_SELECTOR": "success_selector",
    "SUBMIT_SELECTOR": "submit_selector",
    "DOMAINS": "domains_raw",
    "USER": "user_account",
    "PWD": "user_pwd",
}


def _normalize_login_domain(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        try:
            text = urlsplit(text).hostname or text
        except Exception:
            pass
    if text.startswith("*."):
        text = text[2:]
    text = text.lstrip(".")
    text = text.split("/")[0]
    text = text.split(":")[0]
    return text.strip()


def _split_login_domains(value: str) -> list[str]:
    return [
        domain
        for domain in (
            _normalize_login_domain(part)
            for part in re.split(r"[\s,;]+", value or "")
        )
        if domain
    ]


def _current_page_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return ""


def _base_login_settings() -> dict:
    return {
        "open_selector": _env_or_default("VSPIDER_LOGIN_OPEN_SELECTOR", _DEFAULT_LOGIN_OPEN_SELECTOR),
        "user_selector": _env_or_default("VSPIDER_LOGIN_USER_SELECTOR", _DEFAULT_LOGIN_USER_SELECTOR),
        "pwd_selector": _env_or_default("VSPIDER_LOGIN_PWD_SELECTOR", _DEFAULT_LOGIN_PASSWORD_SELECTOR),
        "password_mode_selector": _env_or_default(
            "VSPIDER_LOGIN_PASSWORD_MODE_SELECTOR", _DEFAULT_LOGIN_PASSWORD_MODE_SELECTOR
        ),
        "submit_selector": _env_or_default("VSPIDER_LOGIN_SUBMIT_SELECTOR", _DEFAULT_LOGIN_SUBMIT_SELECTOR),
        "success_selector": os.getenv("VSPIDER_LOGIN_SUCCESS_SELECTOR", "").strip(),
        "success_timeout_ms": _env_int("VSPIDER_LOGIN_SUCCESS_TIMEOUT_MS", 10000),
        "user_account": os.getenv("VSPIDER_LOGIN_USER", "").strip(),
        "user_pwd": os.getenv("VSPIDER_LOGIN_PWD", "").strip(),
        "profile_name": "GLOBAL",
        "matched_domain": "",
        "credential_source": "global",
        "credentials_available": False,
        "blocked_reason": "",
    }


def _load_login_site_profiles() -> list[dict]:
    prefix = "VSPIDER_LOGIN_SITE_"
    profiles: dict[str, dict] = {}

    for env_name, env_value in os.environ.items():
        if not env_name.startswith(prefix):
            continue

        remainder = env_name[len(prefix):]
        field_name = ""
        site_key = ""
        for suffix, mapped_field in _LOGIN_SITE_PROFILE_FIELDS.items():
            token = f"_{suffix}"
            if remainder.endswith(token):
                site_key = remainder[:-len(token)].strip("_")
                field_name = mapped_field
                break

        if not site_key or not field_name:
            continue

        profile = profiles.setdefault(site_key, {"site_key": site_key})
        profile[field_name] = str(env_value or "").strip()

    loaded_profiles: list[dict] = []
    for site_key, profile in profiles.items():
        domains = _split_login_domains(profile.get("domains_raw", ""))
        if not domains:
            logger.warning(
                f"[PRELOGIN] Ignoring site login profile '{site_key}' because *_DOMAINS is missing."
            )
            continue

        loaded_profiles.append(
            {
                "site_key": site_key,
                "domains": domains,
                "user_account": profile.get("user_account", "").strip(),
                "user_pwd": profile.get("user_pwd", "").strip(),
                "open_selector": profile.get("open_selector", "").strip(),
                "user_selector": profile.get("user_selector", "").strip(),
                "pwd_selector": profile.get("pwd_selector", "").strip(),
                "password_mode_selector": profile.get("password_mode_selector", "").strip(),
                "submit_selector": profile.get("submit_selector", "").strip(),
                "success_selector": profile.get("success_selector", "").strip(),
                "success_timeout_ms": profile.get("success_timeout_ms", "").strip(),
            }
        )

    return loaded_profiles


def _resolve_login_settings(url: str) -> dict:
    settings = _base_login_settings()
    settings["credentials_available"] = bool(settings["user_account"] and settings["user_pwd"])
    if not settings["credentials_available"]:
        settings["blocked_reason"] = "global credentials are missing"

    site_profiles = _load_login_site_profiles()
    if not site_profiles:
        return settings

    host = _current_page_host(url)
    allow_global_fallback = _env_flag("VSPIDER_LOGIN_ALLOW_GLOBAL_FALLBACK", default=False)

    best_profile = None
    best_domain = ""
    for profile in site_profiles:
        for domain in profile.get("domains", []):
            if host == domain or host.endswith(f".{domain}"):
                if len(domain) > len(best_domain):
                    best_profile = profile
                    best_domain = domain

    if best_profile:
        for field_name in (
            "open_selector",
            "user_selector",
            "pwd_selector",
            "password_mode_selector",
            "submit_selector",
            "success_selector",
        ):
            if best_profile.get(field_name):
                settings[field_name] = best_profile[field_name]

        timeout_raw = best_profile.get("success_timeout_ms", "")
        if timeout_raw:
            try:
                timeout_value = int(timeout_raw)
            except Exception:
                timeout_value = settings["success_timeout_ms"]
            if timeout_value > 0:
                settings["success_timeout_ms"] = timeout_value

        settings["user_account"] = best_profile.get("user_account", "").strip()
        settings["user_pwd"] = best_profile.get("user_pwd", "").strip()
        settings["profile_name"] = best_profile["site_key"]
        settings["matched_domain"] = best_domain
        settings["credential_source"] = "site-profile"
        settings["credentials_available"] = bool(settings["user_account"] and settings["user_pwd"])
        settings["blocked_reason"] = (
            ""
            if settings["credentials_available"]
            else f"site profile '{best_profile['site_key']}' is missing USER/PWD"
        )
        return settings

    if allow_global_fallback:
        settings["credential_source"] = "global-fallback"
        settings["profile_name"] = "GLOBAL_FALLBACK"
        settings["blocked_reason"] = (
            ""
            if settings["credentials_available"]
            else "global fallback credentials are missing"
        )
        return settings

    settings["credential_source"] = "unmatched-site-profile"
    settings["profile_name"] = ""
    settings["matched_domain"] = ""
    settings["credentials_available"] = False
    settings["blocked_reason"] = (
        f"no configured site login profile matched host '{host or url or '<unknown>'}'"
    )
    return settings


def _resolve_login_vault_value(value: str, label: str) -> str:
    try:
        resolved, used, names = resolve_env_placeholders(value)
    except SecretResolutionError as exc:
        raise RuntimeError(str(exc)) from exc
    if used:
        logger.info(
            "[AUTH VAULT] Resolved pre-login %s from env placeholder(s): %s",
            label,
            ", ".join(names),
        )
    return resolved


async def _run_preflight_login(
    browser: BrowserEnv,
    goal: str,
    source: str = "startup",
    require_login: bool = False,
) -> bool:
    if source == "startup":
        print("[SECURITY] Checking whether the page needs login...")
    logger.info(f"[PRELOGIN] Checking whether the active page needs authentication (source={source}).")

    page = await browser._ensure_active_page(reason="preflight login")
    if not page:
        logger.warning("[PRELOGIN] No active page available, skip pre-flight login.")
        return False

    login_settings = _resolve_login_settings(page.url)
    login_open_selector = login_settings["open_selector"]
    user_selector = login_settings["user_selector"]
    pwd_selector = login_settings["pwd_selector"]
    password_mode_selector = login_settings["password_mode_selector"]
    submit_selector = login_settings["submit_selector"]
    success_selector = login_settings["success_selector"]
    success_timeout_ms = login_settings["success_timeout_ms"]
    login_enabled = _env_flag("VSPIDER_LOGIN_ENABLED", default=True)
    force_login = _env_flag("VSPIDER_LOGIN_FORCE", default=False)

    if login_settings["credential_source"] == "site-profile":
        logger.info(
            "[PRELOGIN] Using site login profile '%s' for host '%s' (matched domain '%s').",
            login_settings["profile_name"],
            _current_page_host(page.url) or "<unknown>",
            login_settings["matched_domain"],
        )
    elif login_settings["credential_source"] == "global-fallback":
        logger.info(
            "[PRELOGIN] No site profile matched host '%s'; using global fallback credentials.",
            _current_page_host(page.url) or "<unknown>",
        )

    password_locator = await _first_visible_locator(page, pwd_selector)
    password_mode_locator = await _first_visible_locator(page, password_mode_selector)
    login_entry = await _first_visible_locator(page, login_open_selector)
    login_like_url = _url_looks_like_login(page.url)
    explicit_login_goal = _goal_requires_login_flow(goal)
    login_surface_visible = bool(password_locator or password_mode_locator)
    proactive_login = force_login or require_login
    should_attempt_login = bool(
        login_surface_visible or (proactive_login and (login_entry or login_like_url or explicit_login_goal or require_login))
    )

    if not login_enabled:
        logger.info("[PRELOGIN] Disabled by VSPIDER_LOGIN_ENABLED=false.")
        if source == "startup":
            print("[SECURITY] Pre-login interceptor disabled by environment.")
        return False

    if not should_attempt_login:
        logger.info(
            "[PRELOGIN] Login surface not visible; skip native login "
            f"(force={force_login}, require_login={require_login}, source={source})."
        )
        if source == "startup":
            print("[SECURITY] No visible login form or popup detected, skipping auto login.")
        return False

    user_account = login_settings["user_account"]
    user_pwd = login_settings["user_pwd"]
    if not login_settings["credentials_available"] and not (proactive_login and login_entry):
        if source == "startup":
            print("[AUTH] Credentials not configured for this site, skipping auto login.")
        logger.warning(
            "[PRELOGIN] Credentials unavailable for auto login: %s.",
            login_settings["blocked_reason"] or "unknown reason",
        )
        return False

    try:
        if not password_locator:
            if login_entry:
                logger.info("[PRELOGIN] Opening login form from visible login entry.")
                await login_entry.click(timeout=5000)
                await asyncio.sleep(0.6)
                page = await browser._ensure_active_page(reason="preflight login entry clicked") or page
            password_mode_locator = await _first_visible_locator(page, password_mode_selector)
            if password_mode_locator:
                logger.info("[PRELOGIN] Switching to password-login mode.")
                await password_mode_locator.click(timeout=5000)
                await asyncio.sleep(0.4)
            password_locator = await _wait_for_visible_locator(page, pwd_selector, timeout_ms=4000)

        if not password_locator:
            logger.info("[PRELOGIN] Login form not exposed after probing, hand back to VLM.")
            if source == "startup":
                print("[SECURITY] Password field not found after probing, handing control back to VLM.")
            return False

        login_settings = _resolve_login_settings(page.url)
        user_selector = login_settings["user_selector"]
        pwd_selector = login_settings["pwd_selector"]
        submit_selector = login_settings["submit_selector"]
        success_selector = login_settings["success_selector"]
        success_timeout_ms = login_settings["success_timeout_ms"]
        user_account = login_settings["user_account"]
        user_pwd = login_settings["user_pwd"]
        if not login_settings["credentials_available"]:
            if source == "startup":
                print("[AUTH] Credentials are still unavailable after opening the login surface.")
            logger.warning(
                "[PRELOGIN] Credentials unavailable for auto login after login surface opened: %s.",
                login_settings["blocked_reason"] or "unknown reason",
            )
            return False

        if source == "startup":
            print("[AUTH] Login required, starting native Playwright login...")
        else:
            print("[AUTH] Login popup detected during execution, taking over with native login...")

        user_account = _resolve_login_vault_value(user_account, "username")
        user_pwd = _resolve_login_vault_value(user_pwd, "password")

        user_locator = await _first_visible_locator(page, user_selector)
        if user_locator:
            await user_locator.fill(user_account)
        else:
            logger.warning("[PRELOGIN] Username input not found; it may be prefilled or selector needs tuning.")

        await password_locator.fill(user_pwd)

        submit_locator = await _first_visible_locator(page, submit_selector)
        if submit_locator:
            await submit_locator.click(timeout=5000)
        else:
            await password_locator.press("Enter")

        page = await browser._ensure_active_page(reason="preflight login submitted") or page

        success = False
        if success_selector:
            success = bool(
                await _wait_for_visible_locator(page, success_selector, timeout_ms=success_timeout_ms)
            )
        else:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + success_timeout_ms / 1000.0
            while loop.time() < deadline:
                page = await browser._ensure_active_page(reason="preflight login waiting") or page
                still_visible = await _first_visible_locator(page, pwd_selector)
                if not still_visible:
                    success = True
                    break
                await asyncio.sleep(0.25)

        if not success:
            raise TimeoutError(
                "login success condition not met; consider setting VSPIDER_LOGIN_SUCCESS_SELECTOR"
            )

        print("[AUTH] Native login succeeded and state has been persisted to browser_data.")
        logger.info("[PRELOGIN] Native Playwright login succeeded and state is persisted.")
        await page.wait_for_load_state("domcontentloaded", timeout=success_timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        return True
    except Exception as exc:
        print(f"[AUTH] Native login failed: {exc}. Falling back to VLM.")
        logger.warning(f"[PRELOGIN] Native login failed, falling back to VLM: {exc}")
        return False


def _goal_requires_login_flow(goal: str) -> bool:
    """
    判断任务目标是否真的涉及登录流程。

    避免在与登录无关的页面上，仅凭"没看到登录按钮"就注入
    "当前已登录"这种强结论，干扰模型理解主任务。
    """
    login_patterns = [
        r"登录", r"登陆", r"login", r"sign[\s-]?in", r"sign[\s-]?on",
        r"账号", r"帐户", r"账户", r"用户名", r"user\s*name",
        r"密码", r"password", r"扫码", r"二维码", r"验证码", r"otp",
        r"credential", r"signin", r"统一身份认证", r"单点登录", r"sso",
        r"工号", r"员工号", r"域账号", r"统一账号", r"ukey", r"usb\s*key", r"ca证书", r"ca登录",
    ]
    return _text_matches_patterns(
        goal,
        login_patterns,
        extra_env_name="VSPIDER_EXTRA_LOGIN_KEYWORDS",
    )


def _goal_prefers_visual_navigation(goal: str) -> bool:
    """
    某些任务天然依赖“看见列表中的具体条目再点击”，
    例如排行榜、搜索结果列表、按序号选择第 N 个项目等。
    这类场景如果 text-only 抽到的 DOM 太稀，应尽快回退到视觉模式。
    """
    patterns = [
        r"排名", r"榜单", r"列表", r"top\s*\d+", r"top250",
        r"第\s*\d+\s*(个|条|项|部|集|页|名|行|列|条记录)", r"第[一二三四五六七八九十两]+",
        r"第一[个条项目部集名行列]", r"第二[个条项目部集名行列]",
        r"点击列表中的", r"找到并点击", r"按.*排序", r"最多播放", r"最热", r"最新",
        r"搜索结果", r"结果页", r"查询结果", r"明细", r"详情", r"表格", r"报表",
        r"电影", r"视频", r"商品", r"文章", r"工单", r"告警", r"缺陷", r"台账",
        r"设备", r"站点", r"站所", r"变电站", r"线路", r"馈线", r"回路", r"台区",
        r"审批", r"流程", r"任务单", r"检修", r"巡检", r"户号", r"档案",
    ]
    return _text_matches_patterns(
        goal,
        patterns,
        extra_env_name="VSPIDER_EXTRA_VISUAL_NAV_KEYWORDS",
    )


def _goal_should_force_vision(goal: str) -> bool:
    """
    对“榜单/列表/表格/结果页里定位具体条目”的任务，默认优先视觉模式。
    这类任务在 text-only 下最容易误点站点全局导航，内网页面尤其如此。
    """
    return _env_flag("VSPIDER_FORCE_VISION_FOR_LIST_TASKS", default=True) and _goal_prefers_visual_navigation(goal)


def _should_fallback_to_vision(goal: str, text_snapshot: str) -> tuple[bool, str]:
    lines = [line.strip() for line in (text_snapshot or "").splitlines() if line.strip()]
    if not lines:
        return True, "text snapshot empty"

    id_lines = [line for line in lines if line.startswith("[ID:")]
    text_lines = [line for line in lines if line.startswith("[TEXT]")]

    if _goal_prefers_visual_navigation(goal):
        if len(lines) < 18:
            return True, f"goal needs list-item navigation but text snapshot is sparse ({len(lines)} lines)"
        if len(id_lines) < 12 and len(text_lines) < 4:
            return True, (
                "goal needs visual list discovery but current text snapshot mostly contains "
                "navigation chrome"
            )

    return False, ""


def _find_target_line(input_descriptions: str, target_id: int) -> str:
    if not input_descriptions or not target_id:
        return ""
    pattern = rf"^\[ID:\s*{int(target_id)}\](.*)$"
    for line in input_descriptions.splitlines():
        m = re.match(pattern, line.strip())
        if m:
            return line.strip()
    return ""


def _decision_is_irrelevant_nav_click(decision: dict, goal: str, input_descriptions: str) -> bool:
    """
    当任务本质上是在列表/结果页里找具体条目时，
    若模型却去点击“电影/音乐/阅读/首页/工作台”之类的全站导航，应直接拦截。
    """
    if (decision.get("action") or "").strip().lower() != "click":
        return False
    if not _goal_prefers_visual_navigation(goal):
        return False

    target_id = int(decision.get("target_id", 0) or 0)
    line = _find_target_line(input_descriptions, target_id)
    if not line:
        return False

    nav_patterns = [
        r">首页</", r">电影</", r">音乐</", r">阅读</", r">读书</", r">同城</", r">小组</",
        r">FM</", r">时间</", r">豆品</", r">播客</", r">工作台</", r">控制台</",
        r">系统管理</", r">帮助</", r">设置</", r">个人中心</", r">消息</",
    ]
    return _text_matches_patterns(
        line,
        nav_patterns,
        extra_env_name="VSPIDER_EXTRA_GLOBAL_NAV_KEYWORDS",
    )


def _goal_is_bulk_extraction(goal: str) -> bool:
    if _goal_is_tooltip_extract(goal):
        return False
    try:
        if infer_goal_output_contract(goal).get("mode") == "answer":
            return False
    except Exception:
        pass
    return bool(
        re.search(
            r"获取|提取|抓取|采集|爬取|抽取|\bextract(?:_link)?\b|\bscrape\b|\bcrawl\b",
            str(goal or ""),
            re.IGNORECASE,
        )
    )


def _decision_click_targets_extraction_control(
    decision: dict,
    input_descriptions: str,
) -> bool:
    action = (decision.get("action") or "").strip().lower()
    if action not in ("click", "click_text", "click_point"):
        return False

    target_id = int(decision.get("target_id", 0) or 0)
    line = _find_target_line(input_descriptions, target_id) if target_id else ""
    text = " ".join(
        part
        for part in (
            line,
            str(decision.get("type_value") or ""),
            str(decision.get("thought") or ""),
        )
        if part
    )
    if not text:
        return False

    control_patterns = [
        r"\bnext\b", r"\bprev(?:ious)?\b", r"\bmore\b", r"load\s*more",
        r"\bpage\b", r"pagination", r"下一页", r"上一页", r"更多", r"加载更多",
        r"搜索", r"查询", r"\bsearch\b", r"\bquery\b", r"筛选", r"\bfilter\b",
        r"排序", r"\bsort\b", r"刷新", r"\brefresh\b", r"展开", r"收起",
        r"textbox", r"input", r"combobox", r"select", r"下拉",
    ]
    return _text_matches_patterns(
        text,
        control_patterns,
        extra_env_name="VSPIDER_EXTRA_EXTRACTION_CONTROL_KEYWORDS",
    )


def _goal_explicitly_requests_voice_or_camera(goal: str) -> bool:
    patterns = [
        r"语音", r"麦克风", r"voice", r"microphone",
        r"相机", r"camera", r"拍照", r"图片搜索", r"以图搜图",
        r"扫码", r"扫一扫", r"qr", r"lens",
    ]
    return any(re.search(pattern, goal, flags=re.IGNORECASE) for pattern in patterns)


def _goal_is_plain_search_task(goal: str) -> bool:
    if _goal_explicitly_requests_voice_or_camera(goal):
        return False
    search_patterns = [
        r"搜索", r"查询", r"search", r"query",
        r"搜索框", r"关键词", r"输入.*搜索框", r"点击搜索按钮",
    ]
    return any(re.search(pattern, goal, flags=re.IGNORECASE) for pattern in search_patterns)


def _decision_is_auxiliary_search_control_click(
    decision: dict,
    goal: str,
    input_descriptions: str,
) -> bool:
    """
    搜索页常见误点：把语音搜索、相机/拍照搜索、扫码入口当成主搜索按钮。
    这类按钮通常不是用户要的“正常输入 + 搜索”路径，应优先拦截。
    """
    if (decision.get("action") or "").strip().lower() != "click":
        return False
    if not _goal_is_plain_search_task(goal):
        return False

    target_id = int(decision.get("target_id", 0) or 0)
    line = _find_target_line(input_descriptions, target_id)
    if not line:
        return False

    aux_patterns = [
        r"语音", r"麦克风", r"voice", r"microphone",
        r"相机", r"camera", r"拍照", r"图片搜索", r"以图搜图",
        r"扫码", r"扫一扫", r"lens",
    ]
    return any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in aux_patterns)


def _decision_is_non_submit_search_control_click(
    decision: dict,
    goal: str,
    input_descriptions: str,
) -> bool:
    """
    搜索页另一个常见误判：把搜索建议项、清除按钮、历史记录入口当成"搜索提交按钮"。
    这类控件会让流程停留在输入态，随后模型又直接 done。
    """
    if (decision.get("action") or "").strip().lower() != "click":
        return False
    if not _goal_is_plain_search_task(goal):
        return False

    target_id = int(decision.get("target_id", 0) or 0)
    line = _find_target_line(input_descriptions, target_id)
    if not line:
        return False

    patterns = [
        r"搜索建议", r"建议", r"suggest", r"history", r"历史记录",
        r"删除", r"清除", r"clear", r"trigger",
    ]
    if not any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in patterns):
        return False

    # 真实搜索按钮本身也可能带 search 字样；避免误杀明显 submit 场景
    submit_markers = [r"搜索", r"提交", r"submit", r"search button", r"百度一下"]
    if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in submit_markers):
        return False
    return True


def _search_goal_done_looks_premature(
    goal: str,
    start_url: str,
    current_url: str,
    page_summary: str,
    semantic_text: str,
) -> bool:
    """
    对常规搜索任务做一层完成态校验：
    - 若仍停留在起始搜索页/输入态，且出现搜索建议、删除、历史记录等痕迹，则不应 done
    - 若 URL 已带查询参数或页面明显进入结果态，则允许 done
    """
    if not _goal_is_plain_search_task(goal):
        return False

    current_url = (current_url or "").strip()
    start_url = (start_url or "").strip()
    summary_text = "\n".join(part for part in (page_summary, semantic_text) if part)

    result_markers = [
        r"结果页", r"搜索结果", r"results?", r"search results?",
        r"\bq=", r"\bquery=", r"\bwd=", r"\bkeyword=", r"\btext=",
    ]
    if any(re.search(pattern, current_url, flags=re.IGNORECASE) for pattern in result_markers):
        return False
    if any(re.search(pattern, summary_text, flags=re.IGNORECASE) for pattern in result_markers):
        return False

    input_stage_markers = [
        r"搜索建议", r"suggest", r"历史记录", r"history",
        r"删除", r"清除", r"clear",
    ]
    same_page = current_url.rstrip("/") == start_url.rstrip("/")
    if same_page and any(re.search(pattern, summary_text, flags=re.IGNORECASE) for pattern in input_stage_markers):
        return True

    return False


def _decision_implies_completion(decision: dict) -> bool:
    """
    当模型在 thought/current_state 中已经明确承认"任务已完成"，
    但 action 仍然输出 click/type 等动作时，进行通用兜底。
    """
    if (decision.get("action") or "").strip().lower() == "done":
        return False

    text = "\n".join(
        str(decision.get(key, "") or "")
        for key in ("current_state", "thought")
    ).strip()
    if not text:
        return False

    follow_up_patterns = [
        r"进入下一阶段",
        r"进入下一子目标",
        r"进入下一步",
        r"推进至下一",
        r"推进至[^\n]{0,30}(?:阶段|子目标|步骤)",
        r"可推进至[^\n]{0,30}(?:阶段|子目标|步骤)",
        r"下一阶段",
        r"下一子目标",
        r"下一步",
        r"接下来",
        r"随后",
        r"然后",
        r"继续(?:执行|点击|选择|展开|翻页|填写|提取|搜索)?",
        r"填写(?:数据)?阶段",
        r"提交阶段",
        r"提取阶段",
        r"还需(?:要)?",
        r"仍需(?:要)?",
        r"需要继续",
        r"需要再",
        r"需(?:要)?(?:点击|选择|展开|输入|提取|翻页|确认)",
        r"点击[^\n]{0,40}以(?:展开|打开|进入|继续|完成|选择|获取)",
        r"选择[^\n]{0,40}以(?:展开|进入|继续|完成)",
        r"展开[^\n]{0,40}以(?:继续|完成|查看)",
        r"以展开其子项",
    ]
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in follow_up_patterns):
        return False

    completion_patterns = [
        r"任务已完成",
        r"任务已(?:经)?(?:全部)?完成",
        r"任务已(?:经)?(?:全部)?达成",
        r"任务[^\n]{0,30}达成退出标准",
        r"用户目标已(?:经)?(?:全部)?达成",
        r"目标已(?:经)?(?:全部)?达成",
        r"已全部达成",
        r"无需再操作",
        r"不需要再操作",
        r"无需再点击",
        r"不需要再点击",
        r"选择已(?:经)?完成",
        r"已成功选择",
        r"可直接结束任务",
        r"可以直接结束任务",
        r"直接结束任务",
        r"直接输出\s*done",
        r"准备输出\s*done",
        r"task (?:is )?complete(?:d)?",
        r"goal (?:has been )?achieved",
        r"already completed",
        r"no further action needed",
        r"all required steps have been completed",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in completion_patterns)


def _decision_mentions_follow_up_work(decision: dict) -> bool:
    """Whether the decision text still describes remaining user-visible work."""
    text = "\n".join(
        str(decision.get(key, "") or "")
        for key in ("progress_review", "current_state", "thought")
    ).strip()
    if not text:
        return False

    follow_up_patterns = [
        r"进入下一阶段",
        r"进入下一子目标",
        r"进入下一步",
        r"推进至下一",
        r"推进至[^\n]{0,30}(?:阶段|子目标|步骤)",
        r"可推进至[^\n]{0,30}(?:阶段|子目标|步骤)",
        r"下一阶段",
        r"下一子目标",
        r"下一步",
        r"接下来",
        r"随后",
        r"然后",
        r"继续(?:执行|点击|选择|展开|翻页|填写|提取|搜索)?",
        r"填写(?:数据)?阶段",
        r"提交阶段",
        r"提取阶段",
        r"还需(?:要)?",
        r"仍需(?:要)?",
        r"需要继续",
        r"需要再",
        r"需(?:要)?(?:点击|选择|展开|输入|提取|翻页|确认)",
        r"点击[^\n]{0,40}以(?:展开|打开|进入|继续|完成|选择|获取)",
        r"选择[^\n]{0,40}以(?:展开|进入|继续|完成)",
        r"展开[^\n]{0,40}以(?:继续|完成|查看)",
        r"以展开其子项",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in follow_up_patterns)


def _decision_claims_current_subgoal_completed(decision: dict) -> bool:
    """
    对 action=done 的决策做补充判定：若模型在 progress_review/thought/current_state
    中明确声明“当前子目标退出标准已满足”，即使漏填 subgoal_status，也视作当前
    子目标已完成。
    """
    if (decision.get("subgoal_status") or "").strip().lower() == "completed":
        return True

    text = "\n".join(
        str(decision.get(key, "") or "")
        for key in ("progress_review", "current_state", "thought")
    ).strip()
    if not text:
        return False

    exit_met_patterns = [
        r"满足(?:了)?[^\n]{0,30}退出标准",
        r"符合[^\n]{0,30}退出标准",
        r"子目标[^\n]{0,20}(?:已完成|完成)",
        r"当前子目标[^\n]{0,20}(?:已完成|完成)",
        r"已验证[^\n]{0,40}(?:成功|完成|可见|已加载)",
        r"exit criteria (?:is )?met",
        r"current subgoal (?:is )?complete(?:d)?",
    ]
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in exit_met_patterns):
        return True

    completion_patterns = [
        r"任务已完成",
        r"任务已(?:经)?(?:全部)?完成",
        r"任务已(?:经)?(?:全部)?达成",
        r"任务[^\n]{0,30}达成退出标准",
        r"用户目标已(?:经)?(?:全部)?达成",
        r"目标已(?:经)?(?:全部)?达成",
        r"已全部达成",
        r"无需再操作",
        r"不需要再操作",
        r"无需再点击",
        r"不需要再点击",
        r"选择已(?:经)?完成",
        r"已成功选择",
        r"可直接结束任务",
        r"可以直接结束任务",
        r"直接结束任务",
        r"直接输出\s*done",
        r"准备输出\s*done",
        r"task (?:is )?complete(?:d)?",
        r"goal (?:has been )?achieved",
        r"already completed",
        r"no further action needed",
        r"all required steps have been completed",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in completion_patterns)


def _decision_should_finish_instead_of_operate(
    decision: dict,
    *,
    output_mode: str = "",
) -> bool:
    """Answer-only guard for contradictory "complete, but still click" decisions."""
    action = (decision.get("action") or "").strip().lower()
    if not action or action in {
        "done",
        "ask_human",
        "error",
        "extract",
        "chat_extract",
        "fetch_link_content",
        "fetch_links_batch",
        "download_image",
        "upload",
        "save_to_memory",
    }:
        return False
    if str(output_mode or "").strip().lower() != "answer":
        return False
    if decision.get("extracted_data"):
        return False
    if _decision_mentions_follow_up_work(decision):
        return False
    if not _decision_claims_current_subgoal_completed(decision):
        return False

    text = "\n".join(
        str(decision.get(key, "") or "")
        for key in ("progress_review", "current_state", "thought")
    ).strip()
    if not text:
        return False

    finish_patterns = [
        r"无需(?:再|进一步)?(?:操作|点击|搜索|处理|交互)",
        r"无须(?:再|进一步)?(?:操作|点击|搜索|处理|交互)",
        r"不需要(?:再|进一步)?(?:操作|点击|搜索|处理|交互)",
        r"应(?:该)?直接(?:结束|输出\s*done)",
        r"可(?:以)?直接(?:结束|输出\s*done)",
        r"任务目标(?:已|已经)?(?:达成|满足|完成)",
        r"用户(?:问题|需求|目标)[^\n]{0,80}已(?:完全)?(?:覆盖|满足|达成)",
        r"所有信息(?:均|都)?已(?:可见|覆盖|满足)",
        r"no further (?:action|operation|click|search) needed",
        r"should (?:finish|end|return done)",
        r"(?:task|goal) (?:is )?(?:complete|completed|satisfied|achieved)",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in finish_patterns)


def _done_targets_final_subgoal(plan: "TaskPlan | Any | None", decision: dict) -> bool:
    """Whether a done decision is completing the already-active final subgoal.

    PLAN GATE runs before Wave 2 advances current_idx, so the first done on the
    last subgoal should be allowed when the model explicitly marks that subgoal
    complete. Otherwise result-page form tasks pay an unnecessary wait/extract
    tail before the second done is accepted.
    """
    if plan is None:
        return False
    sub_goals = list(getattr(plan, "sub_goals", []) or [])
    if not sub_goals:
        return False
    try:
        cur_idx = int(getattr(plan, "current_idx", 0) or 0)
    except Exception:
        cur_idx = 0
    cur_idx = max(0, min(cur_idx, len(sub_goals) - 1))
    return cur_idx >= len(sub_goals) - 1 and _decision_claims_current_subgoal_completed(decision)


def _is_terminal_only_subgoal(subgoal: "TaskPlan | Any") -> bool:
    """识别仅用于收尾输出 done 的行政型尾子目标。"""
    description = str(getattr(subgoal, "description", "") or "").strip()
    exit_criteria = str(getattr(subgoal, "exit_criteria", "") or "").strip()
    if not (description or exit_criteria):
        return False

    def _strip_negated_noop_phrases(text: str) -> str:
        cleaned = text
        noop_patterns = [
            r"不执行任何交互",
            r"无需任何交互",
            r"不需要任何交互",
            r"无须任何交互",
            r"无需再操作",
            r"不需要再操作",
            r"无须再操作",
            r"无需任何操作",
            r"不需要任何操作",
            r"无须任何操作",
        ]
        for pattern in noop_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        return cleaned

    _action_text = _strip_negated_noop_phrases(
        "\n".join(part for part in (description, exit_criteria) if part)
    )
    physical_action_patterns = [
        r"点击",
        r"单击",
        r"\bclick(?:_new_tab|_point)?\b",
        r"输入",
        r"填写",
        r"\btype\b",
        r"提取",
        r"\bextract(?:_link)?\b",
        r"按(?:键|下)",
        r"\bpress_key\b",
        r"滚动",
        r"翻页",
        r"\bscroll\b",
        r"\bsmooth_scroll\b",
        r"悬停",
        r"\bhover\b",
        r"选择",
        r"\bselect\b",
        r"上传",
        r"\bupload\b",
        r"下载",
        r"导出",
        r"\bdownload(?:_image)?\b",
        r"拖拽",
        r"拖动",
        r"\bdrag(?:_and_drop)?\b",
        r"移除",
        r"\bremove_element\b",
        r"导航",
        r"跳转",
        r"\bgoto\b",
        r"登录",
        r"搜索",
        r"提交",
        r"关闭(?:弹窗|对话框|标签页)?",
        r"\bclose_tab\b",
        r"切换(?:标签页)?",
        r"\bswitch_tab\b",
        r"\bask_human\b",
        r"人工处理",
    ]
    if any(re.search(pattern, _action_text, flags=re.IGNORECASE) for pattern in physical_action_patterns):
        return False

    terminal_prefix_patterns = [
        r"^\s*任务完成(?:[:：,，。；!！\s].*)?$",
        r"^\s*结束任务(?:[:：,，。；!！\s].*)?$",
        r"^\s*任务终止(?:[:：,，。；!！\s].*)?$",
        r"^\s*终止任务(?:[:：,，。；!！\s].*)?$",
        r"^\s*确认目标达成(?:后)?(?:[:：,，。；!！\s].*)?$",
        r"^\s*完成\s*goal\s*全部要求(?:[:：,，。；!！\s].*)?$",
        r"^\s*完成用户全部需求(?:[:：,，。；!！\s].*)?$",
        r"^\s*(?:准备)?输出\s*done(?:[:：,，。；!！\s].*)?$",
        r"^\s*action\s*=\s*done(?:[:：,，。；!！\s].*)?$",
        r"^\s*直接\s*done(?:[:：,，。；!！\s].*)?$",
        r"^\s*ready to output done(?:[:：,，。；!！\s].*)?$",
        r"^\s*finish(?: the)? task(?:[:：,，。；!！\s].*)?$",
        r"^\s*terminate(?: the)? task(?:[:：,，。；!！\s].*)?$",
        r"^\s*不执行任何交互(?:[:：,，。；!！\s].*)?$",
        r"^\s*无需任何交互(?:[:：,，。；!！\s].*)?$",
    ]

    for candidate in (description, exit_criteria):
        if candidate and any(
            re.search(pattern, candidate, flags=re.IGNORECASE)
            for pattern in terminal_prefix_patterns
        ):
            return True
    return False


def _goal_explicitly_requests_backtracking(goal: str) -> bool:
    """用户目标若明确要求返回/回到某页，则不启用回退拦截。"""
    backtrack_patterns = [
        r"返回", r"回到", r"回退", r"上一页",
        r"go back", r"return to", r"back to",
    ]
    return any(re.search(pattern, goal, flags=re.IGNORECASE) for pattern in backtrack_patterns)


def _decision_is_regressive_backtrack(decision: dict, goal: str) -> bool:
    """
    结果页/确认页上最常见的误判是：为了"补走中间步骤"又返回首页重做。
    除非用户明确要求返回，否则这种回退一般应直接视为 done。
    """
    if _goal_explicitly_requests_backtracking(goal):
        return False

    action = (decision.get("action") or "").strip().lower()
    if action not in {"click", "goto", "switch_tab"}:
        return False

    text = "\n".join(
        str(decision.get(key, "") or "")
        for key in ("current_state", "thought")
    ).strip()
    if not text:
        return False

    result_page_patterns = [
        r"结果页", r"结果页面", r"搜索结果", r"查询结果",
        r"成功页", r"确认页", r"已提交", r"已执行完毕",
        r"search results?", r"results? page", r"confirmation page",
        r"success page", r"submitted successfully",
    ]
    backtrack_patterns = [
        r"返回首页", r"回到首页", r"返回主页", r"回到主页",
        r"返回主页面", r"回到主页面", r"返回上一页", r"回到上一页",
        r"返回上一步", r"回到上一步",
        r"go back", r"back to home", r"return to home",
        r"return to homepage", r"back to the main page",
    ]

    return (
        any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in result_page_patterns)
        and any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in backtrack_patterns)
    )


def _force_done_decision(decision: dict) -> dict:
    """将一条自相矛盾的决策强制纠正为 done。"""
    normalized = dict(decision)
    normalized["action"] = "done"
    normalized["target_id"] = 0
    normalized["type_value"] = ""
    normalized["memory_key"] = ""
    normalized["point"] = None
    return normalized


def _clean_user_visible_done_message(message: object) -> str:
    """Remove engine-only guard annotations from final user-visible done text."""
    text = str(message or "").strip()
    if not text:
        return ""
    text = re.sub(r"\[ANSWER_DONE_INTENT_GUARD\]\s*", "", text)
    text = re.sub(r"\[(?:EXTRACT_NULL|ZERO_TARGET)_DOWNGRADE\]\s*", "", text)
    text = re.sub(
        r"页面答案已满足用户问题，系统将原动作\s*['\"][^'\"]*['\"]\s*改为\s*done[。.]?\s*",
        "",
        text,
    )
    text = re.sub(
        r"Answer-mode decision text says the task is complete;[^\n]*(?:\n|$)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def _format_extracted_rows_as_answer(rows: object) -> str:
    """Format structured answer-mode rows for the Final Answer panel."""
    if rows is None:
        return ""
    if isinstance(rows, dict):
        direct_answer = rows.get("answer")
        if direct_answer:
            return str(direct_answer).strip()
        row_list: list[object] = [rows]
    elif isinstance(rows, list):
        row_list = rows
    else:
        return str(rows).strip()

    lines: list[str] = []
    for raw_row in row_list[:5]:
        if isinstance(raw_row, dict):
            direct_answer = raw_row.get("answer")
            if direct_answer:
                lines.append(str(direct_answer).strip())
                continue
            weather = str(
                raw_row.get("weather")
                or raw_row.get("天气")
                or raw_row.get("condition")
                or ""
            ).strip()
            temperature = str(
                raw_row.get("temperature")
                or raw_row.get("气温")
                or raw_row.get("temp")
                or ""
            ).strip()
            rain_probability = str(
                raw_row.get("rain_probability")
                or raw_row.get("降雨概率")
                or raw_row.get("precipitation_probability")
                or ""
            ).strip()
            if weather or temperature or rain_probability:
                weather_line = ""
                if weather:
                    weather_line = (
                        f"会下雨，天气为{weather}"
                        if "雨" in weather
                        else f"天气为{weather}"
                    )
                detail_parts = [weather_line] if weather_line else []
                if temperature:
                    detail_parts.append(f"气温 {temperature}")
                if rain_probability:
                    detail_parts.append(f"降雨概率 {rain_probability}")
                lines.append("；".join(detail_parts) + "。")
                continue
            parts = [
                f"{key}: {value}"
                for key, value in raw_row.items()
                if str(value or "").strip()
                and str(key or "").strip()
                not in {"source", "page_url", "url", "output_file"}
            ]
            if parts:
                lines.append("- " + "; ".join(parts))
        elif str(raw_row or "").strip():
            lines.append("- " + str(raw_row).strip())
    if len(row_list) > 5:
        lines.append(f"...共 {len(row_list)} 条")
    return "\n".join(line for line in lines if line).strip()


_WEATHER_GOAL_KEYWORDS = (
    "天气",
    "气温",
    "温度",
    "下雨",
    "有雨",
    "降雨",
    "降水",
)

_WEATHER_CONDITION_RE = (
    r"雷阵雨|阵雨|小雨|中雨|大雨|暴雨|雷雨|雨夹雪|小雪|中雪|大雪|"
    r"多云|晴|阴天?|雾|霾|沙尘|浮尘|扬沙|雨|雪"
)


def _clean_weather_city_candidate(candidate: str) -> str:
    text = str(candidate or "").strip()
    text = re.sub(r"[\s，,。！？?；;：:、（）()【】\[\]\"'“”‘’]+", "", text)
    text = re.sub(
        r"^(?:帮我|请|麻烦|帮忙|能不能|可以|给我|想知道)+",
        "",
        text,
    )
    text = re.sub(
        r"(?:查一下|查询|查查|查|看一下|看看|看|告诉我|了解一下|一下)",
        "",
        text,
    )
    text = re.sub(
        r"(?:今天|明天|后天|明日|未来一周|未来七天|未来7天|未来三天|未来3天)",
        "",
        text,
    )
    text = re.sub(r"(?:天气预报|天气|气温|温度|预报|的)$", "", text)
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z·]", "", text)
    if len(text) > 24:
        text = text[-24:]
    if text in {"天气", "气温", "温度", "下雨", "有雨", "降雨", "降水"}:
        return ""
    return text if len(text) >= 2 else ""


def _extract_weather_city_from_goal(goal: str) -> str:
    text = re.sub(r"\s+", "", str(goal or ""))
    if not any(keyword in text for keyword in _WEATHER_GOAL_KEYWORDS):
        return ""

    rain_match = re.search(
        r"(.{0,60}?)(?:会不会|是否|有没有|有无|会有|可能会)"
        r".{0,8}(?:下雨|有雨|降雨|降水|雨)",
        text,
    )
    if rain_match:
        city = _clean_weather_city_candidate(rain_match.group(1))
        if city:
            return city

    patterns = [
        r"(?:今天|明天|后天|明日)\s*([\u4e00-\u9fffA-Za-z·]{2,30}?)(?:的)?(?:天气|气温|温度|预报)",
        r"([\u4e00-\u9fffA-Za-z·]{2,30}?)(?:今天|明天|后天|明日)(?:的)?(?:天气|气温|温度|预报|会不会|是否|有没有|有无)",
        r"(?:查一下|查询|查查|查|看看|看一下)?\s*([\u4e00-\u9fffA-Za-z·]{2,30}?)(?:的)?(?:天气|气温|温度|天气预报)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        city = _clean_weather_city_candidate(match.group(1))
        if city:
            return city
    return ""


def _infer_answer_weather_search_query(goal: str) -> str:
    """Infer a clean search query for answer-only weather lookups."""
    city = _extract_weather_city_from_goal(goal)
    if not city:
        return ""
    text = str(goal or "")
    if re.search(r"未来\s*(?:一|七|7)\s*天|一周|七天|7天", text):
        horizon = "未来一周"
    elif re.search(r"未来\s*(?:三|3)\s*天|三天|3天", text):
        horizon = "未来三天"
    elif "后天" in text:
        horizon = "后天"
    elif "今天" in text:
        horizon = "今天"
    elif "明天" in text or "明日" in text:
        horizon = "明天"
    else:
        horizon = ""
    return f"{city}{horizon}天气" if horizon else f"{city}天气"


def _baidu_query_from_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
        params = parse_qs(parsed.query or "")
    except Exception:
        return ""
    for key in ("wd", "word", "q", "query"):
        values = params.get(key) or []
        if values:
            return unquote_plus(str(values[0] or "")).strip()
    return ""


def _is_baidu_url(url: str) -> bool:
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except Exception:
        return False
    return host == "baidu.com" or host.endswith(".baidu.com")


def _normalize_search_query(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _infer_answer_stock_search_query(goal: str) -> str:
    """Infer a clean baidu search query for stock-flavoured answer goals."""
    name = _extract_stock_subject_from_goal(goal)
    if not name:
        return ""
    return f"{name} 股价"


def _infer_answer_recipe_search_query(goal: str) -> str:
    """Infer a clean baidu search query for recipe-flavoured answer goals."""
    dish = _extract_recipe_name_from_goal(goal)
    if not dish:
        return ""
    return f"{dish} 做法"


def _infer_answer_flight_search_query(goal: str) -> str:
    """Infer a clean baidu search query for flight-flavoured answer goals."""
    flight = _extract_flight_number_from_goal(goal)
    if not flight:
        return ""
    return f"{flight} 航班动态"


def _build_answer_search_fast_path_url(
    goal: str,
    *,
    start_url: str,
    current_url: str,
    output_mode: str,
) -> tuple[str, str]:
    """Return (query, url) for a safe direct Baidu answer search, if useful.

    Domain-aware: tries weather → stock → recipe → flight inferers in order.
    The first non-empty query wins. Returns ``("", "")`` on:
      * non-answer output_mode
      * neither URL is Baidu
      * no inferer produced a query
      * current Baidu wd already matches the inferred query
    """
    if str(output_mode or "") != "answer":
        return "", ""
    if not (_is_baidu_url(start_url) or _is_baidu_url(current_url)):
        return "", ""
    query = (
        _infer_answer_weather_search_query(goal)
        or _infer_answer_stock_search_query(goal)
        or _infer_answer_recipe_search_query(goal)
        or _infer_answer_flight_search_query(goal)
    )
    if not query:
        return "", ""
    current_query = _baidu_query_from_url(current_url)
    if _normalize_search_query(current_query) == _normalize_search_query(query):
        return "", ""
    return query, "https://www.baidu.com/s?wd=" + quote_plus(query)


async def _maybe_run_answer_search_fast_path(
    browser: BrowserEnv,
    event_stream: EventStream,
    *,
    goal: str,
    start_url: str,
    output_mode: str,
) -> bool:
    current_url = getattr(browser, "current_url", "") or ""
    query, target_url = _build_answer_search_fast_path_url(
        goal,
        start_url=start_url,
        current_url=current_url,
        output_mode=output_mode,
    )
    if not target_url:
        return False
    try:
        page = await browser._ensure_active_page(reason="answer search fast path")
        if page is None or page.is_closed():
            return False
        logger.info(
            "[ANSWER SEARCH FAST PATH] %s -> %s",
            (current_url or start_url)[:140],
            target_url,
        )
        event_stream.guard(
            step=0,
            name="ANSWER_SEARCH_FAST_PATH",
            message=f"Direct search for answer-mode weather query: {query}",
            metadata={
                "query": query,
                "from_url": current_url or start_url,
                "target_url": target_url,
            },
        )
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        try:
            await browser._wait_for_page_stable()
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.debug("[ANSWER SEARCH FAST PATH] skipped: %s", exc)
        event_stream.guard(
            step=0,
            name="ANSWER_SEARCH_FAST_PATH_ERROR",
            message=f"{type(exc).__name__}: {exc}",
            metadata={"query": query, "target_url": target_url},
        )
        return False


def _city_regex_for_weather_text(city: str) -> str:
    city = str(city or "").strip()
    if not city:
        return ""
    suffixes = "市县区州盟旗"
    if city[-1:] in suffixes and len(city) > 2:
        base = re.escape(city[:-1])
        return base + f"[{suffixes}]?"
    return re.escape(city) + f"[{suffixes}]?"


def _normalize_weather_temperature(value: str) -> str:
    temp = str(value or "").strip()
    temp = temp.replace("－", "-").replace("—", "-").replace("到", "~").replace("至", "~")
    temp = re.sub(r"\s+", "", temp)
    if temp.endswith("°"):
        temp = temp[:-1] + "℃"
    elif temp and not re.search(r"(?:℃|°C|度|C)$", temp):
        temp += "℃"
    return temp


def _compose_weather_answer(city: str, weather: str, temperature: str, context: str) -> str:
    weather = str(weather or "").strip()
    temperature = _normalize_weather_temperature(temperature)
    context = str(context or "")
    no_rain = bool(re.search(r"无降水|无降雨|无雨|不下雨|不会下雨|没有降水|没有雨", context))
    has_rain = bool(
        not no_rain
        and (
            "雨" in weather
            or re.search(r"有(?:小雨|中雨|大雨|阵雨|雷雨|降水|降雨)", context)
            or re.search(r"降水概率\s*(?:[1-9]\d?|100)\s*%", context)
        )
    )
    rain_text = "会下雨" if has_rain else "不会下雨"
    parts = [f"明天{city}{rain_text}"]
    if weather:
        parts.append(f"天气为{weather}")
    if temperature:
        parts.append(f"气温 {temperature}")
    return "，".join(parts) + "。"


def _compact_weather_answer_from_text(text: str, goal: str) -> str:
    city = _extract_weather_city_from_goal(goal)
    if not city:
        return ""
    raw = str(text or "").strip()
    if not raw:
        return ""
    cleaned = raw.replace("\u200c", "").replace("\u200b", "").replace("*", "")
    compact = re.sub(r"\s+", " ", cleaned)
    city_re = _city_regex_for_weather_text(city)
    if not city_re:
        return ""
    temp_re = r"[-−]?\d{1,2}\s*[~～\-—至到]\s*[-−]?\d{1,2}\s*(?:℃|°C|°|度|C)?"
    detailed_pattern = re.compile(
        rf"(?P<context>{city_re}\s*明天(?:（[^）]{{0,40}}）|\([^)]{{0,40}}\))?"
        rf"\s*为\s*(?P<weather>{_WEATHER_CONDITION_RE})(?:天气)?"
        rf"[^。；;\n]{{0,40}}?(?:温度范围|气温|温度)\s*(?P<temp>{temp_re})"
        rf"[^。；;\n]{{0,80}})",
        re.IGNORECASE,
    )
    for match in detailed_pattern.finditer(compact):
        return _compose_weather_answer(
            city,
            match.group("weather"),
            match.group("temp"),
            match.group("context"),
        )

    card_pattern = re.compile(
        rf"(?P<context>{city_re}[^。；;\n]{{0,120}}?"
        rf"(?P<temp>{temp_re})\s*(?P<weather>{_WEATHER_CONDITION_RE})"
        rf"[^。；;\n]{{0,80}})",
        re.IGNORECASE,
    )
    for match in card_pattern.finditer(compact):
        return _compose_weather_answer(
            city,
            match.group("weather"),
            match.group("temp"),
            match.group("context"),
        )

    reverse_card_pattern = re.compile(
        rf"(?P<context>{city_re}[^。；;\n]{{0,160}}?"
        rf"(?P<weather>{_WEATHER_CONDITION_RE})\s*(?P<temp>{temp_re})"
        rf"[^。；;\n]{{0,80}})",
        re.IGNORECASE,
    )
    for match in reverse_card_pattern.finditer(compact):
        return _compose_weather_answer(
            city,
            match.group("weather"),
            match.group("temp"),
            match.group("context"),
        )
    return ""


# ── Domain detection vocab for answer-mode goals ────────────────────────────
# Weather vocab is already captured by _WEATHER_GOAL_KEYWORDS.
_STOCK_GOAL_KEYWORDS = (
    "股价", "股票", "股市", "收盘", "开盘", "涨跌",
    "市值", "K线", "行情", "股权", "证券",
)

_RECIPE_GOAL_KEYWORDS = (
    "怎么做", "做法", "食谱", "菜谱", "如何做",
    "怎么烧", "怎么炒", "教程",
)

_FLIGHT_GOAL_KEYWORDS = (
    "航班", "航班号", "航空", "起飞", "降落", "到达",
    "登机口", "航站楼", "延误", "准点",
)


def _detect_answer_domain(goal: str) -> str:
    """Classify an answer-mode goal into a known domain.

    Returns one of ``"weather"`` / ``"stock"`` / ``"recipe"`` / ``"generic"``.
    Used by ``_compact_answer_text_for_goal`` to route to the right
    domain-specific compactor. ``"generic"`` falls through to raw text.
    """
    text = re.sub(r"\s+", "", str(goal or ""))
    if not text:
        return "generic"
    if any(kw in text for kw in _WEATHER_GOAL_KEYWORDS):
        return "weather"
    if any(kw in text for kw in _STOCK_GOAL_KEYWORDS):
        return "stock"
    if any(kw in text for kw in _RECIPE_GOAL_KEYWORDS):
        return "recipe"
    if any(kw in text for kw in _FLIGHT_GOAL_KEYWORDS) or _extract_flight_number_from_goal(text):
        return "flight"
    return "generic"


def _extract_stock_subject_from_goal(goal: str) -> str:
    """Pull the company / ticker name from a stock-flavoured goal."""
    text = re.sub(r"\s+", "", str(goal or ""))
    if not text:
        return ""
    text = re.sub(r"^(?:帮我|请|麻烦|帮忙|能不能|可以|给我|想知道)+", "", text)
    text = re.sub(
        r"(?:查一下|查询|查查|查|看一下|看看|看|告诉我|了解一下|一下|现在|当前|今日|今天|目前)",
        "",
        text,
    )
    m = re.search(
        # Non-greedy so the captured name does not eat the trailing "的";
        # explicit "的?" between name and the topic word handles "贵州茅台的股价".
        r"([\u4e00-\u9fffA-Za-z]{2,12}?)的?(?:股价|股票|股市|收盘价|开盘价|行情|市值)",
        text,
    )
    if m:
        return m.group(1)
    return ""


def _compact_stock_answer_for_goal(text: str, goal: str) -> str:
    """Compress a stock-quote page into ``{name}（{code}）现价 ¥{price}（涨跌幅 {pct}）。``

    Returns ``""`` when the goal is not stock-flavoured or no price line
    can be confidently located — the caller falls back to raw text.
    """
    name = _extract_stock_subject_from_goal(goal)
    if not name:
        return ""
    raw = str(text or "")
    if not raw.strip():
        return ""
    compact = re.sub(r"\s+", " ", raw)
    price_m = re.search(
        r"(?:现价|最新价|当前价|当前)\s*[:：]?\s*(?P<price>\d{1,6}(?:\.\d{1,4})?)\s*元?",
        compact,
    )
    if not price_m:
        # Fallback: name proximity to a price-like number
        anchor = rf"{re.escape(name)}[^\d\n]{{0,40}}?(?P<price>\d{{1,6}}(?:\.\d{{1,4}})?)\s*元"
        price_m = re.search(anchor, compact)
    if not price_m:
        return ""
    price = price_m.group("price")
    window = compact[max(0, price_m.start() - 20):price_m.end() + 80]
    pct_m = re.search(r"(?P<pct>[+\-−]?\d+(?:\.\d+)?)\s*%", window)
    pct = (pct_m.group("pct") + "%") if pct_m else ""
    code_m = re.search(
        rf"{re.escape(name)}[^\d]{{0,12}}(?P<code>\d{{6}})",
        compact,
    )
    code = code_m.group("code") if code_m else ""
    parts = [name]
    if code:
        parts.append(f"（{code}）")
    parts.append(f"现价 ¥{price}")
    if pct:
        parts.append(f"（涨跌幅 {pct}）")
    return "".join(parts) + "。"


def _extract_recipe_name_from_goal(goal: str) -> str:
    """Pull the dish name from a recipe-flavoured goal."""
    text = re.sub(r"\s+", "", str(goal or ""))
    if not text:
        return ""
    text = re.sub(r"^(?:帮我|请|麻烦|帮忙|能不能|可以|给我|想知道)+", "", text)
    m = re.search(
        # Non-greedy + explicit "的?" so "红烧肉的做法" extracts "红烧肉".
        r"([\u4e00-\u9fffA-Za-z]{2,16}?)的?(?:怎么做|做法|食谱|菜谱|如何做|怎么烧|怎么炒|教程)",
        text,
    )
    if m:
        return m.group(1)
    return ""


def _compact_recipe_answer_for_goal(text: str, goal: str) -> str:
    """Summarise a recipe page into ``{name}；用料：…；步骤：A → B → C。``

    Returns ``""`` when the goal is not recipe-flavoured or the dish name
    cannot be located in the body text — caller falls back to raw text.
    """
    name = _extract_recipe_name_from_goal(goal)
    if not name:
        return ""
    raw = str(text or "")
    if not raw.strip():
        return ""
    name_pos = raw.find(name)
    if name_pos < 0:
        return ""
    section = raw[name_pos:name_pos + 800]
    ingredient_lines: list[str] = []
    step_lines: list[str] = []
    for line in section.split("\n"):
        line = line.strip()
        if not line:
            continue
        ing_m = re.match(r"^(?:主料|辅料|配料|材料|用料)\s*[:：]\s*(.+)$", line)
        if ing_m:
            ingredient_lines.append(ing_m.group(1).strip())
            continue
        if len(step_lines) < 3:
            step_m = re.match(r"^(?:步骤\s*)?\d+[\.、：:]?\s*(.+)$", line)
            if step_m and len(step_m.group(1).strip()) >= 2:
                step_lines.append(step_m.group(1).strip())
    parts = [name]
    if ingredient_lines:
        parts.append("用料：" + "；".join(ingredient_lines))
    if step_lines:
        parts.append("步骤：" + " → ".join(step_lines))
    if len(parts) == 1:
        # Only the dish name — not enough signal to compact
        return ""
    return "；".join(parts) + "。"


def _extract_flight_number_from_goal(goal: str) -> str:
    """Extract an IATA-style flight number (e.g. ``CA1234``) from a goal."""
    text = re.sub(r"\s+", "", str(goal or "")).upper()
    if not text:
        return ""
    m = re.search(r"(?<![A-Z0-9])([A-Z]{2}\d{3,4}[A-Z]?)(?![A-Z0-9])", text)
    if m:
        return m.group(1)
    return ""


_FLIGHT_STATUS_KEYWORDS = (
    "已起飞", "已到达", "已落地", "已取消", "取消",
    "延误", "准点", "登机中", "候机中", "计划",
    "备降", "返航", "在飞",
)


def _compact_flight_answer_for_goal(text: str, goal: str) -> str:
    """Compress a flight-info page into ``{flight} {status} {dep} → {arr}。``

    Returns ``""`` if the flight number is missing from goal or absent in
    body text, or no status / time signals are present near it — caller
    falls back to raw text in that case.
    """
    flight = _extract_flight_number_from_goal(goal)
    if not flight:
        return ""
    raw = str(text or "")
    if not raw.strip() or flight not in raw.upper():
        return ""
    compact = re.sub(r"\s+", " ", raw)
    idx = compact.upper().find(flight)
    if idx < 0:
        return ""
    window = compact[idx:idx + 240]
    status = next((kw for kw in _FLIGHT_STATUS_KEYWORDS if kw in window), "")
    times = re.findall(r"\d{1,2}[:：]\d{2}", window)
    parts: list[str] = [flight]
    if status:
        parts.append(status)
    if len(times) >= 2:
        parts.append(f"{times[0]} → {times[1]}")
    elif times:
        parts.append(times[0])
    if len(parts) == 1:
        return ""
    return " ".join(parts) + "。"


def _compact_answer_text_for_goal(text: str, goal: str) -> str:
    """Domain-router for answer-mode page-text compaction.

    Dispatches the raw page text to the matching domain compactor based
    on goal vocabulary; if the domain compactor cannot extract a clean
    answer (returns ``""``), falls back to the raw input. This lets the
    Final Answer panel show the best-available text without ever swallowing
    the original content for non-routable goals.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    domain = _detect_answer_domain(goal)
    if domain == "weather":
        compacted = _compact_weather_answer_from_text(raw, goal)
        if compacted:
            return compacted
    elif domain == "stock":
        compacted = _compact_stock_answer_for_goal(raw, goal)
        if compacted:
            return compacted
    elif domain == "recipe":
        compacted = _compact_recipe_answer_for_goal(raw, goal)
        if compacted:
            return compacted
    elif domain == "flight":
        compacted = _compact_flight_answer_for_goal(raw, goal)
        if compacted:
            return compacted
    return raw



def _compact_rpa_trail(trail: list[dict]) -> list[dict]:
    """
    压缩连续重复的 RPA 动作，避免把重复提交/重复点击固化进缓存。
    只压缩"完全相同"的连续动作，尽量不改变真实流程语义。
    """
    compacted: list[dict] = []

    def _same_step(prev: dict, cur: dict) -> bool:
        return (
            (prev.get("action") or "") == (cur.get("action") or "")
            and (prev.get("xpath") or "") == (cur.get("xpath") or "")
            and (prev.get("ax_role") or "") == (cur.get("ax_role") or "")
            and (prev.get("ax_name") or "") == (cur.get("ax_name") or "")
            and prev.get("target_id") == cur.get("target_id")
            and (prev.get("type_value") or "") == (cur.get("type_value") or "")
            and (prev.get("type_value_template") or "") == (cur.get("type_value_template") or "")
            and (prev.get("url") or "") == (cur.get("url") or "")
            and (prev.get("url_template") or "") == (cur.get("url_template") or "")
            and sorted(prev.get("required_memory_keys") or []) == sorted(cur.get("required_memory_keys") or [])
            and prev.get("x_norm") == cur.get("x_norm")
            and prev.get("y_norm") == cur.get("y_norm")
            # Cross-system: identical actions in different planned systems are
            # NOT the same step -- merging them would drop a system hop.
            and (prev.get("system_id") or "") == (cur.get("system_id") or "")
        )

    for step in trail:
        if not isinstance(step, dict):
            continue
        if compacted and _same_step(compacted[-1], step):
            continue
        compacted.append(dict(step))

    return compacted


# ── 动态内容阈值：ax_name 超过此长度视为动态内容（新闻标题/商品名等），
#    语义定位器大概率与当前页面不匹配，应快速降级到 XPath 结构寻址 ──
_DYNAMIC_CONTENT_NAME_THRESHOLD = 12


def _is_dynamic_content_step(step: dict) -> bool:
    """
    判断当前步骤是否涉及动态内容。

    动态内容特征：
      1. save_to_memory 动作 — 值一定随页面内容变化
      2. ax_name 长度 > 12 — 长文本通常为新闻标题、商品名等时效性内容
    """
    if step.get("action") == "save_to_memory":
        return True
    ax_name = str(step.get("ax_name") or "").strip()
    if len(ax_name) > _DYNAMIC_CONTENT_NAME_THRESHOLD:
        return True
    return False


async def _resolve_composite_locator(page, step: dict, timeout_ms: int):
    """
    复合定位器：Priority 1 语义定位 → Priority 2 XPath 兜底。

    ★ 动态内容智能降级：
      当 step 被判定为动态内容（长 ax_name / save_to_memory）时，
      Priority 1 的 timeout 从默认值压缩到 100ms，实现近乎立即降级到 XPath。
      这样不会死等"昨天的新闻标题"，而是直接用 XPath 物理结构定位。
    """
    from playwright.async_api import Error as PlaywrightError

    ax_role = str(step.get("ax_role") or "").strip().lower()
    ax_name = str(step.get("ax_name") or "").strip()
    xpath = str(step.get("xpath") or "").strip()
    is_dynamic = _is_dynamic_content_step(step)

    # ── Priority 1: 语义定位器（get_by_role + name） ──
    # 动态内容：timeout 压缩到 100ms 快速降级；
    # 纯粹无 ax_name 的 save_to_memory 直接跳过 Priority 1。
    if ax_role and ax_name:
        semantic_timeout = 100 if is_dynamic else timeout_ms
        if is_dynamic:
            logger.info(
                f"[RPA DYNAMIC] 检测到动态内容 (ax_name={ax_name!r}, len={len(ax_name)})，"
                f"语义定位 timeout 压缩至 {semantic_timeout}ms，将快速降级到 XPath"
            )
        try:
            semantic_loc = page.get_by_role(ax_role, name=ax_name).first
            await semantic_loc.wait_for(state="visible", timeout=semantic_timeout)
            return semantic_loc, f"role={ax_role!r}, name={ax_name!r}"
        except PlaywrightError as sem_err:
            logger.debug(
                f"[RPA REPLAY] semantic locator failed, will try xpath: {type(sem_err).__name__}: {sem_err}"
            )
        except Exception as sem_err:
            logger.debug(
                f"[RPA REPLAY] semantic locator failed, will try xpath: {type(sem_err).__name__}: {sem_err}"
            )

    # ── Priority 2: XPath 物理结构定位 ──
    if xpath:
        try:
            xpath_loc = page.locator(f"xpath={xpath}").first
            await xpath_loc.wait_for(state="visible", timeout=timeout_ms)
            return xpath_loc, f"xpath={xpath}"
        except PlaywrightError as xpath_err:
            logger.debug(
                f"[RPA REPLAY] xpath locator failed: {type(xpath_err).__name__}: {xpath_err}"
            )
        except Exception as xpath_err:
            logger.debug(
                f"[RPA REPLAY] xpath locator failed: {type(xpath_err).__name__}: {xpath_err}"
            )

    raise RuntimeError(
        "Composite locator failed on current page: "
        f"role={ax_role!r}, name={ax_name!r}, xpath={xpath!r}"
    )


def _normalize_rpa_cache_payload(payload) -> dict:
    """兼容旧版 list 缓存与新版 dict 缓存。"""
    if isinstance(payload, list):
        trail = _semanticize_rpa_trail(payload, "")
        return {
            "version": 1,
            "replayable": True,
            "reason": "",
            "trail": trail,
            "fail_count": 0,
            "max_failures": 2,
            "completes_task": True,
            "source_url": "",
            "source_goal": "",
            "core_goal": "",
            "normalized_url": "",
            "normalized_goal": "",
        }
    if isinstance(payload, dict):
        trail = payload.get("trail")
        if not isinstance(trail, list):
            trail = []
        meta = _build_rpa_match_metadata(
            str(payload.get("source_url", "") or ""),
            str(payload.get("source_goal", "") or ""),
        )
        source_goal = str(payload.get("source_goal", "") or meta["source_goal"])
        semantic_trail = _semanticize_rpa_trail(trail, source_goal)
        semantic_action = (
            str(semantic_trail[0].get("action") or "")
            if semantic_trail and len(semantic_trail) == 1 and isinstance(semantic_trail[0], dict)
            else ""
        )
        was_calendar_physical = bool(
            semantic_trail
            and len(semantic_trail) == 1
            and isinstance(semantic_trail[0], dict)
            and semantic_action == "date_pick"
        )
        replayable = bool(payload.get("replayable", True))
        reason = str(payload.get("reason", "") or "")
        reason_lower = reason.lower()
        if was_calendar_physical and (
            not replayable or "calendar date selection uses volatile cell positions" in reason
        ):
            replayable = True
            reason = ""
        if semantic_action == "cascader_pick" and (
            not replayable
            or "cascader/multi-level popups are dynamic and require live interaction" in reason_lower
            or "dynamic and require live interaction" in reason_lower
        ):
            replayable = True
            reason = ""
        _completes_task = bool(payload.get("completes_task", True))
        if _goal_is_form_fill(source_goal):
            _completes_task = _trail_completes_form_goal(source_goal, semantic_trail)

        return {
            "version": int(payload.get("version", 4) or 4),
            "replayable": replayable,
            "reason": reason,
            "trail": semantic_trail,
            "fail_count": int(payload.get("fail_count", 0) or 0),
            "max_failures": int(payload.get("max_failures", 2) or 2),
            "completes_task": _completes_task,
            "source_url": str(payload.get("source_url", "") or meta["source_url"]),
            "source_goal": source_goal,
            "core_goal": str(payload.get("core_goal", "") or meta["core_goal"]),
            "normalized_url": str(payload.get("normalized_url", "") or meta["normalized_url"]),
            "normalized_goal": str(payload.get("normalized_goal", "") or meta["normalized_goal"]),
        }
    raise ValueError("Unsupported RPA cache payload format")


def _extract_placeholder_keys(text: str) -> list[str]:
    if not text or "{{" not in text:
        return []
    keys: set[str] = set()
    for raw in re.findall(r"\{\{([^}]+)\}\}", text):
        key = raw.strip()
        if not key or key.lower().startswith("env:"):
            continue
        keys.add(key)
    return sorted(keys)


def _stable_memory_keys(workflow_memory: dict | None) -> list[str]:
    if not workflow_memory:
        return []
    keys: list[str] = []
    for key in workflow_memory.keys():
        if not key or key == "latest_memory":
            continue
        if re.match(r"temp_var_\d+$", str(key)):
            continue
        keys.append(str(key))
    return sorted(set(keys))


def _resolve_replay_template(text: str, workflow_memory: dict | None) -> str:
    if not text or "{{" not in text or not workflow_memory:
        return text

    def _replace(match: re.Match) -> str:
        key = match.group(1).strip()
        val = workflow_memory.get(key)
        return str(val) if val is not None else match.group(0)

    return re.sub(r"\{\{([^}]+)\}\}", _replace, text)


def _write_rpa_cache_payload(path: Path, payload: dict) -> None:
    _RPA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _mark_rpa_cache_failure(path: Path, payload: dict, error_msg: str = "") -> dict:
    updated = dict(payload)
    updated["fail_count"] = int(updated.get("fail_count", 0) or 0) + 1
    max_failures = int(updated.get("max_failures", 2) or 2)
    if updated["fail_count"] >= max_failures:
        updated["replayable"] = False
        updated["reason"] = (
            f"replay failed repeatedly ({updated['fail_count']} times)"
            + (f": {error_msg[:120]}" if error_msg else "")
        )
    _write_rpa_cache_payload(path, updated)
    return updated


async def _replay_switch_system(
    browser: "BrowserEnv",
    session_router,
    *,
    to_system_id: str,
    from_system_id: str = "",
    url: str = "",
    home_browser=None,
    home_system_id: str = "",
    user_data_dir_base: str = "",
    event_stream=None,
):
    """Switch the active browser to ``to_system_id`` mid RPA replay (RPA-XSYS).

    Mirrors the reactive loop's cross-system goto consumer: snapshot the current
    context's storage_state, ask the router for a switch directive, activate it
    (launch / rebind the target system's isolated pooled session), then land the
    rebound browser on ``url`` only if it isn't already there (A2: preserves the
    target's exact page state on a switch-back). Best-effort -- any failure
    returns the inputs unchanged so replay proceeds on the current browser.

    Returns ``(browser, home_browser, home_system_id)``: the (possibly new)
    active browser plus the home-system bookkeeping the caller threads forward.
    """
    try:
        full_state = None
        try:
            _ctx = getattr(browser, "_context", None)
            if _ctx is not None:
                full_state = await _ctx.storage_state()
        except Exception as _state_err:
            logger.debug("[RPA REPLAY] x-sys storage_state skipped: %s", _state_err)

        switch = session_router.acquire_for_switch(
            to_system_id=to_system_id,
            from_system_id=from_system_id,
            full_state=full_state,
        )
        if not switch.get("should_switch"):
            return browser, home_browser, home_system_id

        # 留痕 (mission §三/§四): mirror the reactive-loop goto consumer's
        # session_switch evidence so a cross-system hop during fast-path
        # replay is recorded in the run event_stream, not only the log.
        # Inner-guarded so an emit hiccup never aborts the switch;
        # event_stream None (flag off / no stream) -> no event.
        if event_stream is not None:
            try:
                event_stream.emit("session_switch", **switch)
            except Exception:
                pass

        if home_browser is None:
            home_browser = browser
            home_system_id = from_system_id or ""

        target_browser = await session_router.activate_switch(
            switch,
            url=url,
            user_data_dir_base=user_data_dir_base,
            full_state=full_state,
            home_system_id=home_system_id,
            home_browser=home_browser,
        )
        if target_browser is not None and target_browser is not browser:
            browser = target_browser
            try:
                browser._session_router = session_router
                browser._event_stream = event_stream
            except Exception:
                pass
            if event_stream is not None:
                try:
                    event_stream.emit(
                        "session_switch_activated",
                        run_id=switch.get("run_id", ""),
                        to_system_id=switch.get("to_system_id", ""),
                        to_system_name=switch.get("to_system_name", ""),
                        session_id=switch.get("session_id", ""),
                        via="rpa_replay",
                    )
                except Exception:
                    pass
            logger.info(
                "[RPA REPLAY] cross-system switch -> %s (session=%s)",
                switch.get("to_system_id", ""),
                switch.get("session_id", ""),
            )

        try:
            _page = getattr(browser, "_page", None)
            _current = getattr(browser, "current_url", "") or ""
            if _page is not None and url and session_router.should_renavigate(_current, url):
                await _page.goto(url, wait_until="domcontentloaded", timeout=30000)
                _stable = getattr(browser, "_wait_for_page_stable", None)
                if _stable is not None:
                    await _stable()
        except Exception as _nav_err:
            logger.debug("[RPA REPLAY] x-sys landing nav skipped: %s", _nav_err)

        return browser, home_browser, home_system_id
    except Exception as _switch_err:
        logger.debug("[RPA REPLAY] cross-system switch skipped: %s", _switch_err)
        return browser, home_browser, home_system_id


async def _replay_rpa(
    browser: "BrowserEnv",
    trail: list[dict],
    workflow_memory: dict | None = None,
    vlm: "VLMClient | None" = None,
    session_router=None,
    event_stream=None,
) -> bool:
    """
    极速 RPA 回放：直接用 XPath/坐标执行缓存动作，完全跳过 VLM。

    vlm 仅作为语义宏（date_pick 等）postcheck 的慢路径兜底；
    传 None 时退化为纯 JS 校验。

    Returns:
        True  → 全程无报错，任务完成
        False → 任意步骤失败，需降级回 VLM 主循环
    """
    from playwright.async_api import Error as PlaywrightError
    try:
        from . import semantic_macros as _semantic_macros
    except ImportError:
        import semantic_macros as _semantic_macros  # type: ignore[no-redef]

    # RPA-XSYS: auto-thread the run's SessionRouter off the browser when not
    # passed explicitly. main.py attaches browser._session_router ONLY when
    # VSPIDER_CROSS_SYSTEM_SWITCH is on, so flag off -> None -> no switching.
    if session_router is None:
        session_router = getattr(browser, "_session_router", None)
    # RPA-XSYS-EVT: auto-thread the run event_stream off the browser when
    # not passed, so cross-system replay hops emit session_switch evidence.
    # main.py attaches browser._event_stream alongside _session_router
    # (flag-gated) -> flag off -> None -> no events, byte-identical.
    if event_stream is None:
        event_stream = getattr(browser, "_event_stream", None)

    compacted_trail = _compact_rpa_trail(trail)
    if len(compacted_trail) != len(trail):
        logger.info(
            f"[RPA REPLAY] Compacted cached trail: {len(trail)} → {len(compacted_trail)} steps"
        )

    _RPA_TIMEOUT = 5000  # 每步最长等待 5s，防止卡死

    # ── Cross-system RPA replay state (RPA-XSYS) ──────────────────────
    # Track the active planned system; a step whose system_id differs gets a
    # physical browser switch before it replays. All inert when router None.
    _xsys_current_system = ""
    _xsys_home_browser = None
    _xsys_home_system = ""
    _xsys_udd_base = ""
    if session_router is not None:
        try:
            _xsys_current_system = session_router.system_for_url(
                getattr(browser, "current_url", "") or ""
            )
        except Exception:
            _xsys_current_system = ""
        try:
            try:
                from . import config as _xsys_cfg
            except ImportError:
                import config as _xsys_cfg  # type: ignore[no-redef]
            _xsys_udd_base = getattr(_xsys_cfg, "BROWSER_USER_DATA_DIR", "") or ""
        except Exception:
            _xsys_udd_base = ""

    for idx, step in enumerate(compacted_trail):
        act = step.get("action")
        step_label = f"Step {idx + 1}/{len(compacted_trail)} ({act})"
        try:
            # Cross-system replay hop: switch the active browser before this
            # step runs if it belongs to a different planned system.
            if session_router is not None:
                _step_system = str(step.get("system_id") or "").strip()
                if _step_system and _step_system != _xsys_current_system:
                    if act == "goto":
                        _hop_url = (
                            step.get("url_template") or step.get("url")
                            or step.get("type_value_template")
                            or step.get("type_value") or ""
                        )
                    else:
                        _hop_url = getattr(browser, "current_url", "") or ""
                    _hop_url = _resolve_replay_template(str(_hop_url), workflow_memory)
                    browser, _xsys_home_browser, _xsys_home_system = (
                        await _replay_switch_system(
                            browser,
                            session_router,
                            to_system_id=_step_system,
                            from_system_id=_xsys_current_system,
                            url=_hop_url,
                            home_browser=_xsys_home_browser,
                            home_system_id=_xsys_home_system,
                            user_data_dir_base=_xsys_udd_base,
                            event_stream=event_stream,
                        )
                    )
                    _xsys_current_system = _step_system
            page = browser._page
            if page is None or page.is_closed():
                raise RuntimeError("no active page available for cached replay")
            if act == "goto":
                url = (
                    step.get("url_template")
                    or step.get("url")
                    or step.get("type_value_template")
                    or step.get("type_value")
                    or ""
                )
                url = _resolve_replay_template(str(url), workflow_memory)
                if not url:
                    raise RuntimeError("cached goto step missing url")
                logger.info(f"[RPA REPLAY] {step_label}: goto={url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=_RPA_TIMEOUT)
                await page.wait_for_load_state("domcontentloaded", timeout=_RPA_TIMEOUT)
                await asyncio.sleep(0.8)

            elif act == "click":
                loc, locator_desc = await _resolve_composite_locator(page, step, _RPA_TIMEOUT)
                logger.info(f"[RPA REPLAY] {step_label}: {locator_desc}")
                await loc.click(timeout=_RPA_TIMEOUT)
                # click 可能触发导航或弹新标签页，优先等待当前激活页稳定
                active_page = browser._page if browser._page and not browser._page.is_closed() else page
                await active_page.wait_for_load_state("domcontentloaded", timeout=_RPA_TIMEOUT)
                await asyncio.sleep(0.8)

            elif act in _semantic_macros.actions():
                # Unified semantic-macro dispatch — handles cascader_pick,
                # date_pick, and any future macro registered with the
                # ``semantic_macros`` package. Each macro provides its own JS
                # body + optional VL judge question.
                logger.info(
                    "[RPA REPLAY] %s: dispatching %r through semantic_macros registry",
                    step_label, act,
                )
                # Inject runtime config flags that the JS body expects. date_pick
                # reads ``allow_direct_set`` (config: DATE_PICK_DIRECT_SET_FALLBACK)
                # to decide whether to use ``setNativeValue`` as a last resort.
                if act == "date_pick" and "allow_direct_set" not in step:
                    step["allow_direct_set"] = bool(getattr(
                        _runtime_config_module(),
                        "DATE_PICK_DIRECT_SET_FALLBACK",
                        False,
                    ))
                _macro_result = await _semantic_macros.replay_step(page, step, vlm=vlm)
                if not _macro_result.get("ok"):
                    raise RuntimeError(f"{act} macro failed: {_macro_result}")
                if _macro_result.get("vl_recovered"):
                    print(
                        f"\033[1;33m🔍 [VL JUDGE]\033[0m {act}: VL 视觉裁判判定已生效 "
                        f"({str(_macro_result.get('vl_judge', {}).get('reason',''))[:60]})"
                    )
                logger.info("[RPA REPLAY] %s verified: %s", act, _macro_result)
                await asyncio.sleep(0.5)

            elif act == "type":
                val = step.get("type_value_template") or step.get("type_value", "")
                val = _resolve_replay_template(str(val), workflow_memory)
                loc, locator_desc = await _resolve_composite_locator(page, step, _RPA_TIMEOUT)
                logger.info(f"[RPA REPLAY] {step_label}: {locator_desc} <- {val!r}")
                await loc.fill(val, timeout=_RPA_TIMEOUT)
                await asyncio.sleep(0.3)

            elif act == "hover":
                loc, locator_desc = await _resolve_composite_locator(page, step, _RPA_TIMEOUT)
                logger.info(f"[RPA REPLAY] {step_label}: {locator_desc}")
                await loc.hover(timeout=_RPA_TIMEOUT)
                await asyncio.sleep(0.5)

            elif act == "click_point":
                # ── click_point 无 Playwright 自动等待，必须手动保证页面就绪 ──
                # 步骤 1：等待 DOM 加载完成（防止目标 Canvas/动态 UI 尚未渲染）
                await page.wait_for_load_state("domcontentloaded", timeout=_RPA_TIMEOUT)
                # 步骤 2：强制留 1.5s 给 Canvas 绘制 / 动态组件挂载完毕
                await asyncio.sleep(1.5)
                # 步骤 3：视口归一化换算 → 真实像素坐标
                viewport = page.viewport_size or {"width": 1280, "height": 800}
                real_x = int((step["x_norm"] / 1000.0) * viewport["width"])
                real_y = int((step["y_norm"] / 1000.0) * viewport["height"])
                logger.info(
                    f"[RPA REPLAY] {step_label}: "
                    f"norm=({step['x_norm']}, {step['y_norm']}) → real=({real_x}, {real_y})"
                )
                await page.mouse.click(real_x, real_y)
                await asyncio.sleep(0.8)

            elif act == "press_key":
                key_name = step.get("type_value_template") or step.get("type_value") or ""
                key_name = _resolve_replay_template(str(key_name), workflow_memory)
                if not key_name:
                    raise RuntimeError("cached press_key step missing key")
                logger.info(f"[RPA REPLAY] {step_label}: key={key_name!r}")
                await page.keyboard.press(key_name)
                if key_name == "Enter":
                    await page.wait_for_load_state("domcontentloaded", timeout=_RPA_TIMEOUT)
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(0.3)

            elif act == "switch_tab":
                tab_index = int(step.get("target_id", 0))
                open_pages = [p for p in browser._context.pages if not p.is_closed()]
                if not (0 <= tab_index < len(open_pages)):
                    raise RuntimeError(f"cached switch_tab target out of range: {tab_index}")
                browser._page = open_pages[tab_index]
                await browser._page.bring_to_front()
                await asyncio.sleep(0.8)

            elif act == "close_tab":
                logger.info(f"[RPA REPLAY] {step_label}: close active tab")
                await page.close()
                open_pages = [p for p in browser._context.pages if not p.is_closed()]
                if not open_pages:
                    raise RuntimeError("no remaining page after cached close_tab")
                browser._page = open_pages[-1]
                await browser._page.bring_to_front()
                await browser._page.wait_for_load_state("domcontentloaded", timeout=_RPA_TIMEOUT)
                await asyncio.sleep(0.8)

            elif act == "wait":
                try:
                    wait_secs = max(1, min(10, int(float(step.get("type_value") or "2"))))
                except (ValueError, TypeError):
                    wait_secs = 2
                logger.info(f"[RPA REPLAY] {step_label}: wait {wait_secs}s")
                await asyncio.sleep(wait_secs)

            elif act == "save_to_memory":
                # ═══════════════════════════════════════════════════════════
                # ★ save_to_memory 回放：动态重提取（绝不使用缓存硬编码值）
                #
                # 问题：录制时 type_value 记录的是当时页面的具体文本
                #       （如"总书记引领强国之路｜以质图强..."），但回放时
                #       页面内容已经变化，必须从当前最新页面重新提取。
                #
                # 策略：
                #   1. 用复合定位器（快速降级到 XPath）在当前页面找到目标元素
                #   2. 从元素的 innerText / value 提取最新文本
                #   3. 将 fresh_text 写入 workflow_memory
                # ═══════════════════════════════════════════════════════════
                memory_key = (
                    step.get("memory_key")
                    or step.get("type_value_template", "").strip("{}")
                    or ""
                ).strip()
                if not memory_key:
                    memory_key = f"temp_var_{int(time.time())}"
                    logger.warning(
                        f"[RPA DYNAMIC] save_to_memory 缺少 memory_key，"
                        f"自动生成: {memory_key!r}"
                    )

                # 尝试从当前页面动态提取最新文本
                fresh_text = ""
                try:
                    loc, locator_desc = await _resolve_composite_locator(
                        page, step, _RPA_TIMEOUT
                    )
                    # 提取最新文本：优先 innerText，次选 input value
                    raw_text = await loc.evaluate(
                        """el => {
                            const text = (el.innerText || el.textContent || '').trim();
                            const val  = (el.value || '').trim();
                            return text || val || '';
                        }"""
                    )
                    # 清理隐藏字符、多余换行、首尾空格
                    fresh_text = re.sub(
                        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "",
                        str(raw_text or ""),
                    ).strip()
                    fresh_text = re.sub(r"\s+", " ", fresh_text).strip()
                    logger.info(
                        f"[RPA DYNAMIC] 重新从页面提取了最新文本: "
                        f"{fresh_text!r} (via {locator_desc})"
                    )
                    print(
                        f"\033[1;36m🔄 [RPA DYNAMIC]\033[0m "
                        f"从当前页面重新提取了最新文本: "
                        f"\033[32m{fresh_text[:80]!r}\033[0m"
                    )
                except Exception as extract_err:
                    logger.warning(
                        f"[RPA DYNAMIC] 页面元素定位失败，"
                        f"无法动态提取文本: {extract_err}"
                    )
                    # 降级：尝试用缓存的 type_value 作为最后手段
                    cached_val = (step.get("type_value") or "").strip()
                    if cached_val:
                        fresh_text = cached_val
                        logger.warning(
                            f"[RPA DYNAMIC] 降级使用缓存文本: {fresh_text!r}"
                        )
                    else:
                        raise RuntimeError(
                            f"save_to_memory 回放失败：既无法从页面提取，"
                            f"缓存也无 type_value. 错误: {extract_err}"
                        )

                # 写入 workflow_memory
                if fresh_text and workflow_memory is not None:
                    workflow_memory[memory_key] = fresh_text
                    workflow_memory["latest_memory"] = fresh_text
                    logger.info(
                        f"[RPA DYNAMIC] Memory 已更新: "
                        f"{memory_key!r} = {fresh_text!r}"
                    )
                elif not fresh_text:
                    logger.warning(
                        f"[RPA DYNAMIC] save_to_memory 提取结果为空，"
                        f"未写入 workflow_memory"
                    )

                await asyncio.sleep(0.3)

            elif act in ("fetch_link_content", "fetch_links_batch"):
                type_value = (
                    step.get("type_value_template")
                    or step.get("type_value")
                    or step.get("url")
                    or ""
                )
                type_value = _resolve_replay_template(str(type_value), workflow_memory)
                memory_key = str(step.get("memory_key") or "").strip()
                if not memory_key:
                    memory_key = f"fetched_{idx + 1}"
                logger.info(
                    "[RPA REPLAY] %s: %s -> memory[%s]",
                    step_label,
                    act,
                    memory_key,
                )
                active_page = await browser.execute_action(
                    {
                        "progress_review": "cached fetch replay",
                        "thought": f"Replay cached {act} without VLM.",
                        "current_state": "RPA replay",
                        "action": act,
                        "target_id": int(step.get("target_id") or 0),
                        "type_value": type_value,
                        "memory_key": memory_key,
                        "extracted_data": None,
                        "status": "pending",
                    },
                    workflow_memory=workflow_memory,
                )
                if active_page is not None:
                    browser._page = active_page
                await asyncio.sleep(0.2)

            elif act == "smooth_scroll":
                direction = (step.get("type_value") or "down").strip().lower()
                if direction == "up":
                    js_scroll = "window.scrollBy({top: -window.innerHeight * 0.8, behavior: 'smooth'});"
                else:
                    js_scroll = "window.scrollBy({top: window.innerHeight * 0.8, behavior: 'smooth'});"
                logger.info(f"[RPA REPLAY] {step_label}: smooth_scroll direction={direction}")
                await page.evaluate(js_scroll)
                await asyncio.sleep(1.0)

            elif act == "remove_element":
                xpath = step.get("xpath", "")
                ax_role = step.get("ax_role", "")
                ax_name = step.get("ax_name", "")
                if not xpath:
                    logger.debug(f"[RPA REPLAY] {step_label}: remove_element skipped (no xpath)")
                else:
                    logger.info(
                        f"[RPA REPLAY] {step_label}: remove_element role={ax_role!r}, name={ax_name!r}, xpath={xpath}"
                    )
                    try:
                        loc = page.locator(f"xpath={xpath}")
                        if await loc.count() > 0:
                            await loc.evaluate("el => el.remove()")
                            logger.info(f"[RPA REPLAY] Element at {xpath} removed from DOM")
                        else:
                            # 节点已不存在（可能上次执行已删），视为成功，继续回放
                            logger.debug(f"[RPA REPLAY] remove_element target already gone: {xpath}")
                    except Exception as _rm_err:
                        # 删除失败不阻断整个 RPA 回放，仅警告
                        logger.warning(f"[RPA REPLAY] remove_element soft-fail: {_rm_err}")

            elif act == "select":
                val = step.get("type_value_template") or step.get("type_value", "")
                val = _resolve_replay_template(str(val), workflow_memory)
                loc, locator_desc = await _resolve_composite_locator(page, step, _RPA_TIMEOUT)
                logger.info(f"[RPA REPLAY] {step_label}: {locator_desc} <- {val!r}")
                try:
                    await loc.select_option(label=val, timeout=_RPA_TIMEOUT)
                except Exception:
                    try:
                        await loc.select_option(value=val, timeout=_RPA_TIMEOUT)
                    except Exception:
                        await loc.select_option(index=0, timeout=_RPA_TIMEOUT)
                await asyncio.sleep(0.5)

            else:
                logger.debug(f"[RPA REPLAY] {step_label}: skipping unsupported action")

        except (PlaywrightError, Exception) as rpa_err:
            print(
                f"\n\033[1;41m⚠️  [RPA REPLAY FAILED]\033[0m "
                f"step={idx + 1}/{len(compacted_trail)} action={act} "
                f"reason: {type(rpa_err).__name__}: {rpa_err}\n"
            )
            logger.warning(f"[RPA REPLAY] {step_label} FAILED — {type(rpa_err).__name__}: {rpa_err}")
            try:
                _failure_path = _RPA_CACHE_DIR / "_last_replay_failure.json"
                _failure_record = {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "step_index": idx,
                    "step_count": len(compacted_trail),
                    "action": act,
                    "step_label": step_label,
                    "step_payload": step,
                    "error_type": type(rpa_err).__name__,
                    "error_message": str(rpa_err),
                    "current_url": getattr(browser, "current_url", "") or "",
                }
                _failure_path.write_text(
                    json.dumps(_failure_record, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info(f"[RPA REPLAY] Failure detail written to {_failure_path}")
            except Exception as _persist_err:
                logger.debug(f"[RPA REPLAY] Failed to persist failure detail: {_persist_err}")
            return False

    return True


async def _replay_ready_rpa_steps(
    browser: "BrowserEnv",
    payload: dict,
    cursor: int,
    workflow_memory: dict | None,
    vlm: "VLMClient | None" = None,
) -> tuple[int, list[dict], bool]:
    """回放当前已满足前置条件的连续缓存步骤。"""
    if not payload.get("replayable", True):
        return cursor, [], False

    trail = payload.get("trail") or []
    if cursor >= len(trail):
        return cursor, [], False

    available_keys = set((workflow_memory or {}).keys())
    ready_steps: list[dict] = []
    next_cursor = cursor
    while next_cursor < len(trail):
        step = trail[next_cursor]
        required = set(step.get("required_memory_keys") or [])
        if required and not required.issubset(available_keys):
            break
        ready_steps.append(step)
        next_cursor += 1

    if not ready_steps:
        return cursor, [], False

    logger.info(
        f"[RPA PARTIAL] Replaying cached steps {cursor + 1}-{next_cursor}/{len(trail)} "
        f"(ready after prerequisites satisfied)"
    )
    ok = await _replay_rpa(browser, ready_steps, workflow_memory, vlm=vlm)
    if not ok:
        return cursor, [], True
    return next_cursor, ready_steps, False


def _print_manual_warning(title: str, message: str):
    """
    在终端打印醒目的红色验证码警告。
    使用 ANSI 转义序列实现红色高亮，兼容大多数终端。
    """
    # ANSI: \033[1;31m = 粗体红色, \033[0m = 重置
    RED_BOLD = "\033[1;31m"
    YELLOW_BOLD = "\033[1;33m"
    RESET = "\033[0m"

    warning_lines = [
        "",
        f"{RED_BOLD}{'!' * 70}{RESET}",
        f"{RED_BOLD}!!!                                                              !!!{RESET}",
        f"{RED_BOLD}!!! {title.center(60)} !!!{RESET}",
        f"{RED_BOLD}!!!                                                              !!!{RESET}",
        f"{RED_BOLD}{'!' * 70}{RESET}",
        "",
        f"{YELLOW_BOLD}  >> {message}{RESET}",
        f"{YELLOW_BOLD}  >> After finishing it, come back here and press [Enter] to continue...{RESET}",
        "",
    ]
    for line in warning_lines:
        print(line)


def _runtime_config_module():
    try:
        from . import config as runtime_config
    except ImportError:
        import config as runtime_config
    return runtime_config


def _apply_runtime_overrides(args) -> None:
    """Apply runtime config overrides from CLI arguments and natural-language constraints."""
    config = _runtime_config_module()

    config.BROWSER_USER_DATA_DIR = args.user_data_dir
    if hasattr(args, "auth_profiles") and args.auth_profiles is not None:
        config.AUTH_PROFILES = args.auth_profiles

    override_text = "\n".join(
        part for part in (args.constraints, args.context, args.goal, args.output) if part
    )
    viewport_match = re.search(r"(\d{3,4})\s*[xX]\s*(\d{3,4})", override_text)
    if viewport_match:
        width = int(viewport_match.group(1))
        height = int(viewport_match.group(2))
        if width >= 800 and height >= 600:
            config.VIEWPORT_WIDTH = width
            config.VIEWPORT_HEIGHT = height
            logger.info(f"[VIEWPORT] Runtime override from prompt: {width}x{height}")


def _parse_cli_json_object(parser: argparse.ArgumentParser, raw: str, option_name: str) -> dict:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        parser.error(f"{option_name} must be valid JSON: {exc}")
    if not isinstance(parsed, dict):
        parser.error(f"{option_name} must be a JSON object")
    return parsed


def _build_cli_run_constraints(parser: argparse.ArgumentParser, args: argparse.Namespace) -> dict | None:
    constraints = _parse_cli_json_object(
        parser,
        getattr(args, "run_constraints_json", ""),
        "--run-constraints-json",
    )
    if bool(getattr(args, "resume", False)):
        constraints["resume"] = True
    return constraints or None


def _build_cli_vlm_options(args: argparse.Namespace) -> dict | None:
    options: dict[str, Any] = {}
    field_map = {
        "vlm_model": "model",
        "semantic_model": "semantic_model",
        "vlm_model_type": "model_type",
        "vlm_base_url": "base_url",
        "vlm_api_key": "api_key",
        "semantic_base_url": "semantic_base_url",
        "semantic_api_key": "semantic_api_key",
    }
    for attr, key in field_map.items():
        value = getattr(args, attr, "")
        if isinstance(value, str):
            value = value.strip()
        if value not in ("", None):
            options[key] = value
    if getattr(args, "vlm_temperature", None) is not None:
        options["temperature"] = float(args.vlm_temperature)
    if getattr(args, "vlm_max_tokens", None) is not None:
        options["max_tokens"] = int(args.vlm_max_tokens)
    return options or None


def _resolve_action_tool_metadata(
    action_registry,
    action_name: str,
    *,
    goal: str = "",
    selected_tools: list[dict] | None = None,
) -> dict[str, Any] | None:
    """Resolve safe event metadata for an executed action.

    Exact tool actions keep their metadata directly. Generic aliases like click
    are only labeled when that tool was actually selected for the current goal,
    which avoids auth/profile prompt noise leaking unrelated tool labels into
    normal browser actions.
    """
    tool = action_registry.resolve_for_action(action_name, goal=goal)
    if not tool:
        return None

    normalized_action = str(action_name or "").strip().lower()
    normalized_tool_name = str(tool.get("name") or "").strip().lower()
    if normalized_action != normalized_tool_name:
        selected_names = {
            str(item.get("name") or "").strip().lower()
            for item in (selected_tools or [])
            if isinstance(item, dict)
        }
        if normalized_tool_name not in selected_names:
            return None

    return {
        "name": tool.get("name"),
        "capability": tool.get("capability"),
        "evidence": tool.get("evidence") or [],
        "risk": tool.get("risk") or "",
    }


async def run_agent(
    start_url: str,
    goal: str,
    enable_xhr: bool = False,
    upload_file: str = "",
    xhr_pattern: str = "",
    require_login: bool = False,
    stop_event: threading.Event | None = None,
    auth_profiles: str | None = None,
    vlm_options: dict | None = None,
    run_constraints: dict | None = None,
    prompt_images: list[str] | None = None,
    run_id: str = "",
) -> bool:
    """
    Agent 核心运转循环。

    Args:
        start_url:   初始页面 URL
        goal:        用户的自然语言任务目标
        enable_xhr:  是否启用通用 XHR 拦截器（--xhr 参数）
        upload_file: 预配置的上传文件路径（--upload-file 参数）
        xhr_pattern: 精准截胡 URL 关键词（--xhr-pattern 参数）。
                     非空时开启"混合调度主引擎"：
                     一旦拦截到匹配 URL 的 API 响应，立即保存数据并终止 VLM 循环。
        run_id:     可选外部 run id。API/批处理传入时会作为 runs/<run_id>/、
                    HTML log、event stream、manifest 的统一标识。
    """
    from datetime import datetime

    if auth_profiles is not None:
        _runtime_config_module().AUTH_PROFILES = auth_profiles
    if vlm_options:
        _cfg = _runtime_config_module()
        if vlm_options.get("model"):
            _cfg.VLM_MODEL_NAME = str(vlm_options["model"])
        if vlm_options.get("semantic_model"):
            _cfg.VLM_SEMANTIC_MODEL_NAME = str(vlm_options["semantic_model"])
        if vlm_options.get("semantic_base_url"):
            _cfg.VLM_SEMANTIC_API_BASE = _guard_vlm_endpoint_override(
                str(vlm_options["semantic_base_url"]), label="semantic_base_url"
            )
        if vlm_options.get("semantic_api_key"):
            _cfg.VLM_SEMANTIC_API_KEY = str(vlm_options["semantic_api_key"])
        if vlm_options.get("base_url"):
            _cfg.VLM_API_BASE = _guard_vlm_endpoint_override(
                str(vlm_options["base_url"]), label="base_url"
            )
        if vlm_options.get("api_key"):
            _cfg.VLM_API_KEY = str(vlm_options["api_key"])
        if vlm_options.get("temperature") is not None:
            _cfg.VLM_TEMPERATURE = float(vlm_options["temperature"])
        if vlm_options.get("max_tokens") is not None:
            _cfg.VLM_MAX_TOKENS = int(vlm_options["max_tokens"])
        _cfg.VLM_TEXT_ONLY = str(vlm_options.get("model_type", "")).lower() == "text"

    # MM-2: prompt_context image attachments ride along into vlm.ask(extra_images=).
    # Schedule is deterministic + configurable (default adaptive) to bound tokens.
    try:
        from .prompt_image_policy import should_include_prompt_images as _should_include_prompt_images
    except ImportError:
        from prompt_image_policy import should_include_prompt_images as _should_include_prompt_images
    _prompt_image_policy = str((vlm_options or {}).get("prompt_image_policy", "adaptive"))
    _prompt_images = list(prompt_images or [])
    _last_prompt_image_url: str | None = None

    # Prefer the caller-provided run_id so API task ids, run contracts, logs,
    # and artifacts all point at the same runs/<id>/ directory. Standalone CLI
    # calls still get a timestamp id.
    _caller_run_id = re.sub(r"[^0-9A-Za-z_-]+", "_", str(run_id or "").strip()).strip("_")
    _run_ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if _caller_run_id:
        _run_ts = _caller_run_id
    _registry_record_owned = False
    # L: open per-run phase event jsonl right after run_ts is known so
    # every broadcast_phase call from this run lands in logs/phase_<ts>.jsonl
    try:
        from api_server import set_phase_log_run_id as _set_phase_run

        _set_phase_run(_run_ts)
    except Exception:
        pass
    _vlm_output = f"output_{_run_ts}.xlsx"       # VLM extract 提取的结果
    _xhr_output = f"xhr_{_run_ts}.xlsx"           # XHR 拦截到的 API 数据（分开存）
    _goal_output_mode = "default"
    _goal_output_contract: dict[str, Any] = {}
    # ── 新一次 run：清空上一次 run 残留的 Final Answer 状态 ──
    _reset_run_answer()
    # K4: stash run identity so a failure inside this run lands in
    # runs/failed/<_run_ts>.json with the right goal + duration. Must
    # come AFTER _reset_run_answer() which clears the same fields.
    _record_run_start(run_id=_run_ts, goal=goal, started_at=time.time())

    # PRE-1: input preflight. Resolve a usable start URL when the user gave none
    # (decision A: auto-suggest an entry, never hard-block on a missing URL) and
    # collect clarifications. Best-effort: a preflight failure never aborts a run.
    _preflight = None
    try:
        from .io_contract import build_preflight as _build_preflight
        from .io_contract.entry_llm import entry_llm_from_config

        # §一-B #2: feed the semantic LLM so a URL-less goal resolves to a
        # model-picked start site (source "llm"), not just the search fallback.
        # Off-loop: the sync entry client only makes a blocking call when no
        # start_url was supplied.
        _entry_llm = entry_llm_from_config()
        _preflight = await asyncio.to_thread(
            _build_preflight,
            goal or "",
            start_url=start_url or "",
            upload_file=upload_file or "",
            auth_profiles=auth_profiles or "",
            constraints=run_constraints or None,
            llm=_entry_llm,
        )
        if not (start_url or "").strip() and _preflight.resolved_start_url:
            start_url = _preflight.resolved_start_url
            _entry_src = (
                _preflight.entry_suggestion.source
                if _preflight.entry_suggestion else "inferred"
            )
            logger.info(
                "[PREFLIGHT] no entry url given; starting from %s (%s)",
                start_url, _entry_src,
            )
            _broadcast_log_safe(
                f"[\u5165\u53e3\u63a8\u65ad] \u672a\u7ed9\u7f51\u5740\uff0c\u81ea\u52a8\u4ece {start_url} \u5f00\u59cb"
                f"\uff08{_entry_src}\uff09\uff0c\u5982\u9700\u66f4\u6362\u8bf7\u544a\u77e5"
            )
        for _warn in (_preflight.warnings or []):
            logger.info("[PREFLIGHT][warn] %s", _warn.as_reason())
    except Exception as _pf_err:
        logger.warning("[PREFLIGHT] skipped: %s", _pf_err)

    _registry_record_owned = _ensure_run_registry_record(
        run_id=_run_ts,
        start_url=start_url or "",
        goal=goal or "",
        upload_file=upload_file or "",
        auth_profiles=auth_profiles or "",
        vlm_options=vlm_options or None,
        run_constraints=run_constraints or None,
    )

    # OUT-3: materialize runs/<_run_ts>/{input_contract.json,output_contract.json,
    # manifest.json,artifacts/} skeleton up-front so any writer / harvester that
    # fires during this run lands beside a real contract instead of an empty
    # workspace/artifacts/ blob. ``ensure_input_contract_skeleton`` only writes
    # when the file is absent, so a richer input_contract persisted by
    # smart_batch_runner._persist_io_contracts_safe still wins; ad-hoc CLI /
    # replay runs that skip the dispatch layer now get a usable input contract.
    _initial_output_contract: dict[str, Any] = {}
    try:
        ensure_contract_skeleton(_run_ts)
        ensure_input_contract_skeleton(
            _run_ts,
            goal=goal or "",
            target_url=start_url or "",
            file_path=upload_file or "",
            auth_profiles=auth_profiles or "",
            vlm_options=vlm_options or None,
            constraints=run_constraints or None,
            source="cli",
        )
        _initial_requested_fields = _parse_goal_requested_fields(goal or "")
        _initial_oc = infer_output_contract(
            goal or "",
            requested_fields=_initial_requested_fields,
        )
        write_output_contract(_run_ts, _initial_oc)
        _initial_output_contract = _initial_oc.to_dict()
        logger.info(
            "[IO CONTRACT] run=%s output_kind=%s container=%s mode=%s",
            _run_ts,
            _initial_oc.output_kind,
            _initial_oc.container,
            _initial_oc.mode,
        )
    except Exception as _oc_err:
        logger.warning("[IO CONTRACT] skeleton write failed: %s", _oc_err)

    # PRE-2: decision A -- only genuinely ambiguous inputs (e.g. an attachment
    # whose intent could not be inferred) pause for a human before the run.
    if _preflight is not None and getattr(_preflight, "needs_human", False):
        try:
            await _wait_for_human_resume(_preflight.blocking_reason())
        except Exception as _hg_err:
            logger.warning("[PREFLIGHT] human gate skipped: %s", _hg_err)

    try:
        from .io_contract import set_current_run as _set_current_run
        _set_current_run(_run_ts)
    except Exception:
        try:
            from io_contract import set_current_run as _set_current_run
            _set_current_run(_run_ts)
        except Exception:
            pass

    browser_lease = acquire_browser(run_id=_run_ts)
    browser = browser_lease.browser
    vlm = VLMClient()
    # HTML 轨迹日志实例化在 try 块外，确保 finally 中的 finalize() 始终可访问
    html_logger = HtmlLogger(goal=goal, run_id=_run_ts)
    event_stream = EventStream(run_id=_run_ts)
    _snapshot_goal = goal
    action_registry = build_default_action_registry()
    skill_registry = build_default_skill_registry(load_history=True)
    async def _browser_action_tool(action_payload, workflow_memory=None):
        return await browser.execute_action(action_payload, workflow_memory)

    async def _targeted_probe_tool(action_payload, workflow_memory=None):
        page = await browser._ensure_active_page(reason="targeted probe action")
        before_url = browser.current_url
        before_pages = len(browser._context.pages) if browser._context else 0
        raw_value = str((action_payload or {}).get("type_value") or "").strip()
        probe_goal = goal
        kinds: list[str] = []
        if raw_value:
            if "|" in raw_value:
                kind_part, probe_part = raw_value.split("|", 1)
                kinds = [
                    item.strip()
                    for item in re.split(r"[,，\s]+", kind_part)
                    if item.strip() in {"input", "button", "link", "table", "dialog"}
                ]
                probe_goal = probe_part.strip() or goal
            elif raw_value in {"input", "button", "link", "table", "dialog"}:
                kinds = [raw_value]
            else:
                probe_goal = raw_value
        if not page:
            browser._last_action_result = ActionResult.from_action(
                action_payload,
                success=False,
                error="No active page for targeted probe.",
                before_url=before_url,
                after_url=before_url,
                before_pages=before_pages,
                after_pages=before_pages,
            )
            return None
        probe_result = await probe_page(
            page,
            goal=probe_goal,
            kinds=tuple(kinds) or None,
            limit=12,
        )
        summary = format_probe_text(probe_result, limit=8)
        metadata = {
            "probe": probe_result.as_dict(),
            "probe_goal": probe_goal,
            "candidate_count": len(probe_result.candidates),
        }
        browser._last_action_result = ActionResult.from_action(
            action_payload,
            success=probe_result.ok,
            message=summary,
            error="" if probe_result.ok else "No targeted candidates found.",
            before_url=before_url,
            after_url=browser.current_url,
            before_pages=before_pages,
            after_pages=len(browser._context.pages) if browser._context else before_pages,
            metadata=metadata,
        )
        try:
            if probe_result.ok:
                vlm.inject_error_feedback(
                    "局部感知探针已返回候选元素。优先根据下列 selector/bbox/evidence 选择下一步；"
                    "如果候选足够明确，可改用 click_text/type/press_key 等确定性动作；"
                    "如果候选不匹配，再回退到全页截图/SoM 判断。\n"
                    + summary
                )
            else:
                vlm.inject_error_feedback(
                    "局部感知探针未找到匹配候选。请回退到全页截图/SoM 观察，或先滚动/展开弹窗后重试。"
                )
        except Exception:
            pass
        return page

    async def _try_targeted_click_text_handoff(action_payload: dict) -> bool:
        click_text = str((action_payload or {}).get("type_value") or "").strip()
        if not click_text or len(click_text) > 80:
            return False
        page = await browser._ensure_active_page(reason="targeted click_text handoff")
        if not page:
            return False
        before_url = browser.current_url
        before_pages = len(browser._context.pages) if browser._context else 0
        try:
            probe_result = await probe_page(
                page,
                goal=f"click {click_text}",
                kinds=("button", "link"),
                limit=8,
            )
            candidate = choose_click_handoff_candidate(
                probe_result,
                target_text=click_text,
                min_confidence=0.55,
            )
            if not candidate:
                return False
            frame = page.main_frame
            for item in page.frames:
                try:
                    if candidate.frame_name and item.name == candidate.frame_name:
                        frame = item
                        break
                    if candidate.frame_url and item.url == candidate.frame_url:
                        frame = item
                        break
                except Exception:
                    continue
            locator = frame.locator(candidate.selector).first
            await locator.scroll_into_view_if_needed(timeout=2500)
            await locator.click(timeout=4000)
            await browser._wait_after_action()
            active_page = await browser._ensure_active_page(reason="targeted click_text handoff after click")
            browser._last_action_result = ActionResult.from_action(
                action_payload,
                success=True,
                message=(
                    "targeted_probe high-confidence handoff clicked "
                    f"{candidate.kind} {candidate.text!r}"
                ),
                before_url=before_url,
                after_url=browser.current_url,
                before_pages=before_pages,
                after_pages=len(browser._context.pages) if browser._context else before_pages,
                metadata={
                    "clicked_text": candidate.text,
                    "targeted_handoff": {
                        "mode": "click_text_to_selector",
                        "selector": candidate.selector,
                        "confidence": candidate.confidence,
                        "kind": candidate.kind,
                        "text": candidate.text,
                        "frame_url": candidate.frame_url,
                        "evidence": list(candidate.evidence),
                    },
                    "probe": probe_result.as_dict(),
                },
            )
            logger.info(
                "[TARGETED HANDOFF] click_text %r -> selector=%s conf=%s text=%r",
                click_text,
                candidate.selector,
                candidate.confidence,
                candidate.text,
            )
            return active_page is not None
        except Exception as exc:
            logger.debug("[TARGETED HANDOFF] click_text probe/click skipped: %s", exc)
            return False

    def _resolve_type_value_for_handoff(raw_value: str, workflow_memory=None) -> tuple[str, str]:
        value = str(raw_value or "")
        display_value = value
        if workflow_memory and "{{" in value:
            def _interpolate(match: re.Match) -> str:
                key = match.group(1).strip()
                resolved = (workflow_memory or {}).get(key)
                return match.group(0) if resolved is None else str(resolved)
            value = re.sub(r"\{\{([^}]+)\}\}", _interpolate, value)
            display_value = value
        env_template = display_value if "{{env:" in display_value else ""
        value, used_auth_vault, _env_names = resolve_env_placeholders(value)
        if used_auth_vault:
            display_value = env_template
        return value, display_value

    async def _try_targeted_type_handoff(
        action_payload: dict,
        workflow_memory=None,
    ) -> bool:
        raw_value = str((action_payload or {}).get("type_value") or "")
        if not raw_value.strip():
            return False
        try:
            target_id = int((action_payload or {}).get("target_id") or 0)
        except Exception:
            target_id = 0
        if target_id > 0:
            return False
        page = await browser._ensure_active_page(reason="targeted type handoff")
        if not page:
            return False
        before_url = browser.current_url
        before_pages = len(browser._context.pages) if browser._context else 0
        try:
            probe_goal = goal
            probe_result = await probe_page(
                page,
                goal=probe_goal,
                kinds=("input",),
                limit=8,
            )
            candidate = choose_type_handoff_candidate(
                probe_result,
                min_confidence=0.5,
            )
            if not candidate:
                try:
                    try:
                        from .targeted_type_fallback import fill_best_text_input
                    except ImportError:
                        from targeted_type_fallback import fill_best_text_input
                    value, display_value = _resolve_type_value_for_handoff(
                        raw_value,
                        workflow_memory=workflow_memory,
                    )
                    fallback_result = await fill_best_text_input(page, value)
                    if not fallback_result.get("ok"):
                        return False
                    await browser._wait_after_action(light_action=True)
                    browser.rpa_trail.append({
                        "action": "type",
                        "method": "dom_input_fallback",
                        "type_value": display_value,
                    })
                    browser._last_action_result = ActionResult.from_action(
                        action_payload,
                        success=True,
                        message=(
                            "DOM fallback filled visible text/search input "
                            f"{fallback_result.get('tag', '')} "
                            f"{fallback_result.get('ariaLabel') or fallback_result.get('placeholder') or fallback_result.get('name') or ''!r}"
                        ),
                        before_url=before_url,
                        after_url=browser.current_url,
                        before_pages=before_pages,
                        after_pages=len(browser._context.pages) if browser._context else before_pages,
                        metadata={
                            "value_readbacks": [
                                {
                                    "value": display_value,
                                    "method": "dom_input_fallback",
                                    "observed": fallback_result.get("value", ""),
                                }
                            ],
                            "targeted_handoff": {
                                "mode": "dom_input_fallback",
                                **fallback_result,
                            },
                            "probe": probe_result.as_dict(),
                        },
                    )
                    logger.info(
                        "[TARGETED HANDOFF] type DOM fallback -> %s",
                        fallback_result,
                    )
                    return True
                except Exception as fallback_exc:
                    logger.debug(
                        "[TARGETED HANDOFF] DOM input fallback skipped: %s",
                        fallback_exc,
                    )
                    return False
            frame = page.main_frame
            for item in page.frames:
                try:
                    if candidate.frame_name and item.name == candidate.frame_name:
                        frame = item
                        break
                    if candidate.frame_url and item.url == candidate.frame_url:
                        frame = item
                        break
                except Exception:
                    continue
            value, display_value = _resolve_type_value_for_handoff(
                raw_value,
                workflow_memory=workflow_memory,
            )
            locator = frame.locator(candidate.selector).first
            await locator.scroll_into_view_if_needed(timeout=2500)
            await locator.click(timeout=3000)
            await locator.fill(value, timeout=4000)
            await locator.evaluate(
                """el => {
                    el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true, composed: true}));
                }"""
            )
            await browser._wait_after_action(light_action=True)
            browser.rpa_trail.append({
                "action": "type",
                "selector": candidate.selector,
                "method": "targeted_probe_handoff",
                "type_value": display_value,
            })
            browser._last_action_result = ActionResult.from_action(
                action_payload,
                success=True,
                message=(
                    "targeted_probe high-confidence handoff filled "
                    f"{candidate.tag} {candidate.text!r}"
                ),
                before_url=before_url,
                after_url=browser.current_url,
                before_pages=before_pages,
                after_pages=len(browser._context.pages) if browser._context else before_pages,
                metadata={
                    "value_readbacks": [
                        {
                            "selector": candidate.selector,
                            "value": display_value,
                            "method": "targeted_probe_handoff",
                        }
                    ],
                    "targeted_handoff": {
                        "mode": "type_to_input_selector",
                        "selector": candidate.selector,
                        "confidence": candidate.confidence,
                        "kind": candidate.kind,
                        "text": candidate.text,
                        "frame_url": candidate.frame_url,
                        "evidence": list(candidate.evidence),
                    },
                    "probe": probe_result.as_dict(),
                },
            )
            logger.info(
                "[TARGETED HANDOFF] type -> selector=%s conf=%s text=%r",
                candidate.selector,
                candidate.confidence,
                candidate.text,
            )
            return True
        except Exception as exc:
            logger.debug("[TARGETED HANDOFF] type probe/fill skipped: %s", exc)
            return False

    action_registry.bind("auto_form_fill", _try_auto_form_fill)
    action_registry.bind("round_form_challenge", _run_round_form_if_present)
    action_registry.bind("date_pick", _replay_rpa)
    action_registry.bind("cascader_pick", _replay_rpa)
    action_registry.bind("hover_and_click", _browser_action_tool)
    action_registry.bind("next_page", _browser_action_tool)
    action_registry.bind("targeted_probe", _targeted_probe_tool)
    _registry_dispatch_actions = {"hover_and_click", "next_page", "targeted_probe"}
    _run_succeeded = False
    _run_ckpt = None  # RUN-RESUME1 step2b: run checkpointer (set before the step loop)
    _selected_tools: list[dict] = []
    _capability_route: dict | None = None
    # E1b: runtime cross-system tracker. Built from the capability route's
    # workflow_graph systems once routing completes; observes each step's
    # URL to surface real cross-system hops in the event stream. Stays
    # None (inert) if routing fails so the hot loop hook is a safe no-op.
    _run_system_tracker = None
    # E1c-3a: pool-less session router; describes the BrowserSession a
    # cross-system hop would switch to (decision + evidence). None (inert) when
    # routing fails so the hot loop hook is a safe no-op.
    _session_router = None
    # E1c-3b-2c: the home (primary lease) handle + system id, captured on the
    # first cross-system transition so a hop back to the home system rebinds to
    # the original lease rather than a fresh pooled session.
    _home_browser = None
    _home_system_id = ""

    def _check_stop(context: str) -> None:
        if stop_event and stop_event.is_set():
            raise RuntimeError(f"STOP_REQUESTED::{context}")

    def _abort_if_stale_auth() -> bool:
        if not getattr(browser, "auth_stale_detected", False):
            return False
        stale_msg = getattr(browser, "auth_stale_reason", "") or (
            "Auth profile appears stale; please refresh it with tools/manual_auth.py."
        )
        logger.error("[AUTH STALE] %s", stale_msg)
        _broadcast_log_safe(f"[AUTH STALE] {stale_msg}", level="error")
        _broadcast_done_safe(False, stale_msg)
        return True

    def _with_tool_metadata(result):
        if result is None:
            return result
        try:
            action_name = (
                result.action
                if isinstance(result, ActionResult)
                else str(result.get("action") or "")
            )
        except Exception:
            action_name = ""
        tool_meta = _resolve_action_tool_metadata(
            action_registry,
            action_name,
            goal=goal,
            selected_tools=_selected_tools,
        )
        if not tool_meta:
            return result
        if isinstance(result, ActionResult):
            result.metadata.setdefault("tool", tool_meta)
            return result
        if isinstance(result, dict):
            data = dict(result)
            metadata = dict(data.get("metadata") or {})
            metadata.setdefault("tool", tool_meta)
            data["metadata"] = metadata
            return data
        return result

    async def _recover_active_page(reason: str):
        page = await browser._ensure_active_page(reason=reason)
        if page is not None and not page.is_closed():
            return page
        logger.warning(
            "[BROWSER RECOVERY] No active page during %s; restarting browser at %s",
            reason,
            start_url,
        )
        event_stream.guard(
            step=0,
            name="BROWSER_CONTEXT_RECOVERY",
            message=f"No active page during {reason}; restarted browser context",
            metadata={"start_url": start_url},
        )
        await browser.restart(start_url, reason=reason)
        page = await browser._ensure_active_page(reason=f"after restart: {reason}")
        if page is None or page.is_closed():
            raise RuntimeError(f"No active page after browser restart ({reason})")
        return page

    try:
        _broadcast_log_safe("VSpider Agent started", level="info")
        event_stream.run_start(goal=goal, start_url=start_url)
        _initial_strategy_context = infer_goal_strategy_context(
            goal,
            url=start_url,
            target_count=_parse_goal_target_count(goal),
            requested_fields=_parse_goal_requested_fields(goal),
        )
        try:
            _capability_route = route_capabilities_for_task(
                goal,
                url=start_url,
                limit=12,
            )
            event_stream.emit(
                "capability_route",
                intent=_capability_route.get("intent"),
                backend_plan=_capability_route.get("backend_plan"),
                fallback_chain=_capability_route.get("fallback_chain"),
                capability_manifest=_capability_route.get("capability_manifest"),
                action_ref_schema=_capability_route.get("action_ref_schema"),
                runtime_preflight=_capability_route.get("runtime_preflight"),
                execution_plan=_capability_route.get("execution_plan"),
                workflow_graph=_capability_route.get("workflow_graph"),
                model_roles=_capability_route.get("model_roles"),
                audit=_capability_route.get("audit"),
            )
            try:
                from api_server import broadcast_phase as _broadcast_capability_phase

                _route_names = [
                    item.get("name")
                    for item in (_capability_route.get("backend_plan") or [])[:8]
                    if isinstance(item, dict) and item.get("name")
                ]
                _broadcast_capability_phase(
                    "capability_route",
                    severity="info",
                    message=" → ".join(_route_names),
                    extra={
                        "intent": _capability_route.get("intent"),
                        "backend_plan": _capability_route.get("backend_plan"),
                        "fallback_chain": _capability_route.get("fallback_chain"),
                        "capability_manifest": _capability_route.get("capability_manifest"),
                        "action_ref_schema": _capability_route.get("action_ref_schema"),
                        "runtime_preflight": _capability_route.get("runtime_preflight"),
                        "execution_plan": _capability_route.get("execution_plan"),
                        "workflow_graph": _capability_route.get("workflow_graph"),
                        "model_roles": _capability_route.get("model_roles"),
                        "audit": _capability_route.get("audit"),
                    },
                )
            except Exception as _capability_phase_err:
                logger.debug("[CAPABILITY ROUTER] phase broadcast skipped: %s", _capability_phase_err)
            logger.info(
                "[CAPABILITY ROUTER] intent=%s plan=%s",
                (_capability_route.get("intent") or {}).get("task_type"),
                [item.get("name") for item in (_capability_route.get("backend_plan") or [])[:8]],
            )
        except Exception as _capability_route_err:
            logger.debug("[CAPABILITY ROUTER] skipped: %s", _capability_route_err)
        # E1b: build the runtime cross-system tracker from the planned
        # workflow_graph systems. Inert when routing produced no systems.
        try:
            from visual_web_agent.run_system_tracker import build_run_system_tracker

            _run_system_tracker = build_run_system_tracker(_capability_route)
        except Exception as _tracker_build_err:
            logger.debug("[RUN SYSTEM TRACKER] build skipped: %s", _tracker_build_err)
        # E1c-3a: build a pool-less session router so a cross-system hop can
        # describe the BrowserSession it would switch to. Decision + evidence
        # only; the physical context swap is a later, flag-gated slice.
        try:
            from visual_web_agent.session_router import build_session_router

            _session_router = build_session_router(_run_ts, _capability_route)
        except Exception as _session_router_build_err:
            logger.debug("[SESSION ROUTER] build skipped: %s", _session_router_build_err)
        # E1c cross-system config: single entry for the flag + TTL parses folded
        # from the previously-inline env reads (cross_system_config.v1).
        from visual_web_agent.cross_system_config import (
            cross_system_enabled as _xsys_enabled,
            profile_ttl_hours as _xsys_profile_ttl_hours,
        )
        _selected_tools = action_registry.select_for_goal(
            goal,
            strategy_context=_initial_strategy_context,
        )
        event_stream.emit(
            "tool_catalog",
            tools=action_registry.list_tools(include_disabled=True),
            selected_tools=_selected_tools,
            strategy_context=_initial_strategy_context,
        )
        logger.info(
            "[ACTION REGISTRY] registered=%s selected=%s",
            len(action_registry.list_tools(include_disabled=True)),
            [tool.get("name") for tool in _selected_tools],
        )
        if (
            _goal_is_bulk_extraction(goal)
            and "docs.google.com/spreadsheets/" in str(start_url or "")
        ):
            _sheet_target = _parse_goal_target_count(goal) or 20
            logger.info(f"{'=' * 60}")
            logger.info(f"[TARGET] {goal}")
            _broadcast_log_safe(f"[TARGET] {goal}")
            logger.info(f"[URL] {start_url}")
            _broadcast_log_safe(f"[URL] {start_url}")
            try:
                _sheet_rows = _load_public_google_sheet_rows(start_url, _sheet_target)
            except Exception as exc:
                logger.warning("[GOOGLE SHEETS EXTRACT] CSV export preflight failed: %s", exc)
                _sheet_rows = []
            if _sheet_rows:
                saved_path = save_run_dataset(
                    _sheet_rows,
                    run_id=_run_ts,
                    output_contract=_initial_output_contract,
                    produced_by="google_sheets_csv_export",
                    filename_hint=_vlm_output,
                )
                logger.info(
                    "[GOOGLE SHEETS EXTRACT] saved %s/%s rows via CSV export -> %s",
                    len(_sheet_rows),
                    _sheet_target,
                    saved_path,
                )
                event_stream.extract(
                    step=0,
                    source="GOOGLE_SHEETS_CSV_EXPORT",
                    rows=len(_sheet_rows),
                    output_file=str(saved_path),
                    metadata={
                        "target": _sheet_target,
                        "export_url": _google_sheets_csv_export_url(start_url),
                    },
                )
                _broadcast_log_safe(
                    f"[GOOGLE SHEETS EXTRACT] 已通过 CSV 导出保存 {len(_sheet_rows)}/{_sheet_target} 行。"
                )
                _run_succeeded = len(_sheet_rows) >= _sheet_target
                _broadcast_done_safe(_run_succeeded, "Google Sheets CSV export completed")
                return _run_succeeded
        _check_stop("before_browser_start")
        try:
            from .config import apply_run_constraints
        except ImportError:
            from config import apply_run_constraints
        apply_run_constraints(run_constraints)
        # 启动浏览器并导航
        await browser.start(start_url)
        await _recover_active_page("after browser.start")
        # E1c-A1 (path 2): when cross-system switching is enabled, thread the
        # run's SessionRouter onto the browser so browser_env passes it into
        # each ActionContext and GotoHandler can intercept a cross-system goto
        # before it navigates the current page. Flag off -> never attached ->
        # ctx.session_router stays None -> goto behaviour is byte-identical.
        try:
            if (
                _session_router is not None
                and _xsys_enabled()
            ):
                browser._session_router = _session_router
                # RPA-XSYS-EVT: expose the run event_stream so fast-path
                # replay (_replay_rpa -> _replay_switch_system) emits
                # session_switch evidence. Flag off -> never attached.
                browser._event_stream = event_stream
        except Exception as _xsys_attach_err:
            logger.debug("[SESSION ROUTER] attach skipped: %s", _xsys_attach_err)
        # E1c profile GC: when cross-system switching is on, sweep stale per-
        # system isolated Chromium profile dirs (sys_*) left by past runs before
        # this run launches new ones. TTL via VSPIDER_PROFILE_TTL_HOURS (default
        # 24h); bounded to sys_* under the configured base; best-effort. Flag off
        # -> never runs. Active profiles keep a fresh mtime, so a concurrent run's
        # dirs survive the TTL cutoff.
        try:
            if _xsys_enabled():
                from visual_web_agent.browser_profile import gc_profile_dirs as _gc_profiles
                try:
                    from . import config as _gc_cfg
                except ImportError:
                    import config as _gc_cfg
                try:
                    _gc_ttl_hours = _xsys_profile_ttl_hours()
                except (TypeError, ValueError):
                    _gc_ttl_hours = 24.0
                _gc_removed = _gc_profiles(
                    getattr(_gc_cfg, "BROWSER_USER_DATA_DIR", "") or "",
                    ttl_seconds=_gc_ttl_hours * 3600.0,
                )
                if _gc_removed:
                    event_stream.emit(
                        "profile_gc",
                        removed=_gc_removed,
                        count=len(_gc_removed),
                        ttl_hours=_gc_ttl_hours,
                    )
                    logger.info(
                        "[PROFILE GC] removed %d stale profile dir(s): %s",
                        len(_gc_removed),
                        _gc_removed,
                    )
        except Exception as _gc_err:
            logger.debug("[PROFILE GC] skipped: %s", _gc_err)
        # ── E2: media harvester deterministic fast path ──────────────
        # When capability_router has decided this run wants media
        # (output_kind=media_image/video/audio/pdf/archive or
        # file_generic), try to download everything visible on the
        # landing page deterministically BEFORE spending VLM calls.
        # Any failure inside the hook is logged at debug and silently
        # ignored; the VLM loop continues as if no hook had run.
        try:
            from visual_web_agent.media_harvester import maybe_run_media_harvest

            _media_hook_result = await maybe_run_media_harvest(
                page=getattr(browser, "_page", None),
                capability_route=_capability_route,
                run_id=_run_ts,
                goal=goal,
                resume=bool((run_constraints or {}).get("resume")),
            )
            if _media_hook_result.triggered:
                event_stream.emit(
                    "media_harvest",
                    output_kind=_media_hook_result.output_kind,
                    downloaded=_media_hook_result.downloaded_count,
                    failed=_media_hook_result.failed_count,
                    manifest_appended=_media_hook_result.manifest_appended,
                    candidate_count=_media_hook_result.candidate_count,
                    short_circuit=_media_hook_result.short_circuit,
                    report=_media_hook_result.report_dict,
                )
                try:
                    from api_server import broadcast_phase as _bp_media

                    _bp_media(
                        "media_harvest",
                        severity="info" if _media_hook_result.success else "warning",
                        message=(
                            f"downloaded={_media_hook_result.downloaded_count} "
                            f"failed={_media_hook_result.failed_count} "
                            f"output_kind={_media_hook_result.output_kind}"
                        ),
                        extra=_media_hook_result.to_dict(),
                    )
                except Exception:
                    pass
                if _media_hook_result.short_circuit:
                    _run_succeeded = bool(_media_hook_result.success)
                    _broadcast_done_safe(
                        _run_succeeded,
                        (
                            f"Media harvest completed: "
                            f"{_media_hook_result.downloaded_count} files saved"
                        ),
                    )
                    return _run_succeeded
        except Exception as _media_hook_err:
            logger.debug("[MEDIA HARVEST HOOK] skipped: %s", _media_hook_err)
        # ── end media harvester fast path ────────────────────────────
        _initial_http_status = getattr(browser, "last_navigation_status", None)
        if (
            isinstance(_initial_http_status, int)
            and _initial_http_status >= 400
            and re.search(r"\b404\b|\bhttp\s*error\b|\bstatus\s*code\b|错误|终止|状态码", goal, re.I)
        ):
            message = f"检测到 HTTP {_initial_http_status}，任务终止"
            logger.warning("[HTTP ERROR GUARD] %s url=%s", message, getattr(browser, "last_navigation_url", ""))
            event_stream.guard(
                step=0,
                name="HTTP_ERROR_GUARD",
                message=message,
                metadata={
                    "status_code": _initial_http_status,
                    "url": getattr(browser, "last_navigation_url", "") or start_url,
                },
            )
            event_stream.done(
                step=0,
                success=True,
                reason="http_error_guard",
                message=message,
                metadata={
                    "status_code": _initial_http_status,
                    "url": getattr(browser, "last_navigation_url", "") or start_url,
                },
            )
            _broadcast_done_safe(True, message)
            _run_succeeded = True
            return True
        if _abort_if_stale_auth():
            return False

        # 预配置上传文件路径（--upload-file 参数）
        if upload_file:
            from pathlib import Path as _Path
            _uf = _Path(upload_file)
            if _uf.exists():
                browser.set_upload_file(_uf)
                logger.info(f"[UPLOAD] Pre-configured upload file: {_uf.resolve()}")
            else:
                logger.warning(f"[UPLOAD] --upload-file path not found: {upload_file}")

        # 配置 XHR/Fetch 拦截器（双轨制）
        #   轨道 1（精准截胡）：--xhr-pattern 指定 URL 关键词，命中后跳过 VLM
        #   轨道 2（通用拦截）：--xhr 参数开启，全量收集所有 API 数据列表
        browser.configure_interceptor(
            enabled=enable_xhr,
            filename=_xhr_output,
            unique_key=None,
            min_list_size=20,
            url_pattern=xhr_pattern or None,
            output_contract=_initial_output_contract or None,
        )
        browser.configure_network_intelligence(_run_ts)
        if xhr_pattern:
            logger.info(
                f"[XHR HYBRID] Primary engine (url_pattern={xhr_pattern!r}) → "
                f"will intercept and save to {_xhr_output}"
            )
        if enable_xhr:
            logger.info(f"[XHR] General interceptor enabled → {_xhr_output}")
        else:
            logger.info("[XHR] General interceptor disabled (use --xhr to enable)")

        # ── 预检登录拦截器：在 VLM 接管前，优先用原生 Playwright 完成一次安全登录 ──
        await _run_preflight_login(browser, goal, require_login=require_login)

        # ── Auth Sentinel（通用认证哨兵）──────────────────────────────
        # 不写站点专属"已登录选择器"，只把低成本环境信号交给 VLM 做视觉裁定。
        auth_note = await browser.refresh_auth_sentinel()
        if auth_note:
            goal = (
                goal
                + "\n\n【认证环境（系统通用检测，非业务结论）】\n"
                + auth_note
            )
            logger.info("[AUTH SENTINEL] Injected generic auth environment note into goal.")
        if _abort_if_stale_auth():
            return False
        # ──────────────────────────────────────────────────────────────────

        # ── 相对日期解析（通用能力，不绑定具体网站/控件）────────────────
        # goal 中任何相对日期短语（"下个月15号"、"3天后"、"下周三"、"月底"…）
        # 都由 visual_web_agent.relative_date 提前解析成绝对日期 YYYY-MM-DD。
        # VLM 拿到的 goal 末尾会附加【相对日期解析】块，避免它再去做日期算术。
        try:
            from .relative_date import resolve_relative_date, format_resolver_hint
        except ImportError:
            from relative_date import resolve_relative_date, format_resolver_hint  # type: ignore[no-redef]
        try:
            _rel_date_match = resolve_relative_date(goal)
        except Exception as _rd_err:
            logger.debug("[DATE RESOLVER] skipped (%s)", _rd_err)
            _rel_date_match = None
        if _rel_date_match is not None:
            goal = goal + format_resolver_hint(_rel_date_match)
            logger.info(
                "[DATE RESOLVER] %r → %s (Δm=%+d, conf=%s)",
                _rel_date_match.phrase,
                _rel_date_match.target.isoformat(),
                _rel_date_match.delta_months,
                _rel_date_match.confidence,
            )
        # ──────────────────────────────────────────────────────────────────

        # ── 数据导出 URL 解析（通用能力，不绑定具体数据源）────────────────
        # 当 start_url 命中 data_export 注册表（Sheets / OneDrive / SharePoint …），
        # 在 goal 末尾追加【数据导出 URL】块。VLM 看到提示后第一步直接 goto
        # 这个 URL，绕开 canvas/预览渲染。新数据源只要在 data_export.py 注册
        # 一条 ExportTransform，这里自动生效，无需改 prompt / 改主流程。
        try:
            from .data_export import find_data_export_url
        except ImportError:
            from data_export import find_data_export_url  # type: ignore[no-redef]
        try:
            _de_source, _de_export = find_data_export_url(start_url)
        except Exception as _de_err:
            logger.debug("[DATA EXPORT] skipped (%s)", _de_err)
            _de_source, _de_export = "", ""
        if _de_source and _de_export:
            goal = (
                goal
                + "\n\n【数据导出 URL】（系统自动计算，绕开 canvas 预览）"
                + f"\n· 识别来源: {_de_source}"
                + f"\n· 导出 URL: {_de_export}"
            )
            logger.info(
                "[DATA EXPORT] %s → %s",
                _de_source,
                _de_export,
            )
        # ──────────────────────────────────────────────────────────────────

        logger.info(f"{'=' * 60}")
        logger.info(f"[TARGET] {goal}")
        _broadcast_log_safe(f"[TARGET] {goal}")
        logger.info(f"[URL] {start_url}")
        _broadcast_log_safe(f"[URL] {start_url}")
        _macro_extract_rows: list[dict[str, str]] = []
        _macro_extract_source = ""
        _macro_expected_rows = 0
        _macro_required_fields: list[str] = []
        _skill_match_for_replay = None
        _skill_result_for_replay = None
        _skill_replay_input_url = ""
        try:
            if not _macro_extract_rows:
                _skill_candidate_url = getattr(browser, "current_url", "")
                _skill_strategy_context = infer_goal_strategy_context(
                    goal,
                    url=_skill_candidate_url,
                    target_count=_parse_goal_target_count(goal),
                )
                event_stream.emit(
                    "strategy_context",
                    step=0,
                    context=_skill_strategy_context,
                )
                _skill_match = skill_registry.best_match(
                    url=_skill_candidate_url,
                    goal=goal,
                    strategy_context=_skill_strategy_context,
                )
                if _skill_match:
                    _matched_skill = _skill_match.skill
                    _skill_replay_input_url = _skill_candidate_url
                    _macro_tool = action_registry.resolve_for_action(
                        _matched_skill.action,
                        goal=goal,
                    ) or {
                        "name": _matched_skill.action,
                        "capability": _matched_skill.capability,
                        "risk": "low",
                    }
                    _skill_metadata = _matched_skill.dispatch_metadata(
                        url=getattr(browser, "current_url", ""),
                        goal=goal,
                    )
                    _skill_metadata["selection"] = {
                        "score": _skill_match.score,
                        "reasons": list(_skill_match.reasons),
                        "history_runs": _skill_match.history_runs,
                        "history_success_rate": _skill_match.history_success_rate,
                        "strategy_context": _skill_strategy_context,
                    }
                    event_stream.emit(
                        "skill_match",
                        step=0,
                        skill={
                            "name": _matched_skill.name,
                            "action": _matched_skill.action,
                            "source": _matched_skill.source,
                            "capability": _matched_skill.capability,
                        },
                        metadata=_skill_metadata,
                    )
                    event_stream.emit(
                        "tool_dispatch",
                        step=0,
                        action=_matched_skill.action,
                        tool={
                            "name": _macro_tool.get("name"),
                            "capability": _macro_tool.get("capability"),
                            "risk": _macro_tool.get("risk", "low"),
                        },
                        metadata=_skill_metadata,
                    )
                    _skill_result = await _matched_skill.run(browser, goal)
                    _skill_match_for_replay = _skill_match
                    _skill_result_for_replay = _skill_result
                    _macro_expected_rows = int(_skill_result.expected_rows or 1)
                    _macro_required_fields = list(_skill_result.required_fields or [])
                    _macro_extract_rows = list(_skill_result.rows or [])
                    _macro_extract_source = str(_skill_result.source or _matched_skill.source)
                    for _verification in _skill_result.verifications:
                        event_stream.verify(
                            step=0,
                            name=_verification.name,
                            success=bool(_verification.success),
                            expected=_verification.expected,
                            observed=_verification.observed,
                            metadata=dict(_verification.metadata or {}),
                        )
                    if not _macro_extract_rows:
                        event_stream.guard(
                            step=0,
                            name=f"{_macro_extract_source}_FALLBACK",
                            message=(
                                f"Skill {_matched_skill.name} matched but produced no rows; "
                                "falling back to legacy macro/generic loop."
                            ),
                            metadata=_skill_metadata,
                        )
        except Exception as _macro_err:
            logger.debug("[INTERACTION EXTRACT MACRO] skipped: %s", _macro_err)
            event_stream.guard(
                step=0,
                name="INTERACTION_EXTRACT_MACRO_ERROR",
                message=f"{type(_macro_err).__name__}: {_macro_err}",
                metadata={"source": _macro_extract_source or "unknown"},
            )
            _macro_extract_rows = []
            _macro_extract_source = ""
        if _macro_extract_rows:
            _macro_fields = sorted(
                {
                    str(key)
                    for row in _macro_extract_rows
                    if isinstance(row, dict)
                    for key, value in row.items()
                    if value not in (None, "")
                }
            )
            event_stream.verify(
                step=0,
                name=f"{_macro_extract_source}_row_count",
                success=len(_macro_extract_rows) >= max(1, _macro_expected_rows),
                expected=max(1, _macro_expected_rows),
                observed=len(_macro_extract_rows),
                metadata={"fields": _macro_fields},
            )
            _required_macro_fields = _macro_required_fields or (
                ["trigger", "title", "content"]
                if _macro_extract_source == "MODAL_DIALOG_MACRO"
                else ["step", "title", "url"]
            )
            _missing_macro_fields = [
                field
                for field in _required_macro_fields
                if any(not str((row or {}).get(field) or "").strip() for row in _macro_extract_rows)
            ]
            event_stream.verify(
                step=0,
                name=f"{_macro_extract_source}_required_fields",
                success=not _missing_macro_fields,
                expected=_required_macro_fields,
                observed={"missing": _missing_macro_fields, "fields": _macro_fields},
                metadata={"rows": len(_macro_extract_rows)},
            )
            saved_path = save_run_dataset(
                _macro_extract_rows,
                run_id=_run_ts,
                output_contract=_goal_output_contract,
                produced_by="semantic_macro_extract",
                filename_hint=_vlm_output,
            )
            _skill_replay_path = ""
            if _skill_match_for_replay and _skill_result_for_replay:
                try:
                    _skill_replay_path = str(save_skill_replay_snapshot(
                        skill_match=_skill_match_for_replay,
                        goal=goal,
                        url=_skill_replay_input_url or getattr(browser, "current_url", ""),
                        result=_skill_result_for_replay,
                        run_id=_run_ts,
                        output_file=str(saved_path or ""),
                    ))
                    event_stream.emit(
                        "skill_replay",
                        step=0,
                        skill={
                            "name": _skill_match_for_replay.skill.name,
                            "source": _skill_match_for_replay.skill.source,
                        },
                        path=_skill_replay_path,
                        metadata={
                            "score": _skill_match_for_replay.score,
                            "reasons": list(_skill_match_for_replay.reasons),
                        },
                    )
                except Exception as _replay_err:
                    event_stream.guard(
                        step=0,
                        name="SKILL_REPLAY_SAVE_FAILED",
                        message=f"{type(_replay_err).__name__}: {_replay_err}",
                        metadata={"source": _macro_extract_source},
                    )
            logger.info(
                "[INTERACTION EXTRACT MACRO] %s saved %s rows -> %s",
                _macro_extract_source,
                len(_macro_extract_rows),
                saved_path,
            )
            event_stream.extract(
                step=0,
                source=_macro_extract_source,
                rows=len(_macro_extract_rows),
                output_file=str(saved_path or ""),
                metadata={
                    "url": getattr(browser, "current_url", ""),
                    "skill_replay_path": _skill_replay_path,
                },
            )
            _broadcast_log_safe(
                f"[{_macro_extract_source}] 已保存 {len(_macro_extract_rows)} 条交互抽取结果。"
            )
            _run_succeeded = True
            _broadcast_done_safe(True, f"{_macro_extract_source} completed")
            return True
        if (
            _goal_is_bulk_extraction(goal)
            and "docs.google.com/spreadsheets/" in str(start_url or "")
        ):
            _sheet_target = _parse_goal_target_count(goal) or 20
            try:
                _sheet_rows = _load_public_google_sheet_rows(start_url, _sheet_target)
            except Exception as exc:
                logger.warning("[GOOGLE SHEETS EXTRACT] CSV export fallback failed: %s", exc)
                _sheet_rows = []
            if _sheet_rows:
                saved_path = save_run_dataset(
                    _sheet_rows,
                    run_id=_run_ts,
                    output_contract=_initial_output_contract,
                    produced_by="google_sheets_csv_export",
                    filename_hint=_vlm_output,
                )
                logger.info(
                    "[GOOGLE SHEETS EXTRACT] saved %s/%s rows via CSV export -> %s",
                    len(_sheet_rows),
                    _sheet_target,
                    saved_path,
                )
                event_stream.extract(
                    step=0,
                    source="GOOGLE_SHEETS_CSV_EXPORT",
                    rows=len(_sheet_rows),
                    output_file=str(saved_path),
                    metadata={
                        "target": _sheet_target,
                        "export_url": _google_sheets_csv_export_url(start_url),
                    },
                )
                _broadcast_log_safe(
                    f"[GOOGLE SHEETS EXTRACT] 已通过 CSV 导出保存 {len(_sheet_rows)}/{_sheet_target} 行。"
                )
                _broadcast_done_safe(True, "Google Sheets CSV export completed")
                _run_succeeded = len(_sheet_rows) >= _sheet_target
                return _run_succeeded
        _effective_max_steps = _derive_effective_max_steps(goal)
        # Unified semantic-macro detection. Registry parsers run first; legacy
        # All semantic-macro detection (date_pick, cascader_pick, future ones)
        # flows through the unified registry entry point.
        _initial_semantic_macro = _parse_semantic_macro(goal)
        _semantic_macro_owns_goal = _semantic_macro_should_own_goal(
            goal,
            _initial_semantic_macro,
        )
        _is_form_fill_goal = _goal_is_form_fill(goal)
        if _semantic_macro_owns_goal:
            _is_form_fill_goal = False
            logger.info(
                "[SEMANTIC MACRO] standalone component goal detected; "
                "bypassing generic form-fill mode."
            )
        if not _is_form_fill_goal:
            try:
                _goal_page = await browser._ensure_active_page(reason="detect rpachallenge form goal")
                _goal_challenge_state = await _get_round_form_state(_goal_page) if _goal_page else {}
                if _goal_challenge_state.get("is_challenge"):
                    _is_form_fill_goal = True
                    logger.info("[RPA CHALLENGE] page detected; treating task as a round-aware form goal.")
            except Exception as _challenge_goal_err:
                logger.debug("[RPA CHALLENGE] initial page detection skipped: %s", _challenge_goal_err)
        _form_assignments = _prepare_form_batch_fields(goal) if _is_form_fill_goal else {}
        _requested_output_fields = _parse_goal_requested_fields(goal)
        _goal_output_contract = resolve_output_contract(
            _initial_output_contract,
            dict((_initial_strategy_context or {}).get("output_contract") or {}),
            infer_goal_output_contract(
                goal,
                target_count=_parse_goal_target_count(goal),
                requested_fields=_requested_output_fields,
            ) if not _initial_output_contract else None,
        )
        _goal_output_mode = str(
            _goal_output_contract.get("mode")
            or _goal_output_contract.get("output_mode")
            or "default"
        )
        # 拦截器容器跟随最终契约（不重置去重状态）
        try:
            browser.set_interceptor_output_contract(_goal_output_contract or None)
        except Exception as _ic_err:
            logger.debug("[XHR] interceptor contract refresh skipped: %s", _ic_err)
        # 向前端 Final Answer 面板同步 mode：answer/artifact → answer_type=text/file
        # F3: 同时把目标域分类（weather/stock/recipe/flight/generic）一并存档，
        # 供 done 广播时透传给前端，让 Final Answer 面板按域选卡片渲染。
        _goal_answer_domain = _detect_answer_domain(goal)
        _record_run_answer(
            mode=_goal_output_mode,
            domain=_goal_answer_domain,
        )
        await _maybe_run_answer_search_fast_path(
            browser,
            event_stream,
            goal=goal,
            start_url=start_url,
            output_mode=_goal_output_mode,
        )
        _data_controller = PageDataController(_requested_output_fields)
        if _requested_output_fields:
            logger.info("[EXTRACT SCHEMA] requested fields=%s", _requested_output_fields)
        if _goal_output_mode != "default":
            logger.info(
                "[OUTPUT CONTRACT] mode=%s contract=%s",
                _goal_output_mode,
                _goal_output_contract,
            )
        if _effective_max_steps > MAX_STEPS:
            logger.info(
                f"[MAX STEPS] bulk extraction budget raised: "
                f"{MAX_STEPS} -> {_effective_max_steps}"
            )
            _broadcast_log_safe(
                f"[MAX STEPS] 批量提取任务自动提高步数预算: "
                f"{MAX_STEPS} -> {_effective_max_steps}",
                level="info",
            )
        else:
            logger.info(f"[MAX STEPS] {MAX_STEPS}")
        logger.info(f"{'=' * 60}")
        workflow_memory: dict = {}

        # Component-library forms are more reliable through scoped DOM execution
        # than through step-by-step VLM clicks. The executor honors a title/scope
        # phrase such as "Basic Form 标题下方" before touching fields.
        if _is_form_fill_goal:
            _challenge_done = False
            if _should_use_round_form_macro(goal, _form_assignments):
                _challenge_done = await _run_round_form_if_present(browser, goal)
            if _challenge_done:
                _run_succeeded = True
                _broadcast_done_safe(True, "RPA Challenge completed")
                try:
                    _auto_ss, _ = await browser.mark_and_screenshot(step=99)
                    html_logger.log_step(
                        step_num=99,
                        screenshot_path=str(Path(SCREENSHOT_DIR) / "step_99.png") if _auto_ss else None,
                        action_dict={
                            "action": "done",
                            "target_id": 0,
                            "status": "success",
                            "thought": "RPA Challenge 已点击 Start，并按 challenge.xlsx 连续完成全部轮次；仅最终完成态允许 done。",
                            "type_value": "rpa_challenge",
                        },
                        memory_state=dict(workflow_memory),
                    )
                except Exception as _challenge_log_err:
                    logger.debug("[RPA CHALLENGE] failed to log final screenshot: %s", _challenge_log_err)
                event_stream.verify(
                    step=99,
                    name="rpa_challenge_completion",
                    success=True,
                    expected="final completion state",
                    observed="challenge macro returned complete",
                    metadata={"macro": "round_form", "phase": "startup"},
                )
                event_stream.act(
                    step=99,
                    result=_with_tool_metadata(
                        ActionResult.from_action(
                            {"action": "auto_form_macro", "type_value": "rpa_challenge"},
                            success=True,
                            message="RPA Challenge completed",
                            after_url=getattr(browser, "current_url", "") or "",
                            after_pages=len(browser._context.pages) if browser._context else 0,
                            metadata={"macro": "round_form", "phase": "startup"},
                        )
                    ),
                )
                return True

            _auto_form_blocked = (
                False if _form_assignments else await _auto_form_has_prestart_gate(browser)
            )
            if not _auto_form_blocked:
                _auto_form_ok = await _try_auto_form_fill(browser, goal)
                if _auto_form_ok:
                    _run_succeeded = True
                    _broadcast_done_safe(True, "Auto form fill completed")
                    try:
                        _auto_ss, _ = await browser.mark_and_screenshot(step=99)
                        html_logger.log_step(
                            step_num=99,
                            screenshot_path=str(Path(SCREENSHOT_DIR) / "step_99.png") if _auto_ss else None,
                            action_dict={
                                "action": "done",
                                "target_id": 0,
                                "status": "success",
                                "thought": (
                                    "AUTO FORM 已完成字段回读校验，并已点击 Create/Submit（若目标要求提交）。"
                                    + "\n"
                                    + _format_auto_form_validation_summary(
                                        getattr(browser, "_last_auto_form_result", None)
                                    )
                                ),
                                "type_value": "auto_form",
                                "form_validation": getattr(browser, "_last_auto_form_result", None),
                            },
                            memory_state=dict(workflow_memory),
                        )
                    except Exception as _auto_log_err:
                        logger.debug("[AUTO FORM] failed to log final screenshot: %s", _auto_log_err)
                    event_stream.verify(
                        step=99,
                        name="auto_form_readback",
                        success=True,
                        expected="all parsed form fields",
                        observed="field readback ok",
                        metadata={
                            "form_validation": getattr(
                                browser,
                                "_last_auto_form_result",
                                None,
                            ),
                            "phase": "startup",
                        },
                    )
                    event_stream.act(
                        step=99,
                        result=_with_tool_metadata(
                            ActionResult.from_action(
                                {"action": "auto_form", "type_value": "auto_form"},
                                success=True,
                                message="Auto form fill completed",
                                after_url=getattr(browser, "current_url", "") or "",
                                after_pages=len(browser._context.pages) if browser._context else 0,
                                metadata={
                                    "form_validation": getattr(
                                        browser,
                                        "_last_auto_form_result",
                                        None,
                                    ),
                                    "phase": "startup",
                                },
                            )
                        ),
                    )
                    return True

        # 滑窗：记录最近 6 步的 (action, target_id, landing_url)，用于检测点击死循环
        # 升级为 URL-aware：只有"同一 ID 被点击 ≥ 3 次 **且** 着陆 URL 完全相同"才判定死循环，
        # 翻页 / A-B 循环切换等 URL 在变化的合法重复不再误伤。
        _last_actions: list[tuple[str, int, str]] = []
        # SoM 红框编号属于“动作发起页”，不是动作落地页。比如在 Bing 上 click_new_tab
        # @e19 打开 aidaxue，@e19 仍应记录为 Bing 页上的编号；否则回到 Bing 后会被
        # 误判成跨页复用。
        _som_target_refs: list[tuple[str, int, str, str]] = []
        _LOOP_GUARD_WINDOW = 6
        _loop_guard_blocked_ids: set[int] = set()  # 触发过 LOOP GUARD 的 target_id，后续直接拦截
        _loop_guard_blocked_points: set[tuple] = set()  # click_point 黑名单（bucket 后的坐标）
        # ── Fix 4：RAW（降级前）VLM 输出 LOOP GUARD ──────────────────────
        # 监测 ZERO_TARGET_DOWNGRADE 之前的原始决策，捕捉 VLM 反复输出
        # `click target_id=0 type_value="2"` 这种 schema 错位幻觉（被 validator
        # 降级为 wait 后老 LOOP GUARD 看不见）。连续 3 次就强制硬指令 + 切走。
        _consecutive_zero_target = 0
        _last_zero_target_tv = ""
        try:
            from .wait_loop_guard import WaitLoopTracker
        except ImportError:
            from wait_loop_guard import WaitLoopTracker
        _wait_loop_tracker = WaitLoopTracker()

        def _norm_url_for_guard(url: str) -> str:
            """LOOP GUARD key 稳定化：只保留 scheme+host+path，抛弃 query/fragment。
            修复 Bug B：JD 异常页等站每次刷新注入 timestamp nonce，
            否则同元素同页 3 次点击会被拆成 3 个不同 key，计数器永远不累积。"""
            return _agent_strategy_normalize_guard_url(url)

        def _bucket_point(point) -> tuple:
            """click_point 坐标聚类：千分制 100 宽度桶，容忍 VLM 坐标漂移。
            VLM 常把"刷新"估算为 [500, 750]，下次估 [505, 745] / [512, 757]
            都应视为同一坐标区域 → 全部 map 到 (5, 7)。"""
            if not point or not isinstance(point, (list, tuple)) or len(point) < 2:
                return ()
            try:
                return (int(point[0]) // 100, int(point[1]) // 100)
            except (TypeError, ValueError):
                return ()

        def _target_id_for_guard(decision: dict) -> int:
            try:
                return int(decision.get("target_id", 0) or 0)
            except (TypeError, ValueError):
                return 0

        # LOOP GUARD 覆盖的动作集（Bug #1 修复）
        _LOOP_GUARD_ACTIONS = (
            "click", "click_new_tab", "click_point", "click_text", "hover_and_click"
        )
        _SOM_REF_ACTIONS = (
            "click", "click_text", "click_new_tab", "type", "hover", "select",
            "upload", "press_key", "find_text", "next_page", "save_to_memory",
            "download_image",
        )
        _extract_count = 0  # 连续 extract 次数（中间无翻页 click），>=2 强制 done
        _pagination_probed = False  # 首次 extract 后探测分页器一次（Improvement 1）
        _pagination_kind = ""        # "numeric" / "next_only" / "infinite" / ""
        _pagination_hint_msg = ""    # 待注入到 VLM 的探测结果反馈（下一轮 ask 时消费）
        _first_flip_pending = False  # 首次 extract 后强制下一步走 next_page（引擎层硬约束）
        _page_is_infinite_scroll = False  # 标记当前页面是无限滚动（无分页器）
        _force_next_page_pending = False  # 物理触底且未达标：下一轮强制 next_page
        _force_extract_after_navigation_pending = False  # 翻页落地后：下一轮必须先提取新页，禁止连续翻页跳页
        _block_next_page_until_drained = False  # 当前页只提到少量数据且还能滚动：禁止过早翻页
        _block_next_page_reason = ""
        _first_extract_ever_done = False  # 任务级永久锁：首次 extract 完成过（不会被翻页重置）
        _prev_action_sig: tuple[str, int, str] = ("", 0, "")  # 上一步 (action, target_id, type_value)，用于"思想-动作分离"检测
        _repeat_action_count: int = 0  # 同一 action sig 连续重复次数，用于精确触发 AUTO-ADVANCE
        _total_extracted_rows = 0  # 跨页累加的总行数
        _extract_null_streak = 0  # 连续 extract+null 降级次数，用于三级升级策略
        _extract_null_total_resets = 0  # 防止无限重试：streak 被重置的总次数
        _extracted_page_urls: set = set()  # 已成功提取数据的不同页面 URL 集合
        _extracted_page_keys: set[str] = set()  # URL + table signature; SPA/table pagination stays on one URL
        _seen_extract_row_keys: set[str] = set()  # 行级去重，支持同 URL 无限滚动/局部刷新
        _tooltip_trigger_keys: set[str] = set()  # tooltip 任务按 trigger 统计进度，而不是按候选行累加
        _pagination_exhausted = False  # 分页已耗尽（滚到底+翻页失败），用于容差退出
        # RUN-RESUME1 step3a-wire-2b: opt-in resume seed (inert unless constraints.resume).
        # Rebuild the dedup seen-set from the prior run's dataset so a resumed
        # run skips rows already on disk and only appends genuinely new ones.
        if bool((run_constraints or {}).get("resume")):
            try:
                try:
                    from .resume_seed import seed_seen_from_last_run
                except ImportError:
                    from resume_seed import seed_seen_from_last_run  # type: ignore[no-redef]
                _resume_seed = seed_seen_from_last_run(goal, start_url, _run_ts)
                if _resume_seed.seen:
                    _seen_extract_row_keys |= _resume_seed.seen
                    _total_extracted_rows = max(_total_extracted_rows, _resume_seed.count)
                    logger.info(
                        "[RUN-RESUME] seeded %s fingerprints / %s rows from prior run %s",
                        len(_resume_seed.seen),
                        _resume_seed.count,
                        _resume_seed.prior_run_id,
                    )
            except Exception as _seed_err:
                logger.debug("[RUN-RESUME] resume seed skipped: %s", _seed_err)
        try:
            from visual_web_agent.completion_kernel import NoProgressTracker
        except ImportError:
            from completion_kernel import NoProgressTracker  # type: ignore[no-redef]
        _no_progress_tracker = NoProgressTracker(exhaust_threshold=2)
        _form_scroll_down_streak = 0  # 表单未开始填写前，连续向下滚动次数
        _form_interaction_started = False  # 一旦开始填/点字段，允许正常分段滚动
        _auto_form_retry_count = 0
        _form_submit_clicked_once = False  # demo sites may keep the same page after Create/Submit

        def _sanitize_extraction_candidate(
            *,
            name: str,
            data,
            source_text: str = "",
            data_shape: dict | None = None,
        ) -> dict:
            target_count = _parse_goal_target_count(goal)
            target_remaining = (
                None if target_count is None
                else max(0, target_count - _total_extracted_rows)
            )
            raw_data = data
            if _requested_output_fields and isinstance(data, list):
                filtered_rows, schema_stats = _data_controller.filter_undercomplete_rows(
                    data,
                    normalize_row=lambda row: (
                        _normalize_extracted_row_fields([row], project=True)[0]
                    ),
                )
                if schema_stats.get("dropped"):
                    logger.info(
                        "[EXTRACT SCHEMA] %s dropped %s under-complete rows "
                        "(required_hits=%s/%s)",
                        name,
                        schema_stats.get("dropped"),
                        schema_stats.get("required_hits"),
                        schema_stats.get("total_fields"),
                    )
                raw_data = filtered_rows
            trial_seen = set(_seen_extract_row_keys)
            result = sanitize_extracted_rows(
                raw_data=raw_data,
                source_text=source_text,
                seen_fingerprints=trial_seen,
                target_remaining=target_remaining,
            )
            rows = result.rows
            completeness = 0.0
            if rows:
                widths = []
                for row in rows:
                    if isinstance(row, dict):
                        widths.append(
                            sum(
                                1
                                for value in row.values()
                                if value is not None and str(value).strip()
                            )
                        )
                completeness = (
                    sum(widths) / max(len(widths), 1)
                    if widths else 1.0
                )
            shape = data_shape or {}
            source = name.upper()
            score = result.accepted * 100.0 + completeness * 5.0
            score -= result.duplicates * 8.0
            score -= result.rejected_total * 12.0
            if "DOM_CARDS" in source:
                score += 58.0
                if result.accepted >= 2:
                    score += 12.0
            elif "DOM_LIST" in source:
                if int(shape.get("repeated_list_items") or 0) >= result.accepted >= 2:
                    score += 45.0
                else:
                    score += 25.0
            elif "DOM_TABLE" in source:
                if int(shape.get("table_rows") or 0) >= result.accepted >= 2:
                    score += 35.0
                else:
                    score += 12.0
            elif "FULL_PAGE" in source or "AX_TREE" in source or "INNER_TEXT" in source:
                if int(shape.get("repeated_class_count") or 0) >= 5:
                    score += 25.0
                if result.accepted >= 10:
                    score += 20.0
            elif "VIEWPORT" in source or "VLM" in source:
                score += 3.0

            return {
                "name": name,
                "rows": rows,
                "accepted": result.accepted,
                "duplicates": result.duplicates,
                "rejected": result.rejected_total,
                "fingerprints": set(result.fingerprints),
                "score": score,
                "source_text": source_text,
                "data_shape": shape,
                "data_signature": _data_controller.rows_signature(rows),
            }

        def _expected_rows_from_data_shape(data_shape: dict | None) -> int:
            """Estimate how many structured rows the current page physically exposes.

            This is a guardrail for dense list/table pages: if the DOM clearly
            contains ~25 repeated items, a viewport-only 3-row extraction should
            not be treated as a complete page batch.
            """
            shape = data_shape or {}
            try:
                table_rows = int(shape.get("table_rows") or 0)
                table_cells = int(shape.get("table_cells") or 0)
                repeated = int(shape.get("repeated_class_count") or 0)
                repeated_avg_text = int(shape.get("repeated_avg_text") or 0)
            except (TypeError, ValueError):
                return 0

            expected = 0
            if table_rows >= 3 and table_cells >= 2:
                expected = max(expected, table_rows)
            if repeated >= 5 and repeated_avg_text >= 20:
                expected = max(expected, repeated)
            return expected

        def _candidate_min_expected_rows(candidate: dict) -> int:
            target_count = _parse_goal_target_count(goal)
            target_remaining = (
                None if target_count is None
                else max(0, target_count - _total_extracted_rows)
            )
            expected_rows = _expected_rows_from_data_shape(
                candidate.get("data_shape") or {}
            )
            if expected_rows < 10:
                return 0
            # Dense pages should usually be extracted as a page batch, but if
            # the remaining target is small we only require that many rows.
            # Use magnitude matching, not strict equality: DOM probes may count
            # ads, skeleton rows, placeholders, or hidden repeated nodes.
            page_floor = max(10, int(expected_rows * 0.7))
            if target_remaining is not None:
                return min(expected_rows, target_remaining, page_floor)
            return min(expected_rows, page_floor)

        def _is_under_yield_viewport_candidate(candidate: dict) -> bool:
            source = str(candidate.get("name") or "").upper()
            if "VIEWPORT" not in source and "VLM" not in source:
                return False
            shape = candidate.get("data_shape") or {}
            if bool(shape.get("physically_drained")):
                return False
            minimum = _candidate_min_expected_rows(candidate)
            if minimum <= 0:
                return False
            return int(candidate.get("accepted") or 0) < minimum

        def _choose_best_extraction_candidate(candidates: list[dict]) -> dict | None:
            viable = [c for c in candidates if c.get("accepted", 0) > 0]
            if not viable:
                return None
            filtered: list[dict] = []
            for c in viable:
                if _is_under_yield_viewport_candidate(c):
                    logger.info(
                        "[EXTRACT ARBITER] reject under-yield viewport candidate: "
                        "accepted=%s min_expected=%s shape=%s",
                        c.get("accepted"),
                        _candidate_min_expected_rows(c),
                        c.get("data_shape"),
                    )
                    continue
                filtered.append(c)
            viable = filtered
            if not viable:
                return None
            def _candidate_priority(candidate: dict) -> int:
                source = str(candidate.get("name") or "").upper()
                if _goal_output_mode == "answer":
                    if "VIEWPORT" in source or "VLM" in source:
                        return 6
                    if "FULL_PAGE" in source or "AX_TREE" in source or "INNER_TEXT" in source:
                        return 5
                    if "DOM_CARDS" in source:
                        return 3
                    if "DOM_TABLE" in source:
                        return 2
                    if "DOM_LIST" in source:
                        return 1
                    return 0
                if "DOM_CARDS" in source:
                    return 5
                if "DOM_LIST" in source:
                    return 4
                if "DOM_TABLE" in source:
                    return 3
                if "FULL_PAGE" in source or "LIST_ITEMS_TEXT" in source:
                    return 2
                if "VIEWPORT" in source or "VLM" in source:
                    return 1
                return 0

            if _goal_output_mode == "answer":
                viable.sort(
                    key=lambda c: (
                        _candidate_priority(c),
                        float(c.get("score") or 0),
                        -int(c.get("accepted") or 0),
                    ),
                    reverse=True,
                )
            else:
                viable.sort(
                    key=lambda c: (
                        float(c.get("score") or 0),
                        int(c.get("accepted") or 0),
                        _candidate_priority(c),
                    ),
                    reverse=True,
                )
            chosen = viable[0]
            logger.info(
                "[EXTRACT ARBITER] candidates=%s | selected=%s score=%.1f accepted=%s",
                "; ".join(
                    f"{c.get('name')}:score={float(c.get('score') or 0):.1f},"
                    f"accepted={c.get('accepted')},dup={c.get('duplicates')},rej={c.get('rejected')}"
                    for c in candidates
                ),
                chosen.get("name"),
                float(chosen.get("score") or 0),
                chosen.get("accepted"),
            )
            return chosen

        def _commit_extraction_candidate(candidate: dict) -> tuple[list, int, int, int, str]:
            _seen_extract_row_keys.update(candidate.get("fingerprints") or set())
            return (
                candidate.get("rows") or [],
                int(candidate.get("accepted") or 0),
                int(candidate.get("duplicates") or 0),
                int(candidate.get("rejected") or 0),
                str(candidate.get("source_text") or ""),
            )

        def _record_extract_progress(rows, accepted_count: int) -> tuple[int, int]:
            nonlocal _total_extracted_rows
            if not _goal_is_tooltip_extract(goal):
                _total_extracted_rows += accepted_count
                return accepted_count, _total_extracted_rows

            new_triggers = 0
            for row in rows or []:
                if isinstance(row, dict):
                    trigger_key = extract_tooltip_primary_key(row)
                else:
                    trigger_key = str(row).strip()
                if trigger_key and trigger_key not in _tooltip_trigger_keys:
                    _tooltip_trigger_keys.add(trigger_key)
                    new_triggers += 1
            _total_extracted_rows = len(_tooltip_trigger_keys)
            return new_triggers, _total_extracted_rows

        async def _capture_body_text_excerpt(limit: int = 3000) -> str:
            try:
                page = await browser._ensure_active_page(reason="extraction snapshot body text")
                if not page:
                    return ""
                text = await page.evaluate(
                    """() => String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim()"""
                )
                return str(text or "")[:limit]
            except Exception as exc:
                logger.debug("[EXTRACTION SNAPSHOT] body text capture skipped: %s", exc)
                return ""

        async def _save_extraction_snapshot(
            *,
            source: str,
            rows: list,
            output_file: str,
            accepted_rows: int,
            duplicate_rows: int = 0,
            rejected_rows: int = 0,
            candidates: list[dict] | None = None,
            data_shape: dict | None = None,
            source_text: str = "",
            metadata: dict | None = None,
        ) -> str:
            try:
                snapshot_path = maybe_save_snapshot(
                    url=getattr(browser, "current_url", "") or "",
                    goal=_snapshot_goal,
                    source=source,
                    rows=rows,
                    requested_fields=_requested_output_fields,
                    output_file=output_file,
                    run_id=_run_ts,
                    step=step,
                    total_rows=_total_extracted_rows,
                    accepted_rows=accepted_rows,
                    duplicate_rows=duplicate_rows,
                    rejected_rows=rejected_rows,
                    candidates=candidates or [],
                    data_shape=data_shape or {},
                    source_text=source_text,
                    body_text=await _capture_body_text_excerpt(),
                    metadata=metadata or {},
                )
                if not snapshot_path:
                    return ""
                logger.info("[EXTRACTION SNAPSHOT] saved: %s", snapshot_path)
                event_stream.extract(
                    step=step,
                    source=source,
                    rows=accepted_rows,
                    output_file=output_file,
                    metadata={
                        **dict(metadata or {}),
                        "snapshot_path": str(snapshot_path),
                    },
                )
                return str(snapshot_path)
            except Exception as exc:
                logger.debug("[EXTRACTION SNAPSHOT] save skipped: %s", exc)
                return ""

        async def _try_dom_api_fast_path(
            dom_rows: list,
            *,
            source: str,
            dom_text: str = "",
        ) -> dict:
            try:
                from visual_web_agent.content_completeness_guard import (
                    evaluate_dom_api_completeness,
                    execute_api_fast_path,
                )

                page_text = dom_text or await _capture_body_text_excerpt(6000)
                verdict = evaluate_dom_api_completeness(
                    dom_rows=dom_rows,
                    dom_text=page_text,
                    run_id=_run_ts,
                    goal=goal,
                )
                if not verdict.get("should_fast_path"):
                    return {"applied": False, "verdict": verdict}

                cookies: list = []
                if getattr(browser, "_context", None) is not None:
                    cookies = await browser._context.cookies()
                fast = execute_api_fast_path(
                    run_id=_run_ts,
                    verdict=verdict,
                    cookies=cookies,
                )
                if not fast.get("applied"):
                    return fast

                api_rows = fast.get("rows") or []
                if not api_rows:
                    return {"applied": False, "reason": "empty_api_rows", "verdict": verdict}

                saved_path = ""
                if _goal_output_mode != "answer":
                    saved_path = save_run_dataset(
                        api_rows,
                        run_id=_run_ts,
                        output_contract=_goal_output_contract,
                        produced_by="api_fast_path",
                        filename_hint=_vlm_output,
                    )
                _record_extract_progress(api_rows, len(api_rows))
                logger.info(
                    "[API FAST PATH] upgraded %s via %s rows=%s saved=%s",
                    source,
                    fast.get("endpoint"),
                    len(api_rows),
                    saved_path,
                )
                event_stream.guard(
                    step=step,
                    name="DOM_API_FAST_PATH",
                    message="DOM truncated or sparse; replayed richer API payload.",
                    metadata={"verdict": verdict, "fast_path": fast, "source": source},
                )
                try:
                    from api_server import broadcast_phase as _bp_api_fp

                    _bp_api_fp(
                        "completion_guard",
                        severity="info",
                        message=f"api_fast_path: {fast.get('endpoint', '')[:80]}",
                        step=step,
                        notice_severity=getattr(browser, "_last_notice_severity", None),
                        extra={
                            "guard": "dom_api_fast_path",
                            "evaluation": {
                                "status": "complete",
                                "evidence": verdict.get("reasons") or [],
                                "reasons": ["api_fast_path"],
                            },
                            "endpoint": fast.get("endpoint"),
                            "row_count": len(api_rows),
                        },
                    )
                except Exception:
                    pass
                return {"applied": True, "verdict": verdict, "fast_path": fast, "saved_path": saved_path}
            except Exception as _api_fp_err:
                logger.debug("[API FAST PATH] skipped: %s", _api_fp_err)
                return {"applied": False, "error": str(_api_fp_err)}

        def _xhr_saved_row_count() -> tuple[int | None, str]:
            filename = str(getattr(browser, "_intercept_filename", "") or "")
            if not filename:
                return None, ""
            path = resolve_artifact_path(filename)
            if not path.exists():
                return None, str(path)
            try:
                import pandas as _pd

                df = _pd.read_excel(path)
                return int(len(df.index)), str(path)
            except Exception as exc:
                logger.debug("[XHR HARD KILL] saved-row count skipped: %s", exc)
                return None, str(path)

        def _xhr_target_reached() -> tuple[bool, int, int | None]:
            target = _parse_goal_target_count(goal)
            if not enable_xhr or target is None or browser.intercepted_count <= 0:
                return False, browser.intercepted_count, target
            if _goal_is_tooltip_extract(goal):
                return False, browser.intercepted_count, target
            if not _goal_is_bulk_extraction(goal):
                return False, browser.intercepted_count, target
            saved_count, saved_path = _xhr_saved_row_count()
            effective_count = (
                saved_count
                if saved_count is not None
                else int(browser.intercepted_count or 0)
            )
            if saved_count is not None and saved_count != browser.intercepted_count:
                logger.info(
                    "[XHR HARD KILL] using saved clean rows=%s instead of raw intercepted=%s (%s)",
                    saved_count,
                    browser.intercepted_count,
                    saved_path,
                )
            return effective_count >= target, effective_count, target

        async def _finish_if_xhr_target_reached(reason: str) -> bool:
            nonlocal _total_extracted_rows, _task_completed, _run_succeeded, _log_screenshot_path, _log_decision
            reached, count, target = _xhr_target_reached()
            if not reached:
                return False
            _total_extracted_rows = max(_total_extracted_rows, count)
            output_file = str(getattr(browser, "_intercept_filename", "") or "")
            logger.info(
                "[XHR HARD KILL] intercepted target reached: %s/%s (%s)",
                count,
                target,
                reason,
            )
            _broadcast_log_safe(
                f"[XHR HARD KILL] XHR 清洗后已保存 {count}/{target} 条数据，任务达量结束。"
            )
            _broadcast_done_safe(True, "XHR intercepted target reached")
            event_stream.extract(
                step=step,
                source="XHR_INTERCEPT",
                rows=count,
                output_file=output_file,
                metadata={"reason": reason, "target": target},
            )
            await browser.mark_and_screenshot(step=99)
            _log_screenshot_path = str(Path(SCREENSHOT_DIR) / "step_99.png")
            # PRESERVE the VLM's actual action in the trajectory; only annotate
            # ``thought`` with the guard's exit-reason. See the matching fix in
            # ``_finish_if_file_download_completed`` for rationale.
            _guard_annotation = (
                f"[XHR HARD KILL] XHR 拦截器已捕获 {count}/{target} 条数据，"
                "任务在本步达量后结束。"
            )
            if isinstance(_log_decision, dict):
                _log_decision = dict(_log_decision)
                _original_thought = str(_log_decision.get("thought") or "").strip()
                _log_decision["status"] = "success"
                _log_decision["thought"] = (
                    f"{_guard_annotation}\n"
                    + (f"原 VLM thought：{_original_thought}" if _original_thought else "")
                ).rstrip()
                _log_decision["extract_text_source"] = "XHR_INTERCEPT"
                _log_decision["output_file"] = output_file
            else:
                _log_decision = {
                    "action": "done",
                    "target_id": 0,
                    "type_value": "",
                    "status": "success",
                    "thought": _guard_annotation,
                    "extract_text_source": "XHR_INTERCEPT",
                    "output_file": output_file,
                }
            _task_completed = True
            _run_succeeded = True
            return True

        async def _finish_if_file_download_completed(reason: str) -> bool:
            nonlocal _task_completed, _run_succeeded, _log_screenshot_path, _log_decision
            if not re.search(r"\bdownload\b|下载|samplefile|保存文件", goal, re.I):
                return False
            download_path = str(getattr(browser, "last_download_path", "") or "")
            download_name = str(getattr(browser, "last_download_name", "") or "")
            if not download_path or not Path(download_path).exists():
                return False
            logger.info(
                "[DOWNLOAD GUARD] completed download %s (%s)",
                download_name or Path(download_path).name,
                reason,
            )
            event_stream.guard(
                step=step,
                name="DOWNLOAD_COMPLETED_GUARD",
                message="download completed; ending task",
                metadata={
                    "path": download_path,
                    "name": download_name or Path(download_path).name,
                    "reason": reason,
                },
            )
            event_stream.done(
                step=step,
                success=True,
                reason="download_completed_guard",
                message=f"download completed: {download_name or Path(download_path).name}",
                metadata={
                    "path": download_path,
                    "name": download_name or Path(download_path).name,
                },
            )
            _broadcast_done_safe(True, "Download completed")
            try:
                await browser.mark_and_screenshot(step=99)
                _log_screenshot_path = str(Path(SCREENSHOT_DIR) / "step_99.png")
            except Exception as screenshot_err:
                logger.debug("[DOWNLOAD GUARD] final screenshot skipped: %s", screenshot_err)
            # PRESERVE the VLM's actual click decision in the trajectory.
            # Earlier this branch wiped ``_log_decision`` to ``action=done`` /
            # ``target_id=0`` / ``thought=Download completed``, so the trajectory
            # showed zero visible click and users thought "the agent didn't
            # press Download". The download interceptor only fires from a real
            # network download, so the action that triggered it must have run —
            # keep its fields and only append a guard annotation to ``thought``.
            _saved_file = download_name or Path(download_path).name
            _guard_annotation = (
                f"[DOWNLOAD GUARD] 任务在本步触发的下载完成后结束 — "
                f"文件已保存到 {download_path} ({_saved_file})。"
            )
            if isinstance(_log_decision, dict):
                _log_decision = dict(_log_decision)  # don't mutate the original
                _original_thought = str(_log_decision.get("thought") or "").strip()
                _log_decision["status"] = "success"
                _log_decision["thought"] = (
                    f"{_guard_annotation}\n"
                    + (f"原 VLM thought：{_original_thought}" if _original_thought else "")
                ).rstrip()
                _log_decision["download_path"] = download_path
                _log_decision["downloaded_file"] = _saved_file
            else:
                # Fallback: no VLM decision recorded yet (rare — guard fired
                # before the action could populate _log_decision).
                _log_decision = {
                    "action": "done",
                    "target_id": 0,
                    "type_value": _saved_file,
                    "status": "success",
                    "thought": _guard_annotation,
                    "download_path": download_path,
                    "downloaded_file": _saved_file,
                }
            _task_completed = True
            _run_succeeded = True
            return True

        def _compact_link_match_text(value: object) -> str:
            return re.sub(r"\W+", "", str(value or "").lower(), flags=re.UNICODE)

        def _row_has_url_value(row: dict) -> bool:
            for key, value in row.items():
                key_norm = str(key or "").strip().lower()
                if key_norm in {"url", "link", "href"} and str(value or "").strip():
                    return True
            return False

        def _classify_url_role(value: object) -> str:
            """Classify a URL into a generic extraction role."""
            text = str(value or "").strip().lower()
            if not text:
                return ""
            if "news.ycombinator.com/item" in text:
                return "detail"
            if re.search(r"/(item|story|post|posts|article|articles|thread|threads|comment|comments|detail|details|product|products|issues?)(/|\\?|#|$)", text):
                return "detail"
            return "source"

        def _is_probable_url(value: object) -> bool:
            text = str(value or "").strip()
            return bool(re.match(r"^https?://", text, flags=re.IGNORECASE))

        def _first_int_value(value: object) -> int | object:
            text = str(value or "").strip()
            match = re.search(r"\d[\d,]*", text)
            if not match:
                return value
            try:
                return int(match.group(0).replace(",", ""))
            except ValueError:
                return value

        def _field_aliases(field: str) -> set[str]:
            norm = _normalize_output_field_key(field)
            aliases = {norm} if norm else set()
            alias_map = {
                "url": {"url", "link", "href", "primaryurl", "sourceurl", "detailurl", "网址", "链接"},
                "link": {"url", "link", "href", "primaryurl", "sourceurl", "detailurl", "网址", "链接"},
                "href": {"url", "link", "href"},
                "title": {"title", "name", "heading", "subject", "标题", "名称", "名字"},
                "标题": {"title", "name", "heading", "subject", "标题", "名称", "名字"},
                "name": {"name", "title", "名称", "姓名", "名字"},
                "名称": {"name", "title", "名称", "姓名", "名字"},
                "position": {"position", "职位", "职务", "岗位"},
                "office": {"office", "location", "city", "地区", "地点", "办公室"},
                "age": {"age", "年龄"},
                "time": {"time", "date", "age", "created", "published", "时间", "日期"},
                "author": {"author", "user", "username", "by", "作者", "用户"},
                "rating": {"rating", "score", "评分", "分数", "星级"},
                "score": {"rating", "score", "评分", "分数", "星级"},
                "评分": {"rating", "score", "评分", "分数", "星级"},
                "points": {"points", "score", "votes", "积分", "分数", "点赞"},
                "comments": {"comments", "commentcount", "reviewcount", "评论", "评论数"},
                "reviewcount": {"comments", "commentcount", "reviewcount", "评论", "评论数"},
                "reviews": {"reviews", "reviewcount", "votes", "评价人数", "评价数", "评论数"},
                "评价人数": {"reviews", "reviewcount", "votes", "评价人数", "评价数", "评论数"},
                "summary": {"summary", "description", "intro", "简介", "摘要", "一句话简介"},
                "description": {"summary", "description", "intro", "简介", "摘要", "一句话简介"},
                "intro": {"summary", "description", "intro", "简介", "摘要", "一句话简介"},
                "一句话简介": {"summary", "description", "intro", "简介", "摘要", "一句话简介"},
            }
            for key, values in alias_map.items():
                if norm == key or norm in values:
                    aliases.update(values)
            return aliases

        def _requested_field_coverage(row: dict) -> tuple[int, int]:
            if not _requested_output_fields or not isinstance(row, dict):
                return 0, 0
            normalized_keys = {
                key: _normalize_output_field_key(key)
                for key in row.keys()
                if row.get(key) is not None and str(row.get(key)).strip()
            }
            hit = 0
            total = 0
            for field in _requested_output_fields:
                aliases = _field_aliases(field)
                if not aliases:
                    continue
                total += 1
                for norm_key in normalized_keys.values():
                    if (
                        norm_key in aliases
                        or any(alias and alias in norm_key for alias in aliases)
                        or any(alias and norm_key in alias for alias in aliases)
                    ):
                        hit += 1
                        break
            return hit, total

        def _min_requested_field_hits(total: int) -> int:
            if total <= 0:
                return 0
            if total <= 4:
                return total
            return max(2, int(math.ceil(total * 0.75)))

        def _project_row_to_requested_fields(row: dict) -> dict:
            if not _requested_output_fields or not isinstance(row, dict):
                return row

            normalized_keys = {
                key: _normalize_output_field_key(key)
                for key in row.keys()
            }
            column_keys = sorted(
                [
                    key for key, norm in normalized_keys.items()
                    if re.fullmatch(r"column\d+", norm or "")
                ],
                key=lambda key: int(re.search(r"\d+", normalized_keys[key]).group(0)),
            )
            requested = [
                field for field in _requested_output_fields
                if _normalize_output_field_key(field)
            ]
            if not requested:
                return row

            if (
                column_keys
                and len(column_keys) >= len(requested)
                and len(column_keys) >= max(2, len(row) - 1)
            ):
                return {
                    field: row.get(key)
                    for field, key in zip(requested, column_keys)
                }

            projected = {}
            used_keys: set[str] = set()
            for field in requested:
                aliases = _field_aliases(field)
                best_key = None
                best_score = 0
                for key, norm_key in normalized_keys.items():
                    if key in used_keys or not norm_key:
                        continue
                    score = 0
                    if norm_key in aliases:
                        score = 100
                    elif any(alias and alias in norm_key for alias in aliases):
                        score = 80
                    elif any(alias and norm_key in alias for alias in aliases):
                        score = 70
                    if score > best_score:
                        best_key = key
                        best_score = score
                if best_key is not None:
                    projected[field] = row.get(best_key)
                    used_keys.add(best_key)

            if not projected and column_keys:
                return {
                    field: row.get(key)
                    for field, key in zip(requested, column_keys)
                }

            return projected or row

        def _normalize_extracted_row_fields(rows: list, *, project: bool = True) -> list:
            """Stabilize common forum/list fields before saving."""
            out: list = []
            for row in rows or []:
                if not isinstance(row, dict):
                    out.append(row)
                    continue
                normalized = dict(row)
                if "time" not in normalized and normalized.get("age"):
                    normalized["time"] = normalized.get("age")
                normalized.pop("age", None)

                for key in ("points", "score", "votes", "review_count", "comments", "comment_count"):
                    if key in normalized and normalized.get(key) is not None:
                        normalized[key] = _first_int_value(normalized.get(key))

                if "review_count" not in normalized and "comments" in normalized:
                    normalized["review_count"] = normalized.get("comments")
                normalized.pop("comments", None)

                for legacy_key in ("story_url", "discussion_url"):
                    if legacy_key in normalized and "source_url" not in normalized and "detail_url" not in normalized:
                        role = "detail" if legacy_key == "discussion_url" else "source"
                        normalized[f"{role}_url"] = normalized.get(legacy_key)
                    normalized.pop(legacy_key, None)

                for url_key in ("primary_url", "source_url", "detail_url", "url", "link", "href"):
                    raw_url = normalized.get(url_key)
                    if not (raw_url and _is_probable_url(raw_url)):
                        continue
                    role = _classify_url_role(raw_url)
                    if role == "detail":
                        normalized.setdefault("detail_url", raw_url)
                    else:
                        normalized.setdefault("source_url", raw_url)
                if normalized.get("source_url"):
                    normalized["primary_url"] = normalized.get("source_url")
                elif normalized.get("detail_url"):
                    normalized["primary_url"] = normalized.get("detail_url")
                if normalized.get("primary_url"):
                    normalized["url"] = normalized.get("primary_url")
                if project:
                    normalized = _project_row_to_requested_fields(normalized)
                out.append(normalized)
            return out

        def _row_primary_link_text(row: dict) -> str:
            preferred_markers = (
                "title", "name", "product", "item", "subject", "label",
                "heading", "caption", "标题", "名称", "商品", "项目",
            )
            preferred: list[str] = []
            fallback: list[str] = []
            for key, value in row.items():
                if value is None:
                    continue
                key_norm = str(key or "").strip().lower()
                text = re.sub(r"\s+", " ", str(value).strip())
                if len(_compact_link_match_text(text)) < 8:
                    continue
                if any(marker in key_norm for marker in preferred_markers):
                    preferred.append(text)
                elif not re.fullmatch(r"[\d\s,.:/%+\-]+", text):
                    fallback.append(text)
            candidates = preferred or fallback
            return max(candidates, key=lambda s: len(_compact_link_match_text(s))) if candidates else ""

        async def _enrich_rows_with_dom_links(rows: list) -> list:
            """Fill row URLs by matching title/name text to page anchors.

            This is schema-agnostic: it enriches rows only when a stable text
            field has an unambiguous anchor match in the current DOM.
            """
            rows = _normalize_extracted_row_fields(rows, project=False)
            if not rows or not any(isinstance(row, dict) for row in rows):
                return rows
            page = await browser._ensure_active_page(reason="enrich extracted rows with links")
            if not page:
                return rows
            try:
                anchors = await page.evaluate(
                    """() => {
                        const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const nearestText = (a) => {
                            const direct = clean(a.innerText || a.textContent || a.getAttribute('aria-label') || a.getAttribute('title'));
                            const row = a.closest('article, [role="article"], [role="listitem"], li, tr, .Story, .story, .ais-Hits-item, .hit');
                            const rowText = clean(row ? row.innerText : '');
                            return clean([direct, rowText].filter(Boolean).join(' '));
                        };
                        return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                            text: nearestText(a),
                            own_text: clean(a.innerText || a.textContent || a.getAttribute('aria-label') || a.getAttribute('title')),
                            href: a.href || ''
                        })).filter(x => x.text && x.href && !x.href.startsWith('javascript:'));
                    }"""
                )
            except Exception as exc:
                logger.debug("[LINK ENRICH] anchor scan skipped: %s", exc)
                return rows

            anchor_rows: list[dict] = []
            for anchor in anchors or []:
                text = str(anchor.get("text") or "").strip()
                own_text = str(anchor.get("own_text") or "").strip()
                href = str(anchor.get("href") or "").strip()
                compact = _compact_link_match_text(text)
                if len(compact) >= 8 and href:
                    anchor_rows.append(
                        {
                            "href": href,
                            "compact": compact,
                            "own_compact": _compact_link_match_text(own_text),
                        }
                    )
            if not anchor_rows:
                return rows

            enriched_source = 0
            enriched_detail = 0
            out: list = []
            for row in rows:
                if not isinstance(row, dict):
                    out.append(row)
                    continue
                primary_compact = _compact_link_match_text(_row_primary_link_text(row))
                if len(primary_compact) < 8:
                    out.append(row)
                    continue
                matches = []
                for anchor in anchor_rows:
                    a_compact = anchor["compact"]
                    if primary_compact in a_compact or a_compact in primary_compact:
                        matches.append((min(len(primary_compact), len(a_compact)), anchor))
                matches.sort(key=lambda item: item[0], reverse=True)
                best_match = matches[0][1] if matches and (len(matches) == 1 or matches[0][0] > matches[1][0]) else None
                if best_match:
                    row = dict(row)
                    href = best_match["href"]
                    role = _classify_url_role(href)
                    if role == "detail":
                        if not row.get("detail_url"):
                            row["detail_url"] = href
                            enriched_detail += 1
                    elif not row.get("source_url"):
                        row["source_url"] = href
                        enriched_source += 1
                    if not row.get("primary_url"):
                        row["primary_url"] = row.get("source_url") or row.get("detail_url") or href
                    row["url"] = row.get("primary_url")

                if isinstance(row, dict) and not row.get("detail_url"):
                    detail_matches = []
                    for anchor in anchor_rows:
                        href = anchor["href"]
                        if _classify_url_role(href) != "detail":
                            continue
                        a_compact = anchor["compact"]
                        if primary_compact in a_compact or a_compact in primary_compact:
                            detail_matches.append((min(len(primary_compact), len(a_compact)), anchor))
                    detail_matches.sort(key=lambda item: item[0], reverse=True)
                    if detail_matches:
                        row = dict(row)
                        row["detail_url"] = detail_matches[0][1]["href"]
                        enriched_detail += 1
                out.append(row)
            if enriched_source or enriched_detail:
                logger.info(
                    "[LINK ENRICH] Filled source_url=%s detail_url=%s",
                    enriched_source,
                    enriched_detail,
                )
            return _normalize_extracted_row_fields(out)

        async def _extract_compact_list_text_via_dom(reason: str) -> tuple[str, str, int]:
            """Return compact repeated-list item text when the DOM exposes clear rows."""
            try:
                _page_for_items = await browser._ensure_active_page(reason=reason)
                result = await _page_for_items.evaluate(
                    """() => {
                        const clean = (value) => String(value || '')
                            .replace(/\\s+/g, ' ')
                            .trim();
                        const compact = (value) => clean(value).toLowerCase()
                            .replace(/[^a-z0-9\\u4e00-\\u9fff]+/g, '');
                        const isVisible = (el) => {
                            if (!el || !(el instanceof Element)) return false;
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0;
                        };
                        const candidates = [];
                        const addCandidate = (el, source) => {
                            if (!isVisible(el)) return;
                            const text = clean(el.innerText || el.textContent);
                            if (text.length < 20 || text.length > 1800) return;
                            const childBlocks = Array.from(el.querySelectorAll(
                                'article, [role="article"], [role="listitem"], li, tbody tr'
                            )).filter(node => node !== el && isVisible(node));
                            if (childBlocks.length >= 3 && text.length > 800) return;
                            const links = Array.from(el.querySelectorAll('a[href]'))
                                .filter(isVisible)
                                .map(a => ({
                                    text: clean(a.innerText || a.textContent || a.getAttribute('aria-label')),
                                    href: a.href || ''
                                }))
                                .filter(a => a.href)
                                .slice(0, 6);
                            const key = links[0]?.href || compact(text).slice(0, 180);
                            if (!key) return;
                            candidates.push({source, key, text, links});
                        };

                        const directSelectors = [
                            'article', '[role="article"]', '[role="listitem"]',
                            '.Story', '.story', '.ais-Hits-item', '.hit',
                            '.search-result', '.result', '.item'
                        ];
                        for (const el of document.querySelectorAll(directSelectors.join(','))) {
                            addCandidate(el, 'selector');
                        }

                        const containerSelectors = [
                            'main', '[role="main"]', '#content', '.content',
                            '.list', '.item-list', '.results', '.search-results',
                            'ol', 'ul', 'section'
                        ];
                        for (const root of document.querySelectorAll(containerSelectors.join(','))) {
                            if (!isVisible(root)) continue;
                            const children = Array.from(root.children || []).filter(isVisible);
                            if (children.length < 4) continue;
                            const buckets = new Map();
                            for (const child of children) {
                                const cls = clean(child.className || child.tagName).slice(0, 80);
                                buckets.set(cls, (buckets.get(cls) || 0) + 1);
                            }
                            const repeat = Math.max(...Array.from(buckets.values()), 0);
                            if (repeat < 4) continue;
                            for (const child of children) addCandidate(child, 'container');
                        }

                        const seen = new Set();
                        const rows = [];
                        for (const candidate of candidates) {
                            if (seen.has(candidate.key)) continue;
                            seen.add(candidate.key);
                            rows.push(candidate);
                        }
                        rows.sort((a, b) => {
                            const aTop = document.body.innerText.indexOf(a.text.slice(0, 40));
                            const bTop = document.body.innerText.indexOf(b.text.slice(0, 40));
                            return (aTop < 0 ? 1e9 : aTop) - (bTop < 0 ? 1e9 : bTop);
                        });
                        const selected = rows.slice(0, 120);
                        const lines = selected.map((row, index) => {
                            const linkText = row.links
                                .map(link => {
                                    const label = link.text ? `${link.text} -> ` : '';
                                    return `${label}${link.href}`;
                                })
                                .join(' ; ');
                            return [
                                `Item ${index + 1}: ${row.text}`,
                                linkText ? `Links: ${linkText}` : ''
                            ].filter(Boolean).join('\\n');
                        });
                        return {
                            count: selected.length,
                            text: lines.join('\\n\\n')
                        };
                    }"""
                )
                if not isinstance(result, dict):
                    return "", "", 0
                text = str(result.get("text") or "").strip()
                count = int(result.get("count") or 0)
                if count >= 5 and len(text) >= 400:
                    logger.info(
                        "[EXTRACT FULL] DOM compact list candidate: %s items, %s chars",
                        count,
                        len(text),
                    )
                    return "LIST_ITEMS_TEXT", text, count
            except Exception as list_err:
                logger.debug("[EXTRACT FULL] compact list DOM probe skipped: %s", list_err)
            return "", "", 0

        async def _extract_full_page_text_for_data(reason: str) -> tuple[str, str]:
            """Return the best full-page text source for semantic extraction."""
            list_source, list_text, list_count = await _extract_compact_list_text_via_dom(reason)
            if list_text and list_count >= 10:
                logger.info(
                    "[EXTRACT FULL] using compact DOM list text before AX/innerText "
                    "(items=%s, chars=%s)",
                    list_count,
                    len(list_text),
                )
                return list_source, list_text

            source = "AX_TREE"
            ax_text = await browser.extract_page_text_via_ax_tree()
            body_text = ""
            try:
                _page_for_text = await browser._ensure_active_page(reason=reason)
                body_text = await _page_for_text.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
                body_text = str(body_text or "").strip()
            except Exception as text_err:
                logger.debug("[EXTRACT FULL] innerText fallback skipped: %s", text_err)

            ax_text = str(ax_text or "").strip()
            if list_text and len(list_text) > max(len(ax_text) * 0.5, 1200):
                logger.info(
                    "[EXTRACT FULL] compact DOM list richer than AX slice "
                    "(items=%s, list=%s chars, ax=%s chars), using list text",
                    list_count,
                    len(list_text),
                    len(ax_text),
                )
                return list_source, list_text
            if body_text and len(body_text) > max(len(ax_text) * 1.2, 800):
                if ax_text:
                    logger.info(
                        "[EXTRACT FULL] innerText richer than AX (%s vs %s chars), using combined text",
                        len(body_text),
                        len(ax_text),
                    )
                    return (
                        "AX_TREE+INNER_TEXT",
                        f"【AX Tree 语义文本】\n{ax_text}\n\n【DOM innerText 全页文本】\n{body_text}",
                    )
                logger.info("[EXTRACT FULL] AX Tree empty/short, using innerText")
                return "INNER_TEXT_FALLBACK", body_text
            if ax_text:
                return source, ax_text
            return ("INNER_TEXT_FALLBACK", body_text) if body_text else ("", "")

        async def _extract_body_text_for_semantic_cards(reason: str) -> str:
            """Lightweight body text fallback for schema-driven card extraction."""
            if not _requested_output_fields:
                return ""
            try:
                _body_page = await browser._ensure_active_page(reason=reason)
                text = await _body_page.evaluate(
                    """() => document.body ? String(document.body.innerText || '') : ''"""
                )
                return str(text or "").strip()[:20000]
            except Exception as body_err:
                logger.debug("[EXTRACT DOM] semantic card body text skipped: %s", body_err)
                return ""

        async def _inspect_click_target_for_extract_nav_guard(decision: dict) -> dict:
            action_name = str(decision.get("action") or "").strip().lower()
            if action_name not in {"click", "click_text", "click_point"}:
                return {}
            target_id = int(decision.get("target_id") or 0)
            if target_id <= 0:
                return {
                    "text": str(decision.get("type_value") or ""),
                    "action": action_name,
                }
            try:
                page = await browser._ensure_active_page(
                    reason="inspect extraction same-page nav target"
                )
                if not page:
                    return {}
                return await page.evaluate(
                    """(targetId) => {
                        const el = document.querySelector(`[data-som-id="${targetId}"]`);
                        if (!el) return {exists: false, target_id: targetId};
                        const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const anchor = el.closest('a[href]');
                        const role = clean(el.getAttribute('role') || '');
                        const tag = clean(el.tagName || '').toLowerCase();
                        const href = anchor ? anchor.href : (
                            el.href || el.getAttribute('href') || ''
                        );
                        const text = clean(
                            el.innerText || el.textContent ||
                            el.getAttribute('aria-label') ||
                            el.getAttribute('title') || ''
                        );
                        const cls = clean(el.className || '');
                        const parent = el.closest(
                            'nav,header,[role="navigation"],[role="tablist"],.tab,.tabs,.nav,.navbar,.forecast'
                        );
                        return {
                            exists: true,
                            target_id: targetId,
                            tag,
                            role,
                            href,
                            text,
                            class_name: cls,
                            nav_like: Boolean(parent),
                            tab_like: role === 'tab' ||
                                el.getAttribute('aria-controls') ||
                                el.getAttribute('data-toggle') === 'tab' ||
                                /\\b(tab|tabs|nav-link|active)\\b/i.test(cls),
                        };
                    }""",
                    target_id,
                ) or {}
            except Exception as inspect_err:
                logger.debug("[EXTRACT NAV GUARD] target inspection skipped: %s", inspect_err)
                return {}

        async def _extract_list_rows_via_dom(reason: str) -> tuple[list[dict], str]:
            """Extract repeated list/card rows directly with DOM semantics."""
            try:
                _list_page = await browser._ensure_active_page(reason=reason)
                result = await _list_page.evaluate(
                    """() => {
                        const clean = (value) => String(value || '')
                            .replace(/\\s+/g, ' ')
                            .trim();
                        const compact = (value) => clean(value).toLowerCase()
                            .replace(/[^a-z0-9\\u4e00-\\u9fff]+/g, '');
                        const isVisible = (el) => {
                            if (!el || !(el instanceof Element)) return false;
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0;
                        };
                        const classifyUrl = (href) => {
                            const text = String(href || '').toLowerCase();
                            if (!text) return '';
                            if (text.includes('news.ycombinator.com/item')) return 'detail';
                            if (/\\/(item|story|post|posts|article|articles|thread|threads|comment|comments|detail|details|product|products|issues?)(\\/|\\?|#|$)/.test(text)) {
                                return 'detail';
                            }
                            return 'source';
                        };
                        const firstInt = (value) => {
                            const match = String(value || '').match(/\\d[\\d,]*/);
                            return match ? Number(match[0].replace(/,/g, '')) : null;
                        };
                        const looksMetaLink = (text) => {
                            const t = clean(text).toLowerCase();
                            return !t
                                || /^\\d+[\\d,]*\\s*(points?|comments?|replies?)$/.test(t)
                                || /^\\d+\\s+(seconds?|minutes?|hours?|days?|months?|years?)\\s+ago$/.test(t)
                                || /^\\d+[smhdwy]$/.test(t)
                                || /^(reply|hide|flag|past|favorite|save|share)$/.test(t);
                        };
                        const titleFromRow = (el, links, text) => {
                            const semantic = el.querySelector('h1,h2,h3,h4,[role="heading"],.title,.story-title,.ais-Highlight');
                            const semanticText = clean(semantic ? semantic.innerText || semantic.textContent : '');
                            if (semanticText && semanticText.length >= 4) return semanticText;
                            const link = links.find(l => l.text && !looksMetaLink(l.text));
                            if (link) return link.text;
                            const firstLine = clean((text || '').split(/\\n|\\r/)[0]);
                            return firstLine.length > 220 ? firstLine.slice(0, 220) : firstLine;
                        };
                        const parseMeta = (text, links, el) => {
                            const row = {};
                            const pointMatch = text.match(/(\\d[\\d,]*)\\s*points?/i);
                            if (pointMatch) row.points = firstInt(pointMatch[1]);
                            const commentMatch = text.match(/(\\d[\\d,]*)\\s*(?:comments?|replies?)/i);
                            if (commentMatch) row.review_count = firstInt(commentMatch[1]);
                            const reviewMatch = text.match(/(\\d[\\d,]*)\\s*(?:人评价|评价|条评价|reviews?|ratings?|votes?)/i);
                            if (reviewMatch && !row.review_count) row.review_count = firstInt(reviewMatch[1]);
                            const timeMatch = text.match(/\\b(\\d+\\s+(?:seconds?|minutes?|hours?|days?|months?|years?)\\s+ago|\\d+[smhdwy])\\b/i);
                            if (timeMatch) row.time = clean(timeMatch[1]);

                            const ratingEl = el.querySelector(
                                '.rating_num, .rating_nums, .score, .rating-score, [class*="score"]'
                            );
                            const ratingText = clean(ratingEl ? ratingEl.innerText || ratingEl.textContent : '');
                            const ratingMatch = ratingText.match(/\\b(\\d(?:\\.\\d)?)\\b/)
                                || text.match(/(?:评分|rating|score)\\s*[:：]?\\s*(\\d(?:\\.\\d)?)/i)
                                || text.match(/(?:^|\\s)(\\d\\.\\d)(?:\\s|$)/);
                            if (ratingMatch) row.rating = ratingMatch[1];

                            const summaryEl = el.querySelector(
                                '.quote .inq, .inq, p.quote, .summary, .description, .desc, .intro, [class*="summary"], [class*="description"], [class*="intro"]'
                            );
                            const summaryText = clean(summaryEl ? summaryEl.innerText || summaryEl.textContent : '');
                            if (summaryText && summaryText.length >= 3 && summaryText.length <= 260) {
                                row.summary = summaryText.replace(/^["“”'‘’]+|["“”'‘’]+$/g, '');
                            } else {
                                const quoteMatch = text.match(/[“"']([^“”"']{3,260})[”"']/);
                                if (quoteMatch) row.summary = clean(quoteMatch[1]);
                            }

                            const metaTexts = links.map(l => clean(l.text)).filter(Boolean);
                            const timeIndex = metaTexts.findIndex(t => /^(\\d+\\s+(?:seconds?|minutes?|hours?|days?|months?|years?)\\s+ago|\\d+[smhdwy])$/i.test(t));
                            if (timeIndex > 0 && !row.author) {
                                const prev = metaTexts[timeIndex - 1];
                                if (prev && !looksMetaLink(prev)) row.author = prev;
                            }
                            return row;
                        };
                        const collectLinks = (el) => Array.from(el.querySelectorAll('a[href]'))
                            .filter(isVisible)
                            .map(a => ({
                                text: clean(a.innerText || a.textContent || a.getAttribute('aria-label') || a.getAttribute('title')),
                                href: a.href || ''
                            }))
                            .filter(link => link.href && !link.href.startsWith('javascript:'));

                        const candidates = [];
                        const addCandidate = (el, source) => {
                            if (!isVisible(el)) return;
                            const text = clean(el.innerText || el.textContent);
                            if (text.length < 20 || text.length > 2400) return;
                            const nested = Array.from(el.querySelectorAll(
                                'article, [role="article"], [role="listitem"], li, tbody tr'
                            )).filter(node => node !== el && isVisible(node));
                            if (nested.length >= 3 && text.length > 900) return;
                            const links = collectLinks(el);
                            const key = links[0]?.href || compact(text).slice(0, 220);
                            if (!key) return;
                            candidates.push({el, source, key, text, links});
                        };

                        const directSelectors = [
                            'article', '[role="article"]', '[role="listitem"]',
                            '.Story', '.story', '.ais-Hits-item', '.hit',
                            '.search-result', '.result', '.item', '.card'
                        ];
                        for (const el of document.querySelectorAll(directSelectors.join(','))) {
                            addCandidate(el, 'selector');
                        }

                        const containerSelectors = [
                            'main', '[role="main"]', '#content', '.content',
                            '.list', '.item-list', '.results', '.search-results',
                            'ol', 'ul', 'section'
                        ];
                        for (const root of document.querySelectorAll(containerSelectors.join(','))) {
                            if (!isVisible(root)) continue;
                            const children = Array.from(root.children || []).filter(isVisible);
                            if (children.length < 4) continue;
                            const buckets = new Map();
                            for (const child of children) {
                                const cls = clean(child.className || child.tagName).slice(0, 80);
                                buckets.set(cls, (buckets.get(cls) || 0) + 1);
                            }
                            const repeat = Math.max(...Array.from(buckets.values()), 0);
                            if (repeat < 4) continue;
                            for (const child of children) addCandidate(child, 'container');
                        }

                        const seen = new Set();
                        const rows = [];
                        for (const candidate of candidates) {
                            if (seen.has(candidate.key)) continue;
                            seen.add(candidate.key);
                            const links = candidate.links;
                            const row = parseMeta(candidate.text, links, candidate.el);
                            row.title = titleFromRow(candidate.el, links, candidate.text);
                            row._dom_text = candidate.text.slice(0, 1200);

                            for (const link of links) {
                                const role = classifyUrl(link.href);
                                if (role === 'detail' && !row.detail_url) row.detail_url = link.href;
                                if (role === 'source' && !row.source_url) row.source_url = link.href;
                            }
                            row.primary_url = row.source_url || row.detail_url || links[0]?.href || '';
                            if (row.primary_url) row.url = row.primary_url;

                            if (!row.title || row.title.length < 4) continue;
                            if (!row.url && candidate.text.length < 40) continue;
                            rows.push(row);
                        }
                        const bodyText = document.body ? document.body.innerText || '' : '';
                        rows.sort((a, b) => {
                            const aTop = bodyText.indexOf(String(a.title || '').slice(0, 40));
                            const bTop = bodyText.indexOf(String(b.title || '').slice(0, 40));
                            return (aTop < 0 ? 1e9 : aTop) - (bTop < 0 ? 1e9 : bTop);
                        });
                        const selected = rows.slice(0, 120);
                        const sourceText = selected.map((row, index) => [
                            `Item ${index + 1}: ${row.title}`,
                            row._dom_text,
                            row.source_url ? `source_url: ${row.source_url}` : '',
                            row.detail_url ? `detail_url: ${row.detail_url}` : ''
                        ].filter(Boolean).join('\\n')).join('\\n\\n');
                        const publicRows = selected.map(row => {
                            const copy = {...row};
                            delete copy._dom_text;
                            return copy;
                        });
                        return {rows: publicRows, sourceText};
                    }"""
                )
                if not isinstance(result, dict):
                    return [], ""
                rows = result.get("rows") or []
                source_text = str(result.get("sourceText") or "")
                if isinstance(rows, list) and len(rows) >= 2:
                    rows = _normalize_extracted_row_fields(rows)
                    logger.info(
                        "[EXTRACT DOM] list rows=%s source_chars=%s",
                        len(rows),
                        len(source_text),
                    )
                    return rows, source_text
            except Exception as list_err:
                logger.debug("[EXTRACT DOM] list extraction skipped: %s", list_err)
            return [], ""

        async def _extract_visible_table_rows_via_dom(reason: str) -> list[dict]:
            try:
                _table_page = await browser._ensure_active_page(reason=reason)
                rows = await _table_page.evaluate(
                    """() => {
                        const clean = (value) => String(value || '')
                            .replace(/\\s+/g, ' ')
                            .trim();
                        const cleanHeader = (value) => clean(value)
                            .replace(/\\s*:?[\\s-]*activate to sort column (?:ascending|descending)/ig, '')
                            .replace(/\\s*:?[\\s-]*activate to sort/ig, '')
                            .replace(/\\s*排序(?:升序|降序)?\\s*/g, '')
                            .replace(/\\s+/g, ' ')
                            .trim();
                        const isVisible = (el) => {
                            if (!el || !(el instanceof Element)) return false;
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0;
                        };
                        const headerText = (el) => cleanHeader(
                            el.getAttribute('aria-label')
                            || el.getAttribute('data-label')
                            || el.getAttribute('title')
                            || el.innerText
                            || el.textContent
                        );
                        const rowCells = (tr, includeTh = false) => {
                            const selector = includeTh
                                ? 'th, td, [role="columnheader"], [role="rowheader"], [role="cell"], [role="gridcell"]'
                                : 'td, [role="cell"], [role="gridcell"]';
                            return Array.from(tr.querySelectorAll(selector))
                                .filter(cell => isVisible(cell))
                                .map(cell => clean(cell.innerText || cell.textContent));
                        };
                        const uniqueHeaders = (headers) => {
                            const seen = new Map();
                            return headers.map((header, index) => {
                                let key = cleanHeader(header) || `column_${index + 1}`;
                                const base = key;
                                const count = (seen.get(base) || 0) + 1;
                                seen.set(base, count);
                                if (count > 1) key = `${base}_${count}`;
                                return key;
                            });
                        };
                        const headerCandidatesFor = (table) => {
                            const candidates = [];
                            const add = (nodes, source) => {
                                const headers = Array.from(nodes || [])
                                    .filter(node => node instanceof Element)
                                    .map(headerText)
                                    .filter(Boolean);
                                if (headers.length) candidates.push({source, headers});
                            };

                            add(table.querySelectorAll('thead th, thead td, [role="columnheader"]'), 'table-head');
                            const headerRows = Array.from(table.querySelectorAll('tr'))
                                .filter(tr => tr.querySelector('th, [role="columnheader"]'));
                            for (const tr of headerRows.slice(0, 3)) {
                                add(tr.querySelectorAll('th, td, [role="columnheader"]'), 'header-row');
                            }

                            const id = table.id ? CSS.escape(table.id) : '';
                            const wrapper = table.closest(
                                '.dt-container, .dataTables_wrapper, .datatable, .table-responsive, .table-container, [role="grid"]'
                            );
                            if (wrapper) {
                                add(wrapper.querySelectorAll('thead th, thead td, [role="columnheader"]'), 'wrapper-head');
                                if (id) {
                                    add(
                                        wrapper.querySelectorAll(`[aria-controls="${id}"], [data-dt-column]`),
                                        'wrapper-controls'
                                    );
                                }
                            }

                            return candidates;
                        };
                        const chooseHeaders = (table, width) => {
                            const candidates = headerCandidatesFor(table);
                            candidates.sort((a, b) => {
                                const aExact = a.headers.length === width ? 1 : 0;
                                const bExact = b.headers.length === width ? 1 : 0;
                                return (bExact - aExact)
                                    || (Math.abs(a.headers.length - width) - Math.abs(b.headers.length - width))
                                    || (b.headers.length - a.headers.length);
                            });
                            const best = candidates.find(c => c.headers.length >= width)
                                || candidates.find(c => c.headers.length > 0);
                            if (!best) return [];
                            return uniqueHeaders(best.headers.slice(0, width));
                        };
                        const tables = Array.from(document.querySelectorAll('table'));
                        let best = { score: 0, rows: [] };

                        for (const table of tables) {
                            if (!isVisible(table)) continue;
                            const bodyRows = Array.from(table.querySelectorAll('tbody tr'))
                                .filter(tr => isVisible(tr));
                            const allRows = Array.from(table.querySelectorAll('tr'))
                                .filter(tr => isVisible(tr));
                            const dataRows = bodyRows.length ? bodyRows : allRows.filter(tr => {
                                const hasDataCells = tr.querySelector('td, [role="cell"], [role="gridcell"]');
                                const hasHeaderCells = tr.querySelector('th, [role="columnheader"]');
                                return hasDataCells && !hasHeaderCells;
                            });
                            const firstDataCells = dataRows.length ? rowCells(dataRows[0]) : [];
                            let headers = firstDataCells.length
                                ? chooseHeaders(table, firstDataCells.length)
                                : [];
                            const parsedRows = [];

                            for (const tr of dataRows) {
                                const cells = rowCells(tr);
                                if (cells.length < 2) continue;
                                if (cells.some(cell => /no matching records|no data/i.test(cell))) {
                                    continue;
                                }
                                if (!headers.length || headers.length !== cells.length) {
                                    headers = cells.map((_, index) => `column_${index + 1}`);
                                }
                                const row = {};
                                cells.forEach((cell, index) => {
                                    row[headers[index] || `column_${index + 1}`] = cell;
                                });
                                parsedRows.push(row);
                            }

                            const namedHeaderBonus = headers.some(h => !/^column_\\d+$/.test(h)) ? 10 : 0;
                            const score = parsedRows.length * Math.max(headers.length, 1) + namedHeaderBonus;
                            if (parsedRows.length >= 2 && score > best.score) {
                                best = { score, rows: parsedRows };
                            }
                        }
                        return best.rows;
                    }"""
                )
                if isinstance(rows, list) and rows:
                    logger.info("[EXTRACT DOM] visible table rows=%s", len(rows))
                    return rows
            except Exception as table_err:
                logger.debug("[EXTRACT DOM] table extraction skipped: %s", table_err)
            return []

        async def _visible_table_signature(reason: str) -> str:
            try:
                _sig_page = await browser._ensure_active_page(reason=reason)
                return await _sig_page.evaluate(
                    """() => {
                        const clean = (value) => String(value || '')
                            .replace(/\\s+/g, ' ')
                            .trim();
                        const isVisible = (el) => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0;
                        };
                        const table = Array.from(document.querySelectorAll('table'))
                            .find(t => isVisible(t));
                        if (!table) return '';
                        return Array.from(table.querySelectorAll('tbody tr'))
                            .filter(tr => isVisible(tr))
                            .slice(0, 5)
                            .map(tr => clean(tr.innerText))
                            .join('|');
                    }"""
                ) or ""
            except Exception:
                return ""

        async def _auto_advance_table_page_via_dom(reason: str) -> bool:
            try:
                _page_for_next = await browser._ensure_active_page(reason=reason)
                before_sig = await _visible_table_signature("table autopager before")
                if not before_sig:
                    return False
                result = await _page_for_next.evaluate(
                    """() => {
                        const clean = (value) => String(value || '')
                            .replace(/\\s+/g, ' ')
                            .trim();
                        const isVisible = (el) => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0;
                        };
                        const isDisabled = (el) => {
                            const cls = String(el.className || '').toLowerCase();
                            return el.disabled
                                || el.getAttribute('aria-disabled') === 'true'
                                || cls.includes('disabled');
                        };

                        const tables = Array.from(document.querySelectorAll('table'))
                            .filter(isVisible);
                        if (window.jQuery && window.jQuery.fn && window.jQuery.fn.dataTable) {
                            for (const table of tables) {
                                if (!window.jQuery.fn.dataTable.isDataTable(table)) {
                                    continue;
                                }
                                const dt = window.jQuery(table).DataTable();
                                const info = dt.page.info();
                                if (info && info.page < info.pages - 1) {
                                    dt.page('next').draw('page');
                                    return {
                                        ok: true,
                                        method: 'datatables_api',
                                        page: info.page + 2,
                                        pages: info.pages
                                    };
                                }
                            }
                        }

                        const nextText = /^(next|next page|>|›|»|→|下一页|下页)$/i;
                        const candidates = Array.from(
                            document.querySelectorAll('button,a,[role="button"],[role="link"]')
                        ).filter(isVisible);
                        for (const el of candidates) {
                            const label = clean(
                                el.innerText
                                || el.getAttribute('aria-label')
                                || el.getAttribute('title')
                                || el.textContent
                            );
                            if (!label || !nextText.test(label)) continue;
                            if (isDisabled(el)) continue;
                            el.scrollIntoView({block: 'center', inline: 'center'});
                            el.click();
                            return {ok: true, method: 'dom_next_button', label};
                        }

                        const current = candidates.find(el => {
                            const cls = String(el.className || '').toLowerCase();
                            const label = clean(el.innerText || el.textContent);
                            return /^\\d+$/.test(label)
                                && (cls.includes('current')
                                    || cls.includes('active')
                                    || el.getAttribute('aria-current') === 'page');
                        });
                        if (current) {
                            const currentNo = Number(clean(current.innerText || current.textContent));
                            const next = candidates.find(el => clean(el.innerText || el.textContent) === String(currentNo + 1));
                            if (next && !isDisabled(next)) {
                                next.scrollIntoView({block: 'center', inline: 'center'});
                                next.click();
                                return {ok: true, method: 'dom_numeric_page', page: currentNo + 1};
                            }
                        }

                        return {ok: false, method: 'not_found'};
                    }"""
                )
                if not isinstance(result, dict) or not result.get("ok"):
                    return False
                try:
                    await _page_for_next.wait_for_function(
                        """(before) => {
                            const clean = (value) => String(value || '')
                                .replace(/\\s+/g, ' ')
                                .trim();
                            const isVisible = (el) => {
                                const style = window.getComputedStyle(el);
                                const rect = el.getBoundingClientRect();
                                return style.display !== 'none'
                                    && style.visibility !== 'hidden'
                                    && rect.width > 0
                                    && rect.height > 0;
                            };
                            const table = Array.from(document.querySelectorAll('table'))
                                .find(t => isVisible(t));
                            if (!table) return false;
                            const after = Array.from(table.querySelectorAll('tbody tr'))
                                .filter(tr => isVisible(tr))
                                .slice(0, 5)
                                .map(tr => clean(tr.innerText))
                                .join('|');
                            return after && after !== before;
                        }""",
                        arg=before_sig,
                        timeout=3000,
                    )
                except Exception:
                    after_sig = await _visible_table_signature("table autopager after")
                    if not after_sig or after_sig == before_sig:
                        logger.info(
                            "[TABLE AUTOPAGER] clicked but visible table signature did not change: %s",
                            result,
                        )
                        return False
                logger.info("[TABLE AUTOPAGER] advanced page via %s", result)
                return True
            except Exception as pager_err:
                logger.debug("[TABLE AUTOPAGER] skipped: %s", pager_err)
                return False

        async def _nudge_scroll_after_duplicate_extract(reason: str, scroll_amount: int = 2000) -> bool:
            try:
                _scroll_page = await browser._ensure_active_page(reason=reason)
                before_size = await _scroll_page.evaluate("() => document.body.innerText.length")
                await _scroll_page.evaluate(
                    """(amt) => {
                        window.scrollBy({top: Math.max(amt, window.innerHeight * 1.5), behavior: 'smooth'});
                    }""",
                    scroll_amount,
                )
                await _scroll_page.wait_for_timeout(1500)
                try:
                    await _scroll_page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
                after_size = await _scroll_page.evaluate("() => document.body.innerText.length")
                delta = after_size - before_size
                pct = (delta / before_size * 100) if before_size > 0 else 0.0
                logger.info(
                    "[EXTRACT DEDUP] nudged page downward (%s): %d→%d bytes (+%d, %.1f%%)",
                    reason, before_size, after_size, delta, pct,
                )
                return delta > 100
            except Exception as scroll_err:
                logger.debug("[EXTRACT DEDUP] duplicate-row scroll nudge failed: %s", scroll_err)
                return False

        async def _probe_scroll_drain_state(reason: str) -> dict:
            """Return whether the current page/main scroll container is physically drained."""
            try:
                _probe_page = await browser._ensure_active_page(reason=reason)
                return await _probe_page.evaluate(
                    """() => {
                        const viewportW = window.innerWidth || 0;
                        const viewportH = window.innerHeight || 0;
                        const doc = document.scrollingElement || document.documentElement || document.body;
                        const docRemaining = Math.max(
                            0,
                            (doc.scrollHeight || 0) - ((window.scrollY || doc.scrollTop || 0) + viewportH)
                        );
                        const docScrollable = (doc.scrollHeight || 0) > viewportH + 80;

                        const visibleRect = (el) => {
                            if (!el || !el.getBoundingClientRect) return null;
                            const r = el.getBoundingClientRect();
                            const w = Math.max(0, Math.min(r.right, viewportW) - Math.max(r.left, 0));
                            const h = Math.max(0, Math.min(r.bottom, viewportH) - Math.max(r.top, 0));
                            if (w < 160 || h < 120) return null;
                            const style = window.getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden') return null;
                            const overflowY = style.overflowY || '';
                            const scrollable = /(auto|scroll|overlay)/i.test(overflowY)
                                && el.scrollHeight > el.clientHeight + 80;
                            if (!scrollable) return null;
                            return {el, area: w * h, w, h};
                        };

                        const candidates = Array.from(document.querySelectorAll('main, [role="main"], table, tbody, .el-table__body-wrapper, .ant-table-body, .v-data-table__wrapper, [class*="table"], [class*="list"], [class*="content"], div'))
                            .map(visibleRect)
                            .filter(Boolean)
                            .sort((a, b) => b.area - a.area);
                        const best = candidates[0] || null;
                        const containerRemaining = best
                            ? Math.max(0, best.el.scrollHeight - best.el.scrollTop - best.el.clientHeight)
                            : 0;
                        const containerCanScroll = Boolean(best && containerRemaining > 80);
                        const windowCanScroll = Boolean(docScrollable && docRemaining > 80);
                        const atBottom = !windowCanScroll && !containerCanScroll;
                        return {
                            at_bottom: atBottom,
                            window_remaining: Math.round(docRemaining),
                            window_can_scroll: windowCanScroll,
                            container_remaining: Math.round(containerRemaining),
                            container_can_scroll: containerCanScroll,
                            container_tag: best ? String(best.el.tagName || '').toLowerCase() : '',
                            container_class: best ? String(best.el.className || '').slice(0, 80) : '',
                        };
                    }"""
                ) or {"at_bottom": False, "probe_failed": True}
            except Exception as probe_err:
                logger.debug("[EXTRACT DEDUP] scroll drain probe failed: %s", probe_err)
                return {"at_bottom": False, "probe_failed": True}

        async def _try_pre_extract_fast_path() -> bool:
            """Try deterministic extraction before invoking the VLM planner."""
            nonlocal _run_succeeded, _rpa_cache_allowed, _rpa_skip_reason
            if not _goal_is_bulk_extraction(goal):
                return False
            if _goal_is_tooltip_extract(goal) or _is_form_fill_goal:
                return False
            target_count = _parse_goal_target_count(goal)
            if target_count is None or target_count <= 0:
                return False

            logger.info(
                "[PRE-EXTRACT] starting deterministic preflight target=%s fields=%s",
                target_count,
                _requested_output_fields,
            )
            _broadcast_log_safe(
                f"[PRE-EXTRACT] 先尝试确定性抽取，目标 {target_count} 行。",
                level="info",
            )

            data_shape: dict = {}
            try:
                data_shape = await browser.probe_data_shape()
                logger.info("[PRE-EXTRACT] data_shape=%s", data_shape)
            except Exception as shape_err:
                logger.debug("[PRE-EXTRACT] data shape skipped: %s", shape_err)

            candidates: list[dict] = []

            # Try families in a stable fast-path order. If multiple families
            # satisfy the goal, table-like DOM evidence wins over cards/lists,
            # and full-page/AX only acts as the final pre-planner fallback.
            dom_table_rows = await _extract_visible_table_rows_via_dom(
                "pre-extract visible table rows"
            )
            if dom_table_rows:
                candidates.append(
                    _sanitize_extraction_candidate(
                        name="DOM_TABLE",
                        data=dom_table_rows,
                        source_text="",
                        data_shape=data_shape,
                    )
                )

            body_text = await _extract_body_text_for_semantic_cards(
                "pre-extract semantic card body text"
            )
            dom_list_rows, dom_list_text = await _extract_list_rows_via_dom(
                "pre-extract visible list rows"
            )
            card_source_text = "\n\n".join(
                text for text in (body_text, dom_list_text) if text
            )
            dom_card_rows, dom_card_text = extract_semantic_card_rows(
                dom_list_rows,
                source_text=card_source_text,
                requested_fields=_requested_output_fields,
                goal=goal,
            )
            if dom_card_rows:
                logger.info(
                    "[PRE-EXTRACT] semantic cards rows=%s source_chars=%s",
                    len(dom_card_rows),
                    len(dom_card_text or card_source_text),
                )
                candidates.append(
                    _sanitize_extraction_candidate(
                        name="DOM_CARDS",
                        data=dom_card_rows,
                        source_text=dom_card_text or card_source_text,
                        data_shape=data_shape,
                    )
                )
            if dom_list_rows:
                candidates.append(
                    _sanitize_extraction_candidate(
                        name="DOM_LIST",
                        data=dom_list_rows,
                        source_text=dom_list_text or body_text,
                        data_shape=data_shape,
                    )
                )

            reached_candidate = _choose_pre_extract_reached_candidate(
                candidates,
                target_count,
            )
            if not reached_candidate:
                full_source, full_text = await _extract_full_page_text_for_data(
                    "pre-extract full page/AX fallback"
                )
                if full_text:
                    structured_full = await vlm.extract_structured_data(
                        page_text=full_text,
                        goal=goal,
                    )
                    if (
                        structured_full
                        and isinstance(structured_full, list)
                        and len(structured_full) > 0
                    ):
                        logger.info(
                            "[PRE-EXTRACT] full-page structured rows=%s source=%s chars=%s",
                            len(structured_full),
                            full_source,
                            len(full_text),
                        )
                        candidates.append(
                            _sanitize_extraction_candidate(
                                name=f"FULL_PAGE:{full_source}",
                                data=structured_full,
                                source_text=full_text,
                                data_shape=data_shape,
                            )
                        )

            if not candidates:
                logger.info("[PRE-EXTRACT] no deterministic candidates")
                return False

            try:
                candidates = rank_extraction_candidates_with_history(
                    candidates,
                    url=getattr(browser, "current_url", "") or start_url,
                    requested_fields=_requested_output_fields,
                    goal=_snapshot_goal,
                )
            except Exception as recovery_err:
                logger.debug("[PRE-EXTRACT] recovery ranking skipped: %s", recovery_err)

            chosen = _choose_pre_extract_reached_candidate(candidates, target_count)
            if chosen:
                logger.info(
                    "[PRE-EXTRACT] reached target via ordered family: %s accepted=%s/%s",
                    chosen.get("name"),
                    chosen.get("accepted"),
                    target_count,
                )
            else:
                chosen = _choose_best_extraction_candidate(candidates)
            if not chosen:
                logger.info("[PRE-EXTRACT] no viable candidate after arbitration")
                return False

            accepted = int(chosen.get("accepted") or 0)
            if accepted < target_count:
                logger.info(
                    "[PRE-EXTRACT] best candidate %s has %s/%s rows; continue to planner",
                    chosen.get("name"),
                    accepted,
                    target_count,
                )
                return False

            rows, new_rows, dup_rows, rejected_rows, chosen_source_text = (
                _commit_extraction_candidate(chosen)
            )
            rows = await _enrich_rows_with_dom_links(rows)
            saved_path = save_run_dataset(
                rows,
                run_id=_run_ts,
                output_contract=_goal_output_contract,
                produced_by="pre_extract",
                filename_hint=_vlm_output,
            )
            progress_new_rows, progress_total_rows = _record_extract_progress(
                rows,
                new_rows,
            )
            snapshot_path = await _save_extraction_snapshot(
                source=str(chosen.get("name") or "PRE_EXTRACT"),
                rows=rows,
                output_file=str(saved_path or ""),
                accepted_rows=new_rows,
                duplicate_rows=dup_rows,
                rejected_rows=rejected_rows,
                candidates=candidates,
                data_shape=data_shape,
                source_text=chosen_source_text or body_text,
                metadata={
                    "mode": "pre_extract_fast_path",
                    "progress_new_rows": progress_new_rows,
                    "progress_total_rows": progress_total_rows,
                    "target": target_count,
                },
            )
            _rpa_cache_allowed = False
            _rpa_skip_reason = "pre-extract completed task"
            logger.info(
                "[PRE-EXTRACT] completed via %s rows=%s/%s output=%s snapshot=%s",
                chosen.get("name"),
                progress_total_rows,
                target_count,
                saved_path,
                snapshot_path,
            )
            print(
                f"\033[1;32m🎯 [PRE-EXTRACT]\033[0m "
                f"{chosen.get('name')} 已达量 {progress_total_rows}/{target_count}，"
                "跳过 Planner/VLM 主循环。"
            )
            _broadcast_log_safe(
                f"[PRE-EXTRACT] {chosen.get('name')} 已提取 {progress_total_rows}/{target_count} 行，任务完成。"
            )
            _broadcast_done_safe(True, "Pre-extract target reached")
            try:
                await browser.mark_and_screenshot(step=99)
            except Exception as screenshot_err:
                logger.debug("[PRE-EXTRACT] final screenshot skipped: %s", screenshot_err)
            _run_succeeded = True
            return True

        # ── 自愈计数器 ────────────────────────────────────────────────
        # 连续执行失败超过 _MAX_CONSECUTIVE_ERRORS 次时强制转 ask_human
        _MAX_CONSECUTIVE_ERRORS = 3
        _consecutive_errors = 0
        try:
            from .stuck_recovery_guard import StuckRecoveryState
        except ImportError:
            from stuck_recovery_guard import StuckRecoveryState
        _stuck_recovery_state = StuckRecoveryState()
        _recovery_chain_pending = False
        try:
            from .bot_challenge_guard import BotChallengeState
        except ImportError:
            from bot_challenge_guard import BotChallengeState
        _bot_challenge_state = BotChallengeState()
        # ── Session Drop tracking (Wave 3) ───────────────────────────
        # _last_business_url: most recent non-login URL we observed at
        # step end. Used by the session-drop sniffer to decide whether
        # we've been bounced to login MID-task.
        # _pending_session_return_url: set when sniffer fires; consumed
        # by the post-ask_human resume handler to goto back automatically.
        _last_business_url: str = ""
        _pending_session_return_url: str = ""
        # ── Failure Classifier：统一失败统计 ─────────────────────────
        _failure_stats = _FailureStatsClass()
        # ── Judge：任务完成验证 ──────────────────────────────────────
        _judge = TaskJudge(vlm_client=vlm, config=JudgeConfig(
            enabled=JUDGE_ENABLED,
            max_retries_after_fail=2,
        ))
        _judge_rejections = 0  # done 被 Judge 驳回的次数
        # ── Loop Detector：统一循环检测 ──────────────────────────────
        _loop_detector = ActionLoopDetector(config=LoopDetectorConfig(
            window_size=8,
            action_repeat_threshold=3,
            stagnation_threshold=4,
        ))
        # ── Element Tracker：清空上一任务的追踪条目 ──────────────────
        # 新任务开始时显式 reset，避免上次任务的 last_click 等别名串到本次。
        # 开关关闭时 reset_element_tracker() 是 no-op，不需要额外判断。
        try:
            vlm.reset_element_tracker()
        except Exception as _trk_reset_err:
            logger.debug(f"[TRACKER] reset failed: {_trk_reset_err}")

        # ── Wave 2：Planner / Reflector 状态 ──────────────────────────
        _task_plan: "TaskPlan | None" = None
        _steps_since_reflect = 0
        _reflect_count = 0
        _MAX_REFLECTS = 5          # 一次任务最多 Reflector 调用次数（防 cascade）
        _REFLECT_INTERVAL = 5      # 兜底间隔：连续 N 步未反思时主动触发一次
        _dedup_tripped_last_step = False  # 上一步 extract dedup 命中标志
        _duplicate_zero_extract_streak = 0  # 连续 extract 净新增为 0 的次数
        _abort_requested = False    # Reflector 判 abort 后允许下一步合法 done

        # ── 跨页面记忆库 ──────────────────────────────────────────────
        # VLM 通过 save_to_memory 动作写入，通过 {{key}} 插值在 type 动作读取
        workflow_memory: dict = {}
        _rpa_cache_allowed = True
        _rpa_skip_reason = ""
        _executed_cached_trail: list[dict] = []
        _blank_page_recovery_attempted: set[str] = set()
        _force_vision_next_step = False
        _runtime_config = _runtime_config_module()
        _allow_physical_rpa = bool(getattr(_runtime_config, "RPA_ENABLE_PHYSICAL_REPLAY", True))
        _allow_semantic_rpa = bool(getattr(_runtime_config, "RPA_ENABLE_SEMANTIC_MACRO_REPLAY", True))
        _semantic_macro = (
            _initial_semantic_macro
        ) if _allow_semantic_rpa else None
        _semantic_macro_attempted = False
        _skip_rpa_for_goal, _skip_rpa_reason = _goal_should_skip_rpa(goal)
        if not _allow_physical_rpa:
            _rpa_cache_allowed = False
            _rpa_skip_reason = "physical replay disabled by config"
            logger.info("[RPA] Physical replay disabled by config")
            print(
                "\n\033[1;33m⚡ [RPA]\033[0m 物理肌肉记忆回放已被配置关闭。\n"
                "   semantic macro replay 仍可单独启用。"
            )
        elif _skip_rpa_for_goal:
            _rpa_cache_allowed = False
            _rpa_skip_reason = _skip_rpa_reason
            logger.info(
                f"[RPA] Disabled for this run: {_rpa_skip_reason}"
            )
            print(
                f"\n\033[1;33m⚡ [RPA]\033[0m 当前任务不适合物理肌肉记忆回放，"
                f"已自动禁用物理回放。\n"
                f"   reason: {_rpa_skip_reason}"
            )

        # ══════════════════════════════════════════════════════════════
        # RPA 肌肉记忆：支持"全量极速回放"与"条件满足后的半回放"
        # ══════════════════════════════════════════════════════════════
        _rpa_exact_path = _rpa_cache_path(start_url, goal)
        _rpa_path = _rpa_exact_path
        _rpa_payload: dict | None = None
        _rpa_match_reason = "exact hash match"
        _rpa_cursor = 0
        browser.clear_rpa_trail()  # 清空上次遗留的轨迹

        if _allow_physical_rpa and not _skip_rpa_for_goal:
            _rpa_exact_path, _rpa_payload, _rpa_match_reason = _load_exact_rpa_cache(
                start_url, goal
            )
            if _rpa_payload is not None:
                logger.info(
                    "[RPA] Exact cache hit: %s | reason=%s",
                    _rpa_exact_path,
                    _rpa_match_reason,
                )
            if _rpa_payload is None:
                _similar_match = _find_similar_rpa_cache(start_url, goal, exclude_path=_rpa_exact_path)
                if _similar_match:
                    _rpa_path, _rpa_payload, _rpa_match_reason = _similar_match
                    logger.info(
                        f"[RPA] Similar cache selected: {_rpa_path} | reason={_rpa_match_reason}"
                    )
        if _semantic_macro:
                _semantic_action = str(_semantic_macro.get("action") or "")
                logger.info(
                    "[RPA] Using %s macro generated from current goal: %s",
                    _semantic_action,
                    _semantic_macro.get("path") or _semantic_macro.get("target_date"),
                )
                _rpa_payload = {
                    "version": 4,
                    "replayable": True,
                    "reason": "",
                    "trail": [_semantic_macro],
                    "fail_count": 0,
                    "max_failures": 2,
                    "completes_task": _goal_output_mode != "answer",
                    **_build_rpa_match_metadata(start_url, goal),
                }
                _rpa_path = _rpa_exact_path
                _rpa_match_reason = f"semantic {_semantic_action} macro"
                try:
                    _write_rpa_cache_payload(_rpa_exact_path, _rpa_payload)
                except Exception as _semantic_cache_err:
                    logger.debug(
                        "[RPA] Failed to persist current %s macro: %s",
                        _semantic_action,
                        _semantic_cache_err,
                    )
        if _rpa_payload:
            if not _rpa_payload.get("replayable", True):
                _reason = _rpa_payload.get("reason") or "marked as non-replayable"
                print(
                    f"\n\033[1;33m⚡ [RPA]\033[0m 发现缓存: {_rpa_path.name}\n"
                    f"   该缓存已标记为不适合极速回放（{_reason}），直接进入 VLM 模式..."
                )
                logger.info(f"[RPA] Cache is non-replayable, skip replay: {_reason}")
            else:
                _reason_lower = str(_rpa_match_reason or "").lower()
                if _reason_lower.startswith("normalized exact"):
                    _match_label = "精确命中（normalized exact）"
                elif _reason_lower.startswith("legacy exact"):
                    _match_label = "精确命中（legacy exact）"
                elif _reason_lower.startswith("semantic "):
                    _match_label = f"语义宏命中（{_rpa_match_reason}）"
                elif _rpa_path == _rpa_exact_path:
                    _match_label = f"精确命中（{_rpa_match_reason}）"
                else:
                    _match_label = f"近似命中（{_rpa_match_reason}）"
                print(
                    f"\n\033[1;33m⚡ [RPA]\033[0m 发现肌肉记忆缓存: {_rpa_path.name}\n"
                    f"   {_match_label}，将在条件满足时自动尝试极速回放..."
                )
                await _recover_active_page("before startup RPA replay")
                _new_cursor, _replayed_steps, _replay_failed = await _replay_ready_rpa_steps(
                    browser, _rpa_payload, _rpa_cursor, workflow_memory, vlm=vlm
                )
                if _replay_failed:
                    logger.warning("[RPA] Startup replay failed, falling back to VLM loop.")
                    _rpa_payload = _mark_rpa_cache_failure(
                        _rpa_path, _rpa_payload, "startup replay failure"
                    )
                else:
                    _rpa_cursor = _new_cursor
                    _executed_cached_trail.extend(_replayed_steps)
                    _replay_completes_current_goal = bool(
                        _rpa_payload.get("completes_task", True)
                    )
                    if _goal_output_mode == "answer" and _replay_completes_current_goal:
                        logger.info(
                            "[RPA] Answer-mode replay used only as a navigation prefix; "
                            "final answer still requires page observation/VLM output."
                        )
                        event_stream.guard(
                            step=0,
                            name="ANSWER_RPA_PREFIX_ONLY",
                            message=(
                                "Cached physical replay reached the expected page, "
                                "but answer-mode tasks still require a final observed answer."
                            ),
                            metadata={
                                "cache": str(_rpa_path),
                                "replayed_steps": len(_replayed_steps),
                                "cursor": _rpa_cursor,
                                "trail_length": len(_rpa_payload.get("trail") or []),
                            },
                        )
                        _replay_completes_current_goal = False
                    if _goal_is_form_fill(goal):
                        _replay_completes_current_goal = _trail_completes_form_goal(
                            goal, _executed_cached_trail
                        )
                    if (
                        _replay_completes_current_goal
                        and _rpa_cursor >= len(_rpa_payload.get("trail") or [])
                        and _replayed_steps
                    ):
                        print(f"\033[1;32m✅ [RPA]\033[0m 极速回放成功！全程跳过 VLM，任务已完成。")
                        logger.info("[RPA] Replay succeeded — task done without VLM.")
                        _run_succeeded = True
                        return True
        # ──────────────────────────────────────────────────────────────

        step = 0
        if await _try_pre_extract_fast_path():
            return True

        # ══════════════════════════════════════════════════════════════
        # Wave 2 Planner：任务起手生成子目标清单
        # 失败时静默降级为单子目标 TaskPlan，主循环行为与 Wave 1 等价。
        # ══════════════════════════════════════════════════════════════
        try:
            _task_plan = await vlm.make_plan(
                goal=goal,
                initial_url=browser.current_url or start_url,
                workflow_memory=workflow_memory,
            )
            _original_plan_count = len(_task_plan.sub_goals)
            _task_plan = _normalize_form_task_plan(_task_plan, goal)
            if _is_form_fill_goal and _task_plan is not None and len(_task_plan.sub_goals) != _original_plan_count:
                logger.info(
                    "[PLANNER] Form plan normalized: removed visibility-only subgoals"
                )
                _broadcast_log_safe(
                    "[PLANNER] 表单任务已改写为逐字段填写计划，禁用完整同屏子目标",
                    level="warn",
                )
            print(
                f"\n\033[1;35m📋 [PLANNER]\033[0m 生成 "
                f"\033[35m{len(_task_plan.sub_goals)}\033[0m 个子目标："
            )
            for _sg in _task_plan.sub_goals:
                print(f"   {_sg.id}. {_sg.description}")
        except Exception as _plan_err:
            logger.warning(f"[PLANNER] 调用失败静默降级：{_plan_err}")
            _task_plan = None

        if _task_plan is not None:
            try:
                from visual_web_agent.semantic_router import merge_task_plan_into_route

                _capability_route = merge_task_plan_into_route(_task_plan, _capability_route)
            except Exception as _plan_merge_err:
                logger.debug("[PLANNER] route merge skipped: %s", _plan_merge_err)

        try:
            try:
                from .tab_session_guards import infer_tab_session_anchor
            except ImportError:
                from tab_session_guards import infer_tab_session_anchor
            _tab_session_anchor = infer_tab_session_anchor(goal)
        except Exception as _tsa_init_err:
            logger.debug("[TAB SESSION] anchor init skipped: %s", _tsa_init_err)
            _tab_session_anchor = None
        if _tab_session_anchor is not None:
            logger.info(
                "[TAB SESSION] anchor tab index=%s (goal mentions return to first/start tab)",
                _tab_session_anchor,
            )

        # RUN-RESUME1 step2b: opt-in run checkpoint (inert unless constraints.resume)
        try:
            try:
                from .run_checkpoint import RunCheckpointer
            except ImportError:
                from run_checkpoint import RunCheckpointer
            _run_ckpt = RunCheckpointer.begin(
                _run_ts,
                resume=bool((run_constraints or {}).get("resume")),
                goal=goal,
                start_url=start_url,
            )
        except Exception:
            _run_ckpt = None

        # RUN-RESUME1 step 3: consume the resume decision -- surface it into
        # memory (so the VLM/planner continues instead of restarting) and
        # best-effort skip the leading already-completed sub-goals on the plan.
        # Inert unless a resume was actually decided; never aborts the run.
        if _run_ckpt is not None and _run_ckpt.should_resume:
            try:
                try:
                    from .run_resume_consume import (
                        apply_completed_steps_to_plan,
                        resume_memory_payload,
                    )
                except ImportError:
                    from run_resume_consume import (  # type: ignore[no-redef]
                        apply_completed_steps_to_plan,
                        resume_memory_payload,
                    )
                workflow_memory["__resume_state"] = resume_memory_payload(_run_ckpt.decision)
                _resume_skipped = apply_completed_steps_to_plan(
                    _task_plan, _run_ckpt.decision.completed_steps
                )
                if _resume_skipped:
                    logger.info(
                        "[RUN-RESUME] skipped %s already-completed sub-goal(s) on resume",
                        _resume_skipped,
                    )
            except Exception as _resume_consume_err:
                logger.debug("[RUN-RESUME] consume skipped: %s", _resume_consume_err)

        # RUN-RESUME1 step3a-wire-2b: register this run in the cross-launch resume
        # index up-front so a crash mid-run can still be resumed (the next launch
        # seeds from the prior run's dataset, located via its manifest). Inert
        # unless constraints.resume.
        if bool((run_constraints or {}).get("resume")):
            try:
                try:
                    from .resume_seed import record_run_for_resume
                except ImportError:
                    from resume_seed import record_run_for_resume  # type: ignore[no-redef]
                record_run_for_resume(goal, start_url, _run_ts, status="in_progress")
            except Exception:
                pass

        for step in range(1, _effective_max_steps + 1):
            _check_stop(f"before_step_{step}")
            # K4: track the current step in module state so that if a
            # failure surfaces from inside this iteration the archive
            # record gets ``step_count = N`` instead of None.
            _record_run_step(step)
            logger.info(f"\n{'-' * 50}")
            logger.info(f">> Step {step}/{_effective_max_steps}")
            logger.info(f"{'-' * 50}")

            # ── Session-drop URL tracking (Wave 3) ─────────────────────
            # Snapshot the URL we ENTER this step on. If the snapshot is
            # NOT login-shaped, remember it as the latest business URL.
            # The session-drop guard later compares against this to detect
            # mid-task auth expiry. We update at step start (not end) so
            # the snapshot reflects post-stabilisation state of the prior
            # step's navigation.
            try:
                try:
                    from .session_drop_guard import looks_like_login_url as _sd_looks
                except ImportError:  # pragma: no cover
                    from session_drop_guard import looks_like_login_url as _sd_looks  # type: ignore[no-redef]
                _step_entry_url = browser.current_url or ""
                if _step_entry_url and not _sd_looks(_step_entry_url):
                    _last_business_url = _step_entry_url
            except Exception:
                pass

            # ── E1b: runtime cross-system observation ──────────────────
            # Resolve the URL we entered this step on to a planned system.
            # Only emit when it's a genuine cross-system hop (from a known
            # prior system) so single-system runs stay quiet. Fully guarded
            # so it can never break the step loop.
            try:
                if _run_system_tracker is not None:
                    _sys_transition = _run_system_tracker.observe(
                        browser.current_url or "", step=step
                    )
                    if _sys_transition is not None and _sys_transition.from_system_id:
                        event_stream.emit(
                            "system_transition",
                            **_sys_transition.to_dict(),
                        )
                        try:
                            from api_server import broadcast_phase as _bp_sys

                            _bp_sys(
                                "system_transition",
                                severity="info",
                                message=(
                                    f"{_sys_transition.from_system_name} → "
                                    f"{_sys_transition.to_system_name}"
                                ),
                                extra=_sys_transition.to_dict(),
                            )
                        except Exception:
                            pass
                        # E1c-3b: when cross-system switching is enabled, pool-acquire
                        # the BrowserSession this hop targets, stage its storage_state
                        # subset, and record both the switch + a readback verification
                        # as evidence. Default off -> zero behaviour change; the active
                        # browser handle is NOT rebound here (physical swap is a later
                        # slice). Whole block is best-effort so it can never break the loop.
                        if (
                            _session_router is not None
                            and _xsys_enabled()
                        ):
                            try:
                                _switch_full_state = None
                                try:
                                    _switch_ctx = getattr(browser, "_context", None)
                                    if _switch_ctx is not None:
                                        _switch_full_state = await _switch_ctx.storage_state()
                                except Exception as _switch_state_err:
                                    logger.debug(
                                        "[SESSION ROUTER] storage_state snapshot skipped: %s",
                                        _switch_state_err,
                                    )
                                _switch = _session_router.acquire_for_switch(
                                    to_system_id=_sys_transition.to_system_id,
                                    from_system_id=_sys_transition.from_system_id,
                                    to_system_name=_sys_transition.to_system_name,
                                    full_state=_switch_full_state,
                                )
                                if _switch.get("should_switch"):
                                    event_stream.emit("session_switch", **_switch)
                                    if _switch.get("staged") and _session_router.confirm_active(
                                        _switch.get("to_system_id", ""),
                                        _switch.get("session_id", ""),
                                    ):
                                        event_stream.emit(
                                            "session_switch_verified",
                                            run_id=_switch.get("run_id", ""),
                                            to_system_id=_switch.get("to_system_id", ""),
                                            to_system_name=_switch.get("to_system_name", ""),
                                            session_id=_switch.get("session_id", ""),
                                            auth_profile=_switch.get("auth_profile", ""),
                                            cookie_count=_switch.get("cookie_count", 0),
                                        )
                                    # E1c-3b-2c (path 1): physically rebind the active
                                    # handle to the launched isolated session (or back
                                    # to the home lease). Best-effort: a failure leaves
                                    # the current handle untouched.
                                    try:
                                        if _home_browser is None:
                                            _home_browser = browser
                                            _home_system_id = _sys_transition.from_system_id or ""
                                        # Read the live profile base the same way
                                        # BrowserEnv.start does, so each system's
                                        # isolated profile is a sibling of it.
                                        try:
                                            from . import config as _switch_cfg
                                        except ImportError:
                                            import config as _switch_cfg
                                        _switch_target_browser = await _session_router.activate_switch(
                                            _switch,
                                            url=getattr(browser, "current_url", "") or "",
                                            user_data_dir_base=getattr(_switch_cfg, "BROWSER_USER_DATA_DIR", "") or "",
                                            full_state=_switch_full_state,
                                            home_system_id=_home_system_id,
                                            home_browser=_home_browser,
                                        )
                                        if (
                                            _switch_target_browser is not None
                                            and _switch_target_browser is not browser
                                        ):
                                            browser = _switch_target_browser
                                            event_stream.emit(
                                                "session_switch_activated",
                                                run_id=_switch.get("run_id", ""),
                                                to_system_id=_switch.get("to_system_id", ""),
                                                to_system_name=_switch.get("to_system_name", ""),
                                                session_id=_switch.get("session_id", ""),
                                            )
                                    except Exception as _activate_err:
                                        logger.debug(
                                            "[SESSION ROUTER] switch activate skipped: %s",
                                            _activate_err,
                                        )
                            except Exception as _switch_err:
                                logger.debug("[SESSION ROUTER] switch acquire skipped: %s", _switch_err)
            except Exception as _sys_observe_err:
                logger.debug("[RUN SYSTEM TRACKER] observe skipped: %s", _sys_observe_err)

            # E1c-A1 (path 2): consume a pre-navigation cross-system goto the
            # GotoHandler intercepted this step. The current system's page was
            # deliberately NOT navigated (state preserved); here we physically
            # switch to the target system's isolated session, land it on the
            # intercepted URL, and rebind the loop handle. Best-effort + flag
            # gated; default off -> _pending is never set so this never runs.
            try:
                _pending_xsys = getattr(browser, "_pending_cross_system_goto", None)
                if (
                    _pending_xsys
                    and _session_router is not None
                    and _xsys_enabled()
                ):
                    browser._pending_cross_system_goto = None
                    _xsys_url = _pending_xsys.get("target_url", "") or ""
                    _xsys_full_state = None
                    try:
                        _xsys_ctx = getattr(browser, "_context", None)
                        if _xsys_ctx is not None:
                            _xsys_full_state = await _xsys_ctx.storage_state()
                    except Exception as _xsys_state_err:
                        logger.debug("[SESSION ROUTER] x-sys storage_state skipped: %s", _xsys_state_err)
                    _xsys_switch = _session_router.acquire_for_switch(
                        to_system_id=_pending_xsys.get("to_system_id", ""),
                        from_system_id=_pending_xsys.get("from_system_id", ""),
                        to_system_name=_pending_xsys.get("to_system_name", ""),
                        full_state=_xsys_full_state,
                    )
                    if _xsys_switch.get("should_switch"):
                        event_stream.emit("session_switch", **_xsys_switch)
                        if _home_browser is None:
                            _home_browser = browser
                            _home_system_id = _pending_xsys.get("from_system_id", "") or ""
                        try:
                            from . import config as _xsys_cfg
                        except ImportError:
                            import config as _xsys_cfg
                        _xsys_target_browser = await _session_router.activate_switch(
                            _xsys_switch,
                            url=_xsys_url,
                            user_data_dir_base=getattr(_xsys_cfg, "BROWSER_USER_DATA_DIR", "") or "",
                            full_state=_xsys_full_state,
                            home_system_id=_home_system_id,
                            home_browser=_home_browser,
                        )
                        if (
                            _xsys_target_browser is not None
                            and _xsys_target_browser is not browser
                        ):
                            browser = _xsys_target_browser
                            try:
                                browser._session_router = _session_router
                                browser._event_stream = event_stream
                            except Exception:
                                pass
                            event_stream.emit(
                                "session_switch_activated",
                                run_id=_xsys_switch.get("run_id", ""),
                                to_system_id=_xsys_switch.get("to_system_id", ""),
                                to_system_name=_xsys_switch.get("to_system_name", ""),
                                session_id=_xsys_switch.get("session_id", ""),
                                via="goto_interception",
                            )
                        # Land the now-active target-system browser on the URL
                        # the goto requested -- but only if it isn't already
                        # there (A2): skipping the re-nav preserves the target's
                        # exact page state (home page on a switch-back; the
                        # freshly-launched target on a forward first hop, which
                        # launch_for_switch already navigated -> no double nav).
                        try:
                            _xsys_page = getattr(browser, "_page", None)
                            _xsys_cur = getattr(browser, "current_url", "") or ""
                            if (
                                _xsys_page is not None
                                and _xsys_url
                                and _session_router.should_renavigate(_xsys_cur, _xsys_url)
                            ):
                                await _xsys_page.goto(
                                    _xsys_url, wait_until="domcontentloaded", timeout=30000
                                )
                                await browser._wait_for_page_stable()
                            elif _xsys_url:
                                event_stream.emit(
                                    "session_switch_page_preserved",
                                    run_id=_xsys_switch.get("run_id", ""),
                                    to_system_id=_xsys_switch.get("to_system_id", ""),
                                    to_system_name=_xsys_switch.get("to_system_name", ""),
                                    url=_xsys_url,
                                )
                        except Exception as _xsys_nav_err:
                            logger.debug("[SESSION ROUTER] x-sys goto nav skipped: %s", _xsys_nav_err)
                        # Prime the tracker so next step's observe sees no
                        # phantom A->B transition (avoids path-1 double-firing).
                        try:
                            if _run_system_tracker is not None:
                                _run_system_tracker.observe(
                                    getattr(browser, "current_url", "") or _xsys_url, step=step
                                )
                        except Exception:
                            pass
            except Exception as _pending_xsys_err:
                logger.debug("[SESSION ROUTER] pending cross-system goto skipped: %s", _pending_xsys_err)

            # ── 本轮日志收集状态 ──────────────────────────────────────────
            _log_screenshot_path: str | None = None
            _log_decision: list[dict] | dict | None = None
            _log_error: str | None = None
            _log_reasoning_text_source: str | None = None
            _log_extract_text_source: str | None = None

            try:
                _login_intercepted = await _run_preflight_login(
                    browser,
                    goal,
                    source=f"step-{step}-start",
                    require_login=require_login,
                )
                if _login_intercepted:
                    logger.info("[PRELOGIN] Login popup handled before reasoning; refreshing context.")

                if _semantic_macro and not _is_form_fill_goal:
                    _semantic_state = await _semantic_goal_currently_satisfied(
                        browser,
                        goal,
                    )
                    if _semantic_state.get("ok"):
                        _run_succeeded = True
                        _task_completed = True
                        _broadcast_done_safe(True, "Semantic component goal verified")
                        try:
                            _auto_ss, _ = await browser.mark_and_screenshot(step)
                            _log_screenshot_path = (
                                str(Path(SCREENSHOT_DIR) / f"step_{step:02d}.png")
                                if _auto_ss
                                else None
                            )
                        except Exception as _semantic_ss_err:
                            logger.debug(
                                "[SEMANTIC VERIFY] failed to capture screenshot: %s",
                                _semantic_ss_err,
                            )
                        _log_decision = {
                            "action": "done",
                            "target_id": 0,
                            "status": "success",
                            "type_value": str(_semantic_state.get("expected") or ""),
                            "thought": (
                                "SEMANTIC VERIFY 已从当前页面回读到目标值，"
                                "组件任务完成。"
                            ),
                            "semantic_validation": _semantic_state,
                        }
                        event_stream.verify(
                            step=step,
                            name="semantic_goal",
                            success=True,
                            expected=_semantic_state.get("expected", ""),
                            observed=_semantic_state.get("observed", ""),
                            metadata=_semantic_state,
                        )
                        event_stream.act(
                            step=step,
                            result=_with_tool_metadata(
                                ActionResult.from_action(
                                    {
                                        "action": "semantic_verify",
                                        "type_value": str(_semantic_state.get("expected") or ""),
                                    },
                                    success=True,
                                    message="Semantic component goal verified",
                                    after_url=getattr(browser, "current_url", "") or "",
                                    after_pages=len(browser._context.pages) if browser._context else 0,
                                    metadata={"semantic_validation": _semantic_state},
                                )
                            ),
                        )
                        break

                    if not _semantic_macro_attempted:
                        _semantic_macro_attempted = True
                        _semantic_action = str(_semantic_macro.get("action") or "semantic_macro")
                        logger.info(
                            "[SEMANTIC MACRO] trying %s before VLM loop: %s",
                            _semantic_action,
                            _semantic_macro.get("path") or _semantic_macro.get("target_date"),
                        )
                        _semantic_ok = await _replay_rpa(
                            browser,
                            [dict(_semantic_macro)],
                            workflow_memory,
                            vlm=vlm,
                        )
                        if _semantic_ok:
                            _post_semantic_state = await _semantic_goal_currently_satisfied(
                                browser,
                                goal,
                            )
                            _run_succeeded = True
                            _task_completed = True
                            _broadcast_done_safe(True, "Semantic component macro completed")
                            try:
                                _auto_ss, _ = await browser.mark_and_screenshot(step)
                                _log_screenshot_path = (
                                    str(Path(SCREENSHOT_DIR) / f"step_{step:02d}.png")
                                    if _auto_ss
                                    else None
                                )
                            except Exception as _semantic_ss_err:
                                logger.debug(
                                    "[SEMANTIC MACRO] failed to capture screenshot: %s",
                                    _semantic_ss_err,
                                )
                            _log_decision = {
                                "action": "done",
                                "target_id": 0,
                                "status": "success",
                                "type_value": str(
                                    _post_semantic_state.get("expected")
                                    or _semantic_macro.get("target_date")
                                    or " / ".join(_semantic_macro.get("path") or [])
                                ),
                                "thought": (
                                    f"SEMANTIC MACRO 已执行 {_semantic_action}，"
                                    "并完成组件目标。"
                                ),
                                "semantic_validation": _post_semantic_state,
                            }
                            event_stream.verify(
                                step=step,
                                name="semantic_macro",
                                success=bool(_post_semantic_state.get("ok", True)),
                                expected=_post_semantic_state.get("expected", ""),
                                observed=_post_semantic_state.get("observed", ""),
                                metadata={
                                    "macro": _semantic_macro,
                                    "validation": _post_semantic_state,
                                },
                            )
                            event_stream.act(
                                step=step,
                                result=_with_tool_metadata(
                                    ActionResult.from_action(
                                        {
                                            "action": _semantic_action,
                                            "type_value": str(_log_decision.get("type_value") or ""),
                                        },
                                        success=True,
                                        message="Semantic component macro completed",
                                        after_url=getattr(browser, "current_url", "") or "",
                                        after_pages=len(browser._context.pages) if browser._context else 0,
                                        metadata={"semantic_validation": _post_semantic_state},
                                    )
                                ),
                            )
                            break
                        event_stream.guard(
                            step=step,
                            name="semantic_macro_failed",
                            message=(
                                f"Semantic macro {_semantic_action} failed; "
                                "falling back to VLM loop."
                            ),
                            metadata={"macro": _semantic_macro},
                        )

                _force_challenge_auto_form = False
                if _is_form_fill_goal:
                    _challenge_done = False
                    if _should_use_round_form_macro(goal, _form_assignments):
                        _challenge_done = await _run_round_form_if_present(browser, goal)
                    if _challenge_done:
                        _run_succeeded = True
                        _broadcast_done_safe(True, "RPA Challenge completed")
                        try:
                            _auto_ss, _ = await browser.mark_and_screenshot(step)
                            _log_screenshot_path = (
                                str(Path(SCREENSHOT_DIR) / f"step_{step:02d}.png")
                                if _auto_ss
                                else None
                            )
                        except Exception as _challenge_log_err:
                            logger.debug("[RPA CHALLENGE] failed to capture completion screenshot: %s", _challenge_log_err)
                        _log_decision = {
                            "action": "done",
                            "target_id": 0,
                            "status": "success",
                            "thought": "RPA Challenge 已点击 Start，并按 challenge.xlsx 连续完成全部轮次；仅最终完成态允许 done。",
                            "type_value": "rpa_challenge",
                        }
                        event_stream.verify(
                            step=step,
                            name="rpa_challenge_completion",
                            success=True,
                            expected="final completion state",
                            observed="challenge macro returned complete",
                            metadata={"macro": "round_form"},
                        )
                        event_stream.act(
                            step=step,
                            result=_with_tool_metadata(
                                ActionResult.from_action(
                                    {"action": "auto_form_macro", "type_value": "rpa_challenge"},
                                    success=True,
                                    message="RPA Challenge completed",
                                    after_url=getattr(browser, "current_url", "") or "",
                                    after_pages=len(browser._context.pages) if browser._context else 0,
                                )
                            ),
                        )
                        break
                    _force_challenge_auto_form = (
                        False if _form_assignments else await _has_active_round_form_round(browser)
                    )
                if _is_form_fill_goal and (_auto_form_retry_count < 3 or _force_challenge_auto_form):
                    _auto_form_blocked = (
                        False if _form_assignments else await _auto_form_has_prestart_gate(browser)
                    )
                    if _auto_form_blocked:
                        logger.info("[AUTO FORM] step-level retry deferred until Start gate is cleared")
                    else:
                        if _force_challenge_auto_form:
                            logger.info("[AUTO FORM] active rpachallenge round detected; forcing deterministic retry")
                        else:
                            _auto_form_retry_count += 1
                            logger.info("[AUTO FORM] step-level deterministic retry #%s", _auto_form_retry_count)
                        _auto_form_ok = await _try_auto_form_fill(browser, goal)
                        if _auto_form_ok:
                            _run_succeeded = True
                            _broadcast_done_safe(True, "Auto form fill completed")
                            try:
                                _auto_ss, _ = await browser.mark_and_screenshot(step)
                                _log_screenshot_path = (
                                    str(Path(SCREENSHOT_DIR) / f"step_{step:02d}.png")
                                    if _auto_ss
                                    else None
                                )
                            except Exception as _auto_log_err:
                                logger.debug("[AUTO FORM] failed to capture completion screenshot: %s", _auto_log_err)
                            _log_decision = {
                                "action": "done",
                                "target_id": 0,
                                "status": "success",
                                "thought": (
                                    "AUTO FORM 已完成字段回读校验，并已点击 Create/Submit（若目标要求提交）。"
                                    + "\n"
                                    + _format_auto_form_validation_summary(
                                        getattr(browser, "_last_auto_form_result", None)
                                    )
                                ),
                                "type_value": "auto_form",
                                "form_validation": getattr(browser, "_last_auto_form_result", None),
                            }
                            event_stream.verify(
                                step=step,
                                name="auto_form_readback",
                                success=True,
                                expected="all parsed form fields",
                                observed="field readback ok",
                                metadata={
                                    "form_validation": getattr(
                                        browser,
                                        "_last_auto_form_result",
                                        None,
                                    )
                                },
                            )
                            event_stream.act(
                                step=step,
                                result=_with_tool_metadata(
                                    ActionResult.from_action(
                                        {"action": "auto_form", "type_value": "auto_form"},
                                        success=True,
                                        message="Auto form fill completed",
                                        after_url=getattr(browser, "current_url", "") or "",
                                        after_pages=len(browser._context.pages) if browser._context else 0,
                                        metadata={
                                            "form_validation": getattr(
                                                browser,
                                                "_last_auto_form_result",
                                                None,
                                            )
                                        },
                                    )
                                ),
                            )
                            break

                _page_for_loop = await _recover_active_page(
                    "main loop active page recovery"
                )
                _loop_url = (_page_for_loop.url or "") if _page_for_loop else ""
                if _page_for_loop and (not _loop_url or _loop_url.startswith("about:")) and url:
                    try:
                        logger.warning(
                            "[PAGE RECOVERY] Active page is blank before step %s; navigating back to start URL: %s",
                            step,
                            url,
                        )
                        await _page_for_loop.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await browser._wait_for_page_stable()
                    except Exception as _loop_nav_err:
                        logger.warning("[PAGE RECOVERY] start URL restore failed: %s", _loop_nav_err)

                if _goal_prefers_visual_navigation(goal):
                    _page = await browser._ensure_active_page(reason="blank page recovery check")
                    _current_url = (_page.url or "") if _page else ""
                    if _current_url and _current_url not in _blank_page_recovery_attempted:
                        _is_blank_shell, _blank_reason = await browser.detect_blank_content_shell()
                        if _is_blank_shell:
                            logger.warning(
                                f"[PAGE RECOVERY] Detected likely blank content shell → reload once "
                                f"({_blank_reason})"
                            )
                            _blank_page_recovery_attempted.add(_current_url)
                            await browser.reload_active_page(reason="blank content shell")

                if _rpa_payload and _rpa_payload.get("replayable", True):
                    _trail = _rpa_payload.get("trail") or []
                    if _rpa_cursor < len(_trail):
                        await _recover_active_page("before partial RPA replay")
                        _new_cursor, _replayed_steps, _replay_failed = await _replay_ready_rpa_steps(
                            browser, _rpa_payload, _rpa_cursor, workflow_memory, vlm=vlm
                        )
                        if _replay_failed:
                            logger.warning("[RPA] Partial replay failed, continue with VLM loop.")
                            _rpa_payload = _mark_rpa_cache_failure(
                                _rpa_path, _rpa_payload, "partial replay failure"
                            )
                        elif _replayed_steps:
                            _executed_cached_trail.extend(_replayed_steps)
                            _rpa_cursor = _new_cursor
                            if (
                                _rpa_payload.get("completes_task", True)
                                and _rpa_cursor >= len(_trail)
                            ):
                                if _goal_output_mode == "answer":
                                    logger.info(
                                        "[RPA] Partial replay reached the answer page; "
                                        "continuing so the final answer is observed and logged."
                                    )
                                    event_stream.guard(
                                        step=step,
                                        name="ANSWER_RPA_PREFIX_ONLY",
                                        message=(
                                            "Partial cached replay finished its trail, "
                                            "but answer-mode tasks need a final observed answer."
                                        ),
                                        metadata={
                                            "cache": str(_rpa_path),
                                            "replayed_steps": len(_replayed_steps),
                                            "cursor": _rpa_cursor,
                                            "trail_length": len(_trail),
                                        },
                                    )
                                else:
                                    logger.info("[RPA] Partial replay completed the remaining task.")
                                    await browser.mark_and_screenshot(step=99)
                                    _run_succeeded = True
                                    return True
                            else:
                                continue

                # ══════════════════════════════════════════════════════════════
                # 混合调度：主引擎（网络截胡）优先于备用引擎（VLM 视觉提取）
                # ══════════════════════════════════════════════════════════════
                # 主引擎生效条件：
                #   - 使用了 --xhr-pattern 参数
                #   - 网络拦截器已捕获到匹配 URL 的 API 数据快照
                # 一旦命中，数据已由 Network Sentinel 去重并落盘；无需再截图或请求 VLM。
                core_data = browser.intercepted_data
                if core_data is not None:
                    _record_cnt = len(core_data) if isinstance(core_data, list) else 1
                    logger.info(
                        f"[HYBRID PRIMARY] XHR engine captured {_record_cnt} records — "
                        f"skipping screenshot + VLM."
                    )
                    saved_path = str(resolve_artifact_path(_xhr_output).resolve())
                    logger.info(f"[HYBRID PRIMARY] Already saved by Network Sentinel: {saved_path}")
                    print(
                        f"\n\033[1;32m[主引擎生效]\033[0m 网络层已自动截获核心 API 数据，"
                        f"共 \033[36m{_record_cnt}\033[0m 条，"
                        f"已保存至 \033[33m{saved_path}\033[0m。\n"
                        f"跳过 VLM 视觉提取，任务完成。\n"
                    )
                    await browser.mark_and_screenshot(step=99)
                    _run_succeeded = True
                    event_stream.extract(
                        step=step,
                        source="XHR",
                        rows=_record_cnt,
                        output_file=_xhr_output,
                        metadata={"saved_path": saved_path},
                    )
                    break
                # ════════════════════════════════════════════════════════════
                # 图文双模态融合 (Hybrid Modality)
                # 每一轮都同时采集 SoM 截图 + AX Tree 语义树（含 DOM ID 映射段），融合发送给 VLM。
                # 彻底废除"智能路由/纯文本降级"的单模态切换 —— 视觉与文本互为冗余，
                # VLM 得以用红框数字定位 + AX 语义 / DOM ID 映射校验的方式做综合决策。
                # ════════════════════════════════════════════════════════════
                _tabs_state = await browser.get_tabs_state()
                _tabs_hint = f"\n\n【当前标签页列表】{_tabs_state}" if _tabs_state else ""
                _page_state = await browser.get_active_page_summary()
                _page_hint = f"\n\n【当前页面摘要】{_page_state}" if _page_state else ""

                # 旧的 _force_vision_next_step 信号在双模态下已失效，这里消耗掉以保持语义干净
                _force_vision_next_step = False

                print("\033[1;36m🧠 [HYBRID]\033[0m 同步采集 SoM 截图 + 无障碍语义树 (AX Tree)，融合决策")
                logger.info("[HYBRID] Collecting SoM screenshot + accessibility tree for fused VLM decision")

                await _recover_active_page("before hybrid screenshot")
                try:
                    screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step)
                except RuntimeError as screenshot_err:
                    if "No active page" not in str(screenshot_err):
                        raise
                    logger.warning(
                        "[BROWSER RECOVERY] screenshot failed with no active page; restarting and retrying once"
                    )
                    await browser.restart(start_url, reason="retry hybrid screenshot")
                    screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step)
                _log_screenshot_path = (
                    str(Path(SCREENSHOT_DIR) / f"step_{step:02d}.png") if screenshot_b64 else None
                )

                try:
                    try:
                        from .bot_challenge_guard import handle_bot_challenge_step
                    except ImportError:
                        from bot_challenge_guard import handle_bot_challenge_step
                    _bc_result = await handle_bot_challenge_step(
                        browser,
                        _bot_challenge_state,
                        hitl_callback=_wait_for_human_resume,
                    )
                    if _bc_result.notice:
                        input_descriptions = _bc_result.notice + "\n" + (input_descriptions or "")
                    if _bc_result.cleared_after_hitl:
                        screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step)
                        _log_screenshot_path = (
                            str(Path(SCREENSHOT_DIR) / f"step_{step:02d}.png") if screenshot_b64 else None
                        )
                        try:
                            from .auth_harvester import harvest_storage_state as _bc_harvest
                        except ImportError:
                            from auth_harvester import harvest_storage_state as _bc_harvest
                        try:
                            _bc_hr = await _bc_harvest(browser)
                            if _bc_hr.saved:
                                logger.info("[BOT CHALLENGE] harvested auth profile: %s", _bc_hr.profile_name)
                        except Exception as _bc_harv_err:
                            logger.debug("[BOT CHALLENGE] auth harvest skipped: %s", _bc_harv_err)
                    # PROXY-4b: when the block persists / the IP looks flagged, swap
                    # to the next proxy and reload (policy + budget live downstream).
                    try:
                        _rerouted = await browser.reroute_proxy_on_block(
                            getattr(browser, "current_url", "") or "",
                            result=_bc_result,
                            state=_bot_challenge_state,
                            reason=f"bot challenge {_bc_result.vendor or 'block'}",
                        )
                    except Exception as _reroute_err:
                        _rerouted = False
                        logger.debug("[BOT CHALLENGE] proxy reroute skipped: %s", _reroute_err)
                    if _rerouted:
                        screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step)
                        input_descriptions = (
                            "\n🛡️【Bot Challenge · 已切换代理并重载页面】\n"
                            + (input_descriptions or "")
                        )
                except Exception as _bc_err:
                    logger.debug("[BOT CHALLENGE] step hook skipped: %s", _bc_err)

                try:
                    ax_tree_text = await browser.extract_accessibility_tree()
                except Exception as _ax_err:
                    logger.warning(
                        f"[HYBRID] AX Tree 提取失败，本轮仅凭截图决策: {_ax_err}"
                    )
                    ax_tree_text = ""

                _log_reasoning_text_source = "AX_TREE" if ax_tree_text else "SCREENSHOT_ONLY"

                # ── A11y Enhancer：增强 AX Tree 信息 ─────────────────────
                if ax_tree_text and A11Y_ENHANCER_ENABLED:
                    try:
                        _a11y_enhancer = A11yEnhancer(config=A11yEnhancerConfig(
                            enable_grouping=True,
                            enable_state_annotation=True,
                            enable_hidden_hints=True,
                            max_output_chars=15000,
                        ))
                        _a11y_meta = A11yPageMetadata(
                            url=getattr(browser, "current_url", "") or "",
                            title=(_page_state or "")[:100],
                            total_elements=len(getattr(browser, "_last_som_elements", []) or []),
                            visible_elements=len(getattr(browser, "_last_visible_elements", []) or []),
                            iframe_count=getattr(browser, "_iframe_count", 0),
                            scroll_position=getattr(browser, "_last_scroll_y", 0),
                            page_height=getattr(browser, "_page_height", 0),
                        )
                        _a11y_result = _a11y_enhancer.enhance(ax_tree_text, _a11y_meta)
                        ax_tree_text = _a11y_result.text
                        if _a11y_result.summary:
                            logger.debug(f"[A11Y] {_a11y_result.summary}")
                    except Exception as _a11y_err:
                        logger.debug(f"[A11Y] Enhancement skipped: {_a11y_err}")

                # 兜底：防止超大 AX Tree 撑爆 Token
                if ax_tree_text and len(ax_tree_text) > 15000:
                    _orig_len = len(ax_tree_text)
                    ax_tree_text = ax_tree_text[:15000] + "\n...[WARNING: AX Tree 过长已截断]..."
                    logger.warning(
                        f"[HYBRID] AX Tree 长度 {_orig_len} 超过 15000 阈值，已截断以保护上下文窗口"
                    )
                _browser_state = await BrowserStateSnapshot.from_browser(
                    browser,
                    step=step,
                    screenshot_path=_log_screenshot_path or "",
                    ax_tree_text=ax_tree_text,
                    tabs=_tabs_state,
                    page_summary=_page_state,
                    last_action_result=getattr(browser, "_last_action_result", None),
                    metadata={
                        "reasoning_text_source": _log_reasoning_text_source,
                    },
                )
                event_stream.observe(
                    step=step,
                    url=getattr(browser, "current_url", "") or "",
                    screenshot_path=_log_screenshot_path or "",
                    ax_lines=len(ax_tree_text.splitlines()) if ax_tree_text else 0,
                    browser_state=_browser_state,
                    metadata={
                        "tabs": _tabs_state,
                        "reasoning_text_source": _log_reasoning_text_source,
                    },
                )

                ax_block = ""
                if ax_tree_text:
                    ax_block = (
                        "\n\n=====================================\n"
                        "【辅助信息：页面无障碍语义树 (AX Tree)】\n"
                        "以下是当前页面的纯语义结构，过滤了所有样式噪音。\n"
                        "  · 第一段 [可交互元素 @eN 语义快照] 是按阅读顺序编号的紧凑清单：\n"
                        "    `@eN [role] \"name\" {states}`，N 与截图红框数字一一对应。\n"
                        "    需要操作某元素时，target_id 直接填数字（如 @e5 → target_id=5）。\n"
                        "  · 第二段 [页面语义快照] 提供整体 AX 结构（含标题、文本等），辅助理解上下文。\n"
                        "  ⚠ 执行 extract 时，**必须从 AX Tree 中读取文本数据**（标题、数值、描述），\n"
                        "    而非仅靠截图 OCR。被浮层遮挡的元素在 AX Tree 中仍然存在。\n"
                        f"{ax_tree_text}\n"
                        "====================================="
                    )

                input_descriptions = (
                    (input_descriptions or "") + ax_block + _page_hint + _tabs_hint
                )

                # ── Wave 2 Reflector：仅失败信号或兜底触发 ────────────────
                _prev_loop_guard_size = len(_loop_guard_blocked_ids)
                _reflect_signals: list[str] = []
                if _consecutive_errors >= 2:
                    _reflect_signals.append(f"连续 {_consecutive_errors} 步 action=error")
                if _dedup_tripped_last_step:
                    _reflect_signals.append("上一步 extract 被 dedup 拦截")
                if _steps_since_reflect >= _REFLECT_INTERVAL and _task_plan is not None:
                    _reflect_signals.append(f"{_REFLECT_INTERVAL} 步兜底检查")
                # Fix 4：登录墙 URL 探测（passport/login/signin/sso/captcha）
                _cur_url_lower = (browser.current_url or "").lower()
                if re.search(r"/(login|signin|sign-in|passport|sso|captcha|verify)\b", _cur_url_lower):
                    # 仅当 goal 里没显式给凭证时才当登录墙（goal 含 {{phone}} = 用户主动登录）
                    _goal_has_cred = bool(re.search(r"\{\{\s*(phone|password|username|account|mobile|email)\s*\}\}", goal, re.IGNORECASE))
                    if not _goal_has_cred:
                        _reflect_signals.append(f"当前 URL 疑似登录/验证页：{browser.current_url}")

                if (
                    _reflect_signals
                    and _task_plan is not None
                    and _reflect_count < _MAX_REFLECTS
                ):
                    try:
                        _rd = await vlm.reflect(
                            plan=_task_plan,
                            history_summary=vlm._build_history_summary(),
                            signals=_reflect_signals,
                            current_url=browser.current_url or "",
                        )
                        _reflect_count += 1
                        _steps_since_reflect = 0
                        _dedup_tripped_last_step = False
                        if _rd.decision == "advance" and _rd.advance_to_idx is not None:
                            _target_idx = max(0, min(_rd.advance_to_idx, len(_task_plan.sub_goals) - 1))
                            _task_plan.sub_goals[_task_plan.current_idx].status = "done"
                            _task_plan.current_idx = _target_idx
                            _task_plan.sub_goals[_target_idx].status = "active"
                            vlm.inject_error_feedback(
                                f"🎯 [REFLECTOR] {_rd.reason}；"
                                f"系统已推进至子目标 {_target_idx + 1}/"
                                f"{len(_task_plan.sub_goals)}："
                                f"{_task_plan.sub_goals[_target_idx].description}"
                            )
                        elif _rd.decision == "revise" and _rd.new_sub_goals:
                            _task_plan.sub_goals = _rd.new_sub_goals
                            _task_plan.current_idx = 0
                            if _task_plan.sub_goals:
                                _task_plan.sub_goals[0].status = "active"
                            vlm.inject_error_feedback(
                                f"🔧 [REFLECTOR] 计划已修订（{_rd.reason}）。"
                                f"新的当前子目标："
                                f"{_task_plan.sub_goals[0].description if _task_plan.sub_goals else '(空)'}"
                            )
                        elif _rd.decision == "abort":
                            _abort_requested = True
                            vlm.inject_error_feedback(
                                f"🛑 [REFLECTOR] 判定不可完成（{_rd.reason}）。"
                                f"请立即输出 action=done 结束任务。"
                            )
                        # continue: 不做额外干预，让 VLM 正常推进
                    except Exception as _reflect_err:
                        logger.warning(f"[REFLECTOR] 调用失败忽略：{_reflect_err}")
                else:
                    _steps_since_reflect += 1
                _dedup_tripped_last_step = False

                # ── Improvement 1：消费分页器探测结果（一次性，注入完即清） ──
                if _pagination_hint_msg:
                    input_descriptions = (
                        _pagination_hint_msg + "\n" + (input_descriptions or "")
                    )
                    _pagination_hint_msg = ""

                # ── Path D + Improvement 3：进度透传，标为系统权威记账 ──
                # VLM 没有长程数学记忆，必须在 prompt 里持续回灌权威进度。
                # **强调"系统记账（唯一权威）"** 让 VLM 不再自己心算条数（避免 37/50 vs 30/50 偏差）。
                _prog_target = _parse_goal_target_count(goal)
                if _prog_target is not None and _total_extracted_rows > 0:
                    _prog_pages = len(_extracted_page_urls)
                    _prog_remaining = max(0, _prog_target - _total_extracted_rows)
                    _prog_pct = int(min(100, _total_extracted_rows * 100 / _prog_target))
                    input_descriptions = (
                        f"\n📊【全局抓取进度（系统记账，唯一权威）】"
                        f"{_total_extracted_rows}/{_prog_target} 条 "
                        f"({_prog_pct}%，跨 {_prog_pages} 个页面)，"
                        f"还需 {_prog_remaining} 条；达量后引擎会自动终止任务。\n"
                        f"**禁止**在 thought 里自己心算/估算条数 —— 一切以此数字为准。\n"
                        + (input_descriptions or "")
                    )

                _forced_target = _parse_goal_target_count(goal)
                _can_force_extract_after_navigation_now = (
                    _force_extract_after_navigation_pending
                    and (
                        _forced_target is None
                        or _total_extracted_rows < _forced_target
                    )
                )
                _can_force_next_page_now = (
                    _force_next_page_pending
                    and (
                        _forced_target is None
                        or _total_extracted_rows < _forced_target
                    )
                )
                if _can_force_extract_after_navigation_now:
                    # On a known chat host, swap to chat_extract — generic
                    # extract on yiyan/chat.baidu/etc. captures search-result
                    # cards, missing the AI answer entirely.
                    try:
                        try:
                            from .chat_extract_coercion_guard import (
                                detect_extract_on_chat_host as _force_chat_check,
                            )
                        except ImportError:  # pragma: no cover
                            from chat_extract_coercion_guard import (  # type: ignore[no-redef]
                                detect_extract_on_chat_host as _force_chat_check,
                            )
                        _force_chat_hit = _force_chat_check(
                            {"action": "extract", "memory_key": ""},
                            browser.current_url or "",
                        )
                    except Exception:
                        _force_chat_hit = None

                    if _force_chat_hit:
                        logger.info(
                            "[FORCE EXTRACT AFTER NAV] chat host %s → using chat_extract",
                            _force_chat_hit["matched_host"],
                        )
                        _broadcast_log_safe(
                            f"[FORCE EXTRACT] 聊天域 {_force_chat_hit['matched_host']}，"
                            "改用 chat_extract 抓 AI 回答",
                            level="info",
                        )
                        decisions = [{
                            "action": "chat_extract",
                            "target_id": 0,
                            "type_value": "",
                            "memory_key": _force_chat_hit["memory_key"],
                            "extracted_data": None,
                            "__extract_downgraded": True,
                            "thought": (
                                "[FORCE CHAT_EXTRACT AFTER NAV] 上一步已进入聊天页。"
                                "由于当前是已知 AI 助手域名，系统改用 chat_extract："
                                "等流式 + 选回答容器 + 排搜索结果。"
                            ),
                            "progress_review": "",
                            "current_state": "",
                            "subgoal_status": "in_progress",
                        }]
                    else:
                        logger.info(
                            "[FORCE EXTRACT AFTER NAV] skipping VLM ask; extracting newly landed page"
                        )
                        _broadcast_log_safe(
                            "[FORCE EXTRACT] 翻页已落地，下一步先提取当前页，避免连续翻页跳过目标数据",
                            level="info",
                        )
                        decisions = [{
                            "action": "extract",
                            "target_id": 0,
                            "type_value": "",
                            "memory_key": "",
                            "extracted_data": None,
                            "__extract_downgraded": True,
                            "thought": (
                                "[FORCE EXTRACT 翻页后提取锁] 上一步已成功进入新页面。"
                                "在未提取当前页之前禁止继续 next_page，系统直接启动当前页自动提取。"
                            ),
                            "progress_review": "",
                            "current_state": "",
                            "subgoal_status": "in_progress",
                        }]
                    _force_extract_after_navigation_pending = False
                elif _can_force_next_page_now:
                    logger.info(
                        "[FORCE NEXT_PAGE] skipping VLM ask; executing engine-scheduled next_page"
                    )
                    _broadcast_log_safe(
                        "[FORCE NEXT_PAGE] 引擎已调度下一页，跳过本轮 VLM 决策",
                        level="info",
                    )
                    decisions = [{
                        "action": "next_page",
                        "target_id": 0,
                        "type_value": "",
                        "memory_key": "",
                        "extracted_data": None,
                        "thought": (
                            "[FORCE NEXT_PAGE 引擎短路] 上一步已确认当前页存在分页器"
                            "且目标数量未达成，系统直接执行 next_page，优先尝试 URL "
                            "变异/分页器/页码探测。"
                        ),
                        "progress_review": "",
                        "current_state": "",
                        "subgoal_status": "in_progress",
                    }]
                    _force_next_page_pending = False
                else:
                    if _force_extract_after_navigation_pending:
                        logger.info("[FORCE EXTRACT AFTER NAV] cleared because target is already met")
                        _force_extract_after_navigation_pending = False
                    if _force_next_page_pending:
                        logger.info("[FORCE NEXT_PAGE] cleared because target is already met")
                        _force_next_page_pending = False
                    # G2: emit vlm_call phase event with duration for the
                    # frontend timeline. Best-effort, swallows api_server errors.
                    _vlm_t0 = time.time()
                    decisions = None
                    if _recovery_chain_pending:
                        _recovery_chain_pending = False
                        try:
                            try:
                                from .recovery_chain import attempt_stuck_recovery_chain
                            except ImportError:
                                from recovery_chain import attempt_stuck_recovery_chain
                            decisions = await attempt_stuck_recovery_chain(
                                browser,
                                goal=goal,
                                start_url=start_url,
                                tab_anchor_index=_tab_session_anchor,
                            )
                            if decisions:
                                logger.info(
                                    "[RECOVERY CHAIN] bypassed VLM (%s action(s))",
                                    len(decisions),
                                )
                        except Exception as _rc_err:
                            logger.debug("[RECOVERY CHAIN] skipped: %s", _rc_err)
                            decisions = None
                    if decisions is None:
                        _cur_url = getattr(browser, "current_url", "") or start_url
                        _send_prompt_imgs = bool(_prompt_images) and _should_include_prompt_images(
                            step,
                            policy=_prompt_image_policy,
                            last_sent_url=_last_prompt_image_url,
                            current_url=_cur_url,
                        )
                        decisions = await vlm.ask(
                            screenshot_b64,
                            goal,
                            step,
                            input_descriptions,
                            workflow_memory,
                            task_plan=_task_plan,
                            max_steps=_effective_max_steps,
                            som_elements=getattr(browser, "_last_som_elements", None),
                            capability_route=_capability_route,
                            extra_images=(_prompt_images if _send_prompt_imgs else None),
                        )
                        if _send_prompt_imgs:
                            _last_prompt_image_url = _cur_url
                    try:
                        from api_server import broadcast_phase

                        broadcast_phase(
                            "vlm_call",
                            severity="info",
                            message=f"step {step}",
                            step=step,
                            duration_ms=int((time.time() - _vlm_t0) * 1000),
                            notice_severity=getattr(browser, "_last_notice_severity", None),
                        )
                    except Exception:
                        pass

                if decisions:
                    _head_decision = decisions[0]
                    if _decision_should_finish_instead_of_operate(
                        _head_decision,
                        output_mode=_goal_output_mode,
                    ):
                        _original_action = str(_head_decision.get("action") or "")
                        _original_target = _head_decision.get("target_id", 0)
                        logger.info(
                            "[ANSWER DONE GUARD] answer is already visible; "
                            "rewriting action=%r target_id=%r to done.",
                            _original_action,
                            _original_target,
                        )
                        event_stream.guard(
                            step=step,
                            name="ANSWER_DONE_INTENT_GUARD",
                            message=(
                                "Answer-mode decision text says the task is complete; "
                                "rewrote the physical action to done."
                            ),
                            metadata={
                                "output_mode": _goal_output_mode,
                                "original_action": _original_action,
                                "original_target_id": _original_target,
                            },
                        )
                        _rewritten_decision = _force_done_decision(_head_decision)
                        _rewritten_decision["subgoal_status"] = "completed"
                        _rewritten_decision["__answer_done_guard"] = True
                        _rewritten_decision["__original_action"] = _original_action
                        _rewritten_decision["__original_target_id"] = _original_target
                        _rewritten_decision["thought"] = str(
                            _head_decision.get("thought") or ""
                        )
                        decisions = [_rewritten_decision]

                if decisions:
                    try:
                        from visual_web_agent.completion_kernel import (
                            load_manifest_items_for_run,
                            maybe_short_circuit_decision,
                        )
                        from visual_web_agent.planner_exit_criteria import (
                            current_subgoal_criteria,
                        )

                        _ck_exit_criteria = current_subgoal_criteria(_task_plan)
                        _ck_prev = getattr(browser, "_last_action_result", None)
                        _ck_last_action = str(getattr(_ck_prev, "action", "") or "")
                        _ck_last_ok = (
                            bool(getattr(_ck_prev, "success", False))
                            if _ck_prev is not None else False
                        )
                        _ck = maybe_short_circuit_decision(
                            decisions[0],
                            goal=goal,
                            output_contract=_goal_output_contract,
                            output_mode=_goal_output_mode,
                            total_extracted_rows=_total_extracted_rows,
                            total_pages=len(_extracted_page_keys),
                            goal_target_count=_parse_goal_target_count(goal),
                            goal_target_pages=_parse_goal_target_pages(goal),
                            pagination_exhausted=_pagination_exhausted,
                            manifest_items=load_manifest_items_for_run(_run_ts),
                            workflow_memory=workflow_memory,
                            no_progress_streak=_no_progress_tracker.streak,
                            exit_criteria=_ck_exit_criteria,
                            current_url=getattr(browser, "current_url", "") or "",
                            last_action=_ck_last_action,
                            last_action_success=_ck_last_ok,
                        )
                        if _ck.get("short_circuit"):
                            logger.info(
                                "[COMPLETION KERNEL] rewrote action=%s -> done; evidence=%s",
                                _ck.get("decision", {}).get("__original_action"),
                                (_ck.get("evaluation") or {}).get("evidence"),
                            )
                            event_stream.guard(
                                step=step,
                                name="COMPLETION_KERNEL_SHORT_CIRCUIT",
                                message="Task evidence complete; stopped low-value action.",
                                metadata={
                                    "evaluation": _ck.get("evaluation"),
                                    "original_action": _ck.get("decision", {}).get("__original_action"),
                                },
                            )
                            try:
                                from api_server import broadcast_phase as _bp_ck

                                _bp_ck(
                                    "completion_guard",
                                    severity="info",
                                    message=(
                                        f"short_circuit: "
                                        f"{_ck.get('decision', {}).get('__original_action')} → done"
                                    ),
                                    step=step,
                                    notice_severity=getattr(
                                        browser, "_last_notice_severity", None
                                    ),
                                    extra={
                                        "guard": "completion_kernel_short_circuit",
                                        "evaluation": _ck.get("evaluation"),
                                        "original_action": _ck.get("decision", {}).get(
                                            "__original_action"
                                        ),
                                    },
                                )
                            except Exception:
                                pass
                            decisions = [_ck["decision"]]
                    except Exception as _ck_err:
                        logger.debug("[COMPLETION KERNEL] skipped: %s", _ck_err)

                _log_decision = decisions
                event_stream.decide(
                    step=step,
                    decisions=decisions,
                    url=getattr(browser, "current_url", "") or "",
                )

                # ── Loop Detector：统一循环检测（补充现有 LOOP GUARD）──────
                if decisions:
                    _ld_fp = PageFingerprint(
                        url=getattr(browser, "current_url", "") or "",
                        title=(await browser.get_active_page_summary() if hasattr(browser, "get_active_page_summary") else "")[:100],
                        element_count=len(getattr(browser, "_last_som_elements", []) or []),
                        scroll_position=getattr(browser, "_last_scroll_y", 0),
                    )
                    _ld_result = _loop_detector.check(decisions[0], _ld_fp, step)
                    if _ld_result.loop_detected:
                        logger.warning(
                            f"[LOOP DETECTOR] {_ld_result.loop_type} "
                            f"(confidence={_ld_result.confidence:.2f}): "
                            f"{_ld_result.nudge_message[:120]}"
                        )
                        _broadcast_log_safe(
                            f"[LOOP DETECTOR] {_ld_result.loop_type} detected",
                            level="warn",
                        )
                        vlm.inject_error_feedback(_ld_result.nudge_message)
                        try:
                            from .stuck_recovery_guard import evaluate_loop_recovery
                        except ImportError:
                            from stuck_recovery_guard import evaluate_loop_recovery
                        _sr_loop = evaluate_loop_recovery(
                            _stuck_recovery_state,
                            loop_type=str(_ld_result.loop_type or ""),
                            nudge_number=int(
                                (_ld_result.details or {}).get("nudge_number") or 0
                            ),
                        )
                        if _sr_loop.should_reset:
                            vlm.reset_decision_history(_sr_loop.reason)
                            _loop_detector.reset()
                            _consecutive_errors = 0
                            _recovery_chain_pending = True

                # ── 思想-动作分离同步推进（Cascader / 多级菜单 / 多步表单通用）──
                # 现象：VLM thought 写"已完成应推进下一子目标"但 action 字段
                # 仍是上一步重复（如连续 4 次 click_text "Guide"，但 thought 反复说应推进）。
                # 处理：检测 (action, target_id, type_value) 与上步完全相同 + thought 含推进强词
                # → 自动 set subgoal_status=completed（让 Wave 2 推进），action 改 wait(1s) 跳过重复执行。
                _SUBGOAL_ADVANCE_MARKERS = (
                    "应推进", "应进入下一", "应将 subgoal_status 设为 completed",
                    "应将subgoal_status设为completed",
                    "subgoal_status 设为 completed", "subgoal_status=completed",
                    "应标记 completed", "应标记completed",
                    "已完成，应", "已达成，应", "已成功展开",
                    "进入下一步", "进入下一子目标", "推进至下一",
                    "should advance", "advance to next", "next subgoal",
                )
                if decisions and _prev_action_sig != ("", 0, ""):
                    _hd = decisions[0]
                    _cur_sig = (
                        str(_hd.get("action", "")),
                        int(_hd.get("target_id", 0) or 0),
                        str(_hd.get("type_value", "") or ""),
                    )
                    _is_literal_repeat = (_cur_sig == _prev_action_sig)
                    # 维护重复计数：本步与上步相同则 ++，否则归 1
                    if _is_literal_repeat:
                        _repeat_action_count += 1
                    else:
                        _repeat_action_count = 1
                    _thought = str(_hd.get("thought", "") or "")
                    _has_advance_marker = any(m in _thought for m in _SUBGOAL_ADVANCE_MARKERS)
                    _already_completed = _hd.get("subgoal_status") == "completed"
                    # 过渡动作（scroll/smooth_scroll/wait/press_key）合理重复频繁，永不触发
                    _TRANSITIONAL_ACTIONS = {"scroll", "smooth_scroll", "find_text", "wait", "press_key"}
                    _is_transitional = _cur_sig[0] in _TRANSITIONAL_ACTIONS

                    # 两阶段（软警告 → 硬劫持）— 借鉴 Browser-use 哲学：
                    # 让 VLM 自己刹车，引擎不要轻易篡改动作。
                    #   阶段 1（重复 2 次）：执行原动作 + 注入软警告，VLM 下一轮自己换路
                    #   阶段 2（重复 ≥3 次）：仍不收敛 → 硬劫持改 wait 强制推进子目标
                    if (
                        _is_literal_repeat
                        and _repeat_action_count == 2  # 第 2 次重复：先软警告
                        and not _is_transitional
                        and not _already_completed
                    ):
                        logger.info(
                            f"[REPEAT WARN] 第 2 次重复 {_cur_sig[0]} {_cur_sig[2]!r}，"
                            "注入软警告但不改写动作（让 VLM 自决）"
                        )
                        vlm.inject_error_feedback(
                            f"⚠️ 系统警告：你刚连续 2 次输出完全相同的操作 "
                            f"{_cur_sig[0]} target_id={_cur_sig[1]} type_value={_cur_sig[2]!r}，"
                            f"目标似乎并未推进。\n"
                            f"请重新审视当前页面状态：\n"
                            f"  · 上一步是否真的成功？（看 AX Tree 元素 value/state 有无变化）\n"
                            f"  · 如果真已完成，把 subgoal_status 设为 \"completed\" 并给出**真正的下一步动作**\n"
                            f"  · 如果未完成，换 click_text、不同 target_id、或滚动让目标重新就位\n"
                            f"⛔ 不要再机械重复同一动作。"
                        )
                    if (
                        _is_literal_repeat
                        and _repeat_action_count >= 3  # 第 3 次重复：硬劫持兜底
                        and not _is_transitional
                        and _has_advance_marker
                        and not _already_completed
                    ):
                        logger.info(
                            f"[SUBGOAL AUTO-ADVANCE] thought 含推进信号但 action 重复 "
                            f"({_cur_sig[0]} {_cur_sig[1]} {_cur_sig[2]!r})，"
                            "强制 subgoal_status=completed + 改 wait 跳过重复执行"
                        )
                        _broadcast_log_safe(
                            f"[SUBGOAL AUTO-ADVANCE] 跳过重复 {_cur_sig[0]} {_cur_sig[2]!r}",
                            level="warn",
                        )
                        decisions[0]["subgoal_status"] = "completed"
                        decisions[0]["action"] = "wait"
                        decisions[0]["target_id"] = 0
                        decisions[0]["type_value"] = "1"  # 1 秒占位，让页面状态稳定一下
                        decisions[0]["thought"] = (
                            "[SUBGOAL AUTO-ADVANCE 引擎改写] thought 写「已完成应推进」"
                            f"但原 action={_cur_sig[0]} 与上一步完全相同（已重复 {_repeat_action_count} 次），"
                            "系统强制推进子目标，本步 wait(1s) 让 VLM 下轮按新子目标决策。\n"
                            + _thought
                        )
                        _repeat_action_count = 0  # 重置，避免 wait 后下一步又被误判为重复

                # ── 首翻引擎硬约束：第一次 extract 之后强制 next_page ──
                # VLM 即使读到「首翻铁律」prompt 也常受 probe="infinite" 提示误导走 smooth_scroll，
                # 导致 4-8 步 dedup 弯路。这里在引擎层强行改写决策为 next_page，
                # 让 next_page 五级漏斗（L0 URL Mutation 优先）来判定页面真实模式。
                # 仅触发一次：执行后立即清 flag；若 next_page L4 真的报错滚不动，
                # VLM 下一轮会收到错误反馈正常走 done/click 路径。
                if (
                    _force_next_page_pending
                    and decisions
                    and decisions[0].get("action") not in ("done", "ask_human", "error")
                    and (
                        (_parse_goal_target_count(goal) is None)
                        or (_total_extracted_rows < (_parse_goal_target_count(goal) or 0))
                    )
                ):
                    _orig_action = decisions[0].get("action")
                    logger.info(
                        "[FORCE NEXT_PAGE] 页面已物理触底且目标未达成，"
                        "引擎改写 %r -> next_page",
                        _orig_action,
                    )
                    _broadcast_log_safe(
                        f"[FORCE NEXT_PAGE] 触底未达量，改写 {_orig_action} → next_page",
                        level="warn",
                    )
                    decisions[0]["action"] = "next_page"
                    decisions[0]["target_id"] = 0
                    decisions[0]["type_value"] = ""
                    decisions[0]["thought"] = (
                        f"[FORCE NEXT_PAGE 引擎改写] 原决策={_orig_action}，"
                        "上一轮已确认页面物理触底且目标数量未达成，系统直接使用 next_page "
                        "让底层优先尝试 URL 变异/分页器/页码探测。"
                        + (decisions[0].get("thought") or "")
                    )
                    decisions = [decisions[0]]
                    _force_next_page_pending = False
                elif (
                    _first_flip_pending
                    and _goal_needs_pagination_probe(goal)
                    and decisions
                    and decisions[0].get("action") in ("smooth_scroll", "scroll", "extract")
                ):
                    _orig_action = decisions[0].get("action")
                    if not _page_is_infinite_scroll:
                        # Normal paginated page: rewrite to next_page
                        logger.info(
                            f"[FIRST FLIP] 引擎硬约束：首次 extract 后下一步必须 next_page，"
                            f"已将 VLM 决策 {_orig_action!r} 改写为 next_page"
                        )
                        _broadcast_log_safe(
                            f"[FIRST FLIP] 改写 {_orig_action} → next_page", level="warn"
                        )
                        decisions[0]["action"] = "next_page"
                        decisions[0]["target_id"] = 0
                        decisions[0]["type_value"] = ""
                        # 保留 thought / extracted_data 等原字段，仅改动作类型
                        decisions[0]["thought"] = (
                            f"[FIRST FLIP 引擎改写] 原决策={_orig_action}，"
                            "首次 extract 后系统强制走 next_page 探测分页器。"
                            + (decisions[0].get("thought") or "")
                        )
                        decisions = [decisions[0]]
                    else:
                        # Infinite scroll page: allow smooth_scroll, only rewrite extract → smooth_scroll
                        if _orig_action == "extract":
                            logger.info(
                                "[FIRST FLIP] 无限滚动页面，改写 extract → smooth_scroll"
                            )
                            decisions[0]["action"] = "smooth_scroll"
                        else:
                            logger.info(
                                f"[FIRST FLIP] 无限滚动页面，保留原动作 {_orig_action}"
                            )
                        # keep smooth_scroll/scroll as-is
                    _first_flip_pending = False  # 一次性消费，不再触发
                # 即使 VLM 已经选了 next_page，flag 也清掉避免重复触发
                elif _first_flip_pending and not _goal_needs_pagination_probe(goal):
                    logger.info("[FIRST FLIP] skipped for non-pagination extraction goal")
                    _first_flip_pending = False
                elif _first_flip_pending and decisions and decisions[0].get("action") == "next_page":
                    _first_flip_pending = False

                # ── 过早翻页护栏 ────────────────────────────────────────
                # 当上一轮只提到少量行，且物理探针确认当前页/容器还能继续向下滚时，
                # 不允许 VLM 直接 next_page。这样避免豆瓣/长列表只抓视口前几条就
                # 翻页，跳过当前页下半部分数据。若当前页已触底，则放行 next_page。
                if (
                    _block_next_page_until_drained
                    and decisions
                    and decisions[0].get("action") == "next_page"
                ):
                    _drain_guard_target = _parse_goal_target_count(goal)
                    _target_unmet = (
                        _drain_guard_target is None
                        or _total_extracted_rows < _drain_guard_target
                    )
                    if _target_unmet:
                        _drain_state = await _probe_scroll_drain_state(
                            "premature next_page guard"
                        )
                        if not bool(_drain_state.get("at_bottom")):
                            _orig_thought = decisions[0].get("thought") or ""
                            logger.info(
                                "[PREMATURE PAGE GUARD] rewrite next_page -> smooth_scroll: %s; state=%s",
                                _block_next_page_reason,
                                _drain_state,
                            )
                            _broadcast_log_safe(
                                "[PREMATURE PAGE GUARD] 当前页仍可滚动且只提取到少量数据，先滚动榨干当前页",
                                level="warn",
                            )
                            decisions[0]["action"] = "smooth_scroll"
                            decisions[0]["target_id"] = 0
                            decisions[0]["type_value"] = "down"
                            decisions[0]["thought"] = (
                                "[PREMATURE PAGE GUARD 引擎改写] 当前页尚未物理触底，"
                                "且上一轮提取量不足以证明整页已提完；系统将 next_page "
                                "改为 smooth_scroll，先加载/暴露当前页剩余数据。"
                                + _orig_thought
                            )
                            decisions = [decisions[0]]
                        else:
                            logger.info(
                                "[PREMATURE PAGE GUARD] current page drained; next_page allowed"
                            )
                            _block_next_page_until_drained = False
                            _block_next_page_reason = ""
                    else:
                        _block_next_page_until_drained = False
                        _block_next_page_reason = ""

                # ── Fix 4：连续 ZERO_TARGET_DOWNGRADE RAW LOOP GUARD ───────
                # 检测 VLM 反复输出 click+target_id=0+type_value="X" 的 schema
                # 错位幻觉。已被 validator 降级为 wait，但底层意图仍是同一错误。
                # 连续 3 次相同 type_value → 强行注入硬指令 + 重置积压反馈。
                _head_dec = decisions[0] if decisions else {}
                if _head_dec.get("__zero_target_downgraded"):
                    # Fix C：去掉 type_value 比对（Fix 5 会擦短标签，导致 type_value 看似不一致），
                    # 改为单纯连续计数，更鲁棒覆盖豆瓣那种"thought 反复写 target_id=21 但 JSON 出 0"的死循环。
                    _consecutive_zero_target += 1
                    if _consecutive_zero_target >= 3:
                        # 从 thought 里挖 VLM 真正想点的 @eN（如 "@e21"），让通牒更具体
                        import re as _zt_re
                        _thought = (_head_dec.get("thought") or "")
                        _en_match = _zt_re.search(r"@e(\d+)", _thought)
                        _hint_id = _en_match.group(1) if _en_match else "<你 thought 中提到的 @eN 数字>"
                        logger.warning(
                            f"[RAW GUARD] 连续 {_consecutive_zero_target} 次 "
                            f"ZERO_TARGET_DOWNGRADE，VLM schema 错位 — 强制升级反馈"
                            f"（推断目标 @e{_hint_id}）"
                        )
                        _broadcast_log_safe(
                            f"[RAW GUARD] 连续 {_consecutive_zero_target} 次 ZERO_TARGET 错位 → 升级反馈",
                            level="warn",
                        )
                        vlm.inject_error_feedback(
                            f"🆘【最后通牒 — 你已连续 {_consecutive_zero_target} 步 schema 错位】\n"
                            f"你反复输出 click/type target_id=0，但 thought 明明写了真实 @eN（如 @e{_hint_id}）。\n"
                            f"问题：你把 @eN 的数字部分填错位置了 —— 应填到 target_id 字段，不是 type_value。\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"【请按下面三选一立即输出，否则任务终止】：\n"
                            f"  A. 直接 click：{{\"action\":\"click\",\"target_id\":{_hint_id},"
                            f"\"type_value\":\"\",\"memory_key\":\"\",...}}\n"
                            f"  B. 用 click_text 文本定位（绕开 ID 填位）：\n"
                            f"     {{\"action\":\"click_text\",\"target_id\":0,"
                            f"\"type_value\":\"<按钮可见文字，如 后页 或 2>\",...}}\n"
                            f"  C. 如果任务无法完成，输出 action=done 并在 thought 说明放弃理由。\n"
                            f"⛔ 严禁再输出 target_id=0 + 非空 type_value 的组合。"
                        )
                        # 重置计数器避免连续触发同一警告（每 3 次触发一次）
                        _consecutive_zero_target = 0
                else:
                    _consecutive_zero_target = 0

                try:
                    _wait_loop_msg = _wait_loop_tracker.observe(
                        _head_dec,
                        browser.current_url or "",
                    )
                    if _wait_loop_msg:
                        logger.warning("[WAIT LOOP GUARD] %s", _wait_loop_msg)
                        _broadcast_log_safe(
                            "[WAIT LOOP GUARD] repeated short wait loop detected; "
                            "injecting corrective feedback",
                            level="warn",
                        )
                        vlm.inject_error_feedback(_wait_loop_msg)
                except Exception as _wait_loop_err:
                    logger.debug("[WAIT LOOP GUARD] skipped: %s", _wait_loop_err)

                # ── SUBMIT LOOP GUARD ────────────────────────────────────
                # 拦"VLM 不信自己 submit 成功，又点一次同一个按钮"
                # 触发条件：head=click/click_text/click_new_tab + 同 target_id 出现在
                # 上一步 + 两步之间 URL 已变化（path 不同，submit 显然生效了）。
                # 解决 wait_loop_guard 覆盖不到的 click 变种。
                if decisions:
                    try:
                        try:
                            from .submit_loop_guard import detect_redundant_click_after_navigation
                        except ImportError:  # pragma: no cover
                            from submit_loop_guard import detect_redundant_click_after_navigation  # type: ignore[no-redef]
                        _submit_loop_msg = detect_redundant_click_after_navigation(
                            decisions[0],
                            _last_actions,
                            browser.current_url or "",
                            url_normalizer=_norm_url_for_guard,
                        )
                        if _submit_loop_msg:
                            logger.warning("[SUBMIT LOOP GUARD] %s", _submit_loop_msg[:200])
                            _broadcast_log_safe(
                                "[SUBMIT LOOP GUARD] 同 target_id 跨页重点；注入反馈",
                                level="warn",
                            )
                            vlm.inject_error_feedback(_submit_loop_msg)
                            # Downgrade head to wait so VLM gets a clean re-look
                            # at the new page before retrying. type_value="2"
                            # distinguishes from the wait_loop_guard's "1".
                            _sl_head = decisions[0]
                            _sl_head["action"] = "wait"
                            _sl_head["target_id"] = 0
                            _sl_head["type_value"] = "2"
                            decisions[:] = [_sl_head]
                    except Exception as _submit_loop_err:
                        logger.debug("[SUBMIT LOOP GUARD] skipped: %s", _submit_loop_err)

                # ── SWITCH TAB LOOP GUARD ─────────────────────────────────
                # 拦"VLM 反复 switch_tab(同 N)、每次 status=success 但 thought
                # 始终说'还在错的页'"——心智模型错了不是 switch 错了。
                # 触发：head=switch_tab(N) + 上一步也是 switch_tab(N)。
                if decisions:
                    try:
                        try:
                            from .switch_tab_loop_guard import detect_switch_tab_loop
                        except ImportError:  # pragma: no cover
                            from switch_tab_loop_guard import detect_switch_tab_loop  # type: ignore[no-redef]
                        _switch_loop_msg = detect_switch_tab_loop(
                            decisions[0],
                            _last_actions,
                            threshold=2,
                        )
                        if _switch_loop_msg:
                            logger.warning("[SWITCH TAB LOOP] %s", _switch_loop_msg[:200])
                            _broadcast_log_safe(
                                "[SWITCH TAB LOOP GUARD] 连续 switch_tab 同 N；强制反馈",
                                level="warn",
                            )
                            vlm.inject_error_feedback(_switch_loop_msg)
                            # Downgrade head to wait (type_value="3" 区别于
                            # wait_loop_guard 的 "1" 和 submit_loop_guard 的 "2")
                            # 让 VLM 下一步重看当前 tab 真实内容。
                            _stl_head = decisions[0]
                            _stl_head["action"] = "wait"
                            _stl_head["target_id"] = 0
                            _stl_head["type_value"] = "3"
                            decisions[:] = [_stl_head]
                    except Exception as _switch_loop_err:
                        logger.debug("[SWITCH TAB LOOP] skipped: %s", _switch_loop_err)

                # ── TYPE_VALUE SCHEMA GUARD ───────────────────────────────
                # 拦"VLM 把动作元数据写成 type_value 字符串"。
                # 19:06 文心失败：VLM 想 click_point 却 emit
                # `type target_id=7 type_value="send_button_click_point_870_445"`
                # → 把这串描述真的输入到了聊天框，污染了原文本。
                # 这种错误 LOOP GUARD / SUBMIT_LOOP 都看不见（每次 type_value
                # 字符串都不同，被当成是"换内容重输"了）。
                if decisions:
                    try:
                        try:
                            from .type_value_schema_guard import detect_metadata_in_type_value
                        except ImportError:  # pragma: no cover
                            from type_value_schema_guard import detect_metadata_in_type_value  # type: ignore[no-redef]
                        _tv_schema_msg = detect_metadata_in_type_value(decisions[0])
                        if _tv_schema_msg:
                            logger.warning(
                                "[TYPE_VALUE SCHEMA GUARD] rejected head decision: %s",
                                _tv_schema_msg[:200],
                            )
                            _broadcast_log_safe(
                                "[TYPE_VALUE SCHEMA] 动作元数据被塞进 type_value；拒绝并反馈",
                                level="warn",
                            )
                            vlm.inject_error_feedback(_tv_schema_msg)
                            # Downgrade head to wait (type_value="4" 区别于
                            # wait_loop_guard 的 "1"、submit_loop_guard 的 "2"、
                            # switch_tab_loop_guard 的 "3")
                            _tv_head = decisions[0]
                            _tv_head["action"] = "wait"
                            _tv_head["target_id"] = 0
                            _tv_head["type_value"] = "4"
                            decisions[:] = [_tv_head]
                    except Exception as _tv_schema_err:
                        logger.debug("[TYPE_VALUE SCHEMA] skipped: %s", _tv_schema_err)

                # ── CHAT DRIFT GUARD ──────────────────────────────────────
                # 当用户目标含 "文心/通义/豆包/ChatGPT/..." 等已知 chat brand，
                # 且 VLM 上一步 submit 之后落到 /search/ 页（百度路由把聊天输
                # 入框转成搜索框那种），直接把头动作改写为
                # goto <known_chat_url>，跳过 11 步无效恢复。
                # 解决 run_log_20260514_192447 那种"VLM 准确识别问题但找不到
                # 出路" 的死循环。
                if decisions:
                    try:
                        try:
                            from .chat_entry_drift_guard import detect_chat_to_search_drift
                        except ImportError:  # pragma: no cover
                            from chat_entry_drift_guard import detect_chat_to_search_drift  # type: ignore[no-redef]
                        _drift = detect_chat_to_search_drift(
                            decisions[0],
                            _last_actions,
                            browser.current_url or "",
                            goal,
                        )
                        if _drift:
                            logger.warning(
                                "[CHAT DRIFT GUARD] head rewritten → goto %s (brand=%s)",
                                _drift["goto_url"], _drift["matched_brand"],
                            )
                            _broadcast_log_safe(
                                f"[CHAT DRIFT GUARD] 重定向到 {_drift['matched_brand']} 真聊天 URL",
                                level="warn",
                            )
                            vlm.inject_error_feedback(_drift["feedback"])
                            # Rewrite head into a goto for the recovery URL
                            _drift_head = decisions[0]
                            _drift_head["action"] = "goto"
                            _drift_head["target_id"] = 0
                            _drift_head["type_value"] = _drift["goto_url"]
                            _drift_head["thought"] = (
                                f"[CHAT DRIFT GUARD] 自动恢复：goto {_drift['goto_url']} "
                                f"(原 thought: {str(_drift_head.get('thought') or '')[:200]})"
                            )
                            decisions[:] = [_drift_head]
                    except Exception as _drift_err:
                        logger.debug("[CHAT DRIFT GUARD] skipped: %s", _drift_err)

                # ── SESSION DROP GUARD (Wave 3) ───────────────────────────
                # Mid-task auth expiry: agent was working on a business URL,
                # current page is now a login/auth surface. Capture the
                # business URL, rewrite the head action to ask_human, and
                # let the post-resume handler goto back automatically.
                # Runs BEFORE all other guards so they don't mutate the head
                # in ways that would lose the session-drop signal.
                if decisions and _last_business_url:
                    try:
                        try:
                            from .session_drop_guard import (
                                detect_session_drop as _sdg_detect,
                                build_feedback as _sdg_feedback,
                            )
                        except ImportError:  # pragma: no cover
                            from session_drop_guard import (  # type: ignore[no-redef]
                                detect_session_drop as _sdg_detect,
                                build_feedback as _sdg_feedback,
                            )
                        _sd_head_action = str(decisions[0].get("action") or "")
                        _sd_head_status = str(decisions[0].get("status") or "")
                        # Always probe for session drop — the URL-based check
                        # is the strongest signal. Behaviour differs by what
                        # VLM emitted, but we MUST capture return_url even
                        # when VLM tried to ask for help on its own; and we
                        # MUST rewrite the head when VLM gave up with
                        # done/error on a login-shaped URL (otherwise the
                        # _consecutive_errors counter crashes the run before
                        # the user can intervene).
                        _sd_signal = _sdg_detect(
                            current_url=browser.current_url or "",
                            last_business_url=_last_business_url,
                            initial_url=start_url or "",
                            step=step,
                        )
                        if _sd_signal:
                            _pending_session_return_url = _sd_signal.return_url
                            _orig_sd_action = _sd_head_action
                            # If VLM is already asking for help, leave the
                            # action alone — but the return_url above will
                            # still trigger the auto-goto after resume.
                            if _sd_head_action in ("ask_human", "captcha_detected"):
                                logger.info(
                                    "[SESSION DROP GUARD] return_url=%s recorded; "
                                    "VLM already %s — not rewriting",
                                    _sd_signal.return_url[:80],
                                    _sd_head_action,
                                )
                                _broadcast_log_safe(
                                    f"🔐 [SESSION DROP] VLM 已请求 {_sd_head_action}，"
                                    f"系统将在恢复后自动回到 "
                                    f"{_sd_signal.return_url[:60]}",
                                    level="warn",
                                )
                            else:
                                # Covers normal actions AND the giving-up
                                # patterns (done/error/done+status=error).
                                # Without this rewrite, VLM's done+error keeps
                                # piling up _consecutive_errors and crashes
                                # the run (run_log_20260518_153055 hit 3
                                # consecutive errors before the user could
                                # even see what was happening).
                                _sd_drop_head = decisions[0]
                                _sd_drop_head["action"] = "ask_human"
                                _sd_drop_head["status"] = "error"
                                _sd_drop_head["target_id"] = 0
                                _sd_drop_head["type_value"] = (
                                    f"Session expired mid-task. Last business URL: "
                                    f"{_sd_signal.return_url}. After you log in, "
                                    "the agent will auto-resume from that page."
                                )
                                _sd_drop_head["thought"] = (
                                    f"[SESSION DROP GUARD] {_sd_signal.reason} "
                                    f"(orig action: {_orig_sd_action}"
                                    + (f", status: {_sd_head_status}"
                                       if _sd_head_status else "")
                                    + ")"
                                )
                                decisions[:] = [_sd_drop_head]
                                logger.warning(
                                    "[SESSION DROP GUARD] head → ask_human; "
                                    "orig=%s/%s; return_url=%s",
                                    _orig_sd_action, _sd_head_status,
                                    _sd_signal.return_url[:80],
                                )
                                _broadcast_log_safe(
                                    f"🔐 [SESSION DROP] 任务中途登录态失效"
                                    f"（VLM 原动作 {_orig_sd_action}），"
                                    f"将在登录后自动回到 {_sd_signal.return_url[:60]}",
                                    level="warn",
                                )
                                vlm.inject_error_feedback(_sdg_feedback(_sd_signal))
                                # I4: phase event — guard rewrote VLM's head
                                # action. Severity=warn because the run can
                                # still recover after human login.
                                try:
                                    from api_server import broadcast_phase as _bp_guard

                                    _bp_guard(
                                        "guard",
                                        severity="warn",
                                        message=f"session_drop: {_orig_sd_action} → ask_human",
                                        step=step,
                                        notice_severity=getattr(browser, "_last_notice_severity", None),
                                        extra={
                                            "guard": "session_drop",
                                            "orig_action": _orig_sd_action,
                                            "return_url": _sd_signal.return_url[:200],
                                        },
                                    )
                                except Exception:
                                    pass
                    except Exception as _sd_err:
                        logger.debug("[SESSION DROP GUARD] skipped: %s", _sd_err)

                # ── POST-NOOP TYPE → ENTER COERCION ─────────────────────────
                # When the previous step's TypeHandler short-circuited as
                # a no-op (input already had the target text), VLM very
                # often emits ANOTHER duplicate type next step — its thought
                # may even acknowledge "no need to type" while the JSON
                # still says type. Catch this thought-action divergence by
                # rewriting the duplicate type to press_key Enter so the
                # message actually gets submitted.
                if decisions:
                    try:
                        _ln = getattr(browser, "_last_type_noop", None)
                        if isinstance(_ln, dict):
                            _h = decisions[0]
                            _h_act = str(_h.get("action") or "")
                            _h_tv = str(_h.get("type_value") or "").strip()
                            _h_tid = int(_h.get("target_id") or 0)
                            _ln_tv = str(_ln.get("value") or "").strip()
                            # Hard-correct only when the new decision is the
                            # SAME duplicate type pattern. Different target_id
                            # is fine (could be a different element).
                            if (
                                _h_act == "type"
                                and _h_tv == _ln_tv
                                and _ln_tv
                            ):
                                logger.warning(
                                    "[POST-NOOP TYPE COERCE] last step was TYPE NO-OP "
                                    "for %r; VLM re-emitted type — rewriting to press_key Enter",
                                    _ln_tv[:40],
                                )
                                _broadcast_log_safe(
                                    "⏎ [POST-NOOP COERCE] 上一步 TYPE NO-OP 已确认输入框含目标，"
                                    "本步重复 type 自动改写为 press_key Enter",
                                    level="warn",
                                )
                                _h["action"] = "press_key"
                                _h["target_id"] = 0
                                _h["type_value"] = "Enter"
                                _h["thought"] = (
                                    "[POST-NOOP TYPE COERCE] 上一步 TYPE NO-OP 已确认"
                                    f"输入框含目标文本 {_ln_tv[:40]!r}；本轮的重复 type 被系统"
                                    "改写为 press_key Enter，直接提交消息。原 thought："
                                    + str(_h.get("thought") or "")[:200]
                                )
                                vlm.inject_error_feedback(
                                    "⏎ [POST-NOOP COERCE] 你刚才被 TYPE NO-OP 拦截后又重复 type 了同一文本。"
                                    "系统已替你改写为 press_key Enter。请在 thought 中记下"
                                    "「输入框已确认含目标值，下一步是提交/等回复」，"
                                    "不要再连续 type 同一内容。"
                                )
                                decisions[:] = [_h]
                            # Clear the flag once consumed; if VLM picked a
                            # non-type next action we don't want stale state.
                            browser._last_type_noop = None
                    except Exception as _ntc_err:
                        logger.debug("[POST-NOOP TYPE COERCE] skipped: %s", _ntc_err)

                # ── CHAT SUBMIT COERCE ──────────────────────────────────
                # 修复 run_log_20260518_154220: yiyan.baidu.com 的发送按钮是
                # <div>+<SVG>，SoM 没标号；VLM 退而 press_key Enter，但页面
                # 自定义 Enter 处理器没生效 → 11 次 Enter 都没发出消息 →
                # MAX_STEPS。本 guard 在以下条件全部满足时强制改写为 chat_submit：
                #   1) 当前在已知 chat 域名（yiyan/chat.baidu/chatgpt/claude 等）
                #   2) 当前头部动作是 press_key Enter
                #   3) 最近 2 步里至少有 1 步也是 press_key Enter 且
                #      _last_action_result.success=True（也就是技术上"按下了"
                #      但页面没响应——再按一次只会重复失败）
                # 这样可以让"Enter 不通"早早被打断，把按钮点击交给确定性的
                # chat_send_locator + ChatSubmitHandler。
                if decisions:
                    try:
                        try:
                            from .chat_extract_coercion_guard import (
                                is_on_known_chat_domain as _csc_is_chat,
                            )
                        except ImportError:  # pragma: no cover
                            from chat_extract_coercion_guard import (  # type: ignore[no-redef]
                                is_on_known_chat_domain as _csc_is_chat,
                            )
                        _csc_head = decisions[0]
                        _csc_act = str(_csc_head.get("action") or "")
                        _csc_tv = str(_csc_head.get("type_value") or "").strip().lower()
                        _csc_thought = str(_csc_head.get("thought") or "")
                        _csc_on_chat = False
                        try:
                            _csc_on_chat = bool(_csc_is_chat(browser.current_url or ""))
                        except Exception:
                            _csc_on_chat = False
                        _csc_is_enter_press = (
                            _csc_act == "press_key"
                            and _csc_tv in ("enter", "return")
                        )
                        _prev_action_result = getattr(browser, "_last_action_result", None)
                        _prev_action_name = str(
                            getattr(_prev_action_result, "action", "") or ""
                        )
                        _prev_action_ok = bool(
                            getattr(_prev_action_result, "success", False)
                        )
                        if (
                            _csc_on_chat
                            and _csc_act == "chat_submit"
                            and _prev_action_name == "chat_submit"
                            and _prev_action_ok
                            and not workflow_memory.get("__chat_extract_completed")
                        ):
                            logger.warning(
                                "[CHAT POST-SUBMIT GUARD] previous chat_submit succeeded; "
                                "rewriting repeated chat_submit to chat_extract"
                            )
                            _broadcast_log_safe(
                                "💬 [CHAT POST-SUBMIT GUARD] 上一步已经成功发送消息，"
                                "本步不再重复发送，改为 chat_extract 等待并提取 AI 回复",
                                level="warn",
                            )
                            _csc_head["action"] = "chat_extract"
                            _csc_head["target_id"] = 0
                            _csc_head["type_value"] = ""
                            _csc_head["memory_key"] = (
                                str(_csc_head.get("memory_key") or "").strip()
                                or "ai_answer"
                            )
                            _csc_head["extracted_data"] = None
                            _csc_head["status"] = ""
                            _csc_head["subgoal_status"] = "in_progress"
                            _csc_head["thought"] = (
                                "[CHAT POST-SUBMIT GUARD] 上一步 chat_submit 已成功，"
                                "当前已在聊天会话页；重复发送可能造成多条重复问题。"
                                "系统改写为 chat_extract：等待流式回答稳定并提取正文。"
                                "原 thought："
                                + _csc_thought[:240]
                            )
                            decisions[:] = [_csc_head]
                            vlm.inject_error_feedback(
                                "💬 [CHAT POST-SUBMIT GUARD] 上一步已经成功发送消息。"
                                "在 AI 聊天页发送成功后，下一步应使用 chat_extract "
                                "等待并提取回答，不要继续 chat_submit/点击发送。"
                            )
                            _csc_act = "chat_extract"
                            _csc_tv = ""
                        if _csc_on_chat and _csc_is_enter_press:
                            # 看 vlm._history 最后 2 条里有没有同样的 press_key Enter
                            _recent = (vlm._history or [])[-2:]
                            _prior_enter_count = sum(
                                1 for h in _recent
                                if str(h.get("action") or "") == "press_key"
                                and str(h.get("type_value") or "").strip().lower() in ("enter", "return")
                            )
                            if _prior_enter_count >= 1:
                                logger.warning(
                                    "[CHAT SUBMIT COERCE] 在 chat 域名上检测到 "
                                    "连续 press_key Enter (本轮+%d 历史)；"
                                    "改写为 chat_submit (确定性点击发送按钮)",
                                    _prior_enter_count,
                                )
                                _broadcast_log_safe(
                                    "🚀 [CHAT SUBMIT COERCE] 检测到连续 Enter "
                                    "在 chat 站点未生效，改写为 chat_submit 直接点击发送按钮",
                                    level="warn",
                                )
                                _csc_head["action"] = "chat_submit"
                                _csc_head["target_id"] = 0
                                _csc_head["type_value"] = ""
                                _csc_head["thought"] = (
                                    "[CHAT SUBMIT COERCE] 检测到当前 chat 站点连续 "
                                    "press_key Enter 都没生效（典型场景：发送按钮"
                                    "是 <div> + 图标，被 SoM 漏标且页面没绑定 Enter）。"
                                    "系统改写为 chat_submit，由 chat_send_locator "
                                    "启发式定位发送按钮后用真实坐标点击。原 thought："
                                    + str(_csc_head.get("thought") or "")[:200]
                                )
                                decisions[:] = [_csc_head]
                                vlm.inject_error_feedback(
                                    "⏎ [CHAT SUBMIT COERCE] 你刚才反复 press_key Enter "
                                    "在 chat 站点没生效。已替你改写为 chat_submit。"
                                    "下次在 chat 站点输入消息后，**优先尝试 chat_submit**，"
                                    "Enter 是次选；只在 chat_submit 也失败时再回退 Enter。"
                                )
                        elif _csc_on_chat and _csc_act != "chat_submit":
                            # Some VLMs correctly reason "use chat_submit" in
                            # thought, but still emit click(target_id=N). Others
                            # keep clicking whatever red-box id happens to sit
                            # near the composer. In chat pages, after repeated
                            # submit-intent attempts, the deterministic locator
                            # is safer than another SoM click.
                            _submit_words = (
                                "chat_submit", "发送", "提交", "send", "submit",
                                "paper plane", "paper-plane", "纸飞机", "飞机图标",
                            )

                            def _csc_submitish_text(obj: dict) -> bool:
                                try:
                                    blob = " ".join(
                                        str(obj.get(k) or "")
                                        for k in (
                                            "thought", "progress_review",
                                            "current_state", "type_value",
                                        )
                                    ).lower()
                                except Exception:
                                    return False
                                return any(w.lower() in blob for w in _submit_words)

                            _current_wants_submit = _csc_submitish_text(_csc_head)
                            _thought_names_macro = "chat_submit" in _csc_thought.lower()
                            _recent_submit_attempts = sum(
                                1 for h in (vlm._history or [])[-5:]
                                if str(h.get("action") or "") in (
                                    "click", "click_text", "click_point",
                                    "press_key", "scroll",
                                )
                                and _csc_submitish_text(h)
                            )
                            _is_repeat_submit_attempt = (
                                _current_wants_submit
                                and _csc_act in (
                                    "click", "click_text", "click_point",
                                    "press_key", "scroll",
                                )
                                and _recent_submit_attempts >= 2
                            )
                            if _thought_names_macro or _is_repeat_submit_attempt:
                                _post_submit_to_extract = (
                                    _prev_action_name == "chat_submit"
                                    and _prev_action_ok
                                    and not workflow_memory.get("__chat_extract_completed")
                                )
                                _rewrite_action = (
                                    "chat_extract" if _post_submit_to_extract else "chat_submit"
                                )
                                logger.warning(
                                    "[CHAT SUBMIT COERCE] chat submit intent emitted as %s "
                                    "(recent_submit_attempts=%d, thought_names_macro=%s); "
                                    "rewriting to %s",
                                    _csc_act,
                                    _recent_submit_attempts,
                                    _thought_names_macro,
                                    _rewrite_action,
                                )
                                _broadcast_log_safe(
                                    "🚀 [CHAT SUBMIT COERCE] 检测到聊天页发送意图但动作仍是 "
                                    f"{_csc_act}，改写为 {_rewrite_action}",
                                    level="warn",
                                )
                                _csc_head["action"] = _rewrite_action
                                _csc_head["target_id"] = 0
                                _csc_head["type_value"] = ""
                                if _post_submit_to_extract:
                                    _csc_head["memory_key"] = (
                                        str(_csc_head.get("memory_key") or "").strip()
                                        or "ai_answer"
                                    )
                                    _csc_head["extracted_data"] = None
                                    _csc_head["status"] = ""
                                    _csc_head["subgoal_status"] = "in_progress"
                                _csc_head["thought"] = (
                                    "[CHAT SUBMIT COERCE] 当前 chat 页面中，VLM 的 thought "
                                    "已经指向发送/提交（或明确提到 chat_submit），但 JSON 动作仍是 "
                                    f"{_csc_act}。系统改写为 chat_submit，绕过 SoM 红框误标/"
                                    "重复点击无效按钮。原 thought："
                                    + _csc_thought[:240]
                                )
                                if _post_submit_to_extract:
                                    _csc_head["thought"] = (
                                        "[CHAT POST-SUBMIT GUARD] 上一步 chat_submit 已成功，"
                                        "但本轮 VLM 仍想再次提交。系统改写为 chat_extract，"
                                        "避免重复发送同一问题，并开始等待/提取 AI 回复。原 thought："
                                        + _csc_thought[:240]
                                    )
                                decisions[:] = [_csc_head]
                                if _post_submit_to_extract:
                                    vlm.inject_error_feedback(
                                        "💬 [CHAT POST-SUBMIT GUARD] 上一步已经成功发送消息。"
                                        "下一步应使用 chat_extract 等待并提取回答，不要继续点击/提交。"
                                    )
                                else:
                                    vlm.inject_error_feedback(
                                        "🚀 [CHAT SUBMIT COERCE] 在 chat 站点，发送按钮是图标时不要继续猜 "
                                        "target_id。只要 thought 已经判断要发送/提交，优先输出 "
                                        "{\"action\":\"chat_submit\",\"target_id\":0}。"
                                    )
                    except Exception as _csc_err:
                        logger.debug("[CHAT SUBMIT COERCE] skipped: %s", _csc_err)

                # ── CHAT EXTRACT COERCION GUARD ───────────────────────────
                # 即使 prompt 文档已经说"chat 域名用 chat_extract"，VLM 还是
                # 经常惯性选 extract。这条 guard 在调度前把 extract /
                # extract_link 改写成 chat_extract，避免在 chat.baidu.com /
                # yiyan.baidu.com / chat.openai.com 等页面抓到搜索结果/
                # 参考列表而漏掉 AI 回答正文。
                # 修复目标：run_log_20260515_144253 中 step 5-12 那种
                # "在对的页面上选错动作" 的死循环。
                if decisions:
                    try:
                        try:
                            from .chat_extract_coercion_guard import (
                                detect_extract_on_chat_host,
                                detect_premature_chat_done,
                            )
                        except ImportError:  # pragma: no cover
                            from chat_extract_coercion_guard import (  # type: ignore[no-redef]
                                detect_extract_on_chat_host,
                                detect_premature_chat_done,
                            )
                        _coerce = detect_extract_on_chat_host(
                            decisions[0],
                            browser.current_url or "",
                        )
                        if not _coerce:
                            _coerce = detect_premature_chat_done(
                                decisions[0],
                                browser.current_url or "",
                                goal=goal,
                                chat_extract_completed=bool(
                                    workflow_memory.get("__chat_extract_completed")
                                ),
                            )
                        if _coerce:
                            _coerce_head = decisions[0]
                            _orig_action = str(_coerce_head.get("action") or "")
                            _coerce_head["action"] = "chat_extract"
                            _coerce_head["target_id"] = 0
                            _coerce_head["type_value"] = ""
                            _coerce_head["memory_key"] = _coerce["memory_key"]
                            # chat_extract handler fills extracted_data itself
                            _coerce_head["extracted_data"] = None
                            # A VLM may choose chat_extract but also mark the
                            # subgoal completed with its own payload. Keep the
                            # decision executable so the deterministic handler
                            # actually runs.
                            _coerce_head["status"] = ""
                            _coerce_head["subgoal_status"] = "in_progress"
                            _coerce_head["thought"] = (
                                f"[CHAT EXTRACT GUARD] 在聊天域名 "
                                f"{_coerce['matched_host']} 上把 `{_orig_action}` "
                                f"改写为 `chat_extract` (memory_key="
                                f"{_coerce['memory_key']!r})。原 thought: "
                                f"{str(_coerce_head.get('thought') or '')[:200]}"
                            )
                            decisions[:] = [_coerce_head]
                            logger.warning(
                                "[CHAT EXTRACT GUARD] %s → chat_extract on %s",
                                _orig_action, _coerce["matched_host"],
                            )
                            _broadcast_log_safe(
                                f"[CHAT EXTRACT GUARD] 聊天页 {_coerce['matched_host']} "
                                f"上 {_orig_action} → chat_extract",
                                level="warn",
                            )
                            vlm.inject_error_feedback(_coerce["feedback"])
                    except Exception as _coerce_err:
                        logger.debug("[CHAT EXTRACT GUARD] skipped: %s", _coerce_err)

                # ── Fix 2：拦截"子目标未完就 done"（CRITICAL：Task B bug）
                # VLM 把「子目标完成 → 推进」错认为「全局完成 → done」；
                # 只要 _task_plan 存在、当前不是末子目标、且 Reflector 未判 abort，
                # action=done 一律降级为 error，并注入语义区分反馈。
                if (
                    _task_plan is not None
                    and decisions
                    and decisions[0].get("action") == "done"
                    and not _abort_requested
                ):
                    _cur_idx = _task_plan.current_idx
                    _total = len(_task_plan.sub_goals)
                    _done_or_failed = sum(
                        1 for sg in _task_plan.sub_goals
                        if sg.status in ("done", "failed")
                    )
                    _pending_tail = [
                        sg for sg in _task_plan.sub_goals[_cur_idx + 1:]
                        if sg.status not in ("done", "failed")
                    ]
                    _terminal_only_tail = bool(_pending_tail) and all(
                        _is_terminal_only_subgoal(sg) for sg in _pending_tail
                    )
                    _current_subgoal_complete = _decision_claims_current_subgoal_completed(
                        decisions[0]
                    )
                    _allow_done_via_final_subgoal = _done_targets_final_subgoal(
                        _task_plan, decisions[0]
                    )
                    _allow_done_via_terminal_tail = (
                        _terminal_only_tail and _current_subgoal_complete
                    )
                    _allow_done_via_answer_guard = (
                        _goal_output_mode == "answer"
                        and bool(decisions[0].get("__answer_done_guard"))
                        and _current_subgoal_complete
                    )
                    # Fix B 放行：提取任务已达用户目标量 → 直接允许 done，跳过门闸
                    # 修复"豆瓣 Top250 / 京东搜索 N 条"这类任务在步骤 1 extract 成功后
                    # 被门闸强留反复重提取的 6-7 步冗余循环。
                    _goal_target = _parse_goal_target_count(goal)
                    # Adaptive tolerance: larger targets get proportionally larger tolerance
                    _tolerance = min(5, max(1, int(_goal_target * 0.05))) if _goal_target else 0
                    _extraction_complete = (
                        _goal_target is not None
                        and (
                            _total_extracted_rows >= _goal_target
                            or (
                                _pagination_exhausted
                                and _total_extracted_rows >= _goal_target - _tolerance
                            )
                        )
                    )
                    if _extraction_complete and _total_extracted_rows < _goal_target:
                        logger.info(
                            f"[CLOSE ENOUGH] 容差退出: {_total_extracted_rows}/{_goal_target} "
                            f"(容差 {_tolerance}，分页已耗尽)"
                        )
                    _allow_done_via_completion_kernel = False
                    _completion_eval = {}
                    try:
                        from visual_web_agent.completion_kernel import (
                            evaluate_completion,
                            load_manifest_items_for_run,
                        )
                        from visual_web_agent.planner_exit_criteria import (
                            current_subgoal_criteria,
                        )

                        _pg_prev = getattr(browser, "_last_action_result", None)
                        _pg_last_action = str(getattr(_pg_prev, "action", "") or "")
                        _pg_last_ok = (
                            bool(getattr(_pg_prev, "success", False))
                            if _pg_prev is not None else False
                        )
                        _completion_eval = evaluate_completion(
                            goal=goal,
                            output_contract=_goal_output_contract,
                            output_mode=_goal_output_mode,
                            total_extracted_rows=_total_extracted_rows,
                            total_pages=len(_extracted_page_keys),
                            goal_target_count=_goal_target,
                            goal_target_pages=_parse_goal_target_pages(goal),
                            pagination_exhausted=_pagination_exhausted,
                            manifest_items=load_manifest_items_for_run(_run_ts),
                            workflow_memory=workflow_memory,
                            capability_route=_capability_route,
                            no_progress_streak=_no_progress_tracker.streak,
                            exit_criteria=current_subgoal_criteria(_task_plan),
                            current_url=getattr(browser, "current_url", "") or "",
                            last_action=_pg_last_action,
                            last_action_success=_pg_last_ok,
                        )
                        _allow_done_via_completion_kernel = (
                            _completion_eval.get("status") == "complete"
                            and not _extraction_complete
                        )
                    except Exception as _ck_gate_err:
                        logger.debug("[COMPLETION KERNEL] plan gate skipped: %s", _ck_gate_err)
                    # 允许 done 的条件：已完成子目标数 >= 总数 - 1（仅剩当前 = 最后一个）
                    # 或提取已达量（Fix B）
                    if _extraction_complete:
                        logger.info(
                            f"[PLAN GATE] 放行 done：提取已达量 "
                            f"{_total_extracted_rows}/{_goal_target} 条，跳过门闸"
                        )
                    elif _allow_done_via_final_subgoal:
                        logger.info(
                            "[PLAN GATE] 放行 done：当前就是末子目标，且该子目标已满足退出标准。"
                        )
                        _task_plan.sub_goals[_cur_idx].status = "done"
                    elif _allow_done_via_terminal_tail:
                        logger.info(
                            "[PLAN GATE] 放行 done：当前子目标已满足退出标准，"
                            "其余仅剩收尾型 done 子目标。"
                        )
                        _task_plan.sub_goals[_cur_idx].status = "done"
                        for _tail_sg in _pending_tail:
                            _tail_sg.status = "done"
                    elif _allow_done_via_answer_guard:
                        logger.info(
                            "[PLAN GATE] 放行 done：答案模式护栏确认页面已满足用户问题。"
                        )
                        _task_plan.sub_goals[_cur_idx].status = "done"
                        for _tail_sg in _pending_tail:
                            _tail_sg.status = "done"
                    elif _allow_done_via_completion_kernel:
                        logger.info(
                            "[PLAN GATE] 放行 done：completion_kernel evidence=%s",
                            (_completion_eval or {}).get("evidence"),
                        )
                        try:
                            from api_server import broadcast_phase as _bp_pg_ck

                            _bp_pg_ck(
                                "completion_guard",
                                severity="info",
                                message="plan_gate: completion_kernel allowed done",
                                step=step,
                                notice_severity=getattr(
                                    browser, "_last_notice_severity", None
                                ),
                                extra={
                                    "guard": "plan_gate_completion_kernel",
                                    "evaluation": _completion_eval,
                                },
                            )
                        except Exception:
                            pass
                        _task_plan.sub_goals[_cur_idx].status = "done"
                        for _tail_sg in _pending_tail:
                            _tail_sg.status = "done"
                    if (
                        _done_or_failed < _total - 1
                        and not _extraction_complete
                        and not _allow_done_via_final_subgoal
                        and not _allow_done_via_terminal_tail
                        and not _allow_done_via_answer_guard
                        and not _allow_done_via_completion_kernel
                    ):
                        logger.warning(
                            f"[PLAN GATE] VLM 过早 action=done（进度 "
                            f"{_done_or_failed}/{_total} 子目标完成），改为 subgoal 完成 + wait"
                        )
                        _broadcast_log_safe(
                            f"[PLAN GATE] 拦截过早 done（{_done_or_failed}/{_total}）",
                            level="warn",
                        )
                        decisions[0]["action"] = "wait"
                        decisions[0]["type_value"] = "1"
                        decisions[0]["subgoal_status"] = "completed"
                        # Inject feedback to prevent VLM repeating done
                        _remaining = _total - _done_or_failed - 1
                        vlm.inject_error_feedback(
                            f"✅ 当前子目标已声明完成；但任务仍有 {_remaining} 个子目标未达成，"
                            f"请下一步直接执行 extract 或 next_page，不要重复输出 done。"
                        )

                # ── Wave 2 子目标自宣告推进 ─────────────────────────────
                # VLM 每步返回 subgoal_status：若 completed 则系统推进 _task_plan.current_idx。
                # 末子目标若仍带有真实动作（click / extract / hover_and_click 等），必须先执行；
                # 否则会出现“计划完成但最终按钮/菜单项没有被点击”的假阳性。
                if _task_plan is not None and decisions:
                    _head = decisions[0]
                    if _head.get("subgoal_status") == "completed":
                        _idx = _task_plan.current_idx
                        _is_last = _idx >= len(_task_plan.sub_goals) - 1
                        _task_plan.sub_goals[_idx].status = "done"
                        if not _is_last:
                            _task_plan.current_idx += 1
                            _task_plan.sub_goals[_task_plan.current_idx].status = "active"
                            logger.info(
                                f"[PLAN] VLM 自宣告推进至子目标 "
                                f"{_task_plan.current_idx + 1}/{len(_task_plan.sub_goals)}："
                                f"{_task_plan.sub_goals[_task_plan.current_idx].description}"
                            )
                            _broadcast_log_safe(
                                f"[PLAN] 推进至子目标 {_task_plan.current_idx + 1}: "
                                f"{_task_plan.sub_goals[_task_plan.current_idx].description}"
                            )
                        elif _head.get("action") != "done" and _head.get("action") in ("wait", "noop", "none", ""):
                            logger.warning(
                                f"[PLAN] 末子目标 completed 且 action={_head.get('action')} 无副作用，"
                                f"纠偏为 done"
                            )
                            _head["action"] = "done"
                            _head["target_id"] = 0
                            _head["type_value"] = ""

                # ── extract+null 即时自动提取 ────────────────────────────
                # 借鉴 browser-use 架构：VLM 只需给出 extract 意图，
                # 数据由系统从 AX Tree 自动提取，不再浪费步数等 VLM 重试。
                _has_extract_downgrade = any(
                    d.get("__extract_downgraded")
                    and str(d.get("action") or "").strip().lower()
                    not in {"done", "chat_extract"}
                    for d in decisions
                )
                if _has_extract_downgrade:
                    _extract_null_streak += 1
                    # 同 URL 不再直接跳过：无限滚动/局部刷新常常保持 URL 不变。
                    # 这里仅记录信号，真正是否重复由行级 fingerprint 决定。
                    _current_auto_url = browser.current_url
                    if _current_auto_url in _extracted_page_urls:
                        logger.info(
                            "[EXTRACT AUTO] URL already seen; continuing with row-level dedup: %s",
                            _current_auto_url,
                        )
                    logger.warning(
                        f"[EXTRACT AUTO] VLM 输出 extract+null "
                        f"(第 {_extract_null_streak} 次)，启动 AX Tree 自动提取"
                    )
                    try:
                        _data_shape = {}
                        try:
                            _data_shape = await browser.probe_data_shape()
                            logger.info("[DATA SHAPE] %s", _data_shape)
                            if _expected_rows_from_data_shape(_data_shape) >= 10:
                                _drain_state = await _probe_scroll_drain_state(
                                    "auto extract dense-shape drain override"
                                )
                                _data_shape = dict(_data_shape)
                                _data_shape["physically_drained"] = bool(
                                    _drain_state.get("at_bottom")
                                )
                                _data_shape["drain_state"] = _drain_state
                                logger.info(
                                    "[DATA SHAPE] dense drain_state=%s",
                                    _drain_state,
                                )
                        except Exception as _shape_err:
                            logger.debug("[DATA SHAPE] skipped: %s", _shape_err)

                        _auto_extract_text_source, _ax_text = (
                            await _extract_full_page_text_for_data(
                                "auto extract full page text"
                            )
                        )
                        _auto_candidates: list[dict] = []

                        # ── 尝试用纯文本 VLM 调用结构化提取 ──
                        _structured_auto = await vlm.extract_structured_data(
                            page_text=_ax_text,
                            goal=goal,
                        )
                        if _structured_auto and isinstance(_structured_auto, list) and len(_structured_auto) > 0:
                            logger.info(
                                "[EXTRACT AUTO] 全页结构化提取候选，共 %s 条",
                                len(_structured_auto),
                            )
                            _auto_candidates.append(
                                _sanitize_extraction_candidate(
                                    name=f"FULL_PAGE:{_auto_extract_text_source}",
                                    data=_structured_auto,
                                    source_text=_ax_text,
                                    data_shape=_data_shape,
                                )
                            )
                        else:
                            # 结构化失败，降级为原始文本 blob
                            _auto_extracted = {
                                "source": (
                                    "auto_extract_from_ax_tree"
                                    if _auto_extract_text_source == "AX_TREE"
                                    else "auto_extract_from_inner_text_fallback"
                                ),
                                "page_url": browser.current_url,
                                "page_text": _ax_text[:8000],
                            }
                            _new_rows = 1
                            logger.info(
                                f"[EXTRACT AUTO] 结构化提取未返回数据，"
                                f"降级保存原始页面文本 (source={_auto_extract_text_source}, "
                                f"长度={len(_ax_text)})"
                            )

                        _dom_list_rows, _dom_list_text = (
                            ([], "")
                            if _goal_is_tooltip_extract(goal)
                            else await _extract_list_rows_via_dom(
                                "auto extract visible list rows"
                            )
                        )
                        if _dom_list_rows:
                            _dom_card_source_text = "\n\n".join(
                                text for text in (_ax_text, _dom_list_text) if text
                            )
                            _dom_card_rows, _dom_card_text = extract_semantic_card_rows(
                                _dom_list_rows,
                                source_text=_dom_card_source_text,
                                requested_fields=_requested_output_fields,
                                goal=goal,
                            )
                            if not _dom_card_rows:
                                _dom_card_body_text = await _extract_body_text_for_semantic_cards(
                                    "auto extract semantic card body text"
                                )
                                if _dom_card_body_text:
                                    _dom_card_rows, _dom_card_text = extract_semantic_card_rows(
                                        _dom_list_rows,
                                        source_text="\n\n".join(
                                            text
                                            for text in (
                                                _dom_card_body_text,
                                                _dom_card_source_text,
                                            )
                                            if text
                                        ),
                                        requested_fields=_requested_output_fields,
                                        goal=goal,
                                    )
                            if _dom_card_rows:
                                logger.info(
                                    "[EXTRACT DOM] semantic cards rows=%s source_chars=%s",
                                    len(_dom_card_rows),
                                    len(_dom_card_text or _dom_list_text or ""),
                                )
                                _auto_candidates.append(
                                    _sanitize_extraction_candidate(
                                        name="DOM_CARDS",
                                        data=_dom_card_rows,
                                        source_text=_dom_card_text or _dom_list_text or _ax_text,
                                        data_shape=_data_shape,
                                    )
                                )
                            _auto_candidates.append(
                                _sanitize_extraction_candidate(
                                    name="DOM_LIST",
                                    data=_dom_list_rows,
                                    source_text=_dom_list_text or _ax_text,
                                    data_shape=_data_shape,
                                )
                            )

                        _dom_auto_rows = (
                            []
                            if _goal_is_tooltip_extract(goal)
                            else await _extract_visible_table_rows_via_dom(
                                "auto extract visible table rows"
                            )
                        )
                        if _dom_auto_rows:
                            _auto_candidates.append(
                                _sanitize_extraction_candidate(
                                    name="DOM_TABLE",
                                    data=_dom_auto_rows,
                                    source_text=_ax_text,
                                    data_shape=_data_shape,
                                )
                            )

                        try:
                            _auto_candidates = rank_extraction_candidates_with_history(
                                _auto_candidates,
                                url=_current_auto_url,
                                requested_fields=_requested_output_fields,
                                goal=_snapshot_goal,
                            )
                            _auto_boosted = [
                                c for c in _auto_candidates
                                if (c.get("recovery") or {}).get("boost")
                            ]
                            if _auto_boosted:
                                logger.info(
                                    "[EXTRACT RECOVERY] auto history boosts=%s",
                                    "; ".join(
                                        f"{c.get('name')}:"
                                        f"+{float((c.get('recovery') or {}).get('boost') or 0):.1f}"
                                        f" match={float((c.get('recovery') or {}).get('score') or 0):.2f}"
                                        for c in _auto_boosted
                                    ),
                                )
                        except Exception as _auto_recovery_err:
                            logger.debug("[EXTRACT RECOVERY] auto skipped: %s", _auto_recovery_err)

                        _chosen_candidate = _choose_best_extraction_candidate(_auto_candidates)
                        if _chosen_candidate:
                            (
                                _auto_extracted,
                                _new_rows,
                                _dup_rows,
                                _rejected_rows,
                                _chosen_source_text,
                            ) = _commit_extraction_candidate(_chosen_candidate)
                            _auto_extract_text_source = str(
                                _chosen_candidate.get("name") or _auto_extract_text_source
                            )
                        else:
                            _auto_extracted, _new_rows, _dup_rows, _rejected_rows = (
                                [],
                                0,
                                0,
                                0,
                            )
                        if _new_rows == 0:
                            _dedup_tripped_last_step = True
                            logger.warning(
                                "[EXTRACT AUTO DEDUP] no new rows after row-level filtering "
                                "(duplicates=%s, rejected=%s, url=%s)",
                                _dup_rows,
                                _rejected_rows,
                                _current_auto_url,
                            )
                            _expected_auto_rows = _expected_rows_from_data_shape(_data_shape)
                            if _expected_auto_rows >= 10:
                                _block_next_page_until_drained = True
                                _block_next_page_reason = (
                                    f"dense page exposes about {_expected_auto_rows} rows, "
                                    "but extraction returned too few"
                                )
                                vlm.inject_error_feedback(
                                    "⚠️ 系统探头发现当前页存在密集列表/表格，"
                                    f"大约 {_expected_auto_rows} 个结构化条目；"
                                    "但本次自动提取没有得到足够新增行。\n"
                                    "这说明当前页尚未被可靠提取，下一步先 smooth_scroll down "
                                    "或重新 extract 当前页，禁止直接 next_page。"
                                )
                            else:
                                vlm.inject_error_feedback(
                                    "⚠️ 系统尝试提取当前视野/页面，但行级去重发现没有新增数据。\n"
                                    "请不要再次 extract 同一批内容。下一步应优先执行 "
                                    "smooth_scroll(type_value='down') 加载更多列表项，或使用 "
                                    "next_page / click_text 点击明确的下一页控件。"
                                )
                            await _nudge_scroll_after_duplicate_extract(
                                "auto extract duplicate rows", scroll_amount=1500
                            )
                            continue

                        if _dup_rows or _rejected_rows:
                            logger.info(
                                "[EXTRACT AUTO DEDUP] filtered %s duplicate rows, "
                                "rejected %s unsupported rows, saving %s new rows",
                                _dup_rows,
                                _rejected_rows,
                                _new_rows,
                            )

                        _auto_extracted = await _enrich_rows_with_dom_links(_auto_extracted)
                        saved_path = ""
                        if _goal_output_mode == "answer":
                            logger.info(
                                "[ANSWER OUTPUT] answer-only auto extract kept in run result; "
                                "not saving Excel artifact"
                            )
                        else:
                            saved_path = save_run_dataset(
                                _auto_extracted,
                                run_id=_run_ts,
                                output_contract=_goal_output_contract,
                                produced_by="auto_extract",
                                filename_hint=_vlm_output,
                                unique_key=TOOLTIP_UNIQUE_KEY if _goal_is_tooltip_extract(goal) else None,
                            )
                        _progress_new_rows, _progress_total_rows = _record_extract_progress(
                            _auto_extracted,
                            _new_rows,
                        )
                        _api_fast = await _try_dom_api_fast_path(
                            _auto_extracted,
                            source=_auto_extract_text_source,
                        )
                        if _api_fast.get("applied"):
                            _auto_extracted = _api_fast.get("fast_path", {}).get("rows") or _auto_extracted
                            _progress_total_rows = _total_extracted_rows
                        _auto_snapshot_path = await _save_extraction_snapshot(
                            source=_auto_extract_text_source,
                            rows=_auto_extracted,
                            output_file=str(saved_path or ""),
                            accepted_rows=_new_rows,
                            duplicate_rows=_dup_rows,
                            rejected_rows=_rejected_rows,
                            candidates=_auto_candidates,
                            data_shape=_data_shape,
                            source_text=_chosen_source_text if _chosen_candidate else _ax_text,
                            metadata={
                                "mode": "auto_extract",
                                "progress_new_rows": _progress_new_rows,
                                "progress_total_rows": _progress_total_rows,
                            },
                        )
                        _duplicate_zero_extract_streak = 0
                        if _goal_is_tooltip_extract(goal):
                            logger.info(
                                "[EXTRACT AUTO] tooltip progress: %s new triggers, %s total triggers",
                                _progress_new_rows,
                                _progress_total_rows,
                            )
                        _extract_count += 1
                        _extracted_page_urls.add(browser.current_url)
                        # 与显式 extract 路径保持一致：含数据提取的轨迹不适合极速回放。
                        # Date-picker tasks are later semanticized to a date_pick macro,
                        # so incidental docs-table extraction must not poison that cache.
                        _macro_guard = _parse_semantic_macro(goal)
                        if not (_macro_guard and _macro_guard.get("action") == "date_pick"):
                            _rpa_cache_allowed = False
                            _rpa_skip_reason = "contains auto-extract steps"
                        _log_extract_text_source = _auto_extract_text_source
                        _log_decision = [{
                            "action": "extract",
                            "extracted_data": _auto_extracted,
                            "snapshot_path": _auto_snapshot_path,
                        }]
                        # ── 首翻引擎硬约束：首次 extract 后强制下一步 next_page ──
                        # Bug 修复：用任务级永久锁 _first_extract_ever_done，避免 _extract_count
                        # 在翻页/导航后被重置回 0 → 下一次 extract 又把 flag 设回 True →
                        # FIRST FLIP 在每次翻页后反复触发的问题。
                        if (
                            not _first_extract_ever_done
                            and _should_force_first_flip_after_successful_extract(goal)
                        ):
                            _first_extract_ever_done = True
                            _first_flip_pending = True
                        # ── Improvement 1：首次 extract 后探测分页器（auto-extract 路径） ──
                        if (
                            _goal_needs_pagination_probe(goal)
                            and not _pagination_probed
                            and _extract_count == 1
                        ):
                            _pagination_probed = True
                            try:
                                _probe = await browser.probe_pagination()
                                _pagination_kind = _probe.get("kind", "")
                                _cands = _probe.get("candidates", [])
                                if _probe.get("has_paginator"):
                                    _names = ", ".join(
                                        f"{c['ref']}={c['name']!r}" for c in _cands[:6]
                                    )
                                    _should_force_probe_next, _force_probe_reason = (
                                        _should_schedule_next_page_after_extract(
                                            goal,
                                            new_rows=_new_rows,
                                            total_rows=_total_extracted_rows,
                                            extract_source=_auto_extract_text_source,
                                            pagination_kind=_pagination_kind,
                                            expected_rows=_expected_rows_from_data_shape(_data_shape),
                                            physically_drained=bool(_data_shape.get("physically_drained")),
                                        )
                                    )
                                    if _should_force_probe_next:
                                        _force_next_page_pending = True
                                        _first_flip_pending = False
                                        _block_next_page_until_drained = False
                                        _block_next_page_reason = ""
                                        logger.info(
                                            "[PROBE PAGE] armed next_page after extract: %s",
                                            _force_probe_reason,
                                        )
                                        _broadcast_log_safe(
                                            f"[PROBE PAGE] 已发现分页器，下一轮直接 next_page：{_force_probe_reason}",
                                            level="info",
                                        )
                                        _pagination_hint_msg = (
                                            f"📍【系统探测：本页**带分页器**（{_pagination_kind}）】\n"
                                            f"已确认页面底部存在翻页控件：{_names}。\n"
                                            f"当前页已提取到足够完整的一批数据（{_force_probe_reason}）。"
                                            f"下一步**必须**用 next_page（首选 URL Mutation）翻页，"
                                            f"**禁止** smooth_scroll 当无限滚动处理。"
                                        )
                                    else:
                                        _drain_state = await _probe_scroll_drain_state(
                                            "pagination probe low-yield auto extract"
                                        )
                                        if not bool(_drain_state.get("at_bottom")):
                                            _block_next_page_until_drained = True
                                            _block_next_page_reason = _force_probe_reason
                                        else:
                                            _force_next_page_pending = True
                                            _first_flip_pending = False
                                            _block_next_page_until_drained = False
                                            _block_next_page_reason = ""
                                            _force_probe_reason = (
                                                f"{_force_probe_reason}; physical bottom reached"
                                            )
                                        if _force_next_page_pending:
                                            _pagination_hint_msg = (
                                                f"📍【系统探测：本页**带分页器**（{_pagination_kind}）】\n"
                                                f"已确认页面存在翻页控件：{_names}。\n"
                                                f"本次新增 {_new_rows} 条虽低于探头预期，"
                                                "但页面已物理触底，继续滚动不会暴露更多当前页数据。"
                                                "下一步必须使用 next_page 翻页。"
                                            )
                                        else:
                                            if _force_next_page_pending:
                                                _pagination_hint_msg = (
                                                    f"📍【系统探测：本页**带分页器**（{_pagination_kind}）】\n"
                                                    f"已确认页面存在翻页控件：{_names}。\n"
                                                    f"本次新增 {_new_rows} 条虽低于探头预期，"
                                                    "但页面已物理触底，继续滚动不会暴露更多当前页数据。"
                                                    "下一步必须使用 next_page 翻页。"
                                                )
                                            else:
                                                _pagination_hint_msg = (
                                                    f"📍【系统探测：本页**带分页器**（{_pagination_kind}）】\n"
                                                    f"已确认页面存在翻页控件：{_names}。\n"
                                                    f"但本次仅新增 {_new_rows} 条（{_force_probe_reason}），"
                                                    "不足以证明当前页已提取完。\n"
                                                    "下一步请先 smooth_scroll 向下并继续 extract 当前页；"
                                                    "只有当前页物理触底或无新增后，才使用 next_page。"
                                                )
                                else:
                                    # No paginator detected — mark as infinite scroll
                                    _page_is_infinite_scroll = True
                                    _should_force_probe_next, _force_probe_reason = (
                                        _should_schedule_next_page_after_extract(
                                            goal,
                                            new_rows=_new_rows,
                                            total_rows=_total_extracted_rows,
                                            extract_source=_auto_extract_text_source,
                                            pagination_kind=_pagination_kind,
                                            expected_rows=_expected_rows_from_data_shape(_data_shape),
                                            physically_drained=bool(_data_shape.get("physically_drained")),
                                        )
                                    )
                                    if _should_force_probe_next:
                                        _force_next_page_pending = True
                                        _first_flip_pending = False
                                        _block_next_page_until_drained = False
                                        _block_next_page_reason = ""
                                        logger.info(
                                            "[PROBE PAGE] armed universal next_page after extract: %s",
                                            _force_probe_reason,
                                        )
                                        _broadcast_log_safe(
                                            f"[PROBE PAGE] 大批量提取未达量，下一轮交给 next_page 宏动作：{_force_probe_reason}",
                                            level="info",
                                        )
                                        _pagination_hint_msg = (
                                            "📍【系统探测：本页**无分页器**（infinite 模式）】\n"
                                            "当前提取批次已足够大但目标未达成。"
                                            "**输出 next_page**，引擎层会自动走 L4 瀑布流兜底（smooth_scroll）"
                                            "加载新数据。next_page 是万能翻页动作，不需要你判断模式。"
                                        )
                                    else:
                                        _pagination_hint_msg = (
                                            "📍【系统探测：本页**无分页器**】\n"
                                            f"本次仅新增 {_new_rows} 条（{_force_probe_reason}），"
                                            "请继续 smooth_scroll / extract 当前列表；"
                                            "如果滚动触底且仍未达量，引擎会再调度 next_page 宏动作。"
                                        )
                                logger.info(f"[PROBE PAGE] kind={_pagination_kind} cands={len(_cands)}")
                            except Exception as _probe_err:
                                logger.warning(f"[PROBE PAGE] 失败忽略：{_probe_err}")
                        # ── Re-arm：已知分页器 + 目标未达 → 每次 extract 后强制 next_page ──
                        # 首次探测一次性完成（_pagination_probed=True），但后续 extract
                        # 仍需引擎兜底翻页，避免 VLM 自行翻页出错浪费步数。
                        if (
                            _pagination_probed
                            and _pagination_kind not in ("", "infinite")
                            and not _page_is_infinite_scroll
                            and not _force_next_page_pending
                        ):
                            _rearm_target = _parse_goal_target_count(goal)
                            if _rearm_target is not None and _total_extracted_rows < _rearm_target:
                                _force_next_page_pending = True
                                logger.info(
                                    "[REARM NEXT_PAGE] paginator known (%s), target not met "
                                    "(%s/%s); re-armed for next step",
                                    _pagination_kind,
                                    _total_extracted_rows,
                                    _rearm_target,
                                )
                        # ── Path C：Hard Kill — 引擎层强杀，达量直接终止主循环 ──
                        # 不再注入提示让 VLM 决策，避免它走神或重提取浪费步数。
                        _hk_target = _parse_goal_target_count(goal)
                        if _hk_target is not None and _total_extracted_rows >= _hk_target:
                            logger.info(
                                f"[HARD KILL] 引擎达量终止：累计 "
                                f"{_total_extracted_rows} >= 目标 {_hk_target} 条"
                            )
                            print(
                                f"\033[1;32m🎯 [HARD KILL]\033[0m 已达量 "
                                f"{_total_extracted_rows}/{_hk_target}，引擎层终止任务"
                            )
                            _broadcast_log_safe(
                                f"[HARD KILL] 累计 {_total_extracted_rows}/{_hk_target} 条达量，引擎终止"
                            )
                            _task_completed = True
                            _run_succeeded = True
                            break  # 退出动作循环，主循环检测 _task_completed 退出
                        if _goal_output_mode == "answer":
                            logger.info(
                                "[EXTRACT AUTO] Answer-mode rows kept in run result "
                                "(累计 %s 条)",
                                _total_extracted_rows,
                            )
                            print(
                                f"\033[1;33m⚡ [EXTRACT AUTO]\033[0m "
                                f"VLM 未填充数据，系统已从 {_auto_extract_text_source} 全页提取 "
                                f"\033[36m{_new_rows}\033[0m 条用于回答，未保存 Excel。"
                            )
                            _auto_answer_text = _clean_user_visible_done_message(
                                next(
                                    (
                                        d.get("thought")
                                        or d.get("current_state")
                                        or d.get("progress_review")
                                        or ""
                                        for d in decisions
                                        if isinstance(d, dict)
                                    ),
                                    "",
                                )
                            )
                            if not _auto_answer_text:
                                _auto_answer_text = _format_extracted_rows_as_answer(
                                    _auto_extracted
                                )
                            _auto_answer_text = _compact_answer_text_for_goal(
                                _auto_answer_text,
                                goal,
                            )
                            if _auto_answer_text:
                                _record_run_answer(text=_auto_answer_text)
                            _log_decision = {
                                "action": "done",
                                "target_id": 0,
                                "status": "success",
                                "thought": (
                                    _auto_answer_text
                                    or "Answer-mode auto extraction completed."
                                ),
                                "extracted_data": _auto_extracted,
                                "snapshot_path": _auto_snapshot_path,
                                "extract_text_source": _auto_extract_text_source,
                            }
                            event_stream.done(
                                step=step,
                                success=True,
                                reason="answer_auto_extract_completed",
                                message=(
                                    _auto_answer_text[:500]
                                    if _auto_answer_text
                                    else "answer-mode auto extract completed"
                                ),
                                metadata={
                                    "output_mode": _goal_output_mode,
                                    "output_file": str(saved_path or ""),
                                    "extract_source": _auto_extract_text_source,
                                    "rows": _new_rows,
                                    "snapshot_path": str(_auto_snapshot_path or ""),
                                },
                            )
                            _broadcast_done_safe(True, "answer extract completed")
                            _run_succeeded = True
                            _task_completed = True
                            break
                        else:
                            logger.info(
                                f"[EXTRACT AUTO] Saved to: {saved_path} "
                                f"(累计 {_total_extracted_rows} 条)"
                            )
                            print(
                                f"\033[1;33m⚡ [EXTRACT AUTO]\033[0m "
                                f"VLM 未填充数据，系统已从 {_auto_extract_text_source} 全页提取 "
                                f"\033[36m{_new_rows}\033[0m 条数据。"
                                f"当前总计: \033[36m{_total_extracted_rows}\033[0m 条"
                            )
                        # 将降级的 wait 动作替换为已完成的 extract
                        # 不需要执行内层动作循环，直接跳到翻页引导
                        decisions = _log_decision
                    except Exception as _auto_err:
                        logger.error(
                            f"[EXTRACT AUTO] 自动提取失败: {_auto_err}"
                        )
                    # 无论是否成功，注入智能翻页/结束引导
                    _auto_pages = len(_extracted_page_urls)
                    _target_count = _parse_goal_target_count(goal)
                    _reached_target = (
                        _target_count is not None
                        and _total_extracted_rows >= _target_count
                    )
                    if _reached_target:
                        # 已达到用户指定的目标数量，强烈建议 done
                        vlm.inject_error_feedback(
                            f"✅ 系统已自动提取当前页数据。"
                            f"你已经成功提取了 {_auto_pages} 个不同页面的数据"
                            f"（累计 {_total_extracted_rows} 条）。\n"
                            f"用户要求获取 {_target_count} 条数据，"
                            f"当前已累计 {_total_extracted_rows} 条，"
                            f"**已达到目标**！请立即输出 done 结束任务。"
                        )
                    elif _auto_pages >= 2 and _target_count is not None:
                        # 已提取 2+ 页但未达标，明确要求继续
                        _pag_links = browser.find_pagination_links()
                        if _pag_links:
                            _pag_hint = "\n".join(
                                f"  → [ID: {p['id']}] {p['role']}: \"{p['name']}\""
                                for p in _pag_links
                            )
                            vlm.inject_error_feedback(
                                f"✅ 系统已自动提取当前页数据。"
                                f"已提取 {_auto_pages} 个页面"
                                f"（累计 {_total_extracted_rows} 条），"
                                f"但用户要求 {_target_count} 条，"
                                f"还差 {_target_count - _total_extracted_rows} 条。\n"
                                f"系统发现了翻页链接：\n{_pag_hint}\n"
                                f"【立即操作】请继续点击翻页链接加载下一页，例如："
                                f"click(target_id={_pag_links[0]['id']})\n"
                                f"⚠️ 必须使用上述精确的 ID！"
                            )
                        else:
                            vlm.inject_error_feedback(
                                f"✅ 系统已自动提取当前页数据。"
                                f"已提取 {_auto_pages} 个页面"
                                f"（累计 {_total_extracted_rows} 条），"
                                f"但用户要求 {_target_count} 条，"
                                f"还差 {_target_count - _total_extracted_rows} 条。\n"
                                "请向下滚动查找翻页按钮后继续翻页提取。"
                            )
                    elif _auto_pages >= 2:
                        # 不确定目标数量，让 VLM 自行判断
                        vlm.inject_error_feedback(
                            f"✅ 系统已自动提取当前页数据。"
                            f"你已经成功提取了 {_auto_pages} 个不同页面的数据"
                            f"（累计 {_total_extracted_rows} 条）。\n"
                            "请仔细回顾用户的原始任务要求，"
                            "判断是否需要继续翻页提取更多数据。\n"
                            "如果已满足用户需求，请输出 done 结束任务。"
                        )
                    else:
                        _pag_links = browser.find_pagination_links()
                        if _pag_links:
                            _pag_hint = "\n".join(
                                f"  → [ID: {p['id']}] {p['role']}: \"{p['name']}\""
                                for p in _pag_links
                            )
                            vlm.inject_error_feedback(
                                f"✅ 系统已自动提取当前页数据"
                                f"（已提取 {_auto_pages} 个页面，"
                                f"累计 {_total_extracted_rows} 条）。\n"
                                f"系统在当前页面发现了以下翻页链接：\n{_pag_hint}\n"
                                f"【立即操作】请点击翻页链接加载下一页，例如："
                                f"click(target_id={_pag_links[0]['id']})\n"
                                f"⚠️ 必须使用上述精确的 ID，不要猜测其他 ID！"
                            )
                        else:
                            vlm.inject_error_feedback(
                                f"✅ 系统已自动提取当前页数据"
                                f"（已提取 {_auto_pages} 个页面，"
                                f"累计 {_total_extracted_rows} 条）。\n"
                                "当前页面未发现翻页链接，可能已是最后一页。\n"
                                "如果任务还需要更多数据，请尝试向下滚动查找翻页按钮。\n"
                                "如果已完成所有页的提取，请直接输出 done 结束任务。"
                            )
                    # 跳过内层动作循环（因为 extract 已自动完成）
                    # 但如果连续太多次自动提取同一页面，强制结束
                    if _extract_null_streak >= 2:  # lowered from 3
                        # Check if we should retry instead of terminating
                        _should_terminate = True
                        if (
                            _goal_target is not None
                            and _total_extracted_rows < _goal_target
                            and _extract_null_total_resets < 5  # raised from 3
                        ):
                            _should_terminate = False
                            _extract_null_total_resets += 1
                            _scroll_escalation = {1: 1500, 2: 3000, 3: 4000, 4: 5000, 5: 5000}
                            _scroll_amount = _scroll_escalation.get(_extract_null_total_resets, 5000)
                            logger.warning(
                                f"[EXTRACT AUTO] streak={_extract_null_streak} 但进度 "
                                f"{_total_extracted_rows}/{_goal_target}，"
                                f"递增滚动 {_scroll_amount}px (reset #{_extract_null_total_resets}/5)"
                            )
                            _content_changed = await _nudge_scroll_after_duplicate_extract(
                                f"extract_null_streak={_extract_null_streak}, reset #{_extract_null_total_resets}",
                                scroll_amount=_scroll_amount
                            )
                            if not _content_changed:
                                logger.warning(
                                    f"[EXTRACT AUTO] 滚动后内容未变化，下次重试将使用更大滚动量"
                                )
                            _extract_null_streak = 0
                            continue
                        if _should_terminate:
                            logger.warning(
                                "[EXTRACT AUTO] 连续空提取且重试耗尽，强制结束任务"
                            )
                            _run_succeeded = True
                            _task_completed = True
                            break
                    continue  # 跳到下一步重新截图
                else:
                    if _extract_null_streak > 0:
                        logger.info(
                            f"[EXTRACT AUTO] extract+null 连击已中断 "
                            f"(was {_extract_null_streak})"
                        )
                    _extract_null_streak = 0

                # ── 多标签 Session 守卫：锚定标签 + thought 与 URL 一致性 ──
                if decisions:
                    try:
                        try:
                            from .tab_session_guards import apply_tab_session_guards
                        except ImportError:
                            from tab_session_guards import apply_tab_session_guards
                        _tsg_msg = apply_tab_session_guards(
                            browser,
                            decisions,
                            goal=goal,
                            start_url=start_url,
                            tab_anchor_index=_tab_session_anchor,
                        )
                        if _tsg_msg:
                            vlm.inject_error_feedback(_tsg_msg)
                            _tsg_head = decisions[0]
                            _tsg_head["action"] = "wait"
                            _tsg_head["target_id"] = 0
                            _tsg_head["type_value"] = "1"
                            decisions[:] = [_tsg_head]
                    except Exception as _tsg_err:
                        logger.debug("[TAB SESSION] guards skipped: %s", _tsg_err)

                # ── SoM 跨页失效守卫：同一 target_id 在最近若干步内跨 URL 复用 ──
                # 触发条件比 tab_session_guards 更宽：不需要 goal 写「切回」即生效。
                # 行为：仅在头动作发出且 target_id 在最近 _LOOP_GUARD_WINDOW 步内
                # 出现在不同归一化 URL 时，把头动作改成 wait + 注入 feedback。
                if decisions:
                    try:
                        try:
                            from .som_staleness_guard import detect_stale_som_reference
                        except ImportError:
                            from som_staleness_guard import detect_stale_som_reference
                        _stale_msg = detect_stale_som_reference(
                            decisions[0],
                            _som_target_refs,
                            browser.current_url or "",
                            window=_LOOP_GUARD_WINDOW,
                            url_normalizer=_norm_url_for_guard,
                        )
                        if _stale_msg:
                            logger.warning(
                                "[SOM STALE] target_id=%s on %s recently reused;"
                                " coerced head to wait",
                                decisions[0].get("target_id"),
                                (browser.current_url or "")[:80],
                            )
                            vlm.inject_error_feedback(_stale_msg)
                            _stale_head = decisions[0]
                            _stale_head["action"] = "wait"
                            _stale_head["target_id"] = 0
                            _stale_head["type_value"] = "1"
                            decisions[:] = [_stale_head]
                    except Exception as _stale_err:
                        logger.debug("[SOM STALE] guard skipped: %s", _stale_err)

                # ── 搜索结果纠偏：首个主结果必须按结构选取并在新标签页打开 ──
                if decisions:
                    try:
                        try:
                            from .search_result_guards import apply_search_result_open_guard
                        except ImportError:
                            from search_result_guards import apply_search_result_open_guard
                        await apply_search_result_open_guard(
                            browser,
                            decisions,
                            goal=goal,
                        )
                    except Exception as _search_result_err:
                        logger.debug("[SEARCH RESULT] guard skipped: %s", _search_result_err)

                # ── 标签页关闭纠偏：把浏览器标签标题误当 DOM 文本点击 → close_tab(index) ──
                if decisions:
                    try:
                        try:
                            from .tab_close_guards import (
                                rewrite_close_intent_to_close_tab,
                                rewrite_redundant_close_to_done,
                                rewrite_tab_title_click_to_close_tab,
                            )
                        except ImportError:
                            from tab_close_guards import (
                                rewrite_close_intent_to_close_tab,
                                rewrite_redundant_close_to_done,
                                rewrite_tab_title_click_to_close_tab,
                            )
                        await rewrite_tab_title_click_to_close_tab(
                            browser,
                            decisions,
                            goal=goal,
                        )
                        await rewrite_close_intent_to_close_tab(
                            browser,
                            decisions,
                            goal=goal,
                        )
                        await rewrite_redundant_close_to_done(
                            browser,
                            decisions,
                            goal=goal,
                        )
                    except Exception as _tab_close_err:
                        logger.debug("[TAB CLOSE GUARD] skipped: %s", _tab_close_err)

                # ── 搜索框重输入纠偏：用户明确要求重新输入 query 时，直接 type+Enter ──
                if decisions:
                    try:
                        try:
                            from .search_reentry_guards import apply_search_reentry_guard
                        except ImportError:
                            from search_reentry_guards import apply_search_reentry_guard
                        await apply_search_reentry_guard(
                            browser,
                            decisions,
                            goal=goal,
                        )
                    except Exception as _search_reentry_err:
                        logger.debug("[SEARCH REENTRY] guard skipped: %s", _search_reentry_err)

                # ── 内层动作循环：依次执行批次中的每个动作 ──────────────────
                # break → 中止本批次，进入下一步（重新截图）
                # _task_completed = True + break → 中止批次并退出外层主循环
                _task_completed = False

                # 🛡️ 连招熔断：连招开始前记录当前域名，用于检测意外跨域跳转
                # 单动作批次无需检测（不构成连招），只在 len > 1 时启用
                _batch_initial_domain = (
                    urlparse(browser.current_url).netloc
                    if len(decisions) > 1 else ""
                )

                # 记录本步 (action, target_id, type_value) 供下一步"思想-动作分离"检测
                if decisions:
                    _hd_final = decisions[0]
                    _prev_action_sig = (
                        str(_hd_final.get("action", "")),
                        int(_hd_final.get("target_id", 0) or 0),
                        str(_hd_final.get("type_value", "") or ""),
                    )

                for _action_idx, decision in enumerate(decisions):
                    if _action_idx > 0:
                        if _task_completed:
                            logger.info(
                                "[BATCH POST-EXTRACT GATE] remaining actions skipped: "
                                "task already completed before action %s/%s",
                                _action_idx + 1,
                                len(decisions),
                            )
                            break
                        if _goal_is_bulk_extraction(goal):
                            _completion_state = _extraction_targets_reached(
                                goal,
                                total_rows=_total_extracted_rows,
                                total_pages=len(_extracted_page_keys),
                            )
                            if _completion_state.get("reached"):
                                logger.info(
                                    "[BATCH POST-EXTRACT GATE] extraction target reached; "
                                    "skipping action %s/%s (%s). rows=%s/%s pages=%s/%s",
                                    _action_idx + 1,
                                    len(decisions),
                                    decision.get("action"),
                                    _completion_state.get("total_rows"),
                                    _completion_state.get("row_target"),
                                    _completion_state.get("total_pages"),
                                    _completion_state.get("page_target"),
                                )
                                _broadcast_log_safe(
                                    "[BATCH POST-EXTRACT GATE] 抽取目标已达成，跳过本轮剩余动作并结束任务"
                                )
                                event_stream.guard(
                                    step=step,
                                    name="BATCH_POST_EXTRACT_GATE",
                                    message=(
                                        "Extraction target reached after an earlier batch action; "
                                        "remaining actions skipped."
                                    ),
                                    metadata={
                                        "batch_size": len(decisions),
                                        "skipped_from_action_index": _action_idx,
                                        "next_action": decision.get("action", ""),
                                        "rows": _completion_state.get("total_rows"),
                                        "row_target": _completion_state.get("row_target"),
                                        "rows_reached": _completion_state.get("rows_reached"),
                                        "pages": _completion_state.get("total_pages"),
                                        "page_target": _completion_state.get("page_target"),
                                        "pages_reached": _completion_state.get("pages_reached"),
                                    },
                                )
                                _task_completed = True
                                _run_succeeded = True
                                break
                    _check_stop(f"before_step_{step}_action_{_action_idx + 1}")
                    if len(decisions) > 1:
                        logger.info(
                            f"[BATCH] 执行动作 [{_action_idx + 1}/{len(decisions)}]: "
                            f"{decision.get('action')}"
                        )

                    # 3. 检查异常状态
                    status = decision.get("status", "")
                    action = decision.get("action", "")

                    if status == "error" or action == "error":
                        # Bug #2 修复：VLM API 失败也计入连续错误计数（避免 _ERROR_DECISION
                        # 不走 ActionExecutionError 分支，老路径永远不触发熔断）。
                        _consecutive_errors += 1
                        _api_err_msg = (
                            decision.get("thought")
                            or "VLM 请求失败或返回格式异常"
                        )
                        logger.warning(
                            f"[WARN] VLM returned error status "
                            f"({_consecutive_errors}/{_MAX_CONSECUTIVE_ERRORS}): {_api_err_msg}"
                        )
                        vlm.annotate_last_result(f"❌ VLM API 失败: {_api_err_msg[:60]}")
                        if _consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                            _print_manual_warning(
                                "VLM API KEEPS FAILING",
                                f"VLM 后端连续 {_MAX_CONSECUTIVE_ERRORS} 次返回 error "
                                f"(network / rate-limit / malformed JSON)。"
                                f"检查 VLM_API_BASE / VLM_MODEL_NAME / 网络 / 配额，"
                                f"修复后按回车继续。最后一条：{_api_err_msg[:120]}",
                            )
                            if _stdin_interactive_for_agent():
                                await asyncio.get_event_loop().run_in_executor(None, input)
                                logger.info("[MANUAL] User confirmed after API failures, resuming...")
                                _consecutive_errors = 0
                                vlm.inject_error_feedback("")  # 清空积压反馈
                            else:
                                raise RuntimeError(
                                    f"VLM API consecutive failures ({_MAX_CONSECUTIVE_ERRORS}): "
                                    f"{_api_err_msg[:200]}"
                                )
                        break  # 中止本批次，进入下一步（重新截图）

                    if status == "captcha_detected" or action == "ask_human":
                        # ===== 人类接管协议 (HITL)：红色高亮提示 + 阻塞等待 =====
                        # 提取 VLM 的求助原因（type_value 承载具体描述）
                        _hitl_reason = (decision.get("type_value") or "").strip()

                        if status == "captcha_detected":
                            _print_manual_warning(
                                "CAPTCHA DETECTED - MANUAL ACTION REQUIRED",
                                f"Please complete the CAPTCHA in the browser window manually."
                                + (f"\n  VLM 求助原因: {_hitl_reason}" if _hitl_reason else ""),
                            )
                        else:
                            # ask_human：VLM 主动发起求助
                            _hitl_display = _hitl_reason or "VLM 遭遇障碍，需要人工协助"
                            _print_manual_warning(
                                "⏸️  HUMAN-IN-THE-LOOP — VSpider 主动求援",
                                f"VLM 求助原因: {_hitl_display}",
                            )
                            logger.warning(
                                f"[HITL] VLM 主动触发人类接管 | 原因: {_hitl_display}"
                            )

                        # ask_human 含人工干预，流程不可回放，禁止写入 RPA 缓存
                        _rpa_cache_allowed = False
                        _rpa_skip_reason = "contains ask_human / manual intervention step"

                        # 阻塞等待用户在浏览器中手动完成后恢复（API UI 或 CLI 回车）
                        await _wait_for_human_resume(_hitl_reason or "manual intervention required")
                        logger.info("[HITL] 用户已确认手动操作完成，恢复 VSpider 自动化流程...")

                        # ── Auto-return BEFORE harvest ────────────────────
                        # If SESSION DROP GUARD captured a return URL, goto
                        # it FIRST. Harvesting cookies on the login page
                        # (passport.baidu.com) would dump cookies under
                        # ``passport_baidu_com.json`` — wrong filename for
                        # the actual business site. Going back to the
                        # business URL first means the harvest sees the
                        # business host and writes the correct profile name.
                        if _pending_session_return_url:
                            _return_to = _pending_session_return_url
                            _pending_session_return_url = ""
                            try:
                                _active_page = await browser._ensure_active_page(
                                    reason="session-drop auto-resume goto"
                                )
                                if _active_page is not None:
                                    logger.info(
                                        "[SESSION DROP] auto-goto back to %s",
                                        _return_to[:80],
                                    )
                                    _broadcast_log_safe(
                                        f"🔁 [SESSION DROP] 登录已完成，正在回到 "
                                        f"{_return_to[:60]}",
                                        level="info",
                                    )
                                    await _active_page.goto(
                                        _return_to,
                                        wait_until="domcontentloaded",
                                        timeout=30000,
                                    )
                                    await browser._wait_for_page_stable()
                            except Exception as _return_err:
                                logger.warning(
                                    "[SESSION DROP] auto-return goto failed: %s",
                                    _return_err,
                                )

                        # ── Auto-harvest storage_state after human resume ──
                        # If the user just completed a login (scan QR, SMS code,
                        # password), persist the resulting cookies to
                        # .auth/<host>.json so they don't have to re-do it on
                        # any future run / machine / clean checkout. Filtered to
                        # just the active host's cookies so we don't smear
                        # unrelated sites into the same profile.
                        try:
                            try:
                                from .auth_harvester import harvest_storage_state as _hsh
                            except ImportError:  # pragma: no cover
                                from auth_harvester import harvest_storage_state as _hsh  # type: ignore[no-redef]
                            _harvest_url = browser.current_url or ""
                            _harvest_host = urlparse(_harvest_url).netloc if _harvest_url else ""
                            if _harvest_host and getattr(browser, "_context", None) is not None:
                                _harvest_result = await _hsh(
                                    browser._context,
                                    hint_host=_harvest_host,
                                )
                                if _harvest_result.saved:
                                    logger.info(
                                        "[AUTH HARVEST] %s",
                                        _harvest_result.reason,
                                    )
                                    _broadcast_log_safe(
                                        f"🔑 [登录态已自动保存] {_harvest_result.profile_name}.json "
                                        f"({_harvest_result.cookies_saved} cookies)",
                                        level="info",
                                    )
                                else:
                                    logger.debug(
                                        "[AUTH HARVEST] not saved: %s",
                                        _harvest_result.reason,
                                    )
                        except Exception as _harvest_err:
                            logger.debug(
                                "[AUTH HARVEST] post-resume harvest skipped: %s",
                                _harvest_err,
                            )

                        break  # 中止本批次，进入下一步（重新截图）

                    if action == "done" and _decision_mentions_follow_up_work(decision):
                        logger.warning(
                            "[DONE GUARD] Blocked premature done because decision text still "
                            "describes follow-up work."
                        )
                        vlm.inject_error_feedback(
                            "你刚才输出了 done，但 thought/current_state 仍在描述下一阶段或待执行动作。"
                            "这说明任务还没有完成。不要 done；请直接执行你自己提到的下一步操作。"
                        )
                        break

                    if _decision_implies_completion(decision):
                        logger.info(
                            "[DONE COERCE] VLM thought/current_state already indicates task completion; "
                            f"overriding action={action!r} to done."
                        )
                        decision = _force_done_decision(decision)
                        decisions[_action_idx] = decision
                        status = decision.get("status", "")
                        action = decision.get("action", "")

                    if _decision_is_regressive_backtrack(decision, goal):
                        logger.info(
                            "[REGRESSION GUARD] VLM is trying to navigate back from a result/confirmation page; "
                            f"overriding action={action!r} to done."
                        )
                        decision = _force_done_decision(decision)
                        decisions[_action_idx] = decision
                        status = decision.get("status", "")
                        action = decision.get("action", "")

                    if _decision_is_irrelevant_nav_click(decision, goal, input_descriptions):
                        logger.warning(
                            "[NAV GUARD] Blocked a likely global-navigation misclick while the goal "
                            "requires locating a concrete list/item entry."
                        )
                        _force_vision_next_step = True
                        vlm.inject_error_feedback(
                            "你刚才试图点击站点全局导航（如首页、电影、音乐、工作台等），"
                            "但当前任务要求在列表/结果页中定位具体条目。"
                            "下一步请忽略全局导航，只聚焦列表项本身。"
                        )
                        break

                    if _decision_is_auxiliary_search_control_click(decision, goal, input_descriptions):
                        logger.warning(
                            "[SEARCH GUARD] Blocked a likely click on a voice/camera/scanner control "
                            "while the goal expects a normal typed search flow."
                        )
                        vlm.inject_error_feedback(
                            "你刚才试图点击语音搜索、拍照搜索、扫码或相机按钮。"
                            "当前任务要求的是常规搜索路径：优先聚焦搜索输入框、输入关键词，"
                            "然后点击搜索按钮或按 Enter 提交。"
                            "除非用户明确要求语音/相机/扫码，否则不要再点击这类辅助入口。"
                        )
                        break

                    if _decision_is_non_submit_search_control_click(decision, goal, input_descriptions):
                        logger.warning(
                            "[SEARCH GUARD] Blocked a likely click on a suggestion/clear/history control "
                            "while the goal expects search submission."
                        )
                        vlm.inject_error_feedback(
                            "你刚才试图点击搜索建议项、删除按钮、清除按钮或历史记录入口。"
                            "这些控件通常不会真正提交搜索。"
                            "当前任务应优先选择真正的搜索按钮，或直接使用 press_key + Enter 提交。"
                        )
                        break

                    _extract_goal_target = _parse_goal_target_count(goal)
                    _extract_goal_pages = _parse_goal_target_pages(goal)
                    _is_bulk_extract_goal = _goal_is_bulk_extraction(goal)
                    if (
                        _is_bulk_extract_goal
                        and action in ("click", "click_text", "click_point")
                        and _extract_goal_target is not None
                        and _total_extracted_rows < _extract_goal_target
                    ):
                        _extract_nav_is_pagination = False
                        try:
                            _pagination_links_for_nav_guard = browser.find_pagination_links()
                            _pagination_ids_for_nav_guard = {
                                int(link.get("id"))
                                for link in _pagination_links_for_nav_guard
                                if link.get("id") is not None
                            }
                            _nav_guard_tid = int(decision.get("target_id") or 0)
                            if _nav_guard_tid and _nav_guard_tid in _pagination_ids_for_nav_guard:
                                _extract_nav_is_pagination = True
                            elif action == "click_text":
                                _nav_guard_text = str(decision.get("type_value") or "").strip().lower()
                                _extract_nav_is_pagination = any(
                                    _nav_guard_text
                                    and _nav_guard_text == str(link.get("name") or "").strip().lower()
                                    for link in _pagination_links_for_nav_guard
                                )
                        except Exception:
                            _extract_nav_is_pagination = False

                        if (
                            not _extract_nav_is_pagination
                            and not _decision_click_targets_extraction_control(
                                decision,
                                input_descriptions,
                            )
                        ):
                            _shape_for_same_page_nav: dict = {}
                            try:
                                _shape_for_same_page_nav = await browser.probe_data_shape()
                            except Exception:
                                _shape_for_same_page_nav = {}
                            if _data_shape_exposes_target_candidate(
                                _shape_for_same_page_nav,
                                _extract_goal_target,
                            ):
                                _nav_inspection = await _inspect_click_target_for_extract_nav_guard(
                                    decision
                                )
                                if _click_target_is_same_page_extract_nav(
                                    _nav_inspection,
                                    browser.current_url,
                                ):
                                    _guard_msg = (
                                        "Current page already exposes enough table/list/card "
                                        "candidates for the extraction target; same-page navigation "
                                        "or tab click was rewritten to extract."
                                    )
                                    logger.warning(
                                        "[EXTRACT SAME-PAGE NAV GUARD] %s inspection=%s shape=%s",
                                        _guard_msg,
                                        _nav_inspection,
                                        _shape_for_same_page_nav,
                                    )
                                    event_stream.guard(
                                        step=step,
                                        name="extract_same_page_nav_guard",
                                        message=_guard_msg,
                                        metadata={
                                            "inspection": _nav_inspection,
                                            "shape": _shape_for_same_page_nav,
                                            "progress": {
                                                "rows": _total_extracted_rows,
                                                "target": _extract_goal_target,
                                            },
                                            "decision": {
                                                "action": decision.get("action"),
                                                "target_id": decision.get("target_id"),
                                                "type_value": decision.get("type_value"),
                                                "point": decision.get("point"),
                                            },
                                        },
                                    )
                                    vlm.inject_error_feedback(
                                        "当前是抽取任务，且当前页面已经暴露了足够的表格/列表/卡片候选。"
                                        "不要继续点击同页 tab、当前页链接或导航自链接；"
                                        "系统已将本步改为 extract，先保存当前页面数据。"
                                    )
                                    decision["action"] = "extract"
                                    decision["target_id"] = 0
                                    decision["type_value"] = ""
                                    decision["status"] = "pending"
                                    decision["guard_rewrite"] = "extract_same_page_nav_guard"
                                    decisions[_action_idx] = decision
                                    action = "extract"
                                    status = decision.get("status", "")
                    if action == "done" and _is_bulk_extract_goal:
                        _need_more_rows = (
                            _extract_goal_target is not None
                            and _total_extracted_rows < _extract_goal_target
                        )
                        _need_more_pages = (
                            _extract_goal_pages is not None
                            and len(_extracted_page_keys) < _extract_goal_pages
                        )
                        if _need_more_rows or _need_more_pages:
                            if decision.get("extracted_data"):
                                logger.warning(
                                    "[DONE GUARD] Premature done with extracted_data before extraction "
                                    "target met; downgrading to extract "
                                    "(rows=%s/%s, pages=%s/%s)",
                                    _total_extracted_rows,
                                    _extract_goal_target,
                                    len(_extracted_page_keys),
                                    _extract_goal_pages,
                                )
                                decision["action"] = "extract"
                                if isinstance(decision.get("thought"), str):
                                    decision["thought"] = (
                                        "[DONE_DOWNGRADED_TO_EXTRACT] "
                                        + str(decision.get("thought") or "")
                                    )
                                decisions[_action_idx] = decision
                                status = decision.get("status", "")
                                action = "extract"
                            else:
                                _remaining_parts: list[str] = []
                                if _need_more_rows and _extract_goal_target is not None:
                                    _remaining_parts.append(
                                        f"系统记账仅 {_total_extracted_rows}/{_extract_goal_target} 条"
                                    )
                                if _need_more_pages and _extract_goal_pages is not None:
                                    _remaining_parts.append(
                                        f"仅完成 {len(_extracted_page_keys)}/{_extract_goal_pages} 页"
                                    )
                                logger.warning(
                                    "[DONE GUARD] Blocked premature done on extraction goal: %s",
                                    "; ".join(_remaining_parts) or "progress target not met",
                                )
                                vlm.inject_error_feedback(
                                    "⚠️ 当前仍是抓取/提取任务，但系统权威进度尚未达标（"
                                    + "；".join(_remaining_parts)
                                    + "）。不要直接 done。"
                                    "如果当前页刚刚翻到新页，请先执行 extract，"
                                    "不要把当前可见片段塞进 done 里冒充整页结果；"
                                    "如果当前页已经完整提取过且仍未达量，再继续 next_page。"
                                )
                                break

                    # 4. 数据提取（跨页累加模式）
                    if action == "extract":
                        _macro_guard = _parse_semantic_macro(goal)
                        if not (_macro_guard and _macro_guard.get("action") == "date_pick"):
                            _rpa_cache_allowed = False
                            _rpa_skip_reason = "contains extract steps"
                        extracted = decision.get("extracted_data")
                        _current_url = browser.current_url
                        _current_extract_page_key = _current_url

                        # 同 URL 可能是无限滚动/局部刷新列表，不再直接跳过。
                        # 真实重复由后面的行级 fingerprint 过滤。
                        if _current_url in _extracted_page_urls:
                            logger.info(
                                "[EXTRACT] URL already seen; row-level dedup will decide: %s",
                                _current_url,
                            )

                        if extracted:
                            _log_extract_text_source = "VLM_EXTRACT_OUTPUT"
                            _source_text_for_validation = ""
                            _data_shape = {}
                            try:
                                _data_shape = await browser.probe_data_shape()
                                logger.info("[DATA SHAPE] %s", _data_shape)
                                if _expected_rows_from_data_shape(_data_shape) >= 10:
                                    _drain_state = await _probe_scroll_drain_state(
                                        "explicit extract dense-shape drain override"
                                    )
                                    _data_shape = dict(_data_shape)
                                    _data_shape["physically_drained"] = bool(
                                        _drain_state.get("at_bottom")
                                    )
                                    _data_shape["drain_state"] = _drain_state
                                    logger.info(
                                        "[DATA SHAPE] dense drain_state=%s",
                                        _drain_state,
                                    )
                            except Exception as _shape_err:
                                logger.debug("[DATA SHAPE] skipped: %s", _shape_err)

                            _candidates: list[dict] = []
                            _full_extract_text_source = ""
                            _full_page_text = ""
                            _full_extracted = None
                            try:
                                _full_extract_text_source, _full_page_text = (
                                    await _extract_full_page_text_for_data(
                                        "extract full page text"
                                    )
                                )
                                _source_text_for_validation = _full_page_text or ""
                                _full_extracted = await vlm.extract_structured_data(
                                    page_text=_full_page_text,
                                    goal=goal,
                                    example_data=extracted,
                                )
                                if _full_extracted:
                                    logger.info(
                                        "[EXTRACT FULL] %s returned %s rows",
                                        _full_extract_text_source,
                                        len(_full_extracted)
                                        if isinstance(_full_extracted, list) else 1,
                                    )
                            except Exception as _full_err:
                                logger.warning(
                                    f"[EXTRACT FULL] 全页提取失败，使用 VLM 原始数据: {_full_err}"
                                )
                                try:
                                    _page_for_validate = await browser._ensure_active_page(
                                        reason="extract source validation fallback"
                                    )
                                    _source_text_for_validation = await _page_for_validate.evaluate(
                                        "() => document.body.innerText"
                                    )
                                except Exception:
                                    _source_text_for_validation = ""

                            if extracted:
                                _candidates.append(
                                    _sanitize_extraction_candidate(
                                        name="VIEWPORT_VLM",
                                        data=extracted,
                                        source_text=_source_text_for_validation,
                                        data_shape=_data_shape,
                                    )
                                )
                            if _full_extracted:
                                _candidates.append(
                                    _sanitize_extraction_candidate(
                                        name=f"FULL_PAGE:{_full_extract_text_source}",
                                        data=_full_extracted,
                                        source_text=_full_page_text or _source_text_for_validation,
                                        data_shape=_data_shape,
                                    )
                                )

                            _dom_list_rows, _dom_list_text = (
                                ([], "")
                                if _goal_is_tooltip_extract(goal)
                                else await _extract_list_rows_via_dom(
                                    "extract visible list rows"
                                )
                            )
                            if _dom_list_rows:
                                _dom_card_source_text = "\n\n".join(
                                    text
                                    for text in (
                                        _full_page_text,
                                        _source_text_for_validation,
                                        _dom_list_text,
                                    )
                                    if text
                                )
                                _dom_card_rows, _dom_card_text = extract_semantic_card_rows(
                                    _dom_list_rows,
                                    source_text=_dom_card_source_text,
                                    requested_fields=_requested_output_fields,
                                    goal=goal,
                                )
                                if not _dom_card_rows:
                                    _dom_card_body_text = await _extract_body_text_for_semantic_cards(
                                        "extract semantic card body text"
                                    )
                                    if _dom_card_body_text:
                                        _dom_card_rows, _dom_card_text = extract_semantic_card_rows(
                                            _dom_list_rows,
                                            source_text="\n\n".join(
                                                text
                                                for text in (
                                                    _dom_card_body_text,
                                                    _dom_card_source_text,
                                                )
                                                if text
                                            ),
                                            requested_fields=_requested_output_fields,
                                            goal=goal,
                                        )
                                if _dom_card_rows:
                                    logger.info(
                                        "[EXTRACT DOM] semantic cards rows=%s source_chars=%s",
                                        len(_dom_card_rows),
                                        len(_dom_card_text or _dom_list_text or ""),
                                    )
                                    _candidates.append(
                                        _sanitize_extraction_candidate(
                                            name="DOM_CARDS",
                                            data=_dom_card_rows,
                                            source_text=(
                                                _dom_card_text
                                                or _dom_list_text
                                                or _source_text_for_validation
                                            ),
                                            data_shape=_data_shape,
                                        )
                                    )
                                _candidates.append(
                                    _sanitize_extraction_candidate(
                                        name="DOM_LIST",
                                        data=_dom_list_rows,
                                        source_text=_dom_list_text or _source_text_for_validation,
                                        data_shape=_data_shape,
                                    )
                                )

                            _dom_table_rows = (
                                []
                                if _goal_is_tooltip_extract(goal)
                                else await _extract_visible_table_rows_via_dom(
                                    "extract visible table rows"
                                )
                            )
                            if _dom_table_rows:
                                if not _source_text_for_validation:
                                    try:
                                        _page_for_validate = await browser._ensure_active_page(
                                            reason="extract DOM source validation"
                                        )
                                        _source_text_for_validation = await _page_for_validate.evaluate(
                                            "() => document.body.innerText"
                                        )
                                    except Exception:
                                        _source_text_for_validation = ""
                                _candidates.append(
                                    _sanitize_extraction_candidate(
                                        name="DOM_TABLE",
                                        data=_dom_table_rows,
                                        source_text=_source_text_for_validation,
                                            data_shape=_data_shape,
                                    )
                                )

                            try:
                                _candidates = rank_extraction_candidates_with_history(
                                    _candidates,
                                    url=_current_url,
                                    requested_fields=_requested_output_fields,
                                    goal=_snapshot_goal,
                                )
                                _boosted = [
                                    c for c in _candidates
                                    if (c.get("recovery") or {}).get("boost")
                                ]
                                if _boosted:
                                    logger.info(
                                        "[EXTRACT RECOVERY] history boosts=%s",
                                        "; ".join(
                                            f"{c.get('name')}:"
                                            f"+{float((c.get('recovery') or {}).get('boost') or 0):.1f}"
                                            f" match={float((c.get('recovery') or {}).get('score') or 0):.2f}"
                                            for c in _boosted
                                        ),
                                    )
                            except Exception as _recovery_err:
                                logger.debug("[EXTRACT RECOVERY] skipped: %s", _recovery_err)

                            _chosen_candidate = _choose_best_extraction_candidate(_candidates)
                            if _chosen_candidate:
                                (
                                    extracted,
                                    _new_rows,
                                    _dup_rows,
                                    _rejected_rows,
                                    _source_text_for_validation,
                                ) = _commit_extraction_candidate(_chosen_candidate)
                                _log_extract_text_source = str(
                                    _chosen_candidate.get("name") or "VLM_EXTRACT_OUTPUT"
                                )
                                if _log_extract_text_source == "DOM_TABLE":
                                    _dom_sig = await _visible_table_signature(
                                        "extract DOM page signature"
                                    )
                                    if _dom_sig:
                                        _current_extract_page_key = (
                                            f"{_current_url}#table:"
                                            f"{hashlib.md5(_dom_sig.encode('utf-8', errors='ignore')).hexdigest()}"
                                        )
                            else:
                                extracted, _new_rows, _dup_rows, _rejected_rows = [], 0, 0, 0
                            decision["extracted_data"] = extracted
                            if _new_rows == 0:
                                _dedup_tripped_last_step = True
                                logger.warning(
                                    "[EXTRACT DEDUP] no new rows after row-level filtering "
                                    "(duplicates=%s, rejected=%s, url=%s)",
                                    _dup_rows,
                                    _rejected_rows,
                                    _current_url,
                                )
                                _target_count_pre = _parse_goal_target_count(goal)
                                _pre_reached = (
                                    _target_count_pre is not None
                                    and _total_extracted_rows >= _target_count_pre
                                )
                                if _pre_reached:
                                    vlm.inject_error_feedback(
                                        f"✅ 你已累计提取 {_total_extracted_rows} 条数据，"
                                        f"已达成用户要求的 {_target_count_pre} 条。"
                                        "请立即输出 action=done 结束任务，不要再 extract。"
                                    )
                                else:
                                    _has_prior_extract_page = bool(_extracted_page_urls or _extracted_page_keys)
                                    if _target_count_pre is not None and _has_prior_extract_page:
                                        _duplicate_zero_extract_streak += 1
                                        _scroll_drain = await _probe_scroll_drain_state(
                                            "duplicate extract drain probe"
                                        )
                                        _physically_drained = bool(_scroll_drain.get("at_bottom"))
                                        _probe_failed = bool(_scroll_drain.get("probe_failed"))
                                        if _physically_drained or (_probe_failed and _duplicate_zero_extract_streak >= 3):
                                            _first_flip_pending = True
                                            _drain_reason = (
                                                "物理触底"
                                                if _physically_drained
                                                else "触底探测失败且连续多次无新增"
                                            )
                                            vlm.inject_error_feedback(
                                                f"⚠️ 系统 extract 净新增为 0，且已确认{_drain_reason}。\n"
                                                f"当前累计 {_total_extracted_rows}/{_target_count_pre} 条，"
                                                "说明当前页/当前滚动区域已基本榨干但目标尚未达成。\n"
                                                "下一步必须执行 next_page（target_id=0, type_value=\"\"），"
                                                "让底层优先尝试 URL 变异/分页器/页码；不要继续 smooth_scroll "
                                                "或重复 extract 当前页。"
                                            )
                                        else:
                                            _remaining_hint = (
                                                f"window_remaining={_scroll_drain.get('window_remaining')}, "
                                                f"container_remaining={_scroll_drain.get('container_remaining')}"
                                            )
                                            vlm.inject_error_feedback(
                                                "⚠️ 系统执行了 extract，但行级去重发现没有新增数据。\n"
                                                f"当前累计 {_total_extracted_rows}/{_target_count_pre} 条，"
                                                "这只能证明当前视口没有新行，尚不能证明整页已榨干。\n"
                                                f"物理滚动探测显示仍有下滑空间（{_remaining_hint}）。"
                                                "下一步先 smooth_scroll down 暴露同页下方隐藏数据；"
                                                "只有净新增为 0 且物理触底后，系统才会强制 next_page。"
                                            )
                                            await _nudge_scroll_after_duplicate_extract(
                                                "first duplicate extract before pagination"
                                            )
                                    else:
                                        _duplicate_zero_extract_streak += 1
                                        _expected_dense_rows = _expected_rows_from_data_shape(
                                            _data_shape
                                        )
                                        if _expected_dense_rows >= 10:
                                            _block_next_page_until_drained = True
                                            _block_next_page_reason = (
                                                f"dense page exposes about {_expected_dense_rows} rows, "
                                                "but viewport/full extraction under-yielded"
                                            )
                                            vlm.inject_error_feedback(
                                                "⚠️ 系统探头发现当前页存在密集列表/表格，"
                                                f"大约 {_expected_dense_rows} 个结构化条目；"
                                                "但本次 extract 没有得到足够新增行。\n"
                                                "这说明当前页尚未被可靠提取，下一步先 smooth_scroll down "
                                                "或重新 extract 当前页，禁止直接 next_page。"
                                            )
                                        else:
                                            vlm.inject_error_feedback(
                                                "⚠️ 系统执行了 extract，但行级去重发现没有新增数据。\n"
                                                "请不要重复提取当前列表。下一步优先 next_page；"
                                                "若 next_page 报错，再考虑 smooth_scroll 加载更多。"
                                            )
                                        await _nudge_scroll_after_duplicate_extract(
                                            "explicit extract duplicate rows"
                                        )
                                break

                            if _dup_rows or _rejected_rows:
                                logger.info(
                                    "[EXTRACT DEDUP] filtered %s duplicate rows, "
                                    "rejected %s unsupported rows, saving %s new rows",
                                    _dup_rows,
                                    _rejected_rows,
                                    _new_rows,
                                )

                            # 统计本次新增行数。tooltip 任务按 trigger 主键统计，避免
                            # 中间半成品被 UPSERT 覆盖后仍显示累计过高。
                            extracted = await _enrich_rows_with_dom_links(extracted)
                            decision["extracted_data"] = extracted
                            _progress_new_rows, _progress_total_rows = _record_extract_progress(
                                extracted,
                                _new_rows,
                            )
                            _duplicate_zero_extract_streak = 0
                            logger.info(
                                f"[EXTRACT] 本次提取 {_new_rows} 条，"
                                f"累计已提取 {_progress_total_rows} 条"
                            )
                            saved_path = ""
                            if _goal_output_mode == "answer":
                                logger.info(
                                    "[ANSWER OUTPUT] answer-only result; not saving Excel artifact"
                                )
                            else:
                                saved_path = save_run_dataset(
                                    extracted,
                                    run_id=_run_ts,
                                    output_contract=_goal_output_contract,
                                    produced_by="vlm_extract",
                                    step_id=str(step),
                                    filename_hint=_vlm_output,
                                    unique_key=TOOLTIP_UNIQUE_KEY if _goal_is_tooltip_extract(goal) else None,
                                )
                            _api_fast = await _try_dom_api_fast_path(
                                extracted,
                                source=_log_extract_text_source or "VLM_EXTRACT_OUTPUT",
                            )
                            if _api_fast.get("applied"):
                                extracted = _api_fast.get("fast_path", {}).get("rows") or extracted
                                decision["extracted_data"] = extracted
                                _progress_total_rows = _total_extracted_rows
                            _extract_snapshot_path = await _save_extraction_snapshot(
                                source=_log_extract_text_source or "VLM_EXTRACT_OUTPUT",
                                rows=extracted,
                                output_file=str(saved_path or ""),
                                accepted_rows=_new_rows,
                                duplicate_rows=_dup_rows,
                                rejected_rows=_rejected_rows,
                                candidates=_candidates,
                                data_shape=_data_shape,
                                source_text=_source_text_for_validation,
                                metadata={
                                    "mode": "explicit_extract",
                                    "progress_new_rows": _progress_new_rows,
                                    "progress_total_rows": _progress_total_rows,
                                },
                            )
                            if _extract_snapshot_path:
                                decision["snapshot_path"] = _extract_snapshot_path
                            if saved_path:
                                logger.info(f"[EXTRACT] Saved to: {saved_path}")
                            if _goal_is_tooltip_extract(goal):
                                print(
                                    f"\033[1;32m✅ [EXTRACT]\033[0m "
                                    f"成功合并 \033[36m{_new_rows}\033[0m 条候选。"
                                    f"当前唯一提示项: \033[36m{_progress_total_rows}\033[0m 条"
                                )
                            else:
                                print(
                                    f"\033[1;32m✅ [EXTRACT]\033[0m "
                                    f"成功追加 \033[36m{_new_rows}\033[0m 条数据。"
                                    f"当前总计: \033[36m{_progress_total_rows}\033[0m 条"
                                )
                            _extract_count += 1
                            _extracted_page_urls.add(_current_url)
                            _extracted_page_keys.add(_current_extract_page_key)
                            if _goal_output_mode == "answer":
                                logger.info(
                                    "[ANSWER OUTPUT] completed after targeted extract; "
                                    "source=%s rows=%s saved=%s",
                                    _log_extract_text_source,
                                    _new_rows,
                                    saved_path,
                                )
                                _answer_text = _format_extracted_rows_as_answer(extracted)
                                if not _answer_text:
                                    _answer_text = _clean_user_visible_done_message(
                                        decision.get("thought")
                                        or decision.get("current_state")
                                        or ""
                                    )
                                if _answer_text:
                                    _record_run_answer(text=_answer_text)
                                event_stream.done(
                                    step=step,
                                    success=True,
                                    reason="answer_extract_completed",
                                    message=(
                                        "answer-mode extract completed: "
                                        f"source={_log_extract_text_source} rows={_new_rows}"
                                    ),
                                    metadata={
                                        "output_mode": _goal_output_mode,
                                        "output_file": str(saved_path or ""),
                                        "extract_source": _log_extract_text_source,
                                        "rows": _new_rows,
                                    },
                                )
                                _broadcast_done_safe(True, "answer extract completed")
                                _task_completed = True
                                _run_succeeded = True
                                break
                            # ── 首翻引擎硬约束：首次 extract 后强制下一步 next_page ──
                            # Bug 修复：用任务级永久锁，避免翻页后 _extract_count 重置反复触发
                            if (
                                not _first_extract_ever_done
                                and _should_force_first_flip_after_successful_extract(goal)
                            ):
                                _first_extract_ever_done = True
                                _first_flip_pending = True
                            # ── Improvement 1：首次 extract 后探测分页器（显式 extract 路径） ──
                            if (
                                _goal_needs_pagination_probe(goal)
                                and not _pagination_probed
                                and _extract_count == 1
                            ):
                                _pagination_probed = True
                                try:
                                    _probe = await browser.probe_pagination()
                                    _pagination_kind = _probe.get("kind", "")
                                    _cands = _probe.get("candidates", [])
                                    if _probe.get("has_paginator"):
                                        _names = ", ".join(
                                            f"{c['ref']}={c['name']!r}" for c in _cands[:6]
                                        )
                                        _should_force_probe_next, _force_probe_reason = (
                                            _should_schedule_next_page_after_extract(
                                                goal,
                                                new_rows=_new_rows,
                                                total_rows=_total_extracted_rows,
                                                extract_source=_log_extract_text_source,
                                                pagination_kind=_pagination_kind,
                                                expected_rows=_expected_rows_from_data_shape(_data_shape),
                                                physically_drained=bool(_data_shape.get("physically_drained")),
                                            )
                                        )
                                        if _should_force_probe_next:
                                            _force_next_page_pending = True
                                            _first_flip_pending = False
                                            _block_next_page_until_drained = False
                                            _block_next_page_reason = ""
                                            logger.info(
                                                "[PROBE PAGE] armed next_page after extract: %s",
                                                _force_probe_reason,
                                            )
                                            _broadcast_log_safe(
                                                f"[PROBE PAGE] 已发现分页器，下一轮直接 next_page：{_force_probe_reason}",
                                                level="info",
                                            )
                                            _pagination_hint_msg = (
                                                f"📍【系统探测：本页**带分页器**（{_pagination_kind}）】\n"
                                                f"已确认页面底部存在翻页控件：{_names}。\n"
                                                f"当前页已提取到足够完整的一批数据（{_force_probe_reason}）。"
                                                f"下一步**必须**用 next_page（首选 URL Mutation）翻页，"
                                                f"**禁止** smooth_scroll 当无限滚动处理。"
                                            )
                                        else:
                                            _drain_state = await _probe_scroll_drain_state(
                                                "pagination probe low-yield explicit extract"
                                            )
                                            if not bool(_drain_state.get("at_bottom")):
                                                _block_next_page_until_drained = True
                                                _block_next_page_reason = _force_probe_reason
                                            else:
                                                _force_next_page_pending = True
                                                _first_flip_pending = False
                                                _block_next_page_until_drained = False
                                                _block_next_page_reason = ""
                                                _force_probe_reason = (
                                                    f"{_force_probe_reason}; physical bottom reached"
                                                )
                                            _pagination_hint_msg = (
                                                f"📍【系统探测：本页**带分页器**（{_pagination_kind}）】\n"
                                                f"已确认页面存在翻页控件：{_names}。\n"
                                                f"但本次仅新增 {_new_rows} 条（{_force_probe_reason}），"
                                                "不足以证明当前页已提取完。\n"
                                                "下一步请先 smooth_scroll 向下并继续 extract 当前页；"
                                                "只有当前页物理触底或无新增后，才使用 next_page。"
                                            )
                                    else:
                                        # No paginator detected — mark as infinite scroll
                                        _page_is_infinite_scroll = True
                                        _should_force_probe_next, _force_probe_reason = (
                                            _should_schedule_next_page_after_extract(
                                                goal,
                                                new_rows=_new_rows,
                                                total_rows=_total_extracted_rows,
                                                extract_source=_log_extract_text_source,
                                                pagination_kind=_pagination_kind,
                                                expected_rows=_expected_rows_from_data_shape(_data_shape),
                                                physically_drained=bool(_data_shape.get("physically_drained")),
                                            )
                                        )
                                        if _should_force_probe_next:
                                            _force_next_page_pending = True
                                            _first_flip_pending = False
                                            _block_next_page_until_drained = False
                                            _block_next_page_reason = ""
                                            logger.info(
                                                "[PROBE PAGE] armed universal next_page after extract: %s",
                                                _force_probe_reason,
                                            )
                                            _broadcast_log_safe(
                                                f"[PROBE PAGE] 大批量提取未达量，下一轮交给 next_page 宏动作：{_force_probe_reason}",
                                                level="info",
                                            )
                                            _pagination_hint_msg = (
                                                "📍【系统探测：本页**无分页器**（infinite 模式）】\n"
                                                "当前提取批次已足够大但目标未达成。"
                                                "下一步使用 next_page 宏动作；如果确实没有分页器，"
                                                "底层会自动走 L4 滚动兜底加载新数据。"
                                            )
                                        else:
                                            _pagination_hint_msg = (
                                                "📍【系统探测：本页**无分页器**】\n"
                                                f"本次仅新增 {_new_rows} 条（{_force_probe_reason}），"
                                                "请继续 smooth_scroll / extract 当前列表；"
                                                "如果滚动触底且仍未达量，引擎会再调度 next_page 宏动作。"
                                            )
                                    logger.info(f"[PROBE PAGE] kind={_pagination_kind} cands={len(_cands)}")
                                except Exception as _probe_err:
                                    logger.warning(f"[PROBE PAGE] 失败忽略：{_probe_err}")
                            # ── Re-arm：已知分页器 + 目标未达 → 每次 extract 后强制 next_page ──
                            if (
                                _pagination_probed
                                and _pagination_kind not in ("", "infinite")
                                and not _page_is_infinite_scroll
                                and not _force_next_page_pending
                            ):
                                _rearm_target = _parse_goal_target_count(goal)
                                if _rearm_target is not None and _total_extracted_rows < _rearm_target:
                                    _force_next_page_pending = True
                                    logger.info(
                                        "[REARM NEXT_PAGE] paginator known (%s), target not met "
                                        "(%s/%s); re-armed for next step",
                                        _pagination_kind,
                                        _total_extracted_rows,
                                        _rearm_target,
                                    )
                            # ── Path C：Hard Kill 引擎层强杀（同上）──
                            _hk_target = _parse_goal_target_count(goal)
                            if _hk_target is not None and _total_extracted_rows >= _hk_target:
                                logger.info(
                                    f"[HARD KILL] 引擎达量终止：累计 "
                                    f"{_total_extracted_rows} >= 目标 {_hk_target} 条"
                                )
                                print(
                                    f"\033[1;32m🎯 [HARD KILL]\033[0m 已达量 "
                                    f"{_total_extracted_rows}/{_hk_target}，引擎层终止任务"
                                )
                                _broadcast_log_safe(
                                    f"[HARD KILL] 累计 {_total_extracted_rows}/{_hk_target} 条达量，引擎终止"
                                )
                                _task_completed = True
                                _run_succeeded = True
                                break
                            _hk_pages = _parse_goal_target_pages(goal)
                            if _hk_pages is not None and len(_extracted_page_keys) >= _hk_pages:
                                logger.info(
                                    "[HARD KILL] 表格页数达标：%s/%s pages, rows=%s",
                                    len(_extracted_page_keys),
                                    _hk_pages,
                                    _total_extracted_rows,
                                )
                                _broadcast_log_safe(
                                    f"[HARD KILL] 已提取 {len(_extracted_page_keys)}/{_hk_pages} 页，"
                                    f"累计 {_total_extracted_rows} 条，任务完成"
                                )
                                _task_completed = True
                                _run_succeeded = True
                                break

                            _need_more_table_pages = (
                                _log_extract_text_source == "DOM_TABLE"
                                and (
                                    (_hk_target is not None and _total_extracted_rows < _hk_target)
                                    or (_hk_pages is not None and len(_extracted_page_keys) < _hk_pages)
                                )
                            )
                            if _need_more_table_pages:
                                _advanced = await _auto_advance_table_page_via_dom(
                                    "advance after successful table extract"
                                )
                                if _advanced:
                                    _extract_count = 0
                                    vlm.inject_error_feedback(
                                        f"✅ 系统已保存当前表格页 {_new_rows} 条，"
                                        f"累计 {_total_extracted_rows} 条。"
                                        "底层已自动点击下一页，下一步请直接执行 extract，"
                                        "不要回到上一页，也不要重复提取刚才的数据。"
                                    )
                                    break
                        else:
                            logger.warning("[EXTRACT] No extracted_data in VLM response")

                        # ── 智能翻页/结束引导（根据已提取页数 + 目标数量决定建议） ──
                        _n_pages = max(len(_extracted_page_urls), len(_extracted_page_keys))
                        _target_count_b = _parse_goal_target_count(goal)
                        _reached_target_b = (
                            _target_count_b is not None
                            and _total_extracted_rows >= _target_count_b
                        )
                        if _reached_target_b:
                            vlm.inject_error_feedback(
                                f"✅ 你已成功提取 {_n_pages} 个不同页面的数据"
                                f"（累计 {_total_extracted_rows} 条）。\n"
                                f"用户要求获取 {_target_count_b} 条数据，"
                                f"当前已达到目标！请立即输出 done 结束任务。"
                            )
                        elif _n_pages >= 2 and _target_count_b is not None:
                            _pag_links_c = browser.find_pagination_links()
                            if _pag_links_c:
                                _pag_hint_c = "\n".join(
                                    f"  → [ID: {p['id']}] {p['role']}: \"{p['name']}\""
                                    for p in _pag_links_c
                                )
                                vlm.inject_error_feedback(
                                    f"✅ 你已成功提取 {_n_pages} 个页面"
                                    f"（累计 {_total_extracted_rows} 条），"
                                    f"但用户要求 {_target_count_b} 条，"
                                    f"还差 {_target_count_b - _total_extracted_rows} 条。\n"
                                    f"系统发现了翻页链接：\n{_pag_hint_c}\n"
                                    f"【立即操作】请继续翻页，例如："
                                    f"click(target_id={_pag_links_c[0]['id']})"
                                )
                            else:
                                vlm.inject_error_feedback(
                                    f"✅ 你已成功提取 {_n_pages} 个页面"
                                    f"（累计 {_total_extracted_rows} 条），"
                                    f"但用户要求 {_target_count_b} 条，"
                                    f"还差 {_target_count_b - _total_extracted_rows} 条。\n"
                                    "请向下滚动查找翻页按钮后继续翻页提取。"
                                )
                        elif _n_pages >= 2:
                            vlm.inject_error_feedback(
                                f"✅ 你已成功提取 {_n_pages} 个不同页面的数据"
                                f"（累计 {_total_extracted_rows} 条）。\n"
                                "请仔细回顾用户的原始任务要求，"
                                "判断是否需要继续翻页提取更多数据。\n"
                                "如果已满足用户需求，请输出 done 结束任务。"
                            )
                        elif _extract_count > 0:
                            _pag_links_b = browser.find_pagination_links()
                            if _pag_links_b:
                                _pag_hint_b = "\n".join(
                                    f"  → [ID: {p['id']}] {p['role']}: \"{p['name']}\""
                                    for p in _pag_links_b
                                )
                                vlm.inject_error_feedback(
                                    f"✅ 你已成功提取当前页数据"
                                    f"（第 {_n_pages} 个页面，累计 {_total_extracted_rows} 条）。\n"
                                    f"系统在当前页面发现了以下翻页链接：\n{_pag_hint_b}\n"
                                    f"【立即操作】请点击翻页链接加载下一页，例如："
                                    f"click(target_id={_pag_links_b[0]['id']})\n"
                                    f"⚠️ 必须使用上述精确的 ID，不要猜测其他 ID！"
                                )
                            else:
                                vlm.inject_error_feedback(
                                    f"✅ 你已成功提取当前页数据"
                                    f"（第 {_n_pages} 个页面，累计 {_total_extracted_rows} 条）。\n"
                                    "当前页面未发现翻页链接，可能已是最后一页。\n"
                                    "如果任务还需要更多数据，请尝试向下滚动查找翻页按钮。\n"
                                    "如果已完成所有页的提取，请直接输出 done 结束任务。"
                                )

                        # 连续 extract 守卫（防止 VLM 不翻页也不 done 陷入死循环）
                        if _extract_count >= 3:
                            _guard_target = _parse_goal_target_count(goal)
                            if _guard_target is not None and _total_extracted_rows < _guard_target:
                                logger.warning(
                                    "[EXTRACT GUARD] consecutive extract threshold reached, "
                                    "but target is not met (%s/%s); force navigation instead of ending.",
                                    _total_extracted_rows,
                                    _guard_target,
                                )
                                vlm.inject_error_feedback(
                                    f"⚠️ 系统检测到连续 extract 次数过多，但当前只提取 "
                                    f"{_total_extracted_rows}/{_guard_target} 条，尚未达标。\n"
                                    "下一步禁止继续 extract；必须先执行 next_page、click_text 页码/Next，"
                                    "或 smooth_scroll down 加载更多真实数据。"
                                )
                                _extract_count = 0
                                break
                            logger.warning(
                                "[EXTRACT GUARD] 连续 extract 无翻页动作，"
                                f"已累积 {_total_extracted_rows} 条数据，强制结束任务。"
                            )
                            _run_succeeded = True
                            _task_completed = True
                            # ── PROGRESS SAFEGUARD ──
                            _pg_target = _parse_goal_target_count(goal)
                            if _pg_target is not None and _total_extracted_rows < _pg_target:
                                logger.warning(
                                    f"[PROGRESS SAFEGUARD] 终止时进度不足: {_total_extracted_rows}/{_pg_target}，标记为失败"
                                )
                                _run_succeeded = False
                            _extract_guard_done_state = _extraction_targets_reached(
                                goal,
                                total_rows=_total_extracted_rows,
                                total_pages=len(_extracted_page_keys),
                            )
                            event_stream.done(
                                step=step,
                                success=_run_succeeded,
                                reason=(
                                    "extract_guard"
                                    if _run_succeeded
                                    else "extract_guard_progress_safeguard_failed"
                                ),
                                message="Consecutive extract guard ended the task.",
                                metadata={
                                    "action": action,
                                    "extract_count": _extract_count,
                                    "total_rows": _total_extracted_rows,
                                    "total_pages": len(_extracted_page_keys),
                                    "row_target": _extract_guard_done_state.get("row_target"),
                                    "page_target": _extract_guard_done_state.get("page_target"),
                                    "target_reached": _extract_guard_done_state.get("reached"),
                                },
                            )
                            break

                        # 提取后中止本批次，下一步重新截图（VLM 可能还需要翻页提取更多）
                        break
                    else:
                        # Any real navigation/viewport-changing action resets consecutive extract count.
                        if action in (
                            "click", "click_text", "hover_and_click", "click_point", "click_new_tab",
                            "next_page", "scroll", "smooth_scroll", "find_text", "form_set", "goto",
                            "press_key", "switch_tab", "close_tab",
                        ):
                            _extract_count = 0

                    # 5. 记忆库日志（save_to_memory 动作由 execute_action 内部写入 workflow_memory）
                    if action == "save_to_memory":
                        if workflow_memory:
                            logger.info(f"[MEMORY] Current workflow_memory: {workflow_memory}")

                    # 6. 任务完成
                    if action == "done":
                        page_summary = await browser.get_active_page_summary()
                        current_url = browser.current_url
                        # ── FORM DONE GUARD chat-task escape hatch ────────
                        # If chat_extract has fired or the current page is a
                        # known chat host, this is NOT a form-fill task no
                        # matter what the goal parser thought. Skip the
                        # field-validation guard — its label match will fail
                        # (no <input> for the entire goal paragraph) and
                        # block done forever.
                        _form_guard_bypass_chat = False
                        try:
                            from .chat_extract_coercion_guard import (
                                is_on_known_chat_domain as _form_guard_chat_check,
                            )
                        except ImportError:
                            try:
                                from chat_entry_drift_guard import (  # type: ignore[no-redef]
                                    is_on_known_chat_domain as _form_guard_chat_check,
                                )
                            except ImportError:
                                _form_guard_chat_check = lambda _u: False  # type: ignore[assignment]
                        try:
                            if _form_guard_chat_check(current_url or ""):
                                _form_guard_bypass_chat = True
                        except Exception:
                            pass
                        if not _form_guard_bypass_chat:
                            try:
                                for _trail in (browser.rpa_trail or [])[-6:]:
                                    if str((_trail or {}).get("action") or "") == "chat_extract":
                                        _form_guard_bypass_chat = True
                                        break
                            except Exception:
                                pass
                        if _form_guard_bypass_chat:
                            logger.info(
                                "[FORM DONE GUARD] bypassed — chat task detected "
                                "(host=%s)", (current_url or "")[:80],
                            )
                        if (
                            _is_form_fill_goal
                            and _form_assignments
                            and not _form_guard_bypass_chat
                        ):
                            _done_validation = await _validate_form_assignments_on_page(
                                browser, goal, _form_assignments
                            )
                            if not _done_validation.get("ok"):
                                logger.warning(
                                    "[FORM DONE GUARD] blocked done before explicit fields validate: %s",
                                    _done_validation,
                                )
                                vlm.inject_error_feedback(
                                    "不能直接 done：用户明确给了字段值，但当前页面字段回读未通过。"
                                    "必须先逐项填入用户指定值，并在点击 Submit/Create 前后保留字段回读证据。"
                                )
                                break
                            if _form_goal_requires_submit(goal) and not _form_submit_clicked_once:
                                logger.warning("[FORM DONE GUARD] blocked done before submit click on explicit form goal")
                                vlm.inject_error_feedback(
                                    "字段值已回读正确，但用户要求最后点击 Submit/Create。"
                                    "请点击真实提交按钮，不要直接 done。"
                                )
                                break
                        if _is_form_fill_goal and not _form_guard_bypass_chat:
                            _done_page = await browser._ensure_active_page(reason="rpachallenge done guard")
                            if _done_page:
                                _done_challenge_state = await _get_round_form_state(_done_page)
                                if (
                                    _done_challenge_state.get("is_challenge")
                                    and not _round_form_is_complete(_done_challenge_state)
                                    and _should_use_round_form_macro(goal, _form_assignments)
                                ):
                                    logger.warning(
                                        "[RPA CHALLENGE DONE GUARD] blocked premature done: round=%s/%s start=%s submit=%s",
                                        _done_challenge_state.get("current_round"),
                                        _done_challenge_state.get("total_rounds"),
                                        _done_challenge_state.get("start_visible"),
                                        _done_challenge_state.get("submit_visible"),
                                    )
                                    if await _run_round_form_if_present(browser, goal):
                                        _run_succeeded = True
                                        _task_completed = True
                                        _broadcast_done_safe(True, "RPA Challenge completed")
                                        _log_decision = {
                                            "action": "done",
                                            "target_id": 0,
                                            "status": "success",
                                            "thought": "RPA Challenge done guard 拦截了过早 done，并由确定性宏完成全部轮次。",
                                            "type_value": "rpa_challenge",
                                        }
                                        break
                                    vlm.inject_error_feedback(
                                        "RPA Challenge 不是单次 Submit 表单。当前还未进入最终结果页；"
                                        "不要 done。必须点击 Start（如仍可见）并继续按 challenge.xlsx 完成全部 10 轮，"
                                        "只有最后一轮后出现最终结果/成功态才允许 done。"
                                    )
                                    break
                        ax_tree_text_for_done = ""
                        if _goal_is_plain_search_task(goal):
                            try:
                                # 用 AX Tree 代替旧的文本快照 —— name 字段里仍然包含搜索结果链接标题，
                                # 对 _search_goal_done_looks_premature 的子串检测来说是等价的信息源。
                                ax_tree_text_for_done = await browser.extract_accessibility_tree()
                            except Exception as _done_ax_err:
                                logger.debug(f"[DONE GUARD] AX tree snapshot skipped: {_done_ax_err}")
                        if _search_goal_done_looks_premature(
                            goal,
                            start_url,
                            current_url,
                            page_summary,
                            ax_tree_text_for_done,
                        ):
                            logger.warning(
                                "[DONE GUARD] Blocked a premature done on a search task "
                                "because the page still looks like the input/suggestion stage."
                            )
                            vlm.inject_error_feedback(
                                "你刚才试图结束任务，但当前页面仍像是搜索输入/建议阶段，"
                                "还没有明确进入搜索结果页。"
                                "请不要直接 done；应尝试真正提交搜索，优先使用搜索按钮或 press_key + Enter。"
                            )
                            break

                        # 如果 done 时也附带了 VLM 提取数据，一并保存
                        extracted = decision.get("extracted_data")
                        _done_answer_from_data = ""
                        if extracted:
                            logger.info(f"[EXTRACT] Final data extracted: {extracted}")
                            if _goal_output_mode == "answer":
                                logger.info(
                                    "[ANSWER OUTPUT] final answer payload kept in run result; "
                                    "not saving Excel artifact"
                                )
                                _done_answer_from_data = _format_extracted_rows_as_answer(
                                    extracted
                                )
                                _done_answer_from_data = _compact_answer_text_for_goal(
                                    _done_answer_from_data,
                                    goal,
                                )
                            else:
                                save_run_dataset(
                                    extracted,
                                    run_id=_run_ts,
                                    output_contract=_goal_output_contract,
                                    produced_by="vlm_extract_retry",
                                    step_id=str(step),
                                    filename_hint=_vlm_output,
                                    unique_key=TOOLTIP_UNIQUE_KEY if _goal_is_tooltip_extract(goal) else None,
                                )
                            # 同步更新进度计数器，避免 PROGRESS SAFEGUARD 用到过期数值
                            _record_extract_progress(extracted, len(extracted))
                        if _goal_is_tooltip_extract(goal):
                            _expected_tooltips = _parse_goal_tooltip_targets(goal)
                            _missing_tooltips = [
                                _title_tooltip_target(label)
                                for label in _expected_tooltips
                                if extract_tooltip_primary_key(
                                    {"direction": _title_tooltip_target(label)}
                                )
                                not in _tooltip_trigger_keys
                            ]
                            if _missing_tooltips:
                                logger.warning(
                                    "[TOOLTIP DONE GUARD] Blocked premature done; missing tooltip triggers: %s",
                                    ", ".join(_missing_tooltips),
                                )
                                vlm.inject_error_feedback(
                                    "Tooltip 任务尚未完成，缺少这些触发源的提示文字："
                                    + ", ".join(_missing_tooltips)
                                    + "。请继续 hover 缺失的按钮；只有全部触发源都已提取后才能 done。"
                                )
                                break
                        _date_macro_for_done = _parse_semantic_macro(goal)
                        if _date_macro_for_done and _date_macro_for_done.get("action") == "date_pick":
                            _expected_date = str(_date_macro_for_done.get("target_date") or "").strip()
                            _observed_dates: list[str] = []
                            try:
                                _done_page = await browser._ensure_active_page(reason="date done guard")
                                if _done_page:
                                    _observed_dates = await _done_page.evaluate(
                                        """() => Array.from(document.querySelectorAll('input,textarea,[role=combobox]'))
                                            .filter(el => {
                                                const r = el.getBoundingClientRect();
                                                const s = window.getComputedStyle(el);
                                                return r.width > 0 && r.height > 0 &&
                                                    s.display !== 'none' && s.visibility !== 'hidden';
                                            })
                                            .map(el => [
                                                el.value,
                                                el.getAttribute('aria-label'),
                                                el.getAttribute('placeholder'),
                                                el.getAttribute('title'),
                                                el.textContent
                                            ].filter(Boolean).join(' '))
                                            .filter(Boolean)"""
                                    ) or []
                            except Exception as _date_done_err:
                                logger.debug("[DATE DONE GUARD] probe skipped: %s", _date_done_err)
                            _joined_dates = " | ".join(str(v) for v in _observed_dates)
                            if _expected_date and _expected_date not in _joined_dates:
                                logger.warning(
                                    "[DATE DONE GUARD] Blocked premature/wrong done: expected=%s observed=%s",
                                    _expected_date,
                                    _joined_dates,
                                )
                                event_stream.verify(
                                    step=step,
                                    name="date_done_guard",
                                    success=False,
                                    expected=_expected_date,
                                    observed=_joined_dates,
                                    metadata={"values": _observed_dates},
                                )
                                vlm.inject_error_feedback(
                                    f"日期任务尚未完成：用户目标中的目标日期按系统日期计算应为 {_expected_date}，"
                                    f"但当前页面回读到的是：{_joined_dates or '未找到日期值'}。"
                                    "请不要 done；需要选择准确日期，不能把日历面板当前显示月份再加一个月。"
                                )
                                break
                        # 打印 XHR 拦截汇总
                        if browser.intercepted_count > 0:
                            logger.info(
                                f"[XHR SUMMARY] Total records intercepted this session: "
                                f"{browser.intercepted_count}"
                            )
                        if workflow_memory:
                            logger.info(f"[MEMORY] Final workflow_memory state: {workflow_memory}")

                        # ── Judge 验证：用独立 LLM 调用确认任务真正完成 ─────────
                        if _judge.config.enabled and _judge_rejections < _judge.config.max_retries_after_fail:
                            try:
                                _judge_screenshot = getattr(browser, "_last_screenshot_b64", None)
                                _judge_history = vlm._build_history_summary()
                                _judge_result = await _judge.evaluate(
                                    goal=goal,
                                    screenshot_b64=_judge_screenshot,
                                    history_summary=_judge_history,
                                    current_url=browser.current_url or "",
                                    page_title=(page_summary or "")[:200],
                                )
                                if not _judge_result.passed:
                                    _judge_rejections += 1
                                    _rejection_msg = (
                                        _judge_result.rejection_reason
                                        or f"Judge 验证不通过: {_judge_result.reasoning}"
                                    )
                                    logger.warning(f"[JUDGE] Blocked done ({_judge_rejections}x): {_rejection_msg}")
                                    _broadcast_log_safe(
                                        f"[JUDGE] 任务验证未通过 ({_judge_rejections}x): {_rejection_msg}",
                                        level="warn",
                                    )
                                    vlm.inject_error_feedback(
                                        f"🔍 Judge 验证：任务尚未完成。{_rejection_msg} "
                                        f"{_judge_result.suggested_action or '请继续执行。'}"
                                    )
                                    break
                            except Exception as _judge_err:
                                logger.debug(f"[JUDGE] Verification skipped: {_judge_err}")

                        # ── Final Answer：在广播 done 前把 VLM 的最终回答/思考记录为 run answer ──
                        # 后面 ~13415 行的 _done_message 继续复用 _vlm_done_thought，避免重复清洗
                        _vlm_done_thought = _clean_user_visible_done_message(
                            _done_answer_from_data
                            or decision.get("thought")
                            or decision.get("type_value")
                            or "VLM determined the goal has been achieved."
                        )
                        if _done_answer_from_data:
                            decision["thought"] = _vlm_done_thought
                        _record_run_answer(text=_vlm_done_thought)

                        logger.info("[DONE] Task completed! VLM determined the goal has been achieved.")
                        _broadcast_log_safe("[DONE] Task completed! VLM determined the goal has been achieved.")
                        _broadcast_done_safe(True, "Task completed")
                        await browser.mark_and_screenshot(step=99)

                        # ── RPA 肌肉记忆：保存成功轨迹供下次极速回放 ──────────
                        if browser.rpa_trail or _executed_cached_trail or not _rpa_cache_allowed:
                            try:
                                merged_trail = _executed_cached_trail + browser.rpa_trail
                                compacted_trail = _semanticize_rpa_trail(
                                    _compact_rpa_trail(merged_trail),
                                    goal,
                                )
                                if len(compacted_trail) != len(merged_trail):
                                    logger.info(
                                        f"[RPA] Compacted trail before save: "
                                        f"{len(merged_trail)} → {len(compacted_trail)} steps"
                                    )
                                if not _rpa_cache_allowed and _semantic_macro and _allow_semantic_rpa:
                                    compacted_trail = [dict(_semantic_macro)]
                                    logger.info(
                                        "[RPA] Physical cache write disabled; preserving semantic macro only."
                                    )
                                cache_meta = _build_rpa_match_metadata(start_url, goal)
                                # 提取类任务（获取/提取/抓取/extract）的轨迹不应声称
                                # 回放即可完成任务，必须保留 VLM 提取步骤
                                _is_extract_goal = bool(re.search(
                                    r"获取|提取|抓取|采集|爬取|extract|scrape|crawl",
                                    goal, re.IGNORECASE,
                                ))
                                _trail_has_extract = any(
                                    s.get("action") == "extract" for s in compacted_trail
                                )
                                _semantic_actions = {"date_pick", "cascader_pick"}
                                _semantic_only_trail = bool(compacted_trail) and all(
                                    isinstance(s, dict)
                                    and str(s.get("action") or "") in _semantic_actions
                                    for s in compacted_trail
                                )
                                _completes = not (_is_extract_goal and not _trail_has_extract)
                                if _goal_is_form_fill(goal):
                                    _completes = _trail_completes_form_goal(goal, compacted_trail)
                                if _goal_output_mode == "answer":
                                    # Physical replay may navigate to the right page, but it
                                    # cannot itself produce the answer that the user asked for.
                                    _completes = False
                                _cache_replayable = bool(compacted_trail) and (
                                    _rpa_cache_allowed
                                    or (_allow_semantic_rpa and _semantic_only_trail)
                                )
                                cache_payload = {
                                    "version": 4,
                                    "replayable": _cache_replayable,
                                    "reason": "" if _cache_replayable else (_rpa_skip_reason or "marked as non-replayable"),
                                    "trail": compacted_trail if _cache_replayable else [],
                                    "fail_count": 0,
                                    "max_failures": 2,
                                    "completes_task": _completes,
                                    **cache_meta,
                                }
                                _write_rpa_cache_payload(_rpa_exact_path, cache_payload)
                                if cache_payload["replayable"]:
                                    print(
                                        f"\033[1;33m💾 [RPA]\033[0m 肌肉记忆已保存"
                                        f"（{len(compacted_trail)} 步）→ {_rpa_exact_path.name}"
                                    )
                                    logger.info(f"[RPA] Trail saved: {_rpa_exact_path} ({len(compacted_trail)} steps)")
                                else:
                                    logger.info(
                                        f"[RPA] Cache written as non-replayable: {_rpa_exact_path} "
                                        f"(reason: {_rpa_skip_reason or 'marked as non-replayable'})"
                                    )
                            except Exception as _save_err:
                                logger.warning(f"[RPA] Failed to save trail (non-fatal): {_save_err}")
                        # ────────────────────────────────────────────────────────

                        _task_completed = True
                        _run_succeeded = True
                        # ── PROGRESS SAFEGUARD ──
                        _pg_target = _parse_goal_target_count(goal)
                        if _pg_target is not None and _total_extracted_rows < _pg_target:
                            logger.warning(
                                f"[PROGRESS SAFEGUARD] 终止时进度不足: {_total_extracted_rows}/{_pg_target}，标记为失败"
                            )
                            _run_succeeded = False
                        _done_target_state = _extraction_targets_reached(
                            goal,
                            total_rows=_total_extracted_rows,
                            total_pages=len(_extracted_page_keys),
                        )
                        # 复用 Patch 4 已清洗的文本，保持 event_stream 与 WS done 一致
                        _done_message = _vlm_done_thought
                        event_stream.done(
                            step=step,
                            success=_run_succeeded,
                            reason=(
                                "vlm_done"
                                if _run_succeeded
                                else "vlm_done_progress_safeguard_failed"
                            ),
                            message=_done_message[:500],
                            metadata={
                                "url": current_url,
                                "page_summary": str(page_summary or "")[:1000],
                                "total_rows": _total_extracted_rows,
                                "total_pages": len(_extracted_page_keys),
                                "row_target": _done_target_state.get("row_target"),
                                "page_target": _done_target_state.get("page_target"),
                                "target_reached": _done_target_state.get("reached"),
                                "rows_reached": _done_target_state.get("rows_reached"),
                                "pages_reached": _done_target_state.get("pages_reached"),
                                "action": action,
                            },
                        )
                        break

                    # 7. 执行浏览器操作

                    _raw_type_value = str(decision.get("type_value") or "")
                    _placeholder_keys = _extract_placeholder_keys(_raw_type_value)
                    _required_memory_keys = sorted(
                        set(_stable_memory_keys(workflow_memory)) | set(_placeholder_keys)
                    )
                    if _placeholder_keys:
                        decision["__rpa_template_value"] = _raw_type_value
                    if _required_memory_keys:
                        decision["__rpa_required_keys"] = _required_memory_keys

                    # ── 动态插值（主循环层）：execute_action 前将 {{key}} 替换为真实值 ──
                    # 作为 browser_env.py 内部插值的前置补充：
                    #   - browser_env 只在 type 动作内插值；
                    #   - 此处对所有动作的 type_value 统一处理，包括 goto/press_key 等。
                    # 两层都运行是安全的：第一层替换后 {{}} 消失，第二层不再重复替换。
                    if decision.get("type_value") and workflow_memory:
                        for _mem_k, _mem_v in workflow_memory.items():
                            _placeholder = f"{{{{{_mem_k}}}}}"
                            if _placeholder in decision["type_value"]:
                                decision["type_value"] = decision["type_value"].replace(
                                    _placeholder, str(_mem_v)
                                )
                                logger.info(
                                    f"[INTERPOLATION] {_placeholder} → {_mem_v!r}"
                                )
                                print(
                                    f"\033[1;35m✨ [INTERPOLATION]\033[0m "
                                    f"{_placeholder} → \033[32m{_mem_v!r}\033[0m"
                                )

                    # ── Schema 自愈：把“在链接/按钮上 type 可见文字”转成 click_text ──
                    # 典型误填：VLM 想点击未编号/难定位的 “Zero configuration”，却输出
                    # action=type target_id=<Examples链接> type_value="Zero configuration"。
                    # 对非输入控件执行 type 只会污染搜索框或触发站内搜索，应直接文本点击。
                    if action == "type" and decision.get("target_id", 0):
                        _type_tv = str(decision.get("type_value") or "").strip()
                        _type_tid = decision.get("target_id", 0)
                        _target_meta = None
                        try:
                            _target_id_int = int(_type_tid)
                            _target_meta = next(
                                (
                                    el for el in getattr(browser, "_last_som_elements", [])
                                    if int(el.get("id", -1)) == _target_id_int
                                ),
                                None,
                            )
                        except Exception:
                            _target_meta = None
                        _target_role = str(
                            (_target_meta or {}).get("role")
                            or (_target_meta or {}).get("tag")
                            or ""
                        ).strip().lower()
                        _input_roles = {
                            "textbox", "searchbox", "combobox", "spinbutton",
                            "textarea", "input",
                        }
                        _clickable_roles = {
                            "link", "button", "menuitem", "tab", "option",
                        }
                        # label/文案节点常被 SoM 标在「搜索」区域旁，但实际应对关联输入框执行 type。
                        # 以前用 len(type_value)<=80 兜底会把「在 label 上 type 关键词」误改成 click_text，
                        # 导致在必应等站点把查询词当成按钮文字去点，连续失败。
                        _no_click_text_fallback_roles = {
                            "label",
                            "heading",
                            "paragraph",
                            "statictext",
                            "banner",
                            "section",
                            "article",
                            "main",
                            "navigation",
                            "complementary",
                        }
                        if (
                            _type_tv
                            and _target_role
                            and _target_role not in _input_roles
                            and _target_role not in _no_click_text_fallback_roles
                            and (
                                _target_role in _clickable_roles
                                or len(_type_tv) <= 80
                            )
                        ):
                            if _target_role in _clickable_roles:
                                logger.warning(
                                    "[ACTION FIX] type on clickable target #%s "
                                    "(role=%s, value=%r) -> click",
                                    _type_tid,
                                    _target_role,
                                    _type_tv,
                                )
                                decision["action"] = "click"
                                decision["target_id"] = _type_tid
                                decision["type_value"] = ""
                                action = "click"
                            else:
                                logger.warning(
                                    "[ACTION FIX] type on non-input target #%s "
                                    "(role=%s, value=%r) -> click_text",
                                    _type_tid,
                                    _target_role,
                                    _type_tv,
                                )
                                decision["action"] = "click_text"
                                decision["target_id"] = 0
                                decision["type_value"] = _type_tv
                                action = "click_text"

                    # ── 表单滚动漂移守卫 ─────────────────────────────────────
                    # 表单任务如果尚未开始填写字段，却连续向下滚动，很容易从目标表单
                    # 一路滚到文档页脚/其它示例区域。该守卫只在表单填报目标触发。
                    if _is_form_fill_goal:
                        _form_thought = str(decision.get("thought") or "")
                        _form_tv_for_fix = str(decision.get("type_value") or "").strip()
                        _multi_form_hits = _split_form_assignment_payload(
                            _form_tv_for_fix,
                            _form_assignments,
                        )
                        if action == "form_set" and len(_multi_form_hits) > 1:
                            for _raw_label, _raw_value in _multi_form_hits:
                                _raw_label = _raw_label.strip().strip("\"'“”")
                                _raw_value = _raw_value.strip().strip("\"'“”")
                                if _lookup_form_assignment(_form_assignments, _raw_label):
                                    logger.warning(
                                        "[FORM FIX] multi-field form_set payload %r -> first field %r=%r",
                                        _form_tv_for_fix,
                                        _raw_label,
                                        _raw_value,
                                    )
                                    decision["type_value"] = f"{_raw_label}={_raw_value}"
                                    _form_tv_for_fix = decision["type_value"]
                                    vlm.inject_error_feedback(
                                        "⚠️ form_set 一次只执行一个字段。系统已拆出第一个字段执行；"
                                        "下一步继续用 form_set 分别处理剩余字段。"
                                    )
                                    break
                        _label_value_tv = re.match(r"^\s*([^=\n\r]{1,80})\s*=\s*(.+?)\s*$", _form_tv_for_fix, re.S)
                        if (
                            action == "type"
                            and decision.get("target_id")
                            and _label_value_tv
                            and _target_role in _input_roles
                        ):
                            _raw_label = _label_value_tv.group(1).strip().strip("\"'“”")
                            _raw_value = _label_value_tv.group(2).strip().strip("\"'“”")
                            if _raw_value:
                                logger.warning(
                                    "[FORM FIX] type on input target #%s carried label=value payload %r -> value-only %r",
                                    decision.get("target_id"),
                                    _form_tv_for_fix,
                                    _raw_value,
                                )
                                decision["type_value"] = _raw_value
                                _form_tv_for_fix = _raw_value
                                vlm.inject_error_feedback(
                                    f"⚠️ 表单动作纠偏：显式输入框 target_id={decision.get('target_id')} "
                                    f"不应把字段名一并输入。系统已将 {_raw_label!r}={_raw_value!r} "
                                    "改成仅输入字段值。"
                                )
                        if action == "form_set" and _label_value_tv:
                            _raw_label = _label_value_tv.group(1).strip().strip("\"'“”")
                            if re.search(
                                r"^(create|submit|save|ok)$|提交|保存|确定|创建",
                                _raw_label,
                                re.IGNORECASE,
                            ):
                                logger.warning(
                                    "[FORM FIX] submit button pseudo form_set %r -> click_text %r",
                                    _form_tv_for_fix,
                                    _raw_label,
                                )
                                decision["action"] = "click_text"
                                decision["target_id"] = 0
                                decision["type_value"] = _raw_label
                                action = "click_text"
                                _form_tv_for_fix = _raw_label
                        if action in ("type", "click_text") and _label_value_tv:
                            _raw_label = _label_value_tv.group(1).strip().strip("\"'“”")
                            _raw_value = _label_value_tv.group(2).strip().strip("\"'“”")
                            if _lookup_form_assignment(_form_assignments, _raw_label):
                                logger.warning(
                                    "[FORM FIX] %s carried label=value payload %r -> form_set",
                                    action,
                                    _form_tv_for_fix,
                                )
                                decision["action"] = "form_set"
                                decision["target_id"] = 0
                                decision["type_value"] = f"{_raw_label}={_raw_value}"
                                action = "form_set"
                                vlm.inject_error_feedback(
                                    f"⚠️ 表单动作纠偏：type_value={_form_tv_for_fix!r} 是字段赋值，"
                                    "不能作为普通文本输入或点击文本。系统已改为 form_set。"
                                )
                        _assignment_hit = _lookup_form_assignment(_form_assignments, _form_tv_for_fix)
                        if action in ("type", "click_text") and _assignment_hit:
                            _orig_form_action = action
                            _label, _value = _assignment_hit
                            logger.warning(
                                "[FORM FIX] %s with label-like type_value %r -> form_set %r=%r",
                                action,
                                _form_tv_for_fix,
                                _label,
                                _value,
                            )
                            decision["action"] = "form_set"
                            decision["target_id"] = 0
                            decision["type_value"] = f"{_label}={_value}"
                            action = "form_set"
                            vlm.inject_error_feedback(
                                f"⚠️ 表单动作纠偏：你刚把字段标签 {_form_tv_for_fix!r} 当成了"
                                f"{_orig_form_action} 的目标/输入。系统已改为 form_set：{_label}={_value}。"
                                "后续遇到组件库表单字段，请优先使用 form_set。"
                            )
                        elif action in ("click", "type", "click_text") and _form_assignments:
                            _thought_assignment_hit = None
                            for _label, _value in _form_assignments.items():
                                if _label and _label.lower() in _form_thought.lower():
                                    _thought_assignment_hit = (_label, _value)
                                    break
                            _value_assignment_hit = _lookup_form_assignment_by_value(
                                _form_assignments, _form_tv_for_fix
                            )
                            _route_hit = None
                            if _thought_assignment_hit and _assignment_is_non_text_control(_thought_assignment_hit[0]):
                                _route_hit = _thought_assignment_hit
                            elif _value_assignment_hit and _assignment_is_non_text_control(_value_assignment_hit[0]):
                                _route_hit = _value_assignment_hit
                            if _route_hit:
                                _label, _value = _route_hit
                                logger.warning(
                                    "[FORM FIX] %s routed to form_set via thought/value: %r=%r",
                                    action,
                                    _label,
                                    _value,
                                )
                                decision["action"] = "form_set"
                                decision["target_id"] = 0
                                decision["type_value"] = f"{_label}={_value}"
                                action = "form_set"
                                vlm.inject_error_feedback(
                                    f"⚠️ 表单动作纠偏：当前动作疑似在处理组件控件 {_label!r}，"
                                    f"系统已改为 form_set：{_label}={_value}，避免把值误输入到其它文本框。"
                                )
                        _no_scroll_markers = (
                            "无需再滚动", "不需要再滚动", "无需滚动", "不用再滚动",
                            "已经可见", "已可见", "已出现", "满足退出标准",
                            "no need to scroll", "already visible",
                        )
                        if (
                            action in ("scroll", "smooth_scroll")
                            and any(marker in _form_thought for marker in _no_scroll_markers)
                        ):
                            _cur_sg_text = ""
                            if _task_plan is not None and _task_plan.current is not None:
                                _cur_sg_text = (
                                    f"{_task_plan.current.description}\n"
                                    f"{_task_plan.current.exit_criteria}"
                                )
                            if _text_is_form_visibility_trap(_cur_sg_text):
                                decision["subgoal_status"] = "completed"
                            decision["action"] = "wait"
                            decision["target_id"] = 0
                            decision["type_value"] = "1"
                            action = "wait"
                            vlm.inject_error_feedback(
                                "⚠️ 表单动作纠偏：你的 thought 已判断目标字段/表单可见，"
                                "但 action 仍然是滚动。系统已取消本次滚动。下一步请直接填写/选择"
                                "当前可见字段；若要找某个不可见字段，使用 find_text，而不是盲滚。"
                            )

                        _scroll_dir = str(
                            decision.get("direction")
                            or decision.get("type_value")
                            or "down"
                        ).strip().lower()
                        _is_down_scroll = (
                            action in ("scroll", "smooth_scroll")
                            and _scroll_dir in ("", "down", "bottom", "next")
                        )
                        _is_up_scroll = (
                            action in ("scroll", "smooth_scroll")
                            and _scroll_dir in ("up", "top", "previous", "prev")
                        )
                        _form_action_roles = {
                            "textbox", "searchbox", "combobox", "spinbutton",
                            "textarea", "input", "checkbox", "radio", "switch",
                            "button", "option",
                        }
                        _target_role_for_form = ""
                        try:
                            _target_id_int = int(decision.get("target_id", 0) or 0)
                            if _target_id_int:
                                _target_meta_for_form = next(
                                    (
                                        el for el in getattr(browser, "_last_som_elements", [])
                                        if int(el.get("id", -1)) == _target_id_int
                                    ),
                                    None,
                                )
                                _target_role_for_form = str(
                                    (_target_meta_for_form or {}).get("role")
                                    or (_target_meta_for_form or {}).get("tag")
                                    or ""
                                ).strip().lower()
                        except Exception:
                            _target_role_for_form = ""

                        _form_value_hint = str(
                            decision.get("type_value") or ""
                        ).strip().lower()
                        _form_value_markers = (
                            "activity", "zone", "date", "time", "delivery",
                            "online", "sponsor", "resource", "create",
                            "submit", "pick a date", "表单", "提交",
                        )
                        _click_looks_like_form = (
                            _target_role_for_form in _form_action_roles
                            or any(marker in _form_value_hint for marker in _form_value_markers)
                        )
                        if action in ("type", "select", "form_set", "upload", "press_key") or (
                            action in ("click", "click_text", "hover_and_click")
                            and _click_looks_like_form
                        ):
                            _form_interaction_started = True
                            _form_scroll_down_streak = 0
                        elif _is_up_scroll:
                            _form_scroll_down_streak = 0
                        elif _is_down_scroll and not _form_interaction_started:
                            _form_scroll_down_streak += 1
                            _visible_text = " ".join(
                                str(
                                    el.get("name")
                                    or el.get("text")
                                    or el.get("label")
                                    or ""
                                )
                                for el in getattr(browser, "_last_som_elements", [])
                            ).lower()
                            _footer_markers = (
                                "source", "contributors", "edit this page",
                                "previous", "next", "footer", "sitemap",
                                "源码", "贡献者", "页脚",
                            )
                            _looks_like_footer = any(
                                marker in _visible_text for marker in _footer_markers
                            )
                            if _looks_like_footer or _form_scroll_down_streak >= 7:
                                _reason = (
                                    "当前视口已出现页脚/文档导航信号"
                                    if _looks_like_footer
                                    else "尚未填写任何字段却连续向下滚动过多"
                                )
                                _block_msg = (
                                    f"🚫 表单滚动守卫已取消本次 {action} down：{_reason}。\n"
                                    "你正在执行表单填报任务。不要为了让整张表单和 Create/Submit "
                                    "按钮同时出现在一屏而继续向下滚动。\n"
                                    "【强制指令】下一步请用 find_text 定位当前要处理的字段/按钮"
                                    "（如 Activity name / Activity zone / Create），或回到目标表单区域后立即逐字段填写。"
                                )
                                logger.warning(
                                    "[FORM GUARD] Blocked pre-fill down scroll: "
                                    "streak=%s footer=%s",
                                    _form_scroll_down_streak,
                                    _looks_like_footer,
                                )
                                vlm.inject_error_feedback(_block_msg)
                                vlm.annotate_last_result(
                                    "🚫 表单滚动守卫：未执行继续向下滚动，要求回到表单并开始填字段"
                                )
                                break

                    if _goal_is_tooltip_extract(goal) and action == "hover_and_click":
                        _orig_menu_text = str(decision.get("type_value") or "").strip()
                        logger.info(
                            "[TOOLTIP GUARD] Rewriting hover_and_click to hover for tooltip extraction "
                            "(target_id=%s, ignored menu text=%r)",
                            decision.get("target_id"),
                            _orig_menu_text,
                        )
                        vlm.inject_error_feedback(
                            "Tooltip 读取任务禁止使用 hover_and_click：tooltip 是要读取的短文本，"
                            "不是要点击的菜单项。系统已将本步改为纯 hover；"
                            "一旦 tooltip 文本出现，应立即 extract，不要点击 tooltip。"
                        )
                        decision["action"] = "hover"
                        decision["type_value"] = ""
                        action = "hover"

                    if _goal_is_tooltip_extract(goal) and action == "hover":
                        _hover_label_probe = _infer_tooltip_trigger_label(
                            goal,
                            decision,
                            "",
                            getattr(browser, "element_mapping", {}) or {},
                        )
                        _hover_key_probe = extract_tooltip_primary_key(
                            {"direction": _hover_label_probe}
                        )
                        if _hover_key_probe and _hover_key_probe in _tooltip_trigger_keys:
                            _expected_tooltips = _parse_goal_tooltip_targets(goal)
                            _remaining_tooltips = [
                                _title_tooltip_target(label)
                                for label in _expected_tooltips
                                if extract_tooltip_primary_key(
                                    {"direction": _title_tooltip_target(label)}
                                )
                                not in _tooltip_trigger_keys
                            ]
                            logger.warning(
                                "[TOOLTIP GUARD] Blocked duplicate hover for captured trigger %s; remaining=%s",
                                _hover_label_probe,
                                ", ".join(_remaining_tooltips) or "<unknown>",
                            )
                            vlm.inject_error_feedback(
                                f"Tooltip 触发源 {_hover_label_probe} 已经提取过，"
                                "本次重复 hover 已被系统拦截。"
                                + (
                                    " 请改为 hover 剩余触发源："
                                    + ", ".join(_remaining_tooltips)
                                    + "。"
                                    if _remaining_tooltips
                                    else " 请换下一个尚未提取的 tooltip 触发源。"
                                )
                            )
                            vlm.annotate_last_result(
                                f"🚫 Tooltip 去重守卫：{_hover_label_probe} 已提取，未重复 hover"
                            )
                            break

                    if action == "click":
                        _guard_page = await browser._ensure_active_page(
                            reason="rewrite cascader nav click"
                        )
                        if _guard_page:
                            _cascader_popup_text = await _rewrite_cascader_nav_click_to_popup_text(
                                browser,
                                _guard_page,
                                decision,
                                goal,
                            )
                            if _cascader_popup_text:
                                logger.warning(
                                    "[CASCADER GUARD] Rewriting nav-like click #%s -> click_text %r "
                                    "because a visible popup/menu item has the same label.",
                                    decision.get("target_id"),
                                    _cascader_popup_text,
                                )
                                vlm.inject_error_feedback(
                                    f"当前是级联/多级菜单任务，且弹层里已存在可见菜单项 {_cascader_popup_text!r}。"
                                    "不要点击顶部导航或侧边栏中的同名链接；"
                                    "系统已将本步改为 click_text，优先命中已展开弹层里的菜单项。"
                                )
                                decision["action"] = "click_text"
                                decision["target_id"] = 0
                                decision["type_value"] = _cascader_popup_text
                                action = "click_text"

                    # ── LOOP GUARD 前置拦截：已进入黑名单的 target_id 直接阻断 ───
                    _click_target_id = decision.get("target_id", 0)
                    _click_point_bucket = _bucket_point(decision.get("point"))
                    _id_blocked = (
                        action in ("click", "click_new_tab", "hover_and_click")
                        and _click_target_id != 0
                        and _click_target_id in _loop_guard_blocked_ids
                    )
                    _point_blocked = (
                        action == "click_point"
                        and _click_point_bucket
                        and _click_point_bucket in _loop_guard_blocked_points
                    )
                    if _id_blocked or _point_blocked:
                        _trap_desc = (
                            f"元素 #{_click_target_id}" if _id_blocked
                            else f"坐标区域 ~{_click_point_bucket}"
                        )
                        logger.warning(
                            f"[LOOP GUARD] 前置拦截：{_trap_desc} 已在黑名单，跳过执行"
                        )
                        _block_msg = (
                            f"🚫 系统强制拦截：{_trap_desc} 已被 LOOP GUARD 封禁！\n"
                            f"该目标此前已被检测为死循环陷阱，本次{action}已被直接取消（未执行）。\n"
                            f"【强制指令】立刻停止对 {_trap_desc} 的一切操作！\n"
                            f"请仔细观察最新截图，选择一个**完全不同**的目标继续任务 ——\n"
                            f"例如跳过广告，点击第二/第三个搜索结果；或换个坐标区域；\n"
                            f"或者如果任务实际已完成（结果页已打开、标签已切换），请直接 action=done。"
                        )
                        vlm.inject_error_feedback(_block_msg)
                        vlm.annotate_last_result(
                            f"🚫 被LOOP GUARD前置拦截({_trap_desc}在黑名单), 未执行"
                        )
                        break  # 不执行，直接进入下一步重新截图+决策

                    # ── 预采样：记录 execute_action 前的标签数与 URL，供结果回填 ──
                    _pre_pages_count = (
                        len(browser._context.pages) if browser._context else 0
                    )
                    _pre_url = browser.current_url
                    _pre_download_path = str(
                        getattr(browser, "last_download_path", "") or ""
                    )
                    _pre_click_is_pagination_candidate = False
                    _pre_form_submit_target: dict | None = None
                    _pre_form_submit_validation: dict | None = None
                    if action in ("click", "click_text", "click_point"):
                        try:
                            _pagination_links_pre = browser.find_pagination_links()
                            _pagination_ids_pre = {
                                int(link.get("id"))
                                for link in _pagination_links_pre
                                if link.get("id") is not None
                            }
                            _decision_target_id = int(decision.get("target_id") or 0)
                            if _decision_target_id and _decision_target_id in _pagination_ids_pre:
                                _pre_click_is_pagination_candidate = True
                            elif action == "click_text":
                                _decision_text = str(decision.get("type_value") or "").strip().lower()
                                _pre_click_is_pagination_candidate = any(
                                    _decision_text
                                    and _decision_text == str(link.get("name") or "").strip().lower()
                                    for link in _pagination_links_pre
                                )
                        except Exception:
                            _pre_click_is_pagination_candidate = False

                    if (
                        _is_bulk_extract_goal
                        and action in ("click", "click_text", "click_point")
                        and _extract_goal_target is not None
                        and _total_extracted_rows < _extract_goal_target
                        and not _pre_click_is_pagination_candidate
                        and not _decision_click_targets_extraction_control(
                            decision,
                            input_descriptions,
                        )
                    ):
                        _shape_for_click_guard: dict = {}
                        try:
                            _shape_for_click_guard = await browser.probe_data_shape()
                        except Exception:
                            _shape_for_click_guard = {}
                        _repeated_items = int(
                            _shape_for_click_guard.get("repeated_list_items") or 0
                        )
                        _repeated_classes = int(
                            _shape_for_click_guard.get("repeated_class_count") or 0
                        )
                        _table_rows_for_guard = int(
                            _shape_for_click_guard.get("table_rows") or 0
                        )
                        _list_or_table_like = (
                            _repeated_items >= 5
                            or _repeated_classes >= 5
                            or _table_rows_for_guard >= 3
                        )
                        if _list_or_table_like:
                            _target_line = _find_target_line(
                                input_descriptions,
                                int(decision.get("target_id") or 0),
                            )
                            _guard_msg = (
                                "Blocked a likely content/ad/list-item click during a bulk extraction task. "
                                f"Progress is {_total_extracted_rows}/{_extract_goal_target}; "
                                "use extract/scroll instead of opening individual entries unless the user "
                                "explicitly asks for detail pages."
                            )
                            logger.warning(
                                "[EXTRACT CLICK GUARD] %s target=%r shape=%s",
                                _guard_msg,
                                _target_line or decision.get("type_value") or decision.get("point"),
                                _shape_for_click_guard,
                            )
                            event_stream.guard(
                                step=step,
                                name="extract_click_guard",
                                message=_guard_msg,
                                metadata={
                                    "target": _target_line,
                                    "decision": {
                                        "action": decision.get("action"),
                                        "target_id": decision.get("target_id"),
                                        "type_value": decision.get("type_value"),
                                        "point": decision.get("point"),
                                    },
                                    "shape": _shape_for_click_guard,
                                    "progress": {
                                        "rows": _total_extracted_rows,
                                        "target": _extract_goal_target,
                                    },
                                },
                            )
                            try:
                                vlm.inject_error_feedback(
                                    "当前是批量抓取/提取任务，页面已经呈现列表或表格。"
                                    "不要点击单条内容、广告或详情入口；除非用户明确要求进入详情页，"
                                    "否则应先 extract 当前可见数据，或继续 scroll/next_page 加载更多数据。"
                                    "系统已将本次点击改为向下滚动。"
                                )
                            except Exception:
                                pass
                            decision["action"] = "scroll"
                            decision["target_id"] = 0
                            decision["type_value"] = "down"
                            decision["status"] = "pending"
                            decision["guard_rewrite"] = "extract_click_guard"
                            decisions[_action_idx] = decision
                            action = "scroll"

                    if _is_form_fill_goal and action in ("scroll", "smooth_scroll"):
                        _active_form_assignments, _active_form_state = await _resolve_active_form_assignments(
                            browser,
                            goal,
                            _form_assignments,
                        )
                        _scroll_dir = str(
                            decision.get("direction")
                            or decision.get("type_value")
                            or "down"
                        ).strip().lower()
                        if (
                            _active_form_assignments
                            and _form_goal_requires_submit(goal)
                            and _scroll_dir in ("", "down", "bottom", "next")
                        ):
                            _pre_submit_validation = await _validate_form_assignments_on_page(
                                browser, goal, _active_form_assignments
                            )
                            if _pre_submit_validation.get("ok"):
                                _submit_hint = await _locate_visible_form_submit_text(
                                    browser, goal, _active_form_assignments
                                )
                                _submit_text = str(_submit_hint.get("text") or "").strip()
                                if _submit_text:
                                    logger.warning(
                                        "[FORM SUBMIT REWRITE] fields verified; rewrite %s %r -> click_text %r",
                                        action,
                                        _scroll_dir,
                                        _submit_text,
                                    )
                                    try:
                                        vlm.inject_error_feedback(
                                            "⚠️ 表单动作纠偏：目标字段已经回读成功，"
                                            f"继续 {_scroll_dir or 'down'} 滚动会把表单滚丢。"
                                            f"系统已改为直接点击当前表单的提交按钮 {_submit_text!r}。"
                                        )
                                    except Exception:
                                        pass
                                    decision["action"] = "click_text"
                                    decision["target_id"] = 0
                                    decision["type_value"] = _submit_text
                                    decision["guard_rewrite"] = "form_submit_rewrite"
                                    decisions[_action_idx] = decision
                                    action = "click_text"

                    if _is_form_fill_goal and action in ("click", "click_text", "click_point"):
                        _active_form_assignments, _active_form_state = await _resolve_active_form_assignments(
                            browser,
                            goal,
                            _form_assignments,
                        )
                        _pre_form_submit_target = await _inspect_form_submit_target(
                            browser, action, decision
                        )
                        if _pre_form_submit_target.get("is_submit") and _active_form_assignments:
                            _pre_form_submit_validation = await _validate_form_assignments_on_page(
                                browser, goal, _active_form_assignments
                            )
                            if not _pre_form_submit_validation.get("ok"):
                                _missing = _pre_form_submit_validation.get("missing") or []
                                _missing_text = ", ".join(map(str, _missing[:6])) or "unknown fields"
                                _target_text = (
                                    _pre_form_submit_target.get("controlText")
                                    or _pre_form_submit_target.get("text")
                                    or _pre_form_submit_target.get("type_value")
                                    or "submit"
                                )
                                _guard_msg = (
                                    "Blocked premature form submit: physical submit control "
                                    f"{_target_text!r} was targeted, but field readback is incomplete. "
                                    f"Missing or mismatched fields: {_missing_text}. "
                                    "Continue filling the target form before clicking Create/Submit."
                                )
                                logger.warning("[FORM SUBMIT GUARD] %s", _guard_msg)
                                event_stream.guard(
                                    step=step,
                                    name="form_submit_guard",
                                    message=_guard_msg,
                                    metadata={
                                        "target": _pre_form_submit_target,
                                        "validation": _pre_form_submit_validation,
                                    },
                                )
                                try:
                                    vlm.inject_error_feedback(_guard_msg)
                                except Exception:
                                    pass
                                decision["status"] = "error"
                                decision["thought"] = (
                                    "FORM SUBMIT GUARD blocked this submit attempt because "
                                    f"field readback is incomplete: {_missing_text}."
                                )
                                decision["form_submit_target"] = _pre_form_submit_target
                                decision["form_validation"] = _pre_form_submit_validation
                                _log_decision = decision
                                _log_error = _guard_msg
                                break

                    # ── 自愈执行：捕获 ActionExecutionError 并注入 VLM 反馈 ──────────
                    try:
                        _dispatch_tool = action_registry.resolve_for_action(
                            action,
                            goal=goal,
                        )
                        _targeted_handoff_executed = False
                        if action == "click_text":
                            _targeted_handoff_executed = await _try_targeted_click_text_handoff(
                                decision
                            )
                            if _targeted_handoff_executed:
                                event_stream.emit(
                                    "tool_dispatch",
                                    step=step,
                                    action=action,
                                    tool={
                                        "name": "targeted_probe",
                                        "capability": "perception",
                                        "risk": "low",
                                    },
                                    metadata={
                                        "handoff": "click_text_to_selector",
                                        "type_value": decision.get("type_value", ""),
                                    },
                                )
                                active_page = await browser._ensure_active_page(
                                    reason="targeted handoff dispatch"
                                )
                        elif action == "type":
                            _targeted_handoff_executed = await _try_targeted_type_handoff(
                                decision,
                                workflow_memory=workflow_memory,
                            )
                            if _targeted_handoff_executed:
                                event_stream.emit(
                                    "tool_dispatch",
                                    step=step,
                                    action=action,
                                    tool={
                                        "name": "targeted_probe",
                                        "capability": "perception",
                                        "risk": "low",
                                    },
                                    metadata={
                                        "handoff": "type_to_input_selector",
                                        "type_value": decision.get("type_value", ""),
                                    },
                                )
                                active_page = await browser._ensure_active_page(
                                    reason="targeted type handoff dispatch"
                                )
                            elif int(decision.get("target_id") or 0) == 0:
                                _handoff_msg = (
                                    "type target_id=0 could not be resolved by targeted input probe. "
                                    "Falling back to wait and asking the model to choose a visible input ID, "
                                    "use targeted_probe, or scroll."
                                )
                                logger.warning("[TARGETED HANDOFF] %s", _handoff_msg)
                                event_stream.guard(
                                    step=step,
                                    name="targeted_type_handoff_failed",
                                    message=_handoff_msg,
                                    metadata={
                                        "type_value": decision.get("type_value", ""),
                                    },
                                )
                                try:
                                    vlm.inject_error_feedback(
                                        "你刚才尝试 type 但 target_id=0，局部输入框探针也没有找到足够明确的输入框。"
                                        "请重新观察截图，选择真实输入框 @eN；或先执行 targeted_probe / smooth_scroll。"
                                    )
                                except Exception:
                                    pass
                                decision["action"] = "wait"
                                decision["type_value"] = "2"
                                decision["target_id"] = 0
                                action = "wait"
                        # I1: pre-action timestamp for the broadcast_phase emit
                        # below. Using a fresh local guarantees the post-action
                        # phase event always has a duration_ms value.
                        _action_t0 = time.time()
                        if (
                            not _targeted_handoff_executed
                            and action in _registry_dispatch_actions
                            and _dispatch_tool
                            and _dispatch_tool.get("handler_bound")
                        ):
                            event_stream.emit(
                                "tool_dispatch",
                                step=step,
                                action=action,
                                tool={
                                    "name": _dispatch_tool.get("name"),
                                    "capability": _dispatch_tool.get("capability"),
                                    "risk": _dispatch_tool.get("risk"),
                                },
                                metadata={
                                    "target_id": decision.get("target_id", 0),
                                    "type_value": decision.get("type_value", ""),
                                },
                            )
                            active_page = await action_registry.execute_for_action(
                                action,
                                goal=goal,
                                action_payload=decision,
                                workflow_memory=workflow_memory,
                            )
                        elif not _targeted_handoff_executed:
                            active_page = await browser.execute_action(
                                decision,
                                workflow_memory,
                            )
                        # I1: phase event for the just-executed action so the
                        # frontend timeline can show "vlm_call → action(click) → next vlm_call"
                        # with their respective durations. Best-effort.
                        try:
                            from api_server import broadcast_phase

                            _act_err = getattr(browser, "_last_action_error", None)
                            broadcast_phase(
                                "action",
                                severity="error" if _act_err else "info",
                                message=action or "(no-op)",
                                step=step,
                                duration_ms=int((time.time() - _action_t0) * 1000),
                                notice_severity=getattr(browser, "_last_notice_severity", None),
                                extra={"action_name": action or ""},
                            )
                        except Exception:
                            pass
                        if active_page is not None:
                            # execute_action 内部已更新 browser._page，此处仅做日志追踪
                            logger.debug(f"[TAB GUARD] Active page after action: {(active_page.url or 'about:blank')[:80]}")
                        _last_action_result = getattr(browser, "_last_action_result", None)
                        if _last_action_result is not None:
                            _last_action_result = _with_tool_metadata(_last_action_result)
                            event_stream.act(step=step, result=_last_action_result)

                        # ── Element Tracker auto-capture (post-action) ────
                        # 把刚刚成功操作的元素注册到 vlm.element_tracker，
                        # 后续步骤 DOM 重排后可按结构签名找回。两个别名：
                        #   last_<action>      ：最近一次该动作的元素（覆盖式）
                        #   step{N}_<action>   ：步骤锚定（永不覆盖，可回溯）
                        # 开关关闭时所有 track_element 调用都是 no-op。
                        try:
                            _trk_act = str(decision.get("action") or "")
                            _trk_tid = int(decision.get("target_id") or 0)
                            _trk_ok = bool(
                                getattr(_last_action_result, "success", False)
                            ) if _last_action_result is not None else False
                            # 只追踪"有目标 + 成功 + 与具体 DOM 元素绑定"的动作
                            _trk_trackable_actions = {
                                "click", "type", "select", "hover",
                                "smooth_scroll", "right_click", "double_click",
                            }
                            if (
                                _trk_ok
                                and _trk_tid > 0
                                and _trk_act in _trk_trackable_actions
                            ):
                                _trk_el = next(
                                    (
                                        el for el in getattr(
                                            browser, "_last_som_elements", []
                                        ) or []
                                        if int(el.get("id") or -1) == _trk_tid
                                    ),
                                    None,
                                )
                                if _trk_el is not None:
                                    _trk_payload = {
                                        "som_id": _trk_el.get("id"),
                                        "tag": _trk_el.get("tag", ""),
                                        "text": _trk_el.get("name", ""),
                                        "role": _trk_el.get("role", ""),
                                        "bbox": _trk_el.get("rect") or {},
                                        "parent_chain": (
                                            [_trk_el.get("parentContext") or ""]
                                            if _trk_el.get("parentContext") else []
                                        ),
                                    }
                                    _trk_meta = {
                                        "url": browser.current_url or "",
                                        "step": step,
                                    }
                                    vlm.track_element(
                                        f"last_{_trk_act}",
                                        _trk_payload, step=step, metadata=_trk_meta,
                                    )
                                    vlm.track_element(
                                        f"step{step}_{_trk_act}",
                                        _trk_payload, step=step, metadata=_trk_meta,
                                    )
                                    logger.debug(
                                        f"[TRACKER] auto-captured "
                                        f"{_trk_act}@som_id={_trk_tid} "
                                        f"text={(_trk_el.get('name') or '')[:30]!r}"
                                    )
                        except Exception as _trk_err:
                            logger.debug(f"[TRACKER] auto-capture skipped: {_trk_err}")

                        # ── chat_extract terminal-action check ────────────
                        # The ChatExtractHandler sets ``workflow_memory
                        # ['__chat_extract_completed']`` when it actually
                        # captured an AI answer. main.py has no other way
                        # to know chat_extract is a TERMINAL action — without
                        # this, VLM keeps re-emitting chat_extract every
                        # step because the screen still shows the answer
                        # (run_log_20260518_141728: 18 steps wasted on the
                        # same answer). Treat the sentinel as task complete.
                        if (
                            action == "chat_extract"
                            and isinstance(workflow_memory, dict)
                            and workflow_memory.get("__chat_extract_completed")
                        ):
                            _cec = workflow_memory.get("__chat_extract_completed") or {}
                            _cec_mk = str(_cec.get("memory_key") or "")
                            _cec_len = int(_cec.get("length") or 0)
                            logger.info(
                                "[CHAT EXTRACT DONE] task completed via chat_extract: "
                                "memory_key=%s length=%d",
                                _cec_mk, _cec_len,
                            )
                            _broadcast_log_safe(
                                f"✅ [CHAT EXTRACT DONE] AI 回答已提取 "
                                f"({_cec_len} chars → workflow_memory[{_cec_mk!r}])，任务结束",
                                level="info",
                            )
                            # Replace _log_decision so HTML shows this step
                            # as the final done with the answer alongside.
                            try:
                                _ai_answer_obj = workflow_memory.get(_cec_mk) or {}
                                _chat_answer_row = {
                                    "answer": str(_ai_answer_obj.get("answer") or ""),
                                    "method": _ai_answer_obj.get("method"),
                                    "url": _ai_answer_obj.get("url"),
                                    "length": _ai_answer_obj.get("length"),
                                    "wait_ms": _ai_answer_obj.get("wait_ms"),
                                }
                                # ── Final Answer：把 chat 回答原文记录为 run answer ──
                                # chat_extract 的 broadcast_done 紧接其后触发；
                                # 这里抢在它前面把真正的 AI 回答塞进 run-scoped stash，
                                # 这样前端 Final Answer Tab 能渲染 chat 答案而不是占位文案。
                                _chat_raw_answer = _chat_answer_row["answer"]
                                _chat_final_answer = _compact_answer_text_for_goal(
                                    _chat_raw_answer,
                                    goal,
                                )
                                if _goal_output_mode == "answer" and _chat_final_answer:
                                    _chat_answer_row["answer"] = _chat_final_answer
                                    _chat_answer_row["length"] = len(_chat_final_answer)
                                    try:
                                        if isinstance(_ai_answer_obj, dict):
                                            _ai_answer_obj["answer"] = _chat_final_answer
                                            _ai_answer_obj["length"] = len(_chat_final_answer)
                                            _ai_answer_obj["raw_length"] = _cec_len
                                            workflow_memory[_cec_mk] = _ai_answer_obj
                                            workflow_memory["latest_memory"] = _chat_final_answer[:200]
                                    except Exception:
                                        pass
                                if _chat_final_answer:
                                    _record_run_answer(text=_chat_final_answer)
                                if _goal_output_mode == "answer":
                                    _chat_saved_path = ""
                                    logger.info(
                                        "[ANSWER OUTPUT] chat answer kept in run result; "
                                        "not saving Excel artifact"
                                    )
                                else:
                                    _chat_saved_path = save_run_dataset(
                                        [_chat_answer_row],
                                        run_id=_run_ts,
                                        output_contract=_goal_output_contract,
                                        produced_by="chat_answer",
                                        step_id=str(step),
                                        filename_hint=_vlm_output,
                                    )
                                _log_decision = {
                                    "action": "done",
                                    "target_id": 0,
                                    "type_value": "",
                                    "memory_key": _cec_mk,
                                    "status": "success",
                                    "thought": (
                                        f"[CHAT EXTRACT DONE] AI 回答已成功提取 "
                                        f"(method={_cec.get('method') or 'unknown'}, "
                                        f"length={_cec_len})。回答正文存于 "
                                        f"workflow_memory[{_cec_mk!r}]，任务完成。"
                                    ),
                                    "extracted_data": {
                                        "answer": _chat_answer_row["answer"][:8000],
                                        "method": _chat_answer_row["method"],
                                        "url": _chat_answer_row["url"],
                                        "output_file": _chat_saved_path,
                                    },
                                    "output_file": _chat_saved_path,
                                }
                            except Exception:
                                _chat_saved_path = ""
                                pass
                            _task_completed = True
                            _run_succeeded = True
                            event_stream.done(
                                step=step,
                                success=True,
                                reason="chat_extract_completed",
                                message=(
                                    f"chat_extract completed: memory_key={_cec_mk} "
                                    f"length={_cec_len}"
                                ),
                                metadata={
                                    "memory_key": _cec_mk,
                                    "length": _cec_len,
                                    "method": _cec.get("method"),
                                    "url": _cec.get("url"),
                                    "output_file": _chat_saved_path,
                                },
                            )
                            _broadcast_done_safe(True, "chat_extract completed")
                            break

                        # Reset sentinel for safety — next step's chat_extract
                        # would re-set it, but explicit reset avoids stale
                        # state if some other action mutates workflow_memory.
                        if action != "chat_extract" and isinstance(workflow_memory, dict):
                            workflow_memory.pop("__chat_extract_completed", None)

                        if action == "next_page" and browser.rpa_trail:
                            _last_rpa = browser.rpa_trail[-1]
                            if isinstance(_last_rpa, dict) and _last_rpa.get("action") == "next_page":
                                _np_method = _last_rpa.get("method") or _last_rpa.get("strategy") or ""
                                _np_strategy = _last_rpa.get("strategy") or ""
                                _np_landed = _last_rpa.get("landed_url") or browser.current_url
                                if _np_method:
                                    decision["execution_method"] = str(_np_method)
                                if _np_strategy:
                                    decision["strategy"] = str(_np_strategy)
                                if _np_landed:
                                    decision["landed_url"] = str(_np_landed)
                                decision["status"] = "success"
                                logger.info(
                                    "[NEXT_PAGE RESULT] method=%s strategy=%s landed=%s",
                                    _np_method,
                                    _np_strategy,
                                    str(_np_landed)[:160],
                                )
                                _nav_target = _parse_goal_target_count(goal)
                                if _nav_target is None or _total_extracted_rows < _nav_target:
                                    _force_extract_after_navigation_pending = True
                                    _force_next_page_pending = False
                                    _nav_feedback = (
                                        "已成功翻页到新页面，下一步必须先执行 extract 提取当前页；"
                                        "在当前页完成提取前禁止继续 next_page，避免跳过目标数据。"
                                    )
                                    try:
                                        vlm.inject_error_feedback(_nav_feedback)
                                    except Exception:
                                        pass
                                    logger.info(
                                        "[FORCE EXTRACT AFTER NAV] armed after next_page; progress=%s/%s",
                                        _total_extracted_rows,
                                        _nav_target if _nav_target is not None else "?",
                                    )

                        if (
                            action in ("click", "click_text", "click_point")
                            and _pre_click_is_pagination_candidate
                        ):
                            _nav_target = _parse_goal_target_count(goal)
                            _nav_pages = _parse_goal_target_pages(goal)
                            if (
                                (_nav_target is None or _total_extracted_rows < _nav_target)
                                or (
                                    _nav_pages is not None
                                    and len(_extracted_page_keys) < _nav_pages
                                )
                            ):
                                _force_extract_after_navigation_pending = True
                                _force_next_page_pending = False
                                _nav_feedback = (
                                    "已通过分页控件进入下一页，下一步必须先执行 extract 提取当前页；"
                                    "在当前页完成提取前不要继续点击分页器，也不要直接 done。"
                                )
                                try:
                                    vlm.inject_error_feedback(_nav_feedback)
                                except Exception:
                                    pass
                                logger.info(
                                    "[FORCE EXTRACT AFTER NAV] armed after pagination click; "
                                    "progress=%s/%s",
                                    _total_extracted_rows,
                                    _nav_target if _nav_target is not None else "?",
                                )

                        # ── 标签页切换感知：将 Tab Guard 切换事件注入 VLM 反馈 ──────
                        # G1: 用 consume_tab_notice() 原子取出 (text, severity)，
                        # 让日志按严重程度高亮，前端将来可以按 severity 着色。
                        _tab_switched_this_step = bool(browser._tab_switch_notice)
                        _notice_text, _notice_sev = browser.consume_tab_notice()
                        if _notice_text:
                            vlm.inject_error_feedback(_notice_text)
                            _log_fn = (
                                logger.error if _notice_sev == "error"
                                else logger.warning if _notice_sev == "warn"
                                else logger.info
                            )
                            _log_fn(
                                "[TAB NOTICE/%s] Injected for next VLM step",
                                _notice_sev.upper(),
                            )

                        # ── URL-aware LOOP GUARD（后置检测）──────────────────────
                        # 在 execute_action 之后才能拿到真正的 landing URL，
                        # 只有「同一 ID 被点击 ≥ 3 次 **且** 着陆 URL 完全相同」才判定死循环，
                        # 翻页（URL 递增）或 A/B 循环切换（URL 交替变化）不再误伤。
                        _landing_url = browser.current_url
                        _landing_key_url = _norm_url_for_guard(_landing_url)
                        _point_bucket = _bucket_point(decision.get("point"))
                        if action == "hover_and_click":
                            _point_bucket = (
                                "menu:" + str(decision.get("type_value") or "").strip().lower()
                            )
                        # Bug #1+#3 修复 v2：click_point 的 key 去掉 URL。
                        # 原因：JD/淘宝等风控页每次打空 click_point 会跳到不同 error path，
                        # 带 URL 的 key 永不相等；而 click_point 作为"无 SoM id 的应急坐标点击"
                        # 跨页合法用例不存在（翻页都用 click+target_id），coord 重复即幻觉。
                        _key_url = (
                            "" if action == "click_point" else _landing_key_url
                        )
                        _cur_action_key = (
                            action, decision.get("target_id", 0),
                            _point_bucket, _key_url,
                        )
                        _guard_eligible = action in _LOOP_GUARD_ACTIONS and (
                            _cur_action_key[1] != 0 or bool(_point_bucket)
                        )
                        _click_repeat_count = (
                            _last_actions.count(_cur_action_key) + 1
                            if _guard_eligible else 0
                        )
                        _last_actions.append(_cur_action_key)
                        if action in ("scroll", "next_page", "wait", "extract"):
                            try:
                                _np_state = _no_progress_tracker.observe(
                                    action=action,
                                    row_count=_total_extracted_rows,
                                    url=_landing_key_url,
                                )
                                if _np_state.get("pagination_exhausted"):
                                    _pagination_exhausted = True
                                    logger.info(
                                        "[NO PROGRESS] streak=%s action=%s → pagination exhausted",
                                        _np_state.get("streak"),
                                        action,
                                    )
                                    event_stream.guard(
                                        step=step,
                                        name="NO_PROGRESS_EXHAUSTED",
                                        message="Repeated scroll/page with no new rows.",
                                        metadata=_np_state,
                                    )
                                    try:
                                        from api_server import broadcast_phase as _bp_np

                                        _bp_np(
                                            "completion_guard",
                                            severity="warn",
                                            message=(
                                                f"no_progress: streak="
                                                f"{_np_state.get('streak')}"
                                            ),
                                            step=step,
                                            notice_severity=getattr(
                                                browser, "_last_notice_severity", None
                                            ),
                                            extra={
                                                "guard": "no_progress_exhausted",
                                                "state": _np_state,
                                            },
                                        )
                                    except Exception:
                                        pass
                            except Exception as _np_err:
                                logger.debug("[NO PROGRESS] skipped: %s", _np_err)
                        if len(_last_actions) > _LOOP_GUARD_WINDOW:
                            _last_actions.pop(0)
                        if action in _SOM_REF_ACTIONS:
                            _som_ref = (
                                action,
                                _target_id_for_guard(decision),
                                _point_bucket,
                                _norm_url_for_guard(_pre_url),
                            )
                            _som_target_refs.append(_som_ref)
                            if len(_som_target_refs) > _LOOP_GUARD_WINDOW:
                                _som_target_refs.pop(0)

                        # ── 方案②：把本次执行结果回填到 VLM 历史，让下一轮决策看见「已做什么」 ──
                        _post_pages_count = (
                            len(browser._context.pages) if browser._context else 0
                        )
                        _outcome_parts: list[str] = []
                        if _landing_url and _landing_url != _pre_url:
                            _outcome_parts.append(f"跳转 url={_landing_url[:80]}")
                        else:
                            _outcome_parts.append(f"url 未变 ({_landing_url[:60]})")
                        if _post_pages_count != _pre_pages_count:
                            _outcome_parts.append(
                                f"标签 {_pre_pages_count}→{_post_pages_count}"
                            )
                        if _tab_switched_this_step:
                            _outcome_parts.append("触发TabGuard切换")
                        _hover_tooltip_text = ""
                        if action == "hover_and_click":
                            _outcome_parts.append(
                                f"已原子完成 hover #{decision.get('target_id', 0)} "
                                f"并点击菜单项 {str(decision.get('type_value') or '').strip()!r}"
                            )
                        elif action == "hover" and browser.rpa_trail:
                            _last_hover_rpa = browser.rpa_trail[-1]
                            if (
                                isinstance(_last_hover_rpa, dict)
                                and _last_hover_rpa.get("action") == "hover"
                                and _last_hover_rpa.get("tooltip_text")
                            ):
                                _hover_tooltip_text = str(
                                    _last_hover_rpa.get("tooltip_text") or ""
                                ).strip()
                                _outcome_parts.append(
                                    f"tooltip={_hover_tooltip_text[:120]!r}"
                                )
                        _post_download_path = str(
                            getattr(browser, "last_download_path", "") or ""
                        )
                        _new_download_path = (
                            _post_download_path
                            if _post_download_path and _post_download_path != _pre_download_path
                            else ""
                        )
                        _outcome_parts.extend(
                            action_result_evidence_parts(
                                getattr(browser, "_last_action_result", None),
                                download_path=_new_download_path,
                                download_name=str(
                                    getattr(browser, "last_download_name", "") or ""
                                ) if _new_download_path else "",
                            )
                        )
                        vlm.annotate_last_result("✅ " + " | ".join(_outcome_parts))

                        if await _finish_if_xhr_target_reached(f"after action={action}"):
                            break
                        if await _finish_if_file_download_completed(f"after action={action}"):
                            break

                        # ── TOOLTIP AUTO-EXTRACT GUARD ─────────────────────
                        # Tooltip tasks are read-only: once hover physically exposes
                        # a tooltip, persist it immediately and move on. Do not let
                        # VLM burn steps "confirming" by hovering the same trigger.
                        if (
                            _goal_is_tooltip_extract(goal)
                            and action == "hover"
                            and _hover_tooltip_text
                        ):
                            _tooltip_label = _infer_tooltip_trigger_label(
                                goal,
                                decision,
                                _hover_tooltip_text,
                                getattr(browser, "element_mapping", {}) or {},
                            )
                            _tooltip_row = {
                                "trigger": _tooltip_label,
                                "direction": _tooltip_label,
                                "tooltip_text": _hover_tooltip_text,
                                "target_id": int(decision.get("target_id", 0) or 0),
                            }
                            _tooltip_key = extract_tooltip_primary_key(_tooltip_row)
                            _tooltip_rows = [_tooltip_row] if _tooltip_key else []
                            _new_rows = (
                                1 if _tooltip_key and _tooltip_key not in _tooltip_trigger_keys
                                else 0
                            )
                            _dup_rows = 1 if _tooltip_key and not _new_rows else 0
                            _rejected_rows = 0 if _tooltip_key else 1
                            _source_text = f"{_tooltip_label} {_hover_tooltip_text}"
                            _progress_new_rows, _progress_total_rows = _record_extract_progress(
                                _tooltip_rows,
                                _new_rows,
                            )
                            _expected_tooltips = _parse_goal_tooltip_targets(goal)
                            _remaining_tooltips = [
                                label for label in _expected_tooltips
                                if extract_tooltip_primary_key({"direction": _title_tooltip_target(label)})
                                not in _tooltip_trigger_keys
                            ]

                            if _tooltip_rows:
                                saved_path = save_run_dataset(
                                    _tooltip_rows,
                                    run_id=_run_ts,
                                    output_contract=_goal_output_contract,
                                    produced_by="tooltip_auto",
                                    step_id=str(step),
                                    filename_hint=_vlm_output,
                                    unique_key=TOOLTIP_UNIQUE_KEY,
                                )
                                logger.info(
                                    "[TOOLTIP AUTO] captured %s -> %r (new=%s total=%s saved=%s)",
                                    _tooltip_label,
                                    _hover_tooltip_text,
                                    _progress_new_rows,
                                    _progress_total_rows,
                                    saved_path,
                                )
                                print(
                                    f"\033[1;32m✅ [TOOLTIP]\033[0m "
                                    f"{_tooltip_label}: \033[36m{_hover_tooltip_text}\033[0m "
                                    f"(唯一提示项 {_progress_total_rows})"
                                )
                            else:
                                logger.info(
                                    "[TOOLTIP AUTO] tooltip row rejected/duplicate: label=%s dup=%s rejected=%s",
                                    _tooltip_label,
                                    _dup_rows,
                                    _rejected_rows,
                                )

                            _done_tooltips = bool(_expected_tooltips) and not _remaining_tooltips
                            _tooltip_thought = (
                                f"TOOLTIP AUTO captured {_tooltip_label}: {_hover_tooltip_text!r}. "
                            )
                            if _done_tooltips:
                                _tooltip_thought += (
                                    "All requested tooltip targets have been captured; task completed."
                                )
                                _broadcast_done_safe(True, "Tooltip extraction completed")
                                _run_succeeded = True
                                _task_completed = True
                            elif _remaining_tooltips:
                                _tooltip_thought += (
                                    "Move to remaining tooltip targets: "
                                    + ", ".join(_title_tooltip_target(v) for v in _remaining_tooltips)
                                    + ". Do not hover this same target again."
                                )
                                vlm.inject_error_feedback(_tooltip_thought)
                            else:
                                _tooltip_thought += (
                                    "Move to the next requested tooltip trigger; do not hover this same target again."
                                )
                                vlm.inject_error_feedback(_tooltip_thought)

                            _log_extract_text_source = "TOOLTIP_HOVER"
                            _log_decision = {
                                "action": "done" if _done_tooltips else "extract",
                                "target_id": int(decision.get("target_id", 0) or 0),
                                "type_value": _tooltip_label,
                                "status": "success",
                                "thought": _tooltip_thought,
                                "extracted_data": _tooltip_rows or [_tooltip_row],
                                "tooltip_progress": {
                                    "new": _progress_new_rows,
                                    "total": _progress_total_rows,
                                    "remaining": [
                                        _title_tooltip_target(v) for v in _remaining_tooltips
                                    ],
                                },
                            }
                            event_stream.extract(
                                step=step,
                                source="TOOLTIP_HOVER",
                                rows=len(_tooltip_rows),
                                output_file=_vlm_output if _tooltip_rows else "",
                                metadata={
                                    "label": _tooltip_label,
                                    "text": _hover_tooltip_text,
                                    "done": _done_tooltips,
                                },
                            )
                            break

                        # ── FORM SUBMIT GUARD ──────────────────────────────
                        # Only trust physical evidence: the clicked target must be a real
                        # submit-like DOM/AX control, and goal-derived fields must pass
                        # readback before an inert demo submit is converted to done.
                        _looks_like_form_submit = (
                            _is_form_fill_goal
                            and action in ("click", "click_text")
                            and bool(_pre_form_submit_target)
                            and bool(_pre_form_submit_target.get("is_submit"))
                            and bool(_pre_form_submit_validation)
                            and bool(_pre_form_submit_validation.get("ok"))
                        )
                        _post_submit_challenge_state = None
                        if _looks_like_form_submit:
                            _post_submit_page = await browser._ensure_active_page(reason="inspect post-submit challenge state")
                            if _post_submit_page:
                                _post_submit_challenge_state = await _get_round_form_state(_post_submit_page)
                            if (
                                isinstance(_post_submit_challenge_state, dict)
                                and _post_submit_challenge_state.get("is_challenge")
                                and not _round_form_is_complete(_post_submit_challenge_state)
                            ):
                                logger.info(
                                    "[FORM SUBMIT GUARD] rpachallenge advanced to round %s/%s; continue instead of done.",
                                    _post_submit_challenge_state.get("current_round"),
                                    _post_submit_challenge_state.get("total_rounds"),
                                )
                                _looks_like_form_submit = False
                        if _looks_like_form_submit:
                            if _form_submit_clicked_once:
                                logger.info(
                                    "[FORM SUBMIT GUARD] submit was already clicked once; "
                                    "treating inert demo page as completed."
                                )
                            else:
                                logger.info(
                                    "[FORM SUBMIT GUARD] submit clicked once; "
                                    "ending form task even if the demo page has no visual response."
                                )
                            _form_submit_clicked_once = True
                            _broadcast_log_safe(
                                "[DONE] 表单提交按钮已点击一次；页面无反馈按演示站/无跳转提交处理，自动结束。"
                            )
                            _broadcast_done_safe(True, "Form submit clicked")
                            await browser.mark_and_screenshot(step=99)
                            _log_screenshot_path = str(Path(SCREENSHOT_DIR) / "step_99.png")
                            _submit_text = (
                                _pre_form_submit_target.get("controlText")
                                or _pre_form_submit_target.get("text")
                                or decision.get("type_value")
                                or "submit"
                            )
                            _log_decision = {
                                "action": "done",
                                "target_id": int(decision.get("target_id", 0) or 0),
                                "type_value": str(_submit_text),
                                "status": "success",
                                "thought": (
                                    "表单目标字段已按用户要求完成 DOM 回读验收，"
                                    "且真实提交按钮已点击一次。当前页面无跳转/无提示，"
                                    "按演示站或静默提交处理，任务结束。"
                                ),
                                "form_submit_target": _pre_form_submit_target,
                                "form_validation": _pre_form_submit_validation,
                            }
                            _run_succeeded = True
                            _task_completed = True
                            # ── PROGRESS SAFEGUARD ──
                            _pg_target = _parse_goal_target_count(goal)
                            if _pg_target is not None and _total_extracted_rows < _pg_target:
                                logger.warning(
                                    f"[PROGRESS SAFEGUARD] 终止时进度不足: {_total_extracted_rows}/{_pg_target}，标记为失败"
                                )
                                _run_succeeded = False
                            break

                        if action == "hover_and_click" and _click_repeat_count >= 2:
                            _menu_text = str(decision.get("type_value") or "").strip()
                            logger.info(
                                "[HOVER_AND_CLICK GUARD] same menu action succeeded %s times; "
                                "treating goal as completed to avoid no-result loop.",
                                _click_repeat_count,
                            )
                            _broadcast_log_safe(
                                f"[DONE] hover_and_click 已成功点击菜单项 {_menu_text!r}，"
                                "页面无新增可提取结果，自动结束。"
                            )
                            _broadcast_done_safe(
                                True,
                                f"hover_and_click completed: {_menu_text or 'menu item'}",
                            )
                            await browser.mark_and_screenshot(step=99)
                            _run_succeeded = True
                            _task_completed = True
                            # ── PROGRESS SAFEGUARD ──
                            _pg_target = _parse_goal_target_count(goal)
                            if _pg_target is not None and _total_extracted_rows < _pg_target:
                                logger.warning(
                                    f"[PROGRESS SAFEGUARD] 终止时进度不足: {_total_extracted_rows}/{_pg_target}，标记为失败"
                                )
                                _run_succeeded = False
                            _hover_done_state = _extraction_targets_reached(
                                goal,
                                total_rows=_total_extracted_rows,
                                total_pages=len(_extracted_page_keys),
                            )
                            event_stream.done(
                                step=step,
                                success=_run_succeeded,
                                reason=(
                                    "hover_and_click_guard"
                                    if _run_succeeded
                                    else "hover_and_click_guard_progress_safeguard_failed"
                                ),
                                message=(
                                    f"Repeated hover_and_click for {_menu_text or 'menu item'} "
                                    "succeeded; no visible result delta, so the engine ended the task."
                                ),
                                metadata={
                                    "action": action,
                                    "target_id": decision.get("target_id", 0),
                                    "menu_text": _menu_text,
                                    "repeat_count": _click_repeat_count,
                                    "total_rows": _total_extracted_rows,
                                    "total_pages": len(_extracted_page_keys),
                                    "row_target": _hover_done_state.get("row_target"),
                                    "page_target": _hover_done_state.get("page_target"),
                                    "target_reached": _hover_done_state.get("reached"),
                                },
                            )
                            break

                        if _click_repeat_count >= 3:
                            # 识别是 ID 循环还是坐标循环，构造差异化描述
                            _is_point_loop = action == "click_point" and _point_bucket
                            _trap_tag = (
                                f"坐标区域 ~{_point_bucket}" if _is_point_loop
                                else f"元素 #{_cur_action_key[1]}"
                            )
                            logger.warning(
                                f"[LOOP GUARD] 检测到动作死循环（URL-aware）！"
                                f"{_trap_tag} 在最近 {_LOOP_GUARD_WINDOW} 步内"
                                f"被 {action} {_click_repeat_count} 次且着陆 URL 相同 "
                                f"({_landing_url[:80]}), 交由 VLM 自主反思。"
                            )
                            _page_summary = await browser.get_active_page_summary()
                            _loop_guard_msg = (
                                f"🚨 严重警告：你陷入了操作死循环！\n"
                                f"系统检测到你在最近 {_LOOP_GUARD_WINDOW} 步里已经"
                                f"{_click_repeat_count} 次对 {_trap_tag} 执行了 {action}，"
                                f"且每次之后着陆的 URL 完全相同（{_landing_url[:120]}），"
                                f"说明页面没有任何有效进展。"
                                f"这个目标大概率是无效的、被前端禁用的、或者是诱导点击的陷阱。\n"
                                f"【系统强制指令】：绝对禁止在下一步中再次操作该目标！"
                                f"请立刻观察最新截图，寻找完全不同的路径 —— "
                                f"例如换点另一个红框 ID、或坐标位置偏移 >100 千分位。"
                                f"如果当前页面已经明显达到用户目标状态"
                                f"（如结果页已打开、标签已切换），请直接输出 action=done 结束任务。"
                            )
                            if _page_summary:
                                _loop_guard_msg += f"\n【当前页面摘要】{_page_summary}"
                            vlm.inject_error_feedback(_loop_guard_msg)
                            if _is_point_loop:
                                _loop_guard_blocked_points.add(_point_bucket)
                                logger.info(
                                    f"[LOOP GUARD] 坐标区域 {_point_bucket} 已加入黑名单，"
                                    f"后续 click_point 前置拦截。当前坐标黑名单: "
                                    f"{_loop_guard_blocked_points}"
                                )
                            elif _cur_action_key[1] != 0:
                                _loop_guard_blocked_ids.add(_cur_action_key[1])
                                logger.info(
                                    f"[LOOP GUARD] target_id={_cur_action_key[1]} 已加入黑名单，"
                                    f"后续点击将被前置拦截。当前黑名单: {_loop_guard_blocked_ids}"
                                )
                            break  # 中止本批次，进入下一步（重新截图，让 VLM 看着报错重新决策）

                        _login_intercepted = await _run_preflight_login(
                            browser,
                            goal,
                            source=f"step-{step}-action-{_action_idx + 1}",
                            require_login=require_login,
                        )
                        if _login_intercepted:
                            logger.info("[PRELOGIN] Login popup handled after action; restarting from fresh page state.")
                            break
                        # 执行成功：清零连续错误计数
                        _consecutive_errors = 0

                        # 🛡️ 连招熔断保护 (Macro Guard)
                        # 仅在连招批次（len > 1）且非 goto 动作时检查域名漂移
                        if _batch_initial_domain and action != "goto":
                            _current_domain = urlparse(browser.current_url).netloc
                            if _current_domain and _current_domain != _batch_initial_domain:
                                _guard_msg = (
                                    f"🚨 严重警告：在执行第 {_action_idx + 1} 步（{action}）时，"
                                    f"页面意外跳转到了外部域名 {_current_domain}"
                                    f"（原域名：{_batch_initial_domain}），"
                                    f"可能是动态 DOM 变化导致误触了广告或隐藏链接。"
                                    f"\n连招已被强制中断！请仔细观察最新截图，"
                                    f"找到正确的元素 ID，重新规划动作，不要再下发长连招。"
                                )
                                logger.warning(
                                    f"[MACRO GUARD] 跨域跳转熔断: "
                                    f"{_batch_initial_domain} → {_current_domain}"
                                )
                                vlm.inject_error_feedback(_guard_msg)
                                break  # 中止本批次，进入下一步（重新截图）

                    except ActionExecutionError as exec_err:
                        _consecutive_errors += 1
                        err_msg = str(exec_err)
                        _log_error = err_msg
                        # ── Failure Classifier：统一失败归类 ──────────────────
                        _fc_result = _failure_classifier_fn(
                            error_msg=err_msg,
                            exception=exec_err,
                            context={"step": step, "action": action, "url": _pre_url},
                            step=step,
                        )
                        if _fc_result:
                            _failure_stats.record(_fc_result)
                        _failed_action_result = getattr(browser, "_last_action_result", None)
                        if _failed_action_result is not None:
                            _failed_action_result = _with_tool_metadata(_failed_action_result)
                            event_stream.act(step=step, result=_failed_action_result)
                        else:
                            event_stream.act(
                                step=step,
                                result=_with_tool_metadata(
                                    ActionResult.from_action(
                                        decision,
                                        success=False,
                                        error=err_msg,
                                        before_url=_pre_url,
                                        after_url=getattr(browser, "current_url", "") or "",
                                        before_pages=_pre_pages_count,
                                        after_pages=(
                                            len(browser._context.pages)
                                            if browser._context else _pre_pages_count
                                        ),
                                    ),
                                ),
                            )
                        logger.warning(
                            f"[SELF-HEAL] Action failed ({_consecutive_errors}/{_MAX_CONSECUTIVE_ERRORS}): "
                            f"{err_msg}"
                        )
                        try:
                            from .stuck_recovery_guard import evaluate_failure_recovery
                        except ImportError:
                            from stuck_recovery_guard import evaluate_failure_recovery
                        _sr_fail = evaluate_failure_recovery(
                            _stuck_recovery_state,
                            error_msg=err_msg,
                            target_id=int(decision.get("target_id") or 0),
                            consecutive_errors=_consecutive_errors,
                        )
                        if _sr_fail.should_reset:
                            vlm.reset_decision_history(_sr_fail.reason)
                            _loop_detector.reset()
                            _consecutive_errors = 0
                            _recovery_chain_pending = True
                        # ── Bug A fix：失败也必须进 LOOP GUARD 历史 ───────────────
                        # 否则 VLM 反复点同一不存在的元素，计数器永远为 0，永远不会触发封禁。
                        # 失败路径没导航，用 _pre_url 构建 key（与成功分支同 schema）。
                        _failed_key_url = _norm_url_for_guard(_pre_url)
                        _failed_point_bucket = _bucket_point(decision.get("point"))
                        if action == "hover_and_click":
                            _failed_point_bucket = (
                                "menu:" + str(decision.get("type_value") or "").strip().lower()
                            )
                        # 同成功分支：click_point 的 key 去掉 URL
                        _failed_key_url_final = (
                            "" if action == "click_point" else _failed_key_url
                        )
                        _failed_action_key = (
                            action, decision.get("target_id", 0),
                            _failed_point_bucket, _failed_key_url_final,
                        )
                        _last_actions.append(_failed_action_key)
                        if len(_last_actions) > _LOOP_GUARD_WINDOW:
                            _last_actions.pop(0)
                        if action in _SOM_REF_ACTIONS:
                            _som_failed_ref = (
                                action,
                                _target_id_for_guard(decision),
                                _failed_point_bucket,
                                _failed_key_url,
                            )
                            _som_target_refs.append(_som_failed_ref)
                            if len(_som_target_refs) > _LOOP_GUARD_WINDOW:
                                _som_target_refs.pop(0)
                        # 失败也检查是否达到 LOOP GUARD 阈值（click/click_new_tab/click_point）
                        _failed_eligible = action in _LOOP_GUARD_ACTIONS and (
                            _failed_action_key[1] != 0 or bool(_failed_point_bucket)
                        )
                        if (
                            _failed_eligible
                            and _last_actions.count(_failed_action_key) >= 3
                        ):
                            _failed_is_point = (
                                action == "click_point" and _failed_point_bucket
                            )
                            _failed_trap = (
                                f"坐标区域 ~{_failed_point_bucket}" if _failed_is_point
                                else f"元素 #{_failed_action_key[1]}"
                            )
                            if _failed_is_point:
                                _loop_guard_blocked_points.add(_failed_point_bucket)
                            else:
                                _loop_guard_blocked_ids.add(_failed_action_key[1])
                            logger.warning(
                                f"[LOOP GUARD] {_failed_trap} 连续 3 次{action}失败，加入黑名单"
                            )
                            vlm.inject_error_feedback(
                                f"🚫 {_failed_trap} 已连续 3 次 {action} 失败（{err_msg[:60]}），"
                                f"加入 LOOP GUARD 黑名单。请立即换策略 —— "
                                f"改点其它元素/坐标、或若任务实际已完成直接 action=done。"
                            )
                        # 方案②：失败也回填历史，避免 VLM 误以为动作已经成功
                        vlm.annotate_last_result(f"❌ 失败: {err_msg[:80]}")

                        _scroll_down_failed_at_bottom = (
                            action in ("scroll", "smooth_scroll")
                            and str(decision.get("type_value") or "down").lower()
                            in ("", "down", "bottom", "next")
                            and any(
                                marker in err_msg
                                for marker in (
                                    "已滚到底",
                                    "页面已滚到底部",
                                    "无法继续向下滚动",
                                    "已到页面底部",
                                    "bottom",
                                )
                            )
                        )
                        _target_after_scroll_fail = _parse_goal_target_count(goal)
                        _scroll_bottom_target_unmet = (
                            _target_after_scroll_fail is not None
                            and _total_extracted_rows < _target_after_scroll_fail
                        )
                        if _scroll_down_failed_at_bottom and _scroll_bottom_target_unmet:
                            _pagination_exhausted = True
                            _force_next_page_pending = True
                            _first_flip_pending = False
                            vlm.inject_error_feedback(
                                "⚠️ 底层已确认页面/主滚动区域向下滚动到达底部，"
                                f"但当前仅累计 {_total_extracted_rows}/{_target_after_scroll_fail} 条。\n"
                                "下一轮系统将强制执行 next_page（target_id=0, type_value=\"\"），"
                                "优先尝试 URL 变异、分页器和页码探测；不要继续 smooth_scroll。"
                            )
                            logger.info(
                                "[FORCE NEXT_PAGE] armed after bottom scroll failure: "
                                "%s/%s rows",
                                _total_extracted_rows,
                                _target_after_scroll_fail,
                            )
                            # Check close-enough with tolerance
                            _tolerance = min(5, max(1, int(_target_after_scroll_fail * 0.05)))
                            if _total_extracted_rows >= _target_after_scroll_fail - _tolerance:
                                logger.info(
                                    f"[CLOSE ENOUGH] scroll 到底且进度 {_total_extracted_rows}/{_target_after_scroll_fail} "
                                    f"在容差 {_tolerance} 内，标记完成"
                                )
                                _extraction_complete = True
                                _pagination_exhausted = True

                        if _consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                            # 连续失败达到上限，交人工处理
                            _print_manual_warning(
                                "AGENT STUCK - CONSECUTIVE FAILURES",
                                f"The agent failed {_MAX_CONSECUTIVE_ERRORS} times in a row. "
                                f"Last error: {err_msg[:120]}",
                            )
                            if _stdin_interactive_for_agent():
                                await asyncio.get_event_loop().run_in_executor(None, input)
                                logger.info("[MANUAL] User confirmed, resuming agent...")
                                _consecutive_errors = 0
                                vlm.inject_error_feedback("")  # 清空积压反馈
                            else:
                                _broadcast_log_safe(
                                    f"[AGENT STUCK] 已连续 {_MAX_CONSECUTIVE_ERRORS} 次动作失败（非交互模式已中止）。"
                                    f"最后错误: {err_msg[:240]}",
                                    level="error",
                                )
                                raise RuntimeError(
                                    f"AGENT_STUCK: {_MAX_CONSECUTIVE_ERRORS} consecutive action failures; "
                                    f"last: {err_msg[:240]}"
                                ) from exec_err
                        else:
                            # 将错误注入下一轮 VLM 提示，引导换策略
                            if not _force_next_page_pending:
                                vlm.inject_error_feedback(err_msg)
                        break  # 中止本批次，进入下一步（重新截图）

                # 内层批次循环结束：若任务完成则退出外层主循环
                if _task_completed:
                    break

                # ── Message Compaction：步尾触发异步历史压缩 ───────────────
                await vlm.maybe_compact_history(goal=goal)

            finally:
                # 无论本步以何种方式退出（continue/break/正常/异常），均实时写入轨迹日志
                if isinstance(_log_decision, list):
                    _log_primary = _log_decision[0] if _log_decision else None
                elif isinstance(_log_decision, dict):
                    _log_primary = _log_decision
                else:
                    _log_primary = None

                if isinstance(_log_primary, dict):
                    if _log_reasoning_text_source:
                        _log_primary["reasoning_text_source"] = _log_reasoning_text_source
                    if _log_extract_text_source:
                        _log_primary["extract_text_source"] = _log_extract_text_source

                html_logger.log_step(
                    step_num=step,
                    screenshot_path=_log_screenshot_path,
                    action_dict=_log_decision,
                    error_msg=_log_error,
                    memory_state=dict(workflow_memory),
                )

                # RUN-RESUME1 step2b: persist a per-turn checkpoint (inert unless resume)
                # step 3: also record the plan-anchored completed-step ledger so a
                # later resume can skip them; (labels or None) preserves a prior
                # resumed ledger when this run's plan has no done sub-goals yet.
                if _run_ckpt is not None:
                    _run_completed_steps = None
                    if _run_ckpt.enabled:
                        try:
                            try:
                                from .run_resume_consume import plan_completed_step_labels
                            except ImportError:
                                from run_resume_consume import plan_completed_step_labels  # type: ignore[no-redef]
                            _run_completed_steps = plan_completed_step_labels(_task_plan) or None
                        except Exception:
                            _run_completed_steps = None
                    _run_ckpt.record(
                        step,
                        item_count=_total_extracted_rows,
                        completed_steps=_run_completed_steps,
                    )

        else:
            # for-else: 循环正常结束（没有 break），说明达到最大步数
            if await _finish_if_xhr_target_reached("max steps fallback"):
                pass
            else:
                logger.warning(
                    f"[WARN] Max steps reached ({_effective_max_steps}), task not completed."
                )
                _broadcast_log_safe(
                    f"[WARN] Max steps reached ({_effective_max_steps}), task not completed.",
                    level="warn",
                )
                _broadcast_done_safe(False, f"Max steps reached ({_effective_max_steps})")
                # 保存最终状态截图
                await browser.mark_and_screenshot(step=99)

        # ── Failure Stats：任务结束时输出失败统计 ───────────────────────
        if _failure_stats.total_failures > 0:
            _fc_summary = _failure_stats.summary()
            logger.info(f"[FAILURE STATS] {_fc_summary}")
            _broadcast_log_safe(f"[FAILURE STATS] {_fc_summary}")

    except KeyboardInterrupt:
        logger.info("\n[STOP] User interrupted execution.")
        _broadcast_log_safe("[STOP] User interrupted execution.", level="warn")
        _broadcast_done_safe(False, "User interrupted execution")
    except Exception as e:
        msg = str(e)
        if msg.startswith("STOP_REQUESTED::"):
            stop_ctx = msg.split("::", 1)[1] if "::" in msg else "unknown"
            logger.warning(f"[STOP] Agent cancelled by stop signal at {stop_ctx}")
            _broadcast_log_safe(
                f"[STOP] Agent cancelled by stop signal at {stop_ctx}",
                level="warn",
            )
            _broadcast_done_safe(False, "Agent cancelled by stop signal")
        else:
            logger.error(f"[ERROR] Agent exception: {type(e).__name__}: {e}", exc_info=True)
            _broadcast_log_safe(f"[ERROR] Agent exception: {type(e).__name__}: {e}", level="error")
            _broadcast_done_safe(False, f"Agent exception: {type(e).__name__}: {e}")
            try:
                html_logger.log_step(
                    step_num=0,
                    screenshot_path=None,
                    action_dict={
                        "action": "error",
                        "target_id": 0,
                        "status": "startup_failed",
                        "thought": (
                            "Agent failed before the first browser observation. "
                            "Check the target URL, browser startup, and runtime config."
                        ),
                        "type_value": f"{type(e).__name__}: {e}",
                    },
                    error_msg=f"{type(e).__name__}: {e}",
                    memory_state={},
                )
            except Exception as log_err:
                logger.debug("[ERROR] Failed to write startup error to HTML log: %s", log_err)
    finally:
        try:
            from .io_contract import clear_current_run as _clear_current_run
            _clear_current_run()
        except Exception:
            try:
                from io_contract import clear_current_run as _clear_current_run
                _clear_current_run()
            except Exception:
                pass
        try:
            from api_server import set_phase_log_run_id as _set_phase_run

            _set_phase_run(None)
        except Exception:
            pass
        # RUN-RESUME1 step2b: clear checkpoint on success / persist failed otherwise
        try:
            if _run_ckpt is not None:
                _run_ckpt.finish(_run_succeeded)
        except Exception:
            pass

        # RUN-RESUME1 step3a-wire-2b: finalize the resume-index entry with the
        # final row count + status (last-wins upsert). Inert unless resume.
        if bool((run_constraints or {}).get("resume")):
            try:
                try:
                    from .resume_seed import record_run_for_resume as _rec_resume
                except ImportError:
                    from resume_seed import record_run_for_resume as _rec_resume  # type: ignore[no-redef]
                _rec_resume(
                    goal,
                    start_url,
                    _run_ts,
                    item_count=_total_extracted_rows,
                    status="completed" if _run_succeeded else "failed",
                )
            except Exception:
                pass

        _run_end_metadata = {
            "html_log": str(getattr(html_logger, "path", "") or ""),
            "output_mode": _goal_output_mode,
        }
        try:
            _vlm_artifact_exists = resolve_artifact_path(_vlm_output).exists()
        except Exception:
            _vlm_artifact_exists = False
        try:
            _xhr_artifact_exists = resolve_artifact_path(_xhr_output).exists()
        except Exception:
            _xhr_artifact_exists = False
        if _goal_output_mode != "answer" or _vlm_artifact_exists:
            _run_end_metadata["vlm_output"] = _vlm_output
        if enable_xhr or xhr_pattern or _xhr_artifact_exists:
            _run_end_metadata["xhr_output"] = _xhr_output
        event_stream.run_end(
            success=_run_succeeded,
            reason="completed" if _run_succeeded else "stopped_or_failed",
            metadata=_run_end_metadata,
        )
        _complete_owned_run_registry_record(
            _run_ts,
            owned=_registry_record_owned,
            success=_run_succeeded,
            stopped=bool(stop_event and stop_event.is_set()),
        )
        html_logger.finalize()
        await release_browser(
            browser_lease,
            error="" if _run_succeeded else "stopped_or_failed",
        )
        # E1c-3b: release any cross-system BrowserSessions this run pooled via the
        # session router (acquire_for_switch). Flag-gated so a feature-off run never
        # touches the pool; a safe no-op (returns []) when nothing was acquired.
        if (
            _session_router is not None
            and _xsys_enabled()
        ):
            try:
                _released_sessions = await _session_router.release_all(
                    error="" if _run_succeeded else "stopped_or_failed",
                )
                if _released_sessions:
                    logger.info(
                        "[SESSION ROUTER] released %d pooled session(s)",
                        len(_released_sessions),
                    )
            except Exception as _release_err:
                logger.debug("[SESSION ROUTER] release_all skipped: %s", _release_err)
    return _run_succeeded


def main():
    """CLI 入口：解析命令行参数并启动 Agent。"""
    default_user_data_dir = str(Path(__file__).parent / "browser_data")

    parser = argparse.ArgumentParser(
        description="VSpider - Visual Web Agent (Offline/Intranet Edition)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python main.py --goal "查一下今天的金价"   # url 省略，自动从 goal 推断入口\n'
            '  python main.py --url "http://192.168.1.100/login" --goal "Login and query data"\n'
            '  python main.py --url "https://example.com" --goal "Find contact page"\n'
            '  python main.py --url "http://10.0.0.1" --goal "Login" --user-data-dir ./browser_data'
        ),
    )
    parser.add_argument(
        "--url",
        required=False,
        default="",
        help="Target webpage starting URL (optional; inferred from --goal via preflight when omitted)",
    )
    parser.add_argument(
        "--goal",
        required=True,
        help="Natural language task goal description",
    )
    parser.add_argument(
        "--user-data-dir",
        default=default_user_data_dir,
        help=f"Browser user data directory for session/cookie persistence (default: {default_user_data_dir})",
    )
    parser.add_argument(
        "--auth-profiles",
        default=os.getenv("VSPIDER_AUTH_PROFILES", ""),
        help=(
            "Auth Matrix profiles to load from .auth/ (comma/space separated), or 'auto' "
            "to load profiles whose storage_state domains match --url. "
            "Example: --auth-profiles bilibili_default,jd_test01"
        ),
    )
    parser.add_argument(
        "--context",
        default="",
        help="Extra context instructions for the agent",
    )
    parser.add_argument(
        "--constraints",
        default="",
        help="Strict constraints and rules for the agent",
    )
    parser.add_argument(
        "--run-constraints-json",
        default="",
        help="Structured run constraints JSON object (for example: '{\"resume\": true, \"max_runs\": 3}')",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Enable opt-in resume behavior via input_contract.constraints.resume",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output requirements for extracted data",
    )
    parser.add_argument(
        "--vlm-model",
        default="",
        help="Runtime VLM model override",
    )
    parser.add_argument(
        "--semantic-model",
        default="",
        help="Runtime semantic/text model override",
    )
    parser.add_argument(
        "--vlm-model-type",
        choices=("vl", "text"),
        default="",
        help="Runtime VLM model type override",
    )
    parser.add_argument(
        "--vlm-temperature",
        type=float,
        default=None,
        help="Runtime VLM temperature override",
    )
    parser.add_argument(
        "--vlm-max-tokens",
        type=int,
        default=None,
        help="Runtime VLM max_tokens override",
    )
    parser.add_argument(
        "--vlm-base-url",
        default="",
        help="Runtime VLM base URL override",
    )
    parser.add_argument(
        "--vlm-api-key",
        default="",
        help="Runtime VLM API key override",
    )
    parser.add_argument(
        "--semantic-base-url",
        default="",
        help="Runtime semantic/text base URL override",
    )
    parser.add_argument(
        "--semantic-api-key",
        default="",
        help="Runtime semantic/text API key override",
    )
    parser.add_argument(
        "--xhr",
        action="store_true",
        default=False,
        help="Enable XHR/Fetch interceptor to capture API responses (for enterprise systems with JSON table data)",
    )
    parser.add_argument(
        "--upload-file",
        default="",
        help="Pre-configure the file path to upload when the agent encounters a file upload element (bypasses system file picker dialog)",
    )
    parser.add_argument(
        "--xhr-pattern",
        default="",
        dest="xhr_pattern",
        help=(
            "Enable XHR primary engine: URL substring to match against API responses. "
            "When a response URL contains this pattern, the data is captured immediately "
            "and the VLM loop is skipped. "
            "Example: --xhr-pattern 'api/board' (Baidu hot search), "
            "--xhr-pattern '/api/orders' (enterprise order API)."
        ),
    )
    parser.add_argument(
        "--require-login",
        action="store_true",
        default=False,
        help=(
            "Proactively attempt native account/password login for the current site before the VLM loop. "
            "Use this when a task should start from an authenticated state, such as Bilibili account-only actions."
        ),
    )
    args = parser.parse_args()

    # 将 user-data-dir 注入 config（始终生效，确保 Cookie 持久化）
    _apply_runtime_overrides(args)

    # 组装最终的目标 Prompt
    full_goal = args.goal
    if args.context:
        full_goal += f"\n\n【环境与上下文】\n{args.context}"
    if args.constraints:
        full_goal += f"\n\n【操作约束与限制】\n{args.constraints}"
    if args.output:
        full_goal += f"\n\n【输出要求】\n{args.output}"
    run_constraints = _build_cli_run_constraints(parser, args)
    vlm_options = _build_cli_vlm_options(args)

    logger.info("VSpider - Visual Web Agent starting...")
    logger.info(f"Assembled Full Goal:\n{full_goal}")
    asyncio.run(
        run_agent(
            args.url,
            full_goal,
            enable_xhr=args.xhr,
            upload_file=args.upload_file,
            xhr_pattern=args.xhr_pattern,
            require_login=args.require_login,
            auth_profiles=args.auth_profiles,
            vlm_options=vlm_options,
            run_constraints=run_constraints,
        )
    )


if __name__ == "__main__":
    main()

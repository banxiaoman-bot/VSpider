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
        read_input_contract,
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
    from .phases.perception import PerceptionPhase
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
    from .extraction_engine.recovery import (
        rank_extraction_candidates_with_history,
        maybe_publish_extraction_recovery_hint,
    )
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
    from .virtual_scroll import (
        capture_virtual_list_rows,
        find_virtual_list_scope,
        map_captured_rows_to_fields,
        nudge_virtual_scroll,
    )
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
        read_input_contract,
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
    from phases.perception import PerceptionPhase
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
    from extraction_engine.recovery import (
        rank_extraction_candidates_with_history,
        maybe_publish_extraction_recovery_hint,
    )
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
    from virtual_scroll import (
        capture_virtual_list_rows,
        find_virtual_list_scope,
        map_captured_rows_to_fields,
        nudge_virtual_scroll,
    )

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


async def _wait_for_human_resume(reason: str = "", *, page=None) -> None:
    if page is not None:
        try:
            from visual_web_agent.hitl_form_proxy import try_hitl_form_proxy
            if await try_hitl_form_proxy(page, reason):
                return
        except Exception:
            pass

    _hitl_screenshot = ""
    if page is not None:
        try:
            import base64
            _raw = await page.screenshot(type="jpeg", quality=60)
            _hitl_screenshot = "data:image/jpeg;base64," + base64.b64encode(_raw).decode()
        except Exception:
            pass

    try:
        from api_server import broadcast_human_intervention, wait_for_human_resume

        if broadcast_human_intervention(reason, screenshot=_hitl_screenshot):
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


from .phases.rpa_cache import (  # noqa: E402
    _build_rpa_match_metadata, _clean_form_label_text, _extract_core_goal, _load_exact_rpa_cache,
    _lookup_form_assignment, _lookup_form_assignment_by_value, _normalize_form_task_plan, _normalize_goal_for_rpa,
    _normalize_url_for_rpa, _parse_form_assignments, _rpa_cache_path, _split_form_assignment_payload,
    _text_is_form_visibility_trap, _trail_completes_form_goal,
)

from .phases.goal_parser import (  # noqa: E402
    _parse_goal_target_count, _parse_goal_target_pages, _extraction_targets_reached,
    _normalize_output_field_key, _parse_goal_requested_fields,
    _goal_is_tooltip_extract, _TOOLTIP_PLACEMENT_ORDER, _parse_goal_tooltip_targets,
    _title_tooltip_target, _infer_tooltip_trigger_label,
    _goal_is_cascader_task, _find_visible_popup_menu_label,
    _rewrite_cascader_nav_click_to_popup_text,
    _goal_is_rpa_challenge_task, _goal_is_round_form_task,
    _goal_has_explicit_login_intent, _goal_is_chat_task,
    _goal_is_form_fill, _form_goal_requires_submit,
    _parse_form_repeat_count, _should_use_round_form_macro,
)

_FORM_SUBMIT_RE = re.compile(
    r"^\s*(create|submit|save|ok|confirm|提交|保存|确定|创建|确认)\s*$",
    re.IGNORECASE,
)


from .phases.rpa_macros import (  # noqa: E402
    _RPA_CHALLENGE_DEFAULT_XLSX, _RPA_CHALLENGE_FIELD_ALIASES, _RPA_CHALLENGE_TOTAL_ROUNDS, _click_best_link,
    _click_visible_text, _close_visible_dialog, _extract_main_heading, _extract_visible_dialog_text,
    _get_round_form_state, _get_rpa_challenge_state, _google_sheets_csv_export_url, _guard_vlm_endpoint_override,
    _has_active_round_form_round, _has_active_rpa_challenge_round, _load_public_google_sheet_rows, _load_rpa_challenge_rows,
    _normalize_rpa_challenge_field_name, _parse_modal_trigger_labels, _parse_rpa_challenge_total_rounds, _resolve_active_form_assignments,
    _round_form_is_complete, _rpa_challenge_is_complete, _run_demoqa_droppable_slider_macro_if_applicable, _run_demoqa_slider_macro_if_applicable,
    _run_internet_hovers_macro_if_applicable, _run_modal_extract_macro_if_applicable, _run_reactrouter_docs_macro_if_applicable, _run_round_form_if_present,
    _run_rpa_challenge_if_present, _run_rpa_challenge_macro, _run_selectorshub_shadow_iframe_macro_if_applicable, _run_wikipedia_new_tab_macro_if_applicable,
    _set_demoqa_slider_value, _start_rpa_challenge_if_needed,
)

from .phases.auto_form import (  # noqa: E402
    _AUTO_FORM_NOT_FOUND_REASONS, _FRAME_RICH_TEXT_WRITE_JS, _assignment_is_non_text_control, _auto_form_fill_bound_controls_with_frames,
    _auto_form_has_prestart_gate, _auto_form_rescue_unreachable_iframes, _auto_form_result_found_nothing, _evaluate_rows_with_frame_fallback,
    _format_auto_form_validation_summary, _inspect_form_submit_target, _locate_visible_form_submit_text, _looks_like_submit_text,
    _month_offset_day, _norm_form_text, _parse_goal_scope_title, _parse_relative_month_day,
    _parse_semantic_macro, _prepare_form_batch_fields, _resolve_editor_frame, _semantic_goal_currently_satisfied,
    _semantic_macro_should_own_goal, _semanticize_rpa_trail, _try_auto_form_fill, _try_auto_form_fill_bound_controls,
    _validate_form_assignments_on_page,
)

from .phases.pagination_helpers import (  # noqa: E402
    _derive_effective_max_steps, _find_similar_rpa_cache, _goal_needs_pagination_probe, _goal_should_skip_rpa,
    _load_rpa_cache_payload, _should_force_first_flip_after_successful_extract, _should_schedule_next_page_after_extract,
)

from .phases.login import (  # noqa: E402
    _LOGIN_SITE_PROFILE_FIELDS, _base_login_settings, _current_page_host, _first_visible_locator,
    _goal_requires_login_flow, _load_login_site_profiles, _normalize_login_domain, _resolve_login_settings,
    _resolve_login_vault_value, _run_preflight_login, _split_login_domains, _url_looks_like_login,
    _wait_for_visible_locator,
)

from .phases.decision_helpers import (  # noqa: E402
    _goal_prefers_visual_navigation, _goal_should_force_vision, _should_fallback_to_vision,
    _find_target_line, _decision_is_irrelevant_nav_click, _goal_is_bulk_extraction,
    _decision_click_targets_extraction_control, _goal_explicitly_requests_voice_or_camera,
    _goal_is_plain_search_task, _decision_is_auxiliary_search_control_click,
    _decision_is_non_submit_search_control_click, _search_goal_done_looks_premature,
    _decision_implies_completion, _decision_mentions_follow_up_work,
    _decision_claims_current_subgoal_completed, _decision_should_finish_instead_of_operate,
    _done_targets_final_subgoal, _is_terminal_only_subgoal,
    _goal_explicitly_requests_backtracking, _decision_is_regressive_backtrack,
    _force_done_decision, _clean_user_visible_done_message,
)

from .phases.answer_domain import (  # noqa: E402
    _format_extracted_rows_as_answer, _clean_weather_city_candidate,
    _extract_weather_city_from_goal, _infer_answer_weather_search_query,
    _baidu_query_from_url, _is_baidu_url, _normalize_search_query,
    _infer_answer_stock_search_query, _infer_answer_recipe_search_query,
    _infer_answer_flight_search_query, _build_answer_search_fast_path_url,
    _maybe_run_answer_search_fast_path, _city_regex_for_weather_text,
    _normalize_weather_temperature, _compose_weather_answer,
    _compact_weather_answer_from_text, _detect_answer_domain,
    _extract_stock_subject_from_goal, _compact_stock_answer_for_goal,
    _extract_recipe_name_from_goal, _compact_recipe_answer_for_goal,
    _extract_flight_number_from_goal, _compact_flight_answer_for_goal,
    _compact_answer_text_for_goal,
)

from .phases.rpa_replay import (  # noqa: E402
    _compact_rpa_trail, _is_dynamic_content_step, _resolve_composite_locator,
    _normalize_rpa_cache_payload, _extract_placeholder_keys, _stable_memory_keys,
    _resolve_replay_template, _write_rpa_cache_payload, _mark_rpa_cache_failure,
    _replay_switch_system, _replay_rpa, _replay_ready_rpa_steps, _print_manual_warning,
)

from .phases.cli_config import (  # noqa: E402
    _runtime_config_module, _apply_runtime_overrides, _parse_cli_json_object,
    _build_cli_run_constraints, _build_cli_vlm_options, _resolve_action_tool_metadata,
)

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

    # G1: run identity delegated to phases/startup.py
    from visual_web_agent.phases.startup import prepare_run_identity as _prepare_run_id
    _run_ctx = _prepare_run_id(run_id=run_id)
    _run_ts = _run_ctx.run_ts
    _registry_record_owned = False
    # L: open per-run phase event jsonl right after run_ts is known so
    # every broadcast_phase call from this run lands in logs/phase_<ts>.jsonl
    try:
        from api_server import set_phase_log_run_id as _set_phase_run

        _set_phase_run(_run_ts)
    except Exception:
        pass
    _vlm_output = _run_ctx.vlm_output
    _xhr_output = _run_ctx.xhr_output
    _goal_output_mode = _run_ctx.goal_output_mode
    _goal_output_contract: dict[str, Any] = _run_ctx.goal_output_contract
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
    try:
        from .phases.setup import SetupDeps as _SetupDeps, SetupTools as _SetupTools
    except ImportError:
        from phases.setup import SetupDeps as _SetupDeps, SetupTools as _SetupTools
    _setup = _SetupTools(_SetupDeps(
        browser=browser,
        goal=goal,
        vlm=vlm,
        logger=logger,
        event_stream=event_stream,
        stop_event=stop_event,
        start_url=start_url,
        action_registry=action_registry,
        broadcast_log_safe=_broadcast_log_safe,
        broadcast_done_safe=_broadcast_done_safe,
    ))
    skill_registry = build_default_skill_registry(load_history=True)
    async def _browser_action_tool(action_payload, workflow_memory=None):
        return await _setup.browser_action_tool(action_payload, workflow_memory)

    async def _targeted_probe_tool(action_payload, workflow_memory=None):
        return await _setup.targeted_probe_tool(action_payload, workflow_memory)

    async def _try_targeted_click_text_handoff(action_payload):
        return await _setup.try_targeted_click_text_handoff(action_payload)

    def _resolve_type_value_for_handoff(raw_value, workflow_memory=None):
        return _setup.resolve_type_value_for_handoff(raw_value, workflow_memory)

    async def _try_targeted_type_handoff(action_payload, workflow_memory=None):
        return await _setup.try_targeted_type_handoff(action_payload, workflow_memory)

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

    def _check_stop(context):
        return _setup.check_stop(context)

    def _abort_if_stale_auth():
        return _setup.abort_if_stale_auth()

    def _with_tool_metadata(result):
        return _setup.with_tool_metadata(result, _selected_tools)

    async def _recover_active_page(reason):
        return await _setup.recover_active_page(reason)

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
            # S7: feed the persisted input_contract.json (urls[].system_id /
            # auth_profile) into the router so workflow_graph._build_systems
            # plans systems from the user's declared URLs instead of
            # re-deriving everything from goal text.
            _route_context: dict[str, Any] = {}
            try:
                _persisted_input_contract = read_input_contract(_run_ts)
            except Exception:
                _persisted_input_contract = None
            if isinstance(_persisted_input_contract, dict):
                _route_context["input_contract"] = _persisted_input_contract
            _capability_route = route_capabilities_for_task(
                goal,
                url=start_url,
                context=_route_context or None,
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
        # S9: multi-system runs pre-acquire one pool session per planned web
        # system so the first cross-system hop rebinds an existing session
        # instead of cold-starting one mid-flow. Failure of any single
        # acquire degrades to the legacy on-demand path for that system.
        try:
            if _session_router is not None and _xsys_enabled():
                _planned_web_systems = [
                    str(s.get("id") or "")
                    for s in ((_capability_route.get("workflow_graph") or {}).get("systems") or [])
                    if isinstance(s, dict) and s.get("type") == "web" and s.get("id")
                ]
                if len(_planned_web_systems) > 1:
                    _preacquired_plan = _session_router.pre_acquire_sessions(_planned_web_systems)
                    event_stream.emit(
                        "session_plan_preacquired",
                        run_id=_run_ts,
                        plan=_preacquired_plan,
                    )
                    logger.info(
                        "[SESSION ROUTER] pre-acquired %s/%s planned system sessions: %s",
                        sum(1 for item in _preacquired_plan if item.get("acquired")),
                        len(_preacquired_plan),
                        [
                            f"{item['system_id']}@{item['auth_profile']}"
                            for item in _preacquired_plan
                        ],
                    )
        except Exception as _preacquire_err:
            logger.debug("[SESSION ROUTER] pre-acquire skipped: %s", _preacquire_err)
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
        _consecutive_zero_target = 0  # G4: also tracked by _post_decision_guards
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
        # S1a: group extraction counters into a shared ExtractState so the
        # main loop and the extraction closures mutate one instance.
        try:
            from .extraction_engine.runtime import ExtractState as _ExtractState, ExtractDeps as _ExtractDeps, ExtractRuntime as _ExtractRuntime
        except ImportError:  # pragma: no cover
            from extraction_engine.runtime import ExtractState as _ExtractState, ExtractDeps as _ExtractDeps, ExtractRuntime as _ExtractRuntime  # type: ignore[no-redef]
        _xs = _ExtractState()
        _xs.extract_count = 0  # 连续 extract 次数（中间无翻页 click），>=2 强制 done
        _xs.pagination_probed = False  # 首次 extract 后探测分页器一次（Improvement 1）
        _xs.pagination_kind = ""        # "numeric" / "next_only" / "infinite" / ""
        _xs.pagination_hint_msg = ""    # 待注入到 VLM 的探测结果反馈（下一轮 ask 时消费）
        _xs.first_flip_pending = False  # 首次 extract 后强制下一步走 next_page（引擎层硬约束）
        _xs.page_is_infinite_scroll = False  # 标记当前页面是无限滚动（无分页器）
        _xs.force_next_page_pending = False  # 物理触底且未达标：下一轮强制 next_page
        _xs.force_extract_after_navigation_pending = False  # 翻页落地后：下一轮必须先提取新页，禁止连续翻页跳页
        _xs.block_next_page_until_drained = False  # 当前页只提到少量数据且还能滚动：禁止过早翻页
        _xs.block_next_page_reason = ""
        _xs.first_extract_ever_done = False  # 任务级永久锁：首次 extract 完成过（不会被翻页重置）
        _prev_action_sig: tuple[str, int, str] = ("", 0, "")  # G4: also tracked by _post_decision_guards; kept for other consumers (action, target_id, type_value)，用于"思想-动作分离"检测
        _repeat_action_count: int = 0  # 同一 action sig 连续重复次数，用于精确触发 AUTO-ADVANCE
        _xs.total_extracted_rows = 0  # 跨页累加的总行数
        _xs.extract_null_streak = 0  # 连续 extract+null 降级次数，用于三级升级策略
        _xs.extract_null_total_resets = 0  # 防止无限重试：streak 被重置的总次数
        _xs.extracted_page_urls = set()  # 已成功提取数据的不同页面 URL 集合
        _xs.extracted_page_keys = set()  # URL + table signature; SPA/table pagination stays on one URL
        _xs.seen_extract_row_keys = set()  # 行级去重，支持同 URL 无限滚动/局部刷新
        _xs.tooltip_trigger_keys = set()  # tooltip 任务按 trigger 统计进度，而不是按候选行累加
        _xs.pagination_exhausted = False  # 分页已耗尽（滚到底+翻页失败），用于容差退出
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
                    _xs.seen_extract_row_keys |= _resume_seed.seen
                    _xs.total_extracted_rows = max(_xs.total_extracted_rows, _resume_seed.count)
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

        def _sanitize_extraction_candidate(*, name, data, source_text="", data_shape=None):
            return _extract_rt.sanitize_extraction_candidate(
                name=name, data=data, source_text=source_text, data_shape=data_shape
            )

        def _expected_rows_from_data_shape(data_shape):
            return _extract_rt.expected_rows_from_data_shape(data_shape)

        def _candidate_min_expected_rows(candidate):
            return _extract_rt.candidate_min_expected_rows(candidate)

        def _is_under_yield_viewport_candidate(candidate):
            return _extract_rt.is_under_yield_viewport_candidate(candidate)

        def _choose_best_extraction_candidate(candidates):
            return _extract_rt.choose_best_extraction_candidate(candidates)

        def _commit_extraction_candidate(candidate):
            return _extract_rt.commit_extraction_candidate(candidate)

        def _record_extract_progress(rows, accepted_count):
            return _extract_rt.record_extract_progress(rows, accepted_count)

        async def _capture_body_text_excerpt(limit: int = 3000) -> str:
            return await _extract_rt.capture_body_text_excerpt(limit)

        async def _save_extraction_snapshot(*, source, rows, output_file, accepted_rows, duplicate_rows=0, rejected_rows=0, candidates=None, data_shape=None, source_text="", metadata=None):
            return await _extract_rt.save_extraction_snapshot(
                source=source, rows=rows, output_file=output_file, accepted_rows=accepted_rows,
                duplicate_rows=duplicate_rows, rejected_rows=rejected_rows, candidates=candidates,
                data_shape=data_shape, source_text=source_text, metadata=metadata, step=step,
            )

        async def _try_dom_api_fast_path(dom_rows, *, source, dom_text=""):
            return await _extract_rt.try_dom_api_fast_path(dom_rows, source=source, dom_text=dom_text, step=step)

        def _xhr_saved_row_count():
            return _extract_rt.xhr_saved_row_count()

        def _xhr_target_reached():
            return _extract_rt.xhr_target_reached()

        async def _finish_if_xhr_target_reached(reason: str) -> bool:
            nonlocal _task_completed, _run_succeeded, _log_screenshot_path, _log_decision
            reached, count, target = _xhr_target_reached()
            if not reached:
                return False
            _xs.total_extracted_rows = max(_xs.total_extracted_rows, count)
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

        def _compact_link_match_text(value):
            return _extract_rt.compact_link_match_text(value)

        def _row_has_url_value(row: dict) -> bool:
            for key, value in row.items():
                key_norm = str(key or "").strip().lower()
                if key_norm in {"url", "link", "href"} and str(value or "").strip():
                    return True
            return False

        def _classify_url_role(value):
            return _extract_rt.classify_url_role(value)

        def _is_probable_url(value):
            return _extract_rt.is_probable_url(value)

        def _first_int_value(value):
            return _extract_rt.first_int_value(value)

        def _field_aliases(field):
            return _extract_rt.field_aliases(field)

        def _requested_field_coverage(row):
            return _extract_rt.requested_field_coverage(row)

        def _min_requested_field_hits(total: int) -> int:
            if total <= 0:
                return 0
            if total <= 4:
                return total
            return max(2, int(math.ceil(total * 0.75)))

        def _project_row_to_requested_fields(row):
            return _extract_rt.project_row_to_requested_fields(row)

        def _normalize_extracted_row_fields(rows, *, project=True):
            return _extract_rt.normalize_extracted_row_fields(rows, project=project)

        def _row_primary_link_text(row):
            return _extract_rt.row_primary_link_text(row)

        async def _enrich_rows_with_dom_links(rows):
            return await _extract_rt.enrich_rows_with_dom_links(rows)

        async def _extract_compact_list_text_via_dom(reason):
            return await _extract_rt.extract_compact_list_text_via_dom(reason)

        async def _extract_full_page_text_for_data(reason):
            return await _extract_rt.extract_full_page_text_for_data(reason)

        async def _extract_body_text_for_semantic_cards(reason):
            return await _extract_rt.extract_body_text_for_semantic_cards(reason)

        async def _inspect_click_target_for_extract_nav_guard(decision):
            return await _extract_rt.inspect_click_target_for_extract_nav_guard(decision)

        # S1b: the DOM extraction readers now live on ExtractRuntime; build
        # it once with the deps they used to capture so call sites stay stable.
        _extract_deps = _ExtractDeps(
            browser=browser,
            logger=logger,
            evaluate_rows_with_frame_fallback=_evaluate_rows_with_frame_fallback,
            goal=goal,
            goal_output_mode=_goal_output_mode,
            requested_output_fields=_requested_output_fields,
            data_controller=_data_controller,
            event_stream=event_stream,
            run_ts=_run_ts,
            snapshot_goal=_snapshot_goal,
            goal_output_contract=_goal_output_contract,
            vlm_output=_vlm_output,
            enable_xhr=enable_xhr,
        )
        _extract_rt = _ExtractRuntime(_extract_deps, _xs)

        async def _extract_list_rows_via_dom(reason: str) -> tuple[list[dict], str]:
            return await _extract_rt.extract_list_rows_via_dom(reason)

        async def _extract_visible_table_rows_via_dom(reason: str) -> list[dict]:
            return await _extract_rt.extract_visible_table_rows_via_dom(reason)

        async def _visible_table_signature(reason: str, scope=None) -> str:
            return await _extract_rt.visible_table_signature(reason, scope=scope)

        async def _auto_advance_table_page_via_dom(reason: str) -> bool:
            return await _extract_rt.auto_advance_table_page_via_dom(reason)

        async def _nudge_scroll_after_duplicate_extract(reason, scroll_amount=2000):
            return await _extract_rt.nudge_scroll_after_duplicate_extract(reason, scroll_amount)

        async def _probe_scroll_drain_state(reason: str) -> dict:
            return await _extract_rt.probe_scroll_drain_state(reason)

        async def _detect_canvas_grid(reason: str) -> dict:
            return await _extract_rt.detect_canvas_grid(reason)

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

            # EXTRACT-VSCROLL-2: when static harvests fall short of the goal
            # and an inner scroller exists, run the deterministic capture
            # loop once instead of burning one VLM round per viewport.
            vscroll_rows: list = []
            vscroll_meta: dict = {}
            if (
                len(dom_table_rows) < target_count
                and len(dom_list_rows) < target_count
            ):
                try:
                    drain = await _probe_scroll_drain_state("pre-extract vscroll probe")
                    _vs_scope = None
                    if drain.get("container_can_scroll"):
                        _vs_scope = await browser._ensure_active_page(
                            reason="pre-extract vscroll capture"
                        )
                    else:
                        # EXTRACT-VSCROLL-3: the drain probe sees the main
                        # document only - sweep child frames so iframe-hosted
                        # virtual lists reach the deterministic capture too.
                        _vs_page = await browser._ensure_active_page(
                            reason="pre-extract vscroll frame sweep"
                        )
                        if _vs_page is not None:
                            _vs_probe = await find_virtual_list_scope(
                                _vs_page, include_main=False
                            )
                            _vs_scope = _vs_probe.get("scope")
                    if _vs_scope is not None:
                        vscroll_meta = await capture_virtual_list_rows(
                            _vs_scope, max_rows=max(target_count * 2, 200)
                        )
                        vscroll_rows = list(vscroll_meta.get("rows") or [])
                except Exception as vs_err:
                    logger.debug("[PRE-EXTRACT] vscroll capture skipped: %s", vs_err)
            if len(vscroll_rows) > max(len(dom_table_rows), len(dom_list_rows), 1):
                logger.info(
                    "[PRE-EXTRACT] vscroll capture rows=%s passes=%s complete=%s",
                    len(vscroll_rows),
                    vscroll_meta.get("passes"),
                    vscroll_meta.get("complete"),
                )
                candidates.append(
                    _sanitize_extraction_candidate(
                        name="VSCROLL_LIST",
                        # VSCROLL-FIELDS-1: named columns when headers exist.
                        data=map_captured_rows_to_fields(
                            vscroll_rows, vscroll_meta.get("headers") or []
                        ),
                        source_text="\n".join(
                            str(row.get("text") or "") for row in vscroll_rows[:400]
                        ),
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
                # EXTRACT-CANVAS-1: declare canvas/svg-rendered grids
                # explicitly instead of silently falling through - the
                # planner gets actionable guidance (export button > view
                # switch > screenshot-with-caveat) via workflow_memory.
                canvas_notice = await _detect_canvas_grid(
                    "pre-extract canvas grid probe"
                )
                if canvas_notice:
                    try:
                        workflow_memory["canvas_grid_notice"] = canvas_notice
                    except Exception:
                        pass
                    logger.info(
                        "[PRE-EXTRACT] canvas/svg grid declared: tag=%s %sx%s "
                        "coverage=%s grid_like=%s - deterministic DOM extraction "
                        "unavailable, fallback guidance published",
                        canvas_notice.get("tag"),
                        canvas_notice.get("width"),
                        canvas_notice.get("height"),
                        canvas_notice.get("coverage"),
                        canvas_notice.get("grid_like"),
                    )
                # C1: on collapse, publish a gated selector-recovery hint so the
                # planner can re-attempt biased to a known-good baseline surface.
                try:
                    _rec_hint = maybe_publish_extraction_recovery_hint(
                        workflow_memory, candidates,
                        url=getattr(browser, "current_url", "") or start_url,
                        requested_fields=_requested_output_fields, goal=_snapshot_goal,
                    )
                    if _rec_hint.get("recovered"):
                        logger.info(
                            "[PRE-EXTRACT] selector recovery hint published: source_family=%s coverage=%s",
                            _rec_hint.get("source_family"), _rec_hint.get("baseline_coverage"),
                        )
                except Exception as _rec_hint_err:
                    logger.debug("[PRE-EXTRACT] recovery hint skipped: %s", _rec_hint_err)
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
        # E1: 感知复用状态（last_signature / streak）跨回合持有，必须循环外单例
        _perception_phase = PerceptionPhase()
        # G4: post-decision guards delegated to phases/action_dispatch.py
        from visual_web_agent.phases.action_dispatch import PostDecisionGuards as _PDG
        _post_decision_guards = _PDG()
        # ── Session Drop tracking (Wave 3) ───────────────────────────
        # _last_business_url: most recent non-login URL we observed at
        # step end. Used by the session-drop sniffer to decide whether
        # we've been bounced to login MID-task.
        # _pending_session_return_url: set when sniffer fires; consumed
        # by the post-ask_human resume handler to goto back automatically.
        _last_business_url: str = ""
        _pending_session_return_url: str = ""
        # G1: loop guards delegated to phases/startup.py
        from visual_web_agent.phases.startup import init_loop_guards as _init_guards
        _guards = _init_guards(vlm)
        _failure_stats = _guards.failure_stats
        _judge = _guards.judge
        _judge_rejections = _guards.judge_rejections
        _loop_detector = _guards.loop_detector

        # G2: Planner/Reflector state delegated to phases/planning.py
        from visual_web_agent.phases.planning import PlanningPhase as _PlanningPhase
        _planning = _PlanningPhase()
        _task_plan = _planning.task_plan
        _steps_since_reflect = _planning.steps_since_reflect
        _reflect_count = _planning.reflect_count
        _MAX_REFLECTS = 5
        _REFLECT_INTERVAL = 5
        _dedup_tripped_last_step = _planning.dedup_tripped_last_step
        _duplicate_zero_extract_streak = _planning.duplicate_zero_extract_streak
        _abort_requested = _planning.abort_requested

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
        # G2: initial plan generation delegated to PlanningPhase
        _task_plan = await _planning.make_initial_plan(
            vlm=vlm,
            goal=goal,
            initial_url=browser.current_url or start_url,
            workflow_memory=workflow_memory,
        )
        if _task_plan is not None:
            _original_plan_count = len(_task_plan.sub_goals)
            _task_plan = _normalize_form_task_plan(_task_plan, goal)
            _planning.task_plan = _task_plan
            if _is_form_fill_goal and _task_plan is not None and len(_task_plan.sub_goals) != _original_plan_count:
                logger.info("[PLANNER] Form plan normalized: removed visibility-only subgoals")
                _broadcast_log_safe("[PLANNER] 表单任务已改写为逐字段填写计划，禁用完整同屏子目标", level="warn")
            print(f"\n\033[1;35m📋 [PLANNER]\033[0m 生成 \033[35m{len(_task_plan.sub_goals)}\033[0m 个子目标：")
            for _sg in _task_plan.sub_goals:
                print(f"   {_sg.id}. {_sg.description}")

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
                # 图文双模态融合 (Hybrid Modality) —— 感知段已平移至 phases/perception.py（P1）
                # 每一轮采集 SoM 截图 + AX Tree 语义树，组装 BrowserStateSnapshot 并写 observe 事件。
                # ════════════════════════════════════════════════════════════
                # 旧的 _force_vision_next_step 信号在双模态下已失效，这里消耗掉以保持语义干净
                _force_vision_next_step = False
                _perception = await _perception_phase.run(
                    browser,
                    step=step,
                    event_stream=event_stream,
                    start_url=start_url,
                    recover_active_page=_recover_active_page,
                    wait_for_human_resume=_wait_for_human_resume,
                    bot_challenge_state=_bot_challenge_state,
                )
                screenshot_b64 = _perception.screenshot_b64
                input_descriptions = _perception.input_descriptions
                ax_tree_text = _perception.ax_tree_text
                _log_screenshot_path = _perception.screenshot_path
                _log_reasoning_text_source = _perception.reasoning_text_source
                _browser_state = _perception.browser_state
                _tabs_state = _perception.tabs_state
                _page_state = _perception.page_summary

                # G2: Reflector delegated to PlanningPhase.maybe_reflect()
                _prev_loop_guard_size = len(_loop_guard_blocked_ids)
                _planning.task_plan = _task_plan
                _planning.dedup_tripped_last_step = _dedup_tripped_last_step
                _planning.steps_since_reflect = _steps_since_reflect
                _planning.reflect_count = _reflect_count
                _rd = await _planning.maybe_reflect(
                    vlm=vlm,
                    current_url=browser.current_url or "",
                    goal=goal,
                    consecutive_errors=_consecutive_errors,
                )
                _task_plan = _planning.task_plan
                _steps_since_reflect = _planning.steps_since_reflect
                _reflect_count = _planning.reflect_count
                _dedup_tripped_last_step = _planning.dedup_tripped_last_step
                _abort_requested = _planning.abort_requested

                # ── Improvement 1：消费分页器探测结果（一次性，注入完即清） ──
                if _xs.pagination_hint_msg:
                    input_descriptions = (
                        _xs.pagination_hint_msg + "\n" + (input_descriptions or "")
                    )
                    _xs.pagination_hint_msg = ""

                # ── Path D + Improvement 3：进度透传，标为系统权威记账 ──
                # VLM 没有长程数学记忆，必须在 prompt 里持续回灌权威进度。
                # **强调"系统记账（唯一权威）"** 让 VLM 不再自己心算条数（避免 37/50 vs 30/50 偏差）。
                _prog_target = _parse_goal_target_count(goal)
                if _prog_target is not None and _xs.total_extracted_rows > 0:
                    _prog_pages = len(_xs.extracted_page_urls)
                    _prog_remaining = max(0, _prog_target - _xs.total_extracted_rows)
                    _prog_pct = int(min(100, _xs.total_extracted_rows * 100 / _prog_target))
                    input_descriptions = (
                        f"\n📊【全局抓取进度（系统记账，唯一权威）】"
                        f"{_xs.total_extracted_rows}/{_prog_target} 条 "
                        f"({_prog_pct}%，跨 {_prog_pages} 个页面)，"
                        f"还需 {_prog_remaining} 条；达量后引擎会自动终止任务。\n"
                        f"**禁止**在 thought 里自己心算/估算条数 —— 一切以此数字为准。\n"
                        + (input_descriptions or "")
                    )

                _forced_target = _parse_goal_target_count(goal)
                _can_force_extract_after_navigation_now = (
                    _xs.force_extract_after_navigation_pending
                    and (
                        _forced_target is None
                        or _xs.total_extracted_rows < _forced_target
                    )
                )
                _can_force_next_page_now = (
                    _xs.force_next_page_pending
                    and (
                        _forced_target is None
                        or _xs.total_extracted_rows < _forced_target
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
                    _xs.force_extract_after_navigation_pending = False
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
                    _xs.force_next_page_pending = False
                else:
                    if _xs.force_extract_after_navigation_pending:
                        logger.info("[FORCE EXTRACT AFTER NAV] cleared because target is already met")
                        _xs.force_extract_after_navigation_pending = False
                    if _xs.force_next_page_pending:
                        logger.info("[FORCE NEXT_PAGE] cleared because target is already met")
                        _xs.force_next_page_pending = False
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
                        # G3: VLM decision delegated to phases/decision.py
                        from visual_web_agent.phases.decision import make_vlm_decision as _vlm_decide
                        _vlm_result = await _vlm_decide(
                            vlm=vlm, screenshot_b64=screenshot_b64,
                            goal=goal, step=step,
                            input_descriptions=input_descriptions,
                            workflow_memory=workflow_memory,
                            task_plan=_task_plan,
                            max_steps=_effective_max_steps,
                            som_elements=getattr(browser, "_last_som_elements", None),
                            capability_route=_capability_route,
                            prompt_images=_prompt_images,
                            prompt_image_policy=_prompt_image_policy,
                            current_url=_cur_url,
                            last_prompt_image_url=_last_prompt_image_url,
                        )
                        decisions = _vlm_result.decisions
                        if _vlm_result.prompt_images_sent:
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
                            total_extracted_rows=_xs.total_extracted_rows,
                            total_pages=len(_xs.extracted_page_keys),
                            goal_target_count=_parse_goal_target_count(goal),
                            goal_target_pages=_parse_goal_target_pages(goal),
                            pagination_exhausted=_xs.pagination_exhausted,
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
                # G4: delegate repeat-action guard to PostDecisionGuards
                _rg_result = _post_decision_guards.apply_repeat_guard(decisions, vlm=vlm)
                decisions = _rg_result.decisions

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
                    _xs.force_next_page_pending
                    and decisions
                    and decisions[0].get("action") not in ("done", "ask_human", "error")
                    and (
                        (_parse_goal_target_count(goal) is None)
                        or (_xs.total_extracted_rows < (_parse_goal_target_count(goal) or 0))
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
                    _xs.force_next_page_pending = False
                elif (
                    _xs.first_flip_pending
                    and _goal_needs_pagination_probe(goal)
                    and decisions
                    and decisions[0].get("action") in ("smooth_scroll", "scroll", "extract")
                ):
                    _orig_action = decisions[0].get("action")
                    if not _xs.page_is_infinite_scroll:
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
                    _xs.first_flip_pending = False  # 一次性消费，不再触发
                # 即使 VLM 已经选了 next_page，flag 也清掉避免重复触发
                elif _xs.first_flip_pending and not _goal_needs_pagination_probe(goal):
                    logger.info("[FIRST FLIP] skipped for non-pagination extraction goal")
                    _xs.first_flip_pending = False
                elif _xs.first_flip_pending and decisions and decisions[0].get("action") == "next_page":
                    _xs.first_flip_pending = False

                # ── 过早翻页护栏 ────────────────────────────────────────
                # 当上一轮只提到少量行，且物理探针确认当前页/容器还能继续向下滚时，
                # 不允许 VLM 直接 next_page。这样避免豆瓣/长列表只抓视口前几条就
                # 翻页，跳过当前页下半部分数据。若当前页已触底，则放行 next_page。
                if (
                    _xs.block_next_page_until_drained
                    and decisions
                    and decisions[0].get("action") == "next_page"
                ):
                    _drain_guard_target = _parse_goal_target_count(goal)
                    _target_unmet = (
                        _drain_guard_target is None
                        or _xs.total_extracted_rows < _drain_guard_target
                    )
                    if _target_unmet:
                        _drain_state = await _probe_scroll_drain_state(
                            "premature next_page guard"
                        )
                        if not bool(_drain_state.get("at_bottom")):
                            _orig_thought = decisions[0].get("thought") or ""
                            logger.info(
                                "[PREMATURE PAGE GUARD] rewrite next_page -> smooth_scroll: %s; state=%s",
                                _xs.block_next_page_reason,
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
                            _xs.block_next_page_until_drained = False
                            _xs.block_next_page_reason = ""
                    else:
                        _xs.block_next_page_until_drained = False
                        _xs.block_next_page_reason = ""

                # ── Fix 4：连续 ZERO_TARGET_DOWNGRADE RAW LOOP GUARD ───────
                # 检测 VLM 反复输出 click+target_id=0+type_value="X" 的 schema
                # 错位幻觉。已被 validator 降级为 wait，但底层意图仍是同一错误。
                # 连续 3 次相同 type_value → 强行注入硬指令 + 重置积压反馈。
                # G4: delegate zero-target guard to PostDecisionGuards
                _zt_result = _post_decision_guards.apply_zero_target_guard(decisions, vlm=vlm)
                _consecutive_zero_target = _post_decision_guards.consecutive_zero_target

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
                            _xs.total_extracted_rows >= _goal_target
                            or (
                                _xs.pagination_exhausted
                                and _xs.total_extracted_rows >= _goal_target - _tolerance
                            )
                        )
                    )
                    if _extraction_complete and _xs.total_extracted_rows < _goal_target:
                        logger.info(
                            f"[CLOSE ENOUGH] 容差退出: {_xs.total_extracted_rows}/{_goal_target} "
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
                            total_extracted_rows=_xs.total_extracted_rows,
                            total_pages=len(_xs.extracted_page_keys),
                            goal_target_count=_goal_target,
                            goal_target_pages=_parse_goal_target_pages(goal),
                            pagination_exhausted=_xs.pagination_exhausted,
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
                            f"{_xs.total_extracted_rows}/{_goal_target} 条，跳过门闸"
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
                    _xs.extract_null_streak += 1
                    # 同 URL 不再直接跳过：无限滚动/局部刷新常常保持 URL 不变。
                    # 这里仅记录信号，真正是否重复由行级 fingerprint 决定。
                    _current_auto_url = browser.current_url
                    if _current_auto_url in _xs.extracted_page_urls:
                        logger.info(
                            "[EXTRACT AUTO] URL already seen; continuing with row-level dedup: %s",
                            _current_auto_url,
                        )
                    logger.warning(
                        f"[EXTRACT AUTO] VLM 输出 extract+null "
                        f"(第 {_xs.extract_null_streak} 次)，启动 AX Tree 自动提取"
                    )
                    try:
                        _data_shape = await _extract_rt.compute_data_shape_with_drain(
                            "auto extract dense-shape drain override"
                        )

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
                                _xs.block_next_page_until_drained = True
                                _xs.block_next_page_reason = (
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
                            _progress_total_rows = _xs.total_extracted_rows
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
                        _xs.extract_count += 1
                        _xs.extracted_page_urls.add(browser.current_url)
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
                        # Bug 修复：用任务级永久锁 _xs.first_extract_ever_done，避免 _xs.extract_count
                        # 在翻页/导航后被重置回 0 → 下一次 extract 又把 flag 设回 True →
                        # FIRST FLIP 在每次翻页后反复触发的问题。
                        if (
                            not _xs.first_extract_ever_done
                            and _should_force_first_flip_after_successful_extract(goal)
                        ):
                            _xs.first_extract_ever_done = True
                            _xs.first_flip_pending = True
                        # ── Improvement 1：首次 extract 后探测分页器（auto-extract 路径） ──
                        if (
                            _goal_needs_pagination_probe(goal)
                            and not _xs.pagination_probed
                            and _xs.extract_count == 1
                        ):
                            _xs.pagination_probed = True
                            try:
                                _probe = await browser.probe_pagination()
                                _xs.pagination_kind = _probe.get("kind", "")
                                _cands = _probe.get("candidates", [])
                                if _probe.get("has_paginator"):
                                    _names = ", ".join(
                                        f"{c['ref']}={c['name']!r}" for c in _cands[:6]
                                    )
                                    _should_force_probe_next, _force_probe_reason = (
                                        _should_schedule_next_page_after_extract(
                                            goal,
                                            new_rows=_new_rows,
                                            total_rows=_xs.total_extracted_rows,
                                            extract_source=_auto_extract_text_source,
                                            pagination_kind=_xs.pagination_kind,
                                            expected_rows=_expected_rows_from_data_shape(_data_shape),
                                            physically_drained=bool(_data_shape.get("physically_drained")),
                                        )
                                    )
                                    if _should_force_probe_next:
                                        _xs.force_next_page_pending = True
                                        _xs.first_flip_pending = False
                                        _xs.block_next_page_until_drained = False
                                        _xs.block_next_page_reason = ""
                                        logger.info(
                                            "[PROBE PAGE] armed next_page after extract: %s",
                                            _force_probe_reason,
                                        )
                                        _broadcast_log_safe(
                                            f"[PROBE PAGE] 已发现分页器，下一轮直接 next_page：{_force_probe_reason}",
                                            level="info",
                                        )
                                        _xs.pagination_hint_msg = (
                                            f"📍【系统探测：本页**带分页器**（{_xs.pagination_kind}）】\n"
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
                                            _xs.block_next_page_until_drained = True
                                            _xs.block_next_page_reason = _force_probe_reason
                                        else:
                                            _xs.force_next_page_pending = True
                                            _xs.first_flip_pending = False
                                            _xs.block_next_page_until_drained = False
                                            _xs.block_next_page_reason = ""
                                            _force_probe_reason = (
                                                f"{_force_probe_reason}; physical bottom reached"
                                            )
                                        if _xs.force_next_page_pending:
                                            _xs.pagination_hint_msg = (
                                                f"📍【系统探测：本页**带分页器**（{_xs.pagination_kind}）】\n"
                                                f"已确认页面存在翻页控件：{_names}。\n"
                                                f"本次新增 {_new_rows} 条虽低于探头预期，"
                                                "但页面已物理触底，继续滚动不会暴露更多当前页数据。"
                                                "下一步必须使用 next_page 翻页。"
                                            )
                                        else:
                                            if _xs.force_next_page_pending:
                                                _xs.pagination_hint_msg = (
                                                    f"📍【系统探测：本页**带分页器**（{_xs.pagination_kind}）】\n"
                                                    f"已确认页面存在翻页控件：{_names}。\n"
                                                    f"本次新增 {_new_rows} 条虽低于探头预期，"
                                                    "但页面已物理触底，继续滚动不会暴露更多当前页数据。"
                                                    "下一步必须使用 next_page 翻页。"
                                                )
                                            else:
                                                _xs.pagination_hint_msg = (
                                                    f"📍【系统探测：本页**带分页器**（{_xs.pagination_kind}）】\n"
                                                    f"已确认页面存在翻页控件：{_names}。\n"
                                                    f"但本次仅新增 {_new_rows} 条（{_force_probe_reason}），"
                                                    "不足以证明当前页已提取完。\n"
                                                    "下一步请先 smooth_scroll 向下并继续 extract 当前页；"
                                                    "只有当前页物理触底或无新增后，才使用 next_page。"
                                                )
                                else:
                                    # No paginator detected — mark as infinite scroll
                                    _xs.page_is_infinite_scroll = True
                                    _should_force_probe_next, _force_probe_reason = (
                                        _should_schedule_next_page_after_extract(
                                            goal,
                                            new_rows=_new_rows,
                                            total_rows=_xs.total_extracted_rows,
                                            extract_source=_auto_extract_text_source,
                                            pagination_kind=_xs.pagination_kind,
                                            expected_rows=_expected_rows_from_data_shape(_data_shape),
                                            physically_drained=bool(_data_shape.get("physically_drained")),
                                        )
                                    )
                                    if _should_force_probe_next:
                                        _xs.force_next_page_pending = True
                                        _xs.first_flip_pending = False
                                        _xs.block_next_page_until_drained = False
                                        _xs.block_next_page_reason = ""
                                        logger.info(
                                            "[PROBE PAGE] armed universal next_page after extract: %s",
                                            _force_probe_reason,
                                        )
                                        _broadcast_log_safe(
                                            f"[PROBE PAGE] 大批量提取未达量，下一轮交给 next_page 宏动作：{_force_probe_reason}",
                                            level="info",
                                        )
                                        _xs.pagination_hint_msg = (
                                            "📍【系统探测：本页**无分页器**（infinite 模式）】\n"
                                            "当前提取批次已足够大但目标未达成。"
                                            "**输出 next_page**，引擎层会自动走 L4 瀑布流兜底（smooth_scroll）"
                                            "加载新数据。next_page 是万能翻页动作，不需要你判断模式。"
                                        )
                                    else:
                                        _xs.pagination_hint_msg = (
                                            "📍【系统探测：本页**无分页器**】\n"
                                            f"本次仅新增 {_new_rows} 条（{_force_probe_reason}），"
                                            "请继续 smooth_scroll / extract 当前列表；"
                                            "如果滚动触底且仍未达量，引擎会再调度 next_page 宏动作。"
                                        )
                                logger.info(f"[PROBE PAGE] kind={_xs.pagination_kind} cands={len(_cands)}")
                            except Exception as _probe_err:
                                logger.warning(f"[PROBE PAGE] 失败忽略：{_probe_err}")
                        # ── Re-arm：已知分页器 + 目标未达 → 每次 extract 后强制 next_page ──
                        # 首次探测一次性完成（_xs.pagination_probed=True），但后续 extract
                        # 仍需引擎兜底翻页，避免 VLM 自行翻页出错浪费步数。
                        if (
                            _xs.pagination_probed
                            and _xs.pagination_kind not in ("", "infinite")
                            and not _xs.page_is_infinite_scroll
                            and not _xs.force_next_page_pending
                        ):
                            _rearm_target = _parse_goal_target_count(goal)
                            if _rearm_target is not None and _xs.total_extracted_rows < _rearm_target:
                                _xs.force_next_page_pending = True
                                logger.info(
                                    "[REARM NEXT_PAGE] paginator known (%s), target not met "
                                    "(%s/%s); re-armed for next step",
                                    _xs.pagination_kind,
                                    _xs.total_extracted_rows,
                                    _rearm_target,
                                )
                        # ── Path C：Hard Kill — 引擎层强杀，达量直接终止主循环 ──
                        # 不再注入提示让 VLM 决策，避免它走神或重提取浪费步数。
                        _hk_target = _parse_goal_target_count(goal)
                        if _hk_target is not None and _xs.total_extracted_rows >= _hk_target:
                            logger.info(
                                f"[HARD KILL] 引擎达量终止：累计 "
                                f"{_xs.total_extracted_rows} >= 目标 {_hk_target} 条"
                            )
                            print(
                                f"\033[1;32m🎯 [HARD KILL]\033[0m 已达量 "
                                f"{_xs.total_extracted_rows}/{_hk_target}，引擎层终止任务"
                            )
                            _broadcast_log_safe(
                                f"[HARD KILL] 累计 {_xs.total_extracted_rows}/{_hk_target} 条达量，引擎终止"
                            )
                            _task_completed = True
                            _run_succeeded = True
                            break  # 退出动作循环，主循环检测 _task_completed 退出
                        if _goal_output_mode == "answer":
                            logger.info(
                                "[EXTRACT AUTO] Answer-mode rows kept in run result "
                                "(累计 %s 条)",
                                _xs.total_extracted_rows,
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
                                f"(累计 {_xs.total_extracted_rows} 条)"
                            )
                            print(
                                f"\033[1;33m⚡ [EXTRACT AUTO]\033[0m "
                                f"VLM 未填充数据，系统已从 {_auto_extract_text_source} 全页提取 "
                                f"\033[36m{_new_rows}\033[0m 条数据。"
                                f"当前总计: \033[36m{_xs.total_extracted_rows}\033[0m 条"
                            )
                        # 将降级的 wait 动作替换为已完成的 extract
                        # 不需要执行内层动作循环，直接跳到翻页引导
                        decisions = _log_decision
                    except Exception as _auto_err:
                        logger.error(
                            f"[EXTRACT AUTO] 自动提取失败: {_auto_err}"
                        )
                    # 无论是否成功，注入智能翻页/结束引导
                    _auto_pages = len(_xs.extracted_page_urls)
                    _target_count = _parse_goal_target_count(goal)
                    _reached_target = (
                        _target_count is not None
                        and _xs.total_extracted_rows >= _target_count
                    )
                    if _reached_target:
                        # 已达到用户指定的目标数量，强烈建议 done
                        vlm.inject_error_feedback(
                            f"✅ 系统已自动提取当前页数据。"
                            f"你已经成功提取了 {_auto_pages} 个不同页面的数据"
                            f"（累计 {_xs.total_extracted_rows} 条）。\n"
                            f"用户要求获取 {_target_count} 条数据，"
                            f"当前已累计 {_xs.total_extracted_rows} 条，"
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
                                f"（累计 {_xs.total_extracted_rows} 条），"
                                f"但用户要求 {_target_count} 条，"
                                f"还差 {_target_count - _xs.total_extracted_rows} 条。\n"
                                f"系统发现了翻页链接：\n{_pag_hint}\n"
                                f"【立即操作】请继续点击翻页链接加载下一页，例如："
                                f"click(target_id={_pag_links[0]['id']})\n"
                                f"⚠️ 必须使用上述精确的 ID！"
                            )
                        else:
                            vlm.inject_error_feedback(
                                f"✅ 系统已自动提取当前页数据。"
                                f"已提取 {_auto_pages} 个页面"
                                f"（累计 {_xs.total_extracted_rows} 条），"
                                f"但用户要求 {_target_count} 条，"
                                f"还差 {_target_count - _xs.total_extracted_rows} 条。\n"
                                "请向下滚动查找翻页按钮后继续翻页提取。"
                            )
                    elif _auto_pages >= 2:
                        # 不确定目标数量，让 VLM 自行判断
                        vlm.inject_error_feedback(
                            f"✅ 系统已自动提取当前页数据。"
                            f"你已经成功提取了 {_auto_pages} 个不同页面的数据"
                            f"（累计 {_xs.total_extracted_rows} 条）。\n"
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
                                f"累计 {_xs.total_extracted_rows} 条）。\n"
                                f"系统在当前页面发现了以下翻页链接：\n{_pag_hint}\n"
                                f"【立即操作】请点击翻页链接加载下一页，例如："
                                f"click(target_id={_pag_links[0]['id']})\n"
                                f"⚠️ 必须使用上述精确的 ID，不要猜测其他 ID！"
                            )
                        else:
                            vlm.inject_error_feedback(
                                f"✅ 系统已自动提取当前页数据"
                                f"（已提取 {_auto_pages} 个页面，"
                                f"累计 {_xs.total_extracted_rows} 条）。\n"
                                "当前页面未发现翻页链接，可能已是最后一页。\n"
                                "如果任务还需要更多数据，请尝试向下滚动查找翻页按钮。\n"
                                "如果已完成所有页的提取，请直接输出 done 结束任务。"
                            )
                    # 跳过内层动作循环（因为 extract 已自动完成）
                    # 但如果连续太多次自动提取同一页面，强制结束
                    if _xs.extract_null_streak >= 2:  # lowered from 3
                        # Check if we should retry instead of terminating
                        _should_terminate = True
                        if (
                            _goal_target is not None
                            and _xs.total_extracted_rows < _goal_target
                            and _xs.extract_null_total_resets < 5  # raised from 3
                        ):
                            _should_terminate = False
                            _xs.extract_null_total_resets += 1
                            _scroll_escalation = {1: 1500, 2: 3000, 3: 4000, 4: 5000, 5: 5000}
                            _scroll_amount = _scroll_escalation.get(_xs.extract_null_total_resets, 5000)
                            logger.warning(
                                f"[EXTRACT AUTO] streak={_xs.extract_null_streak} 但进度 "
                                f"{_xs.total_extracted_rows}/{_goal_target}，"
                                f"递增滚动 {_scroll_amount}px (reset #{_xs.extract_null_total_resets}/5)"
                            )
                            _content_changed = await _nudge_scroll_after_duplicate_extract(
                                f"extract_null_streak={_xs.extract_null_streak}, reset #{_xs.extract_null_total_resets}",
                                scroll_amount=_scroll_amount
                            )
                            if not _content_changed:
                                logger.warning(
                                    f"[EXTRACT AUTO] 滚动后内容未变化，下次重试将使用更大滚动量"
                                )
                            _xs.extract_null_streak = 0
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
                    if _xs.extract_null_streak > 0:
                        logger.info(
                            f"[EXTRACT AUTO] extract+null 连击已中断 "
                            f"(was {_xs.extract_null_streak})"
                        )
                    _xs.extract_null_streak = 0

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
                # G4: delegate action sig recording
                _post_decision_guards.record_action_sig(decisions)
                if decisions:
                    _hd_final = decisions[0]
                    _prev_action_sig = _post_decision_guards.prev_action_sig

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
                                total_rows=_xs.total_extracted_rows,
                                total_pages=len(_xs.extracted_page_keys),
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
                        _hitl_page = None
                        try:
                            _hitl_page = await browser._ensure_active_page(reason="hitl form proxy")
                        except Exception:
                            pass
                        await _wait_for_human_resume(_hitl_reason or "manual intervention required", page=_hitl_page)
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
                        and _xs.total_extracted_rows < _extract_goal_target
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
                                                "rows": _xs.total_extracted_rows,
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
                            and _xs.total_extracted_rows < _extract_goal_target
                        )
                        _need_more_pages = (
                            _extract_goal_pages is not None
                            and len(_xs.extracted_page_keys) < _extract_goal_pages
                        )
                        if _need_more_rows or _need_more_pages:
                            if decision.get("extracted_data"):
                                logger.warning(
                                    "[DONE GUARD] Premature done with extracted_data before extraction "
                                    "target met; downgrading to extract "
                                    "(rows=%s/%s, pages=%s/%s)",
                                    _xs.total_extracted_rows,
                                    _extract_goal_target,
                                    len(_xs.extracted_page_keys),
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
                                        f"系统记账仅 {_xs.total_extracted_rows}/{_extract_goal_target} 条"
                                    )
                                if _need_more_pages and _extract_goal_pages is not None:
                                    _remaining_parts.append(
                                        f"仅完成 {len(_xs.extracted_page_keys)}/{_extract_goal_pages} 页"
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
                        if _current_url in _xs.extracted_page_urls:
                            logger.info(
                                "[EXTRACT] URL already seen; row-level dedup will decide: %s",
                                _current_url,
                            )

                        if extracted:
                            _log_extract_text_source = "VLM_EXTRACT_OUTPUT"
                            _source_text_for_validation = ""
                            _data_shape = await _extract_rt.compute_data_shape_with_drain(
                                "explicit extract dense-shape drain override"
                            )

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
                                    and _xs.total_extracted_rows >= _target_count_pre
                                )
                                if _pre_reached:
                                    vlm.inject_error_feedback(
                                        f"✅ 你已累计提取 {_xs.total_extracted_rows} 条数据，"
                                        f"已达成用户要求的 {_target_count_pre} 条。"
                                        "请立即输出 action=done 结束任务，不要再 extract。"
                                    )
                                else:
                                    _has_prior_extract_page = bool(_xs.extracted_page_urls or _xs.extracted_page_keys)
                                    if _target_count_pre is not None and _has_prior_extract_page:
                                        _duplicate_zero_extract_streak += 1
                                        _scroll_drain = await _probe_scroll_drain_state(
                                            "duplicate extract drain probe"
                                        )
                                        _physically_drained = bool(_scroll_drain.get("at_bottom"))
                                        _probe_failed = bool(_scroll_drain.get("probe_failed"))
                                        if _physically_drained or (_probe_failed and _duplicate_zero_extract_streak >= 3):
                                            _xs.first_flip_pending = True
                                            _drain_reason = (
                                                "物理触底"
                                                if _physically_drained
                                                else "触底探测失败且连续多次无新增"
                                            )
                                            vlm.inject_error_feedback(
                                                f"⚠️ 系统 extract 净新增为 0，且已确认{_drain_reason}。\n"
                                                f"当前累计 {_xs.total_extracted_rows}/{_target_count_pre} 条，"
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
                                                f"当前累计 {_xs.total_extracted_rows}/{_target_count_pre} 条，"
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
                                            _xs.block_next_page_until_drained = True
                                            _xs.block_next_page_reason = (
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
                                _progress_total_rows = _xs.total_extracted_rows
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
                            _xs.extract_count += 1
                            _xs.extracted_page_urls.add(_current_url)
                            _xs.extracted_page_keys.add(_current_extract_page_key)
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
                            # Bug 修复：用任务级永久锁，避免翻页后 _xs.extract_count 重置反复触发
                            if (
                                not _xs.first_extract_ever_done
                                and _should_force_first_flip_after_successful_extract(goal)
                            ):
                                _xs.first_extract_ever_done = True
                                _xs.first_flip_pending = True
                            # ── Improvement 1：首次 extract 后探测分页器（显式 extract 路径） ──
                            if (
                                _goal_needs_pagination_probe(goal)
                                and not _xs.pagination_probed
                                and _xs.extract_count == 1
                            ):
                                _xs.pagination_probed = True
                                try:
                                    _probe = await browser.probe_pagination()
                                    _xs.pagination_kind = _probe.get("kind", "")
                                    _cands = _probe.get("candidates", [])
                                    if _probe.get("has_paginator"):
                                        _names = ", ".join(
                                            f"{c['ref']}={c['name']!r}" for c in _cands[:6]
                                        )
                                        _should_force_probe_next, _force_probe_reason = (
                                            _should_schedule_next_page_after_extract(
                                                goal,
                                                new_rows=_new_rows,
                                                total_rows=_xs.total_extracted_rows,
                                                extract_source=_log_extract_text_source,
                                                pagination_kind=_xs.pagination_kind,
                                                expected_rows=_expected_rows_from_data_shape(_data_shape),
                                                physically_drained=bool(_data_shape.get("physically_drained")),
                                            )
                                        )
                                        if _should_force_probe_next:
                                            _xs.force_next_page_pending = True
                                            _xs.first_flip_pending = False
                                            _xs.block_next_page_until_drained = False
                                            _xs.block_next_page_reason = ""
                                            logger.info(
                                                "[PROBE PAGE] armed next_page after extract: %s",
                                                _force_probe_reason,
                                            )
                                            _broadcast_log_safe(
                                                f"[PROBE PAGE] 已发现分页器，下一轮直接 next_page：{_force_probe_reason}",
                                                level="info",
                                            )
                                            _xs.pagination_hint_msg = (
                                                f"📍【系统探测：本页**带分页器**（{_xs.pagination_kind}）】\n"
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
                                                _xs.block_next_page_until_drained = True
                                                _xs.block_next_page_reason = _force_probe_reason
                                            else:
                                                _xs.force_next_page_pending = True
                                                _xs.first_flip_pending = False
                                                _xs.block_next_page_until_drained = False
                                                _xs.block_next_page_reason = ""
                                                _force_probe_reason = (
                                                    f"{_force_probe_reason}; physical bottom reached"
                                                )
                                            _xs.pagination_hint_msg = (
                                                f"📍【系统探测：本页**带分页器**（{_xs.pagination_kind}）】\n"
                                                f"已确认页面存在翻页控件：{_names}。\n"
                                                f"但本次仅新增 {_new_rows} 条（{_force_probe_reason}），"
                                                "不足以证明当前页已提取完。\n"
                                                "下一步请先 smooth_scroll 向下并继续 extract 当前页；"
                                                "只有当前页物理触底或无新增后，才使用 next_page。"
                                            )
                                    else:
                                        # No paginator detected — mark as infinite scroll
                                        _xs.page_is_infinite_scroll = True
                                        _should_force_probe_next, _force_probe_reason = (
                                            _should_schedule_next_page_after_extract(
                                                goal,
                                                new_rows=_new_rows,
                                                total_rows=_xs.total_extracted_rows,
                                                extract_source=_log_extract_text_source,
                                                pagination_kind=_xs.pagination_kind,
                                                expected_rows=_expected_rows_from_data_shape(_data_shape),
                                                physically_drained=bool(_data_shape.get("physically_drained")),
                                            )
                                        )
                                        if _should_force_probe_next:
                                            _xs.force_next_page_pending = True
                                            _xs.first_flip_pending = False
                                            _xs.block_next_page_until_drained = False
                                            _xs.block_next_page_reason = ""
                                            logger.info(
                                                "[PROBE PAGE] armed universal next_page after extract: %s",
                                                _force_probe_reason,
                                            )
                                            _broadcast_log_safe(
                                                f"[PROBE PAGE] 大批量提取未达量，下一轮交给 next_page 宏动作：{_force_probe_reason}",
                                                level="info",
                                            )
                                            _xs.pagination_hint_msg = (
                                                "📍【系统探测：本页**无分页器**（infinite 模式）】\n"
                                                "当前提取批次已足够大但目标未达成。"
                                                "下一步使用 next_page 宏动作；如果确实没有分页器，"
                                                "底层会自动走 L4 滚动兜底加载新数据。"
                                            )
                                        else:
                                            _xs.pagination_hint_msg = (
                                                "📍【系统探测：本页**无分页器**】\n"
                                                f"本次仅新增 {_new_rows} 条（{_force_probe_reason}），"
                                                "请继续 smooth_scroll / extract 当前列表；"
                                                "如果滚动触底且仍未达量，引擎会再调度 next_page 宏动作。"
                                            )
                                    logger.info(f"[PROBE PAGE] kind={_xs.pagination_kind} cands={len(_cands)}")
                                except Exception as _probe_err:
                                    logger.warning(f"[PROBE PAGE] 失败忽略：{_probe_err}")
                            # ── Re-arm：已知分页器 + 目标未达 → 每次 extract 后强制 next_page ──
                            if (
                                _xs.pagination_probed
                                and _xs.pagination_kind not in ("", "infinite")
                                and not _xs.page_is_infinite_scroll
                                and not _xs.force_next_page_pending
                            ):
                                _rearm_target = _parse_goal_target_count(goal)
                                if _rearm_target is not None and _xs.total_extracted_rows < _rearm_target:
                                    _xs.force_next_page_pending = True
                                    logger.info(
                                        "[REARM NEXT_PAGE] paginator known (%s), target not met "
                                        "(%s/%s); re-armed for next step",
                                        _xs.pagination_kind,
                                        _xs.total_extracted_rows,
                                        _rearm_target,
                                    )
                            # ── Path C：Hard Kill 引擎层强杀（同上）──
                            _hk_target = _parse_goal_target_count(goal)
                            if _hk_target is not None and _xs.total_extracted_rows >= _hk_target:
                                logger.info(
                                    f"[HARD KILL] 引擎达量终止：累计 "
                                    f"{_xs.total_extracted_rows} >= 目标 {_hk_target} 条"
                                )
                                print(
                                    f"\033[1;32m🎯 [HARD KILL]\033[0m 已达量 "
                                    f"{_xs.total_extracted_rows}/{_hk_target}，引擎层终止任务"
                                )
                                _broadcast_log_safe(
                                    f"[HARD KILL] 累计 {_xs.total_extracted_rows}/{_hk_target} 条达量，引擎终止"
                                )
                                _task_completed = True
                                _run_succeeded = True
                                break
                            _hk_pages = _parse_goal_target_pages(goal)
                            if _hk_pages is not None and len(_xs.extracted_page_keys) >= _hk_pages:
                                logger.info(
                                    "[HARD KILL] 表格页数达标：%s/%s pages, rows=%s",
                                    len(_xs.extracted_page_keys),
                                    _hk_pages,
                                    _xs.total_extracted_rows,
                                )
                                _broadcast_log_safe(
                                    f"[HARD KILL] 已提取 {len(_xs.extracted_page_keys)}/{_hk_pages} 页，"
                                    f"累计 {_xs.total_extracted_rows} 条，任务完成"
                                )
                                _task_completed = True
                                _run_succeeded = True
                                break

                            _need_more_table_pages = (
                                _log_extract_text_source == "DOM_TABLE"
                                and (
                                    (_hk_target is not None and _xs.total_extracted_rows < _hk_target)
                                    or (_hk_pages is not None and len(_xs.extracted_page_keys) < _hk_pages)
                                )
                            )
                            if _need_more_table_pages:
                                _advanced = await _auto_advance_table_page_via_dom(
                                    "advance after successful table extract"
                                )
                                if _advanced:
                                    _xs.extract_count = 0
                                    vlm.inject_error_feedback(
                                        f"✅ 系统已保存当前表格页 {_new_rows} 条，"
                                        f"累计 {_xs.total_extracted_rows} 条。"
                                        "底层已自动点击下一页，下一步请直接执行 extract，"
                                        "不要回到上一页，也不要重复提取刚才的数据。"
                                    )
                                    break
                        else:
                            logger.warning("[EXTRACT] No extracted_data in VLM response")

                        # ── 智能翻页/结束引导（根据已提取页数 + 目标数量决定建议） ──
                        _n_pages = max(len(_xs.extracted_page_urls), len(_xs.extracted_page_keys))
                        _target_count_b = _parse_goal_target_count(goal)
                        _reached_target_b = (
                            _target_count_b is not None
                            and _xs.total_extracted_rows >= _target_count_b
                        )
                        if _reached_target_b:
                            vlm.inject_error_feedback(
                                f"✅ 你已成功提取 {_n_pages} 个不同页面的数据"
                                f"（累计 {_xs.total_extracted_rows} 条）。\n"
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
                                    f"（累计 {_xs.total_extracted_rows} 条），"
                                    f"但用户要求 {_target_count_b} 条，"
                                    f"还差 {_target_count_b - _xs.total_extracted_rows} 条。\n"
                                    f"系统发现了翻页链接：\n{_pag_hint_c}\n"
                                    f"【立即操作】请继续翻页，例如："
                                    f"click(target_id={_pag_links_c[0]['id']})"
                                )
                            else:
                                vlm.inject_error_feedback(
                                    f"✅ 你已成功提取 {_n_pages} 个页面"
                                    f"（累计 {_xs.total_extracted_rows} 条），"
                                    f"但用户要求 {_target_count_b} 条，"
                                    f"还差 {_target_count_b - _xs.total_extracted_rows} 条。\n"
                                    "请向下滚动查找翻页按钮后继续翻页提取。"
                                )
                        elif _n_pages >= 2:
                            vlm.inject_error_feedback(
                                f"✅ 你已成功提取 {_n_pages} 个不同页面的数据"
                                f"（累计 {_xs.total_extracted_rows} 条）。\n"
                                "请仔细回顾用户的原始任务要求，"
                                "判断是否需要继续翻页提取更多数据。\n"
                                "如果已满足用户需求，请输出 done 结束任务。"
                            )
                        elif _xs.extract_count > 0:
                            _pag_links_b = browser.find_pagination_links()
                            if _pag_links_b:
                                _pag_hint_b = "\n".join(
                                    f"  → [ID: {p['id']}] {p['role']}: \"{p['name']}\""
                                    for p in _pag_links_b
                                )
                                vlm.inject_error_feedback(
                                    f"✅ 你已成功提取当前页数据"
                                    f"（第 {_n_pages} 个页面，累计 {_xs.total_extracted_rows} 条）。\n"
                                    f"系统在当前页面发现了以下翻页链接：\n{_pag_hint_b}\n"
                                    f"【立即操作】请点击翻页链接加载下一页，例如："
                                    f"click(target_id={_pag_links_b[0]['id']})\n"
                                    f"⚠️ 必须使用上述精确的 ID，不要猜测其他 ID！"
                                )
                            else:
                                vlm.inject_error_feedback(
                                    f"✅ 你已成功提取当前页数据"
                                    f"（第 {_n_pages} 个页面，累计 {_xs.total_extracted_rows} 条）。\n"
                                    "当前页面未发现翻页链接，可能已是最后一页。\n"
                                    "如果任务还需要更多数据，请尝试向下滚动查找翻页按钮。\n"
                                    "如果已完成所有页的提取，请直接输出 done 结束任务。"
                                )

                        # 连续 extract 守卫（防止 VLM 不翻页也不 done 陷入死循环）
                        if _xs.extract_count >= 3:
                            _guard_target = _parse_goal_target_count(goal)
                            if _guard_target is not None and _xs.total_extracted_rows < _guard_target:
                                logger.warning(
                                    "[EXTRACT GUARD] consecutive extract threshold reached, "
                                    "but target is not met (%s/%s); force navigation instead of ending.",
                                    _xs.total_extracted_rows,
                                    _guard_target,
                                )
                                vlm.inject_error_feedback(
                                    f"⚠️ 系统检测到连续 extract 次数过多，但当前只提取 "
                                    f"{_xs.total_extracted_rows}/{_guard_target} 条，尚未达标。\n"
                                    "下一步禁止继续 extract；必须先执行 next_page、click_text 页码/Next，"
                                    "或 smooth_scroll down 加载更多真实数据。"
                                )
                                _xs.extract_count = 0
                                break
                            logger.warning(
                                "[EXTRACT GUARD] 连续 extract 无翻页动作，"
                                f"已累积 {_xs.total_extracted_rows} 条数据，强制结束任务。"
                            )
                            _run_succeeded = True
                            _task_completed = True
                            # ── PROGRESS SAFEGUARD ──
                            _pg_target = _parse_goal_target_count(goal)
                            if _pg_target is not None and _xs.total_extracted_rows < _pg_target:
                                logger.warning(
                                    f"[PROGRESS SAFEGUARD] 终止时进度不足: {_xs.total_extracted_rows}/{_pg_target}，标记为失败"
                                )
                                _run_succeeded = False
                            _extract_guard_done_state = _extraction_targets_reached(
                                goal,
                                total_rows=_xs.total_extracted_rows,
                                total_pages=len(_xs.extracted_page_keys),
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
                                    "extract_count": _xs.extract_count,
                                    "total_rows": _xs.total_extracted_rows,
                                    "total_pages": len(_xs.extracted_page_keys),
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
                            _xs.extract_count = 0

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
                                not in _xs.tooltip_trigger_keys
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
                        if _pg_target is not None and _xs.total_extracted_rows < _pg_target:
                            logger.warning(
                                f"[PROGRESS SAFEGUARD] 终止时进度不足: {_xs.total_extracted_rows}/{_pg_target}，标记为失败"
                            )
                            _run_succeeded = False
                        _done_target_state = _extraction_targets_reached(
                            goal,
                            total_rows=_xs.total_extracted_rows,
                            total_pages=len(_xs.extracted_page_keys),
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
                                "total_rows": _xs.total_extracted_rows,
                                "total_pages": len(_xs.extracted_page_keys),
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
                        if _hover_key_probe and _hover_key_probe in _xs.tooltip_trigger_keys:
                            _expected_tooltips = _parse_goal_tooltip_targets(goal)
                            _remaining_tooltips = [
                                _title_tooltip_target(label)
                                for label in _expected_tooltips
                                if extract_tooltip_primary_key(
                                    {"direction": _title_tooltip_target(label)}
                                )
                                not in _xs.tooltip_trigger_keys
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
                        and _xs.total_extracted_rows < _extract_goal_target
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
                                f"Progress is {_xs.total_extracted_rows}/{_extract_goal_target}; "
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
                                        "rows": _xs.total_extracted_rows,
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
                                if _nav_target is None or _xs.total_extracted_rows < _nav_target:
                                    _xs.force_extract_after_navigation_pending = True
                                    _xs.force_next_page_pending = False
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
                                        _xs.total_extracted_rows,
                                        _nav_target if _nav_target is not None else "?",
                                    )

                        if (
                            action in ("click", "click_text", "click_point")
                            and _pre_click_is_pagination_candidate
                        ):
                            _nav_target = _parse_goal_target_count(goal)
                            _nav_pages = _parse_goal_target_pages(goal)
                            if (
                                (_nav_target is None or _xs.total_extracted_rows < _nav_target)
                                or (
                                    _nav_pages is not None
                                    and len(_xs.extracted_page_keys) < _nav_pages
                                )
                            ):
                                _xs.force_extract_after_navigation_pending = True
                                _xs.force_next_page_pending = False
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
                                    _xs.total_extracted_rows,
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
                                    row_count=_xs.total_extracted_rows,
                                    url=_landing_key_url,
                                )
                                if _np_state.get("pagination_exhausted"):
                                    _xs.pagination_exhausted = True
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
                                1 if _tooltip_key and _tooltip_key not in _xs.tooltip_trigger_keys
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
                                not in _xs.tooltip_trigger_keys
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
                            if _pg_target is not None and _xs.total_extracted_rows < _pg_target:
                                logger.warning(
                                    f"[PROGRESS SAFEGUARD] 终止时进度不足: {_xs.total_extracted_rows}/{_pg_target}，标记为失败"
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
                            if _pg_target is not None and _xs.total_extracted_rows < _pg_target:
                                logger.warning(
                                    f"[PROGRESS SAFEGUARD] 终止时进度不足: {_xs.total_extracted_rows}/{_pg_target}，标记为失败"
                                )
                                _run_succeeded = False
                            _hover_done_state = _extraction_targets_reached(
                                goal,
                                total_rows=_xs.total_extracted_rows,
                                total_pages=len(_xs.extracted_page_keys),
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
                                    "total_rows": _xs.total_extracted_rows,
                                    "total_pages": len(_xs.extracted_page_keys),
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
                            and _xs.total_extracted_rows < _target_after_scroll_fail
                        )
                        if _scroll_down_failed_at_bottom and _scroll_bottom_target_unmet:
                            _xs.pagination_exhausted = True
                            _xs.force_next_page_pending = True
                            _xs.first_flip_pending = False
                            vlm.inject_error_feedback(
                                "⚠️ 底层已确认页面/主滚动区域向下滚动到达底部，"
                                f"但当前仅累计 {_xs.total_extracted_rows}/{_target_after_scroll_fail} 条。\n"
                                "下一轮系统将强制执行 next_page（target_id=0, type_value=\"\"），"
                                "优先尝试 URL 变异、分页器和页码探测；不要继续 smooth_scroll。"
                            )
                            logger.info(
                                "[FORCE NEXT_PAGE] armed after bottom scroll failure: "
                                "%s/%s rows",
                                _xs.total_extracted_rows,
                                _target_after_scroll_fail,
                            )
                            # Check close-enough with tolerance
                            _tolerance = min(5, max(1, int(_target_after_scroll_fail * 0.05)))
                            if _xs.total_extracted_rows >= _target_after_scroll_fail - _tolerance:
                                logger.info(
                                    f"[CLOSE ENOUGH] scroll 到底且进度 {_xs.total_extracted_rows}/{_target_after_scroll_fail} "
                                    f"在容差 {_tolerance} 内，标记完成"
                                )
                                _extraction_complete = True
                                _xs.pagination_exhausted = True

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
                            if not _xs.force_next_page_pending:
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
                        item_count=_xs.total_extracted_rows,
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
        # G5: delegate run finalization to phases/finalization.py
        try:
            from visual_web_agent.phases.finalization import finalize_run as _finalize_run
            await _finalize_run(
                run_ckpt=locals().get('_run_ckpt'),
                succeeded=_run_succeeded,
                goal=goal,
                start_url=start_url,
                run_ts=_run_ts,
                total_extracted_rows=locals().get('_xs.total_extracted_rows', 0),
                run_constraints=locals().get('run_constraints'),
                event_stream=event_stream,
                html_logger=html_logger,
                output_mode=locals().get('_goal_output_mode', 'default'),
                vlm_output=locals().get('_vlm_output', ''),
                xhr_output=locals().get('_xhr_output', ''),
                enable_xhr=locals().get('enable_xhr', False),
                xhr_pattern=locals().get('xhr_pattern', ''),
                resolve_artifact_path=resolve_artifact_path,
                registry_record_owned=locals().get('_registry_record_owned', False),
                stop_event=locals().get('stop_event'),
                browser_lease=browser_lease,
                release_browser=release_browser,
                session_router=locals().get('_session_router'),
                xsys_enabled=locals().get('_xsys_enabled'),
                complete_run_registry=locals().get('_complete_owned_run_registry_record'),
            )
        except Exception as _fin_err:
            logger.debug('[FINALIZATION] delegation skipped: %s', _fin_err)
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
                    item_count=_xs.total_extracted_rows,
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

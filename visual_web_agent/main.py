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
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_APP_ROOT = Path(__file__).resolve().parent
for _dotenv_path in (_PROJECT_ROOT / ".env", _APP_ROOT / ".env"):
    if _dotenv_path.exists():
        load_dotenv(dotenv_path=_dotenv_path, override=False)

try:
    from .config import MAX_STEPS, SCREENSHOT_DIR
    from .browser_env import BrowserEnv, ActionExecutionError
    from .vlm_client import VLMClient, TaskPlan, SubGoal
    from .data_manager import save_to_excel
    from .data_sanitizer import (
        TOOLTIP_UNIQUE_KEY,
        extract_tooltip_primary_key,
        sanitize_extracted_rows,
    )
    from .artifact_manager import resolve_artifact_path
    from .trajectory_logger import HtmlLogger
    from .auth_vault import SecretResolutionError, resolve_env_placeholders
    from .page_data_controller import PageDataController
except ImportError:
    from config import MAX_STEPS, SCREENSHOT_DIR
    from browser_env import BrowserEnv, ActionExecutionError
    from vlm_client import VLMClient, TaskPlan, SubGoal
    from data_manager import save_to_excel
    from data_sanitizer import (
        TOOLTIP_UNIQUE_KEY,
        extract_tooltip_primary_key,
        sanitize_extracted_rows,
    )
    from artifact_manager import resolve_artifact_path
    from trajectory_logger import HtmlLogger
    from auth_vault import SecretResolutionError, resolve_env_placeholders
    from page_data_controller import PageDataController

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


def _broadcast_done_safe(success: bool, message: str = "") -> None:
    """向 API WebSocket 广播任务结束信号；未运行 API 时静默降级。"""
    try:
        from api_server import broadcast_done

        broadcast_done(success, message)
    except Exception:
        pass


async def _wait_for_human_resume(reason: str = "") -> None:
    try:
        from api_server import broadcast_human_intervention, wait_for_human_resume

        if broadcast_human_intervention(reason):
            await wait_for_human_resume()
            return
    except Exception:
        pass

    await asyncio.get_event_loop().run_in_executor(
        None,
        input,
        "\n👉 请在弹出的浏览器窗口中手动完成操作（滑块验证 / 扫码登录 / 手动填写等）。\n"
        "✅ 操作完成后，在此终端按下 [回车键] 以恢复自动化流程...\n",
    )


_RPA_CACHE_DIR = Path(__file__).parent / "rpa_cache"


def _rpa_cache_path(url: str, goal: str) -> Path:
    """根据 url + goal 生成稳定的缓存文件路径（MD5 哈希命名）。"""
    key = hashlib.md5(f"{url}||{goal}".encode("utf-8")).hexdigest()
    return _RPA_CACHE_DIR / f"{key}.json"


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
    # 量词主集 + 扩展（覆盖 "部电影 / 的回答 / 家店铺" 这类语义量词）
    _QUANT = r'条|个|项|篇|则|部|家|名|位|款|本|场|首'
    m = re.search(rf'(?:前|共|取|抓)\s*(\d+)\s*(?:{_QUANT})', goal)
    if m:
        return int(m.group(1))
    m = re.search(
        rf'(\d+)\s*(?:{_QUANT})\s*(?:数据|内容|信息|记录|新闻|商品|评论|电影|答案|回答|文章|视频|结果|店铺)',
        goal,
    )
    if m:
        return int(m.group(1))
    # "排名前 N 的 / 前 N 的"：Top-N 语义，无显式量词但意图明确
    m = re.search(r'(?:排名)?前\s*(\d+)\s*(?:的|名)', goal)
    if m:
        return int(m.group(1))
    # "Top N" 英文语义
    m = re.search(r'[Tt]op\s*(\d+)\b', goal)
    if m:
        return int(m.group(1))
    # English bulk extraction wording: "extract 2000 records/items/rows"
    m = re.search(r'(\d+)\s*(?:records?|items?|rows?|entries|results?)\b', goal, re.I)
    if m:
        return int(m.group(1))
    return None


def _parse_goal_target_pages(goal: str) -> int | None:
    """Parse goals such as "前5页" / "提取 3 pages"."""
    m = re.search(r'(?:前|共|取|抓|提取|获取)?\s*(\d+)\s*页', goal)
    if m:
        return int(m.group(1))
    m = re.search(r'(?:first|top|extract|get|scrape)\s*(\d+)\s*pages?\b', goal, re.I)
    if m:
        return int(m.group(1))
    return None


def _normalize_output_field_key(value: object) -> str:
    """Normalize output field names for loose user-goal matching."""
    return re.sub(
        r"[^a-z0-9\u4e00-\u9fff]+",
        "",
        str(value or "").strip().lower(),
    )


def _parse_goal_requested_fields(goal: str) -> list[str]:
    """Parse explicit requested output columns from natural-language goals.

    This intentionally only activates when the user says fields/columns/字段/列,
    so normal goals such as "抓取前 50 条数据" keep the site's natural schema.
    """
    text = _extract_core_goal(goal)
    if not text:
        return []

    patterns = (
        r"(?:字段|列名|列|表头|fields?|columns?)\s*(?:为|是|包括|包含|只要|仅保留|:|：|=)\s*([^。\n；;]+)",
        r"(?:提取|抓取|获取|保存|导出)\s*(?:以下|这些|指定)?\s*(?:字段|列|fields?|columns?)\s*(?:[:：为是=])?\s*([^。\n；;]+)",
        r"(?:with|including|only)\s+(?:fields?|columns?)\s*(?:[:：=])?\s*([^.\n;]+)",
    )
    raw = ""
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            raw = match.group(1)
            break
    if not raw:
        # Also support wording like "提取 Name, Position, Office 字段".
        match = re.search(
            r"(?:提取|抓取|获取|保存|导出)\s+([^。\n；;]{2,160}?)\s*(?:字段|列|fields?\b|columns?\b)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            raw = match.group(1)
    if not raw:
        # Support goals like "前26部电影的标题、评分、评价人数和一句话简介".
        matches = re.findall(
            r"的([^。\n；;]{2,120}?)(?=，?\s*(?:保存|导出|写入|存入|并保存|并导出)|[。\n；;]|$)",
            text,
            flags=re.IGNORECASE,
        )
        field_markers = (
            "标题", "名称", "名字", "评分", "评价", "人数", "简介", "摘要",
            "作者", "时间", "链接", "网址", "title", "name", "rating",
            "score", "review", "summary", "description", "url",
        )
        for candidate in reversed(matches):
            if any(marker.lower() in candidate.lower() for marker in field_markers):
                raw = candidate
                break
    if not raw:
        return []

    raw = re.split(
        r"\s*(?:并(?:保存|导出|写入|存入)?|然后|再|保存到|导出到|写入|存入|to\s+excel|as\s+excel)\s*",
        raw,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    raw = raw.strip(" ：:=[({【\"'`“”‘’")
    raw = raw.strip(" )]}】\"'`“”‘’")
    if not raw:
        return []

    parts = re.split(r"[,，、;；|/]+|\s+(?:and|or)\s+|(?:以及|和|及)", raw)
    fields: list[str] = []
    seen: set[str] = set()
    stop_words = {"数据", "内容", "信息", "记录", "excel", "xlsx", "csv"}
    for part in parts:
        field = re.sub(r"\s+", " ", part).strip(" ：:=[({【\"'`“”‘’)]}】")
        if not field:
            continue
        field = re.sub(r"^(?:and|or|和|及|以及)\s+", "", field, flags=re.IGNORECASE).strip()
        field = re.sub(r"\s+(?:and|or|和|及|以及)$", "", field, flags=re.IGNORECASE).strip()
        norm = _normalize_output_field_key(field)
        if not norm or norm in stop_words or norm in seen:
            continue
        if len(field) > 50:
            continue
        seen.add(norm)
        fields.append(field)
    return fields


def _goal_is_tooltip_extract(goal: str) -> bool:
    """Small hover/tooltip extraction tasks are not bulk pagination jobs."""
    text = str(goal or "").lower()
    return any(
        kw in text
        for kw in (
            "tooltip", "tool tip", "popover", "悬浮", "悬停", "鼠标悬停",
            "提示框", "提示气泡", "黑色提示", "气泡", "浮层提示",
        )
    )


def _goal_is_form_fill(goal: str) -> bool:
    """Whether the goal is an interactive form-filling task."""
    text = str(goal or "").lower()
    if _goal_is_tooltip_extract(text):
        return False
    form_markers = (
        "表单", "填报", "填写", "输入框", "下拉框", "复选框", "单选框",
        "开关", "文本域", "提交", "form", "activity name", "activity zone",
        "basic form", "create",
    )
    return any(marker in text for marker in form_markers)


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
    text = str(goal or "")
    assignments: dict[str, str] = {}
    quote = r"[\"“”'‘’]"
    chunks = re.split(r"[\r\n]+|(?=\s*\d+\s*[.、)]\s*)", text)
    for raw_line in chunks:
        line = raw_line.strip()
        if not line:
            continue
        clean = re.sub(r"^\s*\d+\s*[.、)]\s*", "", line)
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


def _next_month_day(day: int) -> str:
    today = date.today()
    year = today.year + (1 if today.month == 12 else 0)
    month = 1 if today.month == 12 else today.month + 1
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{min(day, last_day):02d}"


def _prepare_form_batch_fields(goal: str) -> dict[str, str]:
    fields = _parse_form_assignments(goal)
    text = str(goal or "")
    if re.search(r"Activity\s*time|活动时间|时间区域", text, re.I):
        m = re.search(r"下个月\s*的?\s*(\d{1,2})\s*号", text)
        day = int(m.group(1)) if m else 0
        if not day:
            chunks = re.split(r"[\r\n]+|(?=\s*\d+\s*[.、)]\s*)", text)
            time_chunk = next((c for c in chunks if re.search(r"Activity\s*time|活动时间|时间区域", c, re.I)), "")
            nums = re.findall(r"\b([1-2]?\d|3[01])\b", time_chunk)
            day = int(nums[-1]) if nums else 0
        if day:
            if 1 <= day <= 31:
                fields["Activity time"] = _next_month_day(day)
    return fields


async def _try_auto_form_fill(browser: "BrowserEnv", goal: str) -> bool:
    """Deterministic label/scoped form executor, used before handing control to VLM."""
    if not _goal_is_form_fill(goal):
        return False
    fields = _prepare_form_batch_fields(goal)
    if len(fields) < 2:
        return False
    scope_title = _parse_goal_scope_title(goal)
    page = await browser._ensure_active_page(reason="before auto form fill")
    if not page:
        return False
    require_submit = bool(
        re.search(
            r"(create|submit|提交|保存|确定|点击.+按钮|按钮)",
            str(goal or ""),
            re.IGNORECASE,
        )
    )
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
                    el.innerText, el.textContent, el.getAttribute('aria-label'),
                    el.getAttribute('placeholder'), el.getAttribute('title'),
                    el.getAttribute('value'), el.name, el.id
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
                            cur.classList?.contains('n-form-item') ||
                            cur.tagName?.toLowerCase() === 'form'
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
                            '.ant-select-item-option','[role=option]','label','li','span','button'
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
                        return {label, ok: checked === shouldOn, mode: 'switch', observed: checked ? 'checked' : 'unchecked'};
                    }

                    const choiceNodes = allVisible('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]', item);
                    const choiceHit = choiceNodes.find(el => norm(textOf(el)).includes(expected));
                    if (choiceHit) {
                        const choiceTarget = choiceHit.closest?.('label,.el-radio,.el-checkbox,[role=radio],[role=checkbox]') || choiceHit;
                        const checked = choiceTarget.matches?.('.is-checked,[aria-checked=true]') ||
                            Boolean(choiceTarget.querySelector?.('.is-checked,[aria-checked=true],input:checked')) ||
                            Boolean(choiceHit.querySelector?.('.is-checked,[aria-checked=true],input:checked'));
                        if (checked) return {label, ok: true, mode: 'choice_checked', observed};
                    }

                    if (expected && observedNorm.includes(expected)) {
                        return {label, ok: true, mode: 'value_visible', observed};
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

                    const selectRoot = allVisible('.el-select,.ant-select,.n-select,[role=combobox]', item)[0];
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
                    const isChoiceValue = /(zone|type|resource|delivery|date|time|下拉|选择|复选|单选|开关|日期|时间)/i.test(label);
                    if ((selectRoot || readonly) && isChoiceValue) {
                        clickEl(selectRoot || readonly);
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
                        results.push({label, ok: Boolean(checked) || choiceTarget.tagName === 'BUTTON', mode: 'choice'});
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

                const submit = allVisible('button,.el-button,[role=button]', scope)
                    .find(el => /^(create|submit|提交|保存|确定)$/i.test(textOf(el).trim())) ||
                    allVisible('button,.el-button,[role=button]')
                    .filter(el => el.getBoundingClientRect().left > 180)
                    .find(el => /^(create|submit|提交|保存|确定)$/i.test(textOf(el).trim()));
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
        if isinstance(result, dict) and result.get("ok"):
            print(f"\033[1;32m✅ [AUTO FORM]\033[0m 已按 DOM scope 执行表单填报")
            _broadcast_log_safe("[AUTO FORM] Deterministic scoped form fill executed")
            return True
    except Exception as err:
        logger.warning("[AUTO FORM] deterministic fill failed, fallback to VLM: %s", err)
    return False


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
    某些 prompt 本身就是为了测试异常链路/容错恢复而设计的，
    例如“点击一个绝对不存在的元素”“故意触发错误步骤”等。
    这类任务不应启用肌肉记忆，否则会被旧缓存直接短路，测不到真实恢复逻辑。
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
        return _normalize_rpa_cache_payload(json.loads(path.read_text(encoding="utf-8")))
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
        return {
            "version": 1,
            "replayable": True,
            "reason": "",
            "trail": payload,
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
        return {
            "version": int(payload.get("version", 4) or 4),
            "replayable": bool(payload.get("replayable", True)),
            "reason": str(payload.get("reason", "") or ""),
            "trail": trail,
            "fail_count": int(payload.get("fail_count", 0) or 0),
            "max_failures": int(payload.get("max_failures", 2) or 2),
            "completes_task": bool(payload.get("completes_task", True)),
            "source_url": str(payload.get("source_url", "") or meta["source_url"]),
            "source_goal": str(payload.get("source_goal", "") or meta["source_goal"]),
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


async def _replay_rpa(
    browser: "BrowserEnv",
    trail: list[dict],
    workflow_memory: dict | None = None,
) -> bool:
    """
    极速 RPA 回放：直接用 XPath/坐标执行缓存动作，完全跳过 VLM。

    Returns:
        True  → 全程无报错，任务完成
        False → 任意步骤失败，需降级回 VLM 主循环
    """
    from playwright.async_api import Error as PlaywrightError

    compacted_trail = _compact_rpa_trail(trail)
    if len(compacted_trail) != len(trail):
        logger.info(
            f"[RPA REPLAY] Compacted cached trail: {len(trail)} → {len(compacted_trail)} steps"
        )

    _RPA_TIMEOUT = 5000  # 每步最长等待 5s，防止卡死

    for idx, step in enumerate(compacted_trail):
        act = step.get("action")
        step_label = f"Step {idx + 1}/{len(compacted_trail)} ({act})"
        try:
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
                f"\033[1;31m⚠️  [肌肉记忆]\033[0m "
                f"回放中断于动作 {idx + 1}，原因: {rpa_err}"
            )
            logger.warning(f"[RPA REPLAY] {step_label} FAILED — {type(rpa_err).__name__}: {rpa_err}")
            return False

    return True


async def _replay_ready_rpa_steps(
    browser: "BrowserEnv",
    payload: dict,
    cursor: int,
    workflow_memory: dict | None,
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
    ok = await _replay_rpa(browser, ready_steps, workflow_memory)
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
            _cfg.VLM_SEMANTIC_API_BASE = str(vlm_options["semantic_base_url"])
        if vlm_options.get("semantic_api_key"):
            _cfg.VLM_SEMANTIC_API_KEY = str(vlm_options["semantic_api_key"])
        if vlm_options.get("base_url"):
            _cfg.VLM_API_BASE = str(vlm_options["base_url"])
        if vlm_options.get("api_key"):
            _cfg.VLM_API_KEY = str(vlm_options["api_key"])
        if vlm_options.get("temperature") is not None:
            _cfg.VLM_TEMPERATURE = float(vlm_options["temperature"])
        if vlm_options.get("max_tokens") is not None:
            _cfg.VLM_MAX_TOKENS = int(vlm_options["max_tokens"])
        _cfg.VLM_TEXT_ONLY = str(vlm_options.get("model_type", "")).lower() == "text"

    # 每次运行生成独立的带时间戳文件名，避免多次运行数据混在一起
    _run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _vlm_output = f"output_{_run_ts}.xlsx"       # VLM extract 提取的结果
    _xhr_output = f"xhr_{_run_ts}.xlsx"           # XHR 拦截到的 API 数据（分开存）

    browser = BrowserEnv()
    vlm = VLMClient()
    # HTML 轨迹日志实例化在 try 块外，确保 finally 中的 finalize() 始终可访问
    html_logger = HtmlLogger(goal=goal)
    _run_succeeded = False

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

    try:
        _broadcast_log_safe("VSpider Agent started", level="info")
        _check_stop("before_browser_start")
        # 启动浏览器并导航
        await browser.start(start_url)
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
        )
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

        logger.info(f"{'=' * 60}")
        logger.info(f"[TARGET] {goal}")
        _broadcast_log_safe(f"[TARGET] {goal}")
        logger.info(f"[URL] {start_url}")
        _broadcast_log_safe(f"[URL] {start_url}")
        _effective_max_steps = _derive_effective_max_steps(goal)
        _is_form_fill_goal = _goal_is_form_fill(goal)
        _form_assignments = _prepare_form_batch_fields(goal) if _is_form_fill_goal else {}
        _requested_output_fields = _parse_goal_requested_fields(goal)
        _data_controller = PageDataController(_requested_output_fields)
        if _requested_output_fields:
            logger.info("[EXTRACT SCHEMA] requested fields=%s", _requested_output_fields)
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

        # Component-library forms are more reliable through scoped DOM execution
        # than through step-by-step VLM clicks. The executor honors a title/scope
        # phrase such as "Basic Form 标题下方" before touching fields.
        if _is_form_fill_goal and _form_assignments:
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
                            "thought": "AUTO FORM 已完成字段回读校验，并已点击 Create/Submit（若目标要求提交）。",
                            "type_value": "auto_form",
                        },
                        memory_state=dict(workflow_memory),
                    )
                except Exception as _auto_log_err:
                    logger.debug("[AUTO FORM] failed to log final screenshot: %s", _auto_log_err)
                return True

        # 滑窗：记录最近 6 步的 (action, target_id, landing_url)，用于检测点击死循环
        # 升级为 URL-aware：只有"同一 ID 被点击 ≥ 3 次 **且** 着陆 URL 完全相同"才判定死循环，
        # 翻页 / A-B 循环切换等 URL 在变化的合法重复不再误伤。
        _last_actions: list[tuple[str, int, str]] = []
        _LOOP_GUARD_WINDOW = 6
        _loop_guard_blocked_ids: set[int] = set()  # 触发过 LOOP GUARD 的 target_id，后续直接拦截
        _loop_guard_blocked_points: set[tuple] = set()  # click_point 黑名单（bucket 后的坐标）
        # ── Fix 4：RAW（降级前）VLM 输出 LOOP GUARD ──────────────────────
        # 监测 ZERO_TARGET_DOWNGRADE 之前的原始决策，捕捉 VLM 反复输出
        # `click target_id=0 type_value="2"` 这种 schema 错位幻觉（被 validator
        # 降级为 wait 后老 LOOP GUARD 看不见）。连续 3 次就强制硬指令 + 切走。
        _consecutive_zero_target = 0
        _last_zero_target_tv = ""

        def _norm_url_for_guard(url: str) -> str:
            """LOOP GUARD key 稳定化：只保留 scheme+host+path，抛弃 query/fragment。
            修复 Bug B：JD 异常页等站每次刷新注入 timestamp nonce，
            否则同元素同页 3 次点击会被拆成 3 个不同 key，计数器永远不累积。"""
            if not url:
                return ""
            try:
                p = urlparse(url)
                return f"{p.scheme}://{p.netloc}{p.path}"
            except Exception:
                return url[:120]

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

        # LOOP GUARD 覆盖的动作集（Bug #1 修复）
        _LOOP_GUARD_ACTIONS = (
            "click", "click_new_tab", "click_point", "click_text", "hover_and_click"
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
            if "DOM_LIST" in source:
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
                if "DOM_LIST" in source:
                    return 4
                if "DOM_TABLE" in source:
                    return 3
                if "FULL_PAGE" in source or "LIST_ITEMS_TEXT" in source:
                    return 2
                if "VIEWPORT" in source or "VLM" in source:
                    return 1
                return 0

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

        # ── 自愈计数器 ────────────────────────────────────────────────
        # 连续执行失败超过 _MAX_CONSECUTIVE_ERRORS 次时强制转 ask_human
        _MAX_CONSECUTIVE_ERRORS = 3
        _consecutive_errors = 0

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
        _skip_rpa_for_goal, _skip_rpa_reason = _goal_should_skip_rpa(goal)
        if _skip_rpa_for_goal:
            _rpa_cache_allowed = False
            _rpa_skip_reason = _skip_rpa_reason
            logger.info(
                f"[RPA] Disabled for this run because the goal is intentionally testing "
                f"an invalid/error path: {_rpa_skip_reason}"
            )
            print(
                f"\n\033[1;33m⚡ [RPA]\033[0m 当前任务包含故意注入的错误/无效步骤，"
                f"已自动禁用肌肉记忆回放。\n"
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

        if not _skip_rpa_for_goal:
            if _rpa_exact_path.exists():
                logger.info(f"[RPA] Exact cache hit: {_rpa_exact_path}")
                _rpa_payload = _load_rpa_cache_payload(_rpa_exact_path)
            if _rpa_payload is None:
                _similar_match = _find_similar_rpa_cache(start_url, goal, exclude_path=_rpa_exact_path)
                if _similar_match:
                    _rpa_path, _rpa_payload, _rpa_match_reason = _similar_match
                    logger.info(
                        f"[RPA] Similar cache selected: {_rpa_path} | reason={_rpa_match_reason}"
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
                _match_label = "精确命中" if _rpa_path == _rpa_exact_path else f"近似命中（{_rpa_match_reason}）"
                print(
                    f"\n\033[1;33m⚡ [RPA]\033[0m 发现肌肉记忆缓存: {_rpa_path.name}\n"
                    f"   {_match_label}，将在条件满足时自动尝试极速回放..."
                )
                _new_cursor, _replayed_steps, _replay_failed = await _replay_ready_rpa_steps(
                    browser, _rpa_payload, _rpa_cursor, workflow_memory
                )
                if _replay_failed:
                    logger.warning("[RPA] Startup replay failed, falling back to VLM loop.")
                    _rpa_payload = _mark_rpa_cache_failure(
                        _rpa_path, _rpa_payload, "startup replay failure"
                    )
                else:
                    _rpa_cursor = _new_cursor
                    _executed_cached_trail.extend(_replayed_steps)
                    if (
                        _rpa_payload.get("completes_task", True)
                        and _rpa_cursor >= len(_rpa_payload.get("trail") or [])
                        and _replayed_steps
                    ):
                        print(f"\033[1;32m✅ [RPA]\033[0m 极速回放成功！全程跳过 VLM，任务已完成。")
                        logger.info("[RPA] Replay succeeded — task done without VLM.")
                        _run_succeeded = True
                        return True
        # ──────────────────────────────────────────────────────────────

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

        for step in range(1, _effective_max_steps + 1):
            _check_stop(f"before_step_{step}")
            logger.info(f"\n{'-' * 50}")
            logger.info(f">> Step {step}/{_effective_max_steps}")
            logger.info(f"{'-' * 50}")

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

                if _is_form_fill_goal and _form_assignments and _auto_form_retry_count < 3:
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
                            "thought": "AUTO FORM 已完成字段回读校验，并已点击 Create/Submit（若目标要求提交）。",
                            "type_value": "auto_form",
                        }
                        break

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
                        _new_cursor, _replayed_steps, _replay_failed = await _replay_ready_rpa_steps(
                            browser, _rpa_payload, _rpa_cursor, workflow_memory
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
                                logger.info("[RPA] Partial replay completed the remaining task.")
                                await browser.mark_and_screenshot(step=99)
                                _run_succeeded = True
                                return True
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

                screenshot_b64, input_descriptions = await browser.mark_and_screenshot(step)
                _log_screenshot_path = (
                    str(Path(SCREENSHOT_DIR) / f"step_{step:02d}.png") if screenshot_b64 else None
                )

                try:
                    ax_tree_text = await browser.extract_accessibility_tree()
                except Exception as _ax_err:
                    logger.warning(
                        f"[HYBRID] AX Tree 提取失败，本轮仅凭截图决策: {_ax_err}"
                    )
                    ax_tree_text = ""

                _log_reasoning_text_source = "AX_TREE" if ax_tree_text else "SCREENSHOT_ONLY"

                # 兜底：防止超大 AX Tree 撑爆 Token
                if ax_tree_text and len(ax_tree_text) > 15000:
                    _orig_len = len(ax_tree_text)
                    ax_tree_text = ax_tree_text[:15000] + "\n...[WARNING: AX Tree 过长已截断]..."
                    logger.warning(
                        f"[HYBRID] AX Tree 长度 {_orig_len} 超过 15000 阈值，已截断以保护上下文窗口"
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
                    decisions = await vlm.ask(
                        screenshot_b64,
                        goal,
                        step,
                        input_descriptions,
                        workflow_memory,
                        task_plan=_task_plan,
                        max_steps=_effective_max_steps,
                    )

                _log_decision = decisions

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
                    _allow_done_via_terminal_tail = (
                        _terminal_only_tail and _current_subgoal_complete
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
                    # 允许 done 的条件：已完成子目标数 >= 总数 - 1（仅剩当前 = 最后一个）
                    # 或提取已达量（Fix B）
                    if _extraction_complete:
                        logger.info(
                            f"[PLAN GATE] 放行 done：提取已达量 "
                            f"{_total_extracted_rows}/{_goal_target} 条，跳过门闸"
                        )
                    elif _allow_done_via_terminal_tail:
                        logger.info(
                            "[PLAN GATE] 放行 done：当前子目标已满足退出标准，"
                            "其余仅剩收尾型 done 子目标。"
                        )
                        _task_plan.sub_goals[_cur_idx].status = "done"
                        for _tail_sg in _pending_tail:
                            _tail_sg.status = "done"
                    if (
                        _done_or_failed < _total - 1
                        and not _extraction_complete
                        and not _allow_done_via_terminal_tail
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
                # 末子目标完成但 action!=done → 强制纠偏为 done，贴合 Wave 2 语义。
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
                        elif _head.get("action") != "done":
                            logger.warning(
                                f"[PLAN] 末子目标 completed 但 action={_head.get('action')}，"
                                f"强制纠偏为 done"
                            )
                            _head["action"] = "done"
                            _head["target_id"] = 0
                            _head["type_value"] = ""

                # ── extract+null 即时自动提取 ────────────────────────────
                # 借鉴 browser-use 架构：VLM 只需给出 extract 意图，
                # 数据由系统从 AX Tree 自动提取，不再浪费步数等 VLM 重试。
                _has_extract_downgrade = any(
                    d.get("__extract_downgraded") for d in decisions
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
                        saved_path = save_to_excel(
                            _auto_extracted,
                            _vlm_output,
                            unique_key=TOOLTIP_UNIQUE_KEY if _goal_is_tooltip_extract(goal) else None,
                        )
                        _progress_new_rows, _progress_total_rows = _record_extract_progress(
                            _auto_extracted,
                            _new_rows,
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
                        # 与显式 extract 路径保持一致：含数据提取的轨迹不适合极速回放
                        _rpa_cache_allowed = False
                        _rpa_skip_reason = "contains auto-extract steps"
                        _log_extract_text_source = _auto_extract_text_source
                        _log_decision = [{
                            "action": "extract",
                            "extracted_data": _auto_extracted,
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
                            await asyncio.get_event_loop().run_in_executor(None, input)
                            logger.info("[MANUAL] User confirmed after API failures, resuming...")
                            _consecutive_errors = 0
                            vlm.inject_error_feedback("")  # 清空积压反馈
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
                        break  # 中止本批次，进入下一步（重新截图）

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
                    _is_bulk_extract_goal = (
                        not _goal_is_tooltip_extract(goal)
                        and bool(
                            re.search(
                                r"获取|提取|抓取|采集|爬取|抽取|\bextract(?:_link)?\b|\bscrape\b|\bcrawl\b",
                                str(goal or ""),
                                re.IGNORECASE,
                            )
                        )
                    )
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
                            # save_to_excel 内部已支持追加写入 + 去重
                            saved_path = save_to_excel(
                                extracted,
                                _vlm_output,
                                unique_key=TOOLTIP_UNIQUE_KEY if _goal_is_tooltip_extract(goal) else None,
                            )
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
                        if extracted:
                            logger.info(f"[EXTRACT] Final data extracted: {extracted}")
                            save_to_excel(
                                extracted,
                                _vlm_output,
                                unique_key=TOOLTIP_UNIQUE_KEY if _goal_is_tooltip_extract(goal) else None,
                            )
                            # 同步更新进度计数器，避免 PROGRESS SAFEGUARD 用到过期数值
                            _record_extract_progress(extracted, len(extracted))
                        # 打印 XHR 拦截汇总
                        if browser.intercepted_count > 0:
                            logger.info(
                                f"[XHR SUMMARY] Total records intercepted this session: "
                                f"{browser.intercepted_count}"
                            )
                        if workflow_memory:
                            logger.info(f"[MEMORY] Final workflow_memory state: {workflow_memory}")
                        logger.info("[DONE] Task completed! VLM determined the goal has been achieved.")
                        _broadcast_log_safe("[DONE] Task completed! VLM determined the goal has been achieved.")
                        _broadcast_done_safe(True, "Task completed")
                        await browser.mark_and_screenshot(step=99)

                        # ── RPA 肌肉记忆：保存成功轨迹供下次极速回放 ──────────
                        if browser.rpa_trail or _executed_cached_trail or not _rpa_cache_allowed:
                            try:
                                merged_trail = _executed_cached_trail + browser.rpa_trail
                                compacted_trail = _compact_rpa_trail(merged_trail)
                                if len(compacted_trail) != len(merged_trail):
                                    logger.info(
                                        f"[RPA] Compacted trail before save: "
                                        f"{len(merged_trail)} → {len(compacted_trail)} steps"
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
                                _completes = not (_is_extract_goal and not _trail_has_extract)
                                cache_payload = {
                                    "version": 4,
                                    "replayable": _rpa_cache_allowed and bool(compacted_trail),
                                    "reason": "" if (_rpa_cache_allowed and compacted_trail) else (_rpa_skip_reason or "marked as non-replayable"),
                                    "trail": compacted_trail if (_rpa_cache_allowed and compacted_trail) else [],
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
                        if (
                            _type_tv
                            and _target_role
                            and _target_role not in _input_roles
                            and (
                                _target_role in _clickable_roles
                                or len(_type_tv) <= 80
                            )
                        ):
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
                        _multi_form_hits = re.findall(
                            r"([^=;\n\r]{1,80})\s*=\s*([^;\n\r]+)",
                            _form_tv_for_fix,
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
                    _pre_click_is_pagination_candidate = False
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

                    # ── 自愈执行：捕获 ActionExecutionError 并注入 VLM 反馈 ──────────
                    try:
                        active_page = await browser.execute_action(decision, workflow_memory)
                        if active_page is not None:
                            # execute_action 内部已更新 browser._page，此处仅做日志追踪
                            logger.debug(f"[TAB GUARD] Active page after action: {(active_page.url or 'about:blank')[:80]}")

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
                        _tab_switched_this_step = bool(browser._tab_switch_notice)
                        if browser._tab_switch_notice:
                            vlm.inject_error_feedback(browser._tab_switch_notice)
                            logger.info(f"[TAB SWITCH] Injected page-switch notice for next VLM step")
                            browser._tab_switch_notice = None

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
                        if len(_last_actions) > _LOOP_GUARD_WINDOW:
                            _last_actions.pop(0)

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
                                _outcome_parts.append(
                                    f"tooltip={str(_last_hover_rpa.get('tooltip_text'))[:120]!r}"
                                )
                        vlm.annotate_last_result("✅ " + " | ".join(_outcome_parts))

                        _submit_marker_text = " ".join(
                            str(decision.get(k) or "")
                            for k in ("type_value", "thought", "progress_review", "current_state")
                        ).lower()
                        _looks_like_form_submit = (
                            _is_form_fill_goal
                            and action in ("click", "click_text", "click_point")
                            and bool(
                                re.search(
                                    r"\b(create|submit|save|ok)\b|提交|保存|确定|创建",
                                    _submit_marker_text,
                                    re.IGNORECASE,
                                )
                            )
                        )
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
                            _log_decision = {
                                "action": "done",
                                "target_id": int(decision.get("target_id", 0) or 0),
                                "type_value": str(decision.get("type_value") or "submit"),
                                "status": "success",
                                "thought": (
                                    "表单字段已完成回读验收，且提交按钮已点击一次。"
                                    "当前页面无跳转/无提示，按演示站或静默提交处理，任务结束。"
                                ),
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
                        logger.warning(
                            f"[SELF-HEAL] Action failed ({_consecutive_errors}/{_MAX_CONSECUTIVE_ERRORS}): "
                            f"{err_msg}"
                        )
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
                            await asyncio.get_event_loop().run_in_executor(None, input)
                            logger.info("[MANUAL] User confirmed, resuming agent...")
                            _consecutive_errors = 0
                            vlm.inject_error_feedback("")  # 清空积压反馈
                        else:
                            # 将错误注入下一轮 VLM 提示，引导换策略
                            if not _force_next_page_pending:
                                vlm.inject_error_feedback(err_msg)
                        break  # 中止本批次，进入下一步（重新截图）

                # 内层批次循环结束：若任务完成则退出外层主循环
                if _task_completed:
                    break

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

        else:
            # for-else: 循环正常结束（没有 break），说明达到最大步数
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
        html_logger.finalize()
        await browser.close()
    return _run_succeeded


def main():
    """CLI 入口：解析命令行参数并启动 Agent。"""
    default_user_data_dir = str(Path(__file__).parent / "browser_data")

    parser = argparse.ArgumentParser(
        description="VSpider - Visual Web Agent (Offline/Intranet Edition)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python main.py --url "http://192.168.1.100/login" --goal "Login and query data"\n'
            '  python main.py --url "https://example.com" --goal "Find contact page"\n'
            '  python main.py --url "http://10.0.0.1" --goal "Login" --user-data-dir ./browser_data'
        ),
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Target webpage starting URL",
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
        "--output",
        default="",
        help="Output requirements for extracted data",
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
        )
    )


if __name__ == "__main__":
    main()

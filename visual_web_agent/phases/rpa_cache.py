"""RPA cache and form assignment utilities — extracted from ``main.py``.

Contains RPA replay cache path management, goal normalization,
form task plan normalization, and assignment parsing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..planner_contract import TaskPlan

logger = logging.getLogger("vspider.main")

try:
    from ..form_engine import (
        parse_form_assignments as engine_parse_form_assignments,
    )
except ImportError:
    engine_parse_form_assignments = None  # type: ignore[assignment]

_RPA_CACHE_DIR = Path(__file__).resolve().parent.parent / "rpa_cache"

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
    # Lazy imports: the load/write helpers live in sibling phase modules that also
    # import rpa_cache, so module-level imports would create an import cycle.
    from .pagination_helpers import _load_rpa_cache_payload
    from .rpa_replay import _write_rpa_cache_payload
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


# ── Goal parser functions extracted to phases/goal_parser.py ──
from .goal_parser import (  # noqa: E402
    _parse_goal_target_count,
    _parse_goal_target_pages,
    _extraction_targets_reached,
    _normalize_output_field_key,
    _parse_goal_requested_fields,
    _goal_is_tooltip_extract,
    _TOOLTIP_PLACEMENT_ORDER,
    _parse_goal_tooltip_targets,
    _title_tooltip_target,
    _infer_tooltip_trigger_label,
    _goal_is_cascader_task,
    _find_visible_popup_menu_label,
    _rewrite_cascader_nav_click_to_popup_text,
    _goal_is_rpa_challenge_task,
    _goal_is_round_form_task,
    _goal_has_explicit_login_intent,
    _goal_is_chat_task,
    _goal_is_form_fill,
    _form_goal_requires_submit,
    _parse_form_repeat_count,
    _should_use_round_form_macro,
)



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


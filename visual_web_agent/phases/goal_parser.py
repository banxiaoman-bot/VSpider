"""Goal parsing utilities extracted from main.py (Slice 1).

Pure and near-pure functions that classify user goals by type
(form-fill, tooltip, chat, RPA challenge, cascader, etc.) and
parse structured parameters (target count, fields, repeat count).
"""
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..browser_env import BrowserEnv

try:
    from ..agent_strategy import (
        extraction_targets_reached as _agent_strategy_extraction_targets_reached,
        normalize_output_field_key as _agent_strategy_normalize_output_field_key,
        parse_goal_requested_fields as _agent_strategy_parse_goal_requested_fields,
        parse_goal_target_count as _agent_strategy_parse_goal_target_count,
        parse_goal_target_pages as _agent_strategy_parse_goal_target_pages,
    )
except ImportError:
    from agent_strategy import (  # type: ignore[no-redef]
        extraction_targets_reached as _agent_strategy_extraction_targets_reached,
        normalize_output_field_key as _agent_strategy_normalize_output_field_key,
        parse_goal_requested_fields as _agent_strategy_parse_goal_requested_fields,
        parse_goal_target_count as _agent_strategy_parse_goal_target_count,
        parse_goal_target_pages as _agent_strategy_parse_goal_target_pages,
    )


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

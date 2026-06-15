"""
Deprecated legacy prompt section builder.

Extracted from prompts.py to reduce file size.
"""

try:
    from .prompts import FULL_SYSTEM_PROMPT, _prompt_section, _has_any
except ImportError:
    from prompts import FULL_SYSTEM_PROMPT, _prompt_section, _has_any


_PROMPT_INTRO = FULL_SYSTEM_PROMPT.split("## 输入上下文", 1)[0].strip()

_CORE_SECTION_HEADINGS = (
    "## 你必须严格遵守以下规则",
    "## JSON 输出格式（严格遵守）",
    "## action 说明",
    "## 🚫 SoM ID 每步都会重新分配（绝对禁止复用历史 ID）",
    "## ⚠️ 截图 ID 与数据的区隔（防 extract 串味）",
    "## 输入上下文（每轮固定收到三部分）",
    "## 决策核心三步法（每一个动作都要走完这三步）",
    "## 📌 寻找目标的终极法则 (Active Exploration)",
    "## 📌 任务完成与强制退出法则 (The 'Done' Directive)",
)

_FORM_KEYWORDS = (
    "表单", "填写", "填入", "输入", "提交", "筛选", "过滤", "查询", "搜索",
    "form", "input", "submit", "search", "filter",
)
_DATE_KEYWORDS = ("日期", "时间", "日历", "date", "time", "calendar")
_ASYNC_SELECT_KEYWORDS = ("异步", "下拉", "联想", "搜索框", "combobox", "select2", "autocomplete")
_TREE_KEYWORDS = ("树形", "组织", "部门", "分类树", "tree")
_UPLOAD_KEYWORDS = ("上传", "导入", "文件", "upload", "import", "file")
_EXTRACT_KEYWORDS = (
    "提取", "获取", "采集", "抓取", "读取", "导出", "下载", "保存", "统计",
    "列表", "表格", "数据", "excel", "csv", "extract", "scrape", "download",
    "export", "table", "data",
)
_LOGIN_KEYWORDS = (
    "login", "signin", "sign in", "auth", "passport", "sso", "登录", "登陆",
    "账号", "账户", "密码", "验证码", "认证",
)
_CREDENTIAL_KEYWORDS = (
    "password", "passwd", "pwd", "phone", "mobile", "email", "username",
    "密码", "手机号", "手机", "邮箱", "账号", "用户名", "验证码", "凭证",
)
_MULTI_TAB_KEYWORDS = (
    "当前标签页列表", "标签页", "新标签", "新窗口", "后台打开", "switch_tab",
    "close_tab", "click_new_tab", "new tab", "tab",
    # 批量取链接内容也归入多 tab 范畴 — 走 fetch_links_batch 更快
    "每个链接", "每条结果", "依次抓取", "依次提取", "批量抓取",
)
_HITL_KEYWORDS = ("captcha", "验证码", "风控", "扫码", "短信", "二次鉴权", "ask_human")

_AUTH_VAULT_RULES = """
## Auth Vault 凭证占位符规则

如果用户或系统上下文明确提供了环境变量形式的凭证占位符（如 `{{env:OA_USER}}`、`{{env:OA_PASS}}`），你可以在 `type_value` 中原样输出该占位符。
- 真实账号/密码不会出现在你的上下文中，底层执行 `type` 动作前会从本机环境变量读取并替换。
- 你绝对不要猜测、编造或展开占位符的真实值。
- 如果页面需要凭证但没有可用的 `{{env:...}}` 或 `{{memory_key}}` 占位符，输出 `ask_human`，不要硬闯。
""".strip()


def _looks_like_login_surface(browser_state: str) -> bool:
    """Detect login forms from AX/input snapshots, including modal and iframe login."""
    text = (browser_state or "").lower()
    if not text:
        return False
    password_markers = (
        'type="password"', "type='password'", "type=password", "input type: password",
        "role: textbox", "role=\"textbox\"", "role='textbox'", "[textbox]",
    )
    password_names = (
        "password", "passwd", "pwd", "current-password", "new-password",
        "密码", "登录密码", "确认密码",
    )
    account_names = (
        "username", "user name", "account", "phone", "mobile", "email",
        "账号", "帐号", "账户", "用户名", "手机号", "手机号码", "邮箱",
    )
    has_password_field = (
        "password" in text
        or "密码" in text
        or any(marker in text for marker in password_markers)
        and any(name in text for name in password_names)
    )
    has_account_field = any(name in text for name in account_names)
    return has_password_field or (has_account_field and _has_any(text, ("登录", "登陆", "sign in", "signin", "login")))


def _build_legacy_section_prompt(
    goal: str = "",
    browser_state: str = "",
    workflow_memory: dict | None = None,
    *,
    force_full: bool = False,
) -> str:
    """
    Deprecated old dynamic prompt builder that sliced sections out of the full prompt.

    SYSTEM_PROMPT/FULL_SYSTEM_PROMPT remain the full legacy prompt for compatibility.
    The dynamic prompt intentionally reuses verbatim sections from the legacy prompt
    so behavior changes are limited to conditional inclusion, not rewritten policy.
    """
    if force_full:
        return FULL_SYSTEM_PROMPT

    haystack = f"{goal}\n{browser_state}".lower()
    login_surface = _looks_like_login_surface(browser_state)
    sections: list[str] = [_PROMPT_INTRO]
    sections.extend(
        _prompt_section(FULL_SYSTEM_PROMPT, heading)
        for heading in _CORE_SECTION_HEADINGS
    )

    if _has_any(haystack, _EXTRACT_KEYWORDS):
        sections.extend(
            [
                _prompt_section(FULL_SYSTEM_PROMPT, "## ⚠️ 提取动作 (Extract) 的绝对视觉法则"),
                _prompt_section(FULL_SYSTEM_PROMPT, "## 海量数据处理决策树（强制执行）"),
                _prompt_section(FULL_SYSTEM_PROMPT, "## 📌 跨页提取的优雅退出法则 (Graceful Exit)"),
            ]
        )

    if _has_any(haystack, _FORM_KEYWORDS):
        sections.extend(
            [
                _prompt_section(FULL_SYSTEM_PROMPT, "## 表单筛选条件预检规则（先读后写）"),
                _prompt_section(FULL_SYSTEM_PROMPT, "## ⚠️ 表单与搜索提交法则 (Atomic Search Submission)"),
            ]
        )
    if _has_any(haystack, _DATE_KEYWORDS):
        sections.append(_prompt_section(FULL_SYSTEM_PROMPT, "## 日期选择器操作规则（强制文本注入）"))
    if _has_any(haystack, _ASYNC_SELECT_KEYWORDS):
        sections.append(_prompt_section(FULL_SYSTEM_PROMPT, "## 异步搜索下拉框操作规则（输入-等待-点击）"))
    if _has_any(haystack, _TREE_KEYWORDS):
        sections.append(_prompt_section(FULL_SYSTEM_PROMPT, "## 树形多选框操作规则（渐进式展开）"))
    if _has_any(haystack, _UPLOAD_KEYWORDS):
        sections.append(_prompt_section(FULL_SYSTEM_PROMPT, "## 文件导出/下载说明"))

    if login_surface or _has_any(haystack, _LOGIN_KEYWORDS):
        sections.extend(
            [
                _prompt_section(FULL_SYSTEM_PROMPT, "## 核心思维路径（必须严格遵循）"),
                _prompt_section(FULL_SYSTEM_PROMPT, "## 登录失败处理规则（必须严格遵守）"),
                _prompt_section(FULL_SYSTEM_PROMPT, "## 登录状态自动检测（第 1 步必须执行此判断）"),
            ]
        )

    if login_surface or _has_any(haystack, _CREDENTIAL_KEYWORDS):
        sections.append(_prompt_section(FULL_SYSTEM_PROMPT, "## 🔒 凭证安全红线（违反立即 ValidationError）"))
        sections.append(_AUTH_VAULT_RULES)

    if _has_any(haystack, _HITL_KEYWORDS):
        sections.append(_prompt_section(FULL_SYSTEM_PROMPT, "## Additional Runtime Rules"))

    if workflow_memory or _has_any(haystack, ("save_to_memory", "{{", "记忆", "跨页面", "变量")):
        sections.append(_prompt_section(FULL_SYSTEM_PROMPT, "## 跨页面记忆库（Workflow Memory）使用规则"))

    if _has_any(haystack, _MULTI_TAB_KEYWORDS):
        sections.append(_prompt_section(FULL_SYSTEM_PROMPT, "## 空间与多标签页操作守则（CRITICAL）"))

    if _has_any(haystack, ("语义对齐", "意图", "模糊", "hover", "拖拽", "drag", "广告", "遮挡")):
        sections.append(_prompt_section(FULL_SYSTEM_PROMPT, "## 语义对齐映射表（Semantic Mapping）"))

    if _has_any(haystack, ("示例", "范例", "few-shot", "few shot")):
        sections.append(_prompt_section(FULL_SYSTEM_PROMPT, "## 思考与决策范例（Few-Shot CoT）"))

    compact = "\n\n".join(s for s in sections if s)
    return compact if compact.strip() else FULL_SYSTEM_PROMPT

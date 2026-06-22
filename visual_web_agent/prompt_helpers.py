"""
VSpider Prompt Helper Functions (from prompts.py)

Keyword lists and detection functions used by build_system_prompt.
"""

import re

__all__ = [
    "_ASYNC_SELECT_KEYWORDS", "_AUTH_VAULT_RULES", "_BULK_EXTRACT_TRIGGERS",
    "_CASCADER_TRIGGERS", "_CHAT_ENTRY_TRIGGERS", "_CONFIRM_DIALOG_TRIGGERS",
    "_CORE_SECTION_HEADINGS", "_CREDENTIAL_KEYWORDS", "_CREDENTIAL_TRIGGERS",
    "_DATA_EXPORT_TRIGGERS", "_DATE_KEYWORDS", "_EXTRACT_KEYWORDS",
    "_EXTRACT_TRIGGERS", "_FEED_AD_FILTER_TRIGGERS", "_FORM_KEYWORDS",
    "_FORM_TRIGGERS", "_HITL_KEYWORDS", "_HITL_TRIGGERS", "_HOVER_MENU_TRIGGERS",
    "_LOGIN_KEYWORDS", "_LOGIN_TRIGGERS", "_MEMORY_TRIGGERS",
    "_MULTI_TAB_KEYWORDS", "_MULTI_TAB_TRIGGERS", "_PAGE_TO_MARKDOWN_TRIGGERS",
    "_PROMPT_INTRO", "_RELATIVE_DATE_TRIGGERS", "_RESUME_RUN_TRIGGERS",
    "_ROW_ACTION_TRIGGERS", "_SEARCH_NAV_TRIGGERS", "_SEMANTIC_TRIGGERS", "_SNAPSHOT_TRIGGERS",
    "_STEPPER_TRIGGERS", "_TOOLTIP_TRIGGERS", "_TREE_KEYWORDS", "_TREE_TRIGGERS",
    "_UPLOAD_KEYWORDS", "_URL_RE", "_VSCROLL_CAPTURE_TRIGGERS",
    "_looks_like_bulk_extract", "_looks_like_login_surface", "_resolver_matches",
    "_text_has_any", "_url_matches_data_export_registry",
]

try:
    from .prompt_templates import FULL_SYSTEM_PROMPT
except ImportError:
    from prompt_templates import FULL_SYSTEM_PROMPT

try:
    from .prompt_skills import SKILL_PROMPTS, STATIC_PROMPT_PARTS
except ImportError:
    from prompt_skills import SKILL_PROMPTS, STATIC_PROMPT_PARTS

try:
    from .extraction_engine.strategies import infer_goal_output_contract
except ImportError:
    from extraction_engine.strategies import infer_goal_output_contract

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

try:
    from .prompt_legacy import _build_legacy_section_prompt  # noqa: F401
except ImportError:
    from prompt_legacy import _build_legacy_section_prompt  # noqa: F401



def _text_has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(k in text for k in keywords)


def _resolver_matches(goal: str) -> bool:
    """Return True iff ``visual_web_agent.relative_date`` could resolve a
    relative-date phrase in ``goal``.

    Wrapped in try/except for two reasons:
      * Test fixtures may import this module before sys.path resolves the
        relative_date module — fall back to keyword-only triggering.
      * relative_date itself is pure / synchronous / no-side-effect, but
        we still defensively isolate any future regex regression so it
        can never crash the system-prompt build.
    """
    if not goal:
        return False
    try:
        try:
            from .relative_date import resolve_relative_date
        except ImportError:  # pragma: no cover - direct script import
            from relative_date import resolve_relative_date
        return resolve_relative_date(goal) is not None
    except Exception:
        return False


# Cheap URL extractor — pulls http(s) URLs out of the browser_state blob so
# the data_export registry can be tested without needing the live page.
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)


def _url_matches_data_export_registry(browser_state: str) -> bool:
    """Return True iff ``browser_state`` contains a URL recognised by any
    transformer registered in :mod:`visual_web_agent.data_export`.

    This is the authoritative trigger for ``DATA_EXPORT_SKILL``: keyword
    fallback (``_DATA_EXPORT_TRIGGERS``) catches text mentions, this catches
    actual URLs in the page snapshot. Adding a new ``ExportTransform``
    auto-wires it through both signals.

    Defensive try/except so a registry import failure can't break prompt
    building (the keyword fallback still fires).
    """
    if not browser_state:
        return False
    try:
        try:
            from .data_export import find_data_export_url
        except ImportError:  # pragma: no cover - direct script import
            from data_export import find_data_export_url
        for match in _URL_RE.finditer(browser_state[:8000]):
            name, export = find_data_export_url(match.group(0))
            if name and export:
                return True
        return False
    except Exception:
        return False


_FORM_TRIGGERS = (
    "表单", "填写", "填入", "输入", "提交", "筛选", "过滤", "查询", "搜索",
    "日期", "下拉", "树形", "上传", "导入",
    "form", "input", "submit", "search", "filter", "date", "calendar",
    "combobox", "select", "autocomplete", "upload", "import", "file",
)
_EXTRACT_TRIGGERS = (
    "提取", "获取", "采集", "抓取", "读取", "导出", "下载", "保存", "统计",
    "列表", "表格", "数据", "excel", "csv", "extract", "scrape", "download",
    "export", "table", "data", "pdf",
)
_BULK_EXTRACT_TRIGGERS = (
    "批量", "全量", "所有", "全部", "多页", "翻页", "分页", "下一页", "每页",
    "几百", "几千", "上千", "保存到excel", "保存到 excel", "导出excel",
    "bulk", "all pages", "pagination", "next page",
)
_LOGIN_TRIGGERS = (
    "login", "signin", "sign in", "auth", "passport", "sso", "登录", "登陆",
    "账号", "账户", "密码", "验证码", "认证", "扫码", "短信",
)
_CREDENTIAL_TRIGGERS = (
    "{{env:", "password", "passwd", "pwd", "phone", "mobile", "email", "username",
    "密码", "手机号", "手机", "邮箱", "账号", "用户名", "凭证", "密钥",
)
_MULTI_TAB_TRIGGERS = (
    # 原有：用户显式描述多 tab 操作
    "标签页", "新标签", "新窗口", "后台打开", "switch_tab", "close_tab",
    "click_new_tab", "new tab", "tab",
    # 新增：批量取链接内容的语义，触发 fetch_links_batch / fetch_link_content 的指引
    "每个链接", "每条结果", "前几条", "前 3 条", "前3条", "前 5 条", "前5条",
    "每篇", "每个搜索结果", "依次抓取", "依次提取", "批量抓取", "批量提取",
    "fetch", "fetch_link", "fetch_links_batch", "extract content from",
)
_HOVER_MENU_TRIGGERS = (
    "hover", "悬浮", "悬停", "鼠标悬停", "下拉菜单", "菜单项", "弹出菜单",
    "dropdown", "drop-down", "menuitem", "hover-trigger", "element plus",
    "element ui", "ant design", "action 1", "action 2", "action 3",
)
_CASCADER_TRIGGERS = (
    "cascader", "级联", "级联选择器", "多级菜单", "多级下拉", "多级选择",
    "树形级联", "选择路径", "->", "→",
)
# Row-action: VLM should use the system's row_action handler when goal asks
# for "delete/edit/view/approve/retry the row where X". Critically narrow —
# pure list-extract goals must NOT pull this in, or VLM will start hunting
# for buttons in extraction tasks.
_ROW_ACTION_TRIGGERS = (
    # CN: "<动词> + 那一行/那条/这条/这行"
    "那一行", "这一行", "那行", "这行", "那条", "这条",
    "行内", "行操作", "行的删除", "行的编辑", "row action",
    # CN explicit row-anchored verbs
    "删除张", "删除李", "删除王", "删除赵",  # 张三/李四等姓氏开头
    "删除该行", "编辑该行", "查看该行", "审批该行", "批准该行", "重试该行",
    "删除这条", "删除该条", "编辑这条", "审批这条", "重试这条",
    "把状态", "状态是",
    "订单号", "订单 #", "order #", "order id",
    # EN: "delete/edit/view/approve/retry the row where ..."
    "the row where", "delete the row", "edit the row",
    "view the row", "approve the row", "retry the row",
    "row with status", "row where status",
)
# Confirm dialog: triggered by destructive verbs OR explicit dialog mentions.
# Co-pulled with ROW_ACTION (most row deletes spawn a confirm modal).
_CONFIRM_DIALOG_TRIGGERS = (
    "确认弹窗", "确认对话框", "确认框", "二次确认", "二次确定",
    "确定按钮", "弹出确认", "弹窗确认", "messagebox", "message box",
    "el-message-box", "ant-modal-confirm", "popconfirm",
    "confirm dialog", "confirmation modal", "confirmation dialog",
    # Destructive verbs that almost always spawn confirm modals
    "删除", "清空", "重置全部", "取消订阅", "退订",
    "delete ", "clear all", "reset all", "unsubscribe",
)
_TREE_TRIGGERS = (
    "树形", "树结构", "树控件", "文件树", "目录树", "组织架构", "组织架构树",
    "分类树", "el-tree", "ant-tree", "ant tree", "<tree>", "node tree",
    "tree view", "tree control", "treeview",
    "expand the node", "expand the folder", "collapse the node",
    "展开节点", "展开目录", "展开父节点", "折叠节点",
)
_STEPPER_TRIGGERS = (
    "stepper", "wizard", "向导", "多步表单", "分步表单", "分步注册",
    "step 1", "step 2", "step 3", "第一步", "第二步", "第三步",
    "next step", "下一步", "上一步", "previous step",
    "el-steps", "ant-steps", "步骤条",
)
# Loaded when the goal references a relative date that the resolver
# (visual_web_agent.relative_date) can pin to an absolute date. The trigger
# is a fast keyword fallback (used by tests + when the resolver isn't
# available); the authoritative test in select_skills calls
# resolve_relative_date() directly so the skill follows the SAME generic
# capability that augments the goal — no per-phrase / per-site patches.
_RELATIVE_DATE_TRIGGERS = (
    # System-injected hint marker (highest signal — 100% reliable)
    "【相对日期解析】",
    # Day offsets
    "今天", "今日", "明天", "明日", "后天", "大后天",
    "昨天", "昨日", "前天", "大前天",
    "today", "tomorrow", "yesterday",
    # Month offsets — both directions, including multi-level "下下月"
    "下个月", "下月", "下下月", "下下个月", "下下下月", "次月",
    "上个月", "上月", "上上月", "上上个月", "上上上月",
    "本月", "这个月", "当月",
    "next month", "last month", "previous month", "this month",
    "following month", "coming month",
    # Year offsets
    "明年", "去年", "今年", "后年", "前年", "大后年", "大前年",
    "next year", "last year", "previous year",
    # Week offsets — also the relative weekday triggers
    "下周", "上周", "本周", "下星期", "上星期",
    "next week", "last week", "this week",
    # Boundaries
    "月底", "月初", "月中", "月末", "本月最后", "本月最后一天",
    "end of month", "start of month", "beginning of month",
    # N-day arithmetic (catch by suffix tokens)
    "天后", "天前", "天之后", "天之前",
    "days ago", "days from now", "days later",
    # In/Ago English
    "in 1 day", "in 2 day", "in 3 day", "in 4 day", "in 5 day",
    "in 6 day", "in 7 day", "in 10 day", "in 14 day", "in 30 day",
)
# Generic data-export trigger keywords. These are a *fallback* — the
# authoritative test is ``_url_matches_data_export_registry`` which calls
# the actual data_export registry. New data sources auto-trigger when
# someone adds an ``ExportTransform`` — no need to update this list.
_DATA_EXPORT_TRIGGERS = (
    # URL signals — caught via browser_state too
    "docs.google.com/spreadsheets",
    "onedrive.live.com", "1drv.ms", ".sharepoint.com",
    # Goal-text signals
    "google sheets", "google sheet", "google 表格", "google表格",
    "google spreadsheet",
    "onedrive", "sharepoint", "office 365", "office365",
    ".xlsx", ".xlsm", ".csv", ".tsv",
    "导出 csv", "导出csv", "下载 csv", "下载csv",
    "导出 excel", "导出excel", "下载 excel", "下载excel",
    "export csv", "export excel", "download csv", "download xlsx",
)
_FEED_AD_FILTER_TRIGGERS = (
    "跳过广告", "跳过推广", "排除广告", "排除推广", "过滤广告", "过滤推广",
    "不要广告", "不计广告", "去除广告", "屏蔽广告",
    "skip ad", "skip ads", "skip sponsored", "exclude ad", "exclude sponsored",
    "filter out ad", "no ads", "without ads",
    # Strong implicit signals: "真实/技术 文章" + "广告/推广" co-occurring
    "真实的技术", "只提取真实", "真实文章",
)
_TOOLTIP_TRIGGERS = (
    "tooltip", "tool tip", "popover", "提示框", "提示气泡", "黑色提示",
    "浮层提示", "气泡", "悬浮提示", "鼠标悬停提示",
)
_HITL_TRIGGERS = (
    "captcha", "验证码", "风控", "滑块", "扫码", "短信", "二次认证", "设备校验",
    "ask_human", "human_intervention",
)
_MEMORY_TRIGGERS = ("save_to_memory", "{{", "记忆", "跨页面", "变量", "memory")
_SEMANTIC_TRIGGERS = (
    "语义", "意图", "模糊", "hover", "拖拽", "drag", "广告", "遮挡", "弹窗", "关闭",
)
# Loaded when the user is asking the agent to use a chat / assistant /
# Q&A service. The pattern "find X assistant + type a question + read
# answer" is the highest-volume failure mode for entry-page confusion
# (run_log_20260514_183718: 20 wasted steps after VLM submitted to the
# Baidu search box thinking it was Wenxin chat input).
_CHAT_ENTRY_TRIGGERS = (
    "助手", "对话", "聊天", "提问", "问 AI", "让 AI", "问问",
    "文心", "通义", "豆包", "kimi", "hunyuan", "腾讯元宝", "智谱", "ChatGLM",
    "chatgpt", "chat gpt", "claude", "gemini", "copilot",
    "ai 回答", "ai回答", "ai 回复", "ai回复",
    "介绍一下", "回答一下", "解释一下",  # common chat-style asks
)
_PAGE_TO_MARKDOWN_TRIGGERS = (
    "markdown", "转md", "转 md", "网页转md", "可读正文", "正文提取", "提取正文",
    "reader mode", "readable", "clean text", "main content", "去噪正文",
    "喂给大模型", "喂大模型", "rag", "llm friendly", "llm-friendly", "page to markdown",
)
_VSCROLL_CAPTURE_TRIGGERS = (
    "虚拟滚动", "虚拟列表", "虚拟表格", "无限滚动", "滚动加载", "滚动采集",
    "全量采集", "滚到底", "滚动到底",
    "virtual scroll", "virtual list", "virtual table", "virtualized",
    "virtualised", "infinite scroll", "react-window", "vue-virtual",
    "ag-grid", "ag grid",
)
_SNAPSHOT_TRIGGERS = (
    "截图", "截屏", "屏幕截图", "整页截图", "保存截图",
    "网页快照", "页面快照", "保存网页", "保存页面", "另存网页", "存为html", "存为 html",
    "screenshot", "page snapshot", "html snapshot", "save the page", "save page",
    "save html",
)
_RESUME_RUN_TRIGGERS = (
    "续跑", "断点续跑", "断点续传", "接着上次", "继续上次", "上次没做完", "上次没完成",
    "接着之前", "继续之前", "resume", "resume run", "continue last", "continue previous",
    "pick up where", "left off",
)
_SEARCH_NAV_TRIGGERS = (
    "搜索并打开", "搜索后打开", "先搜索", "打开第一个结果", "打开搜索结果",
    "第一个结果", "首个结果", "最相关的结果", "用搜索引擎",
    "search and open", "open the first result", "open the top result",
    "first result", "top result", "search result", "search results",
    "/search?q", "bing.com/search", "google.com/search", "baidu.com/s?",
    "so.com/s", "sogou.com/web",
)


def _looks_like_bulk_extract(goal: str) -> bool:
    text = (goal or "").lower()
    if not text:
        return False
    if _text_has_any(text, _BULK_EXTRACT_TRIGGERS):
        return True
    count_match = re.search(
        r"(\d+)\s*(?:条|个|项|篇|则|部|家|名|位|款|本|场|首|records?|items?|rows?)",
        text,
    )
    return bool(count_match and int(count_match.group(1)) >= 50)

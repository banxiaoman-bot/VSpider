"""
VSpider 浏览器控制封装模块

基于 async Playwright 封装浏览器生命周期管理、
SoM 标记注入、截图捕获和操作执行。

支持：
- 登录态/Cookie 持久化（通过 user_data_dir）
- SPA 页面加载状态监测（networkidle + 硬等待双保险）
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from playwright.async_api import (
    async_playwright,
    BrowserContext,
    ElementHandle,
    Frame,
    Page,
    Response,
    Route,
)
from playwright_stealth import Stealth

try:
    from . import config
    from .auth_manager import apply_storage_state_to_context, load_auth_profiles
    from .artifact_manager import artifact_root, register_download_artifact
    from .action_result import ActionResult
    from .browser_profile import resolve_user_data_dir
    from .stealth_profile import build_profile
    from .data_manager import save_intercepted_data
    from .network_intelligence import record_candidate as _record_network_candidate
    from .vlm_client import VSpiderAction
except ImportError:
    import config
    from auth_manager import apply_storage_state_to_context, load_auth_profiles
    from artifact_manager import artifact_root, register_download_artifact
    from action_result import ActionResult
    from browser_profile import resolve_user_data_dir
    from stealth_profile import build_profile
    from data_manager import save_intercepted_data
    from network_intelligence import record_candidate as _record_network_candidate
    from vlm_client import VSpiderAction

try:
    from .browser_intercept_helpers import (
        _format_som_element as format_som_element,
        screenshot_looks_visually_blank,
        extract_data_list,
        score_intercept_candidate,
        stable_json_hash,
        schema_fingerprint,
        row_dedup_key,
        dedupe_intercept_rows,
        flatten_ax_tree_for_extract,
        flatten_ax_tree,
    )
except ImportError:
    from browser_intercept_helpers import (
        _format_som_element as format_som_element,
        screenshot_looks_visually_blank,
        extract_data_list,
        score_intercept_candidate,
        stable_json_hash,
        schema_fingerprint,
        row_dedup_key,
        dedupe_intercept_rows,
        flatten_ax_tree_for_extract,
        flatten_ax_tree,
    )

# ── 网络资源拦截配置 ────────────────────────────────────────────────────────
# 策略：拦截媒体流和字体（体积大、对 SoM 截图无意义），保留 image（视觉模式需要）。
# 同时过滤常见广告追踪域名（不影响目标站点功能，仅减少噪音请求）。
_BLOCK_RESOURCE_TYPES: frozenset[str] = frozenset({"media", "font"})
_BLOCK_AD_DOMAINS: tuple[str, ...] = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "connect.facebook.net",
    "analytics.twitter.com",
    "hotjar.com",
    "clarity.ms",
    "omniture.com",
    "adservice.google.com",
    "googlesyndication.com",
    "cnzz.com",
    "umeng.com",
)
logger = logging.getLogger("vspider.browser")


def _broadcast_image_safe(b64_data_uri: str, step: int) -> None:
    """向 API WebSocket 广播截图；未运行 API 时静默降级。"""
    try:
        from api_server import broadcast_image

        broadcast_image(b64_data_uri, step=step)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════
#  自愈异常：由 execute_action 在捕获操作失败后抛出
# ════════════════════════════════════════════════════════════════

class ActionExecutionError(Exception):
    """
    浏览器动作执行失败时由 execute_action 抛出。

    main.py 捕获此异常后，通过 vlm.inject_error_feedback() 将错误信息
    注入下一轮 VLM 提示，引导模型换策略（自愈机制）。
    连续失败超过阈值时触发 HITL（ask_human）。
    """


# ════════════════════════════════════════════════════════════════
#  全局多标签页焦点守护（Tab Guard）
#  独立函数：不依赖 BrowserEnv 实例，可单独测试
# ════════════════════════════════════════════════════════════════

async def ensure_active_page(
    context: BrowserContext,
    current_page: Page,
    *,
    known_pages: set[int] | None = None,
) -> Page:
    """
    多标签页焦点守护状态机（3 层安全网）。

    执行任意浏览器动作后，页面拓扑可能发生变化：
      - 新 tab 弹出（target="_blank" / window.open）
      - 当前页被关闭（弹窗自动关闭 / 脚本调用 window.close()）
      - 所有 tab 意外全灭

    本函数统一处理上述三种情形，始终返回一个可用的激活 Page。

    Args:
        context:      Playwright BrowserContext 实例
        current_page: 执行动作前持有的 Page 引用
        known_pages:  执行动作前已存在的 Page 的 id() 集合。如果提供，
                      L3「向前跟随」只在最新页面**确实是本次动作新开的**
                      时触发；老 tab（动作前就存在）不会被当成"刚弹出"
                      抢走焦点。传 None 时退化为旧行为（永远跟随 latest）。

    Returns:
        绝对处于激活状态的 Page 对象。
        调用方应以此返回值替换旧的 page 引用，并更新 self._page。
    """
    open_pages = [p for p in context.pages if not p.is_closed()]

    # ── 层 1：极端兜底 ────────────────────────────────────────
    # 所有标签页均已关闭（某些弹窗页面完成操作后会关闭自身及主页）
    if not open_pages:
        new_page = await context.new_page()
        await new_page.bring_to_front()
        logger.warning("[TAB GUARD L1] All pages closed — created new blank page")
        return new_page

    # ── 层 2：向后回退 (Fallback) ─────────────────────────────
    # 当前页已被关闭（手动 close_tab / 脚本调用 window.close()）
    # 焦点退回存活列表最后一个（最近活跃的历史页，通常是调用方所在的列表页）
    if current_page.is_closed():
        fallback = open_pages[-1]
        await fallback.bring_to_front()
        # ★ 给内网系统 / 复杂 SPA 留 1 秒重绘时间，避免截图拿到空白帧
        await asyncio.sleep(1)
        logger.info(
            "[TAB GUARD L2] Current page closed → fallback to: "
            f"{(fallback.url or 'about:blank')[:80]}"
        )
        return fallback

    # ── 层 3：向前跟随 (Follow) — 只对**新开的** tab 生效 ────────
    # 旧行为：永远 follow open_pages[-1]，导致非交互动作（wait/type/scroll）
    # 也会把焦点抢到上一轮残留的 aidaxue tab，破坏 switch_tab 锚定。
    # 新行为：只在最新 tab 不在 known_pages（动作前快照）里时才 follow。
    # 传 None 时为兼容旧调用方，保留全跟随。
    latest = open_pages[-1]
    if latest is not current_page:
        is_new_tab = known_pages is None or id(latest) not in known_pages
        if not is_new_tab:
            logger.debug(
                "[TAB GUARD L3] latest tab %s is pre-existing; "
                "leaving current page %s active",
                (latest.url or 'about:blank')[:60],
                (current_page.url or 'about:blank')[:60],
            )
            return current_page
        await latest.bring_to_front()
        try:
            # 等待新标签页加载完成；广告页/无限 loading 页超时后宽松跳过
            await latest.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            logger.debug("[TAB GUARD L3] networkidle timeout on new tab, proceeding anyway")
        # ★ 同上，内网渲染延迟兜底
        await asyncio.sleep(1)
        logger.info(
            "[TAB GUARD L3] New tab detected → following: "
            f"{(latest.url or 'about:blank')[:80]}"
        )
        return latest

    # 无切换，原页面仍为活跃页
    return current_page


@dataclass
class ActionTarget:
    frame: Frame
    handle: ElementHandle
    selector: str


class BrowserEnv:
    """
    Playwright 浏览器环境封装。

    始终使用 launch_persistent_context 以持久化登录态/Cookie。
    管理浏览器实例的生命周期，提供：
    - 页面导航（持久化登录态，复用 Cookie）
    - SoM 标记注入与截图（截图前等待 SPA 渲染完成）
    - 基于 VLM 决策的操作执行（click/type/scroll）
    """

    # Chromium 启动参数：禁用各种原生弹窗，避免干扰 VLM 截图判断
    _BROWSER_ARGS = [
        "--disable-save-password-bubble",                # 禁用"保存密码"弹窗
        "--password-store=basic",                        # 使用基础密码存储（不弹系统钥匙串）
        "--disable-autofill-keyboard-accessory-view",    # 禁用自动填充下拉框
        "--disable-notifications",                       # 禁用网页通知弹窗
        "--deny-permission-prompts",                     # 原生权限请求（麦克风/摄像头/定位）统一拒绝
        "--disable-popup-blocking",                      # 禁用弹窗拦截（让 Agent 处理弹窗）
        "--disable-infobars",                            # 禁用"Chrome 正在被自动化软件控制"提示条
        "--disable-extensions",                          # 禁用扩展
        "--disable-component-extensions-with-background-pages",  # 禁用组件扩展（含无障碍工具栏）
        "--disable-features=Accessibility",              # 禁用无障碍辅助功能入口
        "--no-first-run",                                # 跳过首次运行引导
        "--no-default-browser-check",                    # 跳过默认浏览器检查
        "--disable-translate",                           # 禁用翻译弹窗
        "--disable-background-networking",               # 减少后台网络请求
        # ── 反自动化检测 ───────────────────────────────────────────
        # 消除 Chromium 内核级别的 navigator.webdriver=true 标志；
        # JS 层无法覆盖该属性，只能通过启动参数在内核层面关闭。
        # 豆瓣/知乎等站点检测到 webdriver=true 时会主动返回空白内容。
        "--disable-blink-features=AutomationControlled",
        # 排除 Chrome 扩展自动化 ID（部分站点检测此字段）
        "--exclude-switches=enable-automation",
    ]

    def __init__(self):
        self._playwright = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        # PROXY-4: rotatable proxy chain. Built once (start / _ensure_proxy_chain)
        # and preserved across restart() so rotation state survives a reroute.
        self._proxy_chain = None
        self._closed: bool = False
        self._som_js: str = ""
        self._screenshot_dir: Path = Path(config.SCREENSHOT_DIR)
        self._download_dir: Path = artifact_root() / "downloads"
        # XHR 拦截相关
        self._intercept_enabled: bool = False  # 默认关闭，由 configure_interceptor() 或 --xhr 参数开启
        self._intercept_count: int = 0
        self._intercept_unique_key: str | list[str] | None = None
        self._intercept_filename: str = "output"
        # output_contract.v1 of the current run; decides the intercept
        # dataset container (xlsx/csv/jsonl) instead of a blind xlsx default.
        self._intercept_output_contract: dict | None = None
        self._registered_pages: set[int] = set()
        # STEALTH-3: identity bundle (UA + CH + ua metadata), set in start() so every page CDP session can replay setUserAgentOverride.
        self._stealth_profile = None
        self._background_tasks: set[asyncio.Task] = set()
        self._intercept_min_list_size: int = 5  # 启发式探测阈值
        # ── 核心 API 精准截胡 ──────────────────────────────────────────
        # 通过 --xhr-pattern 指定 URL 关键词，命中后数据写入此变量，
        # main.py 轮询检测，一旦有值立即触发"主引擎生效"分支，跳过 VLM。
        self._intercept_url_pattern: str | None = None  # URL 匹配关键词（子串匹配）
        self._intercepted_data: list | None = None       # 首次命中的数据快照
        self._intercept_seen_row_keys: set[str] = set()
        self._intercept_schema_fingerprints: set[str] = set()
        self._intercept_endpoint_scores: dict[str, int] = {}
        self._network_run_id: str | None = None
        self._network_candidates_seen: set[str] = set()
        self._upload_file: Path | None = None   # --upload-file 预配置路径
        self.last_navigation_status: int | None = None
        self.last_navigation_url: str = ""
        self.last_download_path: str = ""
        self.last_download_name: str = ""
        self._last_action_error: Exception | None = None  # 自愈：记录本轮操作异常
        self._last_action_result: ActionResult | None = None
        self._tab_switch_notice: str | None = None  # 标签页切换感知通知
        # 机器可读的分级（G1 观测性强化）。set_tab_notice() 会自动写入；
        # 旧式直接赋值 _tab_switch_notice 仍可用（视为 "info"）。
        # 取值: "info" | "warn" | "error"
        self._last_notice_severity: str = "info"
        self._last_native_dialog: dict | None = None  # 原生 alert/confirm/prompt 本次拦截文本
        self._next_prompt_response: str | None = None  # 由 set_prompt_response 预装填的下一次 prompt() 回填值
        # Real page title cache — populated lazily by ``domcontentloaded`` /
        # ``framenavigated`` listeners in ``_register_page``. Keyed by
        # ``id(Page)`` (object identity, so close/reopen with same URL gets
        # a fresh entry). Used by ``tab_state_tracker.snapshot_tabs`` to
        # surface real titles (not URL-derived guesses) to the VLM —
        # otherwise the VLM's thought-cached "title" can stay stale forever
        # (run_log_20260514_183718: VLM kept calling tab [1] "百度文心助手"
        # even though it had become a "rules / terms" page).
        self._page_titles: dict[int, str] = {}
        self._last_som_elements: list[dict] = []  # 最近一轮 SoM 标记的元素列表
        self._last_som_scope: str = "full"  # E2：最近一轮 SoM 选区（viewport/full/selector）
        self._visual_blank_reloaded_urls: set[str] = set()
        self._auth_matrix_note: str = ""
        self.auth_matrix_loaded: bool = False
        self.auth_stale_detected: bool = False
        self.auth_stale_reason: str = ""
        self.auth_status_note: str = ""  # Auth Matrix / Sentinel status injected into prompts
        # ── @eN 别名映射（Wave 3 语义快照）──
        # key="@eN" → {som_id: int, role, name, selector}
        # 每轮 extract_accessibility_tree() 时刷新；供 _coerce_target_id 反解。
        self.element_mapping: dict[str, dict] = {}
        # ── 语义 Locator 自愈频次计数（Wave 3 fallback 观测）──
        # `[data-som-id=N]` 查不到元素时，尝试 get_by_role(role, name=, exact=True)
        # 命中 1 个即用，否则放弃。计数非 0 说明 SPA 重渲真的在吃 data-som-id。
        self._semantic_fallback_count: int = 0
        # ── RPA 肌肉记忆：记录本次任务每步成功动作的真实 XPath / 坐标 ──────
        self.rpa_trail: list[dict] = []
        # ── Tab Visit Stack ────────────────────────────────────────────
        # 栈记录"从哪个 tab 跳过来的"，用于 click_new_tab → close_tab 的
        # 父子回溯。栈顶 = 最近的父 tab。
        #   click_new_tab 成功后：push 当前 page
        #   close_tab 时：pop 栈顶，焦点切到那个 page（跳过已关闭的栈项）
        #   switch_tab 是 VLM 自主侧向导航，不动栈（保留原回溯路径）
        # 存 Page 对象而非 index，因为 close 会让索引漂移。
        self._tab_visit_stack: list[Page] = []

    def clear_rpa_trail(self) -> None:
        """清空本次任务的 RPA 动作轨迹，在任务开始前调用。"""
        self.rpa_trail.clear()
        logger.debug("[RPA] Trail cleared")

    # ── Tab Visit Stack API ───────────────────────────────────────────────
    def push_tab_visit(self, parent_page: Page) -> None:
        """Called by click_new_tab handler when a child tab is opened.

        Records ``parent_page`` so a subsequent close_tab on the child can
        return to the parent. No-op if ``parent_page`` is already at the
        top of the stack (avoid duplicate pushes from retries).
        """
        if parent_page is None or parent_page.is_closed():
            return
        if self._tab_visit_stack and self._tab_visit_stack[-1] is parent_page:
            return
        self._tab_visit_stack.append(parent_page)
        logger.info(
            "[TAB STACK] push parent (depth=%d): %s",
            len(self._tab_visit_stack),
            (parent_page.url or "about:blank")[:60],
        )

    def pop_tab_visit(self) -> Page | None:
        """Called by close_tab handler. Pop the most recent alive parent.

        Skips dead pages (parent might have been closed too). Returns None
        if the stack is empty or every entry is dead — caller falls back to
        ``open_pages[-1]`` for legacy behavior.
        """
        while self._tab_visit_stack:
            candidate = self._tab_visit_stack.pop()
            if candidate is not None and not candidate.is_closed():
                logger.info(
                    "[TAB STACK] pop -> parent: %s (remaining depth=%d)",
                    (candidate.url or "about:blank")[:60],
                    len(self._tab_visit_stack),
                )
                return candidate
        return None

    def peek_tab_visit(self) -> Page | None:
        """Read the stack top without popping. Skips dead pages but keeps
        them on the stack (caller may decide to clean later)."""
        for candidate in reversed(self._tab_visit_stack):
            if candidate is not None and not candidate.is_closed():
                return candidate
        return None

    def clear_tab_visit_stack(self) -> None:
        """Reset the stack at task boundaries."""
        self._tab_visit_stack.clear()
        logger.debug("[TAB STACK] cleared")

    def find_pagination_links(self) -> list[dict]:
        """
        从最近一轮 SoM 标记结果中查找翻页链接。

        扫描 ``_last_som_elements`` 中 name/text 匹配翻页关键词的元素，
        返回 ``[{"id": 81, "name": "More", "role": "link"}, ...]``。
        """
        import re as _re
        _PAG_RE = _re.compile(
            r'^(more|next|next\s*page|load\s*more|older|'
            r'下一页|下页|下一頁|下頁|更多|加载更多|'
            r'>|›|»|▶|→|\d+)$',
            _re.IGNORECASE,
        )
        hits: list[dict] = []
        for el in self._last_som_elements:
            name = (el.get("name") or "").strip()
            if _PAG_RE.match(name):
                hits.append({
                    "id": el.get("id"),
                    "name": name,
                    "role": el.get("role") or el.get("tag") or "?",
                })
        return hits

    async def probe_pagination(self) -> dict:
        """主动探测分页器：滚到底 + 扫 AX Tree element_mapping。

        修复 HN AI Agent 任务那种"首屏看不到分页器 → VLM 误判无限滚动 → 6 步
        smooth_scroll 弯路"的场景。在 extract 之前调一次：
          - 滚到 document.body.scrollHeight
          - 等 1s 让懒加载分页器进入 AX Tree
          - 扫 element_mapping 找翻页类按钮（数字/Next/下一页/›/»）
          - 滚回原位置（避免破坏 VLM 视觉锚点）

        Returns:
            {
                "has_paginator": bool,
                "candidates": [{ref, role, name, som_id}, ...],  # 命中的分页元素
                "kind": "numeric" | "next_only" | "infinite",
            }
        """
        import re as _re
        page = await self._ensure_active_page(reason="probe_pagination")
        if not page:
            return {"has_paginator": False, "candidates": [], "kind": "infinite"}

        _scroll_top_before = 0
        try:
            _scroll_top_before = await page.evaluate("() => window.scrollY") or 0
        except Exception:
            pass

        # 滚到底触发懒加载分页器
        try:
            await page.evaluate(
                "() => window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'})"
            )
            await asyncio.sleep(1.0)  # 等懒加载
        except Exception:
            pass

        # 重新提取 AX Tree（更新 element_mapping）
        try:
            await self.extract_accessibility_tree()
        except Exception as e:
            logger.debug(f"[PROBE PAGE] re-extract AX 失败：{e}")

        _NUM_RE = _re.compile(r"^\d{1,3}$")
        _NEXT_RE = _re.compile(
            r"^(next|next\s*page|more|older|下一页|下页|下一頁|下頁|"
            r"更多|加载更多|>|›|»|▶|→)$",
            _re.IGNORECASE,
        )
        dom_probe: dict = {"candidates": []}
        try:
            dom_probe = await page.evaluate(
                """() => {
                    const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
                    const visible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                            st.visibility !== 'hidden' && st.display !== 'none';
                    };
                    const disabled = (el) => Boolean(
                        el && (
                            el.disabled ||
                            el.getAttribute('aria-disabled') === 'true' ||
                            /\\b(disabled|is-disabled|dt-paging-button disabled|paginate_button disabled|ant-pagination-disabled|el-pagination__disabled)\\b/i
                                .test(String(el.className || ''))
                        )
                    );
                    const textOf = (el) => norm(
                        el.getAttribute('aria-label') ||
                        el.getAttribute('title') ||
                        el.textContent ||
                        el.value ||
                        ''
                    );
                    const clickable = (el) => el.closest?.('a,button,[role=button],[role=link],li,span') || el;
                    const scopeSelectors = [
                        '.dt-paging', '.dataTables_paginate', '.dataTables_wrapper .pagination',
                        '.paginate_button', '[data-dt-idx]',
                        '.el-pagination', '.ant-pagination', '.n-pagination', '.v-pagination',
                        '.pagination', '.pager', '[class*="pagination"]', '[class*="pager"]',
                        'nav[aria-label*="pagination" i]', 'nav[aria-label*="page" i]'
                    ];
                    const scopeEls = Array.from(document.querySelectorAll(scopeSelectors.join(','))).filter(visible);
                    const roots = Array.from(new Set(scopeEls.map((el) =>
                        el.closest?.('.dt-paging,.dataTables_paginate,.dataTables_wrapper,.el-pagination,.ant-pagination,.n-pagination,.v-pagination,.pagination,.pager,nav') || el
                    ))).filter(visible);
                    const nextRe = /^(next|next page|more|older|下一页|下页|后页|更多|加载更多|>|›|»|→)$/i;
                    const candidates = [];
                    let hasNumeric = false;
                    let hasNext = false;
                    const pushCandidate = (el, strategy, score) => {
                        const target = clickable(el);
                        if (!visible(target) || disabled(target)) return;
                        const label = textOf(el) || textOf(target);
                        if (!label) return;
                        candidates.push({
                            source: 'dom',
                            strategy,
                            role: (target.getAttribute('role') || target.tagName || '').toLowerCase(),
                            name: label.slice(0, 80),
                            score,
                        });
                    };
                    for (const root of roots) {
                        const rootClass = String(root.className || '').toLowerCase();
                        const controls = Array.from(root.querySelectorAll('a,button,[role=button],[role=link],li,span,[data-dt-idx]'))
                            .filter(visible);
                        for (const el of controls) {
                            const label = textOf(el);
                            const cls = String(el.className || '').toLowerCase();
                            const aria = norm(el.getAttribute('aria-label') || '').toLowerCase();
                            if (/^\\d{1,3}$/.test(label)) {
                                hasNumeric = true;
                                pushCandidate(el, rootClass.includes('dt') ? 'datatables_numeric' : 'dom_numeric', 50);
                            } else if (
                                nextRe.test(label) ||
                                /\\b(next|paginate_button next|dt-paging-button next|pagination-next|pager-next)\\b/i.test(cls) ||
                                /next|下一页|后页/.test(aria)
                            ) {
                                hasNext = true;
                                pushCandidate(el, rootClass.includes('dt') ? 'datatables_next' : 'dom_next', 90);
                            }
                        }
                    }
                    candidates.sort((a, b) => b.score - a.score);
                    return {
                        candidates: candidates.slice(0, 12),
                        kind: hasNumeric ? 'numeric' : hasNext ? 'next_only' : 'infinite',
                    };
                }"""
            )
        except Exception as e:
            logger.debug(f"[PROBE PAGE] DOM probe failed: {e}")
        candidates: list[dict] = []
        has_numeric = False
        has_next = False
        mapping = getattr(self, "element_mapping", {}) or {}
        for ref, meta in mapping.items():
            role = (meta.get("role") or "").lower()
            name = (meta.get("name") or "").strip()
            if role not in ("button", "link"):
                continue
            if _NUM_RE.match(name):
                has_numeric = True
                candidates.append({
                    "ref": ref, "role": role, "name": name, "source": "ax",
                    "som_id": meta.get("som_id"),
                })
            elif _NEXT_RE.match(name):
                has_next = True
                candidates.append({
                    "ref": ref, "role": role, "name": name, "source": "ax",
                    "som_id": meta.get("som_id"),
                })
        for idx, cand in enumerate((dom_probe or {}).get("candidates", []) or []):
            name = str(cand.get("name") or "").strip()
            if not name:
                continue
            if _NUM_RE.match(name):
                has_numeric = True
            elif _NEXT_RE.match(name) or "next" in str(cand.get("strategy", "")).lower():
                has_next = True
            candidates.append({
                "ref": f"dom:{idx + 1}",
                "role": cand.get("role") or "button",
                "name": name,
                "source": cand.get("source") or "dom",
                "strategy": cand.get("strategy") or "dom_pagination",
                "som_id": None,
            })

        # 滚回原位置，不破坏 VLM 视觉锚点
        try:
            await page.evaluate(f"() => window.scrollTo({{top: {_scroll_top_before}, behavior: 'instant'}})")
            await asyncio.sleep(0.3)
        except Exception:
            pass

        kind = (
            "numeric" if has_numeric
            else "next_only" if has_next
            else "infinite"
        )
        if candidates:
            logger.info(
                "[PROBE PAGE] candidates=%s",
                ", ".join(
                    f"{c.get('ref')}:{c.get('name')}:{c.get('strategy') or c.get('source')}"
                    for c in candidates[:8]
                ),
            )
        return {
            "has_paginator": bool(candidates),
            "candidates": candidates,
            "kind": kind,
        }

    async def _get_xpath(self, handle) -> str:
        """
        注入 JS 提取 DOM 元素的绝对 XPath 字符串。

        优先使用 id 属性（短路径），其次递归逐级拼接标签+位置索引。
        失败时静默返回空字符串，不阻断主流程。
        """
        _XPATH_JS = """
        (el) => {
            const nodeName = (node) => (node && (node.localName || node.tagName) ? String(node.localName || node.tagName).toLowerCase() : '');
            const esc = (value) => JSON.stringify(String(value));
            function getXPath(e) {
                if (!e || e.nodeType !== 1) return '';
                if (e.id) return '//*[@id=' + esc(e.id) + ']';
                if (e === document.documentElement) return '/html[1]';
                if (e === document.body) return '/html[1]/body[1]';
                const segments = [];
                let current = e;
                while (current && current.nodeType === 1 && current !== document.documentElement) {
                    const tag = nodeName(current);
                    let index = 1;
                    let sibling = current.previousElementSibling;
                    while (sibling) {
                        if (nodeName(sibling) === tag) index += 1;
                        sibling = sibling.previousElementSibling;
                    }
                    segments.unshift('/' + tag + '[' + index + ']');
                    current = current.parentElement;
                }
                return '/html[1]' + segments.join('');
            }
            return getXPath(el);
        }
        """
        try:
            return await handle.evaluate(_XPATH_JS) or ""
        except Exception as _xe:
            logger.debug(f"[RPA] XPath extraction failed (non-fatal): {_xe}")
            return ""

    # ── CDP AX Tree（替代 Playwright 1.58 已移除的 page.accessibility）──────
    async def _get_ax_tree_via_cdp(
        self, page: Page, interesting_only: bool = True
    ) -> dict | None:
        """
        通过 Chrome DevTools Protocol 获取完整 Accessibility Tree。

        Playwright ≥ 1.58 移除了 ``page.accessibility.snapshot()``，
        此方法使用 ``Accessibility.getFullAXTree`` CDP 命令替代，
        并将 CDP 扁平节点列表重建为与旧 API 兼容的树形 dict。

        Args:
            page: 当前活动页。
            interesting_only: True 时过滤掉 ignored / generic / none 等
                              装饰性容器节点（仅保留有语义价值的节点）。

        Returns:
            与旧 ``page.accessibility.snapshot()`` 格式兼容的树形 dict，
            失败时返回 None。
        """
        cdp = None
        try:
            cdp = await page.context.new_cdp_session(page)
            result = await cdp.send("Accessibility.getFullAXTree")
            nodes = result.get("nodes", [])
            if not nodes:
                return None

            # ── 1. 将 CDP 扁平节点转为简洁 dict ──
            by_id: dict[str, dict] = {}
            for raw in nodes:
                nid = raw.get("nodeId")
                if nid is None:
                    continue
                role_val = (raw.get("role") or {}).get("value", "")
                name_val = (raw.get("name") or {}).get("value", "")
                val_obj = raw.get("value") or {}
                entry: dict = {"role": role_val, "name": name_val}
                if val_obj.get("value") is not None:
                    entry["value"] = str(val_obj["value"])
                for prop in raw.get("properties", []):
                    pname = prop.get("name", "")
                    pval = (prop.get("value") or {}).get("value")
                    if pname and pval is not None:
                        entry[pname] = pval
                entry["_cids"] = raw.get("childIds", [])
                entry["_ign"] = raw.get("ignored", False)
                by_id[nid] = entry

            # ── 2. 递归重建树 ──
            SKIP_ROLES = frozenset(
                {"none", "presentation", "generic", "InlineTextBox", "LineBreak"}
            )

            def _to_tree(nid: str) -> dict | list | None:
                node = by_id.get(nid)
                if node is None:
                    return None
                children: list[dict] = []
                for cid in node["_cids"]:
                    child = _to_tree(cid)
                    if child is None:
                        continue
                    if isinstance(child, list):
                        children.extend(child)
                    else:
                        children.append(child)
                # interesting_only 模式下跳过被忽略/装饰性节点，
                # 但保留其子节点向上传递
                if interesting_only and (
                    node["_ign"] or node["role"] in SKIP_ROLES
                ):
                    return children or None
                out = {k: v for k, v in node.items() if not k.startswith("_")}
                if children:
                    out["children"] = children
                return out

            root = _to_tree(nodes[0]["nodeId"])
            if isinstance(root, list):
                return {"role": "RootWebArea", "name": "", "children": root}
            return root

        except Exception as e:
            logger.warning(f"[AX CDP] 通过 CDP 获取 AX Tree 失败: {e}")
            return None
        finally:
            if cdp:
                try:
                    await cdp.detach()
                except Exception:
                    pass

    async def _get_accessibility_signature(self, page: Page, handle) -> tuple[str, str]:
        """提取元素的语义角色与可访问名称，优先走轻量 JS 提取。"""
        _AX_JS = r"""
        (el) => {
            const clean = (value) => (value || '')
                .toString()
                .replace(/\s+/g, ' ')
                .trim()
                .slice(0, 120);

            const implicitRole = (node) => {
                if (!node || !node.tagName) return '';
                const tag = node.tagName.toLowerCase();
                const type = (node.getAttribute && (node.getAttribute('type') || '') || '').toLowerCase();
                const role = clean(node.getAttribute && node.getAttribute('role'));
                if (role) return role.toLowerCase();
                if (tag === 'a' && node.hasAttribute && node.hasAttribute('href')) return 'link';
                if (tag === 'button') return 'button';
                if (tag === 'summary') return 'button';
                if (tag === 'textarea') return 'textbox';
                if (tag === 'select') return 'combobox';
                if (tag === 'input') {
                    if (['button', 'submit', 'reset'].includes(type)) return 'button';
                    if (type === 'checkbox') return 'checkbox';
                    if (type === 'radio') return 'radio';
                    if (type === 'search') return 'searchbox';
                    if (type === 'range') return 'slider';
                    return 'textbox';
                }
                if (node.isContentEditable) return 'textbox';
                if (tag === 'img') return 'img';
                if (tag === 'option') return 'option';
                return tag;
            };

            const accessibleName = (node) => {
                if (!node || !node.getAttribute) return '';
                const labelledBy = clean(node.getAttribute('aria-labelledby'));
                if (labelledBy) {
                    const parts = labelledBy.split(/\s+/)
                        .map((id) => document.getElementById(id))
                        .filter(Boolean)
                        .map((n) => clean(n.textContent));
                    const joined = clean(parts.join(' '));
                    if (joined) return joined;
                }
                const ariaLabel = clean(node.getAttribute('aria-label'));
                if (ariaLabel) return ariaLabel;
                const title = clean(node.getAttribute('title'));
                if (title) return title;
                const placeholder = clean(node.getAttribute('placeholder'));
                if (placeholder) return placeholder;
                const alt = clean(node.getAttribute('alt'));
                if (alt) return alt;
                const text = clean(node.textContent);
                if (text) return text;
                return '';
            };

            return { role: implicitRole(el), name: accessibleName(el) };
        }
        """
        role = ""
        name = ""
        try:
            raw = await handle.evaluate(_AX_JS)
            if isinstance(raw, dict):
                role = str(raw.get("role") or "").strip().lower()
                name = str(raw.get("name") or "").strip()
        except Exception as _xe:
            logger.debug(f"[RPA] AX JS signature extraction failed (non-fatal): {_xe}")

        return role, name

    def _track_background_task(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(lambda finished: self._background_tasks.discard(finished))

    async def _dismiss_permission_surfaces(self, reason: str = "") -> bool:
        """
        清理站内权限说明遮罩、语音搜索浮层等干扰层。
        优先尝试点击“拒绝/关闭/取消”，找不到按钮时再隐藏整块遮罩。
        """
        page = await self._ensure_active_page(reason=f"permission sweep {reason}".strip())
        if not page:
            return False

        dismiss_js = r"""
() => {
    const host = (location.hostname || '').toLowerCase();
    if (host.endsWith('baidu.com') || host.endsWith('baidu.cn')) {
        return { dismissed: 0, hidden: 0 };
    }

    const PERMISSION_PATTERNS = [
        /麦克风/, /语音/, /语音搜索/, /使用语音进行搜索/,
        /microphone/i, /voice search/i, /use your microphone/i,
        /camera/i, /摄像头/,
        /notification/i, /通知/, /push/i,
        /location/i, /geolocation/i, /位置/,
        /permission/i, /allow/i, /授权/,
    ];
    const DISMISS_TEXTS = new Set([
        '拒绝', '不允许', '取消', '关闭', '稍后', '以后再说', '稍后再说', '暂不',
        '知道了', '我知道了', '不用了',
        'Not now', 'No thanks', 'Dismiss', 'Close', 'Cancel', 'Block', 'Deny',
    ]);

    function isVisible(el) {
        if (!el || !el.getBoundingClientRect) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width < 8 || rect.height < 8) return false;
        const style = window.getComputedStyle(el);
        return (
            style.display !== 'none' &&
            style.visibility !== 'hidden' &&
            parseFloat(style.opacity || '1') > 0.05
        );
    }

    function normalize(text) {
        return (text || '').replace(/\s+/g, ' ').trim();
    }

    function matchesPermissionText(text) {
        const normalized = normalize(text).slice(0, 400);
        if (!normalized) return false;
        return PERMISSION_PATTERNS.some((pattern) => pattern.test(normalized));
    }

    function isModalLike(el) {
        if (!isVisible(el)) return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        const position = style.position || '';
        const tag = (el.tagName || '').toLowerCase();
        const id = (el.id || '').toLowerCase();
        if (tag === 'html' || tag === 'body') return false;
        if (id === 'wrapper' || id === 'head-wrapper' || id === 's_wrap') return false;
        if (rect.width >= window.innerWidth * 0.95 && rect.height >= window.innerHeight * 0.95) {
            return false;
        }
        const zIndex = Number.parseInt(style.zIndex || '0', 10) || 0;
        const centeredX = Math.abs((rect.left + rect.width / 2) - (window.innerWidth / 2)) < window.innerWidth * 0.4;
        const centeredY = Math.abs((rect.top + rect.height / 2) - (window.innerHeight / 2)) < window.innerHeight * 0.4;
        return (
            el.matches('dialog, [role="dialog"], [aria-modal="true"]') ||
            position === 'fixed' ||
            position === 'sticky' ||
            zIndex >= 100 ||
            (centeredX && centeredY && rect.width >= 180 && rect.height >= 80)
        );
    }

    function hideElement(el) {
        if (!el || !el.style) return false;
        const tag = (el.tagName || '').toLowerCase();
        const id = (el.id || '').toLowerCase();
        if (tag === 'html' || tag === 'body') return false;
        if (id === 'wrapper' || id === 'head-wrapper' || id === 's_wrap') return false;
        const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : { width: 0, height: 0 };
        if (rect.width >= window.innerWidth * 0.95 && rect.height >= window.innerHeight * 0.95) return false;
        el.style.setProperty('display', 'none', 'important');
        el.style.setProperty('visibility', 'hidden', 'important');
        el.style.setProperty('opacity', '0', 'important');
        el.style.setProperty('pointer-events', 'none', 'important');
        return true;
    }

    function clickDismissButtons(container) {
        let clicked = 0;
        const candidates = container.querySelectorAll('button, [role="button"], a, span, div');
        for (const el of candidates) {
            if (!isVisible(el)) continue;
            const text = normalize(el.innerText || el.textContent || '');
            if (!text || !DISMISS_TEXTS.has(text)) continue;
            try {
                el.click();
                clicked += 1;
            } catch (_) {}
        }
        return clicked;
    }

    function hidePossibleBackdrop(container) {
        const nearby = [
            container.previousElementSibling,
            container.nextElementSibling,
            container.parentElement,
        ];
        for (const candidate of nearby) {
            if (!candidate || !isVisible(candidate) || candidate === document.body) continue;
            const rect = candidate.getBoundingClientRect();
            if (
                rect.width >= window.innerWidth * 0.8 &&
                rect.height >= window.innerHeight * 0.6
            ) {
                hideElement(candidate);
            }
        }
    }

    let dismissed = 0;
    let hidden = 0;
    const seen = new WeakSet();
    const containers = document.querySelectorAll(
        'dialog, [role="dialog"], [aria-modal="true"], .modal, .popup, .popover, .overlay, .mask, .drawer'
    );

    for (const container of containers) {
        if (seen.has(container) || !isModalLike(container)) continue;
        seen.add(container);
        const text = normalize(container.innerText || container.textContent || '');
        if (!matchesPermissionText(text)) continue;

        dismissed += clickDismissButtons(container);
        if (isVisible(container)) {
            if (hideElement(container)) hidden += 1;
            hidePossibleBackdrop(container);
        }
    }

    return { dismissed, hidden };
}
"""

        handled = False
        for frame in page.frames:
            try:
                result = await frame.evaluate(dismiss_js)
            except Exception:
                continue

            dismissed = int((result or {}).get("dismissed", 0) or 0)
            hidden = int((result or {}).get("hidden", 0) or 0)
            if dismissed or hidden:
                handled = True
                logger.info(
                    f"[PERMISSION GUARD] Cleared permission surface "
                    f"(dismissed={dismissed}, hidden={hidden})"
                    f"{f' | {reason}' if reason else ''}"
                )
        return handled

    async def _dismiss_login_popup(self, reason: str = "") -> bool:
        """
        清除站内登录引导弹窗（非权限类浮层）。

        很多站点（如 B 站、知乎）在未登录时会弹出"登录后你可以..."的浮层，
        遮挡列表内容导致 VLM 提取漏单。此方法在截图前自动移除。
        """
        page = await self._ensure_active_page(reason=f"login popup sweep {reason}".strip())
        if not page:
            return False

        _LOGIN_POPUP_JS = r"""
() => {
    // 登录引导弹窗的典型特征词
    const LOGIN_HINTS = [
        /登录后/i, /登录.*可以/i, /登录.*体验/i, /请先登录/i,
        /sign\s*in/i, /log\s*in/i, /注册.*登录/i, /立即登录/i,
        /登录.*注册/i, /首次使用/i, /login.*to\s+continue/i,
    ];

    // 安全列表：不能隐藏的根级容器
    const SAFE_IDS = new Set([
        'app', 'root', '__next', 'wrapper', 'head-wrapper',
        's_wrap', 'main', 'content',
    ]);

    function isVisible(el) {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        if (r.width < 20 || r.height < 20) return false;
        const s = window.getComputedStyle(el);
        return s.display !== 'none' && s.visibility !== 'hidden'
            && parseFloat(s.opacity || '1') > 0.05;
    }

    function isFloating(el) {
        const s = window.getComputedStyle(el);
        const pos = s.position || '';
        const z = parseInt(s.zIndex || '0', 10) || 0;
        return pos === 'fixed' || pos === 'absolute' || z >= 50
            || el.matches('[role="dialog"], dialog, [aria-modal="true"]');
    }

    function matchLogin(text) {
        const t = (text || '').replace(/\s+/g, ' ').trim().slice(0, 600);
        return t && LOGIN_HINTS.some(p => p.test(t));
    }

    let hidden = 0;
    // 查找所有浮动层
    const candidates = document.querySelectorAll(
        '[role="dialog"], dialog, [aria-modal="true"], ' +
        '.modal, .popup, .popover, .overlay, .mask, .drawer, ' +
        '.login-panel, .login-tip, .login-guide, .unlogin-popover'
    );

    for (const el of candidates) {
        if (!isVisible(el)) continue;
        const id = (el.id || '').toLowerCase();
        const tag = (el.tagName || '').toLowerCase();
        if (tag === 'html' || tag === 'body' || SAFE_IDS.has(id)) continue;
        // 不能是全屏容器
        const r = el.getBoundingClientRect();
        if (r.width >= window.innerWidth * 0.9 && r.height >= window.innerHeight * 0.9) continue;

        const text = el.innerText || el.textContent || '';
        if (!matchLogin(text)) continue;
        if (!isFloating(el)) continue;

        // 尝试点击关闭按钮
        const closeBtn = el.querySelector(
            '[class*="close"], [aria-label*="关闭"], [aria-label*="Close"], ' +
            '.close-btn, .modal-close, .dialog-close'
        );
        if (closeBtn) {
            try { closeBtn.click(); } catch(_) {}
        }
        // ── 语义级隐身 (Semantic Invisibility) ──────────────────────
        // 不使用 el.remove()：React/Vue 等 SPA 框架的 Virtual DOM
        // 与真实 DOM 保持双向同步，暴力移除节点会导致 VDOM 状态树
        // 不一致，轻则局部渲染异常，重则整页白屏崩溃。
        // 改用五重属性注入，同时满足：
        //   ① 视觉不可见（display:none + opacity:0）
        //   ② AX Tree 不可达（aria-hidden + inert）
        //   ③ 物理不可交互（pointer-events:none + inert）
        //   ④ 前端框架 VDOM 状态不受影响（节点仍在 DOM 树中）
        try {
            el.style.setProperty('display', 'none', 'important');
            el.style.setProperty('opacity', '0', 'important');
            el.style.setProperty('pointer-events', 'none', 'important');
            el.setAttribute('aria-hidden', 'true');
            el.inert = true;
        } catch(_) {}
        hidden += 1;

        // 语义级隐身相邻遮罩背景（保护 VDOM，不做 remove）
        for (const sib of [el.previousElementSibling, el.nextElementSibling]) {
            if (!sib || sib === document.body || !isVisible(sib)) continue;
            const sr = sib.getBoundingClientRect();
            if (sr.width >= window.innerWidth * 0.7 && sr.height >= window.innerHeight * 0.5) {
                try {
                    sib.style.setProperty('display', 'none', 'important');
                    sib.style.setProperty('opacity', '0', 'important');
                    sib.style.setProperty('pointer-events', 'none', 'important');
                    sib.setAttribute('aria-hidden', 'true');
                    sib.inert = true;
                } catch(_) {}
            }
        }
    }

    return { hidden };
}
"""
        handled = False
        for frame in page.frames:
            try:
                result = await frame.evaluate(_LOGIN_POPUP_JS)
            except Exception:
                continue
            if isinstance(result, dict) and result.get("hidden", 0) > 0:
                handled = True
                logger.info(
                    f"[LOGIN POPUP GUARD] Dismissed login popup "
                    f"(hidden={result['hidden']})"
                    f"{f' | {reason}' if reason else ''}"
                )
        return handled

    def _handle_page_closed(self, closed_page: Page) -> None:
        if self._page == closed_page:
            self._page = None
            if self._context and self._context.pages:
                self._track_background_task(
                    self._ensure_active_page(reason="active page closed")
                )

    async def _activate_page(self, page: Page, reason: str = "") -> None:
        if not page or page.is_closed():
            return

        previous = self._page
        if previous != page:
            previous_url = (
                previous.url[:80]
                if previous and not previous.is_closed() and previous.url
                else "<none>"
            )
            next_url = page.url[:80] if page.url else "about:blank"
            logger.info(
                f"[PAGE SWITCH] {reason or 'active page updated'} | "
                f"{previous_url} -> {next_url}"
            )

        self._page = page
        try:
            await page.bring_to_front()
        except Exception:
            pass

    async def _apply_cdp_ua_override(self, page: Page) -> None:
        """STEALTH-3: drive CDP Emulation.setUserAgentOverride so the JS-side
        navigator.userAgentData (and workers / cross-origin iframes) advertise
        the same identity as the header-level UA + Client Hints. Header
        injection alone never populates navigator.userAgentData, so a spoofed
        UA with empty/!mismatched high-entropy hints is itself a bot tell.
        Best-effort: never breaks page registration."""
        profile = getattr(self, "_stealth_profile", None)
        if profile is None:
            return
        try:
            from .stealth_profile import build_cdp_ua_override
        except ImportError:  # pragma: no cover - script/relative import shim
            from stealth_profile import build_cdp_ua_override  # type: ignore[no-redef]
        try:
            cdp = await page.context.new_cdp_session(page)
            await cdp.send(
                "Emulation.setUserAgentOverride",
                build_cdp_ua_override(profile),
            )
        except Exception as _ua_err:
            logger.debug("[STEALTH-3] setUserAgentOverride skipped: %s", _ua_err)

    async def _register_page(
        self, page: Page, reason: str = "", activate: bool = True
    ) -> None:
        if not page or page.is_closed():
            return

        page_key = id(page)
        if page_key not in self._registered_pages:
            self._registered_pages.add(page_key)
            await self._apply_cdp_ua_override(page)
            page.on("response", self._handle_xhr_response)
            page.on("download", self._handle_download)
            page.on(
                "popup",
                lambda popup: self._track_background_task(
                    self._register_page(
                        popup,
                        reason=f"popup from {page.url[:80] if page.url else 'about:blank'}",
                        activate=True,
                    )
                ),
            )
            page.on("close", lambda *_: self._handle_page_closed(page))

            # ── Real page title cache ────────────────────────────────────
            # Fire-and-forget title refresh on navigation events. The cached
            # value is read SYNC by ``tab_state_tracker.snapshot_tabs`` so
            # the TAB STATE CHANGE notice surfaces the *actual* page title
            # rather than a URL-tail guess. This is the fix for the
            # 18:37 Wenxin run where VLM kept calling tab [1] "百度文心助手"
            # even though it had become a terms-of-service page.
            async def _refresh_title(p: Page) -> None:
                try:
                    if p.is_closed():
                        self._page_titles.pop(id(p), None)
                        return
                    t = (await p.title()) or ""
                    if t:
                        self._page_titles[id(p)] = t
                except Exception as _title_err:
                    logger.debug(
                        "[TITLE CACHE] refresh failed for %s: %s",
                        (p.url or "?")[:60], _title_err,
                    )

            # Bind ``page`` by default-arg so the lambda captures the
            # specific Page object instead of the loop variable.
            page.on(
                "domcontentloaded",
                lambda _p=page: self._track_background_task(_refresh_title(_p)),
            )
            page.on(
                "framenavigated",
                lambda _frame, _p=page: self._track_background_task(_refresh_title(_p))
                if _frame == _p.main_frame else None,
            )
            # Initial fetch — page may already be loaded by the time we
            # register (e.g. context.new_page() returns ready).
            self._track_background_task(_refresh_title(page))

            # 自动处理 alert/confirm/prompt 弹窗，避免阻塞 Agent

            async def _auto_dismiss_dialog(dialog):
                # NOTE: This used to be a fire-and-forget acceptor with no
                # VLM feedback path. Native confirm()/alert()/prompt() are
                # *not* in DOM, so the VLM never sees them and would loop
                # ("click delete again -- still there!"). We now surface the
                # dialog text via _last_native_dialog + _tab_switch_notice
                # so the next turn knows the destructive action was confirmed.
                _type = "dialog"
                _msg = ""
                try:
                    _type = str(dialog.type or "dialog")
                    _msg = str(dialog.message or "")[:200]
                    _armed = self._next_prompt_response
                    if _type == "prompt" and _armed is not None:
                        # Consume the armed value (one-shot).
                        self._next_prompt_response = None
                        logger.info(
                            "[DIALOG] Auto-accepting %s with armed value: %r",
                            _type, _armed[:80],
                        )
                        await dialog.accept(prompt_text=_armed)
                        _msg = f"[ARMED] {_armed}"
                    else:
                        logger.info(
                            "[DIALOG] Auto-accepting %s: %s", _type, _msg[:100]
                        )
                        await dialog.accept()
                except Exception as e:
                    logger.debug("[DIALOG] Accept failed: %s", e)
                    return
                try:
                    # Persist for AX summary / debugging
                    self._last_native_dialog = {
                        "type": _type,
                        "message": _msg,
                        "ts": time.time(),
                    }
                except Exception:
                    pass
                try:
                    _notice = (
                        f"✅ [DIALOG ACCEPTED] type={_type} "
                        f"message={_msg!r}\n"
                        "→ 浏览器原生确认弹窗已被系统自动 accept，"
                        "上一步的破坏性操作（删除/提交/清空）"
                        "已真正生效。下一步看页面状态确认结果即可。"
                    )
                    # J: coalesce=True is the helper-level equivalent of the
                    # old "if not prior: set; else: append" branch — when no
                    # prior notice exists we just set, otherwise we append
                    # with a blank-line separator. set_tab_notice also keeps
                    # _last_notice_severity in sync (taking max(prior, new))
                    # so observability layers see the right level.
                    self.set_tab_notice(_notice, severity="info", coalesce=True)
                except Exception:
                    pass

            page.on("dialog", lambda d: self._track_background_task(_auto_dismiss_dialog(d)))

            try:
                await Stealth().apply_stealth_async(page)
            except Exception as exc:
                logger.debug(f"[PAGE REGISTER] Stealth apply skipped: {exc}")

            # ── 网络资源拦截：过滤 media/font + 广告追踪域名 ─────────────────
            # 保留 image：视觉模式截图需要，去掉反而让 SoM 丢失背景语义。
            # abort 替代 fulfill(empty)，更快释放连接。
            async def _network_filter(route: Route) -> None:
                try:
                    rt = route.request.resource_type
                    url = route.request.url
                    if rt in _BLOCK_RESOURCE_TYPES:
                        await route.abort()
                        return
                    if any(domain in url for domain in _BLOCK_AD_DOMAINS):
                        await route.abort()
                        return
                    await route.continue_()
                except Exception:
                    # route 可能因页面关闭而失效，静默跳过
                    pass

            try:
                await page.route("**/*", _network_filter)
                logger.debug("[PAGE REGISTER] Network filter route installed")
            except Exception as _route_err:
                logger.debug(f"[PAGE REGISTER] Route install skipped: {_route_err}")

            logger.info(
                f"[PAGE REGISTER] {reason or 'page discovered'} | "
                f"{page.url[:80] if page.url else 'about:blank'}"
            )

        if activate:
            await self._activate_page(page, reason or "page registered")

    async def _ensure_active_page(self, reason: str = "") -> Page | None:
        if self._page and not self._page.is_closed():
            return self._page

        if not self._context or getattr(self, "_closed", False):
            return self._page

        open_pages = [page for page in self._context.pages if not page.is_closed()]
        if not open_pages:
            try:
                logger.warning(
                    "[TAB GUARD L1] No open pages during %s; creating emergency blank tab",
                    reason or "active-page recovery",
                )
                new_page = await self._context.new_page()
                await self._register_page(
                    new_page,
                    reason=reason or "emergency active page",
                    activate=True,
                )
                return self._page
            except Exception as exc:
                logger.error("[TAB GUARD L1] Failed to create emergency tab: %s", exc)
                self._page = None
                return None

        await self._register_page(
            open_pages[-1], reason=reason or "recover active page", activate=True
        )
        return self._page

    async def _adopt_latest_page(self, reason: str = "") -> None:
        if not self._context:
            return

        open_pages = [page for page in self._context.pages if not page.is_closed()]
        if not open_pages:
            return

        latest_page = open_pages[-1]
        if self._page != latest_page:
            await self._register_page(
                latest_page,
                reason=reason or "new window/tab detected",
                activate=True,
            )

    # ── G1 观测性：分级 notice 助手 ─────────────────────────────────────
    # 三档严重程度：
    #   info  — 正常进度反馈（页面切换、动作完成）
    #   warn  — 异常但不致命（候选不唯一、网络降级、HTTP 4xx）
    #   error — 操作失败需 VLM 切换策略（元素消失、HTTP 5xx、超时）
    # 用法：
    #   browser.set_tab_notice("点击成功", severity="info")
    #   browser.set_tab_notice("候选不唯一,已取首项", severity="warn", coalesce=True)
    #   browser.set_tab_notice("元素已消失", severity="error", coalesce=False)
    _NOTICE_TAG = {
        "info": "ℹ️ [INFO]",
        "warn": "⚠️ [WARN]",
        "error": "🚨 [ERROR]",
    }
    _SEVERITY_RANK = {"info": 0, "warn": 1, "error": 2}

    def set_tab_notice(
        self,
        text: str,
        *,
        severity: str = "info",
        coalesce: bool = True,
    ) -> None:
        """Set or append a notice that will be injected into the next VLM step.

        Parameters
        ----------
        text : str
            The notice content. If it already starts with a recognized emoji
            (ℹ️/⚠️/🚨/✅/🛑), the severity tag is **not** re-prepended (avoids
            double-stamping when handlers compose their own markers).
        severity : {"info", "warn", "error"}, default "info"
            Machine-readable severity. Recorded in ``_last_notice_severity``
            so observability layers (logs/api_server/frontend) can color or
            alert without parsing the text.
        coalesce : bool, default True
            If True and a notice is already present, append with a blank line
            separator (preserves earlier context — e.g. tab switch + drag
            done). If False, **overwrite** (use for fresh errors that should
            dominate the VLM's attention).

            When coalescing, the resulting severity is the **max** of the
            existing and incoming severity, so a later ``error`` upgrades an
            earlier ``info`` block but a later ``info`` cannot downgrade.
        """
        text = (text or "").strip()
        if not text:
            return
        if severity not in self._SEVERITY_RANK:
            severity = "info"
        # Re-stamp tag only when caller didn't bring their own visual marker.
        # Whitelist covers every emoji prefix in use by actions.py / browser_env.py
        # handlers (✅ success, ❌ fail, ⏭️ no-op, ↪️ redirect, ❓ unknown,
        # ☑️ check, ✂️ extract, 🗒️ note, 🔒 lock, ⏱️ timing, etc.) so the
        # migration to set_tab_notice doesn't double-stamp markers like
        # "❌ click_new_tab failed" into "🚨 ❌ click_new_tab failed".
        _SKIP_RESTAMP = (
            "ℹ️", "⚠️", "🚨", "✅", "🛑", "[",
            "❌", "⏭️", "↪️", "❓", "☑️", "✂️", "🗒️", "🔒", "⏱️",
        )
        if not text.startswith(_SKIP_RESTAMP):
            tag = self._NOTICE_TAG.get(severity, "")
            stamped = f"{tag} {text}" if tag else text
        else:
            stamped = text
        prior = self._tab_switch_notice
        if not prior:
            self._tab_switch_notice = stamped
            self._last_notice_severity = severity
            return
        if not coalesce:
            self._tab_switch_notice = stamped
            self._last_notice_severity = severity
            return
        # coalesce: append + take the higher severity
        self._tab_switch_notice = prior + "\n\n" + stamped
        prior_rank = self._SEVERITY_RANK.get(self._last_notice_severity, 0)
        new_rank = self._SEVERITY_RANK.get(severity, 0)
        if new_rank > prior_rank:
            self._last_notice_severity = severity

    def consume_tab_notice(self) -> tuple[str | None, str]:
        """Return ``(text, severity)`` and clear the slot atomically.

        Callers (main.py loop) should prefer this over reading
        ``_tab_switch_notice`` directly so severity stays in sync. Returns
        ``(None, "info")`` when nothing is pending.
        """
        text = self._tab_switch_notice
        severity = self._last_notice_severity
        if text is None:
            return None, "info"
        self._tab_switch_notice = None
        self._last_notice_severity = "info"
        return text, severity

    def clear_tab_notice(self) -> None:
        """Explicitly drop any pending notice and reset severity.

        Use this when a Tab Guard / state tracker has determined the
        previous handler's notice is no longer relevant for the next VLM
        step (e.g. no tab switch happened, but a stale notice from an
        earlier action would mislead). Prefer this over a bare
        ``self._tab_switch_notice = None`` assignment so the severity
        stays in lock-step.
        """
        self._tab_switch_notice = None
        self._last_notice_severity = "info"

    async def _clear_som_overlays(self) -> None:
        """Remove visual SoM overlays before interactions while keeping target ids."""
        page = await self._ensure_active_page()
        if not page:
            return

        cleanup_js = """() => {
            if (typeof window.__clearSomOverlays === 'function') {
                window.__clearSomOverlays();
                return true;
            }
            const container = document.querySelector('#__som_overlay_container');
            if (container && container.parentNode) {
                container.parentNode.removeChild(container);
            }
            document.querySelectorAll('.__som_border, .__som_label').forEach((el) => el.remove());
            return true;
        }"""

        for frame in page.frames:
            try:
                await frame.evaluate(cleanup_js)
            except Exception:
                continue

    async def _resolve_action_target(
        self, target_id: int, action_kind: str
    ) -> ActionTarget | None:
        """Resolve wrappers to the most likely real input or clickable element."""
        page = await self._ensure_active_page()
        if not page:
            return None

        # ── Fix 3：拦截幻觉 target_id（如 9001、99999）──
        # SoM 只会给当前可见交互元素分配编号，VLM 凭空编出来的数字直接报错，
        # 避免浪费一轮 Playwright query + 模糊 "Element not found" 错误。
        # target_id=0 是合法"无目标"值（press_key/wait/scroll 走这条路），豁免。
        if target_id != 0 and self._last_som_elements:
            _valid_ids = {
                int(el["id"]) for el in self._last_som_elements
                if el.get("id") is not None and str(el["id"]).isdigit()
            }
            if _valid_ids and target_id not in _valid_ids:
                _sample = sorted(_valid_ids)
                _hint = (
                    f"{_sample[:10]}...(共 {len(_sample)} 个)"
                    if len(_sample) > 10 else str(_sample)
                )
                _scope_note = (
                    "（本轮为视口局部 SoM，目标可能在视口外——"
                    "先用 smooth_scroll 把它移入视口再操作）"
                    if getattr(self, "_last_som_scope", "full") == "viewport"
                    else ""
                )
                self._last_action_error = ValueError(
                    f"target_id={target_id} 不在本轮 SoM 标记中，疑似幻觉 ID。"
                    f"有效 ID：{_hint}。{_scope_note}"
                    "请从截图红框或 @eN 快照中选一个真实存在的编号，"
                    "若页面无合适目标改用 smooth_scroll / press_key / wait。"
                )
                logger.warning(
                    f"[TARGET HALLUCINATION] target_id={target_id} 不在 "
                    f"SoM 有效集合（{len(_valid_ids)} 个）"
                )
                return None

        selector = f'[data-som-id="{target_id}"]'
        resolver = """({ targetId, actionKind }) => {
            const base = document.querySelector(`[data-som-id="${targetId}"]`);
            if (!base) return null;

            const clickSelector = 'button, a[href], input[type="button"], input[type="submit"], [role="button"], [role="link"], summary, label, [onclick], [tabindex]';

            const isVisible = (el) => {{
                if (!el || !el.getBoundingClientRect) return false;
                const rect = el.getBoundingClientRect();
                if (rect.width < 1 || rect.height < 1) return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' && parseFloat(style.opacity || '1') > 0.05;
            }};

            const isInputLike = (el) => {{
                if (!el) return false;
                return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable;
            }};

            const isClickable = (el) => {{
                if (!el) return false;
                const tag = el.tagName;
                const role = (el.getAttribute('role') || '').toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                const style = window.getComputedStyle(el);
                return (
                    ['BUTTON', 'A', 'SUMMARY', 'LABEL'].includes(tag) ||
                    (tag === 'INPUT' && ['button', 'submit', 'checkbox', 'radio'].includes(type)) ||
                    ['button', 'link', 'tab', 'menuitem', 'checkbox', 'radio', 'switch'].includes(role) ||
                    typeof el.onclick === 'function' ||
                    el.hasAttribute('onclick') ||
                    style.cursor === 'pointer' ||
                    el.tabIndex >= 0
                );
            }};

            const uniq = (items) => {{
                const result = [];
                const seen = new Set();
                for (const el of items) {{
                    if (!el || seen.has(el)) continue;
                    seen.add(el);
                    result.push(el);
                }}
                return result;
            }};

            if (actionKind === 'type') {{
                const label = base.closest('label');
                const labeledInput = label && label.getAttribute('for')
                    ? document.getElementById(label.getAttribute('for'))
                    : null;
                const nearestVisibleInput = () => {{
                    const baseRect = base.getBoundingClientRect();
                    const baseX = baseRect.left + (baseRect.width / 2);
                    const baseY = baseRect.top + (baseRect.height / 2);
                    let best = null;
                    let bestScore = Number.POSITIVE_INFINITY;

                    for (const el of document.querySelectorAll('input:not([type="hidden"]), textarea, [contenteditable], [contenteditable="true"], [contenteditable="plaintext-only"]')) {{
                        if (!isInputLike(el) || !isVisible(el)) continue;
                        const rect = el.getBoundingClientRect();
                        const centerX = rect.left + (rect.width / 2);
                        const centerY = rect.top + (rect.height / 2);
                        const score = Math.hypot(centerX - baseX, centerY - baseY);
                        if (score < bestScore) {{
                            best = el;
                            bestScore = score;
                        }}
                    }}
                    return best;
                }};

                const candidates = uniq([
                    base,
                    base.querySelector('input:not([type="hidden"]), textarea, [contenteditable], [contenteditable="true"], [contenteditable="plaintext-only"]'),
                    labeledInput,
                    base.parentElement
                        ? base.parentElement.querySelector('input:not([type="hidden"]), textarea, [contenteditable], [contenteditable="true"], [contenteditable="plaintext-only"]')
                        : null,
                    base.closest('input, textarea, [contenteditable], [contenteditable="true"], [contenteditable="plaintext-only"]'),
                ]);
                return candidates.find((el) => isInputLike(el) && isVisible(el)) || nearestVisibleInput() || base;
            }}

            const candidates = uniq([
                base.closest(clickSelector),
                base,
                base.querySelector(clickSelector),
                base.parentElement ? base.parentElement.querySelector(clickSelector) : null,
                base.parentElement ? base.parentElement.closest(clickSelector) : null,
            ]);

            return candidates.find((el) => isClickable(el) && isVisible(el)) || base;
        }"""

        for frame in page.frames:
            try:
                base_handle = await frame.query_selector(selector)
                if not base_handle:
                    continue

                handle = await frame.evaluate_handle(
                    resolver,
                    {"targetId": target_id, "actionKind": action_kind},
                )
                resolved_handle = handle.as_element() if handle else None
                if resolved_handle:
                    return ActionTarget(
                        frame=frame,
                        handle=resolved_handle,
                        selector=selector,
                    )

                return ActionTarget(frame=frame, handle=base_handle, selector=selector)
            except Exception:
                continue

        # ── 语义 Locator 自愈回退（Wave 3）──
        # 到这里说明所有 frame 都找不到 [data-som-id=N]。最常见成因：
        # SPA 前端在 SoM 注入后重渲 → 新节点没有 data-som-id 属性。
        # 从 element_mapping 取出上轮快照记录的 role/name，用 Playwright 原生
        # 语义 locator 在主 frame 里重新定位。**命中 1 个才用**，>1 放弃（语义
        # 歧义优先保真），0 个也放弃，让上层自愈链照常抛 ActionExecutionError。
        fallback_target = await self._semantic_fallback_resolve(
            page=page, target_id=target_id, action_kind=action_kind
        )
        if fallback_target is not None:
            return fallback_target

        return None

    async def _semantic_fallback_resolve(
        self, page: Page, target_id: int, action_kind: str
    ) -> "ActionTarget | None":
        """
        基于 element_mapping 里缓存的 role/name，用 get_by_role 重新定位元素。

        严格契约：仅当精确匹配命中 **恰好 1 个** 时才返回；0 或 >1 均返回 None。
        原因：SoM 原始定位依赖视觉歧义消除（红框），降级到 `.first` 会盲点错
        另一个同名按钮（如"提交"在对话框/登录框里重复出现）。保守优于乐观。

        Playwright `get_by_role(name=..., exact=True)` 做完整字符串匹配。
        """
        ref = f"@e{target_id}"
        meta = self.element_mapping.get(ref)
        if not meta:
            return None
        role = (meta.get("role") or "").strip().lower()
        name = (meta.get("name") or "").strip()
        # 角色或名字为空 → 无足够语义信号，放弃
        if not role or not name:
            return None
        # get_by_role 接受的角色白名单（与 extract_accessibility_tree 一致的交互角色）
        _ALLOWED_ROLES = {
            "button", "link", "textbox", "searchbox", "combobox",
            "checkbox", "radio", "switch", "slider",
            "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
            "option", "treeitem",
        }
        if role not in _ALLOWED_ROLES:
            return None
        try:
            locator = page.get_by_role(role, name=name, exact=True)
            count = await locator.count()
        except Exception as e:
            logger.debug(f"[SEMANTIC FALLBACK] locator query failed: {e}")
            return None
        if count != 1:
            logger.info(
                f"[SEMANTIC FALLBACK] {ref} role={role!r} name={name!r} "
                f"命中 {count} 个（需恰好 1），放弃回退"
            )
            return None
        try:
            handle = await locator.element_handle(timeout=2000)
        except Exception as e:
            logger.debug(f"[SEMANTIC FALLBACK] element_handle failed: {e}")
            return None
        if handle is None:
            return None
        self._semantic_fallback_count += 1
        synthetic_selector = f'role={role}[name="{name}"]'
        logger.warning(
            f"[SEMANTIC FALLBACK] {ref} 原选择器 [data-som-id={target_id}] 失效，"
            f"已通过 {synthetic_selector} 自愈定位（action={action_kind}，"
            f"累计 fallback {self._semantic_fallback_count} 次）"
        )
        return ActionTarget(
            frame=page.main_frame,
            handle=handle,
            selector=synthetic_selector,
        )

    async def start(self, url: str, *, user_data_dir_override: str | None = None) -> None:
        """
        启动持久化浏览器上下文并导航到指定 URL。

        使用 launch_persistent_context 保证 Cookie/登录态跨次运行持久化。
        Playwright 会自动创建 user_data_dir（如不存在）。

        Args:
            url: 初始页面 URL
            user_data_dir_override: 跨系统会话隔离用——为目标 system 指定独立
                Chromium profile 目录，避开 launch_persistent_context 的单实例
                锁；None 时沿用全局 config.BROWSER_USER_DATA_DIR（默认行为）。
        """
        # 加载 SoM 注入脚本
        self._closed = False
        self._som_js = config.SOM_SCRIPT_PATH.read_text(encoding="utf-8")
        logger.info(f"SoM script loaded ({len(self._som_js)} chars)")

        # 创建截图和下载目录
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._download_dir.mkdir(parents=True, exist_ok=True)

        # 解析 user_data_dir 路径（cross-system 切换可经 override 给每个 system
        # 独立 profile，避开 launch_persistent_context 的单实例锁）
        user_data_path = resolve_user_data_dir(
            user_data_dir_override,
            default_base=config.BROWSER_USER_DATA_DIR,
            packaged_fallback=Path(__file__).parent / "browser_data",
        )
        user_data_path.mkdir(parents=True, exist_ok=True)

        # 启动 Playwright + 持久化 Chromium 上下文
        self._playwright = await async_playwright().start()

        # 伪造真实 Windows Chrome UA，避免 Headless 特征泄露。
        # STEALTH-1: 用真实 Chromium 主版本拼 UA，并让 Sec-CH-UA Client Hints
        # 与之对齐，避免 UA 谎报版本/平台被 Cloudflare 当作风控信号。
        try:
            _chromium_exe = self._playwright.chromium.executable_path
        except Exception:
            _chromium_exe = None
        _stealth_profile = build_profile(_chromium_exe, platform="Windows")
        _STEALTH_UA = _stealth_profile.user_agent
        self._stealth_profile = _stealth_profile

        logger.info(f"Launching persistent context: {user_data_path.resolve()}")
        # proxy: single static (PROXY_SERVER) or rotating chain (PROXY_CHAIN),
        # resolved by the proxy_chain module so the logic stays out of this
        # oversized file (workflow section 3); self._proxy_chain is kept for
        # future re-route-on-block rotation.
        self._ensure_proxy_chain()
        _proxy_cfg = self._proxy_chain.current() if self._proxy_chain else None
        if _proxy_cfg:
            logger.info(
                "[BROWSER] proxy enabled: %s (chain=%d)",
                _proxy_cfg.get("server", ""),
                len(self._proxy_chain),
            )

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_path.resolve()),
            headless=config.HEADLESS,
            viewport={"width": config.VIEWPORT_WIDTH, "height": config.VIEWPORT_HEIGHT},
            user_agent=_STEALTH_UA,
            ignore_https_errors=True,
            args=self._BROWSER_ARGS,
            accept_downloads=True,  # 启用下载接管
            proxy=_proxy_cfg,
        )
        try:
            self._context.on("close", lambda *_: setattr(self, "_closed", True))
        except Exception:
            pass
        try:
            await self._context.clear_permissions()
            logger.info("Browser permissions cleared at context startup")
        except Exception as exc:
            logger.debug(f"clear_permissions skipped: {exc}")

        # ── 反检测 init script（Context 级，覆盖所有新页面）──────────────────
        # add_init_script 在每次页面导航前执行，早于页面任何 JS，
        # 可靠地抹除 navigator.webdriver / plugins / languages 等自动化指纹。
        _STEALTH_INIT_JS = """
// 1. 抹除最关键的 webdriver 标志
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true,
});

// 2. 注入 window.chrome，模拟真实 Chrome 运行时
if (!window.chrome) {
    window.chrome = { runtime: {} };
}

// 3. navigator.plugins 不在此手写伪造：playwright_stealth 的 navigator_plugins
//    evasion（默认开启，apply_stealth_async 注入，生成带 MimeType 的真实
//    PluginArray）已统一接管。旧整数数组 [1,2,3] 既假又与库双重打补丁，
//    按 backlog STEALTH-1/2 P2 移除（见 docs/vspider_architecture_backlog.md）。

// 4. 伪造语言列表
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en'],
    configurable: true,
});
"""
        await self._context.add_init_script(_STEALTH_INIT_JS)
        logger.info("Stealth init script injected at context level")

        # STEALTH-1: 对齐低熵 Client Hints，使 Sec-CH-UA 版本/平台与上面伪造的
        # UA 完全一致（Cloudflare 会比对二者，不一致即判风险）。
        try:
            await self._context.set_extra_http_headers(_stealth_profile.client_hints)
            logger.info(
                "Client Hints aligned to UA (Chrome %d / %s)",
                _stealth_profile.major,
                _stealth_profile.platform,
            )
        except Exception as _ch_err:
            logger.debug("set_extra_http_headers (client hints) skipped: %s", _ch_err)

        # ── 全局弹窗静默刺客：MutationObserver 自动隐藏常见牛皮癣浮层 ─────────
        # 策略：display:none 而非 remove()，避免触发页面业务 JS 的 DOM 依赖异常。
        # MutationObserver 持续监听，确保动态插入的懒加载弹窗也能被拦截。
        _POPUP_KILLER_JS = """
(function () {
    'use strict';

    // 仅允许非常明确的弹窗/浮层命名，避免误伤主干页面
    const adSelectors = [
        '#cookie-banner',
        '#cookie-notice',
        '.accept-cookies-button',
        '.gdpr-banner',
        'div[id^="ad-"]',
        'div[class*="app-download-float"]',
        'div[class*="tb-float-"]',
        'div[class*="login-modal-mask"]:not(body):not(html)',
        '.bottom-bar-download'
    ].join(', ');

    const MAX_ANCESTOR_DEPTH = 10;

    function getAncestorDepth(el) {
        try {
            let depth = 0;
            let node = el;
            while (node && node.parentElement) {
                depth += 1;
                node = node.parentElement;
                if (depth > MAX_ANCESTOR_DEPTH) break;
            }
            return depth;
        } catch (_) { return false; }
    }

    function canHideElement(el) {
        try {
            if (!el || !el.getBoundingClientRect) return false;
            const tag = (el.tagName || '').toLowerCase();
            const id = (el.id || '').toLowerCase();
            if (tag === 'html' || tag === 'body') return false;
            if (id === 'wrapper' || id === 'head-wrapper' || id === 's_wrap') return false;
            const rect = el.getBoundingClientRect();
            const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
            const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
            if (!viewportWidth || !viewportHeight) return false;
            if (getAncestorDepth(el) > MAX_ANCESTOR_DEPTH) return false;
            if (rect.width >= viewportWidth * 0.8) return false;
            if (rect.height >= viewportHeight * 0.8) return false;
            return true;
            }
        catch (_) { return false; }
    }

    function silenceEl(el) {
        try {
            if (!canHideElement(el)) {
                console.log('拦截器放过了一个巨大的疑似误伤元素:', el);
                return;
            }
            if (el.style) {
                el.style.setProperty('display', 'none', 'important');
            }
        } catch (_) {}
    }

    function scanAndKill(root) {
        try {
            const scope = root && root.querySelectorAll ? root : document;
            scope.querySelectorAll(adSelectors).forEach(silenceEl);
        } catch (_) {}
    }

    // 页面初次扫描
    if (document.readyState !== 'loading') {
        scanAndKill();
    } else {
        document.addEventListener('DOMContentLoaded', scanAndKill, { once: true });
    }

    // MutationObserver：监听后续动态插入的弹窗
    const observer = new MutationObserver(function (mutations) {
        for (const m of mutations) {
            for (const node of m.addedNodes) {
                if (node.nodeType !== 1) continue; // 只处理 Element
                if (node.matches && node.matches(adSelectors)) {
                    silenceEl(node);
                }
                scanAndKill(node);
            }
        }
    });

    observer.observe(document.documentElement, { childList: true, subtree: true });
})();
"""
        ENABLE_POPUP_KILLER = False  # 临时关闭，排查百度首页白板问题
        if ENABLE_POPUP_KILLER:
            await self._context.add_init_script(_POPUP_KILLER_JS)
            logger.info("Popup killer init script injected at context level")
        else:
            logger.info("Popup killer init script disabled for diagnostics")

        _PERMISSION_GUARD_JS = """
(() => {
    const denyPermissionNames = new Set(['microphone', 'camera', 'geolocation', 'notifications']);

    try {
        if (navigator.permissions && typeof navigator.permissions.query === 'function') {
            const originalQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = (params = {}) => {
                const name = String(params.name || '').toLowerCase();
                if (denyPermissionNames.has(name)) {
                    return Promise.resolve({
                        state: 'denied',
                        onchange: null,
                        addEventListener() {},
                        removeEventListener() {},
                        dispatchEvent() { return false; },
                    });
                }
                return originalQuery(params);
            };
        }
    } catch (_) {}

    try {
        if (navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === 'function') {
            navigator.mediaDevices.getUserMedia = async () => {
                throw new DOMException('Permission denied by VSpider', 'NotAllowedError');
            };
        }
    } catch (_) {}

    try {
        if (navigator.geolocation) {
            navigator.geolocation.getCurrentPosition = (_success, error) => {
                if (typeof error === 'function') {
                    error({ code: 1, message: 'Permission denied by VSpider' });
                }
            };
            navigator.geolocation.watchPosition = (_success, error) => {
                if (typeof error === 'function') {
                    error({ code: 1, message: 'Permission denied by VSpider' });
                }
                return 0;
            };
        }
    } catch (_) {}

    try {
        if (window.Notification && typeof window.Notification.requestPermission === 'function') {
            window.Notification.requestPermission = () => Promise.resolve('denied');
            try {
                Object.defineProperty(window.Notification, 'permission', {
                    configurable: true,
                    get: () => 'denied',
                });
            } catch (_) {}
        }
    } catch (_) {}

    try {
        if ('SpeechRecognition' in window) {
            window.SpeechRecognition = undefined;
        }
        if ('webkitSpeechRecognition' in window) {
            window.webkitSpeechRecognition = undefined;
        }
    } catch (_) {}
})();
"""
        await self._context.add_init_script(_PERMISSION_GUARD_JS)
        logger.info("Permission guard init script injected at context level")

        # playwright_stealth 深度补丁（WebGL / Canvas 指纹等高级特征）
        try:
            await Stealth().apply_stealth_async(self._context)
            logger.info("playwright_stealth applied at context level")
        except Exception as _stealth_err:
            logger.debug(f"playwright_stealth skipped: {_stealth_err}")

        # ── Auth Matrix：按 profile 注入 Cookie + origin-scoped LocalStorage ──
        # launch_persistent_context 不支持 storage_state 参数，
        # 通过 add_cookies + origin-scoped add_init_script 两步等效实现。
        auth_profiles = getattr(config, "AUTH_PROFILES", "")
        if auth_profiles:
            try:
                auth_result = load_auth_profiles(
                    auth_profiles,
                    start_url=url,
                    auth_dir=getattr(config, "AUTH_DIR", ""),
                )
                if auth_result.enabled:
                    cookie_count, origin_count = await apply_storage_state_to_context(
                        self._context,
                        auth_result.state,
                    )
                    self._auth_matrix_note = auth_result.prompt_note()
                    self.auth_status_note = self._auth_matrix_note
                    self.auth_matrix_loaded = True
                    logger.info(
                        "[AUTH MATRIX] Loaded profiles=%s cookies=%s origins=%s files=%s",
                        ",".join(auth_result.profiles),
                        cookie_count,
                        origin_count,
                        ",".join(path.name for path in auth_result.files),
                    )
                    for warning in auth_result.warnings:
                        logger.warning("[AUTH MATRIX] %s", warning)
                    try:
                        from .auth_harvester import cf_clearance_profile_hint
                    except ImportError:
                        from auth_harvester import cf_clearance_profile_hint
                    from urllib.parse import urlparse as _urlparse_cf
                    _cf_host = _urlparse_cf(url or "").netloc.split("@")[-1].split(":")[0]
                    _cf_hint = cf_clearance_profile_hint(host=_cf_host)
                    if _cf_hint:
                        self.auth_status_note = f"{self.auth_status_note}\n{_cf_hint}"
                        logger.info("[AUTH MATRIX] %s", _cf_hint)
                else:
                    self._auth_matrix_note = (
                        f"已启用 auth profile 选择（{auth_profiles}），但没有匹配到可加载的状态。"
                    )
                    self.auth_status_note = self._auth_matrix_note
                    self.auth_matrix_loaded = False
                    logger.info("[AUTH MATRIX] No auth profiles matched for %s", url)
            except Exception as _auth_err:
                self._auth_matrix_note = f"Auth Matrix 加载失败：{type(_auth_err).__name__}: {_auth_err}"
                self.auth_status_note = self._auth_matrix_note
                self.auth_matrix_loaded = False
                logger.warning("[AUTH MATRIX] Failed to load auth profiles: %s", _auth_err)
        else:
            self._auth_matrix_note = "未启用 Auth Matrix（未设置 --auth-profiles / VSPIDER_AUTH_PROFILES）。"
            self.auth_status_note = self._auth_matrix_note
            self.auth_matrix_loaded = False
            logger.debug("[AUTH MATRIX] Disabled; no profile injection requested")

            # 兼容旧路径：仅当未启用 Auth Matrix 时才尝试单文件 auth_state.json。
            _auth_candidates = [
                Path(__file__).parent.parent / "auth_state.json",
                Path(__file__).parent / "auth_state.json",
            ]
            _auth_path = next((p for p in _auth_candidates if p.exists()), None)
            if _auth_path:
                try:
                    _auth_state = json.loads(_auth_path.read_text(encoding="utf-8"))
                    cookie_count, origin_count = await apply_storage_state_to_context(
                        self._context,
                        _auth_state,
                    )
                    self._auth_matrix_note = (
                        f"已加载旧版 auth_state.json（cookies={cookie_count}, origins={origin_count}）。"
                    )
                    self.auth_status_note = self._auth_matrix_note
                    self.auth_matrix_loaded = False
                    logger.warning(
                        "[AUTH LEGACY] Loaded deprecated auth_state.json (%s cookies, %s origins). "
                        "Prefer .auth profiles with --auth-profiles.",
                        cookie_count,
                        origin_count,
                    )
                except Exception as _auth_err:
                    logger.warning(f"[AUTH LEGACY] auth_state.json 加载失败（非致命，跳过）: {_auth_err}")

        # launch_persistent_context 默认自带一个 page
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        # 注入隐身衣，抹除自动化指纹
        self._context.on(
            "page",
            lambda page: self._track_background_task(
                self._register_page(page, reason="context page event", activate=True)
            ),
        )
        await self._register_page(self._page, reason="initial context page", activate=True)

        logger.info(
            f"Browser started | Headless={config.HEADLESS} | "
            f"Viewport={config.VIEWPORT_WIDTH}x{config.VIEWPORT_HEIGHT} | "
            f"UserDataDir={user_data_path.resolve()}"
        )

        # 导航到初始页面 — nav_resilience 逐级降级
        logger.info(f"Navigating to: {url}")
        try:
            from .nav_resilience import navigate_with_retry, check_page_health
        except ImportError:
            from nav_resilience import navigate_with_retry, check_page_health
        nav_result = await navigate_with_retry(
            self._page, url, base_timeout_ms=30_000,
            strategies=["domcontentloaded", "load", "commit"],
        )
        response = None
        if nav_result.success:
            self.last_navigation_url = nav_result.final_url or str(url)
            if nav_result.degraded:
                logger.info(
                    "[NAV RESILIENCE] strategy=%s attempts=%s",
                    nav_result.final_strategy, nav_result.attempt_count,
                )
            health = await check_page_health(self._page)
            if not health["healthy"]:
                logger.warning("[NAV HEALTH] issues: %s", health["issues"])
        else:
            nav_err_text = nav_result.attempts[-1].error if nav_result.attempts else "navigation failed"
            parsed = urlsplit(str(url or ""))
            host = (parsed.netloc or "").lower()
            error_text = nav_err_text.lower()
            if "err_http_response_code_failure" in error_text:
                status_match = re.search(r"/status/(\d{3})(?:[/?#]|$)", parsed.path)
                self.last_navigation_status = int(status_match.group(1)) if status_match else 400
                self.last_navigation_url = str(url or "")
                response = None
                logger.warning(
                    "[HTTP STATUS] navigation raised HTTP response code failure; status=%s url=%s",
                    self.last_navigation_status,
                    self.last_navigation_url,
                )
            else:
                can_retry_without_www = (
                    host.startswith("www.")
                    and "err_name_not_resolved" in error_text
                )
                if not can_retry_without_www:
                    raise Exception(nav_err_text)
                retry_url = urlunsplit((
                    parsed.scheme or "https",
                    parsed.netloc[4:],
                    parsed.path,
                    parsed.query,
                    parsed.fragment,
                ))
                logger.warning(
                    "[NAV RETRY] %s failed DNS; retrying without www: %s",
                    url,
                    retry_url,
                )
                fallback = await navigate_with_retry(
                    self._page, retry_url, base_timeout_ms=30_000,
                    strategies=["domcontentloaded", "commit"],
                )
                if fallback.success:
                    nav_result = fallback
                    self.last_navigation_url = fallback.final_url or retry_url
                else:
                    raise Exception(nav_err_text)
        if nav_result.success and response is None:
            try:
                self.last_navigation_status = 200
            except Exception:
                self.last_navigation_status = 200
        if response is not None:
            self.last_navigation_status = response.status
            self.last_navigation_url = response.url
        if self.last_navigation_status and self.last_navigation_status >= 400:
            logger.warning(
                "[HTTP STATUS] initial navigation returned %s for %s",
                self.last_navigation_status,
                self.last_navigation_url,
            )

        # 首次导航后等待 SPA 完全渲染
        await self._wait_for_page_stable()
        await self._dismiss_permission_surfaces(reason="after initial navigation")
        await self.refresh_auth_sentinel()
        logger.info("Page loaded and stable")

        # 挂载 XHR/Fetch 响应拦截器
        logger.info("Page-level interceptors will auto-register for new tabs and popups")

        # 挂载全局底层下载拦截器（绕过弹窗）

    async def restart(self, url: str, reason: str = "") -> None:
        """Restart the persistent browser context in-place and navigate again."""
        logger.warning("[BROWSER RECOVERY] Restarting browser context: %s", reason or "no reason")
        try:
            await self.close()
        except Exception as exc:
            logger.debug("[BROWSER RECOVERY] close before restart skipped: %s", exc)
        self._playwright = None
        self._context = None
        self._page = None
        self._registered_pages.clear()
        self._background_tasks.clear()
        self._tab_visit_stack.clear()
        self._last_action_error = None
        self._last_action_result = None
        self._closed = False
        await self.start(url)

    def _ensure_proxy_chain(self):
        """Build the proxy chain once and cache it (PROXY-4 build-once).

        ``start()`` is re-entered by ``restart()``; rebuilding the chain there
        would reset rotation state (``_idx`` / quarantine) back to the first
        proxy, so a reroute-then-restart could never actually switch IPs. Build
        only when absent so the advanced chain survives a restart. The chain
        logic itself stays in the ``proxy_chain`` module (workflow §三).
        """
        if getattr(self, "_proxy_chain", None) is None:
            try:
                from visual_web_agent.proxy_chain import build_chain_from_config
                self._proxy_chain = build_chain_from_config(config)
            except Exception:
                self._proxy_chain = None
        return self._proxy_chain

    async def reroute_proxy_on_block(
        self,
        url: str,
        *,
        result=None,
        state=None,
        reason: str = "bot challenge",
    ) -> bool:
        """Rotate to the next proxy and relaunch when a block warrants it (PROXY-4).

        Consults the pure ``should_rotate_on_challenge`` policy (PROXY-3) over the
        bot-challenge ``result`` / ``state``. When it says rotate AND a multi-proxy
        chain is configured, the current proxy is marked failed (advancing to the
        next healthy one) and the context is ``restart``ed so the relaunch picks up
        the new proxy via the build-once chain. Returns True iff a reroute happened.

        No-op (returns False) without a usable >1 proxy chain, so single-proxy /
        no-proxy runs are unaffected. The decision lives in ``proxy_chain``; this
        thin method is the only browser-substrate touch (workflow §三).
        """
        try:
            from visual_web_agent.proxy_chain import should_rotate_on_challenge
        except Exception:
            return False
        chain = getattr(self, "_proxy_chain", None)
        if chain is None or len(chain) < 2:
            return False
        if not should_rotate_on_challenge(result, state):
            return False
        # PROXY-4b: bound reroutes per run so a permanently-flagged target can't
        # spin the whole chain endlessly (state carries reroute_count + cap).
        max_reroute = getattr(state, "max_reroute_per_run", None)
        if max_reroute is not None and int(getattr(state, "reroute_count", 0) or 0) >= int(max_reroute):
            return False
        before = (chain.current() or {}).get("server", "")
        chain.mark_failed()
        if state is not None and hasattr(state, "reroute_count"):
            try:
                state.reroute_count += 1
            except Exception:
                pass
        after = (chain.current() or {}).get("server", "")
        logger.warning("[PROXY] reroute on block (%s): %s -> %s", reason, before, after)
        await self.restart(url, reason=f"proxy reroute: {reason}")
        return True

    async def refresh_auth_sentinel(self) -> str:
        """
        Run generic auth-surface detection and build a prompt note.

        This is intentionally not a site-specific "logged in" detector. It only
        exposes cheap environment signals; the VLM still decides from screenshot
        and AX Tree whether the task can proceed.
        """
        page = await self._ensure_active_page(reason="auth sentinel")
        if not page:
            self.auth_status_note = f"{self._auth_matrix_note}\n通用检测：无可用页面。"
            return self.auth_status_note

        try:
            signals = await page.evaluate(
                """
() => {
  const visible = (el) => {
    try {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style && style.visibility !== 'hidden' && style.display !== 'none' &&
             rect.width > 2 && rect.height > 2;
    } catch (_) { return false; }
  };
  const textOf = (el) => ((el.innerText || el.value || el.getAttribute('aria-label') ||
                          el.getAttribute('placeholder') || '') + '').trim();
  const passwordInputs = Array.from(document.querySelectorAll('input[type="password"]')).filter(visible);
  const allControls = Array.from(document.querySelectorAll('a,button,[role="button"],input,textarea'));
  const loginRe = /(登录|登陆|登录\\/注册|sign\\s*in|log\\s*in|login)/i;
  const loginEntries = allControls.filter((el) => visible(el) && loginRe.test(textOf(el)));
  const bodyText = (document.body && document.body.innerText || '').slice(0, 5000);
  const captchaRe = /(验证码|滑块|拼图|扫码|二维码|短信|动态口令|安全验证|风控|captcha|verify|verification)/i;
  return {
    url: location.href,
    title: document.title || '',
    passwordCount: passwordInputs.length,
    loginEntryCount: loginEntries.length,
    captchaLike: captchaRe.test(bodyText),
  };
}
"""
            )
        except Exception as exc:
            self.auth_status_note = (
                f"{self._auth_matrix_note}\n通用检测：Auth Sentinel 执行失败："
                f"{type(exc).__name__}: {exc}"
            )
            logger.debug("[AUTH SENTINEL] failed: %s", exc)
            return self.auth_status_note

        current_url = str(signals.get("url") or page.url or "")
        login_like_url = bool(
            re.search(
                r"login|signin|sign-in|auth|passport|sso|cas|oauth|authserver|iam",
                current_url,
                flags=re.IGNORECASE,
            )
        )
        password_count = int(signals.get("passwordCount") or 0)
        login_entry_count = int(signals.get("loginEntryCount") or 0)
        captcha_like = bool(signals.get("captchaLike"))
        strong_login_wall = bool(
            password_count > 0
            or captcha_like
            or (login_like_url and login_entry_count > 0)
        )
        self.auth_stale_detected = bool(self.auth_matrix_loaded and strong_login_wall)
        self.auth_stale_reason = (
            "Auth profile 已加载，但首屏仍出现强登录墙/验证信号；"
            "可能是 Cookie 或 LocalStorage 过期。请重新运行 tools/manual_auth.py 刷新该 profile。"
            if self.auth_stale_detected else ""
        )

        self.auth_status_note = (
            f"{self._auth_matrix_note}\n"
            "通用检测："
            f"login_like_url={login_like_url}; "
            f"visible_password_inputs={password_count}; "
            f"visible_login_entries={login_entry_count}; "
            f"captcha_or_2fa_like_text={captcha_like}; "
            f"stale_auth_profile={self.auth_stale_detected}。\n"
            "请结合截图和 AX Tree 自主判断是否已登录；若已登录，直接执行业务目标；"
            "若出现验证码/扫码/短信/风控，请 ask_human，不要反复尝试登录。"
        )
        if self.auth_stale_detected:
            self.auth_status_note += f"\n{self.auth_stale_reason}"
        logger.info(
            "[AUTH SENTINEL] login_like_url=%s password_inputs=%s login_entries=%s captcha_like=%s stale=%s",
            login_like_url,
            password_count,
            login_entry_count,
            captcha_like,
            self.auth_stale_detected,
        )
        return self.auth_status_note

    async def detect_login_button(self) -> bool:
        """
        检查当前页面是否存在可见的'登录'文字按钮/链接。
        返回 True 表示未登录（找到登录按钮），False 表示已登录（没有登录按钮）。
        """
        page = await self._ensure_active_page(reason="detect login button")
        if not page:
            return False
        try:
            found = await page.evaluate("""() => {
                const tags = ['a', 'button', 'span', 'div', 'li'];
                for (const tag of tags) {
                    for (const el of document.querySelectorAll(tag)) {
                        const text = (el.innerText || el.textContent || '').trim();
                        if (text === '登录' || text === '登录/注册') {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            if (style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                parseFloat(style.opacity) > 0 &&
                                rect.width > 0 && rect.height > 0) {
                                return true;
                            }
                        }
                    }
                }
                return false;
            }""")
            return bool(found)
        except Exception:
            return False  # 检测失败时不干预，让 VLM 自己判断


    @staticmethod
    def _format_som_element(el: dict) -> str:
        return format_som_element(el)

    def _screenshot_looks_visually_blank(self, screenshot_bytes: bytes) -> tuple[bool, str]:
        return screenshot_looks_visually_blank(screenshot_bytes)

    async def _should_reload_visual_blank(
        self,
        page: Page,
        screenshot_bytes: bytes,
        total_elements: int,
    ) -> tuple[bool, str]:
        looks_blank, visual_reason = self._screenshot_looks_visually_blank(screenshot_bytes)
        if not looks_blank:
            return False, visual_reason

        try:
            stats = await page.evaluate(
                """() => ({
                    bodyTextLength: (document.body?.innerText || '').replace(/\\s+/g, ' ').trim().length,
                    readyState: document.readyState,
                    title: document.title || '',
                    url: location.href
                })"""
            )
        except Exception as e:
            return False, f"{visual_reason}; dom probe failed: {e}"

        body_len = int((stats or {}).get("bodyTextLength") or 0)
        ready = str((stats or {}).get("readyState") or "")
        if body_len < 500:
            return False, f"{visual_reason}; bodyText={body_len}, readyState={ready}"

        # If SoM marked many visible elements, it may simply be a white-themed page.
        if total_elements > 20:
            return False, f"{visual_reason}; bodyText={body_len}, som={total_elements}"

        return True, f"{visual_reason}; bodyText={body_len}, som={total_elements}, readyState={ready}"

    async def mark_and_screenshot(self, step: int = 0, *, scope="viewport") -> tuple[str, str]:
        """
        注入 SoM 标记脚本并截取全屏截图。

        Args:
            step: 当前回合编号（截图文件名 / 日志）。
            scope: SoM 选区（E2 局部 SoM）。``"viewport"``（默认，只标视口内元素）
                / ``"full"``（全页，含折叠下方）/ ``{"selector": "..."}``（只标容器子树）。
        Returns:
            (screenshot_b64, input_descriptions)
        """
        _scope_label = "selector" if isinstance(scope, dict) else str(scope or "viewport")
        page = await self._ensure_active_page(reason="before screenshot")
        if not page:
            raise RuntimeError("No active page available for screenshot.")

        logger.info(f"[Step {step}] Waiting for page to stabilize before screenshot...")
        await self._wait_for_page_stable()
        await self._dismiss_permission_surfaces(reason=f"before screenshot step={step}")
        await self._dismiss_login_popup(reason=f"before screenshot step={step}")

        # _wait_for_page_stable / _dismiss_permission_surfaces 期间可能发生 tab 关闭或 popup 切换
        # （典型场景：RPA close_tab 后紧跟 VLM 循环，navigation 仍在进行中）。
        # 此处必须刷新 page 引用，否则下面所有 await 都会打在一个已关闭的 Page 上。
        page = await self._ensure_active_page(reason="post-stabilize refresh")
        if not page:
            raise RuntimeError("Active page disappeared during stabilization.")

        current_id = 1
        total_elements = 0
        input_descriptions = []
        injected_frames = 0

        # 🛡️ 防白屏：等待 body 挂载完成，避免跳转瞬间 appendChild(null) 崩溃
        try:
            await page.wait_for_selector("body", state="attached", timeout=5000)
        except Exception as _body_err:
            if page.is_closed():
                logger.warning(
                    "[SoM Inject] 等待 body 期间当前 Page 已被关闭，尝试切换到最新活跃页..."
                )
                page = await self._ensure_active_page(reason="body-wait target closed")
                if not page:
                    raise RuntimeError("Page closed during body wait and no replacement is open.")
                try:
                    await page.wait_for_selector("body", state="attached", timeout=5000)
                except Exception as _body_err2:
                    logger.warning(
                        f"[SoM Inject] 备用 Page 的 body 等待再次失败，缓冲 2 秒继续: {_body_err2}"
                    )
                    await asyncio.sleep(2)
            else:
                logger.warning(
                    f"[SoM Inject] body 加载超时，页面可能正在跳转中，强制缓冲 2 秒: {_body_err}"
                )
                await asyncio.sleep(2)

        # 重新拿一次 frames —— 上面如果换了 page，原来的 frame 列表已失效
        frames_to_eval = list(page.frames)
        all_som_elements: list[dict] = []

        # H1: 计时 SoM 注入耗时，慢页 / 重页发出告警 + 阶段事件
        _som_t0 = time.time()
        for frame in frames_to_eval:
            try:
                result = await frame.evaluate(self._som_js, {"startIndex": current_id, "scope": scope})
                if result and isinstance(result, dict):
                    injected_frames += 1
                    current_id = result.get('nextId', current_id)
                    element_map = result.get('resultMap', [])
                    total_elements += len(element_map)
                    all_som_elements.extend(element_map)

                    for el in element_map:
                        input_descriptions.append(
                            self._format_som_element(el)
                        )
            except Exception as eval_err:
                logger.warning(f"Frame evaluation failed: {eval_err}")
        _som_dur_ms = int((time.time() - _som_t0) * 1000)

        # 缓存本轮 SoM 结果，供翻页引导等后续逻辑查找特定元素
        self._last_som_elements = all_som_elements
        # E2：记录本轮选区，供 target_id 幻觉校验补「视口外」提示
        self._last_som_scope = _scope_label

        # H1 性能记账：常态 INFO，慢页 / 重页升级 WARN，并通过 broadcast_phase
        # 把数据推给前端（让大页面的 perf 问题立刻可见）。
        # 阈值：>150 元素 或 >2000ms 视为 heavy；info 用于常态观测。
        _som_heavy = total_elements > 150 or _som_dur_ms > 2000
        if _som_heavy:
            logger.warning(
                f"[Step {step}] SoM HEAVY: {total_elements} elements across "
                f"{injected_frames} frames in {_som_dur_ms}ms "
                f"(threshold: >150 elements or >2s)"
            )
        else:
            logger.info(
                f"[Step {step}] SoM injected across {injected_frames} frames, "
                f"marked {total_elements} interactive elements in {_som_dur_ms}ms"
            )
        # Phase event (G2 channel) so the frontend timeline shows SoM cost.
        try:
            from api_server import broadcast_phase

            broadcast_phase(
                "som_inject",
                severity="warn" if _som_heavy else "info",
                message=f"{total_elements} elements / {injected_frames} frames",
                notice_severity=self._last_notice_severity,
                step=step,
                duration_ms=_som_dur_ms,
                extra={
                    "element_count": int(total_elements),
                    "frame_count": int(injected_frames),
                    "heavy": bool(_som_heavy),
                },
            )
        except Exception:
            pass

        # ── SoM 零元素重试：tab 切换 / visibilitychange 重渲染可能导致暂时性空白 ──
        if total_elements == 0 and not page.is_closed():
            _page_url = (page.url or "").strip()
            if _page_url and not _page_url.startswith("about:"):
                # 诊断：获取页面 DOM 状态，帮助排查是"真空白"还是"过渡态"
                try:
                    _diag = await page.evaluate("""() => ({
                        url: location.href,
                        title: document.title,
                        bodyChildren: document.body ? document.body.childElementCount : -1,
                        bodyTextLen: document.body ? (document.body.innerText || '').length : -1,
                        readyState: document.readyState,
                        visibility: document.visibilityState,
                    })""")
                    logger.warning(
                        f"[SoM RETRY] 0 元素但页面非 about:blank，诊断: {_diag}"
                    )
                except Exception:
                    logger.warning("[SoM RETRY] 0 元素，诊断信息获取失败")

                # 等待 3 秒让页面完成可能的 visibilitychange 重渲染
                logger.info("[SoM RETRY] 等待 3 秒后重试 SoM 注入...")
                await asyncio.sleep(3)
                await self._clear_som_overlays()

                # 重新注入 SoM
                current_id = 1
                total_elements = 0
                input_descriptions.clear()
                injected_frames = 0
                frames_to_eval = list(page.frames)

                for frame in frames_to_eval:
                    try:
                        result = await frame.evaluate(self._som_js, {"startIndex": current_id, "scope": scope})
                        if result and isinstance(result, dict):
                            injected_frames += 1
                            current_id = result.get("nextId", current_id)
                            element_map = result.get("resultMap", [])
                            total_elements += len(element_map)

                            for el in element_map:
                                input_descriptions.append(
                                    self._format_som_element(el)
                                )
                    except Exception as _retry_err:
                        logger.warning(f"[SoM RETRY] Frame evaluation failed: {_retry_err}")

                logger.info(
                    f"[SoM RETRY] 重试结果: {injected_frames} frames, "
                    f"{total_elements} interactive elements"
                )

        # 让红框 / 标签有 500ms 绘制时间。使用进程侧 sleep 而非 page.wait_for_timeout：
        # 后者在 Page 已关闭（TargetClosedError）时会把异常抛到主循环外面。
        await asyncio.sleep(0.5)

        # 截图前再确认一次：frame 评估过程中可能因 navigation 又换了 tab
        if page.is_closed():
            logger.warning("[Screenshot] 当前 Page 已被关闭，重新获取最新活跃页...")
            page = await self._ensure_active_page(reason="pre-screenshot recovery")
            if not page:
                raise RuntimeError("Page closed before screenshot and no replacement is open.")

        # 🛡️ 防字体死锁：超时缩短至 10s，失败后 window.stop() 强制截图
        # 使用 PNG 统一图片链路（本地调试图、VLM 输入、WebSocket 广播）。
        _SS_KWARGS = {"full_page": False, "timeout": 10000, "type": "png"}
        screenshot_bytes: bytes | None = None
        try:
            screenshot_bytes = await page.screenshot(**_SS_KWARGS)
        except Exception as _ss_err:
            logger.error(f"[Screenshot] 截图失败（可能卡在字体加载或动画）: {_ss_err}")
            # 如果就是因为 Page 被关了，重新 acquire 一次再试
            if page.is_closed():
                logger.info("[Screenshot] 目标 Page 已关闭，切到最新活跃页后重试...")
                page = await self._ensure_active_page(reason="screenshot target closed retry")
                if not page:
                    raise RuntimeError(
                        "[Screenshot] 目标 Page 关闭且无可用替代页，放弃本轮截图"
                    )
                try:
                    screenshot_bytes = await page.screenshot(**_SS_KWARGS)
                except Exception as _ss_err_retry:
                    raise RuntimeError(
                        f"[Screenshot] 替代 Page 截图仍失败: {_ss_err_retry}"
                    )
            else:
                # Tier 2: window.stop() then retry Playwright screenshot.
                logger.info("[Screenshot] 执行 window.stop() 强制停止挂起资源，重新截图...")
                _tier2_ok = False
                try:
                    await page.evaluate("window.stop()")
                    await asyncio.sleep(1)
                    screenshot_bytes = await page.screenshot(**_SS_KWARGS)
                    _tier2_ok = True
                except Exception as _ss_err2:
                    logger.warning(
                        "[Screenshot] window.stop() 重试仍失败 (%s)；启动 CDP 兜底",
                        _ss_err2,
                    )

                if not _tier2_ok:
                    # Tier 3: CDP Page.captureScreenshot — bypasses Playwright's
                    # implicit ``document.fonts.ready`` wait that hangs on
                    # heavy SPA / 自定义字体 landing pages (e.g. comate.baidu.com
                    # via SEM redirect). Used by browser-use / puppeteer-extra
                    # for the same reason. Captures the rendered frame as-is.
                    try:
                        cdp = await page.context.new_cdp_session(page)
                        result = await cdp.send(
                            "Page.captureScreenshot",
                            {
                                "format": "jpeg",
                                "quality": 70,
                                "fromSurface": True,
                                "captureBeyondViewport": False,
                            },
                        )
                        screenshot_bytes = base64.b64decode(result["data"])
                        logger.info(
                            "[Screenshot] CDP captureScreenshot 兜底成功 "
                            "(%d bytes, font-loading 死锁已绕开)",
                            len(screenshot_bytes),
                        )
                        try:
                            await cdp.detach()
                        except Exception:
                            pass
                    except Exception as _cdp_err:
                        raise RuntimeError(
                            "[Screenshot] 三层兜底全败 "
                            f"(playwright+stop+cdp 均失败): {_cdp_err}"
                        )

        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""
        if (
            screenshot_bytes
            and current_url
            and not current_url.startswith("about:")
            and current_url not in self._visual_blank_reloaded_urls
        ):
            try:
                should_reload, blank_reason = await self._should_reload_visual_blank(
                    page,
                    screenshot_bytes,
                    total_elements,
                )
            except Exception as visual_probe_err:
                should_reload = False
                blank_reason = f"visual blank probe error: {visual_probe_err}"
            if should_reload:
                logger.warning(
                    "[VISUAL BLANK RECOVERY] Screenshot looks blank while DOM has content; "
                    "reloading once. %s",
                    blank_reason,
                )
                self._visual_blank_reloaded_urls.add(current_url)
                if await self.reload_active_page(reason="visual blank screenshot"):
                    await self._clear_som_overlays()
                    return await self.mark_and_screenshot(step=step)
            else:
                logger.debug("[VISUAL BLANK RECOVERY] skipped: %s", blank_reason)

        debug_path = self._screenshot_dir / f"step_{step:02d}.png"
        debug_path.write_bytes(screenshot_bytes)
        logger.info(f"[Step {step}] Screenshot saved: {debug_path}")

        latest_path = self._screenshot_dir / "debug.png"
        latest_path.write_bytes(screenshot_bytes)

        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        screenshot_data_uri = f"data:image/png;base64,{screenshot_b64}"
        _broadcast_image_safe(screenshot_data_uri, step)

        desc_text = "\n".join(input_descriptions)
        if desc_text:
            desc_text = f"【页面输入框与提示词参考】\n{desc_text}\n"

        return screenshot_b64, desc_text

    async def assess_page_complexity(self) -> tuple[bool, str]:
        """
        视口感知嗅探器：注入轻量 JS 评估当前可视区域的页面复杂度，
        决定本步应使用"极速纯文本降级模式"还是"高精度视觉模式"。

        Returns:
            (is_simple_page, reason)
              is_simple_page=True  → 页面简单，走纯文本模式
              is_simple_page=False → 页面复杂，走视觉截图模式
        """
        page = await self._ensure_active_page(reason="before assess_page_complexity")
        if not page:
            return False, "无活跃页面，兜底走视觉模式"

        sniffer_js = """
        () => {
            // 1. 检查可视区域内是否存在大面积 Canvas / SVG（图表、画布类应用）
            const mediaElements = document.querySelectorAll('canvas, svg');
            let hasComplexMedia = false;
            for (let el of mediaElements) {
                const rect = el.getBoundingClientRect();
                if (rect.width * rect.height > 40000) {
                    hasComplexMedia = true;
                    break;
                }
            }

            // 2. 只统计当前屏幕可视区域内的交互元素数量（视口过滤）
            const interactives = document.querySelectorAll(
                'input, button, a, select, textarea, [role="button"]'
            );
            let visibleCount = 0;
            const wh = window.innerHeight || document.documentElement.clientHeight;
            const ww = window.innerWidth  || document.documentElement.clientWidth;

            for (let el of interactives) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0 &&
                    rect.top <= wh && rect.bottom >= 0 &&
                    rect.left <= ww && rect.right  >= 0) {
                    visibleCount++;
                }
            }

            return { hasComplexMedia: hasComplexMedia, elementCount: visibleCount };
        }
        """
        try:
            stats = await page.evaluate(sniffer_js)

            # 条件 1：存在大型图表/画布，纯文本模式无法感知，必须走视觉
            if stats["hasComplexMedia"]:
                return False, f"检测到大面积图表/画布渲染"

            # 条件 2：可视交互元素超过黄金阈值 60，SoM 标注密集易重叠，必须走视觉
            if stats["elementCount"] > 60:
                return False, f"可视区交互元素过多 ({stats['elementCount']} 个)"

            # 条件 3：结构简单，走极速纯文本降级模式
            return True, f"页面结构简单 (可视元素 {stats['elementCount']} 个)"

        except Exception as e:
            # 嗅探异常时保守兜底：走视觉模式，避免盲目降级
            logger.warning(f"[COMPLEXITY] 嗅探异常，兜底走视觉模式: {e}")
            return False, f"嗅探异常兜底: {e}"

    async def detect_blank_content_shell(self) -> tuple[bool, str]:
        """
        检测"导航还在，但正文区几乎空白"的壳页状态。
        这类页面常见于内容区加载失败、错误跳转到空壳首页、或站点短暂渲染异常。

        冷静期守门员（防止误伤 Bing/Google 等结果页正在渲染的场景）：
          1. 必须 document.readyState === 'complete'
          2. 优先等 networkidle（最多 5s）
          3. 首次采样"空"后，等 4s 再重采一次；仍然空才判定壳页
          4. 两次采样都要跨过 readyState=complete 才计数
        """
        page = await self._ensure_active_page(reason="before detect_blank_content_shell")
        if not page:
            return False, "no active page"

        # ── 冷静期守门：给页面充分渲染时间，避免抢跑判空 ─────────────────────
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass  # 长连接页面永远不会 idle，允许继续

        js = """
        () => {
            function isVisible(el) {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return false;
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') <= 0.01) {
                    return false;
                }
                return true;
            }

            const bodyText = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
            const blocks = document.querySelectorAll(
                'main, article, section, #content, .content, .main, .container, .wrapper, h1, h2, h3, p, li, td, div'
            );

            let meaningfulBlocks = 0;
            let maxContentTextLength = 0;
            for (const el of blocks) {
                if (!isVisible(el)) continue;
                if (el.closest('header, nav, footer, [role="navigation"]')) continue;
                const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                if (text.length >= 8) meaningfulBlocks++;
                if (text.length > maxContentTextLength) maxContentTextLength = text.length;
            }

            return {
                bodyTextLength: bodyText.length,
                meaningfulBlocks,
                maxContentTextLength,
                readyState: document.readyState,
                title: document.title || ''
            };
        }
        """

        def _eval_stats(raw: dict) -> tuple[int, int, int, str]:
            return (
                int(raw.get("bodyTextLength", 0) or 0),
                int(raw.get("meaningfulBlocks", 0) or 0),
                int(raw.get("maxContentTextLength", 0) or 0),
                str(raw.get("readyState", "") or ""),
            )

        try:
            stats = await page.evaluate(js)
        except Exception as e:
            return False, f"detection failed: {e}"

        body_len, block_count, max_text, ready_state = _eval_stats(stats)

        # 页面尚未 complete → 一律视为非空壳（让 loading 继续，不要误杀）
        if ready_state != "complete":
            logger.debug(
                f"[assess] readyState={ready_state!r}，尚未 complete，跳过空壳判定"
            )
            return False, f"readyState={ready_state}, bodyText={body_len}, blocks={block_count}"

        # 第一次采样判为"疑似空" → 延长冷静期 4 秒再采一次
        # 必须两次采样都空才算真空壳（屏蔽渲染中间态）
        is_blank_first = body_len < 180 and block_count < 8 and max_text < 120
        if is_blank_first:
            logger.debug(
                f"[assess] 首采判疑似空壳 (bodyText={body_len}, blocks={block_count}, "
                f"maxBlock={max_text})，延长 4 秒冷静期后二次采样..."
            )
            await asyncio.sleep(4)
            try:
                stats2 = await page.evaluate(js)
                body_len, block_count, max_text, ready_state = _eval_stats(stats2)
                logger.debug(
                    f"[assess] 二采结果: bodyText={body_len}, blocks={block_count}, "
                    f"maxBlock={max_text}, readyState={ready_state}"
                )
            except Exception:
                pass

            if ready_state != "complete":
                return False, (
                    f"readyState regressed to {ready_state} after wait; "
                    f"bodyText={body_len}, blocks={block_count}"
                )

        is_blank = body_len < 180 and block_count < 8 and max_text < 120
        reason = f"bodyText={body_len}, blocks={block_count}, maxBlockText={max_text}"
        return is_blank, reason

    async def extract_text_dom(self) -> str:
        """
        旧版 DOM 只读序列化（保留给遗留/调试路径，非当前 AX Tree 主路径）。

        前置条件：调用前 `mark_and_screenshot` 必须已执行 —— 它通过 som_inject_v5.js
        为页面上所有可见可交互元素写入 `data-som-id`（红框数字 ↔ 属性值 ↔ DOM 行 ID
        三方对齐的单一事实源）。本方法只读取、不新增、不擦除这些 ID。

        Returns:
            多行字符串。每一行是 `[ID: N] <tag ...>text</tag>`，N 与截图红框数字完全一致。
            末尾追加少量 `[TEXT] ...` 行（非交互语义文本，如 h1/h2/p），帮助 VLM 理解页面语境。
            若当前页面尚未注入 SoM ID，返回空串。
        """
        page = await self._ensure_active_page(reason="before legacy extract_text_dom")
        if not page:
            raise RuntimeError("No active page available for legacy DOM extraction.")

        await self._wait_for_page_stable()
        await self._dismiss_permission_surfaces(reason="before legacy text DOM extraction")

        # ── 只读序列化：复用 som_inject_v5.js 已写入的 data-som-id 作为单一事实源 ──
        # 关键设计：此处不再另起 SELECTOR 重新扫描页面，也绝不擦除 data-som-id。
        # 原因：screenshot 上的红框编号由 SoM v5 决定；若这里再扫一遍并重编号，
        # 会导致截图红框 "12" 与 DOM 输出 "[ID: 12]" 指向不同元素 → VLM 误点。
        # 现在流程：mark_and_screenshot 已完成编号 → 旧版 extract_text_dom 只读取 + 序列化。
        _DOM_EXTRACT_JS = """
(() => {
    const results = [];
    const interactiveTexts = new Set();
    const seenText = new Set();

    // 按数字 ID 升序收集，保证与截图红框编号序列一致
    const marked = Array.from(document.querySelectorAll('[data-som-id]'));
    marked.sort((a, b) => {
        const ai = parseInt(a.getAttribute('data-som-id'), 10);
        const bi = parseInt(b.getAttribute('data-som-id'), 10);
        return (isNaN(ai) ? 0 : ai) - (isNaN(bi) ? 0 : bi);
    });

    for (const el of marked) {
        const id = el.getAttribute('data-som-id');
        const tag = (el.tagName || 'UNKNOWN').toLowerCase();
        const attrs = [];

        const inputType = (tag === 'input') ? el.getAttribute('type') : null;
        if (inputType) attrs.push(`type="${inputType}"`);

        const placeholder = el.getAttribute('placeholder');
        if (placeholder) attrs.push(`placeholder="${placeholder.slice(0, 40)}"`);

        const role = el.getAttribute('role');
        if (role) attrs.push(`role="${role}"`);

        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) attrs.push(`aria-label="${ariaLabel.slice(0, 40)}"`);

        const href = el.getAttribute('href');
        if (href && href !== '#') attrs.push(`href="${href.slice(0, 60)}"`);

        const name = el.getAttribute('name');
        if (name) attrs.push(`name="${name.slice(0, 30)}"`);

        const innerText = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 50);
        if (innerText) interactiveTexts.add(innerText);

        const value = (el.value !== undefined && el.value !== null && String(el.value).trim())
            ? String(el.value).slice(0, 30) : '';

        const attrStr = attrs.length ? ' ' + attrs.join(' ') : '';
        let desc = `[ID: ${id}] <${tag}${attrStr}>${innerText}</${tag}>`;
        if (value) desc += ` [当前值: "${value}"]`;

        // 父节点语境：从 li/tr/article 等列表容器聚合相关文本（排名/评分/条目上下文）
        try {
            const parentContainer = el.closest('li, tr, article, .item, .card, .list-item, .entry, .result');
            const parent = parentContainer || el.parentElement;
            if (parent && parent !== document.body) {
                const raw = (parent.innerText || parent.textContent || '').replace(/\\s+/g, ' ').trim();
                const selfText = (el.textContent || '').trim();
                if (raw.length > selfText.length + 10) {
                    desc += ` | 关联信息: ${raw.substring(0, 150)}`;
                }
            }
        } catch (e) {}

        const riskText = [innerText, placeholder || '', role || '', ariaLabel || '', value].join(' ');
        if (/(语音|麦克风|microphone|voice|camera|相机|拍照|图片搜索|以图搜图|扫码|扫一扫|lens)/i.test(riskText)) {
            desc += ' | 风险提示: 语音/拍照/扫码等辅助入口，除非目标明确要求，否则不要优先点击';
        }

        results.push(desc);
    }

    // 文本快照：用于给 VLM 提供页面语义上下文（标题 / 段落 / 表格），
    // 这些元素本身没有 data-som-id，不参与点击，仅辅助理解。
    // 收紧可见性 + 视口过滤，降低 Token。
    function isInViewport(el) {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return false;
        const cs = window.getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0)
            return false;
        const wh = window.innerHeight || document.documentElement.clientHeight;
        const ww = window.innerWidth  || document.documentElement.clientWidth;
        return rect.top <= wh + 50 && rect.bottom >= -50
            && rect.left <= ww + 50 && rect.right >= -50;
    }

    const TEXT_SELECTOR = 'h1, h2, h3, h4, p, li, td, th, strong';  // 收紧：去掉 span/div，太容易把装饰节点带进来
    const INTERACTIVE_DESCENDANT_SELECTOR = '[data-som-id], input, button, a[href], select, textarea';

    for (const el of document.querySelectorAll(TEXT_SELECTOR)) {
        if (!isInViewport(el)) continue;
        // 跳过那些本身就在交互元素内 / 嵌套了交互元素的文本节点，避免重复
        if (el.closest && el.closest('[data-som-id]')) continue;
        if (el.querySelector && el.querySelector(INTERACTIVE_DESCENDANT_SELECTOR)) continue;

        const text = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 100);
        if (text.length < 5) continue;
        if (seenText.has(text) || interactiveTexts.has(text)) continue;

        seenText.add(text);
        results.push(`[TEXT] ${text}`);

        if (seenText.size >= 12) break;
    }

    return { elements: results, markedCount: marked.length };
})
"""
        elements: list[str] = []
        frame_hits = 0
        marked_total = 0
        for frame in page.frames:
            try:
                result = await frame.evaluate(_DOM_EXTRACT_JS)
            except Exception as e:
                logger.debug(f"[LEGACY TEXT DOM] Frame evaluation skipped: {e}")
                continue

            if not result or not isinstance(result, dict):
                continue

            marked_total += int(result.get("markedCount", 0) or 0)
            frame_elements = result.get("elements") or []
            if frame_elements:
                frame_hits += 1
                elements.extend(frame_elements)

        if not elements:
            logger.warning(
                "[LEGACY TEXT DOM] No SoM-tagged interactive elements found; "
                "mark_and_screenshot 可能未成功注入 data-som-id"
            )
            return ""

        dom_text = "\n".join(elements)
        logger.info(
            f"[LEGACY TEXT DOM] Serialized {len(elements)} lines (SoM marked={marked_total}) "
            f"across {frame_hits} frames — read-only, shares IDs with screenshot"
        )
        return dom_text

    # ─────────────────────────────────────────────────────────────────────
    # Accessibility Tree 语义化提取 (新一代图文双模态的文本侧)
    # ─────────────────────────────────────────────────────────────────────
    # 设计分两段输出：
    #   [A] ID 映射段：遍历 document.querySelectorAll('[data-som-id]')，按
    #       ARIA 规范推导 role/name/value/state —— 每一行形如
    #       `[ID: 12] Role: button, Name: "搜索", State: ...`
    #       保留截图红框 ↔ ID ↔ execute_action 选择器的三方锁死。
    #   [B] 语义快照段：CDP Accessibility.getFullAXTree
    #       过滤掉 generic / 无 name/value 的装饰节点，缩进输出整页的 AX Tree
    #       作为 VLM 的页面结构语境。
    # AX Tree 相比 HTML DOM 的优势：过滤 style/script/class 等视觉噪音，
    # 天然只保留语义节点，Token 消耗显著降低。

    @staticmethod
    def _flatten_ax_tree_for_extract(
        node: dict, out: list[str], depth: int = 0, max_depth: int = 15,
    ) -> None:
        flatten_ax_tree_for_extract(node, out, depth, max_depth)

    async def extract_page_text_via_ax_tree(self) -> str:
        """
        通过无障碍树 (AX Tree) 提取页面全部语义文本（专用于数据提取场景）。

        相比 document.body.innerText 的优势：
        - 天然过滤 script/style/class/广告脚本等噪音
        - 只保留语义节点的纯文本内容
        - 覆盖全页（不受视口限制）
        - 结构化缩进保留层级关系

        相比截图 + VLM 的优势：
        - 不受 1280×800 视口限制，一次拿到整页 20+ 条数据
        - 纯文本 token 效率远高于图片

        Returns:
            页面语义文本（纯文本，无 Role/State 标签），最大 16000 字符。
            失败时返回空字符串。
        """
        page = await self._ensure_active_page(reason="ax tree extraction for data")
        if not page:
            return ""

        try:
            # interesting_only=False 获取更完整的语义树
            # 包括 StaticText 等纯文本节点，确保列表数据不遗漏
            ax_root = await self._get_ax_tree_via_cdp(page, interesting_only=False)
            if not ax_root:
                logger.warning("[AX Extract] accessibility.snapshot 返回空")
                return ""

            lines: list[str] = []
            self._flatten_ax_tree_for_extract(ax_root, lines)

            text = "\n".join(lines)
            logger.info(
                f"[AX Extract] 从 AX Tree 提取 {len(lines)} 行语义文本 "
                f"({len(text)} 字符)"
            )
            return text[:16000]

        except Exception as e:
            logger.warning(f"[AX Extract] AX Tree 提取失败: {e}")
            return ""

    async def probe_data_shape(self) -> dict:
        """Lightweight DOM probe used to route extraction without prompt keywords."""
        page = await self._ensure_active_page(reason="probe data shape")
        if not page:
            return {}
        try:
            result = await page.evaluate(
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
                    const visible = (selector) => Array.from(
                        document.querySelectorAll(selector)
                    ).filter(isVisible);

                    const tables = visible(
                        'table, [role="grid"], .el-table, .ant-table, .n-data-table, .dataTable'
                    );
                    let tableRows = 0;
                    let tableCells = 0;
                    for (const table of tables) {
                        const rows = Array.from(
                            table.querySelectorAll('tbody tr, [role="row"]')
                        ).filter(isVisible);
                        tableRows = Math.max(tableRows, rows.length);
                        for (const row of rows) {
                            const cells = Array.from(
                                row.querySelectorAll('td, [role="cell"], [role="gridcell"]')
                            ).filter(isVisible);
                            tableCells = Math.max(tableCells, cells.length);
                        }
                    }

                    const containerSelectors = [
                        'main', 'article', '[role="main"]', '#content', '.content',
                        '.grid_view', '.list', '.list-wp', '.item-list',
                        'ul', 'ol', 'section', '.container'
                    ];
                    let bestList = {count: 0, classRepeat: 0, avgText: 0};
                    for (const root of visible(containerSelectors.join(','))) {
                        const children = Array.from(root.children || []).filter(isVisible);
                        if (children.length < 3) continue;
                        const buckets = new Map();
                        let textTotal = 0;
                        for (const child of children) {
                            const cls = clean(child.className || child.tagName || '').slice(0, 80);
                            buckets.set(cls, (buckets.get(cls) || 0) + 1);
                            textTotal += clean(child.innerText || child.textContent).length;
                        }
                        const repeat = Math.max(...Array.from(buckets.values()), 0);
                        const avgText = textTotal / Math.max(children.length, 1);
                        const score = repeat * Math.max(avgText, 1);
                        const bestScore = bestList.classRepeat * Math.max(bestList.avgText, 1);
                        if (score > bestScore) {
                            bestList = {count: children.length, classRepeat: repeat, avgText};
                        }
                    }

                    const bodyText = clean(document.body ? document.body.innerText : '');
                    return {
                        table_count: tables.length,
                        table_rows: tableRows,
                        table_cells: tableCells,
                        repeated_list_items: bestList.count,
                        repeated_class_count: bestList.classRepeat,
                        repeated_avg_text: Math.round(bestList.avgText),
                        body_text_length: bodyText.length,
                    };
                }"""
            )
            return result if isinstance(result, dict) else {}
        except Exception as e:
            logger.debug("[DATA SHAPE] probe failed: %s", e)
            return {}

    @staticmethod
    def _flatten_ax_tree(
        node: dict, out: list[str], depth: int = 0, max_depth: int = 12
    ) -> None:
        flatten_ax_tree(node, out, depth, max_depth)

    async def extract_accessibility_tree(self) -> str:
        """
        提取页面的无障碍语义树，用作 VLM 图文融合决策的文本侧输入。

        前置条件：与旧版 extract_text_dom 相同，调用前 mark_and_screenshot 需已完成，
        确保 [data-som-id] 已注入 —— 只有这样 ID 映射段才能拿到红框一一对应的编号。

        Returns:
            多行字符串，由两段组成：
              [交互元素 (SoM ID 映射)]  ← 每行一个 `[ID: N] Role/Name/Value/State`
              [页面语义快照 (AX Tree)]  ← Playwright accessibility.snapshot 的缩进平铺
            若两段都空，返回 ""（调用方负责降级）。
        """
        page = await self._ensure_active_page(reason="before extract_accessibility_tree")
        if not page:
            raise RuntimeError("No active page available for AX tree extraction.")

        await self._wait_for_page_stable()
        await self._dismiss_permission_surfaces(reason="before ax tree extraction")

        # ── [A] ID 映射段：按 ARIA 规范从 DOM 属性推导 role/name ──
        # 不调用 `accessibility.snapshot(root=handle)` 的原因：每元素一次 CDP 往返，
        # 50 个元素 ≈ 50 次 round-trip。这里一次 page.evaluate 批量搞定。
        _ID_MAP_JS = r"""
(() => {
    const marked = Array.from(document.querySelectorAll('[data-som-id]'));
    marked.sort((a, b) => {
        const ai = parseInt(a.getAttribute('data-som-id'), 10);
        const bi = parseInt(b.getAttribute('data-som-id'), 10);
        return (isNaN(ai) ? 0 : ai) - (isNaN(bi) ? 0 : bi);
    });

    function deriveRole(el) {
        const explicit = el.getAttribute('role');
        if (explicit) return explicit.trim();
        const tag = el.tagName.toLowerCase();
        if (tag === 'a' && el.hasAttribute('href')) return 'link';
        if (tag === 'button') return 'button';
        if (tag === 'input') {
            const t = (el.getAttribute('type') || 'text').toLowerCase();
            if (['button', 'submit', 'reset'].includes(t)) return 'button';
            if (t === 'checkbox') return 'checkbox';
            if (t === 'radio') return 'radio';
            if (t === 'search') return 'searchbox';
            if (t === 'range') return 'slider';
            return 'textbox';
        }
        if (tag === 'textarea') return 'textbox';
        if (tag === 'select') return 'combobox';
        if (el.isContentEditable) return 'textbox';
        return tag;
    }

    function deriveName(el) {
        const ariaLabelledBy = el.getAttribute('aria-labelledby');
        if (ariaLabelledBy) {
            const parts = ariaLabelledBy.split(/\s+/)
                .map(id => document.getElementById(id))
                .filter(Boolean)
                .map(n => (n.textContent || '').trim());
            const joined = parts.join(' ').trim();
            if (joined) return joined;
        }
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
        const title = el.getAttribute('title');
        if (title && title.trim()) return title.trim();
        const placeholder = el.getAttribute('placeholder');
        if (placeholder && placeholder.trim()) return placeholder.trim();
        const txt = (el.textContent || '').replace(/\s+/g, ' ').trim();
        if (txt) return txt;
        const alt = el.getAttribute('alt');
        if (alt && alt.trim()) return alt.trim();
        return '';
    }

    return marked.map(el => ({
        id: el.getAttribute('data-som-id'),
        role: deriveRole(el),
        name: deriveName(el).slice(0, 80),
        value: (el.value !== undefined && el.value !== null && String(el.value).trim())
            ? String(el.value).slice(0, 40) : '',
        disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
        checked: el.getAttribute('aria-checked') === 'true' || el.checked === true,
        selected: el.selected === true || el.getAttribute('aria-selected') === 'true',
        expanded: el.getAttribute('aria-expanded'),
        required: el.required === true || el.getAttribute('aria-required') === 'true',
        readonly: el.readOnly === true || el.getAttribute('aria-readonly') === 'true',
    }));
})()
"""
        id_rows: list[dict] = []
        try:
            raw = await page.evaluate(_ID_MAP_JS)
            if isinstance(raw, list):
                id_rows = [r for r in raw if isinstance(r, dict) and r.get("id")]
        except Exception as e:
            logger.debug(f"[AX Tree] ID 映射段提取失败: {e}")

        # 可交互角色白名单：只把这些角色写进 @eN 紧凑快照（VLM 决策用）。
        # 其它如 heading/text/img/generic 等落入第二段 AX Tree 作上下文。
        _INTERACTIVE_ROLES = {
            "button", "link", "textbox", "searchbox", "combobox",
            "checkbox", "radio", "switch", "slider",
            "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
            "option", "treeitem",
        }

        # 每轮刷新 @eN 映射；ID 采用 SoM 序号，与截图红框数字同源。
        self.element_mapping.clear()
        compact_lines: list[str] = []
        for row in id_rows:
            role = (row.get("role") or "").strip().lower()
            if role not in _INTERACTIVE_ROLES:
                continue
            try:
                som_id_int = int(row["id"])
            except (TypeError, ValueError):
                continue
            ref = f"@e{som_id_int}"
            name = (row.get("name") or "").strip()
            value = (row.get("value") or "").strip()
            states: list[str] = []
            if row.get("disabled"):
                states.append("disabled")
            if row.get("checked"):
                states.append("checked")
            if row.get("selected"):
                states.append("selected")
            expanded = row.get("expanded")
            if expanded in ("true", "false"):
                states.append(f"expanded={expanded}")
            if row.get("required"):
                states.append("required")
            if row.get("readonly"):
                states.append("readonly")

            parts = [ref, f"[{role or '?'}]"]
            if name:
                parts.append(f'"{name}"')
            if value:
                parts.append(f'value="{value}"')
            if states:
                parts.append("{" + ",".join(states) + "}")
            compact_lines.append(" ".join(parts))
            self.element_mapping[ref] = {
                "som_id": som_id_int,
                "role": role,
                "name": name,
                "selector": f'[data-som-id="{som_id_int}"]',
            }

        # ── [B] 页面语义快照段：CDP AX Tree ──
        ax_lines: list[str] = []
        try:
            ax_root = await self._get_ax_tree_via_cdp(page, interesting_only=True)
            if ax_root:
                self._flatten_ax_tree(ax_root, ax_lines, depth=0, max_depth=12)
        except Exception as e:
            logger.debug(f"[AX Tree] CDP snapshot 失败，跳过语义段: {e}")

        # 语义段上限 120 行（避免长页面 AX Tree 撑爆 Token；ID 映射段不限）
        MAX_SEMANTIC_LINES = 120
        truncated_semantic = ax_lines[:MAX_SEMANTIC_LINES]

        sections: list[str] = []
        if compact_lines:
            sections.append(
                "【可交互元素 (@eN 语义快照)】\n"
                "格式：@eN [role] \"name\" value=\"...\" {states}；@eN 中的数字 = 截图红框序号。\n"
                "操作时 target_id 直接填这个数字（如 @e5 → target_id=5）。\n"
                + "\n".join(compact_lines)
            )
        if truncated_semantic:
            hint = ""
            if len(ax_lines) > MAX_SEMANTIC_LINES:
                hint = f"\n...[AX Tree 过长，已截断 {len(ax_lines) - MAX_SEMANTIC_LINES} 行]..."
            sections.append(
                "【页面语义快照 (AX Tree)】\n" + "\n".join(truncated_semantic) + hint
            )

        if not sections:
            logger.warning(
                "[AX Tree] @eN 映射段与语义段均为空；mark_and_screenshot 可能未注入 data-som-id"
            )
            return ""

        logger.info(
            f"[AX Tree] Emitted {len(compact_lines)} @eN refs + "
            f"{len(truncated_semantic)}/{len(ax_lines)} semantic lines"
        )
        return "\n\n".join(sections)

    # 元素定位超时：改短以 fail fast，页面已刷新时不再苦等
    _LOCATOR_TIMEOUT = 3000

    async def execute_action(
        self,
        action: "VSpiderAction | dict",
        workflow_memory: dict | None = None,
    ) -> Page | None:
        """
        根据 VLM 返回的决策执行对应的浏览器操作。

        dispatch 到 ActionRegistry 中注册的 Handler，具体动作实现见 actions.py。
        保留原接口：Tab Guard / _last_action_error → ActionExecutionError 的自愈链路
        仍由此方法兜底，行为与拆分前 100% 一致。

        Args:
            action: VLM 的决策。推荐传入 VSpiderAction 实例；也兼容原始 dict
                    （会自动 coerce 为 VSpiderAction 并剥离 __rpa_* 元数据）。
            workflow_memory: 跨页面记忆库（可变 dict），由主循环传入；
                             save_to_memory 动作会直接写入此 dict；
                             type 动作会读取此 dict 做 {{key}} 插值。

        Returns:
            执行动作后处于激活状态的 Page 对象（供调用方更新 page 引用）。
            done / unknown action 时返回当前页（无切换）。
        """
        # 延迟导入避免与 actions.py 形成循环
        try:
            from .actions import ActionContext, ActionRegistry, UnknownActionError
        except ImportError:
            from actions import ActionContext, ActionRegistry, UnknownActionError

        # ── 每轮开始前清空上轮残留错误标记 ──────────────────────────────────
        self._last_action_error = None
        self._last_action_result = None

        # ── 输入标准化：dict → VSpiderAction（同时剥离 RPA 元数据） ────────
        rpa_required_keys: list[str] = []
        rpa_template_value: str = ""
        if isinstance(action, dict):
            action_copy = dict(action)  # 不污染调用方
            rpa_required_keys = sorted(
                set(action_copy.pop("__rpa_required_keys", None) or [])
            )
            rpa_template_value = str(
                action_copy.pop("__rpa_template_value", None) or ""
            )
            action_model = VSpiderAction(**action_copy)
        else:
            action_model = action

        _action_started = time.perf_counter()
        _initial_pages = len(self._context.pages) if self._context else 0
        # Snapshot of pages that existed BEFORE this action. Tab Guard L3 uses
        # this to distinguish "freshly opened by this action" (should follow)
        # from "old tab sitting around from a previous step" (should not steal
        # focus from an explicit switch_tab).
        _known_page_ids: set[int] = (
            {id(p) for p in self._context.pages} if self._context else set()
        )
        # Full structural snapshot for the post-action TAB STATE CHANGE notice.
        # Cheap (O(N tabs), no async I/O); fires when opens/closes/focus drift.
        try:
            from .tab_state_tracker import (
                compute_tab_delta_notice as _compute_tab_delta_notice,
                snapshot_tabs as _snapshot_tabs,
            )
        except ImportError:  # pragma: no cover — direct script import
            from tab_state_tracker import (  # type: ignore[no-redef]
                compute_tab_delta_notice as _compute_tab_delta_notice,
                snapshot_tabs as _snapshot_tabs,
            )
        _pre_action_tab_snapshot = _snapshot_tabs(self)

        def _maybe_set_tab_delta_notice() -> None:
            """Compose a tab-delta notice unless a handler already set one
            (handlers like click_new_tab / fetch_link_content / Tab Guard
            produce richer per-action notices and we don't want to clobber)."""
            if self._tab_switch_notice:
                return
            try:
                _delta = _compute_tab_delta_notice(
                    self,
                    _pre_action_tab_snapshot,
                    last_action=str(action_model.action or ""),
                    last_target_id=int(action_model.target_id or 0),
                )
            except Exception as _delta_err:
                logger.debug("[TAB STATE] delta notice skipped: %s", _delta_err)
                return
            if _delta:
                # J: use set_tab_notice so severity stays in sync. The
                # guard at the top of _maybe_set_tab_delta_notice already
                # ensures no prior notice; coalesce flag is therefore moot.
                self.set_tab_notice(_delta, severity="info", coalesce=False)
                logger.info(
                    "[TAB STATE] delta detected for action=%s tid=%s; "
                    "feedback injected for next VLM step",
                    action_model.action, action_model.target_id,
                )

        page = await self._ensure_active_page(reason="before execute_action")
        if not page:
            logger.error("No active page available for action execution")
            self._last_action_result = ActionResult.from_action(
                action_model,
                success=False,
                error="No active page available for action execution",
                before_pages=_initial_pages,
                after_pages=len(self._context.pages) if self._context else 0,
                metadata={
                    "duration_ms": round((time.perf_counter() - _action_started) * 1000, 2)
                },
            )
            return None

        _before_url = page.url or ""
        _before_pages = len(self._context.pages) if self._context else _initial_pages

        def _safe_page_url(candidate: Page | None) -> str:
            if candidate is not None:
                try:
                    if not candidate.is_closed():
                        return candidate.url or ""
                except Exception:
                    pass
            try:
                return self.current_url or ""
            except Exception:
                return ""

        def _record_action_result(
            *,
            success: bool,
            message: str = "",
            error: str = "",
            active: Page | None = None,
            metadata: dict | None = None,
        ) -> ActionResult:
            merged_metadata = dict(metadata or {})
            merged_metadata.setdefault(
                "duration_ms",
                round((time.perf_counter() - _action_started) * 1000, 2),
            )
            result = ActionResult.from_action(
                action_model,
                success=success,
                message=message,
                error=error,
                before_url=_before_url,
                after_url=_safe_page_url(active),
                before_pages=_before_pages,
                after_pages=len(self._context.pages) if self._context else 0,
                metadata=merged_metadata,
            )
            self._last_action_result = result
            return result

        # ── Dispatch 到 Registry 中注册的 Handler ─────────────────────────
        try:
            handler = ActionRegistry.get(action_model.action)
        except UnknownActionError:
            logger.warning(f"Unknown action type: {action_model.action}")
            _record_action_result(
                success=False,
                error=f"Unknown action type: {action_model.action}",
                active=page,
            )
            return page  # 与原 else 分支一致：直接返回当前页，跳过 Tab Guard

        # ``workflow_memory or {}`` was a reference-killing bug: an EMPTY dict
        # is falsy in Python, so ``{} or {}`` returns a brand-new dict and
        # any writes the handler makes (e.g. ChatExtractHandler's sentinel
        # ``__chat_extract_completed``) never reach main.py's local
        # workflow_memory. Use a None-check instead so the caller's dict
        # reference is preserved when it's an empty (but valid) dict.
        # See run_log_20260518_151238 step 5/6: the sentinel was written
        # to a phantom dict, main.py never saw it, and chat_extract ran
        # twice before VLM finally emitted done on its own.
        if workflow_memory is None:
            workflow_memory = {}
        ctx = ActionContext(
            action=action_model,
            browser=self,
            workflow_memory=workflow_memory,
            page=page,
            rpa_required_keys=rpa_required_keys,
            rpa_template_value=rpa_template_value,
            session_router=getattr(self, "_session_router", None),
        )
        try:
            handler_result = await handler.execute(ctx)
        except Exception as exc:
            _record_action_result(
                success=False,
                error=str(exc),
                active=page,
                metadata={"exception_type": type(exc).__name__},
            )
            raise

        # ── Handler 显式返回 Page（switch_tab / done）→ 跳过 Tab Guard ──
        if handler_result is not None:
            self._page = handler_result
            if self._last_action_error is not None:
                _err = self._last_action_error
                self._last_action_error = None
                _record_action_result(
                    success=False,
                    error=(
                        f"action={action_model.action} "
                        f"target_id={action_model.target_id}: {_err}"
                    ),
                    active=handler_result,
                    metadata={"exception_type": type(_err).__name__},
                )
                raise ActionExecutionError(
                    f"action={action_model.action} "
                    f"target_id={action_model.target_id}: {_err}"
                ) from _err
            _record_action_result(
                success=True,
                message="handler returned page",
                active=handler_result,
            )
            # Surface tab open/close/focus drift to the next VLM step.
            _maybe_set_tab_delta_notice()
            return handler_result

        # ══════════════════════════════════════════════════════
        # 焦点守护（Tab Guard）
        # 统一接管所有动作引发的标签页状态变化：
        #   - 新 tab 弹出（Follow）
        #   - 当前页关闭（Fallback）
        #   - 全部页面消失（Emergency）
        # ══════════════════════════════════════════════════════
        active_page = await ensure_active_page(
            self._context, page, known_pages=_known_page_ids
        )

        if active_page is not page:
            # 新页面尚未挂载事件处理器（XHR 拦截、下载监听等），补充注册
            await self._register_page(
                active_page,
                reason=f"tab guard: switched from {(page.url or 'closed')[:60]}",
                activate=True,
            )
            # ── 标签页切换感知：因果归因通知，让 VLM 识别"这是刚才点击的成功副产物" ──
            # 旧版把所有切换都默认为"误触"，推着 VLM 盲目 close_tab → 反向循环。
            # 中立版又太被动，VLM 看不出切换由自己动作触发 → 错把切换当"页面异常"。
            # 新版：报事实 + 明确因果（"是你上一步 click 触发的"）+ 判断框架。
            old_url = page.url if not page.is_closed() else "(已关闭)"
            new_url = active_page.url or "about:blank"
            _prev_action = action_model.action
            _prev_tid = action_model.target_id
            if _prev_action in ("click", "click_new_tab"):
                # J: tab guard observed a switch caused by user's click —
                # fresh notice clobbers any earlier handler notice (the
                # tab switch is the dominant event for the next VLM step).
                self.set_tab_notice(
                    f"✅ 你上一步 {_prev_action}(元素 #{_prev_tid}) 已成功触发页面切换/新标签：\n"
                    f"  旧页面: {old_url[:120]}\n"
                    f"  当前页面: {new_url[:120]}\n"
                    f"⚠️ 点击已经生效，**禁止再次点击同一个 #{_prev_tid}**（会被 LOOP GUARD 拦截）。\n"
                    f"请对照【用户目标】判断：\n"
                    f"  • 目标是'点击XX在新标签打开（+可能再切回）' → "
                    f"点击阶段已完成。若目标要求切回搜索页，用 switch_tab 回原页；"
                    f"若整个任务已达成，直接 action=done 结束。\n"
                    f"  • 目标要求在新页面继续填表/提取 → 留在当前页继续操作。\n"
                    f"  • 当前页明显是广告/验证墙/无关页（URL 含 sem/ad/promo、Cloudflare 验证） "
                    f"→ close_tab 并在原页选**另一个** target_id 重试。",
                    severity="info",
                    coalesce=False,
                )
            else:
                # J: non-click action triggered a tab switch — surface as
                # info notice; VLM still needs to decide whether to follow.
                self.set_tab_notice(
                    f"ℹ️ 标签页切换（上一步动作: {_prev_action}）：\n"
                    f"  旧页面: {old_url[:120]}\n"
                    f"  当前页面: {new_url[:120]}\n"
                    f"请对照用户目标判断这是预期切换还是异常，再决定下一步。",
                    severity="info",
                    coalesce=False,
                )
        else:
            # J: no tab switch — drop any stale notice from earlier in
            # this round; clear_tab_notice resets severity in lock-step.
            self.clear_tab_notice()
        # 更新实例持有的活跃页引用，确保下一轮截图使用正确页面
        self._page = active_page

        # ── 自愈抛出：若本轮操作有异常，向上层暴露以触发 Self-Healing ────────
        if self._last_action_error is not None:
            _err = self._last_action_error
            self._last_action_error = None
            _record_action_result(
                success=False,
                error=(
                    f"action={action_model.action} "
                    f"target_id={action_model.target_id}: {_err}"
                ),
                active=active_page,
                metadata={"exception_type": type(_err).__name__},
            )
            raise ActionExecutionError(
                f"action={action_model.action} "
                f"target_id={action_model.target_id}: {_err}"
            ) from _err

        _record_action_result(success=True, message="ok", active=active_page)
        # Surface tab open/close/focus drift to the next VLM step (this path
        # already sets _tab_switch_notice for active-page changes; the helper
        # is a no-op when that happened, but covers structural changes that
        # didn't shift focus — e.g. background popups closing).
        _maybe_set_tab_delta_notice()
        return active_page

    async def _wait_after_action(
        self, is_navigation: bool = False, light_action: bool = False
    ) -> None:
        """
        智能等待策略：根据动作类型选择轻量或严谨等待。

        light_action=True（type/hover/save_to_memory 等不引发跳转的动作）：
            只做 0.5s 硬等待，跳过 networkidle，避免自动补全 XHR 造成卡顿。
        light_action=False（click/goto/press_key 等可能引发跳转的动作）：
            保留 networkidle(3s) + 硬等待双保险策略。
        """
        if light_action:
            # 轻量动作：只给前端 UI 少量渲染时间，不等网络
            await asyncio.sleep(0.5)
            await self._dismiss_permission_surfaces(reason="after light action")
            return

        page = await self._ensure_active_page(reason="wait after action")
        if not page:
            return

        # 重度动作：等待网络静默（超时缩短至 3s，避免无限 XHR 流卡死）
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
            logger.debug("networkidle reached after action")
        except Exception:
            logger.debug("networkidle timeout (3s) after action, proceeding anyway")

        # 硬等待兜底，给前端框架（Vue nextTick / React setState）渲染留时间
        wait_time = 2.0 if is_navigation else 1.0
        await asyncio.sleep(wait_time)
        await self._dismiss_permission_surfaces(
            reason="after navigation action" if is_navigation else "after action"
        )

    async def _wait_for_page_stable(self) -> None:
        """
        等待页面完全稳定（用于截图前和首次导航后）。

        策略：动态等待 + 必定执行的硬等待双保险。
        不论 networkidle 是否通过，都会追加硬等待。
        """
        page = await self._ensure_active_page(reason="wait for page stable")
        if not page:
            return

        # 第 1 层：尝试 networkidle
        idle_reached = False
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=config.PAGE_STABLE_TIMEOUT
            )
            idle_reached = True
            logger.debug("networkidle reached")
        except Exception:
            logger.debug(
                f"networkidle timeout ({config.PAGE_STABLE_TIMEOUT}ms), not critical"
            )

        # 第 2 层：硬等待兜底
        # 如果 networkidle 已成功，说明网络已静默，硬等待减半即可
        # 确保 SPA 框架的异步渲染（Vue nextTick / React setState）完成
        fallback = config.PAGE_FALLBACK_WAIT
        if idle_reached:
            fallback = fallback / 2

        # 强制加入 1 秒的弹窗动画渲染缓冲，确保 Dialog/Modal 如百度登录的 Fade-in 完全结束
        fallback += 1.0

        await asyncio.sleep(fallback)

        # 第 3 层：内容就绪守卫
        # SPA 页面（React/Vue）常在 networkidle 后才发起数据 API 请求并渲染主体；
        # 若此时 body 可见文本仍然稀少（< 300 字符），等待最多 5 秒让内容出现。
        # 超时则继续执行，不阻塞（登录页/错误页等内容稀少的页面不受影响）。
        try:
            await page.wait_for_function(
                """() => {
                    const body = document.body;
                    if (!body) return false;
                    // 主内容容器已有实质内容
                    const main = body.querySelector(
                        'main, article, [role="main"], #content, .content, ' +
                        '[class*="main-content"], [class*="page-content"]'
                    );
                    if (main && (main.innerText || '').trim().length > 80) return true;
                    // 兜底：整体 body 文本（含 nav）超过 300 字符
                    return (body.innerText || '').replace(/\\s+/g, ' ').trim().length > 300;
                }""",
                timeout=5000,
            )
            logger.debug("content-ready guard passed")
        except Exception:
            logger.debug("content-ready guard timed out (sparse page or slow render), proceeding")

    # ========== 级联菜单辅助：悬停后子菜单探测 ==========

    async def _count_interactive_elements(self) -> int:
        """统计当前页面可见的可交互元素数量（用于悬停前后对比）。"""
        page = await self._ensure_active_page()
        if not page:
            return 0
        try:
            count = await page.evaluate("""() => {
                const sel = 'a,button,[role="button"],[role="menuitem"],[role="option"],li';
                let n = 0;
                for (const el of document.querySelectorAll(sel)) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) n++;
                }
                return n;
            }""")
            return int(count)
        except Exception:
            return 0

    async def _wait_for_submenu(self, count_before: int, max_wait: float = 2.0) -> bool:
        """
        在 hover 之后轮询，最多等待 max_wait 秒检测是否有新可交互元素出现。
        新元素出现（子菜单弹出）则返回 True，超时返回 False。
        """
        step = 0.2
        elapsed = 0.0
        while elapsed < max_wait:
            await asyncio.sleep(step)
            elapsed += step
            count_now = await self._count_interactive_elements()
            if count_now > count_before:
                return True
        return False

    async def _is_calendar_popup_visible(self) -> bool:
        """
        检测页面上是否存在可见的日历/日期选择器弹窗。
        支持 Ant Design、Element UI、Bootstrap Datepicker 等主流组件。
        """
        page = await self._ensure_active_page()
        if not page:
            return False
        try:
            visible = await page.evaluate("""() => {
                const selectors = [
                    '.ant-picker-dropdown',
                    '.ant-calendar-picker-container',
                    '.el-date-picker',
                    '.el-date-range-picker',
                    '.el-picker-panel',
                    '.daterangepicker',
                    '.datepicker',
                    '.flatpickr-calendar',
                    '.picker-panel',
                    '[class*="date-picker"][class*="popup"]',
                    '[class*="datepicker"][class*="show"]',
                    '[class*="calendar"][class*="open"]',
                ];
                for (const sel of selectors) {
                    try {
                        const el = document.querySelector(sel);
                        if (!el) continue;
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        if (r.width > 10 && r.height > 10 &&
                            s.display !== 'none' && s.visibility !== 'hidden' &&
                            parseFloat(s.opacity || '1') > 0.1) return true;
                    } catch (_) {}
                }
                return false;
            }""")
            return bool(visible)
        except Exception:
            return False

    # ========== 文件上传辅助 ==========

    def set_upload_file(self, file_path: str | Path) -> None:
        """预配置上传文件路径（由 main.py 通过 --upload-file 参数注入）。"""
        self._upload_file = Path(file_path)

    async def _find_file_input(self, target_id: int) -> "ActionTarget | None":
        """
        寻找 target_id 附近的 <input type='file'> 元素。
        优先级：target 本身 → 父容器内查找 → 页面级全局查找（含隐藏）。
        """
        page = await self._ensure_active_page()
        if not page:
            return None

        for frame in page.frames:
            try:
                base = await frame.query_selector(f'[data-som-id="{target_id}"]')
                if not base:
                    continue

                handle = await frame.evaluate_handle(
                    """targetId => {
                        const base = document.querySelector(`[data-som-id="${targetId}"]`);
                        if (!base) return null;
                        // 1. target 本身就是 file input
                        if (base.tagName === 'INPUT' && (base.getAttribute('type') || '').toLowerCase() === 'file') return base;
                        // 2. 向上最多 6 层父容器内查找
                        let el = base;
                        for (let i = 0; i < 6; i++) {
                            if (!el) break;
                            const fi = el.querySelector('input[type="file"]');
                            if (fi) return fi;
                            el = el.parentElement;
                        }
                        // 3. 页面级兜底（自定义上传组件通常把 file input 隐藏在 body 下）
                        return document.querySelector('input[type="file"]');
                    }""",
                    target_id,
                )
                el = handle.as_element() if handle else None
                if el:
                    return ActionTarget(frame=frame, handle=el, selector='input[type="file"]')
            except Exception:
                continue
        return None

    # ========== 全局下载拦截 ==========

    async def _handle_download(self, download) -> None:
        """全局静默下载接管，彻底绕过系统原生的另存为弹窗"""
        try:
            # 确保下载目录存在
            self._download_dir.mkdir(parents=True, exist_ok=True)
            
            # 拼接最终路径并静默保存
            final_path = self._download_dir / download.suggested_filename
            await download.save_as(str(final_path.resolve()))
            self.last_download_path = str(final_path.resolve())
            self.last_download_name = str(download.suggested_filename or final_path.name)
            
            print(f"\n✅ [底层拦截] 成功拦截文件下载并静默保存至: \033[36m{final_path.resolve()}\033[0m\n")
            logger.info(f"[DOWNLOAD INTERCEPT] Saved native file: {final_path.resolve()}")
            register_download_artifact(
                final_path,
                source_url=str(getattr(download, "url", "") or ""),
                produced_by="browser_download",
                step_id="native_download",
            )
        except Exception as e:
            logger.error(f"[DOWNLOAD INTERCEPT] Failed to save file: {e}")

    # ========== XHR/Fetch 网络拦截 ==========

    def configure_interceptor(
        self,
        enabled: bool = True,
        filename: str = "output",
        unique_key: str | list[str] = None,
        min_list_size: int = 5,
        url_pattern: str | None = None,
        output_contract: dict | None = None,
    ) -> None:
        """
        配置 XHR/Fetch 拦截器参数。

        Args:
            enabled:     是否启用通用拦截（--xhr 参数控制）
            filename:    通用拦截数据保存的文件名（后缀随容器改写）
            unique_key:  去重字段（如 "id"）
            min_list_size: 启发式探测阈值——JSON 中列表长度 >= 此值才认为是数据
            url_pattern: 精准截胡关键词（子串匹配）。
                         匹配到此关键词的响应 URL 会触发"主引擎"分支：
                         数据存入 self._intercepted_data，main.py 检测后直接保存跳过 VLM。
                         独立于 enabled，即使 enabled=False 也可单独启用精准截胡。
            output_contract: 本次 run 的 output_contract.v1 dict；决定拦截数据
                         的落盘容器（xlsx/csv/jsonl），缺省时落 jsonl（不默认 xlsx）。
        """
        self._intercept_enabled = enabled
        self._intercept_filename = filename
        self._intercept_unique_key = unique_key
        self._intercept_min_list_size = min_list_size
        self._intercept_url_pattern = url_pattern.strip() if url_pattern else None
        self._intercept_output_contract = dict(output_contract) if isinstance(output_contract, dict) else None
        # 每次重新配置时清空上次缓存，防止旧数据触发误判
        self._intercepted_data = None
        self._intercept_seen_row_keys.clear()
        self._intercept_schema_fingerprints.clear()
        self._intercept_endpoint_scores.clear()
        logger.info(
            f"Interceptor configured | enabled={enabled} | "
            f"file={filename} | unique_key={unique_key} | "
            f"min_list={min_list_size} | url_pattern={self._intercept_url_pattern!r}"
        )

    def set_interceptor_output_contract(self, output_contract: dict | None) -> None:
        """Refresh the run's output_contract after late goal inference,
        without resetting dedup state the way configure_interceptor does."""
        self._intercept_output_contract = dict(output_contract) if isinstance(output_contract, dict) else None

    def configure_network_intelligence(self, run_id: str | None) -> None:
        """Enable per-run network candidate indexing.

        ``run_id`` is normally the same timestamp used for HTML log /
        phase jsonl. Passing ``None`` disables persistence while keeping
        existing XHR Excel interception untouched.
        """
        rid = str(run_id or "").strip()
        self._network_run_id = rid or None
        self._network_candidates_seen.clear()

    def _record_network_candidate_safe(
        self,
        *,
        response: Response,
        rows: list[dict],
        score: int = 0,
    ) -> None:
        if not self._network_run_id or not rows:
            return
        try:
            method = response.request.method
        except Exception:
            method = "GET"
        try:
            resource_type = response.request.resource_type
        except Exception:
            resource_type = "xhr"
        try:
            content_type = response.headers.get("content-type", "")
        except Exception:
            content_type = ""
        try:
            req_headers = dict(response.request.headers or {})
        except Exception:
            req_headers = {}
        try:
            req_body = response.request.post_data
        except Exception:
            req_body = None
        fp = f"{response.url}|{len(rows)}|{self._schema_fingerprint(rows)}"
        fp_hash = self._stable_json_hash(fp)
        if fp_hash in self._network_candidates_seen:
            return
        self._network_candidates_seen.add(fp_hash)
        try:
            _record_network_candidate(
                run_id=self._network_run_id,
                url=response.url,
                method=method,
                status=response.status,
                resource_type=resource_type,
                rows=rows,
                score=score,
                content_type=content_type,
                request_headers=req_headers,
                request_body=req_body,
            )
        except Exception as exc:
            logger.debug("[NETWORK INTEL] record candidate failed: %s", exc)

    async def _handle_xhr_response(self, response: Response) -> None:
        """
        XHR/Fetch 响应拦截处理器（双轨制）。

        轨道 1 — 精准截胡（url_pattern，独立于 enabled 标志）：
            当 Response URL 包含 _intercept_url_pattern 子串时触发。
            解析 JSON → 提取数据列表 → 存入 self._intercepted_data（快照）。
            main.py 轮询此变量，命中后直接保存数据并跳过 VLM（主引擎分支）。

        轨道 2 — 通用拦截（enabled=True，--xhr 参数控制）：
            对所有 fetch/xhr 响应做启发式探测，发现数据列表则写入 Excel。
            用于翻页场景的全量数据收集，与轨道 1 独立运行、互不干扰。

        自动挂载到 page.on("response")，对每个网络响应执行：
        1. 过滤：只处理 fetch/xhr 类型的请求
        2. 解析：尝试 response.json()
        3. 探测：在 JSON 中寻找长度 >= min_list_size 的列表
        4. 保存（轨道 2）/ 快照（轨道 1）
        """
        url = response.url

        # ── 提前检查：两条轨道都不需要时直接返回 ──────────────────────────
        pattern_active = bool(self._intercept_url_pattern)
        general_active = self._intercept_enabled
        network_active = bool(self._network_run_id)
        if not pattern_active and not general_active and not network_active:
            return

        # 只处理 API 请求（fetch / xhr）
        try:
            resource_type = response.request.resource_type
        except Exception:
            return
        if resource_type not in ("fetch", "xhr"):
            return

        # 只处理成功的响应
        if response.status < 200 or response.status >= 300:
            return

        # 尝试解析 JSON（两条轨道共用）
        try:
            json_body = await response.json()
        except Exception:
            # 非 JSON 响应（图片、HTML、纯文本），忽略
            return

        # ── 轨道 1：精准截胡 ── URL 模式匹配 ────────────────────────────
        if pattern_active and self._intercepted_data is None:
            # 子串匹配（case-insensitive）
            if self._intercept_url_pattern.lower() in url.lower():
                core_list = self._extract_data_list(json_body)
                if core_list:
                    new_rows = self._dedupe_intercept_rows(core_list)
                    if not new_rows:
                        logger.info(f"[XHR CORE] Duplicate API payload skipped: {url[:120]}")
                        return
                    self._remember_intercept_schema(new_rows)
                    self._intercepted_data = new_rows
                    # 同步写盘（防止 main.py 未及时消费时数据丢失）
                    try:
                        save_intercepted_data(
                            json_list=new_rows,
                            filename=self._intercept_filename,
                            unique_key=self._intercept_unique_key,
                            output_contract=self._intercept_output_contract,
                        )
                        self._intercept_count += len(new_rows)
                    except Exception as save_err:
                        logger.error(f"[XHR CORE] Failed to save: {save_err}")

                    # 彩色高亮提示（醒目绿色）
                    GREEN_BOLD = "\033[1;32m"
                    CYAN = "\033[36m"
                    RESET = "\033[0m"
                    print(
                        f"\n{GREEN_BOLD}[XHR INTERCEPT] 成功拦截核心 API 数据！{RESET} "
                        f"{CYAN}{len(new_rows)} new records{RESET} "
                        f"← {url[:100]}\n"
                    )
                    logger.info(
                        f"[XHR CORE] Captured {len(new_rows)} new records "
                        f"(pattern={self._intercept_url_pattern!r}): {url[:120]}"
                    )
                    return  # 核心数据已处理，无需再走通用轨道重复保存

        # ── 轨道 2/3：通用拦截 + Network Intelligence ────────────────
        # Network Intelligence 只记录候选接口摘要，不保存 Excel；它应能
        # 独立于旧的 --xhr 通用拦截开关运行。
        data_list = self._extract_data_list(json_body)
        if not data_list:
            return

        score, fingerprint = self._score_intercept_candidate(url, data_list)
        if network_active:
            self._record_network_candidate_safe(
                response=response,
                rows=[r for r in data_list if isinstance(r, dict)],
                score=score,
            )
        if not general_active:
            return
        if score < 5:
            logger.debug(
                f"[XHR SENTINEL] Candidate ignored score={score} "
                f"rows={len(data_list)} fp={fingerprint[:80]} url={url[:120]}"
            )
            return

        new_rows = self._dedupe_intercept_rows(data_list)
        if not new_rows:
            logger.info(
                f"[XHR SENTINEL] Duplicate payload skipped "
                f"score={score} fp={fingerprint[:80]} url={url[:120]}"
            )
            return

        self._remember_intercept_schema(new_rows)

        logger.info(
            f"[XHR SENTINEL] Accepted {len(new_rows)}/{len(data_list)} new records "
            f"score={score} fp={fingerprint[:80]} from: {url[:120]}..."
        )

        # 按 output_contract 容器保存（无契约时落 jsonl，不默认 xlsx）
        try:
            save_intercepted_data(
                json_list=new_rows,
                filename=self._intercept_filename,
                unique_key=self._intercept_unique_key,
                output_contract=self._intercept_output_contract,
            )
            self._intercept_count += len(new_rows)
        except Exception as e:
            logger.error(f"[XHR INTERCEPT] Failed to save data: {e}")

    def _stable_json_hash(self, value) -> str:
        return stable_json_hash(value)

    def _row_dedup_key(self, row: dict) -> str:
        return row_dedup_key(row, self._intercept_unique_key)

    def _dedupe_intercept_rows(self, rows: list[dict]) -> list[dict]:
        return dedupe_intercept_rows(rows, self._intercept_unique_key, self._intercept_seen_row_keys)

    def _schema_fingerprint(self, rows: list[dict]) -> str:
        return schema_fingerprint(rows)

    def _remember_intercept_schema(self, rows: list[dict]) -> None:
        fp = self._schema_fingerprint(rows)
        if fp:
            self._intercept_schema_fingerprints.add(fp)

    def _score_intercept_candidate(self, url: str, rows: list[dict]) -> tuple[int, str]:
        return score_intercept_candidate(
            url, rows,
            min_list_size=self._intercept_min_list_size,
            schema_fingerprints=self._intercept_schema_fingerprints,
            endpoint_scores=self._intercept_endpoint_scores,
        )

    def _extract_data_list(self, json_body) -> list[dict] | None:
        return extract_data_list(json_body, self._intercept_min_list_size)

    @property
    def current_url(self) -> str:
        """返回当前活动页面的 URL，页面不可用时返回空字符串。"""
        try:
            return self._page.url if self._page else ""
        except Exception:
            return ""

    def get_active_tab_index(self) -> int:
        """当前 self._page 在 context.pages 中的序号；无法确定时返回 -1。"""
        if not self._context or not self._page:
            return -1
        open_pages = [p for p in self._context.pages if not p.is_closed()]
        try:
            return open_pages.index(self._page)
        except ValueError:
            return -1

    async def get_tabs_state(self) -> str:
        """
        返回当前所有标签页的状态摘要字符串，供注入 VLM 提示使用。

        基本格式：
          [0] 百度一下 (活跃) | [1] 淘宝 | [2] 京东

        若 Tab Visit Stack 非空，会在末尾追加返回路径提示：
          ... ↩ close_tab 将回到 [N] <parent title>

        让 VLM 知道"现在关掉当前 tab 会落到哪儿"，避免拍脑袋猜父子关系。

        若浏览器尚未启动或无任何页面，返回空字符串。
        """
        if not self._context:
            return ""
        open_pages = [p for p in self._context.pages if not p.is_closed()]
        if not open_pages:
            return ""
        parts: list[str] = []
        active_idx = -1
        for idx, p in enumerate(open_pages):
            try:
                title = (await p.title()) or p.url or "about:blank"
                if len(title) > 30:
                    title = title[:27] + "..."
            except Exception:
                title = p.url or "about:blank"
            is_active = (p == self._page)
            if is_active:
                active_idx = idx
            label = f"[{idx}] {title}" + (" (活跃)" if is_active else "")
            parts.append(label)
        base = " | ".join(parts)

        # ── Tab Visit Stack 返回路径提示 ────────────────────────────────
        parent = self.peek_tab_visit()
        if parent is not None and parent in open_pages:
            try:
                parent_title = (await parent.title()) or parent.url or "about:blank"
                if len(parent_title) > 24:
                    parent_title = parent_title[:21] + "..."
            except Exception:
                parent_title = parent.url or "about:blank"
            parent_idx = open_pages.index(parent)
            # 只在父 != 当前活跃 tab 时提示（否则提示无意义）
            if parent_idx != active_idx:
                base += f"   ↩ close_tab 将回到 [{parent_idx}] {parent_title}"
        return base

    async def get_active_page_summary(self) -> str:
        """返回当前激活页面的标题和 URL，供提示词注入与调试使用。"""
        page = await self._ensure_active_page(reason="get active page summary")
        if not page:
            return ""

        try:
            title = (await page.title()) or ""
        except Exception:
            title = ""

        try:
            url = page.url or ""
        except Exception:
            url = ""

        parts: list[str] = []
        if title:
            parts.append(f"标题: {title[:80]}")
        if url:
            parts.append(f"URL: {url[:120]}")
        return " | ".join(parts)

    async def dom_signature(self) -> str:
        """E1 感知复用探针：href + DOM 节点数 + body 文本长度 + 可交互元素数 拼 sha1。

        一次轻量 evaluate（<5ms）；失败 / 无页面时安静返回空串——调用方视为
        "无法判定"并走全量感知，探针绝不影响主链路。
        """
        page = await self._ensure_active_page(reason="dom signature probe")
        if not page:
            return ""
        try:
            raw = await page.evaluate(
                """() => {
                    const body = document.body;
                    const interactive = document.querySelectorAll(
                        'a,button,input,select,textarea,[role=\"button\"],[onclick],[contenteditable]'
                    ).length;
                    return [
                        location.href,
                        document.getElementsByTagName('*').length,
                        body ? (body.innerText || '').length : 0,
                        interactive,
                    ].join('|');
                }"""
            )
        except Exception:
            return ""
        return hashlib.sha1(str(raw).encode("utf-8")).hexdigest()

    async def reload_active_page(self, reason: str = "") -> bool:
        """对当前页做一次轻量重载，并等待重新稳定。"""
        page = await self._ensure_active_page(reason=f"reload active page {reason}".strip())
        if not page:
            return False
        try:
            await page.reload(wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            await self._wait_for_page_stable()
            logger.info(f"[PAGE RECOVERY] Reloaded active page ({reason or 'no reason'})")
            return True
        except Exception as e:
            logger.warning(f"[PAGE RECOVERY] Reload failed ({reason or 'no reason'}): {e}")
            return False

    @property
    def intercepted_count(self) -> int:
        """返回本次会话中拦截到的数据总条数。"""
        return self._intercept_count

    @property
    def intercepted_data(self) -> list | None:
        """
        精准截胡（轨道 1）的数据快照。

        当 url_pattern 命中后由 _handle_xhr_response 写入；
        main.py 检测此属性，非 None 时触发"主引擎生效"分支，跳过 VLM 提取。
        Returns None 时走正常 VLM 备用流程。
        """
        return self._intercepted_data

    # ========== 生命周期 ==========

    async def close(self) -> None:
        """关闭持久化上下文和 Playwright 实例，释放资源。"""
        self._closed = True
        if self._intercept_count > 0:
            logger.info(
                f"Session summary: intercepted {self._intercept_count} total records"
            )
        try:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
            logger.info("Browser closed")
        except Exception as e:
            logger.warning(f"Error while closing browser: {e}")

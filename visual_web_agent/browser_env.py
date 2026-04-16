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
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
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
    from .data_manager import save_intercepted_data
except ImportError:
    import config
    from data_manager import save_intercepted_data

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

async def ensure_active_page(context: BrowserContext, current_page: Page) -> Page:
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

    # ── 层 3：向前跟随 (Follow) ───────────────────────────────
    # open_pages 安全取最后一个（L1 已确保列表非空，此处无越界风险）
    latest = open_pages[-1]
    if latest is not current_page:
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
        self._som_js: str = ""
        self._screenshot_dir: Path = Path(config.SCREENSHOT_DIR)
        self._download_dir: Path = Path("./downloads")
        # XHR 拦截相关
        self._intercept_enabled: bool = False  # 默认关闭，由 configure_interceptor() 或 --xhr 参数开启
        self._intercept_count: int = 0
        self._intercept_unique_key: str | list[str] | None = None
        self._intercept_filename: str = "output.xlsx"
        self._registered_pages: set[int] = set()
        self._background_tasks: set[asyncio.Task] = set()
        self._intercept_min_list_size: int = 5  # 启发式探测阈值
        # ── 核心 API 精准截胡 ──────────────────────────────────────────
        # 通过 --xhr-pattern 指定 URL 关键词，命中后数据写入此变量，
        # main.py 轮询检测，一旦有值立即触发"主引擎生效"分支，跳过 VLM。
        self._intercept_url_pattern: str | None = None  # URL 匹配关键词（子串匹配）
        self._intercepted_data: list | None = None       # 首次命中的数据快照
        self._upload_file: Path | None = None   # --upload-file 预配置路径
        self._last_action_error: Exception | None = None  # 自愈：记录本轮操作异常
        # ── RPA 肌肉记忆：记录本次任务每步成功动作的真实 XPath / 坐标 ──────
        self.rpa_trail: list[dict] = []

    def clear_rpa_trail(self) -> None:
        """清空本次任务的 RPA 动作轨迹，在任务开始前调用。"""
        self.rpa_trail.clear()
        logger.debug("[RPA] Trail cleared")

    async def _get_xpath(self, handle) -> str:
        """
        注入 JS 提取 DOM 元素的绝对 XPath 字符串。

        优先使用 id 属性（短路径），其次递归逐级拼接标签+位置索引。
        失败时静默返回空字符串，不阻断主流程。
        """
        _XPATH_JS = """
        (el) => {
            function getXPath(e) {
                if (!e || e.nodeType !== 1) return '';
                if (e.id) return '//*[@id="' + e.id + '"]';
                if (e === document.body) return '/html/body';
                let ix = 0;
                const siblings = e.parentNode ? e.parentNode.childNodes : [];
                for (let i = 0; i < siblings.length; i++) {
                    const sib = siblings[i];
                    if (sib === e)
                        return getXPath(e.parentNode) + '/' + e.tagName.toLowerCase() + '[' + (ix + 1) + ']';
                    if (sib.nodeType === 1 && sib.tagName === e.tagName) ix++;
                }
                return '';
            }
            return getXPath(el);
        }
        """
        try:
            return await handle.evaluate(_XPATH_JS) or ""
        except Exception as _xe:
            logger.debug(f"[RPA] XPath extraction failed (non-fatal): {_xe}")
            return ""

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
        'dialog, [role="dialog"], [aria-modal="true"], .modal, .popup, .popover, .overlay, .mask, .drawer, section, div'
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

    async def _register_page(
        self, page: Page, reason: str = "", activate: bool = True
    ) -> None:
        if not page or page.is_closed():
            return

        page_key = id(page)
        if page_key not in self._registered_pages:
            self._registered_pages.add(page_key)
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

            # 自动处理 alert/confirm/prompt 弹窗，避免阻塞 Agent
            async def _auto_dismiss_dialog(dialog):
                try:
                    logger.info(
                        f"[DIALOG] Auto-accepting {dialog.type}: "
                        f"{dialog.message[:100] if dialog.message else ''}"
                    )
                    await dialog.accept()
                except Exception as e:
                    logger.debug(f"[DIALOG] Accept failed: {e}")

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

        if not self._context:
            return self._page

        open_pages = [page for page in self._context.pages if not page.is_closed()]
        if not open_pages:
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

        return None

    async def start(self, url: str) -> None:
        """
        启动持久化浏览器上下文并导航到指定 URL。

        使用 launch_persistent_context 保证 Cookie/登录态跨次运行持久化。
        Playwright 会自动创建 user_data_dir（如不存在）。

        Args:
            url: 初始页面 URL
        """
        # 加载 SoM 注入脚本
        self._som_js = config.SOM_SCRIPT_PATH.read_text(encoding="utf-8")
        logger.info(f"SoM script loaded ({len(self._som_js)} chars)")

        # 创建截图和下载目录
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._download_dir.mkdir(parents=True, exist_ok=True)

        # 解析 user_data_dir 路径
        user_data_path = Path(config.BROWSER_USER_DATA_DIR or (Path(__file__).parent / "browser_data"))
        user_data_path.mkdir(parents=True, exist_ok=True)

        # 启动 Playwright + 持久化 Chromium 上下文
        self._playwright = await async_playwright().start()

        # 伪造真实 Windows Chrome UA，避免 Headless 特征泄露
        _STEALTH_UA = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

        logger.info(f"Launching persistent context: {user_data_path.resolve()}")
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_path.resolve()),
            headless=config.HEADLESS,
            viewport={"width": config.VIEWPORT_WIDTH, "height": config.VIEWPORT_HEIGHT},
            user_agent=_STEALTH_UA,
            ignore_https_errors=True,
            args=self._BROWSER_ARGS,
            accept_downloads=True,  # 启用下载接管
        )
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

// 3. 伪造 plugins（真实浏览器通常有 3+ 个）
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3],
    configurable: true,
});

// 4. 伪造语言列表
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en'],
    configurable: true,
});
"""
        await self._context.add_init_script(_STEALTH_INIT_JS)
        logger.info("Stealth init script injected at context level")

        # ── 全局弹窗静默刺客：MutationObserver 自动隐藏常见牛皮癣浮层 ─────────
        # 策略：display:none 而非 remove()，避免触发页面业务 JS 的 DOM 依赖异常。
        # MutationObserver 持续监听，确保动态插入的懒加载弹窗也能被拦截。
        _POPUP_KILLER_JS = """
(function () {
    'use strict';

    // 关键字匹配：id 或 className 中包含这些词的元素将被自动隐藏
    const KILL_KEYWORDS = [
        'cookie-banner', 'cookie_banner', 'cookiebanner', 'cookie-notice',
        'cookie-consent', 'cookie_consent', 'gdpr', 'accept-cookies',
        'app-download', 'app_download', 'appdownload', 'app-banner',
        'download-app', 'open-app', 'openapp',
        'login-modal', 'login_modal', 'loginModal',
        'login-wall', 'signin-modal', 'signup-modal',
        'subscribe-modal', 'subscribe-popup', 'newsletter-popup',
        'ad-float', 'float-ad', 'floatad', 'floating-ad',
        'pop-overlay', 'popoverlay', 'modal-overlay', 'mask-layer',
        'privacy-banner', 'privacy-notice', 'privacy-popup',
        'notification-bar', 'push-notification-bar',
    ];

    // CSS 属性选择器：直接通过 id/class 精确命中
    const KILL_SELECTORS = [
        '#cookie-banner', '#cookieBanner', '#cookie-notice', '#cookieNotice',
        '#app-download-bar', '#appDownloadBar',
        '#gdpr-banner', '#gdprBanner',
        '.cookie-banner', '.cookie-notice', '.cookie-bar',
        '.app-download-float', '.app-banner-float',
        '.login-modal-overlay', '.modal-backdrop',
        '.privacy-overlay', '.subscribe-overlay',
        '[id*="cookie"][id*="banner"]',
        '[class*="cookie"][class*="banner"]',
        '[id*="gdpr"]', '[class*="gdpr"]',
    ].join(', ');

    function matchesKillList(el) {
        try {
            const id = (el.id || '').toLowerCase();
            const cls = (el.className && typeof el.className === 'string')
                ? el.className.toLowerCase() : '';
            return KILL_KEYWORDS.some(kw => id.includes(kw) || cls.includes(kw));
        } catch (_) { return false; }
    }

    function silenceEl(el) {
        try {
            if (el.style) {
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('visibility', 'hidden', 'important');
                el.style.setProperty('opacity', '0', 'important');
            }
        } catch (_) {}
    }

    function scanAndKill() {
        try {
            // 精确选择器命中
            document.querySelectorAll(KILL_SELECTORS).forEach(silenceEl);
            // 关键字遍历（兜底覆盖奇葩命名）
            document.querySelectorAll('div, aside, section, dialog, [class]').forEach(el => {
                if (matchesKillList(el)) silenceEl(el);
            });
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
                if (matchesKillList(node)) { silenceEl(node); continue; }
                // 检查新插入节点内部的子元素
                if (node.querySelectorAll) {
                    try {
                        node.querySelectorAll(KILL_SELECTORS).forEach(silenceEl);
                        node.querySelectorAll('[class]').forEach(el => {
                            if (matchesKillList(el)) silenceEl(el);
                        });
                    } catch (_) {}
                }
            }
        }
    });

    observer.observe(document.documentElement, { childList: true, subtree: true });
})();
"""
        await self._context.add_init_script(_POPUP_KILLER_JS)
        logger.info("Popup killer init script injected at context level")

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

        # ── 状态劫持：注入本地 auth_state.json（Cookie + LocalStorage） ──────
        # launch_persistent_context 不支持 storage_state 参数，
        # 通过 add_cookies + add_init_script 两步等效实现。
        # 文件查找顺序：项目根目录 → visual_web_agent 子目录
        _auth_candidates = [
            Path(__file__).parent.parent / "auth_state.json",
            Path(__file__).parent / "auth_state.json",
        ]
        _auth_path = next((p for p in _auth_candidates if p.exists()), None)
        if _auth_path:
            try:
                _auth_state = json.loads(_auth_path.read_text(encoding="utf-8"))
                # 1. Cookie 注入（立即生效，覆盖已有同名 Cookie）
                _cookies = _auth_state.get("cookies") or []
                if _cookies:
                    await self._context.add_cookies(_cookies)
                    logger.info(
                        f"[AUTH] 检测到 auth_state.json，已挂载 {len(_cookies)} 条 Cookie "
                        f"实现越权登录 ← {_auth_path.name}"
                    )
                # 2. LocalStorage 注入（通过 init_script 在每次导航前写入）
                _origins = _auth_state.get("origins") or []
                _ls_lines: list[str] = []
                for _origin_data in _origins:
                    for _item in (_origin_data.get("localStorage") or []):
                        _k = str(_item.get("name", "")).replace('"', '\\"').replace("\\", "\\\\")
                        _v = str(_item.get("value", "")).replace('"', '\\"').replace("\\", "\\\\")
                        _ls_lines.append(f'try{{localStorage.setItem("{_k}","{_v}");}}catch(_){{}}')
                if _ls_lines:
                    await self._context.add_init_script("\n".join(_ls_lines))
                    logger.info(
                        f"[AUTH] 已注入 {len(_ls_lines)} 条 localStorage 条目"
                    )
            except Exception as _auth_err:
                logger.warning(f"[AUTH] auth_state.json 加载失败（非致命，跳过）: {_auth_err}")
        else:
            logger.debug("[AUTH] 未检测到 auth_state.json，跳过状态注入")

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

        # 导航到初始页面
        logger.info(f"Navigating to: {url}")
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # 首次导航后等待 SPA 完全渲染
        await self._wait_for_page_stable()
        await self._dismiss_permission_surfaces(reason="after initial navigation")
        logger.info("Page loaded and stable")

        # 挂载 XHR/Fetch 响应拦截器
        logger.info("Page-level interceptors will auto-register for new tabs and popups")

        # 挂载全局底层下载拦截器（绕过弹窗）

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

    async def mark_and_screenshot(self, step: int = 0) -> tuple[str, str]:
        """
        注入 SoM 标记脚本并截取全屏截图。
        Returns:
            (screenshot_b64, input_descriptions)
        """
        page = await self._ensure_active_page(reason="before screenshot")
        if not page:
            raise RuntimeError("No active page available for screenshot.")

        logger.info(f"[Step {step}] Waiting for page to stabilize before screenshot...")
        await self._wait_for_page_stable()
        await self._dismiss_permission_surfaces(reason=f"before screenshot step={step}")

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

        for frame in frames_to_eval:
            try:
                result = await frame.evaluate(self._som_js, current_id)
                if result and isinstance(result, dict):
                    injected_frames += 1
                    current_id = result.get('nextId', current_id)
                    element_map = result.get('resultMap', [])
                    total_elements += len(element_map)
                    
                    for el in element_map:
                        desc = (el.get("inputDesc") or "").strip()
                        label = (el.get("text") or "").strip()
                        if desc:
                            if label and label not in desc:
                                desc = f"{desc} | 文本: {label[:80]}"
                        elif label:
                            desc = label[:80]

                        if desc:
                            parent_ctx = el.get("parentContext", "")
                            ctx_suffix = f" | 关联信息: {parent_ctx}" if parent_ctx else ""
                            risk_text = " ".join(part for part in (desc, parent_ctx) if part)
                            risk_suffix = ""
                            if re.search(
                                r"语音|麦克风|microphone|voice|camera|相机|拍照|图片搜索|以图搜图|扫码|扫一扫|lens",
                                risk_text,
                                flags=re.IGNORECASE,
                            ):
                                risk_suffix = (
                                    " | 风险提示: 语音/拍照/扫码等辅助入口，"
                                    "除非目标明确要求，否则不要优先点击"
                                )
                            input_descriptions.append(
                                f"[ID: {el['id']}] {el['tag'].upper()} -> {desc}{ctx_suffix}{risk_suffix}"
                            )
            except Exception as eval_err:
                logger.warning(f"Frame evaluation failed: {eval_err}")

        logger.info(
            f"[Step {step}] SoM injected across {injected_frames} frames, "
            f"marked {total_elements} interactive elements"
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
                logger.info("[Screenshot] 执行 window.stop() 强制停止挂起资源，重新截图...")
                try:
                    await page.evaluate("window.stop()")
                    await asyncio.sleep(1)
                    screenshot_bytes = await page.screenshot(**_SS_KWARGS)
                except Exception as _ss_err2:
                    raise RuntimeError(
                        f"[Screenshot] 致命错误：强制截图依然失败，页面可能已崩溃: {_ss_err2}"
                    )

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
        DOM 只读序列化（图文双模态的文本侧）。

        前置条件：调用前 `mark_and_screenshot` 必须已执行 —— 它通过 som_inject_v5.js
        为页面上所有可见可交互元素写入 `data-som-id`（红框数字 ↔ 属性值 ↔ DOM 行 ID
        三方对齐的单一事实源）。本方法只读取、不新增、不擦除这些 ID。

        Returns:
            多行字符串。每一行是 `[ID: N] <tag ...>text</tag>`，N 与截图红框数字完全一致。
            末尾追加少量 `[TEXT] ...` 行（非交互语义文本，如 h1/h2/p），帮助 VLM 理解页面语境。
            若当前页面尚未注入 SoM ID，返回空串。
        """
        page = await self._ensure_active_page(reason="before extract_text_dom")
        if not page:
            raise RuntimeError("No active page available for DOM extraction.")

        await self._wait_for_page_stable()
        await self._dismiss_permission_surfaces(reason="before text dom extraction")

        # ── 只读序列化：复用 som_inject_v5.js 已写入的 data-som-id 作为单一事实源 ──
        # 关键设计：此处不再另起 SELECTOR 重新扫描页面，也绝不擦除 data-som-id。
        # 原因：screenshot 上的红框编号由 SoM v5 决定；若这里再扫一遍并重编号，
        # 会导致截图红框 "12" 与 DOM 输出 "[ID: 12]" 指向不同元素 → VLM 误点。
        # 现在流程：mark_and_screenshot 已完成编号 → extract_text_dom 只读取 + 序列化。
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
                logger.debug(f"[TEXT DOM] Frame evaluation skipped: {e}")
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
                "[TEXT DOM] No SoM-tagged interactive elements found; "
                "mark_and_screenshot 可能未成功注入 data-som-id"
            )
            return ""

        dom_text = "\n".join(elements)
        logger.info(
            f"[TEXT DOM] Serialized {len(elements)} lines (SoM marked={marked_total}) "
            f"across {frame_hits} frames — read-only, shares IDs with screenshot"
        )
        return dom_text

    # 元素定位超时：改短以 fail fast，页面已刷新时不再苦等
    _LOCATOR_TIMEOUT = 3000

    async def execute_action(
        self,
        action_dict: dict,
        workflow_memory: dict | None = None,
    ) -> Page | None:
        """
        根据 VLM 返回的决策字典执行对应的浏览器操作。

        Args:
            action_dict: VLM 的决策字典，包含 action/target_id/type_value/memory_key
            workflow_memory: 跨页面记忆库（可变 dict），由主循环传入；
                             save_to_memory 动作会直接写入此 dict；
                             type 动作会读取此 dict 做 {{key}} 插值。

        Returns:
            执行动作后处于激活状态的 Page 对象（供调用方更新 page 引用）。
            done / unknown action 时返回当前页（无切换）。
        """
        # ── 每轮开始前清空上轮残留错误标记（防跨轮污染）──────────────────────
        self._last_action_error = None

        page = await self._ensure_active_page(reason="before execute_action")
        if not page:
            logger.error("No active page available for action execution")
            return
        action = action_dict.get("action", "")
        target_id = action_dict.get("target_id", 0)
        type_value = action_dict.get("type_value", "")
        memory_key = action_dict.get("memory_key") or ""
        rpa_required_keys = sorted(set(action_dict.get("__rpa_required_keys") or []))
        rpa_template_value = str(action_dict.get("__rpa_template_value") or "")

        def _with_rpa_meta(step: dict) -> dict:
            if rpa_required_keys:
                step["required_memory_keys"] = list(rpa_required_keys)
            if action == "type" and rpa_template_value:
                step["type_value_template"] = rpa_template_value
            if action == "goto" and rpa_template_value:
                step["url_template"] = rpa_template_value
            return step

        if action == "click":
            selector = f'[data-som-id="{target_id}"]'
            logger.info(f"Executing click: element #{target_id} ({selector})")
            try:
                await self._clear_som_overlays()
                target = await self._resolve_action_target(target_id, "click")
                if target:
                    await target.handle.scroll_into_view_if_needed(
                        timeout=self._LOCATOR_TIMEOUT
                    )
                else:
                    raise RuntimeError(
                        f"Element #{target_id} not found on active page or its iframes"
                    )
                # ── RPA 取证（先于点击）：点击可能触发页面跳转，上下文会随之销毁 ──
                # 必须在 click() 之前提取 XPath，否则跳转后 evaluate 会抛
                # "Execution context was destroyed" 错误
                _pending_xpath = await self._get_xpath(target.handle)

                # 直接点击（全局 _handle_download 已兜底接管所有下载事件，无需包裹 expect_download）
                try:
                    await target.handle.click(
                        force=True, timeout=self._LOCATOR_TIMEOUT
                    )
                except Exception:
                    # Playwright 原生 click 失败时，使用 JS 兜底
                    await target.handle.evaluate(
                        """el => {
                            el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
                            if (typeof el.click === 'function') {
                                el.click();
                            } else {
                                el.dispatchEvent(new MouseEvent('click', {
                                    bubbles: true,
                                    cancelable: true,
                                    composed: true,
                                    view: window
                                }));
                            }
                        }"""
                    )
                # ── RPA 录制：click 成功，将取证阶段缓存的 XPath 追加到轨迹 ──
                if _pending_xpath:
                    self.rpa_trail.append(
                        _with_rpa_meta({"action": "click", "xpath": _pending_xpath, "type_value": ""})
                    )
                    logger.debug(f"[RPA] Recorded click: {_pending_xpath}")
                logger.info(f"Click element #{target_id} succeeded")
            except Exception as e:
                # fail fast：元素可能已因页面刷新而消失
                self._last_action_error = e
                logger.error(f"Click element #{target_id} failed (page may have refreshed): {e}")

            # 点击是最可能引发页面跳转的操作，必须充分等待
            await self._wait_after_action(is_navigation=True)

        elif action == "type":
            selector = f'[data-som-id="{target_id}"]'
            logger.info(f"Executing type: element #{target_id} <- {type_value!r}")
            try:
                await self._clear_som_overlays()
                target = await self._resolve_action_target(target_id, "type")

                if not target:
                    raise RuntimeError(
                        f"Element #{target_id} not found on active page or its iframes"
                    )

                await target.handle.scroll_into_view_if_needed(timeout=self._LOCATOR_TIMEOUT)

                # ── RPA 取证（先于输入）：在键盘操作前提取 XPath ────────────
                # type 动作本身不会导致上下文销毁，但保持与 click 一致的"先取证"
                # 原则，防止极端情况下 onInput 回调触发跳转时丢失上下文
                _pending_xpath = await self._get_xpath(target.handle)

                # ★ 记录输入前交互元素数量，用于异步下拉框检测
                count_before = await self._count_interactive_elements()

                # 1. 强制唤醒光标：点击视觉表层元素，依赖事件冒泡自动 focus 到真实输入框
                try:
                    await target.handle.click(force=True, timeout=self._LOCATOR_TIMEOUT)
                except Exception:
                    await target.handle.evaluate("""el => {
                        if (typeof el.click === 'function') el.click();
                        else el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                    }""")

                # 2. 缓冲时间：等待 JS 动画完成（如滑出输入面板）
                await asyncio.sleep(0.5)

                # ★ 动态插值：将 {{key}} 替换为 workflow_memory 中的真实值
                # 例如 type_value="{{order_id}}" → "ORD-2024-001"
                if workflow_memory and "{{" in type_value:
                    def _interpolate(m: re.Match) -> str:
                        key = m.group(1).strip()
                        val = workflow_memory.get(key)
                        if val is None:
                            logger.warning(
                                f"[MEMORY] interpolation: key '{key}' not found in workflow_memory "
                                f"(available: {list(workflow_memory.keys())})"
                            )
                            return m.group(0)  # 保留原始占位符
                        return str(val)
                    resolved = re.sub(r"\{\{([^}]+)\}\}", _interpolate, type_value)
                    if resolved != type_value:
                        logger.info(
                            f"[MEMORY] type interpolation: {type_value!r} → {resolved!r}"
                        )
                    type_value = resolved

                # 3. 键盘直输
                modifier = "Meta" if sys.platform == "darwin" else "Control"
                await page.keyboard.press(f"{modifier}+a")
                await page.keyboard.press("Backspace")
                await page.keyboard.type(type_value, delay=50)

                # ── RPA 录制：type 成功，将取证阶段缓存的 XPath 追加到轨迹 ──
                if _pending_xpath:
                    self.rpa_trail.append(
                        _with_rpa_meta({"action": "type", "xpath": _pending_xpath, "type_value": type_value})
                    )
                    logger.debug(f"[RPA] Recorded type: {_pending_xpath} <- {type_value!r}")
                logger.info(f"Type into element #{target_id} succeeded")

                # ══════════════════════════════════════════════════════
                # ★ Boss 1 & 2：智能输入后处理
                # 短暂等待，让防抖计时器触发 / 日历弹窗渲染
                await asyncio.sleep(0.4)

                # Boss 1：日期选择器 — 检测日历弹窗并强制按 Tab 收起
                # 绝大多数日期组件（Ant Design/Element UI）在收到 Tab 或 Enter 后会收起日历
                if await self._is_calendar_popup_visible():
                    await page.keyboard.press("Tab")
                    logger.info(
                        f"[TYPE] Date picker calendar detected → dismissed with Tab. "
                        f"Next screenshot will show clean input state."
                    )
                else:
                    # Boss 2：异步搜索下拉框 — 等待动态下拉列表出现（最多 2.5 秒）
                    appeared = await self._wait_for_submenu(count_before, max_wait=2.5)
                    if appeared:
                        logger.info(
                            f"[TYPE] Async dropdown items appeared after input. "
                            f"Next screenshot will capture the option list for VLM to click."
                        )
                # ══════════════════════════════════════════════════════

            except Exception as e:
                self._last_action_error = e
                logger.error(f"Type into element #{target_id} failed: {e}")

            # type 不引发页面跳转，跳过 networkidle 避免自动补全 XHR 卡顿
            await self._wait_after_action(light_action=True)

        elif action == "hover":
            selector = f'[data-som-id="{target_id}"]'
            logger.info(f"Executing hover: element #{target_id} ({selector})")
            try:
                await self._clear_som_overlays()
                target = await self._resolve_action_target(target_id, "click")
                if not target:
                    raise RuntimeError(
                        f"Element #{target_id} not found on active page or its iframes"
                    )
                await target.handle.scroll_into_view_if_needed(timeout=self._LOCATOR_TIMEOUT)

                # ★ 级联菜单支持：记录悬停前的可交互元素数量
                count_before = await self._count_interactive_elements()

                await target.handle.hover(force=True, timeout=self._LOCATOR_TIMEOUT)
                logger.info(f"Hover element #{target_id} succeeded")

                # ★ 等待子菜单弹出（最多 2 秒），检测是否有新元素出现
                appeared = await self._wait_for_submenu(count_before, max_wait=2.0)
                if appeared:
                    logger.info(f"[HOVER] Submenu/dropdown appeared after hovering #{target_id}")
                else:
                    logger.warning(
                        f"[HOVER TIMEOUT] No new elements appeared after hovering #{target_id} "
                        f"within 2s — parent item may be incorrect or menu requires a click to open."
                    )
            except Exception as e:
                self._last_action_error = e
                logger.error(f"Hover element #{target_id} failed: {e}")

            await self._wait_after_action(light_action=True)

        elif action == "scroll":
            # ── 智能滚动：支持 down/up/bottom/top + 无效滚动边界检测 ──────────
            # type_value 指定方向（新协议）；兜底兼容旧协议 target_id 正负号
            raw_dir = (type_value or "").strip().lower()
            if raw_dir in ("down", "up", "bottom", "top"):
                direction = raw_dir
            elif target_id < 0:
                direction = "up"
            else:
                direction = "down"

            # 滚动前记录位置
            prev_y = await page.evaluate("window.scrollY")

            if direction == "bottom":
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif direction == "top":
                await page.evaluate("window.scrollTo(0, 0)")
            elif direction == "up":
                await page.evaluate("window.scrollBy(0, -700)")
            else:  # down
                await page.evaluate("window.scrollBy(0, 700)")

            # 给页面渲染和懒加载响应时间
            await asyncio.sleep(1.0)

            # 滚动后记录位置
            new_y = await page.evaluate("window.scrollY")
            logger.info(f"[SCROLL] direction={direction} scrollY: {prev_y} → {new_y}")
            print(f"🔄 [SCROLL] 执行方向: {direction}, 位置变化: {prev_y} → {new_y}")

            # 边界检测：位置未变 → 已到边缘，抛异常接入自愈系统
            if prev_y == new_y:
                raise ActionExecutionError(
                    f"执行 scroll ({direction}) 无效：页面未发生滚动，"
                    f"可能已到达页面边缘或该方向无滚动条。"
                    f"请观察当前截图，不要再继续向该方向滚动。"
                )

        elif action == "upload":
            logger.info(f"Executing upload: element #{target_id} <- {type_value!r}")
            # ★ 文件路径解析优先级：
            #   1. type_value 是真实存在的路径（VLM 从 goal 中提取）
            #   2. --upload-file 预配置路径
            file_path: Path | None = None
            if type_value and type_value.strip():
                candidate = Path(type_value.strip())
                if candidate.exists():
                    file_path = candidate
                    logger.info(f"[UPLOAD] Using path from type_value: {file_path}")
            if file_path is None and self._upload_file and self._upload_file.exists():
                file_path = self._upload_file
                logger.info(f"[UPLOAD] Using pre-configured --upload-file: {file_path}")
            if file_path is None:
                logger.error(
                    f"[UPLOAD] No valid file found. "
                    f"Provide a real path via --upload-file or in type_value. Got: {type_value!r}"
                )
                return
            try:
                await self._clear_som_overlays()
                # ★ 不点击"上传"按钮（会弹出系统文件选择器），
                #   直接定位 <input type="file"> 并通过 set_input_files 静默注入
                file_target = await self._find_file_input(target_id)
                if not file_target:
                    raise RuntimeError(
                        f"No <input type='file'> found near element #{target_id}"
                    )
                await file_target.handle.set_input_files(str(file_path.resolve()))
                logger.info(f"[UPLOAD] File injected successfully: {file_path.resolve()}")
                print(f"\n\033[1;32m✅ 文件上传成功:\033[0m \033[36m{file_path.resolve()}\033[0m\n")
            except Exception as e:
                self._last_action_error = e
                logger.error(f"[UPLOAD] Failed for element #{target_id}: {e}")

            await self._wait_after_action(light_action=True)

        elif action == "extract_link":
            selector = f'[data-som-id="{target_id}"]'
            logger.info(f"Executing extract_link: element #{target_id} ({selector})")
            try:
                # 获取在 som_inject.js 中注入的 URL 属性
                target = await self._resolve_action_target(target_id, "click")
                if not target:
                    raise RuntimeError(
                        f"Element #{target_id} not found on active page or its iframes"
                    )
                extracted_url = await target.handle.evaluate(
                    """el => el.getAttribute('data-som-url') || el.getAttribute('href') || el.getAttribute('src') || ''"""
                )
                if extracted_url:
                    logger.info(f"[EXTRACT_LINK] Successfully extracted URL: {extracted_url}")
                    # 打印高亮日志
                    print(f"\n\033[1;32m[LINK EXTRACTED]\033[0m \033[36m{extracted_url}\033[0m\n")
                    # 保存到独立文件
                    from data_manager import save_to_excel
                    save_to_excel({"target_id": target_id, "url": extracted_url}, "output_links.xlsx")
                else:
                    logger.warning(f"[EXTRACT_LINK] No URL found in data-som-url for element #{target_id}")
            except Exception as e:
                self._last_action_error = e
                logger.error(f"Extract link for element #{target_id} failed: {e}")

        elif action == "download_image":
            selector = f'[data-som-id="{target_id}"]'
            logger.info(f"Executing download_image: element #{target_id} ({selector})")
            try:
                # 获取在 som_inject.js 中注入的 URL 属性
                target = await self._resolve_action_target(target_id, "click")
                if not target:
                    raise RuntimeError(
                        f"Element #{target_id} not found on active page or its iframes"
                    )
                extracted_url = await target.handle.evaluate(
                    """el => el.getAttribute('data-som-url') || el.getAttribute('src') || el.getAttribute('href') || ''"""
                )
                # 如果没有，尝试直接取 src 或 href
                
                if extracted_url:
                    # 将相对路径自动处理为绝对路径
                    import urllib.parse
                    import time
                    abs_url = urllib.parse.urljoin(page.url, extracted_url)
                    
                    logger.info(f"[DOWNLOAD_IMAGE] Target URL: {abs_url}")
                    
                    # 使用当前上下文环境发起 GET 请求，自动继承 Cookie 与登入态
                    response = await self._context.request.get(abs_url)
                    img_bytes = await response.body()
                    
                    # 创建 images 目录
                    img_dir = self._download_dir / "images"
                    img_dir.mkdir(parents=True, exist_ok=True)
                    
                    # 确定文件后缀
                    content_type = response.headers.get("content-type", "")
                    ext = ".png"
                    if "jpeg" in content_type or "jpg" in content_type:
                        ext = ".jpg"
                    elif "gif" in content_type:
                        ext = ".gif"
                    
                    # 写盘
                    filename = f"img_{int(time.time())}{ext}"
                    filepath = img_dir / filename
                    filepath.write_bytes(img_bytes)
                    
                    print(f"\n\033[1;32m✅ 成功下载图片:\033[0m \033[36m{filepath.resolve()}\033[0m\n")
                    logger.info(f"[DOWNLOAD_IMAGE] Saved local: {filepath.resolve()}")
                else:
                    logger.warning(f"[DOWNLOAD_IMAGE] No URL found for element #{target_id}")
            except Exception as e:
                self._last_action_error = e
                logger.error(f"Download image for element #{target_id} failed: {e}")

        elif action == "select":
            selector = f'[data-som-id="{target_id}"]'
            logger.info(f"Executing select: element #{target_id} ({selector}) <- {type_value!r}")
            try:
                await self._clear_som_overlays()
                target = await self._resolve_action_target(target_id, "click")
                if not target:
                    raise RuntimeError(
                        f"Element #{target_id} not found on active page or its iframes"
                    )
                # 尝试通过 label 文本选择
                try:
                    await target.handle.select_option(label=type_value, timeout=self._LOCATOR_TIMEOUT)
                except Exception:
                    # label 匹配失败，尝试通过 value 匹配
                    try:
                        await target.handle.select_option(value=type_value, timeout=self._LOCATOR_TIMEOUT)
                    except Exception:
                        # 最后尝试通过索引匹配
                        await target.handle.select_option(index=0, timeout=self._LOCATOR_TIMEOUT)
                logger.info(f"Select element #{target_id} succeeded")
            except Exception as e:
                self._last_action_error = e
                logger.error(f"Select element #{target_id} failed: {e}")
            await self._wait_after_action(is_navigation=False)

        elif action == "smooth_scroll":
            # ── 平滑滚动：behavior:'smooth' 模拟人类滚轮，更易触发懒加载 ────────
            # type_value 指定方向：down（默认）/ up
            direction = (type_value or "down").strip().lower()
            if direction == "up":
                js_scroll = "window.scrollBy({top: -window.innerHeight * 0.8, behavior: 'smooth'});"
            else:
                js_scroll = "window.scrollBy({top: window.innerHeight * 0.8, behavior: 'smooth'});"
            try:
                prev_y = await page.evaluate("window.scrollY")
                await page.evaluate(js_scroll)
                # smooth 动画约 400-600ms，再等懒加载回填内容
                await asyncio.sleep(1.0)
                new_y = await page.evaluate("window.scrollY")
                logger.info(f"[SMOOTH_SCROLL] direction={direction} scrollY: {prev_y} → {new_y}")
                print(f"🌊 [SMOOTH_SCROLL] direction={direction}, 位置变化: {prev_y} → {new_y}")
                # RPA 录制
                self.rpa_trail.append(
                    _with_rpa_meta({"action": "smooth_scroll", "type_value": direction})
                )
            except Exception as e:
                self._last_action_error = e
                logger.error(f"[SMOOTH_SCROLL] Failed: {e}")

        elif action == "remove_element":
            # ── 物理铲除 DOM 节点：用于清除广告遮罩、浮层等阻挡元素 ─────────────
            selector = f'[data-som-id="{target_id}"]'
            logger.info(f"[REMOVE_ELEMENT] Removing element #{target_id} ({selector})")
            try:
                await self._clear_som_overlays()
                target = await self._resolve_action_target(target_id, "remove_element")
                if not target:
                    raise RuntimeError(
                        f"Element #{target_id} not found — cannot remove"
                    )
                # 先取证 XPath（删除后节点消失，无法再 evaluate）
                _pending_xpath = await self._get_xpath(target.handle)
                # 从 DOM 树中物理删除该节点
                await target.handle.evaluate("el => el.remove()")
                logger.info(f"[REMOVE_ELEMENT] Element #{target_id} removed from DOM")
                print(f"🗑️  [REMOVE_ELEMENT] 已铲除元素 #{target_id} (xpath={_pending_xpath})")
                # RPA 录制（xpath 用于回放时定位，若已删则跳过）
                if _pending_xpath:
                    self.rpa_trail.append(
                        _with_rpa_meta(
                            {"action": "remove_element", "xpath": _pending_xpath, "type_value": ""}
                        )
                    )
            except Exception as e:
                self._last_action_error = e
                logger.error(f"[REMOVE_ELEMENT] Failed for element #{target_id}: {e}")

        elif action == "wait":
            # ── 显式等待：让 VLM 主动控制等待时间（如长动画、慢加载） ────────────
            # type_value 填秒数字符串；默认 2 秒，上限 10 秒（防止 VLM 胡填大数卡死）
            try:
                wait_secs = max(1, min(10, int(float(type_value or "2"))))
            except (ValueError, TypeError):
                wait_secs = 2
            logger.info(f"[WAIT] Explicit wait: {wait_secs}s (requested: {type_value!r})")
            print(f"⏳ [WAIT] 显式等待 {wait_secs} 秒...")
            await asyncio.sleep(wait_secs)
            # RPA 录制：保留等待节奏，确保回放时动画/加载时序一致
            self.rpa_trail.append(
                _with_rpa_meta({"action": "wait", "type_value": str(wait_secs)})
            )

        elif action == "press_key":
            key_name = (type_value or "").strip()
            if not key_name:
                raise ActionExecutionError(
                    "press_key 动作必须在 type_value 中提供按键名称（如 'Enter'、'Escape'、'Tab'）。"
                )
            logger.info(f"Executing press_key: {key_name!r}")
            try:
                await page.keyboard.press(key_name)
                self.rpa_trail.append(
                    _with_rpa_meta({"action": "press_key", "type_value": key_name})
                )
                logger.debug(f"[RPA] Recorded press_key: {key_name!r}")
                logger.info(f"Press key {key_name!r} succeeded")
            except Exception as e:
                self._last_action_error = e
                logger.error(f"Press key {key_name!r} failed: {e}")
            # Enter 通常触发表单提交/页面跳转，保留 networkidle 等待；其余按键轻量等待
            await self._wait_after_action(is_navigation=(key_name == "Enter"))

        elif action == "goto":
            url = type_value
            if not url:
                raise ActionExecutionError("goto 动作缺少目标 URL，请在 type_value 中填写完整的 URL。")

            # 🛡️ 多标签页 goto 智能拦截器
            # 比对目标 URL 与所有已打开标签页，防止"原位覆盖"破坏空间结构
            url_clean = url.split("?")[0].rstrip("/")
            open_pages = [p for p in self._context.pages if not p.is_closed()]

            # 找目标 URL 已在哪个标签页
            target_idx = -1
            for idx, p in enumerate(open_pages):
                if p.url.split("?")[0].rstrip("/") == url_clean:
                    target_idx = idx
                    break

            # 找当前页在列表中的位置
            try:
                current_idx = open_pages.index(page)
            except ValueError:
                current_idx = -1

            _goto_recorded = False

            if target_idx != -1 and target_idx != current_idx:
                # 情况 1：目标页已在后台 → 拦截，强制转为 switch_tab
                self._page = open_pages[target_idx]
                await self._page.bring_to_front()
                await asyncio.sleep(0.5)
                _goto_recorded = True
                logger.info(
                    f"[GOTO INTERCEPTOR] 目标已存在于 Tab [{target_idx}]，"
                    f"已强制转换为 switch_tab，避免原位覆盖: {url[:80]}"
                )
            elif target_idx != -1 and target_idx == current_idx:
                # 情况 2：目标就是当前页 → 静默忽略，无需重复跳转
                logger.info(
                    f"[GOTO INTERCEPTOR] 当前已在目标页面，忽略 goto 请求: {url[:80]}"
                )
            else:
                # 情况 3：目标不存在于任何标签页 → 正常原位跳转
                logger.info(f"Executing goto: {url}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await self._wait_for_page_stable()
                    _goto_recorded = True
                    logger.info(f"Navigation to {url} succeeded")
                except Exception as e:
                    self._last_action_error = e
                    logger.error(f"Navigation to {url} failed: {e}")

            if _goto_recorded:
                self.rpa_trail.append(_with_rpa_meta({"action": "goto", "url": url}))
                logger.debug(f"[RPA] Recorded goto: {url}")

        elif action == "close_tab":
            # 关闭当前标签页，并主动将焦点切回剩余最后一个页面
            logger.info("[CLOSE TAB] Closing current page and falling back to previous tab")
            try:
                await page.close()
                self.rpa_trail.append(_with_rpa_meta({"action": "close_tab", "type_value": ""}))
                logger.debug("[RPA] Recorded close_tab")
                logger.info("[CLOSE TAB] Page closed successfully")
            except Exception as e:
                self._last_action_error = e
                logger.error(f"[CLOSE TAB] Failed to close page: {e}")
            # 主动更新 self._page，无需等待 Tab Guard L2 被动触发
            open_pages = [p for p in self._context.pages if not p.is_closed()]
            if open_pages:
                self._page = open_pages[-1]
                await self._page.bring_to_front()
                await asyncio.sleep(0.8)
                logger.info(
                    f"[CLOSE TAB] Focused back to: {(self._page.url or 'about:blank')[:80]}"
                )

        elif action == "click_point":
            # ── 无选择器坐标点击：千分制归一化坐标 → 真实像素 ──────────────
            # VLM 输出 0-1000 的归一化坐标（与视口无关），
            # 此处动态读取当前视口尺寸并换算为真实像素坐标后驱动鼠标点击。
            point = action_dict.get("point")
            if (
                not point
                or not isinstance(point, (list, tuple))
                or len(point) != 2
            ):
                raise ActionExecutionError(
                    "click_point 动作必须提供有效的 [x, y] 千分制归一化坐标，"
                    f"收到的 point 值为：{point!r}。"
                    "请在截图中目视估算目标元素位置，以 0-1000 范围输出坐标后重新提交。"
                )

            # 动态获取当前视口尺寸，支持非标准分辨率和响应式布局
            viewport = page.viewport_size
            if not viewport:
                raise ActionExecutionError(
                    "无法获取当前页面的视口尺寸，请检查页面状态。"
                )
            vw, vh = viewport["width"], viewport["height"]

            # 千分制坐标 (0-1000) → 真实像素坐标
            x_norm, y_norm = float(point[0]), float(point[1])
            real_x = int((x_norm / 1000.0) * vw)
            real_y = int((y_norm / 1000.0) * vh)

            logger.info(
                f"[CLICK_POINT] 归一化坐标 {point} -> 真实像素 ({real_x}, {real_y})"
                f" (视口 {vw}x{vh})"
            )
            print(
                f"\033[1;35m🎯 [物理干预]\033[0m "
                f"VLM 归一化坐标 {point} → 真实屏幕坐标 ({real_x}, {real_y})"
                f"  [视口 {vw}×{vh}]"
            )
            await page.mouse.move(real_x, real_y)
            await page.mouse.click(real_x, real_y)
            # ── RPA 录制：坐标点击直接记录归一化坐标（回放时按视口比例还原）──
            self.rpa_trail.append(
                _with_rpa_meta({"action": "click_point", "x_norm": point[0], "y_norm": point[1]})
            )
            logger.debug(f"[RPA] Recorded click_point: norm=({point[0]}, {point[1]}) real=({real_x}, {real_y})")
            await self._wait_after_action()

        elif action == "switch_tab":
            # 按索引切换到指定标签页（索引由 VLM 从 tabs_state 摘要中读取）
            tab_index = action_dict.get("target_id", 0)
            open_pages = [p for p in self._context.pages if not p.is_closed()]
            if 0 <= tab_index < len(open_pages):
                self._page = open_pages[tab_index]
                await self._page.bring_to_front()
                await asyncio.sleep(0.5)
                self.rpa_trail.append(
                    _with_rpa_meta({"action": "switch_tab", "target_id": tab_index})
                )
                logger.debug(f"[RPA] Recorded switch_tab: {tab_index}")
                logger.info(
                    f"[SWITCH TAB] Switched to tab [{tab_index}]: "
                    f"{(self._page.url or 'about:blank')[:80]}"
                )
            else:
                raise ActionExecutionError(
                    f"switch_tab 失败：标签页索引 {tab_index} 超出范围"
                    f"（当前共 {len(open_pages)} 个标签页，有效索引 0~{len(open_pages)-1}）。"
                )

        elif action == "save_to_memory":
            # ── 跨页面记忆库：双源提取 + 动态 key 兜底 + latest_memory 双写 ──
            #
            # VLM 的两种常见失误：
            #   A. 忘了填 memory_key → 动态生成 temp_var_<timestamp>，不静默丢弃
            #   B. 把值直接写在 type_value 而非让底层从 DOM 读 → 也能接收

            # ── 1. memory_key 动态兜底：不再使用固定硬编码名称 ──────────────
            effective_key = memory_key  # 已在函数顶部从 action_dict 提取
            if not effective_key:
                effective_key = f"temp_var_{int(time.time())}"
                logger.warning(
                    f"[MEMORY] save_to_memory: memory_key missing, "
                    f"auto-generated key='{effective_key}'"
                )
                print(
                    f"\033[1;33m⚠️  [MEMORY]\033[0m "
                    f"VLM 未提供 memory_key，已动态生成临时命名为 '{effective_key}'"
                )

            logger.info(
                f"Executing save_to_memory: element #{target_id} → key={effective_key!r}"
            )

            # ── 2. 双源提取策略 ───────────────────────────────────────────────
            #   优先级 1：type_value 非空 → VLM 直接把值写在这里了
            #   优先级 2：target_id 有效 → 从 DOM 元素提取 innerText / value
            #   两者都无 → 抛出 RuntimeError，触发自愈反馈
            try:
                extracted_text: str = ""

                if type_value and type_value.strip():
                    # VLM 把值直接填在 type_value（常见于纯文本展示、无红框 ID 场景）
                    extracted_text = type_value.strip()
                    logger.info(
                        f"[MEMORY] Value sourced from type_value: {extracted_text!r}"
                    )
                    print(
                        f"\033[36m🧠 [MEMORY]\033[0m "
                        f"VLM 直接传递了文本值: {extracted_text!r}"
                    )

                elif target_id and target_id != 0:
                    # 从 DOM 元素提取可见文字或输入框当前值
                    await self._clear_som_overlays()
                    target = await self._resolve_action_target(target_id, "click")
                    if not target:
                        raise RuntimeError(
                            f"Element #{target_id} not found on active page or its iframes"
                        )
                    extracted_text = str(
                        await target.handle.evaluate(
                            """el => {
                                const text = (el.innerText || el.textContent || '').trim();
                                const val  = (el.value || '').trim();
                                return text || val || '';
                            }"""
                        )
                    ).strip()
                    logger.info(
                        f"[MEMORY] Value sourced from DOM element #{target_id}: "
                        f"{extracted_text!r}"
                    )
                    print(
                        f"\033[36m🔍 [MEMORY]\033[0m "
                        f"从页面元素提取了文本: {extracted_text!r}"
                    )

                else:
                    raise RuntimeError(
                        "save_to_memory 失败：VLM 既未提供 type_value，"
                        "也未提供有效的 target_id，无法获取要保存的值"
                    )

                # ── 3. 双写记忆库：指定 key + latest_memory 快捷键 ──────────
                if not extracted_text:
                    logger.warning(
                        f"[MEMORY] save_to_memory: extracted value is empty "
                        f"(element #{target_id})"
                    )
                else:
                    if workflow_memory is not None:
                        workflow_memory[effective_key] = extracted_text
                        # 同步更新 latest_memory，供 VLM 忘记变量名时兜底引用
                        workflow_memory["latest_memory"] = extracted_text
                    logger.info(
                        f"[MEMORY] Saved: {effective_key!r} = {extracted_text!r} "
                        f"(latest_memory also updated)"
                    )
                    print(
                        f"\n\033[1;36m[MEMORY SAVED]\033[0m "
                        f"\033[33m{effective_key}\033[0m = "
                        f"\033[32m{extracted_text!r}\033[0m  "
                        f"\033[90m(latest_memory 已同步)\033[0m\n"
                    )

            except Exception as e:
                self._last_action_error = e
                logger.error(f"[MEMORY] save_to_memory failed: {e}")

            # save_to_memory 不触发导航，跳过 networkidle
            await self._wait_after_action(light_action=True)

        elif action == "done":
            logger.info("Task marked as done, no action executed")
            return page  # done 不触发页面切换，直接返回当前页

        else:
            logger.warning(f"Unknown action type: {action}")
            return page  # 未知动作同上

        # ══════════════════════════════════════════════════════
        # 焦点守护（Tab Guard）
        # 统一接管所有动作引发的标签页状态变化：
        #   - 新 tab 弹出（Follow）
        #   - 当前页关闭（Fallback）
        #   - 全部页面消失（Emergency）
        # ══════════════════════════════════════════════════════
        active_page = await ensure_active_page(self._context, page)

        if active_page is not page:
            # 新页面尚未挂载事件处理器（XHR 拦截、下载监听等），补充注册
            await self._register_page(
                active_page,
                reason=f"tab guard: switched from {(page.url or 'closed')[:60]}",
                activate=True,
            )
        # 更新实例持有的活跃页引用，确保下一轮截图使用正确页面
        self._page = active_page

        # ── 自愈抛出：若本轮操作有异常，向上层暴露以触发 Self-Healing ────────
        # 先将错误暂存到局部变量并清空实例状态，避免下轮误判
        if self._last_action_error is not None:
            _err = self._last_action_error
            self._last_action_error = None
            raise ActionExecutionError(
                f"action={action} target_id={action_dict.get('target_id', 0)}: {_err}"
            ) from _err

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
            
            print(f"\n✅ [底层拦截] 成功拦截文件下载并静默保存至: \033[36m{final_path.resolve()}\033[0m\n")
            logger.info(f"[DOWNLOAD INTERCEPT] Saved native file: {final_path.resolve()}")
        except Exception as e:
            logger.error(f"[DOWNLOAD INTERCEPT] Failed to save file: {e}")

    # ========== XHR/Fetch 网络拦截 ==========

    def configure_interceptor(
        self,
        enabled: bool = True,
        filename: str = "output.xlsx",
        unique_key: str | list[str] = None,
        min_list_size: int = 5,
        url_pattern: str | None = None,
    ) -> None:
        """
        配置 XHR/Fetch 拦截器参数。

        Args:
            enabled:     是否启用通用拦截（--xhr 参数控制）
            filename:    通用拦截数据保存的 Excel 文件名
            unique_key:  去重字段（如 "id"）
            min_list_size: 启发式探测阈值——JSON 中列表长度 >= 此值才认为是数据
            url_pattern: 精准截胡关键词（子串匹配）。
                         匹配到此关键词的响应 URL 会触发"主引擎"分支：
                         数据存入 self._intercepted_data，main.py 检测后直接保存跳过 VLM。
                         独立于 enabled，即使 enabled=False 也可单独启用精准截胡。
        """
        self._intercept_enabled = enabled
        self._intercept_filename = filename
        self._intercept_unique_key = unique_key
        self._intercept_min_list_size = min_list_size
        self._intercept_url_pattern = url_pattern.strip() if url_pattern else None
        # 每次重新配置时清空上次缓存，防止旧数据触发误判
        self._intercepted_data = None
        logger.info(
            f"Interceptor configured | enabled={enabled} | "
            f"file={filename} | unique_key={unique_key} | "
            f"min_list={min_list_size} | url_pattern={self._intercept_url_pattern!r}"
        )

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
        if not pattern_active and not general_active:
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
                    self._intercepted_data = core_list
                    # 同步写盘（防止 main.py 未及时消费时数据丢失）
                    try:
                        save_intercepted_data(
                            json_list=core_list,
                            filename=self._intercept_filename,
                            unique_key=self._intercept_unique_key,
                        )
                        self._intercept_count += len(core_list)
                    except Exception as save_err:
                        logger.error(f"[XHR CORE] Failed to save: {save_err}")

                    # 彩色高亮提示（醒目绿色）
                    GREEN_BOLD = "\033[1;32m"
                    CYAN = "\033[36m"
                    RESET = "\033[0m"
                    print(
                        f"\n{GREEN_BOLD}[XHR INTERCEPT] 成功拦截核心 API 数据！{RESET} "
                        f"{CYAN}{len(core_list)} 条记录{RESET} "
                        f"← {url[:100]}\n"
                    )
                    logger.info(
                        f"[XHR CORE] Captured {len(core_list)} records "
                        f"(pattern={self._intercept_url_pattern!r}): {url[:120]}"
                    )
                    return  # 核心数据已处理，无需再走通用轨道重复保存

        # ── 轨道 2：通用拦截 ── 启发式全量收集 ──────────────────────────
        if not general_active:
            return

        data_list = self._extract_data_list(json_body)
        if not data_list:
            return

        logger.info(
            f"[XHR INTERCEPT] Detected {len(data_list)} records "
            f"from: {url[:120]}..."
        )

        # 保存到 Excel
        try:
            save_intercepted_data(
                json_list=data_list,
                filename=self._intercept_filename,
                unique_key=self._intercept_unique_key,
            )
            self._intercept_count += len(data_list)
        except Exception as e:
            logger.error(f"[XHR INTERCEPT] Failed to save data: {e}")

    def _extract_data_list(self, json_body) -> list[dict] | None:
        """
        启发式探测 JSON 响应中的数据列表。

        检查策略（按优先级）：
        1. json_body 本身是 list[dict] 且长度 >= 阈值
        2. json_body 是 dict，遍历所有值，找第一个符合条件的 list[dict]
        3. 递归检查常见嵌套路径：data.rows, data.list, data.records, result.data 等
        """
        threshold = self._intercept_min_list_size

        # 情况 1：顶层就是列表
        if isinstance(json_body, list):
            if len(json_body) >= threshold and isinstance(json_body[0], dict):
                return json_body
            return None

        # 情况 2：dict，遍历所有值
        if isinstance(json_body, dict):
            # 优先检查常见键名
            priority_keys = [
                "data", "rows", "list", "records", "items",
                "result", "results", "content", "details",
            ]
            # 先查优先键
            for key in priority_keys:
                val = json_body.get(key)
                if isinstance(val, list) and len(val) >= threshold:
                    if val and isinstance(val[0], dict):
                        return val
                # 可能嵌套一层：data.rows, data.list
                if isinstance(val, dict):
                    for sub_key in priority_keys:
                        sub_val = val.get(sub_key)
                        if isinstance(sub_val, list) and len(sub_val) >= threshold:
                            if sub_val and isinstance(sub_val[0], dict):
                                return sub_val

            # 兜底：遍历所有值
            for key, val in json_body.items():
                if key.startswith("_"):
                    continue
                if isinstance(val, list) and len(val) >= threshold:
                    if val and isinstance(val[0], dict):
                        return val

        return None

    @property
    def current_url(self) -> str:
        """返回当前活动页面的 URL，页面不可用时返回空字符串。"""
        try:
            return self._page.url if self._page else ""
        except Exception:
            return ""

    async def get_tabs_state(self) -> str:
        """
        返回当前所有标签页的状态摘要字符串，供注入 VLM 提示使用。

        格式示例：
          [0] 百度一下 (活跃) | [1] 淘宝 | [2] 京东

        若浏览器尚未启动或无任何页面，返回空字符串。
        """
        if not self._context:
            return ""
        open_pages = [p for p in self._context.pages if not p.is_closed()]
        if not open_pages:
            return ""
        parts: list[str] = []
        for idx, p in enumerate(open_pages):
            try:
                title = (await p.title()) or p.url or "about:blank"
                # 截断过长标题，避免撑爆提示词
                if len(title) > 30:
                    title = title[:27] + "..."
            except Exception:
                title = p.url or "about:blank"
            is_active = (p == self._page)
            label = f"[{idx}] {title}" + (" (活跃)" if is_active else "")
            parts.append(label)
        return " | ".join(parts)

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

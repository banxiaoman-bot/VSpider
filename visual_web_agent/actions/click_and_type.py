from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from ._base import (
    ActionContext,
    ActionHandler,
    ActionRegistry,
    UnknownActionError,
    _click_locator_with_js_fallback,
    _is_navigation_context_destroyed,
    _resolve_env_placeholders,
    _scroll_largest_container,
)

try:
    from ..vlm_client import VSpiderAction
    from ..browser_env import ActionExecutionError
    from ..auth_vault import SecretResolutionError, resolve_env_placeholders as _resolve_env
    from ..artifact_manager import register_download_artifact
    from ..page_data_controller import DATA_SIGNATURE_JS, pagination_moved
    from ..chat_answer_extractor import clean_chat_answer_text, extract_chat_answer
    from ..chat_send_locator import find_send_button as _chat_find_send_button
except ImportError:
    from vlm_client import VSpiderAction
    from browser_env import ActionExecutionError
    from auth_vault import SecretResolutionError, resolve_env_placeholders as _resolve_env
    from artifact_manager import register_download_artifact
    from page_data_controller import DATA_SIGNATURE_JS, pagination_moved
    from chat_answer_extractor import clean_chat_answer_text, extract_chat_answer
    from chat_send_locator import find_send_button as _chat_find_send_button

if TYPE_CHECKING:
    from playwright.async_api import Page
    from ..browser_env import BrowserEnv

logger = logging.getLogger("vspider.actions")


"""Click / ClickNewTab / FetchLinkContent / Type / Hover handlers"""

@ActionRegistry.register("click")
class ClickHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing click: element #{target_id} ({selector})")
        _sc_method = ""
        try:
            # ── E4: selector cache read — try cached CSS selector before SoM ──
            _sc_handle = None
            try:
                from visual_web_agent.selector_cache import (
                    SelectorCache as _SelC, cache_host as _sch,
                    selector_cache_enabled as _sce, som_cache_key as _sck,
                    validate_cached_target as _scv, derive_selector as _scd,
                )
                if _sce():
                    _sc_url = getattr(page, "url", "") or ""
                    _sc_host = _sch(_sc_url)
                    _sc_ckey = _sck(getattr(browser, "_last_som_elements", []), target_id) if _sc_host else ""
                    if _sc_ckey:
                        _sc_c = _SelC(_sc_host)
                        _sc_e = _sc_c.lookup("click", _sc_ckey)
                        if _sc_e:
                            _sc_name = _sc_ckey.split("::", 1)[-1] if "::" in _sc_ckey else ""
                            _sc_handle = await _scv(page, _sc_e, _sc_name)
                            if _sc_handle:
                                _sc_c.record_hit("click", _sc_ckey)
                                _sc_method = f"selector_cache {_sc_e.get('selector', '')}"
                                logger.info(f"[E4] click #{target_id} resolved via selector cache")
                            else:
                                _sc_c.invalidate("click", _sc_ckey)
            except Exception:
                _sc_handle = None

            if _sc_handle is not None:
                _the_handle = _sc_handle
            else:
                await browser._clear_som_overlays()
                target = await browser._resolve_action_target(target_id, "click")
                if target:
                    _the_handle = target.handle
                else:
                    raise RuntimeError(
                        f"Element #{target_id} not found on active page or its iframes "
                        f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                        f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                        f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
                    )
            await _the_handle.scroll_into_view_if_needed(
                timeout=browser._LOCATOR_TIMEOUT
            )
            _pending_xpath = await browser._get_xpath(_the_handle)
            _pending_ax_role, _pending_ax_name = await browser._get_accessibility_signature(
                page, _the_handle
            )

            try:
                await _the_handle.click(
                    force=True, timeout=browser._LOCATOR_TIMEOUT
                )
            except Exception as native_err:
                logger.warning(
                    f"[JS CLICK FALLBACK] native click blocked for element #{target_id}: "
                    f"{native_err}"
                )
                await _the_handle.evaluate(
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
            # E4: write-back — derive CSS selector and store for future runs
            if not _sc_method:
                try:
                    _scd_sel = await _scd(_the_handle)
                    if _scd_sel and _sc_host and _sc_ckey:
                        _SelC(_sc_host).store("click", _sc_ckey, _scd_sel,
                                              signature=f"{_pending_ax_role}::{_pending_ax_name}")
                except Exception:
                    pass
            if _pending_xpath:
                _rpa = {
                    "action": "click",
                    "xpath": _pending_xpath,
                    "ax_role": _pending_ax_role or "",
                    "ax_name": _pending_ax_name or "",
                    "type_value": "",
                }
                if _sc_method:
                    _rpa["method"] = _sc_method
                browser.rpa_trail.append(ctx.with_rpa_meta(_rpa))
                logger.debug(f"[RPA] Recorded click: {_pending_xpath}")
            logger.info(f"Click element #{target_id} succeeded")
        except Exception as e:
            if _is_navigation_context_destroyed(e):
                logger.info(
                    f"[CLICK] element #{target_id} triggered navigation; "
                    f"ignoring stale execution-context error: {e}"
                )
            else:
                browser._last_action_error = e
                logger.error(
                    f"Click element #{target_id} failed (page may have refreshed): {e}"
                )

        await browser._wait_after_action(is_navigation=True)
        return None


@ActionRegistry.register("click_new_tab")
class ClickNewTabHandler(ActionHandler):
    """
    中键点击：在新标签页打开链接，**焦点保留在原页面**。

    "Open in new tab" 的人类语义即"后台 tab"——用户没要求立即跳过去看。
    若 VLM 之后想看新 tab 内容，应显式 switch_tab；本 handler 不替它决定。
    这避免了"点击伪装广告 → 落到重 SPA 新 tab → 截图字体死锁 → agent 崩溃"
    的级联失败（参见 Baidu SEM 'Python入门-...' → comate.baidu.com 案例）。

    实现：
      1. 通过 SoM ID 拿到目标元素（同普通 click）
      2. middle-button click 触发浏览器原生新 tab；popup 监听器会把新 tab
         注册为 active —— 这是要矫正的"反语义"
      3. 显式 ``return ctx.page`` 让 dispatcher **跳过 Tab Guard**，并把
         焦点拨回 origin（若仍存活）
    """

    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing click_new_tab: element #{target_id} ({selector})")
        # Snapshot of pages BEFORE the click so we can detect whether a new
        # tab was actually opened (vs middle-click on non-link → no-op).
        # Used downstream to compose the "success notice" for VLM that
        # confirms the new tab opened even though focus stays on origin.
        _pre_pages: set[int] = (
            {id(p) for p in browser._context.pages if not p.is_closed()}
            if browser._context else set()
        )
        try:
            await browser._clear_som_overlays()
            target = await browser._resolve_action_target(target_id, "click_new_tab")
            if target:
                await target.handle.scroll_into_view_if_needed(
                    timeout=browser._LOCATOR_TIMEOUT
                )
            else:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
                )
            _pending_xpath = await browser._get_xpath(target.handle)
            _pending_ax_role, _pending_ax_name = await browser._get_accessibility_signature(
                page, target.handle
            )

            # 核心：中键点击 → 浏览器原生在新标签页打开
            try:
                await target.handle.click(
                    button="middle", force=True, timeout=browser._LOCATOR_TIMEOUT
                )
            except Exception:
                # middle-click 失败时降级为 Ctrl+Click
                try:
                    await target.handle.click(
                        modifiers=["Control"], force=True,
                        timeout=browser._LOCATOR_TIMEOUT,
                    )
                except Exception:
                    # 最终降级：JS 层 window.open
                    href = await target.handle.evaluate(
                        """el => {
                            const a = el.closest('a') || el.querySelector('a');
                            return a ? a.href : (el.href || el.getAttribute('href') || '');
                        }"""
                    )
                    if href:
                        await page.evaluate(f"window.open({href!r}, '_blank')")
                    else:
                        raise RuntimeError(
                            f"Element #{target_id} has no href; "
                            f"cannot open in new tab. Use regular 'click' instead."
                        )

            if _pending_xpath:
                browser.rpa_trail.append(
                    ctx.with_rpa_meta({
                        "action": "click_new_tab",
                        "xpath": _pending_xpath,
                        "ax_role": _pending_ax_role or "",
                        "ax_name": _pending_ax_name or "",
                        "type_value": "",
                    })
                )
                logger.debug(f"[RPA] Recorded click_new_tab: {_pending_xpath}")
            logger.info(f"click_new_tab element #{target_id} succeeded")
        except Exception as e:
            browser._last_action_error = e
            logger.error(
                f"click_new_tab element #{target_id} failed: {e}"
            )

        await browser._wait_after_action(is_navigation=True)

        # ── Tab Visit Stack：记录"从这个 tab 开出去的" ─────────────────
        # 用途：后续 close_tab(子) 时，知道该返回哪个父。即使 VLM 中途
        # switch_tab 到别处，stack 也忠实记录了开新 tab 这一刻的 origin。
        # 仅在确实有新 tab 时 push（middle-click 在非链接元素上是无效 no-op）。
        try:
            _post_pages = (
                [p for p in browser._context.pages if not p.is_closed()]
                if browser._context else []
            )
            _new_tab_opened = any(id(p) not in _pre_pages for p in _post_pages)
            if _new_tab_opened:
                browser.push_tab_visit(page)
        except Exception as _stack_err:
            logger.debug("[click_new_tab] push_tab_visit failed: %s", _stack_err)
            _post_pages = []
            _new_tab_opened = False

        # ── VLM 反馈通知：补偿 Fix 2 引起的"视觉信号丢失" ─────────────
        # 焦点留 origin 后，VLM 截图看不出 click_new_tab 成功，会陷入
        # "我没点中，换个 ID 再点" 的无限重试（Playwright Baidu 日志里点了 14 次）。
        # 注入到 ``_tab_switch_notice``，下一步 ask() 自动作为 feedback 喂给 VLM。
        if _new_tab_opened and not page.is_closed():
            try:
                # 找出新 tab 的索引 + origin 的索引
                _new_tabs = [p for p in _post_pages if id(p) not in _pre_pages]
                _new_tab = _new_tabs[-1] if _new_tabs else None
                _new_idx = _post_pages.index(_new_tab) if _new_tab in _post_pages else -1
                _origin_idx = _post_pages.index(page) if page in _post_pages else 0
                _total = len(_post_pages)
                _new_url = ""
                try:
                    _new_url = (_new_tab.url or "")[:80] if _new_tab else ""
                except Exception:
                    _new_url = ""
                browser.set_tab_notice(
                    f"✅ 上一步 click_new_tab(元素 #{target_id}) 已成功开启新标签页。\n"
                    f"  • 新标签索引: [{_new_idx}] {_new_url}\n"
                    f"  • 当前共 {_total} 个 tab\n"
                    f"  • 🔒 焦点【保留在原页 [{_origin_idx}]】"
                    f"(per '在新标签页打开' = 后台 tab 语义)\n"
                    f"  • 截图仍是原页 —— 这是正确的，**不是失败**\n"
                    f"⚠️ 不要再次 click_new_tab 同一元素 —— 会无限开新标签。\n"
                    f"下一步选项：\n"
                    f"  (a) 任务要求查看/操作新 tab → switch_tab(target_id={_new_idx}, type_value=\"{_new_idx}\")\n"
                    f"  (b) 任务要求继续在原页操作 → 直接执行下一动作\n"
                    f"  (c) 之后 close_tab(子 tab) 会自动回到父 tab [{_origin_idx}]",
                    severity='info',
                    coalesce=False,
                )
                logger.info(
                    "[click_new_tab] success notice injected: new=[%d] %s",
                    _new_idx, _new_url[:60],
                )
            except Exception as _notice_err:
                logger.debug(
                    "[click_new_tab] notice composition failed: %s",
                    _notice_err,
                )
        elif not _new_tab_opened:
            # No-op detection: middle-click on non-link element didn't open
            # anything. Tell VLM so it stops retrying the wrong element.
            try:
                browser.set_tab_notice(
                    f"❌ click_new_tab(元素 #{target_id}) 未能开启新标签页。\n"
                    f"  • 当前 tab 数量未变化，说明该元素不是链接（或链接被前端拦截）\n"
                    f"  • 不要再用 click_new_tab 操作同一元素\n"
                    f"建议：\n"
                    f"  (a) 改用普通 click 操作（如果元素本身只触发同页跳转）\n"
                    f"  (b) 换一个明确是 <a> link 的元素（看 AX Tree 里 role=link 的项）\n"
                    f"  (c) 用 click_text 按可见文字定位真正的链接",
                    severity='warn',
                    coalesce=False,
                )
                logger.info(
                    "[click_new_tab] no-op notice injected: element #%d "
                    "did not open a new tab", target_id,
                )
            except Exception:
                pass

        # ── 中键打开 = 后台 tab：把焦点拨回 origin ────────────────────
        # popup 监听器会把新 tab 注册为 active（_register_page activate=True），
        # 但 click_new_tab 的语义是"留在当前页"。显式 restore 之后 return page，
        # dispatcher 跳过 Tab Guard，焦点稳稳留在 origin。
        # origin 已关闭则 fallback 让 Tab Guard 处理（return None）。
        if not page.is_closed():
            if browser._page is not page:
                try:
                    await browser._activate_page(
                        page,
                        reason=f"click_new_tab: keep origin focused (target #{target_id})",
                    )
                except Exception as _restore_err:
                    logger.debug(
                        "[click_new_tab] origin restore failed; "
                        "letting Tab Guard handle: %s",
                        _restore_err,
                    )
                    return None
            return page  # 显式返回：dispatcher 跳过 Tab Guard
        return None


@ActionRegistry.register("fetch_link_content", "fetch_links_batch")
class FetchLinkContentHandler(ActionHandler):
    """
    Background tab fetch + JS extract + close + memory write — VLM stays free.

    Replaces the slow loop ``click_new_tab → switch_tab → screenshot →
    extract → close_tab`` (12-20 s, multiple VLM calls) with a single atomic
    action (1-3 s, zero VLM cost between fetch and result).

    Step shape:
      target_id : SoM ID of the link element (preferred — href read via JS)
      type_value: literal URL string (fallback when no link is on-screen)
      memory_key: where to store the result. Always required by the model;
                  auto-generated if missing for back-compat.

    Result written to ``workflow_memory[memory_key]``:
        {"url": <final URL after redirects>,
         "title": <h1 or <title>>,
         "content": <innerText of main/article/body, capped at 6 K>}
    ``latest_memory`` is also updated to a 200-char preview so trailing
    ``save_to_memory``-style consumers keep working.

    Caveats (told to the user via tab_switch_notice):
      * Only supports content extraction. If the new page needs interaction,
        use ``click_new_tab`` + ``switch_tab`` instead.
      * Cloudflare / heavy SPA / login-walled pages may serve different
        content to a programmatic ``goto`` than to a real click — back off
        to the interactive path when that happens.
    """

    _MAX_CONTENT_CHARS = 6000
    _GOTO_TIMEOUT_MS = 15000
    _SETTLE_AFTER_LOAD_S = 0.5
    _MAX_BATCH_LINKS = 20
    _MAX_BATCH_CONCURRENCY = 5

    def _parse_options(self, type_value: str) -> tuple[dict[str, Any], bool]:
        text = (type_value or "").strip()
        if not text or text[0] not in "[{":
            return {}, False
        try:
            data = json.loads(text)
        except Exception:
            return {}, False
        if isinstance(data, dict):
            return data, True
        if isinstance(data, list):
            if all(
                isinstance(item, str)
                and item.lower().startswith(("http://", "https://"))
                for item in data
            ):
                return {"urls": data}, True
            return {"target_ids": data}, True
        return {}, True

    def _parse_target_ids(self, value: Any) -> list[int]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = re.findall(r"@?e?(\d+)", str(value), flags=re.I)
        ids: list[int] = []
        seen: set[int] = set()
        for item in raw_items:
            try:
                if isinstance(item, str):
                    match = re.search(r"\d+", item)
                    if not match:
                        continue
                    tid = int(match.group(0))
                else:
                    tid = int(item)
            except (TypeError, ValueError):
                continue
            if tid > 0 and tid not in seen:
                ids.append(tid)
                seen.add(tid)
        return ids

    def _parse_urls(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = re.split(r"[\s,]+", str(value).strip())
        urls: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            url = str(item or "").strip().strip("'\"")
            if (
                url
                and url.lower().startswith(("http://", "https://"))
                and url not in seen
            ):
                urls.append(url)
                seen.add(url)
        return urls

    def _normalize_selectors(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = re.split(r"[\n;]+|,\s*(?=[.#\[:a-zA-Z*])", str(value))
        selectors: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            selector = str(item or "").strip()
            if selector and selector not in seen:
                selectors.append(selector)
                seen.add(selector)
        return selectors[:12]

    def _extract_mode(self, options: dict[str, Any]) -> str:
        mode = str(
            options.get("mode")
            or options.get("extract_mode")
            or options.get("content_mode")
            or "dom"
        ).strip().lower()
        return "ax" if mode in {"ax", "accessibility", "accessibility_tree"} else "dom"

    def _batch_concurrency(self, options: dict[str, Any], count: int) -> int:
        try:
            configured = int(options.get("concurrency") or options.get("parallel") or 4)
        except (TypeError, ValueError):
            configured = 4
        return max(1, min(count, configured, self._MAX_BATCH_CONCURRENCY))

    def _ensure_http_url(self, url: str, action_name: str) -> str:
        url = str(url or "").strip()
        lowered = url.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ActionExecutionError(
                f"{action_name}: 拒绝访问非 http(s) URL: {url[:80]!r}"
            )
        return url

    async def _resolve_url_from_target(
        self,
        browser: "BrowserEnv",
        target_id: int,
        action_name: str,
    ) -> str:
        await browser._clear_som_overlays()
        target = await browser._resolve_action_target(target_id, action_name)
        if not target:
            raise ActionExecutionError(
                f"{action_name}: 元素 #{target_id} 在当前页或 iframe 中找不到"
            )
        try:
            url = str(
                await target.handle.evaluate(
                    """el => {
                        const a = el.closest('a') || el.querySelector('a') || el;
                        return String(a.href || a.getAttribute('href') || '').trim();
                    }"""
                ) or ""
            ).strip()
        except Exception as href_err:
            raise ActionExecutionError(
                f"{action_name}: 读取 #{target_id} 的 href 失败: {href_err}"
            )
        if not url:
            raise ActionExecutionError(f"{action_name}: 元素 #{target_id} 没有 href")
        return self._ensure_http_url(url, action_name)

    async def _extract_dom_payload(
        self,
        page: "Page",
        selectors: list[str],
    ) -> dict[str, Any]:
        return await page.evaluate(
            r"""([maxChars, selectorList]) => {
                const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
                const removeNoise = (root) => {
                    if (!root || !root.querySelectorAll) return;
                    root.querySelectorAll([
                        'script','style','noscript','template',
                        'nav','footer','header','aside',
                        '[role="navigation"]','[role="banner"]','[role="contentinfo"]',
                        '.nav','.navbar','.footer','.sidebar','.menu',
                        '.cookie','.cookies','.ads','.ad','.advertisement'
                    ].join(',')).forEach((n) => n.remove());
                };
                const textFrom = (node) => {
                    if (!node) return '';
                    const clone = node.cloneNode(true);
                    removeNoise(clone);
                    return clean(clone.innerText || clone.textContent || '');
                };
                const title = clean(
                    document.querySelector('h1')?.innerText
                    || document.title
                    || ''
                );
                const selectors = Array.isArray(selectorList) ? selectorList : [];
                const selected = [];
                for (const sel of selectors) {
                    try {
                        selected.push(...Array.from(document.querySelectorAll(sel)));
                    } catch (_) {}
                }
                if (selected.length) {
                    const joined = selected.map(textFrom).filter(Boolean).join('\n\n');
                    return {
                        title,
                        content: joined.slice(0, maxChars),
                        url: location.href,
                        full_len: joined.length,
                        mode: 'dom',
                        selectors,
                        selector_count: selected.length,
                    };
                }
                const containers = [
                    document.querySelector('article'),
                    document.querySelector('main'),
                    document.querySelector('[role="main"]'),
                    document.querySelector('#mw-content-text'),
                    document.querySelector('#content'),
                    document.querySelector('.content'),
                    document.body,
                ].filter(Boolean);
                let best = '';
                for (const c of containers) {
                    const t = textFrom(c);
                    if (t.length > best.length) best = t;
                    if (best.length >= maxChars) break;
                }
                return {
                    title,
                    content: best.slice(0, maxChars),
                    url: location.href,
                    full_len: best.length,
                    mode: 'dom',
                    selectors,
                    selector_count: 0,
                };
            }""",
            [self._MAX_CONTENT_CHARS, selectors],
        )

    def _flatten_ax_tree(self, tree: Any) -> tuple[str, list[dict[str, Any]]]:
        lines: list[str] = []
        structured: list[dict[str, Any]] = []
        skip_roles = {"generic", "none", "presentation", "InlineTextBox", "LineBreak"}

        def walk(node: Any, depth: int = 0) -> None:
            if isinstance(node, list):
                for child in node:
                    walk(child, depth)
                return
            if not isinstance(node, dict):
                return
            role = str(node.get("role") or "").strip()
            name = str(node.get("name") or "").strip()
            value = str(node.get("value") or "").strip()
            if role not in skip_roles and (role or name or value):
                pieces = [f"[{role or 'node'}]"]
                if name:
                    pieces.append(name)
                if value and value != name:
                    pieces.append(f"= {value}")
                for key in ("checked", "selected", "expanded", "disabled", "pressed"):
                    if key in node:
                        pieces.append(f"{key}:{node.get(key)}")
                lines.append("  " * min(depth, 6) + " ".join(pieces))
                structured.append({
                    "role": role,
                    "name": name,
                    "value": value,
                    "depth": depth,
                })
            for child in node.get("children") or []:
                walk(child, depth + 1)

        walk(tree)
        content = "\n".join(lines)
        return content[: self._MAX_CONTENT_CHARS], structured[:400]

    async def _extract_ax_payload(
        self,
        browser: "BrowserEnv",
        page: "Page",
        selectors: list[str],
    ) -> dict[str, Any]:
        tree = None
        getter = getattr(browser, "_get_ax_tree_via_cdp", None)
        if callable(getter):
            tree = await getter(page, interesting_only=True)
        if not tree:
            payload = await self._extract_dom_payload(page, selectors)
            payload["mode"] = "dom_fallback_from_ax"
            return payload
        content, structured = self._flatten_ax_tree(tree)
        selector_payload: dict[str, Any] = {}
        if selectors:
            try:
                selector_payload = await self._extract_dom_payload(page, selectors)
            except Exception as selector_err:
                logger.debug("[fetch_link_content] selector scope in ax mode failed: %s", selector_err)
        selector_content = str(selector_payload.get("content") or "").strip()
        if selector_content:
            content = (
                "[selector_scope]\n"
                f"{selector_content[: self._MAX_CONTENT_CHARS]}\n\n"
                "[ax_tree]\n"
                f"{content}"
            )[: self._MAX_CONTENT_CHARS]
        try:
            meta = await page.evaluate(
                "() => ({title: document.title || '', url: location.href})"
            )
        except Exception:
            meta = {}
        return {
            "title": str((meta or {}).get("title") or "").strip(),
            "content": content,
            "url": str((meta or {}).get("url") or getattr(page, "url", "") or "").strip(),
            "full_len": len(content),
            "mode": "ax",
            "selectors": selectors,
            "selector_count": int(selector_payload.get("selector_count") or 0),
            "selector_content": selector_content[: self._MAX_CONTENT_CHARS],
            "structured": structured,
        }

    async def _extract_payload(
        self,
        browser: "BrowserEnv",
        page: "Page",
        *,
        mode: str,
        selectors: list[str],
    ) -> dict[str, Any]:
        if mode == "ax":
            return await self._extract_ax_payload(browser, page, selectors)
        return await self._extract_dom_payload(page, selectors)

    async def _fetch_one(
        self,
        ctx: ActionContext,
        url: str,
        *,
        mode: str,
        selectors: list[str],
        action_name: str,
    ) -> dict[str, Any]:
        new_page = None
        try:
            new_page = await ctx.page.context.new_page()
            response = None
            try:
                # Capture the navigation response so we can surface the HTTP
                # status to VLM. None when the URL has no main resource
                # (data:/about: schemes) or when redirected without a body.
                response = await new_page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._GOTO_TIMEOUT_MS,
                )
            except Exception as goto_err:
                raise ActionExecutionError(
                    f"{action_name}: goto 失败 ({url[:80]!r}): {goto_err}"
                )
            await asyncio.sleep(self._SETTLE_AFTER_LOAD_S)
            try:
                http_status = (
                    int(response.status) if response is not None else None
                )
            except Exception:
                http_status = None
            http_ok = bool(http_status and 200 <= http_status < 400)
            try:
                payload = await self._extract_payload(
                    ctx.browser,
                    new_page,
                    mode=mode,
                    selectors=selectors,
                )
            except Exception as ev_err:
                raise ActionExecutionError(
                    f"{action_name}: 内容提取失败 ({url[:80]!r}): {ev_err}"
                )
            payload = dict(payload or {})
            payload["title"] = str(payload.get("title") or "").strip()
            payload["content"] = str(payload.get("content") or "").strip()
            payload["url"] = str(payload.get("url") or url).strip()
            payload["full_len"] = int(payload.get("full_len") or len(payload["content"]))
            payload.setdefault("mode", mode)
            payload.setdefault("selectors", selectors)
            payload["http_status"] = http_status
            payload["http_ok"] = http_ok
            return payload
        finally:
            if new_page is not None:
                try:
                    await new_page.close()
                except Exception as close_err:
                    logger.debug("[%s] new tab close failed: %s", action_name, close_err)

    async def _restore_origin(
        self,
        browser: "BrowserEnv",
        page: "Page",
        action_name: str,
    ) -> None:
        if not page.is_closed() and browser._page is not page:
            try:
                await browser._activate_page(page, reason=f"{action_name}: restore origin")
            except Exception as restore_err:
                logger.debug("[%s] origin restore failed: %s", action_name, restore_err)

    def _single_url_from_options(
        self,
        options: dict[str, Any],
        type_value: str,
        parsed_json: bool,
    ) -> str:
        for key in ("url", "href"):
            if options.get(key):
                return str(options.get(key) or "").strip()
        urls = self._parse_urls(options.get("urls"))
        if urls:
            return urls[0]
        if type_value and not parsed_json:
            return type_value
        return ""

    async def _execute_batch(
        self,
        ctx: ActionContext,
        *,
        options: dict[str, Any],
        type_value: str,
        parsed_json: bool,
    ) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        action_name = ctx.action.action or "fetch_links_batch"
        selectors = self._normalize_selectors(
            options.get("selectors") if "selectors" in options else options.get("selector")
        )
        mode = self._extract_mode(options)
        memory_key = (
            (ctx.action.memory_key or "").strip()
            or str(options.get("memory_key") or options.get("memory_key_prefix") or "").strip()
            or f"fetched_batch_{int(time.time())}"
        )

        target_ids = self._parse_target_ids(
            options.get("target_ids") if "target_ids" in options else options.get("ids")
        )
        urls = self._parse_urls(
            options.get("urls") if "urls" in options else options.get("url")
        )
        if ctx.action.target_id and ctx.action.target_id != 0:
            target_ids = [
                ctx.action.target_id,
                *[tid for tid in target_ids if tid != ctx.action.target_id],
            ]
        if not parsed_json and type_value:
            raw_urls = self._parse_urls(type_value)
            if raw_urls:
                urls.extend(u for u in raw_urls if u not in urls)
            else:
                urls.extend([])
                target_ids.extend(
                    tid for tid in self._parse_target_ids(type_value)
                    if tid not in target_ids
                )

        for tid in target_ids[: self._MAX_BATCH_LINKS]:
            urls.append(await self._resolve_url_from_target(browser, tid, action_name))
        urls = [self._ensure_http_url(url, action_name) for url in urls]
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url not in seen:
                deduped.append(url)
                seen.add(url)
        urls = deduped[: self._MAX_BATCH_LINKS]
        if not urls:
            raise ActionExecutionError(
                f"{action_name}: 需要 type_value JSON 提供 target_ids/urls，或 target_id"
            )

        concurrency = self._batch_concurrency(options, len(urls))
        sem = asyncio.Semaphore(concurrency)

        async def run_one(index: int, url: str) -> dict[str, Any]:
            async with sem:
                item_key = f"{memory_key}_{index + 1}"
                try:
                    payload = await self._fetch_one(
                        ctx,
                        url,
                        mode=mode,
                        selectors=selectors,
                        action_name=action_name,
                    )
                    payload["ok"] = True
                except Exception as err:
                    payload = {
                        "ok": False,
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": str(err),
                        "mode": mode,
                        "selectors": selectors,
                        "full_len": 0,
                    }
                payload["memory_key"] = item_key
                return payload

        results = await asyncio.gather(*(run_one(i, url) for i, url in enumerate(urls)))
        await self._restore_origin(browser, page, action_name)

        workflow_memory = ctx.workflow_memory
        if workflow_memory is not None:
            workflow_memory[memory_key] = results
            previews: list[str] = []
            for result in results:
                key = str(result.get("memory_key") or "")
                if key:
                    workflow_memory[key] = result
                if result.get("ok") and result.get("content"):
                    previews.append(str(result.get("content") or "")[:200])
            workflow_memory["latest_memory"] = "\n\n".join(previews)[:1000]

        try:
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "fetch_links_batch",
                    "type_value": json.dumps(
                        {
                            "urls": urls,
                            "mode": mode,
                            "selectors": selectors,
                            "concurrency": concurrency,
                        },
                        ensure_ascii=False,
                    ),
                    "memory_key": memory_key,
                })
            )
        except Exception as trail_err:
            logger.debug("[%s] rpa_trail append failed: %s", action_name, trail_err)

        ok_count = sum(1 for result in results if result.get("ok"))
        # Build a short HTTP failure summary (4xx/5xx + connection errors)
        # so VLM can decide whether to retry or skip the bad ones.
        _http_failures: list[str] = []
        for _r in results:
            _st = _r.get("http_status")
            _ok = bool(_r.get("ok"))
            _u = str(_r.get("url") or "")[:60]
            if not _ok:
                # Connection error (no status reached)
                _err = str(_r.get("error") or "").splitlines()[0][:80] if _r.get("error") else "fetch error"
                _http_failures.append(f"    · ❌ {_u} — {_err}")
            elif _st is not None and (_st >= 400 or _st < 200):
                _http_failures.append(f"    · ⚠️ HTTP {_st}: {_u}")
        _failures_block = ""
        if _http_failures:
            _failures_block = (
                "\n  • 失败链接（建议跳过或换 selector）:\n"
                + "\n".join(_http_failures[:10])
                + ("\n    · …" if len(_http_failures) > 10 else "")
            )
        browser.set_tab_notice(
            f"✅ fetch_links_batch 完成 — 并发抓取 {ok_count}/{len(results)} 条链接并写回 memory。\n"
            f"  • mode: {mode}\n"
            f"  • selectors: {selectors or 'default main/article/body'}\n"
            f"  • 存储: workflow_memory[{memory_key!r}] = list[{{url, title, content, ok, http_status}}]\n"
            f"  • 单条: workflow_memory[{memory_key + '_1'!r}], ...\n"
            f"  • 焦点仍在原页"
            + _failures_block,
            severity='info',
            coalesce=False,
        )
        logger.info(
            "[FETCH LINKS BATCH] %d/%d ok -> memory[%s]",
            ok_count,
            len(results),
            memory_key,
        )
        print(
            f"\033[1;36m🔗 [FETCH BATCH]\033[0m "
            f"{ok_count}/{len(results)} -> memory[\033[33m{memory_key}\033[0m]"
        )
        return page

    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        action = ctx.action
        target_id = action.target_id
        type_value = (action.type_value or "").strip()
        options, parsed_json = self._parse_options(type_value)
        action_name = action.action or "fetch_link_content"
        if action_name == "fetch_links_batch" or any(
            key in options for key in ("target_ids", "ids", "urls")
        ):
            return await self._execute_batch(
                ctx,
                options=options,
                type_value=type_value,
                parsed_json=parsed_json,
            )

        memory_key = (
            (action.memory_key or "").strip()
            or str(options.get("memory_key") or "").strip()
        )
        if not memory_key:
            memory_key = f"fetched_{int(time.time())}"
            logger.warning(
                "[fetch_link_content] memory_key missing — auto-generated %s",
                memory_key,
            )
        selectors = self._normalize_selectors(
            options.get("selectors") if "selectors" in options else options.get("selector")
        )
        mode = self._extract_mode(options)

        # 1. Resolve URL ────────────────────────────────────────────────
        if target_id and target_id != 0:
            url = await self._resolve_url_from_target(browser, target_id, action_name)
        else:
            url = self._single_url_from_options(options, type_value, parsed_json)
        if not url:
            raise ActionExecutionError(
                "fetch_link_content: 既未提供 target_id (链接元素)，"
                "也未提供 type_value (URL)"
            )
        url = self._ensure_http_url(url, action_name)

        # 2. Background fetch + extract + close ─────────────────────────
        payload = await self._fetch_one(
            ctx,
            url,
            mode=mode,
            selectors=selectors,
            action_name=action_name,
        )
        title = str(payload.get("title") or "").strip()
        content = str(payload.get("content") or "").strip()
        final_url = str(payload.get("url") or url).strip()
        full_len = int(payload.get("full_len") or len(content))

        # 3. Restore origin focus (popup listener may have swapped) ─────
        await self._restore_origin(browser, page, action_name)

        # 4. Write to workflow memory ───────────────────────────────────
        workflow_memory = ctx.workflow_memory
        if workflow_memory is not None:
            stored = {
                "url": final_url,
                "title": title,
                "content": content,
            }
            for optional_key in (
                "mode",
                "selectors",
                "selector_count",
                "selector_content",
                "structured",
            ):
                if optional_key in payload:
                    stored[optional_key] = payload[optional_key]
            workflow_memory[memory_key] = stored
            workflow_memory["latest_memory"] = content[:200]

        # 5. RPA trail (deterministic replay knows nothing but the URL) ─
        try:
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "fetch_link_content",
                    "type_value": json.dumps(
                        {
                            "url": url,
                            "mode": mode,
                            "selectors": selectors,
                        },
                        ensure_ascii=False,
                    ) if (mode != "dom" or selectors) else url,
                    "memory_key": memory_key,
                })
            )
        except Exception as _trail_err:
            logger.debug("[fetch_link_content] rpa_trail append failed: %s", _trail_err)

        # 6. Tell VLM what happened ─────────────────────────────────────
        truncation_note = (
            f" (truncated from {full_len})"
            if full_len > len(content)
            else ""
        )
        # Surface HTTP status. 4xx/5xx is a strong signal for VLM to skip
        # the URL rather than treat the error page as real data.
        _http_status = payload.get("http_status")
        _http_ok = bool(payload.get("http_ok"))
        if _http_status is None:
            _http_line = "ℹ️ HTTP: 状态未知（data:/about: scheme 或 SPA 路由）"
            _http_advisory = ""
        elif 200 <= _http_status < 300:
            _http_line = f"✅ HTTP: {_http_status}"
            _http_advisory = ""
        elif 300 <= _http_status < 400:
            _http_line = f"↪️ HTTP: {_http_status}（重定向，已跟随）"
            _http_advisory = ""
        elif 400 <= _http_status < 500:
            _http_line = f"⚠️ HTTP: {_http_status} 客户端错误"
            _http_advisory = (
                "\n\n⚠️ [HTTP ERROR] 该链接返回 "
                f"{_http_status}（404/403/401 等），抓回的内容很可能是错误页 HTML。"
                "建议：不要 extract 该 memory，改用其他链接或换一个 selector。"
            )
        elif 500 <= _http_status < 600:
            _http_line = f"⚠️ HTTP: {_http_status} 服务器错误"
            _http_advisory = (
                "\n\n⚠️ [HTTP ERROR] 该链接返回 "
                f"{_http_status}（服务端故障）。"
                "建议：稍后用 wait+fetch_link_content 重试一次；连续失败应跳过。"
            )
        else:
            _http_line = f"❓ HTTP: {_http_status}"
            _http_advisory = ""
        browser.set_tab_notice(
            f"✅ fetch_link_content 完成 — 已用 JS 跨 tab 抓取并写回 memory。\n"
            f"  • URL: {final_url[:120]}\n"
            f"  • {_http_line}\n"
            f"  • Title: {title[:80]!r}\n"
            f"  • mode: {mode}; selectors: {selectors or 'default main/article/body'}\n"
            f"  • 抽取: {len(content)} 字{truncation_note}\n"
            f"  • 存储: workflow_memory[{memory_key!r}] = {{url, title, content, http_status}}\n"
            f"  • 新标签页已关闭，焦点仍在原页 — 截图不变是正确的\n"
            f"用法：\n"
            f"  • type_value 模板插值: {{{{{memory_key}.content}}}} / {{{{{memory_key}.title}}}}\n"
            f"  • 多链接抓取：emit fetch_links_batch，type_value 填 JSON {{target_ids:[...]}}",
            severity='info',
            coalesce=False,
        )
        if _http_advisory:
            # J: append HTTP advisory as a coalesced warn notice
            # so frontend gets max(info, warn) = warn severity.
            browser.set_tab_notice(
                _http_advisory, severity='warn', coalesce=True,
            )

        logger.info(
            "[FETCH LINK CONTENT] %s → memory[%s] (%d/%d chars)",
            url[:80], memory_key, len(content), full_len,
        )
        print(
            f"\033[1;36m🔗 [FETCH LINK]\033[0m "
            f"{url[:60]}... → memory[\033[33m{memory_key}\033[0m] "
            f"(\033[32m{len(content)}\033[0m chars)"
        )

        return page  # skip Tab Guard


# S5: standalone richtext writer for TypeHandler — mirrors form_set's
# setRichTextValue editor adapters (Quill → TinyMCE → CKEditor5 →
# execCommand insertText → structured paragraphs). keyboard.type into a
# contenteditable desyncs the editor's document model (Quill re-renders
# over it, TinyMCE never sees it), so type routes through the same APIs.
_TYPE_RICHTEXT_WRITE_JS = """(el, raw) => {
    const text = String(raw == null ? '' : raw);
    const escHtml = (s) => String(s).replace(/&/g, '&amp;')
        .replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const toParagraphHtml = (t) => String(t)
        .split(/\\n{2,}/)
        .map(part => `<p>${escHtml(part).replace(/\\n/g, '<br>')}</p>`)
        .join('') || '<p></p>';
    const qlEditor = el.classList?.contains('ql-editor')
        ? el
        : (el.querySelector?.('.ql-editor')
            || el.closest?.('.ql-container')?.querySelector?.('.ql-editor'));
    if (qlEditor) {
        const container = qlEditor.closest('.ql-container') || qlEditor.parentElement;
        const quill = (container && container.__quill)
            || window.Quill?.find?.(container) || null;
        if (quill && typeof quill.setText === 'function') {
            quill.setText(text, 'user');
            return 'quill_api';
        }
    }
    const tiny = window.tinymce;
    if (tiny && (typeof tiny.get === 'function' || Array.isArray(tiny.editors))) {
        const editors = Array.from(tiny.editors || []);
        const ids = [el.id, el.id?.replace(/_ifr$/, '')].filter(Boolean);
        const byId = typeof tiny.get === 'function'
            ? ids.map(id => tiny.get(id)).find(Boolean) : null;
        const ed = byId || editors.find(e => {
            const body = e?.getBody?.();
            if (body && (body === el || body.contains?.(el) || el.contains?.(body))) return true;
            return Boolean(e?.getContainer?.()?.contains?.(el));
        });
        if (ed && typeof ed.setContent === 'function') {
            ed.setContent(toParagraphHtml(text));
            ed.fire?.('change');
            return 'tinymce_api';
        }
    }
    const ckHost = el.closest?.('.ck-editor__editable') || el;
    if (ckHost?.ckeditorInstance && typeof ckHost.ckeditorInstance.setData === 'function') {
        ckHost.ckeditorInstance.setData(toParagraphHtml(text));
        return 'ckeditor5_api';
    }
    try {
        el.focus?.({preventScroll: true});
        const sel = window.getSelection?.();
        if (sel && typeof document.execCommand === 'function') {
            sel.selectAllChildren(el);
            document.execCommand('delete', false, null);
            if (document.execCommand('insertText', false, text)) {
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return 'exec_insert_text';
            }
        }
    } catch (_) {}
    el.innerHTML = toParagraphHtml(text);
    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return 'structured_paragraphs';
}"""


@ActionRegistry.register("type")
class TypeHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        type_value = ctx.action.type_value
        workflow_memory = ctx.workflow_memory
        display_value = type_value
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing type: element #{target_id} <- {display_value!r}")
        try:
            await browser._clear_som_overlays()
            target = await browser._resolve_action_target(target_id, "type")

            if not target:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
                )

            await target.handle.scroll_into_view_if_needed(
                timeout=browser._LOCATOR_TIMEOUT
            )

            # 视口锁定预检（暗礁 1 防线）：
            # SoM ID 基于视口实时分配；如果输入框只露一半就 type，下一帧截图 ID 会洗牌，
            # VLM 会把同一字段误认成另一个字段反复覆写。强制要求目标元素**完整在视口内**，
            # 否则主动 scrollIntoView({block:"center"}) 居中 + 等待稳定后再操作。
            try:
                _viewport_check = await target.handle.evaluate(
                    """el => {
                        const r = el.getBoundingClientRect();
                        const vh = window.innerHeight;
                        const vw = window.innerWidth;
                        return {
                            top: r.top, bottom: r.bottom, left: r.left, right: r.right,
                            height: r.height, width: r.width, vh, vw,
                            fully_visible: r.top >= 0 && r.bottom <= vh && r.left >= 0 && r.right <= vw,
                            partial_clip: Math.max(0, -r.top) + Math.max(0, r.bottom - vh)
                        };
                    }"""
                )
                if _viewport_check and not _viewport_check.get("fully_visible"):
                    _clip = _viewport_check.get("partial_clip", 0) or 0
                    _h = _viewport_check.get("height", 1) or 1
                    if _h > 0 and (_clip / _h) > 0.2:  # 超过 20% 高度被截断
                        logger.info(
                            f"[TYPE] 目标输入框被视口截断 {_clip:.0f}/{_h:.0f}px "
                            f"({_clip / _h * 100:.0f}%)，强制 scrollIntoView({{block:'center'}})"
                        )
                        await target.handle.evaluate(
                            "el => el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'})"
                        )
                        await asyncio.sleep(0.4)  # 等滚动稳定
            except Exception as _vp_err:
                logger.debug(f"[TYPE] viewport precheck failed (non-fatal): {_vp_err}")

            # ── No-op short-circuit: input already contains the target text ─
            # When VLM types the same text twice in a row (run_log_20260518_141728
            # step 1+2 both typed "介绍一下deepseek" because step 1's success
            # wasn't obvious from the next screenshot), the second type appends
            # or overwrites and breaks the message. If the element's current
            # text already exactly equals what we're about to type, skip the
            # action and tell VLM the value is set.
            try:
                _current_value = await target.handle.evaluate(
                    """el => {
                        const v = (el.value != null ? el.value : null);
                        if (v != null) return String(v);
                        return String(el.innerText || el.textContent || '');
                    }"""
                )
                _current_norm = str(_current_value or "").strip()
                _target_norm = str(type_value or "").strip()
                if _target_norm and _current_norm == _target_norm:
                    logger.info(
                        "[TYPE NO-OP] element #%s already contains target text "
                        "(%d chars); skipping duplicate type",
                        target_id, len(_target_norm),
                    )
                    print(
                        f"\033[33m⏭️  [TYPE NO-OP]\033[0m 输入框已含目标文本 "
                        f"({len(_target_norm)} 字)，跳过重复输入"
                    )
                    browser.rpa_trail.append(
                        ctx.with_rpa_meta({
                            "action": "type",
                            "target_id": target_id,
                            "type_value": type_value,
                            "noop": True,
                        })
                    )
                    # Arm the post-noop hard-correction signal so the NEXT
                    # step's guard chain can hard-rewrite a duplicate type
                    # to press_key Enter. The notice (soft signal) alone
                    # doesn't always change VLM's mind — its thought may
                    # say "no need to type" while the JSON still emits
                    # type (run_log_20260518_145655 step 4: thought said
                    # "无需重复输入" but action was type again).
                    try:
                        browser._last_type_noop = {
                            "target_id": int(target_id),
                            "value": _target_norm,
                        }
                    except Exception:
                        pass
                    try:
                        browser.set_tab_notice(
                            f"⏭️ [TYPE NO-OP] 输入框 #{target_id} 已含目标文本 "
                            f"{_target_norm[:40]!r}。"
                            "下一步请直接 press_key Enter 提交或点发送按钮；"
                            "不要重复 type 同一内容（如再次 type，系统会自动改写为 press_key Enter）。",
                            severity='info',
                            coalesce=False,
                        )
                    except Exception:
                        pass
                    return None
            except Exception as _noop_err:
                logger.debug("[TYPE] no-op precheck failed (non-fatal): %s", _noop_err)

            _pending_xpath = await browser._get_xpath(target.handle)
            _pending_ax_role, _pending_ax_name = await browser._get_accessibility_signature(
                page, target.handle
            )

            count_before = await browser._count_interactive_elements()

            try:
                await target.handle.click(
                    force=True, timeout=browser._LOCATOR_TIMEOUT
                )
            except Exception:
                await target.handle.evaluate("""el => {
                    if (typeof el.click === 'function') el.click();
                    else el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                }""")

            await asyncio.sleep(0.5)

            # 动态插值：{{key}} → workflow_memory 中的真实值
            if workflow_memory and "{{" in type_value:
                def _interpolate(m: re.Match) -> str:
                    key = m.group(1).strip()
                    val = workflow_memory.get(key)
                    if val is None:
                        logger.warning(
                            f"[MEMORY] interpolation: key '{key}' not found in workflow_memory "
                            f"(available: {list(workflow_memory.keys())})"
                        )
                        return m.group(0)
                    return str(val)
                resolved = re.sub(r"\{\{([^}]+)\}\}", _interpolate, type_value)
                if resolved != type_value:
                    logger.info(
                        f"[MEMORY] type interpolation: {type_value!r} → {resolved!r}"
                    )
                type_value = resolved
                display_value = resolved

            env_template = display_value if "{{env:" in display_value else ""
            type_value, used_auth_vault, env_names = _resolve_env_placeholders(type_value)
            if used_auth_vault:
                display_value = env_template
                logger.info(
                    "[AUTH VAULT] type_value resolved from env placeholder(s): %s",
                    ", ".join(env_names),
                )

            # ── S5: contenteditable hosts route through the rich-text editor
            # API instead of raw keyboard typing, which desyncs the editor's
            # document model (same adapters as form_set).
            try:
                _is_richtext_host = bool(
                    await target.handle.evaluate(
                        "el => Boolean(el && el.isContentEditable)"
                    )
                )
            except Exception:
                _is_richtext_host = False

            _richtext_method = ""
            if _is_richtext_host:
                _richtext_method = str(
                    await target.handle.evaluate(_TYPE_RICHTEXT_WRITE_JS, type_value)
                    or ""
                )
                _observed_text = str(
                    await target.handle.evaluate(
                        "el => String(el.innerText || el.textContent || '').trim()"
                    )
                    or ""
                )
                _norm = lambda s: re.sub(r"\s+", " ", str(s or "")).strip()  # noqa: E731
                if _norm(type_value) and _norm(type_value) not in _norm(_observed_text):
                    raise RuntimeError(
                        f"type richtext readback mismatch on element #{target_id}: "
                        f"wrote via {_richtext_method}, but editor text is "
                        f"{_observed_text[:120]!r}"
                    )
                logger.info(
                    "[TYPE RICHTEXT] element #%s written via %s (form_set adapters)",
                    target_id,
                    _richtext_method,
                )
            else:
                modifier = "Meta" if sys.platform == "darwin" else "Control"
                await page.keyboard.press(f"{modifier}+a")
                await page.keyboard.press("Backspace")
                await page.keyboard.type(type_value, delay=50)

            if _pending_xpath:
                _trail_step = {
                    "action": "type",
                    "xpath": _pending_xpath,
                    "ax_role": _pending_ax_role or "",
                    "ax_name": _pending_ax_name or "",
                    "type_value": display_value if used_auth_vault else type_value,
                }
                if _richtext_method:
                    _trail_step["method"] = _richtext_method
                    _trail_step["verified"] = True
                browser.rpa_trail.append(ctx.with_rpa_meta(_trail_step))
                logger.debug(
                    "[RPA] Recorded type: %s <- %r",
                    _pending_xpath,
                    display_value if used_auth_vault else type_value,
                )
            # E4: write-back — derive and store CSS selector for future runs
            try:
                from visual_web_agent.selector_cache import (
                    SelectorCache as _SelC, cache_host as _sch,
                    selector_cache_enabled as _sce, som_cache_key as _sck,
                    derive_selector as _scd,
                )
                if _sce():
                    _sc_url = getattr(page, "url", "") or ""
                    _sc_host = _sch(_sc_url)
                    _sc_ckey = _sck(getattr(browser, "_last_som_elements", []), target_id) if _sc_host else ""
                    if _sc_ckey:
                        _sc_sel = await _scd(target.handle)
                        if _sc_sel:
                            _SelC(_sc_host).store("type", _sc_ckey, _sc_sel,
                                                  signature=f"{_pending_ax_role}::{_pending_ax_name}")
            except Exception:
                pass
            logger.info(f"Type into element #{target_id} succeeded")

            await asyncio.sleep(0.4)

            if await browser._is_calendar_popup_visible():
                await page.keyboard.press("Tab")
                logger.info(
                    f"[TYPE] Date picker calendar detected → dismissed with Tab. "
                    f"Next screenshot will show clean input state."
                )
            else:
                appeared = await browser._wait_for_submenu(count_before, max_wait=2.5)
                if appeared:
                    logger.info(
                        f"[TYPE] Async dropdown items appeared after input. "
                        f"Next screenshot will capture the option list for VLM to click."
                    )

        except Exception as e:
            browser._last_action_error = e
            logger.error(f"Type into element #{target_id} failed: {e}")

        await browser._wait_after_action(light_action=True)
        return None


@ActionRegistry.register("hover")
class HoverHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing hover: element #{target_id} ({selector})")
        try:
            await browser._clear_som_overlays()
            target = await browser._resolve_action_target(target_id, "click")
            if not target:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
                )
            await target.handle.scroll_into_view_if_needed(
                timeout=browser._LOCATOR_TIMEOUT
            )
            _pending_xpath = await browser._get_xpath(target.handle)
            _pending_ax_role, _pending_ax_name = await browser._get_accessibility_signature(
                page, target.handle
            )

            count_before = await browser._count_interactive_elements()

            await target.handle.hover(force=True, timeout=browser._LOCATOR_TIMEOUT)
            logger.info(f"Hover element #{target_id} succeeded")
            _hover_meta = None
            if _pending_xpath:
                _hover_meta = ctx.with_rpa_meta({
                    "action": "hover",
                    "xpath": _pending_xpath,
                    "ax_role": _pending_ax_role or "",
                    "ax_name": _pending_ax_name or "",
                    "type_value": "",
                })
                browser.rpa_trail.append(_hover_meta)

            appeared = await browser._wait_for_submenu(count_before, max_wait=2.0)
            if appeared:
                logger.info(
                    f"[HOVER] Submenu/dropdown appeared after hovering #{target_id}"
                )
                try:
                    tooltip_text = await page.evaluate(
                        """() => {
                            const selectors = [
                                '[role="tooltip"]',
                                '.el-tooltip__popper',
                                '.el-popper',
                                '.ant-tooltip-inner',
                                '.tooltip',
                                '.v-popper__inner'
                            ];
                            const seen = new Set();
                            const out = [];
                            for (const sel of selectors) {
                                for (const el of document.querySelectorAll(sel)) {
                                    const rect = el.getBoundingClientRect();
                                    const style = window.getComputedStyle(el);
                                    if (
                                        rect.width > 0 && rect.height > 0 &&
                                        style.visibility !== 'hidden' &&
                                        style.display !== 'none' &&
                                        Number(style.opacity || 1) > 0
                                    ) {
                                        const text = (el.innerText || el.textContent || '').trim();
                                        if (text && !seen.has(text)) {
                                            seen.add(text);
                                            out.push(text);
                                        }
                                    }
                                }
                            }
                            return out.join(' | ');
                        }"""
                    )
                    if tooltip_text:
                        logger.info("[HOVER] visible tooltip text: %s", tooltip_text)
                        if isinstance(_hover_meta, dict):
                            _hover_meta["tooltip_text"] = tooltip_text
                        # Surface tooltip text to next VLM step. Without
                        # this, the tooltip is gone by the next screenshot
                        # (mouse moves to SoM sampler) and VLM re-hovers.
                        try:
                            _trimmed = tooltip_text[:200]
                            _tooltip_notice = (
                                f"ℹ️ [TOOLTIP READ] hover ＃{target_id} "
                                f"显示 tooltip: {_trimmed!r}\n"
                                "→ 该文本已被系统读取。下一步可直接 extract / done / "
                                "需要在上下文中使用 tooltip 文本，不要重复 hover 同一元素。"
                            )
                            if not browser._tab_switch_notice:
                                # J: route through set_tab_notice so the
                                # severity slot stays in sync; guard is kept
                                # so a richer Tab Guard notice still wins.
                                browser.set_tab_notice(
                                    _tooltip_notice, severity="info", coalesce=False,
                                )
                        except Exception:
                            pass
                except Exception as _tooltip_err:
                    logger.debug("[HOVER] tooltip text capture skipped: %s", _tooltip_err)
            else:
                logger.warning(
                    f"[HOVER TIMEOUT] No new elements appeared after hovering "
                    f"#{target_id} within 2s — parent item may be incorrect or "
                    f"menu requires a click to open."
                )
                # Tooltips are often non-interactive poppers, so the interactive
                # element count can stay unchanged even when a tooltip is visible.
                try:
                    tooltip_text = await page.evaluate(
                        """() => {
                            const selectors = [
                                '[role="tooltip"]',
                                '.el-tooltip__popper',
                                '.el-popper[aria-hidden="false"]',
                                '.el-popper',
                                '.ant-tooltip-inner',
                                '.tooltip',
                                '.v-popper__inner'
                            ];
                            const seen = new Set();
                            const out = [];
                            for (const sel of selectors) {
                                for (const el of document.querySelectorAll(sel)) {
                                    const rect = el.getBoundingClientRect();
                                    const style = window.getComputedStyle(el);
                                    if (
                                        rect.width > 0 && rect.height > 0 &&
                                        style.visibility !== 'hidden' &&
                                        style.display !== 'none' &&
                                        Number(style.opacity || 1) > 0
                                    ) {
                                        const text = (el.innerText || el.textContent || '').trim();
                                        if (text && !seen.has(text)) {
                                            seen.add(text);
                                            out.push(text);
                                        }
                                    }
                                }
                            }
                            return out.join(' | ');
                        }"""
                    )
                    if tooltip_text:
                        logger.info("[HOVER] visible tooltip text: %s", tooltip_text)
                        if isinstance(_hover_meta, dict):
                            _hover_meta["tooltip_text"] = tooltip_text
                        # Surface tooltip text to next VLM step. Without
                        # this, the tooltip is gone by the next screenshot
                        # (mouse moves to SoM sampler) and VLM re-hovers.
                        try:
                            _trimmed = tooltip_text[:200]
                            _tooltip_notice = (
                                f"ℹ️ [TOOLTIP READ] hover ＃{target_id} "
                                f"显示 tooltip: {_trimmed!r}\n"
                                "→ 该文本已被系统读取。下一步可直接 extract / done / "
                                "需要在上下文中使用 tooltip 文本，不要重复 hover 同一元素。"
                            )
                            if not browser._tab_switch_notice:
                                # J: route through set_tab_notice so the
                                # severity slot stays in sync; guard is kept
                                # so a richer Tab Guard notice still wins.
                                browser.set_tab_notice(
                                    _tooltip_notice, severity="info", coalesce=False,
                                )
                        except Exception:
                            pass
                except Exception as _tooltip_err:
                    logger.debug("[HOVER] tooltip text capture skipped: %s", _tooltip_err)
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"Hover element #{target_id} failed: {e}")

        await browser._wait_after_action(light_action=True)
        return None



from __future__ import annotations

import asyncio
import json
import logging
import re
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


"""PressKey / Select / DragAndDrop / RemoveElement handlers"""

@ActionRegistry.register("press_key")
class PressKeyHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        key_name = (ctx.action.type_value or "").strip()
        if not key_name:
            raise ActionExecutionError(
                "press_key 动作必须在 type_value 中提供按键名称（如 'Enter'、'Escape'、'Tab'）。"
            )
        logger.info(f"Executing press_key: {key_name!r} (target_id={target_id})")

        _atomic_done = False
        if target_id and int(target_id) > 0:
            try:
                await browser._clear_som_overlays()
                _pk_target = await browser._resolve_action_target(target_id, "click")
                if _pk_target:
                    await _pk_target.handle.press(
                        key_name, timeout=browser._LOCATOR_TIMEOUT
                    )
                    _atomic_done = True
                    logger.info(
                        f"[press_key] 原子级按键成功："
                        f"元素 #{target_id} + {key_name!r}"
                    )
                else:
                    logger.warning(
                        f"[press_key] 元素 #{target_id} 未找到，降级为全局按键"
                    )
            except Exception as _atomic_err:
                logger.warning(
                    f"[press_key] 原子级按键失败: {_atomic_err}，降级为全局按键"
                )

        if not _atomic_done:
            try:
                await page.keyboard.press(key_name)
                logger.info(f"Press key {key_name!r} (global) succeeded")
            except Exception as e:
                browser._last_action_error = e
                logger.error(f"Press key {key_name!r} failed: {e}")

        browser.rpa_trail.append(
            ctx.with_rpa_meta({"action": "press_key", "type_value": key_name})
        )
        logger.debug(f"[RPA] Recorded press_key: {key_name!r}")
        await browser._wait_after_action(is_navigation=(key_name == "Enter"))
        return None


@ActionRegistry.register("select")
class SelectHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        type_value = ctx.action.type_value
        selector = f'[data-som-id="{target_id}"]'
        logger.info(
            f"Executing select: element #{target_id} ({selector}) <- {type_value!r}"
        )
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
            _pending_xpath = await browser._get_xpath(target.handle)
            _pending_ax_role, _pending_ax_name = await browser._get_accessibility_signature(
                page, target.handle
            )
            selected_via = "label"
            try:
                await target.handle.select_option(
                    label=type_value, timeout=browser._LOCATOR_TIMEOUT
                )
            except Exception:
                selected_via = "value"
                await target.handle.select_option(
                    value=type_value, timeout=browser._LOCATOR_TIMEOUT
                )
            # readback evidence: verify the option actually selected matches
            # what was requested — never trust select_option's return alone.
            readback = await target.handle.evaluate(
                """el => {
                    if (!el || el.tagName !== 'SELECT') return null;
                    const opt = el.selectedOptions && el.selectedOptions[0];
                    return {
                        text: opt ? (opt.label || opt.textContent || '').trim() : '',
                        value: el.value != null ? String(el.value) : '',
                        index: el.selectedIndex,
                    };
                }"""
            )
            observed_text = ""
            observed_value = ""
            if isinstance(readback, dict):
                observed_text = str(readback.get("text") or "").strip()
                observed_value = str(readback.get("value") or "").strip()
                expected = (type_value or "").strip()
                matched = bool(expected) and (
                    observed_text == expected
                    or observed_value == expected
                    or (expected and expected in observed_text)
                )
                if not matched:
                    raise RuntimeError(
                        f"select readback mismatch on element #{target_id}: "
                        f"expected {type_value!r}, observed "
                        f"text={observed_text!r} value={observed_value!r}"
                    )
            logger.info(
                f"Select element #{target_id} succeeded via {selected_via} "
                f"(readback text={observed_text!r} value={observed_value!r})"
            )
            if _pending_xpath:
                browser.rpa_trail.append(
                    ctx.with_rpa_meta({
                        "action": "select",
                        "xpath": _pending_xpath,
                        "ax_role": _pending_ax_role or "",
                        "ax_name": _pending_ax_name or "",
                        "type_value": type_value,
                        "method": selected_via,
                        "verified": True,
                        "observed": observed_text or observed_value,
                    })
                )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"Select element #{target_id} failed: {e}")
        await browser._wait_after_action(is_navigation=False)
        return None


@ActionRegistry.register("drag_and_drop")
class DragAndDropHandler(ActionHandler):
    """drag_and_drop v2 — supports text-located destinations.

    type_value may be:
      * a SoM ID string (digits)         e.g. "42"
      * a text-locator     ``text:<txt>`` e.g. "text:In Progress"

    For text targets we resolve the destination via cross-frame
    ``get_by_text`` (most-specific match wins by shortest text length),
    matching the same heuristic used by row_action / extract_row.
    """

    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        type_value = (ctx.action.type_value or "").strip()
        # ── Parse destination spec ────────────────────────────────────
        is_text_dest = type_value.lower().startswith("text:")
        dest_text = ""
        drop_id = 0
        if is_text_dest:
            dest_text = type_value.split(":", 1)[1].strip()
            if not dest_text:
                raise RuntimeError(
                    "drag_and_drop: type_value 'text:' 后必须有目标可见文本"
                )
        else:
            try:
                drop_id = int(type_value) if type_value else 0
            except ValueError:
                raise RuntimeError(
                    f"drag_and_drop: type_value 必须是 SoM ID (数字) 或 'text:<文本>' "
                    f"形式，得到 {type_value!r}"
                )
        logger.info(
            "Executing drag_and_drop: #%s → %s",
            target_id,
            f"text={dest_text!r}" if is_text_dest else f"#{drop_id}",
        )
        try:
            await browser._clear_som_overlays()
            source = await browser._resolve_action_target(target_id, "drag_and_drop")
            if not source:
                raise RuntimeError(f"Drag source element #{target_id} not found")

            await source.handle.scroll_into_view_if_needed(
                timeout=browser._LOCATOR_TIMEOUT
            )
            _src_xpath = await browser._get_xpath(source.handle)
            _src_ax_role, _src_ax_name = await browser._get_accessibility_signature(
                page, source.handle
            )

            # ── Resolve destination handle ────────────────────────────
            dest_handle = None
            _dst_xpath = ""
            if is_text_dest:
                # Cross-frame text search — pick most-specific (shortest) match.
                try:
                    candidate_frames = list(page.frames)
                except Exception:
                    candidate_frames = [page]
                try:
                    mf = page.main_frame
                    if mf in candidate_frames:
                        candidate_frames.sort(key=lambda f: 0 if f is mf else 1)
                except Exception:
                    pass
                for frame in candidate_frames:
                    try:
                        loc = frame.get_by_text(dest_text, exact=False)
                    except Exception:
                        continue
                    try:
                        n = await loc.count()
                    except Exception:
                        continue
                    if n == 0:
                        continue
                    best_idx = 0
                    if n > 1:
                        lengths = []
                        for i in range(min(n, 10)):
                            try:
                                txt = await loc.nth(i).inner_text()
                                lengths.append((i, len((txt or "").strip())))
                            except Exception:
                                lengths.append((i, 10_000))
                        lengths.sort(key=lambda x: x[1])
                        best_idx = lengths[0][0]
                    cand = loc.nth(best_idx)
                    try:
                        dest_handle = await cand.element_handle()
                    except Exception:
                        dest_handle = None
                    if dest_handle is not None:
                        try:
                            _dst_xpath = await browser._get_xpath(dest_handle)
                        except Exception:
                            _dst_xpath = ""
                        break
                if dest_handle is None:
                    raise RuntimeError(
                        f"Drop target text {dest_text!r} not found in any frame"
                    )
            else:
                dest = await browser._resolve_action_target(drop_id, "drag_and_drop")
                if not dest:
                    raise RuntimeError(f"Drop target element #{drop_id} not found")
                dest_handle = dest.handle
                try:
                    _dst_xpath = await browser._get_xpath(dest_handle)
                except Exception:
                    _dst_xpath = ""

            await source.handle.drag_to(dest_handle, timeout=browser._LOCATOR_TIMEOUT)
            _dest_label = (
                f"text={dest_text!r}" if is_text_dest else f"#{drop_id}"
            )
            logger.info(f"drag_and_drop #{target_id} → {_dest_label} succeeded")
            if _src_xpath:
                browser.rpa_trail.append(
                    ctx.with_rpa_meta({
                        "action": "drag_and_drop",
                        "xpath": _src_xpath,
                        "ax_role": _src_ax_role or "",
                        "ax_name": _src_ax_name or "",
                        "type_value": type_value or str(drop_id),
                        "drop_xpath": _dst_xpath or "",
                        "dest_text": dest_text if is_text_dest else "",
                    })
                )
            try:
                _notice = (
                    f"✅ [DRAG DONE] #{target_id} → {_dest_label}\n"
                    "→ 下一步看页面状态确认 drop 后效果。"
                )
                if not browser._tab_switch_notice:
                    # J: route through set_tab_notice so _last_notice_severity
                    # stays in sync with the legacy _tab_switch_notice slot.
                    browser.set_tab_notice(_notice, severity="info", coalesce=False)
            except Exception:
                pass
        except Exception as e:
            browser._last_action_error = e
            _dest_label = (
                f"text={dest_text!r}" if is_text_dest else f"#{drop_id}"
            )
            logger.error(f"drag_and_drop #{target_id} → {_dest_label} failed: {e}")

        await browser._wait_after_action(light_action=False)
        return None


@ActionRegistry.register("remove_element")
class RemoveElementHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"[REMOVE_ELEMENT] Removing element #{target_id} ({selector})")
        try:
            await browser._clear_som_overlays()
            target = await browser._resolve_action_target(target_id, "remove_element")
            if not target:
                raise RuntimeError(
                    f"Element #{target_id} not found — cannot remove"
                )
            _pending_xpath = await browser._get_xpath(target.handle)
            _pending_ax_role, _pending_ax_name = await browser._get_accessibility_signature(
                page, target.handle
            )
            await target.handle.evaluate("""el => {
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('opacity', '0', 'important');
                el.style.setProperty('pointer-events', 'none', 'important');
                el.setAttribute('aria-hidden', 'true');
                el.inert = true;
            }""")
            logger.info(f"[REMOVE_ELEMENT] Element #{target_id} semantically hidden")
            print(
                f"🗑️  [REMOVE_ELEMENT] 已语义隐身元素 #{target_id} "
                f"(xpath={_pending_xpath})"
            )
            if _pending_xpath:
                browser.rpa_trail.append(
                    ctx.with_rpa_meta({
                        "action": "remove_element",
                        "xpath": _pending_xpath,
                        "ax_role": _pending_ax_role or "",
                        "ax_name": _pending_ax_name or "",
                        "type_value": "",
                    })
                )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"[REMOVE_ELEMENT] Failed for element #{target_id}: {e}")
        return None



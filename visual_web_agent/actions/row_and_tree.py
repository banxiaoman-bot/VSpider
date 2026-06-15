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


"""RowAction / ExtractRow / TreeCheck / SetPromptResponse / SwitchTab handlers"""

@ActionRegistry.register("row_action")
class RowActionHandler(ActionHandler):
    """Locate a row by visible text, then click an in-row button by its text.

    # ROW_ACTION_V2  -- enhanced with: cross-frame search, auto-confirm,
    # and canvas-fallback hinting.

    Why this exists
    ===============
    "Delete the row where name='Li Si'" is one of the most common tabular
    intents AND one of the most common ways VLMs fail: every row has its
    own "Delete" button with a different SoM red-box ID, and VLM picks the
    wrong one by visual order (which doesn't match DOM order).

    Schema
    ------
        target_id   = 0  (ignored - row & button are both text-located)
        type_value  = "<row filter>||<button text>[||confirm]"
                       e.g. "Li Si||Delete"
                            "ORD-2024-001||View"
                            "Li Si||Delete||confirm"   <- auto-confirm modal

    Where the 3rd segment, if present and one of
    ``confirm/yes/ok/true/1/确认/确定``, triggers an
    additional click on the post-action confirm modal's "OK" button.

    Implementation (deterministic, no VLM in the loop):
      1. Iterate ``page.frames`` (top + all iframes - admin panels matter).
      2. For each frame, try a chain of row selectors
         (``tr`` / ``[role=row]`` / Element / Ant / Naive / generic).
      3. ``locator.filter(has_text=<row>)`` to lock onto the matching row.
         Multiple matches -> pick the row with the *shortest* total text
         (closest to "just the filter").
      4. ``locator(button|a|[role=button]).filter(has_text=<btn>)`` for
         the in-row action.
      5. Click using the project's standard JS-fallback wrapper.
      6. If ``auto_confirm`` was requested, wait briefly for a modal to
         render in the SAME frame, then click its OK button.
      7. On total failure, probe for large ``canvas`` / ``svg`` elements
         and append a data_export hint to the error message.
    """

    # Class-level constants so tests can monkeypatch them.
    _ROW_SELECTORS = (
        "tr",
        "[role=row]",
        ".el-table__row",
        ".ant-table-row",
        ".n-data-table-tr",
        ".table-row",
    )
    _BUTTON_SELECTORS = (
        "button",
        "a",
        "[role=button]",
        ".el-button",
        ".ant-btn",
        ".n-button",
        "[data-action]",
    )
    # Modal / message-box / Popconfirm scopes searched for the OK button
    # after a row click. Order matters: more-specific first.
    _CONFIRM_MODAL_SCOPES = (
        ".el-message-box",
        ".el-message-box__btns",
        ".ant-modal-confirm",
        ".ant-modal-confirm-btns",
        ".ant-popover-buttons",
        ".el-popconfirm",
        "[role=alertdialog]",
        "[role=dialog]",
        ".n-modal",
        ".v-dialog",
        ".modal.show",  # Bootstrap
    )
    _CONFIRM_LABELS = (
        # In priority order: exact CN first, then EN. Native fallthrough
        # to "Yes" handles Element/Ant/Naive whose i18n strings vary.
        "确定", "确认", "提交", "是",
        "OK", "Ok", "Yes", "Confirm", "Submit",
    )
    _AUTO_CONFIRM_TOKENS = frozenset({
        "confirm", "yes", "ok", "true", "1",
        "确认", "确定", "y",
    })

    async def execute(self, ctx: "ActionContext") -> "Optional[Page]":
        browser = ctx.browser
        page = ctx.page
        raw = (ctx.action.type_value or "").strip()
        if not page:
            raise ActionExecutionError("row_action: 无活动页面。")
        if "||" not in raw:
            raise ActionExecutionError(
                "row_action 必须用 '||' 分隔行筛选文本和按钮文字，"
                f"如 '张三||删除' 或 '张三||删除||confirm'。"
                f"当前 type_value={raw!r}"
            )
        parts = [p.strip() for p in raw.split("||")]
        if len(parts) < 2 or len(parts) > 3:
            raise ActionExecutionError(
                "row_action 期望 '行筛选||按钮文字' 或 "
                "'行筛选||按钮文字||confirm'，"
                f"得到 {len(parts)} 段: {raw!r}"
            )
        row_filter = parts[0]
        btn_text = parts[1]
        auto_confirm = (
            len(parts) == 3
            and parts[2].lower() in self._AUTO_CONFIRM_TOKENS
        )
        if not row_filter or not btn_text:
            raise ActionExecutionError(
                "row_action: 行筛选文本和按钮文字都不能为空。"
                f"当前: row={row_filter!r} button={btn_text!r}"
            )

        await browser._clear_som_overlays()

        # ── Step 1-5: locate + click ─────────────────────────────────
        clicked_path = ""
        used_frame = None
        # page.frames includes the main frame; iterate them all.
        try:
            candidate_frames = list(page.frames)
        except Exception:
            candidate_frames = [page]
        # Order: main frame first (most common); the rest in document order.
        try:
            main_frame = page.main_frame
            if main_frame in candidate_frames:
                candidate_frames.sort(key=lambda f: 0 if f is main_frame else 1)
        except Exception:
            pass

        for frame in candidate_frames:
            for row_sel in self._ROW_SELECTORS:
                try:
                    row_loc = frame.locator(row_sel).filter(has_text=row_filter)
                except Exception:
                    continue
                try:
                    row_count = await row_loc.count()
                except Exception:
                    continue
                if row_count == 0:
                    continue
                best_idx = 0
                if row_count > 1:
                    lengths = []
                    for i in range(min(row_count, 10)):
                        try:
                            txt = await row_loc.nth(i).inner_text()
                            lengths.append((i, len((txt or "").strip())))
                        except Exception:
                            lengths.append((i, 10_000))
                    lengths.sort(key=lambda x: x[1])
                    best_idx = lengths[0][0]
                    logger.info(
                        "[ROW_ACTION] %d rows matched filter %r in frame=%r %s; "
                        "picked row index %d (shortest text)",
                        row_count, row_filter,
                        getattr(frame, "url", "?")[:60], row_sel, best_idx,
                    )
                target_row = row_loc.nth(best_idx)
                try:
                    await target_row.scroll_into_view_if_needed(
                        timeout=browser._LOCATOR_TIMEOUT
                    )
                except Exception:
                    pass
                for btn_sel in self._BUTTON_SELECTORS:
                    try:
                        btn = target_row.locator(btn_sel).filter(has_text=btn_text)
                        if await btn.count() == 0:
                            continue
                        cand = btn.first
                        if not await cand.is_visible():
                            continue
                        mode = await _click_locator_with_js_fallback(
                            cand,
                            f"row_action frame={getattr(frame, 'url', '?')[:40]!r} "
                            f"row={row_sel} filter={row_filter!r} "
                            f"btn={btn_sel} text={btn_text!r}",
                            timeout=3000,
                        )
                        clicked_path = (
                            f"frame={getattr(frame, 'url', 'main')[:60]!r} "
                            f"{row_sel}.has_text({row_filter!r})[{best_idx}] "
                            f"-> {btn_sel}.has_text({btn_text!r}) ({mode})"
                        )
                        used_frame = frame
                        break
                    except Exception as click_err:
                        logger.debug(
                            "[ROW_ACTION] %s.filter(%r) %s.filter(%r) failed: %s",
                            row_sel, row_filter, btn_sel, btn_text, click_err,
                        )
                        continue
                if clicked_path:
                    break
            if clicked_path:
                break

        if not clicked_path:
            # ── Step 7: canvas/svg fallback hint ─────────────────────
            canvas_hint = ""
            try:
                has_canvas_table = await page.evaluate(
                    """() => {
                        const els = [
                            ...document.querySelectorAll('canvas'),
                            ...document.querySelectorAll('svg')
                        ];
                        return els.some(el => {
                            const r = el.getBoundingClientRect();
                            return r.width >= 500 && r.height >= 300;
                        });
                    }"""
                )
            except Exception:
                has_canvas_table = False
            if has_canvas_table:
                canvas_hint = (
                    "\n\n💡 检测到页面含大尺寸 canvas/svg，目标"
                    "表格可能是 canvas/svg 渲染（如 AntV / "
                    "ECharts / Handsontable canvas-mode / PDF preview）。"
                    "row_action 只能操作 DOM 节点，请改用 data_export skill，"
                    "或在原页找一个「导出 CSV / 复制表格」按钮手动触发下载。"
                )
            raise ActionExecutionError(
                f"row_action: 在页面所有 frame 中找不到含 "
                f"{row_filter!r} 的行 + 行内按钮 {btn_text!r}。"
                "检查行筛选文本是否完全可见、按钮文字是否完整匹配。"
                + canvas_hint
            )

        # ── Step 6: auto-confirm a follow-up Modal/MessageBox ────────
        auto_confirm_status = ""
        if auto_confirm:
            try:
                # Brief animation pause; el-message-box typically opens in
                # 100-200ms, ant-modal-confirm in 200-300ms.
                import asyncio as _asyncio
                await _asyncio.sleep(0.4)
                # First scan the click frame, then the main page frames.
                confirm_frames = []
                if used_frame is not None:
                    confirm_frames.append(used_frame)
                try:
                    for f in page.frames:
                        if f is not used_frame:
                            confirm_frames.append(f)
                except Exception:
                    pass

                confirmed_with = ""
                for cf in confirm_frames:
                    for scope_sel in self._CONFIRM_MODAL_SCOPES:
                        for label in self._CONFIRM_LABELS:
                            try:
                                # Button (or [role=button]) within the modal scope
                                btn = (
                                    cf.locator(scope_sel)
                                    .locator("button, [role=button], .el-button, .ant-btn")
                                    .filter(has_text=label)
                                )
                                if await btn.count() == 0:
                                    continue
                                cand = btn.first
                                if not await cand.is_visible():
                                    continue
                                await _click_locator_with_js_fallback(
                                    cand,
                                    f"row_action auto_confirm scope={scope_sel} label={label!r}",
                                    timeout=2000,
                                )
                                confirmed_with = (
                                    f"{scope_sel} > '{label}'"
                                )
                                break
                            except Exception:
                                continue
                        if confirmed_with:
                            break
                    if confirmed_with:
                        break

                if confirmed_with:
                    auto_confirm_status = (
                        f"; auto_confirm OK ({confirmed_with})"
                    )
                else:
                    # Could be a native confirm() - check the recently-captured
                    # dialog text. If so, we're already done (listener accepted it).
                    last_dialog = getattr(browser, "_last_native_dialog", None)
                    if last_dialog and last_dialog.get("type") in {"confirm", "alert"}:
                        auto_confirm_status = (
                            "; auto_confirm: 原生 confirm() 已被系统 accept"
                        )
                    else:
                        auto_confirm_status = (
                            "; auto_confirm: 未发现确认弹窗"
                            "（可能不需要确认，或使用了非标准 modal）"
                        )
            except Exception as confirm_err:
                logger.warning(
                    "[ROW_ACTION] auto_confirm phase error: %s", confirm_err
                )
                auto_confirm_status = f"; auto_confirm error: {confirm_err}"

        logger.info("[ROW_ACTION] ✅ %s%s", clicked_path, auto_confirm_status)
        print(
            f"\033[1;35m🎯 [ROW_ACTION]\033[0m "
            f"row={row_filter!r} → click {btn_text!r}{auto_confirm_status}"
        )
        browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "row_action",
                "row_filter": row_filter,
                "button_text": btn_text,
                "auto_confirm": auto_confirm,
                "strategy": clicked_path,
                "confirm_status": auto_confirm_status.lstrip("; "),
            })
        )
        # Surface to VLM next turn so it knows the destructive op completed
        # AND whether a follow-up confirm was already handled here.
        try:
            _notice = (
                f"✅ [ROW_ACTION DONE] row={row_filter!r} "
                f"button={btn_text!r}"
                + (f" {auto_confirm_status.strip(';').strip()}" if auto_confirm_status else "")
                + "\n→ 下一步请看页面状态确认结果"
                + ("。如果 modal 还在，手动 click_text 确定按钮。" if not auto_confirm or "OK" not in auto_confirm_status else "。")
            )
            if not browser._tab_switch_notice:
                # J: route through set_tab_notice so _last_notice_severity
                # stays in sync with the legacy _tab_switch_notice slot.
                # Guard is preserved (skip-if-prior) — coalesce flag is
                # therefore moot but kept explicit for documentation.
                browser.set_tab_notice(_notice, severity="info", coalesce=False)
        except Exception:
            pass
        await browser._wait_after_action()
        return None


@ActionRegistry.register("extract_row")
class ExtractRowHandler(ActionHandler):
    """Read a value from a table row, write it to ``workflow_memory``.

    Symmetric counterpart to ``row_action`` (which CLICKS an in-row button).
    Use this when the goal is "tell me X for the row matching Y" rather than
    "do something destructive to that row".

    Schema
    ------
        target_id  = 0  (ignored - row & column are both text-located)
        type_value = "<row filter>||<column>"
                     - row filter: visible text uniquely identifying the row
                     - column: column header text (e.g. "Status", "状态")
                              OR ``*`` to capture the whole row's inner_text
        memory_key = (optional) target memory slot; auto-named if absent

    Examples
    --------
        {"action":"extract_row","type_value":"ORD-2024-001||状态",
         "memory_key":"order_status"}
        {"action":"extract_row","type_value":"alice@x.com||*",
         "memory_key":"alice_row_text"}

    Implementation
    --------------
    1. Iterate page.frames (same as row_action v2 - admin iframes matter).
    2. Locate the row via row-selector chain + ``filter(has_text=row)``.
    3. If column == "*", read ``inner_text()`` of the whole row.
       Otherwise: enumerate <th> in the row's parent table, find the
       header text matching the requested column (case-insensitive,
       trimmed), then read the cell at the same column index.
    4. Write to ``workflow_memory[memory_key]``; also update
       ``latest_memory`` for downstream save_to_memory chains.
    5. Push a ``[ROW EXTRACTED]`` notice to ``_tab_switch_notice`` so
       VLM doesn't re-attempt the read on the next turn.
    """

    _ROW_SELECTORS = RowActionHandler._ROW_SELECTORS

    async def execute(self, ctx: "ActionContext") -> "Optional[Page]":
        browser = ctx.browser
        page = ctx.page
        raw = (ctx.action.type_value or "").strip()
        if not page:
            raise ActionExecutionError("extract_row: 无活动页面。")
        if "||" not in raw:
            raise ActionExecutionError(
                "extract_row 必须用 '||' 分隔行筛选和列名，"
                f"如 'ORD-2024-001||状态' 或 'alice||*'。"
                f"当前 type_value={raw!r}"
            )
        parts = [p.strip() for p in raw.split("||")]
        if len(parts) != 2:
            raise ActionExecutionError(
                "extract_row 期望息严格 2 段（行筛选||列名），"
                f"得到 {len(parts)}: {raw!r}"
            )
        row_filter, column = parts[0], parts[1]
        if not row_filter or not column:
            raise ActionExecutionError(
                "extract_row: 行筛选文本和列名都不能为空。"
                f"row={row_filter!r} column={column!r}"
            )
        whole_row = (column == "*")

        await browser._clear_som_overlays()

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

        captured_text = ""
        captured_path = ""
        for frame in candidate_frames:
            for row_sel in self._ROW_SELECTORS:
                try:
                    row_loc = frame.locator(row_sel).filter(has_text=row_filter)
                except Exception:
                    continue
                try:
                    row_count = await row_loc.count()
                except Exception:
                    continue
                if row_count == 0:
                    continue
                best_idx = 0
                if row_count > 1:
                    lengths = []
                    for i in range(min(row_count, 10)):
                        try:
                            txt = await row_loc.nth(i).inner_text()
                            lengths.append((i, len((txt or "").strip())))
                        except Exception:
                            lengths.append((i, 10_000))
                    lengths.sort(key=lambda x: x[1])
                    best_idx = lengths[0][0]
                target_row = row_loc.nth(best_idx)
                try:
                    await target_row.scroll_into_view_if_needed(
                        timeout=browser._LOCATOR_TIMEOUT
                    )
                except Exception:
                    pass
                try:
                    if whole_row:
                        text = await target_row.inner_text()
                        captured_text = (text or "").strip()
                    else:
                        # Column-by-header lookup. Run in-frame to keep all
                        # heuristics atomic w.r.t. DOM mutations.
                        cell_text = await target_row.evaluate(
                            """(rowEl, colName) => {
                                const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                const target = norm(colName);
                                let table = rowEl.closest('table');
                                let headers = [];
                                if (table) {
                                    headers = Array.from(table.querySelectorAll('thead th'));
                                    if (!headers.length) {
                                        const firstRow = table.querySelector('tr');
                                        if (firstRow) headers = Array.from(firstRow.querySelectorAll('th, td'));
                                    }
                                }
                                if (!headers.length) {
                                    const grid = rowEl.closest('[role=grid], [role=table]');
                                    if (grid) {
                                        const headerRow = grid.querySelector('[role=rowheader]')
                                            || grid.querySelector('[role=row]');
                                        if (headerRow) headers = Array.from(headerRow.querySelectorAll('[role=columnheader], [role=cell]'));
                                    }
                                }
                                let colIdx = headers.findIndex(h => norm(h.innerText || h.textContent) === target);
                                if (colIdx === -1) {
                                    colIdx = headers.findIndex(h => norm(h.innerText || h.textContent).includes(target));
                                }
                                if (colIdx === -1) return null;
                                const cells = Array.from(rowEl.querySelectorAll(':scope > td, :scope > [role=cell], :scope > [role=gridcell]'));
                                if (!cells.length) return null;
                                if (colIdx >= cells.length) return null;
                                return (cells[colIdx].innerText || cells[colIdx].textContent || '').trim();
                            }""",
                            column,
                        )
                        if cell_text is not None:
                            captured_text = cell_text
                    captured_path = (
                        f"frame={getattr(frame, 'url', 'main')[:60]!r} "
                        f"{row_sel}.has_text({row_filter!r})[{best_idx}]"
                        f"::{column!r}"
                    )
                    break
                except Exception as read_err:
                    logger.debug(
                        "[EXTRACT_ROW] read failed in %s: %s", row_sel, read_err
                    )
                    continue
            if captured_path:
                break

        if not captured_path:
            raise ActionExecutionError(
                f"extract_row: 在页面所有 frame 中找不到含 "
                f"{row_filter!r} 的行。检查行筛选文本是否可见。"
            )
        if not whole_row and not captured_text:
            raise ActionExecutionError(
                f"extract_row: 找到了行，但列 {column!r} 未在表头里匹配。"
                "请在表头中查看列标题原文（如 '下单时间' vs '创建时间'）。"
            )

        # Memory writeback
        memory_key = (getattr(ctx.action, "memory_key", "") or "").strip()
        if not memory_key:
            # Auto-name from row_filter; alphanumeric + underscore.
            normalised = "".join(
                c if c.isalnum() else "_" for c in row_filter[:32]
            ).strip("_") or "row"
            col_part = "all" if whole_row else "".join(
                c if c.isalnum() else "_" for c in column[:16]
            ).strip("_") or "col"
            memory_key = f"row_{normalised}_{col_part}"
            logger.info("[EXTRACT_ROW] memory_key auto-named: %s", memory_key)

        if ctx.workflow_memory is not None:
            try:
                ctx.workflow_memory[memory_key] = captured_text
                ctx.workflow_memory["latest_memory"] = captured_text[:200]
            except Exception as mem_err:
                logger.warning("[EXTRACT_ROW] memory write failed: %s", mem_err)

        logger.info(
            "[EXTRACT_ROW] ✅ row=%r col=%r path=%s -> memory[%s] (%d chars)",
            row_filter, column, captured_path, memory_key, len(captured_text),
        )
        print(
            f"\033[1;36m✂️  [EXTRACT_ROW]\033[0m "
            f"row={row_filter!r} col={column!r} → memory[\033[33m{memory_key}\033[0m] "
            f"({len(captured_text)} 字)"
        )

        browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "extract_row",
                "row_filter": row_filter,
                "column": column,
                "memory_key": memory_key,
                "strategy": captured_path,
                "captured_chars": len(captured_text),
            })
        )
        try:
            preview = captured_text[:120]
            if len(captured_text) > 120:
                preview += "…"
            _notice = (
                f"✂️ [ROW EXTRACTED] row={row_filter!r} "
                f"col={column!r} → memory[{memory_key}]\n"
                f"  · 值: {preview!r}\n"
                "→ 下一步可用 {{" + memory_key + "}} 模板引用，不要重复 extract_row。"
            )
            if not browser._tab_switch_notice:
                # J: route through set_tab_notice so _last_notice_severity
                # stays in sync with the legacy _tab_switch_notice slot.
                # Guard is preserved (skip-if-prior) — coalesce flag is
                # therefore moot but kept explicit for documentation.
                browser.set_tab_notice(_notice, severity="info", coalesce=False)
        except Exception:
            pass

        await browser._wait_after_action(light_action=True)
        return None


@ActionRegistry.register("tree_check")
class TreeCheckHandler(ActionHandler):
    """Toggle a checkbox in a tree-view by visible node text.

    Symmetric counterpart to ``click_text`` for tree controls. The native
    impulse to ``click_text "Settings"`` on a checkbox tree HITS the label
    (which usually toggles selection but not check state) and leaves the
    actual checkbox alone in many UI kits.

    Schema
    ------
        target_id  = 0  (ignored - node is text-located)
        type_value = "<node text>"            (default: check)
                   | "<node text>||check"
                   | "<node text>||uncheck"

    Examples
    --------
        {"action":"tree_check","type_value":"设置"}
        {"action":"tree_check","type_value":"Settings||check"}
        {"action":"tree_check","type_value":"Frontend||uncheck"}

    Implementation
    --------------
    1. Iterate page.frames (cross-iframe).
    2. Locate the tree node by text within a tree-node selector chain
       (``[role=treeitem]`` / ``.el-tree-node`` / ``.ant-tree-treenode``
       / ``.n-tree-node``).
    3. Within that node, find the FIRST checkbox-like element via JS
       (covers ``input[type=checkbox]``, ``[role=checkbox]``, and
       framework-specific wrappers like ``.el-checkbox__input``,
       ``.ant-tree-checkbox``).
    4. Read its current ``aria-checked`` / ``checked`` state. If the
       desired state is already set, no-op (idempotent).
    5. Otherwise click the checkbox using the JS-fallback wrapper.
    """

    _TREE_NODE_SELECTORS = (
        "[role=treeitem]",
        ".el-tree-node",
        ".ant-tree-treenode",
        ".n-tree-node",
        ".tree-node",
    )

    async def execute(self, ctx: "ActionContext") -> "Optional[Page]":
        browser = ctx.browser
        page = ctx.page
        raw = (ctx.action.type_value or "").strip()
        if not page:
            raise ActionExecutionError("tree_check: 无活动页面。")
        if not raw:
            raise ActionExecutionError(
                "tree_check: type_value 必须提供节点文本。"
            )
        parts = [p.strip() for p in raw.split("||")]
        if len(parts) > 2:
            raise ActionExecutionError(
                f"tree_check schema: '<节点文本>[||check|uncheck]'，得到 {len(parts)} 段: {raw!r}"
            )
        node_text = parts[0]
        op = parts[1].lower() if len(parts) == 2 else "check"
        if op not in {"check", "uncheck", "toggle"}:
            raise ActionExecutionError(
                f"tree_check op 必须是 check/uncheck/toggle，得到 {op!r}"
            )
        if not node_text:
            raise ActionExecutionError("tree_check: 节点文本不能为空。")

        await browser._clear_som_overlays()

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

        toggled_path = ""
        prior_state = None
        new_state = None
        for frame in candidate_frames:
            for node_sel in self._TREE_NODE_SELECTORS:
                try:
                    node_loc = frame.locator(node_sel).filter(has_text=node_text)
                except Exception:
                    continue
                try:
                    n = await node_loc.count()
                except Exception:
                    continue
                if n == 0:
                    continue
                best_idx = 0
                if n > 1:
                    lengths = []
                    for i in range(min(n, 10)):
                        try:
                            txt = await node_loc.nth(i).inner_text()
                            lengths.append((i, len((txt or "").strip())))
                        except Exception:
                            lengths.append((i, 10_000))
                    lengths.sort(key=lambda x: x[1])
                    best_idx = lengths[0][0]
                node = node_loc.nth(best_idx)
                try:
                    await node.scroll_into_view_if_needed(
                        timeout=browser._LOCATOR_TIMEOUT
                    )
                except Exception:
                    pass
                # Run JS to find checkbox + read current state in one call.
                try:
                    state_info = await node.evaluate(
                        """(nodeEl) => {
                            const cb = nodeEl.querySelector(
                                'input[type=checkbox], '
                                + '[role=checkbox], '
                                + '.el-checkbox__original, '
                                + '.el-checkbox__input, '
                                + '.ant-tree-checkbox, '
                                + '.n-checkbox-box__border'
                            );
                            if (!cb) return {found: false};
                            // Resolve the most-likely interactive ancestor.
                            const interactive = cb.closest(
                                '.el-checkbox, .ant-tree-checkbox, .n-checkbox, label'
                            ) || cb;
                            let checked = false;
                            if (cb.tagName === 'INPUT') {
                                checked = !!cb.checked;
                            } else {
                                const ariaChecked = (cb.getAttribute('aria-checked') || interactive.getAttribute('aria-checked') || '').toLowerCase();
                                if (ariaChecked === 'true') checked = true;
                                else if (ariaChecked === 'mixed') checked = true;
                                // Framework class probes
                                const cls = (cb.className || '') + ' ' + (interactive.className || '');
                                if (/is-checked|ant-tree-checkbox-checked|n-checkbox--checked/.test(cls)) checked = true;
                            }
                            return {found: true, checked};
                        }"""
                    )
                except Exception as state_err:
                    logger.debug(
                        "[TREE_CHECK] state read failed in %s: %s",
                        node_sel, state_err,
                    )
                    continue

                if not state_info or not state_info.get("found"):
                    # Node matched by text but no checkbox in it; skip to
                    # next selector / frame in case a different DOM stamp
                    # has the right shape.
                    continue
                prior_state = bool(state_info.get("checked"))
                want = (
                    True if op == "check"
                    else False if op == "uncheck"
                    else (not prior_state)  # toggle
                )
                if prior_state == want:
                    new_state = prior_state
                    toggled_path = (
                        f"frame={getattr(frame, 'url', 'main')[:60]!r} "
                        f"{node_sel}.has_text({node_text!r}) [no-op: already {prior_state}]"
                    )
                    break
                # Click the interactive element.
                try:
                    cb_loc = node.locator(
                        ".el-checkbox, "
                        ".ant-tree-checkbox, "
                        ".n-checkbox, "
                        "[role=checkbox], "
                        "label:has(input[type=checkbox]), "
                        "input[type=checkbox]"
                    ).first
                    await _click_locator_with_js_fallback(
                        cb_loc,
                        f"tree_check node={node_text!r} op={op}",
                        timeout=2500,
                    )
                    new_state = want
                    toggled_path = (
                        f"frame={getattr(frame, 'url', 'main')[:60]!r} "
                        f"{node_sel}.has_text({node_text!r}) "
                        f"{prior_state} → {want}"
                    )
                    break
                except Exception as click_err:
                    logger.debug(
                        "[TREE_CHECK] click failed in %s: %s", node_sel, click_err
                    )
                    continue
            if toggled_path:
                break

        if not toggled_path:
            raise ActionExecutionError(
                f"tree_check: 在页面所有 frame 中找不到包含 "
                f"{node_text!r} 且含 checkbox 的树节点。"
                "检查：节点文本是否可见？该树是否启用了 checkbox 模式？"
                "（如未启用，请改用 click_text 点节点名选中）"
            )

        logger.info("[TREE_CHECK] ✅ %s", toggled_path)
        print(
            f"\033[1;35m☑️  [TREE_CHECK]\033[0m "
            f"node={node_text!r} op={op} → state={new_state}"
        )
        browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "tree_check",
                "node_text": node_text,
                "op": op,
                "prior_state": prior_state,
                "new_state": new_state,
                "strategy": toggled_path,
            })
        )
        try:
            _notice = (
                f"☑️ [TREE CHECK DONE] node={node_text!r} "
                f"op={op}: {prior_state} → {new_state}\n"
                "→ 下一步请看页面是否需要采集多个节点，或点「确定」提交。"
            )
            if not browser._tab_switch_notice:
                # J: route through set_tab_notice so _last_notice_severity
                # stays in sync with the legacy _tab_switch_notice slot.
                # Guard is preserved (skip-if-prior) — coalesce flag is
                # therefore moot but kept explicit for documentation.
                browser.set_tab_notice(_notice, severity="info", coalesce=False)
        except Exception:
            pass

        await browser._wait_after_action(light_action=True)
        return None


@ActionRegistry.register("set_prompt_response")
class SetPromptResponseHandler(ActionHandler):
    """Pre-arm the response value for the NEXT native ``prompt()`` dialog.

    Native ``prompt()`` is synchronous — the dialog listener must
    accept/dismiss within milliseconds, and there's no way to ask
    VLM what value to inject mid-dialog. This action lets VLM stage
    a value first, then trigger the action that opens the prompt.

    Schema
    ------
        target_id  = 0  (ignored)
        type_value = the string to submit when the next prompt fires

    Behaviour
    ---------
    * The value is **one-shot**: consumed by the next prompt(), then
      cleared. Subsequent prompts fall back to auto-accept-empty.
    * If no prompt() actually fires before the next page navigation
      or task end, the value is silently discarded (no leak across
      tasks).
    * Pairs with ``confirm_dialog`` skill: emit
      ``set_prompt_response`` in the SAME action batch as the click
      that triggers the prompt — they execute sequentially.
    """

    async def execute(self, ctx: "ActionContext") -> "Optional[Page]":
        browser = ctx.browser
        value = ctx.action.type_value or ""
        browser._next_prompt_response = value
        logger.info(
            "[SET_PROMPT_RESPONSE] armed: %r (length=%d)",
            value[:80], len(value),
        )
        print(
            f"\033[1;36m🗒️  [PROMPT ARMED]\033[0m "
            f"value={value[:60]!r} (len={len(value)})"
        )
        browser.rpa_trail.append(
            ctx.with_rpa_meta({
                "action": "set_prompt_response",
                "type_value": value,
            })
        )
        try:
            _notice = (
                f"🗒️ [PROMPT ARMED] 下一个原生 prompt() 将自动提交: {value[:80]!r}\n"
                "→ 请接下来发出会触发 prompt() 的动作（如 click）。"
                "仅一次有效。"
            )
            if not browser._tab_switch_notice:
                # J: route through set_tab_notice so _last_notice_severity
                # stays in sync with the legacy _tab_switch_notice slot.
                # Guard is preserved (skip-if-prior) — coalesce flag is
                # therefore moot but kept explicit for documentation.
                browser.set_tab_notice(_notice, severity="info", coalesce=False)
        except Exception:
            pass
        return None


@ActionRegistry.register("switch_tab")
class SwitchTabHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        tab_index = ctx.action.target_id
        open_pages = [p for p in browser._context.pages if not p.is_closed()]
        if 0 <= tab_index < len(open_pages):
            target_page = open_pages[tab_index]

            await browser._register_page(
                target_page,
                reason=f"switch_tab[{tab_index}]",
                activate=True,
            )

            try:
                await target_page.bring_to_front()
            except Exception as _btf_err:
                logger.warning(f"[SWITCH TAB] bring_to_front 失败: {_btf_err}")

            await asyncio.sleep(0.5)
            try:
                await browser._page.wait_for_load_state(
                    "domcontentloaded", timeout=5000
                )
            except Exception:
                pass
            try:
                await browser._page.wait_for_load_state(
                    "networkidle", timeout=5000
                )
            except Exception:
                pass
            await asyncio.sleep(0.5)

            browser.rpa_trail.append(
                ctx.with_rpa_meta({"action": "switch_tab", "target_id": tab_index})
            )
            logger.debug(f"[RPA] Recorded switch_tab: {tab_index}")
            logger.info(
                f"[SWITCH TAB] Switched to tab [{tab_index}]: "
                f"{(browser._page.url or 'about:blank')[:80]}"
            )
            return browser._page  # 显式返回：dispatcher 跳过 Tab Guard
        else:
            raise ActionExecutionError(
                f"switch_tab 失败：标签页索引 {tab_index} 超出范围"
                f"（当前共 {len(open_pages)} 个标签页，有效索引 0~{len(open_pages)-1}）。"
            )


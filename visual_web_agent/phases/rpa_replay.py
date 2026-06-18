"""RPA replay engine extracted from main.py (Slice 1).

Functions for RPA trail compaction, cache payload management,
composite locator resolution, cross-system replay switching,
and the core _replay_rpa fast-path executor.
"""
import asyncio
import json
import logging
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..browser_env import BrowserEnv
    from ..vlm_client import VLMClient

logger = logging.getLogger("vspider.main")

_RPA_CACHE_DIR = Path(__file__).resolve().parent.parent / "rpa_cache"


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
            # Cross-system: identical actions in different planned systems are
            # NOT the same step -- merging them would drop a system hop.
            and (prev.get("system_id") or "") == (cur.get("system_id") or "")
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
        trail = _semanticize_rpa_trail(payload, "")
        return {
            "version": 1,
            "replayable": True,
            "reason": "",
            "trail": trail,
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
        source_goal = str(payload.get("source_goal", "") or meta["source_goal"])
        semantic_trail = _semanticize_rpa_trail(trail, source_goal)
        semantic_action = (
            str(semantic_trail[0].get("action") or "")
            if semantic_trail and len(semantic_trail) == 1 and isinstance(semantic_trail[0], dict)
            else ""
        )
        was_calendar_physical = bool(
            semantic_trail
            and len(semantic_trail) == 1
            and isinstance(semantic_trail[0], dict)
            and semantic_action == "date_pick"
        )
        replayable = bool(payload.get("replayable", True))
        reason = str(payload.get("reason", "") or "")
        reason_lower = reason.lower()
        if was_calendar_physical and (
            not replayable or "calendar date selection uses volatile cell positions" in reason
        ):
            replayable = True
            reason = ""
        if semantic_action == "cascader_pick" and (
            not replayable
            or "cascader/multi-level popups are dynamic and require live interaction" in reason_lower
            or "dynamic and require live interaction" in reason_lower
        ):
            replayable = True
            reason = ""
        _completes_task = bool(payload.get("completes_task", True))
        if _goal_is_form_fill(source_goal):
            _completes_task = _trail_completes_form_goal(source_goal, semantic_trail)

        return {
            "version": int(payload.get("version", 4) or 4),
            "replayable": replayable,
            "reason": reason,
            "trail": semantic_trail,
            "fail_count": int(payload.get("fail_count", 0) or 0),
            "max_failures": int(payload.get("max_failures", 2) or 2),
            "completes_task": _completes_task,
            "source_url": str(payload.get("source_url", "") or meta["source_url"]),
            "source_goal": source_goal,
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


async def _replay_switch_system(
    browser: "BrowserEnv",
    session_router,
    *,
    to_system_id: str,
    from_system_id: str = "",
    url: str = "",
    home_browser=None,
    home_system_id: str = "",
    user_data_dir_base: str = "",
    event_stream=None,
):
    """Switch the active browser to ``to_system_id`` mid RPA replay (RPA-XSYS).

    Mirrors the reactive loop's cross-system goto consumer: snapshot the current
    context's storage_state, ask the router for a switch directive, activate it
    (launch / rebind the target system's isolated pooled session), then land the
    rebound browser on ``url`` only if it isn't already there (A2: preserves the
    target's exact page state on a switch-back). Best-effort -- any failure
    returns the inputs unchanged so replay proceeds on the current browser.

    Returns ``(browser, home_browser, home_system_id)``: the (possibly new)
    active browser plus the home-system bookkeeping the caller threads forward.
    """
    try:
        full_state = None
        try:
            _ctx = getattr(browser, "_context", None)
            if _ctx is not None:
                full_state = await _ctx.storage_state()
        except Exception as _state_err:
            logger.debug("[RPA REPLAY] x-sys storage_state skipped: %s", _state_err)

        switch = session_router.acquire_for_switch(
            to_system_id=to_system_id,
            from_system_id=from_system_id,
            full_state=full_state,
        )
        if not switch.get("should_switch"):
            return browser, home_browser, home_system_id

        # 留痕 (mission §三/§四): mirror the reactive-loop goto consumer's
        # session_switch evidence so a cross-system hop during fast-path
        # replay is recorded in the run event_stream, not only the log.
        # Inner-guarded so an emit hiccup never aborts the switch;
        # event_stream None (flag off / no stream) -> no event.
        if event_stream is not None:
            try:
                event_stream.emit("session_switch", **switch)
            except Exception:
                pass

        if home_browser is None:
            home_browser = browser
            home_system_id = from_system_id or ""

        target_browser = await session_router.activate_switch(
            switch,
            url=url,
            user_data_dir_base=user_data_dir_base,
            full_state=full_state,
            home_system_id=home_system_id,
            home_browser=home_browser,
        )
        if target_browser is not None and target_browser is not browser:
            browser = target_browser
            try:
                browser._session_router = session_router
                browser._event_stream = event_stream
            except Exception:
                pass
            if event_stream is not None:
                try:
                    event_stream.emit(
                        "session_switch_activated",
                        run_id=switch.get("run_id", ""),
                        to_system_id=switch.get("to_system_id", ""),
                        to_system_name=switch.get("to_system_name", ""),
                        session_id=switch.get("session_id", ""),
                        via="rpa_replay",
                    )
                except Exception:
                    pass
            logger.info(
                "[RPA REPLAY] cross-system switch -> %s (session=%s)",
                switch.get("to_system_id", ""),
                switch.get("session_id", ""),
            )

        try:
            _page = getattr(browser, "_page", None)
            _current = getattr(browser, "current_url", "") or ""
            if _page is not None and url and session_router.should_renavigate(_current, url):
                await _page.goto(url, wait_until="domcontentloaded", timeout=30000)
                _stable = getattr(browser, "_wait_for_page_stable", None)
                if _stable is not None:
                    await _stable()
        except Exception as _nav_err:
            logger.debug("[RPA REPLAY] x-sys landing nav skipped: %s", _nav_err)

        return browser, home_browser, home_system_id
    except Exception as _switch_err:
        logger.debug("[RPA REPLAY] cross-system switch skipped: %s", _switch_err)
        return browser, home_browser, home_system_id


async def _replay_rpa(
    browser: "BrowserEnv",
    trail: list[dict],
    workflow_memory: dict | None = None,
    vlm: "VLMClient | None" = None,
    session_router=None,
    event_stream=None,
) -> bool:
    """
    极速 RPA 回放：直接用 XPath/坐标执行缓存动作，完全跳过 VLM。

    vlm 仅作为语义宏（date_pick 等）postcheck 的慢路径兜底；
    传 None 时退化为纯 JS 校验。

    Returns:
        True  → 全程无报错，任务完成
        False → 任意步骤失败，需降级回 VLM 主循环
    """
    from playwright.async_api import Error as PlaywrightError
    try:
        from .. import semantic_macros as _semantic_macros
    except ImportError:
        try:
            from . import semantic_macros as _semantic_macros  # type: ignore[no-redef]
        except ImportError:
            import semantic_macros as _semantic_macros  # type: ignore[no-redef]

    # RPA-XSYS: auto-thread the run's SessionRouter off the browser when not
    # passed explicitly. main.py attaches browser._session_router ONLY when
    # VSPIDER_CROSS_SYSTEM_SWITCH is on, so flag off -> None -> no switching.
    if session_router is None:
        session_router = getattr(browser, "_session_router", None)
    # RPA-XSYS-EVT: auto-thread the run event_stream off the browser when
    # not passed, so cross-system replay hops emit session_switch evidence.
    # main.py attaches browser._event_stream alongside _session_router
    # (flag-gated) -> flag off -> None -> no events, byte-identical.
    if event_stream is None:
        event_stream = getattr(browser, "_event_stream", None)

    compacted_trail = _compact_rpa_trail(trail)
    if len(compacted_trail) != len(trail):
        logger.info(
            f"[RPA REPLAY] Compacted cached trail: {len(trail)} → {len(compacted_trail)} steps"
        )

    _RPA_TIMEOUT = 5000  # 每步最长等待 5s，防止卡死

    # ── Cross-system RPA replay state (RPA-XSYS) ──────────────────────
    # Track the active planned system; a step whose system_id differs gets a
    # physical browser switch before it replays. All inert when router None.
    _xsys_current_system = ""
    _xsys_home_browser = None
    _xsys_home_system = ""
    _xsys_udd_base = ""
    if session_router is not None:
        try:
            _xsys_current_system = session_router.system_for_url(
                getattr(browser, "current_url", "") or ""
            )
        except Exception:
            _xsys_current_system = ""
        try:
            try:
                from . import config as _xsys_cfg
            except ImportError:
                import config as _xsys_cfg  # type: ignore[no-redef]
            _xsys_udd_base = getattr(_xsys_cfg, "BROWSER_USER_DATA_DIR", "") or ""
        except Exception:
            _xsys_udd_base = ""

    for idx, step in enumerate(compacted_trail):
        act = step.get("action")
        step_label = f"Step {idx + 1}/{len(compacted_trail)} ({act})"
        try:
            # Cross-system replay hop: switch the active browser before this
            # step runs if it belongs to a different planned system.
            if session_router is not None:
                _step_system = str(step.get("system_id") or "").strip()
                if _step_system and _step_system != _xsys_current_system:
                    if act == "goto":
                        _hop_url = (
                            step.get("url_template") or step.get("url")
                            or step.get("type_value_template")
                            or step.get("type_value") or ""
                        )
                    else:
                        _hop_url = getattr(browser, "current_url", "") or ""
                    _hop_url = _resolve_replay_template(str(_hop_url), workflow_memory)
                    browser, _xsys_home_browser, _xsys_home_system = (
                        await _replay_switch_system(
                            browser,
                            session_router,
                            to_system_id=_step_system,
                            from_system_id=_xsys_current_system,
                            url=_hop_url,
                            home_browser=_xsys_home_browser,
                            home_system_id=_xsys_home_system,
                            user_data_dir_base=_xsys_udd_base,
                            event_stream=event_stream,
                        )
                    )
                    _xsys_current_system = _step_system
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

            elif act in _semantic_macros.actions():
                # Unified semantic-macro dispatch — handles cascader_pick,
                # date_pick, and any future macro registered with the
                # ``semantic_macros`` package. Each macro provides its own JS
                # body + optional VL judge question.
                logger.info(
                    "[RPA REPLAY] %s: dispatching %r through semantic_macros registry",
                    step_label, act,
                )
                # Inject runtime config flags that the JS body expects. date_pick
                # reads ``allow_direct_set`` (config: DATE_PICK_DIRECT_SET_FALLBACK)
                # to decide whether to use ``setNativeValue`` as a last resort.
                if act == "date_pick" and "allow_direct_set" not in step:
                    step["allow_direct_set"] = bool(getattr(
                        _runtime_config_module(),
                        "DATE_PICK_DIRECT_SET_FALLBACK",
                        False,
                    ))
                _macro_result = await _semantic_macros.replay_step(page, step, vlm=vlm)
                if not _macro_result.get("ok"):
                    raise RuntimeError(f"{act} macro failed: {_macro_result}")
                if _macro_result.get("vl_recovered"):
                    print(
                        f"\033[1;33m🔍 [VL JUDGE]\033[0m {act}: VL 视觉裁判判定已生效 "
                        f"({str(_macro_result.get('vl_judge', {}).get('reason',''))[:60]})"
                    )
                logger.info("[RPA REPLAY] %s verified: %s", act, _macro_result)
                await asyncio.sleep(0.5)

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

            elif act in ("fetch_link_content", "fetch_links_batch"):
                type_value = (
                    step.get("type_value_template")
                    or step.get("type_value")
                    or step.get("url")
                    or ""
                )
                type_value = _resolve_replay_template(str(type_value), workflow_memory)
                memory_key = str(step.get("memory_key") or "").strip()
                if not memory_key:
                    memory_key = f"fetched_{idx + 1}"
                logger.info(
                    "[RPA REPLAY] %s: %s -> memory[%s]",
                    step_label,
                    act,
                    memory_key,
                )
                active_page = await browser.execute_action(
                    {
                        "progress_review": "cached fetch replay",
                        "thought": f"Replay cached {act} without VLM.",
                        "current_state": "RPA replay",
                        "action": act,
                        "target_id": int(step.get("target_id") or 0),
                        "type_value": type_value,
                        "memory_key": memory_key,
                        "extracted_data": None,
                        "status": "pending",
                    },
                    workflow_memory=workflow_memory,
                )
                if active_page is not None:
                    browser._page = active_page
                await asyncio.sleep(0.2)

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
                f"\n\033[1;41m⚠️  [RPA REPLAY FAILED]\033[0m "
                f"step={idx + 1}/{len(compacted_trail)} action={act} "
                f"reason: {type(rpa_err).__name__}: {rpa_err}\n"
            )
            logger.warning(f"[RPA REPLAY] {step_label} FAILED — {type(rpa_err).__name__}: {rpa_err}")
            try:
                _failure_path = _RPA_CACHE_DIR / "_last_replay_failure.json"
                _failure_record = {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "step_index": idx,
                    "step_count": len(compacted_trail),
                    "action": act,
                    "step_label": step_label,
                    "step_payload": step,
                    "error_type": type(rpa_err).__name__,
                    "error_message": str(rpa_err),
                    "current_url": getattr(browser, "current_url", "") or "",
                }
                _failure_path.write_text(
                    json.dumps(_failure_record, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info(f"[RPA REPLAY] Failure detail written to {_failure_path}")
            except Exception as _persist_err:
                logger.debug(f"[RPA REPLAY] Failed to persist failure detail: {_persist_err}")
            return False

    return True


async def _replay_ready_rpa_steps(
    browser: "BrowserEnv",
    payload: dict,
    cursor: int,
    workflow_memory: dict | None,
    vlm: "VLMClient | None" = None,
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
    ok = await _replay_rpa(browser, ready_steps, workflow_memory, vlm=vlm)
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



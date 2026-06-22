"""Setup / handoff tools carved out of ``main.run_agent`` (slice S3).

These nine helpers ran as closures at the top of ``run_agent`` (the browser
action tool, the targeted-probe perception handoffs, the stop / stale-auth
guards, tool-metadata tagging and active-page recovery). They are pure
relocations onto :class:`SetupTools`, which reads everything through
:class:`SetupDeps` so ``run_agent`` keeps thin delegating wrappers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

try:  # pragma: no cover - import shim mirrors main.py
    from ..perception.targeted import (
        choose_click_handoff_candidate,
        choose_type_handoff_candidate,
        format_probe_text,
        probe_page,
    )
    from ..action_result import ActionResult
    from ..auth_vault import resolve_env_placeholders
    from .cli_config import _resolve_action_tool_metadata
except ImportError:  # pragma: no cover
    from perception.targeted import (
        choose_click_handoff_candidate,
        choose_type_handoff_candidate,
        format_probe_text,
        probe_page,
    )
    from action_result import ActionResult
    from auth_vault import resolve_env_placeholders
    from phases.cli_config import _resolve_action_tool_metadata


@dataclass
class SetupDeps:
    """Dependency handles for the setup/handoff tools lifted out of run_agent."""

    browser: Any
    goal: str
    vlm: Any
    logger: Any
    event_stream: Any
    stop_event: Any
    start_url: str
    action_registry: Any
    broadcast_log_safe: Callable[..., Any]
    broadcast_done_safe: Callable[..., Any]


class SetupTools:
    """Setup/handoff helpers moved verbatim from run_agent (pure relocation)."""

    def __init__(self, deps: SetupDeps) -> None:
        self.deps = deps

    async def browser_action_tool(self, action_payload, workflow_memory=None):
        return await self.deps.browser.execute_action(action_payload, workflow_memory)

    async def targeted_probe_tool(self, action_payload, workflow_memory=None):
        page = await self.deps.browser._ensure_active_page(reason="targeted probe action")
        before_url = self.deps.browser.current_url
        before_pages = len(self.deps.browser._context.pages) if self.deps.browser._context else 0
        raw_value = str((action_payload or {}).get("type_value") or "").strip()
        probe_goal = self.deps.goal
        kinds: list[str] = []
        if raw_value:
            if "|" in raw_value:
                kind_part, probe_part = raw_value.split("|", 1)
                kinds = [
                    item.strip()
                    for item in re.split(r"[,，\s]+", kind_part)
                    if item.strip() in {"input", "button", "link", "table", "dialog"}
                ]
                probe_goal = probe_part.strip() or self.deps.goal
            elif raw_value in {"input", "button", "link", "table", "dialog"}:
                kinds = [raw_value]
            else:
                probe_goal = raw_value
        if not page:
            self.deps.browser._last_action_result = ActionResult.from_action(
                action_payload,
                success=False,
                error="No active page for targeted probe.",
                before_url=before_url,
                after_url=before_url,
                before_pages=before_pages,
                after_pages=before_pages,
            )
            return None
        probe_result = await probe_page(
            page,
            goal=probe_goal,
            kinds=tuple(kinds) or None,
            limit=12,
        )
        summary = format_probe_text(probe_result, limit=8)
        metadata = {
            "probe": probe_result.as_dict(),
            "probe_goal": probe_goal,
            "candidate_count": len(probe_result.candidates),
        }
        self.deps.browser._last_action_result = ActionResult.from_action(
            action_payload,
            success=probe_result.ok,
            message=summary,
            error="" if probe_result.ok else "No targeted candidates found.",
            before_url=before_url,
            after_url=self.deps.browser.current_url,
            before_pages=before_pages,
            after_pages=len(self.deps.browser._context.pages) if self.deps.browser._context else before_pages,
            metadata=metadata,
        )
        try:
            if probe_result.ok:
                self.deps.vlm.inject_error_feedback(
                    "局部感知探针已返回候选元素。优先根据下列 selector/bbox/evidence 选择下一步；"
                    "如果候选足够明确，可改用 click_text/type/press_key 等确定性动作；"
                    "如果候选不匹配，再回退到全页截图/SoM 判断。\n"
                    + summary
                )
            else:
                self.deps.vlm.inject_error_feedback(
                    "局部感知探针未找到匹配候选。请回退到全页截图/SoM 观察，或先滚动/展开弹窗后重试。"
                )
        except Exception:
            pass
        return page

    async def try_targeted_click_text_handoff(self, action_payload: dict) -> bool:
        click_text = str((action_payload or {}).get("type_value") or "").strip()
        if not click_text or len(click_text) > 80:
            return False
        page = await self.deps.browser._ensure_active_page(reason="targeted click_text handoff")
        if not page:
            return False
        before_url = self.deps.browser.current_url
        before_pages = len(self.deps.browser._context.pages) if self.deps.browser._context else 0
        try:
            probe_result = await probe_page(
                page,
                goal=f"click {click_text}",
                kinds=("button", "link"),
                limit=8,
            )
            candidate = choose_click_handoff_candidate(
                probe_result,
                target_text=click_text,
                min_confidence=0.55,
            )
            if not candidate:
                return False
            frame = page.main_frame
            for item in page.frames:
                try:
                    if candidate.frame_name and item.name == candidate.frame_name:
                        frame = item
                        break
                    if candidate.frame_url and item.url == candidate.frame_url:
                        frame = item
                        break
                except Exception:
                    continue
            locator = frame.locator(candidate.selector).first
            await locator.scroll_into_view_if_needed(timeout=2500)
            await locator.click(timeout=4000)
            await self.deps.browser._wait_after_action()
            active_page = await self.deps.browser._ensure_active_page(reason="targeted click_text handoff after click")
            self.deps.browser._last_action_result = ActionResult.from_action(
                action_payload,
                success=True,
                message=(
                    "targeted_probe high-confidence handoff clicked "
                    f"{candidate.kind} {candidate.text!r}"
                ),
                before_url=before_url,
                after_url=self.deps.browser.current_url,
                before_pages=before_pages,
                after_pages=len(self.deps.browser._context.pages) if self.deps.browser._context else before_pages,
                metadata={
                    "clicked_text": candidate.text,
                    "targeted_handoff": {
                        "mode": "click_text_to_selector",
                        "selector": candidate.selector,
                        "confidence": candidate.confidence,
                        "kind": candidate.kind,
                        "text": candidate.text,
                        "frame_url": candidate.frame_url,
                        "evidence": list(candidate.evidence),
                    },
                    "probe": probe_result.as_dict(),
                },
            )
            self.deps.logger.info(
                "[TARGETED HANDOFF] click_text %r -> selector=%s conf=%s text=%r",
                click_text,
                candidate.selector,
                candidate.confidence,
                candidate.text,
            )
            return active_page is not None
        except Exception as exc:
            self.deps.logger.debug("[TARGETED HANDOFF] click_text probe/click skipped: %s", exc)
            return False

    def resolve_type_value_for_handoff(self, raw_value: str, workflow_memory=None) -> tuple[str, str]:
        value = str(raw_value or "")
        display_value = value
        if workflow_memory and "{{" in value:
            def _interpolate(match: re.Match) -> str:
                key = match.group(1).strip()
                resolved = (workflow_memory or {}).get(key)
                return match.group(0) if resolved is None else str(resolved)
            value = re.sub(r"\{\{([^}]+)\}\}", _interpolate, value)
            display_value = value
        env_template = display_value if "{{env:" in display_value else ""
        value, used_auth_vault, _env_names = resolve_env_placeholders(value)
        if used_auth_vault:
            display_value = env_template
        return value, display_value

    async def try_targeted_type_handoff(self, 
        action_payload: dict,
        workflow_memory=None,
    ) -> bool:
        raw_value = str((action_payload or {}).get("type_value") or "")
        if not raw_value.strip():
            return False
        try:
            target_id = int((action_payload or {}).get("target_id") or 0)
        except Exception:
            target_id = 0
        if target_id > 0:
            return False
        page = await self.deps.browser._ensure_active_page(reason="targeted type handoff")
        if not page:
            return False
        before_url = self.deps.browser.current_url
        before_pages = len(self.deps.browser._context.pages) if self.deps.browser._context else 0
        try:
            probe_goal = self.deps.goal
            probe_result = await probe_page(
                page,
                goal=probe_goal,
                kinds=("input",),
                limit=8,
            )
            candidate = choose_type_handoff_candidate(
                probe_result,
                min_confidence=0.5,
            )
            if not candidate:
                try:
                    try:
                        from .targeted_type_fallback import fill_best_text_input
                    except ImportError:
                        from targeted_type_fallback import fill_best_text_input
                    value, display_value = self.resolve_type_value_for_handoff(
                        raw_value,
                        workflow_memory=workflow_memory,
                    )
                    fallback_result = await fill_best_text_input(page, value)
                    if not fallback_result.get("ok"):
                        return False
                    await self.deps.browser._wait_after_action(light_action=True)
                    self.deps.browser.rpa_trail.append({
                        "action": "type",
                        "method": "dom_input_fallback",
                        "type_value": display_value,
                    })
                    self.deps.browser._last_action_result = ActionResult.from_action(
                        action_payload,
                        success=True,
                        message=(
                            "DOM fallback filled visible text/search input "
                            f"{fallback_result.get('tag', '')} "
                            f"{fallback_result.get('ariaLabel') or fallback_result.get('placeholder') or fallback_result.get('name') or ''!r}"
                        ),
                        before_url=before_url,
                        after_url=self.deps.browser.current_url,
                        before_pages=before_pages,
                        after_pages=len(self.deps.browser._context.pages) if self.deps.browser._context else before_pages,
                        metadata={
                            "value_readbacks": [
                                {
                                    "value": display_value,
                                    "method": "dom_input_fallback",
                                    "observed": fallback_result.get("value", ""),
                                }
                            ],
                            "targeted_handoff": {
                                "mode": "dom_input_fallback",
                                **fallback_result,
                            },
                            "probe": probe_result.as_dict(),
                        },
                    )
                    self.deps.logger.info(
                        "[TARGETED HANDOFF] type DOM fallback -> %s",
                        fallback_result,
                    )
                    return True
                except Exception as fallback_exc:
                    self.deps.logger.debug(
                        "[TARGETED HANDOFF] DOM input fallback skipped: %s",
                        fallback_exc,
                    )
                    return False
            frame = page.main_frame
            for item in page.frames:
                try:
                    if candidate.frame_name and item.name == candidate.frame_name:
                        frame = item
                        break
                    if candidate.frame_url and item.url == candidate.frame_url:
                        frame = item
                        break
                except Exception:
                    continue
            value, display_value = self.resolve_type_value_for_handoff(
                raw_value,
                workflow_memory=workflow_memory,
            )
            locator = frame.locator(candidate.selector).first
            await locator.scroll_into_view_if_needed(timeout=2500)
            await locator.click(timeout=3000)
            await locator.fill(value, timeout=4000)
            await locator.evaluate(
                """el => {
                    el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true, composed: true}));
                }"""
            )
            await self.deps.browser._wait_after_action(light_action=True)
            self.deps.browser.rpa_trail.append({
                "action": "type",
                "selector": candidate.selector,
                "method": "targeted_probe_handoff",
                "type_value": display_value,
            })
            self.deps.browser._last_action_result = ActionResult.from_action(
                action_payload,
                success=True,
                message=(
                    "targeted_probe high-confidence handoff filled "
                    f"{candidate.tag} {candidate.text!r}"
                ),
                before_url=before_url,
                after_url=self.deps.browser.current_url,
                before_pages=before_pages,
                after_pages=len(self.deps.browser._context.pages) if self.deps.browser._context else before_pages,
                metadata={
                    "value_readbacks": [
                        {
                            "selector": candidate.selector,
                            "value": display_value,
                            "method": "targeted_probe_handoff",
                        }
                    ],
                    "targeted_handoff": {
                        "mode": "type_to_input_selector",
                        "selector": candidate.selector,
                        "confidence": candidate.confidence,
                        "kind": candidate.kind,
                        "text": candidate.text,
                        "frame_url": candidate.frame_url,
                        "evidence": list(candidate.evidence),
                    },
                    "probe": probe_result.as_dict(),
                },
            )
            self.deps.logger.info(
                "[TARGETED HANDOFF] type -> selector=%s conf=%s text=%r",
                candidate.selector,
                candidate.confidence,
                candidate.text,
            )
            return True
        except Exception as exc:
            self.deps.logger.debug("[TARGETED HANDOFF] type probe/fill skipped: %s", exc)
            return False

    def check_stop(self, context: str) -> None:
        if self.deps.stop_event and self.deps.stop_event.is_set():
            raise RuntimeError(f"STOP_REQUESTED::{context}")

    def abort_if_stale_auth(self, ) -> bool:
        if not getattr(self.deps.browser, "auth_stale_detected", False):
            return False
        stale_msg = getattr(self.deps.browser, "auth_stale_reason", "") or (
            "Auth profile appears stale; please refresh it with tools/manual_auth.py."
        )
        self.deps.logger.error("[AUTH STALE] %s", stale_msg)
        self.deps.broadcast_log_safe(f"[AUTH STALE] {stale_msg}", level="error")
        self.deps.broadcast_done_safe(False, stale_msg)
        return True

    def with_tool_metadata(self, result, selected_tools):
        if result is None:
            return result
        try:
            action_name = (
                result.action
                if isinstance(result, ActionResult)
                else str(result.get("action") or "")
            )
        except Exception:
            action_name = ""
        tool_meta = _resolve_action_tool_metadata(
            self.deps.action_registry,
            action_name,
            goal=self.deps.goal,
            selected_tools=selected_tools,
        )
        if not tool_meta:
            return result
        if isinstance(result, ActionResult):
            result.metadata.setdefault("tool", tool_meta)
            return result
        if isinstance(result, dict):
            data = dict(result)
            metadata = dict(data.get("metadata") or {})
            metadata.setdefault("tool", tool_meta)
            data["metadata"] = metadata
            return data
        return result

    async def recover_active_page(self, reason: str):
        page = await self.deps.browser._ensure_active_page(reason=reason)
        if page is not None and not page.is_closed():
            return page
        self.deps.logger.warning(
            "[BROWSER RECOVERY] No active page during %s; restarting self.deps.browser at %s",
            reason,
            self.deps.start_url,
        )
        self.deps.event_stream.guard(
            step=0,
            name="BROWSER_CONTEXT_RECOVERY",
            message=f"No active page during {reason}; restarted self.deps.browser context",
            metadata={"self.deps.start_url": self.deps.start_url},
        )
        await self.deps.browser.restart(self.deps.start_url, reason=reason)
        page = await self.deps.browser._ensure_active_page(reason=f"after restart: {reason}")
        if page is None or page.is_closed():
            raise RuntimeError(f"No active page after self.deps.browser restart ({reason})")
        return page

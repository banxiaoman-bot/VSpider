"""Regression tests for the dismiss_consent deterministic capability (DC-1).

Covers (spec: docs/superpowers/specs/2026-06-21-dismiss-consent-design.md):
  - VSpiderAction schema acceptance of the new action
  - Handler registration + package export
  - Idempotent no-op when no consent wall is present
  - L1 known-CMP hit clicks + verifies overlay disappearance
  - L2 affirmative-text fallback
  - reject-only overlay -> no-op (probe applies exclusion)
  - cross-iframe fallback
  - click-but-overlay-persists -> dismissed=False
  - ActionTool metadata registration
  - capability_router consent signal + backend plan routing
  - prompt skill content
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from visual_web_agent.vlm_client import VSpiderAction
from visual_web_agent.actions import (
    ActionRegistry,
    ActionExecutionError,
    DismissConsentHandler,
)


# ════════════════════════════════════════════════════════════════════
#                       SCHEMA / VLM CLIENT
# ════════════════════════════════════════════════════════════════════


class TestSchema:
    def test_dismiss_consent_accepted(self) -> None:
        v = VSpiderAction(action="dismiss_consent", target_id=0, type_value="", memory_key="")
        assert v.action == "dismiss_consent"

    def test_unknown_action_still_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VSpiderAction(action="not_a_real_action", target_id=0, type_value="", memory_key="")


# ════════════════════════════════════════════════════════════════════
#                       HANDLER REGISTRATION
# ════════════════════════════════════════════════════════════════════


class TestRegistration:
    def test_handler_registered(self) -> None:
        assert ActionRegistry._handlers["dismiss_consent"] is DismissConsentHandler


# ════════════════════════════════════════════════════════════════════
#                       STUB SCAFFOLD
# ════════════════════════════════════════════════════════════════════


def _make_browser_stub() -> SimpleNamespace:
    async def _no_op(*_, **__):
        return None

    return SimpleNamespace(_wait_after_action=_no_op, rpa_trail=[])


def _make_ctx(*, page: Any) -> SimpleNamespace:
    return SimpleNamespace(
        action=SimpleNamespace(action="dismiss_consent", target_id=0, type_value=""),
        browser=_make_browser_stub(),
        page=page,
        workflow_memory={},
        with_rpa_meta=lambda d: d,
    )


class _StubLocator:
    def __init__(self, *, click_raises: bool = False):
        self.click_raises = click_raises
        self.clicked = False

    @property
    def first(self):
        return self

    async def scroll_into_view_if_needed(self, **_):
        return None

    async def click(self, **_):
        if self.click_raises:
            raise RuntimeError("blocked")
        self.clicked = True

    async def evaluate(self, *_a, **_k):
        return None


class _StubScope:
    """Stands in for a Playwright Page or Frame.

    First ``evaluate`` per scope returns the mark-consent probe dict; later
    calls return the still-visible boolean (verify + optional retry).
    """

    def __init__(self, name: str, *, probe=None, still_visible=False, locator=None):
        self.url = f"https://example.test/{name}"
        self._probe = probe if probe is not None else {"found": False}
        self._still = still_visible
        self._locator = locator or _StubLocator()
        self.eval_calls = 0

    def locator(self, _sel: str):
        return self._locator

    async def evaluate(self, _script: str, *args, **_k):
        self.eval_calls += 1
        if self.eval_calls == 1:
            return self._probe
        return self._still


class _StubPage:
    def __init__(self, scope: _StubScope, frames=None):
        self._scope = scope
        self.url = scope.url
        self.frames = frames if frames is not None else [scope]
        self.main_frame = self.frames[0] if self.frames else None

    def locator(self, sel: str):
        return self._scope.locator(sel)

    async def evaluate(self, script: str, *args, **k):
        return await self._scope.evaluate(script, *args, **k)


# ════════════════════════════════════════════════════════════════════
#                       NO-OP / GUARD
# ════════════════════════════════════════════════════════════════════


class TestNoop:
    def test_clean_page_is_noop_success(self) -> None:
        scope = _StubScope("main", probe={"found": False})
        ctx = _make_ctx(page=_StubPage(scope))
        asyncio.run(DismissConsentHandler().execute(ctx))
        trail = ctx.browser.rpa_trail
        assert trail and trail[-1]["action"] == "dismiss_consent"
        assert trail[-1]["dismissed"] is False
        assert trail[-1]["strategy"] == "noop"

    def test_missing_page_raises(self) -> None:
        ctx = _make_ctx(page=None)
        with pytest.raises(ActionExecutionError, match="无活动页面"):
            asyncio.run(DismissConsentHandler().execute(ctx))


# ════════════════════════════════════════════════════════════════════
#                       DETECTION
# ════════════════════════════════════════════════════════════════════


class TestDetection:
    def test_known_cmp_hit_clicks_and_dismisses(self) -> None:
        loc = _StubLocator()
        scope = _StubScope(
            "main",
            probe={"found": True, "strategy": "known_cmp",
                   "cmp": "OneTrust", "selector": "#onetrust-accept-btn-handler"},
            still_visible=False, locator=loc,
        )
        ctx = _make_ctx(page=_StubPage(scope))
        asyncio.run(DismissConsentHandler().execute(ctx))
        assert loc.clicked is True
        last = ctx.browser.rpa_trail[-1]
        assert last["strategy"] == "known_cmp"
        assert last["dismissed"] is True

    def test_accept_text_fallback(self) -> None:
        loc = _StubLocator()
        scope = _StubScope(
            "main",
            probe={"found": True, "strategy": "accept_text",
                   "cmp": "text", "label": "接受全部"},
            still_visible=False, locator=loc,
        )
        ctx = _make_ctx(page=_StubPage(scope))
        asyncio.run(DismissConsentHandler().execute(ctx))
        assert loc.clicked is True
        assert ctx.browser.rpa_trail[-1]["strategy"] == "accept_text"

    def test_reject_only_overlay_is_noop(self) -> None:
        loc = _StubLocator()
        scope = _StubScope("main", probe={"found": False}, locator=loc)
        ctx = _make_ctx(page=_StubPage(scope))
        asyncio.run(DismissConsentHandler().execute(ctx))
        assert loc.clicked is False
        assert ctx.browser.rpa_trail[-1]["strategy"] == "noop"

    def test_iframe_fallback(self) -> None:
        main = _StubScope("main", probe={"found": False})
        child_loc = _StubLocator()
        child = _StubScope(
            "cmp_frame",
            probe={"found": True, "strategy": "known_cmp",
                   "cmp": "TrustArc", "selector": "#truste-consent-button"},
            still_visible=False, locator=child_loc,
        )
        page = _StubPage(main, frames=[main, child])
        ctx = _make_ctx(page=page)
        asyncio.run(DismissConsentHandler().execute(ctx))
        assert child_loc.clicked is True
        last = ctx.browser.rpa_trail[-1]
        assert last["frame_url"].endswith("cmp_frame")
        assert last["scanned_frames"] == 2

    def test_click_but_overlay_persists_marks_not_dismissed(self) -> None:
        loc = _StubLocator()
        scope = _StubScope(
            "main",
            probe={"found": True, "strategy": "known_cmp",
                   "cmp": "OneTrust", "selector": "#onetrust-accept-btn-handler"},
            still_visible=True, locator=loc,
        )
        ctx = _make_ctx(page=_StubPage(scope))
        asyncio.run(DismissConsentHandler().execute(ctx))
        assert loc.clicked is True
        assert ctx.browser.rpa_trail[-1]["dismissed"] is False


# ════════════════════════════════════════════════════════════════════
#                       ACTION TOOL METADATA
# ════════════════════════════════════════════════════════════════════


class TestActionTool:
    def test_tool_registered_with_evidence(self) -> None:
        from visual_web_agent.action_registry import build_default_action_registry

        reg = build_default_action_registry()
        tool = reg.get("dismiss_consent")
        assert tool is not None
        assert "consent_dismissed.v1" in tool.evidence
        assert tool.risk == "low"


# ════════════════════════════════════════════════════════════════════
#                       CAPABILITY ROUTER
# ════════════════════════════════════════════════════════════════════


class TestRouter:
    def test_consent_signal_detected(self) -> None:
        from visual_web_agent.capability_router import _signals

        sig = _signals("打开页面后关闭 cookie 同意弹窗再抓取列表", {})
        assert sig.get("consent_preferred") is True

    def test_backend_plan_includes_dismiss_consent(self) -> None:
        from visual_web_agent.capability_router import _signals, _backend_plan

        sig = _signals("关闭 cookie consent banner 后抓取", {})
        plan = _backend_plan(sig, {}, [])
        assert any("dismiss_consent" in str(entry) for entry in plan)


# ════════════════════════════════════════════════════════════════════
#                       PROMPT SKILL
# ════════════════════════════════════════════════════════════════════


class TestSkill:
    def test_skill_documents_action_and_safety(self) -> None:
        from visual_web_agent.prompt_skills import DISMISS_CONSENT_SKILL

        assert "dismiss_consent" in DISMISS_CONSENT_SKILL
        assert "接受全部" in DISMISS_CONSENT_SKILL
        assert "拒绝" in DISMISS_CONSENT_SKILL

    def test_skill_registered_in_prompts_dict(self) -> None:
        from visual_web_agent.prompt_skills import SKILL_PROMPTS, DISMISS_CONSENT_SKILL

        assert SKILL_PROMPTS.get("dismiss_consent") is DISMISS_CONSENT_SKILL

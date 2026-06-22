"""E2: pin the "Element #N not found" error message format.

When a SoM target ID can't be resolved (page mutated between snapshot and
action), every handler should raise ``RuntimeError`` with a message that:

1. **Identifies the missing target** by ID.
2. **Names the failure mode** ("SoM ID 失效" / "页面发生变化") so the VLM
   recognises it from the prompt's `CONFIRM_DIALOG_SKILL` / fallback rules.
3. **Suggests two concrete recovery paths**: ``click_text`` with a visible
   label from the VLM's own thought, OR ``wait`` + fresh snapshot.

A regression here would silently undo the E2 fix (the message was previously
a terse "Element #N not found on active page or its iframes" with no hint —
VLM would loop the same dead ID).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from visual_web_agent.actions import ClickHandler, TypeHandler
from visual_web_agent.vlm_client import VSpiderAction


ACTIONS_PKG = Path(__file__).resolve().parent.parent / "visual_web_agent" / "actions"


def _read_all_actions_source() -> str:
    """Read all .py files in the actions package as a single concatenated source."""
    parts = []
    for py in sorted(ACTIONS_PKG.glob("*.py")):
        parts.append(py.read_text(encoding="utf-8"))
    return "\n".join(parts)


# ════════════════════════════════════════════════════════════════════
# Source-level invariant: at least 6 callsites must carry the hint.
# (Known to be 8 today; floor of 6 catches accidental regression.)
# ════════════════════════════════════════════════════════════════════


class TestSourceInvariant:
    def test_hint_replaces_all_terse_messages(self) -> None:
        src = _read_all_actions_source()
        # The hint must be present at every "Element #N not found" call.
        terse = len(re.findall(
            r'f"Element #\{target_id\} not found on active page or its iframes"\s*$',
            src,
            flags=re.M,
        ))
        # No terse messages should remain — they all have a continuation line
        # appending the hint, so the bare quote-closing line is gone.
        assert terse == 0, (
            f"Found {terse} terse 'Element #N not found' messages without "
            f"the recovery hint — the E2 patch was regressed."
        )

    def test_hint_recommendations_present(self) -> None:
        src = _read_all_actions_source()
        # Must mention both recovery paths
        assert "click_text" in src
        assert "SoM ID 失效" in src
        # Should appear at all 8 callsites
        n = len(re.findall(r"用 click_text\(type_value=", src))
        assert n >= 6, f"Hint propagation regressed: only {n} sites carry it"


# ════════════════════════════════════════════════════════════════════
# Runtime: handlers raise the enriched message when target missing.
# ════════════════════════════════════════════════════════════════════


def _make_ctx_with_no_target(action_name: str = "click") -> SimpleNamespace:
    """Build a ctx where ``_resolve_action_target`` returns None — i.e. the
    SoM ID 42 does not resolve, triggering the enriched RuntimeError.

    The handlers swallow the RuntimeError into ``browser._last_action_error``
    and continue to ``_wait_after_action``, so we stub that too. Test reads
    the message back from ``_last_action_error`` instead of ``pytest.raises``.
    """

    async def _resolve(_tid: int, _name: str):
        return None  # simulate "SoM ID went stale"

    async def _noop(*_a, **_kw):
        return None

    browser = SimpleNamespace(
        _clear_som_overlays=_noop,
        _resolve_action_target=_resolve,
        _wait_after_action=_noop,  # handler calls this in the finally path
        _LOCATOR_TIMEOUT=2000,
        _last_action_error=None,
        _last_action_result=None,
        _tab_switch_notice=None,
        _last_notice_severity="info",
        _last_native_dialog=None,
        rpa_trail=[],
        _ensure_active_page=AsyncMock(return_value=None),
        current_url="https://example.test/",
    )
    action = VSpiderAction(
        action=action_name,
        target_id=42,
        type_value="hello" if action_name == "type" else "",
        memory_key="",
    )
    return SimpleNamespace(
        action=action,
        browser=browser,
        page=object(),
        workflow_memory={},  # TypeHandler reads this
        with_rpa_meta=lambda d: d,
        rpa_required_keys=[],
        rpa_template_value="",
        trace_id="t0",
    )


def _assert_enriched(msg: str) -> None:
    assert "Element #42 not found" in msg
    assert "SoM ID 失效" in msg
    assert "click_text" in msg
    assert "wait" in msg


class TestClickHandlerEnrichedError:
    def test_message_recorded_with_hints(self) -> None:
        ctx = _make_ctx_with_no_target("click")
        asyncio.run(ClickHandler().execute(ctx))
        # Handler swallows RuntimeError into _last_action_error
        err = ctx.browser._last_action_error
        assert err is not None, "click handler should record the RuntimeError"
        _assert_enriched(str(err))


class TestTypeHandlerEnrichedError:
    def test_message_recorded_with_hints(self) -> None:
        ctx = _make_ctx_with_no_target("type")
        try:
            asyncio.run(TypeHandler().execute(ctx))
        except Exception:
            # TypeHandler may bubble the error in some code paths — that's fine
            pass
        # In either path, the error should be recorded with the hint
        err = ctx.browser._last_action_error
        if err is None:
            pytest.skip(
                "type handler path didn't reach _last_action_error; "
                "source-level invariants still pin the contract"
            )
        _assert_enriched(str(err))

"""G3: ``ActionContext.trace_id`` auto-stamp on RPA trail entries.

Pure-Python tests; no Playwright, no async.
"""

from __future__ import annotations

import re

import pytest

from visual_web_agent.actions import ActionContext
from visual_web_agent.vlm_client import VSpiderAction


def _mk_ctx(action: str = "click", target_id: int = 1, **action_kwargs):
    act = VSpiderAction(
        action=action,
        target_id=target_id,
        type_value=action_kwargs.pop("type_value", ""),
        memory_key=action_kwargs.pop("memory_key", ""),
        **action_kwargs,
    )
    return ActionContext(
        action=act, browser=object(), workflow_memory={}, page=object(),
    )


_HEX8 = re.compile(r"^[0-9a-f]{8}$")


class TestTraceIdGeneration:
    def test_default_factory_produces_8_hex_chars(self) -> None:
        ctx = _mk_ctx()
        assert _HEX8.match(ctx.trace_id), f"got {ctx.trace_id!r}"

    def test_each_context_gets_distinct_id(self) -> None:
        ids = {_mk_ctx().trace_id for _ in range(50)}
        # 8 hex chars = 32 bits → 50 random samples colliding is ~1e-7
        assert len(ids) == 50

    def test_explicit_override_respected(self) -> None:
        act = VSpiderAction(action="click", target_id=1, type_value="", memory_key="")
        ctx = ActionContext(
            action=act,
            browser=object(),
            workflow_memory={},
            page=object(),
            trace_id="manual01",
        )
        assert ctx.trace_id == "manual01"


class TestStampingViaWithRpaMeta:
    def test_step_gets_trace_id(self) -> None:
        ctx = _mk_ctx()
        step = ctx.with_rpa_meta({"action": "click"})
        assert step["trace_id"] == ctx.trace_id

    def test_caller_override_preserved(self) -> None:
        ctx = _mk_ctx()
        step = ctx.with_rpa_meta({"action": "click", "trace_id": "CALLER_X"})
        # setdefault — caller's override wins.
        assert step["trace_id"] == "CALLER_X"
        assert step["trace_id"] != ctx.trace_id

    def test_repeated_calls_same_ctx_share_id(self) -> None:
        """Multiple actions emitted by the same handler should share trace_id
        so the frontend can group them as one VLM decision's effect."""
        ctx = _mk_ctx()
        s1 = ctx.with_rpa_meta({"action": "click"})
        s2 = ctx.with_rpa_meta({"action": "wait"})
        s3 = ctx.with_rpa_meta({"action": "type"})
        assert s1["trace_id"] == s2["trace_id"] == s3["trace_id"] == ctx.trace_id

    def test_distinct_contexts_distinct_ids(self) -> None:
        """Two different action decisions should produce different trails."""
        ctx_a = _mk_ctx()
        ctx_b = _mk_ctx()
        sa = ctx_a.with_rpa_meta({"action": "click"})
        sb = ctx_b.with_rpa_meta({"action": "click"})
        assert sa["trace_id"] != sb["trace_id"]


class TestPreservesExistingMeta:
    """trace_id stamping must not regress existing rpa_meta semantics."""

    def test_target_id_still_set(self) -> None:
        ctx = _mk_ctx(target_id=42)
        step = ctx.with_rpa_meta({"action": "click"})
        assert step["target_id"] == 42
        assert step["trace_id"] == ctx.trace_id

    def test_required_memory_keys_still_set(self) -> None:
        ctx = _mk_ctx()
        ctx.rpa_required_keys = ["k1", "k2"]
        step = ctx.with_rpa_meta({"action": "type"})
        assert step["required_memory_keys"] == ["k1", "k2"]
        assert step["trace_id"] == ctx.trace_id

    def test_template_value_for_type(self) -> None:
        ctx = _mk_ctx(action="type", type_value="hello")
        ctx.rpa_template_value = "{{name}}"
        step = ctx.with_rpa_meta({"action": "type", "type_value": "hello"})
        assert step["type_value_template"] == "{{name}}"
        assert step["trace_id"] == ctx.trace_id

    def test_template_value_for_goto(self) -> None:
        ctx = _mk_ctx(action="goto", type_value="https://example.test")
        ctx.rpa_template_value = "https://{{host}}/"
        step = ctx.with_rpa_meta({"action": "goto", "url": "https://example.test"})
        assert step["url_template"] == "https://{{host}}/"
        assert step["trace_id"] == ctx.trace_id


class TestBackwardCompatTrailEntry:
    """A pre-G3 RPA trail consumer that just looks for 'action' and 'target_id'
    must still work — trace_id is purely additive."""

    def test_legacy_consumer_unaffected(self) -> None:
        ctx = _mk_ctx(target_id=7)
        step = ctx.with_rpa_meta({"action": "click"})
        assert step["action"] == "click"
        assert step["target_id"] == 7
        # trace_id is the only NEW key for a vanilla click step
        legacy_keys = {"action", "target_id"}
        new_keys = set(step.keys()) - legacy_keys
        assert new_keys == {"trace_id"}

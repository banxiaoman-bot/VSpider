"""Regression tests for the symmetric read-only / tree-checkbox actions:

* ``extract_row`` — text-located row read into ``workflow_memory``
* ``tree_check`` — text-located tree node checkbox toggle (idempotent)

Tests cover:
  - Pydantic schema acceptance in ``VSpiderAction``
  - Skill prompt updates (ROW_ACTION_SKILL, TREE_SKILL)
  - Handler registration
  - Schema parsing (1 / 2 / 3 segment cases, empty values, op tokens)
  - Cross-iframe iteration via stub frames
  - Memory writeback for extract_row
  - Idempotent no-op for tree_check when already in target state
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from visual_web_agent.actions import (
    ActionExecutionError,
    ActionRegistry,
    ExtractRowHandler,
    TreeCheckHandler,
)
from visual_web_agent.prompt_skills import ROW_ACTION_SKILL, TREE_SKILL
from visual_web_agent.vlm_client import VSpiderAction


# ════════════════════════════════════════════════════════════════════
#                       SCHEMA / VLM CLIENT
# ════════════════════════════════════════════════════════════════════


class TestVlmClientLiterals:
    def test_extract_row_passes_validation(self) -> None:
        v = VSpiderAction(
            action="extract_row",
            target_id=0,
            type_value="ORD-2024-001||status",
            memory_key="order_status",
        )
        assert v.action == "extract_row"

    def test_tree_check_passes_validation(self) -> None:
        v = VSpiderAction(
            action="tree_check",
            target_id=0,
            type_value="Settings||check",
            memory_key="",
        )
        assert v.action == "tree_check"

    def test_unknown_action_still_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VSpiderAction(
                action="not_a_real_action",
                target_id=0,
                type_value="",
                memory_key="",
            )


# ════════════════════════════════════════════════════════════════════
#                       SKILL PROMPT CONTENT
# ════════════════════════════════════════════════════════════════════


class TestRowActionSkillExtraction:
    def test_documents_extract_row(self) -> None:
        assert "extract_row" in ROW_ACTION_SKILL
        # The 2-segment schema with column == "*" is mentioned
        assert "||*" in ROW_ACTION_SKILL or "||\\*" in ROW_ACTION_SKILL or "*" in ROW_ACTION_SKILL
        # memory_key reference for downstream chaining
        assert "memory_key" in ROW_ACTION_SKILL

    def test_warns_against_click_then_extract_pattern(self) -> None:
        """The prompt must tell VLM not to "click row to expand then extract"."""
        skill = ROW_ACTION_SKILL.lower()
        assert "extract_row" in skill
        # Anti-pattern note
        assert "反模式" in ROW_ACTION_SKILL or "antipattern" in skill


class TestTreeSkillCheckbox:
    def test_documents_tree_check(self) -> None:
        assert "tree_check" in TREE_SKILL

    def test_documents_three_ops(self) -> None:
        # check / uncheck / toggle must all be advertised
        for op in ("check", "uncheck", "toggle"):
            assert op in TREE_SKILL.lower()

    def test_warns_against_click_text_label_pattern(self) -> None:
        """VLM commonly clicks the label thinking it'll toggle the checkbox."""
        # Tells VLM that click_text on the label likely won't toggle check state
        assert "click_text" in TREE_SKILL


# ════════════════════════════════════════════════════════════════════
#                       HANDLER REGISTRATION
# ════════════════════════════════════════════════════════════════════


class TestHandlerRegistration:
    def test_extract_row_registered(self) -> None:
        assert ActionRegistry._handlers["extract_row"] is ExtractRowHandler

    def test_tree_check_registered(self) -> None:
        assert ActionRegistry._handlers["tree_check"] is TreeCheckHandler


# ════════════════════════════════════════════════════════════════════
#                       SCHEMA PARSING
# ════════════════════════════════════════════════════════════════════


def _make_ctx(
    *,
    type_value: str,
    page: Any,
    memory_key: str = "",
    workflow_memory: dict | None = None,
) -> SimpleNamespace:
    action = SimpleNamespace(
        target_id=0,
        type_value=type_value,
        memory_key=memory_key,
    )
    browser = _make_browser_stub()
    return SimpleNamespace(
        action=action,
        browser=browser,
        page=page,
        workflow_memory=workflow_memory if workflow_memory is not None else {},
        with_rpa_meta=lambda d: d,
    )


def _make_browser_stub() -> SimpleNamespace:
    async def _no_op(*_, **__):
        return None

    return SimpleNamespace(
        _clear_som_overlays=_no_op,
        _LOCATOR_TIMEOUT=1000,
        _wait_after_action=_no_op,
        _tab_switch_notice=None,
        _last_native_dialog=None,
        rpa_trail=[],
    )


class TestExtractRowSchema:
    def test_no_separator_rejected(self) -> None:
        ctx = _make_ctx(type_value="just one segment", page=object())
        with pytest.raises(ActionExecutionError, match=r"\|\|"):
            asyncio.run(ExtractRowHandler().execute(ctx))

    def test_too_many_segments_rejected(self) -> None:
        ctx = _make_ctx(type_value="a||b||c", page=object())
        with pytest.raises(ActionExecutionError, match=r"严格 2|得到"):
            asyncio.run(ExtractRowHandler().execute(ctx))

    def test_empty_values_rejected(self) -> None:
        ctx = _make_ctx(type_value="||status", page=object())
        with pytest.raises(ActionExecutionError, match=r"不能为空"):
            asyncio.run(ExtractRowHandler().execute(ctx))


class TestTreeCheckSchema:
    def test_empty_value_rejected(self) -> None:
        ctx = _make_ctx(type_value="", page=object())
        with pytest.raises(ActionExecutionError, match=r"必须|empty|提供"):
            asyncio.run(TreeCheckHandler().execute(ctx))

    @pytest.mark.parametrize("op", ["check", "uncheck", "toggle"])
    def test_known_ops_accepted(self, op: str) -> None:
        # The op is parsed before frame iteration, so a stub page with no
        # frames lets us assert "no-not-found-because-of-bad-op" path:
        # we expect a "no tree node found" error, NOT a schema error.
        page = _StubPage([])
        ctx = _make_ctx(type_value=f"NodeText||{op}", page=page)
        with pytest.raises(ActionExecutionError) as exc_info:
            asyncio.run(TreeCheckHandler().execute(ctx))
        msg = str(exc_info.value)
        assert "找不到" in msg or "frame" in msg
        assert "op" not in msg  # not an op-validation error

    def test_unknown_op_rejected(self) -> None:
        ctx = _make_ctx(type_value="Node||delete", page=object())
        with pytest.raises(ActionExecutionError, match=r"check.*uncheck|op"):
            asyncio.run(TreeCheckHandler().execute(ctx))


# ════════════════════════════════════════════════════════════════════
#                       STUB FRAME / LOCATOR
# ════════════════════════════════════════════════════════════════════


class _StubLocator:
    """Generic stub used by both handlers' frame iteration tests."""

    def __init__(
        self,
        *,
        row_count: int = 0,
        inner_text: str = "row text",
        eval_result: Any = None,
    ):
        self._row_count = row_count
        self._inner_text = inner_text
        self._eval_result = eval_result
        self.scroll_called = False

    def filter(self, **_):
        return self

    def locator(self, sel: str):
        # Drilling: the child locator returns whatever was specified at
        # construction time (so tree_check's checkbox-locator returns
        # itself, allowing click_called to be observed).
        return self

    async def count(self):
        return self._row_count

    def nth(self, _i: int):
        return self

    @property
    def first(self):
        return self

    async def is_visible(self):
        return True

    async def inner_text(self):
        return self._inner_text

    async def scroll_into_view_if_needed(self, **_):
        self.scroll_called = True
        return None

    async def click(self, **_):
        return None

    async def evaluate(self, _script: str, *_args, **_kwargs):
        return self._eval_result


class _StubFrame:
    def __init__(
        self,
        name: str,
        *,
        locator_factory=None,
    ):
        self.url = f"https://example.test/{name}"
        self.name = name
        self._factory = locator_factory or (lambda sel: _StubLocator())
        self.locator_calls: list[str] = []

    def locator(self, sel: str):
        self.locator_calls.append(sel)
        return self._factory(sel)


class _StubPage:
    def __init__(self, frames):
        self.frames = list(frames)
        self.main_frame = self.frames[0] if self.frames else None

    async def evaluate(self, *_, **__):
        return False  # no canvas


# ════════════════════════════════════════════════════════════════════
#                  EXTRACT_ROW: cross-iframe + memory writeback
# ════════════════════════════════════════════════════════════════════


class TestExtractRowMemoryWriteback:
    def test_whole_row_capture(self) -> None:
        """type_value '...||*' captures the entire row's inner_text into memory."""

        def factory(sel: str):
            return _StubLocator(row_count=1, inner_text="alice  active  edit")

        frame = _StubFrame("main", locator_factory=factory)
        page = _StubPage([frame])
        memory: dict[str, Any] = {}
        ctx = _make_ctx(
            type_value="alice||*",
            page=page,
            memory_key="alice_row",
            workflow_memory=memory,
        )
        asyncio.run(ExtractRowHandler().execute(ctx))
        assert memory.get("alice_row") == "alice  active  edit"
        assert memory.get("latest_memory") == "alice  active  edit"

    def test_column_capture_runs_evaluate(self) -> None:
        """Single-column read drops into the in-frame JS evaluator."""

        def factory(sel: str):
            # Cell text returned by the JS evaluator probe
            return _StubLocator(row_count=1, eval_result="active")

        frame = _StubFrame("main", locator_factory=factory)
        page = _StubPage([frame])
        memory: dict[str, Any] = {}
        ctx = _make_ctx(
            type_value="alice||status",
            page=page,
            memory_key="alice_status",
            workflow_memory=memory,
        )
        asyncio.run(ExtractRowHandler().execute(ctx))
        assert memory.get("alice_status") == "active"

    def test_auto_named_memory_key(self) -> None:
        """When VLM omits memory_key, handler synthesises a stable one."""

        def factory(sel: str):
            return _StubLocator(row_count=1, inner_text="x")

        frame = _StubFrame("main", locator_factory=factory)
        page = _StubPage([frame])
        memory: dict[str, Any] = {}
        ctx = _make_ctx(
            type_value="ORDER 123||*",
            page=page,
            memory_key="",
            workflow_memory=memory,
        )
        asyncio.run(ExtractRowHandler().execute(ctx))
        # No empty key, no spaces, no slashes
        assert "" not in [k for k in memory.keys() if k != "latest_memory"]
        keys = [k for k in memory if k.startswith("row_")]
        assert keys, f"expected a row_-prefixed key in {memory.keys()}"
        for k in keys:
            assert " " not in k

    def test_falls_through_to_iframe(self) -> None:
        """Tables in admin iframes are reached via frame iteration."""

        def empty(sel: str):
            return _StubLocator(row_count=0)

        def has_match(sel: str):
            return _StubLocator(row_count=1, inner_text="found")

        main = _StubFrame("main", locator_factory=empty)
        nested = _StubFrame("admin_iframe", locator_factory=has_match)
        page = _StubPage([main, nested])
        ctx = _make_ctx(
            type_value="ghost||*",
            page=page,
            memory_key="g",
            workflow_memory={},
        )
        asyncio.run(ExtractRowHandler().execute(ctx))
        assert main.locator_calls, "main frame skipped"
        assert nested.locator_calls, "iframe never reached"

    def test_failure_when_no_row_anywhere(self) -> None:
        def empty(sel: str):
            return _StubLocator(row_count=0)

        main = _StubFrame("main", locator_factory=empty)
        page = _StubPage([main])
        ctx = _make_ctx(
            type_value="ghost||*",
            page=page,
            memory_key="g",
            workflow_memory={},
        )
        with pytest.raises(ActionExecutionError, match=r"找不到|frame"):
            asyncio.run(ExtractRowHandler().execute(ctx))


# ════════════════════════════════════════════════════════════════════
#                  TREE_CHECK: idempotency + state read
# ════════════════════════════════════════════════════════════════════


class TestTreeCheckBehaviour:
    def test_check_when_already_checked_is_idempotent(self) -> None:
        """If the state matches the request, no click is dispatched."""

        clicked = []

        class _NodeStub(_StubLocator):
            async def evaluate(self, *_, **__):
                # Already checked
                return {"found": True, "checked": True}

            async def click(self, **_):
                clicked.append("clicked")

        def factory(sel: str):
            return _NodeStub(row_count=1)

        main = _StubFrame("main", locator_factory=factory)
        page = _StubPage([main])
        ctx = _make_ctx(
            type_value="Settings||check",
            page=page,
            workflow_memory={},
        )
        asyncio.run(TreeCheckHandler().execute(ctx))
        assert clicked == [], (
            f"tree_check check on already-checked node should be a no-op; "
            f"got clicks: {clicked}"
        )

    def test_uncheck_when_already_unchecked_is_idempotent(self) -> None:
        clicked = []

        class _NodeStub(_StubLocator):
            async def evaluate(self, *_, **__):
                return {"found": True, "checked": False}

            async def click(self, **_):
                clicked.append("clicked")

        def factory(sel: str):
            return _NodeStub(row_count=1)

        main = _StubFrame("main", locator_factory=factory)
        page = _StubPage([main])
        ctx = _make_ctx(
            type_value="Settings||uncheck",
            page=page,
            workflow_memory={},
        )
        asyncio.run(TreeCheckHandler().execute(ctx))
        assert clicked == []

    def test_check_when_unchecked_clicks(self) -> None:
        """When the desired state differs, the checkbox IS clicked."""
        clicked = []

        class _NodeStub(_StubLocator):
            async def evaluate(self, *_, **__):
                return {"found": True, "checked": False}

            async def click(self, **_):
                clicked.append("clicked")

        def factory(sel: str):
            return _NodeStub(row_count=1)

        main = _StubFrame("main", locator_factory=factory)
        page = _StubPage([main])
        ctx = _make_ctx(
            type_value="Settings||check",
            page=page,
            workflow_memory={},
        )
        asyncio.run(TreeCheckHandler().execute(ctx))
        assert clicked == ["clicked"], (
            f"tree_check check on unchecked node should click once; "
            f"got: {clicked}"
        )

    def test_no_checkbox_in_node_falls_through(self) -> None:
        """If the matched node has no checkbox, handler tries other selectors
        and ultimately raises a clean error."""

        class _NodeStub(_StubLocator):
            async def evaluate(self, *_, **__):
                return {"found": False}

        def factory(sel: str):
            return _NodeStub(row_count=1)

        main = _StubFrame("main", locator_factory=factory)
        page = _StubPage([main])
        ctx = _make_ctx(
            type_value="Settings||check",
            page=page,
            workflow_memory={},
        )
        with pytest.raises(ActionExecutionError, match=r"找不到|checkbox"):
            asyncio.run(TreeCheckHandler().execute(ctx))

    def test_default_op_is_check(self) -> None:
        """Omitting the op segment defaults to 'check'."""
        clicked = []

        class _NodeStub(_StubLocator):
            async def evaluate(self, *_, **__):
                return {"found": True, "checked": False}

            async def click(self, **_):
                clicked.append("clicked")

        def factory(sel: str):
            return _NodeStub(row_count=1)

        main = _StubFrame("main", locator_factory=factory)
        page = _StubPage([main])
        ctx = _make_ctx(
            type_value="Settings",
            page=page,
            workflow_memory={},
        )
        asyncio.run(TreeCheckHandler().execute(ctx))
        # default op=check; was unchecked -> should click
        assert clicked == ["clicked"]

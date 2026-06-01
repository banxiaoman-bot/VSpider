"""Regression tests for the v2 enhancements to row_action:

  1. Iterates `page.frames` (cross-iframe support, not just main page).
  2. Optional auto-confirm via 3-segment ``||confirm`` flag.
  3. Canvas/SVG fallback hint in the failure error message.

These tests run against the real handler with a stub page/frame model
(no Playwright runtime), exercising the schema parsing, frame iteration
order, auto-confirm token recognition, and the failure-path probe.

The full browser-level integration is covered by the existing
``test_row_action_handler.py`` suite when a Playwright context is
available.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from visual_web_agent.actions import (
    ActionExecutionError,
    ActionRegistry,
    RowActionHandler,
)
from visual_web_agent.prompt_skills import ROW_ACTION_SKILL


# ════════════════════════════════════════════════════════════════════
#                    PROMPT CONTENT (v2 schema documented)
# ════════════════════════════════════════════════════════════════════


class TestRowActionSkillV2Documents:
    def test_documents_three_segment_schema(self) -> None:
        # The 3-segment confirm syntax must be visible to VLM.
        assert "||confirm" in ROW_ACTION_SKILL
        assert "v2 schema" in ROW_ACTION_SKILL or "v2 schema (3" in ROW_ACTION_SKILL

    def test_lists_recognised_confirm_tokens(self) -> None:
        # Tells VLM what 3rd-segment values trigger auto-confirm.
        for tok in ("confirm", "yes", "ok"):
            assert tok in ROW_ACTION_SKILL.lower()

    def test_warns_about_canvas_fallback(self) -> None:
        skill = ROW_ACTION_SKILL.lower()
        assert "canvas" in skill
        assert "data_export" in skill

    def test_mentions_iframe_capability(self) -> None:
        # VLM should know that the handler already searches iframes,
        # so it doesn't try to switch_frame manually first.
        assert "iframe" in ROW_ACTION_SKILL.lower()


# ════════════════════════════════════════════════════════════════════
#                    SCHEMA / TOKEN RECOGNITION
# ════════════════════════════════════════════════════════════════════


class TestAutoConfirmTokens:
    """Class-level token whitelist must accept the canonical confirm words."""

    @pytest.mark.parametrize(
        "token",
        ["confirm", "yes", "ok", "true", "1", "确认", "确定", "y"],
    )
    def test_recognised(self, token: str) -> None:
        assert token in RowActionHandler._AUTO_CONFIRM_TOKENS

    @pytest.mark.parametrize(
        "token",
        ["maybe", "no", "cancel", "false", "0", "取消", ""],
    )
    def test_rejected(self, token: str) -> None:
        assert token not in RowActionHandler._AUTO_CONFIRM_TOKENS

    def test_label_priority_cn_first(self) -> None:
        """Confirm-button labels are searched in priority order: CN → EN.
        This matters because Element Plus shows both '确定' AND 'OK' on
        some i18n setups, but '确定' is the localised default."""
        labels = RowActionHandler._CONFIRM_LABELS
        cn_idx = labels.index("确定")
        en_idx = labels.index("OK")
        assert cn_idx < en_idx, (
            f"CN label '确定' (idx={cn_idx}) should come before EN 'OK' "
            f"(idx={en_idx}) in _CONFIRM_LABELS"
        )

    def test_modal_scopes_cover_major_frameworks(self) -> None:
        scopes_str = " ".join(RowActionHandler._CONFIRM_MODAL_SCOPES)
        # Element Plus, Ant Design, Naive, Bootstrap, generic ARIA
        for marker in (
            "el-message-box",
            "ant-modal-confirm",
            "el-popconfirm",
            "[role=alertdialog]",
            ".n-modal",
        ):
            assert marker in scopes_str, f"missing modal scope: {marker}"


# ════════════════════════════════════════════════════════════════════
#                    SCHEMA PARSING (handler input validation)
# ════════════════════════════════════════════════════════════════════


def _make_handler() -> RowActionHandler:
    return ActionRegistry._handlers["row_action"]()


def _make_ctx(
    type_value: str,
    *,
    page: Any = None,
    target_id: int = 0,
) -> SimpleNamespace:
    """Build a minimal ActionContext that the handler can consume."""
    action = SimpleNamespace(target_id=target_id, type_value=type_value)
    browser = SimpleNamespace()
    return SimpleNamespace(action=action, browser=browser, page=page)


class TestSchemaParsing:
    """The handler must reject malformed input loudly so VLM can retry."""

    def test_no_separator_rejected(self) -> None:
        h = _make_handler()
        ctx = _make_ctx("just one segment", page=object())
        with pytest.raises(ActionExecutionError, match=r"\|\|"):
            asyncio.run(h.execute(ctx))

    def test_too_many_segments_rejected(self) -> None:
        h = _make_handler()
        ctx = _make_ctx("a||b||c||d", page=object())
        with pytest.raises(ActionExecutionError, match=r"4 段|得到"):
            asyncio.run(h.execute(ctx))

    def test_empty_row_filter_rejected(self) -> None:
        h = _make_handler()
        ctx = _make_ctx("||删除", page=object())
        with pytest.raises(ActionExecutionError, match=r"不能为空|empty"):
            asyncio.run(h.execute(ctx))

    def test_empty_button_text_rejected(self) -> None:
        h = _make_handler()
        ctx = _make_ctx("张三||", page=object())
        with pytest.raises(ActionExecutionError, match=r"不能为空|empty"):
            asyncio.run(h.execute(ctx))

    def test_no_active_page_rejected(self) -> None:
        h = _make_handler()
        ctx = _make_ctx("张三||删除", page=None)
        with pytest.raises(ActionExecutionError, match=r"无活动页面"):
            asyncio.run(h.execute(ctx))


# ════════════════════════════════════════════════════════════════════
#               FRAME ITERATION (cross-iframe support)
# ════════════════════════════════════════════════════════════════════


class _StubLocator:
    """A fake locator that returns a fixed count + scripted clicks.

    Behaviour:
      * ``filter(has_text=...)`` returns self (chained call); the locator
        records the filter so tests can inspect it.
      * ``count()`` returns a configurable integer.
      * ``locator(...)`` returns another stub (drilling into rows/buttons).
      * ``first.click()`` / ``scroll_into_view_if_needed()`` are no-ops.
    """

    def __init__(self, *, row_count: int = 0, button_count: int = 0,
                 visible: bool = True, inner_text: str = "row"):
        self._row_count = row_count
        self._button_count = button_count
        self._visible = visible
        self._inner_text = inner_text
        self._is_button_locator = False
        self.click_called = False

    def filter(self, **kwargs):
        return self

    def locator(self, sel: str):
        # When a row stub drills into 'button', return a button stub.
        # The child must carry button_count=1 so its async count() reports
        # 1 visible button (it's the BUTTON locator, not the row locator).
        child = _StubLocator(
            row_count=0,
            button_count=self._button_count,
            visible=self._visible,
            inner_text=self._inner_text,
        )
        child._is_button_locator = True
        return child

    async def count(self):
        return self._button_count if self._is_button_locator else self._row_count

    def nth(self, i: int):
        return self

    @property
    def first(self):
        return self

    async def is_visible(self):
        return self._visible

    async def inner_text(self):
        return self._inner_text

    async def scroll_into_view_if_needed(self, **_):
        return None

    async def click(self, **_):
        self.click_called = True
        return None

    async def evaluate(self, *_args, **_kwargs):
        return None


class _StubFrame:
    def __init__(self, name: str, *, has_match: bool = False):
        self.url = f"https://example.test/{name}"
        self.name = name
        self._has_match = has_match
        self.locator_calls: list[str] = []

    def locator(self, sel: str):
        self.locator_calls.append(sel)
        if self._has_match:
            return _StubLocator(row_count=1, button_count=1)
        return _StubLocator(row_count=0)


class _StubPage:
    def __init__(self, frames):
        self.frames = list(frames)
        self.main_frame = self.frames[0] if self.frames else None
        self._eval_result: bool = False

    async def evaluate(self, *_args, **_kwargs):
        return self._eval_result


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


class TestFrameIteration:
    """The handler must search ALL frames, not just the top-level page."""

    def test_main_frame_searched_first(self) -> None:
        """When the main frame has a match, iframe frames are skipped."""
        main = _StubFrame("main", has_match=True)
        nested = _StubFrame("admin_iframe", has_match=True)
        page = _StubPage([main, nested])
        browser = _make_browser_stub()

        ctx = SimpleNamespace(
            action=SimpleNamespace(target_id=0, type_value="alice||delete"),
            browser=browser,
            page=page,
            with_rpa_meta=lambda d: d,
        )
        h = _make_handler()
        # Should not raise — match in main frame is enough
        asyncio.run(h.execute(ctx))
        # Main frame consulted; nested may or may not be (early-exit allowed)
        assert main.locator_calls, "main frame was never queried"

    def test_falls_through_to_iframe_when_main_empty(self) -> None:
        """Tables in admin iframes are reached via frame iteration."""
        main = _StubFrame("main", has_match=False)
        nested = _StubFrame("admin_iframe", has_match=True)
        page = _StubPage([main, nested])
        browser = _make_browser_stub()

        ctx = SimpleNamespace(
            action=SimpleNamespace(target_id=0, type_value="alice||delete"),
            browser=browser,
            page=page,
            with_rpa_meta=lambda d: d,
        )
        h = _make_handler()
        asyncio.run(h.execute(ctx))
        # Both frames must have been probed
        assert main.locator_calls, "main frame was never queried"
        assert nested.locator_calls, "iframe was never queried (cross-frame search broken)"

    def test_failure_when_no_frame_has_match(self) -> None:
        main = _StubFrame("main", has_match=False)
        nested = _StubFrame("admin_iframe", has_match=False)
        page = _StubPage([main, nested])
        page._eval_result = False  # no canvas
        browser = _make_browser_stub()

        ctx = SimpleNamespace(
            action=SimpleNamespace(target_id=0, type_value="ghost||delete"),
            browser=browser,
            page=page,
            with_rpa_meta=lambda d: d,
        )
        h = _make_handler()
        with pytest.raises(ActionExecutionError, match=r"frame|找不到"):
            asyncio.run(h.execute(ctx))
        # Both frames probed before giving up
        assert main.locator_calls, "main frame was never queried"
        assert nested.locator_calls, "iframe was never queried"


# ════════════════════════════════════════════════════════════════════
#                    CANVAS FALLBACK HINT
# ════════════════════════════════════════════════════════════════════


class TestCanvasFallbackHint:
    """When row_action fails AND a large canvas/svg is present, the
    error message must point VLM at data_export."""

    def test_hint_appended_when_canvas_detected(self) -> None:
        main = _StubFrame("main", has_match=False)
        page = _StubPage([main])
        page._eval_result = True  # large canvas detected
        browser = _make_browser_stub()

        ctx = SimpleNamespace(
            action=SimpleNamespace(target_id=0, type_value="ghost||delete"),
            browser=browser,
            page=page,
            with_rpa_meta=lambda d: d,
        )
        h = _make_handler()
        with pytest.raises(ActionExecutionError) as exc_info:
            asyncio.run(h.execute(ctx))
        msg = str(exc_info.value)
        assert "data_export" in msg, (
            f"canvas detected but data_export hint missing: {msg!r}"
        )
        assert "canvas" in msg.lower() or "svg" in msg.lower()

    def test_no_hint_when_no_canvas(self) -> None:
        main = _StubFrame("main", has_match=False)
        page = _StubPage([main])
        page._eval_result = False  # no canvas
        browser = _make_browser_stub()

        ctx = SimpleNamespace(
            action=SimpleNamespace(target_id=0, type_value="ghost||delete"),
            browser=browser,
            page=page,
            with_rpa_meta=lambda d: d,
        )
        h = _make_handler()
        with pytest.raises(ActionExecutionError) as exc_info:
            asyncio.run(h.execute(ctx))
        msg = str(exc_info.value)
        # Without canvas, don't pollute the error with a data_export ref.
        assert "data_export" not in msg, (
            f"data_export hinted unnecessarily (no canvas on page): {msg!r}"
        )

    def test_canvas_probe_failure_safe(self) -> None:
        """If page.evaluate() blows up, the error path still surfaces a
        clean message (no nested exception leak)."""
        main = _StubFrame("main", has_match=False)

        class _BrokenPage(_StubPage):
            async def evaluate(self, *_, **__):
                raise RuntimeError("CSP blocked eval")

        page = _BrokenPage([main])
        browser = _make_browser_stub()

        ctx = SimpleNamespace(
            action=SimpleNamespace(target_id=0, type_value="ghost||delete"),
            browser=browser,
            page=page,
            with_rpa_meta=lambda d: d,
        )
        h = _make_handler()
        with pytest.raises(ActionExecutionError) as exc_info:
            asyncio.run(h.execute(ctx))
        assert "frame" in str(exc_info.value) or "找不到" in str(exc_info.value)
        # No canvas hint appended (probe was unsafe to run)
        assert "data_export" not in str(exc_info.value)


# ════════════════════════════════════════════════════════════════════
#                    DOC: schema enforcement summary
# ════════════════════════════════════════════════════════════════════


class TestRowActionV2DocstringClues:
    """The handler's class docstring must continue to advertise both the
    base 2-segment schema AND the v2 3-segment confirm flag, so anyone
    grepping the source can find it."""

    def test_docstring_has_both_schemas(self) -> None:
        doc = RowActionHandler.__doc__ or ""
        assert "||" in doc
        assert "confirm" in doc.lower()
        assert "ROW_ACTION_V2" in doc

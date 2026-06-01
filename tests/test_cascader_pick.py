"""Unit tests for cascader_pick macro + replay_step dispatcher."""

from __future__ import annotations

import asyncio

import pytest

from visual_web_agent import semantic_macros
from visual_web_agent.semantic_macros import Macro
from visual_web_agent.semantic_macros.cascader_pick import (
    _extract_path_from_goal,
    _extract_placeholder,
    _extract_section,
    parse,
)


# ── Goal parser ───────────────────────────────────────────────────────────────
def test_parse_basic_chinese_arrow_form() -> None:
    g = "在基础级联选择器中，依次点击展开：Guide -> Navigation -> Top Navigation。最后确认输入框里出现了对应的值。"
    step = parse(g)
    assert step is not None
    assert step["action"] == "cascader_pick"
    assert step["path"] == ["Guide", "Navigation", "Top Navigation"]
    assert step["anchor"]["section"] == "基础级联选择器"
    assert step["anchor"]["placeholder"] == "Select"
    assert step["anchor"]["occurrence"] == 1
    assert step["validate"]["input_should_contain_each"] == ["Guide", "Navigation", "Top Navigation"]


def test_parse_chinese_full_width_arrow() -> None:
    g = "依次选择：北京 → 海淀区 → 中关村"
    step = parse(g)
    assert step is not None
    assert step["path"] == ["北京", "海淀区", "中关村"]


def test_parse_english_phrase() -> None:
    g = 'In the cascader select the path: A > B > C'
    step = parse(g)
    assert step is not None
    assert step["path"] == ["A", "B", "C"]


def test_parse_returns_none_without_trigger() -> None:
    """Path-like syntax without any cascader-keyword trigger should not match —
    avoid hijacking unrelated goals that happen to use '>'."""
    assert parse("点击 A > 然后做 B > 完成 C") is None
    assert parse("提取表格里的数据") is None
    assert parse("") is None


def test_parse_requires_at_least_two_levels() -> None:
    """Single click isn't a cascader; let normal click handle it."""
    assert parse("依次展开：Guide") is None


def test_parse_stops_at_sentence_boundary() -> None:
    g = "依次点击：A -> B。然后输入 X -> Y -> Z"
    step = parse(g)
    assert step is not None
    assert step["path"] == ["A", "B"]


def test_extract_section_at_explicit_location() -> None:
    assert _extract_section("在 Basic Form 区域中依次选择 A -> B") == "Basic Form 区域"
    assert _extract_section("在基础级联选择器中，依次点击 X -> Y") == "基础级联选择器"


def test_extract_placeholder_prefers_quoted_hint() -> None:
    assert _extract_placeholder('占位符为「请选择」，依次 A -> B') == "请选择"
    assert _extract_placeholder("默认 cascader: A -> B") == "Select"


def test_extract_path_handles_mixed_separators() -> None:
    """If we ever encounter a goal mixing arrows, the FIRST consistent run
    is what we want; tolerate both -> and →."""
    g = "依次选择：A -> B → C"
    parts = _extract_path_from_goal(g)
    assert parts == ["A", "B", "C"]


def test_section_aliases_resolved_to_english_label() -> None:
    """When goal uses the Chinese demo-section name, we look up the English
    label so findInputByAnchor can match the element-plus.org h2 text."""
    g = "在基础级联选择器中，依次点击：A -> B"
    step = parse(g)
    # Either the explicit "在...中" capture wins, or the alias fallback fires.
    assert step is not None
    assert step["anchor"]["section"] in {"基础级联选择器", "Basic usage"}


def test_section_alias_used_when_no_explicit_anchor() -> None:
    g = "基础用法演示页：依次选择 A -> B"
    step = parse(g)
    assert step is not None
    assert step["anchor"]["section"] == "Basic usage"


def test_occurrence_parsed_from_chinese_and_english() -> None:
    g = "在第二个级联选择器里依次选择：A -> B"
    step = parse(g)
    assert step is not None
    assert step["anchor"]["occurrence"] == 2

    g = "依次选择 second cascader path A > B"
    step = parse(g)
    assert step is not None
    assert step["anchor"]["occurrence"] == 2

    g = "在第3个里依次选择：A -> B"
    step = parse(g)
    assert step is not None
    assert step["anchor"]["occurrence"] == 3


# ── Macro registered & discoverable ───────────────────────────────────────────
def test_macro_is_registered() -> None:
    macro = semantic_macros.get("cascader_pick")
    assert macro is not None
    assert macro.action == "cascader_pick"
    assert "cascader_pick" in semantic_macros.actions()


def test_parse_goal_routes_cascader_through_registry() -> None:
    """Calling the unified parse_goal entrypoint must find cascader_pick."""
    g = "在级联选择器中依次点击：Guide -> Form -> Basic"
    step = semantic_macros.parse_goal(g)
    assert step is not None
    assert step["action"] == "cascader_pick"
    assert step["path"] == ["Guide", "Form", "Basic"]


def test_build_macro_js_includes_cascader_body() -> None:
    macro = semantic_macros.get("cascader_pick")
    assert macro is not None
    js = semantic_macros.build_macro_js(macro.js_body)
    assert "const norm" in js  # shared primitive bundle inlined
    assert "MENUITEM_SELECTOR" in js  # cascader body fragment
    assert "matchMenuItem" in js
    assert "path: pathLabels" in js  # body destructures step
    assert "async (step) =>" in js  # async wrapper from build_macro_js


# ── replay_step dispatcher (with a fake Page) ────────────────────────────────
class _FakePage:
    def __init__(self, evaluate_result):
        self._result = evaluate_result
        self.evaluate_calls = 0
        self.screenshot_calls = 0

    async def evaluate(self, js, arg):
        self.evaluate_calls += 1
        self.last_js = js
        self.last_arg = arg
        if callable(self._result):
            return self._result(arg)
        return self._result

    async def screenshot(self, **kwargs):
        self.screenshot_calls += 1
        return b"fake-jpeg-bytes"


class _FakeVLM:
    def __init__(self, verdict: str = "yes", reason: str = "looks right"):
        self.calls = []
        self._verdict = verdict
        self._reason = reason

    async def judge_screenshot(self, ss_b64, question, context=""):
        self.calls.append({"question": question, "context": context, "has_ss": ss_b64 is not None})
        return {"verdict": self._verdict, "reason": self._reason}


def _register_dummy(action="dummy", **kwargs):
    return Macro(
        action=action,
        parse=lambda g: None,
        js_body="return {ok: true};",
        **kwargs,
    )


def test_replay_step_ok_path() -> None:
    async def go():
        with semantic_macros.snapshot_registry():
            semantic_macros.register(_register_dummy())
            page = _FakePage({"ok": True, "observed": "Guide / Form"})
            result = await semantic_macros.replay_step(
                page, {"action": "dummy", "path": ["Guide", "Form"]}
            )
            assert result["ok"] is True
            assert page.evaluate_calls == 1
            assert page.screenshot_calls == 0  # no VL fallback needed

    asyncio.run(go())


def test_replay_step_no_macro_raises() -> None:
    async def go():
        page = _FakePage({"ok": True})
        with pytest.raises(ValueError):
            await semantic_macros.replay_step(page, {"action": "unknown-action"})

    asyncio.run(go())


def test_replay_step_non_dict_return_synthesizes_failure() -> None:
    async def go():
        with semantic_macros.snapshot_registry():
            semantic_macros.register(_register_dummy())
            page = _FakePage("not-a-dict")
            result = await semantic_macros.replay_step(page, {"action": "dummy"})
            assert result["ok"] is False
            assert result["reason"] == "macro_returned_non_dict"
            assert result["raw"] == "not-a-dict"

    asyncio.run(go())


def test_replay_step_vl_judge_recovers_postcheck_failed() -> None:
    async def go():
        with semantic_macros.snapshot_registry():
            semantic_macros.register(
                _register_dummy(postcheck_question=lambda s, r: "did it work?")
            )
            page = _FakePage({"ok": False, "reason": "postcheck_failed", "observed": "X"})
            vlm = _FakeVLM(verdict="yes")
            result = await semantic_macros.replay_step(
                page, {"action": "dummy"}, vlm=vlm
            )
            assert result["ok"] is True
            assert result["vl_recovered"] is True
            assert result["vl_judge"]["verdict"] == "yes"
            assert page.screenshot_calls == 1
            assert len(vlm.calls) == 1

    asyncio.run(go())


def test_replay_step_vl_judge_no_for_postcheck() -> None:
    async def go():
        with semantic_macros.snapshot_registry():
            semantic_macros.register(
                _register_dummy(postcheck_question=lambda s, r: "did it work?")
            )
            page = _FakePage({"ok": False, "reason": "postcheck_failed", "observed": "X"})
            vlm = _FakeVLM(verdict="no")
            result = await semantic_macros.replay_step(
                page, {"action": "dummy"}, vlm=vlm
            )
            assert result["ok"] is False
            assert result.get("vl_recovered") is None
            assert result["vl_judge"]["verdict"] == "no"

    asyncio.run(go())


def test_replay_step_vl_skipped_for_non_retryable_reason() -> None:
    """Non-retryable reasons (e.g. anchor_not_found) bypass the VL judge —
    no point asking the VLM if the input wasn't even located."""
    async def go():
        with semantic_macros.snapshot_registry():
            semantic_macros.register(
                _register_dummy(postcheck_question=lambda s, r: "should we recover?")
            )
            page = _FakePage({"ok": False, "reason": "anchor_not_found"})
            vlm = _FakeVLM(verdict="yes")
            result = await semantic_macros.replay_step(
                page, {"action": "dummy"}, vlm=vlm
            )
            assert result["ok"] is False
            assert "vl_judge" not in result
            assert page.screenshot_calls == 0
            assert len(vlm.calls) == 0

    asyncio.run(go())


def test_replay_step_vl_skipped_when_no_question_builder() -> None:
    async def go():
        with semantic_macros.snapshot_registry():
            # postcheck_question=None disables VL fallback
            semantic_macros.register(_register_dummy(postcheck_question=None))
            page = _FakePage({"ok": False, "reason": "postcheck_failed"})
            vlm = _FakeVLM(verdict="yes")
            result = await semantic_macros.replay_step(
                page, {"action": "dummy"}, vlm=vlm
            )
            assert result["ok"] is False
            assert page.screenshot_calls == 0

    asyncio.run(go())


def test_replay_step_screenshot_failure_doesnt_crash() -> None:
    """If page.screenshot raises, we still call VL with b64=None and the judge
    can return 'unclear'; replay_step must not propagate the exception."""
    async def go():
        with semantic_macros.snapshot_registry():
            semantic_macros.register(
                _register_dummy(postcheck_question=lambda s, r: "?")
            )

            class _BrokenPage(_FakePage):
                async def screenshot(self, **kwargs):
                    raise RuntimeError("screenshot service down")

            page = _BrokenPage({"ok": False, "reason": "postcheck_failed"})
            vlm = _FakeVLM(verdict="unclear")
            result = await semantic_macros.replay_step(
                page, {"action": "dummy"}, vlm=vlm
            )
            assert result["ok"] is False
            assert result["vl_judge"]["verdict"] == "unclear"
            # VL was still called (with b64=None)
            assert vlm.calls[0]["has_ss"] is False

    asyncio.run(go())

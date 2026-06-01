"""Unit tests for the semantic_macros registry framework."""

from __future__ import annotations

import pytest

from visual_web_agent import semantic_macros
from visual_web_agent.semantic_macros import Macro


# ── Macro dataclass invariants ────────────────────────────────────────────────
def test_macro_requires_action() -> None:
    with pytest.raises(ValueError):
        Macro(action="", parse=lambda g: None, js_body="return {ok:true};")


def test_macro_requires_callable_parser() -> None:
    with pytest.raises(TypeError):
        Macro(action="x", parse="not-callable", js_body="return {ok:true};")  # type: ignore[arg-type]


def test_macro_requires_non_empty_js_body() -> None:
    with pytest.raises(ValueError):
        Macro(action="x", parse=lambda g: None, js_body="")


def test_macro_is_immutable_frozen_dataclass() -> None:
    m = Macro(action="x", parse=lambda g: None, js_body="return {ok:true};")
    with pytest.raises(Exception):  # FrozenInstanceError
        m.action = "y"  # type: ignore[misc]


# ── Registry basics ───────────────────────────────────────────────────────────
def test_register_and_lookup() -> None:
    with semantic_macros.snapshot_registry():
        m = Macro(action="foo", parse=lambda g: None, js_body="return {ok:true};")
        semantic_macros.register(m)
        assert semantic_macros.get("foo") is m
        assert "foo" in semantic_macros.actions()


def test_unregister() -> None:
    with semantic_macros.snapshot_registry():
        m = Macro(action="foo", parse=lambda g: None, js_body="return {ok:true};")
        semantic_macros.register(m)
        assert semantic_macros.unregister("foo") is m
        assert semantic_macros.get("foo") is None
        assert semantic_macros.unregister("foo") is None  # idempotent


def test_register_rejects_non_macro() -> None:
    with semantic_macros.snapshot_registry():
        with pytest.raises(TypeError):
            semantic_macros.register({"action": "foo"})  # type: ignore[arg-type]


def test_snapshot_isolates_changes() -> None:
    """snapshot_registry must restore prior state even after register/unregister."""
    before = semantic_macros.actions()
    with semantic_macros.snapshot_registry():
        semantic_macros.register(
            Macro(action="x-snap", parse=lambda g: None, js_body="return {ok:true};")
        )
        assert "x-snap" in semantic_macros.actions()
    assert "x-snap" not in semantic_macros.actions()
    assert semantic_macros.actions() == before


# ── parse_goal: first-match by priority ───────────────────────────────────────
def test_parse_goal_returns_none_when_no_match() -> None:
    with semantic_macros.snapshot_registry():
        semantic_macros.register(
            Macro(action="no-match", parse=lambda g: None, js_body="return {ok:true};")
        )
        assert semantic_macros.parse_goal("anything") is None


def test_parse_goal_priority_lower_runs_first() -> None:
    with semantic_macros.snapshot_registry():
        semantic_macros.register(
            Macro(
                action="generic",
                parse=lambda g: {"action": "generic", "from": "generic"},
                js_body="return {ok:true};",
                priority=999,
            )
        )
        semantic_macros.register(
            Macro(
                action="specific",
                parse=lambda g: {"action": "specific", "from": "specific"} if "calendar" in g else None,
                js_body="return {ok:true};",
                priority=10,
            )
        )
        # Specific runs first (lower priority), matches → wins.
        result = semantic_macros.parse_goal("open calendar and pick a day")
        assert result is not None
        assert result["from"] == "specific"
        # When specific abstains, generic catches all.
        result = semantic_macros.parse_goal("anything else")
        assert result is not None
        assert result["from"] == "generic"


def test_parse_goal_ties_broken_by_action_name() -> None:
    with semantic_macros.snapshot_registry():
        semantic_macros.register(
            Macro(action="b", parse=lambda g: {"from": "b"}, js_body="return {ok:true};", priority=50)
        )
        semantic_macros.register(
            Macro(action="a", parse=lambda g: {"from": "a"}, js_body="return {ok:true};", priority=50)
        )
        assert semantic_macros.parse_goal("test")["from"] == "a"


def test_parse_goal_skips_buggy_parser() -> None:
    """A parser that raises must not block subsequent macros."""
    def crashing(_goal: str) -> dict | None:
        raise RuntimeError("simulated bug in parser")

    with semantic_macros.snapshot_registry():
        semantic_macros.register(
            Macro(action="crashes", parse=crashing, js_body="return {ok:true};", priority=10)
        )
        semantic_macros.register(
            Macro(
                action="works",
                parse=lambda g: {"from": "works"},
                js_body="return {ok:true};",
                priority=20,
            )
        )
        result = semantic_macros.parse_goal("hello")
        assert result == {"action": "works", "from": "works"}


def test_parse_goal_enforces_action_contract() -> None:
    """If the parser forgets to set step['action'], parse_goal injects it."""
    with semantic_macros.snapshot_registry():
        semantic_macros.register(
            Macro(
                action="x",
                parse=lambda g: {"some_param": 1},  # missing 'action' key
                js_body="return {ok:true};",
            )
        )
        result = semantic_macros.parse_goal("test")
        assert result is not None
        assert result["action"] == "x"
        assert result["some_param"] == 1


def test_parse_goal_handles_empty_goal() -> None:
    with semantic_macros.snapshot_registry():
        semantic_macros.register(
            Macro(action="x", parse=lambda g: {"from": "x"}, js_body="return {ok:true};")
        )
        assert semantic_macros.parse_goal("") is None
        assert semantic_macros.parse_goal(None) is None  # type: ignore[arg-type]


# ── build_macro_js ────────────────────────────────────────────────────────────
def test_build_macro_js_wraps_with_primitives() -> None:
    js = semantic_macros.build_macro_js("return {ok: true};")
    assert js.startswith("async (step) => {")
    assert "const norm" in js  # primitives present
    assert "const clickEl" in js
    assert "return {ok: true};" in js
    assert js.endswith("}")


def test_build_macro_js_rejects_empty_body() -> None:
    with pytest.raises(ValueError):
        semantic_macros.build_macro_js("")
    with pytest.raises(ValueError):
        semantic_macros.build_macro_js("   \n\t  ")


# ── Sanity: primitives bundle contains the expected helpers ───────────────────
def test_primitives_bundle_has_required_helpers() -> None:
    """Every primitive a macro might depend on must be present.

    If you remove or rename a primitive here, audit every macro JS body.
    """
    p = semantic_macros.MACRO_PRIMITIVES
    for needle in (
        "const norm",
        "const textOf",
        "const isVisible",
        "const allVisible",
        "const sleep",
        "const clickEl",
        "const findInputByAnchor",
        "const findVisiblePopper",
        "const sepNorm",
    ):
        assert needle in p, f"primitives bundle missing: {needle}"

"""Detect VLM emitting action metadata as a literal ``type_value`` string.

Direct regression for run_log_20260514_190658 (Wenxin chat):
  step 6:  {"action": "type", "target_id": 7,
            "type_value": "send_button_click_point_870_445"}
  step 11: {"action": "type", "target_id": 7,
            "type_value": "send_button_click_point_900_450"}
  step 14: {"action": "type", "target_id": 7, "type_value": "↑"}

VLM intended click_point but encoded the intent as a description string,
which got faithfully typed into the chat input box, destroying the real
question text.
"""

from __future__ import annotations

import pytest

from visual_web_agent.type_value_schema_guard import detect_metadata_in_type_value


def _decision(action: str, target_id: int, type_value: str) -> dict:
    return {
        "action": action,
        "target_id": target_id,
        "type_value": type_value,
        "thought": "",
        "memory_key": "",
    }


# ── Direct 19:06 reproducer ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad_tv",
    [
        "send_button_click_point_870_445",
        "send_button_click_point_900_450",
        "click_point_870_445",
        "click_button_x_870_y_445",
        "press_key_870_445",
    ],
)
def test_detects_action_plus_coord_metadata(bad_tv: str) -> None:
    """Exact pattern from the Wenxin failure: action-name + numeric coords."""
    msg = detect_metadata_in_type_value(_decision("type", 7, bad_tv))
    assert msg is not None
    assert "TYPE_VALUE SCHEMA GUARD" in msg
    assert "click_point" in msg  # suggested correct format


def test_extracts_coords_for_concrete_suggestion() -> None:
    """When coords appear in the bad type_value, the feedback should
    suggest the exact correct ``click_point`` JSON."""
    msg = detect_metadata_in_type_value(
        _decision("type", 7, "send_button_click_point_870_445")
    )
    assert msg is not None
    # Should suggest click_point with the extracted coordinates
    assert "[870" in msg or "[870,445]" in msg or "870,445" in msg
    assert "click_point" in msg


# ── Lone arrow / icon glyphs ─────────────────────────────────────────────────
@pytest.mark.parametrize("arrow", ["↑", "→", "▶", "►", "➤", "➔", "⏎"])
def test_detects_lone_arrow_glyph(arrow: str) -> None:
    msg = detect_metadata_in_type_value(_decision("type", 7, arrow))
    assert msg is not None
    assert "图标" in msg or "icon" in msg.lower() or "send" in msg.lower()


def test_arrow_inside_real_text_is_NOT_flagged() -> None:
    """A real user message that happens to contain an arrow IS fine."""
    msg = detect_metadata_in_type_value(
        _decision("type", 7, "点击 ↑ 按钮提交订单")
    )
    assert msg is None


# ── Bare action-keyword identifier ───────────────────────────────────────────
@pytest.mark.parametrize(
    "tv",
    [
        "click_point",
        "send_button",
        "submit_button",
        "press_key",
        "action_metadata",
    ],
)
def test_detects_bare_action_keyword(tv: str) -> None:
    msg = detect_metadata_in_type_value(_decision("type", 7, tv))
    assert msg is not None


# ── Negative: real user text stays untouched ─────────────────────────────────
@pytest.mark.parametrize(
    "real_text",
    [
        "介绍一下 deepseek",
        "请帮我写一段 Python 代码",
        "What is the weather in 上海 today?",
        "VLM Agent",
        "登录密码 abc123",
        "send the report to me tomorrow at 9am",  # contains "send" but not a metadata pattern
        "1234567890",
        "user@example.com",
        "https://example.com/path?q=test",
        "联系电话 138-1234-5678",
    ],
)
def test_legitimate_user_text_not_flagged(real_text: str) -> None:
    msg = detect_metadata_in_type_value(_decision("type", 7, real_text))
    assert msg is None, f"false positive on legitimate text: {real_text!r}"


# ── Negative: non-type actions are not our concern ───────────────────────────
@pytest.mark.parametrize(
    "non_text_action",
    ["click", "scroll", "press_key", "switch_tab", "close_tab", "done", "wait"],
)
def test_non_text_actions_pass_through(non_text_action: str) -> None:
    """Actions where type_value isn't user text (e.g. press_key has key names)
    shouldn't be policed by this guard."""
    msg = detect_metadata_in_type_value(
        _decision(non_text_action, 0, "click_point_870_445")
    )
    assert msg is None


# ── Affects all text-value actions, not just type ────────────────────────────
@pytest.mark.parametrize(
    "text_action",
    ["type", "click_text", "find_text", "save_to_memory", "hover_and_click", "form_set"],
)
def test_fires_on_all_text_value_actions(text_action: str) -> None:
    msg = detect_metadata_in_type_value(
        _decision(text_action, 7, "send_button_click_point_870_445")
    )
    assert msg is not None


# ── Defensive: edge cases ────────────────────────────────────────────────────
def test_empty_type_value_passes() -> None:
    msg = detect_metadata_in_type_value(_decision("type", 7, ""))
    assert msg is None


def test_non_dict_input_passes() -> None:
    msg = detect_metadata_in_type_value("not a dict")  # type: ignore[arg-type]
    assert msg is None


def test_missing_action_passes() -> None:
    msg = detect_metadata_in_type_value({"type_value": "send_button_click_point_870_445"})
    assert msg is None


# ── Feedback content quality ─────────────────────────────────────────────────
def test_feedback_includes_proper_action_alternatives() -> None:
    msg = detect_metadata_in_type_value(
        _decision("type", 7, "send_button_click_point_870_445")
    )
    assert msg is not None
    # Must point to the THREE proper alternatives
    assert "click_point" in msg
    assert "click_text" in msg
    # Must explicitly forbid the failed pattern
    assert "严禁" in msg


def test_feedback_quotes_offending_value() -> None:
    """User needs to see the exact bad string in the feedback to map it
    back to their VLM thought."""
    bad = "send_button_click_point_870_445"
    msg = detect_metadata_in_type_value(_decision("type", 7, bad))
    assert msg is not None
    # The exact bad string should appear (possibly truncated, but a prefix)
    assert "send_button" in msg


# ── Long real text containing 'send' is fine ─────────────────────────────────
def test_long_text_with_send_word_not_flagged() -> None:
    """Common false-positive avoidance: 'send' as English word in a real
    sentence shouldn't trigger."""
    msg = detect_metadata_in_type_value(
        _decision("type", 7, "Please send the latest deepseek report to my email tomorrow")
    )
    assert msg is None

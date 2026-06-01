"""Detect VLM emitting action-metadata as a literal ``type_value``.

Failure mode observed in run_log_20260514_190658:

  step 6:  {"action": "type", "target_id": 7,
            "type_value": "send_button_click_point_870_445"}
  step 11: {"action": "type", "target_id": 7,
            "type_value": "send_button_click_point_900_450"}
  step 14: {"action": "type", "target_id": 7, "type_value": "↑"}

The VLM intended ``click_point(point=[870, 445])`` etc. but encoded the
intent as a *description string* inside ``type_value``. The dispatcher
faithfully types that string into the chat input box — destroying the
real user text ("介绍一下deepseek") that step 3 had typed.

The VLM never recovers because each retry types another bogus description,
and the LOOP GUARD's URL-key reset hides the repetition.

This guard is **structural**: it inspects the shape of ``type_value`` for
patterns that almost certainly aren't real user text:

  * Looks like an action name (``click``, ``send_button``, ``click_point``,
    ``press_key``, ``coordinate``, ``tap_``)
  * Contains *embedded numeric coordinates* with underscore separators
    (``_870_445``, ``_900_450``)
  * Is a lone arrow/icon glyph (``↑`` / ``→`` / ``▶``) that's clearly meant
    for click_text but emitted as type

When detected, the guard rejects the decision before execution and tells
the VLM which proper action shape to use instead.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Action-metadata "smell" patterns. We intentionally allow Chinese / English
# user text — a real prompt like "click here please" wouldn't be flagged
# because there'd be no embedded numeric coordinates AND no underscore-token
# action prefix.
_SCHEMA_LEAK_RE = re.compile(
    r"(?:^|[_\-])"               # start of token
    r"(?:click|tap|press|send|submit|hover|drag|select|scroll|focus|blur)"
    r"(?:[_\-](?:button|point|key|target|coord|coordinate|elem|element|tab|icon))?"
    # Coordinate tail. Tolerate optional axis labels: ``_870_445`` /
    # ``_x870_y445`` / ``_x_870_y_445`` all match. Each tail segment must
    # ultimately resolve to a digit run.
    r"(?:[_\-][a-z]*[_\-]?\d+){1,4}",
    re.IGNORECASE,
)

# Bare action-keyword + numeric pair anywhere (catches "send button 870,445")
_ACTION_PLUS_COORD_RE = re.compile(
    r"(?:click_point|click_text|send_button|submit_button|press_key|"
    r"coordinate_pair|action_metadata)",
    re.IGNORECASE,
)

# Lone arrow / send-icon glyphs typed into the input
_LONE_ARROW_GLYPHS = frozenset({
    "↑", "→", "▶", "►", "➤", "➔", "⮕", "↪", "⏎",
})

# Actions whose ``type_value`` SHOULD be user text (not metadata)
_TEXT_VALUE_ACTIONS = frozenset({
    "type", "click_text", "find_text", "save_to_memory", "hover_and_click", "form_set",
})


def detect_metadata_in_type_value(head: dict[str, Any]) -> str | None:
    """Return feedback when ``type_value`` looks like action metadata; else None.

    Conservative by design: only fires on highly specific patterns. A real
    user prompt like "send me the report tomorrow at 9am" would NOT match
    (no underscore-separated action+coord pattern, no lone arrow glyph).
    """
    if not isinstance(head, dict):
        return None
    action = str(head.get("action") or "")
    if action not in _TEXT_VALUE_ACTIONS:
        return None

    tv = str(head.get("type_value") or "").strip()
    if not tv:
        return None
    # Cheap exits — most legitimate prompts are long, contain spaces, and
    # don't look like underscore-tokenized identifiers.
    if " " in tv and len(tv) > 30 and not _SCHEMA_LEAK_RE.search(tv):
        return None

    reason: str = ""
    suggestion: str = ""

    # Pattern 1: Lone arrow glyph being typed as text
    if tv in _LONE_ARROW_GLYPHS:
        reason = f"`type_value={tv!r}` 是一个孤立的箭头/图标符号"
        suggestion = (
            "  • 想点击送出按钮 → 用 `click_text` 配可见文字（如 \"发送\"），"
            "或 `click_point` 配归一化坐标，或 `press_key Enter`\n"
            "  • 不要把图标字符填到 type_value（它会被作为字面文本输入到聊天框）"
        )
    # Pattern 2: Action-name + coordinate descriptor (the 19:06 failure)
    elif _SCHEMA_LEAK_RE.search(tv):
        reason = (
            f"`type_value={tv!r}` 是一段含坐标的动作描述字符串，"
            "看起来像是想做 click_point 但 schema 错位了"
        )
        # Try to extract the coordinates and suggest the proper action
        nums = re.findall(r"\d+", tv)
        coord_hint = ""
        if len(nums) >= 2:
            x, y = nums[-2], nums[-1]
            coord_hint = (
                f"  • 推断你想点的归一化坐标是 [{x}, {y}]，"
                f"正确格式：`{{\"action\":\"click_point\",\"target_id\":0,"
                f"\"point\":[{x},{y}],\"type_value\":\"\",\"memory_key\":\"\"}}`\n"
            )
        suggestion = (
            coord_hint
            + "  • 想点击带文字的按钮 → `click_text` 配 type_value=\"按钮可见文字\"\n"
            "  • type_value 字段只用于输入真实文本，不要塞动作元数据"
        )
    # Pattern 3: Bare action-keyword identifier
    elif _ACTION_PLUS_COORD_RE.search(tv):
        reason = (
            f"`type_value={tv!r}` 含动作关键字（click_point/send_button 等），"
            "应该是动作名而不是文本"
        )
        suggestion = (
            "  • type_value 只放用户文本（如 \"介绍一下 deepseek\"）\n"
            "  • 想点击 → 把动作改成 `click` / `click_text` / `click_point`，不是 `type`"
        )
    else:
        return None

    logger.warning(
        "[TYPE_VALUE SCHEMA GUARD] %s: action=%s type_value=%r",
        reason, action, tv[:60],
    )
    return (
        f"⚠️ [TYPE_VALUE SCHEMA GUARD] {reason}。\n"
        f"  当前决策: action={action} target_id={head.get('target_id')} "
        f"type_value={tv[:80]!r}\n"
        "🚨 type_value 字段只用于**用户的真实文本内容**（要输入到 input 的字符串），"
        "不能是操作描述、坐标、按钮名、图标符号。\n"
        "下一步请用以下之一：\n"
        + suggestion
        + "\n⛔ 严禁继续把动作描述塞进 type_value。"
    )

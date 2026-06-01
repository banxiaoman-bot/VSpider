from __future__ import annotations

import json
import re

from visual_web_agent.chat_send_locator import (
    _SEND_BUTTON_SELECTORS,
    _build_locator_js,
)
from visual_web_agent.extraction_engine.strategies import infer_goal_strategy_context


def test_chat_send_locator_selectors_are_json_escaped() -> None:
    """Selectors with their own quotes must not break page.evaluate JS."""
    js = _build_locator_js()
    m = re.search(r"const SELECTORS = (\[.*?\]);", js, re.DOTALL)
    assert m, "SELECTORS array not found in locator JS"
    assert json.loads(m.group(1)) == list(_SEND_BUTTON_SELECTORS)


def test_chat_send_locator_does_not_hand_quote_css_selectors() -> None:
    """Regression for SyntaxError: Unexpected identifier '发送'."""
    js = _build_locator_js()
    assert "['[aria-label='" not in js
    assert "[\"[aria-label='" in js


def test_main_coerces_chat_submit_named_thoughts() -> None:
    """The VLM often says chat_submit in thought but emits click JSON."""
    source = open("visual_web_agent/main.py", encoding="utf-8").read()
    assert "thought_names_macro" in source
    assert "_rewrite_action" in source
    assert '"chat_submit"' in source
    assert "rewriting to %s" in source
    assert "重复点击无效按钮" in source


def test_chat_goal_strategy_prefers_chat_tools_before_generic_form() -> None:
    context = infer_goal_strategy_context(
        "打开页面，在文心助手输入框中输入 hermes 和 openclaw 的区别，然后发送并获取 AI 返回的内容",
        url="https://yiyan.baidu.com/",
    )
    assert "chat" in context["capabilities"]
    assert "chat_submit" in context["preferred_actions"]
    assert "chat_extract" in context["preferred_actions"]
    assert context["capabilities"].index("chat") < context["capabilities"].index("form")

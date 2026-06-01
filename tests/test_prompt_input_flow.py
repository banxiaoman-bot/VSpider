from visual_web_agent.prompt_skills import INPUT_FLOW_PROMPT, STATIC_PROMPT_PARTS


def test_input_flow_prompt_is_static() -> None:
    assert INPUT_FLOW_PROMPT in STATIC_PROMPT_PARTS


def test_input_flow_prompt_forbids_click_then_type_for_inputs() -> None:
    text = INPUT_FLOW_PROMPT.lower()
    assert "searchbox" in text
    assert "textbox" in text
    assert "use `type` directly" in text
    assert "do not emit a" in text
    assert "standalone `click`" in text


def test_input_flow_prompt_recommends_type_enter_batch() -> None:
    text = INPUT_FLOW_PROMPT
    assert "press_key" in text
    assert "Enter" in text
    assert "one batch" in text

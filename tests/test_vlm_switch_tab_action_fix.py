"""Regression: thought says switch_tab but action is click must not recover @eN."""

from __future__ import annotations

from visual_web_agent.vlm_client import VSpiderAction


def test_thought_switch_tab_coerces_click_to_switch_tab() -> None:
    act = VSpiderAction(
        action="click",
        target_id=0,
        type_value="",
        thought="下一步 switch_tab 切回第一个标签页，索引 0",
        status="success",
        memory_key="",
    )
    assert act.action == "switch_tab"
    assert act.target_id == 0
    assert act.type_value == "0"


def test_thought_switch_tab_does_not_recover_stale_element_id() -> None:
    act = VSpiderAction(
        action="click",
        target_id=0,
        type_value="",
        thought="已打开新标签，现在切换回标签 0；之前点的是 @e21",
        status="success",
        memory_key="",
    )
    assert act.action == "switch_tab"
    assert act.target_id == 0


def test_click_still_recovers_id_when_no_tab_intent() -> None:
    act = VSpiderAction(
        action="click",
        target_id=0,
        type_value="",
        thought="点击 @e21 打开第一条结果",
        status="success",
        memory_key="",
    )
    assert act.action == "click"
    assert act.target_id == 21

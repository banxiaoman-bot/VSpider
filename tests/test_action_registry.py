import asyncio

import pytest

from visual_web_agent.action_registry import build_default_action_registry
from visual_web_agent.extraction_engine.strategies import infer_goal_strategy_context
from visual_web_agent.main import _resolve_action_tool_metadata
from visual_web_agent.vlm_client import VSpiderAction


def _selected_names(goal: str) -> list[str]:
    registry = build_default_action_registry()
    return [tool["name"] for tool in registry.select_for_goal(goal)]


def _selected_names_with_strategy(goal: str, url: str = "") -> list[str]:
    registry = build_default_action_registry()
    context = infer_goal_strategy_context(goal, url=url)
    return [
        tool["name"]
        for tool in registry.select_for_goal(goal, strategy_context=context)
    ]


def test_selects_form_and_date_tools() -> None:
    names = _selected_names(
        "\u586b\u5199 Basic Form\uff0c\u9009\u62e9 Pick a date\uff0c"
        "\u4e0b\u4e2a\u6708 15 \u53f7\uff0c\u6700\u540e Create"
    )
    assert "auto_form_fill" in names
    assert "date_pick" in names
    assert "round_form_challenge" not in names


def test_selects_targeted_probe_for_local_element_lookup() -> None:
    names = _selected_names("局部感知探针 查找输入框 search input")
    assert names[0] == "targeted_probe"


def test_selects_round_form_for_rpa_challenge() -> None:
    names = _selected_names(
        "https://rpachallenge.com/ \u5728\u8868\u5355\u4e2d\u586b\u5165 "
        "First Name \u6700\u540e\u70b9\u51fb Submit"
    )
    assert names[0] == "round_form_challenge"
    assert "auto_form_fill" in names


def test_selects_table_and_pagination_tools() -> None:
    names = _selected_names(
        "\u6293\u53d6\u9875\u9762\u6838\u5fc3\u6570\u636e\u8868\u683c"
        "\u4e2d\u7684\u524d 30 \u6761\u5458\u5de5\u6570\u636e\uff0c"
        "\u70b9\u51fb Next \u7ffb\u9875"
    )
    assert "table_extract" in names
    assert "next_page" in names


def test_selects_interaction_extract_macros() -> None:
    modal_names = _selected_names(
        "Click 'Small modal', extract dialog text, close it, then click 'Large modal'."
    )
    assert "modal_dialog_macro" in modal_names

    docs_names = _selected_names(
        "https://reactrouter.com/ open Docs, click Upgrading from v6, then API components Form"
    )
    assert "reactrouter_docs_macro" in docs_names


def test_selects_file_and_http_guard_tools_from_strategy_context() -> None:
    upload_names = _selected_names_with_strategy(
        "点击选择文件按钮，上传本地 excel 文件",
        url="https://demoqa.com/upload-download",
    )
    assert "file_upload" in upload_names

    download_names = _selected_names_with_strategy(
        "点击 Download 按钮，下载 sampleFile.jpeg",
        url="https://demoqa.com/upload-download",
    )
    assert "file_download" in download_names

    http_names = _selected_names_with_strategy(
        "检测到404错误后输出检测到404，任务终止",
        url="https://www.httpbin.org/status/404",
    )
    assert "http_error_guard" in http_names


def test_resolves_action_to_tool_metadata() -> None:
    registry = build_default_action_registry()

    form_tool = registry.resolve_for_action("form_set", goal="填写表单")
    assert form_tool is not None
    assert form_tool["name"] == "auto_form_fill"
    assert form_tool["capability"] == "form"

    hover_tool = registry.resolve_for_action("hover_and_click", goal="hover dropdown")
    assert hover_tool is not None
    assert hover_tool["name"] == "hover_and_click"

    next_tool = registry.resolve_for_action("next_page", goal="点击 Next 翻页")
    assert next_tool is not None
    assert next_tool["name"] == "next_page"
    assert next_tool["capability"] == "pagination"

    probe_tool = registry.resolve_for_action(
        "targeted_probe",
        goal="find search input",
    )
    assert probe_tool is not None
    assert probe_tool["name"] == "targeted_probe"
    assert probe_tool["changes_state"] is False

    macro_tool = registry.resolve_for_action(
        "modal_dialog_macro",
        goal="extract Small modal dialog content",
    )
    assert macro_tool is not None
    assert macro_tool["name"] == "modal_dialog_macro"
    assert macro_tool["capability"] == "dialog"

    extract_tool = registry.resolve_for_action(
        "extract",
        goal="\u6293\u53d6\u9875\u9762\u6838\u5fc3\u6570\u636e\u8868\u683c",
    )
    assert extract_tool is not None
    assert extract_tool["name"] == "table_extract"

    fetch_tool = registry.resolve_for_action(
        "fetch_links_batch",
        goal="fetch content from the first 3 result links",
    )
    assert fetch_tool is not None
    assert fetch_tool["name"] == "fetch_links_batch"
    assert fetch_tool["changes_state"] is False


def test_generic_click_requires_download_goal_to_resolve_file_download() -> None:
    registry = build_default_action_registry()

    unrelated_click_tool = registry.resolve_for_action(
        "click",
        goal="填写 First name 和 Last name，然后点击 Submit",
    )
    assert unrelated_click_tool is None

    download_click_tool = registry.resolve_for_action(
        "click",
        goal="点击 Download 按钮，下载 sampleFile.jpeg",
    )
    assert download_click_tool is not None
    assert download_click_tool["name"] == "file_download"


def test_action_tool_metadata_ignores_auth_profile_noise_for_plain_click() -> None:
    registry = build_default_action_registry()
    goal = (
        "在页面中找到 First name 和 Last name 输入框，输入 测试 和 用户。然后点击 Submit 按钮。"
        "提交后断言页面包含 测试 用户。\n\n"
        "【认证环境（系统通用检测，非业务结论）】\n"
        "未启用 Auth Matrix（未设置 --auth-profiles / VSPIDER_AUTH_PROFILES）。\n"
        "通用检测：login_like_url=False; visible_password_inputs=0; visible_login_entries=2; "
        "captcha_or_2fa_like_text=False; stale_auth_profile=False。"
    )
    context = infer_goal_strategy_context(
        goal,
        url="https://www.w3schools.com/html/html_forms.asp",
    )
    selected_tools = registry.select_for_goal(goal, strategy_context=context)

    assert "file_download" not in [tool["name"] for tool in selected_tools]
    assert _resolve_action_tool_metadata(
        registry,
        "click",
        goal=goal,
        selected_tools=selected_tools,
    ) is None


def test_action_tool_metadata_keeps_download_label_for_download_click() -> None:
    registry = build_default_action_registry()
    goal = "点击 Download 按钮，下载 sampleFile.jpeg"
    context = infer_goal_strategy_context(
        goal,
        url="https://demoqa.com/upload-download",
    )
    selected_tools = registry.select_for_goal(goal, strategy_context=context)

    assert "file_download" in [tool["name"] for tool in selected_tools]
    tool_meta = _resolve_action_tool_metadata(
        registry,
        "click",
        goal=goal,
        selected_tools=selected_tools,
    )

    assert tool_meta is not None
    assert tool_meta["name"] == "file_download"


def test_executes_bound_async_tool_handler() -> None:
    registry = build_default_action_registry()

    async def handler(action_payload, workflow_memory=None):
        return {
            "action": action_payload["action"],
            "memory": dict(workflow_memory or {}),
        }

    registry.bind("next_page", handler)

    result = asyncio.run(
        registry.execute_for_action(
            "next_page",
            goal="点击 Next 翻页",
            action_payload={"action": "next_page"},
            workflow_memory={"k": "v"},
        )
    )

    assert result == {"action": "next_page", "memory": {"k": "v"}}


def test_execute_requires_bound_handler() -> None:
    registry = build_default_action_registry()

    with pytest.raises(RuntimeError, match="no bound handler"):
        asyncio.run(
            registry.execute_for_action(
                "hover_and_click",
                goal="hover dropdown",
                action_payload={"action": "hover_and_click"},
            )
        )


def test_vlm_action_accepts_targeted_probe() -> None:
    action = VSpiderAction(
        progress_review="need local candidates",
        thought="probe the likely search input",
        current_state="page loaded",
        action="targeted_probe",
        target_id=0,
        type_value="input | search input",
        memory_key="",
        extracted_data=None,
        status="pending",
    )

    assert action.action == "targeted_probe"
    assert action.target_id == 0


def test_vlm_action_accepts_fetch_links_batch() -> None:
    action = VSpiderAction(
        progress_review="need link contents",
        thought="batch fetch the visible result links",
        current_state="search results are visible",
        action="fetch_links_batch",
        target_id=0,
        type_value='{"target_ids":[16,32],"mode":"ax","selectors":["article"]}',
        memory_key="results",
        extracted_data=None,
        status="pending",
    )

    assert action.action == "fetch_links_batch"
    assert action.target_id == 0
    assert "target_ids" in action.type_value


def test_vlm_action_preserves_type_zero_for_targeted_handoff() -> None:
    action = VSpiderAction(
        progress_review="need to fill the search input",
        thought="the search box needs local input resolution",
        current_state="page loaded",
        action="type",
        target_id=0,
        type_value="vue3",
        memory_key="",
        extracted_data=None,
        status="pending",
    )

    assert action.action == "type"
    assert action.target_id == 0
    assert action.type_value == "vue3"
    assert "[TARGETED_TYPE_PENDING]" in action.thought


def test_vlm_action_converts_short_click_text_to_type_for_input_context() -> None:
    action = VSpiderAction(
        progress_review="need to fill the first name input",
        thought="@e67 is the First name textbox input, so the next step should type 测试",
        current_state="Example form textbox is visible",
        action="click",
        target_id=67,
        type_value="测试",
        memory_key="",
        extracted_data=None,
        status="pending",
    )

    assert action.action == "type"
    assert action.target_id == 67
    assert action.type_value == "测试"


def test_vlm_action_converts_search_click_text_to_type_for_medium_query() -> None:
    action = VSpiderAction(
        progress_review="need to search",
        thought="@e3 is the searchbox, type Python教程",
        current_state="Bing search input is focused",
        action="click",
        target_id=3,
        type_value="Python教程",
        memory_key="",
        extracted_data=None,
        status="pending",
    )

    assert action.action == "type"
    assert action.target_id == 3
    assert action.type_value == "Python教程"


def test_vlm_action_keeps_short_click_label_behavior_for_non_input_context() -> None:
    action = VSpiderAction(
        progress_review="need to click page 2",
        thought="the pagination button 2 is visible and should be clicked",
        current_state="pagination buttons are visible",
        action="click",
        target_id=12,
        type_value="2",
        memory_key="",
        extracted_data=None,
        status="pending",
    )

    assert action.action == "click"
    assert action.target_id == 12
    assert action.type_value == ""


def test_vlm_action_does_not_type_long_click_explanation() -> None:
    action = VSpiderAction(
        progress_review="search results are visible",
        thought="click the first result link in a new tab; do not type this explanation",
        current_state="result links are visible",
        action="click",
        target_id=30,
        type_value="click on first result link in a new tab, then switch back to the original tab",
        memory_key="",
        extracted_data=None,
        status="pending",
    )

    assert action.action == "click"
    assert action.target_id == 30
    assert action.type_value == ""


def test_vlm_action_converts_click_new_tab_type_value_to_action() -> None:
    action = VSpiderAction(
        progress_review="search results are visible",
        thought="click target #14 in a new tab",
        current_state="result links are visible",
        action="click",
        target_id=14,
        type_value="click_new_tab",
        memory_key="",
        extracted_data=None,
        status="pending",
    )

    assert action.action == "click_new_tab"
    assert action.target_id == 14
    assert action.type_value == ""


def test_vlm_action_click_with_scroll_reasoning_becomes_scroll() -> None:
    action = VSpiderAction(
        progress_review="First name and Last name are already filled",
        thought="Submit button is not in the current viewport, so we need to scroll down to it",
        current_state="@e67 and @e74 are already filled",
        action="click",
        target_id=0,
        type_value="",
        memory_key="",
        extracted_data=None,
        status="pending",
    )

    assert action.action == "scroll"
    assert action.target_id == 0
    assert action.type_value == "down"


def test_vlm_action_click_down_with_scroll_reasoning_stays_scroll() -> None:
    action = VSpiderAction(
        progress_review="@e67 and @e74 are already filled",
        thought="Submit button is below the viewport, continue to scroll down",
        current_state="submit not visible yet",
        action="click",
        target_id=0,
        type_value="down",
        memory_key="",
        extracted_data=None,
        status="pending",
    )

    assert action.action == "scroll"
    assert action.target_id == 0
    assert action.type_value == "down"

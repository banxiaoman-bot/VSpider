import asyncio

from visual_web_agent.search_reentry_guards import (
    apply_search_reentry_guard,
    parse_first_search_query,
    parse_reentry_query,
)


class _Frame:
    def __init__(self, state):
        self.state = state

    async def evaluate(self, script):
        return self.state


class _Page:
    def __init__(self, state):
        self.frames = [_Frame(state)]


class _Browser:
    def __init__(self, state, current_url=""):
        self.page = _Page(state)
        self.current_url = current_url

    async def _ensure_active_page(self, reason=""):
        return self.page


def _run(coro):
    return asyncio.run(coro)


def test_parse_reentry_query_chinese_goal() -> None:
    goal = "在必应搜索框里重新输入VLM Agent，点击搜索。最后关闭标签页。"
    assert parse_reentry_query(goal) == "VLM Agent"


def test_parse_first_search_query_skips_searchbox_reentry_phrase() -> None:
    assert parse_first_search_query("在必应搜索框里重新输入VLM Agent，点击搜索。") == ""


def test_rewrites_confused_click_to_type_enter() -> None:
    browser = _Browser({"target_id": 3, "value": "Python教程"})
    decisions = [{"action": "click", "target_id": 3, "type_value": "", "thought": "click searchbox and input VLM Agent"}]

    changed = _run(
        apply_search_reentry_guard(
            browser,
            decisions,
            goal="在必应搜索框里重新输入VLM Agent，点击搜索。",
        )
    )

    assert changed is True
    assert decisions[0]["action"] == "type"
    assert decisions[0]["target_id"] == 3
    assert decisions[0]["type_value"] == "VLM Agent"
    assert decisions[1]["action"] == "press_key"
    assert decisions[1]["type_value"] == "Enter"


def test_does_not_rewrite_before_first_search_stage() -> None:
    browser = _Browser({"target_id": 3, "value": "Python教程"}, current_url="https://cn.bing.com/")
    decisions = [{"action": "click", "target_id": 3, "type_value": "", "thought": "click searchbox"}]

    changed = _run(
        apply_search_reentry_guard(
            browser,
            decisions,
            goal=(
                "在必应搜索Python教程。点击第一个结果，确保在新标签页打开。"
                "然后切换回最初的必应标签页。在必应搜索框里重新输入VLM Agent，点击搜索。"
            ),
        )
    )

    assert changed is False
    assert decisions[0]["action"] == "click"


def test_rewrites_after_first_search_result_url_is_reached() -> None:
    browser = _Browser(
        {"target_id": 3, "value": "Python教程"},
        current_url="https://cn.bing.com/search?q=Python%E6%95%99%E7%A8%8B",
    )
    decisions = [{"action": "click", "target_id": 2, "type_value": "", "thought": "重新输入 VLM Agent and click search"}]

    changed = _run(
        apply_search_reentry_guard(
            browser,
            decisions,
            goal=(
                "在必应搜索Python教程。点击第一个结果，确保在新标签页打开。"
                "然后切换回最初的必应标签页。在必应搜索框里重新输入VLM Agent，点击搜索。"
            ),
        )
    )

    assert changed is True
    assert decisions[0]["action"] == "type"
    assert decisions[0]["type_value"] == "VLM Agent"


def test_does_not_rewrite_when_current_decision_is_still_first_result_click() -> None:
    browser = _Browser(
        {"target_id": 3, "value": "Python教程"},
        current_url="https://cn.bing.com/search?q=Python%E6%95%99%E7%A8%8B",
    )
    decisions = [
        {
            "action": "click_new_tab",
            "target_id": 14,
            "type_value": "",
            "thought": "click the first Python教程 search result in a new tab",
        }
    ]

    changed = _run(
        apply_search_reentry_guard(
            browser,
            decisions,
            goal=(
                "在必应搜索Python教程。点击第一个结果。"
                "然后切换回最初的必应标签页。在必应搜索框里重新输入VLM Agent，点击搜索。"
            ),
        )
    )

    assert changed is False
    assert decisions[0]["action"] == "click_new_tab"


def test_does_not_rewrite_when_value_already_matches() -> None:
    browser = _Browser({"target_id": 3, "value": "VLM Agent"})
    decisions = [{"action": "click", "target_id": 3, "type_value": ""}]

    changed = _run(
        apply_search_reentry_guard(
            browser,
            decisions,
            goal="重新输入VLM Agent，点击搜索。",
        )
    )

    assert changed is False
    assert decisions[0]["action"] == "click"


def test_does_not_rewrite_tab_management() -> None:
    browser = _Browser({"target_id": 3, "value": "Python教程"})
    decisions = [{"action": "switch_tab", "target_id": 0, "type_value": "0"}]

    changed = _run(
        apply_search_reentry_guard(
            browser,
            decisions,
            goal="重新输入VLM Agent，点击搜索。",
        )
    )

    assert changed is False
    assert decisions[0]["action"] == "switch_tab"

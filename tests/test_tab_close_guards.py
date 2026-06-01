import asyncio

from visual_web_agent.tab_close_guards import rewrite_tab_title_click_to_close_tab
from visual_web_agent.tab_close_guards import rewrite_redundant_close_to_done
from visual_web_agent.tab_close_guards import rewrite_close_intent_to_close_tab


class _Page:
    def __init__(self, title: str):
        self._title = title
        self._closed = False

    def is_closed(self) -> bool:
        return self._closed

    async def title(self) -> str:
        return self._title


class _Context:
    def __init__(self, pages):
        self.pages = pages


class _Browser:
    def __init__(self, pages, active_idx: int = 0):
        self._context = _Context(pages)
        self._page = pages[active_idx]


def _run(coro):
    return asyncio.run(coro)


def test_rewrites_tab_title_click_to_close_tab_index() -> None:
    pages = [_Page("VLM Agent - 搜索"), _Page("AI大学堂")]
    browser = _Browser(pages, active_idx=0)
    decisions = [
        {
            "action": "click_text",
            "target_id": 0,
            "type_value": "AI大学堂",
            "thought": "需要关闭 [1] AI大学堂 标签页，不关闭当前必应页。",
        }
    ]

    changed = _run(
        rewrite_tab_title_click_to_close_tab(
            browser,
            decisions,
            goal="最后关闭第一个结果的那个标签页，不要关闭当前必应页。",
        )
    )

    assert changed is True
    assert decisions[0]["action"] == "close_tab"
    assert decisions[0]["target_id"] == 0
    assert decisions[0]["type_value"] == "1"


def test_falls_back_to_tab_index_in_thought() -> None:
    pages = [_Page("VLM Agent - 搜索"), _Page("Some Result Page")]
    browser = _Browser(pages, active_idx=0)
    decisions = [
        {
            "action": "click_text",
            "target_id": 0,
            "type_value": "missing title",
            "thought": "先切换到标签页 [1]，然后 close_tab。",
        }
    ]

    changed = _run(
        rewrite_tab_title_click_to_close_tab(
            browser,
            decisions,
            goal="关闭第一个结果的新标签页。",
        )
    )

    assert changed is True
    assert decisions[0]["action"] == "close_tab"
    assert decisions[0]["type_value"] == "1"


def test_does_not_rewrite_non_close_goal() -> None:
    pages = [_Page("Bing"), _Page("AI大学堂")]
    browser = _Browser(pages, active_idx=0)
    decisions = [{"action": "click_text", "target_id": 0, "type_value": "AI大学堂"}]

    changed = _run(
        rewrite_tab_title_click_to_close_tab(
            browser,
            decisions,
            goal="点击页面里的 AI大学堂 链接。",
        )
    )

    assert changed is False
    assert decisions[0]["action"] == "click_text"


def test_does_not_rewrite_switch_back_intent_even_when_goal_has_later_close() -> None:
    pages = [_Page("VLM Agent - 搜索"), _Page("知乎文章")]
    browser = _Browser(pages, active_idx=0)
    decisions = [
        {
            "action": "click_text",
            "target_id": 0,
            "type_value": "返回到必应搜索",
            "thought": "当前需要切换回最初的必应搜索标签页，点击返回到必应搜索。",
        }
    ]

    changed = _run(
        rewrite_tab_title_click_to_close_tab(
            browser,
            decisions,
            goal="然后切换回最初的必应标签页。最后关闭第一个结果的那个标签页。",
        )
    )

    assert changed is False
    assert decisions[0]["action"] == "click_text"


def test_does_not_rewrite_current_tab_match() -> None:
    pages = [_Page("AI大学堂"), _Page("Bing")]
    browser = _Browser(pages, active_idx=0)
    decisions = [{"action": "click_text", "target_id": 0, "type_value": "AI大学堂"}]

    changed = _run(
        rewrite_tab_title_click_to_close_tab(
            browser,
            decisions,
            goal="关闭第一个结果的新标签页。",
        )
    )

    assert changed is False
    assert decisions[0]["action"] == "click_text"


def test_rewrites_redundant_close_to_done_when_only_current_tab_remains() -> None:
    pages = [_Page("VLM Agent - 搜索")]
    browser = _Browser(pages, active_idx=0)
    decisions = [
        {
            "action": "close_tab",
            "target_id": 1,
            "type_value": "1",
            "thought": "关闭第一个结果所打开的新标签页。",
        }
    ]

    changed = _run(
        rewrite_redundant_close_to_done(
            browser,
            decisions,
            goal="最后关闭第一个结果的那个标签页，不要关闭当前必应页。输出done。",
        )
    )

    assert changed is True
    assert decisions[0]["action"] == "done"
    assert decisions[0]["type_value"] == "done"
    assert decisions[0]["subgoal_status"] == "completed"


def test_rewrites_plain_click_close_intent_to_close_tab() -> None:
    pages = [_Page("VLM Agent - Search"), _Page("Python Tutorial")]
    browser = _Browser(pages, active_idx=0)
    decisions = [
        {
            "action": "click",
            "target_id": 2,
            "type_value": "",
            "thought": "Need to close tab [1] that contains the first result; do not close current tab.",
        }
    ]

    changed = _run(
        rewrite_close_intent_to_close_tab(
            browser,
            decisions,
            goal="Finally close the first result tab, do not close the current tab.",
        )
    )

    assert changed is True
    assert decisions[0]["action"] == "close_tab"
    assert decisions[0]["target_id"] == 0
    assert decisions[0]["type_value"] == "1"


def test_rewrites_close_intent_to_only_non_current_tab() -> None:
    pages = [_Page("VLM Agent - Search"), _Page("Python Tutorial")]
    browser = _Browser(pages, active_idx=0)
    decisions = [
        {
            "action": "wait",
            "target_id": 0,
            "type_value": "1",
            "thought": "Close the result tab now.",
        }
    ]

    changed = _run(
        rewrite_close_intent_to_close_tab(
            browser,
            decisions,
            goal="Close the result tab and keep the current tab.",
        )
    )

    assert changed is True
    assert decisions[0]["action"] == "close_tab"
    assert decisions[0]["type_value"] == "1"

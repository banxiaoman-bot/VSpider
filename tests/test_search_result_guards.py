import asyncio

from visual_web_agent.search_result_guards import (
    _probe_script,
    apply_search_result_open_guard,
    goal_requests_first_result_new_tab,
)


class _Frame:
    def __init__(self, result):
        self.result = result
        self.last_script = ""

    async def evaluate(self, script):
        self.last_script = script
        return self.result


class _Page:
    def __init__(self, result=None):
        self.frames = [_Frame(result)]
        self._closed = False

    def is_closed(self):
        return self._closed


class _Context:
    def __init__(self, pages):
        self.pages = pages


class _Browser:
    def __init__(self, result, current_url, extra_pages=0):
        self._page = _Page(result)
        pages = [self._page] + [_Page(None) for _ in range(extra_pages)]
        self._context = _Context(pages)
        self.current_url = current_url

    async def _ensure_active_page(self, reason=""):
        return self._page


def _run(coro):
    return asyncio.run(coro)


def test_goal_intent_is_site_agnostic() -> None:
    decision = {"action": "click", "thought": "open first result"}
    assert goal_requests_first_result_new_tab(
        "在 Google 搜索Python教程。点击第一个结果，确保在新标签页打开。", decision
    )
    assert goal_requests_first_result_new_tab(
        "在百度搜索Python教程。打开首个结果到新窗口。", decision
    )


def test_rewrites_first_result_click_to_click_new_tab() -> None:
    browser = _Browser(
        {"target_id": 23, "text": "Python Tutorial", "href": "https://docs.python.org/3/tutorial/"},
        "https://www.bing.com/search?q=Python%E6%95%99%E7%A8%8B",
    )
    decisions = [{"action": "click", "target_id": 2, "type_value": "", "thought": "click first result"}]

    changed = _run(
        apply_search_result_open_guard(
            browser,
            decisions,
            goal="在必应搜索Python教程。点击第一个结果，确保在新标签页打开。",
        )
    )

    assert changed is True
    assert decisions[0]["action"] == "click_new_tab"
    assert decisions[0]["target_id"] == 23
    assert decisions[0]["type_value"] == ""


def test_does_not_rewrite_when_query_stage_is_different() -> None:
    browser = _Browser(
        {"target_id": 23, "text": "Python Tutorial", "href": "https://docs.python.org/3/tutorial/"},
        "https://www.bing.com/search?q=VLM+Agent",
    )
    decisions = [{"action": "click", "target_id": 2, "type_value": "", "thought": "click first result"}]

    changed = _run(
        apply_search_result_open_guard(
            browser,
            decisions,
            goal="在必应搜索Python教程。点击第一个结果，确保在新标签页打开。然后重新输入VLM Agent。",
        )
    )

    assert changed is False
    assert decisions[0]["action"] == "click"


def test_does_not_hijack_reentry_after_result_tab_already_open() -> None:
    browser = _Browser(
        {"target_id": 23, "text": "Python Tutorial", "href": "https://docs.python.org/3/tutorial/"},
        "https://www.bing.com/search?q=Python%E6%95%99%E7%A8%8B",
        extra_pages=1,
    )
    decisions = [{"action": "click", "target_id": 2, "type_value": "", "thought": "re-enter VLM Agent in the search box"}]

    changed = _run(
        apply_search_result_open_guard(
            browser,
            decisions,
            goal="在必应搜索Python教程。点击第一个结果，确保在新标签页打开。",
        )
    )

    assert changed is False
    assert decisions[0]["action"] == "click"


def test_rewrites_explicit_first_result_even_with_other_tabs_open() -> None:
    browser = _Browser(
        {"target_id": 23, "text": "Python Tutorial", "href": "https://docs.python.org/3/tutorial/"},
        "https://www.bing.com/search?q=Python%E6%95%99%E7%A8%8B",
        extra_pages=1,
    )
    decisions = [{"action": "click", "target_id": 2, "type_value": "", "thought": "click the first result"}]

    changed = _run(
        apply_search_result_open_guard(
            browser,
            decisions,
            goal="在必应搜索Python教程。点击第一个结果，确保在新标签页打开。",
        )
    )

    assert changed is True
    assert decisions[0]["action"] == "click_new_tab"
    assert decisions[0]["target_id"] == 23


def test_does_not_rewrite_without_som_target_id() -> None:
    browser = _Browser(
        {"target_id": 0, "text": "Python Tutorial", "href": "https://docs.python.org/3/tutorial/"},
        "https://www.bing.com/search?q=Python%E6%95%99%E7%A8%8B",
    )
    decisions = [{"action": "click", "target_id": 2, "type_value": "", "thought": "click first result"}]

    changed = _run(
        apply_search_result_open_guard(
            browser,
            decisions,
            goal="在必应搜索Python教程。点击第一个结果，确保在新标签页打开。",
        )
    )

    assert changed is False
    assert decisions[0]["action"] == "click"


def test_probe_script_has_generic_filters() -> None:
    js = _probe_script("Python教程")
    assert "sponsored" in js
    assert "广告" in js
    assert "r.left >" in js
    assert "queryTokens" in js
    assert ".b_algo" in js
    assert "[data-sokoban-container]" in js

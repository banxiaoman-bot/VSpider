"""Unit tests for FetchLinkContentHandler — JS-driven cross-tab fetching.

Contract: open a link in a background tab, extract text via page.evaluate,
close the tab, write {url, title, content} to workflow_memory[memory_key],
and inject a tab-switch notice telling the VLM what happened. The original
page must remain the active page throughout.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from visual_web_agent.actions import FetchLinkContentHandler

# J: stub set_tab_notice/clear_tab_notice helpers
from _notice_stub import install_notice_stub


class _FakePage:
    def __init__(self, url: str = "https://origin.example/", closed: bool = False) -> None:
        self.url = url
        self._closed = closed
        self.title_value = "Origin"
        # evaluate is configured per test
        self.evaluate = AsyncMock()
        # goto is configured per test
        self.goto = AsyncMock()
        self.close = AsyncMock()
        self.bring_to_front = AsyncMock()
        self.context = None  # set by harness

    def is_closed(self) -> bool:
        return self._closed


class _FakeContext:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages
        self.new_page_calls = 0
        # Pre-baked sequence of pages new_page() returns
        self._queue: list[_FakePage] = []

    async def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        if self._queue:
            p = self._queue.pop(0)
        else:
            p = _FakePage(url="about:blank")
        p.context = self
        self.pages.append(p)
        return p


class _FakeTargetHandle:
    def __init__(self, href: str = "https://target.example/article") -> None:
        self._href = href
        self.evaluate = AsyncMock(return_value=href)


class _FakeTarget:
    def __init__(self, href: str = "https://target.example/article") -> None:
        self.handle = _FakeTargetHandle(href)


def _build_ctx(
    *,
    target_id: int = 0,
    type_value: str = "",
    memory_key: str = "result_1",
    resolved_href: str = "https://target.example/article",
    new_page: _FakePage | None = None,
):
    """Assemble a fully-mocked ActionContext + browser double.

    The fetched-tab evaluate() result defaults to a 200-char article payload.
    Tests override new_page.evaluate / new_page.goto as needed for failure modes.
    """
    origin = _FakePage(url="https://origin.example/")
    ctx_pages = [origin]
    fake_context = _FakeContext(ctx_pages)
    if new_page is None:
        new_page = _FakePage(url="https://target.example/article")
        new_page.evaluate = AsyncMock(
            return_value={
                "title": "Target Article",
                "content": "Article body text " * 10,
                "url": "https://target.example/article-canonical",
                "full_len": 170,
            }
        )
    fake_context._queue.append(new_page)
    origin.context = fake_context

    browser = SimpleNamespace()
    browser._page = origin
    browser._context = fake_context
    browser._LOCATOR_TIMEOUT = 5000
    browser._last_action_error = None
    browser._tab_switch_notice = None
    install_notice_stub(browser)
    browser.rpa_trail = []

    browser._clear_som_overlays = AsyncMock()
    if target_id and target_id != 0:
        browser._resolve_action_target = AsyncMock(return_value=_FakeTarget(resolved_href))
    else:
        browser._resolve_action_target = AsyncMock(return_value=None)

    async def _activate_page(target_page, reason: str = ""):
        browser._page = target_page
    browser._activate_page = _activate_page

    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = origin
    ctx.workflow_memory = {}
    ctx.action = SimpleNamespace(
        target_id=target_id,
        type_value=type_value,
        memory_key=memory_key,
        action="fetch_link_content",
    )
    ctx.with_rpa_meta = lambda d: d
    return ctx, browser, new_page


def _run(coro):
    return asyncio.run(coro)


# ── Success: target_id-driven (href read via JS) ──────────────────────────────
def test_success_via_target_id_writes_memory_and_notice() -> None:
    ctx, browser, new_page = _build_ctx(
        target_id=16, resolved_href="https://target.example/article"
    )
    result = _run(FetchLinkContentHandler().execute(ctx))

    # Returns origin → dispatcher skips Tab Guard
    assert result is ctx.page

    # New tab was opened + closed
    assert browser._context.new_page_calls == 1
    new_page.goto.assert_awaited_once()
    new_page.close.assert_awaited_once()

    # Memory written under the requested key + latest_memory mirror
    assert "result_1" in ctx.workflow_memory
    stored = ctx.workflow_memory["result_1"]
    assert stored["url"] == "https://target.example/article-canonical"
    assert stored["title"] == "Target Article"
    assert "Article body text" in stored["content"]
    assert ctx.workflow_memory["latest_memory"]
    assert ctx.workflow_memory["latest_memory"] in stored["content"]

    # VLM-facing notice
    assert browser._tab_switch_notice is not None
    notice = browser._tab_switch_notice
    assert "fetch_link_content 完成" in notice
    assert "result_1" in notice
    assert "焦点仍在原页" in notice


# ── Success: literal URL via type_value ───────────────────────────────────────
def test_success_via_type_value_url() -> None:
    ctx, browser, new_page = _build_ctx(
        target_id=0,
        type_value="https://docs.python.org/3/tutorial/",
        memory_key="py_tut",
    )
    _run(FetchLinkContentHandler().execute(ctx))

    new_page.goto.assert_awaited_once()
    args, kwargs = new_page.goto.call_args
    assert args[0] == "https://docs.python.org/3/tutorial/"
    assert kwargs.get("wait_until") == "domcontentloaded"
    assert "py_tut" in ctx.workflow_memory


# ── Bad URL schemes: refuse without opening a tab ─────────────────────────────
@pytest.mark.parametrize(
    "bad_url",
    [
        "javascript:alert(1)",
        "mailto:x@example.com",
        "about:blank",
        "data:text/html,<h1>x</h1>",
        "",
    ],
)
def test_refuses_non_http_url(bad_url: str) -> None:
    from visual_web_agent.browser_env import ActionExecutionError
    ctx, browser, new_page = _build_ctx(target_id=0, type_value=bad_url)
    with pytest.raises(ActionExecutionError) as exc_info:
        _run(FetchLinkContentHandler().execute(ctx))
    # Either scheme-rejection or "missing URL/target" — both must surface clearly
    msg = str(exc_info.value)
    assert "fetch_link_content" in msg
    # No tab was opened
    assert browser._context.new_page_calls == 0


# ── Missing href on target_id element ─────────────────────────────────────────
def test_target_id_without_href_raises() -> None:
    from visual_web_agent.browser_env import ActionExecutionError
    # Element resolves but its href evaluates to ""
    ctx, browser, _ = _build_ctx(target_id=16, resolved_href="")
    with pytest.raises(ActionExecutionError):
        _run(FetchLinkContentHandler().execute(ctx))


# ── target_id not resolvable ──────────────────────────────────────────────────
def test_target_id_unresolved_raises() -> None:
    from visual_web_agent.browser_env import ActionExecutionError
    ctx, browser, _ = _build_ctx(target_id=99)
    browser._resolve_action_target = AsyncMock(return_value=None)
    with pytest.raises(ActionExecutionError) as exc_info:
        _run(FetchLinkContentHandler().execute(ctx))
    assert "#99" in str(exc_info.value)


# ── Neither target_id nor type_value ──────────────────────────────────────────
def test_no_input_raises() -> None:
    from visual_web_agent.browser_env import ActionExecutionError
    ctx, browser, _ = _build_ctx(target_id=0, type_value="")
    with pytest.raises(ActionExecutionError):
        _run(FetchLinkContentHandler().execute(ctx))


# ── goto failure (network / CSP / nav abort) ──────────────────────────────────
def test_goto_failure_closes_tab_and_raises() -> None:
    from visual_web_agent.browser_env import ActionExecutionError
    new_page = _FakePage(url="https://broken.example/")
    new_page.goto = AsyncMock(side_effect=RuntimeError("net::ERR_TIMED_OUT"))
    ctx, browser, _ = _build_ctx(
        target_id=0, type_value="https://broken.example/", new_page=new_page
    )
    with pytest.raises(ActionExecutionError) as exc_info:
        _run(FetchLinkContentHandler().execute(ctx))
    assert "goto" in str(exc_info.value)
    # We still cleaned up the new tab on the error path
    new_page.close.assert_awaited_once()


# ── Truncation note appears when content exceeds cap ──────────────────────────
def test_truncation_note_included_in_notice() -> None:
    new_page = _FakePage(url="https://long.example/")
    new_page.evaluate = AsyncMock(
        return_value={
            "title": "Long Doc",
            "content": "x" * 6000,
            "url": "https://long.example/",
            "full_len": 12345,  # raw was much longer than the cap
        }
    )
    ctx, browser, _ = _build_ctx(
        target_id=0, type_value="https://long.example/", new_page=new_page
    )
    _run(FetchLinkContentHandler().execute(ctx))
    assert "truncated from 12345" in browser._tab_switch_notice


# ── memory_key auto-generation when VLM forgets ───────────────────────────────
def test_missing_memory_key_auto_generates() -> None:
    ctx, browser, _ = _build_ctx(
        target_id=0, type_value="https://x.example/", memory_key=""
    )
    _run(FetchLinkContentHandler().execute(ctx))
    keys = [k for k in ctx.workflow_memory.keys() if k.startswith("fetched_")]
    assert len(keys) == 1


# ── RPA trail records the action for replay ───────────────────────────────────
def test_rpa_trail_records_url() -> None:
    ctx, browser, _ = _build_ctx(
        target_id=0,
        type_value="https://docs.python.org/3/",
        memory_key="py_doc",
    )
    _run(FetchLinkContentHandler().execute(ctx))
    assert len(browser.rpa_trail) == 1
    entry = browser.rpa_trail[0]
    assert entry["action"] == "fetch_link_content"
    assert entry["type_value"] == "https://docs.python.org/3/"
    assert entry["memory_key"] == "py_doc"


# ── Origin focus restored if popup listener swapped it during goto ────────────
def test_origin_focus_restored_when_new_tab_steals() -> None:
    new_page = _FakePage(url="https://target.example/")
    new_page.evaluate = AsyncMock(
        return_value={"title": "T", "content": "C", "url": "https://target.example/", "full_len": 1}
    )
    ctx, browser, _ = _build_ctx(
        target_id=0, type_value="https://target.example/", new_page=new_page
    )

    # Simulate popup listener swapping browser._page during new_page.goto
    origin = ctx.page
    original_goto = new_page.goto
    async def _goto_swap(*args, **kwargs):
        browser._page = new_page
        await original_goto(*args, **kwargs)
    new_page.goto = _goto_swap

    _run(FetchLinkContentHandler().execute(ctx))
    # After the handler, browser._page is back on origin
    assert browser._page is origin


# ── Action is registered in the registry under its canonical name ─────────────
def test_action_is_registered() -> None:
    from visual_web_agent.actions import ActionRegistry
    handler = ActionRegistry.get("fetch_link_content")
    assert handler is not None
    assert isinstance(handler, FetchLinkContentHandler)


def test_batch_action_is_registered() -> None:
    from visual_web_agent.actions import ActionRegistry
    handler = ActionRegistry.get("fetch_links_batch")
    assert handler is not None
    assert isinstance(handler, FetchLinkContentHandler)


def test_single_fetch_accepts_json_selectors() -> None:
    new_page = _FakePage(url="https://target.example/article")
    new_page.evaluate = AsyncMock(
        return_value={
            "title": "Scoped",
            "content": "Scoped article text",
            "url": "https://target.example/article",
            "full_len": 19,
            "mode": "dom",
            "selectors": ["article", "main"],
            "selector_count": 1,
        }
    )
    ctx, browser, _ = _build_ctx(
        target_id=0,
        type_value=json.dumps({
            "url": "https://target.example/article",
            "selectors": ["article", "main"],
        }),
        memory_key="scoped",
        new_page=new_page,
    )
    _run(FetchLinkContentHandler().execute(ctx))

    script, args = new_page.evaluate.call_args.args
    assert "selectorList" in script
    assert args[1] == ["article", "main"]
    stored = ctx.workflow_memory["scoped"]
    assert stored["selectors"] == ["article", "main"]
    assert stored["selector_count"] == 1
    assert browser.rpa_trail[0]["action"] == "fetch_link_content"
    assert "selectors" in browser.rpa_trail[0]["type_value"]


def test_ax_mode_uses_cdp_tree_and_writes_structured_memory() -> None:
    ctx, browser, new_page = _build_ctx(
        target_id=0,
        type_value=json.dumps({"url": "https://forms.example/", "mode": "ax"}),
        memory_key="ax_page",
    )
    browser._get_ax_tree_via_cdp = AsyncMock(
        return_value={
            "role": "RootWebArea",
            "name": "Form page",
            "children": [
                {"role": "heading", "name": "Contact"},
                {"role": "textbox", "name": "Email", "value": "a@example.com"},
                {"role": "button", "name": "Submit", "disabled": False},
            ],
        }
    )
    new_page.evaluate = AsyncMock(
        return_value={"title": "Contact Form", "url": "https://forms.example/"}
    )

    _run(FetchLinkContentHandler().execute(ctx))

    browser._get_ax_tree_via_cdp.assert_awaited_once_with(
        new_page,
        interesting_only=True,
    )
    stored = ctx.workflow_memory["ax_page"]
    assert stored["mode"] == "ax"
    assert "[textbox] Email = a@example.com" in stored["content"]
    assert any(node["role"] == "button" for node in stored["structured"])


def test_ax_mode_with_selectors_prepends_selector_scope() -> None:
    ctx, browser, new_page = _build_ctx(
        target_id=0,
        type_value=json.dumps({
            "url": "https://forms.example/",
            "mode": "ax",
            "selectors": ["main"],
        }),
        memory_key="ax_scoped",
    )
    browser._get_ax_tree_via_cdp = AsyncMock(
        return_value={
            "role": "RootWebArea",
            "name": "Form page",
            "children": [
                {"role": "heading", "name": "Contact"},
                {"role": "textbox", "name": "Email"},
            ],
        }
    )
    new_page.evaluate = AsyncMock(
        side_effect=[
            {
                "title": "Contact",
                "content": "Only main form fields",
                "url": "https://forms.example/",
                "full_len": 21,
                "mode": "dom",
                "selectors": ["main"],
                "selector_count": 1,
            },
            {"title": "Contact Form", "url": "https://forms.example/"},
        ]
    )

    _run(FetchLinkContentHandler().execute(ctx))

    stored = ctx.workflow_memory["ax_scoped"]
    assert stored["mode"] == "ax"
    assert stored["selector_count"] == 1
    assert stored["selector_content"] == "Only main form fields"
    assert stored["content"].startswith("[selector_scope]\nOnly main form fields")
    assert "[ax_tree]" in stored["content"]


def test_batch_fetch_target_ids_writes_list_and_individual_keys() -> None:
    first = _FakePage(url="https://one.example/")
    first.evaluate = AsyncMock(
        return_value={
            "title": "One",
            "content": "First article",
            "url": "https://one.example/final",
            "full_len": 13,
        }
    )
    second = _FakePage(url="https://two.example/")
    second.evaluate = AsyncMock(
        return_value={
            "title": "Two",
            "content": "Second article",
            "url": "https://two.example/final",
            "full_len": 14,
        }
    )
    ctx, browser, _ = _build_ctx(
        target_id=0,
        type_value=json.dumps({"target_ids": [16, 32], "concurrency": 2}),
        memory_key="results",
        new_page=first,
    )
    browser._context._queue.append(second)
    browser._resolve_action_target = AsyncMock(
        side_effect=[
            _FakeTarget("https://one.example/"),
            _FakeTarget("https://two.example/"),
        ]
    )
    ctx.action.action = "fetch_links_batch"

    result = _run(FetchLinkContentHandler().execute(ctx))

    assert result is ctx.page
    assert browser._context.new_page_calls == 2
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()
    batch = ctx.workflow_memory["results"]
    assert [item["title"] for item in batch] == ["One", "Two"]
    assert ctx.workflow_memory["results_1"]["content"] == "First article"
    assert ctx.workflow_memory["results_2"]["content"] == "Second article"
    assert ctx.workflow_memory["latest_memory"]
    assert browser.rpa_trail[0]["action"] == "fetch_links_batch"
    assert "fetch_links_batch 完成" in browser._tab_switch_notice


def test_batch_fetch_collects_per_url_errors_without_failing_whole_batch() -> None:
    good = _FakePage(url="https://good.example/")
    good.evaluate = AsyncMock(
        return_value={
            "title": "Good",
            "content": "Good article",
            "url": "https://good.example/final",
            "full_len": 12,
        }
    )
    bad = _FakePage(url="https://bad.example/")
    bad.goto = AsyncMock(side_effect=RuntimeError("net::ERR_FAILED"))
    ctx, browser, _ = _build_ctx(
        target_id=0,
        type_value=json.dumps({
            "urls": ["https://good.example/", "https://bad.example/"],
            "concurrency": 2,
        }),
        memory_key="batch",
        new_page=good,
    )
    browser._context._queue.append(bad)
    ctx.action.action = "fetch_links_batch"

    _run(FetchLinkContentHandler().execute(ctx))

    batch = ctx.workflow_memory["batch"]
    assert [item["ok"] for item in batch] == [True, False]
    assert "net::ERR_FAILED" in batch[1]["error"]
    bad.close.assert_awaited_once()

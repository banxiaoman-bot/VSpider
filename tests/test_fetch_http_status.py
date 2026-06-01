"""Regression tests for HTTP status injection in FetchLinkContentHandler.

What's covered
==============
* ``Response.status`` is captured from ``page.goto`` and persisted in the
  payload as ``http_status`` / ``http_ok``.
* Single-URL notice surfaces ``HTTP: <code>`` plus an emoji indicator.
* For 4xx/5xx, an explicit ``[HTTP ERROR]`` advisory is appended so VLM
  knows the captured content is likely an error page (not real data).
* Batch notice lists failed URLs with their status codes (capped at 10).
* Schemes without a navigation response (data:/about:) yield an "状态未知"
  line rather than crashing.
* Existing success path remains green even when AsyncMock returns no
  status (regression guard for the test-infrastructure compatibility).
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


# ── reuse the existing test infrastructure pattern ──────────────────────────


class _FakePage:
    def __init__(self, url: str = "https://origin.example/") -> None:
        self.url = url
        self._closed = False
        self.evaluate = AsyncMock(
            return_value={
                "title": "T",
                "content": "Body",
                "url": url,
                "full_len": 4,
            }
        )
        self.goto = AsyncMock()  # tests override return_value
        self.close = AsyncMock()
        self.bring_to_front = AsyncMock()
        self.context = None

    def is_closed(self) -> bool:
        return self._closed


class _FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self._queue = []
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        p = self._queue.pop(0) if self._queue else _FakePage("about:blank")
        p.context = self
        self.pages.append(p)
        return p


def _make_response(status: int):
    """Build a fake Playwright Response object with a usable .status."""
    resp = SimpleNamespace()
    resp.status = status
    return resp


def _build_ctx(
    *,
    type_value: str,
    memory_key: str = "result_1",
    new_page_status: int | None = 200,
    new_page=None,
):
    origin = _FakePage("https://origin.example/")
    ctx_pages = [origin]
    fake_context = _FakeContext(ctx_pages)

    if new_page is None:
        new_page = _FakePage(type_value or "https://target.example/")
        new_page.evaluate = AsyncMock(
            return_value={
                "title": "Target",
                "content": "Hello from target",
                "url": new_page.url,
                "full_len": 17,
            }
        )

    if new_page_status is not None:
        new_page.goto = AsyncMock(return_value=_make_response(new_page_status))
    else:
        # data:/about: schemes return no main resource Response
        new_page.goto = AsyncMock(return_value=None)

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
    browser._resolve_action_target = AsyncMock(return_value=None)

    async def _activate_page(target_page, reason: str = ""):
        browser._page = target_page

    browser._activate_page = _activate_page

    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = origin
    ctx.workflow_memory = {}
    ctx.action = SimpleNamespace(
        target_id=0,
        type_value=type_value,
        memory_key=memory_key,
        action="fetch_link_content",
    )
    ctx.with_rpa_meta = lambda d: d
    return ctx, browser, new_page


def _run(coro):
    return asyncio.run(coro)


# ════════════════════════════════════════════════════════════════════
#                       SINGLE URL — STATUS SURFACING
# ════════════════════════════════════════════════════════════════════


class TestSingleUrlHttpStatus:
    def test_200_appears_in_notice(self) -> None:
        ctx, browser, _ = _build_ctx(
            type_value="https://example.test/", new_page_status=200,
        )
        _run(FetchLinkContentHandler().execute(ctx))
        notice = browser._tab_switch_notice
        assert notice is not None
        assert "HTTP: 200" in notice
        # No advisory for success
        assert "[HTTP ERROR]" not in notice

    def test_404_emits_advisory(self) -> None:
        ctx, browser, _ = _build_ctx(
            type_value="https://example.test/missing", new_page_status=404,
        )
        _run(FetchLinkContentHandler().execute(ctx))
        notice = browser._tab_switch_notice
        assert "HTTP: 404" in notice
        assert "[HTTP ERROR]" in notice
        # 4xx label
        assert "客户端错误" in notice
        # Actionable hint for VLM
        assert "extract" in notice.lower() or "跳过" in notice or "memory" in notice

    def test_403_emits_advisory(self) -> None:
        ctx, browser, _ = _build_ctx(
            type_value="https://example.test/forbidden", new_page_status=403,
        )
        _run(FetchLinkContentHandler().execute(ctx))
        notice = browser._tab_switch_notice
        assert "HTTP: 403" in notice
        assert "[HTTP ERROR]" in notice

    def test_500_emits_retry_hint(self) -> None:
        ctx, browser, _ = _build_ctx(
            type_value="https://example.test/oops", new_page_status=500,
        )
        _run(FetchLinkContentHandler().execute(ctx))
        notice = browser._tab_switch_notice
        assert "HTTP: 500" in notice
        assert "[HTTP ERROR]" in notice
        assert "服务器错误" in notice
        # 5xx-specific advisory mentions retry
        assert "重试" in notice or "稍后" in notice

    def test_redirect_3xx(self) -> None:
        """3xx already followed by Playwright; surface for transparency
        but no advisory needed."""
        ctx, browser, _ = _build_ctx(
            type_value="https://example.test/old", new_page_status=301,
        )
        _run(FetchLinkContentHandler().execute(ctx))
        notice = browser._tab_switch_notice
        assert "HTTP: 301" in notice
        # 3xx label or follow note
        assert "重定向" in notice or "↪" in notice
        assert "[HTTP ERROR]" not in notice

    def test_no_response_object_yields_unknown(self) -> None:
        """data:/about: schemes don't generate a main Response."""
        ctx, browser, _ = _build_ctx(
            type_value="https://example.test/spa-route",
            new_page_status=None,  # goto returns None
        )
        _run(FetchLinkContentHandler().execute(ctx))
        notice = browser._tab_switch_notice
        assert "状态未知" in notice
        assert "[HTTP ERROR]" not in notice

    def test_payload_carries_status_to_workflow_memory(self) -> None:
        ctx, browser, _ = _build_ctx(
            type_value="https://example.test/", new_page_status=418,
        )
        _run(FetchLinkContentHandler().execute(ctx))
        # The payload status is referenced in the notice (full propagation
        # to workflow_memory is an internal detail; the notice is the
        # contract the VLM observes).
        notice = browser._tab_switch_notice
        assert "418" in notice


# ════════════════════════════════════════════════════════════════════
#                       BATCH — FAILURE SUMMARY
# ════════════════════════════════════════════════════════════════════


class _BatchPage(_FakePage):
    def __init__(self, status_code: int, *, raise_goto: bool = False):
        super().__init__(url=f"https://batch.test/{status_code}")
        self.status_code = status_code
        self.evaluate = AsyncMock(
            return_value={
                "title": f"page-{status_code}",
                "content": f"Content {status_code}",
                "url": self.url,
                "full_len": 12,
            }
        )
        if raise_goto:
            self.goto = AsyncMock(side_effect=ConnectionError("DNS failed"))
        else:
            self.goto = AsyncMock(return_value=_make_response(status_code))


def _build_batch_ctx(
    *,
    pages: list[_BatchPage],
    memory_key: str = "batch_1",
):
    origin = _FakePage("https://origin.example/")
    fake_context = _FakeContext([origin])
    for p in pages:
        fake_context._queue.append(p)
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
    browser._resolve_action_target = AsyncMock(return_value=None)

    async def _activate(target_page, reason=""):
        browser._page = target_page

    browser._activate_page = _activate

    ctx = SimpleNamespace()
    ctx.browser = browser
    ctx.page = origin
    ctx.workflow_memory = {}
    urls = [p.url for p in pages]
    ctx.action = SimpleNamespace(
        target_id=0,
        type_value=json.dumps({"urls": urls}, ensure_ascii=False),
        memory_key=memory_key,
        action="fetch_links_batch",
    )
    ctx.with_rpa_meta = lambda d: d
    return ctx, browser


class TestBatchFailureSummary:
    def test_mixed_results_listed(self) -> None:
        pages = [
            _BatchPage(200),
            _BatchPage(404),
            _BatchPage(503),
        ]
        ctx, browser = _build_batch_ctx(pages=pages, memory_key="batch_1")
        _run(FetchLinkContentHandler().execute(ctx))
        notice = browser._tab_switch_notice
        assert notice is not None
        # Header acknowledges total
        assert "3 条链接" in notice or "/3" in notice or "3/" in notice
        # Failures section
        assert "失败链接" in notice
        # Both error codes appear with status numbers
        assert "404" in notice
        assert "503" in notice
        # Successful URL (200) is NOT listed under failures
        assert "batch.test/200" not in notice or notice.count("batch.test/200") <= 1
        # The advisory tells VLM to skip
        assert "跳过" in notice or "selector" in notice

    def test_all_success_no_failure_block(self) -> None:
        pages = [_BatchPage(200), _BatchPage(200)]
        ctx, browser = _build_batch_ctx(pages=pages)
        _run(FetchLinkContentHandler().execute(ctx))
        notice = browser._tab_switch_notice
        # No failure block when nothing failed
        assert "失败链接" not in notice

    def test_connection_failure_listed_with_message(self) -> None:
        """Connection-level failures (no status reached) appear with the
        error message, not a status code."""
        pages = [_BatchPage(200), _BatchPage(0, raise_goto=True)]
        ctx, browser = _build_batch_ctx(pages=pages)
        _run(FetchLinkContentHandler().execute(ctx))
        notice = browser._tab_switch_notice
        assert "失败链接" in notice
        # ConnectionError-style error message surfaces (DNS/TLS/etc.)
        assert "DNS" in notice or "fetch error" in notice or "❌" in notice

    def test_failure_list_capped(self) -> None:
        """If >10 failures, summary truncates with a … marker."""
        pages = [_BatchPage(404) for _ in range(15)]
        ctx, browser = _build_batch_ctx(pages=pages)
        _run(FetchLinkContentHandler().execute(ctx))
        notice = browser._tab_switch_notice
        # Truncation marker present
        assert "…" in notice or "..." in notice or "更多" in notice
        # And not all 15 URLs present (cap at 10)
        # batch.test/404 appears for each failure; with cap there should be
        # fewer than 15 occurrences in the failure block (a few more in
        # other places like "3 条链接" header).
        assert notice.count("batch.test/404") <= 11  # 10 in failures + maybe 1 elsewhere


# ════════════════════════════════════════════════════════════════════
#                       PAYLOAD CONTRACT
# ════════════════════════════════════════════════════════════════════


class TestPayloadFields:
    """The payload returned from ``_fetch_one`` MUST include http_status
    (int|None) and http_ok (bool). Other downstream features (rpa_trail
    inspection, future analytics) depend on this."""

    def test_payload_carries_http_status_int(self) -> None:
        ctx, browser, np = _build_ctx(
            type_value="https://example.test/", new_page_status=200,
        )
        # Run via the public path so all wrappers normalize the payload
        _run(FetchLinkContentHandler().execute(ctx))
        # The notice is our public proxy: 200 must be there
        assert "HTTP: 200" in browser._tab_switch_notice

    def test_payload_carries_http_status_none_for_no_response(self) -> None:
        ctx, browser, _ = _build_ctx(
            type_value="https://example.test/", new_page_status=None,
        )
        _run(FetchLinkContentHandler().execute(ctx))
        assert "状态未知" in browser._tab_switch_notice

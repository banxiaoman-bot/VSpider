"""Stub-frame regression for GotoHandler's cross-system pre-navigation
interception (A1, Path-2).

When cross-system switching is enabled, main.py threads the run's
``SessionRouter`` into each ``ActionContext`` (``ctx.session_router``). The
``GotoHandler`` then, *before* running ``page.goto``, asks the router whether
the goto target belongs to a different planned system than the current page.
If so it must NOT navigate the current page (so that system's page state is
preserved); instead it records a ``_pending_cross_system_goto`` directive on
the browser for the reactive loop to act on (acquire -> launch -> rebind ->
navigate the target system's isolated session).

When no router is threaded (flag off), or the goto is same-system / to an
unknown domain, the handler must fall through to a normal ``page.goto`` --
byte-identical to the pre-A1 behaviour.

These tests drive the handler with ``SimpleNamespace`` stubs (mirroring
``test_extract_row_and_tree_check.py``) so no Playwright / browser is needed;
the ``SessionRouter`` itself is real (its interception logic is pure).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from visual_web_agent.actions import GotoHandler
from visual_web_agent.session_router import SessionRouter, SystemAuthPlan


_SYSTEMS = [
    {"id": "system_1", "name": "Alpha", "domain": "alpha.com"},
    {"id": "system_2", "name": "Beta", "domain": "beta.com"},
]


def _router() -> SessionRouter:
    return SessionRouter(run_id="r1", plan=SystemAuthPlan(systems=_SYSTEMS), pool=None)


class _StubPage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.goto_calls: list[str] = []

    def is_closed(self) -> bool:
        return False

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append(url)


def _make_ctx(target_url: str, current_url: str, *, session_router: Any) -> SimpleNamespace:
    async def _wait_stable(*_: Any, **__: Any) -> None:
        return None

    page = _StubPage(current_url)
    browser = SimpleNamespace(
        current_url=current_url,
        _page=page,
        _context=SimpleNamespace(pages=[page]),
        _tab_switch_notice=None,
        _pending_cross_system_goto=None,
        _last_action_error=None,
        rpa_trail=[],
        _wait_for_page_stable=_wait_stable,
        _session_router=session_router,
    )
    return SimpleNamespace(
        action=SimpleNamespace(action="goto", type_value=target_url, target_id=0),
        browser=browser,
        page=page,
        workflow_memory={},
        session_router=session_router,
        with_rpa_meta=lambda d: d,
    )


class TestGotoCrossSystemInterception:
    def test_cross_system_goto_is_intercepted_not_navigated(self) -> None:
        ctx = _make_ctx(
            "https://beta.com/list", "https://alpha.com/home", session_router=_router()
        )
        result = asyncio.run(GotoHandler().execute(ctx))
        assert result is None
        # The current (system_1) page is preserved -- goto never runs on it.
        assert ctx.page.goto_calls == []
        pending = ctx.browser._pending_cross_system_goto
        assert pending is not None
        assert pending["should_intercept"] is True
        assert pending["to_system_id"] == "system_2"
        assert pending["from_system_id"] == "system_1"
        assert pending["target_url"] == "https://beta.com/list"
        # A notice is surfaced so the VLM understands the page didn't change here.
        assert ctx.browser._tab_switch_notice

    def test_same_system_goto_navigates_normally(self) -> None:
        ctx = _make_ctx(
            "https://alpha.com/other", "https://alpha.com/home", session_router=_router()
        )
        asyncio.run(GotoHandler().execute(ctx))
        assert ctx.page.goto_calls == ["https://alpha.com/other"]
        assert ctx.browser._pending_cross_system_goto is None

    def test_no_router_goto_navigates_normally(self) -> None:
        # flag off -> main.py never threads a router -> byte-identical goto.
        ctx = _make_ctx(
            "https://beta.com/list", "https://alpha.com/home", session_router=None
        )
        asyncio.run(GotoHandler().execute(ctx))
        assert ctx.page.goto_calls == ["https://beta.com/list"]
        assert ctx.browser._pending_cross_system_goto is None

    def test_unknown_domain_goto_navigates_normally(self) -> None:
        ctx = _make_ctx(
            "https://example.org/x", "https://alpha.com/home", session_router=_router()
        )
        asyncio.run(GotoHandler().execute(ctx))
        assert ctx.page.goto_calls == ["https://example.org/x"]
        assert ctx.browser._pending_cross_system_goto is None

    def test_first_navigation_blank_current_navigates_normally(self) -> None:
        # current_url resolves to no known system -> establishing home, no intercept.
        ctx = _make_ctx(
            "https://beta.com/list", "about:blank", session_router=_router()
        )
        asyncio.run(GotoHandler().execute(ctx))
        assert ctx.page.goto_calls == ["https://beta.com/list"]
        assert ctx.browser._pending_cross_system_goto is None

"""Regression tests for the DC-2 auto consent guard (perception-entry).

DC-2 wires the DC-1 ``dismiss_consent`` capability into the perception phase as
an automatic, idempotent, once-per-URL guard so cookie/consent walls get cleared
before the agent "looks" at a page — no VLM round trip required.

Covers:
  - DismissConsentHandler.scan_and_dismiss(page) pure-core refactor returns the
    result dict with no browser side effects (no rpa_trail / _wait_after_action)
  - auto_dismiss_consent dismisses on a consent wall, records one rpa_trail entry
    tagged source="perception_guard", and marks the URL handled
  - dedup: a second call on the same URL is a no-op (the scan does not re-run)
  - clean page: no dismiss -> no rpa_trail pollution, URL still marked handled
  - a scan exception is swallowed (perception must never break)
  - None page -> returns None
  - PerceptionPhase._maybe_dismiss_consent runs the guard once per URL and honors
    the CONSENT_GUARD_ENABLED kill switch
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


# ════════════════════════════════════════════════════════════════════
#                       STUB SCAFFOLD (mirrors DC-1)
# ════════════════════════════════════════════════════════════════════


class _StubLocator:
    def __init__(self, *, click_raises: bool = False):
        self.click_raises = click_raises
        self.clicked = False

    @property
    def first(self):
        return self

    async def scroll_into_view_if_needed(self, **_):
        return None

    async def click(self, **_):
        if self.click_raises:
            raise RuntimeError("blocked")
        self.clicked = True

    async def evaluate(self, *_a, **_k):
        return None


class _StubScope:
    """Stands in for a Playwright Page or Frame.

    First ``evaluate`` per scope returns the mark-consent probe dict; later
    calls return the still-visible boolean (verify + optional retry).
    """

    def __init__(self, name: str, *, probe=None, still_visible=False, locator=None):
        self.url = f"https://example.test/{name}"
        self._probe = probe if probe is not None else {"found": False}
        self._still = still_visible
        self._locator = locator or _StubLocator()
        self.eval_calls = 0

    def locator(self, _sel: str):
        return self._locator

    async def evaluate(self, _script: str, *args, **_k):
        self.eval_calls += 1
        if self.eval_calls == 1:
            return self._probe
        return self._still


class _StubPage:
    def __init__(self, scope: _StubScope, frames=None, url=None):
        self._scope = scope
        self.url = url if url is not None else scope.url
        self.frames = frames if frames is not None else [scope]
        self.main_frame = self.frames[0] if self.frames else None

    def locator(self, sel: str):
        return self._scope.locator(sel)

    async def evaluate(self, script: str, *args, **k):
        return await self._scope.evaluate(script, *args, **k)


def _browser_stub(url: str = ""):
    async def _no_op(*_, **__):
        return None

    return SimpleNamespace(rpa_trail=[], current_url=url, _wait_after_action=_no_op)


def _wall_scope(name: str = "wall", locator=None) -> _StubScope:
    return _StubScope(
        name,
        probe={
            "found": True,
            "strategy": "known_cmp",
            "cmp": "OneTrust",
            "selector": "#onetrust-accept-btn-handler",
        },
        still_visible=False,
        locator=locator or _StubLocator(),
    )


# ════════════════════════════════════════════════════════════════════
#                 scan_and_dismiss CORE REFACTOR (DC-1)
# ════════════════════════════════════════════════════════════════════


class TestScanCore:
    def test_scan_and_dismiss_returns_result_dict(self) -> None:
        from visual_web_agent.actions import DismissConsentHandler

        loc = _StubLocator()
        page = _StubPage(_wall_scope(locator=loc))
        result = asyncio.run(DismissConsentHandler().scan_and_dismiss(page))
        assert loc.clicked is True
        assert result["dismissed"] is True
        assert result["strategy"] == "known_cmp"
        assert result["action"] == "dismiss_consent"

    def test_scan_clean_page_is_noop(self) -> None:
        from visual_web_agent.actions import DismissConsentHandler

        page = _StubPage(_StubScope("clean", probe={"found": False}))
        result = asyncio.run(DismissConsentHandler().scan_and_dismiss(page))
        assert result["dismissed"] is False
        assert result["strategy"] == "noop"

    def test_scan_has_no_browser_side_effects(self) -> None:
        # scan_and_dismiss must be pure: it only takes a page, never a browser.
        from visual_web_agent.actions import DismissConsentHandler

        page = _StubPage(_wall_scope())
        # Should run without any browser object at all.
        result = asyncio.run(DismissConsentHandler().scan_and_dismiss(page))
        assert isinstance(result, dict)


# ════════════════════════════════════════════════════════════════════
#                 auto_dismiss_consent GUARD (DC-2)
# ════════════════════════════════════════════════════════════════════


class TestGuard:
    def test_guard_dismisses_and_records_with_source(self) -> None:
        from visual_web_agent.consent_guard import auto_dismiss_consent

        loc = _StubLocator()
        page = _StubPage(_wall_scope(locator=loc))
        browser = _browser_stub(page.url)
        handled: set[str] = set()
        result = asyncio.run(
            auto_dismiss_consent(browser, page, handled_urls=handled)
        )
        assert result is not None and result["dismissed"] is True
        assert loc.clicked is True
        assert browser.rpa_trail, "a successful dismiss must leave an rpa_trail entry"
        entry = browser.rpa_trail[-1]
        assert entry["action"] == "dismiss_consent"
        assert entry["source"] == "perception_guard"
        assert len(handled) == 1

    def test_guard_dedup_second_call_is_noop(self) -> None:
        from visual_web_agent.consent_guard import auto_dismiss_consent

        scope = _wall_scope()
        page = _StubPage(scope)
        browser = _browser_stub(page.url)
        handled: set[str] = set()
        asyncio.run(auto_dismiss_consent(browser, page, handled_urls=handled))
        calls_after_first = scope.eval_calls
        result2 = asyncio.run(
            auto_dismiss_consent(browser, page, handled_urls=handled)
        )
        assert result2 is None
        assert scope.eval_calls == calls_after_first  # scan did not re-run
        assert len(browser.rpa_trail) == 1

    def test_guard_clean_page_no_trail_but_marks_url(self) -> None:
        from visual_web_agent.consent_guard import auto_dismiss_consent

        page = _StubPage(_StubScope("clean", probe={"found": False}))
        browser = _browser_stub(page.url)
        handled: set[str] = set()
        result = asyncio.run(
            auto_dismiss_consent(browser, page, handled_urls=handled)
        )
        assert result is not None and result["dismissed"] is False
        assert browser.rpa_trail == []  # no pollution on a clean no-op
        assert len(handled) == 1  # URL still marked so we don't rescan every turn

    def test_guard_scan_exception_is_swallowed(self) -> None:
        from visual_web_agent.consent_guard import auto_dismiss_consent

        class _BoomPage:
            url = "https://example.test/boom"
            main_frame = None

            @property
            def frames(self):
                raise RuntimeError("frames boom")

        browser = _browser_stub("https://example.test/boom")
        handled: set[str] = set()
        # Perception must never break: a scan blow-up returns None, not raise.
        result = asyncio.run(
            auto_dismiss_consent(browser, _BoomPage(), handled_urls=handled)
        )
        assert result is None
        assert browser.rpa_trail == []

    def test_guard_none_page_returns_none(self) -> None:
        from visual_web_agent.consent_guard import auto_dismiss_consent

        browser = _browser_stub("")
        result = asyncio.run(
            auto_dismiss_consent(browser, None, handled_urls=set())
        )
        assert result is None

    def test_norm_url_ignores_fragment_and_trailing_slash(self) -> None:
        from visual_web_agent.consent_guard import _norm_url

        a = _norm_url("https://x.test/a/?q=1#frag")
        b = _norm_url("https://x.test/a?q=1")
        assert a == b


# ════════════════════════════════════════════════════════════════════
#                 PerceptionPhase WIRING (DC-2 entry A)
# ════════════════════════════════════════════════════════════════════


class TestPerceptionGuard:
    def test_maybe_dismiss_runs_once_per_url(self) -> None:
        from visual_web_agent.phases.perception import PerceptionPhase

        loc = _StubLocator()
        scope = _wall_scope("p1", locator=loc)
        page = _StubPage(scope)
        browser = _browser_stub(page.url)

        async def _recover(_reason):
            return page

        phase = PerceptionPhase()
        asyncio.run(phase._maybe_dismiss_consent(browser, _recover))
        assert loc.clicked is True
        calls_after_first = scope.eval_calls
        # Second turn on the same URL: cheap dedup, no re-scan.
        asyncio.run(phase._maybe_dismiss_consent(browser, _recover))
        assert scope.eval_calls == calls_after_first

    def test_kill_switch_disables_guard(self, monkeypatch) -> None:
        import visual_web_agent.phases.perception as perc
        from visual_web_agent.phases.perception import PerceptionPhase

        loc = _StubLocator()
        scope = _wall_scope("p2", locator=loc)
        page = _StubPage(scope)
        browser = _browser_stub(page.url)

        async def _recover(_reason):
            return page

        monkeypatch.setattr(perc, "CONSENT_GUARD_ENABLED", False, raising=False)
        phase = PerceptionPhase()
        asyncio.run(phase._maybe_dismiss_consent(browser, _recover))
        assert loc.clicked is False  # guard disabled -> nothing clicked

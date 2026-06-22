"""L1-L6 advanced UI scenarios — real Chromium e2e.

L1  Breadcrumb       — multi-level path, click to navigate
L2  Copy table       — select rows → copy TSV → paste verify
L3  Skeleton loading — placeholder → API delay → real content
L4  Multi-window     — window.open + postMessage
L5  Script injection — inject <script>, execute, read result
L6  Error boundary   — trigger error overlay → recover
"""
from __future__ import annotations

import os
import sys

import pytest

_SKIP_REASON = ""
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    _SKIP_REASON = "playwright not installed"

if not _SKIP_REASON:
    _DEFAULT = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "ms-playwright"
    )
    _FALLBACK = os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "ms-playwright"
    )
    if not (os.path.isdir(_DEFAULT) or os.path.isdir(_FALLBACK)):
        _SKIP_REASON = "chromium not installed"

pytestmark = pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON)

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from scenario_site import (  # noqa: E402
    ScenarioStore,
    ScenarioServer,
    make_alpha_handler,
)


@pytest.fixture(scope="module")
def infra():
    store = ScenarioStore()
    alpha = ScenarioServer(make_alpha_handler(store)).start()
    yield {"store": store, "alpha_url": alpha.base_url}
    alpha.stop()


@pytest.fixture()
def page(infra):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        pg = ctx.new_page()
        yield pg, infra
        browser.close()


@pytest.fixture()
def clipboard_page(infra):
    """Page with clipboard permissions for copy/paste tests."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            permissions=["clipboard-read", "clipboard-write"]
        )
        pg = ctx.new_page()
        yield pg, infra, ctx
        browser.close()


# ===================================================================
# L1: Breadcrumb navigation
# ===================================================================

class TestL1_Breadcrumb:
    def test_home_page_no_parent(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/breadcrumb/home")
        title = pg.text_content("#bc-title")
        assert title == "首页"
        current = pg.query_selector(".bc-current")
        assert current.get_attribute("data-page") == "home"

    def test_deep_path_shows_trail(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/breadcrumb/temp-sensor")
        links = pg.query_selector_all(".bc-link")
        assert len(links) == 2  # home → sensors
        current = pg.query_selector(".bc-current")
        assert "温控器" in current.text_content()

    def test_click_breadcrumb_navigates(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/breadcrumb/temp-sensor")
        pg.click(".bc-link[data-page='sensors']")
        pg.wait_for_selector("#bc-title")
        title = pg.text_content("#bc-title")
        assert title == "传感器"

    def test_children_links_present(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/breadcrumb/home")
        children = pg.query_selector_all(".bc-child")
        assert len(children) >= 2

    def test_navigate_to_child(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/breadcrumb/home")
        pg.click(".bc-child[href='/breadcrumb/network']")
        pg.wait_for_selector("#bc-title")
        assert pg.text_content("#bc-title") == "网络设备"


# ===================================================================
# L2: Copy table data
# ===================================================================

class TestL2_CopyTable:
    def test_select_and_copy(self, clipboard_page):
        pg, infra, ctx = clipboard_page
        pg.goto(f"{infra['alpha_url']}/copy-table")
        checks = pg.query_selector_all(".ct-check")
        checks[0].click()
        checks[2].click()
        pg.click("#ct-copy")
        pg.wait_for_function(
            "document.getElementById('ct-copied').textContent === '2'",
            timeout=5000,
        )
        clipboard = pg.evaluate("navigator.clipboard.readText()")
        assert "\t" in clipboard
        lines = clipboard.strip().split("\n")
        assert len(lines) == 2

    def test_paste_matches_copied(self, clipboard_page):
        pg, infra, ctx = clipboard_page
        pg.goto(f"{infra['alpha_url']}/copy-table")
        pg.query_selector_all(".ct-check")[0].click()
        pg.click("#ct-copy")
        pg.wait_for_function(
            "document.getElementById('ct-copied').textContent !== '0'",
            timeout=5000,
        )
        clipboard = pg.evaluate("navigator.clipboard.readText()")
        pg.fill("#ct-paste", clipboard)
        pasted = pg.input_value("#ct-paste")
        assert pasted == clipboard


# ===================================================================
# L3: Skeleton loading
# ===================================================================

class TestL3_Skeleton:
    def test_initial_shows_skeleton(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/skeleton")
        assert pg.is_visible("#sk-skeleton")
        assert pg.is_hidden("#sk-content")
        assert pg.text_content("#sk-status") == "loading"

    def test_content_replaces_skeleton(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/skeleton")
        pg.wait_for_function(
            "document.getElementById('sk-status').textContent === 'loaded'",
            timeout=5000,
        )
        assert pg.is_hidden("#sk-skeleton")
        assert pg.is_visible("#sk-content")
        name = pg.text_content("#sk-data-name")
        assert "温控器" in name

    def test_skeleton_lines_exist(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/skeleton")
        lines = pg.query_selector_all(".sk-line")
        assert len(lines) == 3


# ===================================================================
# L4: Multi-window popup (limited in headless — test page rendering)
# ===================================================================

class TestL4_PopupWindow:
    def test_parent_page_renders(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/popup")
        assert pg.is_visible("#pp-title")
        assert pg.is_visible("#pp-open")

    def test_child_page_renders(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/popup-child")
        assert pg.is_visible("#pc-title")
        assert pg.is_visible("#pc-send")

    def test_popup_opens_and_communicates(self, page):
        pg, infra = page
        ctx = pg.context
        pg.goto(f"{infra['alpha_url']}/popup")
        with ctx.expect_page() as new_page_info:
            pg.click("#pp-open")
        child = new_page_info.value
        child.wait_for_load_state()
        child.fill("#pc-input", "hello from child")
        child.click("#pc-send")
        pg.wait_for_function(
            "document.getElementById('pp-reply').textContent !== 'none'",
            timeout=5000,
        )
        reply = pg.text_content("#pp-reply")
        assert reply == "hello from child"


# ===================================================================
# L5: Dynamic script injection
# ===================================================================

class TestL5_ScriptInjection:
    def test_inject_computes_result(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/script-inject")
        pg.click("#si-inject")
        pg.wait_for_function(
            "document.getElementById('si-result').textContent !== 'none'",
            timeout=5000,
        )
        result = pg.text_content("#si-result")
        assert result == "1764"  # 42 * 42

    def test_injected_global_variable(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/script-inject")
        pg.click("#si-inject")
        pg.wait_for_timeout(300)
        val = pg.evaluate("window._injectedResult")
        assert val == 1764


# ===================================================================
# L6: Error boundary
# ===================================================================

class TestL6_ErrorBoundary:
    def test_initial_state_normal(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/error-boundary")
        assert pg.is_visible("#eb-app")
        assert pg.is_hidden("#eb-error")
        assert pg.text_content("#eb-status") == "normal"

    def test_trigger_shows_error(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/error-boundary")
        pg.click("#eb-trigger")
        pg.wait_for_timeout(200)
        assert pg.is_hidden("#eb-app")
        assert pg.is_visible("#eb-error")
        assert pg.text_content("#eb-status") == "error"
        msg = pg.text_content("#eb-error-msg")
        assert "TypeError" in msg

    def test_recover_restores_normal(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/error-boundary")
        pg.click("#eb-trigger")
        pg.wait_for_timeout(200)
        pg.click("#eb-recover")
        pg.wait_for_timeout(200)
        assert pg.is_visible("#eb-app")
        assert pg.is_hidden("#eb-error")
        assert pg.text_content("#eb-status") == "recovered"

    def test_trigger_recover_trigger_again(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/error-boundary")
        pg.click("#eb-trigger")
        pg.wait_for_timeout(200)
        pg.click("#eb-recover")
        pg.wait_for_timeout(200)
        pg.click("#eb-trigger")
        pg.wait_for_timeout(200)
        assert pg.is_visible("#eb-error")
        assert pg.text_content("#eb-status") == "error"

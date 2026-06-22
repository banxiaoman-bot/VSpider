"""J1-J6 navigation / accessibility pattern scenarios — real Chromium e2e.

J1  Tab panel        — tab switching, content update, URL hash sync
J2  Inline edit      — dblclick to edit cell, save/cancel
J3  Autocomplete     — debounced input, dropdown, select
J4  Toast notify     — success/error/warning, auto-dismiss, stack
J5  Hash navigation  — hash change → content, back/forward
J6  ARIA accessible  — aria-expanded, aria-selected, listbox
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


# ===================================================================
# J1: Tab panel
# ===================================================================

class TestJ1_TabPanel:
    def test_initial_tab_is_overview(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tabs")
        assert pg.is_visible("#tab-overview")
        assert pg.is_hidden("#tab-specs")
        text = pg.text_content("#tab-overview")
        assert "12 款" in text

    def test_switch_tab_shows_content(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tabs")
        pg.click("[data-tab='tab-specs']")
        pg.wait_for_timeout(200)
        assert pg.is_visible("#tab-specs")
        assert pg.is_hidden("#tab-overview")
        text = pg.text_content("#tab-specs")
        assert "精度" in text

    def test_tab_updates_hash(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tabs")
        pg.click("[data-tab='tab-reviews']")
        pg.wait_for_timeout(200)
        url = pg.url
        assert "#tab-reviews" in url

    def test_hash_navigates_to_tab(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tabs#tab-specs")
        pg.wait_for_timeout(300)
        assert pg.is_visible("#tab-specs")
        assert pg.is_hidden("#tab-overview")


# ===================================================================
# J2: Inline table editing
# ===================================================================

class TestJ2_InlineEdit:
    def test_dblclick_opens_input(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/inline-edit")
        cell = pg.query_selector("td.ie-name")
        cell.dblclick()
        pg.wait_for_timeout(200)
        inp = cell.query_selector("input.ie-input")
        assert inp is not None

    def test_enter_saves_edit(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/inline-edit")
        cell = pg.query_selector("td.ie-name")
        original = cell.text_content()
        cell.dblclick()
        pg.wait_for_timeout(200)
        inp = cell.query_selector("input.ie-input")
        inp.fill("改名设备")
        inp.press("Enter")
        pg.wait_for_timeout(200)
        assert cell.text_content() == "改名设备"
        last = pg.text_content("#ie-last-edit")
        assert "name=改名设备" in last

    def test_escape_cancels_edit(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/inline-edit")
        cell = pg.query_selector("td.ie-name")
        original = cell.text_content()
        cell.dblclick()
        pg.wait_for_timeout(200)
        inp = cell.query_selector("input.ie-input")
        inp.fill("不保存")
        inp.press("Escape")
        pg.wait_for_timeout(200)
        assert cell.text_content() == original

    def test_edit_price_column(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/inline-edit")
        cell = pg.query_selector("td.ie-price")
        cell.dblclick()
        pg.wait_for_timeout(200)
        inp = cell.query_selector("input.ie-input")
        inp.fill("999.99")
        inp.press("Enter")
        pg.wait_for_timeout(200)
        assert cell.text_content() == "999.99"
        last = pg.text_content("#ie-last-edit")
        assert "price=999.99" in last


# ===================================================================
# J3: Autocomplete
# ===================================================================

class TestJ3_Autocomplete:
    def test_typing_shows_suggestions(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/autocomplete")
        pg.fill("#ac-input", "温控")
        pg.wait_for_function(
            "document.getElementById('ac-suggestions').style.display === 'block'",
            timeout=5000,
        )
        options = pg.query_selector_all(".ac-option")
        assert len(options) >= 1
        assert any("温控" in o.text_content() for o in options)

    def test_selecting_option_fills_input(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/autocomplete")
        pg.fill("#ac-input", "网关")
        pg.wait_for_function(
            "document.getElementById('ac-suggestions').style.display === 'block'",
            timeout=5000,
        )
        pg.click(".ac-option")
        pg.wait_for_timeout(200)
        selected = pg.text_content("#ac-selected")
        assert selected != "none"
        assert pg.evaluate(
            "document.getElementById('ac-suggestions').style.display"
        ) == "none"

    def test_no_match_hides_suggestions(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/autocomplete")
        pg.fill("#ac-input", "不存在的产品xyz")
        pg.wait_for_timeout(500)
        display = pg.evaluate(
            "document.getElementById('ac-suggestions').style.display"
        )
        assert display == "none"


# ===================================================================
# J4: Toast notifications
# ===================================================================

class TestJ4_Toast:
    def test_success_toast_appears(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/toast")
        pg.click("#toast-success")
        pg.wait_for_timeout(200)
        toasts = pg.query_selector_all(".toast-success")
        assert len(toasts) >= 1
        assert "操作成功" in toasts[0].text_content()

    def test_toast_auto_dismiss(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/toast")
        pg.click("#toast-error")
        pg.wait_for_timeout(200)
        assert len(pg.query_selector_all(".toast-error")) >= 1
        pg.wait_for_timeout(1500)
        remaining = pg.query_selector_all(".toast-error")
        assert len(remaining) == 0

    def test_multiple_toasts_stack(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/toast")
        pg.click("#toast-success")
        pg.click("#toast-error")
        pg.click("#toast-warning")
        pg.wait_for_timeout(300)
        toasts = pg.query_selector_all(".toast")
        assert len(toasts) == 3
        count = pg.text_content("#toast-count")
        assert count == "3"


# ===================================================================
# J5: URL hash navigation
# ===================================================================

class TestJ5_HashNav:
    def test_default_shows_home(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/hash-nav")
        title = pg.text_content("#hash-section-title")
        assert title == "首页"

    def test_click_link_changes_content(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/hash-nav")
        pg.click("a[href='#about']")
        pg.wait_for_timeout(300)
        title = pg.text_content("#hash-section-title")
        assert title == "关于"
        body = pg.text_content("#hash-section-body")
        assert "公司" in body

    def test_direct_hash_url(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/hash-nav#contact")
        pg.wait_for_timeout(300)
        title = pg.text_content("#hash-section-title")
        assert title == "联系"

    def test_back_forward_navigation(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/hash-nav")
        pg.click("a[href='#about']")
        pg.wait_for_timeout(200)
        pg.click("a[href='#contact']")
        pg.wait_for_timeout(200)
        pg.go_back()
        pg.wait_for_timeout(300)
        title = pg.text_content("#hash-section-title")
        assert title == "关于"
        pg.go_forward()
        pg.wait_for_timeout(300)
        title = pg.text_content("#hash-section-title")
        assert title == "联系"


# ===================================================================
# J6: ARIA accessibility
# ===================================================================

class TestJ6_Aria:
    def test_accordion_expand_collapse(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/aria")
        btn = pg.query_selector(".acc-header")
        assert btn.get_attribute("aria-expanded") == "false"
        btn.click()
        pg.wait_for_timeout(200)
        assert btn.get_attribute("aria-expanded") == "true"
        panel = pg.query_selector("#acc-panel-1")
        assert panel.get_attribute("aria-hidden") == "false"
        assert panel.is_visible()
        btn.click()
        pg.wait_for_timeout(200)
        assert btn.get_attribute("aria-expanded") == "false"
        assert panel.get_attribute("aria-hidden") == "true"

    def test_listbox_selection(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/aria")
        opt2 = pg.query_selector("#opt-2")
        assert opt2.get_attribute("aria-selected") == "false"
        opt2.click()
        pg.wait_for_timeout(200)
        assert opt2.get_attribute("aria-selected") == "true"
        selected = pg.text_content("#aria-selected")
        assert "工业网关" in selected

    def test_single_selection_deselects_others(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/aria")
        pg.click("#opt-1")
        pg.wait_for_timeout(100)
        assert pg.get_attribute("#opt-1", "aria-selected") == "true"
        pg.click("#opt-3")
        pg.wait_for_timeout(100)
        assert pg.get_attribute("#opt-1", "aria-selected") == "false"
        assert pg.get_attribute("#opt-3", "aria-selected") == "true"

    def test_aria_labels_exist(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/aria")
        listbox = pg.query_selector("[role=listbox]")
        assert listbox.get_attribute("aria-label") == "产品选择"
        btns = pg.query_selector_all(".acc-header")
        for btn in btns:
            assert btn.get_attribute("aria-controls") is not None
        panels = pg.query_selector_all(".acc-panel")
        for panel in panels:
            assert panel.get_attribute("role") == "region"

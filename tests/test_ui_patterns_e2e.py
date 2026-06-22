"""I1-I6 UI pattern scenarios — real Chromium e2e.

I1  Modal dialog     — <dialog> open/close, form submit, cancel
I2  Progress bar     — multi-step progress, cancel, reset
I3  Responsive       — viewport resize triggers layout change
I4  HTTP ETag cache  — conditional requests, 304 Not Modified
I5  Rich text edit   — bold/italic/underline, HTML export
I6  Data attributes  — read/write data-*, filter by attribute
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
# I1: Modal dialog
# ===================================================================

class TestI1_Modal:
    def test_dialog_opens_and_closes(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/modal")
        assert pg.is_hidden("#my-dialog")
        pg.click("#modal-open")
        pg.wait_for_selector("#my-dialog[open]", timeout=3000)
        assert pg.is_visible("#dialog-heading")

    def test_confirm_submits_form_data(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/modal")
        pg.click("#modal-open")
        pg.wait_for_selector("#my-dialog[open]")
        pg.fill("#dlg-product", "温控器")
        pg.fill("#dlg-qty", "3")
        pg.click("#dlg-confirm")
        pg.wait_for_function(
            "document.getElementById('modal-result').textContent !== 'none'",
            timeout=3000,
        )
        result = pg.text_content("#modal-result")
        assert result == "温控器x3"

    def test_cancel_sets_cancelled(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/modal")
        pg.click("#modal-open")
        pg.wait_for_selector("#my-dialog[open]")
        pg.click("#dlg-cancel")
        pg.wait_for_function(
            "document.getElementById('modal-result').textContent !== 'none'",
            timeout=3000,
        )
        result = pg.text_content("#modal-result")
        assert result == "cancelled"


# ===================================================================
# I2: Progress bar
# ===================================================================

class TestI2_Progress:
    def test_start_to_complete(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/progress")
        pg.click("#prog-start")
        pg.wait_for_function(
            "document.getElementById('prog-status').textContent === 'complete'",
            timeout=10000,
        )
        value = pg.text_content("#prog-value")
        assert value == "100"

    def test_cancel_stops_progress(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/progress")
        pg.click("#prog-start")
        pg.wait_for_function(
            "parseInt(document.getElementById('prog-value').textContent) >= 20",
            timeout=5000,
        )
        pg.click("#prog-cancel")
        pg.wait_for_timeout(300)
        status = pg.text_content("#prog-status")
        assert status == "cancelled"
        val = int(pg.text_content("#prog-value"))
        assert 20 <= val < 100

    def test_reset_returns_to_zero(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/progress")
        pg.click("#prog-start")
        pg.wait_for_function(
            "parseInt(document.getElementById('prog-value').textContent) >= 30",
            timeout=5000,
        )
        pg.click("#prog-reset")
        pg.wait_for_timeout(200)
        status = pg.text_content("#prog-status")
        assert status == "idle"
        value = pg.text_content("#prog-value")
        assert value == "0"


# ===================================================================
# I3: Responsive layout
# ===================================================================

class TestI3_Responsive:
    def test_wide_viewport_shows_sidebar(self, page):
        pg, infra = page
        pg.set_viewport_size({"width": 1024, "height": 768})
        pg.goto(f"{infra['alpha_url']}/responsive")
        mode = pg.text_content("#resp-mode")
        assert mode == "wide"
        direction = pg.evaluate(
            "getComputedStyle(document.getElementById('resp-layout')).flexDirection"
        )
        assert direction == "row"

    def test_narrow_viewport_stacks_vertically(self, page):
        pg, infra = page
        pg.set_viewport_size({"width": 400, "height": 768})
        pg.goto(f"{infra['alpha_url']}/responsive")
        mode = pg.text_content("#resp-mode")
        assert mode == "narrow"
        direction = pg.evaluate(
            "getComputedStyle(document.getElementById('resp-layout')).flexDirection"
        )
        assert direction == "column"

    def test_resize_triggers_mode_change(self, page):
        pg, infra = page
        pg.set_viewport_size({"width": 1024, "height": 768})
        pg.goto(f"{infra['alpha_url']}/responsive")
        assert pg.text_content("#resp-mode") == "wide"
        pg.set_viewport_size({"width": 500, "height": 768})
        pg.wait_for_timeout(300)
        assert pg.text_content("#resp-mode") == "narrow"


# ===================================================================
# I4: HTTP ETag caching
# ===================================================================

class TestI4_ETagCache:
    def test_first_request_returns_200(self, page):
        pg, infra = page
        resp = pg.request.get(f"{infra['alpha_url']}/api/cached-data?v=1")
        assert resp.status == 200
        data = resp.json()
        assert data["version"] == 1
        assert resp.headers.get("etag") == '"v1"'

    def test_conditional_request_304(self, page):
        pg, infra = page
        resp = pg.request.get(
            f"{infra['alpha_url']}/api/cached-data?v=1",
            headers={"If-None-Match": '"v1"'},
        )
        assert resp.status == 304

    def test_etag_mismatch_returns_200(self, page):
        pg, infra = page
        resp = pg.request.get(
            f"{infra['alpha_url']}/api/cached-data?v=2",
            headers={"If-None-Match": '"v1"'},
        )
        assert resp.status == 200
        data = resp.json()
        assert data["version"] == 2


# ===================================================================
# I5: Rich text editing
# ===================================================================

class TestI5_RichText:
    def test_bold_applies_strong_tag(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/richtext")
        editor = pg.query_selector("#rt-editor")
        editor.click()
        pg.keyboard.press("Control+a")
        pg.click("#rt-bold")
        pg.click("#rt-export")
        output = pg.text_content("#rt-output")
        assert "<b>" in output or "<strong>" in output

    def test_italic_applies_tag(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/richtext")
        editor = pg.query_selector("#rt-editor")
        editor.click()
        pg.keyboard.press("Control+a")
        pg.click("#rt-italic")
        pg.click("#rt-export")
        output = pg.text_content("#rt-output")
        assert "<i>" in output or "<em>" in output

    def test_type_and_export(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/richtext")
        editor = pg.query_selector("#rt-editor")
        editor.click()
        pg.keyboard.press("Control+a")
        pg.keyboard.type("新内容测试")
        pg.click("#rt-export")
        output = pg.text_content("#rt-output")
        assert "新内容测试" in output


# ===================================================================
# I6: Data attribute manipulation
# ===================================================================

class TestI6_DataAttr:
    def test_initial_all_visible(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/data-attr")
        items = pg.query_selector_all(".da-item")
        visible = [it for it in items if it.is_visible()]
        assert len(visible) == 8

    def test_filter_by_category(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/data-attr")
        pg.click("#da-filter-sensor")
        pg.wait_for_timeout(200)
        items = pg.query_selector_all(".da-item")
        visible = [it for it in items if it.is_visible()]
        for it in visible:
            cat = it.get_attribute("data-category")
            assert cat == "传感"

    def test_toggle_selection(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/data-attr")
        pg.click("#item-1 .da-toggle")
        pg.click("#item-3 .da-toggle")
        pg.wait_for_timeout(200)
        count = pg.text_content("#da-selected-count")
        assert count == "2"
        sel1 = pg.get_attribute("#item-1", "data-selected")
        assert sel1 == "true"
        sel2 = pg.get_attribute("#item-2", "data-selected")
        assert sel2 == "false"

    def test_deselect_toggles_back(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/data-attr")
        pg.click("#item-1 .da-toggle")
        pg.wait_for_timeout(100)
        assert pg.get_attribute("#item-1", "data-selected") == "true"
        pg.click("#item-1 .da-toggle")
        pg.wait_for_timeout(100)
        assert pg.get_attribute("#item-1", "data-selected") == "false"
        count = pg.text_content("#da-selected-count")
        assert count == "0"

    def test_filter_then_select(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/data-attr")
        pg.click("#da-filter-network")
        pg.wait_for_timeout(200)
        visible = [
            it for it in pg.query_selector_all(".da-item") if it.is_visible()
        ]
        for it in visible:
            it.query_selector(".da-toggle").click()
        pg.wait_for_timeout(200)
        count = int(pg.text_content("#da-selected-count"))
        assert count == len(visible)
        assert count >= 1

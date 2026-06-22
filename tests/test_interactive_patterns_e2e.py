"""N1-N6 interactive pattern scenarios — real Chromium e2e.

N1  Countdown       — start, pause, reset, finish callback
N2  Reorder list    — move items up/down, verify order
N3  Search highlight— search text, highlight matches, count
N4  Card grid       — click to expand, collapse, count expanded
N5  Sticky header   — scroll preserves header visibility
N6  Conditional form— radio switches visible sections
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


class TestN1_Countdown:
    def test_runs_to_zero(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/countdown")
        pg.click("#cd-start")
        pg.wait_for_function(
            "document.getElementById('cd-status').textContent === 'finished'",
            timeout=10000,
        )
        assert pg.text_content("#cd-value") == "0"

    def test_pause_stops(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/countdown")
        pg.click("#cd-start")
        pg.wait_for_function(
            "parseInt(document.getElementById('cd-value').textContent) <= 7",
            timeout=5000,
        )
        pg.click("#cd-pause")
        val = int(pg.text_content("#cd-value"))
        pg.wait_for_timeout(300)
        assert int(pg.text_content("#cd-value")) == val

    def test_reset_returns_to_10(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/countdown")
        pg.click("#cd-start")
        pg.wait_for_timeout(300)
        pg.click("#cd-reset")
        assert pg.text_content("#cd-value") == "10"
        assert pg.text_content("#cd-status") == "idle"


class TestN2_ReorderList:
    def test_initial_order(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/reorder")
        order = pg.text_content("#ro-order")
        assert order == "1,2,3,4,5"

    def test_move_down(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/reorder")
        pg.click(".ro-item[data-id='1']")
        pg.click("#ro-move-down")
        pg.wait_for_timeout(200)
        order = pg.text_content("#ro-order")
        assert order == "2,1,3,4,5"

    def test_move_up(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/reorder")
        pg.click(".ro-item[data-id='3']")
        pg.click("#ro-move-up")
        pg.wait_for_timeout(200)
        order = pg.text_content("#ro-order")
        assert order == "1,3,2,4,5"


class TestN3_SearchHighlight:
    def test_search_finds_matches(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/search-highlight")
        pg.fill("#sh-input", "传感")
        pg.click("#sh-search")
        pg.wait_for_timeout(200)
        count = int(pg.text_content("#sh-match-count"))
        assert count >= 1

    def test_highlight_marks_present(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/search-highlight")
        pg.fill("#sh-input", "工业")
        pg.click("#sh-search")
        pg.wait_for_timeout(200)
        marks = pg.query_selector_all(".sh-highlight")
        assert len(marks) >= 1
        assert "工业" in marks[0].text_content()

    def test_no_match_zero_count(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/search-highlight")
        pg.fill("#sh-input", "不存在的词汇xyz")
        pg.click("#sh-search")
        pg.wait_for_timeout(200)
        count = pg.text_content("#sh-match-count")
        assert count == "0"


class TestN4_CardGrid:
    def test_click_expands_card(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/card-grid")
        pg.click("#card-1")
        pg.wait_for_timeout(200)
        detail = pg.query_selector("#detail-1")
        assert detail.is_visible()

    def test_click_again_collapses(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/card-grid")
        pg.click("#card-2")
        pg.wait_for_timeout(200)
        pg.click("#card-2")
        pg.wait_for_timeout(200)
        detail = pg.query_selector("#detail-2")
        assert not detail.is_visible()

    def test_multiple_expanded(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/card-grid")
        pg.click("#card-1")
        pg.click("#card-3")
        pg.wait_for_timeout(200)
        assert pg.query_selector("#detail-1").is_visible()
        assert pg.query_selector("#detail-3").is_visible()


class TestN5_StickyHeader:
    def test_header_visible_after_scroll(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/sticky-table")
        pg.evaluate(
            "document.getElementById('st-container').scrollTop = 300"
        )
        pg.wait_for_timeout(200)
        th = pg.query_selector("#st-th-sku")
        assert th.is_visible()

    def test_rows_scroll_while_header_stays(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/sticky-table")
        first_row_before = pg.evaluate(
            "document.querySelector('#st-table tbody tr').getBoundingClientRect().top"
        )
        pg.evaluate(
            "document.getElementById('st-container').scrollTop = 100"
        )
        pg.wait_for_timeout(200)
        first_row_after = pg.evaluate(
            "document.querySelector('#st-table tbody tr').getBoundingClientRect().top"
        )
        assert first_row_after < first_row_before


class TestN6_ConditionalForm:
    def test_default_shows_personal(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/conditional-form")
        assert pg.is_visible("#sec-personal")
        assert pg.is_hidden("#sec-company")
        assert pg.is_hidden("#sec-government")

    def test_switch_to_company(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/conditional-form")
        pg.click("input[value='company']")
        pg.wait_for_timeout(200)
        assert pg.is_hidden("#sec-personal")
        assert pg.is_visible("#sec-company")

    def test_switch_to_government(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/conditional-form")
        pg.click("input[value='government']")
        pg.wait_for_timeout(200)
        assert pg.is_visible("#sec-government")
        assert pg.is_hidden("#sec-company")

    def test_submit_company_fields(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/conditional-form")
        pg.click("input[value='company']")
        pg.fill("#cf-company", "测试公司")
        pg.fill("#cf-taxid", "91110000")
        pg.fill("#cf-legal", "张三")
        pg.click("#cf-submit")
        pg.wait_for_timeout(200)
        result = pg.text_content("#cf-result")
        assert "company:" in result
        assert "测试公司" in result
        assert "91110000" in result

"""K1-K6 widget scenarios — real Chromium e2e.

K1  Tree view       — expand/collapse, nested nodes, select
K2  Carousel        — next/prev, dots, auto-play
K3  Client sort     — click header, multi-col, asc/desc toggle
K4  Drop zone       — file input, preview, count
K5  Date picker     — calendar, select date, change month
K6  Virtual scroll  — large list, visible range, scroll to index
"""
from __future__ import annotations

import os
import sys
import tempfile

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
# K1: Tree view
# ===================================================================

class TestK1_TreeView:
    def test_root_visible_children_hidden(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tree")
        assert pg.is_visible("#node-root")
        assert pg.is_hidden("#children-root")

    def test_expand_shows_children(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tree")
        pg.click("#node-root > .tree-toggle")
        pg.wait_for_timeout(200)
        assert pg.is_visible("#children-root")
        children = pg.query_selector_all("#children-root > .tree-node")
        assert len(children) == 3

    def test_nested_expand(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tree")
        pg.click("#node-root > .tree-toggle")
        pg.wait_for_timeout(200)
        pg.click("#node-sensor > .tree-toggle")
        pg.wait_for_timeout(200)
        assert pg.is_visible("#children-sensor")
        leaves = pg.query_selector_all("#children-sensor > .tree-node")
        assert len(leaves) == 3

    def test_select_node(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tree")
        pg.click("#node-root > .tree-toggle")
        pg.wait_for_timeout(200)
        pg.click("#node-sensor > .tree-toggle")
        pg.wait_for_timeout(200)
        pg.click("#node-temp > .tree-label")
        pg.wait_for_timeout(200)
        selected = pg.text_content("#tree-selected")
        assert "温控器" in selected

    def test_collapse_hides_children(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tree")
        pg.click("#node-root > .tree-toggle")
        pg.wait_for_timeout(200)
        assert pg.is_visible("#children-root")
        pg.click("#node-root > .tree-toggle")
        pg.wait_for_timeout(200)
        assert pg.is_hidden("#children-root")


# ===================================================================
# K2: Carousel
# ===================================================================

class TestK2_Carousel:
    def test_initial_shows_first_slide(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/carousel")
        first = pg.query_selector(".carousel-slide[data-index='0']")
        assert first.is_visible()
        second = pg.query_selector(".carousel-slide[data-index='1']")
        assert not second.is_visible()
        current = pg.text_content("#carousel-current")
        assert current == "0"

    def test_next_advances_slide(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/carousel")
        pg.click("#carousel-next")
        pg.wait_for_timeout(200)
        assert pg.text_content("#carousel-current") == "1"
        second = pg.query_selector(".carousel-slide[data-index='1']")
        assert second.is_visible()

    def test_prev_goes_back(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/carousel")
        pg.click("#carousel-next")
        pg.click("#carousel-next")
        pg.click("#carousel-prev")
        pg.wait_for_timeout(200)
        assert pg.text_content("#carousel-current") == "1"

    def test_dots_navigate(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/carousel")
        pg.click(".carousel-dot[data-index='2']")
        pg.wait_for_timeout(200)
        assert pg.text_content("#carousel-current") == "2"
        third = pg.query_selector(".carousel-slide[data-index='2']")
        assert third.is_visible()

    def test_wraps_around(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/carousel")
        pg.click("#carousel-prev")
        pg.wait_for_timeout(200)
        assert pg.text_content("#carousel-current") == "3"


# ===================================================================
# K3: Client-side table sort
# ===================================================================

class TestK3_SortableTable:
    def test_sort_by_price_ascending(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/sortable")
        pg.click("th[data-col='2']")
        pg.wait_for_timeout(300)
        assert pg.text_content("#sort-dir") == "asc"
        rows = pg.query_selector_all("#sort-table tbody tr")
        prices = [float(r.query_selector("td:nth-child(3)").get_attribute("data-value"))
                  for r in rows]
        assert prices == sorted(prices)

    def test_sort_by_price_descending(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/sortable")
        pg.click("th[data-col='2']")
        pg.click("th[data-col='2']")
        pg.wait_for_timeout(300)
        assert pg.text_content("#sort-dir") == "desc"
        rows = pg.query_selector_all("#sort-table tbody tr")
        prices = [float(r.query_selector("td:nth-child(3)").get_attribute("data-value"))
                  for r in rows]
        assert prices == sorted(prices, reverse=True)

    def test_sort_by_stock(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/sortable")
        pg.click("th[data-col='3']")
        pg.wait_for_timeout(300)
        assert pg.text_content("#sort-col") == "3"
        rows = pg.query_selector_all("#sort-table tbody tr")
        stocks = [int(r.query_selector("td:nth-child(4)").get_attribute("data-value"))
                  for r in rows]
        assert stocks == sorted(stocks)

    def test_sort_by_name_text(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/sortable")
        pg.click("th[data-col='1']")
        pg.wait_for_timeout(300)
        rows = pg.query_selector_all("#sort-table tbody tr")
        names = [r.query_selector("td:nth-child(2)").text_content() for r in rows]
        assert names == sorted(names)


# ===================================================================
# K4: Drop zone (file input, since drag simulation is complex)
# ===================================================================

class TestK4_DropZone:
    def test_file_input_shows_preview(self, page):
        pg, infra = page
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        tmp.write("test content")
        tmp.close()
        try:
            pg.goto(f"{infra['alpha_url']}/dropzone")
            pg.set_input_files("#dz-input", tmp.name)
            pg.wait_for_timeout(300)
            count = pg.text_content("#dz-count")
            assert count == "1"
            previews = pg.query_selector_all(".dz-file")
            assert len(previews) == 1
            assert os.path.basename(tmp.name) in previews[0].text_content()
        finally:
            os.unlink(tmp.name)

    def test_multiple_files(self, page):
        pg, infra = page
        files = []
        for i in range(3):
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=f"_{i}.txt", delete=False, encoding="utf-8"
            )
            tmp.write(f"file {i}")
            tmp.close()
            files.append(tmp.name)
        try:
            pg.goto(f"{infra['alpha_url']}/dropzone")
            pg.set_input_files("#dz-input", files)
            pg.wait_for_timeout(300)
            count = pg.text_content("#dz-count")
            assert count == "3"
            previews = pg.query_selector_all(".dz-file")
            assert len(previews) == 3
        finally:
            for f in files:
                os.unlink(f)


# ===================================================================
# K5: Date picker
# ===================================================================

class TestK5_DatePicker:
    def test_calendar_renders_days(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/datepicker")
        days = pg.query_selector_all(".dp-day")
        assert len(days) >= 28

    def test_select_date(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/datepicker")
        days = pg.query_selector_all(".dp-day")
        days[14].click()
        pg.wait_for_timeout(200)
        selected = pg.text_content("#dp-selected")
        assert selected != "none"
        assert "-15" in selected

    def test_change_month_forward(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/datepicker")
        label_before = pg.text_content("#dp-month-label")
        pg.click("#dp-next-month")
        pg.wait_for_timeout(200)
        label_after = pg.text_content("#dp-month-label")
        assert label_before != label_after

    def test_change_month_backward(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/datepicker")
        label_before = pg.text_content("#dp-month-label")
        pg.click("#dp-prev-month")
        pg.wait_for_timeout(200)
        label_after = pg.text_content("#dp-month-label")
        assert label_before != label_after


# ===================================================================
# K6: Virtual scroll
# ===================================================================

class TestK6_VirtualScroll:
    def test_initial_renders_visible_items(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/virtual-scroll")
        items = pg.query_selector_all(".vs-item")
        assert 5 < len(items) < 50

    def test_scroll_updates_range(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/virtual-scroll")
        range_before = pg.text_content("#vs-range")
        pg.evaluate("document.getElementById('vs-container').scrollTop = 4000")
        pg.wait_for_timeout(300)
        range_after = pg.text_content("#vs-range")
        assert range_before != range_after
        start = int(range_after.split("-")[0])
        assert start > 50

    def test_scroll_to_bottom(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/virtual-scroll")
        pg.evaluate(
            "document.getElementById('vs-container').scrollTop = "
            "document.getElementById('vs-spacer').offsetHeight"
        )
        pg.wait_for_timeout(300)
        range_text = pg.text_content("#vs-range")
        end = int(range_text.split("-")[1])
        assert end >= 990

    def test_items_have_correct_content(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/virtual-scroll")
        pg.evaluate("document.getElementById('vs-container').scrollTop = 2000")
        pg.wait_for_timeout(300)
        items = pg.query_selector_all(".vs-item")
        for item in items:
            idx = item.get_attribute("data-index")
            text = item.text_content()
            assert f"项目 #{idx}" == text

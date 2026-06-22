"""O1-O6 control pattern scenarios — real Chromium e2e.

O1  Step indicator    — multi-step progress, click navigate
O2  Tag input         — add/remove tags, uniqueness
O3  Range slider      — drag value, display, bounds
O4  Color picker      — select color, preview, hex output
O5  Notification badge— counter increment/clear
O6  Nested accordion  — multi-level expand/collapse
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
    ScenarioStore, ScenarioServer, make_alpha_handler,
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


class TestO1_StepIndicator:
    def test_initial_step_zero(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/step-indicator")
        assert pg.text_content("#si-current") == "0"

    def test_next_advances(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/step-indicator")
        pg.click("#si-next")
        assert pg.text_content("#si-current") == "1"
        pg.click("#si-next")
        assert pg.text_content("#si-current") == "2"

    def test_prev_goes_back(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/step-indicator")
        pg.click("#si-next")
        pg.click("#si-next")
        pg.click("#si-prev")
        assert pg.text_content("#si-current") == "1"

    def test_cant_go_below_zero(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/step-indicator")
        pg.click("#si-prev")
        assert pg.text_content("#si-current") == "0"


class TestO2_TagInput:
    def test_add_tag(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tag-input")
        pg.fill("#ti-input", "Python")
        pg.press("#ti-input", "Enter")
        pg.wait_for_timeout(200)
        assert pg.text_content("#ti-count") == "1"
        tags = pg.query_selector_all(".ti-tag")
        assert len(tags) == 1

    def test_add_multiple_tags(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tag-input")
        for t in ["React", "Vue", "Angular"]:
            pg.fill("#ti-input", t)
            pg.press("#ti-input", "Enter")
        pg.wait_for_timeout(200)
        assert pg.text_content("#ti-count") == "3"

    def test_duplicate_rejected(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tag-input")
        pg.fill("#ti-input", "Java")
        pg.press("#ti-input", "Enter")
        pg.fill("#ti-input", "Java")
        pg.press("#ti-input", "Enter")
        pg.wait_for_timeout(200)
        assert pg.text_content("#ti-count") == "1"

    def test_remove_tag(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tag-input")
        pg.fill("#ti-input", "Rust")
        pg.press("#ti-input", "Enter")
        pg.wait_for_timeout(200)
        pg.click(".ti-remove")
        pg.wait_for_timeout(200)
        assert pg.text_content("#ti-count") == "0"


class TestO3_RangeSlider:
    def test_initial_value(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/range-slider")
        assert pg.text_content("#rs-value") == "50"

    def test_change_value(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/range-slider")
        pg.evaluate("""
            var s = document.getElementById('rs-slider');
            s.value = 75;
            s.dispatchEvent(new Event('input'));
        """)
        pg.wait_for_timeout(100)
        assert pg.text_content("#rs-value") == "75"

    def test_min_max_bounds(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/range-slider")
        min_val = pg.get_attribute("#rs-slider", "min")
        max_val = pg.get_attribute("#rs-slider", "max")
        assert min_val == "0"
        assert max_val == "100"


class TestO4_ColorPicker:
    def test_initial_color(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/color-picker")
        hex_text = pg.text_content("#cp-hex")
        assert hex_text == "#3498db"

    def test_change_color(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/color-picker")
        pg.evaluate("""
            var inp = document.getElementById('cp-input');
            inp.value = '#ff0000';
            inp.dispatchEvent(new Event('input'));
        """)
        pg.wait_for_timeout(100)
        assert pg.text_content("#cp-hex") == "#ff0000"
        bg = pg.evaluate(
            "document.getElementById('cp-preview').style.background"
        )
        assert "ff0000" in bg.lower() or "rgb(255, 0, 0)" in bg


class TestO5_Badge:
    def test_initial_zero(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/badge")
        assert pg.text_content("#bd-badge") == "0"

    def test_add_notifications(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/badge")
        pg.click("#bd-add")
        pg.click("#bd-add")
        pg.click("#bd-add")
        assert pg.text_content("#bd-badge") == "3"
        assert pg.text_content("#bd-total") == "3"

    def test_clear_resets(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/badge")
        pg.click("#bd-add")
        pg.click("#bd-add")
        pg.click("#bd-clear")
        assert pg.text_content("#bd-badge") == "0"


class TestO6_NestedAccordion:
    def test_first_level_expand(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/nested-accordion")
        assert pg.is_hidden("#na-c1")
        pg.click("#na-g1 > .na-toggle")
        pg.wait_for_timeout(200)
        assert pg.is_visible("#na-c1")

    def test_second_level_expand(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/nested-accordion")
        pg.click("#na-g1 > .na-toggle")
        pg.wait_for_timeout(200)
        pg.click("#na-g1-1 > .na-toggle")
        pg.wait_for_timeout(200)
        assert pg.is_visible("#na-c1-1")
        leaves = pg.query_selector_all("#na-c1-1 .na-leaf")
        assert len(leaves) == 2

    def test_independent_groups(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/nested-accordion")
        pg.click("#na-g1 > .na-toggle")
        pg.wait_for_timeout(100)
        pg.click("#na-g2 > .na-toggle")
        pg.wait_for_timeout(100)
        assert pg.is_visible("#na-c1")
        assert pg.is_visible("#na-c2")

    def test_collapse_parent_hides_children(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/nested-accordion")
        pg.click("#na-g1 > .na-toggle")
        pg.wait_for_timeout(100)
        pg.click("#na-g1-1 > .na-toggle")
        pg.wait_for_timeout(100)
        assert pg.is_visible("#na-c1-1")
        pg.click("#na-g1 > .na-toggle")
        pg.wait_for_timeout(100)
        assert pg.is_hidden("#na-c1")

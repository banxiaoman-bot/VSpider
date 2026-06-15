"""G1-G6 streaming / advanced browser scenarios — real Chromium e2e.

G1  Server-Sent Events  — SSE stream → page updates live
G2  Rate Limiting        — 429 + Retry-After, retry then success
G3  Lazy-loading images  — IntersectionObserver, scroll to trigger
G4  Multi-iframe comms   — postMessage parent ↔ children
G5  localStorage         — multi-key persist + survive reload
G6  Server-side paginate — filter + sort + CSV export
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import sys
import textwrap

import pytest

# ---------------------------------------------------------------------------
# Skip early if no browser available
# ---------------------------------------------------------------------------
_SKIP_REASON = ""

try:
    from playwright.sync_api import sync_playwright  # type: ignore
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

# ---------------------------------------------------------------------------
# Ensure tests/ is on sys.path
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from scenario_site import (  # noqa: E402
    ScenarioStore,
    ScenarioServer,
    make_alpha_handler,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def infra():
    store = ScenarioStore()
    alpha = ScenarioServer(make_alpha_handler(store)).start()
    yield {"store": store, "alpha_url": alpha.base_url, "store_ref": store}
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
# G1: Server-Sent Events
# ===================================================================

class TestG1_SSE:
    def test_sse_stream_receives_all_events(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/sse")
        pg.wait_for_selector("#sse-status", state="attached")
        pg.wait_for_function(
            "document.getElementById('sse-status').textContent === 'complete'",
            timeout=10000,
        )
        count = pg.text_content("#sse-count")
        assert int(count) == 5

    def test_sse_events_rendered_in_table(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/sse")
        pg.wait_for_function(
            "document.getElementById('sse-status').textContent === 'complete'",
            timeout=10000,
        )
        rows = pg.query_selector_all("#sse-events tbody tr")
        assert len(rows) == 5
        first_id = rows[0].get_attribute("data-event-id")
        assert first_id == "1"
        last_id = rows[-1].get_attribute("data-event-id")
        assert last_id == "5"

    def test_sse_done_event_closes_stream(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/sse")
        pg.wait_for_function(
            "document.getElementById('sse-status').textContent === 'complete'",
            timeout=10000,
        )
        status = pg.text_content("#sse-status")
        assert status == "complete"


# ===================================================================
# G2: Rate Limiting
# ===================================================================

class TestG2_RateLimit:
    def test_429_then_success(self, page):
        pg, infra = page
        store = infra["store_ref"]
        store.rate_limit_remaining = 2
        pg.goto(f"{infra['alpha_url']}/rate-limit")
        # First call → 429
        pg.click("#rl-fetch")
        pg.wait_for_function(
            "document.getElementById('rl-status').textContent === 'rate_limited'",
            timeout=5000,
        )
        blocked = pg.text_content("#rl-blocked")
        assert blocked == "1"
        retry_after = pg.evaluate(
            "document.getElementById('rl-status').dataset.retryAfter"
        )
        assert retry_after == "1"

        # Second call → still 429
        pg.click("#rl-fetch")
        pg.wait_for_function(
            "document.getElementById('rl-blocked').textContent === '2'",
            timeout=5000,
        )

        # Third call → success (rate_limit_remaining exhausted)
        pg.click("#rl-fetch")
        pg.wait_for_function(
            "document.getElementById('rl-status').textContent === 'ok'",
            timeout=5000,
        )
        success = pg.text_content("#rl-success")
        assert success == "1"

    def test_no_limit_direct_success(self, page):
        pg, infra = page
        store = infra["store_ref"]
        store.rate_limit_remaining = 0
        pg.goto(f"{infra['alpha_url']}/rate-limit")
        pg.click("#rl-fetch")
        pg.wait_for_function(
            "document.getElementById('rl-status').textContent === 'ok'",
            timeout=5000,
        )
        payload = pg.evaluate(
            "document.getElementById('rl-status').dataset.payload"
        )
        assert "rate_limit_passed" in payload


# ===================================================================
# G3: Lazy-loading images
# ===================================================================

class TestG3_LazyImages:
    def test_initial_only_visible_loaded(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/lazy-images")
        pg.wait_for_timeout(500)
        loaded = pg.query_selector_all("img.lazy.loaded")
        total = pg.query_selector_all("img.lazy")
        assert len(loaded) < len(total), "not all images should load initially"
        assert len(loaded) >= 1, "at least first image should be visible"

    def test_scroll_loads_all(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/lazy-images")
        pg.wait_for_timeout(300)
        # Scroll each card into view to trigger IntersectionObserver
        cards = pg.query_selector_all(".lazy-card")
        for card in cards:
            card.scroll_into_view_if_needed()
            pg.wait_for_timeout(100)
        pg.wait_for_function(
            f"document.getElementById('lazy-loaded').textContent === '8'",
            timeout=5000,
        )
        loaded_count = pg.text_content("#lazy-loaded")
        assert loaded_count == "8"

    def test_lazy_images_have_src_after_load(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/lazy-images")
        cards = pg.query_selector_all(".lazy-card")
        for card in cards:
            card.scroll_into_view_if_needed()
            pg.wait_for_timeout(100)
        pg.wait_for_function(
            "document.getElementById('lazy-loaded').textContent === '8'",
            timeout=5000,
        )
        imgs = pg.query_selector_all("img.lazy.loaded")
        for img in imgs:
            src = img.get_attribute("src")
            assert src and src.startswith("/img/lazy-")


# ===================================================================
# G4: Multi-iframe postMessage
# ===================================================================

class TestG4_IframeComm:
    def test_broadcast_and_replies(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/iframe-comm")
        pg.wait_for_selector("#frame-a", state="attached")
        pg.wait_for_selector("#frame-b", state="attached")
        # Wait for iframes to load
        pg.wait_for_timeout(500)
        pg.click("#iframe-send")
        pg.wait_for_function(
            "document.getElementById('iframe-replies').textContent === '2'",
            timeout=5000,
        )
        replies = pg.query_selector_all(".iframe-reply")
        assert len(replies) == 2
        froms = {r.get_attribute("data-from") for r in replies}
        assert froms == {"child-a", "child-b"}

    def test_child_a_computes_correctly(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/iframe-comm")
        pg.wait_for_timeout(500)
        pg.click("#iframe-send")
        pg.wait_for_function(
            "document.getElementById('iframe-replies').textContent === '2'",
            timeout=5000,
        )
        replies = pg.query_selector_all(".iframe-reply")
        texts = {r.get_attribute("data-from"): r.text_content() for r in replies}
        assert "84" in texts["child-a"]  # 42 * 2
        assert "126" in texts["child-b"]  # 42 * 3

    def test_iframe_children_update_status(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/iframe-comm")
        pg.wait_for_timeout(500)
        pg.click("#iframe-send")
        pg.wait_for_timeout(500)
        frame_a = pg.frame(name="frame-a") or pg.frames[1]
        status_a = frame_a.text_content("#child-status")
        assert "computed" in status_a


# ===================================================================
# G5: localStorage persistence
# ===================================================================

class TestG5_LocalStorage:
    def test_save_and_display(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/localstorage")
        pg.fill("#ls-key", "color")
        pg.fill("#ls-value", "blue")
        pg.click("#ls-save")
        pg.wait_for_function(
            "document.getElementById('ls-count').textContent === '1'",
            timeout=3000,
        )
        cells = pg.query_selector_all("#ls-table tbody td.ls-v")
        assert any(c.text_content() == "blue" for c in cells)

    def test_multiple_keys(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/localstorage")
        for k, v in [("a", "1"), ("b", "2"), ("c", "3")]:
            pg.fill("#ls-key", k)
            pg.fill("#ls-value", v)
            pg.click("#ls-save")
        pg.wait_for_function(
            "document.getElementById('ls-count').textContent === '3'",
            timeout=3000,
        )
        count = pg.text_content("#ls-count")
        assert count == "3"

    def test_survives_reload(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/localstorage")
        pg.fill("#ls-key", "persist")
        pg.fill("#ls-value", "yes")
        pg.click("#ls-save")
        pg.wait_for_function(
            "document.getElementById('ls-count').textContent !== '0'",
            timeout=3000,
        )
        pg.reload()
        pg.wait_for_selector("#ls-count", state="attached")
        count = int(pg.text_content("#ls-count"))
        assert count >= 1
        cells = pg.query_selector_all("#ls-table tbody td.ls-v")
        assert any(c.text_content() == "yes" for c in cells)

    def test_clear_all(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/localstorage")
        pg.fill("#ls-key", "tmp")
        pg.fill("#ls-value", "x")
        pg.click("#ls-save")
        pg.wait_for_function(
            "document.getElementById('ls-count').textContent !== '0'",
            timeout=3000,
        )
        pg.click("#ls-clear")
        pg.wait_for_function(
            "document.getElementById('ls-count').textContent === '0'",
            timeout=3000,
        )
        count = pg.text_content("#ls-count")
        assert count == "0"


# ===================================================================
# G6: Server-side paginated table + filter + sort + CSV export
# ===================================================================

class TestG6_ServerTable:
    def test_first_page_shows_3_rows(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/server-table")
        rows = pg.query_selector_all("#g6-table tbody tr")
        assert len(rows) == 3
        info = pg.text_content("#g6-page-info")
        assert "第 1 页" in info

    def test_navigate_to_page_2(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/server-table")
        pg.click("#g6-next")
        pg.wait_for_selector("#g6-page-info")
        info = pg.text_content("#g6-page-info")
        assert "第 2 页" in info
        rows = pg.query_selector_all("#g6-table tbody tr")
        assert len(rows) == 3

    def test_filter_by_category(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/server-table?category=传感")
        rows = pg.query_selector_all("#g6-table tbody tr")
        for row in rows:
            cells = row.query_selector_all("td")
            cat = cells[-1].text_content()
            assert cat == "传感"

    def test_sort_price_ascending(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/server-table?sort=price_asc")
        rows = pg.query_selector_all("#g6-table tbody tr")
        prices = []
        for row in rows:
            cells = row.query_selector_all("td")
            prices.append(float(cells[2].text_content()))
        assert prices == sorted(prices)

    def test_sort_price_descending(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/server-table?sort=price_desc")
        rows = pg.query_selector_all("#g6-table tbody tr")
        prices = []
        for row in rows:
            cells = row.query_selector_all("td")
            prices.append(float(cells[2].text_content()))
        assert prices == sorted(prices, reverse=True)

    def test_csv_export_all(self, page):
        pg, infra = page
        store = infra["store_ref"]
        before = store.csv_exports
        resp = pg.request.get(f"{infra['alpha_url']}/api/export-csv")
        assert resp.status == 200
        assert store.csv_exports == before + 1
        text = resp.text()
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        assert len(rows) == 12

    def test_csv_export_filtered(self, page):
        pg, infra = page
        resp = pg.request.get(
            f"{infra['alpha_url']}/api/export-csv?category=网络"
        )
        assert resp.status == 200
        text = resp.text()
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        assert len(rows) >= 1
        for r in rows:
            assert r["category"] == "网络"

    def test_combined_filter_sort_paginate(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/server-table?category=传感&sort=price_asc&page=1")
        rows = pg.query_selector_all("#g6-table tbody tr")
        assert len(rows) >= 1
        prices = []
        for row in rows:
            cells = row.query_selector_all("td")
            assert cells[-1].text_content() == "传感"
            prices.append(float(cells[2].text_content()))
        assert prices == sorted(prices)

"""H1-H6 component / browser API scenarios — real Chromium e2e.

H1  Shadow DOM       — custom element, shadow root, slot, internal form
H2  Web Worker       — offload computation, postMessage results
H3  MutationObserver — watch DOM changes, count mutations
H4  Theme switching  — dark/light toggle, computed styles, localStorage
H5  Tooltip / hover  — hover triggers, content check, hide on leave
H6  File processing  — upload → server process → result page
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Skip early if no browser available
# ---------------------------------------------------------------------------
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
# H1: Shadow DOM
# ===================================================================

class TestH1_ShadowDOM:
    def test_custom_element_renders_slot(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/shadow-dom")
        card = pg.query_selector("product-card")
        assert card is not None
        # Slot projection: check the light DOM span was slotted in
        slotted = card.evaluate(
            "el => el.querySelector('[slot=heading]').textContent"
        )
        assert "测试商品卡" in slotted
        # Also verify the shadow root's slot has assigned nodes
        has_assigned = card.evaluate(
            "el => el.shadowRoot.querySelector('slot[name=heading]')"
            ".assignedNodes().length > 0"
        )
        assert has_assigned is True

    def test_shadow_internal_form_submit(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/shadow-dom")
        card = pg.query_selector("product-card")
        shadow = card.evaluate_handle("el => el.shadowRoot")
        shadow.evaluate("root => root.getElementById('qty-input').value = '5'")
        shadow.evaluate("root => root.getElementById('note-input').value = '加急'")
        shadow.evaluate("root => root.getElementById('submit-btn').click()")
        pg.wait_for_timeout(300)
        result = pg.text_content("#shadow-result")
        assert result == "5|加急"

    def test_shadow_internal_status_updates(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/shadow-dom")
        card = pg.query_selector("product-card")
        shadow = card.evaluate_handle("el => el.shadowRoot")
        initial = shadow.evaluate(
            "root => root.getElementById('inner-status').textContent"
        )
        assert initial == "waiting"
        shadow.evaluate("root => root.getElementById('submit-btn').click()")
        pg.wait_for_timeout(200)
        after = shadow.evaluate(
            "root => root.getElementById('inner-status').textContent"
        )
        assert "submitted" in after


# ===================================================================
# H2: Web Worker
# ===================================================================

class TestH2_WebWorker:
    def test_worker_computes_sum(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/worker")
        pg.click("#worker-start")
        pg.wait_for_function(
            "document.getElementById('worker-status').textContent === 'done'",
            timeout=10000,
        )
        result = pg.text_content("#worker-result")
        assert "sum(1..100)=5050" in result

    def test_worker_status_transitions(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/worker")
        initial = pg.text_content("#worker-status")
        assert initial == "idle"
        pg.click("#worker-start")
        pg.wait_for_function(
            "document.getElementById('worker-status').textContent === 'done'",
            timeout=10000,
        )
        final = pg.text_content("#worker-status")
        assert final == "done"


# ===================================================================
# H3: MutationObserver
# ===================================================================

class TestH3_MutationObserver:
    def test_add_nodes_increments_count(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/mutation-observer")
        pg.click("#mo-add")
        pg.click("#mo-add")
        pg.click("#mo-add")
        pg.wait_for_timeout(200)
        count = int(pg.text_content("#mo-count"))
        assert count >= 3

    def test_modify_triggers_mutation(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/mutation-observer")
        pg.click("#mo-add")
        pg.wait_for_timeout(100)
        before = int(pg.text_content("#mo-count"))
        pg.click("#mo-modify")
        pg.wait_for_timeout(200)
        after = int(pg.text_content("#mo-count"))
        assert after > before

    def test_added_nodes_have_correct_content(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/mutation-observer")
        for _ in range(3):
            pg.click("#mo-add")
            pg.wait_for_timeout(50)
        items = pg.query_selector_all(".mo-item")
        assert len(items) == 3
        texts = [it.text_content() for it in items]
        assert "节点 1" in texts[0]
        assert "节点 3" in texts[2]


# ===================================================================
# H4: Theme switching
# ===================================================================

class TestH4_Theme:
    def test_default_is_light(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/theme")
        label = pg.text_content("#theme-label")
        assert "light" in label
        has_dark = pg.evaluate(
            "document.body.classList.contains('dark')"
        )
        assert has_dark is False

    def test_toggle_to_dark(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/theme")
        pg.click("#theme-toggle")
        pg.wait_for_timeout(200)
        label = pg.text_content("#theme-label")
        assert "dark" in label
        has_dark = pg.evaluate(
            "document.body.classList.contains('dark')"
        )
        assert has_dark is True

    def test_dark_persists_via_localstorage(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/theme")
        pg.click("#theme-toggle")
        pg.wait_for_timeout(200)
        pg.reload()
        pg.wait_for_selector("#theme-label")
        label = pg.text_content("#theme-label")
        assert "dark" in label

    def test_toggle_back_to_light(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/theme")
        pg.click("#theme-toggle")  # → dark
        pg.click("#theme-toggle")  # → light
        pg.wait_for_timeout(200)
        label = pg.text_content("#theme-label")
        assert "light" in label


# ===================================================================
# H5: Tooltip / hover
# ===================================================================

class TestH5_Tooltip:
    def test_hover_shows_tooltip(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tooltip")
        trigger = pg.query_selector("#tip-1")
        trigger.hover()
        pg.wait_for_timeout(300)
        box = pg.query_selector("#tooltip-box")
        display = box.evaluate("el => getComputedStyle(el).display")
        assert display != "none"
        text = box.text_content()
        assert "精度" in text

    def test_leave_hides_tooltip(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tooltip")
        pg.hover("#tip-1")
        pg.wait_for_timeout(200)
        pg.hover("#tip-title")  # move away
        pg.wait_for_timeout(200)
        display = pg.evaluate(
            "getComputedStyle(document.getElementById('tooltip-box')).display"
        )
        assert display == "none"

    def test_different_tooltips_for_different_items(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/tooltip")
        pg.hover("#tip-2")
        pg.wait_for_timeout(200)
        text2 = pg.text_content("#tooltip-box")
        assert "Modbus" in text2

        pg.hover("#tip-3")
        pg.wait_for_timeout(200)
        text3 = pg.text_content("#tooltip-box")
        assert "ARM" in text3


# ===================================================================
# H6: File processing pipeline
# ===================================================================

class TestH6_FileProcess:
    def test_uppercase_processing(self, page):
        pg, infra = page
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        tmp.write("hello world\nfoo bar")
        tmp.close()
        try:
            pg.goto(f"{infra['alpha_url']}/file-process")
            pg.set_input_files("#fp-file", tmp.name)
            pg.select_option("#fp-action", "uppercase")
            pg.click("#fp-submit")
            pg.wait_for_selector("#fp-done", timeout=10000)
            output = pg.text_content("#fp-output")
            assert "HELLO WORLD" in output
            assert "FOO BAR" in output
            action = pg.text_content("#fp-action")
            assert "uppercase" in action
        finally:
            os.unlink(tmp.name)

    def test_reverse_processing(self, page):
        pg, infra = page
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        tmp.write("abcdef")
        tmp.close()
        try:
            pg.goto(f"{infra['alpha_url']}/file-process")
            pg.set_input_files("#fp-file", tmp.name)
            pg.select_option("#fp-action", "reverse")
            pg.click("#fp-submit")
            pg.wait_for_selector("#fp-done", timeout=10000)
            output = pg.text_content("#fp-output")
            assert "fedcba" in output
        finally:
            os.unlink(tmp.name)

    def test_linecount_processing(self, page):
        pg, infra = page
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        tmp.write("line1\nline2\nline3\nline4")
        tmp.close()
        try:
            pg.goto(f"{infra['alpha_url']}/file-process")
            pg.set_input_files("#fp-file", tmp.name)
            pg.select_option("#fp-action", "linecount")
            pg.click("#fp-submit")
            pg.wait_for_selector("#fp-done", timeout=10000)
            output = pg.text_content("#fp-output")
            assert output.strip() == "4"
        finally:
            os.unlink(tmp.name)

    def test_result_page_shows_filename(self, page):
        pg, infra = page
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
            prefix="myfile_"
        )
        tmp.write("test")
        tmp.close()
        try:
            pg.goto(f"{infra['alpha_url']}/file-process")
            pg.set_input_files("#fp-file", tmp.name)
            pg.click("#fp-submit")
            pg.wait_for_selector("#fp-done", timeout=10000)
            fname = pg.text_content("#fp-filename")
            assert os.path.basename(tmp.name) in fname
        finally:
            os.unlink(tmp.name)

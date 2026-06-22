"""M1-M6 browser API scenarios — real Chromium e2e.

M1  Canvas          — draw, read pixel, clear
M2  Web Animation   — start, pause, finish
M3  Multi-form      — submit one doesn't affect other
M4  Cookie mgmt     — set, read, delete cookies
M5  Nav guard       — beforeunload dirty detection
M6  Undo/Redo       — history tracking, undo, redo
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
# M1: Canvas
# ===================================================================

class TestM1_Canvas:
    def test_draw_and_read_pixel(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/canvas")
        pg.click("#cv-draw")
        pg.click("#cv-read")
        pixel = pg.text_content("#cv-pixel")
        assert "255" in pixel  # red channel
        assert "rgba" in pixel

    def test_clear_canvas(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/canvas")
        pg.click("#cv-draw")
        pg.click("#cv-clear")
        pixel = pg.text_content("#cv-pixel")
        assert pixel == "cleared"

    def test_canvas_screenshot(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/canvas")
        pg.click("#cv-draw")
        data_url = pg.evaluate(
            "document.getElementById('cv-canvas').toDataURL('image/png')"
        )
        assert data_url.startswith("data:image/png;base64,")
        assert len(data_url) > 100


# ===================================================================
# M2: Web Animation
# ===================================================================

class TestM2_Animation:
    def test_start_animation(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/animation")
        pg.click("#anim-start")
        pg.wait_for_timeout(100)
        status = pg.text_content("#anim-status")
        assert status in ("running", "finished")

    def test_animation_finishes(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/animation")
        pg.click("#anim-start")
        pg.wait_for_function(
            "document.getElementById('anim-status').textContent === 'finished'",
            timeout=5000,
        )
        assert pg.text_content("#anim-status") == "finished"

    def test_pause_and_resume(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/animation")
        pg.click("#anim-start")
        pg.wait_for_timeout(100)
        pg.click("#anim-pause")
        assert pg.text_content("#anim-status") == "paused"
        pg.click("#anim-resume")
        assert pg.text_content("#anim-status") == "running"


# ===================================================================
# M3: Multi-form isolation
# ===================================================================

class TestM3_MultiForm:
    def test_submit_a_does_not_affect_b(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/multiform")
        pg.fill("#mf-a-name", "TestA")
        pg.click("#mf-a-submit")
        pg.wait_for_timeout(200)
        a_result = pg.text_content("#mf-a-result")
        assert "TestA" in a_result
        b_result = pg.text_content("#mf-b-result")
        assert b_result == "none"

    def test_submit_b_does_not_affect_a(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/multiform")
        pg.fill("#mf-b-qty", "10")
        pg.click("#mf-b-submit")
        pg.wait_for_timeout(200)
        b_result = pg.text_content("#mf-b-result")
        assert "10" in b_result
        a_result = pg.text_content("#mf-a-result")
        assert a_result == "none"

    def test_both_forms_independent(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/multiform")
        pg.fill("#mf-a-name", "Alpha")
        pg.fill("#mf-b-name", "Beta")
        pg.click("#mf-a-submit")
        pg.click("#mf-b-submit")
        pg.wait_for_timeout(200)
        assert "Alpha" in pg.text_content("#mf-a-result")
        assert "Beta" in pg.text_content("#mf-b-result")


# ===================================================================
# M4: Cookie management
# ===================================================================

class TestM4_CookieManagement:
    def test_set_cookie(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/cookie-mgmt")
        pg.fill("#cm-key", "test_key")
        pg.fill("#cm-value", "test_val")
        pg.click("#cm-set")
        pg.wait_for_timeout(200)
        display = pg.text_content("#cm-display")
        assert "test_key=test_val" in display

    def test_delete_cookie(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/cookie-mgmt")
        pg.fill("#cm-key", "del_key")
        pg.fill("#cm-value", "del_val")
        pg.click("#cm-set")
        pg.wait_for_timeout(200)
        pg.fill("#cm-key", "del_key")
        pg.click("#cm-delete")
        pg.wait_for_timeout(200)
        display = pg.text_content("#cm-display")
        assert "del_key" not in display

    def test_multiple_cookies(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/cookie-mgmt")
        for k, v in [("a", "1"), ("b", "2")]:
            pg.fill("#cm-key", k)
            pg.fill("#cm-value", v)
            pg.click("#cm-set")
            pg.wait_for_timeout(100)
        display = pg.text_content("#cm-display")
        assert "a=1" in display
        assert "b=2" in display


# ===================================================================
# M5: Navigation guard
# ===================================================================

class TestM5_NavGuard:
    def test_clean_state(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/nav-guard")
        dirty = pg.text_content("#ng-dirty")
        assert dirty == "clean"

    def test_input_marks_dirty(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/nav-guard")
        pg.fill("#ng-input", "something")
        dirty = pg.text_content("#ng-dirty")
        assert dirty == "dirty"

    def test_beforeunload_fires_when_dirty(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/nav-guard")
        pg.fill("#ng-input", "data")
        has_listener = pg.evaluate("""
          (() => {
            let fired = false;
            const e = new Event('beforeunload', {cancelable: true});
            window.dispatchEvent(e);
            return e.defaultPrevented;
          })()
        """)
        assert has_listener is True


# ===================================================================
# M6: Undo/Redo
# ===================================================================

class TestM6_UndoRedo:
    @staticmethod
    def _push_value(pg, value: str):
        """Programmatically push a value into the undo/redo stack."""
        pg.evaluate(f"""(() => {{
            var inp = document.getElementById('ur-input');
            inp.value = {repr(value)};
            undoStack = undoStack.slice(0, undoPos + 1);
            undoStack.push(inp.value);
            undoPos = undoStack.length - 1;
            updateUndoInfo();
        }})()""")
        pg.wait_for_timeout(50)

    def test_push_builds_history(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/undo-redo")
        self._push_value(pg, "first")
        self._push_value(pg, "second")
        history_len = int(pg.text_content("#ur-history-len"))
        assert history_len == 3

    def test_undo_restores_previous(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/undo-redo")
        self._push_value(pg, "alpha")
        self._push_value(pg, "beta")
        pg.click("#ur-undo")
        pg.wait_for_timeout(100)
        val = pg.input_value("#ur-input")
        assert val == "alpha"

    def test_redo_reapplies(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/undo-redo")
        self._push_value(pg, "one")
        self._push_value(pg, "two")
        pg.click("#ur-undo")
        pg.wait_for_timeout(100)
        pg.click("#ur-redo")
        pg.wait_for_timeout(100)
        val = pg.input_value("#ur-input")
        assert val == "two"

    def test_undo_to_empty(self, page):
        pg, infra = page
        pg.goto(f"{infra['alpha_url']}/undo-redo")
        self._push_value(pg, "temp")
        pg.click("#ur-undo")
        pg.wait_for_timeout(100)
        val = pg.input_value("#ur-input")
        assert val == ""
        pos = int(pg.text_content("#ur-pos"))
        assert pos == 0

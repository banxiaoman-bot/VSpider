"""K6: source-pin tests for the failed-run detail dialog wiring.

Like ``test_keyboard_shortcuts.py``, these tests don't run JS — they
read the component source and assert the SHAPE of the dialog code so
that accidental regressions are caught at CI time. The protections we
want:

  * Row-click on the 失败记录 table dispatches to ``openFailedRunDetail``
  * The dialog's ``v-model`` is bound to ``failedRunDialogVisible``
  * ``fetchFailedRunPhaseEvents`` hits the K6 backend route exactly
  * Prev/next navigation uses ``failedRunsList`` (newest-first list) and
    is wired to keyboard ←/→ inside the dialog AND to the footer buttons
  * The HTML-log button uses ``@click.stop`` so it doesn't bubble up to
    the row-click handler (otherwise clicking the button would open
    BOTH the new tab AND the dialog)

The dialog was extracted from App.vue into FailedRunsPane.vue during the
Y126 component split. The keyboard ←/→ dispatcher remains in App.vue
and delegates via ``failedRunsPaneRef.value?.goToPrev/NextFailedRun()``.

Run: ``pytest tests/test_failed_run_detail_dialog.py -q``
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_UI_SRC = Path(__file__).resolve().parent.parent / "vspider-ui" / "src"
APP_VUE = _UI_SRC / "App.vue"
FAILED_RUNS_PANE = _UI_SRC / "components" / "FailedRunsPane.vue"


@pytest.fixture(scope="module")
def src() -> str:
    return FAILED_RUNS_PANE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_src() -> str:
    return APP_VUE.read_text(encoding="utf-8")


# ── Reactive state ────────────────────────────────────────────────────


class TestDialogState:
    def test_selected_failed_run_ref_defined(self, src: str) -> None:
        assert "const selectedFailedRun = ref(null)" in src

    def test_dialog_visibility_ref_defined(self, src: str) -> None:
        assert "const failedRunDialogVisible = ref(false)" in src

    def test_phase_events_state_refs_defined(self, src: str) -> None:
        # Three refs that drive the preview pane: events / status / total.
        assert "const failedRunPhaseEvents = ref([])" in src
        assert "const failedRunPhaseStatus = ref('idle')" in src
        assert "const failedRunPhaseTotal = ref(0)" in src
        assert "const failedRunPhaseTruncated = ref(false)" in src


# ── Helpers ───────────────────────────────────────────────────────────


class TestDialogHelpers:
    def test_pretty_print_uses_two_space_indent(self, src: str) -> None:
        """Match the on-disk archive format so a copy from the dialog is
        a drop-in replacement for ``cat runs/failed/<id>.json``."""
        m = re.search(
            r"const selectedFailedRunJson\s*=\s*computed\(.*?"
            r"JSON\.stringify\(\s*selectedFailedRun\.value,\s*null,\s*2\s*\)",
            src, flags=re.S,
        )
        assert m, "selectedFailedRunJson must be a computed using JSON.stringify(.., null, 2)"

    def test_open_helper_seeds_state_and_fetches(self, src: str) -> None:
        m = re.search(
            r"const openFailedRunDetail\s*=\s*\(rec\)\s*=>\s*\{(.*?)\n\}\n",
            src, flags=re.S,
        )
        assert m, "openFailedRunDetail must be defined"
        body = m.group(1)
        # All three side-effects: set selected, open dialog, fetch events.
        assert "selectedFailedRun.value = rec" in body
        assert "failedRunDialogVisible.value = true" in body
        assert "fetchFailedRunPhaseEvents(rec)" in body

    def test_fetch_helper_targets_correct_endpoint(self, src: str) -> None:
        # Must hit the exact K6 endpoint; encodeURIComponent on the
        # run_id prevents accidental path-traversal injection from
        # weird records with whitespace / slashes.
        m = re.search(
            r"`\$\{API_BASE\}/api/failed_runs/\$\{encodeURIComponent\(rec\.run_id\)\}/phase_events\?limit=200`",
            src,
        )
        assert m, "fetchFailedRunPhaseEvents must use the K6 endpoint with encoded run_id"

    def test_fetch_helper_handles_missing_log(self, src: str) -> None:
        """When ``paths_exist.phase_jsonl === false`` we should skip the
        request entirely (no point hitting the backend just to 404)."""
        m = re.search(
            r"const fetchFailedRunPhaseEvents.*?paths_exist.*?phase_jsonl.*?"
            r"failedRunPhaseStatus\.value\s*=\s*'missing'",
            src, flags=re.S,
        )
        assert m, "fetch helper must short-circuit when paths_exist.phase_jsonl is false"

    def test_fetch_helper_distinguishes_404_from_other_errors(self, src: str) -> None:
        """404 → 'missing' (friendly); other failures → 'error' (red banner)."""
        m1 = re.search(
            r"response\.status\s*===\s*404.*?failedRunPhaseStatus\.value\s*=\s*'missing'",
            src, flags=re.S,
        )
        m2 = re.search(
            r"catch\s*\(err\).*?failedRunPhaseStatus\.value\s*=\s*'error'",
            src, flags=re.S,
        )
        assert m1, "404 path must set status='missing'"
        assert m2, "catch path must set status='error'"


# ── Navigation ────────────────────────────────────────────────────────


class TestDialogNavigation:
    def test_prev_uses_failed_runs_list(self, src: str) -> None:
        m = re.search(
            r"const goToPrevFailedRun\s*=.*?\n}\n",
            src, flags=re.S,
        )
        assert m and "failedRunsList.value" in m.group(0)

    def test_next_uses_failed_runs_list(self, src: str) -> None:
        m = re.search(
            r"const goToNextFailedRun\s*=.*?\n}\n",
            src, flags=re.S,
        )
        assert m and "failedRunsList.value" in m.group(0)

    def test_prev_wraps_to_end(self, src: str) -> None:
        """idx <= 0 should wrap to ``list.length - 1`` so ← from the
        first row lands on the oldest entry."""
        m = re.search(
            r"const goToPrevFailedRun.*?idx\s*<=\s*0\s*\?\s*list\.length\s*-\s*1",
            src, flags=re.S,
        )
        assert m, "goToPrevFailedRun must wrap idx<=0 to list.length-1"

    def test_next_wraps_to_start(self, src: str) -> None:
        m = re.search(
            r"const goToNextFailedRun.*?idx\s*>=\s*list\.length\s*-\s*1\s*\?\s*0",
            src, flags=re.S,
        )
        assert m, "goToNextFailedRun must wrap idx>=length-1 to 0"

    def test_keyboard_dispatcher_routes_arrows_when_dialog_open(
        self, app_src: str,
    ) -> None:
        m_left = re.search(
            r"failedRun(?:sPaneRef\.value\?\.failedRun)?DialogVisible.*?"
            r"event\.key\s*===\s*'ArrowLeft'.*?goToPrevFailedRun\(\)",
            app_src, flags=re.S,
        )
        m_right = re.search(
            r"failedRun(?:sPaneRef\.value\?\.failedRun)?DialogVisible.*?"
            r"event\.key\s*===\s*'ArrowRight'.*?goToNextFailedRun\(\)",
            app_src, flags=re.S,
        )
        assert m_left, "← must call goToPrevFailedRun when dialog is open"
        assert m_right, "→ must call goToNextFailedRun when dialog is open"


# ── Template wiring ───────────────────────────────────────────────────


class TestTableRowClickWiring:
    def test_row_click_handler_bound(self, src: str) -> None:
        """el-table @row-click must dispatch to openFailedRunDetail.
        Without this the whole row becomes a clickable affordance —
        critical for the K6 UX."""
        m = re.search(
            r'<el-table[^>]*?@row-click="openFailedRunDetail"',
            src, flags=re.S,
        )
        assert m, "失败记录 el-table must bind @row-click=\"openFailedRunDetail\""

    def test_row_click_class_present(self, src: str) -> None:
        """Cursor styling depends on the .failed-runs-clickable class."""
        m = re.search(r'class="[^"]*failed-runs-clickable', src)
        assert m, "el-table must carry the failed-runs-clickable class"

    def test_html_log_button_uses_click_stop(self, src: str) -> None:
        """Without ``.stop`` the button click bubbles to the row, opening
        BOTH the new tab AND the detail dialog (jarring UX)."""
        m = re.search(
            r'@click\.stop="openFailedRunLog\(scope\.row\)"',
            src,
        )
        assert m, "HTML-log button must use @click.stop"


class TestDialogTemplate:
    def test_dialog_v_model_bound(self, src: str) -> None:
        m = re.search(
            r'<el-dialog[^>]*?v-model="failedRunDialogVisible"',
            src, flags=re.S,
        )
        assert m

    def test_dialog_has_destroy_on_close(self, src: str) -> None:
        """destroy-on-close keeps the DOM tree light when the dialog
        isn't open and forces re-fetch on the next open."""
        # Find the failed-run dialog block and confirm destroy-on-close
        # is one of its props.
        m = re.search(
            r'<el-dialog[^>]*?v-model="failedRunDialogVisible"[\s\S]*?>',
            src,
        )
        assert m and "destroy-on-close" in m.group(0)

    def test_meta_section_renders_run_id(self, src: str) -> None:
        # The structured meta section must surface the run_id explicitly
        # so the user can copy it without scrolling to the JSON dump.
        assert (
            "selectedFailedRun.run_id" in src
            and "Run ID" in src
        )

    def test_phase_preview_renders_severity_class(self, src: str) -> None:
        m = re.search(
            r'`preview-sev-\$\{formatPhasePreviewSeverity\(evt\.severity\)\}`',
            src,
        )
        assert m, "phase preview <li> must apply preview-sev-<level> class"

    def test_dialog_footer_has_nav_buttons(self, src: str) -> None:
        # Both prev and next buttons present
        assert '@click="goToPrevFailedRun"' in src
        assert '@click="goToNextFailedRun"' in src

    def test_dialog_footer_has_html_log_button(self, src: str) -> None:
        m = re.search(
            r'@click="openFailedRunLog\(selectedFailedRun\)"',
            src,
        )
        assert m, "Footer must offer 'HTML 日志' jump too"

    def test_dialog_footer_has_copy_button(self, src: str) -> None:
        m = re.search(
            r'@click="copyFailedRunJson"',
            src,
        )
        assert m

    def test_nav_buttons_disabled_with_singleton_list(self, src: str) -> None:
        """When there's only one failure, prev/next are pointless."""
        m = re.findall(
            r':disabled="failedRunsList\.length\s*<\s*2"',
            src,
        )
        assert len(m) >= 2, (
            "Both prev and next failed-run nav buttons must be "
            "disabled when failedRunsList.length < 2"
        )

    def test_html_log_button_disabled_when_log_missing(self, src: str) -> None:
        """If paths_exist.html_log is false, the button must be greyed
        out — the route would 404 anyway."""
        m = re.search(
            r':disabled="!selectedFailedRun[\s\S]*?paths_exist[\s\S]*?html_log\s*===\s*false',
            src,
        )
        assert m, "Footer HTML log button must check paths_exist.html_log"

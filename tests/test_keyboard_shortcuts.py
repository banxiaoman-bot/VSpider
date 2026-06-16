"""T: source-pin tests for the App.vue keyboard shortcut dispatcher.

We don't have a JS test runner in this repo, so the next-best protection
against accidental regressions is to assert the SHAPE of the handler
file: each shortcut must dispatch to the function we expect, and the
listener must be registered+unregistered symmetrically.

These tests fail loudly if someone:
  • Reuses Ctrl+K for a different action
  • Drops the addEventListener / removeEventListener pair on lifecycle
  • Removes the help dialog or its ``ref="promptInputRef"`` binding
  • Renames TAB_ORDER away from the tabs the UI currently has

Run: ``pytest tests/test_keyboard_shortcuts.py -q``
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


APP_VUE = (
    Path(__file__).resolve().parent.parent
    / "vspider-ui" / "src" / "App.vue"
)


@pytest.fixture(scope="module")
def src() -> str:
    return APP_VUE.read_text(encoding="utf-8")


# ── Dispatcher lifecycle ──────────────────────────────────────────────


class TestDispatcherLifecycle:
    def test_handler_function_defined(self, src: str) -> None:
        assert "const handleGlobalKeydown = (event) =>" in src

    def test_listener_added_on_mount(self, src: str) -> None:
        assert "window.addEventListener('keydown', handleGlobalKeydown)" in src

    def test_listener_removed_on_unmount(self, src: str) -> None:
        assert "window.removeEventListener('keydown', handleGlobalKeydown)" in src

    def test_uses_ctrl_or_meta_modifier(self, src: str) -> None:
        """macOS Cmd key + Windows/Linux Ctrl key must both trigger primary
        shortcuts. If someone drops the metaKey OR, macOS users silently
        lose all bindings."""
        assert "event.ctrlKey || event.metaKey" in src

    def test_typing_target_guard_present(self, src: str) -> None:
        """Global shortcuts (except Ctrl+Enter) must be suppressed when
        focus is inside an input/textarea/contenteditable element."""
        assert "_isTypingTarget" in src
        assert re.search(r"if\s*\(\s*typing\s*\)\s*return", src)


# ── Individual shortcut wiring ────────────────────────────────────────


class TestGlobalShortcuts:
    def test_ctrl_enter_calls_submit(self, src: str) -> None:
        # Anchor on the Enter-key branch and confirm it calls submitTask.
        m = re.search(
            r"event\.key\s*===\s*'Enter'.*?submitTask\(\)",
            src, flags=re.S,
        )
        assert m, "Ctrl+Enter must dispatch to submitTask()"

    def test_ctrl_enter_respects_isrunning(self, src: str) -> None:
        """Spam-pressing Ctrl+Enter during a run must not re-submit. The
        guard is the same one the UI button uses."""
        m = re.search(
            r"event\.key\s*===\s*'Enter'.*?!\s*isRunning\.value.*?submitTask",
            src, flags=re.S,
        )
        assert m, "Ctrl+Enter must check !isRunning.value before submitting"

    def test_ctrl_k_focuses_prompt(self, src: str) -> None:
        m = re.search(
            r"event\.key\s*===\s*'k'\s*\|\|\s*event\.key\s*===\s*'K'.*?focusPromptInput\(\)",
            src, flags=re.S,
        )
        assert m, "Ctrl+K must call focusPromptInput()"

    def test_ctrl_slash_toggles_help(self, src: str) -> None:
        m = re.search(
            r"event\.key\s*===\s*'/'.*?helpDialogVisible\.value\s*=\s*!helpDialogVisible\.value",
            src, flags=re.S,
        )
        assert m, "Ctrl+/ must toggle helpDialogVisible"

    def test_ctrl_digits_switch_tabs(self, src: str) -> None:
        m = re.search(
            r"event\.key\s*>=\s*'1'\s*&&\s*event\.key\s*<=\s*'9'.*?"
            r"setActiveBottomTab\(TAB_ORDER\[i\]\)",
            src, flags=re.S,
        )
        assert m, "Ctrl+1..9 must dispatch to setActiveBottomTab(TAB_ORDER[i])"


class TestTabOrderInvariants:
    def test_tab_order_has_six_tabs(self, src: str) -> None:
        """TAB_ORDER must list the 6 tabs the UI has today, in the same
        order they appear in the DOM. If a future tab is added (or one
        removed), this test forces a deliberate update."""
        m = re.search(
            r"const TAB_ORDER\s*=\s*\[(.*?)\]",
            src, flags=re.S,
        )
        assert m, "TAB_ORDER constant must exist"
        items = [
            s.strip().strip("'\"")
            for s in m.group(1).split(",")
            if s.strip()
        ]
        assert items == [
            "terminal", "timeline", "capability", "final", "artifacts", "runs",
        ], f"unexpected TAB_ORDER contents: {items!r}"


class TestTimelineShortcuts:
    def test_ctrl_e_exports_jsonl(self, src: str) -> None:
        m = re.search(
            r"activeBottomTab\.value\s*===\s*'timeline'.*?"
            r"event\.key\s*===\s*'e'\s*\|\|\s*event\.key\s*===\s*'E'.*?"
            r"exportPhaseEventsAsJsonl\(\)",
            src, flags=re.S,
        )
        assert m, "Ctrl+E (in Timeline) must call exportPhaseEventsAsJsonl()"

    def test_end_scrolls_to_bottom(self, src: str) -> None:
        m = re.search(
            r"event\.key\s*===\s*'End'.*?scroll(?:TimelineTo|To)Bottom\(\)",
            src, flags=re.S,
        )
        assert m

    def test_home_scrolls_to_top(self, src: str) -> None:
        m = re.search(
            r"event\.key\s*===\s*'Home'.*?scroll(?:TimelineTo|To)Top\(\)",
            src, flags=re.S,
        )
        assert m


class TestCapabilityShortcuts:
    def test_ctrl_e_exports_capability_trace_jsonl(self, src: str) -> None:
        m = re.search(
            r"activeBottomTab\.value\s*===\s*'capability'.*?"
            r"event\.key\s*===\s*'e'\s*\|\|\s*event\.key\s*===\s*'E'.*?"
            r"exportCapabilityTraceAsJsonl\(\)",
            src, flags=re.S,
        )
        assert m, "Ctrl+E (in Capability) must call exportCapabilityTraceAsJsonl()"


class TestDialogNavigation:
    @pytest.fixture(scope="class")
    def combined_src(self) -> str:
        app = APP_VUE.read_text(encoding="utf-8")
        timeline = (
            APP_VUE.parent / "components" / "TimelinePanel.vue"
        ).read_text(encoding="utf-8")
        return app + "\n" + timeline

    def test_arrow_left_calls_prev(self, combined_src: str) -> None:
        m = re.search(
            r"phaseDialogVisible.*?"
            r"event\.key\s*===\s*'ArrowLeft'.*?goToPrevPhaseEvent\(\)",
            combined_src, flags=re.S,
        )
        assert m

    def test_arrow_right_calls_next(self, combined_src: str) -> None:
        m = re.search(
            r"phaseDialogVisible.*?"
            r"event\.key\s*===\s*'ArrowRight'.*?goToNextPhaseEvent\(\)",
            combined_src, flags=re.S,
        )
        assert m

    def test_prev_next_use_filtered_list(self, combined_src: str) -> None:
        """←/→ should respect the user's phase/severity filter (O) —
        otherwise hiding 'info' chips and pressing → would jump straight
        back into hidden events. We assert by reading the source of the
        prev/next helpers."""
        assert "filteredPhaseEvents.value" in combined_src
        prev_block = re.search(
            r"const goToPrevPhaseEvent\s*=.*?\n}\n", combined_src, flags=re.S,
        )
        next_block = re.search(
            r"const goToNextPhaseEvent\s*=.*?\n}\n", combined_src, flags=re.S,
        )
        assert prev_block and "filteredPhaseEvents" in prev_block.group(0)
        assert next_block and "filteredPhaseEvents" in next_block.group(0)


# ── Template wiring ───────────────────────────────────────────────────


class TestPromptInputRef:
    def test_prompt_textarea_bound_to_ref(self, src: str) -> None:
        """Ctrl+K calls focusPromptInput → that helper needs the el-input
        instance via :ref. If the ref binding is dropped, focus silently
        no-ops."""
        m = re.search(
            r'<el-input\s+ref="promptInputRef"\s+v-model="prompt"',
            src,
        )
        assert m, "prompt el-input must have ref=\"promptInputRef\""


class TestHelpDialog:
    """Cheat-sheet was extracted to components/dialogs/ShortcutHelpDialog.vue
    (Y126 component split); read App.vue + the dialog component together."""

    @pytest.fixture(scope="class")
    def src(self) -> str:
        app = APP_VUE.read_text(encoding="utf-8")
        dialog = (
            APP_VUE.parent / "components" / "dialogs" / "ShortcutHelpDialog.vue"
        ).read_text(encoding="utf-8")
        return app + "\n" + dialog

    def test_dialog_template_present(self, src: str) -> None:
        assert 'v-model:visible="helpDialogVisible"' in src
        assert 'class="shortcut-dialog"' in src

    def test_lists_all_four_global_shortcuts(self, src: str) -> None:
        """The cheat-sheet must document all 4 global keys. If a binding
        is added but the help dialog isn't updated, users won't discover
        it."""
        # Heuristic: each label kbd appears at least once in a row.
        labels = ["Enter", "K", "/", "1</kbd>"]
        for label in labels:
            assert label in src, f"shortcut help dialog missing reference to {label!r}"

    def test_lists_timeline_shortcuts(self, src: str) -> None:
        # E + End + Home rows
        for label in ["<kbd>E</kbd>", "<kbd>End</kbd>", "<kbd>Home</kbd>"]:
            assert label in src, f"help dialog missing {label}"

    def test_lists_arrow_keys(self, src: str) -> None:
        assert "<kbd>←</kbd>" in src
        assert "<kbd>→</kbd>" in src


class TestPhaseDialogNavButtons:
    @pytest.fixture(scope="class")
    def timeline_src(self) -> str:
        return (
            APP_VUE.parent / "components" / "TimelinePanel.vue"
        ).read_text(encoding="utf-8")

    def test_prev_button_present(self, timeline_src: str) -> None:
        m = re.search(
            r'@click="goToPrevPhaseEvent"',
            timeline_src,
        )
        assert m, "phase dialog footer must include the prev button"

    def test_next_button_present(self, timeline_src: str) -> None:
        m = re.search(
            r'@click="goToNextPhaseEvent"',
            timeline_src,
        )
        assert m, "phase dialog footer must include the next button"

    def test_nav_buttons_disabled_when_single_event(self, timeline_src: str) -> None:
        """When the filtered list has <2 events there's nothing to step
        through; the buttons must visually communicate that."""
        m = re.findall(
            r':disabled="filteredPhaseEvents\.length\s*<\s*2"',
            timeline_src,
        )
        assert len(m) >= 2, (
            "expected both prev/next nav buttons to be :disabled when "
            "filteredPhaseEvents.length < 2"
        )

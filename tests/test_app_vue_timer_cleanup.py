"""ASYNC-LEAK-1 regression: every pending timer must be torn down on unmount.

All timer teardowns have been progressively extracted from App.vue into composables:
- D-UI-25: outputContractPreviewTimer → useOutputContractPreview.js
- D-UI-28: disconnectWebSocket → useRunStream.js → useWebSocket.js
- D-UI-29: finalAnswerCopyTimer → useCopyFeedback.js; onMounted/onUnmounted → useAppBootstrap.js

``reconnectTimer`` (useWebSocket.js), ``_chipClickTimer`` (TimelinePanel.vue) still
self-clean in their respective files.
Structural test: assert App.vue wires the composables and composables own cleanup.
"""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
APP_VUE = ROOT / "vspider-ui" / "src" / "App.vue"
TIMELINE_PANEL = ROOT / "vspider-ui" / "src" / "components" / "TimelinePanel.vue"
USE_WEBSOCKET = ROOT / "vspider-ui" / "src" / "composables" / "useWebSocket.js"
USE_OUTPUT_CONTRACT_PREVIEW = (
    ROOT / "vspider-ui" / "src" / "composables" / "useOutputContractPreview.js"
)
USE_COPY_FEEDBACK = (
    ROOT / "vspider-ui" / "src" / "composables" / "useCopyFeedback.js"
)
USE_APP_BOOTSTRAP = (
    ROOT / "vspider-ui" / "src" / "composables" / "useAppBootstrap.js"
)


@pytest.fixture(scope="module")
def app_src() -> str:
    return APP_VUE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def timeline_src() -> str:
    return TIMELINE_PANEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ws_src() -> str:
    return USE_WEBSOCKET.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def output_contract_preview_src() -> str:
    return USE_OUTPUT_CONTRACT_PREVIEW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def copy_feedback_src() -> str:
    return USE_COPY_FEEDBACK.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_bootstrap_src() -> str:
    return USE_APP_BOOTSTRAP.read_text(encoding="utf-8")


def test_onunmounted_clears_output_contract_preview_timer(
    app_src: str, output_contract_preview_src: str
) -> None:
    assert "useOutputContractPreview(" in app_src, (
        "App.vue must wire useOutputContractPreview so the preview debounce is "
        "owned (and torn down) by the composable"
    )
    assert "onUnmounted(" in output_contract_preview_src
    assert "timer" in output_contract_preview_src, (
        "useOutputContractPreview must clear its pending timer "
        "on unmount so its callback can't fire after unmount"
    )


def test_onunmounted_still_clears_known_timers(
    app_src: str, ws_src: str, timeline_src: str, app_bootstrap_src: str
) -> None:
    assert "useAppBootstrap(" in app_src, (
        "App.vue must wire useAppBootstrap which owns onMounted/onUnmounted"
    )
    assert "onUnmounted(" in app_bootstrap_src
    assert "disconnectWebSocket" in app_bootstrap_src
    assert "reconnectTimer" in ws_src
    assert "socket" in ws_src
    assert "_chipClickTimer" in timeline_src, (
        "_chipClickTimer cleanup must exist in TimelinePanel.vue "
        "(moved from App.vue during component extraction)"
    )


def test_onunmounted_clears_final_answer_copy_timer(
    app_src: str, copy_feedback_src: str
) -> None:
    assert "useCopyFeedback(" in app_src, (
        "App.vue must wire useCopyFeedback so the copy-feedback timer is "
        "owned (and torn down) by the composable"
    )
    assert "onUnmounted(" in copy_feedback_src
    assert "timer" in copy_feedback_src, (
        "useCopyFeedback must clear its pending timer on unmount so "
        "the reset callback can't fire after the component is gone"
    )

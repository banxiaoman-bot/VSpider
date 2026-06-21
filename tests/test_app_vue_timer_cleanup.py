"""ASYNC-LEAK-1 regression: App.vue onUnmounted must clear every pending timer.

The component schedules an ``outputContractPreviewTimer`` debounce (it is only
``clearTimeout``-ed inside the debounce itself before rescheduling). On unmount
the last scheduled timer stays pending, so its callback fires after the
component is gone -- issuing a fetch and writing refs on a detached component.

``reconnectTimer`` and ``_chipClickTimer`` are already torn down in
``onUnmounted``; this pins that ``outputContractPreviewTimer`` joins them.
Structural test (matches tests/test_frontend_component_split_y126.py style):
no JS runner, just assert the source teardown is present.
"""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
APP_VUE = ROOT / "vspider-ui" / "src" / "App.vue"
TIMELINE_PANEL = ROOT / "vspider-ui" / "src" / "components" / "TimelinePanel.vue"
USE_WEBSOCKET = ROOT / "vspider-ui" / "src" / "composables" / "useWebSocket.js"


@pytest.fixture(scope="module")
def app_src() -> str:
    return APP_VUE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def timeline_src() -> str:
    return TIMELINE_PANEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ws_src() -> str:
    return USE_WEBSOCKET.read_text(encoding="utf-8")


def _onunmounted_block(src: str) -> str:
    start = src.find("onUnmounted(() => {")
    assert start != -1, "onUnmounted handler not found"
    # The keydown listener teardown moved into composables/useKeyboardCommand.js
    # (it owns its own onUnmounted now), so App.vue's onUnmounted no longer ends
    # with removeEventListener('keydown', ...). Delimit on the arrow-function
    # close instead.
    end = src.find("\n})", start)
    assert end != -1, "onUnmounted end anchor not found"
    return src[start:end]


def test_onunmounted_clears_output_contract_preview_timer(app_src: str) -> None:
    block = _onunmounted_block(app_src)
    assert "outputContractPreviewTimer" in block, (
        "onUnmounted must clear the pending outputContractPreviewTimer debounce "
        "so its callback can't fire (fetch + ref writes) after unmount"
    )


def test_onunmounted_still_clears_known_timers(
    app_src: str, ws_src: str, timeline_src: str
) -> None:
    block = _onunmounted_block(app_src)
    # WebSocket teardown (reconnect timer cancel + socket close) was extracted
    # into composables/useWebSocket.js; App.vue's onUnmounted now delegates via
    # disconnectWebSocket() instead of inlining the reconnectTimer/socket cleanup.
    assert "disconnectWebSocket()" in block
    assert "reconnectTimer" in ws_src
    assert "socket" in ws_src
    assert "_chipClickTimer" in timeline_src, (
        "_chipClickTimer cleanup must exist in TimelinePanel.vue "
        "(moved from App.vue during component extraction)"
    )


def test_onunmounted_clears_final_answer_copy_timer(app_src: str) -> None:
    block = _onunmounted_block(app_src)
    assert "finalAnswerCopyTimer" in block, (
        "onUnmounted must clear finalAnswerCopyTimer so the copy-feedback reset "
        "can't fire after the component is gone"
    )

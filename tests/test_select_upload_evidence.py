"""Regression tests for Slice S1/S2 — evidence chains on select & upload.

S1 ``SelectHandler``:
  - no more silent ``index=0`` fallback (would mis-pick the first option)
  - readback evidence: the selected option's text/value must match the
    requested ``type_value``, otherwise the action fails loudly
  - rpa_trail entry carries method / verified / observed fields

S2 ``UploadHandler``:
  - page_echo evidence: ``input.files`` is read back after
    ``set_input_files`` and must contain the injected file name
  - rpa_trail entry carries uploaded_file / page_echo / verified fields
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from visual_web_agent.actions import ActionContext, SelectHandler, UploadHandler
from visual_web_agent.vlm_client import VSpiderAction


# ════════════════════════════════════════════════════════════════════
#                       STUBS
# ════════════════════════════════════════════════════════════════════


class _StubSelectHandle:
    """Stub of an ElementHandle backing a <select> element."""

    def __init__(
        self,
        *,
        fail_label: bool = False,
        fail_value: bool = False,
        readback: dict | None = None,
    ) -> None:
        self.fail_label = fail_label
        self.fail_value = fail_value
        self.readback = readback
        self.calls: list[str] = []

    async def select_option(self, label=None, value=None, index=None, timeout=None):
        if index is not None:
            raise AssertionError(
                "silent index=0 fallback must not be used (Slice S1)"
            )
        if label is not None:
            self.calls.append("label")
            if self.fail_label:
                raise RuntimeError("no option with this label")
            return [label]
        if value is not None:
            self.calls.append("value")
            if self.fail_value:
                raise RuntimeError("no option with this value")
            return [value]
        raise AssertionError("select_option called without label/value")

    async def evaluate(self, js, *args):
        assert "selectedOptions" in js
        return self.readback


class _StubFileInputHandle:
    """Stub of the <input type=file> element handle."""

    def __init__(self, echo: list | None) -> None:
        self.echo = echo
        self.injected: str | None = None

    async def set_input_files(self, path):
        self.injected = str(path)

    async def evaluate(self, js, *args):
        assert "files" in js
        return self.echo


class _StubBrowser:
    _LOCATOR_TIMEOUT = 500

    def __init__(self, handle, *, upload_file=None) -> None:
        self.rpa_trail: list = []
        self._last_action_error = None
        self._handle = handle
        self._upload_file = upload_file

    async def _clear_som_overlays(self):
        pass

    async def _resolve_action_target(self, target_id, kind):
        return SimpleNamespace(handle=self._handle)

    async def _find_file_input(self, target_id):
        return SimpleNamespace(handle=self._handle)

    async def _get_xpath(self, handle):
        return "/html/body/x"

    async def _get_accessibility_signature(self, page, handle):
        return ("combobox", "City")

    async def _wait_after_action(self, **kwargs):
        pass


def _make_ctx(action_name: str, browser, *, type_value: str = "") -> ActionContext:
    action = VSpiderAction(
        action=action_name,
        target_id=7,
        type_value=type_value,
        memory_key="",
    )
    return ActionContext(
        action=action,
        browser=browser,
        workflow_memory={},
        page=SimpleNamespace(),
    )


# ════════════════════════════════════════════════════════════════════
#                       S1 — SELECT READBACK
# ════════════════════════════════════════════════════════════════════


class TestSelectReadback:
    def test_label_match_records_verified_trail(self) -> None:
        handle = _StubSelectHandle(
            readback={"text": "Beijing", "value": "bj", "index": 2}
        )
        browser = _StubBrowser(handle)
        ctx = _make_ctx("select", browser, type_value="Beijing")
        asyncio.run(SelectHandler().execute(ctx))
        assert browser._last_action_error is None
        assert browser.rpa_trail, "verified select must land on rpa_trail"
        entry = browser.rpa_trail[-1]
        assert entry["action"] == "select"
        assert entry["method"] == "label"
        assert entry["verified"] is True
        assert entry["observed"] == "Beijing"

    def test_value_fallback_still_verified(self) -> None:
        handle = _StubSelectHandle(
            fail_label=True,
            readback={"text": "北京市", "value": "Beijing", "index": 1},
        )
        browser = _StubBrowser(handle)
        ctx = _make_ctx("select", browser, type_value="Beijing")
        asyncio.run(SelectHandler().execute(ctx))
        assert browser._last_action_error is None
        assert handle.calls == ["label", "value"]
        assert browser.rpa_trail[-1]["method"] == "value"

    def test_no_silent_index_zero_fallback(self) -> None:
        """Both label and value misses must fail loudly, never pick option #0."""
        handle = _StubSelectHandle(fail_label=True, fail_value=True)
        browser = _StubBrowser(handle)
        ctx = _make_ctx("select", browser, type_value="Nowhere")
        asyncio.run(SelectHandler().execute(ctx))
        assert browser._last_action_error is not None
        assert not browser.rpa_trail

    def test_readback_mismatch_fails_loudly(self) -> None:
        """select_option 'succeeding' is not enough — the bound value must echo."""
        handle = _StubSelectHandle(
            readback={"text": "Shanghai", "value": "sh", "index": 0}
        )
        browser = _StubBrowser(handle)
        ctx = _make_ctx("select", browser, type_value="Beijing")
        asyncio.run(SelectHandler().execute(ctx))
        assert browser._last_action_error is not None
        assert "readback mismatch" in str(browser._last_action_error)
        assert not browser.rpa_trail

    def test_non_select_element_skips_verification(self) -> None:
        """Custom widgets (evaluate -> null) cannot be verified; don't block."""
        handle = _StubSelectHandle(readback=None)
        browser = _StubBrowser(handle)
        ctx = _make_ctx("select", browser, type_value="Beijing")
        asyncio.run(SelectHandler().execute(ctx))
        assert browser._last_action_error is None
        assert browser.rpa_trail


# ════════════════════════════════════════════════════════════════════
#                       S2 — UPLOAD PAGE ECHO
# ════════════════════════════════════════════════════════════════════


class TestUploadPageEcho:
    def _make_file(self, tmp_path: Path) -> Path:
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF-fake")
        return f

    def test_echo_match_records_evidence_trail(self, tmp_path: Path) -> None:
        f = self._make_file(tmp_path)
        handle = _StubFileInputHandle(echo=[{"name": f.name, "size": 9}])
        browser = _StubBrowser(handle)
        ctx = _make_ctx("upload", browser, type_value=str(f))
        asyncio.run(UploadHandler().execute(ctx))
        assert browser._last_action_error is None
        assert handle.injected == str(f.resolve())
        entry = browser.rpa_trail[-1]
        assert entry["action"] == "upload"
        assert entry["uploaded_file"] == f.name
        assert entry["page_echo"] == [{"name": f.name, "size": 9}]
        assert entry["verified"] is True

    def test_empty_echo_fails_loudly(self, tmp_path: Path) -> None:
        """input.files staying empty == the file never landed; must not pass."""
        f = self._make_file(tmp_path)
        handle = _StubFileInputHandle(echo=[])
        browser = _StubBrowser(handle)
        ctx = _make_ctx("upload", browser, type_value=str(f))
        asyncio.run(UploadHandler().execute(ctx))
        assert browser._last_action_error is not None
        assert "page_echo mismatch" in str(browser._last_action_error)
        assert not browser.rpa_trail

    def test_wrong_file_echo_fails_loudly(self, tmp_path: Path) -> None:
        f = self._make_file(tmp_path)
        handle = _StubFileInputHandle(echo=[{"name": "other.bin", "size": 1}])
        browser = _StubBrowser(handle)
        ctx = _make_ctx("upload", browser, type_value=str(f))
        asyncio.run(UploadHandler().execute(ctx))
        assert browser._last_action_error is not None
        assert not browser.rpa_trail

"""Regression tests for Slice S4/S5.

S4 ``html_snapshot`` / ``screenshot`` first-class actions:
  - schema acceptance + handler/tool registration
  - persists the page HTML / PNG through resolve_output_path and registers
    the artifact (manifest plumbing) with the right kind/mime
  - empty HTML / empty screenshot bytes fail loudly (no fake artifacts)
  - memory writeback + rpa_trail evidence (output_path / size / verified)

S5 ``type`` rich-text reuse:
  - contenteditable hosts route through the form_set editor adapters
    (no raw keyboard typing that desyncs Quill/TinyMCE/ProseMirror)
  - readback mismatch fails loudly
  - plain inputs keep the original keyboard path
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from visual_web_agent import snapshot_actions
from visual_web_agent.actions import (
    ActionContext,
    ActionExecutionError,
    ActionRegistry,
    TypeHandler,
)
from visual_web_agent.action_registry import build_default_action_registry
from visual_web_agent.prompt_skills import SKILL_PROMPTS, SNAPSHOT_SKILL
from visual_web_agent.snapshot_actions import (
    HtmlSnapshotHandler,
    ScreenshotCaptureHandler,
)
from visual_web_agent.vlm_client import VSpiderAction


# ════════════════════════════════════════════════════════════════════
#                       SCHEMA / REGISTRATION / PROMPTS
# ════════════════════════════════════════════════════════════════════


class TestWiring:
    def test_schema_accepts_snapshot_actions(self) -> None:
        for name in ("html_snapshot", "screenshot"):
            v = VSpiderAction(action=name, target_id=0, type_value="", memory_key="")
            assert v.action == name

    def test_handlers_registered(self) -> None:
        assert ActionRegistry._handlers["html_snapshot"] is HtmlSnapshotHandler
        assert ActionRegistry._handlers["screenshot"] is ScreenshotCaptureHandler

    def test_action_tools_registered(self) -> None:
        registry = build_default_action_registry()
        names = {tool["name"] for tool in registry.list_tools(include_disabled=True)}
        assert {"html_snapshot", "screenshot"} <= names

    def test_snapshot_skill_wired(self) -> None:
        assert SKILL_PROMPTS["snapshot"] is SNAPSHOT_SKILL
        assert "html_snapshot" in SNAPSHOT_SKILL
        assert "screenshot" in SNAPSHOT_SKILL
        assert "manifest" in SNAPSHOT_SKILL

    def test_capability_router_signal(self) -> None:
        from visual_web_agent.capability_router import _SNAPSHOT_RE

        for goal in ("把这个页面整页截图保存", "保存网页快照", "take a screenshot of the page"):
            assert _SNAPSHOT_RE.search(goal), goal
        assert not _SNAPSHOT_RE.search("提取表格前 10 行数据")


# ════════════════════════════════════════════════════════════════════
#                       S4 STUBS
# ════════════════════════════════════════════════════════════════════


class _StubPage:
    def __init__(self, *, html: str = "", png: bytes = b"", url: str = "https://example.com") -> None:
        self._html = html
        self._png = png
        self.url = url
        self.screenshot_kwargs: list[dict] = []

    async def content(self) -> str:
        return self._html

    async def screenshot(self, **kwargs) -> bytes:
        self.screenshot_kwargs.append(kwargs)
        return self._png


class _StubBrowser:
    def __init__(self) -> None:
        self.rpa_trail: list = []


@pytest.fixture()
def artifact_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []

    def _resolve(filename):
        return tmp_path / Path(filename).name

    def _register(path, **kwargs):
        calls.append({"path": str(path), **kwargs})

    monkeypatch.setattr(snapshot_actions, "resolve_output_path", _resolve)
    monkeypatch.setattr(snapshot_actions, "register_artifact", _register)
    return tmp_path, calls


def _make_ctx(action_name: str, page, *, type_value: str = "", memory_key: str = "") -> ActionContext:
    action = VSpiderAction(
        action=action_name,
        target_id=0,
        type_value=type_value,
        memory_key=memory_key,
    )
    return ActionContext(
        action=action,
        browser=_StubBrowser(),
        workflow_memory={},
        page=page,
    )


# ════════════════════════════════════════════════════════════════════
#                       S4 — HTML SNAPSHOT
# ════════════════════════════════════════════════════════════════════


class TestHtmlSnapshot:
    def test_persists_registers_and_records(self, artifact_dir) -> None:
        tmp, calls = artifact_dir
        ctx = _make_ctx("html_snapshot", _StubPage(html="<html><body>hi</body></html>"))
        asyncio.run(HtmlSnapshotHandler().execute(ctx))

        assert calls and calls[0]["kind"] == "html_snapshot"
        assert calls[0]["mime"] == "text/html"
        saved = Path(calls[0]["path"])
        assert saved.exists() and saved.suffix == ".html"
        assert "hi" in saved.read_text(encoding="utf-8")

        mem = ctx.workflow_memory["html_snapshot"]
        assert mem["path"] == str(saved)
        assert mem["size"] > 0
        assert mem["source_url"] == "https://example.com"

        entry = ctx.browser.rpa_trail[-1]
        assert entry["action"] == "html_snapshot"
        assert entry["output_kind"] == "html_snapshot"
        assert entry["output_path"] == str(saved)
        assert entry["verified"] is True

    def test_filename_hint_from_type_value(self, artifact_dir) -> None:
        tmp, calls = artifact_dir
        ctx = _make_ctx(
            "html_snapshot",
            _StubPage(html="<html>x</html>"),
            type_value="checkout_page",
            memory_key="snap",
        )
        asyncio.run(HtmlSnapshotHandler().execute(ctx))
        assert Path(calls[0]["path"]).name == "checkout_page.html"
        assert "snap" in ctx.workflow_memory

    def test_empty_html_fails_loudly(self, artifact_dir) -> None:
        tmp, calls = artifact_dir
        ctx = _make_ctx("html_snapshot", _StubPage(html="   "))
        with pytest.raises(ActionExecutionError):
            asyncio.run(HtmlSnapshotHandler().execute(ctx))
        assert not calls

    def test_missing_page_raises(self) -> None:
        ctx = _make_ctx("html_snapshot", None)
        with pytest.raises(ActionExecutionError):
            asyncio.run(HtmlSnapshotHandler().execute(ctx))


# ════════════════════════════════════════════════════════════════════
#                       S4 — SCREENSHOT
# ════════════════════════════════════════════════════════════════════


class TestScreenshotCapture:
    def test_viewport_default(self, artifact_dir) -> None:
        tmp, calls = artifact_dir
        page = _StubPage(png=b"\x89PNG-fake")
        ctx = _make_ctx("screenshot", page)
        asyncio.run(ScreenshotCaptureHandler().execute(ctx))

        assert page.screenshot_kwargs == [{"full_page": False}]
        assert calls[0]["kind"] == "screenshot"
        assert calls[0]["mime"] == "image/png"
        saved = Path(calls[0]["path"])
        assert saved.exists() and saved.suffix == ".png"

        mem = ctx.workflow_memory["screenshot"]
        assert mem["full_page"] is False
        entry = ctx.browser.rpa_trail[-1]
        assert entry["action"] == "screenshot"
        assert entry["verified"] is True

    def test_full_page_mode(self, artifact_dir) -> None:
        tmp, calls = artifact_dir
        page = _StubPage(png=b"\x89PNG-fake")
        ctx = _make_ctx("screenshot", page, type_value="full")
        asyncio.run(ScreenshotCaptureHandler().execute(ctx))
        assert page.screenshot_kwargs == [{"full_page": True}]
        assert ctx.workflow_memory["screenshot"]["full_page"] is True

    def test_empty_bytes_fail_loudly(self, artifact_dir) -> None:
        tmp, calls = artifact_dir
        ctx = _make_ctx("screenshot", _StubPage(png=b""))
        with pytest.raises(ActionExecutionError):
            asyncio.run(ScreenshotCaptureHandler().execute(ctx))
        assert not calls


# ════════════════════════════════════════════════════════════════════
#                       S5 — TYPE × RICH TEXT
# ════════════════════════════════════════════════════════════════════


class _TypeHandle:
    """Stub element handle driving TypeHandler's evaluate dispatch."""

    def __init__(self, *, contenteditable: bool, readback: str | None = None) -> None:
        self.contenteditable = contenteditable
        self.readback = readback
        self.richtext_written: list[str] = []
        self.clicked = False

    async def scroll_into_view_if_needed(self, timeout=None):
        pass

    async def click(self, **kwargs):
        self.clicked = True

    async def evaluate(self, js, *args):
        if "getBoundingClientRect" in js:
            return {"fully_visible": True, "partial_clip": 0, "height": 30}
        if "el.value != null" in js:
            return ""  # no-op precheck: field currently empty
        if "toParagraphHtml" in js:
            self.richtext_written.append(str(args[0]) if args else "")
            return "exec_insert_text"
        if "isContentEditable" in js:
            return self.contenteditable
        if "innerText || el.textContent" in js:
            if self.readback is not None:
                return self.readback
            return self.richtext_written[-1] if self.richtext_written else ""
        if "scrollIntoView" in js:
            return None
        return None


class _Keyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []
        self.typed: list[str] = []

    async def press(self, key):
        self.pressed.append(key)

    async def type(self, text, delay=None):
        self.typed.append(text)


class _TypePage:
    def __init__(self) -> None:
        self.keyboard = _Keyboard()
        self.url = "https://example.com"


class _TypeBrowser:
    _LOCATOR_TIMEOUT = 500

    def __init__(self, handle) -> None:
        self.rpa_trail: list = []
        self._last_action_error = None
        self._handle = handle

    async def _clear_som_overlays(self):
        pass

    async def _resolve_action_target(self, target_id, kind):
        return SimpleNamespace(handle=self._handle)

    async def _get_xpath(self, handle):
        return "/html/body/div"

    async def _get_accessibility_signature(self, page, handle):
        return ("textbox", "Editor")

    async def _count_interactive_elements(self):
        return 0

    async def _is_calendar_popup_visible(self):
        return False

    async def _wait_for_submenu(self, count_before, max_wait=2.5):
        return False

    async def _wait_after_action(self, **kwargs):
        pass

    def set_tab_notice(self, *args, **kwargs):
        pass


def _type_ctx(browser, page, text: str) -> ActionContext:
    action = VSpiderAction(action="type", target_id=3, type_value=text, memory_key="")
    return ActionContext(action=action, browser=browser, workflow_memory={}, page=page)


class TestTypeRichtext:
    def test_contenteditable_routes_through_editor_api(self) -> None:
        handle = _TypeHandle(contenteditable=True)
        browser = _TypeBrowser(handle)
        page = _TypePage()
        asyncio.run(TypeHandler().execute(_type_ctx(browser, page, "hello world")))

        assert browser._last_action_error is None
        assert handle.richtext_written == ["hello world"]
        # keyboard path must NOT run for richtext hosts
        assert page.keyboard.typed == []
        assert page.keyboard.pressed == []
        entry = browser.rpa_trail[-1]
        assert entry["action"] == "type"
        assert entry["method"] == "exec_insert_text"
        assert entry["verified"] is True

    def test_richtext_readback_mismatch_fails_loudly(self) -> None:
        handle = _TypeHandle(contenteditable=True, readback="something else")
        browser = _TypeBrowser(handle)
        page = _TypePage()
        asyncio.run(TypeHandler().execute(_type_ctx(browser, page, "hello world")))

        assert browser._last_action_error is not None
        assert "readback mismatch" in str(browser._last_action_error)

    def test_plain_input_keeps_keyboard_path(self) -> None:
        handle = _TypeHandle(contenteditable=False)
        browser = _TypeBrowser(handle)
        page = _TypePage()
        asyncio.run(TypeHandler().execute(_type_ctx(browser, page, "hello world")))

        assert browser._last_action_error is None
        assert handle.richtext_written == []
        assert page.keyboard.typed == ["hello world"]
        entry = browser.rpa_trail[-1]
        assert "method" not in entry

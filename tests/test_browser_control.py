from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visual_web_agent.browser_backend import (
    BrowserBackendHealth,
    BrowserBackendInfo,
    BrowserBackendSession,
    RemotePlaywrightBrowserBackend,
    build_default_browser_backend,
    list_browser_backends,
)
from visual_web_agent.browser_control import BrowserControlManager, BrowserControlSession, _browser_action_recovery_actions, _classify_browser_action_failure, build_browser_action_issue_summary, build_browser_action_trace


def _local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_browser_control_tests"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


class _FakeLocator:
    def __init__(self) -> None:
        self.clicked = False
        self.dblclicked = False
        self.focused = False
        self.hovered = False
        self.pressed = ""
        self.filled = ""
        self.typed = ""
        self.waited = False
        self.checked = False
        self.selected = ""
        self.uploaded = None
        self.scrolled_into_view = False

    @property
    def first(self):
        return self

    def nth(self, index: int):
        self.nth_index = index
        return self

    async def click(self, timeout: int = 0) -> None:
        self.clicked = True

    async def dblclick(self, timeout: int = 0) -> None:
        self.dblclicked = True

    async def focus(self, timeout: int = 0) -> None:
        self.focused = True

    async def hover(self, timeout: int = 0) -> None:
        self.hovered = True

    async def fill(self, text: str, timeout: int = 0) -> None:
        self.filled = text

    async def type(self, text: str, timeout: int = 0) -> None:
        self.typed += text

    async def press(self, key: str, timeout: int = 0) -> None:
        self.pressed = key

    async def wait_for(self, timeout: int = 0) -> None:
        self.waited = True

    async def check(self, timeout: int = 0) -> None:
        self.checked = True

    async def uncheck(self, timeout: int = 0) -> None:
        self.checked = False

    async def select_option(self, value: str, timeout: int = 0):
        self.selected = value
        return [value]

    async def scroll_into_view_if_needed(self, timeout: int = 0) -> None:
        self.scrolled_into_view = True

    async def set_input_files(self, paths, timeout: int = 0) -> None:
        self.uploaded = paths

    async def count(self) -> int:
        return 3

    async def bounding_box(self, timeout: int = 0):
        return {"x": 1, "y": 2, "width": 3, "height": 4}

    async def is_visible(self, timeout: int = 0) -> bool:
        return True

    async def is_enabled(self, timeout: int = 0) -> bool:
        return True

    async def is_checked(self, timeout: int = 0) -> bool:
        return self.checked

    async def inner_text(self, timeout: int = 0) -> str:
        return "hello"

    async def inner_html(self, timeout: int = 0) -> str:
        return "<b>hello</b>"

    async def input_value(self, timeout: int = 0) -> str:
        return self.filled or "value"

    async def get_attribute(self, attr: str, timeout: int = 0) -> str:
        return f"attr:{attr}"


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://example.com"
        self.locator_obj = _FakeLocator()
        self.last_selector = ""
        self.keyboard = self
        self.mouse = self
        self.key_pressed = ""
        self.wheel_delta = (0, 0)
        self.wait_timeout = 0
        self.load_state = ""
        self.closed = False
        self.handlers = {}
        self.local_storage = {}

    def is_closed(self) -> bool:
        return self.closed

    async def evaluate(self, script, selector=None):
        script_text = str(script or "")
        if "localStorage.clear" in script_text:
            self.local_storage.clear()
            return None
        if "localStorage.setItem" in script_text:
            key, value = selector
            self.local_storage[str(key)] = str(value)
            return None
        if "localStorage.getItem" in script_text:
            return self.local_storage.get(str(selector))
        if "Object.fromEntries" in script_text:
            return dict(self.local_storage)
        if selector is None:
            return "eval-result"
        if isinstance(selector, list) and "querySelectorAll('body *')" in script_text:
            return [
                {"score": 0.9, "tag": "button", "role": "button", "name": "Cancel", "selector": "button.cancel", "type": "", "href": ""},
                {"score": 0.8, "tag": "button", "role": "button", "name": "Reset", "selector": "button.reset", "type": "", "href": ""},
            ]
        if "role_selector" in script_text:
            return {
                "css": str(selector),
                "xpath": "/html/body/button",
                "role": "button",
                "role_selector": 'role=button[name="Submit"]',
                "text": "Submit",
                "tag": "button",
                "type": "",
                "href": "",
                "attributes": {"class": "submit"},
            }
        return [
            {"tag": "button", "role": "button", "name": "Submit", "selector": "button.submit", "type": "", "href": ""},
            {"tag": "input", "role": "", "name": "Email", "selector": "#email", "type": "text", "href": ""},
        ]

    def locator(self, selector: str) -> _FakeLocator:
        self.last_selector = selector
        return self.locator_obj

    def get_by_text(self, text: str) -> _FakeLocator:
        return self.locator_obj

    def get_by_role(self, role: str, **kwargs) -> _FakeLocator:
        self.last_role = role
        self.last_role_kwargs = kwargs
        return self.locator_obj

    def get_by_label(self, label: str, **kwargs) -> _FakeLocator:
        self.last_label = label
        self.last_label_kwargs = kwargs
        return self.locator_obj

    async def title(self) -> str:
        return "Example"

    async def screenshot(self, **kwargs) -> bytes:
        return b"png"

    async def pdf(self, path: str) -> None:
        self.pdf_path = path

    async def press(self, key: str) -> None:
        self.key_pressed = key

    async def wheel(self, dx: int, dy: int) -> None:
        self.wheel_delta = (dx, dy)

    async def wait_for_timeout(self, ms: int) -> None:
        self.wait_timeout = ms

    async def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        self.load_state = state

    async def go_back(self, wait_until: str = ""):
        self.url = "https://example.com/back"
        return None

    async def go_forward(self, wait_until: str = ""):
        self.url = "https://example.com/forward"
        return None

    async def reload(self, wait_until: str = ""):
        self.url = "https://example.com/reload"
        return None

    async def goto(self, url: str, wait_until: str = "", timeout: int = 0):
        self.url = url
        return None

    async def close(self) -> None:
        self.closed = True

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler


class _FakeContext:
    def __init__(self) -> None:
        self.pages = [_FakePage()]
        self.cookie_items = [{"name": "sid", "value": "1"}]

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    async def cookies(self):
        return list(self.cookie_items)

    async def add_cookies(self, cookies) -> None:
        self.cookie_items.extend(cookies)

    async def clear_cookies(self) -> None:
        self.cookie_items = []


class _FakeCloseable:
    def __init__(self) -> None:
        self.closed = False
        self.stopped = False

    async def close(self) -> None:
        self.closed = True

    async def stop(self) -> None:
        self.stopped = True


class _FakeApiBrowserControl:
    def __init__(self) -> None:
        self.closed = False

    def backend_status(self):
        return {
            "active": {"name": "fake_backend", "kind": "fake"},
            "available": [{"name": "fake_backend", "kind": "fake"}],
            "session_count": 1,
        }

    def list_sessions(self):
        return [{"session_id": "default", "url": "https://example.com", "ref_count": 1}]

    async def open(self, url: str, *, session_id: str = "default", headed: bool = False):
        return {"session_id": session_id, "url": url, "status_code": 200, "headed": headed}

    async def tabs(self, *, session_id: str = "default"):
        return {"session_id": session_id, "count": 1, "tabs": [{"index": 0, "url": "https://example.com", "active": True}]}

    async def tab_new(self, url: str = "", *, session_id: str = "default"):
        return {"session_id": session_id, "index": 1, "url": url}

    async def tab_switch(self, index: int, *, session_id: str = "default"):
        return {"session_id": session_id, "index": index, "url": "https://example.com/tab"}

    async def tab_close(self, index: int | None = None, *, session_id: str = "default"):
        return {"session_id": session_id, "closed": True, "remaining": 1}

    async def snapshot(self, *, session_id: str = "default", interactive: bool = True):
        return {"session_id": session_id, "count": 1, "items": [{"ref": "@e1", "name": "Submit"}], "interactive": interactive}

    async def find(self, strategy: str, query: str, *, session_id: str = "default", action: str = "text", name: str = "", value: str = "", attr: str = "", index: int = 0, exact: bool = True):
        return {"session_id": session_id, "strategy": strategy, "query": query, "action": action, "value": value or query}

    async def selector(self, ref: str, *, session_id: str = "default"):
        return {"session_id": session_id, "ref": ref, "selectors": {"css": "button.submit", "xpath": "/html/body/button", "role": "role=button", "text": "Submit"}}

    async def similar(self, ref: str, *, session_id: str = "default", limit: int = 20):
        return {"session_id": session_id, "ref": ref, "count": 1, "items": [{"ref": "@e2", "score": 0.9, "name": "Cancel"}], "limit": limit}

    async def click(self, ref: str, *, session_id: str = "default"):
        return {"session_id": session_id, "ref": ref}

    async def dblclick(self, ref: str, *, session_id: str = "default"):
        return {"session_id": session_id, "ref": ref}

    async def focus(self, ref: str, *, session_id: str = "default"):
        return {"session_id": session_id, "ref": ref}

    async def hover(self, ref: str, *, session_id: str = "default"):
        return {"session_id": session_id, "ref": ref}

    async def fill(self, ref: str, text: str, *, session_id: str = "default"):
        return {"session_id": session_id, "ref": ref, "value": text}

    async def check(self, ref: str, *, session_id: str = "default", checked: bool = True):
        return {"session_id": session_id, "ref": ref, "checked": checked}

    async def select(self, ref: str, value: str, *, session_id: str = "default"):
        return {"session_id": session_id, "ref": ref, "value": value, "selected": [value]}

    async def scroll_into_view(self, ref: str, *, session_id: str = "default"):
        return {"session_id": session_id, "ref": ref}

    async def upload(self, ref: str, paths, *, session_id: str = "default"):
        return {"session_id": session_id, "ref": ref, "paths": paths if isinstance(paths, list) else [paths]}

    async def type(self, ref: str, text: str, *, session_id: str = "default"):
        return {"session_id": session_id, "ref": ref, "value": text}

    async def press(self, key: str, *, session_id: str = "default", ref: str = ""):
        return {"session_id": session_id, "ref": ref, "key": key}

    async def scroll(self, direction: str = "down", amount: int = 500, *, session_id: str = "default"):
        return {"session_id": session_id, "direction": direction, "amount": amount}

    async def wait(self, *, session_id: str = "default", ms: int = 0, ref: str = "", text: str = "", load_state: str = ""):
        return {"session_id": session_id, "waited": True, "ms": ms}

    async def evaluate(self, script: str, *, session_id: str = "default"):
        return {"session_id": session_id, "value": script}

    async def navigate(self, action: str, *, session_id: str = "default"):
        return {"session_id": session_id, "action": action, "url": f"https://example.com/{action}"}

    async def get(self, kind: str, *, ref: str = "", attr: str = "", selector: str = "", session_id: str = "default"):
        return {"session_id": session_id, "kind": kind, "value": selector or attr or ref or "ok"}

    async def is_state(self, kind: str, ref: str, *, session_id: str = "default"):
        return {"session_id": session_id, "kind": kind, "ref": ref, "value": True}

    async def screenshot(self, *, session_id: str = "default", path: str = "", full_page: bool = False):
        return {"session_id": session_id, "path": path, "data": "abc", "full_page": full_page}

    async def pdf(self, *, session_id: str = "default", path: str = ""):
        return {"session_id": session_id, "path": path}

    async def cookies(self, *, session_id: str = "default"):
        return {"session_id": session_id, "cookies": [{"name": "sid", "value": "1"}]}

    async def cookies_set(self, name: str, value: str, *, session_id: str = "default", url: str = ""):
        return {"session_id": session_id, "cookie": {"name": name, "value": value, "url": url}}

    async def cookies_clear(self, *, session_id: str = "default"):
        return {"session_id": session_id, "cleared": True}

    async def storage_local(self, *, session_id: str = "default", key: str = "", value=None, clear: bool = False):
        return {"session_id": session_id, "key": key, "value": {} if clear else value if value is not None else "stored"}

    def console(self, *, session_id: str = "default", clear: bool = False):
        return {"session_id": session_id, "count": 1, "items": [{"text": "hello"}], "cleared": clear}

    def errors(self, *, session_id: str = "default", clear: bool = False):
        return {"session_id": session_id, "count": 1, "items": [{"error": "boom"}], "cleared": clear}

    def network_requests(self, *, session_id: str = "default", filter_text: str = "", clear: bool = False):
        return {"session_id": session_id, "count": 1, "items": [{"url": "https://example.com/api"}], "cleared": clear}

    async def close(self, session_id: str = "default"):
        self.closed = True
        return {"session_id": session_id, "closed": True}


class _FakeBrowserBackend:
    def __init__(self) -> None:
        self.created_headed: bool | None = None
        self.health_calls = 0
        self.page = _FakePage()
        self.browser = _FakeCloseable()
        self.context = _FakeContext()
        self.playwright = _FakeCloseable()

    def info(self) -> BrowserBackendInfo:
        return BrowserBackendInfo(
            name="fake_backend",
            kind="fake",
            transport="in_memory",
            supports_headed=True,
            status="available",
        )

    def health(self) -> dict:
        self.health_calls += 1
        return BrowserBackendHealth(status="healthy", reachable=True, check_kind="fake").to_dict()

    async def create_session(self, *, headed: bool = False) -> BrowserBackendSession:
        self.created_headed = bool(headed)
        return BrowserBackendSession(
            playwright=self.playwright,
            browser=self.browser,
            context=self.context,
            page=self.page,
            backend_info=self.info().to_dict(),
        )


def test_browser_control_snapshot_maps_refs() -> None:
    manager = BrowserControlManager()
    page = _FakePage()
    manager.sessions["default"] = BrowserControlSession(session_id="default", page=page)

    snapshot = asyncio.run(manager.snapshot())

    assert snapshot["count"] == 2
    assert snapshot["items"][0]["ref"] == "@e1"
    assert "selector" not in snapshot["items"][0]
    assert manager.sessions["default"].refs["@e1"] == "button.submit"


def test_browser_backend_abstraction_creates_sessions_and_reports_status() -> None:
    backend = _FakeBrowserBackend()
    manager = BrowserControlManager(backend=backend)

    opened = asyncio.run(manager.open("https://example.com/backend", session_id="b1", headed=True))
    status = manager.backend_status()
    sessions = manager.list_sessions()
    available = list_browser_backends()

    assert opened["session_id"] == "b1"
    assert opened["url"] == "https://example.com/backend"
    assert backend.created_headed is True
    assert status["active"]["name"] == "fake_backend"
    assert status["health"]["status"] == "healthy"
    assert status["health"]["check_kind"] == "fake"
    assert status["health"]["cache"]["hit"] is False
    assert status["session_count"] == 1
    assert any(item["name"] == "playwright_chromium" for item in available)
    assert any(item["name"] == "stealth_browser" and item["status"] == "planned" for item in available)
    assert sessions[0]["backend"]["name"] == "fake_backend"


def test_browser_backend_status_uses_health_cache(monkeypatch) -> None:
    monkeypatch.setenv("VSPIDER_BROWSER_HEALTH_CACHE_TTL_MS", "5000")
    backend = _FakeBrowserBackend()
    manager = BrowserControlManager(backend=backend)

    first = manager.backend_status()
    second = manager.backend_status()

    assert backend.health_calls == 1
    assert first["health"]["cache"]["hit"] is False
    assert second["health"]["cache"]["hit"] is True
    assert second["health"]["cache"]["ttl_s"] == 5.0


def test_remote_browser_backend_is_configurable_by_environment(monkeypatch) -> None:
    monkeypatch.delenv("VSPIDER_BROWSER_BACKEND", raising=False)
    monkeypatch.delenv("VSPIDER_REMOTE_BROWSER_ENDPOINT", raising=False)
    monkeypatch.delenv("VSPIDER_BROWSER_CDP_ENDPOINT", raising=False)
    monkeypatch.delenv("VSPIDER_BROWSER_WS_ENDPOINT", raising=False)
    monkeypatch.delenv("VSPIDER_REMOTE_BROWSER_MODE", raising=False)

    default_backend = build_default_browser_backend()
    planned_backend = RemotePlaywrightBrowserBackend()
    planned = planned_backend.info().to_dict()
    planned_health = planned_backend.health()

    monkeypatch.setenv("VSPIDER_BROWSER_BACKEND", "remote_playwright")
    monkeypatch.setenv("VSPIDER_REMOTE_BROWSER_ENDPOINT", "ws://127.0.0.1:9333/playwright")
    selected = build_default_browser_backend()
    status = selected.info().to_dict()
    available = list_browser_backends()

    assert default_backend.info().name == "playwright_chromium"
    assert planned["name"] == "remote_playwright"
    assert planned["status"] == "planned"
    assert planned_health["status"] == "not_configured"
    assert isinstance(selected, RemotePlaywrightBrowserBackend)
    assert status["status"] == "available"
    assert status["transport"] == "ws"
    assert status["config"]["endpoint_configured"] is True
    assert any(item["name"] == "remote_playwright" and item["status"] == "available" for item in available)


def test_remote_browser_backend_health_reports_unreachable_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("VSPIDER_BROWSER_HEALTH_TIMEOUT_MS", "50")
    backend = RemotePlaywrightBrowserBackend(endpoint="ws://127.0.0.1:9/playwright", mode="ws")

    health = backend.health()

    assert health["status"] == "unhealthy"
    assert health["reachable"] is False
    assert health["check_kind"] == "tcp_probe"
    assert health["latency_ms"] is not None


def test_browser_action_trace_contract_shape() -> None:
    trace = build_browser_action_trace(
        "click",
        session_id="s1",
        started_at=1.0,
        ended_at=1.25,
        target={"ref": "@e1"},
        action_ref={"version": "action_ref.v1", "ref": "@e1", "source": "browser_ref", "selector": "#email"},
        result_summary={"url": "https://example.com"},
        backend={"name": "fake_backend"},
        warning_codes=["selector_fallback_used"],
        recommended_action="inspect_browser_action",
    )

    assert trace["version"] == "browser_action_trace.v1"
    assert trace["source"] == "browser_control"
    assert trace["action"] == "click"
    assert trace["status"] == "warn"
    assert trace["blocking"] is False
    assert trace["session_id"] == "s1"
    assert trace["duration_ms"] == 250
    assert trace["target"]["ref"] == "@e1"
    assert trace["action_ref"]["source"] == "browser_ref"
    assert trace["result_summary"]["url"] == "https://example.com"
    assert trace["backend"]["name"] == "fake_backend"
    assert trace["warning_codes"] == ["selector_fallback_used"]
    assert trace["recommended_action"] == "inspect_browser_action"
    assert trace["issue_summary"]["version"] == "browser_action_issue_summary.v1"
    assert trace["issue_summary"]["status"] == "warn"
    assert trace["issue_summary"]["issue_count"] == 1
    assert trace["issue_summary"]["issues"][0]["code"] == "selector_fallback_used"
    assert trace["issue_summary"]["recommended_actions"] == ["inspect_browser_action"]


def test_browser_action_issue_summary_contract_shape() -> None:
    summary = build_browser_action_issue_summary({
        "action": "click",
        "status": "ok",
        "target": {"ref": "@e1"},
        "warning_codes": [],
    })

    assert summary["version"] == "browser_action_issue_summary.v1"
    assert summary["source"] == "browser_control"
    assert summary["status"] == "warn"
    assert summary["blocking"] is False
    assert summary["action"] == "click"
    assert summary["issue_count"] == 2
    assert {item["code"] for item in summary["issues"]} == {"action_ref_missing", "selector_missing"}
    assert summary["warnings_by_source"]["action_ref"] == ["action_ref_missing"]
    assert summary["recommended_action"] == "refresh_browser_snapshot"
    assert summary["recommended_actions"] == ["refresh_browser_snapshot"]


def test_browser_control_click_fill_get_and_screenshot() -> None:
    manager = BrowserControlManager()
    page = _FakePage()
    session = BrowserControlSession(session_id="default", page=page, refs={"@e1": "#email"})
    manager.sessions["default"] = session

    click = asyncio.run(manager.click("@e1"))
    fill = asyncio.run(manager.fill("@e1", "user@example.com"))
    title = asyncio.run(manager.get("title"))
    text = asyncio.run(manager.get("text", ref="@e1"))
    shot = asyncio.run(manager.screenshot())

    assert click["ref"] == "@e1"
    assert fill["value"] == "user@example.com"
    assert click["action_trace"]["version"] == "browser_action_trace.v1"
    assert click["action_trace"]["action"] == "click"
    assert click["action_trace"]["target"]["ref"] == "@e1"
    assert click["action_trace"]["action_ref"]["selector"] == "#email"
    assert click["action_trace"]["issue_summary"]["status"] == "ok"
    assert fill["action_trace"]["action"] == "fill"
    assert fill["action_trace"]["result_summary"]["value_length"] == len("user@example.com")
    assert title["value"] == "Example"
    assert title["action_trace"]["action"] == "get"
    assert title["action_trace"]["target"]["kind"] == "title"
    assert text["value"] == "hello"
    assert text["action_trace"]["action_ref"]["selector"] == "#email"
    assert shot["data"] == "cG5n"
    assert shot["action_trace"]["action"] == "screenshot"
    assert shot["action_trace"]["result_summary"]["inline_data"] is True
    assert page.last_selector == "#email"


def test_browser_control_failed_action_keeps_exception_with_trace() -> None:
    manager = BrowserControlManager()
    page = _FakePage()
    session = BrowserControlSession(
        session_id="default",
        page=page,
        refs={"@e1": "#email"},
        backend_info={"name": "fake_backend"},
    )
    manager.sessions["default"] = session

    async def fail_click(timeout: int = 0) -> None:
        raise RuntimeError("click failed")

    page.locator_obj.click = fail_click

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(manager.click("@e1"))

    trace = exc_info.value.action_trace
    summary = exc_info.value.action_issue_summary

    assert trace["version"] == "browser_action_trace.v1"
    assert trace["action"] == "click"
    assert trace["status"] == "error"
    assert trace["target"]["ref"] == "@e1"
    assert trace["target"]["selector"] == "#email"
    assert trace["action_ref"]["selector"] == "#email"
    assert trace["result_summary"]["failed"] is True
    assert trace["result_summary"]["error_type"] == "RuntimeError"
    assert trace["result_summary"]["failure_code"] == "unknown_action_error"
    assert trace["result_summary"]["failure_category"] == "unknown"
    assert trace["result_summary"]["recovery_actions"] == ["inspect_browser_action", "capture_screenshot"]
    assert trace["warning_codes"] == ["action_failed", "unknown_action_error"]
    assert trace["recommended_action"] == "inspect_browser_action"
    assert summary["version"] == "browser_action_issue_summary.v1"
    assert summary["status"] == "error"
    assert {item["code"] for item in summary["issues"]} >= {"action_failed", "unknown_action_error", "action_error"}
    assert summary["recommended_actions"] == ["inspect_browser_action", "capture_screenshot"]


@pytest.mark.parametrize(
    ("exc", "action", "code", "category", "recommended_action"),
    [
        (TimeoutError("Timeout 30000ms exceeded"), "click", "timeout", "timing", "increase_wait_or_check_runtime"),
        (ValueError("unknown ref: @e99"), "click", "selector_missing", "target_resolution", "refresh_snapshot_or_use_similar_selector"),
        (RuntimeError("Element is not visible"), "click", "element_not_visible", "element_state", "scroll_into_view_or_wait_visible"),
        (RuntimeError("Target page has been closed"), "click", "context_closed", "browser_context", "reopen_browser_session"),
        (RuntimeError("net::ERR_NAME_NOT_RESOLVED"), "open", "navigation_failed", "navigation", "check_url_or_retry_navigation"),
    ],
)
def test_browser_action_failure_classifier_taxonomy(exc: Exception, action: str, code: str, category: str, recommended_action: str) -> None:
    failure = _classify_browser_action_failure(exc, action)

    assert failure["code"] == code
    assert failure["category"] == category
    assert failure["recommended_action"] == recommended_action


@pytest.mark.parametrize(
    ("failure_code", "action", "recovery_actions"),
    [
        ("selector_missing", "click", ["refresh_browser_snapshot", "use_similar_selector", "retry_action_with_new_ref"]),
        ("timeout", "click", ["increase_wait_timeout", "check_browser_runtime", "retry_action_once"]),
        ("click_intercepted", "click", ["close_overlay", "try_alternate_click", "retry_action_once"]),
        ("backend_unavailable", "open", ["check_browser_backend_health", "switch_browser_backend"]),
        ("unknown_action_error", "screenshot", ["inspect_browser_action"]),
    ],
)
def test_browser_action_recovery_actions_map_failure_codes(failure_code: str, action: str, recovery_actions: list[str]) -> None:
    assert _browser_action_recovery_actions(failure_code, action) == recovery_actions


def test_browser_control_api_failed_action_detail_exposes_trace(monkeypatch) -> None:
    import api_server

    manager = BrowserControlManager()
    page = _FakePage()
    manager.sessions["default"] = BrowserControlSession(
        session_id="default",
        page=page,
        refs={"@e1": "#email"},
    )

    async def fail_click(timeout: int = 0) -> None:
        raise RuntimeError("api click failed")

    page.locator_obj.click = fail_click
    monkeypatch.setattr(api_server, "_browser_control", manager)
    client = TestClient(api_server.app)

    resp = client.post("/api/browser_control/click", json={"ref": "@e1"})
    detail = resp.json()["detail"]

    assert resp.status_code == 400
    assert detail["message"] == "api click failed"
    assert detail["action_trace"]["version"] == "browser_action_trace.v1"
    assert detail["action_trace"]["status"] == "error"
    assert detail["action_trace"]["action"] == "click"
    assert detail["action_trace"]["result_summary"]["failure_code"] == "unknown_action_error"
    assert detail["action_trace"]["result_summary"]["recovery_actions"] == ["inspect_browser_action", "capture_screenshot"]
    assert detail["action_trace"]["warning_codes"] == ["action_failed", "unknown_action_error"]
    assert detail["action_issue_summary"]["status"] == "error"
    assert detail["action_issue_summary"]["recommended_actions"] == ["inspect_browser_action", "capture_screenshot"]


def test_browser_control_common_interactions() -> None:
    manager = BrowserControlManager()
    page = _FakePage()
    manager.sessions["default"] = BrowserControlSession(session_id="default", page=page, refs={"@e1": "#email"})

    hover = asyncio.run(manager.hover("@e1"))
    typed = asyncio.run(manager.type("@e1", "abc"))
    pressed_ref = asyncio.run(manager.press("Enter", ref="@e1"))
    pressed_page = asyncio.run(manager.press("Escape"))
    scrolled = asyncio.run(manager.scroll("down", 300))
    waited = asyncio.run(manager.wait(ms=25))
    waited_ref = asyncio.run(manager.wait(ref="@e1"))
    evaluated = asyncio.run(manager.evaluate("document.title"))
    nav = asyncio.run(manager.navigate("reload"))

    assert hover["ref"] == "@e1"
    assert typed["value"] == "abc"
    assert pressed_ref["key"] == "Enter"
    assert pressed_page["key"] == "Escape"
    assert scrolled["amount"] == 300
    assert waited["waited"] is True
    assert waited_ref["waited"] is True
    assert evaluated["value"] == "eval-result"
    assert nav["action"] == "reload"
    assert hover["action_trace"]["action"] == "hover"
    assert typed["action_trace"]["action"] == "type"
    assert typed["action_trace"]["result_summary"]["value_length"] == 3
    assert pressed_ref["action_trace"]["target"]["target_type"] == "element"
    assert pressed_page["action_trace"]["target"]["target_type"] == "page"
    assert scrolled["action_trace"]["action"] == "scroll"
    assert scrolled["action_trace"]["result_summary"]["wheel_delta"] == [0, 300]
    assert waited["action_trace"]["target"]["kind"] == "timeout"
    assert waited_ref["action_trace"]["action_ref"]["selector"] == "#email"
    assert nav["action_trace"]["action"] == "navigate"
    assert nav["action_trace"]["result_summary"]["url"] == "https://example.com/reload"
    assert page.locator_obj.hovered is True
    assert page.locator_obj.typed == "abc"
    assert page.locator_obj.pressed == "Enter"
    assert page.key_pressed == "Escape"
    assert page.wheel_delta == (0, 300)
    assert page.wait_timeout == 25


def test_browser_control_y19_element_operations_and_state() -> None:
    tmp_path = _local_tmp_path()
    manager = BrowserControlManager()
    page = _FakePage()
    manager.sessions["default"] = BrowserControlSession(session_id="default", page=page, refs={"@e1": "#email"})
    pdf_path = tmp_path / "page.pdf"

    try:
        dblclick = asyncio.run(manager.dblclick("@e1"))
        focus = asyncio.run(manager.focus("@e1"))
        checked = asyncio.run(manager.check("@e1"))
        unchecked = asyncio.run(manager.check("@e1", checked=False))
        selected = asyncio.run(manager.select("@e1", "value-a"))
        scroll_view = asyncio.run(manager.scroll_into_view("@e1"))
        uploaded = asyncio.run(manager.upload("@e1", [str(tmp_path / "a.txt")]))
        count = asyncio.run(manager.get("count", selector=".item"))
        box = asyncio.run(manager.get("box", ref="@e1"))
        visible = asyncio.run(manager.is_state("visible", "@e1"))
        pdf = asyncio.run(manager.pdf(path=str(pdf_path)))

        assert dblclick["ref"] == "@e1"
        assert focus["ref"] == "@e1"
        assert checked["checked"] is True
        assert unchecked["checked"] is False
        assert selected["selected"] == ["value-a"]
        assert scroll_view["ref"] == "@e1"
        assert uploaded["paths"] == [str(tmp_path / "a.txt")]
        assert count["value"] == 3
        assert box["value"]["width"] == 3
        assert visible["value"] is True
        assert pdf["path"] == str(pdf_path)
        assert page.locator_obj.dblclicked is True
        assert page.locator_obj.focused is True
        assert page.locator_obj.scrolled_into_view is True
    finally:
        shutil.rmtree(tmp_path.parent, ignore_errors=True)


def test_browser_control_y20_tabs_cookies_storage_and_observability() -> None:
    manager = BrowserControlManager()
    context = _FakeContext()
    session = BrowserControlSession(session_id="default", context=context, page=context.pages[0])
    manager.sessions["default"] = session

    tabs = asyncio.run(manager.tabs())
    new_tab = asyncio.run(manager.tab_new("https://example.com/new"))
    switched = asyncio.run(manager.tab_switch(0))
    cookies = asyncio.run(manager.cookies())
    cookie_set = asyncio.run(manager.cookies_set("token", "abc"))
    storage_set = asyncio.run(manager.storage_local(key="k", value="v"))
    storage_get = asyncio.run(manager.storage_local(key="k"))
    storage_all = asyncio.run(manager.storage_local())
    storage_clear = asyncio.run(manager.storage_local(clear=True))
    session.console_messages.append({"text": "hello"})
    session.page_errors.append({"error": "boom"})
    session.network_requests.append({"url": "https://example.com/api/users"})
    console = manager.console()
    errors = manager.errors(clear=True)
    network = manager.network_requests(filter_text="api")
    closed = asyncio.run(manager.tab_close(1))

    assert tabs["count"] == 1
    assert new_tab["index"] == 1
    assert new_tab["url"] == "https://example.com/new"
    assert switched["index"] == 0
    assert cookies["cookies"][0]["name"] == "sid"
    assert cookie_set["cookie"]["name"] == "token"
    assert storage_set["value"] == "v"
    assert storage_get["value"] == "v"
    assert storage_all["value"]["k"] == "v"
    assert storage_clear["value"] == {}
    assert console["items"][0]["text"] == "hello"
    assert errors["items"][0]["error"] == "boom"
    assert session.page_errors == []
    assert network["items"][0]["url"].endswith("/api/users")
    assert closed["closed"] is True


def test_browser_control_y21_find_semantic_locators() -> None:
    manager = BrowserControlManager()
    page = _FakePage()
    manager.sessions["default"] = BrowserControlSession(session_id="default", page=page)

    role_click = asyncio.run(manager.find("role", "button", action="click", name="Submit"))
    label_fill = asyncio.run(manager.find("label", "Email", action="fill", value="user@example.com"))
    css_count = asyncio.run(manager.find("css", ".item", action="count"))
    nth_text = asyncio.run(manager.find("nth", ".item", action="text", index=2))

    assert role_click["value"] is True
    assert role_click["action_trace"]["action"] == "find"
    assert role_click["action_trace"]["target"]["strategy"] == "role"
    assert role_click["action_trace"]["result_summary"]["value_type"] == "bool"
    assert page.last_role == "button"
    assert page.last_role_kwargs == {"name": "Submit", "exact": True}
    assert label_fill["value"] == "user@example.com"
    assert page.last_label == "Email"
    assert css_count["value"] == 3
    assert nth_text["value"] == "hello"
    assert page.locator_obj.nth_index == 2


def test_browser_control_y26_selector_generation_and_similar_elements() -> None:
    manager = BrowserControlManager()
    page = _FakePage()
    session = BrowserControlSession(session_id="default", page=page, refs={"@e1": "button.submit"})
    manager.sessions["default"] = session

    selector = asyncio.run(manager.selector("@e1"))
    similar = asyncio.run(manager.similar("@e1", limit=2))

    assert selector["selectors"]["css"] == "button.submit"
    assert selector["action_trace"]["action"] == "selector"
    assert selector["action_trace"]["action_ref"]["source"] == "browser_ref"
    assert selector["selectors"]["xpath"] == "/html/body/button"
    assert selector["selectors"]["role"] == 'role=button[name="Submit"]'
    assert selector["element"]["attributes"]["class"] == "submit"
    assert similar["count"] == 2
    assert similar["action_trace"]["action"] == "similar"
    assert similar["action_trace"]["result_summary"]["count"] == 2
    assert similar["items"][0]["ref"] == "@e2"
    assert session.refs["@e2"] == "button.cancel"
    assert session.refs["@e3"] == "button.reset"


def test_browser_control_close_session() -> None:
    manager = BrowserControlManager()
    context = _FakeCloseable()
    browser = _FakeCloseable()
    playwright = _FakeCloseable()
    manager.sessions["s1"] = BrowserControlSession(
        session_id="s1",
        context=context,
        browser=browser,
        playwright=playwright,
        page=_FakePage(),
    )

    result = asyncio.run(manager.close("s1"))

    assert result == {"session_id": "s1", "closed": True}
    assert "s1" not in manager.sessions
    assert context.closed is True
    assert browser.closed is True
    assert playwright.stopped is True


def test_browser_control_api_wiring(monkeypatch) -> None:
    import api_server

    fake = _FakeApiBrowserControl()
    monkeypatch.setattr(api_server, "_browser_control", fake)
    client = TestClient(api_server.app)

    assert client.get("/api/browser_control/sessions").json()["sessions"][0]["session_id"] == "default"
    assert client.get("/api/browser_control/backend").json()["result"]["active"]["name"] == "fake_backend"
    assert client.post("/api/browser_control/tabs", json={}).json()["result"]["count"] == 1
    assert client.post("/api/browser_control/tab/new", json={"url": "https://example.com/new"}).json()["result"]["index"] == 1
    assert client.post("/api/browser_control/tab/switch", json={"index": 0}).json()["result"]["index"] == 0
    assert client.post("/api/browser_control/tab/close", json={"index": 0}).json()["result"]["closed"] is True
    assert client.post("/api/browser_control/open", json={"url": "https://example.com"}).json()["result"]["status_code"] == 200
    assert client.post("/api/browser_control/snapshot", json={}).json()["snapshot"]["items"][0]["ref"] == "@e1"
    assert client.post("/api/browser_control/find", json={"strategy": "role", "query": "button", "action": "click"}).json()["result"]["action"] == "click"
    assert client.post("/api/browser_control/selector", json={"ref": "@e1"}).json()["result"]["selectors"]["css"] == "button.submit"
    assert client.post("/api/browser_control/similar", json={"ref": "@e1", "limit": 3}).json()["result"]["count"] == 1
    assert client.post("/api/browser_control/click", json={"ref": "@e1"}).json()["result"]["ref"] == "@e1"
    assert client.post("/api/browser_control/dblclick", json={"ref": "@e1"}).json()["result"]["ref"] == "@e1"
    assert client.post("/api/browser_control/focus", json={"ref": "@e1"}).json()["result"]["ref"] == "@e1"
    assert client.post("/api/browser_control/hover", json={"ref": "@e1"}).json()["result"]["ref"] == "@e1"
    assert client.post("/api/browser_control/fill", json={"ref": "@e1", "text": "x"}).json()["result"]["value"] == "x"
    assert client.post("/api/browser_control/check", json={"ref": "@e1"}).json()["result"]["checked"] is True
    assert client.post("/api/browser_control/uncheck", json={"ref": "@e1"}).json()["result"]["checked"] is False
    assert client.post("/api/browser_control/select", json={"ref": "@e1", "value": "a"}).json()["result"]["selected"] == ["a"]
    assert client.post("/api/browser_control/scrollintoview", json={"ref": "@e1"}).json()["result"]["ref"] == "@e1"
    assert client.post("/api/browser_control/upload", json={"ref": "@e1", "path": "a.txt"}).json()["result"]["paths"] == ["a.txt"]
    assert client.post("/api/browser_control/type", json={"ref": "@e1", "text": "x"}).json()["result"]["value"] == "x"
    assert client.post("/api/browser_control/press", json={"key": "Enter"}).json()["result"]["key"] == "Enter"
    assert client.post("/api/browser_control/scroll", json={"direction": "down", "amount": 100}).json()["result"]["amount"] == 100
    assert client.post("/api/browser_control/wait", json={"ms": 10}).json()["result"]["waited"] is True
    assert client.post("/api/browser_control/eval", json={"script": "1+1"}).json()["result"]["value"] == "1+1"
    assert client.post("/api/browser_control/navigate", json={"action": "reload"}).json()["result"]["action"] == "reload"
    assert client.post("/api/browser_control/get", json={"kind": "title"}).json()["result"]["kind"] == "title"
    assert client.post("/api/browser_control/get", json={"kind": "count", "selector": ".item"}).json()["result"]["value"] == ".item"
    assert client.post("/api/browser_control/is", json={"kind": "visible", "ref": "@e1"}).json()["result"]["value"] is True
    assert client.post("/api/browser_control/screenshot", json={}).json()["result"]["data"] == "abc"
    assert client.post("/api/browser_control/pdf", json={"path": "out.pdf"}).json()["result"]["path"] == "out.pdf"
    assert client.post("/api/browser_control/cookies", json={}).json()["result"]["cookies"][0]["name"] == "sid"
    assert client.post("/api/browser_control/cookies/set", json={"name": "sid", "value": "2"}).json()["result"]["cookie"]["value"] == "2"
    assert client.post("/api/browser_control/cookies/clear", json={}).json()["result"]["cleared"] is True
    assert client.post("/api/browser_control/storage/local", json={"key": "k", "value": "v"}).json()["result"]["value"] == "v"
    assert client.post("/api/browser_control/console", json={}).json()["result"]["items"][0]["text"] == "hello"
    assert client.post("/api/browser_control/errors", json={"clear": True}).json()["result"]["cleared"] is True
    assert client.post("/api/browser_control/network/requests", json={"filter": "api"}).json()["result"]["count"] == 1
    assert client.post("/api/browser_control/close", json={}).json()["result"]["closed"] is True


def test_browser_control_source_wiring() -> None:
    root = Path(__file__).resolve().parent.parent
    api_src = (root / "api_server.py").read_text(encoding="utf-8")
    backend_src = (root / "visual_web_agent" / "browser_backend.py").read_text(encoding="utf-8")
    control_api_src = (root / "visual_web_agent" / "browser_control_api.py").read_text(encoding="utf-8")
    control_src = (root / "visual_web_agent" / "browser_control.py").read_text(encoding="utf-8")

    assert "from visual_web_agent.browser_control_api import BrowserControlApiDeps, create_browser_control_router" in api_src
    assert "from visual_web_agent.browser_control import BrowserControlManager" in api_src
    assert "_browser_control = BrowserControlManager()" in api_src
    assert "app_browser_control_router = create_browser_control_router(BrowserControlApiDeps(" in api_src
    assert "browser_control=lambda: _browser_control" in api_src
    assert "http_exception_detail=_http_exception_detail" in api_src
    assert "app.include_router(app_browser_control_router)" in api_src
    assert "class BrowserControlApiDeps" in control_api_src
    assert "def create_browser_control_router(" in control_api_src
    assert "router = APIRouter" in control_api_src
    for route in [
        '@router.get("/api/browser_control/sessions"',
        '@router.get("/api/browser_control/backend"',
        '@router.post("/api/browser_control/open"',
        '@router.post("/api/browser_control/tabs"',
        '@router.post("/api/browser_control/tab/new"',
        '@router.post("/api/browser_control/tab/switch"',
        '@router.post("/api/browser_control/tab/close"',
        '@router.post("/api/browser_control/snapshot"',
        '@router.post("/api/browser_control/find"',
        '@router.post("/api/browser_control/selector"',
        '@router.post("/api/browser_control/similar"',
        '@router.post("/api/browser_control/click"',
        '@router.post("/api/browser_control/dblclick"',
        '@router.post("/api/browser_control/focus"',
        '@router.post("/api/browser_control/hover"',
        '@router.post("/api/browser_control/fill"',
        '@router.post("/api/browser_control/check"',
        '@router.post("/api/browser_control/uncheck"',
        '@router.post("/api/browser_control/select"',
        '@router.post("/api/browser_control/scrollintoview"',
        '@router.post("/api/browser_control/upload"',
        '@router.post("/api/browser_control/type"',
        '@router.post("/api/browser_control/press"',
        '@router.post("/api/browser_control/scroll"',
        '@router.post("/api/browser_control/wait"',
        '@router.post("/api/browser_control/eval"',
        '@router.post("/api/browser_control/navigate"',
        '@router.post("/api/browser_control/get"',
        '@router.post("/api/browser_control/is"',
        '@router.post("/api/browser_control/screenshot"',
        '@router.post("/api/browser_control/pdf"',
        '@router.post("/api/browser_control/cookies"',
        '@router.post("/api/browser_control/cookies/set"',
        '@router.post("/api/browser_control/cookies/clear"',
        '@router.post("/api/browser_control/storage/local"',
        '@router.post("/api/browser_control/console"',
        '@router.post("/api/browser_control/errors"',
        '@router.post("/api/browser_control/network/requests"',
        '@router.post("/api/browser_control/close"',
    ]:
        assert route in control_api_src
    assert "class BrowserBackendInfo" in backend_src
    assert "class BrowserBackendSession" in backend_src
    assert "class BrowserBackendHealth" in backend_src
    assert "class PlaywrightBrowserBackend" in backend_src
    assert "class RemotePlaywrightBrowserBackend" in backend_src
    assert "def list_browser_backends(" in backend_src
    assert "def browser_backend_health_from_info(" in backend_src
    assert "socket.create_connection" in backend_src
    assert "VSPIDER_BROWSER_HEALTH_TIMEOUT_MS" in backend_src
    assert "def build_default_browser_backend(" in backend_src
    assert "VSPIDER_REMOTE_BROWSER_ENDPOINT" in backend_src
    assert "connect_over_cdp" in backend_src
    assert "browser_backend_health_from_info" in control_src
    assert "_backend_health_cache_ttl_s" in control_src
    assert "VSPIDER_BROWSER_HEALTH_CACHE_TTL_MS" in control_src
    assert '"cache": health' not in control_src
    assert "def build_browser_action_trace(" in control_src
    assert "def build_browser_action_issue_summary(" in control_src
    assert "def _classify_browser_action_failure(" in control_src
    assert "def _browser_action_recovery_actions(" in control_src
    assert "def _attach_browser_action_failure_trace(" in control_src
    assert "def _browser_action_failure_traced(" in control_src
    assert '"version": _BROWSER_ACTION_TRACE_VERSION' in control_src
    assert '"version": _BROWSER_ACTION_ISSUE_SUMMARY_VERSION' in control_src
    assert '"blocking": False' in control_src
    assert 'warning_codes = list(dict.fromkeys(["action_failed", failure_code]))' in control_src
    assert '"failure_code": failure_code' in control_src
    assert '"failure_category": failure_category' in control_src
    assert '"recovery_actions": recovery_actions' in control_src
    assert 'for item in result_summary.get("recovery_actions") or []:' in control_src
    assert '"retry_action_with_new_ref"' in control_src
    assert '"check_browser_runtime"' in control_src
    assert '"selector_missing", "category": "target_resolution"' in control_src
    assert '"timeout", "category": "timing"' in control_src
    assert 'setattr(exc, "action_trace", trace)' in control_src
    assert 'setattr(exc, "action_issue_summary", trace.get("issue_summary") or {})' in control_src
    assert '"action_trace": build_browser_action_trace(' in control_src
    assert 'trace["issue_summary"] = build_browser_action_issue_summary(trace)' in control_src
    assert '"target_type": "element" if ref else "page"' in control_src
    assert '"inline_data": not bool(path)' in control_src
    assert "class BrowserControlManager:" in control_src
    assert "def backend_status(" in control_src
    assert '"health": health' in control_src
    assert "backend_session = await self.backend.create_session(headed=headed)" in control_src
    assert "backend_info=dict(backend_session.backend_info or {})" in control_src
    assert "async def open(" in control_src
    assert "async def snapshot(" in control_src
    assert "async def find(" in control_src
    assert "async def selector(" in control_src
    assert "async def similar(" in control_src
    assert "def _find_locator(" in control_src
    assert "async def dblclick(" in control_src
    assert "async def check(" in control_src
    assert "async def is_state(" in control_src
    assert "async def hover(" in control_src
    assert "async def navigate(" in control_src
    assert "async def tabs(" in control_src
    assert "async def cookies(" in control_src
    assert "async def storage_local(" in control_src
    assert "def network_requests(" in control_src
    assert "def _http_exception_detail(exc: Exception) -> Any:" in api_src
    assert '"action_trace": dict(action_trace)' in api_src
    assert "detail=detail(exc)" in control_api_src

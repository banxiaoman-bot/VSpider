"""Tests for visual_web_agent.hitl_form_proxy (offline, stub page)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from visual_web_agent.hitl_form_proxy import detect_form_fields, fill_form_fields


class _StubPage:
    def __init__(self, fields: list[dict] | None = None) -> None:
        self._fields = fields or []
        self._filled: dict[str, str] = {}

    async def evaluate(self, _js: str) -> list[dict]:
        return list(self._fields)

    def locator(self, selector: str):
        return _StubLocator(self, selector)

    async def screenshot(self, *, type: str = "png", quality: int = 80) -> bytes:
        return b"\x89PNG_stub"


class _StubLocator:
    def __init__(self, page: _StubPage, selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return 1

    async def fill(self, value: str) -> None:
        self._page._filled[self._selector] = value


SAMPLE_FIELDS = [
    {
        "id": "username",
        "tag": "input",
        "type": "text",
        "name": "username",
        "label": "Username",
        "placeholder": "Enter username",
        "value": "",
        "required": True,
        "selector": "#username",
    },
    {
        "id": "password",
        "tag": "input",
        "type": "password",
        "name": "password",
        "label": "Password",
        "placeholder": "",
        "value": "",
        "required": True,
        "selector": "#password",
    },
]


def test_detect_form_fields_returns_list() -> None:
    page = _StubPage(SAMPLE_FIELDS)
    result = asyncio.run(detect_form_fields(page))
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["id"] == "username"
    assert result[1]["type"] == "password"


def test_detect_form_fields_empty_page() -> None:
    page = _StubPage([])
    result = asyncio.run(detect_form_fields(page))
    assert result == []


def test_detect_form_fields_evaluate_exception() -> None:
    page = _StubPage()
    page.evaluate = AsyncMock(side_effect=RuntimeError("crash"))
    result = asyncio.run(detect_form_fields(page))
    assert result == []


def test_fill_form_fields_fills_values() -> None:
    page = _StubPage(SAMPLE_FIELDS)
    values = {"username": "admin", "password": "secret123"}
    filled = asyncio.run(fill_form_fields(page, SAMPLE_FIELDS, values))
    assert filled == 2
    assert page._filled["#username"] == "admin"
    assert page._filled["#password"] == "secret123"


def test_fill_form_fields_skips_empty() -> None:
    page = _StubPage(SAMPLE_FIELDS)
    values = {"username": "admin", "password": ""}
    filled = asyncio.run(fill_form_fields(page, SAMPLE_FIELDS, values))
    assert filled == 1
    assert "#password" not in page._filled


def test_fill_form_fields_missing_selector() -> None:
    fields_no_selector = [{"id": "x", "selector": ""}]
    page = _StubPage()
    filled = asyncio.run(fill_form_fields(page, fields_no_selector, {"x": "val"}))
    assert filled == 0

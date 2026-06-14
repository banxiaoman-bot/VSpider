"""E4: cross-run selector cache + click_text/click/type wiring.

Cache hits must keep full evidence (visible + text fingerprint before the
click); validation failure invalidates and falls back to the original
funnel, which writes the fresh locate back into the cache.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from visual_web_agent.selector_cache import (
    DERIVE_SELECTOR_JS,
    SelectorCache,
    cache_host,
    click_cached_selector,
    derive_selector,
    normalize_key,
    selector_cache_enabled,
    som_cache_key,
    validate_cached_target,
)


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_selector_cache_tests"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ════════════════════════════════════════════════════════════════════
#                       PURE CACHE STORE
# ════════════════════════════════════════════════════════════════════


class TestSelectorCacheStore:
    def test_store_lookup_roundtrip_and_file_shape(self, local_tmp_path: Path) -> None:
        cache = SelectorCache("demo.example.com", base_dir=local_tmp_path)
        cache.store("click_text", "下一页", "#next-btn")

        entry = cache.lookup("click_text", "下一页")
        assert entry["selector"] == "#next-btn"
        assert entry["hits"] == 0
        assert entry["signature"] == normalize_key("下一页")

        raw = json.loads((local_tmp_path / "demo.example.com.json").read_text(encoding="utf-8"))
        assert raw["version"] == "action_selector_cache.v1"
        assert raw["host"] == "demo.example.com"
        assert "click_text::下一页" in raw["entries"]

    def test_lookup_normalises_key(self, local_tmp_path: Path) -> None:
        cache = SelectorCache("demo.example.com", base_dir=local_tmp_path)
        cache.store("click_text", "  Next   Page ", "#n")

        assert cache.lookup("click_text", "next page")["selector"] == "#n"
        assert cache.lookup("click_text", "other") is None

    def test_record_hit_and_invalidate(self, local_tmp_path: Path) -> None:
        cache = SelectorCache("demo.example.com", base_dir=local_tmp_path)
        cache.store("click_text", "Next", "#n")
        cache.record_hit("click_text", "Next")
        cache.record_hit("click_text", "Next")

        fresh = SelectorCache("demo.example.com", base_dir=local_tmp_path)
        assert fresh.lookup("click_text", "Next")["hits"] == 2

        fresh.invalidate("click_text", "Next")
        assert SelectorCache("demo.example.com", base_dir=local_tmp_path).lookup("click_text", "Next") is None

    def test_corrupted_file_starts_fresh(self, local_tmp_path: Path) -> None:
        path = local_tmp_path / "demo.example.com.json"
        path.write_text("{not json", encoding="utf-8")

        cache = SelectorCache("demo.example.com", base_dir=local_tmp_path)
        assert cache.entries() == {}
        cache.store("click_text", "ok", "#ok")
        assert SelectorCache("demo.example.com", base_dir=local_tmp_path).lookup("click_text", "ok")

    def test_capacity_prunes_oldest(self, local_tmp_path: Path) -> None:
        cache = SelectorCache("demo.example.com", base_dir=local_tmp_path)
        for i in range(205):
            cache.store("click_text", f"key {i}", f"#sel{i}")

        kept = SelectorCache("demo.example.com", base_dir=local_tmp_path).entries()
        assert len(kept) <= 200
        assert "click_text::key 204" in kept  # newest survives

    def test_cache_host_and_enabled_flag(self, monkeypatch) -> None:
        assert cache_host("https://user@Demo.Example.com:8443/p?q=1") == "demo.example.com"
        assert cache_host("") == ""
        monkeypatch.delenv("VSPIDER_SELECTOR_CACHE", raising=False)
        assert selector_cache_enabled() is True
        monkeypatch.setenv("VSPIDER_SELECTOR_CACHE", "0")
        assert selector_cache_enabled() is False


# ════════════════════════════════════════════════════════════════════
#                       ASYNC HELPERS (stub locators)
# ════════════════════════════════════════════════════════════════════


class _StubLocator:
    def __init__(self, *, count: int = 1, visible: bool = True, text: str = "", derived: str = "#derived"):
        self._count = count
        self._visible = visible
        self._text = text
        self._derived = derived
        self.click_calls = 0

    def locator(self, _sel: str):
        return self

    @property
    def first(self):
        return self

    def nth(self, _i: int):
        return self

    def filter(self, **_):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def inner_text(self):
        return self._text

    async def evaluate(self, script: str, *args, **kwargs):
        if script == DERIVE_SELECTOR_JS:
            return self._derived
        return False  # disabled-ancestor probe in the funnel

    async def scroll_into_view_if_needed(self, **_):
        return None

    async def click(self, **_):
        self.click_calls += 1


class _StubPage:
    def __init__(self, *, cached: _StubLocator | None = None, funnel: _StubLocator | None = None, url: str = "https://demo.example.com/list"):
        self.url = url
        self._cached = cached
        self._funnel = funnel
        self.locator_calls: list[str] = []

    def locator(self, sel: str):
        self.locator_calls.append(sel)
        if self._cached is not None and sel == (self._cached_sel if hasattr(self, "_cached_sel") else "#cached"):
            return self._cached
        # funnel tier-1 popup selectors: return empty
        return _StubLocator(count=0)

    def get_by_text(self, _text: str, exact: bool = True):
        return self._funnel if self._funnel is not None else _StubLocator(count=0)

    def get_by_role(self, _role: str, **_):
        return _StubLocator(count=0)


async def _fake_click_fn(locator: Any, label: str, timeout: int = 3000) -> str:
    await locator.click()
    return "native"


class TestAsyncHelpers:
    def test_click_cached_selector_happy_path(self) -> None:
        cached = _StubLocator(text="下一页 >", derived="")
        page = _StubPage(cached=cached)
        page._cached_sel = "#cached"

        used = asyncio.run(click_cached_selector(page, {"selector": "#cached"}, "下一页", _fake_click_fn))

        assert used.startswith("selector_cache #cached")
        assert cached.click_calls == 1

    def test_text_mismatch_rejected_without_click(self) -> None:
        cached = _StubLocator(text="完全无关")
        page = _StubPage(cached=cached)
        page._cached_sel = "#cached"

        used = asyncio.run(click_cached_selector(page, {"selector": "#cached"}, "下一页", _fake_click_fn))

        assert used == ""
        assert cached.click_calls == 0

    def test_zero_count_and_invisible_rejected(self) -> None:
        for stub in (_StubLocator(count=0, text="下一页"), _StubLocator(visible=False, text="下一页")):
            page = _StubPage(cached=stub)
            page._cached_sel = "#cached"
            assert asyncio.run(click_cached_selector(page, {"selector": "#cached"}, "下一页", _fake_click_fn)) == ""

    def test_derive_selector_value_and_quiet_failure(self) -> None:
        assert asyncio.run(derive_selector(_StubLocator(derived="#btn"))) == "#btn"

        class _Raises:
            async def evaluate(self, *_a, **_k):
                raise RuntimeError("gone")

        assert asyncio.run(derive_selector(_Raises())) == ""


# ════════════════════════════════════════════════════════════════════
#                       CLICK_TEXT WIRING (stub e2e)
# ════════════════════════════════════════════════════════════════════


def _make_ctx(text: str, page: Any) -> SimpleNamespace:
    async def _no_op(*_, **__):
        return None

    browser = SimpleNamespace(
        _clear_som_overlays=_no_op,
        _LOCATOR_TIMEOUT=1000,
        _wait_after_action=_no_op,
        _tab_switch_notice=None,
        _last_native_dialog=None,
        rpa_trail=[],
    )
    action = SimpleNamespace(target_id=0, type_value=text, memory_key="")
    return SimpleNamespace(
        action=action,
        browser=browser,
        page=page,
        workflow_memory={},
        with_rpa_meta=lambda d: d,
    )


class TestClickTextWiring:
    def _run(self, ctx) -> None:
        from visual_web_agent.actions import ClickTextHandler

        asyncio.run(ClickTextHandler().execute(ctx))

    def test_cache_hit_skips_funnel_and_records_hit(self, local_tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("VSPIDER_SELECTOR_CACHE_DIR", str(local_tmp_path))
        SelectorCache("demo.example.com", base_dir=local_tmp_path).store("click_text", "下一页", "#cached")

        cached = _StubLocator(text="下一页")
        funnel = _StubLocator(count=1, text="下一页")
        page = _StubPage(cached=cached, funnel=funnel)
        page._cached_sel = "#cached"
        ctx = _make_ctx("下一页", page)

        self._run(ctx)

        assert cached.click_calls == 1
        assert funnel.click_calls == 0
        trail = ctx.browser.rpa_trail[-1]
        assert "selector_cache #cached" in trail["strategy"]
        assert SelectorCache("demo.example.com", base_dir=local_tmp_path).lookup("click_text", "下一页")["hits"] == 1

    def test_cache_miss_uses_funnel_and_writes_back(self, local_tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("VSPIDER_SELECTOR_CACHE_DIR", str(local_tmp_path))
        funnel = _StubLocator(count=1, text="下一页", derived="#derived-btn")
        page = _StubPage(funnel=funnel)
        ctx = _make_ctx("下一页", page)

        self._run(ctx)

        assert funnel.click_calls == 1
        entry = SelectorCache("demo.example.com", base_dir=local_tmp_path).lookup("click_text", "下一页")
        assert entry is not None
        assert entry["selector"] == "#derived-btn"
        assert "selector_cache" not in ctx.browser.rpa_trail[-1]["strategy"]

    def test_stale_cache_invalidated_then_funnel_restores(self, local_tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("VSPIDER_SELECTOR_CACHE_DIR", str(local_tmp_path))
        SelectorCache("demo.example.com", base_dir=local_tmp_path).store("click_text", "下一页", "#cached")

        stale = _StubLocator(text="别的按钮")  # fingerprint mismatch
        funnel = _StubLocator(count=1, text="下一页", derived="#fresh")
        page = _StubPage(cached=stale, funnel=funnel)
        page._cached_sel = "#cached"
        ctx = _make_ctx("下一页", page)

        self._run(ctx)

        assert stale.click_calls == 0
        assert funnel.click_calls == 1
        entry = SelectorCache("demo.example.com", base_dir=local_tmp_path).lookup("click_text", "下一页")
        assert entry["selector"] == "#fresh"

    def test_cache_disabled_via_env(self, local_tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("VSPIDER_SELECTOR_CACHE_DIR", str(local_tmp_path))
        monkeypatch.setenv("VSPIDER_SELECTOR_CACHE", "0")
        SelectorCache("demo.example.com", base_dir=local_tmp_path).store("click_text", "下一页", "#cached")

        cached = _StubLocator(text="下一页")
        funnel = _StubLocator(count=1, text="下一页")
        page = _StubPage(cached=cached, funnel=funnel)
        page._cached_sel = "#cached"
        ctx = _make_ctx("下一页", page)

        self._run(ctx)

        assert cached.click_calls == 0
        assert funnel.click_calls == 1


# ════════════════════════════════════════════════════════════════════
#                       SOM CACHE KEY & VALIDATE TARGET
# ════════════════════════════════════════════════════════════════════


class TestSomCacheKey:
    def test_extracts_role_and_name(self) -> None:
        som = [{"id": 1, "role": "button", "name": "Submit"}, {"id": 2, "role": "link", "name": "Home"}]
        key = som_cache_key(som, 1)
        assert "button" in key
        assert "submit" in key

    def test_missing_target_returns_empty(self) -> None:
        som = [{"id": 1, "role": "button", "name": "Submit"}]
        assert som_cache_key(som, 99) == ""

    def test_empty_role_and_name_returns_empty(self) -> None:
        som = [{"id": 1, "role": "", "name": ""}]
        assert som_cache_key(som, 1) == ""

    def test_normalises_key(self) -> None:
        som = [{"id": 3, "role": "TEXTBOX", "name": "  User Name  "}]
        key = som_cache_key(som, 3)
        assert key == normalize_key("TEXTBOX::  User Name  ")


class TestValidateCachedTarget:
    def test_valid_visible_element_with_matching_text(self) -> None:
        loc = _StubLocator(count=1, visible=True, text="Submit Form")
        page = _StubPage(cached=loc)
        page._cached_sel = "#btn"
        result = asyncio.run(validate_cached_target(page, {"selector": "#btn"}, "submit"))
        assert result is not None

    def test_invisible_element_rejected(self) -> None:
        loc = _StubLocator(count=1, visible=False, text="Submit")
        page = _StubPage(cached=loc)
        page._cached_sel = "#btn"
        result = asyncio.run(validate_cached_target(page, {"selector": "#btn"}, "submit"))
        assert result is None

    def test_text_mismatch_rejected(self) -> None:
        loc = _StubLocator(count=1, visible=True, text="Cancel")
        page = _StubPage(cached=loc)
        page._cached_sel = "#btn"
        result = asyncio.run(validate_cached_target(page, {"selector": "#btn"}, "submit"))
        assert result is None

    def test_empty_name_accepts_any_text(self) -> None:
        loc = _StubLocator(count=1, visible=True, text="anything")
        page = _StubPage(cached=loc)
        page._cached_sel = "#btn"
        result = asyncio.run(validate_cached_target(page, {"selector": "#btn"}, ""))
        assert result is not None

    def test_zero_count_rejected(self) -> None:
        loc = _StubLocator(count=0, visible=True, text="Submit")
        page = _StubPage(cached=loc)
        page._cached_sel = "#btn"
        result = asyncio.run(validate_cached_target(page, {"selector": "#btn"}, "submit"))
        assert result is None

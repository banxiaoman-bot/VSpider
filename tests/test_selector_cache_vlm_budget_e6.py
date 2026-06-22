"""Tests for E4 (selector_action_cache), E5 (vlm_budget), E6 (api_replay routing)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time

import pytest

# ---------------------------------------------------------------------------
# E4: selector_action_cache
# ---------------------------------------------------------------------------

from visual_web_agent.selector_action_cache import (
    SelectorActionCache,
    get_cache,
    selector_fingerprint,
    _path_pattern,
)


class TestSelectorFingerprint:
    def test_stable(self):
        fp1 = selector_fingerprint("#name", "input", "textbox", "Name")
        fp2 = selector_fingerprint("#name", "input", "textbox", "Name")
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_varies_on_selector(self):
        fp_a = selector_fingerprint("#name", "input")
        fp_b = selector_fingerprint("#email", "input")
        assert fp_a != fp_b

    def test_empty_fields(self):
        fp = selector_fingerprint("", "", "", "")
        assert len(fp) == 16


class TestPathPattern:
    def test_strips_query(self):
        assert _path_pattern("https://example.com/a/b?x=1") == "/a/b"

    def test_root(self):
        assert _path_pattern("https://example.com") == "/"

    def test_trailing_slash(self):
        assert _path_pattern("https://example.com/foo/") == "/foo"


class TestSelectorActionCache:
    @pytest.fixture(autouse=True)
    def cache_dir(self, tmp_path):
        self._dir = tmp_path / "sel_cache"
        self._dir.mkdir()
        return self._dir

    def _cache(self, host="example.com"):
        return SelectorActionCache(host, base_dir=self._dir)

    def test_miss_returns_none(self):
        c = self._cache()
        assert c.lookup("https://example.com/form", "#name") is None

    def test_store_then_hit(self):
        c = self._cache()
        action = {"action": "type", "type_value": "hello", "selector": "#name"}
        c.store("https://example.com/form", "#name", action, tag="input", label="Name")
        c.save()

        c2 = SelectorActionCache("example.com", base_dir=self._dir)
        hit = c2.lookup("https://example.com/form?tab=2", "#name", tag="input", label="Name")
        assert hit is not None
        assert hit["action"] == "type"
        assert hit["type_value"] == "hello"

    def test_different_path_misses(self):
        c = self._cache()
        c.store("https://example.com/form-a", "#name", {"action": "type", "selector": "#name"})
        c.save()
        c2 = SelectorActionCache("example.com", base_dir=self._dir)
        assert c2.lookup("https://example.com/form-b", "#name") is None

    def test_stats(self):
        c = self._cache()
        c.store("https://example.com/f", "#a", {"action": "click", "selector": "#a"})
        c.store("https://example.com/f", "#b", {"action": "type", "selector": "#b"})
        assert c.stats()["entries"] == 2

    def test_eviction(self):
        c = SelectorActionCache("example.com", base_dir=self._dir, max_entries=2)
        for i in range(5):
            c.store(
                f"https://example.com/p{i}",
                f"#el{i}",
                {"action": "click", "selector": f"#el{i}"},
            )
        assert c.stats()["entries"] == 2

    def test_age_eviction(self):
        c = self._cache(host="old.com")
        c.store("https://old.com/page", "#x", {"action": "click", "selector": "#x"})
        state = c._load()
        for key in state["entries"]:
            state["entries"][key]["ts"] = time.time() - 999_999
        c.save()
        c2 = SelectorActionCache("old.com", base_dir=self._dir, max_age_s=1000)
        assert c2.lookup("https://old.com/page", "#x") is None

    def test_clear(self):
        c = self._cache()
        c.store("https://example.com/f", "#a", {"action": "click", "selector": "#a"})
        c.save()
        c.clear()
        assert c.stats()["entries"] == 0

    def test_get_cache_factory(self):
        c = get_cache("https://foo.bar.com/page", base_dir=self._dir)
        assert c.host == "foo.bar.com"


# ---------------------------------------------------------------------------
# E5: vlm_budget
# ---------------------------------------------------------------------------

from visual_web_agent.vlm_budget import VlmBudget, BudgetConfig, VlmBudgetExhausted


class TestVlmBudget:
    def test_record_increments(self):
        b = VlmBudget()
        b.record(step=1, prompt_tokens=100, completion_tokens=50, latency_ms=200)
        assert b.total_calls == 1
        assert b.total_tokens == 150
        assert b.total_latency_ms == 200

    def test_multiple_records(self):
        b = VlmBudget()
        b.record(step=1, prompt_tokens=100, completion_tokens=50)
        b.record(step=2, prompt_tokens=200, completion_tokens=80)
        assert b.total_calls == 2
        assert b.total_prompt_tokens == 300
        assert b.total_completion_tokens == 130
        assert b.total_tokens == 430

    def test_hard_limit_calls(self):
        b = VlmBudget(config=BudgetConfig(max_calls=3))
        b.record(step=1, prompt_tokens=10, completion_tokens=5)
        b.record(step=2, prompt_tokens=10, completion_tokens=5)
        with pytest.raises(VlmBudgetExhausted, match="call budget"):
            b.record(step=3, prompt_tokens=10, completion_tokens=5)

    def test_hard_limit_tokens(self):
        b = VlmBudget(config=BudgetConfig(max_tokens=200))
        b.record(step=1, prompt_tokens=100, completion_tokens=50)
        with pytest.raises(VlmBudgetExhausted, match="token budget"):
            b.record(step=2, prompt_tokens=100, completion_tokens=50)

    def test_no_limit(self):
        b = VlmBudget(config=BudgetConfig(max_calls=0, max_tokens=0))
        for i in range(100):
            b.record(step=i, prompt_tokens=1000, completion_tokens=500)
        assert b.total_calls == 100

    def test_remaining(self):
        b = VlmBudget(config=BudgetConfig(max_calls=10, max_tokens=5000))
        b.record(step=1, prompt_tokens=100, completion_tokens=50)
        assert b.remaining_calls() == 9
        assert b.remaining_tokens() == 4850

    def test_remaining_unlimited(self):
        b = VlmBudget()
        assert b.remaining_calls() is None
        assert b.remaining_tokens() is None

    def test_summary(self):
        b = VlmBudget(config=BudgetConfig(max_calls=5))
        b.record(step=1, prompt_tokens=200, completion_tokens=100, latency_ms=500)
        s = b.summary()
        assert s["total_calls"] == 1
        assert s["total_tokens"] == 300
        assert s["avg_latency_ms"] == 500
        assert s["budget_max_calls"] == 5
        assert s["remaining_calls"] == 4

    def test_step_log(self):
        b = VlmBudget()
        b.record(step=1, prompt_tokens=10, completion_tokens=5, latency_ms=100)
        b.record(step=2, prompt_tokens=20, completion_tokens=10, latency_ms=200)
        log = b.step_log()
        assert len(log) == 2
        assert log[0]["step"] == 1
        assert log[1]["prompt_tokens"] == 20

    def test_soft_warning(self):
        b = VlmBudget(config=BudgetConfig(max_calls=10, soft_ratio=0.5))
        for i in range(4):
            b.record(step=i, prompt_tokens=10, completion_tokens=5)
        assert not b._soft_warned_calls
        b.record(step=5, prompt_tokens=10, completion_tokens=5)
        assert b._soft_warned_calls


# ---------------------------------------------------------------------------
# E6: api_replay routing priority / selector cache signal
# ---------------------------------------------------------------------------

from visual_web_agent.capability_router import route_task


class TestE6Routing:
    def test_api_replay_in_fallback_for_api_goal(self):
        result = route_task("通过 API 接口获取商品 JSON 数据", url="https://example.com/products")
        chain = result.get("fallback_chain", [])
        cap_names = [s.get("capability") for s in chain]
        assert "api_replay" in cap_names
        assert "network_intelligence" in cap_names

    def test_selector_cache_signal_injected(self, tmp_path):
        from visual_web_agent.selector_action_cache import SelectorActionCache

        cache = SelectorActionCache("test-host.com", base_dir=tmp_path)
        cache.store(
            "https://test-host.com/form",
            "#name",
            {"action": "type", "selector": "#name", "type_value": "test"},
        )
        cache.save()
        import visual_web_agent.capability_router as _cr
        orig = _cr._apply_capture_fixture_probe

        def patched(signals, strategy_context, url):
            from visual_web_agent.selector_action_cache import get_cache
            host = "test-host.com"
            try:
                c = get_cache(url, base_dir=tmp_path)
                stats = c.stats()
                if stats.get("entries", 0) > 0:
                    signals["selector_cache_available"] = True
                    strategy_context["selector_action_cache"] = {
                        "host": host,
                        "cached_entries": stats["entries"],
                    }
            except Exception:
                pass

        _cr._apply_capture_fixture_probe = patched
        try:
            result = route_task(
                "填写注册表单",
                url="https://test-host.com/form",
            )
            chain = result.get("fallback_chain", [])
            cap_names = [s.get("capability") for s in chain]
            assert "selector_action_cache" in cap_names
        finally:
            _cr._apply_capture_fixture_probe = orig

    def test_signals_include_form(self):
        result = route_task("填写表单提交注册信息", url="https://example.com/register")
        signals = result.get("signals", {})
        assert signals.get("form") or signals.get("browser_interaction")

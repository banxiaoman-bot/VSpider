"""Unit tests for concurrency config and batch orchestrator (no Playwright)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pandas as pd
import pytest

from visual_web_agent import concurrency_config as cc
from visual_web_agent import batch_orchestrator as bo
from visual_web_agent.browser_pool import (
    BrowserPool,
    apply_pool_capacity_for_workload,
    resolve_browser_pool_config,
)


class TestConcurrencyConfig:
    def test_collect_start_urls_dedupes(self) -> None:
        urls = cc.collect_start_urls(
            "https://a.test",
            ["https://b.test", "https://a.test"],
        )
        assert urls == ["https://a.test", "https://b.test"]

    def test_batch_row_concurrency_respects_max_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VSPIDER_BATCH_ROW_CONCURRENCY", "8")
        assert cc.resolve_batch_row_concurrency({"max_runs": 3}) == 3

    def test_start_url_concurrency_default_serial(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VSPIDER_START_URL_CONCURRENCY", raising=False)
        assert cc.resolve_start_url_concurrency() == 1


class _FakeBrowser:
    async def close(self) -> None:
        return None


class TestBrowserPoolWorkload:
    def test_workload_contexts_enables_parallel_without_experimental(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VSPIDER_BROWSER_MAX_CONTEXTS", "4")
        monkeypatch.delenv("VSPIDER_EXPERIMENTAL_PARALLEL_RUNS", raising=False)

        config = resolve_browser_pool_config(workload_contexts=3)

        assert config["effective_max_contexts"] == 3
        assert config["parallel_enabled"] is True

    def test_apply_pool_capacity_updates_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VSPIDER_BROWSER_MAX_CONTEXTS", "5")
        monkeypatch.delenv("VSPIDER_EXPERIMENTAL_PARALLEL_RUNS", raising=False)

        applied = apply_pool_capacity_for_workload(4)

        assert applied == 4
        from visual_web_agent.browser_pool import get_browser_pool

        status = get_browser_pool().status()
        assert status["max_contexts"] == 4
        assert status["parallel_enabled"] is True


class TestBatchOrchestrator:
    def test_start_urls_serial_when_concurrency_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VSPIDER_START_URL_CONCURRENCY", "1")
        calls: list[str] = []

        async def fake_run(**kwargs: Any) -> bool:
            calls.append(str(kwargs.get("start_url")))
            return True

        ok, total = asyncio.run(
            bo.run_start_urls_parallel(
                target_url="https://a.test",
                urls=["https://b.test"],
                goal="g",
                run_agent=fake_run,
                stop_event=None,
                auth_profiles=None,
                vlm_options=None,
                emit_log=lambda *_a, **_k: None,
            )
        )

        assert total == 2
        assert ok == 2
        assert calls == ["https://a.test", "https://b.test"]

    def test_start_urls_parallel_when_concurrency_gt_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VSPIDER_START_URL_CONCURRENCY", "2")
        monkeypatch.setenv("VSPIDER_BROWSER_MAX_CONTEXTS", "2")
        calls: list[str] = []

        async def fake_run(**kwargs: Any) -> bool:
            calls.append(str(kwargs.get("start_url")))
            await asyncio.sleep(0.01)
            return True

        ok, total = asyncio.run(
            bo.run_start_urls_parallel(
                target_url="https://a.test",
                urls=["https://b.test"],
                goal="g",
                run_agent=fake_run,
                stop_event=None,
                auth_profiles=None,
                vlm_options=None,
                emit_log=lambda *_a, **_k: None,
            )
        )

        assert total == 2
        assert ok == 2
        assert sorted(calls) == ["https://a.test", "https://b.test"]

    def test_batch_rows_parallel_respects_concurrency(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VSPIDER_BATCH_ROW_CONCURRENCY", "2")
        monkeypatch.setenv("VSPIDER_BROWSER_MAX_CONTEXTS", "2")
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def fake_run(**_kwargs: Any) -> bool:
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.05)
            async with lock:
                active -= 1
            return True

        df = pd.DataFrame({"项目编号": ["a", "b", "c"], "填报状态": ["", "", ""]})
        saved: list[int] = []

        ok, total = asyncio.run(
            bo.run_batch_rows_parallel(
                df=df,
                prompt="do {{项目编号}}",
                target_url="https://x.test",
                run_agent=fake_run,
                stop_event=None,
                auth_profiles=None,
                vlm_options=None,
                render_goal=lambda p, row: f"{p}-{row['项目编号']}",
                normalize_row=lambda row: {k: str(v) for k, v in row.items()},
                save_progress=lambda: saved.append(1),
                emit_log=lambda *_a, **_k: None,
            )
        )

        assert total == 3
        assert ok == 3
        assert peak <= 2
        assert len(saved) >= 3

    def test_batch_rows_honors_stop_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VSPIDER_BATCH_ROW_CONCURRENCY", "1")
        stop = threading.Event()
        stop.set()
        df = pd.DataFrame({"项目编号": ["a"], "填报状态": [""]})

        ok, total = asyncio.run(
            bo.run_batch_rows_parallel(
                df=df,
                prompt="x",
                target_url="https://x.test",
                run_agent=asyncio.sleep,  # type: ignore[arg-type]
                stop_event=stop,
                auth_profiles=None,
                vlm_options=None,
                render_goal=lambda p, _row: p,
                normalize_row=lambda row: dict(row),
                save_progress=lambda: None,
                emit_log=lambda *_a, **_k: None,
            )
        )

        assert total == 1
        assert ok == 0

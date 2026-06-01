"""Parallel orchestration for batch rows and multi start-URL runs."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Awaitable, Callable

import pandas as pd

from visual_web_agent.concurrency_config import (
    collect_start_urls,
    resolve_batch_row_concurrency,
    resolve_start_url_concurrency,
)


RunAgentFn = Callable[..., Awaitable[bool]]
EmitLogFn = Callable[[str, str], None]
SaveProgressFn = Callable[[], None]


async def run_start_urls_parallel(
    *,
    target_url: str,
    urls: list[str] | None,
    goal: str,
    run_agent: RunAgentFn,
    stop_event: threading.Event | None,
    auth_profiles: str | None,
    vlm_options: dict | None,
    upload_file: str = "",
    constraints: dict[str, Any] | None = None,
    emit_log: EmitLogFn,
) -> tuple[int, int]:
    """Run one agent per start URL (up to concurrency cap). Returns (ok, total)."""

    start_urls = collect_start_urls(target_url, urls)
    if len(start_urls) <= 1:
        ok = await run_agent(
            start_url=start_urls[0] if start_urls else target_url,
            goal=goal,
            stop_event=stop_event,
            auth_profiles=auth_profiles,
            vlm_options=vlm_options,
            upload_file=upload_file or "",
        )
        return (1 if ok else 0, 1)

    concurrency = min(resolve_start_url_concurrency(constraints), len(start_urls))
    from visual_web_agent.browser_pool import apply_pool_capacity_for_workload

    applied = apply_pool_capacity_for_workload(concurrency)
    if applied < concurrency:
        emit_log(
            f"⚠️ [Batch Orchestrator] start-url 并发 {concurrency} 受浏览器池限制，"
            f"实际 {applied}",
            "warn",
        )
        concurrency = applied

    emit_log(
        f"🔀 [Batch Orchestrator] 多起点并行 | urls={len(start_urls)} | "
        f"concurrency={concurrency}",
        "info",
    )

    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[bool] = []

    async def _one(index: int, url: str) -> None:
        if stop_event and stop_event.is_set():
            results.append(False)
            return
        async with sem:
            if stop_event and stop_event.is_set():
                results.append(False)
                return
            emit_log(f"▶️ [子 run {index + 1}/{len(start_urls)}] {url}", "info")
            try:
                ok = await run_agent(
                    start_url=url,
                    goal=goal,
                    stop_event=stop_event,
                    auth_profiles=auth_profiles,
                    vlm_options=vlm_options,
                    upload_file=upload_file or "",
                )
                results.append(bool(ok))
                level = "info" if ok else "error"
                emit_log(
                    f"{'✅' if ok else '❌'} [子 run {index + 1}] 完成",
                    level,
                )
            except Exception as exc:
                results.append(False)
                emit_log(f"❌ [子 run {index + 1}] 异常: {exc}", "error")

    await asyncio.gather(*[_one(i, u) for i, u in enumerate(start_urls)])
    success = sum(1 for item in results if item)
    return success, len(start_urls)


async def run_batch_rows_parallel(
    *,
    df: pd.DataFrame,
    prompt: str,
    target_url: str,
    run_agent: RunAgentFn,
    stop_event: threading.Event | None,
    auth_profiles: str | None,
    vlm_options: dict | None,
    render_goal: Callable[[str, dict[str, str]], str],
    normalize_row: Callable[[pd.Series], dict[str, str]],
    save_progress: SaveProgressFn,
    emit_log: EmitLogFn,
    constraints: dict[str, Any] | None = None,
    inter_row_sleep_s: float = 0.0,
) -> tuple[int, int]:
    """Process spreadsheet rows with optional parallelism. Returns (ok, total)."""

    total = len(df)
    pending: list[tuple[Any, int]] = []
    for index, row in df.iterrows():
        row_no = int(index) + 1 if isinstance(index, (int, float)) else len(pending) + 1
        status = str(row.get("填报状态", "")).strip()
        if status == "成功":
            emit_log(f"⏭️ 第 {row_no} 行已成功，跳过", "info")
            continue
        pending.append((index, row_no))

    if not pending:
        return total, total

    concurrency = resolve_batch_row_concurrency(constraints)
    if concurrency <= 1:
        success_count = 0
        for index, row_no in pending:
            if stop_event and stop_event.is_set():
                emit_log("⏹️ [Batch Runner] 收到停止信号，结束批处理循环", "warn")
                break
            row = df.loc[index]
            project_no = str(row.get("项目编号", "未知")).strip() or "未知"
            goal = render_goal(prompt, normalize_row(row))
            emit_log("=" * 48, "info")
            emit_log(
                f"▶️ 开始处理第 {row_no}/{total} 行 | 项目编号={project_no}",
                "info",
            )
            try:
                ok = await run_agent(
                    start_url=target_url,
                    goal=goal,
                    stop_event=stop_event,
                    auth_profiles=auth_profiles,
                    vlm_options=vlm_options,
                )
                if ok:
                    df.at[index, "填报状态"] = "成功"
                    df.at[index, "日志备注"] = "Agent 执行完成"
                    success_count += 1
                    emit_log(f"✅ 第 {row_no} 行处理成功", "info")
                else:
                    df.at[index, "填报状态"] = "失败"
                    df.at[index, "日志备注"] = "Agent 返回失败状态"
                    emit_log(f"❌ 第 {row_no} 行处理失败", "error")
            except Exception as exc:
                df.at[index, "填报状态"] = "失败"
                df.at[index, "日志备注"] = f"异常: {str(exc)[:200]}"
                emit_log(f"❌ 第 {row_no} 行处理失败: {exc}", "error")
            finally:
                save_progress()
                if inter_row_sleep_s > 0:
                    await asyncio.sleep(inter_row_sleep_s)
        already_ok = total - len(pending)
        return already_ok + success_count, total

    from visual_web_agent.browser_pool import apply_pool_capacity_for_workload

    applied = apply_pool_capacity_for_workload(concurrency)
    if applied < concurrency:
        emit_log(
            f"⚠️ [Batch Orchestrator] 行级并发 {concurrency} 受浏览器池限制，实际 {applied}",
            "warn",
        )
        concurrency = applied

    emit_log(
        f"🔀 [Batch Orchestrator] 行级并行 | pending={len(pending)} | "
        f"concurrency={concurrency}",
        "info",
    )

    sem = asyncio.Semaphore(max(1, concurrency))
    lock = asyncio.Lock()
    success_count = 0
    already_ok = total - len(pending)

    async def _one(index: Any, row_no: int) -> None:
        nonlocal success_count
        if stop_event and stop_event.is_set():
            return
        row = df.loc[index]
        project_no = str(row.get("项目编号", "未知")).strip() or "未知"
        goal = render_goal(prompt, normalize_row(row))
        async with sem:
            if stop_event and stop_event.is_set():
                return
            emit_log(
                f"▶️ 开始处理第 {row_no}/{total} 行 | 项目编号={project_no}",
                "info",
            )
            try:
                ok = await run_agent(
                    start_url=target_url,
                    goal=goal,
                    stop_event=stop_event,
                    auth_profiles=auth_profiles,
                    vlm_options=vlm_options,
                )
                async with lock:
                    if ok:
                        df.at[index, "填报状态"] = "成功"
                        df.at[index, "日志备注"] = "Agent 执行完成"
                        success_count += 1
                        emit_log(f"✅ 第 {row_no} 行处理成功", "info")
                    else:
                        df.at[index, "填报状态"] = "失败"
                        df.at[index, "日志备注"] = "Agent 返回失败状态"
                        emit_log(f"❌ 第 {row_no} 行处理失败", "error")
                    save_progress()
            except Exception as exc:
                async with lock:
                    df.at[index, "填报状态"] = "失败"
                    df.at[index, "日志备注"] = f"异常: {str(exc)[:200]}"
                    emit_log(f"❌ 第 {row_no} 行处理失败: {exc}", "error")
                    save_progress()

    await asyncio.gather(*[_one(index, row_no) for index, row_no in pending])
    return already_ok + success_count, total

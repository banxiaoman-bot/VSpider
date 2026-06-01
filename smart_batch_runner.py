from __future__ import annotations

import asyncio
import re
import sys
import threading
from pathlib import Path

import pandas as pd

from visual_web_agent.artifact_manager import register_artifact, resolve_artifact_path
from visual_web_agent.io_contract import (
    build_input_contract,
    infer_output_contract,
    write_input_contract,
    write_output_contract,
)

try:
    from api_server import broadcast_done, broadcast_log
except Exception:
    # 允许 CLI 独立运行：没有 API 服务时降级为控制台输出
    def broadcast_log(content: str, level: str = "info") -> None:  # type: ignore[override]
        _ = level
        print(content)

    def broadcast_done(success: bool, message: str = "") -> None:  # type: ignore[override]
        _ = success
        if message:
            print(message)


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _emit_log(message: str, level: str = "info") -> None:
    """统一日志出口：控制台 + WebSocket 广播。"""
    print(message)
    try:
        broadcast_log(message, level=level)
    except Exception:
        pass


def _load_dataframe(file_path: str) -> pd.DataFrame:
    """Legacy loader retained as a safety net for ``batch_rows`` attachments
    when the adapter path is unavailable. Prefer
    :func:`_load_batch_rows_via_adapter`."""

    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix == ".tsv":
        return pd.read_csv(path, sep="\t")
    if suffix in {".xls", ".xlsx", ".xlsm"}:
        return pd.read_excel(path)
    if suffix == ".ods":
        return pd.read_excel(path, engine="odf")
    if suffix == ".parquet":
        return pd.read_parquet(path)
    raise ValueError(f"仅支持 csv/tsv/xls/xlsx/xlsm/ods/parquet，收到: {suffix or '<none>'}")


def _dispatch_attachment(file_path: str, goal: str):
    """Build a transient ``InputContract`` for the single attachment and run
    it through ``adapt_attachment``.

    Returns ``(spec, result)`` where ``spec`` is the inferred
    :class:`AttachmentSpec` and ``result`` is the :class:`AdapterResult`.
    Callers should branch on ``spec.intent`` (which is the authoritative
    routing key) and ``result.kind`` (which describes the produced shape).
    """

    from visual_web_agent.attachment_adapters import adapt_attachment

    fp = Path(file_path)
    sample_bytes = b""
    try:
        if fp.exists():
            with open(fp, "rb") as _fh:
                sample_bytes = _fh.read(4096)
    except Exception:
        sample_bytes = b""

    contract = build_input_contract(
        goal=goal or "",
        attachments=[{
            "path": str(fp),
            "filename": fp.name,
            "size": fp.stat().st_size if fp.exists() else 0,
            "sample_bytes": sample_bytes,
        }],
    )
    spec = contract.attachments[0] if contract.attachments else None
    if spec is None:
        from visual_web_agent.attachment_adapters.base import AdapterResult

        empty = AdapterResult(kind="unknown", ok=False, reasons=["no_attachment_spec"])
        return None, empty
    result = adapt_attachment(spec, goal=goal or "")
    return spec, result


def _load_batch_rows_via_adapter(file_path: str, goal: str) -> pd.DataFrame:
    """Load a tabular attachment as a DataFrame via ``adapt_attachment``.

    Falls back to :func:`_load_dataframe` when the adapter returns no rows,
    which preserves the legacy crash-on-unsupported-suffix behaviour rather
    than silently emitting an empty DataFrame.
    """

    spec, result = _dispatch_attachment(file_path, goal)
    if spec is None or spec.intent != "batch_rows" or not result.ok or result.kind != "rows":
        return _load_dataframe(file_path)
    rows = list(result.rows or [])
    if not rows:
        return _load_dataframe(file_path)
    return pd.DataFrame(rows)


def _augment_prompt_with_attachment(prompt: str, result) -> str:
    """Stitch an adapter result into the prompt for non-``batch_rows`` runs.

    The agent receives the original user goal plus a ``【附件】`` block whose
    contents depend on the adapter kind (text excerpt, caption, file path,
    media refs ...). Empty additions return ``prompt`` unchanged.
    """

    block_lines: list[str] = []
    if getattr(result, "kind", "") == "upload":
        upload_path = getattr(result, "upload_path", "") or ""
        meta = getattr(result, "metadata", {}) or {}
        filename = str(meta.get("filename") or "")
        block_lines.append(f"附件类型: 上传至页面")
        if filename:
            block_lines.append(f"文件名: {filename}")
        if upload_path:
            block_lines.append(f"本地路径: {upload_path}")
    elif getattr(result, "kind", "") == "media_source":
        meta = getattr(result, "metadata", {}) or {}
        filename = str(meta.get("filename") or "")
        block_lines.append("附件类型: 媒体源")
        if filename:
            block_lines.append(f"文件名: {filename}")
        refs = list(getattr(result, "media_refs", []) or [])
        if refs:
            block_lines.append("引用: " + ", ".join(refs[:5]))
    else:
        text = (getattr(result, "text", "") or "").strip()
        if text:
            block_lines.append(f"附件摘要({getattr(result, 'kind', 'attachment')}):")
            block_lines.append(text)

    if not block_lines:
        return prompt
    block = "\n".join(block_lines)
    return f"{prompt}\n\n【附件】\n{block}"


def _normalize_row(row: pd.Series) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in row.items():
        normalized_key = str(key).strip()
        if pd.isna(value):
            cleaned[normalized_key] = ""
        else:
            cleaned[normalized_key] = str(value).strip()
    return cleaned


def _render_goal(prompt: str, row_data: dict[str, str]) -> str:
    """
    渲染每一行的动态目标：
    - 如果 prompt 含有 {{列名}} 占位符，则按列值替换
    - 如果不含占位符，则在 prompt 后追加当前行的结构化数据
    """

    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return row_data.get(key, "")

    rendered = _PLACEHOLDER_RE.sub(repl, prompt)
    if _PLACEHOLDER_RE.search(prompt):
        return rendered

    detail_lines = [f"- {k}: {v}" for k, v in row_data.items() if v]
    if not detail_lines:
        return prompt

    details = "\n".join(detail_lines)
    return f"{prompt}\n\n【当前数据行】\n{details}"


def _run_single_goal(prompt: str) -> str:
    """无文件时的单任务模式，直接使用原始 prompt。"""
    return prompt.strip()


def _persist_io_contracts_safe(
    *,
    run_id: str,
    goal: str,
    target_url: str,
    urls: list[str],
    file_path: str | None,
    auth_profiles: str,
    vlm_options: dict | None,
    run_constraints: dict | None = None,
) -> None:
    """Persist ``input_contract.json`` + ``output_contract.json`` for the run.

    Best-effort: any failure here is logged and swallowed so legacy callers
    that never set ``run_id`` keep working.
    """

    if not run_id:
        return
    try:
        attachments: list[dict] = []
        if file_path:
            fp = Path(file_path)
            attachments.append({
                "path": str(fp),
                "filename": fp.name,
                "size": fp.stat().st_size if fp.exists() else 0,
            })

        contract = build_input_contract(
            goal=goal or "",
            target_url=target_url or "",
            urls=urls or [],
            attachments=attachments,
            auth_profiles=auth_profiles or "",
            vlm_options=vlm_options or {},
            constraints=run_constraints or None,
            source="api",
        )
        write_input_contract(run_id, contract)

        oc = infer_output_contract(goal or "")
        write_output_contract(run_id, oc)
        _emit_log(
            f"📐 [IO Contract] input_kind={[u.role for u in contract.urls]} "
            f"attachments={[a.intent for a in contract.attachments]} "
            f"output_kind={oc.output_kind} container={oc.container}"
        )
    except Exception as exc:
        _emit_log(f"⚠️ [IO Contract] persist failed: {exc}", level="warn")


async def run_smart_batch(
    target_url: str,
    prompt: str,
    file_path: str | None,
    stop_event: threading.Event | None = None,
    auth_profiles: str = "",
    vlm_options: dict | None = None,
    *,
    run_id: str = "",
    urls: list[str] | None = None,
    run_constraints: dict | None = None,
) -> bool:
    """
    批处理入口（供 FastAPI BackgroundTasks 调用）。

    Args:
        target_url: 前端传入目标 URL
        prompt: 前端传入任务提示词（支持 {{列名}} 占位符）
        file_path: 上传文件的落盘路径
        stop_event: 停止信号
        run_id: 当前 run 的 ID；非空时会把 input/output contract 落盘到
            ``runs/<run_id>/`` 便于复现与审计。
        urls: 多 URL 任务的可选额外起点（``target_url`` 始终是第一个）。
    """
    from visual_web_agent import main as agent_main

    _persist_io_contracts_safe(
        run_id=run_id,
        goal=prompt,
        target_url=target_url,
        urls=urls or [],
        file_path=file_path,
        auth_profiles=auth_profiles,
        vlm_options=vlm_options,
        run_constraints=run_constraints,
    )

    attachment_spec = None
    attachment_result = None
    attachment_intent = ""
    if file_path:
        try:
            attachment_spec, attachment_result = _dispatch_attachment(file_path, prompt or "")
            attachment_intent = (
                attachment_spec.intent if attachment_spec is not None else ""
            )
        except Exception as exc:
            _emit_log(f"⚠️ [Batch Runner] 附件 dispatcher 失败，将回退到批处理: {exc}", level="warn")
            attachment_spec = None
            attachment_result = None
            attachment_intent = "batch_rows"
        if attachment_intent == "batch_rows":
            _emit_log(f"🚀 [Batch Runner] 启动批量引擎 | file={file_path}")
        else:
            kind = getattr(attachment_result, "kind", "?") if attachment_result else "?"
            _emit_log(
                f"🚀 [Batch Runner] 启动单任务（附件） | file={file_path} | "
                f"intent={attachment_intent or 'unknown'} | adapter={kind}"
            )
    else:
        _emit_log("🚀 [Batch Runner] 启动单任务引擎 | no file attached")
    _emit_log(f"🌐 [Batch Runner] target_url={target_url}")

    # ── Branch A: no file OR attachment is not batch-rows ────────────────
    if not file_path or attachment_intent != "batch_rows":
        success = False
        try:
            from visual_web_agent.batch_orchestrator import run_start_urls_parallel

            base_goal = _run_single_goal(prompt)
            augmented_goal = base_goal
            upload_path_for_agent = ""
            if attachment_result is not None:
                augmented_goal = _augment_prompt_with_attachment(base_goal, attachment_result)
                if attachment_intent == "upload_to_page":
                    upload_path_for_agent = (
                        getattr(attachment_result, "upload_path", "")
                        or (file_path or "")
                    )

            async def _run_one(**kwargs: object) -> bool:
                kwargs.setdefault("vlm_options", vlm_options or None)
                kwargs.setdefault("run_constraints", run_constraints)
                return await agent_main.run_agent(**kwargs)

            ok_count, total_runs = await run_start_urls_parallel(
                target_url=target_url,
                urls=urls,
                goal=augmented_goal,
                run_agent=_run_one,
                stop_event=stop_event,
                auth_profiles=auth_profiles or None,
                vlm_options=vlm_options or None,
                upload_file=upload_path_for_agent or "",
                constraints=run_constraints,
                emit_log=lambda msg, level="info": _emit_log(msg, level=level),
            )
            success = ok_count == total_runs and total_runs > 0
            if stop_event and stop_event.is_set():
                summary = "⏹️ 单任务已终止"
            elif success:
                summary = "🎉 单任务执行完成"
            else:
                summary = "❌ 单任务执行失败"
            _emit_log(summary)
            try:
                agent_done_broadcasted = bool(
                    getattr(agent_main, "_run_done_was_broadcasted", lambda: False)()
                )
                if not agent_done_broadcasted:
                    broadcast_done(
                        bool(success) and not (stop_event and stop_event.is_set()),
                        summary,
                    )
            except Exception:
                pass
        except Exception as exc:
            _emit_log(f"❌ 单任务执行失败: {exc}", level="error")
            try:
                broadcast_done(False, f"单任务执行失败: {exc}")
            except Exception:
                pass
            success = False
        return bool(success) and not (stop_event and stop_event.is_set())

    # ── Branch B: batch_rows attachment ──────────────────────────────────
    try:
        df = _load_batch_rows_via_adapter(file_path, prompt or "")
    except Exception as exc:
        _emit_log(f"❌ 读取文件失败: {exc}", level="error")
        try:
            broadcast_done(False, f"批处理启动失败: {exc}")
        except Exception:
            pass
        return False

    if "填报状态" not in df.columns:
        df["填报状态"] = "未处理"
    if "日志备注" not in df.columns:
        df["日志备注"] = ""

    total = len(df)
    output_file = str(resolve_artifact_path(f"{Path(file_path).stem}_处理结果.xlsx", subdir="batch"))
    _emit_log(f"📫 已载入 {total} 条记录，结果文件: {output_file}")

    def _save_progress() -> None:
        try:
            df.to_excel(output_file, index=False)
            register_artifact(output_file)
            _emit_log(f"💾 已保存进度: {output_file}")
        except Exception as save_exc:
            _emit_log(f"⚠️ 保存进度失败: {save_exc}", level="warn")

    from visual_web_agent.batch_orchestrator import run_batch_rows_parallel

    async def _run_row(**kwargs: object) -> bool:
        return await agent_main.run_agent(
            **kwargs,
            auth_profiles=auth_profiles or None,
            vlm_options=vlm_options or None,
            run_constraints=run_constraints,
        )

    success_count, _ = await run_batch_rows_parallel(
        df=df,
        prompt=prompt,
        target_url=target_url,
        run_agent=_run_row,
        stop_event=stop_event,
        auth_profiles=auth_profiles or None,
        vlm_options=vlm_options or None,
        render_goal=_render_goal,
        normalize_row=_normalize_row,
        save_progress=_save_progress,
        emit_log=lambda msg, level="info": _emit_log(msg, level=level),
        constraints=run_constraints,
        inter_row_sleep_s=1.0,
    )

    if stop_event and stop_event.is_set():
        summary = f"⏹️ 批量任务已终止：成功 {success_count}/{total}"
    else:
        summary = f"🎉 批量任务结束：成功 {success_count}/{total}"
    _emit_log(summary)
    try:
        broadcast_done(not (stop_event and stop_event.is_set()), summary)
    except Exception:
        pass
    return success_count == total and not (stop_event and stop_event.is_set())


def run_smart_batch_sync(
    target_url: str,
    prompt: str,
    file_path: str,
    stop_event: threading.Event | None = None,
    auth_profiles: str = "",
    vlm_options: dict | None = None,
    *,
    run_id: str = "",
    urls: list[str] | None = None,
    run_constraints: dict | None = None,
) -> bool:
    """CLI 同步入口。

    ``run_id`` / ``urls`` 是新加的可选 kwargs：当从 ``api_server`` 调用时携带
    任务 ID 与多 URL 列表，让 :func:`run_smart_batch` 把 input/output
    contract 落盘到 ``runs/<run_id>/``。CLI 直接调用时省略即可，行为与
    旧版本完全兼容。
    """

    if sys.platform == "win32" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass
    return asyncio.run(
        run_smart_batch(
            target_url,
            prompt,
            file_path,
            stop_event=stop_event,
            auth_profiles=auth_profiles,
            vlm_options=vlm_options or None,
            run_id=run_id,
            urls=urls,
            run_constraints=run_constraints,
        )
    )


if __name__ == "__main__":
    # 本地调试示例
    run_smart_batch_sync(
        target_url="https://your-audit-system.com/form",
        prompt="请根据当前数据行填写表单，提交后完成任务。",
        file_path="audit_data.xlsx",
    )

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
import threading
from pathlib import Path

import pandas as pd

from visual_web_agent.artifact_manager import register_artifact, resolve_artifact_path
from visual_web_agent.io_contract import (
    append_manifest_item,
    build_input_contract,
    ensure_contract_skeleton,
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

# Batch-row resume (BATCH-RESUME1): the orchestrator skips rows already marked
# done; these drive carrying a prior run's progress back into a fresh DataFrame.
_RESUME_DONE_STATUS = "成功"
_URL_KEY_CANDIDATES = ("url", "链接", "网址", "链接地址", "link", "链接url")
_STATUS_COLS = ("填报状态", "日志备注")


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


def _dispatch_attachment(file_path: str, goal: str, intent_override: str = ""):
    """Build a transient ``InputContract`` for the single attachment and run
    it through ``adapt_attachment``.

    ``intent_override`` is the user's explicit intent from the API layer; a
    valid value wins over inference (``build_input_contract`` falls back to
    inference for empty/invalid values, so this stays backward compatible).

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
            "intent": str(intent_override or "").strip(),
        }],
    )
    spec = contract.attachments[0] if contract.attachments else None
    if spec is None:
        from visual_web_agent.attachment_adapters.base import AdapterResult

        empty = AdapterResult(kind="unknown", ok=False, reasons=["no_attachment_spec"])
        return None, empty
    result = adapt_attachment(spec, goal=goal or "")
    return spec, result


def _load_batch_rows_via_adapter(
    file_path: str, goal: str, intent_override: str = ""
) -> pd.DataFrame:
    """Load a tabular attachment as a DataFrame via ``adapt_attachment``.

    Falls back to :func:`_load_dataframe` when the adapter returns no rows,
    which preserves the legacy crash-on-unsupported-suffix behaviour rather
    than silently emitting an empty DataFrame.
    """

    spec, result = _dispatch_attachment(file_path, goal, intent_override=intent_override)
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


def _row_resume_key(df: pd.DataFrame) -> str:
    """Pick a stable per-row key column for resume matching.

    Prefers a URL-like column (case-insensitive); returns ``""`` when none
    exists, in which case the caller falls back to positional (row-order)
    matching against the prior result file.
    """
    lower = {str(col).strip().lower(): col for col in df.columns}
    for candidate in _URL_KEY_CANDIDATES:
        if candidate in lower:
            return str(lower[candidate])
    return ""


def _content_columns(df: pd.DataFrame) -> list:
    """Data columns used for content-hash row keys (excludes status / log cols)."""
    return [c for c in df.columns if str(c).strip() not in _STATUS_COLS]


def _row_content_hash(row, columns: list) -> str:
    """Stable per-row content fingerprint over ``columns`` (NaN / blank -> '').

    An order-independent resume key for tables without a URL-like column
    (BATCH-RESUME2): two rows with identical data hash the same, so reordered /
    inserted rows still match a prior run instead of falling back to fragile
    positional matching.
    """
    parts: list[str] = []
    for col in columns:
        val = row.get(col, "")
        try:
            blank = bool(pd.isna(val))
        except (TypeError, ValueError):
            blank = False
        if blank or val is None:
            sval = ""
        else:
            sval = str(val).strip()
            if sval.lower() in ("nan", "none", "nat"):
                sval = ""
        parts.append(sval)
    raw = "\x1f".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _merge_prior_progress(
    df: pd.DataFrame,
    prior_df: pd.DataFrame,
    *,
    key_col: str = "",
) -> tuple[pd.DataFrame, int]:
    """Carry a prior run's *successful* rows into ``df`` so the orchestrator's
    existing skip-on-``成功`` logic resumes instead of re-running them.

    Only ``填报状态 == "成功"`` is carried — failed / unprocessed rows are left
    untouched so they get retried. Matching is by ``key_col`` when it is present
    in both frames (robust to row reorder / inserted rows); otherwise it falls
    back to positional (row-order) matching. Rows already ``成功`` in ``df`` are
    left as-is and not re-counted. Returns ``(df, resumed_count)``.
    """
    if prior_df is None or "填报状态" not in getattr(prior_df, "columns", []):
        return df, 0
    if df is None or df.empty:
        return df, 0
    if "填报状态" not in df.columns:
        df["填报状态"] = "未处理"
    if "日志备注" not in df.columns:
        df["日志备注"] = ""

    resumed = 0
    use_key = bool(key_col) and key_col in df.columns and key_col in prior_df.columns
    if use_key:
        prior_has_note = "日志备注" in prior_df.columns
        done: dict[str, str] = {}
        for _, prow in prior_df.iterrows():
            if str(prow.get("填报状态", "")).strip() == _RESUME_DONE_STATUS:
                key = str(prow.get(key_col, "")).strip()
                if key:
                    done[key] = str(prow.get("日志备注", "") or "") if prior_has_note else ""
        for index, row in df.iterrows():
            key = str(row.get(key_col, "")).strip()
            if not key or key not in done:
                continue
            if str(row.get("填报状态", "")).strip() == _RESUME_DONE_STATUS:
                continue
            df.at[index, "填报状态"] = _RESUME_DONE_STATUS
            df.at[index, "日志备注"] = done[key] or "上次已成功(续跑跳过)"
            resumed += 1
        return df, resumed

    # Tier 2 (BATCH-RESUME2): content-hash matching when no usable URL key.
    # Order-independent, so reordered / inserted rows still resume; falls through
    # to positional only when the two frames' data columns differ.
    df_content = _content_columns(df)
    prior_content = _content_columns(prior_df)
    if df_content and sorted(map(str, df_content)) == sorted(map(str, prior_content)):
        prior_has_note = "日志备注" in prior_df.columns
        done_hashes: dict[str, str] = {}
        for _, prow in prior_df.iterrows():
            if str(prow.get("填报状态", "")).strip() == _RESUME_DONE_STATUS:
                h = _row_content_hash(prow, df_content)
                done_hashes[h] = str(prow.get("日志备注", "") or "") if prior_has_note else ""
        if done_hashes:
            for index, row in df.iterrows():
                if str(row.get("填报状态", "")).strip() == _RESUME_DONE_STATUS:
                    continue
                h = _row_content_hash(row, df_content)
                if h in done_hashes:
                    df.at[index, "填报状态"] = _RESUME_DONE_STATUS
                    df.at[index, "日志备注"] = done_hashes[h] or "上次已成功(内容续跑跳过)"
                    resumed += 1
            return df, resumed

    prior_status = list(prior_df["填报状态"])
    prior_notes = (
        list(prior_df["日志备注"]) if "日志备注" in prior_df.columns else [""] * len(prior_status)
    )
    for pos, index in enumerate(df.index):
        if pos >= len(prior_status):
            break
        if str(prior_status[pos]).strip() != _RESUME_DONE_STATUS:
            continue
        if str(df.at[index, "填报状态"]).strip() == _RESUME_DONE_STATUS:
            continue
        note = prior_notes[pos] if pos < len(prior_notes) else ""
        df.at[index, "填报状态"] = _RESUME_DONE_STATUS
        df.at[index, "日志备注"] = str(note or "上次已成功(续跑跳过)")
        resumed += 1
    return df, resumed


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
    attachment_intent: str = "",
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
                "intent": str(attachment_intent or "").strip(),
            })

        all_urls: list[str] = []
        if target_url:
            all_urls.append(str(target_url))
        for url in urls or []:
            u = str(url or "").strip()
            if u and u not in all_urls:
                all_urls.append(u)

        ensure_contract_skeleton(run_id)
        contract = build_input_contract(
            goal=goal or "",
            target_url="",
            urls=all_urls,
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


def _record_child_run_to_parent(parent_run_id: str, payload: dict[str, object]) -> None:
    """Best-effort parent manifest entry for a URL/row child run."""

    parent = str(parent_run_id or payload.get("parent_run_id") or "").strip()
    child = str(payload.get("child_run_id") or "").strip()
    if not parent or not child or parent == child:
        return
    try:
        child_kind = str(payload.get("child_kind") or "child").strip() or "child"
        index = int(payload.get("index") or 0)
        total = int(payload.get("total") or 0)
        extra: dict[str, object] = {
            "entry_type": "child_run",
            "child_run_id": child,
            "child_kind": child_kind,
            "index": index,
            "total": total,
            "success": bool(payload.get("success")),
        }
        for key in ("start_url", "row_no", "project_no", "error"):
            value = payload.get(key)
            if value not in (None, ""):
                extra[key] = value

        ensure_contract_skeleton(parent)
        append_manifest_item(
            parent,
            kind="log",
            path=f"runs/{child}/manifest.json",
            source_url=str(payload.get("start_url") or ""),
            produced_by="batch_orchestrator",
            step_id=f"{child_kind}_{index:04d}" if index else child_kind,
            extra=extra,
        )
    except Exception as exc:
        _emit_log(f"[IO Contract] child run manifest append failed: {exc}", level="warn")


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
    resume: bool = False,
    attachment_intent: str = "",
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
        attachment_intent: 用户显式声明的附件 intent（输入契约 §一-B 用户
            覆盖位）；空字符串表示交给 ``infer_attachment_intent`` 推断。
    """
    from visual_web_agent import main as agent_main

    intent_override = str(attachment_intent or "").strip()
    _persist_io_contracts_safe(
        run_id=run_id,
        goal=prompt,
        target_url=target_url,
        urls=urls or [],
        file_path=file_path,
        auth_profiles=auth_profiles,
        vlm_options=vlm_options,
        run_constraints=run_constraints,
        attachment_intent=intent_override,
    )

    attachment_spec = None
    attachment_result = None
    attachment_intent = ""
    if file_path:
        try:
            attachment_spec, attachment_result = _dispatch_attachment(
                file_path, prompt or "", intent_override=intent_override
            )
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
            prompt_images_for_agent: list[str] = []
            if attachment_result is not None:
                augmented_goal = _augment_prompt_with_attachment(base_goal, attachment_result)
                if attachment_intent == "upload_to_page":
                    upload_path_for_agent = (
                        getattr(attachment_result, "upload_path", "")
                        or (file_path or "")
                    )
                prompt_images_for_agent = list(
                    getattr(attachment_result, "image_b64", []) or []
                )

            async def _run_one(**kwargs: object) -> bool:
                kwargs.setdefault("vlm_options", vlm_options or None)
                kwargs.setdefault("run_constraints", run_constraints)
                if prompt_images_for_agent:
                    kwargs.setdefault("prompt_images", prompt_images_for_agent)
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
                run_id=run_id,
                on_child_run=lambda payload: _record_child_run_to_parent(run_id, payload),
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
        df = _load_batch_rows_via_adapter(
            file_path, prompt or "", intent_override=intent_override
        )
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

    # BATCH-RESUME1: opt-in断点续跑。resume 显式参数或 run_constraints.resume 任一为真，
    # 且上次结果文件存在时，把上次「成功」行合并回来，让 orchestrator 自动跳过它们。
    _rc = run_constraints if isinstance(run_constraints, dict) else {}
    effective_resume = bool(resume) or bool(_rc.get("resume"))
    if effective_resume and Path(output_file).exists():
        try:
            prior_df = pd.read_excel(output_file)
            df, _resumed = _merge_prior_progress(df, prior_df, key_col=_row_resume_key(df))
            if _resumed:
                _emit_log(f"♻️ [Batch Resume] 从上次结果续跑：跳过 {_resumed}/{total} 行已成功")
            else:
                _emit_log("♻️ [Batch Resume] 上次结果无可续行，全量执行")
        except Exception as resume_exc:
            _emit_log(f"⚠️ [Batch Resume] 读取上次结果失败，全量重跑: {resume_exc}", level="warn")

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
        run_id=run_id,
        on_child_run=lambda payload: _record_child_run_to_parent(run_id, payload),
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
    resume: bool = False,
    attachment_intent: str = "",
) -> bool:
    """CLI 同步入口。

    ``run_id`` / ``urls`` 是新加的可选 kwargs：当从 ``api_server`` 调用时携带
    任务 ID 与多 URL 列表，让 :func:`run_smart_batch` 把 input/output
    contract 落盘到 ``runs/<run_id>/``。``attachment_intent`` 携带用户显式
    声明的附件 intent（空 = 自动推断）。CLI 直接调用时省略即可，行为与
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
            resume=resume,
            attachment_intent=attachment_intent,
        )
    )


if __name__ == "__main__":
    # 本地调试示例
    run_smart_batch_sync(
        target_url="https://your-audit-system.com/form",
        prompt="请根据当前数据行填写表单，提交后完成任务。",
        file_path="audit_data.xlsx",
    )

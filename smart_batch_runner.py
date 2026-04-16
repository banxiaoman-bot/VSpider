from __future__ import annotations

import asyncio
import re
import sys
import threading
from pathlib import Path

import pandas as pd

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
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".xls", ".xlsx"}:
        return pd.read_excel(path)
    raise ValueError(f"仅支持 .csv/.xls/.xlsx，收到: {suffix or '<none>'}")


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


async def run_smart_batch(
    target_url: str,
    prompt: str,
    file_path: str | None,
    stop_event: threading.Event | None = None,
) -> None:
    """
    批处理入口（供 FastAPI BackgroundTasks 调用）。

    Args:
        target_url: 前端传入目标 URL
        prompt: 前端传入任务提示词（支持 {{列名}} 占位符）
        file_path: 上传文件的落盘路径
        stop_event: 停止信号
    """
    from visual_web_agent.main import run_agent

    if file_path:
        _emit_log(f"🚀 [Batch Runner] 启动批量引擎 | file={file_path}")
    else:
        _emit_log("🚀 [Batch Runner] 启动单任务引擎 | no file attached")
    _emit_log(f"🌐 [Batch Runner] target_url={target_url}")

    if not file_path:
        try:
            goal = _run_single_goal(prompt)
            success = await run_agent(start_url=target_url, goal=goal, stop_event=stop_event)
            if stop_event and stop_event.is_set():
                summary = "⏹️ 单任务已终止"
            elif success:
                summary = "🎉 单任务执行完成"
            else:
                summary = "❌ 单任务执行失败"
            _emit_log(summary)
            try:
                broadcast_done(bool(success) and not (stop_event and stop_event.is_set()), summary)
            except Exception:
                pass
        except Exception as exc:
            _emit_log(f"❌ 单任务执行失败: {exc}", level="error")
            try:
                broadcast_done(False, f"单任务执行失败: {exc}")
            except Exception:
                pass
        return

    try:
        df = _load_dataframe(file_path)
    except Exception as exc:
        _emit_log(f"❌ 读取文件失败: {exc}", level="error")
        try:
            broadcast_done(False, f"批处理启动失败: {exc}")
        except Exception:
            pass
        return

    if "填报状态" not in df.columns:
        df["填报状态"] = "未处理"
    if "日志备注" not in df.columns:
        df["日志备注"] = ""

    total = len(df)
    success_count = 0
    output_file = str(Path(file_path).with_name(f"{Path(file_path).stem}_处理结果.xlsx"))
    _emit_log(f"📫 已载入 {total} 条记录，结果文件: {output_file}")

    for index, row in df.iterrows():
        if stop_event and stop_event.is_set():
            _emit_log("⏹️ [Batch Runner] 收到停止信号，结束批处理循环", level="warn")
            break

        row_no = index + 1
        current_status = str(row.get("填报状态", "")).strip()
        project_no = str(row.get("项目编号", "未知")).strip() or "未知"

        if current_status == "成功":
            _emit_log(f"⏭️ 第 {row_no} 行 ({project_no}) 已成功，跳过")
            continue

        row_data = _normalize_row(row)
        goal = _render_goal(prompt, row_data)

        _emit_log("=" * 48)
        _emit_log(f"▶️ 开始处理第 {row_no}/{total} 行 | 项目编号={project_no}")

        try:
            success = await run_agent(start_url=target_url, goal=goal, stop_event=stop_event)
            if success:
                df.at[index, "填报状态"] = "成功"
                df.at[index, "日志备注"] = "Agent 执行完成"
                success_count += 1
                _emit_log(f"✅ 第 {row_no} 行处理成功")
            else:
                df.at[index, "填报状态"] = "失败"
                df.at[index, "日志备注"] = "Agent 返回失败状态"
                _emit_log(f"❌ 第 {row_no} 行处理失败", level="error")
        except Exception as exc:
            df.at[index, "填报状态"] = "失败"
            df.at[index, "日志备注"] = f"异常: {str(exc)[:200]}"
            _emit_log(f"❌ 第 {row_no} 行处理失败: {exc}", level="error")
        finally:
            try:
                df.to_excel(output_file, index=False)
                _emit_log(f"💾 已保存进度: {output_file}")
            except Exception as save_exc:
                _emit_log(f"⚠️ 保存进度失败: {save_exc}", level="warn")
            await asyncio.sleep(1)

    if stop_event and stop_event.is_set():
        summary = f"⏹️ 批量任务已终止：成功 {success_count}/{total}"
    else:
        summary = f"🎉 批量任务结束：成功 {success_count}/{total}"
    _emit_log(summary)
    try:
        broadcast_done(not (stop_event and stop_event.is_set()), summary)
    except Exception:
        pass


def run_smart_batch_sync(
    target_url: str,
    prompt: str,
    file_path: str,
    stop_event: threading.Event | None = None,
) -> None:
    """CLI 同步入口。"""
    if sys.platform == "win32" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass
    asyncio.run(run_smart_batch(target_url, prompt, file_path, stop_event=stop_event))


if __name__ == "__main__":
    # 本地调试示例
    run_smart_batch_sync(
        target_url="https://your-audit-system.com/form",
        prompt="请根据当前数据行填写表单，提交后完成任务。",
        file_path="audit_data.xlsx",
    )

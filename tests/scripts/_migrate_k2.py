"""K2: byte-level migration to add ``/api/failed_runs`` endpoints to
``api_server.py`` (which is CRLF; the chat ``edit`` tool refuses LF-only
multi-line replacements against it).

Adds:
  • Import: ``from fastapi.responses import FileResponse, JSONResponse``
  • Import: ``from fastapi import HTTPException``  (the existing
    ``from fastapi import (...)`` block is extended in place)
  • Import: ``from visual_web_agent import failure_archive as _failure_archive``
  • Endpoint ``GET /api/failed_runs`` — list recent failures
  • Endpoint ``GET /api/failed_runs/{run_id}/log`` — serve HTML log

Idempotent: each insertion checks for a sentinel substring before applying.

Run: ``python tests/scripts/_migrate_k2.py``
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "api_server.py"


def main() -> None:
    raw = TARGET.read_bytes()
    text = raw.decode("utf-8")
    eol = "\r\n" if b"\r\n" in raw else "\n"

    changes = 0

    # ── 1) Extend the existing fastapi import tuple to include HTTPException
    fastapi_old = (
        f"from fastapi import ({eol}"
        f"    BackgroundTasks,{eol}"
        f"    FastAPI,{eol}"
        f"    File,{eol}"
    )
    fastapi_new = (
        f"from fastapi import ({eol}"
        f"    BackgroundTasks,{eol}"
        f"    FastAPI,{eol}"
        f"    File,{eol}"
        f"    HTTPException,{eol}"
    )
    if "HTTPException" in text:
        print("[skip] HTTPException already imported")
    else:
        if fastapi_old not in text:
            raise SystemExit("FATAL: fastapi import anchor not found")
        text = text.replace(fastapi_old, fastapi_new, 1)
        changes += 1
        print("[ok ] HTTPException added to fastapi import")

    # ── 2) Add fastapi.responses.FileResponse import after fastapi.staticfiles
    sf_old = f"from fastapi.staticfiles import StaticFiles{eol}"
    sf_new = (
        f"from fastapi.staticfiles import StaticFiles{eol}"
        f"from fastapi.responses import FileResponse, JSONResponse{eol}"
    )
    if "from fastapi.responses import" in text:
        print("[skip] fastapi.responses already imported")
    else:
        if sf_old not in text:
            raise SystemExit("FATAL: staticfiles import anchor not found")
        text = text.replace(sf_old, sf_new, 1)
        changes += 1
        print("[ok ] FileResponse / JSONResponse imported")

    # ── 3) Add failure_archive import after artifact_manager import
    am_old = (
        f"from visual_web_agent.artifact_manager import artifact_root, "
        f"artifact_url{eol}"
    )
    am_new = (
        f"from visual_web_agent.artifact_manager import artifact_root, "
        f"artifact_url{eol}"
        f"from visual_web_agent import failure_archive as _failure_archive{eol}"
    )
    if "failure_archive as _failure_archive" in text:
        print("[skip] failure_archive already imported")
    else:
        if am_old not in text:
            raise SystemExit("FATAL: artifact_manager import anchor not found")
        text = text.replace(am_old, am_new, 1)
        changes += 1
        print("[ok ] failure_archive imported")

    # ── 4) Add the two endpoints right before /api/start_batch
    # Anchor on the unique decorator line of /api/start_batch.
    sb_old = f'@app.post("/api/start_batch", summary="启动批处理任务（后台执行）"){eol}'
    endpoints_block = (
        f'@app.get("/api/failed_runs", summary="列出最近的失败 run（K2）"){eol}'
        f"async def get_failed_runs(limit: int = 50) -> dict:{eol}"
        f'    """返回最新的 ``limit`` 条失败 run 记录。{eol}'
        f"{eol}"
        f"    每条记录是 ``failure_archive.list_failed_runs`` 输出的字典，{eol}"
        f"    包含 schema_version/run_id/ts/reason/goal/duration_s 等字段，{eol}"
        f"    以及一个 ``paths_exist`` 子字典指明 HTML 日志 / phase jsonl /{eol}"
        f"    event jsonl 是否仍存在，前端据此决定按钮是否可用。{eol}"
        f'    """{eol}'
        f"    try:{eol}"
        f"        # ``base_dir`` / ``project_root`` 默认即可：模块自己解析项目根。{eol}"
        f"        records = _failure_archive.list_failed_runs(limit=int(limit or 50)){eol}"
        f"    except Exception as exc:{eol}"
        f'        logger.warning("[FAILED RUNS] list error: %s", exc){eol}'
        f"        records = []{eol}"
        f'    return {{"status": "success", "count": len(records), "items": records}}{eol}'
        f"{eol}"
        f"{eol}"
        f'@app.get("/api/failed_runs/{{run_id}}/log", summary="下载失败 run 的 HTML 轨迹日志（K2）"){eol}'
        f"async def get_failed_run_html_log(run_id: str):{eol}"
        f'    """返回 ``logs/run_log_<run_id>.html``。run_id 必须仅含 ASCII{eol}'
        f"    字母/数字/下划线，避免 path traversal；不存在则 404。{eol}"
        f'    """{eol}'
        f"    rid = (run_id or '').strip(){eol}"
        f"    # 仅允许 _0-9A-Za-z 的运行 ID（HtmlLogger 用 strftime 生成，{eol}"
        f"    # 形如 20260524_191800，所以这条白名单足够保守）。{eol}"
        f"    if not rid or not all(c.isalnum() or c == '_' for c in rid):{eol}"
        f'        raise HTTPException(status_code=400, detail="invalid run_id"){eol}'
        f'    log_path = Path("logs") / f"run_log_{{rid}}.html"{eol}'
        f"    if not log_path.exists() or not log_path.is_file():{eol}"
        f'        raise HTTPException(status_code=404, detail="log not found"){eol}'
        f"    return FileResponse({eol}"
        f"        path=str(log_path),{eol}"
        f'        media_type="text/html; charset=utf-8",{eol}'
        f"        filename=log_path.name,{eol}"
        f"    ){eol}"
        f"{eol}"
        f"{eol}"
    )
    sb_new = endpoints_block + sb_old
    if 'get("/api/failed_runs"' in text:
        print("[skip] failed_runs endpoint already present")
    else:
        if sb_old not in text:
            raise SystemExit("FATAL: /api/start_batch anchor not found")
        text = text.replace(sb_old, sb_new, 1)
        changes += 1
        print("[ok ] /api/failed_runs + /api/failed_runs/{run_id}/log added")

    # ── 5) Pathlib import safety — Path is already imported in this file?
    if "from pathlib import Path" not in text:
        # Conservative: alert but don't auto-add (don't want to mis-place it).
        raise SystemExit(
            "FATAL: 'from pathlib import Path' missing in api_server.py — "
            "endpoint code uses Path; please add the import manually."
        )

    if changes == 0:
        print("No changes (already migrated).")
        return

    TARGET.write_bytes(text.encode("utf-8"))
    print(f"Wrote {TARGET} ({changes} changes).")


if __name__ == "__main__":
    main()

"""``html_snapshot`` / ``screenshot`` first-class capability handlers (Slice S4).

Shipped as its own module (not appended to the oversized ``actions.py``) per
the workflow rule §三 "新功能拆模块". Closes the M1 audit gap "截图/HTML 快照
不进 manifest"：both handlers persist through ``resolve_output_path`` (which
prefers ``runs/<run_id>/artifacts/``) and ``register_artifact`` (which appends
the ``manifest.json`` entry when a run context is active), so the run's
manifest stays the single trusted index of everything produced.

    html_snapshot:  page.content()      ──►  *.html  + manifest (kind=html_snapshot)
    screenshot:     page.screenshot()   ──►  *.png   + manifest (kind=screenshot)

Both write back {path, size, source_url, ...} into ``workflow_memory`` and
append an rpa_trail entry carrying verifiable evidence (output_path + bytes).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

try:
    from .actions import (
        ActionContext,
        ActionExecutionError,
        ActionHandler,
        ActionRegistry,
    )
    from .artifact_manager import register_artifact, resolve_output_path
except ImportError:  # pragma: no cover - flat-layout fallback, mirrors actions.py
    from actions import (  # type: ignore[no-redef]
        ActionContext,
        ActionExecutionError,
        ActionHandler,
        ActionRegistry,
    )
    from artifact_manager import register_artifact, resolve_output_path  # type: ignore[no-redef]


def _page_url(page: Any) -> str:
    try:
        return page.url or ""
    except Exception:  # pragma: no cover - defensive
        return ""


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


@ActionRegistry.register("html_snapshot")
class HtmlSnapshotHandler(ActionHandler):
    """Persist the current tab's full HTML as a manifest-tracked artifact."""

    async def execute(self, ctx: ActionContext) -> Optional[Any]:
        page = ctx.page
        if page is None:
            raise ActionExecutionError("html_snapshot: 无活动页面。")
        try:
            html = await page.content()
        except Exception as exc:
            raise ActionExecutionError(
                f"html_snapshot: 读取页面 HTML 失败: {exc}"
            ) from exc
        if not (html or "").strip():
            raise ActionExecutionError("html_snapshot: 页面 HTML 为空，未落盘。")

        source_url = _page_url(page)
        hint = (ctx.action.type_value or "").strip()
        stem = hint or f"page_snapshot_{_timestamp()}"
        target = resolve_output_path(f"{Path(stem).stem}.html")
        target.write_text(html, encoding="utf-8")
        size = target.stat().st_size
        if size <= 0:
            raise ActionExecutionError(
                f"html_snapshot: 落盘文件为空 ({target})，evidence 校验失败。"
            )
        register_artifact(
            target,
            kind="html_snapshot",
            mime="text/html",
            size=size,
            source_url=source_url,
            produced_by="html_snapshot",
            step_id="html_snapshot",
        )

        mem_key = (ctx.action.memory_key or "").strip() or "html_snapshot"
        try:
            ctx.workflow_memory[mem_key] = {
                "path": str(target),
                "size": size,
                "source_url": source_url,
            }
        except Exception:  # pragma: no cover - memory is a plain dict in practice
            pass
        try:
            ctx.browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "html_snapshot",
                    "type_value": hint,
                    "source_url": source_url,
                    "output_kind": "html_snapshot",
                    "output_path": str(target),
                    "size": size,
                    "verified": True,
                })
            )
        except Exception:  # pragma: no cover - trail is best-effort evidence
            pass
        return None


@ActionRegistry.register("screenshot")
class ScreenshotCaptureHandler(ActionHandler):
    """Persist a viewport / full-page screenshot as a manifest-tracked artifact."""

    async def execute(self, ctx: ActionContext) -> Optional[Any]:
        page = ctx.page
        if page is None:
            raise ActionExecutionError("screenshot: 无活动页面。")
        mode = (ctx.action.type_value or "").strip().lower()
        full_page = mode in {"full", "full_page", "fullpage", "整页", "全页", "全屏"}
        try:
            png = await page.screenshot(full_page=full_page)
        except Exception as exc:
            if full_page:
                # some pages (sticky viewports / huge canvases) reject
                # full-page capture; the viewport shot is still evidence.
                try:
                    png = await page.screenshot(full_page=False)
                    full_page = False
                except Exception as retry_exc:
                    raise ActionExecutionError(
                        f"screenshot: 截图失败: {retry_exc}"
                    ) from retry_exc
            else:
                raise ActionExecutionError(f"screenshot: 截图失败: {exc}") from exc
        if not png:
            raise ActionExecutionError("screenshot: 截图返回空字节，未落盘。")

        source_url = _page_url(page)
        target = resolve_output_path(f"screenshot_{_timestamp()}.png")
        target.write_bytes(png)
        size = target.stat().st_size
        if size <= 0:
            raise ActionExecutionError(
                f"screenshot: 落盘文件为空 ({target})，evidence 校验失败。"
            )
        register_artifact(
            target,
            kind="screenshot",
            mime="image/png",
            size=size,
            source_url=source_url,
            produced_by="screenshot",
            step_id="screenshot",
        )

        mem_key = (ctx.action.memory_key or "").strip() or "screenshot"
        try:
            ctx.workflow_memory[mem_key] = {
                "path": str(target),
                "size": size,
                "full_page": full_page,
                "source_url": source_url,
            }
        except Exception:  # pragma: no cover
            pass
        try:
            ctx.browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "screenshot",
                    "type_value": mode,
                    "source_url": source_url,
                    "output_kind": "screenshot",
                    "output_path": str(target),
                    "size": size,
                    "full_page": full_page,
                    "verified": True,
                })
            )
        except Exception:  # pragma: no cover
            pass
        return None

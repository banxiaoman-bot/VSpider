"""``vscroll_capture`` capability handler (Slice VSCROLL-ACTION-1).

Shipped as its own module (not appended to the oversized ``actions.py``) per
the workflow rule §三 "新功能拆模块". The deterministic capture loop lives in
:mod:`visual_web_agent.virtual_scroll`; until now it was reachable only from
the pre-extract fast-path (before the planner starts), so a virtualised list
discovered mid-run - behind a login, a navigation, a tab switch - still cost
one VLM round per viewport. This thin handler exposes the same capture as a
mid-run action:

    find_virtual_list_scope (main doc -> child frames)
        └─►  capture_virtual_list_rows (snapshot + nudge + dedup)
                 ├─►  workflow_memory["vscroll_rows"]
                 └─►  runs/<id>/artifacts/*.jsonl + manifest (dataset_rows)

``type_value`` (optional) caps the harvest row count; ``memory_key``
(optional) renames the memory slot.
"""

from __future__ import annotations

from typing import Any, Optional

try:
    from .actions import (
        ActionContext,
        ActionExecutionError,
        ActionHandler,
        ActionRegistry,
    )
    from .virtual_scroll import (
        capture_virtual_list_rows,
        find_virtual_list_scope,
        map_captured_rows_to_fields,
    )
except ImportError:  # pragma: no cover - flat-layout fallback, mirrors actions.py
    from actions import (  # type: ignore[no-redef]
        ActionContext,
        ActionExecutionError,
        ActionHandler,
        ActionRegistry,
    )
    from virtual_scroll import (  # type: ignore[no-redef]
        capture_virtual_list_rows,
        find_virtual_list_scope,
        map_captured_rows_to_fields,
    )

_DEFAULT_MAX_ROWS = 2000
_MAX_ROWS_CEILING = 20000


@ActionRegistry.register("vscroll_capture")
class VscrollCaptureHandler(ActionHandler):
    """Deterministic one-shot harvest of a virtualised list, mid-run."""

    async def execute(self, ctx: ActionContext) -> Optional[Any]:
        page = ctx.page
        if page is None:
            raise ActionExecutionError("vscroll_capture: 无活动页面。")

        max_rows = self._max_rows(ctx.action.type_value)

        probe = await find_virtual_list_scope(page, include_main=True)
        scope = probe.get("scope")
        if scope is None:
            raise ActionExecutionError(
                "vscroll_capture: 未发现带滚动余量的虚拟列表容器"
                "（主文档与同源子 iframe 均未命中）；改用普通 extract / scroll。"
            )

        meta = await capture_virtual_list_rows(scope, max_rows=max_rows)
        rows = list(meta.get("rows") or [])
        if not rows:
            raise ActionExecutionError(
                "vscroll_capture: 容器命中但未采集到行"
                f"（passes={meta.get('passes')}, container={meta.get('container') or '?'}）；"
                "改用普通 extract。"
            )

        try:
            source_url = str(page.url or "")
        except Exception:  # pragma: no cover - defensive
            source_url = ""

        # VSCROLL-FIELDS-1: persist named columns when headers were harvested.
        headers = list(meta.get("headers") or [])
        mapped_rows = map_captured_rows_to_fields(rows, headers)
        entry = self._persist(mapped_rows, source_url=source_url)
        output_path = (entry or {}).get("path", "")

        evidence = {
            "rows": mapped_rows,
            "row_count": len(rows),
            "complete": bool(meta.get("complete")),
            "passes": meta.get("passes"),
            "container": meta.get("container") or "",
            "axis": meta.get("axis") or "y",
            "headers": headers,
            "where": probe.get("where") or "",
            "frame_url": probe.get("url") or "",
            "source_url": source_url,
            "output_path": output_path,
        }
        mem_key = (ctx.action.memory_key or "").strip() or "vscroll_rows"
        try:
            ctx.workflow_memory[mem_key] = evidence
        except Exception:  # pragma: no cover - memory is a plain dict in practice
            pass

        try:
            ctx.browser.rpa_trail.append(
                ctx.with_rpa_meta(
                    {
                        "action": "vscroll_capture",
                        "row_count": len(rows),
                        "passes": meta.get("passes"),
                        "complete": bool(meta.get("complete")),
                        "container": meta.get("container") or "",
                        "axis": meta.get("axis") or "y",
                        "header_count": len(headers),
                        "where": probe.get("where") or "",
                        "frame_url": probe.get("url") or "",
                        "max_rows": max_rows,
                        "output_kind": "dataset_rows",
                        "output_path": output_path,
                        "source_url": source_url,
                    }
                )
            )
        except Exception:  # pragma: no cover - trail is best-effort evidence
            pass

        return None

    @staticmethod
    def _max_rows(type_value: Any) -> int:
        """``type_value`` is an optional row cap; junk falls back to default."""
        try:
            n = int(str(type_value or "").strip())
        except (TypeError, ValueError):
            return _DEFAULT_MAX_ROWS
        if n <= 0:
            return _DEFAULT_MAX_ROWS
        return min(n, _MAX_ROWS_CEILING)

    @staticmethod
    def _persist(rows: list, *, source_url: str) -> Optional[dict]:
        """Write the rows as a ``dataset_rows`` jsonl artifact + manifest entry.

        Returns ``None`` when no run context is published (e.g. ad-hoc calls);
        the agent still gets the rows via ``workflow_memory``.
        """
        try:
            from .data_writers import write_jsonl
            from .io_contract import current_base_dir, current_run_id
        except ImportError:  # pragma: no cover - flat-layout fallback
            from data_writers import write_jsonl  # type: ignore[no-redef]
            from io_contract import (  # type: ignore[no-redef]
                current_base_dir,
                current_run_id,
            )

        run_id = current_run_id()
        if not run_id:
            return None
        return write_jsonl(
            rows,
            run_id=run_id,
            output_kind="dataset_rows",
            produced_by="vscroll_capture",
            source_url=source_url,
            base_dir=current_base_dir(),
        )

"""``page_to_markdown`` capability handler.

Shipped as its own module (not appended to the oversized ``actions.py``) per
the workflow rule §三 "新功能拆模块". The deterministic HTML→Markdown logic
lives in :mod:`visual_web_agent.extraction_engine.fit_markdown`; this thin
handler wires it to the live tab and the run manifest:

    live tab HTML  ──►  html_to_fit_markdown  ──►  workflow_memory
                                              └──►  runs/<id>/artifacts/*.md
                                                    + manifest (kind=markdown_doc)

``type_value`` (optional) is treated as a BM25 focus query so the agent can ask
for "just the parts about X" — handy for question-answering / RAG feeds while
spending far fewer VLM tokens than a full-page screenshot.
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
    from .extraction_engine.fit_markdown import FitMarkdownResult, html_to_fit_markdown
    from .extraction_engine.chunking import chunk_markdown
except ImportError:  # pragma: no cover - flat-layout fallback, mirrors actions.py
    from actions import (  # type: ignore[no-redef]
        ActionContext,
        ActionExecutionError,
        ActionHandler,
        ActionRegistry,
    )
    from extraction_engine.fit_markdown import (  # type: ignore[no-redef]
        FitMarkdownResult,
        html_to_fit_markdown,
    )
    from extraction_engine.chunking import chunk_markdown  # type: ignore[no-redef]


@ActionRegistry.register("page_to_markdown")
class PageToMarkdownHandler(ActionHandler):
    """Convert the current tab into denoised, LLM-friendly Markdown."""

    async def execute(self, ctx: ActionContext) -> Optional[Any]:
        page = ctx.page
        if page is None:
            raise ActionExecutionError("page_to_markdown: 无活动页面。")

        try:
            html = await page.content()
        except Exception as exc:  # pragma: no cover - browser failure path
            raise ActionExecutionError(
                f"page_to_markdown: 读取页面 HTML 失败: {exc}"
            ) from exc

        try:
            base_url = page.url or ""
        except Exception:  # pragma: no cover - defensive
            base_url = ""

        query = (ctx.action.type_value or "").strip()
        result = html_to_fit_markdown(html, base_url=base_url, query=query)

        entry = self._persist(result, base_url=base_url)
        output_path = (entry or {}).get("path", "")

        chunks = chunk_markdown(result.markdown, strategy="heading")
        chunk_records = [
            {"index": c.index, "heading": c.heading, "word_count": c.word_count, "text": c.text}
            for c in chunks
        ]
        chunks_entry = self._persist_chunks(chunk_records, base_url=base_url)
        chunks_path = (chunks_entry or {}).get("path", "")

        mem_key = (ctx.action.memory_key or "").strip() or "page_markdown"
        try:
            ctx.workflow_memory[mem_key] = {
                "markdown": result.markdown,
                "markdown_path": output_path,
                "word_count": result.word_count,
                "links": result.links,
                "source_url": base_url,
                "query": query,
                "chunk_count": len(chunk_records),
                "chunks_path": chunks_path,
            }
        except Exception:  # pragma: no cover - memory is a plain dict in practice
            pass

        try:
            ctx.browser.rpa_trail.append(
                ctx.with_rpa_meta(
                    {
                        "action": "page_to_markdown",
                        "type_value": query,
                        "source_url": base_url,
                        "word_count": result.word_count,
                        "link_count": len(result.links),
                        "removed_blocks": result.removed_blocks,
                        "output_kind": "markdown_doc",
                        "output_path": output_path,
                        "chunk_count": len(chunk_records),
                        "chunks_path": chunks_path,
                    }
                )
            )
        except Exception:  # pragma: no cover - trail is best-effort evidence
            pass

        return None

    @staticmethod
    def _persist(result: "FitMarkdownResult", *, base_url: str) -> Optional[dict]:
        """Write the Markdown artifact + manifest entry for the active run.

        Returns ``None`` when no run context is published (e.g. ad-hoc calls);
        the agent still gets the Markdown via ``workflow_memory``.
        """
        try:
            from .data_writers import write_markdown
            from .io_contract import current_base_dir, current_run_id
        except ImportError:  # pragma: no cover - flat-layout fallback
            from data_writers import write_markdown  # type: ignore[no-redef]
            from io_contract import (  # type: ignore[no-redef]
                current_base_dir,
                current_run_id,
            )

        run_id = current_run_id()
        if not run_id:
            return None
        return write_markdown(
            result.markdown,
            run_id=run_id,
            output_kind="markdown_doc",
            produced_by="page_to_markdown",
            source_url=base_url,
            base_dir=current_base_dir(),
        )

    @staticmethod
    def _persist_chunks(chunk_records: list, *, base_url: str) -> Optional[dict]:
        """Write the RAG chunks as a ``markdown_chunks`` jsonl artifact.

        Returns ``None`` with no run context or no chunks; the agent still gets
        ``chunk_count`` via ``workflow_memory``.
        """
        if not chunk_records:
            return None
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
            chunk_records,
            run_id=run_id,
            output_kind="markdown_chunks",
            produced_by="page_to_markdown",
            source_url=base_url,
            base_dir=current_base_dir(),
        )

"""``save_artifact`` -- container-aware dispatch for ``data_writers``.

Single entry point the agent loop calls instead of ``save_to_excel``::

    item = save_artifact(
        rows,
        run_id=_run_ts,
        container=oc.container,
        output_kind=oc.output_kind,
        produced_by="vlm_extract",
        step_id=str(step),
    )

The container value comes from ``output_contract.v1`` -- so this is the
chokepoint that finally honours the contract instead of forcing every
run to xlsx.

The dispatch table is intentionally explicit (no auto-discovery / no
plugin loaders). New containers should be added in three places:

1. ``OUTPUT_CONTRACT.CONTAINERS`` (the allowed set)
2. ``data_writers/<name>_writer.py`` (the implementation)
3. ``CONTAINER_TO_WRITER`` below (the dispatch entry)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Iterable

from .csv_writer import write_csv
from ._base import default_filename
from .files_folder_writer import write_files_folder
from .html_writer import write_html
from .inline_text_writer import write_inline_text
from .json_writer import write_json
from .jsonl_writer import write_jsonl
from .markdown_writer import write_markdown
from .xlsx_writer import write_xlsx


WriterFn = Callable[..., dict[str, Any]]


CONTAINER_TO_WRITER: dict[str, WriterFn] = {
    "csv":          write_csv,
    "jsonl":        write_jsonl,
    "json":         write_json,
    "markdown":     write_markdown,
    "html":         write_html,
    "files_folder": write_files_folder,
    "inline_text":  write_inline_text,
    "xlsx":         write_xlsx,
    # zip is intentionally NOT auto-registered -- archive output goes
    # through files_folder + a downstream zipper so the contract layer
    # never silently swallows multi-file payloads into one opaque blob.
}


_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_.-]+$")


def save_artifact(
    data: Any,
    *,
    run_id: str,
    container: str,
    output_kind: str = "",
    produced_by: str = "",
    step_id: str = "",
    source_url: str | Iterable[str] = "",
    filename_hint: str = "",
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Route ``data`` to the writer registered for ``container``.

    Args:
        data: payload shape depends on the writer (rows / dict / str / blobs).
        run_id: target ``runs/<run_id>/`` directory; must be a non-empty,
            filesystem-safe identifier.
        container: ``output_contract.container`` value; one of
            ``CONTAINER_TO_WRITER`` keys.
        output_kind: ``output_contract.output_kind``; flows through to the
            manifest entry ``kind`` field for cross-referencing.
        produced_by: short label of the agent capability that produced
            the artifact (e.g. ``"vlm_extract"`` / ``"xhr_intercept"``).
        step_id: optional step identifier (loop iteration / phase id) so
            manifest entries can be traced back to a specific decision.
        source_url: upstream URL(s) the data came from (recorded on the
            manifest item, supports dedup-and-merge across runs).
        filename_hint: optional caller-provided filename; falls back to
            a deterministic ``produced_by + timestamp`` name.
        extra: optional dict merged into the ``ManifestItem.extra`` field.
        base_dir: optional override for the runs root (mainly for tests).

    Returns:
        The dict produced by the chosen writer. For single-file writers
        this is the ``ManifestItem.to_dict()`` plus ``path``. For
        ``files_folder`` it is ``{"path", "items", ...}`` summarising the
        whole folder. For ``inline_text`` it carries ``inline=True`` and
        the ``text`` payload (no file on disk).

    Raises:
        ValueError: ``container`` unknown / ``run_id`` invalid.
    """
    if not run_id or not _RUN_ID_RE.fullmatch(str(run_id)):
        raise ValueError(f"save_artifact: invalid run_id {run_id!r}")

    writer = CONTAINER_TO_WRITER.get(container)
    if writer is None:
        raise ValueError(
            f"save_artifact: unknown container {container!r}; "
            f"expected one of {sorted(CONTAINER_TO_WRITER.keys())}"
        )

    return writer(
        data,
        run_id=run_id,
        output_kind=output_kind,
        produced_by=produced_by,
        step_id=step_id,
        source_url=source_url,
        filename_hint=filename_hint,
        extra=extra,
        base_dir=base_dir,
    )


def resolve_output_contract(*contracts: dict[str, Any] | None) -> dict[str, Any]:
    """Merge contract dicts; later entries win. Always returns container/kind."""

    merged: dict[str, Any] = {}
    for contract in contracts:
        if not isinstance(contract, dict):
            continue
        for key, value in contract.items():
            if value not in (None, ""):
                merged[key] = value
    merged.setdefault("container", "xlsx")
    merged.setdefault("output_kind", "dataset_rows")
    merged.setdefault("mode", "default")
    return merged


def save_run_dataset(
    data: Any,
    *,
    run_id: str,
    output_contract: dict[str, Any] | None = None,
    produced_by: str = "",
    step_id: str = "",
    filename_hint: str = "",
    unique_key: str | None = None,
    source_url: str | Iterable[str] = "",
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> str:
    """Contract-aware save for the main agent loop.

    Uses ``save_artifact`` for container dispatch. When ``unique_key`` is
    set and container is xlsx, delegates to legacy ``save_to_excel`` so
    tooltip upsert / append dedupe keeps working until xlsx_writer grows
    the same semantics.
    """

    contract = resolve_output_contract(output_contract)
    container = str(contract.get("container") or "xlsx")
    output_kind = str(contract.get("output_kind") or "dataset_rows")

    if unique_key and container == "xlsx":
        try:
            from visual_web_agent.data_manager import save_to_excel
        except ImportError:
            from data_manager import save_to_excel

        hint = filename_hint or default_filename(produced_by=produced_by or "extract", suffix=".xlsx")
        saved = save_to_excel(data, filename=hint, unique_key=unique_key)
        return str(saved or "")

    result = save_artifact(
        data,
        run_id=run_id,
        container=container,
        output_kind=output_kind,
        produced_by=produced_by,
        step_id=step_id,
        source_url=source_url,
        filename_hint=filename_hint,
        extra=extra,
        base_dir=base_dir,
    )
    path = result.get("path")
    if path:
        return str(path)
    if result.get("inline"):
        return str(result.get("text") or "")
    return ""

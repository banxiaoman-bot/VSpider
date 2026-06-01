"""Container-aware artifact writers for ``output_contract.v1``.

Replaces the hard-coded ``data_manager.save_to_excel`` default with a
dispatch table keyed by ``output_contract.container``. Each writer is a
small module under this package; ``dispatch.save_artifact`` is the only
entry point new code should call.

See ``docs/io_contract_design.md`` Section 2 for the contract field
definitions and the ``output_kind -> container`` mapping that drives
which writer ends up handling the payload.
"""

from .dispatch import CONTAINER_TO_WRITER, resolve_output_contract, save_artifact, save_run_dataset
from .csv_writer import write_csv
from .jsonl_writer import write_jsonl
from .json_writer import write_json
from .markdown_writer import write_markdown
from .html_writer import write_html
from .files_folder_writer import write_files_folder
from .inline_text_writer import write_inline_text
from .xlsx_writer import write_xlsx

__all__ = [
    "CONTAINER_TO_WRITER",
    "resolve_output_contract",
    "save_artifact",
    "save_run_dataset",
    "write_csv",
    "write_jsonl",
    "write_json",
    "write_markdown",
    "write_html",
    "write_files_folder",
    "write_inline_text",
    "write_xlsx",
]

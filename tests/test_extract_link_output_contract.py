"""OUTPUT-DEFAULT-4 regression: extract_link must not blind-write output_links.xlsx.

Legacy ``extract_link`` called ``data_manager.save_to_excel(..., "output_links.xlsx")``
-- a hard-coded xlsx file in the CWD that ignored the run's
``output_contract.container`` and was not run-scoped (mission §一-A: never default
to Excel; the on-disk form must follow the contract).

``_persist_extracted_link`` routes the extracted link through ``save_run_dataset``
honoring the active run's container, run-scoped under ``runs/<run_id>/artifacts``,
and is a no-op (returns "") when no run is active so unit/test envs never litter
the CWD with an ``output_links.xlsx``.

Deterministic: no Playwright / browser -- exercises the persistence helper only.
"""

from __future__ import annotations

from pathlib import Path

from visual_web_agent.actions import _persist_extracted_link
from visual_web_agent.io_contract import (
    clear_current_run,
    set_current_run,
    write_output_contract,
)


def _setup_run(tmp_path: Path, run_id: str, container: str) -> None:
    write_output_contract(
        run_id,
        {"mode": "artifact", "output_kind": "dataset_rows", "container": container},
        base_dir=tmp_path,
    )
    set_current_run(run_id, base_dir=tmp_path)


def test_persist_link_honors_run_container_csv(tmp_path: Path) -> None:
    try:
        _setup_run(tmp_path, "run_link_csv", "csv")
        path = _persist_extracted_link(3, "https://example.com/a")
    finally:
        clear_current_run()
    assert path.endswith(".csv"), path
    assert Path(path).exists()
    # run-scoped, not a CWD output_links.xlsx
    assert "run_link_csv" in path.replace("\\", "/")
    assert not Path("output_links.xlsx").exists()


def test_persist_link_honors_run_container_jsonl(tmp_path: Path) -> None:
    try:
        _setup_run(tmp_path, "run_link_jsonl", "jsonl")
        path = _persist_extracted_link(7, "https://example.com/c")
    finally:
        clear_current_run()
    assert path.endswith(".jsonl"), path
    assert Path(path).exists()


def test_persist_link_no_active_run_is_noop(tmp_path: Path) -> None:
    clear_current_run()
    path = _persist_extracted_link(1, "https://example.com/b")
    assert path == ""
    assert not Path("output_links.xlsx").exists()

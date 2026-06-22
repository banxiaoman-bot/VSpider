"""Shared state and result types for the extract action pipeline.

These dataclasses decouple the extract logic from ``run_agent()`` closures
so the handler can live in ``actions/extract_action.py`` while the mutable
flags/counters stay in a single inspectable bundle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtractState:
    """Mutable bag of all extract-related flags that survive across steps.

    ``run_agent()`` creates one instance at loop start and passes it into
    every ``handle_extract_action()`` call.  The handler mutates fields in
    place; the caller reads them back for inter-step pagination / guard
    decisions.
    """

    total_extracted_rows: int = 0
    extracted_page_urls: set[str] = field(default_factory=set)
    extracted_page_keys: set[str] = field(default_factory=set)
    seen_extract_row_keys: set[str] = field(default_factory=set)
    tooltip_trigger_keys: set[str] = field(default_factory=set)
    extract_count: int = 0

    dedup_tripped_last_step: bool = False
    duplicate_zero_extract_streak: int = 0

    first_flip_pending: bool = True
    first_extract_ever_done: bool = False

    pagination_probed: bool = False
    pagination_kind: str = ""
    page_is_infinite_scroll: bool = False

    force_next_page_pending: bool = False
    block_next_page_until_drained: bool = False
    block_next_page_reason: str = ""

    pagination_hint_msg: str = ""

    rpa_cache_allowed: bool = True
    rpa_skip_reason: str = ""


@dataclass
class ExtractActionResult:
    """Return value from ``handle_extract_action()``.

    Communicates back to the caller loop:
    - what happened (rows saved, paths produced)
    - whether to break the inner action loop
    - whether the whole task is done
    """

    saved_path: str = ""
    snapshot_path: str = ""
    new_rows: int = 0
    total_rows: int = 0

    should_break: bool = True
    task_completed: bool = False
    run_succeeded: bool = False

    log_source: str = ""
    pagination_hint: str = ""

    extra: dict[str, Any] = field(default_factory=dict)

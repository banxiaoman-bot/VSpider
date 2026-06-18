"""Runtime state and helpers for the extraction subsystem carved out of
``main.run_agent``.

S1a introduces :class:`ExtractState`, a single mutable container for the
~19 run-level extraction counters that the main step loop and the extraction
closures both read and write. Grouping them here breaks the closure/loop
two-way coupling so subsequent slices (S1b-S1d) can move the closures into an
``ExtractRuntime`` that operates on a shared state instance — no behaviour
change, pure relocation of state.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExtractState:
    """Run-level extraction counters shared by the main loop and ExtractRuntime.

    Field names mirror the original ``main.run_agent`` locals with the leading
    underscore dropped (e.g. ``_total_extracted_rows`` -> ``total_extracted_rows``).
    """

    # ── pagination / flow flags ────────────────────────────────────────────
    extract_count: int = 0
    pagination_probed: bool = False
    pagination_kind: str = ""
    pagination_hint_msg: str = ""
    first_flip_pending: bool = False
    page_is_infinite_scroll: bool = False
    force_next_page_pending: bool = False
    force_extract_after_navigation_pending: bool = False
    block_next_page_until_drained: bool = False
    block_next_page_reason: str = ""
    first_extract_ever_done: bool = False
    pagination_exhausted: bool = False

    # ── row / progress counters ────────────────────────────────────────────
    total_extracted_rows: int = 0
    extract_null_streak: int = 0
    extract_null_total_resets: int = 0

    # ── dedup / progress sets ──────────────────────────────────────────────
    extracted_page_urls: set = field(default_factory=set)
    extracted_page_keys: set = field(default_factory=set)
    seen_extract_row_keys: set = field(default_factory=set)
    tooltip_trigger_keys: set = field(default_factory=set)

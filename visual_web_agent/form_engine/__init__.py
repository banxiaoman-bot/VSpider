"""Form engine helpers for deterministic web form execution.

This package is the stable boundary for generic form capability:
- parse natural-language field assignments
- keep framework-specific JS adapter code out of the agent loop
- normalize validation evidence for logs and guards

The main agent still owns browser orchestration; form_engine owns form semantics.
"""

from .js_adapters import ADAPTERS, OPTION_SELECTORS, adapter_names
from .parser import parse_form_assignments, prepare_form_batch_fields

__all__ = [
    "ADAPTERS",
    "OPTION_SELECTORS",
    "adapter_names",
    "parse_form_assignments",
    "prepare_form_batch_fields",
]

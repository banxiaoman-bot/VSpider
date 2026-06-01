"""Date-arithmetic and goal-parsing helpers shared by date-related macros.

Owned by ``semantic_macros`` to keep the package free of any upward
dependency. ``main.py`` retains its own copies of ``_month_offset_day``,
``_parse_relative_month_day``, and ``_parse_goal_scope_title`` for unrelated
form-fill code paths (they predate the macro framework and serve a different
caller).
"""

from __future__ import annotations

import calendar
import re
from datetime import date


def next_month_day(day: int) -> str:
    today = date.today()
    year = today.year + (1 if today.month == 12 else 0)
    month = 1 if today.month == 12 else today.month + 1
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{min(day, last_day):02d}"


def month_offset_day(offset: int, day: int) -> str:
    today = date.today()
    month_index = today.month - 1 + int(offset or 0)
    year = today.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{min(max(1, int(day or 1)), last_day):02d}"


def parse_relative_month_day(text: str) -> tuple[int, int] | None:
    """Parse 下个月/下下个月/下下下个月 + day, or 下N个月 + day."""
    if not text:
        return None
    m = re.search(r"(下{1,6})个?月\s*的?\s*(3[01]|[12]?\d)\s*[号日]?", text)
    if m:
        return len(m.group(1)), int(m.group(2))
    m = re.search(r"下\s*([1-9]\d?)\s*个?月\s*的?\s*(3[01]|[12]?\d)\s*[号日]?", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def parse_goal_scope_title(goal: str) -> str:
    """Best-effort: extract the quoted section title the user is referring to."""
    text = str(goal or "")
    patterns = (
        r"[\"“”'‘’]([^\"“”'‘’]{1,80})[\"“”'‘’]\s*(?:标题|区域|表单|表格)?\s*(?:下方|下面|内部|内|中)",
        r"(?:标题|区域|表单|表格)\s*[\"“”'‘’]([^\"“”'‘’]{1,80})[\"“”'‘’]",
    )
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return ""

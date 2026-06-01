"""Generic relative-date resolver.

Why this exists
===============
The agent often gets goals phrased relative to "now":

    选定下个月的 15 号
    pick the 15th of next month
    三天后那天的天气
    本月底之前完成
    下周三的会议
    去年 12 月 1 号的报销单

Teaching the VLM to do calendar arithmetic ("if today is May 22, next month
is June, so I want June 15") is brittle: the model can be one off (current
month vs. current view), can over-flip the month panel, or pick the wrong
weekday. Computing the absolute target date deterministically in Python
and injecting it into the goal as a hint shifts the VLM's job from
"arithmetic + navigation" to **plain visual matching**: it just needs to
land on the date the system already computed.

This module is **the entire generic capability**. It is *not* a per-site
patch. Any new relative-date phrasing the user encounters can be added as
one new matcher (or one new entry to an existing table) without touching
prompts, browser code, or VLM logic.

Public API
==========

    resolve_relative_date(goal: str, today: date | None = None)
        -> RelativeDateMatch | None

Returns ``None`` when no recognised phrase is present (the agent then
falls back to its existing FORM_SKILL guidance — never makes things
worse). Otherwise returns the matched phrase, the absolute target date,
the months-from-today offset (signed), and a confidence label.

Matchers (ordered most-specific first)
======================================

1. **Month-relative + day**:  下下月3号 / 下个月15号 / 上月20号 /
   三个月后的5号 / 上上个月12号 / N months from now the Xth
2. **Year-relative + month + day**:  明年6月15号 / 去年12月1号 /
   后年元旦 / next year June 15 / 2026年6月15号
3. **N-day arithmetic**:  3天后 / 7天前 / in 5 days / 10 days ago
4. **Simple day offsets**:  今天/明天/后天/大后天/昨天/前天/大前天 +
   today / tomorrow / the day after tomorrow / yesterday / ...
5. **Relative weekday**:  下周三 / 本周一 / 上周五 / next Wednesday /
   last Friday / this Monday
6. **Month boundaries**:  月底 / 下月底 / 上月初 / 月中 /
   end of month / start of month
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Optional


@dataclass(frozen=True)
class RelativeDateMatch:
    """Result of resolving a relative-date phrase.

    Attributes:
        phrase:  The substring (or canonical form) that triggered the match.
                 Useful for the [DATE RESOLVER] note shown to the agent.
        target:  The computed absolute date.
        delta_months:  Months from today's month-anchor (signed, may be 0).
                       The agent uses this to decide how many times to click
                       the next/prev-month arrow.
        confidence:  ``"high"`` / ``"medium"`` / ``"low"`` — diagnostic only,
                     the public boolean question is just "did we match".
    """

    phrase: str
    target: date
    delta_months: int
    confidence: str


# ── Number parsing ─────────────────────────────────────────────────────────

_CN_DIGIT = {
    "零": 0, "〇": 0, "○": 0,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10,
}


def _parse_cn_int(s: str) -> int | None:
    """Parse a Chinese numeral 0-99. Returns None if not parseable."""
    if not s:
        return None
    if s in _CN_DIGIT:
        return _CN_DIGIT[s]
    if len(s) == 2 and s[1] == "十" and s[0] in _CN_DIGIT:  # 二十..九十
        return _CN_DIGIT[s[0]] * 10
    if (
        len(s) == 3
        and s[1] == "十"
        and s[0] in _CN_DIGIT
        and s[2] in _CN_DIGIT
    ):  # 二十一 .. 九十九
        return _CN_DIGIT[s[0]] * 10 + _CN_DIGIT[s[2]]
    if len(s) == 2 and s[0] == "十" and s[1] in _CN_DIGIT:  # 十一..十九
        return 10 + _CN_DIGIT[s[1]]
    return None


def _parse_int(s: str | None) -> int | None:
    """Parse arabic OR Chinese numeral."""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    return _parse_cn_int(s)


# ── Month / week arithmetic ────────────────────────────────────────────────

def _add_months(d: date, months: int) -> date:
    """Add months with year overflow + day clamping (1月31 + 1mo → 2月28/29)."""
    total = (d.month - 1) + months
    y = d.year + total // 12
    m = total % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last_day))


def _last_day_of_month(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _first_day_of_month(d: date) -> date:
    return date(d.year, d.month, 1)


def _resolve_weekday(today: date, week_offset: int, target_wd: int) -> date:
    """Return the date for ``target_wd`` (0=Mon..6=Sun) in the week that's
    ``week_offset`` weeks away from today's week (Monday-anchored)."""
    monday_this_week = today - timedelta(days=today.weekday())
    return monday_this_week + timedelta(weeks=week_offset, days=target_wd)


# ── Shared regex fragments ─────────────────────────────────────────────────

_NUM = r"(?:\d+|[零一二三四五六七八九十]+)"
# Day suffix: 号, 日, 'th', 'st', 'nd', 'rd'  (suffix is sometimes omitted in
# casual goals; we still REQUIRE it to avoid eating arbitrary digits like
# "前 50 条" or "Top 30").
_DAY_NUMBER_RE = re.compile(
    rf"({_NUM})\s*(?:号|日|th|st|nd|rd)\b",
    re.I,
)


def _extract_day_number(text: str, near_pos: int = 0, window: int = 0) -> int | None:
    """Find a day number 1-31 in ``text``.

    Two modes:
      * ``window == 0`` → scan the whole text (used by year-matcher fallback).
      * ``window > 0`` → only scan ``text[near_pos:near_pos+window]``. This
        is the proximity-based mode used by month-relative matchers so that
        a goal like "本月15号开始 下个月底完成" doesn't wrongly bind the
        "15号" to "下个月". Pick a window of ~12 chars to allow for an
        intervening "的" / "的第" etc.
    """
    if window > 0:
        scan = text[near_pos : near_pos + window]
    else:
        scan = text
    for m in _DAY_NUMBER_RE.finditer(scan):
        n = _parse_int(m.group(1))
        if n is not None and 1 <= n <= 31:
            return n
    return None


def _extract_month_number(text: str, near_pos: int = 0, window: int = 0) -> int | None:
    """Find a month number 1-12 in ``text`` requiring the 月 suffix."""
    if window > 0:
        scan = text[near_pos : near_pos + window]
    else:
        scan = text
    for m in re.finditer(rf"({_NUM})\s*月", scan):
        n = _parse_int(m.group(1))
        if n is not None and 1 <= n <= 12:
            return n
    return None


# ── Matcher 1: Month-relative + day ────────────────────────────────────────

# 下下下月X号 / 上上月Y号 / 三个月后的X号 / 下个月15号 / 下下个月3号
# Order alternatives longest-first so "下下个" wins over "下个" inside "下下个月":
#   "下下个月" — left-to-right scan tries 下下下个 (no), 下下下 (no),
#   下下个 (yes!) → group(1)="下下个", offset = +2 ✓
_NEXT_MONTH_RUN = re.compile(r"(下下下个|下下下|下下个|下下|下个|下)月")
_PREV_MONTH_RUN = re.compile(r"(上上上个|上上上|上上个|上上|上个|上)月")
_THIS_MONTH = re.compile(r"(本月|这个月|这月|当月)")

_N_MONTHS_FUTURE = re.compile(rf"({_NUM})\s*个?月\s*(?:之?后|以?后)")
_N_MONTHS_PAST = re.compile(rf"({_NUM})\s*个?月\s*(?:之?前|以?前)")

# English: "next month", "last month", "in 2 months", etc.
# NB: ``\b`` is unsafe when Chinese surrounds the phrase — Python 3's regex
# treats CJK as \w, so "查看in" has no \b before "in". Use Latin-only
# lookbehind / lookahead so the boundaries trigger on whitespace, CJK or BOS.
_EN_BOUNDARY_BEFORE = r"(?<![a-z0-9])"
_EN_BOUNDARY_AFTER = r"(?![a-z0-9])"
_EN_NEXT_MONTH = re.compile(
    rf"{_EN_BOUNDARY_BEFORE}(next|coming|following)\s+month{_EN_BOUNDARY_AFTER}",
    re.I,
)
_EN_PREV_MONTH = re.compile(
    rf"{_EN_BOUNDARY_BEFORE}(last|previous)\s+month{_EN_BOUNDARY_AFTER}",
    re.I,
)
_EN_IN_N_MONTHS = re.compile(
    rf"{_EN_BOUNDARY_BEFORE}in\s+(\d+)\s+months?{_EN_BOUNDARY_AFTER}",
    re.I,
)
_EN_N_MONTHS_AGO = re.compile(
    rf"{_EN_BOUNDARY_BEFORE}(\d+)\s+months?\s+ago{_EN_BOUNDARY_AFTER}",
    re.I,
)


def _run_to_offset(prefix: str) -> int:
    """Map a "下/下个/下下/下下下/上.../上上上" prefix to a signed month offset.

    The prefix passed in is **just the directional marker** (no "月" suffix),
    e.g. "下个", "下下", "下", "上上上". The optional "个" is dropped before
    counting characters so "下个" → 1 (not 2) and "下下个" never appears.
    """
    if not prefix:
        return 0
    sign = +1 if prefix.startswith("下") else -1
    body = prefix.replace("个", "")
    return sign * len(body)


def _match_relative_month_with_day(
    text: str, today: date
) -> Optional[RelativeDateMatch]:
    # Chinese: 下/上 prefix + day
    candidates: list[tuple[int, tuple[int, int]]] = []
    for pattern in (_NEXT_MONTH_RUN, _PREV_MONTH_RUN):
        m = pattern.search(text)
        if m:
            offset = _run_to_offset(m.group(1))
            candidates.append((offset, m.span()))
    # 本月X号
    m = _THIS_MONTH.search(text)
    if m:
        candidates.append((0, m.span()))
    # N个月后/前
    for pattern, sign in ((_N_MONTHS_FUTURE, +1), (_N_MONTHS_PAST, -1)):
        m = pattern.search(text)
        if m:
            n = _parse_int(m.group(1))
            if n is not None:
                candidates.append((sign * n, m.span()))

    for offset, span in candidates:
        day = _extract_day_number(text, near_pos=span[1], window=14)
        if day is None:
            continue
        anchor = today.replace(day=1)
        target_month = _add_months(anchor, offset)
        last = calendar.monthrange(target_month.year, target_month.month)[1]
        clamped = max(1, min(day, last))
        return RelativeDateMatch(
            phrase=text[span[0]:span[1]] + f"{clamped}号",
            target=date(target_month.year, target_month.month, clamped),
            delta_months=offset,
            confidence="high",
        )

    # English: next/previous/last + month + (the Xth | day N)
    en_candidates: list[tuple[int, tuple[int, int]]] = []
    if (m := _EN_NEXT_MONTH.search(text)):
        en_candidates.append((+1, m.span()))
    if (m := _EN_PREV_MONTH.search(text)):
        en_candidates.append((-1, m.span()))
    if (m := _EN_IN_N_MONTHS.search(text)):
        n = int(m.group(1))
        en_candidates.append((+n, m.span()))
    if (m := _EN_N_MONTHS_AGO.search(text)):
        n = int(m.group(1))
        en_candidates.append((-n, m.span()))

    for offset, span in en_candidates:
        # English picks: "the 15th of next month" → ordinal BEFORE the prefix;
        # "next month, the 15th" → ordinal AFTER. Try both windows.
        day = _extract_day_number(text, near_pos=span[1], window=20)
        if day is None and span[0] > 0:
            pre_start = max(0, span[0] - 20)
            day = _extract_day_number(
                text, near_pos=pre_start, window=span[0] - pre_start
            )
        if day is None:
            continue
        anchor = today.replace(day=1)
        target_month = _add_months(anchor, offset)
        last = calendar.monthrange(target_month.year, target_month.month)[1]
        clamped = max(1, min(day, last))
        return RelativeDateMatch(
            phrase=text[span[0]:span[1]] + f" {clamped}",
            target=date(target_month.year, target_month.month, clamped),
            delta_months=offset,
            confidence="high",
        )

    return None


# ── Matcher 2: Year-relative + month + day ────────────────────────────────

_YEAR_RELATIVE_CN = [
    ("大前年", -3), ("前年", -2), ("去年", -1), ("今年", 0),
    ("明年", 1), ("后年", 2), ("大后年", 3),
]


def _match_year_with_month_day(
    text: str, today: date
) -> Optional[RelativeDateMatch]:
    for phrase, year_offset in _YEAR_RELATIVE_CN:
        if phrase not in text:
            continue
        idx = text.index(phrase)
        # Year-relative phrases usually pin the month/day to within ~16 chars
        # right after them: "明年6月15号"、"明年的6月15号", etc.
        month = _extract_month_number(text, near_pos=idx + len(phrase), window=16)
        day = _extract_day_number(text, near_pos=idx + len(phrase), window=16)
        if month is None and day is None:
            # Pure 明年/去年 with no specifics — leave for matcher 6 fallback
            # so we always know what day to land on.
            target_year = today.year + year_offset
            return RelativeDateMatch(
                phrase=phrase,
                target=date(target_year, today.month, min(today.day, calendar.monthrange(target_year, today.month)[1])),
                delta_months=year_offset * 12,
                confidence="medium",
            )
        target_year = today.year + year_offset
        target_month = month if month is not None else today.month
        last = calendar.monthrange(target_year, target_month)[1]
        target_day = min(day, last) if day is not None else min(today.day, last)
        delta_m = (target_year - today.year) * 12 + (target_month - today.month)
        return RelativeDateMatch(
            phrase=phrase + (f"{target_month}月{target_day}号" if (month or day) else ""),
            target=date(target_year, target_month, target_day),
            delta_months=delta_m,
            confidence="high",
        )

    # English year-relative
    if re.search(
        rf"{_EN_BOUNDARY_BEFORE}next\s+year{_EN_BOUNDARY_AFTER}", text, re.I,
    ):
        return RelativeDateMatch(
            "next year", _safe_year_replace(today, +1), 12, "medium",
        )
    if re.search(
        rf"{_EN_BOUNDARY_BEFORE}(last|previous)\s+year{_EN_BOUNDARY_AFTER}",
        text, re.I,
    ):
        return RelativeDateMatch(
            "last year", _safe_year_replace(today, -1), -12, "medium",
        )
    return None


def _safe_year_replace(d: date, year_delta: int) -> date:
    """Year arithmetic that handles Feb 29 → Feb 28 in a non-leap year."""
    y = d.year + year_delta
    last = calendar.monthrange(y, d.month)[1]
    return date(y, d.month, min(d.day, last))


# ── Matcher 3: N-day arithmetic ───────────────────────────────────────────

_N_DAYS_FUTURE = re.compile(rf"({_NUM})\s*天\s*(?:之?后|以?后)")
_N_DAYS_PAST = re.compile(rf"({_NUM})\s*天\s*(?:之?前|以?前)")
_EN_IN_N_DAYS = re.compile(
    rf"{_EN_BOUNDARY_BEFORE}in\s+(\d+)\s+days?{_EN_BOUNDARY_AFTER}",
    re.I,
)
_EN_N_DAYS_AGO = re.compile(
    rf"{_EN_BOUNDARY_BEFORE}(\d+)\s+days?\s+ago{_EN_BOUNDARY_AFTER}",
    re.I,
)


def _match_n_days(text: str, today: date) -> Optional[RelativeDateMatch]:
    for pattern, sign in ((_N_DAYS_FUTURE, +1), (_N_DAYS_PAST, -1)):
        m = pattern.search(text)
        if m:
            n = _parse_int(m.group(1))
            if n is not None:
                return RelativeDateMatch(
                    phrase=m.group(0),
                    target=today + timedelta(days=sign * n),
                    delta_months=0,
                    confidence="high",
                )
    if (m := _EN_IN_N_DAYS.search(text)):
        n = int(m.group(1))
        return RelativeDateMatch(m.group(0), today + timedelta(days=n), 0, "high")
    if (m := _EN_N_DAYS_AGO.search(text)):
        n = int(m.group(1))
        return RelativeDateMatch(m.group(0), today - timedelta(days=n), 0, "high")
    return None


# ── Matcher 4: Simple day offsets ─────────────────────────────────────────

_DAY_OFFSETS = [
    # Most-specific first (otherwise "后天" matches inside "大后天")
    ("大后天", 3),
    ("大前天", -3),
    ("后天", 2),
    ("前天", -2),
    ("明天", 1), ("明日", 1),
    ("昨天", -1), ("昨日", -1),
    ("今天", 0), ("今日", 0),
    ("the day after tomorrow", 2),
    ("the day before yesterday", -2),
    ("tomorrow", 1),
    ("yesterday", -1),
    ("today", 0),
]


def _match_simple_day_offset(
    text: str, today: date
) -> Optional[RelativeDateMatch]:
    for phrase, offset in _DAY_OFFSETS:
        if phrase in text:
            return RelativeDateMatch(
                phrase=phrase,
                target=today + timedelta(days=offset),
                delta_months=0,
                confidence="high",
            )
    return None


# ── Matcher 5: Relative weekday ───────────────────────────────────────────

_WEEKDAY_CN = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
    "日": 6, "天": 6,
}
_WEEKDAY_EN = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_WEEK_PATTERN_CN = re.compile(
    r"(下下下|下下|下个|下|上上上|上上|上个|上|本|这个|这)"
    r"(?:周|星期|礼拜)([一二三四五六天日])"
)
_WEEK_PATTERN_EN = re.compile(
    rf"{_EN_BOUNDARY_BEFORE}(this|next|last)\s+"
    rf"(monday|tuesday|wednesday|thursday|friday|saturday|sunday){_EN_BOUNDARY_AFTER}",
    re.I,
)


def _week_prefix_to_offset(prefix: str) -> int:
    if prefix.startswith("下"):
        body = prefix.replace("个", "")
        return len(body)
    if prefix.startswith("上"):
        body = prefix.replace("个", "")
        return -len(body)
    return 0  # 本/这/这个


def _match_relative_weekday(text: str, today: date) -> Optional[RelativeDateMatch]:
    if (m := _WEEK_PATTERN_CN.search(text)):
        offset = _week_prefix_to_offset(m.group(1))
        target_wd = _WEEKDAY_CN[m.group(2)]
        d = _resolve_weekday(today, offset, target_wd)
        return RelativeDateMatch(
            phrase=m.group(0),
            target=d,
            delta_months=(d.year - today.year) * 12 + (d.month - today.month),
            confidence="high",
        )
    if (m := _WEEK_PATTERN_EN.search(text)):
        offset = {"this": 0, "next": 1, "last": -1}[m.group(1).lower()]
        target_wd = _WEEKDAY_EN[m.group(2).lower()]
        d = _resolve_weekday(today, offset, target_wd)
        return RelativeDateMatch(
            phrase=m.group(0),
            target=d,
            delta_months=(d.year - today.year) * 12 + (d.month - today.month),
            confidence="high",
        )
    return None


# ── Matcher 6: Month boundaries ───────────────────────────────────────────

_MONTH_PREFIX_OFFSETS = [
    ("下下下个月", 3), ("下下下月", 3),
    ("下下个月", 2), ("下下月", 2),
    ("下个月", 1), ("下月", 1),
    ("上上上个月", -3), ("上上上月", -3),
    ("上上个月", -2), ("上上月", -2),
    ("上个月", -1), ("上月", -1),
    ("本月", 0), ("这个月", 0), ("当月", 0),
]


def _match_month_boundary(text: str, today: date) -> Optional[RelativeDateMatch]:
    # Chinese: prefix + 底/初/中/最后一天/第一天
    for prefix, offset in _MONTH_PREFIX_OFFSETS:
        if prefix not in text:
            continue
        idx = text.index(prefix)
        suffix_window = text[idx + len(prefix): idx + len(prefix) + 8]
        shifted = _add_months(today.replace(day=1), offset)
        if "底" in suffix_window or "最后一天" in suffix_window or "末" in suffix_window:
            return RelativeDateMatch(
                phrase=prefix + "底",
                target=_last_day_of_month(shifted),
                delta_months=offset,
                confidence="high",
            )
        if "初" in suffix_window or "第一天" in suffix_window or "1号" in suffix_window:
            return RelativeDateMatch(
                phrase=prefix + "初",
                target=_first_day_of_month(shifted),
                delta_months=offset,
                confidence="high",
            )
        if "中" in suffix_window:
            mid = date(shifted.year, shifted.month, 15)
            return RelativeDateMatch(
                phrase=prefix + "中",
                target=mid,
                delta_months=offset,
                confidence="medium",
            )
    # Bare 月底/月初 with no prefix → assume current month
    if "月底" in text or "本月最后" in text:
        return RelativeDateMatch(
            phrase="月底",
            target=_last_day_of_month(today),
            delta_months=0,
            confidence="medium",
        )
    if "月初" in text:
        return RelativeDateMatch(
            phrase="月初",
            target=_first_day_of_month(today),
            delta_months=0,
            confidence="medium",
        )
    # English
    if re.search(
        rf"{_EN_BOUNDARY_BEFORE}end of (?:the\s+)?month{_EN_BOUNDARY_AFTER}",
        text, re.I,
    ):
        return RelativeDateMatch(
            "end of month", _last_day_of_month(today), 0, "medium"
        )
    if re.search(
        rf"{_EN_BOUNDARY_BEFORE}(?:start|beginning) of (?:the\s+)?month"
        rf"{_EN_BOUNDARY_AFTER}",
        text, re.I,
    ):
        return RelativeDateMatch(
            "start of month", _first_day_of_month(today), 0, "medium"
        )
    return None


# ── Public API ────────────────────────────────────────────────────────────

# Order: most specific first (relative-month-WITH-DAY beats bare month-boundary;
# year+month+day beats simple day-offset). The first non-None wins.
_MATCHERS: list[Callable[[str, date], Optional[RelativeDateMatch]]] = [
    _match_relative_month_with_day,
    _match_year_with_month_day,
    _match_relative_weekday,
    _match_n_days,
    _match_month_boundary,
    _match_simple_day_offset,
]


def resolve_relative_date(
    goal: str,
    today: date | None = None,
) -> RelativeDateMatch | None:
    """Resolve any recognised relative-date phrase in ``goal`` → absolute date.

    Returns ``None`` when no phrase matched. The agent's existing FORM_SKILL
    guidance still applies in that case — this resolver is purely additive.

    The resolver is conservative: every matcher requires unambiguous evidence
    (e.g. "下个月" without a day still goes to month-boundary, not month+day).
    Whitespace is stripped before matching to handle goals like
    ``下 个 月 1 5 号``.
    """
    today = today or date.today()
    raw = str(goal or "")
    if not raw.strip():
        return None
    # Normalise whitespace to single spaces (do NOT strip entirely — English
    # matchers like "in 5 days" / "next wednesday" need at least one space
    # between tokens). Chinese matchers tolerate this — they use literal
    # character matches, not \s constraints, between Chinese chars.
    text = re.sub(r"\s+", " ", raw).strip().lower()
    if not text:
        return None
    for matcher in _MATCHERS:
        result = matcher(text, today)
        if result is not None:
            return result
    return None


def format_resolver_hint(match: RelativeDateMatch) -> str:
    """Render a ``RelativeDateMatch`` as the goal-injection block.

    The format is a stable contract — ``select_skills`` looks for the
    "【相对日期解析】" marker as a fast-path trigger. Future format changes
    must keep that marker as the first line of the block.
    """
    sign = "+" if match.delta_months > 0 else ""
    delta_part = (
        f"，与本月差值: {sign}{match.delta_months} 个月"
        if match.delta_months
        else ""
    )
    return (
        "\n\n【相对日期解析】（系统自动计算，请直接在日历上匹配此绝对日期）\n"
        f"· 识别短语: \"{match.phrase}\"\n"
        f"· 绝对日期: {match.target.isoformat()}（"
        f"{match.target.year}年"
        f"{match.target.month}月"
        f"{match.target.day}日{delta_part}）\n"
        f"· 置信度: {match.confidence}"
    )

"""Generic extraction helpers for repeated semantic cards.

This module is intentionally field-driven. It does not know about one
particular weather site; it recognizes repeated card text when the requested
schema asks for date/day, condition/status, temperature, or wind-like values.
"""

from __future__ import annotations

import math
import re
from typing import Any


_TEMP_UNIT_RE = r"(?:℃|℉|°C|°|C\b)"
_TEMP_RE = re.compile(rf"([-+]?\d+(?:\.\d+)?)\s*({_TEMP_UNIT_RE})", re.I)
_TEMP_RANGE_RE = re.compile(
    r"([-+]?\d+(?:\.\d+)?)\s*(?:/|／|~|～|-|至|到)\s*"
    rf"([-+]?\d+(?:\.\d+)?)\s*({_TEMP_UNIT_RE})",
    re.I,
)
_DATE_LABEL_RE = re.compile(
    r"(今天|今日|明天|后天|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"\btoday\b|\btomorrow\b|\bday\s+after\s+tomorrow\b|"
    r"\bmon(?:day)?\b|\btue(?:sday)?\b|\bwed(?:nesday)?\b|\bthu(?:rsday)?\b|"
    r"\bfri(?:day)?\b|\bsat(?:urday)?\b|\bsun(?:day)?\b)",
    re.I,
)
_DATE_RE = re.compile(r"(\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}[/-]\d{1,2})")
_GENERIC_DATE_RE = re.compile(
    r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2})(?!\s*(?:℃|℉|°|C\b|F\b))\b",
    re.I,
)
_WIND_RE = re.compile(
    r"((?:[东南西北中]{1,3}风|无持续风向|微风|"
    r"[a-z ]{0,24}wind)?\s*(?:<\s*)?\d+\s*(?:[-~～]\s*(?:<\s*)?\d+)?\s*级"
    r"(?:\s*转\s*(?:<\s*)?\d+\s*级)?)",
    re.I,
)
_WIND_EN_RE = re.compile(
    r"\b(?:wind|winds?)\s*[:：=-]?\s*(?:level\s*)?"
    r"(?:<\s*)?\d+\s*(?:[-~～]\s*(?:<\s*)?\d+)?"
    r"(?:\s*(?:level|levels|mph|km/h|kph|级))?\b",
    re.I,
)
_WEATHER_WORDS = (
    "雷阵雨",
    "雨夹雪",
    "沙尘暴",
    "特大暴雨",
    "大暴雨",
    "暴雨",
    "大雨",
    "中雨",
    "小雨",
    "阵雨",
    "大雪",
    "中雪",
    "小雪",
    "阵雪",
    "多云",
    "晴",
    "阴",
    "雾",
    "霾",
    "扬沙",
    "浮尘",
    "sunny",
    "cloudy",
    "overcast",
    "rain",
    "showers",
    "thunderstorm",
    "snow",
    "fog",
    "haze",
)
_WEATHER_RE = re.compile(
    r"("
    + "|".join(re.escape(word) for word in sorted(_WEATHER_WORDS, key=len, reverse=True))
    + r")(?:\s*转\s*("
    + "|".join(re.escape(word) for word in sorted(_WEATHER_WORDS, key=len, reverse=True))
    + r"))*",
    re.I,
)
_SECTION_STOP_RE = re.compile(
    r"(分时段预报|逐小时|小时预报|生活指数|天气资讯|周边地区|周边景点|"
    r"高清图集|重大天气事件|查看更多|weather\s+news|hourly\s+forecast|"
    r"life\s+index|nearby)",
    re.I,
)
_RELATIVE_DAY_LABELS = {"今天", "今日", "明天", "后天"}
_WEEKDAY_LABEL_RE = re.compile(
    r"^(周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)$",
    re.I,
)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _field_key(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _parse_card_horizon(goal: str) -> int | None:
    text = str(goal or "")
    lower = text.lower()
    if all(label in text for label in ("今天", "明天", "后天")):
        return 3
    if all(label in lower for label in ("today", "tomorrow")) and re.search(
        r"day\s+after\s+tomorrow|dayafter\s+tomorrow",
        lower,
    ):
        return 3
    if re.search(r"前\s*三\s*天|前三天|未来\s*三\s*天|接下来\s*三\s*天", text):
        return 3
    if re.search(r"一\s*周|本周|下周|weekly|week\s+forecast", text, re.I):
        return 7
    patterns = (
        r"(?:未来|接下来|连续|前|近|后续)?\s*(\d{1,2})\s*(?:天|日)\s*(?:天气|预报|forecast)",
        r"(?:天气|预报|forecast)\s*(?:未来|接下来|连续|前|近|后续)?\s*(\d{1,2})\s*(?:天|日)",
        r"(?:next|future|coming|following|first|top)\s*(\d{1,2})\s*days?\b",
        r"(\d{1,2})\s*days?\s*(?:forecast|weather)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            count = int(match.group(1))
            if 1 <= count <= 31:
                return count
    return None


def _field_kind(field: str) -> str:
    key = _field_key(field)
    if not key:
        return ""
    if any(marker in key for marker in ("wind", "风力", "风速", "风级", "风")):
        return "wind"
    if any(marker in key for marker in ("hightemp", "maxtemp", "highest", "maximum", "最高", "高温")):
        return "high_temp"
    if any(marker in key for marker in ("lowtemp", "mintemp", "lowest", "minimum", "最低", "低温")):
        return "low_temp"
    if any(marker in key for marker in ("temperature", "temp", "温度", "气温")):
        return "temperature"
    if any(marker in key for marker in ("weather", "condition", "phenomenon", "status", "天气", "现象")):
        return "weather"
    if any(marker in key for marker in ("daylabel", "day", "date", "period", "日期", "星期", "时间")):
        return "day"
    return ""


def _should_attempt_card_extraction(requested_fields: list[str], goal: str) -> bool:
    kinds = {_field_kind(field) for field in requested_fields or []}
    kinds.discard("")
    if len(kinds & {"day", "weather", "high_temp", "low_temp", "temperature", "wind"}) >= 2:
        return True
    goal_key = _field_key(goal)
    return bool(
        {"天气", "预报", "温度", "气温", "风力", "forecast", "weather", "temperature"}
        & set(re.findall(r"[a-z]+|[\u4e00-\u9fff]{2}", goal_key))
    )


def _text_blocks_from_source(source_text: str) -> list[str]:
    text = str(source_text or "").strip()
    if not text:
        return []
    blocks = re.split(r"\n\s*\n(?=Item\s+\d+\s*:)", text)
    cleaned: list[str] = []
    for block in blocks:
        block = re.sub(r"(?m)^\s*(source_url|detail_url|primary_url|url)\s*:\s*\S+\s*$", "", block)
        block = re.sub(r"(?m)^\s*Item\s+\d+\s*:\s*", "", block)
        block = _clean(block)
        if block and len(block) >= 8:
            cleaned.append(block)
    return cleaned


def _forecast_blocks_from_text(source_text: str, horizon: int | None = None) -> list[str]:
    text = _clean(source_text)
    if not text:
        return []
    anchor_re = re.compile(
        r"(?:\d{1,2}\s*[月/-]?\s*\d{0,2}\s*[日号]?\s*[（(]\s*)?"
        r"(今天|今日|明天|后天|周[一二三四五六日天]|星期[一二三四五六日天]|"
        r"\btoday\b|\btomorrow\b|\bday\s+after\s+tomorrow\b|"
        r"\bmon(?:day)?\b|\btue(?:sday)?\b|\bwed(?:nesday)?\b|\bthu(?:rsday)?\b|"
        r"\bfri(?:day)?\b|\bsat(?:urday)?\b|\bsun(?:day)?\b)"
        r"(?:\s*[）)])?",
        re.I,
    )
    first_anchor = anchor_re.search(text)
    if first_anchor:
        stop = _SECTION_STOP_RE.search(text, first_anchor.end())
        if stop:
            text = text[: stop.start()]
    anchors = list(anchor_re.finditer(text))
    blocks: list[str] = []
    include_weekdays = bool(horizon and horizon > 3)
    for index, match in enumerate(anchors):
        label = _clean(match.group(1))
        is_relative = label in _RELATIVE_DAY_LABELS or label.lower() in {
            "today",
            "tomorrow",
            "day after tomorrow",
        }
        if not is_relative and not (include_weekdays and _WEEKDAY_LABEL_RE.match(label)):
            continue
        start = match.start()
        end = anchors[index + 1].start() if index + 1 < len(anchors) else min(len(text), start + 140)
        block = _clean(text[start:end])
        if len(block) > 180:
            block = block[:180]
        if _TEMP_RE.search(block) and (_WIND_RE.search(block) or _WEATHER_RE.search(block)):
            blocks.append(block)
        if horizon and len(blocks) >= horizon:
            break
    return blocks


def _clean_table_like_line(line: str) -> str:
    text = re.sub(r"(?i)^\s*(item|card|row|cell|text|generic|paragraph)\s*\d*\s*[:：-]?\s*", "", str(line or ""))
    text = re.sub(r"^\s*[-*•]+\s*", "", text)
    text = text.strip(" \t|")
    return _clean(text)


def _line_has_day_or_date(line: str) -> bool:
    return bool(_DATE_LABEL_RE.search(line) or _GENERIC_DATE_RE.search(line))


def _looks_like_forecast_block(block: str) -> bool:
    text = _clean(block)
    if not text:
        return False
    if not _TEMP_RE.search(text):
        return False
    if not (_WIND_RE.search(text) or _WIND_EN_RE.search(text) or _WEATHER_RE.search(text)):
        return False
    return _line_has_day_or_date(text)


def _forecast_blocks_from_table_like_text(
    source_text: str,
    horizon: int | None = None,
) -> list[str]:
    """Recover forecast rows from AX/table text that preserves row-ish lines."""
    raw_lines = str(source_text or "").splitlines()
    lines = [
        _clean_table_like_line(line)
        for line in raw_lines
    ]
    lines = [
        line for line in lines
        if line and not _SECTION_STOP_RE.search(line)
    ]
    if not lines:
        return []

    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _line_has_day_or_date(line):
            index += 1
            continue

        collected = [line]
        cursor = index + 1
        while cursor < len(lines) and cursor < index + 8:
            next_line = lines[cursor]
            if _line_has_day_or_date(next_line) and _looks_like_forecast_block(
                " ".join(collected)
            ):
                break
            collected.append(next_line)
            joined = " ".join(collected)
            has_wind = bool(_WIND_RE.search(joined) or _WIND_EN_RE.search(joined))
            next_is_new_day = (
                cursor + 1 >= len(lines)
                or _line_has_day_or_date(lines[cursor + 1])
            )
            if _looks_like_forecast_block(joined) and (has_wind or next_is_new_day):
                break
            cursor += 1

        block = _clean(" ".join(collected))
        if _looks_like_forecast_block(block):
            blocks.append(block[:220])
            if horizon and len(blocks) >= horizon:
                break
            index = max(cursor, index + 1)
        else:
            index += 1

    return blocks


def _text_blocks_from_rows(rows: list[dict]) -> list[str]:
    blocks: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        parts: list[str] = []
        for key, value in row.items():
            if value is None:
                continue
            key_text = str(key or "").lower()
            if key_text.endswith("url") or key_text in {"url", "href", "link"}:
                continue
            parts.append(str(value))
        text = _clean(" ".join(parts))
        if text and len(text) >= 8:
            blocks.append(text)
    return blocks


def _extract_day_label(text: str) -> str:
    match = _DATE_LABEL_RE.search(text)
    if match:
        return _clean(match.group(1))
    match = _DATE_RE.search(text)
    if match:
        return _clean(match.group(1))
    match = _GENERIC_DATE_RE.search(text)
    return _clean(match.group(0)) if match else ""


def _extract_temperatures(text: str) -> tuple[str, str, str]:
    values: list[tuple[float, str]] = []
    covered_spans: list[tuple[int, int]] = []

    for match in _TEMP_RANGE_RE.finditer(text):
        unit = match.group(3)
        unit = "℃" if unit.lower() in {"°c", "c"} else unit
        for group_idx in (1, 2):
            raw_num = match.group(group_idx)
            try:
                num = float(raw_num)
            except ValueError:
                continue
            rendered = f"{int(num) if num.is_integer() else num:g}{unit}"
            values.append((num, rendered))
        covered_spans.append(match.span())

    for match in _TEMP_RE.finditer(text):
        if any(start <= match.start() < end for start, end in covered_spans):
            continue
        raw_num = match.group(1)
        unit = match.group(2)
        try:
            num = float(raw_num)
        except ValueError:
            continue
        unit = "℃" if unit.lower() in {"°c", "c"} else unit
        rendered = f"{int(num) if num.is_integer() else num:g}{unit}"
        values.append((num, rendered))
    if not values:
        return "", "", ""
    high = max(values, key=lambda item: item[0])[1]
    low = min(values, key=lambda item: item[0])[1]
    combined = " / ".join(item[1] for item in values[:2])
    return high, low, combined


def _extract_wind(text: str) -> str:
    matches = [_clean(match.group(1)) for match in _WIND_RE.finditer(text)]
    matches = [match for match in matches if match and not re.fullmatch(r"\d+\s*级", match)]
    if matches:
        value = max(matches, key=len)
        level = re.search(r"((?:<\s*)?\d+\s*(?:[-~～]\s*(?:<\s*)?\d+)?\s*级(?:\s*转\s*(?:<\s*)?\d+\s*级)?)", value)
        return _clean(level.group(1)) if level else value
    english = _WIND_EN_RE.search(text)
    if english:
        return _clean(english.group(0))
    fallback = re.search(r"(?:风力|风级|风)\s*[:：]?\s*([^\s,，。；;]{1,24})", text)
    return _clean(fallback.group(1)) if fallback else ""


def _extract_weather(text: str) -> str:
    matches: list[str] = []
    for match in _WEATHER_RE.finditer(text):
        value = _clean(match.group(0))
        if value:
            matches.append(value)
    if matches:
        return max(matches, key=len)

    for part in re.split(r"[。；;,\n]| {2,}", text):
        candidate = _clean(part)
        if not candidate or len(candidate) > 40:
            continue
        if _TEMP_RE.search(candidate) or _WIND_RE.search(candidate) or _DATE_LABEL_RE.search(candidate):
            continue
        if re.search(r"[a-zA-Z\u4e00-\u9fff]", candidate):
            return candidate
    return ""


def _project_card(parsed: dict[str, str], requested_fields: list[str]) -> dict[str, str]:
    row: dict[str, str] = {}
    for field in requested_fields or []:
        kind = _field_kind(field)
        if kind == "day":
            value = parsed.get("day", "")
        elif kind == "weather":
            value = parsed.get("weather", "")
        elif kind == "high_temp":
            value = parsed.get("high_temp", "") or parsed.get("temperature", "")
        elif kind == "low_temp":
            value = parsed.get("low_temp", "") or parsed.get("temperature", "")
        elif kind == "temperature":
            value = parsed.get("temperature", "") or parsed.get("high_temp", "") or parsed.get("low_temp", "")
        elif kind == "wind":
            value = parsed.get("wind", "")
        else:
            value = ""
        if value:
            row[field] = value
    return row


def extract_semantic_card_rows(
    rows: list[dict],
    *,
    source_text: str = "",
    requested_fields: list[str] | None = None,
    goal: str = "",
) -> tuple[list[dict], str]:
    """Build structured rows from repeated forecast/status cards.

    Returns an empty result unless the requested schema indicates that a card
    parser is relevant. This keeps article/search/table list extraction intact.
    """
    fields = [str(field) for field in (requested_fields or []) if str(field or "").strip()]
    if not fields or not _should_attempt_card_extraction(fields, goal):
        return [], ""

    horizon = _parse_card_horizon(goal)
    blocks = _forecast_blocks_from_text(source_text, horizon=horizon)
    if not blocks:
        blocks = _forecast_blocks_from_table_like_text(source_text, horizon=horizon)
    if not blocks:
        blocks = _text_blocks_from_source(source_text) or _text_blocks_from_rows(rows)
    if not blocks:
        return [], ""

    extracted: list[dict] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    known_fields = sum(1 for field in fields if _field_kind(field))
    minimum_hits = (
        len(fields)
        if known_fields == len(fields)
        else max(2, int(math.ceil(max(known_fields, len(fields)) * 0.75)))
    )
    for block in blocks:
        high, low, temperature = _extract_temperatures(block)
        parsed = {
            "day": _extract_day_label(block),
            "weather": _extract_weather(block),
            "high_temp": high,
            "low_temp": low,
            "temperature": temperature,
            "wind": _extract_wind(block),
        }
        row = _project_card(parsed, fields)
        hits = sum(1 for field in fields if _clean(row.get(field)))
        if hits < minimum_hits:
            continue
        key = tuple(sorted((field, _clean(value)) for field, value in row.items()))
        if key in seen:
            continue
        seen.add(key)
        extracted.append(row)
        if horizon and len(extracted) >= horizon:
            break

    if not extracted:
        return [], ""
    card_text = "\n\n".join(
        f"Card {idx + 1}: "
        + "; ".join(f"{key}={value}" for key, value in row.items())
        for idx, row in enumerate(extracted)
    )
    return extracted, card_text

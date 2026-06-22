from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


def extract_core_goal(goal: str) -> str:
    if not goal:
        return ""
    parts = re.split(r"\n\s*\n【[^】]+】\s*\n?", str(goal), maxsplit=1)
    return (parts[0] if parts else str(goal)).strip()


def parse_goal_target_count(goal: str) -> int | None:
    quant = r"条|个|项|篇|则|部|家|名|位|款|本|场|首"
    m = re.search(rf"(?:前|共|取|抓)\s*(\d+)\s*(?:{quant})", goal)
    if m:
        return int(m.group(1))
    m = re.search(
        rf"(\d+)\s*(?:{quant})\s*(?:数据|内容|信息|记录|新闻|商品|评论|电影|答案|回答|文章|视频|结果|店铺)",
        goal,
    )
    if m:
        return int(m.group(1))
    m = re.search(r"(?:排名)?前\s*(\d+)\s*(?:的|名)", goal)
    if m:
        return int(m.group(1))
    m = re.search(r"[Tt]op\s*(\d+)\b", goal)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*(?:records?|items?|rows?|entries|results?)\b", goal, re.I)
    if m:
        return int(m.group(1))
    if re.search(r"前\s*三\s*天|前三天", goal):
        return 3
    if all(label in goal for label in ("今天", "明天", "后天")):
        return 3
    goal_lower = str(goal or "").lower()
    if all(label in goal_lower for label in ("today", "tomorrow")) and re.search(
        r"day\s+after\s+tomorrow|dayafter\s+tomorrow",
        goal_lower,
    ):
        return 3
    if re.search(r"一\s*周|本周|下周|weekly|week\s+forecast", goal, re.I):
        return 7
    day_patterns = (
        r"(?:未来|接下来|连续|前|近|后续)?\s*(\d{1,2})\s*(?:天|日)\s*(?:天气|预报|forecast)",
        r"(?:天气|预报|forecast)\s*(?:未来|接下来|连续|前|近|后续)?\s*(\d{1,2})\s*(?:天|日)",
        r"(?:next|future|coming|following|first|top)\s*(\d{1,2})\s*days?\b",
        r"(\d{1,2})\s*days?\s*(?:forecast|weather)\b",
    )
    for pattern in day_patterns:
        m = re.search(pattern, goal, re.I)
        if m:
            count = int(m.group(1))
            if 1 <= count <= 31:
                return count
    return None


def parse_goal_target_pages(goal: str) -> int | None:
    m = re.search(r"(?:前|共|取|抓|提取|获取)?\s*(\d+)\s*页", goal)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:first|top|extract|get|scrape)\s*(\d+)\s*pages?\b", goal, re.I)
    if m:
        return int(m.group(1))
    return None


def extraction_targets_reached(
    goal: str,
    *,
    total_rows: int = 0,
    total_pages: int = 0,
) -> dict[str, object]:
    row_target = parse_goal_target_count(goal)
    page_target = parse_goal_target_pages(goal)
    rows_reached = row_target is not None and total_rows >= row_target
    pages_reached = page_target is not None and total_pages >= page_target
    return {
        "reached": bool(rows_reached or pages_reached),
        "rows_reached": rows_reached,
        "pages_reached": pages_reached,
        "row_target": row_target,
        "page_target": page_target,
        "total_rows": total_rows,
        "total_pages": total_pages,
    }


def normalize_output_field_key(value: object) -> str:
    return re.sub(
        r"[^a-z0-9\u4e00-\u9fff]+",
        "",
        str(value or "").strip().lower(),
    )


def parse_goal_requested_fields(goal: str) -> list[str]:
    text = extract_core_goal(goal)
    full = str(goal or "")
    if not full:
        return []
    patterns = (
        r"(?:字段|列名|列|表头|fields?|columns?)\s*(?:为|是|包括|包含|只要|仅保留|:|：|=)\s*([^。\n；;]+)",
        r"(?:提取|抓取|获取|保存|导出)\s*(?:以下|这些|指定)?\s*(?:字段|列|fields?|columns?)\s*(?:[:：为是=])?\s*([^。\n；;]+)",
        r"(?:with|including|only)\s+(?:fields?|columns?)\s*(?:[:：=])?\s*([^.\n;]+)",
    )
    raw = ""
    # The explicit "字段:/fields:" spec is authoritative; search the FULL goal so a
    # spec living in the 【输出要求】 section (which extract_core_goal strips) is
    # still captured. Otherwise field names mis-parse from the natural-language
    # goal and every extracted row is dropped as "under-complete".
    for pattern in patterns:
        match = re.search(pattern, full, flags=re.IGNORECASE)
        if match:
            raw = match.group(1)
            break
    if not raw:
        match = re.search(
            r"(?:提取|抓取|获取|保存|导出)\s+([^。\n；;]{2,160}?)\s*(?:字段|列|fields?\b|columns?\b)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            raw = match.group(1)
    if not raw:
        matches = re.findall(
            r"的([^。\n；;]{2,120}?)(?=，?\s*(?:保存|导出|写入|存入|并保存|并导出)|[。\n；;]|$)",
            text,
            flags=re.IGNORECASE,
        )
        field_markers = (
            "标题", "名称", "名字", "评分", "评价", "人数", "简介", "摘要",
            "作者", "时间", "链接", "网址", "title", "name", "rating",
            "score", "review", "summary", "description", "url",
        )
        for candidate in reversed(matches):
            if any(marker.lower() in candidate.lower() for marker in field_markers):
                raw = candidate
                break
    if not raw:
        return []
    # Drop parenthetical descriptions before splitting so "quote(名言文字), author(作者)"
    # yields machine field names ["quote", "author"] (handled on the whole string to
    # survive the trailing-bracket strip below).
    raw = re.sub(r"[（(][^（()）]*[)）]", "", raw)
    raw = re.split(
        r"\s*(?:并(?:保存|导出|写入|存入)?|然后|再|保存到|导出到|写入|存入|to\s+excel|as\s+excel)\s*",
        raw,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    raw = raw.strip(" ：:=[({【\"'`“”‘’")
    raw = raw.strip(" )]}】\"'`“”‘’")
    if not raw:
        return []
    parts = re.split(r"[,，、;；|/]+|\s+(?:and|or)\s+|(?:以及|和|及)", raw)
    fields: list[str] = []
    seen: set[str] = set()
    stop_words = {"数据", "内容", "信息", "记录", "excel", "xlsx", "csv"}
    for part in parts:
        field = re.sub(r"\s+", " ", part).strip(" ：:=[({【\"'`“”‘’)]}】")
        if not field:
            continue
        field = re.sub(r"^(?:and|or|和|及|以及)\s+", "", field, flags=re.IGNORECASE).strip()
        field = re.sub(r"\s+(?:and|or|和|及|以及)$", "", field, flags=re.IGNORECASE).strip()
        norm = normalize_output_field_key(field)
        if not norm or norm in stop_words or norm in seen:
            continue
        if len(field) > 50:
            continue
        seen.add(norm)
        fields.append(field)
    return fields


def normalize_guard_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return url[:120]

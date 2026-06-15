"""Answer domain utilities extracted from main.py (Slice 1).

Functions for formatting extracted rows as answers, detecting answer domains
(weather/stock/recipe/flight), compact answer text generation, and Baidu
search fast-path logic.
"""
import logging
import re
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlsplit
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..browser_env import BrowserEnv
    from ..event_stream import EventStream

logger = logging.getLogger("vspider.main")

try:
    from ..browser_env import BrowserEnv
    from ..event_stream import EventStream
except ImportError:
    from browser_env import BrowserEnv  # type: ignore[no-redef]
    from event_stream import EventStream  # type: ignore[no-redef]


def _format_extracted_rows_as_answer(rows: object) -> str:
    """Format structured answer-mode rows for the Final Answer panel."""
    if rows is None:
        return ""
    if isinstance(rows, dict):
        direct_answer = rows.get("answer")
        if direct_answer:
            return str(direct_answer).strip()
        row_list: list[object] = [rows]
    elif isinstance(rows, list):
        row_list = rows
    else:
        return str(rows).strip()

    lines: list[str] = []
    for raw_row in row_list[:5]:
        if isinstance(raw_row, dict):
            direct_answer = raw_row.get("answer")
            if direct_answer:
                lines.append(str(direct_answer).strip())
                continue
            weather = str(
                raw_row.get("weather")
                or raw_row.get("天气")
                or raw_row.get("condition")
                or ""
            ).strip()
            temperature = str(
                raw_row.get("temperature")
                or raw_row.get("气温")
                or raw_row.get("temp")
                or ""
            ).strip()
            rain_probability = str(
                raw_row.get("rain_probability")
                or raw_row.get("降雨概率")
                or raw_row.get("precipitation_probability")
                or ""
            ).strip()
            if weather or temperature or rain_probability:
                weather_line = ""
                if weather:
                    weather_line = (
                        f"会下雨，天气为{weather}"
                        if "雨" in weather
                        else f"天气为{weather}"
                    )
                detail_parts = [weather_line] if weather_line else []
                if temperature:
                    detail_parts.append(f"气温 {temperature}")
                if rain_probability:
                    detail_parts.append(f"降雨概率 {rain_probability}")
                lines.append("；".join(detail_parts) + "。")
                continue
            parts = [
                f"{key}: {value}"
                for key, value in raw_row.items()
                if str(value or "").strip()
                and str(key or "").strip()
                not in {"source", "page_url", "url", "output_file"}
            ]
            if parts:
                lines.append("- " + "; ".join(parts))
        elif str(raw_row or "").strip():
            lines.append("- " + str(raw_row).strip())
    if len(row_list) > 5:
        lines.append(f"...共 {len(row_list)} 条")
    return "\n".join(line for line in lines if line).strip()


_WEATHER_GOAL_KEYWORDS = (
    "天气",
    "气温",
    "温度",
    "下雨",
    "有雨",
    "降雨",
    "降水",
)

_WEATHER_CONDITION_RE = (
    r"雷阵雨|阵雨|小雨|中雨|大雨|暴雨|雷雨|雨夹雪|小雪|中雪|大雪|"
    r"多云|晴|阴天?|雾|霾|沙尘|浮尘|扬沙|雨|雪"
)


def _clean_weather_city_candidate(candidate: str) -> str:
    text = str(candidate or "").strip()
    text = re.sub(r"[\s，,。！？?；;：:、（）()【】\[\]\"'“”‘’]+", "", text)
    text = re.sub(
        r"^(?:帮我|请|麻烦|帮忙|能不能|可以|给我|想知道)+",
        "",
        text,
    )
    text = re.sub(
        r"(?:查一下|查询|查查|查|看一下|看看|看|告诉我|了解一下|一下)",
        "",
        text,
    )
    text = re.sub(
        r"(?:今天|明天|后天|明日|未来一周|未来七天|未来7天|未来三天|未来3天)",
        "",
        text,
    )
    text = re.sub(r"(?:天气预报|天气|气温|温度|预报|的)$", "", text)
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z·]", "", text)
    if len(text) > 24:
        text = text[-24:]
    if text in {"天气", "气温", "温度", "下雨", "有雨", "降雨", "降水"}:
        return ""
    return text if len(text) >= 2 else ""


def _extract_weather_city_from_goal(goal: str) -> str:
    text = re.sub(r"\s+", "", str(goal or ""))
    if not any(keyword in text for keyword in _WEATHER_GOAL_KEYWORDS):
        return ""

    rain_match = re.search(
        r"(.{0,60}?)(?:会不会|是否|有没有|有无|会有|可能会)"
        r".{0,8}(?:下雨|有雨|降雨|降水|雨)",
        text,
    )
    if rain_match:
        city = _clean_weather_city_candidate(rain_match.group(1))
        if city:
            return city

    patterns = [
        r"(?:今天|明天|后天|明日)\s*([\u4e00-\u9fffA-Za-z·]{2,30}?)(?:的)?(?:天气|气温|温度|预报)",
        r"([\u4e00-\u9fffA-Za-z·]{2,30}?)(?:今天|明天|后天|明日)(?:的)?(?:天气|气温|温度|预报|会不会|是否|有没有|有无)",
        r"(?:查一下|查询|查查|查|看看|看一下)?\s*([\u4e00-\u9fffA-Za-z·]{2,30}?)(?:的)?(?:天气|气温|温度|天气预报)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        city = _clean_weather_city_candidate(match.group(1))
        if city:
            return city
    return ""


def _infer_answer_weather_search_query(goal: str) -> str:
    """Infer a clean search query for answer-only weather lookups."""
    city = _extract_weather_city_from_goal(goal)
    if not city:
        return ""
    text = str(goal or "")
    if re.search(r"未来\s*(?:一|七|7)\s*天|一周|七天|7天", text):
        horizon = "未来一周"
    elif re.search(r"未来\s*(?:三|3)\s*天|三天|3天", text):
        horizon = "未来三天"
    elif "后天" in text:
        horizon = "后天"
    elif "今天" in text:
        horizon = "今天"
    elif "明天" in text or "明日" in text:
        horizon = "明天"
    else:
        horizon = ""
    return f"{city}{horizon}天气" if horizon else f"{city}天气"


def _baidu_query_from_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
        params = parse_qs(parsed.query or "")
    except Exception:
        return ""
    for key in ("wd", "word", "q", "query"):
        values = params.get(key) or []
        if values:
            return unquote_plus(str(values[0] or "")).strip()
    return ""


def _is_baidu_url(url: str) -> bool:
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except Exception:
        return False
    return host == "baidu.com" or host.endswith(".baidu.com")


def _normalize_search_query(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _infer_answer_stock_search_query(goal: str) -> str:
    """Infer a clean baidu search query for stock-flavoured answer goals."""
    name = _extract_stock_subject_from_goal(goal)
    if not name:
        return ""
    return f"{name} 股价"


def _infer_answer_recipe_search_query(goal: str) -> str:
    """Infer a clean baidu search query for recipe-flavoured answer goals."""
    dish = _extract_recipe_name_from_goal(goal)
    if not dish:
        return ""
    return f"{dish} 做法"


def _infer_answer_flight_search_query(goal: str) -> str:
    """Infer a clean baidu search query for flight-flavoured answer goals."""
    flight = _extract_flight_number_from_goal(goal)
    if not flight:
        return ""
    return f"{flight} 航班动态"


def _build_answer_search_fast_path_url(
    goal: str,
    *,
    start_url: str,
    current_url: str,
    output_mode: str,
) -> tuple[str, str]:
    """Return (query, url) for a safe direct Baidu answer search, if useful.

    Domain-aware: tries weather → stock → recipe → flight inferers in order.
    The first non-empty query wins. Returns ``("", "")`` on:
      * non-answer output_mode
      * neither URL is Baidu
      * no inferer produced a query
      * current Baidu wd already matches the inferred query
    """
    if str(output_mode or "") != "answer":
        return "", ""
    if not (_is_baidu_url(start_url) or _is_baidu_url(current_url)):
        return "", ""
    query = (
        _infer_answer_weather_search_query(goal)
        or _infer_answer_stock_search_query(goal)
        or _infer_answer_recipe_search_query(goal)
        or _infer_answer_flight_search_query(goal)
    )
    if not query:
        return "", ""
    current_query = _baidu_query_from_url(current_url)
    if _normalize_search_query(current_query) == _normalize_search_query(query):
        return "", ""
    return query, "https://www.baidu.com/s?wd=" + quote_plus(query)


async def _maybe_run_answer_search_fast_path(
    browser: BrowserEnv,
    event_stream: EventStream,
    *,
    goal: str,
    start_url: str,
    output_mode: str,
) -> bool:
    current_url = getattr(browser, "current_url", "") or ""
    query, target_url = _build_answer_search_fast_path_url(
        goal,
        start_url=start_url,
        current_url=current_url,
        output_mode=output_mode,
    )
    if not target_url:
        return False
    try:
        page = await browser._ensure_active_page(reason="answer search fast path")
        if page is None or page.is_closed():
            return False
        logger.info(
            "[ANSWER SEARCH FAST PATH] %s -> %s",
            (current_url or start_url)[:140],
            target_url,
        )
        event_stream.guard(
            step=0,
            name="ANSWER_SEARCH_FAST_PATH",
            message=f"Direct search for answer-mode weather query: {query}",
            metadata={
                "query": query,
                "from_url": current_url or start_url,
                "target_url": target_url,
            },
        )
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        try:
            await browser._wait_for_page_stable()
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.debug("[ANSWER SEARCH FAST PATH] skipped: %s", exc)
        event_stream.guard(
            step=0,
            name="ANSWER_SEARCH_FAST_PATH_ERROR",
            message=f"{type(exc).__name__}: {exc}",
            metadata={"query": query, "target_url": target_url},
        )
        return False


def _city_regex_for_weather_text(city: str) -> str:
    city = str(city or "").strip()
    if not city:
        return ""
    suffixes = "市县区州盟旗"
    if city[-1:] in suffixes and len(city) > 2:
        base = re.escape(city[:-1])
        return base + f"[{suffixes}]?"
    return re.escape(city) + f"[{suffixes}]?"


def _normalize_weather_temperature(value: str) -> str:
    temp = str(value or "").strip()
    temp = temp.replace("－", "-").replace("—", "-").replace("到", "~").replace("至", "~")
    temp = re.sub(r"\s+", "", temp)
    if temp.endswith("°"):
        temp = temp[:-1] + "℃"
    elif temp and not re.search(r"(?:℃|°C|度|C)$", temp):
        temp += "℃"
    return temp


def _compose_weather_answer(city: str, weather: str, temperature: str, context: str) -> str:
    weather = str(weather or "").strip()
    temperature = _normalize_weather_temperature(temperature)
    context = str(context or "")
    no_rain = bool(re.search(r"无降水|无降雨|无雨|不下雨|不会下雨|没有降水|没有雨", context))
    has_rain = bool(
        not no_rain
        and (
            "雨" in weather
            or re.search(r"有(?:小雨|中雨|大雨|阵雨|雷雨|降水|降雨)", context)
            or re.search(r"降水概率\s*(?:[1-9]\d?|100)\s*%", context)
        )
    )
    rain_text = "会下雨" if has_rain else "不会下雨"
    parts = [f"明天{city}{rain_text}"]
    if weather:
        parts.append(f"天气为{weather}")
    if temperature:
        parts.append(f"气温 {temperature}")
    return "，".join(parts) + "。"


def _compact_weather_answer_from_text(text: str, goal: str) -> str:
    city = _extract_weather_city_from_goal(goal)
    if not city:
        return ""
    raw = str(text or "").strip()
    if not raw:
        return ""
    cleaned = raw.replace("\u200c", "").replace("\u200b", "").replace("*", "")
    compact = re.sub(r"\s+", " ", cleaned)
    city_re = _city_regex_for_weather_text(city)
    if not city_re:
        return ""
    temp_re = r"[-−]?\d{1,2}\s*[~～\-—至到]\s*[-−]?\d{1,2}\s*(?:℃|°C|°|度|C)?"
    detailed_pattern = re.compile(
        rf"(?P<context>{city_re}\s*明天(?:（[^）]{{0,40}}）|\([^)]{{0,40}}\))?"
        rf"\s*为\s*(?P<weather>{_WEATHER_CONDITION_RE})(?:天气)?"
        rf"[^。；;\n]{{0,40}}?(?:温度范围|气温|温度)\s*(?P<temp>{temp_re})"
        rf"[^。；;\n]{{0,80}})",
        re.IGNORECASE,
    )
    for match in detailed_pattern.finditer(compact):
        return _compose_weather_answer(
            city,
            match.group("weather"),
            match.group("temp"),
            match.group("context"),
        )

    card_pattern = re.compile(
        rf"(?P<context>{city_re}[^。；;\n]{{0,120}}?"
        rf"(?P<temp>{temp_re})\s*(?P<weather>{_WEATHER_CONDITION_RE})"
        rf"[^。；;\n]{{0,80}})",
        re.IGNORECASE,
    )
    for match in card_pattern.finditer(compact):
        return _compose_weather_answer(
            city,
            match.group("weather"),
            match.group("temp"),
            match.group("context"),
        )

    reverse_card_pattern = re.compile(
        rf"(?P<context>{city_re}[^。；;\n]{{0,160}}?"
        rf"(?P<weather>{_WEATHER_CONDITION_RE})\s*(?P<temp>{temp_re})"
        rf"[^。；;\n]{{0,80}})",
        re.IGNORECASE,
    )
    for match in reverse_card_pattern.finditer(compact):
        return _compose_weather_answer(
            city,
            match.group("weather"),
            match.group("temp"),
            match.group("context"),
        )
    return ""


# ── Domain detection vocab for answer-mode goals ────────────────────────────
# Weather vocab is already captured by _WEATHER_GOAL_KEYWORDS.
_STOCK_GOAL_KEYWORDS = (
    "股价", "股票", "股市", "收盘", "开盘", "涨跌",
    "市值", "K线", "行情", "股权", "证券",
)

_RECIPE_GOAL_KEYWORDS = (
    "怎么做", "做法", "食谱", "菜谱", "如何做",
    "怎么烧", "怎么炒", "教程",
)

_FLIGHT_GOAL_KEYWORDS = (
    "航班", "航班号", "航空", "起飞", "降落", "到达",
    "登机口", "航站楼", "延误", "准点",
)


def _detect_answer_domain(goal: str) -> str:
    """Classify an answer-mode goal into a known domain.

    Returns one of ``"weather"`` / ``"stock"`` / ``"recipe"`` / ``"generic"``.
    Used by ``_compact_answer_text_for_goal`` to route to the right
    domain-specific compactor. ``"generic"`` falls through to raw text.
    """
    text = re.sub(r"\s+", "", str(goal or ""))
    if not text:
        return "generic"
    if any(kw in text for kw in _WEATHER_GOAL_KEYWORDS):
        return "weather"
    if any(kw in text for kw in _STOCK_GOAL_KEYWORDS):
        return "stock"
    if any(kw in text for kw in _RECIPE_GOAL_KEYWORDS):
        return "recipe"
    if any(kw in text for kw in _FLIGHT_GOAL_KEYWORDS) or _extract_flight_number_from_goal(text):
        return "flight"
    return "generic"


def _extract_stock_subject_from_goal(goal: str) -> str:
    """Pull the company / ticker name from a stock-flavoured goal."""
    text = re.sub(r"\s+", "", str(goal or ""))
    if not text:
        return ""
    text = re.sub(r"^(?:帮我|请|麻烦|帮忙|能不能|可以|给我|想知道)+", "", text)
    text = re.sub(
        r"(?:查一下|查询|查查|查|看一下|看看|看|告诉我|了解一下|一下|现在|当前|今日|今天|目前)",
        "",
        text,
    )
    m = re.search(
        # Non-greedy so the captured name does not eat the trailing "的";
        # explicit "的?" between name and the topic word handles "贵州茅台的股价".
        r"([\u4e00-\u9fffA-Za-z]{2,12}?)的?(?:股价|股票|股市|收盘价|开盘价|行情|市值)",
        text,
    )
    if m:
        return m.group(1)
    return ""


def _compact_stock_answer_for_goal(text: str, goal: str) -> str:
    """Compress a stock-quote page into ``{name}（{code}）现价 ¥{price}（涨跌幅 {pct}）。``

    Returns ``""`` when the goal is not stock-flavoured or no price line
    can be confidently located — the caller falls back to raw text.
    """
    name = _extract_stock_subject_from_goal(goal)
    if not name:
        return ""
    raw = str(text or "")
    if not raw.strip():
        return ""
    compact = re.sub(r"\s+", " ", raw)
    price_m = re.search(
        r"(?:现价|最新价|当前价|当前)\s*[:：]?\s*(?P<price>\d{1,6}(?:\.\d{1,4})?)\s*元?",
        compact,
    )
    if not price_m:
        # Fallback: name proximity to a price-like number
        anchor = rf"{re.escape(name)}[^\d\n]{{0,40}}?(?P<price>\d{{1,6}}(?:\.\d{{1,4}})?)\s*元"
        price_m = re.search(anchor, compact)
    if not price_m:
        return ""
    price = price_m.group("price")
    window = compact[max(0, price_m.start() - 20):price_m.end() + 80]
    pct_m = re.search(r"(?P<pct>[+\-−]?\d+(?:\.\d+)?)\s*%", window)
    pct = (pct_m.group("pct") + "%") if pct_m else ""
    code_m = re.search(
        rf"{re.escape(name)}[^\d]{{0,12}}(?P<code>\d{{6}})",
        compact,
    )
    code = code_m.group("code") if code_m else ""
    parts = [name]
    if code:
        parts.append(f"（{code}）")
    parts.append(f"现价 ¥{price}")
    if pct:
        parts.append(f"（涨跌幅 {pct}）")
    return "".join(parts) + "。"


def _extract_recipe_name_from_goal(goal: str) -> str:
    """Pull the dish name from a recipe-flavoured goal."""
    text = re.sub(r"\s+", "", str(goal or ""))
    if not text:
        return ""
    text = re.sub(r"^(?:帮我|请|麻烦|帮忙|能不能|可以|给我|想知道)+", "", text)
    m = re.search(
        # Non-greedy + explicit "的?" so "红烧肉的做法" extracts "红烧肉".
        r"([\u4e00-\u9fffA-Za-z]{2,16}?)的?(?:怎么做|做法|食谱|菜谱|如何做|怎么烧|怎么炒|教程)",
        text,
    )
    if m:
        return m.group(1)
    return ""


def _compact_recipe_answer_for_goal(text: str, goal: str) -> str:
    """Summarise a recipe page into ``{name}；用料：…；步骤：A → B → C。``

    Returns ``""`` when the goal is not recipe-flavoured or the dish name
    cannot be located in the body text — caller falls back to raw text.
    """
    name = _extract_recipe_name_from_goal(goal)
    if not name:
        return ""
    raw = str(text or "")
    if not raw.strip():
        return ""
    name_pos = raw.find(name)
    if name_pos < 0:
        return ""
    section = raw[name_pos:name_pos + 800]
    ingredient_lines: list[str] = []
    step_lines: list[str] = []
    for line in section.split("\n"):
        line = line.strip()
        if not line:
            continue
        ing_m = re.match(r"^(?:主料|辅料|配料|材料|用料)\s*[:：]\s*(.+)$", line)
        if ing_m:
            ingredient_lines.append(ing_m.group(1).strip())
            continue
        if len(step_lines) < 3:
            step_m = re.match(r"^(?:步骤\s*)?\d+[\.、：:]?\s*(.+)$", line)
            if step_m and len(step_m.group(1).strip()) >= 2:
                step_lines.append(step_m.group(1).strip())
    parts = [name]
    if ingredient_lines:
        parts.append("用料：" + "；".join(ingredient_lines))
    if step_lines:
        parts.append("步骤：" + " → ".join(step_lines))
    if len(parts) == 1:
        # Only the dish name — not enough signal to compact
        return ""
    return "；".join(parts) + "。"


def _extract_flight_number_from_goal(goal: str) -> str:
    """Extract an IATA-style flight number (e.g. ``CA1234``) from a goal."""
    text = re.sub(r"\s+", "", str(goal or "")).upper()
    if not text:
        return ""
    m = re.search(r"(?<![A-Z0-9])([A-Z]{2}\d{3,4}[A-Z]?)(?![A-Z0-9])", text)
    if m:
        return m.group(1)
    return ""


_FLIGHT_STATUS_KEYWORDS = (
    "已起飞", "已到达", "已落地", "已取消", "取消",
    "延误", "准点", "登机中", "候机中", "计划",
    "备降", "返航", "在飞",
)


def _compact_flight_answer_for_goal(text: str, goal: str) -> str:
    """Compress a flight-info page into ``{flight} {status} {dep} → {arr}。``

    Returns ``""`` if the flight number is missing from goal or absent in
    body text, or no status / time signals are present near it — caller
    falls back to raw text in that case.
    """
    flight = _extract_flight_number_from_goal(goal)
    if not flight:
        return ""
    raw = str(text or "")
    if not raw.strip() or flight not in raw.upper():
        return ""
    compact = re.sub(r"\s+", " ", raw)
    idx = compact.upper().find(flight)
    if idx < 0:
        return ""
    window = compact[idx:idx + 240]
    status = next((kw for kw in _FLIGHT_STATUS_KEYWORDS if kw in window), "")
    times = re.findall(r"\d{1,2}[:：]\d{2}", window)
    parts: list[str] = [flight]
    if status:
        parts.append(status)
    if len(times) >= 2:
        parts.append(f"{times[0]} → {times[1]}")
    elif times:
        parts.append(times[0])
    if len(parts) == 1:
        return ""
    return " ".join(parts) + "。"


def _compact_answer_text_for_goal(text: str, goal: str) -> str:
    """Domain-router for answer-mode page-text compaction.

    Dispatches the raw page text to the matching domain compactor based
    on goal vocabulary; if the domain compactor cannot extract a clean
    answer (returns ``""``), falls back to the raw input. This lets the
    Final Answer panel show the best-available text without ever swallowing
    the original content for non-routable goals.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    domain = _detect_answer_domain(goal)
    if domain == "weather":
        compacted = _compact_weather_answer_from_text(raw, goal)
        if compacted:
            return compacted
    elif domain == "stock":
        compacted = _compact_stock_answer_for_goal(raw, goal)
        if compacted:
            return compacted
    elif domain == "recipe":
        compacted = _compact_recipe_answer_for_goal(raw, goal)
        if compacted:
            return compacted
    elif domain == "flight":
        compacted = _compact_flight_answer_for_goal(raw, goal)
        if compacted:
            return compacted
    return raw



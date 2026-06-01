"""Natural-language parsing for deterministic form filling.

The parser intentionally stays conservative. It extracts explicit
label -> value assignments and a small set of common form idioms such as
"input X and select Y" for autocomplete fields.
"""

from __future__ import annotations

import calendar
import re
from datetime import date


_QUOTE = r"[\"“”'‘’]"


def _quoted_tokens(text: str) -> list[str]:
    return [
        token.strip()
        for token in re.findall(r"[\"“”'‘’]([^\"“”'‘’]{1,80})[\"“”'‘’]", str(text or ""))
        if token.strip()
    ]


def _split_parallel_terms(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    cleaned = re.sub(
        r"^(?:找到|find|locate|在页面中找到|在当前页面找到|字段|field)\s*",
        "",
        cleaned,
        flags=re.I,
    ).strip(" ：:,，。;；")
    parts = re.split(r"\s*(?:和|与|及|、|,|，|/|\band\b)\s*", cleaned, flags=re.I)
    return [
        _clean_label(part).strip("\"'“”‘’ ")
        for part in parts
        if _clean_label(part).strip("\"'“”‘’ ")
    ]


def _parse_parallel_input_assignments(text: str) -> dict[str, str]:
    """Parse goals like "find A and B inputs, enter X and Y".

    This covers ordinary HTML/demo forms where the user describes fields in one
    sentence instead of writing explicit ``label=value`` pairs.
    """
    assignments: dict[str, str] = {}
    pattern = re.compile(
        r"(?P<labels>.{1,220}?)"
        r"(?:输入框|文本框|输入栏|input\s*(?:box(?:es)?|fields?)?|textbox(?:es)?|fields?)"
        r".{0,50}?"
        r"(?:输入|填入|填写|键入|type|enter)\s*"
        r"(?P<values>.{1,220}?)(?:然后|接着|并|提交|点击|断言|最后|$|[。.;；])",
        re.I | re.S,
    )
    for match in pattern.finditer(str(text or "")):
        label_text = match.group("labels")
        value_text = match.group("values")
        labels = _quoted_tokens(label_text) or _split_parallel_terms(label_text)
        values = _quoted_tokens(value_text) or _split_parallel_terms(value_text)
        if len(labels) < 2 or len(values) < 2 or len(labels) != len(values):
            continue
        for label, value in zip(labels, values):
            clean_label = _clean_label(label)
            clean_value = value.strip()
            if clean_label and clean_value:
                assignments[clean_label] = clean_value
    return assignments


def _clean_label(raw_label: str) -> str:
    label = re.sub(
        r"(输入框|下拉框|区域|开关|复选框|单选框|文本域|textarea|input|select|checkbox|radio|switch).*",
        "",
        str(raw_label or ""),
        flags=re.I,
    ).strip()
    label = re.split(r"[：:]", label)[-1].strip()
    label = re.sub(
        r"^(?:在)?(?:表单|页面)?(?:中)?(?:填入|输入|填写)?(?:以下)?(?:数据|字段|信息)?\s*",
        "",
        label,
        flags=re.I,
    ).strip()
    return label


def parse_form_assignments(goal: str) -> dict[str, str]:
    """Best-effort extraction of label -> desired value from CN/EN form goals."""
    text = str(goal or "")
    assignments: dict[str, str] = _parse_parallel_input_assignments(text)
    chunks = re.split(r"[\r\n]+|(?=\s*\d+\s*[.、)]\s*)", text)

    for raw_line in chunks:
        line = raw_line.strip()
        if not line:
            continue
        clean = re.sub(r"^\s*\d+\s*[.、)]\s*", "", line)
        inline_pairs = re.findall(
            rf"(?:^|[：:，,；;])\s*([^：:，,；;\n\"“”'‘’]{{1,80}}?)\s*[=:：]\s*{_QUOTE}([^\"“”'‘’]+){_QUOTE}",
            clean,
        )
        if len(inline_pairs) >= 2:
            for raw_label, raw_value in inline_pairs:
                label = _clean_label(raw_label)
                value = raw_value.strip()
                if not label or not value:
                    continue
                if re.search(r"^(确认|点击)", label, re.I):
                    continue
                assignments[label] = value

            autocomplete_pairs = re.findall(
                rf"(?:^|[，,；;])\s*([^，,；;\n\"“”'‘’]{{1,80}}?)\s*(?:输入|type)\s*{_QUOTE}([^\"“”'‘’]+){_QUOTE}\s*(?:并|and)?\s*(?:选中|选择|select|pick)[^\"“”'‘’]{{0,40}}{_QUOTE}([^\"“”'‘’]+){_QUOTE}",
                clean,
                flags=re.I,
            )
            for raw_label, _typed_value, raw_selected_value in autocomplete_pairs:
                label = _clean_label(raw_label)
                if label and raw_selected_value.strip():
                    assignments[label] = raw_selected_value.strip()
            continue

        if re.search(r"(找到网页|完整表单区域|完成以下|以下填报|业务指令|任务要求)", clean) and not re.match(r"^[A-Za-z]", clean):
            continue

        label_match = re.match(
            r"([^：:，,]+?)\s*(?:输入框|下拉框|区域|开关|复选框|单选框|文本域|textarea|input|select|checkbox|radio|switch)?\s*[：:]",
            clean,
            re.I,
        )
        label = label_match.group(1).strip() if label_match else ""
        if not label:
            label = clean.split("：", 1)[0].split(":", 1)[0].strip()
            label = re.sub(r"(输入框|下拉框|区域|开关|复选框|单选框|文本域).*", "", label).strip()
        ascii_label = re.match(
            r"^([A-Za-z][A-Za-z0-9_/-]*(?:\s+[A-Za-z][A-Za-z0-9_/-]*){0,3})\b",
            clean,
        )
        if ascii_label and (
            not label
            or len(label) > 60
            or re.search(r"[\"“”]", label)
            or label.lower().startswith(ascii_label.group(1).lower())
        ):
            label = ascii_label.group(1).strip()
        if re.search(r"^(确认|点击)", label, re.I) or (
            re.search(r"(按钮|button|submit|create)", clean, re.I)
            and re.search(r"(确认|点击|最下方|提交|保存)", clean, re.I)
        ):
            continue
        if len(label) > 80:
            continue

        value = ""
        m = re.search(rf"(?:填入|输入|填写|选择|选定|勾选|选中|设为|设置为)\s*{_QUOTE}([^\"“”'‘’]+){_QUOTE}", clean)
        if m:
            value = m.group(1).strip()
        if not value:
            m = re.search(
                rf"(?:输入|type)\s*{_QUOTE}([^\"“”'‘’]+){_QUOTE}\s*(?:并|and)?\s*(?:选中|选择|select|pick)[^\"“”'‘’]{{0,40}}{_QUOTE}([^\"“”'‘’]+){_QUOTE}",
                clean,
                flags=re.I,
            )
            if m:
                value = m.group(2).strip()
        if not value:
            m = re.search(rf"{_QUOTE}([^\"“”'‘’]+){_QUOTE}", clean)
            if m:
                value = m.group(1).strip()
        if not value and re.search(r"开启|打开|切换为开启", clean):
            value = "开启"
        if not value and re.search(r"(delivery|switch|toggle|开关)", label, re.I):
            value = "开启"
        if value and value.strip().lower() in {"create", "submit", "save", "保存", "提交"} and not re.match(r"^[A-Za-z]", label):
            continue
        if value and value.strip().lower() == "basic form" and not re.match(r"^basic form$", label.strip(), re.I):
            continue
        if label and value:
            assignments[label] = value

    return assignments


def _parse_relative_month_day(text: str) -> tuple[int, int] | None:
    raw = str(text or "")
    m = re.search(r"(下个月|下月|next\s+month).*?([1-2]?\d|3[01])\s*(?:号|日|day)?", raw, re.I)
    if m:
        return 1, int(m.group(2))
    m = re.search(r"(本月|这个月|this\s+month).*?([1-2]?\d|3[01])\s*(?:号|日|day)?", raw, re.I)
    if m:
        return 0, int(m.group(2))
    return None


def _month_offset_day(offset: int, day: int) -> str:
    today = date.today()
    month_index = today.month + int(offset)
    year = today.year + (month_index - 1) // 12
    month = (month_index - 1) % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    safe_day = max(1, min(int(day), last_day))
    return f"{year:04d}-{month:02d}-{safe_day:02d}"


def prepare_form_batch_fields(goal: str) -> dict[str, str]:
    fields = parse_form_assignments(goal)
    text = str(goal or "")
    if re.search(r"Activity\s*time|活动时间|时间区域", text, re.I):
        relative_month_day = _parse_relative_month_day(text)
        offset, day = relative_month_day if relative_month_day else (1, 0)
        if not day:
            chunks = re.split(r"[\r\n]+|(?=\s*\d+\s*[.、)]\s*)", text)
            time_chunk = next((c for c in chunks if re.search(r"Activity\s*time|活动时间|时间区域", c, re.I)), "")
            nums = re.findall(r"\b([1-2]?\d|3[01])\b", time_chunk)
            day = int(nums[-1]) if nums else 0
        if 1 <= day <= 31:
            fields["Activity time"] = _month_offset_day(offset, day)
    return fields

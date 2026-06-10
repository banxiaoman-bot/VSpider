from __future__ import annotations

import json
import re
import time
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from visual_web_agent.artifact_manager import artifact_url, register_artifact, resolve_artifact_path


_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")  # no "." => blocks ./.. path traversal
_FIELD_RE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]+")
_CARD_HINT_RE = re.compile(r"(?:card|item|product|result|row|entry|article|list)", re.I)
_CSS_ATTR_PSEUDO_RE = re.compile(r"::attr\(([^)]+)\)\s*$", re.I)
_CSS_CLASS_RE = re.compile(r"\.([0-9A-Za-z_-]+)")
_CSS_ID_RE = re.compile(r"#([0-9A-Za-z_-]+)")
_CSS_ATTR_RE = re.compile(r"\[([0-9A-Za-z_:-]+)(?:\s*([*^$~|]?=)\s*['\"]?([^'\"\]]*)['\"]?)?\]")
_XPATH_RE = re.compile(r"^(?:\.?//)(\*|[0-9A-Za-z_:-]+)(?:\[(.+)\])?$")
_XPATH_EXACT_RE = re.compile(r"^@([0-9A-Za-z_:-]+)\s*=\s*['\"]([^'\"]+)['\"]$")
_XPATH_CONTAINS_RE = re.compile(r"^contains\(@([0-9A-Za-z_:-]+)\s*,\s*['\"]([^'\"]+)['\"]\)$")
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


def _safe_run_id(run_id: str) -> str:
    rid = str(run_id or "manual").strip() or "manual"
    if not _RUN_ID_RE.fullmatch(rid):
        raise ValueError("invalid run_id")
    return rid


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _field_name(value: Any) -> str:
    text = _clean_text(value).lower()
    if not text:
        return ""
    text = text.replace("-", "_")
    text = _FIELD_RE.sub("_", text).strip("_")
    parts = [p for p in text.split("_") if p]
    return parts[-1] if parts else ""


def _coerce_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for item in rows:
        if isinstance(item, dict):
            out.append(dict(item))
        elif item not in (None, ""):
            out.append({"value": item})
    return out


class _HtmlNode:
    def __init__(self, tag: str, attrs: dict[str, str] | None = None, parent: "_HtmlNode | None" = None) -> None:
        self.tag = str(tag or "").lower()
        self.attrs = {str(k).lower(): str(v or "") for k, v in (attrs or {}).items()}
        self.parent = parent
        self.children: list[_HtmlNode] = []
        self.parts: list[str | _HtmlNode] = []

    def append(self, node: "_HtmlNode") -> None:
        self.children.append(node)
        self.parts.append(node)

    def append_text(self, text: str) -> None:
        if text:
            self.parts.append(text)

    def descendants(self) -> list["_HtmlNode"]:
        out: list[_HtmlNode] = []
        for child in self.children:
            out.append(child)
            out.extend(child.descendants())
        return out

    def text(self) -> str:
        parts: list[str] = []
        for item in self.parts:
            value = item.text() if isinstance(item, _HtmlNode) else item
            if value:
                parts.append(str(value))
        return _clean_text(" ".join(parts))

    def inner_html(self) -> str:
        parts: list[str] = []
        for item in self.parts:
            if isinstance(item, _HtmlNode):
                parts.append(item.outer_html())
            else:
                parts.append(escape(str(item), quote=False))
        return "".join(parts)

    def outer_html(self) -> str:
        if self.tag == "document":
            return self.inner_html()
        attrs = "".join(
            f' {escape(k, quote=True)}="{escape(v, quote=True)}"'
            for k, v in self.attrs.items()
            if v != ""
        )
        return f"<{self.tag}{attrs}>{self.inner_html()}</{self.tag}>"


class _SelectorTreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("document")
        self.stack: list[_HtmlNode] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _HtmlNode(tag, {k: v or "" for k, v in attrs}, self.stack[-1])
        self.stack[-1].append(node)
        if node.tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _HtmlNode(tag, {k: v or "" for k, v in attrs}, self.stack[-1])
        self.stack[-1].append(node)

    def handle_data(self, data: str) -> None:
        self.stack[-1].append_text(data)

    def handle_endtag(self, tag: str) -> None:
        tag_l = str(tag or "").lower()
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == tag_l:
                del self.stack[idx:]
                break


def _selector_tree(html: str) -> _HtmlNode:
    parser = _SelectorTreeParser()
    parser.feed(str(html or ""))
    return parser.root


def _dedupe_nodes(nodes: list[_HtmlNode]) -> list[_HtmlNode]:
    out: list[_HtmlNode] = []
    seen: set[int] = set()
    for node in nodes:
        key = id(node)
        if key not in seen:
            seen.add(key)
            out.append(node)
    return out


def _css_parts(selector: str) -> list[str]:
    return [part for part in re.split(r"\s+", str(selector or "").strip()) if part]


def _css_pseudo(selector: str, output: str, attr: str) -> tuple[str, str, str]:
    text = str(selector or "").strip()
    match = _CSS_ATTR_PSEUDO_RE.search(text)
    if match:
        return text[: match.start()].strip(), "attr", match.group(1).strip()
    if text.lower().endswith("::text"):
        return text[:-6].strip(), "text", attr
    return text, output, attr


def _css_attr_match(value: str, op: str, expected: str) -> bool:
    if not op:
        return value != ""
    if op == "=":
        return value == expected
    if op == "*=":
        return expected in value
    if op == "^=":
        return value.startswith(expected)
    if op == "$=":
        return value.endswith(expected)
    if op == "~=":
        return expected in value.split()
    if op == "|=":
        return value == expected or value.startswith(expected + "-")
    return False


def _matches_css(node: _HtmlNode, token: str) -> bool:
    if node.tag == "document":
        return False
    raw = str(token or "").strip()
    if not raw or raw in {">", "+", "~"}:
        return False
    tag_match = re.match(r"^(\*|[A-Za-z][0-9A-Za-z_:-]*)", raw)
    if tag_match:
        tag = tag_match.group(1).lower()
        if tag != "*" and node.tag != tag:
            return False
    for ident in _CSS_ID_RE.findall(raw):
        if node.attrs.get("id", "") != ident:
            return False
    classes = set(node.attrs.get("class", "").split())
    for cls in _CSS_CLASS_RE.findall(raw):
        if cls not in classes:
            return False
    for attr, op, expected in _CSS_ATTR_RE.findall(raw):
        if not _css_attr_match(node.attrs.get(attr.lower(), ""), op, expected):
            return False
    return True


def _select_css_nodes(root: _HtmlNode, selector: str) -> list[_HtmlNode]:
    groups = [group.strip() for group in str(selector or "").split(",") if group.strip()]
    selected: list[_HtmlNode] = []
    for group in groups or ["*"]:
        current = [root]
        combinator = "descendant"
        for token in _css_parts(group.replace(">", " > ")):
            if token == ">":
                combinator = "child"
                continue
            next_nodes: list[_HtmlNode] = []
            for base in current:
                pool = base.children if combinator == "child" else base.descendants()
                next_nodes.extend(node for node in pool if _matches_css(node, token))
            current = _dedupe_nodes(next_nodes)
            combinator = "descendant"
        selected.extend(current)
    return _dedupe_nodes(selected)


def _xpath_output(selector: str, output: str, attr: str) -> tuple[str, str, str]:
    text = str(selector or "").strip()
    attr_match = re.search(r"/@([0-9A-Za-z_:-]+)\s*$", text)
    if attr_match:
        return text[: attr_match.start()].strip(), "attr", attr_match.group(1)
    if text.endswith("/text()"):
        return text[: -7].strip(), "text", attr
    return text, output, attr


def _xpath_predicate(node: _HtmlNode, predicate: str) -> bool:
    pred = str(predicate or "").strip()
    if not pred:
        return True
    exact = _XPATH_EXACT_RE.match(pred)
    if exact:
        return node.attrs.get(exact.group(1).lower(), "") == exact.group(2)
    contains = _XPATH_CONTAINS_RE.match(pred)
    if contains:
        return contains.group(2) in node.attrs.get(contains.group(1).lower(), "")
    return False


def _select_xpath_nodes(root: _HtmlNode, selector: str) -> list[_HtmlNode]:
    match = _XPATH_RE.match(str(selector or "").strip())
    if not match:
        return []
    tag = match.group(1).lower()
    predicate = match.group(2) or ""
    return [
        node for node in root.descendants()
        if (tag == "*" or node.tag == tag) and _xpath_predicate(node, predicate)
    ]


def _select_text_nodes(root: _HtmlNode, query: str, *, tag: str = "", regex: bool = False, case_sensitive: bool = False) -> list[_HtmlNode]:
    q = str(query or "")
    if not q:
        return []
    pattern = re.compile(q, 0 if case_sensitive else re.I) if regex else None
    tag_l = str(tag or "").lower()
    out: list[_HtmlNode] = []
    for node in root.descendants():
        if tag_l and node.tag != tag_l:
            continue
        text = node.text()
        if regex:
            if pattern and pattern.search(text):
                out.append(node)
        elif (q if case_sensitive else q.lower()) in (text if case_sensitive else text.lower()):
            out.append(node)
    return out


def _node_payload(node: _HtmlNode, output: str, attr: str) -> Any:
    mode = str(output or "text").lower()
    if mode == "text":
        return node.text()
    if mode == "html":
        return node.inner_html()
    if mode in {"outer_html", "outerhtml"}:
        return node.outer_html()
    if mode == "attr":
        return node.attrs.get(str(attr or "").lower(), "")
    if mode == "node":
        return {
            "tag": node.tag,
            "attrs": dict(node.attrs),
            "text": node.text(),
            "html": node.inner_html(),
        }
    raise ValueError("unsupported selector output")


def select(
    source: str,
    *,
    selector: str = "",
    selector_type: str = "css",
    mode: str = "all",
    output: str = "text",
    attr: str = "",
    text: str = "",
    regex: str = "",
    tag: str = "",
    max_results: int = 100,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    root = _selector_tree(str(source or ""))
    st = str(selector_type or "css").lower()
    out_mode = str(output or "text").lower()
    attr_name = str(attr or "")
    query = str(selector or "")
    if st == "css":
        query, out_mode, attr_name = _css_pseudo(query, out_mode, attr_name)
        nodes = _select_css_nodes(root, query)
    elif st == "xpath":
        query, out_mode, attr_name = _xpath_output(query, out_mode, attr_name)
        nodes = _select_xpath_nodes(root, query)
    elif st == "text":
        nodes = _select_text_nodes(root, text or query, tag=tag, case_sensitive=case_sensitive)
    elif st == "regex":
        nodes = _select_text_nodes(root, regex or query, tag=tag, regex=True, case_sensitive=case_sensitive)
    else:
        raise ValueError("unsupported selector_type")
    try:
        limit = max(1, min(int(max_results), 1000))
    except Exception:
        limit = 100
    mode_l = str(mode or "all").lower()
    count = len(nodes)
    if mode_l == "count":
        values: list[Any] = []
    elif mode_l == "first":
        values = [_node_payload(node, out_mode, attr_name) for node in nodes[:1]]
    elif mode_l == "all":
        values = [_node_payload(node, out_mode, attr_name) for node in nodes[:limit]]
    else:
        raise ValueError("unsupported selector mode")
    return {
        "selector_type": st,
        "selector": query,
        "mode": mode_l,
        "output": out_mode,
        "attr": attr_name,
        "count": count,
        "results": values,
        "first": values[0] if values else None,
    }


def _extract_json_rows(value: Any) -> list[dict[str, Any]]:
    """Pick the best row-list in a nested JSON payload.

    Quality-weighted: a list of dicts (real records) outscores a longer list
    of bare scalars (trace/tag noise), so ``{"meta": {"trace": [6 strings]},
    "data": {"records": [3 dicts]}}`` resolves to the records.
    """
    best: list[dict[str, Any]] = []
    best_score = 0.0

    def visit(node: Any) -> None:
        nonlocal best, best_score
        if isinstance(node, list):
            rows = _coerce_rows(node)
            if rows:
                dict_count = sum(1 for item in node if isinstance(item, dict))
                quality = 3.0 if dict_count * 2 >= len(rows) else 1.0
                score = len(rows) * quality
                if score > best_score:
                    best = rows
                    best_score = score
            for item in node[:30]:
                if isinstance(item, (dict, list)):
                    visit(item)
            return
        if isinstance(node, dict):
            for key in ("data", "items", "list", "rows", "records", "results", "result", "content"):
                if key in node:
                    visit(node[key])
            for val in node.values():
                if isinstance(val, (dict, list)):
                    visit(val)

    visit(value)
    return best


class _TableGrid:
    """Occupancy-aware grid for one ``<table>``: expands rowspan/colspan."""

    def __init__(self) -> None:
        self.rows: list[list[str]] = []
        self.header_flags: list[bool] = []
        self._pending: dict[int, dict[int, str]] = {}
        self._row_idx = 0

    def add_row(self, cells: list[tuple[str, int, int]], *, is_header: bool) -> None:
        placed: dict[int, str] = dict(self._pending.pop(self._row_idx, {}))
        col = 0
        for text, colspan, rowspan in cells:
            while col in placed:
                col += 1
            for k in range(colspan):
                placed[col + k] = text
                for dr in range(1, rowspan):
                    self._pending.setdefault(self._row_idx + dr, {})[col + k] = text
            col += colspan
        self._row_idx += 1
        if placed and any(_clean_text(v) for v in placed.values()):
            width = max(placed) + 1
            self.rows.append([placed.get(i, "") for i in range(width)])
            self.header_flags.append(is_header)


class _TableParser(HTMLParser):
    """Nested-table-safe parser: each ``<table>`` builds its own grid on a stack,
    so inner tables no longer shred the outer table's rows."""

    _MAX_SPAN = 100

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_TableGrid] = []
        self._stack: list[dict[str, Any]] = []

    @classmethod
    def _span(cls, attrs: dict[str, str], key: str) -> int:
        try:
            return max(1, min(int(attrs.get(key) or "1"), cls._MAX_SPAN))
        except Exception:
            return 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        if tag_l == "table":
            self._stack.append(
                {
                    "grid": _TableGrid(),
                    "cells": [],
                    "in_row": False,
                    "in_cell": False,
                    "cell_parts": [],
                    "cell_span": (1, 1),
                    "in_thead": False,
                    "row_all_th": True,
                }
            )
            return
        state = self._stack[-1] if self._stack else None
        if state is None:
            return
        if tag_l == "thead":
            state["in_thead"] = True
        elif tag_l == "tr":
            state["in_row"] = True
            state["in_cell"] = False
            state["cells"] = []
            state["row_all_th"] = True
        elif state["in_row"] and tag_l in {"td", "th"}:
            attr = {str(k).lower(): str(v or "") for k, v in attrs}
            state["in_cell"] = True
            state["cell_parts"] = []
            state["cell_span"] = (self._span(attr, "colspan"), self._span(attr, "rowspan"))
            if tag_l == "td":
                state["row_all_th"] = False

    def handle_data(self, data: str) -> None:
        state = self._stack[-1] if self._stack else None
        if state is not None and state["in_cell"]:
            text = _clean_text(data)
            if text:
                state["cell_parts"].append(text)

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        state = self._stack[-1] if self._stack else None
        if state is None:
            return
        if tag_l in {"td", "th"} and state["in_cell"]:
            colspan, rowspan = state["cell_span"]
            state["cells"].append((_clean_text(" ".join(state["cell_parts"])), colspan, rowspan))
            state["cell_parts"] = []
            state["in_cell"] = False
        elif tag_l == "tr" and state["in_row"]:
            is_header = bool(state["in_thead"] or (state["row_all_th"] and state["cells"]))
            state["grid"].add_row(state["cells"], is_header=is_header)
            state["cells"] = []
            state["in_row"] = False
        elif tag_l == "thead":
            state["in_thead"] = False
        elif tag_l == "table":
            done = self._stack.pop()
            if done["grid"].rows:
                self.tables.append(done["grid"])


_NUMERIC_CELL_RE = re.compile(r"[-+]?\d[\d,.]*%?")


def _looks_numeric(cell: str) -> bool:
    return bool(_NUMERIC_CELL_RE.fullmatch(_clean_text(cell)))


def _headerless_first_row_is_header(rows: list[list[str]]) -> bool:
    """Without any <th>/<thead> signal, only promote row0 to header when it is
    unique, fully non-numeric AND at least one column flips text->numeric in
    the body. Plain data tables (e.g. product/price rows) keep all rows."""
    first = rows[0]
    if not first or len(set(first)) != len(first):
        return False
    if any(_looks_numeric(cell) for cell in first if _clean_text(cell)):
        return False
    if len(rows) == 1:
        return False
    width = max(len(row) for row in rows)
    for col in range(width):
        head = first[col] if col < len(first) else ""
        body = [_clean_text(row[col]) for row in rows[1:] if col < len(row)]
        body = [cell for cell in body if cell]
        if not body or _looks_numeric(head):
            continue
        numeric = sum(1 for cell in body if _looks_numeric(cell))
        if numeric * 2 >= len(body):
            return True
    return False


def _merged_header_names(header_rows: list[list[str]], width: int) -> list[str]:
    """Collapse stacked header rows: each column takes the deepest non-empty
    label (leaf), falling back upward; duplicates get _2/_3 suffixes."""
    names: list[str] = []
    for col in range(width):
        label = ""
        for row in reversed(header_rows):
            cell = _clean_text(row[col]) if col < len(row) else ""
            if cell:
                label = cell
                break
        names.append(_field_name(label) or f"col_{col + 1}")
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        bump = seen.get(name, 0) + 1
        seen[name] = bump
        out.append(name if bump == 1 else f"{name}_{bump}")
    return out


def _rows_from_table(grid: _TableGrid) -> list[dict[str, Any]]:
    rows = grid.rows
    if not rows:
        return []
    width = max(len(row) for row in rows)
    header_count = 0
    for flag in grid.header_flags:
        if flag:
            header_count += 1
        else:
            break
    if header_count == 0 and _headerless_first_row_is_header(rows):
        header_count = 1
    if header_count:
        headers = _merged_header_names(rows[:header_count], width)
        data_rows = rows[header_count:]
    else:
        headers = [f"col_{idx + 1}" for idx in range(width)]
        data_rows = rows
    out: list[dict[str, Any]] = []
    for row in data_rows:
        item: dict[str, Any] = {}
        for idx, header in enumerate(headers):
            item[header] = row[idx] if idx < len(row) else ""
        if any(_clean_text(v) for v in item.values()):
            out.append(item)
    return out


def extract_html_tables_all(html: str) -> list[list[dict[str, Any]]]:
    """Every table on the page as its own row-set, largest first."""
    parser = _TableParser()
    parser.feed(str(html or ""))
    candidates = [_rows_from_table(grid) for grid in parser.tables]
    candidates = [rows for rows in candidates if rows]
    candidates.sort(key=lambda rows: (len(rows), len(rows[0]) if rows else 0), reverse=True)
    return candidates


def extract_html_tables(html: str) -> list[dict[str, Any]]:
    tables = extract_html_tables_all(html)
    return tables[0] if tables else []


class _CardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self._stack: list[dict[str, Any]] = []
        self._active: dict[str, Any] | None = None
        self._field_stack: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): v or "" for k, v in attrs}
        token = " ".join(attr.get(k, "") for k in ("class", "id", "role", "data-testid"))
        is_block = tag.lower() in {"article", "li"} or _CARD_HINT_RE.search(token or "") is not None
        if is_block and self._active is None:
            self._active = {"fields": {}, "text": [], "links": []}
            self._stack.append(self._active)
            self._depth = 0
        if self._active is not None:
            self._depth += 1
            field = _field_name(attr.get("data-field") or attr.get("aria-label") or attr.get("class") or attr.get("id") or tag)
            self._field_stack.append(field)
            href = attr.get("href") or attr.get("src")
            if href:
                self._active["links"].append(href)
        else:
            self._field_stack.append("")

    def handle_data(self, data: str) -> None:
        text = _clean_text(data)
        if not text or self._active is None:
            return
        self._active["text"].append(text)
        field = self._field_stack[-1] if self._field_stack else ""
        if field and field not in {"div", "span", "p", "a", "li", "article", "section"}:
            current = self._active["fields"].get(field, "")
            self._active["fields"][field] = _clean_text(f"{current} {text}")

    def handle_endtag(self, tag: str) -> None:
        if self._field_stack:
            self._field_stack.pop()
        if self._active is not None:
            self._depth -= 1
        if self._active is not None and self._depth <= 0:
            if self._stack and self._stack[-1] is self._active:
                row = dict(self._active["fields"])
                text = _clean_text(" ".join(self._active["text"]))
                if text and "text" not in row:
                    row["text"] = text
                if self._active["links"] and "url" not in row:
                    row["url"] = self._active["links"][0]
                if len(row) >= 2 or text:
                    self.rows.append(row)
                self._stack.pop()
                self._active = None
                self._depth = 0


def extract_html_cards(html: str) -> list[dict[str, Any]]:
    parser = _CardParser()
    parser.feed(str(html or ""))
    rows = []
    seen: set[str] = set()
    for row in parser.rows:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def _filter_fields(rows: list[dict[str, Any]], fields: list[str] | None) -> list[dict[str, Any]]:
    wanted = [_field_name(f) for f in fields or [] if _field_name(f)]
    if not wanted:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        lower = {_field_name(k): v for k, v in row.items()}
        item = {field: lower.get(field, "") for field in wanted}
        if any(_clean_text(v) for v in item.values()):
            out.append(item)
    return out


def _fields_of(rows: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fields:
                fields.append(str(key))
    return fields


def extract(
    source: Any,
    *,
    source_type: str = "auto",
    requested_fields: list[str] | None = None,
    max_rows: int = 1000,
    all_tables: bool = False,
) -> dict[str, Any]:
    st = str(source_type or "auto").strip().lower()
    rows: list[dict[str, Any]] = []
    family = "UNKNOWN"
    html_tables: list[list[dict[str, Any]]] = []
    if isinstance(source, (dict, list)):
        rows = _extract_json_rows(source)
        family = "API_JSON"
    elif isinstance(source, str):
        text = source.strip()
        if st in {"json", "api_json", "auto"}:
            try:
                rows = _extract_json_rows(json.loads(text))
                family = "API_JSON"
            except Exception:
                rows = []
        if not rows and st in {"html", "dom_table", "auto"}:
            html_tables = extract_html_tables_all(text)
            rows = html_tables[0] if html_tables else []
            family = "DOM_TABLE" if rows else family
        if not rows and st in {"html", "dom_cards", "dom_list", "auto"}:
            rows = extract_html_cards(text)
            family = "DOM_CARDS" if rows else family
    rows = _filter_fields(rows, requested_fields)
    try:
        n = max(1, min(int(max_rows), 10000))
    except Exception:
        n = 1000
    rows = rows[:n]
    result: dict[str, Any] = {
        "source_family": family,
        "row_count": len(rows),
        "fields": _fields_of(rows),
        "rows": rows,
        "sample": rows[:3],
        # additive (output_contract rule: fields are only ever added):
        # how many DOM tables were discovered on the page, so callers can
        # tell when the default largest-table pick is dropping data.
        "table_count": len(html_tables),
    }
    if all_tables and html_tables:
        tables_out: list[dict[str, Any]] = []
        for idx, table_rows in enumerate(html_tables):
            t_rows = _filter_fields(table_rows, requested_fields)[:n]
            if not t_rows:
                continue
            tables_out.append(
                {
                    "index": idx,
                    "row_count": len(t_rows),
                    "fields": _fields_of(t_rows),
                    "rows": t_rows,
                }
            )
        result["tables"] = tables_out
    return result


def export_jsonl(result: dict[str, Any], *, run_id: str = "manual") -> dict[str, str]:
    rid = _safe_run_id(run_id)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = resolve_artifact_path(f"extract_{rid}_{ts}.jsonl", subdir="extractor")
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _coerce_rows(result.get("rows") or [])
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    if rid in {"manual", "route_executor"}:
        register_artifact(path)
    else:
        source_url = str(result.get("source_url") or result.get("url") or "")
        try:
            register_artifact(
                path,
                run_id=rid,
                kind="dataset_records",
                mime="application/x-ndjson",
                source_url=source_url,
                produced_by="generic_extractor",
                step_id="export_jsonl",
                extra={
                    "row_count": len(rows),
                    "fields": list(result.get("fields") or []),
                },
            )
        except TypeError:
            register_artifact(path)
    return {"path": str(path), "url": artifact_url(path)}

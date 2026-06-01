"""Shared extraction and pagination control helpers.

This module is deliberately small and dependency-light. It centralizes the
generic rules that decide whether extracted rows satisfy the requested schema
and whether a page/pager action actually moved to a new data page.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable


DATA_SIGNATURE_JS = r"""() => {
    const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const compact = (value) => clean(value).toLowerCase()
        .replace(/[^a-z0-9\u4e00-\u9fff]+/g, '');
    const isVisible = (el) => {
        if (!el || !(el instanceof Element)) return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0
            && rect.height > 0
            && style.display !== 'none'
            && style.visibility !== 'hidden'
            && Number(style.opacity || 1) > 0.01;
    };
    const firstVisibleTable = Array.from(document.querySelectorAll('table')).find(isVisible);
    const tableRows = [];
    let tableInfo = '';
    let tablePageStart = null;
    let tablePageEnd = null;
    let tablePageTotal = null;

    if (firstVisibleTable) {
        const trs = Array.from(firstVisibleTable.querySelectorAll('tbody tr'))
            .filter(isVisible)
            .slice(0, 12);
        for (const tr of trs) {
            const cells = Array.from(tr.querySelectorAll('th,td,[role="cell"],[role="gridcell"]'))
                .filter(isVisible)
                .map(cell => clean(cell.innerText || cell.textContent))
                .filter(Boolean)
                .slice(0, 8);
            if (cells.length) tableRows.push(cells.join('|'));
        }
        const scope = firstVisibleTable.closest(
            '.dt-container,.dataTables_wrapper,.datatable,.table-responsive,.table-container,main,section,body'
        ) || document.body;
        const infoNodes = Array.from(scope.querySelectorAll(
            '.dt-info,.dataTables_info,[id$="_info"],[class*="info"]'
        )).filter(isVisible);
        tableInfo = clean(infoNodes.map(n => n.innerText || n.textContent).join(' '));
        let m = tableInfo.match(/showing\s+(\d+)\s+to\s+(\d+)\s+of\s+(\d+)/i);
        if (!m) m = tableInfo.match(/(\d+)\s*-\s*(\d+)\s*(?:of|\/)\s*(\d+)/i);
        if (m) {
            tablePageStart = Number(m[1]);
            tablePageEnd = Number(m[2]);
            tablePageTotal = Number(m[3]);
        }
    }

    const listRows = [];
    const listSelectors = [
        'article', '[role="article"]', '[role="listitem"]',
        '.Story', '.story', '.ais-Hits-item', '.hit',
        '.search-result', '.result', '.item', '.card', 'li'
    ];
    for (const el of Array.from(document.querySelectorAll(listSelectors.join(',')))) {
        if (!isVisible(el)) continue;
        const text = clean(el.innerText || el.textContent);
        if (text.length < 12 || text.length > 1800) continue;
        const nested = Array.from(el.querySelectorAll(listSelectors.join(',')))
            .filter(node => node !== el && isVisible(node));
        if (nested.length >= 3 && text.length > 600) continue;
        listRows.push(text.slice(0, 240));
        if (listRows.length >= 20) break;
    }

    const bodyText = clean(document.body ? document.body.innerText || '' : '');
    const payload = {
        url: location.href,
        title: document.title || '',
        readyState: document.readyState,
        bodyTextLength: bodyText.length,
        scrollHeight: document.body ? document.body.scrollHeight : 0,
        tableInfo,
        tablePageStart,
        tablePageEnd,
        tablePageTotal,
        tableRows,
        listRows
    };
    payload.rowSignature = compact([...tableRows, ...listRows].slice(0, 12).join('\n')).slice(0, 500);
    return payload;
}"""


def normalize_field_key(value: object) -> str:
    return re.sub(
        r"[^a-z0-9\u4e00-\u9fff]+",
        "",
        str(value or "").strip().lower(),
    )


def _field_aliases(field: str) -> set[str]:
    norm = normalize_field_key(field)
    aliases = {norm} if norm else set()
    alias_map = {
        "url": {"url", "link", "href", "primaryurl", "sourceurl", "detailurl", "网址", "链接"},
        "link": {"url", "link", "href", "primaryurl", "sourceurl", "detailurl", "网址", "链接"},
        "title": {"title", "name", "heading", "subject", "标题", "名称", "名字"},
        "标题": {"title", "name", "heading", "subject", "标题", "名称", "名字"},
        "name": {"name", "title", "名称", "姓名", "名字"},
        "名称": {"name", "title", "名称", "姓名", "名字"},
        "position": {"position", "职位", "职务", "岗位"},
        "office": {"office", "location", "city", "地区", "地点", "办公室"},
        "time": {"time", "date", "age", "created", "published", "时间", "日期"},
        "author": {"author", "user", "username", "by", "作者", "用户"},
        "rating": {"rating", "score", "评分", "分数", "星级"},
        "score": {"rating", "score", "评分", "分数", "星级"},
        "评分": {"rating", "score", "评分", "分数", "星级"},
        "points": {"points", "score", "votes", "积分", "分数", "点赞"},
        "comments": {"comments", "commentcount", "reviewcount", "评论", "评论数"},
        "reviewcount": {"comments", "commentcount", "reviewcount", "评论", "评论数"},
        "reviews": {"reviews", "reviewcount", "votes", "评价人数", "评价数", "评论数"},
        "评价人数": {"reviews", "reviewcount", "votes", "评价人数", "评价数", "评论数"},
        "summary": {"summary", "description", "intro", "简介", "摘要", "一句话简介"},
        "description": {"summary", "description", "intro", "简介", "摘要", "一句话简介"},
        "intro": {"summary", "description", "intro", "简介", "摘要", "一句话简介"},
        "一句话简介": {"summary", "description", "intro", "简介", "摘要", "一句话简介"},
    }
    for key, values in alias_map.items():
        if norm == key or norm in values:
            aliases.update(values)
    return aliases


def _hash_payload(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


@dataclass
class PageDataController:
    requested_fields: list[str]

    def requested_field_coverage(self, row: dict) -> tuple[int, int]:
        if not self.requested_fields or not isinstance(row, dict):
            return 0, 0
        normalized_keys = {
            key: normalize_field_key(key)
            for key, value in row.items()
            if value is not None and str(value).strip()
        }
        hit = 0
        total = 0
        for field in self.requested_fields:
            aliases = _field_aliases(field)
            if not aliases:
                continue
            total += 1
            for norm_key in normalized_keys.values():
                if (
                    norm_key in aliases
                    or any(alias and alias in norm_key for alias in aliases)
                    or any(alias and norm_key in alias for alias in aliases)
                ):
                    hit += 1
                    break
        return hit, total

    @staticmethod
    def min_requested_field_hits(total: int) -> int:
        if total <= 0:
            return 0
        if total <= 4:
            return total
        return max(2, int(math.ceil(total * 0.75)))

    def filter_undercomplete_rows(
        self,
        rows: list,
        normalize_row: Callable[[dict], dict] | None = None,
    ) -> tuple[list, dict[str, int]]:
        if not self.requested_fields:
            return rows, {"dropped": 0, "required_hits": 0, "total_fields": 0}
        kept: list = []
        dropped = 0
        expected_total = 0
        expected_min = 0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            prepared = normalize_row(row) if normalize_row else row
            hit, total = self.requested_field_coverage(prepared)
            expected_total = max(expected_total, total)
            required = self.min_requested_field_hits(total)
            expected_min = max(expected_min, required)
            if total and hit < required:
                dropped += 1
                continue
            kept.append(prepared)
        return kept, {
            "dropped": dropped,
            "required_hits": expected_min,
            "total_fields": expected_total,
        }

    @staticmethod
    def rows_signature(rows: list[dict] | None, *, limit: int = 12) -> str:
        compact_rows: list[dict] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            clean = {
                str(key): str(value).strip()
                for key, value in row.items()
                if value is not None and str(value).strip()
            }
            if clean:
                compact_rows.append(clean)
            if len(compact_rows) >= limit:
                break
        return _hash_payload(compact_rows) if compact_rows else ""


def page_data_signature(raw: dict | None) -> dict:
    data = raw if isinstance(raw, dict) else {}
    return {
        "url": str(data.get("url") or ""),
        "bodyTextLength": int(data.get("bodyTextLength") or 0),
        "scrollHeight": int(data.get("scrollHeight") or 0),
        "tableInfo": str(data.get("tableInfo") or ""),
        "tablePageStart": data.get("tablePageStart"),
        "tablePageEnd": data.get("tablePageEnd"),
        "tablePageTotal": data.get("tablePageTotal"),
        "rowSignature": str(data.get("rowSignature") or ""),
        "hasTableRows": bool(data.get("tableRows")),
        "hasListRows": bool(data.get("listRows")),
    }


def pagination_moved(before: dict | None, after: dict | None) -> tuple[bool, str]:
    b = page_data_signature(before)
    a = page_data_signature(after)
    if not b or not a:
        return False, "missing data signature"

    if a["url"] and b["url"] and a["url"] != b["url"]:
        return True, "url changed"

    b_start = b.get("tablePageStart")
    b_end = b.get("tablePageEnd")
    a_start = a.get("tablePageStart")
    a_end = a.get("tablePageEnd")
    if b_start is not None and b_end is not None and a_start is not None and a_end is not None:
        if (a_start, a_end) != (b_start, b_end):
            return True, f"table page window changed {b_start}-{b_end} -> {a_start}-{a_end}"
        if a["rowSignature"] != b["rowSignature"]:
            return False, "table rows changed but page window did not; likely sort/filter"
        return False, "table page window unchanged"

    if a["rowSignature"] and b["rowSignature"] and a["rowSignature"] != b["rowSignature"]:
        return True, "row signature changed"

    if a["bodyTextLength"] > b["bodyTextLength"] + 200 and a["scrollHeight"] > b["scrollHeight"]:
        return True, "content grew after paging"

    return False, "data signature unchanged"

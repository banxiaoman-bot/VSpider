"""Parse planner sub-goal exit_criteria into machine-readable checks."""

from __future__ import annotations

import re
from typing import Any


_ROW_COUNT_RE = re.compile(r"(\d+)\s*条")
_URL_HINT_RE = re.compile(
    r"(?:url|网址|链接).{0,20}(?:含|包含|匹配|match|含\s*[`'\"]?)([^`'\"；。\n]+)",
    re.I,
)
_EXTRACT_DONE_RE = re.compile(r"extract|提取|输出.*json|结构化", re.I)
_SUBMIT_DONE_RE = re.compile(r"submit|create|提交|创建|发送", re.I)
_ANSWER_DONE_RE = re.compile(r"回答|答案|reply|response|final answer", re.I)
_VISIBLE_RE = re.compile(r"可见|已加载|loaded|appear|显示", re.I)


def parse_exit_criteria(text: str, *, description: str = "") -> list[dict[str, Any]]:
    blob = "\n".join(part for part in (description, text) if part).strip()
    if not blob:
        return []

    lower = blob.lower()
    criteria: list[dict[str, Any]] = []

    for match in _ROW_COUNT_RE.finditer(blob):
        criteria.append({"type": "row_count", "target": int(match.group(1))})

    url_match = _URL_HINT_RE.search(blob)
    if url_match:
        criteria.append({"type": "url_contains", "value": url_match.group(1).strip()})

    if _EXTRACT_DONE_RE.search(blob):
        criteria.append({"type": "extract_done"})
    if _SUBMIT_DONE_RE.search(blob):
        criteria.append({"type": "submit_done"})
    if _ANSWER_DONE_RE.search(blob):
        criteria.append({"type": "answer_ready"})
    if _VISIBLE_RE.search(blob) and not criteria:
        criteria.append({"type": "page_ready"})

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in criteria:
        key = f"{item.get('type')}:{item.get('target', item.get('value', ''))}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def parse_subgoal_plan(task_plan: Any) -> list[dict[str, Any]]:
    sub_goals = list(getattr(task_plan, "sub_goals", None) or [])
    out: list[dict[str, Any]] = []
    for sg in sub_goals:
        description = str(getattr(sg, "description", "") or "")
        exit_text = str(getattr(sg, "exit_criteria", "") or "")
        out.append({
            "id": getattr(sg, "id", None),
            "description": description,
            "exit_criteria_text": exit_text,
            "criteria": parse_exit_criteria(exit_text, description=description),
            "status": str(getattr(sg, "status", "") or ""),
        })
    return out


def current_subgoal_criteria(task_plan: Any) -> list[dict[str, Any]]:
    if task_plan is None:
        return []
    sub_goals = list(getattr(task_plan, "sub_goals", None) or [])
    if not sub_goals:
        return []
    idx = int(getattr(task_plan, "current_idx", 0) or 0)
    idx = max(0, min(idx, len(sub_goals) - 1))
    sg = sub_goals[idx]
    return parse_exit_criteria(
        str(getattr(sg, "exit_criteria", "") or ""),
        description=str(getattr(sg, "description", "") or ""),
    )


def evaluate_exit_criteria(
    criteria: list[dict[str, Any]] | None,
    *,
    total_extracted_rows: int = 0,
    current_url: str = "",
    workflow_memory: dict[str, Any] | None = None,
    last_action: str = "",
    last_action_success: bool = False,
) -> dict[str, Any]:
    items = [dict(item) for item in (criteria or []) if isinstance(item, dict)]
    if not items:
        return {"passed": False, "checks": [], "matched": []}

    memory = dict(workflow_memory or {})
    checks: list[dict[str, Any]] = []
    matched: list[str] = []

    for item in items:
        kind = str(item.get("type") or "")
        passed = False
        detail = ""
        if kind == "row_count":
            target = int(item.get("target") or 0)
            passed = total_extracted_rows >= target > 0
            detail = f"rows={total_extracted_rows} target={target}"
        elif kind == "url_contains":
            needle = str(item.get("value") or "").strip().lower()
            passed = bool(needle) and needle in str(current_url or "").lower()
            detail = f"url contains {needle!r}"
        elif kind == "extract_done":
            passed = last_action in {"extract", "chat_extract"} and last_action_success
            detail = f"last_action={last_action} success={last_action_success}"
        elif kind == "submit_done":
            passed = last_action in {"click", "chat_submit", "form_set"} and last_action_success
            detail = "submit-like action succeeded"
        elif kind == "answer_ready":
            answer = str(memory.get("final_answer") or memory.get("answer") or "").strip()
            passed = bool(answer)
            detail = "workflow_memory answer present" if passed else "answer missing"
        elif kind == "page_ready":
            passed = bool(str(current_url or "").strip())
            detail = "page url present"
        checks.append({"type": kind, "passed": passed, "detail": detail})
        if passed:
            matched.append(kind)

    required = [str(item.get("type") or "") for item in items if item.get("type")]
    passed_all = bool(required) and all(
        any(ch.get("type") == req and ch.get("passed") for ch in checks)
        for req in required
    )
    return {"passed": passed_all, "checks": checks, "matched": matched}

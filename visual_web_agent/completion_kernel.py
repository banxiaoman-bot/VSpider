"""Unified task completion evaluation — evidence-based stop/continue decisions."""

from __future__ import annotations

import re
from typing import Any

from visual_web_agent.planner_exit_criteria import evaluate_exit_criteria
from visual_web_agent.success_verifier import verify_route_success

_COMPLETION_VERSION = "completion_evaluation.v1"

_TERMINAL_ACTIONS = frozenset({
    "done",
    "ask_human",
    "error",
    "extract",
    "chat_extract",
    "fetch_link_content",
    "fetch_links_batch",
    "download_image",
    "upload",
    "save_to_memory",
})

_LOW_VALUE_WHEN_COMPLETE = frozenset({
    "scroll",
    "next_page",
    "wait",
    "hover",
    "click",
    "type",
    "press_key",
})

_PROGRESS_ACTIONS = frozenset({"scroll", "next_page", "wait", "extract"})


class NoProgressTracker:
    """Track consecutive low-value steps that fail to increase extract evidence."""

    def __init__(self, *, exhaust_threshold: int = 2) -> None:
        self.streak = 0
        self.exhaust_threshold = max(1, int(exhaust_threshold))
        self._last_row_count = 0
        self._last_url_key = ""

    def observe(
        self,
        *,
        action: str,
        row_count: int = 0,
        url: str = "",
    ) -> dict[str, Any]:
        act = str(action or "").strip().lower()
        if act not in _PROGRESS_ACTIONS:
            self.streak = 0
            return {"streak": 0, "pagination_exhausted": False, "progressed": True}

        url_key = str(url or "").strip().lower()
        progressed = row_count > self._last_row_count
        if not progressed and url_key and url_key != self._last_url_key and act in {"next_page", "scroll"}:
            progressed = True

        if progressed:
            self.streak = 0
        else:
            self.streak += 1

        self._last_row_count = max(self._last_row_count, int(row_count or 0))
        if url_key:
            self._last_url_key = url_key

        exhausted = (
            self.streak >= self.exhaust_threshold
            and act in {"scroll", "next_page", "wait"}
        )
        return {
            "streak": self.streak,
            "pagination_exhausted": exhausted,
            "progressed": progressed,
        }

    def reset(self) -> None:
        self.streak = 0
        self._last_row_count = 0
        self._last_url_key = ""


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, dict) else {}


def _check(name: str, passed: bool, detail: str, **extra: Any) -> dict[str, Any]:
    item = {"name": name, "passed": bool(passed), "detail": str(detail or "")}
    item.update(extra)
    return item


def _extract_tolerance(goal_target: int | None) -> int:
    if not goal_target or goal_target <= 0:
        return 0
    return min(5, max(1, int(goal_target * 0.05)))


def _answer_text_from_memory(workflow_memory: dict[str, Any] | None) -> str:
    memory = _as_dict(workflow_memory)
    for key in ("final_answer", "answer", "chat_answer", "last_answer"):
        text = str(memory.get(key) or "").strip()
        if text:
            return text
    items = memory.get("items")
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            for key in ("answer", "text", "value", "content"):
                text = str(first.get(key) or "").strip()
                if text:
                    return text
    return ""


def _manifest_satisfies_contract(
    manifest_items: list[dict[str, Any]] | None,
    output_contract: dict[str, Any],
) -> bool:
    items = [dict(item) for item in (manifest_items or []) if isinstance(item, dict)]
    if not items:
        return False
    kind = str(output_contract.get("output_kind") or "")
    if kind.startswith("media_") or kind in {"file_generic", "screenshot"}:
        return any(str(item.get("sha256") or item.get("path") or "") for item in items)
    if kind in {"dataset_rows", "dataset_records"}:
        rows = 0
        for item in items:
            extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            rows += int(extra.get("row_count") or 0)
            if item.get("kind") in {"dataset_rows", "dataset_records"}:
                return True
        return rows > 0
    if kind == "answer_text":
        return any(str(item.get("kind") or "") == "answer_text" for item in items)
    return len(items) > 0


def evaluate_completion(
    *,
    goal: str = "",
    output_contract: dict[str, Any] | None = None,
    output_mode: str = "",
    total_extracted_rows: int = 0,
    goal_target_count: int | None = None,
    pagination_exhausted: bool = False,
    manifest_items: list[dict[str, Any]] | None = None,
    workflow_memory: dict[str, Any] | None = None,
    browser_state: dict[str, Any] | None = None,
    capability_route: dict[str, Any] | None = None,
    no_progress_streak: int = 0,
    exit_criteria: list[dict[str, Any]] | None = None,
    current_url: str = "",
    last_action: str = "",
    last_action_success: bool = False,
) -> dict[str, Any]:
    contract = _as_dict(output_contract)
    mode = str(output_mode or contract.get("mode") or "default").strip().lower()
    output_kind = str(contract.get("output_kind") or "")
    checks: list[dict[str, Any]] = []
    evidence: list[str] = []
    target = goal_target_count
    tolerance = _extract_tolerance(target)

    extract_complete = False
    if target is not None and target > 0:
        exact = total_extracted_rows >= target
        close_enough = (
            pagination_exhausted
            and total_extracted_rows >= max(0, target - tolerance)
        )
        extract_complete = exact or close_enough
        checks.append(_check(
            "extract_row_target",
            extract_complete,
            f"rows={total_extracted_rows} target={target} tolerance={tolerance} exhausted={pagination_exhausted}",
            observed=total_extracted_rows,
            target=target,
        ))
        if extract_complete:
            evidence.append(f"extract_rows:{total_extracted_rows}/{target}")

    answer_text = _answer_text_from_memory(workflow_memory)
    answer_complete = mode == "answer" and bool(answer_text)
    if mode == "answer":
        checks.append(_check(
            "answer_in_memory",
            answer_complete,
            "workflow_memory carries final answer" if answer_complete else "answer mode but no answer text yet",
        ))
        if answer_complete:
            evidence.append("answer:workflow_memory")

    manifest_ok = _manifest_satisfies_contract(manifest_items, contract)
    if output_kind:
        checks.append(_check(
            "manifest_contract",
            manifest_ok,
            "manifest satisfies output_contract" if manifest_ok else "manifest missing expected artifact",
            output_kind=output_kind,
        ))
        if manifest_ok:
            evidence.append(f"manifest:{output_kind}")

    route_complete = False
    route = _as_dict(capability_route)
    if route and total_extracted_rows > 0:
        verification = verify_route_success(
            route,
            capability="generic_extractor",
            result={"rows": [{}] * total_extracted_rows, "row_count": total_extracted_rows},
            artifact={"path": ""},
            payload={"target_count": target},
        )
        route_complete = bool(verification.get("passed"))
        checks.append(_check(
            "success_verifier",
            route_complete,
            str(verification.get("summary") or ""),
            observed=verification.get("observed_count"),
            target=verification.get("target_count"),
        ))
        if route_complete:
            evidence.append("verifier:passed")

    no_progress_complete = (
        no_progress_streak >= 2
        and total_extracted_rows > 0
        and target is not None
        and total_extracted_rows >= max(1, target - tolerance)
    )
    if no_progress_streak:
        checks.append(_check(
            "no_progress_streak",
            no_progress_complete,
            f"streak={no_progress_streak}",
            streak=no_progress_streak,
        ))
        if no_progress_complete:
            evidence.append(f"no_progress:{no_progress_streak}")

    exit_eval = evaluate_exit_criteria(
        exit_criteria,
        total_extracted_rows=total_extracted_rows,
        current_url=current_url,
        workflow_memory=workflow_memory,
        last_action=last_action,
        last_action_success=last_action_success,
    )
    subgoal_exit_complete = bool(exit_eval.get("passed"))
    if exit_criteria:
        checks.append(_check(
            "subgoal_exit_criteria",
            subgoal_exit_complete,
            ",".join(exit_eval.get("matched") or []) or "subgoal criteria pending",
            checks=exit_eval.get("checks") or [],
        ))
        if subgoal_exit_complete:
            evidence.append("subgoal_exit:" + ",".join(exit_eval.get("matched") or []))

    complete = any([
        extract_complete,
        answer_complete,
        manifest_ok and output_kind.startswith("media_"),
        manifest_ok and output_kind in {"file_generic", "screenshot"},
        route_complete and extract_complete,
        no_progress_complete,
        subgoal_exit_complete,
    ])

    if complete:
        status = "complete"
        recommended = "done"
        confidence = 0.95 if extract_complete or answer_complete else 0.85
    elif no_progress_streak >= 3 and total_extracted_rows > 0:
        status = "blocked"
        recommended = "recovery"
        confidence = 0.6
    else:
        status = "continue"
        recommended = "continue"
        confidence = 0.5

    reasons: list[str] = []
    if extract_complete:
        reasons.append("extract_target_met")
    if answer_complete:
        reasons.append("answer_ready")
    if manifest_ok:
        reasons.append("manifest_ready")
    if subgoal_exit_complete:
        reasons.append("subgoal_exit_met")
    if no_progress_complete:
        reasons.append("pagination_stalled_with_enough_rows")

    return {
        "version": _COMPLETION_VERSION,
        "status": status,
        "confidence": confidence,
        "recommended_action": recommended,
        "checks": checks,
        "evidence": evidence,
        "reasons": reasons,
        "goal_excerpt": str(goal or "")[:120],
        "output_mode": mode,
        "output_kind": output_kind,
        "subgoal_exit": exit_eval,
    }


def maybe_short_circuit_decision(
    decision: dict[str, Any],
    *,
    goal: str = "",
    output_contract: dict[str, Any] | None = None,
    output_mode: str = "",
    total_extracted_rows: int = 0,
    goal_target_count: int | None = None,
    pagination_exhausted: bool = False,
    manifest_items: list[dict[str, Any]] | None = None,
    workflow_memory: dict[str, Any] | None = None,
    browser_state: dict[str, Any] | None = None,
    capability_route: dict[str, Any] | None = None,
    no_progress_streak: int = 0,
    exit_criteria: list[dict[str, Any]] | None = None,
    current_url: str = "",
    last_action: str = "",
    last_action_success: bool = False,
) -> dict[str, Any]:
    action = str(decision.get("action") or "").strip().lower()
    if action in _TERMINAL_ACTIONS:
        return {"short_circuit": False, "decision": decision, "evaluation": {}}

    evaluation = evaluate_completion(
        goal=goal,
        output_contract=output_contract,
        output_mode=output_mode,
        total_extracted_rows=total_extracted_rows,
        goal_target_count=goal_target_count,
        pagination_exhausted=pagination_exhausted,
        manifest_items=manifest_items,
        workflow_memory=workflow_memory,
        browser_state=browser_state,
        capability_route=capability_route,
        no_progress_streak=no_progress_streak,
        exit_criteria=exit_criteria,
        current_url=current_url,
        last_action=last_action,
        last_action_success=last_action_success,
    )
    if evaluation.get("status") != "complete":
        return {"short_circuit": False, "decision": decision, "evaluation": evaluation}
    if action not in _LOW_VALUE_WHEN_COMPLETE:
        return {"short_circuit": False, "decision": decision, "evaluation": evaluation}

    rewritten = dict(decision)
    rewritten["action"] = "done"
    rewritten["target_id"] = 0
    rewritten["type_value"] = ""
    rewritten["subgoal_status"] = "completed"
    rewritten["__completion_kernel"] = True
    rewritten["__original_action"] = action
    rewritten["thought"] = (
        str(decision.get("thought") or "")
        + f" [COMPLETION KERNEL] evidence={','.join(evaluation.get('evidence') or [])}; forced done."
    ).strip()
    return {"short_circuit": True, "decision": rewritten, "evaluation": evaluation}


def load_manifest_items_for_run(run_id: str) -> list[dict[str, Any]]:
    rid = str(run_id or "").strip()
    if not rid:
        return []
    try:
        from visual_web_agent.io_contract.persistence import read_manifest

        payload = read_manifest(rid)
        items = payload.get("items") if isinstance(payload, dict) else None
        return [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    except Exception:
        return []

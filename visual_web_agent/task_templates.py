from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TaskTemplateStep:
    id: str
    order: int
    capability: str
    purpose: str
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    preconditions: tuple[str, ...] = ()
    success_criteria: dict[str, Any] = field(default_factory=dict)
    fallback_to: tuple[str, ...] = ()
    repair_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order": self.order,
            "capability": self.capability,
            "purpose": self.purpose,
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs),
            "preconditions": list(self.preconditions),
            "success_criteria": dict(self.success_criteria),
            "fallback_to": list(self.fallback_to),
            "repair_paths": list(self.repair_paths),
        }


@dataclass(frozen=True)
class TaskTemplate:
    id: str
    name: str
    task_type: str
    description: str
    triggers: tuple[str, ...]
    steps: tuple[TaskTemplateStep, ...]
    required_capabilities: tuple[str, ...] = ()
    success_criteria: dict[str, Any] = field(default_factory=dict)
    fallback_caps: tuple[str, ...] = ()
    recommended_output_kind: str = "mixed"
    recommended_container: str = "files_folder"
    parameters_schema: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "task_type": self.task_type,
            "description": self.description,
            "triggers": list(self.triggers),
            "steps": [step.to_dict() for step in self.steps],
            "required_capabilities": list(self.required_capabilities),
            "success_criteria": dict(self.success_criteria),
            "fallback_caps": list(self.fallback_caps),
            "recommended_output_kind": self.recommended_output_kind,
            "recommended_container": self.recommended_container,
            "parameters_schema": dict(self.parameters_schema),
            "notes": list(self.notes),
        }


class TaskTemplateRegistry:
    def __init__(self, templates: list[TaskTemplate] | None = None) -> None:
        self._templates = list(templates or _default_templates())
        self._experience: dict[str, dict[str, float]] = {}

    def list(self) -> list[dict[str, Any]]:
        return [template.to_dict() for template in self._templates]

    def get(self, template_id: str) -> TaskTemplate | None:
        for template in self._templates:
            if template.id == template_id:
                return template
        return None

    def record_experience(self, template_id: str, *, passed: bool, score: float | None = None) -> None:
        data = self._experience.setdefault(template_id, {"runs": 0.0, "passes": 0.0, "score": 0.0})
        data["runs"] += 1.0
        data["passes"] += 1.0 if passed else 0.0
        if score is not None:
            data["score"] = (data["score"] + float(score)) / 2.0 if data["score"] else float(score)

    def template_score(self, template_id: str) -> float:
        data = self._experience.get(template_id) or {}
        runs = float(data.get("runs") or 0.0)
        passes = float(data.get("passes") or 0.0)
        score = float(data.get("score") or 0.0)
        if runs <= 0:
            return 0.0
        success_rate = passes / runs
        return success_rate * 2.0 + score

    def match(self, goal: str, context: dict[str, Any] | None = None) -> TaskTemplate | None:
        scored = self.score_templates(goal, context)
        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], len(item[1].steps)))
        winner_score, winner = scored[0]
        if winner_score < 1.0:
            return None
        return winner

    def best_match(self, goal: str, context: dict[str, Any] | None = None) -> tuple[TaskTemplate | None, float, str]:
        scored = self.score_templates(goal, context)
        if not scored:
            return None, 0.0, "no_template_match"
        scored.sort(key=lambda item: (-item[0], len(item[1].steps)))
        score, winner = scored[0]
        if score < 1.0:
            return None, score, "low_confidence"
        if self._should_degrade(winner.id):
            return None, score, "template_degraded"
        return winner, score, "matched"

    def score_templates(self, goal: str, context: dict[str, Any] | None = None) -> list[tuple[float, TaskTemplate]]:
        text = f"{goal or ''} {context or {}}".lower()
        scored: list[tuple[float, TaskTemplate]] = []
        for template in self._templates:
            score = self._score_template(template, text)
            if score:
                scored.append((score, template))
        return scored

    def _score_template(self, template: TaskTemplate, text: str) -> float:
        score = 0.0
        for trigger in template.triggers:
            if trigger.lower() in text:
                score += 3.0
        for capability in template.required_capabilities:
            if capability.lower() in text:
                score += 1.0
        if template.task_type == "crawl_pagination" and any(token in text for token in ["分页", "翻页", "多页", "page", "pages"]):
            score += 3.0
        if template.task_type == "export_artifact" and any(token in text for token in ["导出", "export", "保存", "文件"]):
            score += 3.0
        if template.task_type == "api_replay" and any(token in text for token in ["接口", "api", "xhr", "fetch", "回放"]):
            score += 3.0
        if template.task_type == "login_then_action" and any(token in text for token in ["登录", "login", "sign in", "auth", "验证码"]):
            score += 3.0
        if template.task_type == "visual_recovery" and any(token in text for token in ["选择器", "selector", "找不到", "不可见", "视觉"]):
            score += 3.0
        score += self.template_score(template.id)
        if self._should_degrade(template.id):
            score -= 1.5
        return score

    def _should_degrade(self, template_id: str) -> bool:
        data = self._experience.get(template_id) or {}
        runs = float(data.get("runs") or 0.0)
        if runs < 3:
            return False
        success_rate = float(data.get("passes") or 0.0) / runs if runs else 0.0
        return success_rate < 0.35


def _default_templates() -> list[TaskTemplate]:
    return [
        TaskTemplate(
            id="crawl_pagination.v1",
            name="分页抓取模板",
            task_type="crawl_pagination",
            description="适用于列表页、搜索结果页、评论列表等分页采集任务。",
            triggers=("分页", "翻页", "多页", "列表", "抓取", "crawl"),
            required_capabilities=("generic_extractor", "spider_lite", "item_pipeline", "feed_export"),
            recommended_output_kind="dataset_rows",
            recommended_container="xlsx",
            parameters_schema={"start_url": "str", "max_pages": "int", "target_count": "int", "selector": "str"},
            steps=(
                TaskTemplateStep(
                    id="step_1_open_list",
                    order=1,
                    capability="generic_extractor",
                    purpose="打开并解析列表页",
                    outputs={"items": "rows"},
                    success_criteria={"row_count": ">0"},
                    repair_paths=("selector_generator", "browser_control_find"),
                ),
                TaskTemplateStep(
                    id="step_2_paginate",
                    order=2,
                    capability="spider_lite",
                    purpose="翻页并持续采集",
                    inputs={"max_pages": 10},
                    outputs={"items": "rows"},
                    preconditions=("list page opened",),
                    success_criteria={"item_count": ">0"},
                    repair_paths=("api_replay", "browser_control"),
                ),
                TaskTemplateStep(
                    id="step_3_export",
                    order=3,
                    capability="feed_export",
                    purpose="导出采集结果",
                    outputs={"artifact": "file"},
                    preconditions=("items ready",),
                    success_criteria={"artifact": "exists"},
                    repair_paths=("artifact_manager",),
                ),
            ),
            success_criteria={"count": ">0", "artifact": True},
            notes=("Prefer API or deterministic extractor before visual fallback.",),
        ),
        TaskTemplate(
            id="export_artifact.v1",
            name="导出文件模板",
            task_type="export_artifact",
            description="适用于把结果导出为 JSONL、CSV、Excel 或 Markdown 文件的任务。",
            triggers=("导出", "export", "保存", "文件", "下载"),
            required_capabilities=("feed_export", "artifact_manager"),
            recommended_output_kind="file_generic",
            recommended_container="files_folder",
            parameters_schema={"output_format": "str", "filename": "str", "base_dir": "str"},
            steps=(
                TaskTemplateStep(
                    id="step_1_prepare_export",
                    order=1,
                    capability="feed_export",
                    purpose="准备导出文件",
                    outputs={"artifact": "file"},
                    success_criteria={"artifact": "exists"},
                    repair_paths=("artifact_manager",),
                ),
            ),
            success_criteria={"artifact": True},
            notes=("Validate file existence and size after write.",),
        ),
        TaskTemplate(
            id="api_replay.v1",
            name="接口回放模板",
            task_type="api_replay",
            description="适用于页面数据来源稳定接口的回放和复现。",
            triggers=("接口", "api", "xhr", "fetch", "回放"),
            required_capabilities=("api_replay", "network_intelligence", "generic_extractor"),
            recommended_output_kind="dataset_records",
            recommended_container="jsonl",
            parameters_schema={"endpoint": "str", "method": "str", "payload": "dict"},
            steps=(
                TaskTemplateStep(
                    id="step_1_probe_api",
                    order=1,
                    capability="network_intelligence",
                    purpose="识别接口请求",
                    outputs={"api_candidates": "list"},
                    success_criteria={"candidates": ">0"},
                    repair_paths=("api_replay",),
                ),
                TaskTemplateStep(
                    id="step_2_replay_api",
                    order=2,
                    capability="api_replay",
                    purpose="回放接口并取回数据",
                    preconditions=("api candidates found",),
                    outputs={"items": "records"},
                    success_criteria={"response": "valid"},
                    repair_paths=("network_intelligence", "browser_control"),
                ),
            ),
            success_criteria={"response": "valid"},
        ),
        TaskTemplate(
            id="login_then_action.v1",
            name="登录后操作模板",
            task_type="login_then_action",
            description="适用于需要账号态、会话态或二次验证后执行的任务。",
            triggers=("登录", "login", "auth", "验证码", "2fa"),
            required_capabilities=("browser_control", "human_guard"),
            recommended_output_kind="mixed",
            recommended_container="files_folder",
            parameters_schema={"auth_profile": "str", "needs_2fa": "bool"},
            steps=(
                TaskTemplateStep(
                    id="step_1_auth",
                    order=1,
                    capability="browser_control",
                    purpose="完成登录或身份检查",
                    outputs={"browser_state": "authenticated"},
                    success_criteria={"authenticated": True},
                    repair_paths=("human_guard", "browser_runtime_doctor"),
                ),
                TaskTemplateStep(
                    id="step_2_action",
                    order=2,
                    capability="action_registry_macros",
                    purpose="登录后执行目标操作",
                    preconditions=("authenticated",),
                    outputs={"action_evidence": "present"},
                    success_criteria={"action_result": "ok"},
                    repair_paths=("selector_generator", "vision_agent"),
                ),
            ),
            success_criteria={"authenticated": True, "action_result": "ok"},
        ),
        TaskTemplate(
            id="visual_recovery.v1",
            name="视觉兜底修复模板",
            task_type="visual_recovery",
            description="适用于 selector 失效、DOM 不稳定或复杂交互的兜底修复。",
            triggers=("视觉", "selector", "选择器", "找不到", "不可见", "兜底"),
            required_capabilities=("vision_agent", "selector_generator", "browser_control_find"),
            recommended_output_kind="mixed",
            recommended_container="files_folder",
            parameters_schema={"visual_target": "str", "scan_region": "str"},
            steps=(
                TaskTemplateStep(
                    id="step_1_scan",
                    order=1,
                    capability="vision_agent",
                    purpose="视觉扫描目标区域",
                    outputs={"browser_state": "visual_grounding"},
                    success_criteria={"target_visible": True},
                    repair_paths=("selector_generator",),
                ),
                TaskTemplateStep(
                    id="step_2_recover",
                    order=2,
                    capability="browser_control_find",
                    purpose="恢复可点击或可输入目标",
                    preconditions=("target visible",),
                    outputs={"action_evidence": "present"},
                    success_criteria={"recovered": True},
                    repair_paths=("vision_agent", "browser_control"),
                ),
            ),
            success_criteria={"recovered": True},
        ),
    ]

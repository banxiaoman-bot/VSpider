"""Prompt regressions for artifact / manifest completion discipline."""

from __future__ import annotations

from visual_web_agent.prompt_skills import (
    BULK_EXTRACT_SKILL,
    COMPLETION_PROMPT,
    DOWNLOAD_SKILL,
    EXTRACT_SKILL,
)
from visual_web_agent.prompts import build_user_message


def test_completion_prompt_requires_manifest_evidence_before_done() -> None:
    assert "runs/<run_id>/manifest.json" in COMPLETION_PROMPT
    assert "artifact 路径" in COMPLETION_PROMPT
    assert "download_completed" in COMPLETION_PROMPT
    assert "不要编造文件名" in COMPLETION_PROMPT


def test_download_skill_ties_artifacts_to_current_run_manifest() -> None:
    assert "当前 run" in DOWNLOAD_SKILL
    assert "runs/<run_id>/manifest.json" in DOWNLOAD_SKILL
    assert "manifest item" in DOWNLOAD_SKILL
    assert "口头总结" in DOWNLOAD_SKILL


def test_extract_skills_prefer_structured_paths_over_visual_reading() -> None:
    assert "网络/API 拦截" in EXTRACT_SKILL
    assert "api_replay" in EXTRACT_SKILL
    assert "VLM 逐行读屏" in EXTRACT_SKILL
    assert "artifact/manifest 证据" in BULK_EXTRACT_SKILL


def test_artifact_output_intent_requires_output_evidence() -> None:
    message = build_user_message(
        goal="抓取前10条数据，字段为 title, url，并导出 Excel",
        step=1,
        max_steps=10,
    )

    assert "structured data or a saved artifact" in message
    assert "network/API replay" in message
    assert "manifest item" in message
    assert "do not invent filenames" in message

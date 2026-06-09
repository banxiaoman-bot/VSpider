"""三入口对等回归：CLI ``main.py`` 不得把 ``--url`` 当硬必填。

mission §一 / §一-B：前端 / CLI / API 三入口对等，goal 是唯一始终必填，
``url`` 可省略（由 goal→URL 推断 / preflight 兜底补全）。此前 CLI 的
``--url required=True`` 违反对等——仅 ``--goal`` 启动会被 argparse 拒绝。

本测试直接驱动真实的 ``main.main()`` + 真实 argparse，patch 掉 run_agent /
asyncio.run 以免真的拉起浏览器，验证：仅给 ``--goal`` 时不报
``the following arguments are required: --url``，且 ``url`` 以空串透传给
run_agent（让下游 preflight 去推断入口）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _consume(coro):
    """Stand-in for ``asyncio.run`` that closes the coroutine without running
    it, so the test exercises ``main()``'s arg parsing without launching the
    browser and without leaking a 'coroutine was never awaited' warning."""
    if hasattr(coro, "close"):
        coro.close()
    return None


@pytest.fixture()
def main_module():
    return pytest.importorskip("visual_web_agent.main")


def test_cli_starts_with_only_goal(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    m = main_module
    captured: dict[str, object] = {}

    def _fake_run_agent(url, goal, **kwargs):
        captured["url"] = url
        captured["goal"] = goal

        async def _noop():
            return True

        return _noop()

    monkeypatch.setattr(m, "run_agent", _fake_run_agent, raising=True)
    monkeypatch.setattr(m.asyncio, "run", _consume, raising=True)
    if hasattr(m, "_apply_runtime_overrides"):
        monkeypatch.setattr(m, "_apply_runtime_overrides", lambda args: None, raising=True)
    monkeypatch.setattr(sys, "argv", ["main.py", "--goal", "查一下今天的金价"], raising=True)

    # Must NOT raise SystemExit(2) for a missing --url.
    m.main()

    assert captured.get("goal") == "查一下今天的金价"
    assert captured.get("url") == "", "缺省 --url 应以空串透传给 run_agent，由 preflight 推断入口"


def test_cli_explicit_url_still_passed(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    m = main_module
    captured: dict[str, object] = {}

    def _fake_run_agent(url, goal, **kwargs):
        captured["url"] = url
        captured["goal"] = goal

        async def _noop():
            return True

        return _noop()

    monkeypatch.setattr(m, "run_agent", _fake_run_agent, raising=True)
    monkeypatch.setattr(m.asyncio, "run", _consume, raising=True)
    if hasattr(m, "_apply_runtime_overrides"):
        monkeypatch.setattr(m, "_apply_runtime_overrides", lambda args: None, raising=True)
    monkeypatch.setattr(
        sys, "argv",
        ["main.py", "--url", "https://example.com", "--goal", "找联系方式"],
        raising=True,
    )

    m.main()

    assert captured.get("url") == "https://example.com"


def test_cli_threads_runtime_options(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    m = main_module
    captured: dict[str, object] = {}

    def _fake_run_agent(url, goal, **kwargs):
        captured["url"] = url
        captured["goal"] = goal
        captured["kwargs"] = kwargs

        async def _noop():
            return True

        return _noop()

    monkeypatch.setattr(m, "run_agent", _fake_run_agent, raising=True)
    monkeypatch.setattr(m.asyncio, "run", _consume, raising=True)
    if hasattr(m, "_apply_runtime_overrides"):
        monkeypatch.setattr(m, "_apply_runtime_overrides", lambda args: None, raising=True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main.py",
            "--url", "https://example.com",
            "--goal", "collect",
            "--auth-profiles", "admin",
            "--run-constraints-json", '{"max_runs": 2}',
            "--resume",
            "--vlm-model", "vision-model",
            "--semantic-model", "semantic-model",
            "--vlm-model-type", "text",
            "--vlm-temperature", "0.2",
            "--vlm-max-tokens", "1024",
            "--vlm-base-url", "https://vlm.example/v1",
            "--semantic-base-url", "https://semantic.example/v1",
        ],
        raising=True,
    )

    m.main()

    kwargs = captured.get("kwargs")
    assert isinstance(kwargs, dict)
    assert kwargs["auth_profiles"] == "admin"
    assert kwargs["run_constraints"] == {"max_runs": 2, "resume": True}
    assert kwargs["vlm_options"] == {
        "model": "vision-model",
        "semantic_model": "semantic-model",
        "model_type": "text",
        "base_url": "https://vlm.example/v1",
        "semantic_base_url": "https://semantic.example/v1",
        "temperature": 0.2,
        "max_tokens": 1024,
    }


def test_run_agent_input_contract_skeleton_records_vlm_options() -> None:
    src = (Path(__file__).resolve().parent.parent / "visual_web_agent" / "main.py").read_text(
        encoding="utf-8",
    )

    assert "ensure_input_contract_skeleton(" in src
    assert "vlm_options=vlm_options or None" in src

"""Full agent-loop regression against the local fixture site with a VLM stub.

Unlike ``test_complex_scenarios_e2e.py`` (which drives Playwright directly and
exercises VSpider's deterministic helpers), this test boots the *real*
``run_agent`` main loop end-to-end against the Alpha fixture server, with the
``VLMClient`` replaced by a scripted offline stub. It proves the whole
pipeline — preflight → browser launch → per-step SoM screenshot →
bot-challenge probe → VLM decision → ``done`` finalize — runs on a local site
with no network access and no real model.

The stub:
* ``make_plan`` / ``reflect`` / ``extract_structured_data`` return their
  zero-regression fallbacks (no LLM call),
* ``ask`` records the call and returns a single ``done`` decision so the loop
  terminates deterministically on the first decision step.

Skipped cleanly when no headless Chromium is available.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from tests.scenario_site import ScenarioServer, ScenarioStore, make_alpha_handler


# ---------------------------------------------------------------------------
# Chromium availability probe (mirrors the complex-scenario suite)
# ---------------------------------------------------------------------------


def _resolve_browsers_path() -> None:
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if default.is_dir() and any(default.glob("chromium*")):
        return
    fallback = Path.home() / "AppData" / "Local" / "ms-playwright"
    if fallback.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(fallback)


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    _resolve_browsers_path()
    try:
        pw = sync_playwright().start()
    except Exception:
        return False
    try:
        inst = pw.chromium.launch(headless=True)
        inst.close()
        return True
    except Exception:
        return False
    finally:
        pw.stop()


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="headless chromium not available"
)


# ---------------------------------------------------------------------------
# Scripted offline VLM stub
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.ask_calls = 0
        self.plan_calls = 0
        self.goals: list[str] = []


def _make_stub_cls(recorder: _Recorder):
    from visual_web_agent.vlm_client import (
        ReflectorDecision,
        SubGoal,
        TaskPlan,
        VLMClient,
    )

    class _ScriptedVLM(VLMClient):
        async def make_plan(self, goal, initial_url, workflow_memory=None):
            recorder.plan_calls += 1
            return TaskPlan(
                goal=goal,
                sub_goals=[SubGoal(
                    id=1,
                    description=goal[:80] or "task",
                    exit_criteria="确认页面加载完成即可结束",
                    status="active",
                )],
                current_idx=0,
            )

        async def reflect(self, plan, history_summary, signals, current_url):
            return ReflectorDecision(decision="continue", reason="stub reflector")

        async def extract_structured_data(self, page_text, goal, example_data=None):
            return []

        async def ask(
            self,
            screenshot_b64,
            goal,
            step,
            input_descriptions="",
            workflow_memory=None,
            task_plan=None,
            max_steps=None,
            som_elements=None,
            capability_route=None,
            extra_images=None,
        ):
            recorder.ask_calls += 1
            recorder.goals.append(goal)
            return [{
                "action": "done",
                "target_id": 0,
                "status": "success",
                "thought": "首页已正常加载，任务确认完成。",
                "type_value": "",
                "subgoal_status": "completed",
            }]

    return _ScriptedVLM


# ---------------------------------------------------------------------------
# Fixture site
# ---------------------------------------------------------------------------


@pytest.fixture()
def alpha_site():
    alpha = ScenarioServer(make_alpha_handler(ScenarioStore()), host="127.0.0.1").start()
    yield alpha
    alpha.stop()


# ---------------------------------------------------------------------------
# Test: full loop terminates with a done decision driven by the stub
# ---------------------------------------------------------------------------


def test_run_agent_full_loop_finishes_on_fixture(alpha_site, monkeypatch):
    from visual_web_agent import config as vw_config
    from visual_web_agent import main as vw_main

    recorder = _Recorder()
    monkeypatch.setattr(vw_main, "VLMClient", _make_stub_cls(recorder))
    # headless + small step budget + loopback allowed -> fast, bounded, offline
    monkeypatch.setattr(vw_config, "HEADLESS", True)
    monkeypatch.setattr(vw_main, "MAX_STEPS", 4)
    monkeypatch.setenv("VSPIDER_ALLOW_PRIVATE_URLS", "1")

    goal = "打开页面，确认 Alpha 商品目录首页已正常加载后立即结束"

    async def _drive():
        return await asyncio.wait_for(
            vw_main.run_agent(
                f"{alpha_site.base_url}/",
                goal,
                run_id="stub_loop_alpha",
            ),
            timeout=180,
        )

    result = asyncio.run(_drive())

    # the scripted stub actually drove the perception->decision cycle
    assert recorder.plan_calls >= 1, "make_plan must be invoked by the loop"
    assert recorder.ask_calls >= 1, "vlm.ask must be reached at least once"
    # the loop forwards the goal (with an appended auth-environment notice) to ask
    assert recorder.goals[0].startswith(goal)
    # the loop honored the done decision and returned a terminal bool
    assert result is True


def test_run_agent_loop_records_run_artifacts(alpha_site, monkeypatch):
    """The loop must leave a run trace (event stream) for the stub-driven run."""
    from visual_web_agent import config as vw_config
    from visual_web_agent import main as vw_main

    recorder = _Recorder()
    monkeypatch.setattr(vw_main, "VLMClient", _make_stub_cls(recorder))
    monkeypatch.setattr(vw_config, "HEADLESS", True)
    monkeypatch.setattr(vw_main, "MAX_STEPS", 4)
    monkeypatch.setenv("VSPIDER_ALLOW_PRIVATE_URLS", "1")

    run_id = "stub_loop_artifacts"
    logs_dir = Path("logs")
    before = set(logs_dir.glob(f"event_stream_*{run_id}*.jsonl")) if logs_dir.exists() else set()

    async def _drive():
        return await asyncio.wait_for(
            vw_main.run_agent(
                f"{alpha_site.base_url}/",
                "打开页面，确认首页已正常加载后立即结束",
                run_id=run_id,
            ),
            timeout=180,
        )

    result = asyncio.run(_drive())
    assert result is True
    assert recorder.ask_calls >= 1

    # a run directory or event stream for this run id should now exist
    run_dir = Path("runs") / run_id
    event_streams = list(logs_dir.glob("event_stream_*.jsonl")) if logs_dir.exists() else []
    assert run_dir.exists() or event_streams, "stub-driven run produced no trace"

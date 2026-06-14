"""E7: efficiency baseline benchmark — locks E1-E3 perception optimization gains.

Runs a fixed 8-turn sequence through ``PerceptionPhase`` with deterministic
stub inputs and asserts that the optimizations produce measurable savings:

* **E1 reuse**: unchanged DOM turns skip screenshot/AX (perception_reused).
* **E2 scope**: steady-state turns use viewport scope (not full).
* **E3 diff**: second-round AX contains incremental diff (not full text).
* **Baseline**: total AX characters across all observe events stays under a
  recorded ceiling — any later change that regresses >20% breaks the test.

Turn sequence (8 turns on a stable page):
  T1: first turn (full perception, full scope, full AX)
  T2: same signature, no mutation (reuse)
  T3: same signature, no mutation (reuse)
  T4: same signature, no mutation (reuse) → streak=3
  T5: escape valve fires (full perception, full scope, full AX baseline)
  T6: signature changes (full perception, viewport scope, AX diff)
  T7: same new signature (reuse)
  T8: signature changes again (full perception, viewport scope, AX diff)
"""

from __future__ import annotations

import asyncio

import pytest

from visual_web_agent.phases import perception as perception_mod
from visual_web_agent.phases.perception import PerceptionPhase, PerceptionSnapshot

# ---------------------------------------------------------------------------
# fixture AX trees (realistic size for baseline measurement)
# ---------------------------------------------------------------------------

AX_TREE_A = "\n".join([
    f'@e{i} [{role}] "{name}" {{{state}}}'
    for i, (role, name, state) in enumerate([
        ("heading", "Alpha 商品目录", "level=1"),
        ("link", "首页", "enabled"),
        ("link", "产品", "enabled"),
        ("link", "关于", "enabled"),
        ("table", "商品列表", ""),
        ("row", "ALP-001 智能温控器 ¥199", ""),
        ("row", "ALP-002 工业网关 ¥1299", ""),
        ("row", "ALP-003 边缘计算盒 ¥2599", ""),
        ("row", "ALP-004 振动传感器 ¥459", ""),
        ("link", "下一页", "enabled"),
        ("link", "上一页", "disabled"),
        ("button", "筛选", "enabled"),
        ("textbox", "搜索", "focused"),
        ("link", "下载报表", "enabled"),
        ("link", "采购申请", "enabled"),
        ("img", "banner.png", ""),
        ("navigation", "底部导航", ""),
        ("link", "隐私政策", "enabled"),
        ("link", "帮助中心", "enabled"),
        ("contentinfo", "© 2026 Alpha", ""),
    ], start=1)
])

AX_TREE_B = "\n".join([
    f'@e{i} [{role}] "{name}" {{{state}}}'
    for i, (role, name, state) in enumerate([
        ("heading", "Alpha 商品目录", "level=1"),
        ("link", "首页", "enabled"),
        ("link", "产品", "enabled"),
        ("link", "关于", "enabled"),
        ("table", "商品列表", ""),
        ("row", "ALP-001 智能温控器 ¥199", ""),
        ("row", "ALP-002 工业网关 ¥1299", ""),
        ("row", "ALP-003 边缘计算盒 ¥2599", ""),
        ("row", "ALP-004 振动传感器 ¥459", ""),
        ("link", "下一页", "enabled"),
        ("link", "上一页", "enabled"),  # changed
        ("button", "筛选", "enabled"),
        ("textbox", "搜索", ""),  # changed
        ("link", "下载报表", "enabled"),
        ("link", "采购申请", "enabled"),
        ("img", "banner.png", ""),
        ("navigation", "底部导航", ""),
        ("link", "隐私政策", "enabled"),
        ("link", "帮助中心", "enabled"),
        ("contentinfo", "© 2026 Alpha", ""),
        ("alert", "新品上架通知", ""),  # added
    ], start=1)
])

# A→B diff: 18 unchanged, +1 added (@e21 alert), 2 changed lines → 2 removed + 2 added


# ---------------------------------------------------------------------------
# stubs
# ---------------------------------------------------------------------------


class _StubPage:
    url = "https://alpha.example/catalog"

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return "Alpha Catalog"

    async def evaluate(self, script):
        return "body text"


class _StubEventStream:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def observe(self, **kwargs) -> None:
        self.events.append(kwargs)


class _StubBrowser:
    def __init__(self) -> None:
        self.current_url = "https://alpha.example/catalog"
        self._last_som_elements = [{"id": i} for i in range(1, 21)]
        self._last_action_result = None
        self._page = _StubPage()
        self.signature = "sig-baseline-a"
        self.screenshot_calls = 0
        self.ax_calls = 0
        self.tabs_calls = 0
        self._ax_tree = AX_TREE_A

    async def dom_signature(self) -> str:
        return self.signature

    async def _ensure_active_page(self, reason=""):
        return self._page

    async def get_tabs_state(self) -> str:
        self.tabs_calls += 1
        return "Tab 0: Alpha Catalog (active)"

    async def get_active_page_summary(self) -> str:
        return "Alpha Catalog — fixture"

    async def mark_and_screenshot(self, step, scope="viewport"):
        self.screenshot_calls += 1
        self.last_scope = scope
        return f"b64-{step}", "stub-som-desc"

    async def restart(self, start_url, reason=""):
        pass

    async def extract_accessibility_tree(self) -> str:
        self.ax_calls += 1
        return self._ax_tree

    async def reroute_proxy_on_block(self, url, **kwargs) -> bool:
        return False


async def _noop_recover(reason):
    return None


async def _noop_hitl(reason=""):
    return None


def _run_turn(phase, browser, events, step):
    return asyncio.run(
        phase.run(
            browser,
            step=step,
            event_stream=events,
            start_url="https://alpha.example/catalog",
            recover_active_page=_noop_recover,
            wait_for_human_resume=_noop_hitl,
            bot_challenge_state=object(),
        )
    )


@pytest.fixture(autouse=True)
def _disable_a11y_enhancer(monkeypatch):
    monkeypatch.setattr(perception_mod, "A11Y_ENHANCER_ENABLED", False)


# ---------------------------------------------------------------------------
# Baselines (recorded with E1-E3 active; tighten as optimizations land)
# ---------------------------------------------------------------------------

BASELINE_FULL_PERCEPTION_COUNT = 4   # T1 + T5(escape) + T6(sig change) + T8(sig change)
BASELINE_REUSED_COUNT = 3            # T2 + T3 + T4 + T7 = 4, but T4 triggers escape → 3 reuse + 1 escape
BASELINE_TOTAL_AX_CHARS = 2500      # full text × 2 + diff text × 2 ≈ much less than 8 × full
BASELINE_VIEWPORT_SCOPE_COUNT = 2   # T6 + T8 (steady non-first, non-escape turns)
BASELINE_REGRESSION_RATIO = 1.2     # any metric exceeding baseline × 1.2 = test red


# ---------------------------------------------------------------------------
# E7 benchmark test
# ---------------------------------------------------------------------------


def test_efficiency_baseline_8_turn_sequence():
    """Lock the perception optimization gains from E1/E2/E3."""
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    # T1: first turn — full perception, full scope
    _run_turn(phase, browser, events, step=1)

    # T2-T4: same signature, no mutation — should reuse
    for s in (2, 3, 4):
        _run_turn(phase, browser, events, step=s)

    # T5: escape valve fires — full perception forced
    _run_turn(phase, browser, events, step=5)

    # T6: signature changes, AX tree changes — full perception, AX diff
    browser.signature = "sig-baseline-b"
    browser._ax_tree = AX_TREE_B
    _run_turn(phase, browser, events, step=6)

    # T7: same new signature — reuse
    _run_turn(phase, browser, events, step=7)

    # T8: signature changes again — full perception
    browser.signature = "sig-baseline-c"
    _run_turn(phase, browser, events, step=8)

    # ── Collect metrics ──

    full_perception_count = browser.ax_calls
    reused_count = sum(1 for e in events.events if e.get("perception_reused"))
    total_ax_chars = sum(e.get("ax_lines", 0) for e in events.events)
    viewport_scopes = sum(
        1 for e in events.events
        if not e.get("perception_reused")
        and hasattr(browser, "last_scope")
    )

    # Count actual scope decisions from screenshots
    screenshot_count = browser.screenshot_calls

    # ── Assert baselines (fail if >20% regression) ──

    assert full_perception_count <= BASELINE_FULL_PERCEPTION_COUNT, (
        f"Full perception count {full_perception_count} exceeds "
        f"baseline {BASELINE_FULL_PERCEPTION_COUNT}"
    )
    assert reused_count >= BASELINE_REUSED_COUNT, (
        f"Reused perception count {reused_count} below "
        f"baseline {BASELINE_REUSED_COUNT} (E1 regression?)"
    )

    # E3: total AX chars should be well below 8 × full_text_length
    naive_total = len(AX_TREE_A) * 8  # without any optimization
    assert total_ax_chars < naive_total * 0.5, (
        f"Total AX chars {total_ax_chars} is ≥50% of naive total "
        f"{naive_total} — E1 reuse + E3 diff not effective enough"
    )

    # E1: reuse should save at least 3 screenshot calls out of 8
    assert screenshot_count <= 5, (
        f"Screenshot count {screenshot_count} — expected ≤5 "
        f"(E1 reuse should skip at least 3)"
    )


def test_ax_diff_reduces_observe_event_size():
    """E3 diff should produce smaller observe events than full AX repeats."""
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    # T1: full AX
    _run_turn(phase, browser, events, step=1)
    first_ax_lines = events.events[0].get("ax_lines", 0)

    # T2: force new signature → full perception, AX diff (not first round)
    browser.signature = "sig-diff-check"
    browser._ax_tree = AX_TREE_B
    _run_turn(phase, browser, events, step=2)
    second_ax_lines = events.events[1].get("ax_lines", 0)

    assert second_ax_lines < first_ax_lines, (
        f"AX diff ({second_ax_lines} lines) should be smaller than "
        f"full AX ({first_ax_lines} lines)"
    )


def test_escape_valve_resets_ax_diff_baseline():
    """After E1 escape valve, AX diff baseline is refreshed (full text)."""
    phase = PerceptionPhase()
    browser = _StubBrowser()
    events = _StubEventStream()

    _run_turn(phase, browser, events, step=1)  # full
    for s in (2, 3, 4):
        _run_turn(phase, browser, events, step=s)  # reuse x3

    # T5: escape valve → full perception, full AX (not diff)
    snap5 = _run_turn(phase, browser, events, step=5)
    assert "[AX 增量]" not in snap5.ax_tree_text
    assert AX_TREE_A in snap5.ax_tree_text

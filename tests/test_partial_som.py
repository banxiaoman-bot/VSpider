"""E2 局部 SoM（partial / viewport-scoped Set-of-Mark）.

锁定三层契约：

JS 选区语义（真 Chromium 长页 fixture）
* ``scope="full"`` 标注整页可交互元素（含视口外的折叠下方元素）；
* ``scope="viewport"`` 只标注与视口相交的元素——是 full 集合的真子集，且
  不含视口外元素（= 当前默认行为，向下兼容旧的纯数字 startIndex 调用）；
* ``scope={"selector": "#region"}`` 只标注容器子树内的元素。

感知层选区决策（stub，无需 Chromium）
* 首回合 / E1 逃生阀回合 / 上一动作为滚动翻页 / VLM 显式请求全页 → ``full``；
* 其余稳定回合 → ``viewport``；VLM 请求是一次性的（消费后复位）；
* 滚动后即便 DOM 签名未变也必须重新感知（修正 E1「滚动后复用陈旧快照」）。

幻觉 target_id 报错带 scope 提示
* viewport 局部标注下命中越界 ID → 报错提示「目标可能在视口外，先 scroll」。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from visual_web_agent.config import SOM_SCRIPT_PATH
from visual_web_agent.phases import perception as perception_mod
from visual_web_agent.phases.perception import PerceptionPhase


# ---------------------------------------------------------------------------
# Chromium availability probe (mirrors the e2e suites)
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


requires_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="headless chromium not available"
)


_FIXTURE_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<style>
  body { margin: 0; }
  button { display: block; width: 220px; height: 40px; margin: 8px; }
  #spacer { margin-top: 3000px; }
</style></head><body>
  <div id="region">
    <button>region-1</button>
    <button>region-2</button>
  </div>
  <button>top-a</button>
  <button>top-b</button>
  <div id="spacer">
    <button>bottom-a</button>
    <button>bottom-b</button>
  </div>
</body></html>
"""

_ABOVE_FOLD = {"region-1", "region-2", "top-a", "top-b"}
_BELOW_FOLD = {"bottom-a", "bottom-b"}
_REGION = {"region-1", "region-2"}


async def _marked_texts(arg) -> set[str]:
    from playwright.async_api import async_playwright

    som_js = SOM_SCRIPT_PATH.read_text(encoding="utf-8")
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    try:
        ctx = await browser.new_context(viewport={"width": 1000, "height": 700})
        page = await ctx.new_page()
        await page.set_content(_FIXTURE_HTML, wait_until="load")
        await page.wait_for_timeout(150)
        if arg is None:
            await page.evaluate(som_js)
        else:
            await page.evaluate(som_js, arg)
        texts = await page.eval_on_selector_all(
            "[data-som-id]", "els => els.map(e => (e.textContent || '').trim())"
        )
        await ctx.close()
        return {t for t in texts if t}
    finally:
        await browser.close()
        await pw.stop()


@requires_chromium
def test_full_scope_marks_above_and_below_fold():
    marked = asyncio.run(_marked_texts({"scope": "full"}))
    assert _ABOVE_FOLD <= marked, f"full should mark above-fold buttons; got {marked}"
    assert _BELOW_FOLD <= marked, f"full should mark below-fold buttons; got {marked}"


@requires_chromium
def test_viewport_scope_is_subset_of_full_and_excludes_below_fold():
    full = asyncio.run(_marked_texts({"scope": "full"}))
    viewport = asyncio.run(_marked_texts({"scope": "viewport"}))
    known = _ABOVE_FOLD | _BELOW_FOLD
    full_known = full & known
    viewport_known = viewport & known
    # viewport ⊊ full over the known button universe
    assert viewport_known < full_known, (
        f"viewport set must be a strict subset of full; "
        f"viewport={viewport_known} full={full_known}"
    )
    # viewport keeps above-fold, drops below-fold
    assert _ABOVE_FOLD <= viewport, f"viewport should mark above-fold; got {viewport}"
    assert not (_BELOW_FOLD & viewport), f"viewport must exclude below-fold; got {viewport}"


@requires_chromium
def test_legacy_number_arg_behaves_as_viewport():
    # Backward compat: the historical ``frame.evaluate(som_js, current_id)``
    # number form must keep the old viewport-bound behavior.
    legacy = asyncio.run(_marked_texts(1))
    assert _ABOVE_FOLD <= legacy
    assert not (_BELOW_FOLD & legacy)


@requires_chromium
def test_no_arg_defaults_to_viewport():
    none_arg = asyncio.run(_marked_texts(None))
    assert _ABOVE_FOLD <= none_arg
    assert not (_BELOW_FOLD & none_arg)


@requires_chromium
def test_selector_scope_only_marks_container():
    marked = asyncio.run(_marked_texts({"scope": {"selector": "#region"}}))
    known = _ABOVE_FOLD | _BELOW_FOLD
    assert (marked & known) == _REGION, (
        f"selector scope must mark only container buttons; got {marked & known}"
    )


# ---------------------------------------------------------------------------
# perception-layer scope decision (stub, no chromium)
# ---------------------------------------------------------------------------


class _ScopePage:
    url = "https://alpha.example/list"

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return "Alpha"

    async def evaluate(self, script):
        return "body text"


class _ScrollAction:
    action = "smooth_scroll"
    changed_url = False
    changed_dom = False


class _ScopeStubBrowser:
    def __init__(self) -> None:
        self.current_url = "https://alpha.example/list"
        self._last_som_elements = [{"id": 1}]
        self._last_action_result = None
        self._page = _ScopePage()
        self.signature = "sig-1"
        self.scope_calls: list = []
        self.screenshot_calls = 0

    async def dom_signature(self) -> str:
        return self.signature

    async def _ensure_active_page(self, reason: str = ""):
        return self._page

    async def get_tabs_state(self) -> str:
        return ""

    async def get_active_page_summary(self) -> str:
        return ""

    async def mark_and_screenshot(self, step: int, scope="viewport"):
        self.screenshot_calls += 1
        self.scope_calls.append(scope)
        return f"shot-{self.screenshot_calls}", "@e1 button"

    async def restart(self, start_url: str, reason: str = "") -> None:
        pass

    async def extract_accessibility_tree(self) -> str:
        return '@e1 [button] "x"'

    async def reroute_proxy_on_block(self, url, **kwargs) -> bool:
        return False


async def _noop_recover(reason: str):
    return None


async def _noop_hitl(reason: str = "") -> None:
    return None


def _run_turn(phase, browser, step):
    class _Events:
        def observe(self, **kwargs):
            pass

    return asyncio.run(
        phase.run(
            browser,
            step=step,
            event_stream=_Events(),
            start_url="https://alpha.example/list",
            recover_active_page=_noop_recover,
            wait_for_human_resume=_noop_hitl,
            bot_challenge_state=object(),
        )
    )


@pytest.fixture(autouse=True)
def _disable_a11y(monkeypatch):
    monkeypatch.setattr(perception_mod, "A11Y_ENHANCER_ENABLED", False)


def test_first_turn_uses_full_scope():
    phase = PerceptionPhase()
    browser = _ScopeStubBrowser()
    _run_turn(phase, browser, 1)
    assert browser.scope_calls == ["full"]


def test_steady_turn_uses_viewport_scope():
    phase = PerceptionPhase()
    browser = _ScopeStubBrowser()
    _run_turn(phase, browser, 1)           # first → full
    browser.signature = "sig-2"            # changed → re-perceive (not reuse)
    _run_turn(phase, browser, 2)
    assert browser.scope_calls == ["full", "viewport"]


def test_scroll_action_forces_full_scope_even_with_same_signature():
    phase = PerceptionPhase()
    browser = _ScopeStubBrowser()
    _run_turn(phase, browser, 1)           # full
    browser.signature = "sig-2"
    _run_turn(phase, browser, 2)           # viewport
    # scroll keeps the DOM signature identical, but the viewport moved →
    # E2 must re-perceive with full scope (and not reuse the stale snapshot).
    browser._last_action_result = _ScrollAction()
    _run_turn(phase, browser, 3)
    assert browser.scope_calls == ["full", "viewport", "full"]


def test_escape_valve_turn_uses_full_scope():
    phase = PerceptionPhase()
    browser = _ScopeStubBrowser()
    _run_turn(phase, browser, 1)           # full
    for step in (2, 3, 4):                  # reuse x3 (same signature, no action)
        _run_turn(phase, browser, step)
    assert browser.screenshot_calls == 1
    _run_turn(phase, browser, 5)           # escape valve → full perception
    assert browser.scope_calls == ["full", "full"]


def test_vlm_request_full_som_is_one_shot():
    phase = PerceptionPhase()
    browser = _ScopeStubBrowser()
    _run_turn(phase, browser, 1)           # full
    browser._request_full_som = True
    browser.signature = "sig-2"
    _run_turn(phase, browser, 2)           # would be viewport, but VLM asked full
    browser.signature = "sig-3"
    _run_turn(phase, browser, 3)           # flag consumed → viewport
    assert browser.scope_calls == ["full", "full", "viewport"]
    assert getattr(browser, "_request_full_som") is False


# ---------------------------------------------------------------------------
# hallucination error scope hint
# ---------------------------------------------------------------------------


class _ResolveStub:
    def __init__(self, scope: str) -> None:
        self._last_som_elements = [{"id": 1}, {"id": 2}]
        self._last_som_scope = scope
        self._last_action_error = None

    async def _ensure_active_page(self, reason: str = ""):
        return object()  # truthy page; hallucination branch returns before use


def _resolve(stub):
    from visual_web_agent.browser_env import BrowserEnv

    return asyncio.run(BrowserEnv._resolve_action_target(stub, 999, "click"))


def test_viewport_scope_hallucination_error_hints_scroll():
    stub = _ResolveStub("viewport")
    assert _resolve(stub) is None
    msg = str(stub._last_action_error)
    assert "视口" in msg and "scroll" in msg, f"viewport hint missing: {msg}"


def test_full_scope_hallucination_error_has_no_viewport_hint():
    stub = _ResolveStub("full")
    assert _resolve(stub) is None
    msg = str(stub._last_action_error)
    assert "目标可能在视口外" not in msg, f"full scope should not add viewport hint: {msg}"

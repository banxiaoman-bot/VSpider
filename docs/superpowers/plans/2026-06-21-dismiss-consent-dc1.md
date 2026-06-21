# dismiss_consent (DC-1) 实现计划

> **面向 AI 代理的工作者：** 用 `executing-plans` 技能逐任务实现此计划。每个任务 TDD（先红后绿）+ 单独 commit。设计稿见 `docs/superpowers/specs/2026-06-21-dismiss-consent-design.md`。

**目标：** 新增确定性能力 `dismiss_consent`——用 DOM/JS 确定性命中主流 CMP 的「接受全部」按钮并点击、校验弹层消失，解锁后续交互/抽取（替代当前 VLM 视觉处理，直击铁律#1 准确）。
**架构：** 对标 `next_page` 的分层 locator 范式。新增动作枚举 + `DismissConsentHandler`（L1 known-CMP 选择器 → L2 容器内多语肯定文本 → iframe 兜底 → 校验消失），幂等无副作用（无墙=干净 no-op）。本计划只做 DC-1 显式动作；DC-2 自动守卫单独切片。
**技术栈：** Python 3 / Playwright async / Pydantic / pytest（stub-frame，不起真浏览器）。

---

## 文件结构（创建/修改 + 职责）

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `visual_web_agent/vlm_client.py`（`VSpiderAction` 定义实为 `vlm_models.py`） | 改 | 动作枚举加 `"dismiss_consent"` |
| `visual_web_agent/actions/page_ops.py` | 改 | 新增 `DismissConsentHandler`（`@ActionRegistry.register("dismiss_consent")`），核心执行逻辑 |
| `visual_web_agent/actions/__init__.py` | 改 | 导出 `DismissConsentHandler`（import + `__all__`） |
| `visual_web_agent/action_registry.py` | 改 | `build_default_action_registry` 注册 `dismiss_consent` ActionTool 元数据 |
| `visual_web_agent/main.py` | 改 | `action_registry.bind("dismiss_consent", _browser_action_tool)` + `_registry_dispatch_actions` 集合加入 |
| `visual_web_agent/capability_router.py` | 改 | `_CONSENT_RE` + `_signals` 信号 + `_backend_plan` 的 `_add` 路由 |
| `visual_web_agent/prompt_skills.py` | 改 | `DISMISS_CONSENT_SKILL` 常量 + 技能字典登记；升级 :255 提示 |
| `tests/test_dismiss_consent.py` | 建 | stub-frame 回归（schema/注册/L1/L2/排除词/iframe/no-op/校验） |
| `docs/vspider_architecture_backlog.md` | 改 | 记一行 Slice DC-1 |

> **CRLF 警示**：`actions/page_ops.py` / `main.py` / `vlm_models.py` 是 CRLF。跨空行大块插入用 `_patch_dc1.py` 按字节读改写回、patch 完即删（工程规范）。`StrReplace` 单点小改可直接用。
> **提交纪律**：每个任务只 `git add` 本任务涉及文件，**禁用 `git add -A`**（分支上有并发会话的 R2/BBR 改动）。

---

## Task 1 · 动作枚举加 `dismiss_consent`

**文件：** `visual_web_agent/vlm_models.py`（枚举），`tests/test_dismiss_consent.py`（新建，先写 schema 测试）

**步骤：**

1. 新建 `tests/test_dismiss_consent.py`，写失败的 schema 测试：

```python
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from visual_web_agent.vlm_client import VSpiderAction


class TestSchema:
    def test_dismiss_consent_accepted(self) -> None:
        v = VSpiderAction(action="dismiss_consent", target_id=0, type_value="")
        assert v.action == "dismiss_consent"
```

2. 运行确认失败：`python -m pytest tests/test_dismiss_consent.py -q`（预期 `ValidationError`）。

3. 在 `vlm_models.py` 的 `action: Literal[...]` 列表（`open_top_search_result` 后、`] = Field(` 前）加一行：

```python
        "open_top_search_result", # 搜索结果页：确定性打开首个非广告有机结果并导航（落地二次广告校验 + 候选轮替）
        "dismiss_consent", # Cookie/同意墙确定性关闭：DOM/JS 命中主流 CMP「接受全部」按钮，校验弹层消失，解锁后续交互
```

4. 运行确认通过：`python -m pytest tests/test_dismiss_consent.py::TestSchema -q`（预期 1 passed）。

**验证：** `python -m pytest tests/test_dismiss_consent.py -q` → 1 passed。
**提交：** `git add visual_web_agent/vlm_models.py tests/test_dismiss_consent.py && git commit -m "feat(consent): add dismiss_consent action enum (DC-1 step1)"`

---

## Task 2 · `DismissConsentHandler` 骨架 + 注册 + 导出 + no-op

**文件：** `visual_web_agent/actions/page_ops.py`，`visual_web_agent/actions/__init__.py`，`tests/test_dismiss_consent.py`

**步骤：**

1. 在 `tests/test_dismiss_consent.py` 追加注册测试 + stub 脚手架 + no-op 测试（先红）：

```python
from visual_web_agent.actions import ActionRegistry, ActionExecutionError, DismissConsentHandler


def _make_browser_stub() -> SimpleNamespace:
    async def _no_op(*_, **__):
        return None
    return SimpleNamespace(_wait_after_action=_no_op, rpa_trail=[])


def _make_ctx(*, page: Any) -> SimpleNamespace:
    return SimpleNamespace(
        action=SimpleNamespace(action="dismiss_consent", target_id=0, type_value=""),
        browser=_make_browser_stub(),
        page=page,
        workflow_memory={},
        with_rpa_meta=lambda d: d,
    )


class _StubLocator:
    def __init__(self, *, click_raises: bool = False):
        self.click_raises = click_raises
        self.clicked = False
    @property
    def first(self):
        return self
    async def scroll_into_view_if_needed(self, **_):
        return None
    async def click(self, **_):
        if self.click_raises:
            raise RuntimeError("blocked")
        self.clicked = True
    async def evaluate(self, *_a, **_k):
        return None


class _StubScope:
    """Stands in for a Playwright Page or Frame."""
    def __init__(self, name: str, *, probe=None, still_visible=False, locator=None):
        self.url = f"https://example.test/{name}"
        self._probe = probe          # dict returned by the mark-consent probe
        self._still = still_visible   # bool returned by the still-visible probe
        self._locator = locator or _StubLocator()
        self.eval_calls = 0
    def locator(self, _sel: str):
        return self._locator
    async def evaluate(self, _script: str, *args, **_k):
        self.eval_calls += 1
        # First evaluate per scan = mark probe; subsequent = still-visible check.
        if self.eval_calls == 1:
            return self._probe if self._probe is not None else {"found": False}
        return self._still


class _StubPage:
    def __init__(self, scope: _StubScope, frames=None):
        self._scope = scope
        self.url = scope.url
        self.frames = frames if frames is not None else [scope]
        self.main_frame = self.frames[0] if self.frames else None
    def locator(self, sel: str):
        return self._scope.locator(sel)
    async def evaluate(self, script: str, *args, **k):
        return await self._scope.evaluate(script, *args, **k)


class TestRegistration:
    def test_handler_registered(self) -> None:
        assert ActionRegistry._handlers["dismiss_consent"] is DismissConsentHandler


class TestNoop:
    def test_clean_page_is_noop_success(self) -> None:
        scope = _StubScope("main", probe={"found": False})
        page = _StubPage(scope)
        ctx = _make_ctx(page=page)
        # Must NOT raise; clean no-op.
        asyncio.run(DismissConsentHandler().execute(ctx))
        trail = ctx.browser.rpa_trail
        assert trail and trail[-1]["action"] == "dismiss_consent"
        assert trail[-1]["dismissed"] is False
        assert trail[-1]["strategy"] == "noop"

    def test_missing_page_raises(self) -> None:
        ctx = _make_ctx(page=None)
        with pytest.raises(ActionExecutionError, match="无活动页面"):
            asyncio.run(DismissConsentHandler().execute(ctx))
```

2. 运行确认失败：`python -m pytest tests/test_dismiss_consent.py -q`（预期 ImportError：`DismissConsentHandler` 不存在）。

3. 在 `actions/page_ops.py` 末尾（`HoverAndClickHandler` 之后）新增 handler。**用 `_patch_dc1.py` 按字节追加**（CRLF）。完整代码：

```python
@ActionRegistry.register("dismiss_consent")
class DismissConsentHandler(ActionHandler):
    """Deterministically dismiss cookie/consent walls (CMP) by clicking the
    'Accept all' control so downstream interaction/extraction is unblocked.

    Layered like next_page: L1 known-CMP selectors -> L2 scoped multilingual
    affirmative text (reject/manage excluded) -> child-iframe fallback ->
    verify the overlay disappeared. Idempotent no-op when no wall is present.
    """

    _KNOWN_CMP_SELECTORS: ClassVar[list[str]] = [
        "#onetrust-accept-btn-handler",
        "#accept-recommended-btn-handler",
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "#CybotCookiebotDialogBodyButtonAccept",
        "#truste-consent-button",
        ".qc-cmp2-summary-buttons button[mode='primary']",
        "#didomi-notice-agree-button",
        "[data-testid='uc-accept-all-button']",
        "button#uc-btn-accept-banner",
        ".osano-cm-accept-all",
        ".cky-btn-accept",
        ".cmplz-accept",
        ".cc-allow",
        ".cm-btn-success",
        ".cm-btn-accept-all",
        "[data-tid='banner-accept']",
        "#BorlabsCookieBoxSaveButton",
        "a[data-cookie-accept-all]",
        ".sp_choice_type_11",
        "#wt-cli-accept-all-btn",
        ".wt-cli-accept-all-btn",
    ]
    _ACCEPT_TEXTS: ClassVar[list[str]] = [
        "accept all", "accept all cookies", "i accept", "i agree", "agree",
        "allow all", "got it", "accept", "ok",
        "接受全部", "全部接受", "同意", "同意全部", "允许全部", "我知道了", "同意并继续",
        "alle akzeptieren", "akzeptieren", "tout accepter", "accepter",
        "aceptar todo", "同意する", "すべて同意", "모두 동의",
    ]
    _REJECT_TEXTS: ClassVar[list[str]] = [
        "reject", "decline", "manage", "settings", "preferences", "customize",
        "only necessary", "拒绝", "管理", "设置", "仅必要", "自定义",
    ]
    _CONTAINER_SELECTORS: ClassVar[str] = (
        "[role='dialog'],[aria-modal='true'],"
        "[class*='cookie'],[class*='consent'],[class*='cmp'],"
        "[class*='gdpr'],[class*='privacy'],[id*='cookie'],[id*='consent']"
    )
    _MARK: ClassVar[str] = "data-vspider-consent-accept"

    def _iter_scopes(self, page: Any):
        scopes = [(page, getattr(page, "url", "") or "")]
        main_frame = getattr(page, "main_frame", None)
        for frame in list(getattr(page, "frames", None) or []):
            if frame is main_frame:
                continue
            is_detached = getattr(frame, "is_detached", None)
            if callable(is_detached) and is_detached():
                continue
            scopes.append((frame, str(getattr(frame, "url", "") or "")))
        return scopes

    async def execute(self, ctx: "ActionContext") -> "Optional[Page]":
        browser = ctx.browser
        page = ctx.page
        if not page:
            raise ActionExecutionError("dismiss_consent: 无活动页面。")

        result: dict[str, Any] = {
            "action": "dismiss_consent", "cmp": "none", "strategy": "noop",
            "selector": "", "frame_url": "", "dismissed": False, "scanned_frames": 0,
        }
        payload = [
            self._KNOWN_CMP_SELECTORS, self._ACCEPT_TEXTS,
            self._REJECT_TEXTS, self._CONTAINER_SELECTORS, self._MARK,
        ]

        for scope, frame_url in self._iter_scopes(page):
            result["scanned_frames"] += 1
            try:
                probe = await scope.evaluate(self._JS_MARK_CONSENT, payload)
            except Exception as probe_err:
                logger.debug("[DISMISS_CONSENT] probe failed (%s): %s", frame_url, probe_err)
                continue
            if not probe or not probe.get("found"):
                continue
            loc = scope.locator(f'[{self._MARK}="1"]').first
            label = (
                f"dismiss_consent {probe.get('strategy')} "
                f"{probe.get('selector') or probe.get('label')!r}"
            )
            try:
                await _click_locator_with_js_fallback(loc, label, timeout=3000)
            except Exception as click_err:
                logger.debug("[DISMISS_CONSENT] click failed for %s: %s", label, click_err)
                continue
            dismissed = await self._verify_gone(scope, payload)
            if not dismissed:
                try:
                    probe2 = await scope.evaluate(self._JS_MARK_CONSENT, payload)
                    if probe2 and probe2.get("found"):
                        loc2 = scope.locator(f'[{self._MARK}="1"]').first
                        await _click_locator_with_js_fallback(loc2, label + " retry", timeout=3000)
                        dismissed = await self._verify_gone(scope, payload)
                except Exception:
                    pass
            result.update(
                cmp=probe.get("cmp") or probe.get("strategy") or "text",
                strategy=probe.get("strategy") or "accept_text",
                selector=probe.get("selector") or "",
                frame_url=frame_url,
                dismissed=bool(dismissed),
            )
            if dismissed:
                break

        logger.info(
            "[DISMISS_CONSENT] strategy=%s cmp=%s dismissed=%s frames=%s",
            result["strategy"], result["cmp"], result["dismissed"], result["scanned_frames"],
        )
        browser.rpa_trail.append(ctx.with_rpa_meta(dict(result)))
        await browser._wait_after_action()
        return None

    async def _verify_gone(self, scope: Any, payload: list) -> bool:
        try:
            still = await scope.evaluate(self._JS_CONSENT_STILL_VISIBLE, payload)
        except Exception:
            return False
        return not bool(still)
```

4. 在同一 patch 里，于 handler 类体内补两段 JS 常量（`_JS_MARK_CONSENT` / `_JS_CONSENT_STILL_VISIBLE`），见 **Task 3**（先放最小可用版以让 Task 2 测试通过亦可，但建议 Task 2/3 合并 patch 一次成型）。Task 2 仅断言 no-op + 注册，stub 的 `evaluate` 已 mock，故 JS 内容不被执行——**Task 2 只需 JS 常量存在（语法占位真实 JS 字符串，非 TODO）**。

5. 在 `actions/__init__.py` 的 `from .page_ops import (...)` 加 `DismissConsentHandler`，并加入 `__all__`：

```python
from .page_ops import (
    ClickPointHandler, NextPageHandler,
    DismissConsentHandler,
    # ... existing ...
    RowActionHandler, ExtractRowHandler, TreeCheckHandler,
```
```python
    "ClickPointHandler", "NextPageHandler",
    "DismissConsentHandler",
    # ...
    "RowActionHandler", "ExtractRowHandler", "TreeCheckHandler",
```

6. 运行：`python -m pytest tests/test_dismiss_consent.py -q`（预期 TestSchema/TestRegistration/TestNoop 全绿）。

**验证：** `python -m pytest tests/test_dismiss_consent.py -q` → 3 类全 passed；`python -c "import visual_web_agent.actions as a; print(a.DismissConsentHandler.__name__)"` 打印类名。
**提交：** `git add visual_web_agent/actions/page_ops.py visual_web_agent/actions/__init__.py tests/test_dismiss_consent.py && git commit -m "feat(consent): DismissConsentHandler skeleton + registration + noop (DC-1 step2)"`

---

## Task 3 · 检测逻辑 JS 探针 + L1/L2/iframe/排除词/校验测试

**文件：** `visual_web_agent/actions/page_ops.py`（补两段 JS 常量），`tests/test_dismiss_consent.py`（补检测测试）

**步骤：**

1. 先在测试里补检测用例（先红——若 Task 2 已放空壳 JS，此处断言点击/排除/iframe 行为）：

```python
class TestDetection:
    def test_known_cmp_hit_clicks_and_dismisses(self) -> None:
        loc = _StubLocator()
        scope = _StubScope(
            "main",
            probe={"found": True, "strategy": "known_cmp",
                   "cmp": "OneTrust", "selector": "#onetrust-accept-btn-handler"},
            still_visible=False, locator=loc,
        )
        page = _StubPage(scope)
        ctx = _make_ctx(page=page)
        asyncio.run(DismissConsentHandler().execute(ctx))
        assert loc.clicked is True
        last = ctx.browser.rpa_trail[-1]
        assert last["strategy"] == "known_cmp"
        assert last["dismissed"] is True

    def test_accept_text_fallback(self) -> None:
        loc = _StubLocator()
        scope = _StubScope(
            "main",
            probe={"found": True, "strategy": "accept_text",
                   "cmp": "text", "label": "接受全部"},
            still_visible=False, locator=loc,
        )
        ctx = _make_ctx(page=_StubPage(scope))
        asyncio.run(DismissConsentHandler().execute(ctx))
        assert loc.clicked is True
        assert ctx.browser.rpa_trail[-1]["strategy"] == "accept_text"

    def test_reject_only_overlay_is_noop(self) -> None:
        # Probe (which applies the reject-exclusion in JS) reports not-found.
        loc = _StubLocator()
        scope = _StubScope("main", probe={"found": False}, locator=loc)
        ctx = _make_ctx(page=_StubPage(scope))
        asyncio.run(DismissConsentHandler().execute(ctx))
        assert loc.clicked is False
        assert ctx.browser.rpa_trail[-1]["strategy"] == "noop"

    def test_iframe_fallback(self) -> None:
        main = _StubScope("main", probe={"found": False})
        child_loc = _StubLocator()
        child = _StubScope(
            "cmp_frame",
            probe={"found": True, "strategy": "known_cmp",
                   "cmp": "TrustArc", "selector": "#truste-consent-button"},
            still_visible=False, locator=child_loc,
        )
        page = _StubPage(main, frames=[main, child])
        ctx = _make_ctx(page=page)
        asyncio.run(DismissConsentHandler().execute(ctx))
        assert child_loc.clicked is True
        assert ctx.browser.rpa_trail[-1]["frame_url"].endswith("cmp_frame")
        assert ctx.browser.rpa_trail[-1]["scanned_frames"] == 2

    def test_click_but_overlay_persists_marks_not_dismissed(self) -> None:
        loc = _StubLocator()
        scope = _StubScope(
            "main",
            probe={"found": True, "strategy": "known_cmp",
                   "cmp": "OneTrust", "selector": "#onetrust-accept-btn-handler"},
            still_visible=True, locator=loc,   # overlay stays visible after click
        )
        ctx = _make_ctx(page=_StubPage(scope))
        asyncio.run(DismissConsentHandler().execute(ctx))
        assert loc.clicked is True
        assert ctx.browser.rpa_trail[-1]["dismissed"] is False
```

> 注：`_StubScope.evaluate` 第 1 次返回 probe、之后返回 `still_visible`；retry 分支会再调 evaluate（第 3 次仍返回 `still`），与上面断言一致。若实现 retry 后多一次 mark 调用，调整 `_StubScope` 让 `eval_calls>=3` 仍返回 `self._still` 即可（已如此实现）。

2. 运行确认失败：`python -m pytest tests/test_dismiss_consent.py::TestDetection -q`。

3. 在 `DismissConsentHandler` 类体补两段真实 JS 常量（`payload = [known, accept, reject, container, mark]`）：

```python
    _JS_MARK_CONSENT: ClassVar[str] = r"""
([known, accept, reject, container, MARK]) => {
  document.querySelectorAll(`[${MARK}]`).forEach(el => el.removeAttribute(MARK));
  const vw = window.innerWidth || document.documentElement.clientWidth || 0;
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const st = window.getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden' && Number(st.opacity || 1) > 0.01;
  };
  const enabled = (el) => !(el.disabled || el.getAttribute('aria-disabled') === 'true');
  const mark = (el, extra) => {
    el.setAttribute(MARK, '1');
    try { el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'}); } catch (_) {}
    return Object.assign({found: true}, extra);
  };
  // L1: known CMP selectors (most precise)
  for (const sel of known) {
    let el;
    try { el = document.querySelector(sel); } catch (_) { continue; }
    if (el && visible(el) && enabled(el)) {
      return mark(el, {strategy: 'known_cmp', cmp: 'cmp', selector: sel});
    }
  }
  // L2: affirmative text scoped to consent containers, reject excluded
  let roots = [];
  try { roots = Array.from(document.querySelectorAll(container)).filter(visible); } catch (_) { roots = []; }
  const textOf = (el) => norm([el.innerText, el.textContent, el.getAttribute('aria-label'), el.value].filter(Boolean).join(' '));
  for (const root of roots) {
    const nodes = Array.from(root.querySelectorAll("a,button,[role='button'],input[type='button'],input[type='submit']")).filter(visible).filter(enabled);
    for (const el of nodes) {
      const t = textOf(el);
      if (!t || t.length > 40) continue;
      if (reject.some(w => t.includes(w))) continue;
      if (accept.some(w => t === w || t === w + ' cookies' || t === 'allow ' + w)) {
        return mark(el, {strategy: 'accept_text', cmp: 'text', label: t});
      }
    }
    // looser: whole-word affirmative inside a consent container
    for (const el of nodes) {
      const t = textOf(el);
      if (!t || t.length > 40) continue;
      if (reject.some(w => t.includes(w))) continue;
      if (accept.some(w => t.split(/\s+/).join(' ') === w)) {
        return mark(el, {strategy: 'accept_text', cmp: 'text', label: t});
      }
    }
  }
  return {found: false, reason: 'no-consent-control'};
}
"""

    _JS_CONSENT_STILL_VISIBLE: ClassVar[str] = r"""
([known, accept, reject, container, MARK]) => {
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) <= 0.01) return false;
    return true;
  };
  for (const sel of known) {
    let el;
    try { el = document.querySelector(sel); } catch (_) { continue; }
    if (el && visible(el)) return true;
  }
  let roots = [];
  try { roots = Array.from(document.querySelectorAll(container)); } catch (_) { roots = []; }
  for (const root of roots) {
    if (!visible(root)) continue;
    const st = window.getComputedStyle(root);
    const fixedish = st.position === 'fixed' || st.position === 'sticky' || Number(st.zIndex || 0) >= 1000;
    if (fixedish) return true;
  }
  return false;
}
"""
```

4. 运行确认通过：`python -m pytest tests/test_dismiss_consent.py -q`（预期全绿）；`python -m py_compile visual_web_agent/actions/page_ops.py`。

**验证：** `python -m pytest tests/test_dismiss_consent.py -q` → 全 passed（schema+注册+noop+detection 共约 10 例）。
**提交：** `git add visual_web_agent/actions/page_ops.py tests/test_dismiss_consent.py && git commit -m "feat(consent): consent detection probe (known-CMP + scoped accept-text + iframe + verify) (DC-1 step3)"`

---

## Task 4 · ActionTool 元数据注册

**文件：** `visual_web_agent/action_registry.py`，`tests/test_dismiss_consent.py`

**步骤：**

1. 测试补（先红）：

```python
class TestActionTool:
    def test_tool_registered_with_evidence(self) -> None:
        from visual_web_agent.action_registry import build_default_action_registry
        reg = build_default_action_registry()
        tool = reg.get("dismiss_consent")
        assert tool is not None
        assert "consent_dismissed.v1" in tool.evidence
        assert tool.risk == "low"
```

> 若 `ActionRegistry` 取工具的方法名不是 `get`，按 `action_registry.py` 实际 API 调整（grep `def get` / `def lookup` / `_tools`）。

2. 运行确认失败。

3. 在 `build_default_action_registry`（紧接 `open_top_search_result` 注册块后）加：

```python
    register(ActionTool(
        name="dismiss_consent",
        capability="browser_actions",
        description="Deterministically dismiss cookie/consent walls (CMP) by clicking 'Accept all', verified by overlay disappearance; idempotent no-op when absent.",
        actions=("dismiss_consent",),
        aliases=(
            "cookie", "cookies", "consent", "accept all", "accept cookies",
            "gdpr", "cookie banner", "同意", "接受全部", "全部接受",
            "我知道了", "cookie 横幅", "隐私弹窗", "同意墙",
        ),
        tags=("overlay", "consent", "cookie", "unblock"),
        evidence=("consent_dismissed.v1",),
        deterministic=True,
        changes_state=True,
        risk="low",
    ))
```

4. 运行确认通过。

**验证：** `python -m pytest tests/test_dismiss_consent.py::TestActionTool -q` → passed。
**提交：** `git add visual_web_agent/action_registry.py tests/test_dismiss_consent.py && git commit -m "feat(consent): register dismiss_consent ActionTool metadata (DC-1 step4)"`

---

## Task 5 · main.py 绑定 + 派发集合

**文件：** `visual_web_agent/main.py`

**步骤（StrReplace 两处，每处都是绑定段精确匹配，避免碰并发会话改动）：**

1. 绑定段（@966 `next_page` 之后）加：

```python
    action_registry.bind("next_page", _browser_action_tool)
    action_registry.bind("dismiss_consent", _browser_action_tool)
    action_registry.bind("targeted_probe", _targeted_probe_tool)
```

2. 派发集合（@968）加入：

```python
    _registry_dispatch_actions = {"hover_and_click", "next_page", "dismiss_consent", "targeted_probe"}
```

3. 验证导入与编译：`python -m py_compile visual_web_agent/main.py`。

**验证：** `python -m py_compile visual_web_agent/main.py` exit 0；`python -m pytest tests/test_dismiss_consent.py -q` 仍全绿。
**提交：** `git add visual_web_agent/main.py && git commit -m "feat(consent): wire dismiss_consent into registry dispatch (DC-1 step5)"`

---

## Task 6 · capability_router 路由

**文件：** `visual_web_agent/capability_router.py`，`tests/test_dismiss_consent.py`

**步骤：**

1. 测试补（先红）：

```python
class TestRouter:
    def test_consent_goal_routes_dismiss_consent(self) -> None:
        from visual_web_agent.capability_router import route_capabilities
        plan = route_capabilities("打开 https://example.com 关闭 cookie 同意弹窗后抓取列表")
        names = " ".join(str(plan)).lower()
        assert "dismiss_consent" in names
```

> 路由入口函数名以 `capability_router.py` 实际为准（grep `def route` / `def plan`）。若返回结构非 list[dict]，按实际断言。

2. 运行确认失败。

3. 加正则常量（`_AUTH_RE` 附近）：

```python
_CONSENT_RE = re.compile(
    r"\b(cookie\s*(banner|consent|notice|wall)?|consent|gdpr|ccpa|onetrust|cookiebot|trustarc|didomi|usercentrics|quantcast)\b"
    r"|cookie\s*弹窗|同意墙|隐私弹窗|接受全部\s*cookie|关闭\s*cookie|cookie\s*横幅|同意\s*cookie",
    re.I,
)
```

4. `_signals()` 加：

```python
    consent = bool(_CONSENT_RE.search(text))
```
并在返回 dict 加 `"consent_preferred": consent,`。

5. `_backend_plan()` 加（放 `search_nav_preferred` 块附近）：

```python
    if signals.get("consent_preferred"):
        _add(plan, "dismiss_consent", "browser_actions", "DC-1", ["ActionRegistry: dismiss_consent"], "When a cookie/GDPR consent wall blocks the page, deterministically click the CMP 'Accept all' control (OneTrust/Cookiebot/TrustArc/... + scoped multilingual accept text, reject excluded, iframe fallback, verified by overlay disappearance) before any extraction, instead of asking the VLM to visually find and click it.", "deterministic_router")
```

6. 运行确认通过。

**验证：** `python -m pytest tests/test_dismiss_consent.py::TestRouter -q` → passed。
**提交：** `git add visual_web_agent/capability_router.py tests/test_dismiss_consent.py && git commit -m "feat(consent): route consent goals to dismiss_consent (DC-1 step6)"`

---

## Task 7 · prompt_skills 技能区块 + :255 升级

**文件：** `visual_web_agent/prompt_skills.py`，`tests/test_dismiss_consent.py`

**步骤：**

1. 测试补（先红）：

```python
class TestSkill:
    def test_skill_documents_action_and_safety(self) -> None:
        from visual_web_agent.prompt_skills import DISMISS_CONSENT_SKILL
        assert "dismiss_consent" in DISMISS_CONSENT_SKILL
        # default accept + reject exclusion mentioned
        assert "接受全部" in DISMISS_CONSENT_SKILL
        assert "拒绝" in DISMISS_CONSENT_SKILL
```

2. 运行确认失败。

3. 加常量（与其它 `*_SKILL` 并列，如 `TOOLTIP_SKILL` 附近）：

```python
DISMISS_CONSENT_SKILL = """
🍪 Cookie / 同意墙（GDPR 同意弹窗）：
- 看到 cookie 横幅 / 隐私同意墙 / "Accept all cookies" / "我们重视您的隐私" 等遮挡时，
  **优先输出确定性动作 `dismiss_consent`**（target_id=0, type_value=""），不要用 click_point 视觉点击。
- 它会自动命中主流 CMP（OneTrust/Cookiebot/TrustArc/Quantcast/Didomi/Usercentrics…）的
  「接受全部」按钮，默认**接受全部**以解锁内容；绝不点"拒绝/管理/设置"。
- 无同意墙时是干净 no-op，不会报错。点完会校验弹层消失。
- 同意墙挡住列表/表单/抽取时，先 dismiss_consent，再继续原任务。
"""
```

4. 把该常量登记进技能字典（line 1250 附近的 `{...}`）：

```python
    "dismiss_consent": DISMISS_CONSENT_SKILL,
```

5. 升级旧提示（:255 那条「extract 前若有广告、cookie 横幅、登录弹窗等遮挡，先 Escape/click 关闭或 remove_element」）改为：

```python
4. extract 前若有 cookie 同意墙优先 `dismiss_consent`（确定性）；其它广告/登录弹窗遮挡再用 Escape/click/remove_element。
```

6. 运行确认通过。

**验证：** `python -m pytest tests/test_dismiss_consent.py::TestSkill -q` → passed。
**提交：** `git add visual_web_agent/prompt_skills.py tests/test_dismiss_consent.py && git commit -m "feat(consent): add dismiss_consent skill block + upgrade overlay hint (DC-1 step7)"`

---

## Task 8 · 全量收口 + agent_case 回归 + backlog

**文件：** `docs/vspider_architecture_backlog.md`（+ 1 条 agent_case，位置以 `tests/` 中既有 agent_case 目录为准）

**步骤：**

1. 加 1 条 agent_case 回归（按既有 agent_case 格式，goal 含 cookie 同意墙、断言 route/选用 `dismiss_consent`；若无真实站点，断言路由 + 工具选取层即可，不跑浏览器）。grep 既有样例：`tests` 中 `agent_case` / `universal_benchmark` 用法。
2. backlog 追加：

```markdown
## Slice DC-1 (M1 准确 + M3 通用): Cookie/同意墙确定性关闭 dismiss_consent (done, P1)

- 能力名: dismiss_consent（DOM/JS 命中主流 CMP「接受全部」+ 容器内多语肯定文本兜底 + iframe + 校验消失；契约 consent_dismissed.v1）。替代既有「VLM 视觉提示先 Escape/click 关闭」，直击铁律#1 准确。
- 影响层: model_plane(vlm_models 枚举) + operations_plane(action_registry/capability_router) + execution_kernel(actions/page_ops handler + main.py 绑定) + prompts(prompt_skills)。
- 设计/计划: docs/superpowers/specs/2026-06-21-dismiss-consent-design.md / docs/superpowers/plans/2026-06-21-dismiss-consent-dc1.md。
- 触发: DC-1 仅显式动作（VLM/路由触发）；DC-2 自动前置守卫后续单独切片。
- 新增 contract 字段: consent_dismissed.v1（cmp/strategy/selector/frame_url/dismissed/scanned_frames）。
- Tests: tests/test_dismiss_consent.py（schema/注册/L1 known-CMP/L2 accept-text/排除词 no-op/iframe/校验消失/无墙 no-op）+ 1 条 agent_case 路由回归。
- 风险: 低（默认不自动触发，纯新增动作；L2 容器作用域+整词+排除词三重防误点）。
```

3. 全量收口：`python scripts/validate_y.py DC-1 --target-test tests/test_dismiss_consent.py`。
   - 预期：定向测试全绿 → build（前端，归并发，可 `--skip-build` 若纯后端）→ 核心 pytest → 全量 pytest **0 新增失败**（用 `git stash` 仅本切片文件验证既有 UI 失败为 HEAD 既有）。

**验证：** `validate_y.py` 全链路绿（或全量 pytest 仅剩既有无关失败）。
**提交：** `git add docs/vspider_architecture_backlog.md tests/<agent_case 文件> && git commit -m "test(consent): agent_case route regression + backlog DC-1 (DC-1 step8)"`

---

## 自检（writing-plans）

**1. 规格覆盖度**（对照 spec 各节）：
- §3 触发模型(DC-1 显式) → Task 1/5（枚举+派发）✓
- §4 L1 known-CMP → Task 3 `_JS_MARK_CONSENT` 的 known 分支 ✓
- §4 L2 多语文本+排除词 → Task 3 accept/reject 分支 + `TestDetection.test_reject_only_overlay_is_noop` ✓
- §4 iframe → `_iter_scopes` + `TestDetection.test_iframe_fallback` ✓
- §4 证据/校验消失 → `_verify_gone` + `_JS_CONSENT_STILL_VISIBLE` + `test_click_but_overlay_persists` ✓
- §4 幂等 no-op → `TestNoop` ✓
- §5 contract consent_dismissed.v1 → Task 4 evidence + rpa_trail result 字段 ✓
- §6 六步映射 → Task 1~8 一一对应 ✓
- §8 测试计划 6 例 → Task 2/3 测试 ✓

**2. 占位符扫描：** 无 TODO/待定；每个代码步骤含真实代码块。两处「以实际 API 为准」（`reg.get` / `route_capabilities` 名）已标注 grep 兜底——执行时 1 行确认，非占位。

**3. 类型一致性：** `payload`（5 元 list）在 `_JS_MARK_CONSENT` / `_JS_CONSENT_STILL_VISIBLE` / `execute` / `_verify_gone` 全一致；`_MARK` 常量统一；probe dict 字段（found/strategy/cmp/selector/label）在 handler 与 stub 测试一致；`result` 字段（action/cmp/strategy/selector/frame_url/dismissed/scanned_frames）与 Task 4 evidence + 断言一致。

**待执行时确认的 2 个外部 API 名（非阻塞）：** `ActionRegistry` 取工具方法（`get`/`lookup`）、`capability_router` 路由入口（`route_capabilities`/`plan_*`）。执行 Task 4/6 前各 grep 1 次即可。

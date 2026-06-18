# main.py 解构（run_agent 拆分）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。每个 slice 实施前先按 TDD 写失败测试，完成后走 `python scripts/validate_y.py <slice_id> --target-test tests/test_<file>.py` 收口，并在 `docs/vspider_architecture_backlog.md` 记一行 done slice。

**目标：** 把 `visual_web_agent/main.py` 中长达 ~10780 行的 `run_agent` 单函数，按"抽取子系统 → 动作分发迁注册表 → 起手闭包"三阶段切到独立模块，主文件回落到可维护体量，全程纯平移零行为改动。
**架构：** 先用 `ExtractState` 数据类归集 ~20 个共享可变计数器（破解闭包/主循环双向耦合），再把 43 个 extraction 闭包搬进 `extraction_engine/runtime.py` 的 `ExtractRuntime` 方法；随后把 38 路内联 `action==` 分发迁到 `actions/<cap>.py` handler，主循环收敛为 `dispatcher.dispatch(action)`；最后把起手 handoff 闭包抽到 `phases/setup.py`。
**技术栈：** Python 3.13 / Playwright / pytest stub-frame 范式（参考 `tests/test_extract_row_and_tree_check.py::_StubLocator`）。

---

## 现状证据（实测，2026-06-18）

| 度量 | 值 | 来源 |
|---|---|---|
| `main.py` 总行数 | 11278 | `Measure-Object -Line` |
| `run_agent` 行范围 | 719 → 11498（~10780 行） | 顶层 `def` 扫描 |
| 主 `for step` 循环 | 4884 → ~11265（~6380 行） | grep `for step in range` |
| extraction 闭包家族 | 2336 → ~4400（~2100 行，43 个闭包） | grep `^        (async def\|def) ` |
| 起手/handoff 闭包 | 924 → ~2335（~1400 行） | grep 同上 |
| 已外迁包 | `phases/`(~8000 行) `actions/`(~7600 行) `form_engine/` `extraction_engine/` | 目录实测 |

**双向耦合证据（关键）：** extraction 闭包与主循环共享 ~20 个可变 run 级计数器，二者都读写。证据见 `main.py:2284-2334`：

```
_extract_count, _pagination_probed, _pagination_kind, _pagination_hint_msg,
_first_flip_pending, _page_is_infinite_scroll, _force_next_page_pending,
_force_extract_after_navigation_pending, _block_next_page_until_drained,
_block_next_page_reason, _first_extract_ever_done, _total_extracted_rows,
_extract_null_streak, _extract_null_total_resets, _extracted_page_urls,
_extracted_page_keys, _seen_extract_row_keys, _tooltip_trigger_keys,
_pagination_exhausted
```

因此 extraction 不能直接平移——必须先把这组计数器收进一个共享对象，闭包与循环都改引用它，才能把闭包搬走而不断开状态。

**闭包对依赖的捕获（构造注入清单）：** `browser`、`event_stream`、`logger`、`goal`、`_requested_output_fields`、`_data_controller`、`_parse_goal_target_count`、`_normalize_extracted_row_fields`（自身也在闭包内，随 S1c 一起搬）。

---

## 文件结构（将要创建/修改）

### 新模块

| 路径 | 职责 |
|---|---|
| `visual_web_agent/extraction_engine/runtime.py` | `ExtractState`（共享可变计数器）+ `ExtractDeps`（依赖句柄）+ `ExtractRuntime`（43 个抽取方法） |
| `visual_web_agent/actions/dispatch_table.py` | 主循环动作分发表：把 38 路 `action==` 收敛为注册表查表派发 |
| `visual_web_agent/phases/setup.py` | 起手 handoff 闭包（`_browser_action_tool` / `_targeted_probe_tool` / handoff / `_check_stop` / `_recover_active_page`） |

### 修改文件

| 路径 | 变更 |
|---|---|
| `visual_web_agent/main.py` | S1a 计数器归集 → S1b-S1d 闭包委托 → S2 分发委托 → S3 起手委托；最终仅剩 run_agent 骨架 + CLI |
| `visual_web_agent/action_registry.py` | S2：补齐被迁动作的 `ActionTool` 注册（aliases/tags/evidence） |
| `docs/vspider_architecture_backlog.md` | 每个 slice 记一行 done |

---

## Slice 清单

### S1a：引入 ExtractState 归集共享计数器（准备）

- **文件：** `visual_web_agent/extraction_engine/runtime.py`（新建，仅 ExtractState）、`visual_web_agent/main.py`、`tests/test_extract_runtime.py`（新建）
- **步骤：**
  1. 写失败测试：`ExtractState()` 默认值正确，且为可变共享对象（同一实例两处引用互见）。

```python
# tests/test_extract_runtime.py
from visual_web_agent.extraction_engine.runtime import ExtractState

def test_extract_state_defaults_and_shared_mutation():
    s = ExtractState()
    assert s.total_extracted_rows == 0
    assert s.pagination_kind == ""
    assert s.seen_extract_row_keys == set()
    alias = s
    alias.total_extracted_rows += 3
    alias.seen_extract_row_keys.add("k")
    assert s.total_extracted_rows == 3
    assert "k" in s.seen_extract_row_keys
```

  2. 在 `runtime.py` 定义 `ExtractState`（字段一一对应 `main.py:2284-2334` 的 19 个计数器，去掉前导下划线）：

```python
# visual_web_agent/extraction_engine/runtime.py
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ExtractState:
    """Run 级抽取计数器；主循环与 ExtractRuntime 共享同一实例。"""
    extract_count: int = 0
    pagination_probed: bool = False
    pagination_kind: str = ""
    pagination_hint_msg: str = ""
    first_flip_pending: bool = False
    page_is_infinite_scroll: bool = False
    force_next_page_pending: bool = False
    force_extract_after_navigation_pending: bool = False
    block_next_page_until_drained: bool = False
    block_next_page_reason: str = ""
    first_extract_ever_done: bool = False
    total_extracted_rows: int = 0
    extract_null_streak: int = 0
    extract_null_total_resets: int = 0
    pagination_exhausted: bool = False
    extracted_page_urls: set = field(default_factory=set)
    extracted_page_keys: set = field(default_factory=set)
    seen_extract_row_keys: set = field(default_factory=set)
    tooltip_trigger_keys: set = field(default_factory=set)
```

  3. 在 `main.py:2284-2334` 用 `_xs = ExtractState()` 替换 19 处局部初始化（保留注释）。
  4. 用 `_patch_extract_state.py` 字节级 patch：把 run_agent 全函数体内对这 19 个 `_xxx` 标识符的读写改写为 `_xs.xxx`（CRLF 规范，patch 完即删）。改写需覆盖闭包段（2336-4400）与主循环段（4884-11265）全部引用点。
- **验证：** `python scripts/validate_y.py S1a --target-test tests/test_extract_runtime.py`（定向 + 全量 pytest 0 新增失败）
- **提交：** `refactor(main): introduce ExtractState to group extraction counters — no behavior change`
- **新增 contract 字段：** 无。

### S1b：DOM 抽取读闭包 → ExtractRuntime 方法（拆分）

- **文件：** `visual_web_agent/extraction_engine/runtime.py`、`visual_web_agent/main.py`、`tests/test_extract_runtime.py`
- **步骤：**
  1. 写失败测试：用 stub frame 断言 `ExtractRuntime.extract_list_rows_via_dom(reason)` 返回 `(rows, source)`，且不依赖 run_agent 闭包。

```python
# tests/test_extract_runtime.py (追加)
import asyncio
from visual_web_agent.extraction_engine.runtime import (
    ExtractState, ExtractDeps, ExtractRuntime,
)

class _StubFrame:
    async def evaluate(self, js, *a):  # DOM 抽取 JS 返回固定行
        return [{"title": "A", "url": "/a"}, {"title": "B", "url": "/b"}]

class _StubBrowser:
    async def _ensure_active_page(self, reason=""):
        return _StubFrame()

def _mk_runtime():
    deps = ExtractDeps(
        browser=_StubBrowser(), event_stream=None, logger=__import__("logging").getLogger("t"),
        goal="抓 10 条", requested_output_fields=["title", "url"], data_controller=None,
        parse_goal_target_count=lambda g: 10,
    )
    return ExtractRuntime(deps, ExtractState())

def test_extract_list_rows_via_dom_pure():
    rt = _mk_runtime()
    rows, source = asyncio.run(rt.extract_list_rows_via_dom("test"))
    assert len(rows) == 2 and rows[0]["title"] == "A"
```

  2. 在 `runtime.py` 定义 `ExtractDeps` + `ExtractRuntime` 骨架：

```python
# visual_web_agent/extraction_engine/runtime.py (追加)
from typing import Any, Callable, Optional


@dataclass
class ExtractDeps:
    browser: Any
    event_stream: Any
    logger: Any
    goal: str
    requested_output_fields: list
    data_controller: Any
    parse_goal_target_count: Callable[[str], Optional[int]]


class ExtractRuntime:
    def __init__(self, deps: ExtractDeps, state: ExtractState) -> None:
        self.deps = deps
        self.state = state
```

  3. 把以下 6 个"读取型"DOM 闭包平移为 `ExtractRuntime` 方法（`browser`→`self.deps.browser`，`_xs.xxx`→`self.state.xxx`，内联 JS 整段照搬）：`_extract_list_rows_via_dom`(3514)、`_extract_visible_table_rows_via_dom`(3729)、`_visible_table_signature`(3888)、`_auto_advance_table_page_via_dom`(3930)、`_probe_scroll_drain_state`(4156)、`_detect_canvas_grid`(4212)。
  4. `main.py` 在闭包定义处构造 `_extract_rt = ExtractRuntime(_extract_deps, _xs)`，原闭包调用点改为 `_extract_rt.<method>(...)`（用 `_patch_extract_runtime_b.py` 字节级 patch）。
- **验证：** `python scripts/validate_y.py S1b --target-test tests/test_extract_runtime.py`
- **提交：** `refactor(main): move DOM extraction readers into ExtractRuntime — pure move`
- **新增 contract 字段：** 无。

### S1c：候选仲裁闭包 → ExtractRuntime 方法（拆分）

- **文件：** 同 S1b
- **步骤：**
  1. 写失败测试：`ExtractRuntime.choose_best_candidate([...])` 在多候选中按既有优先级选中正确项；`sanitize_candidate(...)` 丢弃欠完整行。（断言与拆分前 `_choose_best_extraction_candidate` / `_sanitize_extraction_candidate` 行为一致——先在拆分前对同输入录基线。）
  2. 平移仲裁/归一族闭包为方法：`_sanitize_extraction_candidate`(2336)、`_expected_rows_from_data_shape`(2430)、`_candidate_min_expected_rows`(2453)、`_is_under_yield_viewport_candidate`(2473)、`_choose_best_extraction_candidate`(2485)、`_commit_extraction_candidate`(2562)、`_record_extract_progress`(2572)、`_normalize_extracted_row_fields`(3093) 及其依赖的字段工具闭包（`_field_aliases` 2964 / `_requested_field_coverage` 2998 / `_project_row_to_requested_fields` 3030 等）。
  3. `_data_controller` / `_requested_output_fields` / `_parse_goal_target_count` 经 `self.deps` 访问；`_total_extracted_rows` 等经 `self.state` 访问。
  4. `main.py` 调用点改 `_extract_rt.<method>(...)`（字节级 patch）。
- **验证：** `python scripts/validate_y.py S1c --target-test tests/test_extract_runtime.py`
- **提交：** `refactor(main): move extraction candidate arbiter into ExtractRuntime — pure move`
- **新增 contract 字段：** 无。

### S1d：快路径/收尾闭包 → ExtractRuntime 方法（拆分）

- **文件：** 同 S1b
- **步骤：**
  1. 写失败测试：`ExtractRuntime.try_pre_extract_fast_path()` 在 stub DOM-API 命中时返回 `True` 且写入 state.total_extracted_rows；`finish_if_xhr_target_reached(reason)` 达量返回 `True`。
  2. 平移：`_try_dom_api_fast_path`(2654)、`_xhr_saved_row_count`(2741)、`_xhr_target_reached`(2772)、`_finish_if_xhr_target_reached`(2795)、`_finish_if_file_download_completed`(2852)、`_save_extraction_snapshot`(2603)、`_capture_body_text_excerpt`(2590)、`_enrich_rows_with_dom_links`(3160)、`_extract_compact_list_text_via_dom`(3266)、`_extract_full_page_text_for_data`(3392)、`_extract_body_text_for_semantic_cards`(3443)、`_inspect_click_target_for_extract_nav_guard`(3457)、`_nudge_scroll_after_duplicate_extract`(4113)、`_try_pre_extract_fast_path`(4274)。
  3. 调用点改 `_extract_rt.<method>(...)`（字节级 patch）。
- **验证：** `python scripts/validate_y.py S1d --target-test tests/test_extract_runtime.py`
- **提交：** `refactor(main): move extraction fast-path & finalizers into ExtractRuntime — pure move`
- **新增 contract 字段：** 无。

### S1e：抽取子系统收口（收口）

- **文件：** `tests/test_file_size_baseline.py`、`docs/vspider_architecture_backlog.md`
- **步骤：**
  1. 更新基线断言：`main.py < 9500 行`（S1 预计减 ~2000 行）；`extraction_engine/runtime.py` 存在且含 `ExtractRuntime`。
  2. 全量 pytest 0 新增失败。
  3. backlog 记 S1a-S1e done。
- **验证：** `python scripts/validate_y.py S1e --target-test tests/test_file_size_baseline.py`
- **提交：** `test: extraction subsystem extraction baseline + backlog closeout`

### S2：主循环动作分发迁注册表（拆分，分 5-6 子片）

- **文件：** `visual_web_agent/actions/dispatch_table.py`（新建）、`visual_web_agent/action_registry.py`、对应 `actions/<cap>.py`、`visual_web_agent/main.py`、`tests/test_dispatch_table.py`
- **背景：** 主循环 4884-11265 内 38 路 `action==` 分支。逐组迁移，每子片只迁 5-8 个动作，避免 omnibus。
- **通用步骤（每子片复用）：**
  1. 写失败测试：`dispatch_action(action_dict, ctx)` 对该组动作返回与原内联分支一致的 `ActionResult`（先对原分支录 stub-frame 基线）。
  2. 把该组内联分支体平移到对应 `actions/<cap>.py` handler（沿用 row_action v2 模板：iframe → selector chain → 最短文本去歧义 → `_click_locator_with_js_fallback` → `browser._tab_switch_notice` → `browser.rpa_trail.append`）。
  3. 在 `action_registry.py::build_default_action_registry` 补 `ActionTool` 注册（aliases/tags/evidence）。
  4. `dispatch_table.py` 查注册表派发；`main.py` 该组分支改为 `result = dispatch_action(action, ctx)`（字节级 patch）。
- **子片划分（按动作族）：**
  - S2a：导航族（goto_url / switch_tab / close_tab / next_page / click_new_tab）
  - S2b：点击族（click / click_text / click_point / hover_and_click）
  - S2c：输入族（type / select_option / press_key / upload）
  - S2d：抽取/数据族（extract / save_to_memory / download_image / find_text）
  - S2e：控制族（wait_action / ask_human / done_signal / scroll_page）
  - S2f：收口——基线 `main.py < 6000 行` + backlog。
- **验证（每子片）：** `python scripts/validate_y.py S2x --target-test tests/test_dispatch_table.py`
- **提交（每子片）：** `refactor(main): migrate <族> dispatch to registry — pure move`
- **新增 contract 字段：** 无（沿用 `ActionResult.v1`）。

### S3：起手 handoff 闭包 → phases/setup.py（拆分）

- **文件：** `visual_web_agent/phases/setup.py`（新建）、`visual_web_agent/main.py`、`tests/test_phase_setup_split.py`
- **步骤：**
  1. 写失败测试：`SetupTools(browser, ...)` 暴露 `browser_action_tool` / `targeted_probe_tool` / `try_targeted_click_text_handoff` / `try_targeted_type_handoff` / `check_stop` / `recover_active_page`，stub browser 下行为与拆分前一致。
  2. 平移闭包：`_browser_action_tool`(924)、`_targeted_probe_tool`(927)、`_try_targeted_click_text_handoff`(997)、`_resolve_type_value_for_handoff`(1073)、`_try_targeted_type_handoff`(1089)、`_check_stop`(1278)、`_abort_if_stale_auth`(1282)、`_with_tool_metadata`(1293)、`_recover_active_page`(1323) 到 `SetupTools` 方法。
  3. `main.py` 构造 `_setup = SetupTools(...)`，调用点委托（字节级 patch）。
- **验证：** `python scripts/validate_y.py S3 --target-test tests/test_phase_setup_split.py`
- **提交：** `refactor(main): extract setup/handoff tools into phases/setup — pure move`
- **新增 contract 字段：** 无。

### B：全局体量基线收口（收口）

- **文件：** `tests/test_file_size_baseline.py`、`docs/vspider_architecture_backlog.md`
- **步骤：**
  1. 终态基线断言：`main.py < 5500 行`（run_agent 骨架 + CLI），`extraction_engine/runtime.py` / `actions/dispatch_table.py` / `phases/setup.py` 三新模块存在。
  2. 全量 pytest 0 新增失败。
  3. backlog 记本计划收口。
- **验证：** 全量 pytest。
- **提交：** `test: main.py decomposition final baseline + backlog closeout`

---

## 依赖与实施顺序

```
S1a (ExtractState 归集)
  ↓
S1b (DOM 读) → S1c (候选仲裁) → S1d (快路径/收尾) → S1e (S1 收口)
  ↓
S2a → S2b → S2c → S2d → S2e → S2f (S2 收口)
  ↓
S3 (起手闭包) → B (终态收口)
```

- S1a **必须最先**（后续 S1b-S1d 依赖 ExtractState 已就位）。
- S1b/S1c/S1d **严格串行**（同改 runtime.py + main.py 同区段，避免 patch 冲突）。
- S2 在 S1 之后（循环体引用的 extraction 已收敛，分发迁移面更小）。
- S3 与 S2 **可并行**（不同代码区段：起手段 vs 循环体）。
- B 最后。

## 预估里程碑体量

完成后 `main.py` 预计从 11278 行降至 ~5000-5500 行（run_agent 骨架 + CLI + 少量辅助）；新增 `extraction_engine/runtime.py` ~2200 行、`actions/dispatch_table.py` + 各 handler 增量、`phases/setup.py` ~1400 行。所有 slice 为纯平移零行为改动。

## 风险

1. **CRLF 字节级 patch**：每个 slice 都需写 `_patch_*.py` 脚本按字节搬移，patch 完即删（VSpider 工程规范强制）。
2. **共享可变状态**：S1a 的 ExtractState 改写面横跨闭包段 + 主循环段（~6000 行内的标识符引用），必须穷举所有读写点，漏改即静默状态丢失——以全量 pytest + 抽取回归兜底。
3. **闭包捕获遗漏**：部分 extraction 闭包可能还捕获了未在依赖清单中的临时局部；S1b-S1d 平移时若发现，追加进 `ExtractDeps` 而非回退内联。
4. **动作分发行为漂移**：S2 每子片必须先对原内联分支录 stub-frame 基线，再平移，确保 `ActionResult` 字段逐一对齐。

## 自检

- [x] 规格覆盖：①extraction(S1) ②动作分发(S2) ③起手闭包(S3) 三大巨块全部有 slice；体量基线 S1e/S2f/B 三道防线。
- [x] 占位符扫描：每个 slice 含文件/步骤（含真实接口代码与精确行号）/验证命令/提交；无 TODO/待定。
- [x] 类型一致：`ExtractState` / `ExtractDeps` / `ExtractRuntime` 字段与方法名在 S1a-S1d 间一致；`dispatch_action(action, ctx) -> ActionResult` 在 S2 各子片一致；`SetupTools` 方法名在 S3 一致。
- [x] 不破坏契约：全程纯平移，无新增/改名 contract 字段。

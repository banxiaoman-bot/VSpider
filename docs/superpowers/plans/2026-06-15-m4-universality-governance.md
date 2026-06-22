# M4「通用·治理」实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。

**目标：** 拆解超体量文件 + 贯通输出契约，让宗旨「通用」铁律有制度化落地
**架构：** main.py(18593行) 按 Agent 生命周期拆为 phases/ 子模块；api_server 按业务拆 router；output_contract→manifest 链路闭环
**技术栈：** Python 3.13 / Playwright / Vue 3 / pytest

---

## 现状证据

| 文件 | 行数 | 警戒线 | 状态 |
|---|---|---|---|
| `main.py` | 18593 | ~850KB | 🔴 远超，run_agent 单函数 ~11000 行 |
| `browser_env.py` | 4769 | ~207KB | 🟡 |
| `api_server.py` | 3872 | ~130KB | 🟡 |
| `App.vue` | 4674 | ~287KB | 🟢 已拆到 181KB |
| `actions/` | 9 模块 | ~314KB | 🟢 已拆 |

### 契约缺口

- `manifest.json` 写入：仅 `persistence.py` / `media_harvester` / `main.py` 中有，data_writers/dispatch.py 未闭环
- media 下载：media_harvester 存在但未在所有 output_kind=media_* 场景贯通
- `input_contract.json` 落盘：io_contract/persistence.py 有，但 preflight 推断(attachment intent / URL from goal) 未完整接线

---

## 文件结构（将要创建/修改）

### 新模块

| 路径 | 职责 |
|---|---|
| `visual_web_agent/phases/startup.py` | run_agent 初始化：浏览器启动、auth sentinel、pre-checks、状态变量 |
| `visual_web_agent/phases/planning.py` | Planner / Reflector / TaskPlan 状态机 |
| `visual_web_agent/phases/decision.py` | VLM 决策回合：构造 prompt → VLM 调用 → 解析 action |
| `visual_web_agent/phases/action_dispatch.py` | 动作执行分发 + post-action hooks |
| `visual_web_agent/phases/finalization.py` | run 结束：汇总、manifest、event_stream 收尾 |
| `visual_web_agent/som_injector.py` | SoM 注入逻辑（从 browser_env.py 抽出） |
| `api_routes/run_api.py` | /run 相关路由 |
| `api_routes/task_api.py` | /task 相关路由 |
| `api_routes/ws_api.py` | WebSocket 路由 |

### 修改文件

| 路径 | 变更 |
|---|---|
| `visual_web_agent/main.py` | 每个 G-slice 减 1000-3000 行，最终仅剩 run_agent 骨架 + CLI |
| `visual_web_agent/browser_env.py` | G6 抽 SoM 后减 ~500 行 |
| `api_server.py` | G7 拆 router 后仅剩 app 启动 + 中间件 |
| `visual_web_agent/data_writers/dispatch.py` | C1 manifest 写入闭环 |
| `visual_web_agent/io_contract/persistence.py` | C1 manifest 验证 |
| `tests/test_phase_*.py` | 每个 G-slice 配回归测试 |

---

## Slice 清单

### G0: 清理 actions.py.bak (准备)

- **文件：** 删除 `visual_web_agent/actions.py.bak`
- **步骤：** 确认 `actions/` 9 模块完整覆盖后删除 bak
- **验证：** `python -m pytest tests/ -q` 全绿
- **提交：** `chore: remove actions.py.bak after successful split`

### G1: main.py → phases/startup.py (拆分)

- **文件：** `visual_web_agent/phases/startup.py`, `visual_web_agent/main.py`
- **步骤：**
  1. 写失败测试：StartupPhase.run() 返回 RunContext 数据类
  2. 从 run_agent 提取初始化段（浏览器启动 → auth sentinel → pre-checks → 状态变量初始化 → LoopDetector/Judge/FailureClassifier 创建），平移到 StartupPhase
  3. main.py 一行委托 `ctx = StartupPhase(config).run()`
  4. 按 CRLF patch 规范：写 `_patch_startup.py` 字节级替换，patch 完即删
- **验证：** 定向测试绿 + `test_agent_loop_stub_e2e` 绿 + 全量 pytest
- **提交：** `refactor(main): extract startup phase — pure move, no behavior change`

### G2: main.py → phases/planning.py (拆分)

- **文件：** `visual_web_agent/phases/planning.py`, `visual_web_agent/main.py`
- **步骤：**
  1. 写失败测试：PlanningPhase.maybe_plan() / .maybe_reflect() 返回 TaskPlan / ReflectDecision
  2. 从 run_agent 提取 Planner/Reflector 相关段（TaskPlan 构造 + 反思触发逻辑 + abort 判定），平移到 PlanningPhase
  3. main.py 循环内一行委托 `plan_result = planning.maybe_reflect(step, ...)`
  4. CRLF patch 规范同 G1
- **验证：** 定向测试绿 + e2e 绿 + 全量 pytest
- **提交：** `refactor(main): extract planning phase — pure move, no behavior change`

### G3: main.py → phases/decision.py (拆分)

- **文件：** `visual_web_agent/phases/decision.py`, `visual_web_agent/main.py`
- **步骤：**
  1. 写失败测试：DecisionPhase.decide() 输入 PerceptionSnapshot → 输出 ParsedAction
  2. 从 run_agent 提取 VLM 决策段（prompt 构造 → VLM 调用 → 响应解析 → action 提取），平移
  3. main.py 一行委托 `action = decision.decide(snapshot, ...)`
  4. CRLF patch
- **验证：** 同上
- **提交：** `refactor(main): extract decision phase — pure move, no behavior change`

### G4: main.py → phases/action_dispatch.py (拆分)

- **文件：** `visual_web_agent/phases/action_dispatch.py`, `visual_web_agent/main.py`
- **步骤：**
  1. 写失败测试：ActionDispatcher.dispatch(action, browser) → ActionResult
  2. 从 run_agent 提取动作执行段（action 类型分发 → handler 调用 → post-action hooks：loop_detector / wait_guard / submit_guard / tab_switch），平移
  3. main.py 一行委托 `result = dispatcher.dispatch(action, browser)`
  4. CRLF patch
- **验证：** 同上
- **提交：** `refactor(main): extract action dispatch phase — pure move, no behavior change`

### G5: main.py → phases/finalization.py (拆分)

- **文件：** `visual_web_agent/phases/finalization.py`, `visual_web_agent/main.py`
- **步骤：**
  1. 写失败测试：FinalizationPhase.finalize() 返回 RunSummary
  2. 从 run_agent 提取结束段（汇总数据 → 写 manifest → 关 event_stream → 生成 run_summary → 保存 HTML trace），平移
  3. main.py 一行委托 `summary = finalization.finalize(ctx, ...)`
  4. CRLF patch
- **验证：** 同上
- **提交：** `refactor(main): extract finalization phase — pure move, no behavior change`

### G6: browser_env.py → som_injector.py (拆分)

- **文件：** `visual_web_agent/som_injector.py`, `visual_web_agent/browser_env.py`
- **步骤：**
  1. 写失败测试：SomInjector.inject(page, scope) → SomResult
  2. 从 browser_env.py 提取 SoM 注入相关函数（JS 注入 + 元素标注 + 截图叠加），平移
  3. browser_env.py 一行 import 委托
  4. CRLF patch
- **验证：** E2 局部 SoM 12 例绿 + e2e 绿 + 全量 pytest
- **提交：** `refactor(browser_env): extract SoM injector — pure move`

### G7: api_server.py → api_routes/ (拆分)

- **文件：** `api_routes/__init__.py`, `api_routes/run_api.py`, `api_routes/task_api.py`, `api_routes/ws_api.py`, `api_server.py`
- **步骤：**
  1. 按路由前缀分组：/api/run/* → run_api.py, /api/task/* → task_api.py, WebSocket → ws_api.py
  2. 每组 route handler 平移到对应文件，api_server.py 仅留 app 创建 + 中间件 + router include
  3. CRLF patch
- **验证：** `npm run build` + API 测试绿 + 全量 pytest
- **提交：** `refactor(api): split routers by business domain`

### C1: manifest.json 写入闭环 (契约)

- **文件：** `visual_web_agent/data_writers/dispatch.py`, `visual_web_agent/io_contract/persistence.py`, `tests/test_manifest_enforcement.py`
- **步骤：**
  1. 写失败测试：每次 write_output 调用后 manifest.json 必须含对应 entry
  2. dispatch.py 的 write_output 末尾追加 `append_manifest_entry(run_id, entry)`
  3. persistence.py 新增 `append_manifest_entry()` + `validate_manifest(run_id)` 验证完整性
  4. 确保 xlsx_writer / csv_writer / jsonl 三条路径都触发 manifest 写入
- **验证：** 定向测试 + 全量 pytest
- **提交：** `feat(contract): manifest.json enforcement for all data writers`
- **新增 contract 字段：** `manifest.json` entry 增 `writer` 字段标识来源

### C2: media_harvester 真下载贯通 (契约)

- **文件：** `visual_web_agent/media_harvester/harvester.py`, `visual_web_agent/media_harvester/agent_hook.py`, `tests/test_media_download.py`
- **步骤：**
  1. 写失败测试：output_kind=media_image/video 时产出真文件（非 xlsx row）
  2. agent_hook 检测 output_contract.output_kind ∈ media_* 时路由到 harvester.download()
  3. harvester.download() 落盘到 `runs/<run_id>/artifacts/` + 写 manifest entry
  4. 禁止 media URL 作为 row 写入 xlsx（data_writers/dispatch.py 加门禁断言）
- **验证：** stub HTTP server + 定向测试 + 全量 pytest
- **提交：** `feat(contract): media harvester true download — no more URL-as-row`
- **新增 contract 字段：** 无（manifest entry 格式不变）

### B1: 体量基线 + 收口 (收口)

- **文件：** `tests/test_file_size_baseline.py`, `docs/vspider_architecture_backlog.md`
- **步骤：**
  1. 写基线测试：main.py < 5000 行, browser_env.py < 4000 行, api_server.py < 1000 行
  2. 全量 pytest 0 新增失败
  3. backlog 记录 M4 收口
- **验证：** 全量 pytest
- **提交：** `test: M4 file size baseline + backlog closeout`

---

## 依赖与实施顺序

```
G0 (清理)
  ↓
G1 (startup) → G2 (planning) → G3 (decision) → G4 (dispatch) → G5 (finalization)
  ↓                                                                    ↓
G6 (SoM injector)   G7 (api routes)                              C1 (manifest)
                                                                      ↓
                                                                 C2 (media download)
                                                                      ↓
                                                                 B1 (baseline + 收口)
```

- G1-G5 **严格串行**（每步从 main.py 切走一段，后续切依赖前续的稳定边界）
- G6 / G7 与 G1-G5 **可并行**（不同文件）
- C1 在 G5 之后（finalization 稳定后才接 manifest）
- C2 在 C1 之后（依赖 manifest 写入机制）
- B1 最后

## 预估里程碑体量

完成后 main.py 预计从 18593 行降至 ~4000-5000 行（CLI + run_agent 骨架 + 辅助函数），browser_env.py 减 ~500 行，api_server.py 减至 ~800 行。所有 G-slice 为纯平移零行为改动。

## 风险

1. **CRLF 字节级 patch**：每个 G-slice 都需要写 `_patch_*.py` 脚本，patch 完即删——VSpider 工程规范强制。
2. **main.py 11K 行函数**：run_agent 内部变量大量交叉引用，每次切割需精确识别依赖图，可能需要引入 RunContext 数据类传递状态。
3. **import 环**：phases/ 子模块可能需要延迟 import 避免循环依赖。

## 自检

- [x] 规格覆盖：体量警戒线 7 个文件中 main.py / browser_env.py / api_server.py 覆盖（actions/ 已拆、App.vue 已达标、vlm_client.py / prompts.py 体量可接受）
- [x] 契约覆盖：output_contract → manifest 闭环（C1）+ media 真下载（C2）
- [x] 无占位符：每个 slice 有文件/步骤/验证/提交
- [x] 类型一致：RunContext / PerceptionSnapshot / ParsedAction / ActionResult / RunSummary 一致

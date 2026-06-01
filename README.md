# VSpider - Capability-Orchestrated Browser Agent

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Playwright](https://img.shields.io/badge/Playwright-async-green.svg)](https://playwright.dev/python/)
[![Vue](https://img.shields.io/badge/Vue-3-green.svg)](https://vuejs.org/)

VSpider 是一个自研的网页自动化与结构化抽取系统。它从早期的“截图 → SoM 标注 → VLM 决策 → 浏览器操作”循环，演化为 **Capability Router + Browser Control + Runtime Guards + Data Plane + Regression Fixtures** 的分层浏览器智能体。

项目目标是：在网页点击、填写、悬停、翻页、抽取、回放和诊断场景中，优先使用可验证的 DOM/AX/Playwright/JS deterministic 路径；视觉/VLM 能力作为规划、理解和兜底能力，而不是替代所有确定性执行。

## 当前架构

VSpider 当前按七层能力组织：

```text
intent_planning
  capability_router.py       # 意图识别、能力路由、planner feedback
  planner_contract.py        # execution_plan / risk flags / planner input

operations_plane
  api_server.py              # FastAPI 应用入口与跨模块集成
  capability_failure_fixture_api.py
  artifact_manager.py        # artifact path / URL / registry
  run_registry.py
  queue_state.py

runtime_guards
  loop_detector.py
  wait_loop_guard.py
  submit_loop_guard.py
  tab/session/drop guards

browser_substrate
  browser_control.py         # 低层浏览器控制 API 与 action trace
  browser_backend.py         # local / remote Playwright backend
  browser_pool.py            # runtime status / capacity / health
  browser_env.py             # Agent loop 使用的 Playwright browser env

data_plane
  extraction_engine/         # selector / snapshot / recovery / strategies
  data_manager.py
  data_sanitizer.py
  spider_lite.py
  network_intelligence.py

execution_kernel
  main.py                    # 主 Agent loop
  actions.py                 # action handlers
  action_registry.py         # deterministic tool catalog
  action_result.py           # structured action evidence
  route_executor.py

model_plane
  vlm_client.py
  prompts.py
  prompt_skills.py
  message_compaction.py
```

## 核心能力

- **Capability Router**：将用户目标路由到 browser control、spider、extractor、API replay、human guard 等能力。
- **Browser Control API**：提供 open、snapshot、click、fill、hover、type、press、scroll、wait、navigate、selector、similar、screenshot 等低层操作。
- **ActionRef 合约**：归一化 SoM、AX、selector、bbox、browser ref，降低目标定位不稳定性。
- **Runtime Preflight/Drift/Issue Summary**：在 route、plan、execute、UI 侧展示浏览器 runtime 状态和风险。
- **Browser Action Trace**：成功/失败 action 都可产生 `browser_action_trace.v1` 与 `browser_action_issue_summary.v1`。
- **Failure Fixture Regression**：可从 capability execute failure bundle 生成 fixture，离线 replay，批量 replay，查看 history 和 latest-vs-previous trend。
- **Extraction Engine**：支持结构化抽取、snapshot replay、selector fingerprint/recovery 等离线可复现能力。
- **Frontend Capability Panel**：展示 capability trace、runtime health、browser action issues、failure fixtures、batch replay history/trend。

## 快速开始

### 安装后端依赖

```powershell
pip install -r visual_web_agent/requirements.txt
playwright install chromium
```

### 安装前端依赖

```powershell
npm install --prefix vspider-ui
```

### 配置环境变量

```powershell
Copy-Item .env.example .env
```

编辑 `.env` 或 `visual_web_agent/.env.example` 中对应配置，填入模型服务地址、Key、浏览器后端等参数。

### 启动 API 服务

```powershell
python api_server.py
```

### 启动前端

```powershell
npm run dev --prefix vspider-ui
```

### 运行 Agent CLI

```powershell
python -m visual_web_agent.main --url "https://example.com" --goal "提取页面上的列表数据"
```

## 常用验证命令

Y99 后推荐使用统一验证脚本，避免误扫外部参考仓库或临时目录：

```powershell
python scripts/validate_y.py y100_capability_fixture_router --target-test tests/test_capability_router.py
```

该脚本会依次执行：

1. targeted pytest
2. `npm run build` in `vspider-ui`
3. core pytest set
4. full `python -m pytest tests -q`

清理 pytest 临时目录：

```powershell
python scripts/clean_pytest_tmp.py
python scripts/clean_pytest_tmp.py --apply
```

默认 `clean_pytest_tmp.py` 是 dry-run，只有带 `--apply` 才会删除。

## 测试发现规则

根目录 `pytest.ini` 将默认测试发现限制在 `tests/`，并排除：

- `.tmp_*`
- `tmp*`
- `workspace`
- `browser-use-main`
- `skyvern-main`
- `WebVoyager-main`
- `vspider-ui/node_modules`
- `vspider-ui/dist`

因此推荐使用：

```powershell
python -m pytest tests -q --basetemp .tmp_pytest_validate_local_full
```

## 外部参考项目

仓库中可能包含 `browser-use-main`、`skyvern-main`、`WebVoyager-main` 等参考目录。它们用于架构学习和对比，不是 VSpider 的运行时依赖。当前原则是借鉴浏览器状态、工具注册、事件流、selector recovery、视觉 grounding 等模式，但不直接引入 vendor dependency，除非明确需要。

## 运行示例

```powershell
python -m visual_web_agent.main `
  --url "https://example.com" `
  --goal "打开页面，填写搜索条件，并导出结果表格"
```

```powershell
python -m visual_web_agent.debug_cli state --url "https://example.com"
python -m visual_web_agent.debug_cli clickable --url "https://example.com"
python -m visual_web_agent.debug_cli tools --goal "点击 Next 翻页"
```

## 相关文档

- `docs/open_source_patterns.md`
- `docs/vspider_architecture_backlog.md`
- `docs/AGENT_NOTES.md`

## License

MIT

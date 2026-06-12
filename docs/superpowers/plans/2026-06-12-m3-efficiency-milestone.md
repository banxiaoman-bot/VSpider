# M3「高效」里程碑 立项计划（perception & cost）

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐 slice 实现此计划。
> 每个 slice 实施前先按 TDD 写失败测试，完成后走 `python scripts/validate_y.py <slice_id> --target-test tests/test_<file>.py` 收口，并在 `docs/vspider_architecture_backlog.md` 记一行 done slice。

**目标：** 落实宗旨铁律第 2 条「高效」——最少回合完成目标；能局部感知就不全页 SoM；能 API replay 就不重放浏览器；能缓存就不重抓。
**架构：** 在感知阶段（browser_env SoM/截图/AX）加「复用 → 局部 → 增量」三级降本，在决策阶段（capability_router/vlm_client）加「缓存命中跳 VLM → 回合预算」两道闸，最后用 benchmark 回归锁住收益。
**技术栈：** 现有 Playwright + SoM v6 注入脚本（`config.SOM_SCRIPT_PATH`）、`extraction_engine/selectors.py` selector_fingerprint、`event_stream.py`、pytest stub-frame 范式。

**现状证据（立项依据）：**
- 全库无 `partial_som / viewport_only / perception_budget / perception_cache` 任何实现（Grep 0 命中）；SoM 每回合全页注入（`browser_env.py:1740` 起加载全量脚本，`_last_som_elements` 只存最近一轮）。
- `dom_hash` 仅在 `actions.py` 内部使用，未用于感知阶段跳过判断。
- `selector_fingerprint` 只活在 `extraction_engine/{selectors,snapshots,recovery}.py` 抽取链路，未反哺主循环动作定位、不跨 run 持久化。
- M1（准确 S1-S5）、M2（跨系统 S6-S11）已收口，全量 pytest 3284 通过，工程地基稳定，适合开效率主线。

**Slice 编号约定：** 延续 backlog 风格，本里程碑用 `E*`（Efficiency）前缀；P0/P1 是前置准备。

---

## P0（前置）：仓库卫生 .gitignore

- 影响层: 工程基建，零运行时改动。
- 文件: `.gitignore`（补条目）。
- 内容: 忽略 `runs/`、`logs/`、`screenshots/`、`workspace/artifacts/`、`workspace/tmp_*/`、`temp_uploads/`、`visual_web_agent/browser_data/`、`**/__pycache__/`、`.pytest_cache/`、`.tmp_pytest_*/`、`tests/.tmp_intelligence/`。
- 动机: 当前 git status 130+ 条运行时产物 untracked 裸露，含浏览器 cookie/storage（`browser_data/`）有误提交风险。
- 验证: `git status --short` 不再出现上述目录；已有受控文件不受影响。
- 验收: 干跑一次 stub 任务后 `git status` 干净。

## P1（前置）：感知阶段抽离 phases/perception.py

- 影响层: execution_kernel（main.py 拆分第一刀，遵守体量警戒线规则）。
- 文件: 新建 `visual_web_agent/phases/__init__.py`、`visual_web_agent/phases/perception.py`；`visual_web_agent/main.py` 感知段改为调用。
- 方法: 只做「平移 + 委托」，不改行为——把 main.py 中每回合「截图 + SoM 注入 + AX 摘要 + browser_state 组装」的代码块原样搬入 `PerceptionPhase.run(browser, ctx) -> BrowserStateSnapshot`，main.py 原位置一行委托。CRLF 大块平移按工程规范写 `_patch_perception_split.py` 按字节搬移后即删。
- 测试: `tests/test_phase_perception_split.py`——stub browser 断言 PerceptionPhase 返回的 snapshot 字段与拆分前 main 路径一致（url/title/screenshot_path/ax_excerpt/interactive_count）；现有 `test_agent_loop_stub_e2e.py` 2 例必须保持绿。
- 验收: main.py 行数净减少；全量 pytest 不掉。
- 风险: 中——main.py 850KB CRLF，必须按字节 patch；拆分范围只许感知段，禁止顺手重构。

## E1：感知复用（perception reuse / skip）

- 能力名: perception_reuse。
- 影响层: execution_kernel（phases/perception.py）+ browser_substrate（browser_env.py 加 DOM 签名探针）。
- 方法:
  1. `browser_env.py` 新增 `async def dom_signature(self) -> str`：`page.evaluate` 取 `location.href + DOM 节点数 + body 文本长度 + 可交互元素数` 拼 sha1（一次 evaluate，<5ms）。
  2. `PerceptionPhase` 持有 `last_signature`；本回合签名相同且上一动作 `ActionResult.changed_url/changed_dom` 均 False → 跳过截图与 SoM 重注入，复用上一轮 `BrowserStateSnapshot`，snapshot 标记 `reused=True`。
  3. event_stream `observe` 事件加 `perception_reused: bool`（仅新增字段，契约向下兼容）。
  4. 逃生阀: 连续复用 ≥3 回合强制全量感知一次，防签名碰撞死视。
- 测试: `tests/test_perception_reuse.py`——stub page 两轮同签名断言第二轮不调 screenshot/SoM；动作 changed_dom=True 强制重感知；连续 3 次复用后第 4 次强制全量。
- 验收: stub e2e 任务里静态等待回合（如 wait/scroll 后未变）感知调用次数下降；evidence 链不缺。

## E2：局部 SoM（viewport / region 标注）

- 能力名: partial_som。
- 影响层: browser_substrate（SoM 注入 JS + browser_env.py）+ execution_kernel（phases/perception.py 选区策略）。
- 方法:
  1. SoM 脚本（`config.SOM_SCRIPT_PATH` 指向的 JS）加入参 `{scope: "viewport" | "full" | {selector}}`：viewport 模式只给 `getBoundingClientRect` 与视口相交的可交互元素编号；selector 模式只标注容器内元素。
  2. `browser_env` SoM 调用透传 scope；默认 `viewport`，以下情形回退 `full`：首回合、上一动作含 scroll/翻页、VLM 上一轮显式要求全页、E1 强制全量回合。
  3. `_last_som_elements` 记录本轮 scope，`target_id` 幻觉校验报错信息带 scope 提示（"目标可能在视口外，先 scroll"）。
- 测试: `tests/test_partial_som.py`——真 Chromium 长页 fixture：viewport 模式编号集 ⊂ full 模式且不含视口外元素；scroll 后回退 full 一轮；selector scope 只含容器内元素。
- 验收: 长列表页感知 token（SoM 元素行数）显著下降；现有 SoM 回归（som_id 定位、SEMANTIC FALLBACK）全绿。
- 风险: 中——视口外目标变成两步（scroll→点击），需要 prompts.py 加一条 skill 区块说明 scope 语义（六步范式第 4 步）。

## E3：AX 摘要增量 diff

- 能力名: ax_incremental。
- 影响层: execution_kernel（phases/perception.py）。
- 方法: snapshot 组装时持有上一轮 AX excerpt 的行集合；本轮输出改为 `unchanged_count + added_lines + removed_lines`（首回合/强制全量回合给全量）。prompt 渲染层把 diff 形态翻译成自然描述喂 VLM。
- 测试: `tests/test_ax_incremental.py`——两轮 stub AX 树：第二轮输出仅含新增/消失行；首轮全量；E1 强制全量回合给全量。
- 验收: 多回合任务 observe 事件平均体积下降；VLM 决策正确性 stub 回归不掉。

## E4：selector_fingerprint 跨 run 动作缓存

- 能力名: action_selector_cache。
- 影响层: data_plane（新模块 `visual_web_agent/selector_cache.py`）+ operations_plane（actions.py click/type 定位前探查）。
- 方法:
  1. 新模块 `selector_cache.py`：键 = `host + 目标语义归一（role+name 截断）`，值 = `{selector, fingerprint, hits, last_ok_at}`；落盘 `workspace/selector_cache/<host>.json`，复用 `extraction_engine/selectors.py` 的 fingerprint 校验函数判断 selector 是否仍指向同语义元素。
  2. click/type handler 定位前先查缓存：fingerprint 校验通过直接用缓存 selector（rpa_trail 记 `method=selector_cache`），失败则走原 SoM/VLM 路径并把成功定位回写缓存。
  3. 缓存命中也必须产出完整 evidence（readback/bbox），不降准确性换效率。
- 测试: `tests/test_selector_cache.py`——stub-frame：首次定位回写缓存；二次任务命中跳 VLM 路径；fingerprint 失配回退原路径并刷新缓存；缓存文件结构断言。
- 验收: 同站点重复任务（如每日抓同列表）第二次 run 的 VLM 调用次数下降；准确性回归全绿。

## E5：VLM 回合预算与成本计量

- 能力名: vlm_budget。
- 影响层: model_plane（vlm_client.py 计量钩子）+ runtime_guards（新 guard）+ intent_planning（input_contract.constraints 透传）。
- 方法:
  1. `input_contract.constraints` 已有 `max_runs/rate_limit_qps`，新增 `max_vlm_calls`（0=不限，仅新增字段）。
  2. vlm_client 每次调用累计 `{calls, prompt_tokens, completion_tokens}` 进 run 级计数器；event_stream 每次 `decide` 事件带累计值。
  3. 新 guard `vlm_budget_guard`：超预算 → 触发确定性收口（能 done 则 done，不能则走 capability_failure_fixture 落盘 + 明确报告"预算耗尽于第 N 回合"），禁止静默截断。
  4. `run_summary` / manifest 增加成本小结（calls/tokens）。
- 测试: `tests/test_vlm_budget.py`——stub VLM 计数累加；预算 3 跑到第 4 次决策触发 guard；预算 0 不限；event_stream 字段断言。
- 验收: 每个 run 能回答"花了几次 VLM、多少 token"；失控长跑被预算闸住且留痕。

## E6：api_replay 优先级提升（capture 命中即上位）

- 能力名: api_replay_first。
- 影响层: intent_planning（capability_router.py 路由排序）。
- 方法: `route_task` 组 fallback_chain 时探查该 host 是否已有可用 capture fixture（api_replay 模块现成的 fixture 索引）；命中则把 `api_replay` 提到链首（浏览器路径降为 fallback），signals 加 `api_replay_available`（仅新增）。route_executor 已支持按链执行（M2 已验证 prefers_api 路径），本 slice 只动排序。
- 测试: `tests/test_api_replay_first.py`——有 fixture 的 host 链首为 api_replay；无 fixture 顺序不变；fixture 过期/校验失败回退浏览器路径。
- 验收: 二次抓取同 API 数据源的任务不再起浏览器；首次任务行为不变。

## E7：效率基线 benchmark 回归（收口 slice）

- 能力名: efficiency_baseline。
- 影响层: 仅测试资产。
- 方法: `tests/test_efficiency_baseline.py`——复用 `tests/scenario_site.py` 双系统 fixture + stub VLM，跑固定 3 个任务（列表抽取 / 表单提交 / 跨系统接力），统计 `{回合数, VLM 调用数, 感知全量次数, observe 事件总字节}`，断言不超过录定基线（基线常量随 E1-E6 落地逐步收紧并在本文件内更新，防回归）。
- 验收: 任一后续 PR 让效率指标回退 >20% 即测试红。

---

## 实施顺序与依赖

```
P0 ──┐
P1 ──┼─→ E1 ─→ E2 ─→ E3 ─┐
     │                    ├─→ E7（最后收口）
     └─→ E4（独立） E5（独立） E6（独立）
```

- P0/P1 先行；E1→E2→E3 串行（同在 perception 链路）；E4/E5/E6 互相独立可并行。
- 每个 slice 单独 commit + `validate_y` 收口 + backlog 记 done 行；禁止 omnibus。

## 自检

- 规格覆盖度: 宗旨「高效」四个分句——最少回合(E5/E7)、局部感知(E1/E2/E3)、API replay(E6)、缓存不重抓(E4) 全部有对应 slice。✓
- 不破坏契约: 所有新字段（perception_reused / scope / max_vlm_calls / api_replay_available / data 成本小结）均为仅新增。✓
- 不降准确性: E1 有逃生阀、E2 有 full 回退、E4 命中仍出全套 evidence——效率不以牺牲铁律 1 为代价。✓

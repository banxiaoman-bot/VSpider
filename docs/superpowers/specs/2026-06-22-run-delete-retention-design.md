# 运行记录删除 + 保留策略（run delete + retention）设计

> brainstorming 产物（本文件不含运行代码）。已与用户确认 **4 项决策**：①保留=按条数最近 **100** 条；②GC=每次新 run **完成后**触发 prune；③手动删除=删整 `runs/<id>/` + 移除索引，**正在运行的 run 禁删**；④UX=运行记录每行删除 + **二次确认**，连带产物，不做一键清空、不单独删产物。实施按 `writing-plans` 出计划 → TDD → 收口。

## 1. 背景与目标

### 痛点（实测证据）
- `/api/runs` 仅 GET / `{id}` / retry / network/replay（`api_routes/runs_api.py`），**无 DELETE**；`/api/artifacts` 仅 GET；前端 `RunRegistryPanel.vue` 操作列只有「详情」，无删除。
- 后端**无任何 runs/产物保留或清理逻辑**（全仓仅 `run_registry._atomic_write_json` 的临时文件 unlink + 测试 rmtree；`browser_profile` 的 TTL-GC 是浏览器配置，与 runs 无关）。
- 结果：每跑一次任务累积一份 `runs/registry/<id>.json` + `runs/<id>/`（manifest/artifacts/screenshots）+ `logs/*_<id>.*`，**永不回收 → 无限堆积**。

### 目标
- 自动保留最近 **100** 条 run，超出的最旧**终态** run 自动清理（含其产物与日志）。
- 用户可在运行记录面板**逐条删除**（二次确认），删除连带该 run 的产物与日志。
- **绝不**删除正在运行的 run。

### 非目标（YAGNI）
- 不做按时间（TTL）保留（仅条数；后续需要再加）。
- 不做「一键清空全部」。
- 不做产物 tab 的单文件删除（删 run 即连带清产物，避免「记录在、产物缺」不一致态）。
- 不做软删除。
- 不做后台定时 GC 线程（仅在 run 完成后顺手 prune）。

## 2. 范围

| slice | 内容 | 触碰层 | 风险 |
| --- | --- | --- | --- |
| RUN-DEL-1 | `run_registry` 加 `delete_run` / `prune_runs`；`DELETE /api/runs/{id}` + 活动任务护栏；run 完成后 prune 接线；`RunRegistryPanel` 行内删除 + 二次确认；测试 | run_registry / runs_api / api_server / RunRegistryPanel / tests | 中（删文件，需护栏 + 路径安全） |

单一实现计划可覆盖。

## 3. 已定决策（与用户确认）

| # | 决策点 | 选择 |
| --- | --- | --- |
| 1 | 保留策略 | 按条数，保留最近 **100**（env `VSPIDER_RUN_RETENTION_KEEP` 可覆盖） |
| 2 | GC 触发 | 每次新 run **完成后**（`complete_run` 之后）prune 到 ≤100 |
| 3 | 删除范围 + 安全 | 删 `runs/registry/<id>.json` + `runs/<id>/` + `logs/*_<id>.*`；**活动 run 禁删（409）** |
| 4 | 删除 UX | 运行记录每行删除按钮 + `el-popconfirm` 二次确认；连带产物；无一键清空 |

## 4. 后端设计

### 4.1 `visual_web_agent/run_registry.py`（加两个模块函数）

复用既有 `_safe_run_id`（`^[0-9A-Za-z_-]+$`，已阻断 `.`/`..` 路径穿越）、`project_root()`、`registry_root()`、`_run_path()`、`_derive_paths()`。

| 函数 | 职责 |
| --- | --- |
| `delete_run(run_id, *, base_dir=None) -> bool` | 纯文件级删除：移除 `runs/registry/<id>.json`、`runs/<id>/` 整目录（若存在）、`_derive_paths(id)` 里的 `logs/run_log_<id>.html` / `logs/phase_<id>.jsonl` / `logs/event_stream_<id>.jsonl`。run_id 非法 → `ValueError`；记录不存在 → 返回 `False`；成功 → `True`。**不做状态护栏**（护栏放调用方，见 4.2/4.3） |
| `prune_runs(*, keep=100, base_dir=None) -> list[str]` | 按 `created_at` 倒序列出全部 run；对**索引 > keep** 且**状态属于 `_TERMINAL_STATUS`** 的逐个调 `delete_run`；返回被删 run_id 列表。**绝不删非终态**（running/queued/paused 即使很旧也保留） |

- 删除用 `shutil.rmtree(path, ignore_errors=True)` 删 `runs/<id>/`；`Path.unlink(missing_ok=True)` 删 json 与 log 文件。
- 路径安全：所有路径都经 `_safe_run_id` + 限定在 `project_root()/runs|logs` 下，杜绝穿越。

### 4.2 `DELETE /api/runs/{run_id}`（`api_routes/runs_api.py`）

- `register_runs_routes` 新增依赖 `is_run_active: Callable[[str], bool]`。
- 处理：
  1. `rec = run_registry.load_run(run_id)`；`None` → **404** `run not found`。
  2. `is_run_active(run_id)` 为真 → **409** `cannot delete a running run`（活动任务护栏，权威信号取自实时 `active_tasks`，不依赖可能陈旧的记录 status，故陈旧 'running' 记录仍可删）。
  3. `run_registry.delete_run(run_id)` → 返回 `{status:"success", deleted: run_id}`。
- run_id 非法（`ValueError`）→ **400**。

### 4.3 接线（`api_server.py`）

- `register_runs_routes(... , is_run_active=lambda rid: _is_active_run(rid))`，其中 `_is_active_run` 读 `active_tasks.get("current_task")` 判断 `task_id == rid` 且未结束。
- 模块常量 `RUN_RETENTION_KEEP = int(os.getenv("VSPIDER_RUN_RETENTION_KEEP", "100"))`。
- 在 `_run_batch_task` 完成路径（`api_server.py:299` `complete_run` 之后）追加：`_run_registry.prune_runs(keep=RUN_RETENTION_KEEP)`，包 `try/except` 仅 warn（prune 失败不影响主流程）。

## 5. 前端设计（`RunRegistryPanel.vue`）

- 「操作」列在「详情」按钮旁加**删除**按钮，用 `el-popconfirm` 包裹（title「删除该 run 及其产物？不可恢复」）；`@click.stop` 防止触发行点击的详情弹窗。
- 新增 `deleteRun(row)`：`apiFetch('/api/runs/'+encodeURIComponent(row.run_id), { method:'DELETE' })`：
  - 200 → 从本地 `runs` 列表移除该行 + `ElMessage.success('已删除')` + `emit('loaded', runs.value)`（同步计数/badge）。
  - 409 → `ElMessage.warning('运行中的任务不可删除')`。
  - 404 → 从列表移除（已不存在）+ info 提示。
  - 其它 → `ElMessage.error`。
- 引入 `ElMessage`（element-plus）。
- 操作列宽度由 92 调宽到容纳两按钮（约 150）。

## 6. 安全
- 路径穿越：`_safe_run_id` 正则 + 限定根目录；`rmtree` 仅作用于 `runs/<safe_id>/`。
- 活动任务护栏：实时 `active_tasks` 判定，409 拒删；prune 只删终态。
- 删除不可恢复：前端 `el-popconfirm` 二次确认；spec 注明无回收站（YAGNI）。

## 7. 落地映射（精确文件）

| 步 | 文件 | 改动 |
| --- | --- | --- |
| 1 后端存储 | `visual_web_agent/run_registry.py` | 新增 `delete_run` + `prune_runs`（+ `import shutil`） |
| 2 后端端点 | `api_routes/runs_api.py` | `register_runs_routes` 加 `is_run_active` 形参 + `DELETE /api/runs/{id}` |
| 3 后端接线 | `api_server.py` | 传 `is_run_active`；加 `RUN_RETENTION_KEEP`；`complete_run` 后 `prune_runs` |
| 4 前端 | `vspider-ui/src/components/RunRegistryPanel.vue` | 行内删除按钮 + popconfirm + `deleteRun` + ElMessage |
| 5 测试 | `tests/test_run_delete_retention.py`（新增）+ 前端 source-structural 断言 | 见 §8 |

## 8. 测试计划

后端 `tests/test_run_delete_retention.py`（pytest + TestClient，monkeypatch base_dir/`active_tasks`）：
1. `delete_run`：建记录 + `runs/<id>/` + log 文件 → 删后三者皆不存在、返回 True。
2. `delete_run` 不存在 → False；非法 id（含 `..`）→ ValueError。
3. `prune_runs(keep=N)`：造 N+3 条终态 → 删最旧 3、保留最新 N；非终态（running）即使最旧也不删。
4. `DELETE /api/runs/{id}`：终态 run → 200 + 文件清除；不存在 → 404；`is_run_active` 为真 → 409 且文件仍在。

前端（沿用 `test_frontend_component_split_y126.py` 的 source-structural 风格）：
5. 断言 `RunRegistryPanel.vue` 含 `method: 'DELETE'` 的 `apiFetch('/api/runs/...')` + `el-popconfirm`（无组件 mount 基建，按现有惯例做源断言；纯交互删除逻辑由后端测试覆盖核心风险）。

## 9. 风险
1. **误删活动 run**：实时 `active_tasks` 护栏 + prune 只删终态。
2. **路径穿越**：`_safe_run_id` + 根目录限定。
3. **prune 抖动/失败**：包 try/except 仅 warn，不阻断 run 完成。
4. **陈旧 'running' 记录永久不可删**：护栏取实时 active_tasks 而非记录 status，故陈旧记录可手动删。
5. **CRLF 字节 patch**：`api_server.py` 若 StrReplace 失败按 `vspider-workflow §一` 写 `_patch_rundel.py` 字节改写即删。

## 10. 回滚
- 删 `DELETE` 端点 + `is_run_active` 形参；移除 `prune_runs` 调用与 `delete_run`/`prune_runs` 函数；还原 `RunRegistryPanel` 操作列。无配置/不调用时行为=现状。

## 11. 自检（规格）
- [x] 占位符：无 TODO/待定。
- [x] 一致性：4 决策（100 条 / 完成后 prune / 删整目录+护栏 / 行内删+二次确认）全篇一致。
- [x] 范围：RUN-DEL-1 单一计划可覆盖。
- [x] 模糊性：删除范围（registry json + runs/<id>/ + logs/*_<id>.*）、护栏（实时 active_tasks → 409）、prune 只删终态，均已明确。

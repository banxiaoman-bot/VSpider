# 产物 Tab 按 run 分组 + 任务短名 设计

> brainstorming 产物（本文件不含运行代码）。已与用户确认 **3 项决策**：①产物 Tab 只展 `runs/<id>/artifacts/` + manifest（不再平铺全局副产物），按 run 分组；②组标题=规则提炼的任务短名（默认 ≤14 字，仅显示，物理文件名不动）+ 时间 + 状态；③只显示「有产物」的 run，默认最新展开其余折叠。实施按 writing-plans → TDD → 收口。

## 1. 背景与目标

### 痛点（实测）
- 现「产物」Tab 调 `GET /api/artifacts`，列的是**全局 `ARTIFACT_DIR` 平铺**（含 `capability/`、`api_replay/` 等技术副产物），文件名是 `类型_runid_时间戳.json`，**与运行记录不一一对应、看不懂、与任务无关**。
- 真正按任务的产物在 `runs/<run_id>/artifacts/` + `manifest.json`（与 run 一一对应），但只在「运行记录详情弹窗」里能看到。

### 目标
- 产物 Tab 改为**按 run 分组**：每个有产物的 run 一组，组标题=任务短名（规则提炼，中/英）+ 创建时间 + 状态；组内列该 run 的产物（文件名/kind/大小/下载）。
- 短名仅用于**显示**，物理文件名与落盘逻辑不变（零路径/撞名风险）。

### 非目标（YAGNI）
- 不改产物物理文件名 / 落盘逻辑。
- 不用 LLM 起名（纯规则提炼）。
- 不显示无产物的 run（空跑/纯问答）。
- capability/api_replay 副产物不再进产物 Tab（它们语义属「能力追踪」，本切片不动其落盘，仅产物 Tab 不再列全局目录）。

## 2. 范围

| slice | 内容 | 触碰层 | 风险 |
| --- | --- | --- | --- |
| ART-RUN-1 | 后端 `GET /api/run_artifacts`（聚合 list_runs + manifest + 短名）；前端产物 Tab 改 `ArtifactsByRun` 分组视图；测试 | api_server / run_registry(只读) / queue_core(只读复用) / App.vue / 新组件 / tests | 低-中（只读聚合，无写盘） |

## 3. 已定决策

| # | 决策 | 选择 |
| --- | --- | --- |
| 1 | 数据源 | 只 `runs/<id>/artifacts/` + manifest，按 run 分组（A） |
| 2 | 组标题 | 规则提炼短名（≤14 字，仅显示）+ 时间 + 状态（A） |
| 3 | 上列范围 | 只显有产物的 run；默认最新展开 |

## 4. 后端设计

### 4.1 任务短名提炼 `_derive_task_label(prompt, target_url, limit=14)`（api_server.py 内小函数）
规则（纯字符串，无模型）：
1. 取 `prompt` 去首尾空白；为空则回退 `target_url` 的 host；再空回退 `run_id`。
2. 去掉常见前缀虚词：`请`/`帮我`/`帮忙`/`麻烦`/`把`/`将`/`需要`/`我想`/`我要`（正则锚定开头，循环剥离）。
3. 按首个句读切分（`。！？\n，、；,.!?;` 任一），取第一分句。
4. 截断到 `limit` 字（超出加 `…`）。
5. 结果为空兜底用 `run_id`。

### 4.2 `GET /api/run_artifacts?limit=50`（api_server.py 直接定义，复用既有依赖）
- 调 `_run_registry.list_runs(limit=limit)` 拿 run 列表。
- 对每个 run 调 `_load_run_contract_bundle(run_id)` 取 manifest。
- 过滤：仅保留 `manifest.items` 非空的 run。
- 每个 run 产出：
  ```json
  {
    "run_id": "...", "label": "提取首页名言与作者", "status": "succeeded",
    "created_at": 1750000000.0, "target_url": "...", "goal": "<full prompt>",
    "artifacts": [
      {"filename": "data.xlsx", "rel": "data.xlsx", "kind": "dataset_rows",
       "size_kb": 12.3, "download_url": "/download/runs/<id>/artifacts/data.xlsx"}
    ]
  }
  ```
  - `rel`：manifest item 的 path 去掉 `runs/<id>/artifacts/` 前缀（与现 `RunRegistryPanel.artifactHref` 同算法）。
  - `download_url`：复用现有 `GET /download/runs/{id}/artifacts/{filename}` 端点。
  - `kind`/`size`：取自 manifest item（size→size_kb）。
- 返回 `{status:"success", count, runs:[...]}`，按 created_at 倒序。

> 不新增写盘、不改 manifest；纯只读聚合。manifest item 的 path 可能是绝对路径（见样本），用 `ARTIFACTS_DIRNAME` 拆分取相对段。

## 5. 前端设计

### 5.1 新组件 `vspider-ui/src/components/ArtifactsByRun.vue`
- 挂载 + `refreshToken` 变化时 `GET /api/run_artifacts?limit=50`。
- `el-collapse`（手风琴非互斥，默认 `v-model` 设为最新 run 的 run_id 展开）。
- 每组 header：`<状态 el-tag> 短名 · MM-DD HH:mm · N 个产物`；hover title=完整 goal + run_id。
- 组内 `el-table`（沿用 `artifact-table` + 深盒 + col-mono）：文件名 / kind / KB(col-mono) / 下载链接。
- 空态：「暂无带产物的运行」。
- 复用 `API_BASE` 拼下载链接（后端已给 download_url，直接 `API_BASE + download_url`）。

### 5.2 `App.vue`
- 产物 `el-tab-pane` 内容由原 `<el-table :data="artifactList">` 换成 `<ArtifactsByRun :refresh-token="runHistoryRefreshToken" />`（复用运行记录的刷新令牌）。
- 移除/保留 `useScreenshotArtifacts` 的 artifactList 取数：`hasNewArtifacts` 红点逻辑保留；artifactList 表格不再用于此 Tab（截图历史 `RunScreenshotHistory` 不受影响，它在右下角独立）。
  - 最小改动：仅替换 Tab 内渲染，不动 composable（artifactList 仍可被其它逻辑用）。

## 6. 落地映射

| 步 | 文件 | 改动 |
| --- | --- | --- |
| 1 后端短名 | `api_server.py` | `_derive_task_label` 小函数 |
| 2 后端端点 | `api_server.py` | `GET /api/run_artifacts` |
| 3 前端组件 | `vspider-ui/src/components/ArtifactsByRun.vue`（新增） | 分组视图 |
| 4 前端接线 | `vspider-ui/src/App.vue` | 产物 Tab 换组件 + import |
| 5 测试 | `tests/test_run_artifacts_api.py`（新增）+ 前端 source 断言 | 见 §7 |

## 7. 测试计划

后端 `tests/test_run_artifacts_api.py`（TestClient + monkeypatch run_registry.project_root→tmp）：
1. `_derive_task_label`：剥前缀虚词、取首分句、截断 ≤14+…、空→run_id 兜底、英文 goal 原样。
2. `GET /api/run_artifacts`：造 2 run（一个有 manifest items、一个空）→ 只返回有产物的；字段含 label/status/artifacts[].download_url。
3. download_url 指向既有 `/download/runs/{id}/artifacts/{file}` 形态。

前端（沿用 source-structural 风格 `test_frontend_component_split_y126.py`）：
4. 断言 `ArtifactsByRun.vue` 含 `/api/run_artifacts` + `el-collapse`；`App.vue` 引入并使用 `ArtifactsByRun`。

## 8. 风险
1. **manifest path 绝对/相对混用**：用 `ARTIFACTS_DIRNAME` 拆分取相对段，拆不出则用 basename 兜底。
2. **短名中文/特殊字符**：纯显示、不进路径，无 slug 风险；前端 `:title` 展示完整。
3. **run 很多时聚合慢**：limit=50 + 只读 json；与现 `/api/runs` 同量级，可接受。
4. **CRLF**：`api_server.py` StrReplace 失败走字节 patch。

## 9. 回滚
- 产物 Tab 换回 `<el-table :data="artifactList">`；删 `ArtifactsByRun.vue` + `/api/run_artifacts` + `_derive_task_label`。`/api/artifacts` 旧端点保留未删（其它处可能引用），零影响。

## 10. 自检
- [x] 占位符：无。
- [x] 一致性：3 决策（按 run 分组 / 规则短名仅显示 / 只显有产物）全篇一致。
- [x] 范围：ART-RUN-1 单一计划可覆盖。
- [x] 模糊性：短名规则、端点字段、download_url 复用、空 run 过滤均已明确。

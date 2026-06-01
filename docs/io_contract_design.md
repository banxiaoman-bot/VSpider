# VSpider I/O Contract v1 设计文档

> 落实 `.cursor/rules/vspider-mission.mdc` §一-A / §一-B。
> 这是 input + output 合一的契约层，不涉及具体能力实现。

## 0. 目标

把今天分散在 `target_url + prompt + file` 三个 form 字段、以及
`infer_goal_output_contract` 二元结果（answer / artifact）的输入输出，统一成两个
显式可序列化的契约对象：

- `input_contract.v1` —— 任何 run 启动前必须能落盘为 `runs/<id>/input_contract.json`
- `output_contract.v1` —— 任何 run 启动前必须能落盘为 `runs/<id>/output_contract.json`
- `manifest.v1` —— 实际产物清单，append-only，写到 `runs/<id>/manifest.json`

三者构成 run 的"输入→输出"完整契约，使任何 run 都可**纯凭这三个文件复现**。

---

## 1. `input_contract.v1`

### 1.1 字段

```jsonc
{
  "version": "input_contract.v1",
  "goal": "用户自然语言任务（唯一始终必填）",
  "urls": [
    {
      "url": "https://example.com",
      "role": "start | reference | dataset | api | unknown",
      "system_id": "auto | <user-defined>",
      "auth_profile": "auto | <profile_name>"
    }
  ],
  "attachments": [
    {
      "path": "temp_uploads/<sha256>.<ext>",
      "filename": "<原始文件名>",
      "mime": "application/pdf",
      "size": 12345,
      "sha256": "<hex>",
      "intent": "batch_rows | upload_to_page | prompt_context | media_source | unknown",
      "schema": { "columns": ["..."], "url_column": "auto | <name>" }
    }
  ],
  "auth_profiles": ["bilibili_default", "..."],
  "model_overrides": {
    "vlm":      { "base_url": "", "api_key": "", "model": "", "temperature": null, "max_tokens": null },
    "semantic": { "base_url": "", "api_key": "", "model": "" }
  },
  "constraints": {
    "max_runs": 0,
    "rate_limit_qps": 0,
    "allow_cross_system": true,
    "max_steps": 0
  },
  "source": "api | cli | replay | scheduler",
  "created_at": "<ISO 8601>"
}
```

### 1.2 校验与放宽

- `goal` 是唯一**强校验**字段。
- `urls` 缺失合法：planner 会从 `goal` 抽 URL 或建议默认入口（搜索引擎 / 站内搜索）。
- `urls[].url` 必校验：scheme ∈ {http,https,file}，host 非空。
- `attachments[i].intent` 缺省时由 `attachment_adapters` 按 magic bytes + mime + 列名 + goal 推断。
- `auth_profiles` 缺省时按 `urls[].url` 自动匹配 `.auth/*.json`。
- `model_overrides.*` 任意子键缺省都允许，回退到 `.env` 配置。

### 1.3 attachment intent 推断表

| 触发条件 | intent | 后续消费 |
|---|---|---|
| `.csv/.xlsx/.xls/.tsv/.parquet` 且 goal 含"按行/填报/批量/逐条" | `batch_rows` | smart_batch_runner，行展开为 sub-goal |
| 同上，**且有 URL 列** | `batch_rows + dataset_source` | URL 列并入 `urls[].role=start` |
| `.json/.jsonl/.yaml` 且是数组 | `batch_rows` | 同上 |
| `.txt/.md/.html/.json`（非数组） | `prompt_context` | 拼进 system message |
| `.pdf` 且 goal 含"上传/提交/附件" | `upload_to_page` | Playwright `set_input_files` |
| `.pdf` 且 goal 含"阅读/总结/抽取" | `prompt_context` | 抽文本 / OCR 后入 prompt |
| `.png/.jpg/.webp` 且 goal 含"上传/提交" | `upload_to_page` | 同上 |
| `.png/.jpg/.webp` 默认 | `prompt_context` | 多模态喂 VLM |
| `.mp4/.mp3/.wav/.zip/.7z/.rar` | `upload_to_page` 或 `media_source` | 默认不解包 |
| 未匹配 | `unknown` | 标记 + 默认 `upload_to_page` 兜底 |

### 1.4 cross_system 路由

- `urls[].system_id` 是一等公民。
- planner 看到多个不同 `system_id` 时，`execution_plan.systems` 必须**等于** `urls` 去重后的 system 列表。
- `route_executor` 按 `step.system_id` 切 `BrowserSession`，cookie/storage_state 不串。

---

## 2. `output_contract.v1`

### 2.1 字段

```jsonc
{
  "version": "output_contract.v1",
  "mode": "answer | artifact | mixed | default",
  "output_kind": "answer_text | dataset_rows | dataset_records | media_image | media_video | media_audio | media_pdf | media_archive | file_generic | html_snapshot | screenshot | code_or_text | mixed",
  "container": "inline_text | xlsx | csv | jsonl | json | files_folder | zip | html | markdown",
  "fields": ["title", "url", "..."],
  "post_process": ["dedup", "transcode", "ocr", "thumbnail"],
  "user_explicit": false,
  "reasons": ["goal_requests_pdf_download", "..."],
  "created_at": "<ISO 8601>"
}
```

### 2.2 推断顺序

1. **用户显式声明**：goal 含"存为 PDF / 保存图片 / 输出 markdown / 只回答" → `user_explicit=true`。
2. **被抓内容类型**：response `Content-Type` / URL 后缀 / 标签 → 覆盖 default。
3. **任务语义**：semantic LLM 兜底分类（默认走规则，无 LLM 时退化为正则）。
4. **都拿不准**：`mode=default / output_kind=mixed / container=files_folder`，并广播
   `"已抓到 X，未指定输出，请选择落盘形式"`。**绝不擅自塞 xlsx**。

### 2.3 output_kind → container 默认映射

| output_kind | container | 备注 |
|---|---|---|
| `answer_text` | `inline_text` | 只回 chat，不落盘 |
| `dataset_rows` | `xlsx`（<100 行）/ `jsonl`（≥100 行） | 同时写 xlsx 摘要 |
| `dataset_records` | `jsonl` | 嵌套结构 |
| `media_image` | `files_folder` | 配 `manifest.json`（url/alt/size/sha256） |
| `media_video` / `media_audio` | `files_folder` | 大文件 Range 续传，记 codec/duration |
| `media_pdf` | `files_folder` | 可选 OCR/抽文本作为副产物 |
| `media_archive` | `files_folder` | 原样落盘，不自动解压 |
| `file_generic` | `files_folder` | 未知后缀走通用下载 |
| `html_snapshot` | `html` | 单/多页快照 + selector fingerprint |
| `screenshot` | `files_folder` | 走 `browser_env.screenshot()` |
| `mixed` | `files_folder` | 必须写 `manifest.json` 索引 |

### 2.4 强约束

- `data_manager.py` / `data_export.py` **禁止默认 xlsx**，必须读 `container`。
- 抽取器**禁止**把媒体 URL 当 row 写进 xlsx 算交付，必须真下载走 `media_*` 分支。
- 新 action / capability 产出，必须在 `ActionResult.output_kind` + `output_path` 上声明，event_stream 必有对应 `verify` 事件。

---

## 3. `manifest.v1`

### 3.1 字段

```jsonc
{
  "version": "manifest.v1",
  "run_id": "<task_id>",
  "items": [
    {
      "kind": "media_image | media_video | media_pdf | dataset_rows | html_snapshot | screenshot | log | other",
      "path": "runs/<id>/artifacts/<sha256>.<ext>",
      "size": 102400,
      "sha256": "<hex>",
      "mime": "image/png",
      "source_url": "https://example.com/...",
      "produced_by": "<action_name>",
      "step_id": "step_03",
      "created_at": "<ISO 8601>",
      "extra": { "width": 1920, "height": 1080 }
    }
  ],
  "updated_at": "<ISO 8601>"
}
```

### 3.2 写入规则

- **append-only**：新增产物追加一条 `items`，从不覆盖；`updated_at` 跟着刷。
- **去重**：同一 `sha256` 不重复登记；不同 `source_url` 同内容只登一次，`source_url` 升级为 list。
- **原子写**：写新版本到临时文件再 `rename`，避免并发损坏。
- **事件流呼应**：每次追加同步发 `verify.output_complete` 事件到 `event_stream`。

---

## 4. `runs/<run_id>/` 目录契约

```
runs/<run_id>/
  input_contract.json       # 输入归一结果
  output_contract.json      # 输出推断结果
  manifest.json             # 产物总表（append-only）
  artifacts/                # 真实产物：图片/视频/pdf/xlsx/jsonl/html...
  html_trace.html
  event_stream.jsonl
  screenshots/
```

任何 run 的"输入 → 调度 → 产物"链路都可凭这一目录完整复现。

---

## 5. 兼容性策略

### 5.1 既有 form 入口 `/api/start_batch`

- `target_url` 字段**继续接受**，等价于 `urls=[{url, role=start, system_id=auto, auth_profile=auto}]`。
- 新增可选 `urls` 字段（JSON 字符串数组），存在时**覆盖** `target_url`。
- 旧 `file` 字段继续接受，等价于 `attachments=[{path,..., intent=auto}]`，intent 推断后再分发。
- 旧客户端零修改可继续工作。

### 5.2 既有 `infer_goal_output_contract`

- 返回字段**只增不删**：保留 `mode / answer_required / artifact_required / save_artifact / structured_rows / reasons`，新增 `output_kind / container / media_hint / post_process / user_explicit`。
- 旧调用方读不到新字段也不会炸。

### 5.3 既有 `run_agent(start_url=...)`

- 第一阶段继续接 `str`，等价 `[start_url]`；多 URL 由 `smart_batch_runner` 在外层串行 N 次。
- 第二阶段（S5 跨系统执行）再改为 `start_urls: list[str]`。

---

## 6. 落地切片

| 切片 | 范围 | 风险 | 状态 |
|---|---|---|---|
| **C** | 本文档 | 0 | doing |
| **A1** | `visual_web_agent/io_contract/` 新建：`input_contract.py` / `output_contract.py` / `manifest.py`（pure，无 IO） | 低 | next |
| **A2** | `tests/test_io_contract.py` 全 pure 单测 | 低 | next |
| **A3** | `infer_goal_output_contract` 向后兼容升级 + media 正则 | 低 | next |
| **A4** | `api_server.start_batch` 接受多 URL + 调用契约模块落盘 | 中（碰 130KB 文件） | next round |
| **B**  | `visual_web_agent/attachment_adapters/` 包：rows/text/image/pdf/archive/generic | 中 | next round |
| **S4** | `temp_uploads/` 改 sha256 内容寻址 + GC 守护 | 中 | follow-up |
| **S5** | 执行层 cross_system 真切 BrowserSession | 高 | 单独 backlog |

每个切片完成后跑 `python scripts/validate_y.py io_contract_<slice> --target-test tests/test_io_contract.py`。

---

## 7. 不在本期范围

- 真正的 `media_harvester` 能力实现（仅留契约和 stub adapter）
- `BrowserSessionPool` cross-system 调度（仅留 system_id 字段）
- `attachment_adapters` 的真实 PDF/图/视频解析（仅留 adapter 接口与 dispatch）
- 前端 `App.vue` 改造（先在前端**保持现状**，等契约稳定再改 UI）

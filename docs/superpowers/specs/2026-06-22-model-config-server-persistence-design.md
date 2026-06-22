# 模型配置服务端持久化（model_config.json）设计

> 面向 AI 工作者：本文是「前端模型配置服务端持久化」的**设计稿**（brainstorming 产物，本文件不含运行代码）。已与用户确认 4 项核心决策：**①方案 B（独立 `model_config.json`，不写 `.env`）；②触发=连接成功自动存；③apiKey 回填=脱敏（GET 永不回传明文）；④单套全局配置（YAGNI，不做多 profile）**。实施按 `writing-plans` 出计划 → TDD 逐步实现 → 收口。

## 1. 背景与目标

### 痛点（实测证据）
- 前端模型配置只持久化到浏览器 **localStorage**（`useModelSettings.js:64-77` `saveModelSettings`），且**不含 apiKey**（仅存 selectedModel / selectedSemanticModel / modelBaseUrl / semanticBaseUrl / temperature / maxTokens）。
- `useModelSettings.js:18-21` 的 `modelApiKey` / `semanticApiKey` 是裸 ref，无任何持久化；页面刷新后必须重填。
- 运行时 `useTaskForm.js:170-190` 把 url/key/model 作为**每次运行的 FormData** 发给 `/api/start_batch`；后端 `api_server.py:1144-1163` 构建 `vlm_options` 时把空值丢弃（`{k: v for ... if v not in ("", None)}`），空字段回退 `config.py:21-29` 的 `.env` 默认。
- 结果：用户「配一次、长期复用」的诉求无法满足——baseUrl/model 靠 localStorage 半持久，**apiKey 每次刷新都要重填**，且换浏览器/设备全丢。

### 目标
- 「连接」成功后**自动**把当前 VLM/语义模型配置（含 apiKey）持久化到**服务端** `model_config.json`。
- 下次打开页面自动回填（apiKey **脱敏**显示「已保存」占位，不回传明文）；不换模型即无需重配。
- 改了前端配置再次「连接」即覆盖。
- 运行优先级：**本次 run 表单显式值 > model_config.json > .env 默认**（沿用现有空值丢弃语义，最小改动）。

### 非目标（本设计不做）
- 不做多套 profile 切换（YAGNI；单套全局足够，用户明确「不换就一直用」）。
- 不改既有 localStorage 持久化（保留；二者并存，localStorage 仍管前端 UI 态回填 baseUrl/model/参数）。
- 不做公网鉴权（POST 沿用现有同源本地 API 现状；公网部署再单独加）。
- 不动 `.env` 文件本身（明确选了方案 B 而非 A）。

## 2. 范围

| slice | 内容 | 触碰层 | 风险 |
| --- | --- | --- | --- |
| MC-1（本设计主体） | 后端 `model_config_store.py` + `GET/POST /api/model_config` + start_batch 回退接线；前端 useModelSettings 自动存/回填 + apiKey 脱敏占位；测试 | api_server / 新模块 / useModelSettings / useTaskForm(只读现有) / App.vue(占位文案) / .gitignore / tests | 低-中 |

单一实现计划可覆盖；不拆子项。

## 3. 已定决策（与用户确认）

| # | 决策点 | 选择 | 备注 |
| --- | --- | --- | --- |
| 1 | 持久化载体 | 方案 B：独立 `model_config.json` | 不写 `.env`，避免热加载/写坏/并发覆盖 |
| 2 | 保存触发 | 连接成功自动存 | `fetchRemoteModels` 成功后 POST；选中模型变化时也同步 |
| 3 | apiKey 回填 | A 脱敏 | GET 只回掩码 + has_api_key；明文永不出服务端 |
| 4 | profile 套数 | 单套全局 | YAGNI |

## 4. 数据模型 · `model_config.json`

位置：项目根（与 `.env` 同级，便于发现）；写入 `.gitignore`。

```json
{
  "version": 1,
  "vlm": {
    "base_url": "",
    "api_key": "",
    "model": "",
    "model_type": "vl",
    "temperature": 0.1,
    "max_tokens": 4096
  },
  "semantic": {
    "base_url": "",
    "api_key": "",
    "model": ""
  },
  "updated_at": "ISO8601"
}
```

- 文件缺失 / 损坏 → 视为空配置（全回退 `.env`），不抛错。
- `api_key` 字段：保存时若前端传空，**保留旧值**（不清空）；显式传新值才覆盖。

## 5. 后端设计

### 5.1 新增模块 `model_config_store.py`（避免再灌 api_server.py 大文件）

| 函数 | 职责 |
| --- | --- |
| `load_model_config() -> dict` | 读 json；缺失/损坏返回空骨架；不抛错 |
| `save_model_config(payload: dict) -> dict` | 覆盖写；apiKey 空则保留旧值；写 `updated_at`；原子写（临时文件 + rename） |
| `masked_config() -> dict` | 读后把 `api_key` → `sk-****abcd` 掩码 + `has_api_key: bool`，删明文 |
| `resolve_vlm_options(form: dict) -> dict` | 合并优先级 `form > json > .env(config.py)`，输出与现有 vlm_options 同结构 |

### 5.2 新增端点（api_server.py）

- `GET /api/model_config` → 返回 `masked_config()`（供前端回填；apiKey 仅掩码）。
- `POST /api/model_config` → body 接收 `{vlm:{...}, semantic:{...}}`，调 `save_model_config`，返回 `masked_config()`。

### 5.3 改造 start_batch（api_server.py:1144-1163）

- 现状：`vlm_options` 由 Form 显式值构建后丢弃空值。
- 改造：丢弃空值**之后**，用 `model_config.json` 回退仍为空的字段（即 `resolve_vlm_options` 的中间层）；最终空字段继续由 `config.py` 的 `.env` 默认兜底（现状不变）。
- 净效果：优先级 `本次 run Form 显式值 > model_config.json > .env`。

## 6. 前端设计

### 6.1 `useModelSettings.js`
- `fetchRemoteModels`（@27-46）成功分支末尾 → 调 `POST /api/model_config` 自动存当前 base_url/api_key/选中模型/参数（VLM 与 semantic 各自独立，按调用方区分）。
- 新增对 `selectedModel` / `selectedSemanticModel` 的同步：选中变化时（模型在连接后才选）也 POST 一次（「选好即存」）。
- 新增 `loadServerModelConfig()`：组件挂载时 `GET /api/model_config` 回填 base_url/model/参数；apiKey 字段**不填明文**，仅当 `has_api_key=true` 时把输入框 placeholder 设为「已保存（留空沿用）」。
- localStorage 逻辑保留不动（二者并存）。

### 6.2 `App.vue`
- apiKey 输入框（@512）placeholder 动态化：已存 key → 「已保存（留空沿用）」；否则 → 「API Key」。
- 「连接」按钮（@513 / @536）行为不变（仍调 `fetchRemoteModels`），自动存逻辑在 composable 内完成。

### 6.3 run 提交（`useTaskForm.js:182-190`）
- 不改：apiKey 留空时本就不 append（@182 `if (modelApiKey.value.trim())`），后端自动用 json 里的 key。零冲突。

## 7. 安全
- `model_config.json` 写入 `.gitignore`（与 `.env` 同级防护）。
- apiKey 明文只存服务端文件 + 只在「保存方向」由前端传入；**GET 永不回传明文**（仅掩码 + has_api_key）。
- POST 沿用现有同源本地 API（不额外鉴权）；公网部署需另加鉴权——本切片不做，spec 留注。

## 8. 落地映射（精确文件）

| 步 | 文件 | 改动 |
| --- | --- | --- |
| 1 后端存储 | `model_config_store.py`（新增） | load/save/mask/resolve 四函数 + 原子写 |
| 2 后端端点 | `api_server.py`（@943 区附近加 2 个路由） | `GET/POST /api/model_config` |
| 3 后端回退 | `api_server.py:1144-1163` | vlm_options 丢空值后插一层 json 回退 |
| 4 前端存取 | `vspider-ui/src/composables/useModelSettings.js` | 连接成功 POST + 挂载 GET 回填 + apiKey 占位 |
| 5 前端占位 | `vspider-ui/src/App.vue:512` | apiKey placeholder 动态文案 |
| 6 忽略 | `.gitignore` | 加 `model_config.json` |
| 7 测试 | `tests/test_model_config_store.py`（新增）+ 扩展 `vspider-ui/tests/useModelSettings.test.js` | 见 §9 |

## 9. 测试计划
后端 `tests/test_model_config_store.py`：
1. load 缺失文件 → 空骨架不抛错。
2. save → load 往返一致；`updated_at` 写入。
3. save 时 apiKey 传空 → 保留旧值；传新值 → 覆盖。
4. `masked_config` → api_key 掩码 + has_api_key，无明文泄漏。
5. `resolve_vlm_options` 优先级：form > json > .env（三层各覆盖一次）。

前端 `vspider-ui/tests/useModelSettings.test.js`（扩展）：
6. `fetchRemoteModels` 成功后触发一次 `POST /api/model_config`。
7. 挂载调 GET 回填 base_url/model；apiKey 不填明文、has_api_key 时占位「已保存」。

## 10. 风险
1. **apiKey 明文落盘**：本就是用户诉求；防护=.gitignore + GET 脱敏 + 不公网暴露；spec 标注公网需加鉴权。
2. **自动存过于频繁**：连接成功 + 选模型变化各 POST 一次，频率可接受；如有抖动可加防抖（YAGNI，先不做）。
3. **并发写**：原子写（temp + rename）避免半写；单用户本地场景冲突概率低。
4. **行为兼容**：无 json 时行为=现状（全回退 .env / localStorage）；前端 GET 失败静默降级到现状，不阻断。
5. **CRLF 字节级 patch**：`api_server.py` 若为 CRLF 且需跨空行插入，按工程规范写 `_patch_mc1.py` 字节改写回，patch 完即删。

## 11. 回滚
- 删 `model_config.json` + 还原 `api_server.py:1144-1163` 那一处回退插层。
- 端点、前端存取、新模块可保留（无配置文件时行为=现状），低风险残留。

## 12. 自检（规格）
- [x] 占位符：无 TODO/待定。
- [x] 一致性：4 项决策（方案B/自动存/脱敏/单套）全篇一致；优先级 form>json>.env 在 §1/§4/§5.3 一致。
- [x] 范围：MC-1 单一实现计划可覆盖；多 profile 明确划为非目标。
- [x] 模糊性：apiKey 空值=保留旧值已明确；GET 脱敏=永不回传明文已明确；自动存触发点（连接成功 + 选模型变化）已明确。

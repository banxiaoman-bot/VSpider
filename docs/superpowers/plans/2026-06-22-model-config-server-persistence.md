# 模型配置服务端持久化 实现计划

> **面向 AI 代理的工作者：** 用 `executing-plans` 技能逐任务实现此计划；每个任务独立 commit。配套设计稿：`docs/superpowers/specs/2026-06-22-model-config-server-persistence-design.md`。

**目标：** 前端「连接」成功后把 VLM/语义模型配置（含 apiKey）持久化到服务端 `model_config.json`，下次自动回填（apiKey 脱敏）、不换模型免重配；运行优先级 `本次 run 表单显式值 > model_config.json > .env 默认`。
**架构：** 新增纯函数模块 `visual_web_agent/model_config_store.py`（load/save/mask/apply）；`api_server.py` 加 `GET/POST /api/model_config` 两个端点 + 在 start_batch 构建 `vlm_options` 后插一层 JSON 回退；前端 `useModelSettings.js` 加自动存/回填 + apiKey 脱敏占位，经 `useAppBootstrap` 在挂载时拉取回填。
**技术栈：** Python 3 / FastAPI / pytest（后端）；Vue 3 composable / Vitest（前端）。

---

## 文件结构

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `.gitignore` | 改 | 忽略 `model_config.json`（明文 apiKey 落盘，绝不入 git） |
| `visual_web_agent/model_config_store.py` | 新增 | 配置文件读写 / 脱敏 / 合并到 vlm_options（纯函数，路径可注入便于测试） |
| `tests/test_model_config_store.py` | 新增 | 单元测试：load/save/mask/apply 优先级 + 空 apiKey 保留旧值 |
| `api_server.py` | 改 | import store；加 `GET/POST /api/model_config`；start_batch:1163 后插 JSON 回退 |
| `tests/test_model_config_api.py` | 新增 | TestClient：POST→GET 脱敏往返 + start_batch JSON 回退 |
| `vspider-ui/src/composables/useModelSettings.js` | 改 | persistModelConfig / loadServerModelConfig / has_saved_key 标志 / fetchRemoteModels 加 kind |
| `vspider-ui/tests/useModelSettings.test.js` | 改 | 新增：连接成功触发 POST、GET 回填脱敏 |
| `vspider-ui/src/composables/useAppBootstrap.js` | 改 | runBootstrap 调 loadServerModelConfig |
| `vspider-ui/tests/useAppBootstrap.test.js` | 改 | 断言 loadServerModelConfig 被调一次 |
| `vspider-ui/src/App.vue` | 改 | apiKey placeholder 动态文案 + 接线 loadServerModelConfig/has_saved_key |
| `docs/vspider_architecture_backlog.md` | 改 | 记一行 Slice MC-1 |

---

## Task 1 — .gitignore 忽略配置文件

**文件：** `.gitignore`

**步骤 1.1：** 在 `.gitignore` 第 1 行 `.env` 之后加一行。

```
.env
model_config.json
__pycache__/
```

**验证：** `git check-ignore model_config.json` 输出 `model_config.json`（exit 0）。

**提交：** `git add .gitignore && git commit -m "chore: gitignore model_config.json (MC-1 plaintext secret never tracked)"`

---

## Task 2 — 后端存储模块（TDD）

### 步骤 2.1：写失败测试 `tests/test_model_config_store.py`

```python
"""Unit tests for visual_web_agent.model_config_store (MC-1).

Pins: file round-trip, blank api_key keeps old secret, masked_config never
leaks plaintext, and apply_to_vlm_options precedence (form override wins,
JSON fills the gaps, empties left for the downstream .env layer).
"""

from __future__ import annotations

import json

import pytest

from visual_web_agent import model_config_store as mcs


@pytest.fixture()
def cfg_path(tmp_path):
    return tmp_path / "model_config.json"


class TestLoad:
    def test_missing_file_returns_skeleton(self, cfg_path):
        cfg = mcs.load_model_config(cfg_path)
        assert cfg["version"] == 1
        assert cfg["vlm"]["base_url"] == ""
        assert cfg["semantic"]["model"] == ""

    def test_corrupt_file_returns_skeleton(self, cfg_path):
        cfg_path.write_text("{ not json", encoding="utf-8")
        cfg = mcs.load_model_config(cfg_path)
        assert cfg["vlm"]["api_key"] == ""


class TestSave:
    def test_round_trip(self, cfg_path):
        mcs.save_model_config(
            {"vlm": {"base_url": "https://v/v1", "api_key": "sk-abcd1234", "model": "qwen"}},
            cfg_path,
        )
        cfg = mcs.load_model_config(cfg_path)
        assert cfg["vlm"]["base_url"] == "https://v/v1"
        assert cfg["vlm"]["api_key"] == "sk-abcd1234"
        assert cfg["vlm"]["model"] == "qwen"
        assert cfg["updated_at"]  # ISO timestamp written

    def test_blank_api_key_keeps_old(self, cfg_path):
        mcs.save_model_config({"vlm": {"api_key": "sk-keepme9999"}}, cfg_path)
        mcs.save_model_config({"vlm": {"base_url": "https://v/v1", "api_key": ""}}, cfg_path)
        cfg = mcs.load_model_config(cfg_path)
        assert cfg["vlm"]["api_key"] == "sk-keepme9999"  # not wiped
        assert cfg["vlm"]["base_url"] == "https://v/v1"

    def test_new_api_key_overwrites(self, cfg_path):
        mcs.save_model_config({"vlm": {"api_key": "old"}}, cfg_path)
        mcs.save_model_config({"vlm": {"api_key": "sk-newkey5678"}}, cfg_path)
        assert mcs.load_model_config(cfg_path)["vlm"]["api_key"] == "sk-newkey5678"


class TestMasked:
    def test_masks_api_key_and_flags(self, cfg_path):
        mcs.save_model_config(
            {"vlm": {"api_key": "sk-abcd1234"}, "semantic": {"api_key": ""}},
            cfg_path,
        )
        masked = mcs.masked_config(cfg_path)
        assert masked["vlm"]["api_key"] == "sk-****1234"
        assert masked["vlm"]["has_api_key"] is True
        assert masked["semantic"]["api_key"] == ""
        assert masked["semantic"]["has_api_key"] is False
        # plaintext must never appear
        assert "sk-abcd1234" not in json.dumps(masked)


class TestApply:
    def test_form_override_wins(self, cfg_path):
        mcs.save_model_config({"vlm": {"model": "json-model", "base_url": "https://json/v1"}}, cfg_path)
        out = mcs.apply_to_vlm_options({"model": "form-model"}, cfg_path)
        assert out["model"] == "form-model"  # form wins
        assert out["base_url"] == "https://json/v1"  # gap filled from json

    def test_empty_options_filled_from_json(self, cfg_path):
        mcs.save_model_config(
            {"vlm": {"model": "m", "base_url": "https://j/v1", "api_key": "sk-k"},
             "semantic": {"model": "sm", "base_url": "https://s/v1"}},
            cfg_path,
        )
        out = mcs.apply_to_vlm_options({}, cfg_path)
        assert out["model"] == "m"
        assert out["api_key"] == "sk-k"
        assert out["semantic_model"] == "sm"
        assert out["semantic_base_url"] == "https://s/v1"

    def test_no_file_leaves_options_untouched(self, cfg_path):
        out = mcs.apply_to_vlm_options({"model": "x"}, cfg_path)
        assert out == {"model": "x"}  # nothing injected -> .env default applies downstream
```

### 步骤 2.2：跑测试确认失败

```
python -m pytest tests/test_model_config_store.py -q
```
预期：collection / import error（`ModuleNotFoundError: visual_web_agent.model_config_store`）。

### 步骤 2.3：实现 `visual_web_agent/model_config_store.py`

```python
"""Server-side persistence for the frontend model configuration (MC-1 / 方案 B).

Stores VLM + semantic settings (incl. API keys) in one JSON file so a user
configures once and reuses it across sessions/devices. Run-time precedence stays
``per-run form override > model_config.json > .env default`` -- the .env layer is
applied downstream by ``visual_web_agent.config`` and is intentionally NOT read
here, so this module has no import-time side effects and is trivially testable.

Security: ``api_key`` is stored as plaintext on the server only. It is NEVER
returned to the browser (the GET route goes through ``masked_config``). The file
is in ``.gitignore``.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Project root (this module lives in visual_web_agent/), next to .env.
_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "model_config.json"

_EMPTY: dict[str, Any] = {
    "version": 1,
    "vlm": {"base_url": "", "api_key": "", "model": "", "model_type": "vl",
            "temperature": 0.1, "max_tokens": 4096},
    "semantic": {"base_url": "", "api_key": "", "model": ""},
    "updated_at": "",
}

# vlm_options key -> (json section, json key). Only string fields participate in
# the fallback: model_type/temperature/max_tokens are always sent by the form,
# so injecting them here could wrongly shadow the .env defaults.
_OPTION_MAP = {
    "model": ("vlm", "model"),
    "base_url": ("vlm", "base_url"),
    "api_key": ("vlm", "api_key"),
    "semantic_model": ("semantic", "model"),
    "semantic_base_url": ("semantic", "base_url"),
    "semantic_api_key": ("semantic", "api_key"),
}


def _path(path: "str | os.PathLike[str] | None") -> Path:
    return Path(path) if path is not None else _DEFAULT_PATH


def _skeleton() -> dict[str, Any]:
    return json.loads(json.dumps(_EMPTY))  # deep copy


def load_model_config(path: "str | os.PathLike[str] | None" = None) -> dict[str, Any]:
    p = _path(path)
    if not p.exists():
        return _skeleton()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return _skeleton()
    if not isinstance(raw, dict):
        return _skeleton()
    merged = _skeleton()
    for section in ("vlm", "semantic"):
        sub = raw.get(section)
        if isinstance(sub, dict):
            for k in merged[section]:
                if k in sub:
                    merged[section][k] = sub[k]
    if isinstance(raw.get("updated_at"), str):
        merged["updated_at"] = raw["updated_at"]
    return merged


def _atomic_write(p: Path, data: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".model_config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_model_config(payload: dict[str, Any],
                      path: "str | os.PathLike[str] | None" = None) -> dict[str, Any]:
    current = load_model_config(path)
    for section in ("vlm", "semantic"):
        incoming = payload.get(section)
        if not isinstance(incoming, dict):
            continue
        for key in current[section]:
            if key not in incoming:
                continue
            val = incoming[key]
            # api_key: blank means "keep old" so the stored secret is not wiped
            # when the frontend re-saves without re-entering it.
            if key == "api_key" and (val is None or str(val).strip() == ""):
                continue
            current[section][key] = val
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(_path(path), current)
    return current


def _mask(secret: str) -> str:
    s = (secret or "").strip()
    if not s:
        return ""
    if len(s) <= 4:
        return "****"
    return f"{s[:3]}****{s[-4:]}"


def masked_config(path: "str | os.PathLike[str] | None" = None) -> dict[str, Any]:
    cfg = load_model_config(path)
    for section in ("vlm", "semantic"):
        key = cfg[section].get("api_key", "")
        cfg[section]["has_api_key"] = bool((key or "").strip())
        cfg[section]["api_key"] = _mask(key)
    return cfg


def apply_to_vlm_options(vlm_options: dict[str, Any],
                         path: "str | os.PathLike[str] | None" = None) -> dict[str, Any]:
    """Fill missing/empty vlm_options keys from model_config.json.

    Form-provided values already in ``vlm_options`` win. Keys still empty fall to
    the JSON store; anything still missing is left untouched for the downstream
    .env default layer.
    """
    cfg = load_model_config(path)
    merged = dict(vlm_options)
    for opt_key, (section, json_key) in _OPTION_MAP.items():
        if merged.get(opt_key) not in (None, ""):
            continue  # form override wins
        val = cfg.get(section, {}).get(json_key, "")
        if val in (None, ""):
            continue  # leave to .env default downstream
        merged[opt_key] = val
    return merged
```

### 步骤 2.4：跑测试确认通过

```
python -m pytest tests/test_model_config_store.py -q
```
预期：全绿。

### 步骤 2.5：提交

```
git add visual_web_agent/model_config_store.py tests/test_model_config_store.py
git commit -m "feat(MC-1): model_config_store — server-side model config (load/save/mask/apply)"
```

---

## Task 3 — 后端端点 + start_batch 回退（TDD）

### 步骤 3.1：写失败测试 `tests/test_model_config_api.py`

```python
"""TestClient coverage for the model-config endpoints + start_batch fallback (MC-1).

The store path is redirected to a tmp file so tests never touch the real
model_config.json. Workers/cooldown are stubbed (mirrors
tests/test_start_batch_url_optional.py).
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    import visual_web_agent.model_config_store as mcs
    monkeypatch.setattr(mcs, "_DEFAULT_PATH", tmp_path / "model_config.json")

    import api_server as api
    monkeypatch.setattr(api, "_start_queue_workers", lambda bt: ([], {}))
    monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": False, "in_cooldown": False})
    return TestClient(api.app)


class TestModelConfigEndpoints:
    def test_post_then_get_is_masked(self, client):
        r = client.post("/api/model_config", json={
            "vlm": {"base_url": "https://v/v1", "api_key": "sk-abcd1234", "model": "qwen"},
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "success"
        assert body["config"]["vlm"]["api_key"] == "sk-****1234"
        assert body["config"]["vlm"]["has_api_key"] is True
        assert "sk-abcd1234" not in r.text

        g = client.get("/api/model_config")
        assert g.status_code == 200, g.text
        gcfg = g.json()["config"]
        assert gcfg["vlm"]["base_url"] == "https://v/v1"
        assert gcfg["vlm"]["model"] == "qwen"
        assert "sk-abcd1234" not in g.text  # plaintext never leaves the server


class TestStartBatchFallback:
    def test_saved_model_used_when_form_omits_it(self, client):
        client.post("/api/model_config", json={"vlm": {"model": "saved-qwen", "base_url": "https://v/v1"}})
        resp = client.post("/api/start_batch", data={
            "prompt": "抓取 https://a.test/ 列表",
            "target_url": "https://a.test/",
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "success", body
        assert body["vlm_model"] == "saved-qwen"  # JSON fallback applied

    def test_form_model_overrides_saved(self, client):
        client.post("/api/model_config", json={"vlm": {"model": "saved-qwen"}})
        resp = client.post("/api/start_batch", data={
            "prompt": "抓取 https://a.test/ 列表",
            "target_url": "https://a.test/",
            "vlm_model": "form-deepseek",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["vlm_model"] == "form-deepseek"  # form wins
```

### 步骤 3.2：跑测试确认失败

```
python -m pytest tests/test_model_config_api.py -q
```
预期：404（端点不存在）→ `test_post_then_get_is_masked` 断言失败；`test_saved_model_used...` 得到空 vlm_model。

### 步骤 3.3：实现 api_server.py 改动

**3.3a** 在 import 区（`api_server.py` 约第 60 行，紧随其它 `from visual_web_agent import ... as ...`）加：

```python
from visual_web_agent import model_config_store as _model_config_store
```

**3.3b** 在 `vlm_options` 去空值之后（`api_server.py:1163` 那一行 `vlm_options = {k: v for k, v in vlm_options.items() if v not in ("", None)}` 紧后）插入：

```python
    # MC-1: fill still-missing model fields from server-side model_config.json.
    # Precedence: per-run form override > model_config.json > .env (downstream).
    vlm_options = _model_config_store.apply_to_vlm_options(vlm_options)
```

> CRLF 注意：`api_server.py` 若为 CRLF，直接 StrReplace 上面这处单点插入通常可命中；若报 "string not found"，按 `vspider-workflow §一` 写 `_patch_mc1.py` 字节级插入后即删。

**3.3c** 在 `stop_batch` 之前（`api_server.py:1316` `@app.post("/api/stop_batch"...)` 那行之上）新增两个路由：

```python
@app.get("/api/model_config", summary="读取已保存的模型配置（apiKey 脱敏）")
async def get_model_config() -> dict:
    return {"status": "success", "config": _model_config_store.masked_config()}


@app.post("/api/model_config", summary="保存模型配置（覆盖；apiKey 留空保留旧值）")
async def post_model_config(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
    _model_config_store.save_model_config(payload)
    return {"status": "success", "config": _model_config_store.masked_config()}
```

> `Body` 与 `Any` 均已在 `api_server.py` 顶部导入（`from fastapi import (... Body ...)`、`from typing import Any`），无需新增。

### 步骤 3.4：跑测试确认通过

```
python -m pytest tests/test_model_config_api.py tests/test_model_config_store.py -q
```
预期：全绿。

### 步骤 3.5：提交

```
git add api_server.py tests/test_model_config_api.py
git commit -m "feat(MC-1): /api/model_config GET/POST + start_batch json fallback"
```

---

## Task 4 — 前端 useModelSettings（TDD）

### 步骤 4.1：写失败测试（扩展 `vspider-ui/tests/useModelSettings.test.js`）

在文件顶部 import 区下方加 client mock（紧跟现有 `vi.mock('element-plus', ...)` 之后）：

```js
vi.mock('../src/api/client.js', () => ({
  apiFetch: vi.fn(),
}))
```

并 import 之：

```js
import { apiFetch } from '../src/api/client.js'
```

在文件末尾追加：

```js
describe('useModelSettings server persistence (MC-1)', () => {
  it('POSTs config to /api/model_config after a successful connect', async () => {
    apiFetch.mockResolvedValue({ ok: true, json: async () => ({ config: { vlm: { has_api_key: true } } }) })
    const ms = useModelSettings()
    ms.modelBaseUrl.value = 'https://v/v1'
    ms.modelApiKey.value = 'sk-abcd1234'
    const target = ref([])
    const loading = ref(false)
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ data: [{ id: 'qwen' }] }) })))
    await ms.fetchRemoteModels('https://v/v1', 'sk-abcd1234', target, loading, 'vlm')
    const calledModelConfig = apiFetch.mock.calls.some(c => c[0] === '/api/model_config' && c[1]?.method === 'POST')
    expect(calledModelConfig).toBe(true)
    expect(ms.vlmHasSavedKey.value).toBe(true)
  })

  it('loadServerModelConfig refills non-secret fields and sets has_saved_key, never the plaintext key', async () => {
    apiFetch.mockResolvedValue({ ok: true, json: async () => ({ config: {
      vlm: { base_url: 'https://v/v1', model: 'qwen', temperature: 0.3, max_tokens: 2048, api_key: 'sk-****1234', has_api_key: true },
      semantic: { base_url: 'https://s/v1', model: 'deepseek-chat', api_key: '', has_api_key: false },
    } }) })
    const ms = useModelSettings()
    await ms.loadServerModelConfig()
    expect(ms.modelBaseUrl.value).toBe('https://v/v1')
    expect(ms.selectedModel.value).toBe('qwen')
    expect(ms.modelTemperature.value).toBe(0.3)
    expect(ms.semanticBaseUrl.value).toBe('https://s/v1')
    expect(ms.vlmHasSavedKey.value).toBe(true)
    expect(ms.semanticHasSavedKey.value).toBe(false)
    expect(ms.modelApiKey.value).toBe('')  // masked key never refilled into the input
  })
})
```

### 步骤 4.2：跑测试确认失败

```
cd vspider-ui && npx vitest run tests/useModelSettings.test.js
```
预期：`vlmHasSavedKey` / `loadServerModelConfig` 未定义 → 失败。

### 步骤 4.3：实现 `vspider-ui/src/composables/useModelSettings.js`

**4.3a** 顶部 import 加 `nextTick` 与 `apiFetch`：

```js
import { ref, computed, watch, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { apiFetch } from '../api/client.js'
```

**4.3b** 在现有 ref 声明区（`semanticRemoteLoading` 之后）加两个标志 + 一个 hydrating 闭包变量：

```js
  const vlmHasSavedKey = ref(false)
  const semanticHasSavedKey = ref(false)
  let hydrating = false
```

**4.3c** 把 `fetchRemoteModels` 签名加 `kind`，并在成功分支末尾触发持久化（改 `ElMessage.success(...)` 那一行的后面）：

```js
  async function fetchRemoteModels (baseUrl, apiKey, targetRef, loadingRef, kind) {
    if (!baseUrl) { ElMessage.warning('请先填写 Base URL'); return }
    loadingRef.value = true
    try {
      const headers = { 'Content-Type': 'application/json' }
      if (apiKey) headers['Authorization'] = `Bearer ${apiKey}`
      const url = baseUrl.replace(/\/+$/, '') + '/models'
      const resp = await fetch(url, { headers })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const json = await resp.json()
      const models = (json.data || json.models || []).map(m => typeof m === 'string' ? m : m.id).filter(Boolean)
      if (!models.length) { ElMessage.warning('未返回可用模型'); return }
      targetRef.value = models
      ElMessage.success(`获取到 ${models.length} 个模型`)
      if (kind) await persistModelConfig(kind)
    } catch (err) {
      ElMessage.error(`连接失败: ${String(err)}`)
    } finally {
      loadingRef.value = false
    }
  }
```

**4.3d** 在 `fetchRemoteModels` 之后新增 `persistModelConfig` 与 `loadServerModelConfig`：

```js
  async function persistModelConfig (section) {
    try {
      const payload = {}
      if (!section || section === 'vlm') {
        payload.vlm = {
          base_url: modelBaseUrl.value.trim(),
          api_key: modelApiKey.value.trim(),
          model: selectedModel.value === 'backend-default' ? '' : selectedModel.value,
          model_type: selectedModelType.value,
          temperature: modelTemperature.value,
          max_tokens: modelMaxTokens.value,
        }
      }
      if (!section || section === 'semantic') {
        payload.semantic = {
          base_url: semanticBaseUrl.value.trim(),
          api_key: semanticApiKey.value.trim(),
          model: selectedSemanticModel.value === 'backend-default' ? '' : selectedSemanticModel.value,
        }
      }
      const resp = await apiFetch('/api/model_config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!resp || !resp.ok) return
      const json = await resp.json()
      const cfg = (json && json.config) || {}
      if (cfg.vlm) vlmHasSavedKey.value = !!cfg.vlm.has_api_key
      if (cfg.semantic) semanticHasSavedKey.value = !!cfg.semantic.has_api_key
    } catch (err) {
      console.warn('[settings] failed to persist server model config', err)
    }
  }

  async function loadServerModelConfig () {
    hydrating = true
    try {
      const resp = await apiFetch('/api/model_config')
      if (!resp || !resp.ok) return
      const json = await resp.json()
      const cfg = (json && json.config) || {}
      const vlm = cfg.vlm || {}
      const sem = cfg.semantic || {}
      if (vlm.base_url) modelBaseUrl.value = vlm.base_url
      if (vlm.model) { selectedModel.value = vlm.model; vlmRemoteModels.value = [vlm.model] }
      if (typeof vlm.temperature === 'number') modelTemperature.value = vlm.temperature
      if (typeof vlm.max_tokens === 'number') modelMaxTokens.value = vlm.max_tokens
      if (sem.base_url) semanticBaseUrl.value = sem.base_url
      if (sem.model) { selectedSemanticModel.value = sem.model; semanticRemoteModels.value = [sem.model] }
      vlmHasSavedKey.value = !!vlm.has_api_key
      semanticHasSavedKey.value = !!sem.has_api_key
    } catch (err) {
      console.warn('[settings] failed to load server model config', err)
    } finally {
      await nextTick()
      hydrating = false
    }
  }
```

**4.3e** 在现有 localStorage `watch`（`saveModelSettings` 那个）之后，加一个「选好即存」watch：

```js
  watch([selectedModel, selectedSemanticModel], ([m, s], [pm, ps]) => {
    if (hydrating) return
    if (m !== pm) persistModelConfig('vlm')
    if (s !== ps) persistModelConfig('semantic')
  })
```

**4.3f** 在 `return { ... }` 中追加导出：

```js
    vlmHasSavedKey,
    semanticHasSavedKey,
    persistModelConfig,
    loadServerModelConfig,
```

### 步骤 4.4：跑测试确认通过

```
cd vspider-ui && npx vitest run tests/useModelSettings.test.js
```
预期：新旧用例全绿（旧的 fetchRemoteModels 用例不传 kind → 不触发 persist，行为不变）。

### 步骤 4.5：提交

```
git add vspider-ui/src/composables/useModelSettings.js vspider-ui/tests/useModelSettings.test.js
git commit -m "feat(MC-1): useModelSettings auto-persist + server refill (masked key)"
```

---

## Task 5 — 前端接线（bootstrap + App.vue）（TDD）

### 步骤 5.1：写失败测试（扩展 `vspider-ui/tests/useAppBootstrap.test.js`）

`makeDeps` 的返回对象里加 `loadServerModelConfig: vi.fn(),`（在 `loadModelSettings: vi.fn(),` 下一行），并在 `'calls all init functions once'` 用例中追加断言：

```js
    expect(deps.loadServerModelConfig).toHaveBeenCalledOnce()
```

### 步骤 5.2：跑测试确认失败

```
cd vspider-ui && npx vitest run tests/useAppBootstrap.test.js
```
预期：`loadServerModelConfig` 未被调用 → 失败。

### 步骤 5.3a：实现 `vspider-ui/src/composables/useAppBootstrap.js`

`runBootstrap` 的解构加 `loadServerModelConfig`，并在 `loadModelSettings()` 后调用：

```js
  const {
    loadModelSettings,
    loadServerModelConfig,
    registerBuiltinCommands, slashRegistry, slashCommandDeps,
    connectWebSocket,
    loadAuthProfiles,
    loadCaptchaSolverStatus,
    fetchArtifacts,
    fetchBrowserRuntimeStatus,
    failedRunsPaneRef,
  } = deps

  loadModelSettings()
  loadServerModelConfig?.()
  registerBuiltinCommands(slashRegistry, slashCommandDeps)
```

### 步骤 5.3b：实现 `vspider-ui/src/App.vue` 接线

- 从 `useModelSettings()` 解构处（约 95 行 `loadModelSettings,` 附近）追加：

```js
  loadServerModelConfig,
  vlmHasSavedKey,
  semanticHasSavedKey,
```

- `useAppBootstrap({ ... })` 调用（约 383 行）在 `loadModelSettings,` 下一行加：

```js
  loadServerModelConfig,
```

- VLM apiKey 输入框（`App.vue:512`）`placeholder="API Key"` 改为动态：

```html
                <el-input v-model="modelApiKey" clearable show-password :disabled="isRunning" :placeholder="vlmHasSavedKey ? '已保存（留空沿用）' : 'API Key'" size="small" />
```

- 语义 apiKey 输入框（`App.vue:535`）同理：

```html
                <el-input v-model="semanticApiKey" clearable show-password :disabled="isRunning" :placeholder="semanticHasSavedKey ? '已保存（留空沿用）' : 'API Key'" size="small" />
```

- VLM「连接」按钮（`App.vue:513`）`fetchRemoteModels(...)` 末尾加 `'vlm'`：

```html
                <el-button size="small" :loading="vlmRemoteLoading" @click="fetchRemoteModels(modelBaseUrl, modelApiKey, vlmRemoteModels, vlmRemoteLoading, 'vlm')">连接</el-button>
```

- 语义「连接」按钮（`App.vue:536`）末尾加 `'semantic'`：

```html
                <el-button size="small" :loading="semanticRemoteLoading" @click="fetchRemoteModels(semanticBaseUrl, semanticApiKey, semanticRemoteModels, semanticRemoteLoading, 'semantic')">连接</el-button>
```

### 步骤 5.4：跑测试确认通过 + 构建

```
cd vspider-ui && npx vitest run tests/useAppBootstrap.test.js tests/useModelSettings.test.js && npm run build
```
预期：测试全绿；build 成功（App.vue 语法正确）。

### 步骤 5.5：提交

```
git add vspider-ui/src/composables/useAppBootstrap.js vspider-ui/src/App.vue vspider-ui/tests/useAppBootstrap.test.js
git commit -m "feat(MC-1): wire loadServerModelConfig on mount + masked apiKey placeholder"
```

---

## Task 6 — 收口验证 + backlog

### 步骤 6.1：后端核心 + 全量 pytest

```
python -m pytest tests/test_model_config_store.py tests/test_model_config_api.py -q
python -m pytest -q
```
预期：MC-1 用例全绿；全量无新增 fail。

### 步骤 6.2：backlog 记一行

在 `docs/vspider_architecture_backlog.md` 末尾 Slice 列表加：

```
- MC-1: 模型配置服务端持久化 — model_config_store + /api/model_config(GET/POST) + start_batch json 回退 + 前端连接成功自动存/挂载回填（apiKey 脱敏）。优先级 form>json>.env。
```

### 步骤 6.3：提交

```
git add docs/vspider_architecture_backlog.md
git commit -m "docs(MC-1): backlog slice for model config server persistence"
```

---

## 自检

**1. 规格覆盖度：**
- 数据模型 `model_config.json` → Task 2（store + _EMPTY 骨架）。
- 后端 store load/save/mask/resolve → Task 2（`apply_to_vlm_options` 即 spec 的 resolve）。
- `GET/POST /api/model_config` → Task 3。
- start_batch 优先级 form>json>.env → Task 3（3.3b 注入）+ test_model_config_api 两条断言。
- 前端连接成功自动存 → Task 4（fetchRemoteModels kind + persistModelConfig）。
- 挂载 GET 回填 + apiKey 脱敏占位 → Task 4（loadServerModelConfig）+ Task 5（bootstrap + App.vue placeholder）。
- 安全 .gitignore → Task 1。
- 测试（后端 store/api + 前端 settings/bootstrap）→ Task 2/3/4/5。
- 全部 spec 章节均有对应任务，无遗漏。

**2. 占位符扫描：** 无 TODO/待补；每个代码步骤含完整代码 + 精确命令 + 预期输出。

**3. 类型一致性：** `load_model_config / save_model_config / masked_config / apply_to_vlm_options / _DEFAULT_PATH / _OPTION_MAP` 跨 store 与 api/测试一致；前端 `persistModelConfig(section) / loadServerModelConfig / vlmHasSavedKey / semanticHasSavedKey / fetchRemoteModels(...,kind)` 跨 composable、App.vue、bootstrap、测试一致。

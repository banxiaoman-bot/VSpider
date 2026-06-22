# 产物 Tab 按 run 分组 + 任务短名 实现计划

> **面向 AI 代理的工作者：** 用 `.cursor/skills/executing-plans` 逐任务实现，每任务遵循 `.cursor/skills/test-driven-development`。收口走 `code-audit`。

**目标：** 产物 Tab 改为按 run 分组展示（组标题=规则提炼任务短名+时间+状态），只显有产物的 run，复用现有下载端点。
**架构：** 后端 `_derive_task_label` + `GET /api/run_artifacts`（只读聚合 list_runs + manifest）；前端新组件 `ArtifactsByRun.vue` 替换产物 Tab 内容。
**技术栈：** FastAPI + pytest/TestClient；Vue 3 + Element Plus + Vitest。
**关联规格：** `docs/superpowers/specs/2026-06-22-artifacts-by-run-design.md`。

---

## 关键 grounding（已核实）
- `_run_registry.list_runs(limit=...)` 返回 run 列表（含 `run_id/status/created_at/target_url/prompt`）。
- `_load_run_contract_bundle(run_id)`（api_server 已导入 @195）返回 `{manifest:{items:[...]}, ...}`；manifest item 含 `path/kind/size`。
- manifest item 的 `path` 可能是绝对路径（样本：`/media/.../runs/run_exec/artifacts/xxx.jsonl`）→ 用 `ARTIFACTS_DIRNAME="artifacts"` 拆相对段。
- 下载端点已存在：`GET /download/runs/{run_id}/artifacts/{filename:path}`（api_server.py:575）。
- 前端 `runHistoryRefreshToken`（App.vue:125）可作刷新令牌；产物表当前在 App.vue:761-779。
- 前端测试惯例：source-structural（`test_frontend_component_split_y126.py`），无组件 mount。

---

## 文件结构

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `api_server.py` | 改 | `_derive_task_label` + `GET /api/run_artifacts` |
| `vspider-ui/src/components/ArtifactsByRun.vue` | 新建 | 按 run 分组的产物视图 |
| `vspider-ui/src/App.vue` | 改 | 产物 Tab 内容换组件 + import |
| `tests/test_run_artifacts_api.py` | 新建 | 短名 + 端点测试 |

---

## Task 1 · 后端 `_derive_task_label` + `/api/run_artifacts`（TDD）

**文件：** `api_server.py`、`tests/test_run_artifacts_api.py`（新建）

### Step 1.1 · 写失败测试 `tests/test_run_artifacts_api.py`

```python
"""ART-RUN-1: /api/run_artifacts 按 run 聚合 + 任务短名。"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

import api_server as api
import visual_web_agent.run_registry as rr


class TestDeriveTaskLabel:
    def test_strips_leading_filler_and_truncates(self) -> None:
        f = api._derive_task_label
        assert f("请帮我提取首页所有名言的文字和对应作者", "", limit=14).startswith("提取首页")
        assert len(f("请帮我提取首页所有名言的文字和对应作者的详细信息啊啊啊", "", limit=14)) <= 15  # +省略号
        assert f("", "https://quotes.toscrape.com/path", limit=14) == "quotes.toscrape.com"
        assert f("", "", limit=14) == ""  # 空→空（端点再用 run_id 兜底）

    def test_takes_first_clause(self) -> None:
        f = api._derive_task_label
        assert f("抓取名言，然后导出excel", "", limit=20) == "抓取名言"

    def test_english_goal_kept(self) -> None:
        f = api._derive_task_label
        assert f("extract all quotes and authors", "", limit=20) == "extract all quotes"[:20] or \
               f("extract all quotes and authors", "", limit=20).startswith("extract")


class TestRunArtifactsEndpoint:
    @pytest.fixture()
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rr, "project_root", lambda: tmp_path)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        return TestClient(api.app, raise_server_exceptions=False)

    def _make_run_with_manifest(self, tmp_path: Path, rid: str, *, prompt: str, with_item: bool) -> None:
        rr.create_run(run_id=rid, target_url="https://e.test", prompt=prompt, status="running")
        rr.update_run(rid, status="succeeded")
        run_dir = tmp_path / "runs" / rid
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        if with_item:
            (run_dir / "artifacts" / "data.xlsx").write_text("x", encoding="utf-8")
            manifest = {
                "version": "manifest.v1", "run_id": rid,
                "items": [{
                    "kind": "dataset_rows",
                    "path": f"runs/{rid}/artifacts/data.xlsx",
                    "size": 2048, "produced_by": "extractor",
                }],
            }
            import json
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_returns_only_runs_with_artifacts(self, client, tmp_path, monkeypatch) -> None:
        # _load_run_contract_bundle reads io_contract default_runs_root; point it at tmp.
        import visual_web_agent.io_contract.persistence as iop
        monkeypatch.setattr(iop, "default_runs_root", lambda: tmp_path / "runs")
        self._make_run_with_manifest(tmp_path, "with_art", prompt="请帮我提取首页名言与作者", with_item=True)
        self._make_run_with_manifest(tmp_path, "no_art", prompt="只问个问题", with_item=False)
        body = client.get("/api/run_artifacts?limit=50").json()
        assert body["status"] == "success"
        ids = {g["run_id"] for g in body["runs"]}
        assert "with_art" in ids
        assert "no_art" not in ids
        grp = next(g for g in body["runs"] if g["run_id"] == "with_art")
        assert grp["label"].startswith("提取首页")
        assert grp["artifacts"][0]["download_url"] == "/download/runs/with_art/artifacts/data.xlsx"
        assert grp["artifacts"][0]["size_kb"] == 2.0
```

### Step 1.2 · 运行确认失败
```bash
.venv/bin/python -m pytest tests/test_run_artifacts_api.py -q
```
预期：`AttributeError: module 'api_server' has no attribute '_derive_task_label'`（红）。

### Step 1.3 · 实现（api_server.py）

加短名函数（放在 `_harvest_urls_from_text` 等工具函数区，或 `_task_snapshot` 附近）：

```python
import re as _re_label  # 若文件已 import re，可直接用 re

_LABEL_FILLERS = ("请", "帮我", "帮忙", "麻烦", "把", "将", "需要", "我想", "我要", "请帮我")


def _derive_task_label(prompt: str, target_url: str = "", limit: int = 14) -> str:
    """Rule-based short task label for display only (no model, no path use)."""
    text = str(prompt or "").strip()
    if not text:
        host = ""
        try:
            from urllib.parse import urlparse
            host = urlparse(str(target_url or "")).netloc
        except Exception:
            host = ""
        return host
    # 循环剥离开头虚词
    changed = True
    while changed:
        changed = False
        for f in sorted(_LABEL_FILLERS, key=len, reverse=True):
            if text.startswith(f):
                text = text[len(f):].lstrip()
                changed = True
    # 取首分句
    parts = re.split(r"[。！？\n，、；,.!?;]", text, maxsplit=1)
    head = (parts[0] if parts else text).strip()
    if not head:
        head = text.strip()
    if len(head) > limit:
        head = head[:limit] + "…"
    return head
```

加端点（在 `/api/artifacts` 端点之后，约 api_server.py:688 后）：

```python
@app.get("/api/run_artifacts", summary="按 run 分组列出任务产物（ART-RUN-1）")
async def get_run_artifacts(limit: int = 50) -> dict:
    from visual_web_agent.io_contract.persistence import ARTIFACTS_DIRNAME
    try:
        runs = _run_registry.list_runs(limit=limit)
    except Exception as exc:
        logger.warning("[RUN ARTIFACTS] list_runs failed: %s", exc)
        raise HTTPException(status_code=500, detail="run registry read failed") from exc
    groups: list[dict[str, Any]] = []
    for rec in runs:
        rid = str(rec.get("run_id") or "")
        if not rid:
            continue
        bundle = _load_run_contract_bundle(rid)
        manifest = bundle.get("manifest") or {}
        items = manifest.get("items") if isinstance(manifest, dict) else None
        if not items:
            continue
        arts: list[dict[str, Any]] = []
        for it in items:
            raw_path = str(it.get("path") or "").replace("\\", "/")
            marker = f"/{ARTIFACTS_DIRNAME}/"
            rel = raw_path.split(marker, 1)[1] if marker in raw_path else raw_path.rsplit("/", 1)[-1]
            if not rel:
                continue
            size = it.get("size")
            arts.append({
                "filename": rel.rsplit("/", 1)[-1],
                "rel": rel,
                "kind": str(it.get("kind") or "other"),
                "size_kb": round(float(size) / 1024, 2) if isinstance(size, (int, float)) else None,
                "produced_by": str(it.get("produced_by") or ""),
                "download_url": f"/download/runs/{rid}/artifacts/{rel}",
            })
        if not arts:
            continue
        label = _derive_task_label(str(rec.get("prompt") or ""), str(rec.get("target_url") or "")) or rid
        groups.append({
            "run_id": rid,
            "label": label,
            "status": str(rec.get("status") or ""),
            "created_at": rec.get("created_at"),
            "target_url": str(rec.get("target_url") or ""),
            "goal": str(rec.get("prompt") or ""),
            "artifacts": arts,
        })
    groups.sort(key=lambda g: float(g.get("created_at") or 0), reverse=True)
    return {"status": "success", "count": len(groups), "runs": groups}
```

> 确认文件顶部已 `import re` 与 `from fastapi import HTTPException`（runs_api 用过 HTTPException；api_server 若无则在端点内 `from fastapi import HTTPException`）。CRLF 失败走字节 patch。

### Step 1.4 · 运行确认通过
```bash
.venv/bin/python -m pytest tests/test_run_artifacts_api.py -q
```
预期：全绿。

### Step 1.5 · Commit
```bash
git add api_server.py tests/test_run_artifacts_api.py
git commit -m "feat(api): GET /api/run_artifacts grouped by run + rule-based task label"
```

---

## Task 2 · 前端 `ArtifactsByRun.vue` + 接线（TDD-source）

**文件：** `vspider-ui/src/components/ArtifactsByRun.vue`（新建）、`vspider-ui/src/App.vue`、`tests/test_run_artifacts_api.py`（追加 source 断言）

### Step 2.1 · 追加前端 source 断言
在 `tests/test_run_artifacts_api.py` 末尾追加：

```python
class TestArtifactsByRunWiring:
    def test_component_and_app_wiring(self) -> None:
        comp = Path("vspider-ui/src/components/ArtifactsByRun.vue").read_text(encoding="utf-8")
        assert "/api/run_artifacts" in comp
        assert "el-collapse" in comp
        app = Path("vspider-ui/src/App.vue").read_text(encoding="utf-8")
        assert "ArtifactsByRun" in app
```
（文件顶部已 `from pathlib import Path`。）

### Step 2.2 · 运行确认失败
```bash
.venv/bin/python -m pytest tests/test_run_artifacts_api.py::TestArtifactsByRunWiring -q
```
预期：红（组件不存在）。

### Step 2.3 · 新建 `ArtifactsByRun.vue`

```vue
<script setup>
import { onMounted, ref, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { API_BASE, apiFetch } from '../api/client.js'

const props = defineProps({ refreshToken: { type: Number, default: 0 } })

const groups = ref([])
const loading = ref(false)
const activeNames = ref([])

const fetchGroups = async () => {
  if (loading.value) return
  loading.value = true
  try {
    const resp = await apiFetch('/api/run_artifacts?limit=50')
    const result = await resp.json()
    if (!resp.ok || result.status !== 'success') throw new Error(result.detail || 'load failed')
    groups.value = Array.isArray(result.runs) ? result.runs : []
    if (groups.value.length) activeNames.value = [groups.value[0].run_id]
  } catch (err) {
    console.warn('[run_artifacts] fetch failed:', err)
  } finally {
    loading.value = false
  }
}

const formatTime = (ts) => {
  if (!Number.isFinite(ts)) return '-'
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
const statusTagType = (s) => {
  const v = String(s || '').toLowerCase()
  if (v === 'succeeded') return 'success'
  if (v === 'failed' || v === 'error') return 'danger'
  if (v === 'stopped' || v === 'paused') return 'warning'
  if (v === 'running' || v === 'queued') return 'primary'
  return 'info'
}

onMounted(fetchGroups)
watch(() => props.refreshToken, fetchGroups)
</script>

<template>
  <div class="artifacts-by-run">
    <div class="abr-toolbar">
      <el-button size="small" plain :icon="Refresh" :loading="loading" @click="fetchGroups">刷新</el-button>
      <span class="abr-count">{{ groups.length }} 个任务</span>
    </div>
    <el-collapse v-if="groups.length" v-model="activeNames" class="abr-collapse">
      <el-collapse-item v-for="g in groups" :key="g.run_id" :name="g.run_id">
        <template #title>
          <span class="abr-group-title" :title="`${g.goal || ''}\n${g.run_id}`">
            <el-tag size="small" :type="statusTagType(g.status)">{{ g.status || 'unknown' }}</el-tag>
            <span class="abr-label">{{ g.label || g.run_id }}</span>
            <span class="abr-meta">· {{ formatTime(g.created_at) }} · {{ g.artifacts.length }} 个产物</span>
          </span>
        </template>
        <el-table :data="g.artifacts" class="artifact-table" header-cell-class-name="dark-table-header" empty-text="无产物">
          <el-table-column prop="filename" label="文件" show-overflow-tooltip />
          <el-table-column prop="kind" label="类型" width="140" />
          <el-table-column prop="size_kb" label="KB" width="80" class-name="col-mono">
            <template #default="scope">{{ scope.row.size_kb ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="" width="72">
            <template #default="scope">
              <a :href="`${API_BASE}${scope.row.download_url}`" download class="download-link">下载</a>
            </template>
          </el-table-column>
        </el-table>
      </el-collapse-item>
    </el-collapse>
    <p v-else class="abr-empty">暂无带产物的运行</p>
  </div>
</template>

<style scoped>
.artifacts-by-run { display: flex; flex-direction: column; height: 100%; min-height: 0; gap: 8px; }
.abr-toolbar { display: flex; align-items: center; gap: 8px; }
.abr-count { color: var(--vsp-text-faint); font-size: 12px; }
.abr-collapse { flex: 1; min-height: 0; overflow-y: auto; }
.abr-group-title { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }
.abr-label { color: var(--vsp-text-strong); font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 360px; }
.abr-meta { color: var(--vsp-text-muted); font-size: 12px; }
.abr-empty { padding: 28px 14px; text-align: center; color: var(--vsp-text-muted); }
.download-link { color: var(--vsp-cyan-600); font-weight: 700; text-decoration: none; }
.download-link:hover { color: var(--vsp-cyan-700); }
</style>
```

### Step 2.4 · `App.vue` 接线

(a) import 区（与其它组件 import 同处，如 `RunScreenshotHistory` import 旁）加：
```javascript
import ArtifactsByRun from './components/ArtifactsByRun.vue'
```

(b) 产物 `el-tab-pane`（App.vue:761-779 的 `artifacts-box` + el-table 整块）替换为：
```html
            <div class="artifacts-box">
              <ArtifactsByRun :refresh-token="runHistoryRefreshToken" />
            </div>
```
> 即把原 `<el-table :data="artifactList" ...>...</el-table>` 整块换成 `<ArtifactsByRun>`。`artifacts-box` 深盒外壳保留（填满+深盒一致）。`artifactList` / `useScreenshotArtifacts` 取数与 `hasNewArtifacts` 红点逻辑**不动**（仅该 Tab 渲染改变）。

### Step 2.5 · 验证
```bash
.venv/bin/python -m pytest tests/test_run_artifacts_api.py -q
cd vspider-ui && npx vitest run && npm run build
```
预期：pytest 全绿；vitest 全量 passed；build 成功。

### Step 2.6 · Commit
```bash
git add vspider-ui/src/components/ArtifactsByRun.vue vspider-ui/src/App.vue tests/test_run_artifacts_api.py
git commit -m "feat(ui): artifacts tab grouped by run with task labels (ArtifactsByRun)"
```

---

## Task 3 · 收口验证

### Step 3.1 · 后端定向 + collect
```bash
.venv/bin/python -m pytest tests/test_run_artifacts_api.py tests/test_run_registry.py -q
.venv/bin/python -m pytest tests/ --co -q
```
### Step 3.2 · 前端
```bash
cd vspider-ui && npx vitest run && npm run build
```
### Step 3.3 · code-audit
核：①`_derive_task_label` 仅显示、不进路径；②`/api/run_artifacts` 只读、只返回有产物的 run、download_url 复用既有端点；③manifest path 绝对/相对都能拆相对段；④前端 `artifactList`/红点逻辑未破坏；⑤ReadLints 0 错误。

---

## 规格覆盖度自检

| spec 需求 | 任务 |
| --- | --- |
| §4.1 短名规则 | Task 1 |
| §4.2 /api/run_artifacts | Task 1 |
| §5.1 ArtifactsByRun 组件 | Task 2 |
| §5.2 App.vue 接线 | Task 2 |
| §7 测试 | Task 1/2 |

**占位符扫描：** 无。
**类型一致性：** `_derive_task_label`/`get_run_artifacts`/`ArtifactsByRun`/`refreshToken` 跨任务一致。
**偏离说明：** 前端 source-structural 断言（无 mount 基建）；核心聚合风险由后端测试覆盖。

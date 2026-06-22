# 运行记录删除 + 保留策略 实现计划

> **面向 AI 代理的工作者：** 使用 `.cursor/skills/executing-plans` 逐任务实现，每任务遵循 `.cursor/skills/test-driven-development`（先写失败测试 → 跑红 → 最小实现 → 跑绿 → commit）。收口走 `code-audit` + `test-engineering`（涉删文件，核心链路）。

**目标：** 运行记录可逐条删除（连带产物/日志、二次确认、活动 run 禁删）+ 每次 run 完成后自动保留最近 100 条。
**架构：** `run_registry` 加 `delete_run`/`prune_runs`（纯文件级，路径安全）；`DELETE /api/runs/{id}` 端点（活动任务护栏 409）；`api_server` 在 `complete_run` 后 prune；`RunRegistryPanel` 行内删除按钮 + `el-popconfirm`。
**技术栈：** FastAPI + pytest/TestClient；Vue 3 + Element Plus + Vitest。
**关联规格：** `docs/superpowers/specs/2026-06-22-run-delete-retention-design.md`。

---

## 关键 grounding（已核实）
- 记录：`runs/registry/<id>.json`（`run_registry.py`，模块级函数）；产物：`runs/<id>/artifacts/`；日志：`logs/run_log_<id>.html` / `logs/phase_<id>.jsonl` / `logs/event_stream_<id>.jsonl`。
- `run_registry` 已有 `_safe_run_id`（正则 `^[0-9A-Za-z_-]+$` 阻断 `.`/`..`）、`registry_root(base_dir)`、`list_runs`、`load_run`、`_TERMINAL_STATUS={"succeeded","failed","stopped","error"}`。
- `complete_run` 在 `api_server.py:299` 的 `_run_batch_task` 完成路径调用。
- 活动任务：`api_server.active_tasks["current_task"]`（含 `task_id`）+ `_task_snapshot()`（含 `running`）。
- 前端列表：`RunRegistryPanel.vue`「操作」列（当前仅「详情」，宽 92）。

## 文件结构

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `visual_web_agent/run_registry.py` | 改 | `import shutil` + `delete_run` + `prune_runs` |
| `api_routes/runs_api.py` | 改 | `register_runs_routes` 加 `is_run_active` 形参 + `DELETE /api/runs/{id}` |
| `api_server.py` | 改 | `_is_active_run` + `RUN_RETENTION_KEEP` + 传 `is_run_active` + `complete_run` 后 `prune_runs` |
| `vspider-ui/src/components/RunRegistryPanel.vue` | 改 | 行内删除 + popconfirm + `deleteRun` + ElMessage |
| `tests/test_run_delete_retention.py` | 新建 | delete_run/prune 单测 + DELETE 端点 404/409/200 |

---

## Task 1 · `run_registry` delete_run + prune_runs（TDD）

**文件：** `visual_web_agent/run_registry.py`、`tests/test_run_delete_retention.py`（新建）

### Step 1.1 · 写失败测试（先建测试文件，仅 registry 部分）
新建 `tests/test_run_delete_retention.py`：

```python
"""RUN-DEL-1: run 删除 + 保留策略测试。

monkeypatch run_registry.project_root → tmp_path，使 registry / runs/<id>/ / logs
全部落在 tmp，隔离真实仓库。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import visual_web_agent.run_registry as rr


@pytest.fixture()
def tmp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(rr, "project_root", lambda: tmp_path)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _make_run(rid: str, *, status: str, created_at: float) -> None:
    rec = rr.create_run(run_id=rid, target_url="https://e.test", prompt="p", status="running")
    rr.update_run(rid, status=status, extra={"created_at": created_at})
    # 产物目录 + 日志文件
    art = rr.project_root() / "runs" / rid / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    (art / "a.txt").write_text("x", encoding="utf-8")
    (rr.project_root() / "logs" / f"run_log_{rid}.html").write_text("<html>", encoding="utf-8")


class TestDeleteRun:
    def test_removes_record_dir_and_logs(self, tmp_root: Path) -> None:
        _make_run("r1", status="succeeded", created_at=1.0)
        assert (tmp_root / "runs" / "registry" / "r1.json").exists()
        assert (tmp_root / "runs" / "r1").is_dir()
        assert (tmp_root / "logs" / "run_log_r1.html").exists()
        assert rr.delete_run("r1") is True
        assert not (tmp_root / "runs" / "registry" / "r1.json").exists()
        assert not (tmp_root / "runs" / "r1").exists()
        assert not (tmp_root / "logs" / "run_log_r1.html").exists()

    def test_missing_returns_false(self, tmp_root: Path) -> None:
        assert rr.delete_run("nope") is False

    def test_invalid_id_raises(self, tmp_root: Path) -> None:
        with pytest.raises(ValueError):
            rr.delete_run("../etc/passwd")


class TestPruneRuns:
    def test_keeps_newest_terminal_deletes_old_terminal(self, tmp_root: Path) -> None:
        for i in range(5):
            _make_run(f"t{i}", status="succeeded", created_at=float(i))  # t4 newest
        deleted = rr.prune_runs(keep=2)
        remaining = {r["run_id"] for r in rr.list_runs(limit=500)}
        assert remaining == {"t4", "t3"}            # newest 2 kept
        assert set(deleted) == {"t0", "t1", "t2"}

    def test_never_deletes_non_terminal(self, tmp_root: Path) -> None:
        _make_run("old_run", status="running", created_at=0.0)   # oldest but running
        for i in range(3):
            _make_run(f"d{i}", status="succeeded", created_at=float(i + 1))
        rr.prune_runs(keep=1)
        remaining = {r["run_id"] for r in rr.list_runs(limit=500)}
        assert "old_run" in remaining               # running never pruned
        assert "d2" in remaining                     # newest terminal kept
```

### Step 1.2 · 运行确认失败
```bash
.venv/bin/python -m pytest tests/test_run_delete_retention.py -q
```
预期：`AttributeError: module 'visual_web_agent.run_registry' has no attribute 'delete_run'`（红）。

### Step 1.3 · 实现 `delete_run` + `prune_runs`

在 `run_registry.py` 顶部 import 区加 `import shutil`（与现有 `import os` 同段）。

在文件末尾（`list_runs` 之后）追加：

```python
def delete_run(run_id: str, *, base_dir: str | Path | None = None) -> bool:
    """Delete a run's registry record + ``runs/<id>/`` dir + ``logs/*_<id>.*``.

    Pure file removal — NO status guard (callers enforce active-run protection).
    Returns True iff the registry record existed. Raises ValueError on bad id.
    """
    rid = _safe_run_id(run_id)
    reg = registry_root(base_dir)          # .../runs/registry
    runs_root = reg.parent                  # .../runs
    proj = runs_root.parent                 # project root (or tmp in tests)
    reg_path = reg / f"{rid}.json"
    existed = reg_path.exists()
    reg_path.unlink(missing_ok=True)
    run_dir = runs_root / rid
    if run_dir.is_dir():
        shutil.rmtree(run_dir, ignore_errors=True)
    for rel in (f"run_log_{rid}.html", f"phase_{rid}.jsonl", f"event_stream_{rid}.jsonl"):
        try:
            (proj / "logs" / rel).unlink(missing_ok=True)
        except OSError:
            pass
    return existed


def prune_runs(*, keep: int = 100, base_dir: str | Path | None = None) -> list[str]:
    """Keep newest *keep* runs by created_at; delete older **terminal** ones.

    Never deletes non-terminal (running/queued/paused) runs even if old.
    Returns deleted run_ids.
    """
    try:
        k = max(0, int(keep))
    except (TypeError, ValueError):
        k = 100
    recs: list[dict[str, Any]] = []
    for path in registry_root(base_dir).glob("*.json"):
        if not path.is_file():
            continue
        rec = load_run(path.stem, base_dir)
        if rec:
            recs.append(rec)
    recs.sort(key=lambda r: float(r.get("created_at") or 0), reverse=True)
    deleted: list[str] = []
    for rec in recs[k:]:
        if rec.get("status") in _TERMINAL_STATUS:
            rid = str(rec.get("run_id") or rec.get("task_id") or "")
            try:
                if rid and delete_run(rid, base_dir=base_dir):
                    deleted.append(rid)
            except ValueError:
                continue
    return deleted
```

### Step 1.4 · 运行确认通过
```bash
.venv/bin/python -m pytest tests/test_run_delete_retention.py -q
```
预期：5 passed。

### Step 1.5 · Commit
```bash
git add visual_web_agent/run_registry.py tests/test_run_delete_retention.py
git commit -m "feat(run-registry): add delete_run + prune_runs (count-based retention)"
```

---

## Task 2 · `DELETE /api/runs/{id}` 端点 + 护栏 + prune 接线（TDD）

**文件：** `api_routes/runs_api.py`、`api_server.py`、`tests/test_run_delete_retention.py`（追加端点测试）

### Step 2.1 · 追加端点失败测试
在 `tests/test_run_delete_retention.py` 末尾追加：

```python
class TestDeleteEndpoint:
    @pytest.fixture()
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from starlette.testclient import TestClient
        import api_server
        # 让 run_registry 落在 tmp（api_server 以 _run_registry 引用同模块）
        monkeypatch.setattr(rr, "project_root", lambda: tmp_path)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        return TestClient(api_server.app, raise_server_exceptions=False), api_server

    def _make_terminal(self, rid: str) -> None:
        rr.create_run(run_id=rid, target_url="https://e.test", prompt="p", status="running")
        rr.update_run(rid, status="succeeded")

    def test_delete_terminal_run_200(self, client) -> None:
        c, api = client
        import api_server
        monkeypatch_running = None
        api.active_tasks.pop("current_task", None)
        self._make_terminal("e1")
        resp = c.request("DELETE", "/api/runs/e1")
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] == "e1"
        assert rr.load_run("e1") is None

    def test_delete_missing_404(self, client) -> None:
        c, api = client
        api.active_tasks.pop("current_task", None)
        assert c.request("DELETE", "/api/runs/ghost").status_code == 404

    def test_delete_active_run_409(self, client, monkeypatch: pytest.MonkeyPatch) -> None:
        c, api = client
        self._make_terminal("act1")
        monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": True, "in_cooldown": False})
        monkeypatch.setitem(api.active_tasks, "current_task", {"task_id": "act1"})
        resp = c.request("DELETE", "/api/runs/act1")
        assert resp.status_code == 409, resp.text
        assert rr.load_run("act1") is not None   # not deleted
```

### Step 2.2 · 运行确认失败
```bash
.venv/bin/python -m pytest tests/test_run_delete_retention.py::TestDeleteEndpoint -q
```
预期：DELETE 未注册 → 405/404，断言失败（红）。

### Step 2.3a · `api_routes/runs_api.py` 加端点 + 形参

`register_runs_routes` 函数签名追加形参 `is_run_active: Callable[[str], bool]`（放在 `start_queue_workers` 之后）：

```python
    retry_run_as_queued_task: Callable,
    start_queue_workers: Callable,
    is_run_active: Callable,
) -> None:
```

在 `get_run`（`@app.get("/api/runs/{run_id}")`）之后追加：

```python
    @app.delete("/api/runs/{run_id}", summary="删除运行记录及其产物（RUN-DEL-1）")
    async def delete_run_record(run_id: str) -> dict:
        rec = run_registry.load_run(run_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="run not found")
        if is_run_active(run_id):
            raise HTTPException(status_code=409, detail="cannot delete a running run")
        try:
            run_registry.delete_run(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid run_id") from exc
        return {"status": "success", "deleted": run_id}
```

### Step 2.3b · `api_server.py` 接线

1. 加模块常量（与其它 `os.getenv` 常量同区，如文件上部配置段）：
```python
RUN_RETENTION_KEEP = int(os.getenv("VSPIDER_RUN_RETENTION_KEEP", "100"))
```

2. 加活动判定函数（`_task_snapshot` 定义之后）：
```python
def _is_active_run(run_id: str) -> bool:
    """True iff *run_id* is the currently-executing task (live signal)."""
    try:
        if not _task_snapshot().get("running"):
            return False
    except Exception:
        return False
    cur = active_tasks.get("current_task") or {}
    return str(cur.get("task_id") or "") == str(run_id or "")
```

3. 在 `register_runs_routes(...)` 调用处追加实参 `is_run_active=_is_active_run`。
> 用 Grep 定位 `register_runs_routes(` 调用，在其关键字实参末尾加 `is_run_active=_is_active_run,`。

4. 在 `_run_batch_task` 完成路径 `complete_run` 之后（`api_server.py:299-305` 附近）追加：
```python
        try:
            _run_registry.prune_runs(keep=RUN_RETENTION_KEEP)
        except Exception as exc:
            logger.debug("[RUN REGISTRY] prune_runs failed: %s", exc)
```
> `api_server.py` 为 CRLF；StrReplace 失败按 `vspider-workflow §一` 写 `_patch_rundel.py` 字节改写即删。

### Step 2.4 · 运行确认通过（端点 + 既有 runs 回归）
```bash
.venv/bin/python -m pytest tests/test_run_delete_retention.py tests/test_run_registry.py tests/test_task_queue.py -q
```
预期：全绿（新端点 + 既有 run/queue 回归不破）。

### Step 2.5 · Commit
```bash
git add api_routes/runs_api.py api_server.py tests/test_run_delete_retention.py
git commit -m "feat(api): DELETE /api/runs/{id} with active-run guard + prune on completion"
```

---

## Task 3 · 前端 `RunRegistryPanel` 行内删除（TDD-source）

**文件：** `vspider-ui/src/components/RunRegistryPanel.vue`、`tests/test_run_delete_retention.py`（追加 source 断言）

### Step 3.1 · 追加前端 source 断言测试
在 `tests/test_run_delete_retention.py` 末尾追加（沿用 `test_frontend_component_split_y126.py` 风格）：

```python
class TestRunRegistryPanelDeleteWiring:
    def test_panel_has_delete_call_and_popconfirm(self) -> None:
        from pathlib import Path as _P
        src = _P("vspider-ui/src/components/RunRegistryPanel.vue").read_text(encoding="utf-8")
        assert "method: 'DELETE'" in src
        assert "/api/runs/" in src
        assert "el-popconfirm" in src
        assert "deleteRun" in src
```

### Step 3.2 · 运行确认失败
```bash
.venv/bin/python -m pytest tests/test_run_delete_retention.py::TestRunRegistryPanelDeleteWiring -q
```
预期：红（panel 尚无 deleteRun/popconfirm）。

### Step 3.3 · 实现 `RunRegistryPanel.vue`

(a) import 区加 `ElMessage` 与 `Delete` 图标：
```javascript
import { computed, onMounted, ref, watch } from 'vue'
import { Delete, Document, Refresh, View } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { API_BASE, apiFetch } from '../api/client.js'
```

(b) 在 `closeDialog` 之前加 `deleteRun`：
```javascript
const deleteRun = async (row) => {
  if (!row || !row.run_id) return
  try {
    const response = await apiFetch(`/api/runs/${encodeURIComponent(row.run_id)}`, { method: 'DELETE' })
    if (response.status === 409) { ElMessage.warning('运行中的任务不可删除'); return }
    if (response.status === 404) {
      runs.value = runs.value.filter((r) => r.run_id !== row.run_id)
      emit('loaded', runs.value)
      ElMessage.info('记录已不存在，已从列表移除')
      return
    }
    const result = await response.json().catch(() => ({}))
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || 'delete failed')
    }
    runs.value = runs.value.filter((r) => r.run_id !== row.run_id)
    emit('loaded', runs.value)
    ElMessage.success('已删除')
  } catch (err) {
    ElMessage.error(`删除失败: ${String(err)}`)
  }
}
```

(c) 「操作」列（当前 `width="92"` 仅「详情」）替换为：
```html
      <el-table-column label="操作" width="158">
        <template #default="scope">
          <el-button
            size="small"
            plain
            :icon="View"
            @click.stop="openRunDetail(scope.row)"
          >
            详情
          </el-button>
          <el-popconfirm
            title="删除该 run 及其产物？不可恢复"
            confirm-button-text="删除"
            cancel-button-text="取消"
            width="240"
            @confirm="deleteRun(scope.row)"
          >
            <template #reference>
              <el-button size="small" plain type="danger" :icon="Delete" @click.stop />
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
```

### Step 3.4 · 验证（source 测试 + 前端构建/全量）
```bash
.venv/bin/python -m pytest tests/test_run_delete_retention.py -q
cd vspider-ui && npx vitest run && npm run build
```
预期：pytest 全绿；vitest 全量 passed；`vite build` 成功。

### Step 3.5 · Commit
```bash
git add vspider-ui/src/components/RunRegistryPanel.vue tests/test_run_delete_retention.py
git commit -m "feat(ui): per-row run delete with confirm in RunRegistryPanel"
```

---

## Task 4 · 收口验证

### Step 4.1 · 后端定向 + 回归 + collect
```bash
.venv/bin/python -m pytest tests/test_run_delete_retention.py tests/test_run_registry.py tests/test_task_queue.py tests/test_network_intelligence.py -q
.venv/bin/python -m pytest tests/ --co -q   # 0 import 错误
```

### Step 4.2 · 前端
```bash
cd vspider-ui && npx vitest run && npm run build
```

### Step 4.3 · `code-audit` 收口
对照命中技能复核：①`delete_run` 路径仅限 `runs/<safe_id>/` + `logs/*_<id>.*` + registry json，`_safe_run_id` 阻断穿越；②DELETE 端点 404/409/200 齐全，活动护栏取实时 `_task_snapshot`+`active_tasks`；③prune 只删终态、try/except 不阻断 run 完成；④前端 409/404/success 分支齐全 + 二次确认；⑤`ReadLints` 受影响文件 0 错误。

---

## 规格覆盖度自检

| spec 需求 | 覆盖任务 |
| --- | --- |
| §4.1 delete_run / prune_runs | Task 1 |
| §4.2 DELETE 端点 + 404/409 | Task 2 |
| §4.3 is_run_active + RUN_RETENTION_KEEP + complete 后 prune | Task 2 |
| §5 前端行内删除 + popconfirm + 分支提示 | Task 3 |
| §6 路径安全 + 活动护栏 + 二次确认 | Task 1/2/3 |
| §8 测试（delete/prune/端点/前端 source） | Task 1/2/3 |

**占位符扫描：** 无 TODO/待定；每步含可运行代码或精确命令。
**类型一致性：** `delete_run`/`prune_runs`/`is_run_active`/`RUN_RETENTION_KEEP`/`deleteRun` 跨任务命名一致。
**偏离说明：** 前端用 source-structural 断言（项目无组件 mount 基建，沿用 y126 既有惯例）；核心删除风险由后端测试覆盖。

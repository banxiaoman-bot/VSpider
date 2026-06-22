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
    rr.create_run(run_id=rid, target_url="https://e.test", prompt="p", status="running")
    rr.update_run(rid, status=status, extra={"created_at": created_at})
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
        assert remaining == {"t4", "t3"}
        assert set(deleted) == {"t0", "t1", "t2"}

    def test_never_deletes_non_terminal(self, tmp_root: Path) -> None:
        _make_run("old_run", status="running", created_at=0.0)
        for i in range(3):
            _make_run(f"d{i}", status="succeeded", created_at=float(i + 1))
        rr.prune_runs(keep=1)
        remaining = {r["run_id"] for r in rr.list_runs(limit=500)}
        assert "old_run" in remaining
        assert "d2" in remaining


class TestDeleteEndpoint:
    @pytest.fixture()
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from starlette.testclient import TestClient
        import api_server
        monkeypatch.setattr(rr, "project_root", lambda: tmp_path)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        return TestClient(api_server.app, raise_server_exceptions=False), api_server

    def _make_terminal(self, rid: str) -> None:
        rr.create_run(run_id=rid, target_url="https://e.test", prompt="p", status="running")
        rr.update_run(rid, status="succeeded")

    def test_delete_terminal_run_200(self, client) -> None:
        c, api = client
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
        assert rr.load_run("act1") is not None


class TestRunRegistryPanelDeleteWiring:
    def test_panel_has_delete_call_and_popconfirm(self) -> None:
        src = Path("vspider-ui/src/components/RunRegistryPanel.vue").read_text(encoding="utf-8")
        assert "method: 'DELETE'" in src
        assert "/api/runs/" in src
        assert "el-popconfirm" in src
        assert "deleteRun" in src

"""ART-RUN-1: /api/run_artifacts 按 run 聚合 + 任务短名。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import api_server as api
import visual_web_agent.run_registry as rr


class TestDeriveTaskLabel:
    def test_strips_leading_filler_and_truncates(self) -> None:
        f = api._derive_task_label
        assert f("请帮我提取首页所有名言的文字和对应作者", "", limit=14).startswith("提取首页")
        assert len(f("请帮我提取首页所有名言的文字和对应作者的详细信息啊啊啊", "", limit=14)) <= 15
        assert f("", "https://quotes.toscrape.com/path", limit=14) == "quotes.toscrape.com"
        assert f("", "", limit=14) == ""

    def test_takes_first_clause(self) -> None:
        f = api._derive_task_label
        assert f("抓取名言，然后导出excel", "", limit=20) == "抓取名言"

    def test_english_goal_kept(self) -> None:
        f = api._derive_task_label
        out = f("extract all quotes and authors", "", limit=20)
        assert out.startswith("extract")


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
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_returns_only_runs_with_artifacts(self, client, tmp_path, monkeypatch) -> None:
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


class TestArtifactsByRunWiring:
    def test_component_and_app_wiring(self) -> None:
        comp = Path("vspider-ui/src/components/ArtifactsByRun.vue").read_text(encoding="utf-8")
        assert "/api/run_artifacts" in comp
        assert "el-collapse" in comp
        app = Path("vspider-ui/src/App.vue").read_text(encoding="utf-8")
        assert "ArtifactsByRun" in app

"""OUT-5 + frontend P2 API/composable smoke tests."""

from __future__ import annotations

import pytest


@pytest.fixture()
def client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    import api_server as api

    return TestClient(api.app)


def test_output_contract_preview_api(client) -> None:
    resp = client.get(
        "/api/output_contract/preview",
        params={"goal": "提取页面表格并保存为 csv"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "success"
    oc = payload["output_contract"]
    assert oc["container"] in {"csv", "xlsx", "jsonl"}
    assert oc["output_kind"]


def test_download_run_artifact_route(client, tmp_path, monkeypatch) -> None:
    from visual_web_agent.io_contract.persistence import ARTIFACTS_DIRNAME, run_dir

    monkeypatch.setattr(
        "visual_web_agent.io_contract.persistence.default_runs_root",
        lambda: tmp_path,
    )
    run_id = "run_dl_smoke"
    artifact = run_dir(run_id, base_dir=tmp_path) / ARTIFACTS_DIRNAME / "demo.txt"
    artifact.write_text("hello run artifact", encoding="utf-8")

    resp = client.get(f"/download/runs/{run_id}/artifacts/demo.txt")
    assert resp.status_code == 200
    assert resp.content == b"hello run artifact"


def test_artifact_url_for_run_scoped_path(tmp_path, monkeypatch) -> None:
    from visual_web_agent import artifact_manager as am
    from visual_web_agent.io_contract.persistence import ARTIFACTS_DIRNAME, run_dir

    run_id = "run_url_smoke"
    path = run_dir(run_id, base_dir=tmp_path) / ARTIFACTS_DIRNAME / "out.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a,b\n1,2\n", encoding="utf-8")

    monkeypatch.setattr(am, "_runs_root", lambda: tmp_path)
    url = am.artifact_url(path)
    assert url.startswith("/download/runs/")
    assert "out.csv" in url

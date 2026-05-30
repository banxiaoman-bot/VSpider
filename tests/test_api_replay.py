from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visual_web_agent import api_replay


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_api_replay_tests"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_build_replay_url_replaces_wildcards() -> None:
    url = api_replay.build_replay_url(
        "https://api.example.com/search?q=phone&page=*&limit=*",
        page=3,
        page_size=25,
    )
    assert url == "https://api.example.com/search?q=phone&page=3&limit=25"


def test_build_replay_url_adds_pagination_when_absent() -> None:
    url = api_replay.build_replay_url(
        "https://api.example.com/items?q=phone",
        page=2,
        page_size=10,
    )
    assert url == "https://api.example.com/items?q=phone&page=2&limit=10"


def test_choose_candidate_by_endpoint_or_score() -> None:
    candidates = [
        {"endpoint": "https://api.example.com/a", "score": 1, "row_count": 5},
        {"endpoint": "https://api.example.com/b", "score": 9, "row_count": 2},
    ]
    assert api_replay.choose_candidate(candidates, endpoint="https://api.example.com/a")["endpoint"].endswith("/a")
    assert api_replay.choose_candidate(candidates)["endpoint"].endswith("/b")


def test_replay_candidate_with_fetcher_exports_jsonl(monkeypatch: pytest.MonkeyPatch, local_tmp_path: Path) -> None:
    artifact_root = local_tmp_path / "artifacts"

    monkeypatch.setattr(api_replay, "resolve_artifact_path", lambda filename, subdir="": artifact_root / subdir / filename)
    monkeypatch.setattr(api_replay, "register_artifact", lambda path: None)
    monkeypatch.setattr(api_replay, "artifact_url", lambda path: "/download/" + Path(path).name)

    candidate = {
        "endpoint": "https://api.example.com/items?page=*&limit=*",
        "method": "GET",
        "score": 8,
        "schema": "id|title",
    }

    def fake_fetcher(url: str, headers: dict[str, str], timeout_s: float, method: str, body: str):
        assert url == "https://api.example.com/items?page=1&limit=2"
        assert method == "GET"
        assert "accept" in headers
        payload = json.dumps({"data": {"items": [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]}})
        return 200, {"content-type": "application/json"}, payload

    result = api_replay.replay_candidate(
        run_id="run_replay",
        candidate=candidate,
        page=1,
        page_size=2,
        fetcher=fake_fetcher,
    )

    assert result["status"] == "success"
    assert result["row_count"] == 2
    assert result["sample"][0]["title"] == "A"
    artifact = Path(result["artifact"]["path"])
    assert artifact.exists()
    assert artifact.read_text(encoding="utf-8").splitlines()[0] == '{"id":1,"title":"A"}'


def test_replay_candidate_posts_body_and_method(monkeypatch: pytest.MonkeyPatch, local_tmp_path: Path) -> None:
    monkeypatch.setattr(api_replay, "resolve_artifact_path", lambda filename, subdir="": local_tmp_path / subdir / filename)
    monkeypatch.setattr(api_replay, "register_artifact", lambda path: None)
    monkeypatch.setattr(api_replay, "artifact_url", lambda path: "/download/" + Path(path).name)

    candidate = {
        "endpoint": "https://api.example.com/search",
        "method": "POST",
        "request_body": '{"q": "phone"}',
        "score": 8,
    }
    seen: dict[str, object] = {}

    def fake_fetcher(url, headers, timeout_s, method, body):
        seen["method"] = method
        seen["body"] = body
        return 200, {"content-type": "application/json"}, json.dumps({"data": [{"id": 1}]})

    result = api_replay.replay_candidate(
        run_id="run_post",
        candidate=candidate,
        page=None,
        page_size=None,
        fetcher=fake_fetcher,
    )
    assert seen["method"] == "POST"
    assert seen["body"] == '{"q": "phone"}'
    assert result["status"] == "success"
    assert result["row_count"] == 1


def test_replay_candidate_marks_non_2xx_as_http_error(local_tmp_path: Path) -> None:
    candidate = {"endpoint": "https://api.example.com/detail?id=1", "method": "GET", "score": 8}

    def forbidden_fetcher(url, headers, timeout_s, method, body):
        return 403, {"content-type": "application/json"}, json.dumps({"error": "forbidden"})

    result = api_replay.replay_candidate(
        run_id="run_403",
        candidate=candidate,
        fetcher=forbidden_fetcher,
    )
    assert result["http_ok"] is False
    assert result["status"] == "http_error"
    assert result["row_count"] == 0


def test_api_replay_dry_run_endpoint(monkeypatch: pytest.MonkeyPatch, local_tmp_path: Path) -> None:
    import api_server

    monkeypatch.setattr(api_server._network_intelligence, "network_root", lambda base_dir=None: local_tmp_path)
    monkeypatch.setattr(api_server._network_intelligence, "network_path", lambda run_id, base_dir=None: local_tmp_path / f"{run_id}.jsonl")

    api_server._network_intelligence.record_candidate(
        run_id="api_replay_run",
        url="https://api.example.com/list?page=1",
        rows=[{"id": 1, "title": "one"}],
        score=5,
        base_dir=local_tmp_path,
    )

    client = TestClient(api_server.app)
    resp = client.post("/api/runs/api_replay_run/network/replay?page=2&page_size=20")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "success"
    assert payload["dry_run"] is True
    assert payload["plan"]["url"] == "https://api.example.com/list?page=2&limit=20"


def test_api_replay_source_wiring() -> None:
    root = Path(__file__).resolve().parent.parent
    api_src = (root / "api_server.py").read_text(encoding="utf-8")
    assert "from visual_web_agent import api_replay as _api_replay" in api_src
    assert '@app.post("/api/runs/{run_id}/network/replay"' in api_src
    assert "_api_replay.build_replay_plan" in api_src
    assert "_api_replay.replay_candidate" in api_src

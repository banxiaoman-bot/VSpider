from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visual_web_agent import network_intelligence as ni


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_network_intelligence_tests"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_normalize_endpoint_strips_volatile_and_masks_pagination() -> None:
    url = "https://api.example.com/search?q=phone&page=3&ts=123456&limit=20&_="
    assert ni.normalize_endpoint(url) == "https://api.example.com/search?q=phone&page=*&limit=*"


def test_record_and_list_candidates(local_tmp_path: Path) -> None:
    rows = [
        {"id": i, "title": f"item {i}", "price": i * 10, "url": f"https://x/{i}"}
        for i in range(8)
    ]
    rec = ni.record_candidate(
        run_id="run_net",
        url="https://api.example.com/items?page=1&ts=999",
        method="post",
        status=200,
        resource_type="xhr",
        rows=rows,
        score=3,
        content_type="application/json",
        base_dir=local_tmp_path,
    )

    assert rec is not None
    assert rec["run_id"] == "run_net"
    assert rec["method"] == "POST"
    assert rec["row_count"] == 8
    assert rec["endpoint"] == "https://api.example.com/items?page=*"
    assert rec["score"] >= 7
    assert rec["sample"][0]["title"] == "item 0"

    listed = ni.list_candidates("run_net", limit=10, min_score=1, base_dir=local_tmp_path)
    assert len(listed) == 1
    assert listed[0]["url"].startswith("https://api.example.com/items")


def test_summarize_groups_by_normalized_endpoint(local_tmp_path: Path) -> None:
    for page in (1, 2):
        ni.record_candidate(
            run_id="run_sum",
            url=f"https://api.example.com/list?page={page}",
            rows=[{"id": page, "name": "A"}],
            score=5,
            base_dir=local_tmp_path,
        )

    items = ni.list_candidates("run_sum", base_dir=local_tmp_path)
    summary = ni.summarize_candidates(items)
    assert summary["endpoint_count"] == 1
    top = summary["top_endpoints"][0]
    assert top["endpoint"] == "https://api.example.com/list?page=*"
    assert top["hits"] == 2


def test_invalid_run_id_is_safe(local_tmp_path: Path) -> None:
    assert ni.list_candidates("../bad", base_dir=local_tmp_path) == []
    with pytest.raises(ValueError):
        ni.record_candidate(
            run_id="../bad",
            url="https://api.example.com/list",
            rows=[{"id": 1}],
            base_dir=local_tmp_path,
        )


def test_api_run_network_endpoint(monkeypatch: pytest.MonkeyPatch, local_tmp_path: Path) -> None:
    import api_server

    monkeypatch.setattr(api_server._network_intelligence, "network_root", lambda base_dir=None: local_tmp_path)
    monkeypatch.setattr(api_server._network_intelligence, "network_path", lambda run_id, base_dir=None: local_tmp_path / f"{run_id}.jsonl")

    api_server._network_intelligence.record_candidate(
        run_id="api_net",
        url="https://api.example.com/search?page=1",
        rows=[{"id": 1, "title": "one"}],
        score=5,
        base_dir=local_tmp_path,
    )

    client = TestClient(api_server.app)
    resp = client.get("/api/runs/api_net/network")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "success"
    assert payload["run_id"] == "api_net"
    assert payload["count"] == 1
    assert payload["summary"]["endpoint_count"] == 1


def test_build_candidate_captures_auth_headers_and_body() -> None:
    candidate = ni.build_candidate(
        run_id="run_auth",
        url="https://api.example.com/detail?id=1",
        method="post",
        rows=[{"id": 1, "content": "x" * 500}],
        request_headers={
            "Authorization": "Bearer secret-token",
            "X-CSRF-Token": "csrf-1",
            "Cookie": "should-not-be-kept",
            "User-Agent": "should-not-be-kept",
        },
        request_body='{"id": 1}',
    )
    assert candidate["request_headers"]["authorization"] == "Bearer secret-token"
    assert candidate["request_headers"]["x-csrf-token"] == "csrf-1"
    assert "cookie" not in candidate["request_headers"]
    assert "user-agent" not in candidate["request_headers"]
    assert candidate["request_body"] == '{"id": 1}'


def test_build_candidate_stores_real_max_text_len() -> None:
    long_text = "完整正文" * 100  # 400 chars, well beyond the 180-char sample cap
    candidate = ni.build_candidate(
        run_id="run_len",
        url="https://api.example.com/detail?id=1",
        rows=[{"content": long_text}],
    )
    assert len(candidate["sample"][0]["content"]) <= 181
    assert candidate["max_text_len"] == len(long_text)
    assert candidate["field_max_lengths"]["content"] == len(long_text)


def test_capped_full_rows_is_bounded() -> None:
    rows = [{"content": "y" * 50000, "id": i} for i in range(300)]
    candidate = ni.build_candidate(
        run_id="run_full",
        url="https://api.example.com/list?page=1",
        rows=rows,
    )
    full = candidate["full_rows"]
    assert 0 < len(full) <= ni._FULL_ROWS_MAX_ROWS
    assert all(len(r.get("content", "")) <= ni._FULL_ROWS_MAX_FIELD_CHARS for r in full)


def test_browser_and_main_wiring_source_pins() -> None:
    root = Path(__file__).resolve().parent.parent
    browser_src = (root / "visual_web_agent" / "browser_env.py").read_text(encoding="utf-8")
    main_src = (root / "visual_web_agent" / "main.py").read_text(encoding="utf-8")
    api_src = (root / "api_server.py").read_text(encoding="utf-8")

    assert "from .network_intelligence import record_candidate as _record_network_candidate" in browser_src
    assert "self._network_run_id: str | None = None" in browser_src
    assert "def configure_network_intelligence(self, run_id: str | None) -> None:" in browser_src
    assert "network_active = bool(self._network_run_id)" in browser_src
    assert "self._record_network_candidate_safe(" in browser_src
    assert "browser.configure_network_intelligence(_run_ts)" in main_src
    runs_src = (root / "api_routes" / "runs_api.py").read_text(encoding="utf-8")
    assert '@app.get(' in runs_src and "runs/{run_id}/network" in runs_src

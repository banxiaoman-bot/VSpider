"""MC-1: start_batch 在表单未给 vlm_model 时，回退用 model_config.json 的值。

经响应回显的 vlm_model 断言 resolve_vlm_options 已接入（api_server.py 回显段）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

import api_server as api
import model_config_store as store


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(store, "MODEL_CONFIG_PATH", tmp_path / "model_config.json")
    monkeypatch.setattr(api, "_start_queue_workers", lambda bt: ([], {}))
    monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": False, "in_cooldown": False})
    return TestClient(api.app, raise_server_exceptions=False)


class TestStartBatchModelConfigFallback:
    def test_empty_form_model_falls_back_to_json(self, client: TestClient) -> None:
        store.save_model_config({"vlm": {"model": "qwen-from-json", "api_key": "sk-abcdefgh1234"}})
        resp = client.post("/api/start_batch", data={
            "prompt": "抓取 https://quotes.toscrape.com/ 名言",
            # no vlm_model / vlm_base_url / vlm_api_key in the form
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "success", body
        assert body.get("vlm_model") == "qwen-from-json", body  # json tier applied

    def test_form_model_overrides_json(self, client: TestClient) -> None:
        store.save_model_config({"vlm": {"model": "qwen-from-json"}})
        resp = client.post("/api/start_batch", data={
            "prompt": "抓取 https://quotes.toscrape.com/ 名言",
            "vlm_model": "qwen-from-form",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json().get("vlm_model") == "qwen-from-form"  # form tier wins

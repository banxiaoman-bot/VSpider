"""OUT-4 regression: ``/api/start_batch`` must accept an empty
``target_url`` and either infer it from the prompt or return a clean
error -- never blow up with ``Field required``.

These tests exercise the FastAPI endpoint via TestClient so the form
validation and our new URL-inference branch are both covered.
"""

from __future__ import annotations

import io

import pytest


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture()
def client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    import api_server as api

    return TestClient(api.app)


class TestHarvestUrlsFromText:
    def test_finds_plain_url(self) -> None:
        from api_server import _harvest_urls_from_text

        urls = _harvest_urls_from_text("打开 https://example.com 看看")
        assert urls == ["https://example.com"]

    def test_strips_trailing_punctuation(self) -> None:
        from api_server import _harvest_urls_from_text

        urls = _harvest_urls_from_text("访问 https://example.com/page，然后...")
        assert urls == ["https://example.com/page"]

    def test_multiple_urls_kept_in_order(self) -> None:
        from api_server import _harvest_urls_from_text

        urls = _harvest_urls_from_text(
            "先看 https://a.test 再看 https://b.test 最后 https://a.test"
        )
        assert urls == ["https://a.test", "https://b.test"]

    def test_empty_text(self) -> None:
        from api_server import _harvest_urls_from_text

        assert _harvest_urls_from_text("") == []
        assert _harvest_urls_from_text("仅文字，没有链接") == []


class TestStartBatchUrlOptional:
    def test_missing_target_url_with_prompt_url_inferred(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import api_server as api

        # Make sure we don't actually start any worker.
        monkeypatch.setattr(api, "_start_queue_workers", lambda bt: ([], {}))
        # Skip cooldown checks.
        monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": False, "in_cooldown": False})

        resp = client.post(
            "/api/start_batch",
            data={
                "prompt": "请抓取 https://quotes.toscrape.com/ 首页所有名言",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "success", body
        assert body.get("target_url") == "https://quotes.toscrape.com/"

    def test_missing_target_url_and_no_prompt_url_returns_error(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import api_server as api

        monkeypatch.setattr(api, "_start_queue_workers", lambda bt: ([], {}))
        monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": False, "in_cooldown": False})

        resp = client.post(
            "/api/start_batch",
            data={"prompt": "帮我看下天气怎么样"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "error"
        msg = body.get("message", "")
        assert "target_url" in msg or "URL" in msg

    def test_explicit_target_url_still_wins(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import api_server as api

        monkeypatch.setattr(api, "_start_queue_workers", lambda bt: ([], {}))
        monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": False, "in_cooldown": False})

        resp = client.post(
            "/api/start_batch",
            data={
                "prompt": "去 https://decoy.example/ 看看",
                "target_url": "https://primary.example/",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "success", body
        assert body.get("target_url") == "https://primary.example/"

    def test_multiple_prompt_urls_become_extras_when_inferred(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import api_server as api

        monkeypatch.setattr(api, "_start_queue_workers", lambda bt: ([], {}))
        monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": False, "in_cooldown": False})

        resp = client.post(
            "/api/start_batch",
            data={
                "prompt": "对比 https://site-a.test/ 与 https://site-b.test/ 的内容",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "success", body
        assert body.get("target_url") == "https://site-a.test/"
        assert "https://site-b.test/" in (body.get("urls") or [])

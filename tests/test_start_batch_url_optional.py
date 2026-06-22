"""OUT-4 / input_contract §一-B regression: ``/api/start_batch`` must accept an
empty ``target_url``. It first tries to harvest a URL from the prompt; failing
that it defers to the io_contract preflight, which resolves a search-engine
entry (decision A: never hard-block a URL-less goal). It never blows up with
``Field required``.

These tests exercise the FastAPI endpoint via TestClient so the form
validation, the prompt-harvest branch, and the preflight entry-inference branch
are all covered.
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

    def test_missing_target_url_and_no_prompt_url_infers_search_entry(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """input_contract §一-B: a URL-less goal is no longer hard-blocked at
        the HTTP edge. The io_contract preflight resolves a search-engine entry
        the agent can start from, and the response flags it as auto-inferred so
        the frontend can let the user confirm / override."""
        import api_server as api

        monkeypatch.setattr(api, "_start_queue_workers", lambda bt: ([], {}))
        monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": False, "in_cooldown": False})

        resp = client.post(
            "/api/start_batch",
            data={"prompt": "帮我看下天气怎么样"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "success", body
        target_url = body.get("target_url") or ""
        assert target_url.startswith("http"), body
        assert body.get("target_url_auto_entry"), body

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

    def test_explicit_urls_override_target_url(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import api_server as api

        monkeypatch.setattr(api, "_start_queue_workers", lambda bt: ([], {}))
        monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": False, "in_cooldown": False})

        resp = client.post(
            "/api/start_batch",
            data={
                "prompt": "compare these pages",
                "target_url": "https://legacy.example/",
                "urls": '["https://a.test/", "https://b.test/"]',
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "success", body
        assert body.get("target_url") == "https://a.test/"
        assert body.get("urls") == ["https://b.test/"]

    def test_url_less_goal_uses_semantic_entry_llm(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """input_contract §一-B #2: a URL-less goal must let the semantic LLM
        pick a real start site (entry_suggestion.source == "llm"), wired into
        the ``/api/start_batch`` main entry -- not silently degrade to the Bing
        search fallback. We stub ``entry_llm_from_config`` so the model
        "returns" a concrete site; the resolved target_url must be that site."""
        import api_server as api

        monkeypatch.setattr(api, "_start_queue_workers", lambda bt: ([], {}))
        monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": False, "in_cooldown": False})

        import visual_web_agent.io_contract.entry_llm as el

        monkeypatch.setattr(
            el, "entry_llm_from_config",
            lambda: (lambda _prompt: "https://www.zhihu.com"),
            raising=True,
        )

        resp = client.post(
            "/api/start_batch",
            data={"prompt": "到知乎找一篇深度学习入门的高赞回答"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "success", body
        assert (body.get("target_url") or "").startswith("https://www.zhihu.com"), body
        assert body.get("target_url_auto_entry"), body

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


class TestStartBatchAttachmentIntent:
    """input_contract §一-B: ``/api/start_batch`` must report the inferred
    attachment intent so the frontend can explicitly route batch_rows vs
    upload_to_page vs prompt_context, instead of treating every upload as a
    batch DataFrame."""

    def test_no_file_reports_empty_intent(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import api_server as api

        monkeypatch.setattr(api, "_start_queue_workers", lambda bt: ([], {}))
        monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": False, "in_cooldown": False})

        resp = client.post(
            "/api/start_batch",
            data={"prompt": "抓取 https://a.test/ 列表"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "success", body
        assert body.get("attachment_intent") == ""

    def test_xlsx_with_row_goal_reports_batch_rows(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import api_server as api

        monkeypatch.setattr(api, "_start_queue_workers", lambda bt: ([], {}))
        monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": False, "in_cooldown": False})

        resp = client.post(
            "/api/start_batch",
            data={"prompt": "按行逐条填报", "target_url": "https://form.example/"},
            files={
                "file": (
                    "rows.xlsx",
                    b"PK\x03\x04fake-xlsx-bytes",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "success", body
        assert body.get("attachment_intent") == "batch_rows", body

    def test_pdf_with_upload_goal_reports_upload_to_page(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import api_server as api

        monkeypatch.setattr(api, "_start_queue_workers", lambda bt: ([], {}))
        monkeypatch.setattr(api, "_task_snapshot", lambda: {"running": False, "in_cooldown": False})

        resp = client.post(
            "/api/start_batch",
            data={"prompt": "把这个 PDF 上传到页面", "target_url": "https://form.example/"},
            files={"file": ("doc.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "success", body
        assert body.get("attachment_intent") == "upload_to_page", body

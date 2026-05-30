"""Tests for DOM/API completeness fast path (NET-6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from visual_web_agent import network_intelligence as ni
from visual_web_agent.content_completeness_guard import (
    build_cookie_header,
    detect_dom_truncation,
    evaluate_dom_api_completeness,
    execute_api_fast_path,
)


@pytest.fixture()
def net_dir(tmp_path: Path) -> Path:
    return tmp_path


def test_detect_dom_truncation_markers() -> None:
    out = detect_dom_truncation(
        dom_text="这是摘要… 开通会员查看全文",
        dom_rows=[{"title": "A", "content": "短内容"}],
    )
    assert out["truncated"] is True
    assert "page_text" in out["markers"]


def test_evaluate_prefers_richer_api_candidate(net_dir: Path) -> None:
    dom_rows = [{"title": "商品A", "content": "这是被截断的摘要…"}]
    ni.record_candidate(
        run_id="run_fp",
        url="https://api.example.com/items?page=1",
        rows=[
            {
                "id": 1,
                "title": "商品A",
                "content": "这是完整正文" * 20,
            }
        ],
        score=6,
        base_dir=net_dir,
    )
    candidates = ni.list_candidates("run_fp", base_dir=net_dir)
    verdict = evaluate_dom_api_completeness(
        dom_rows=dom_rows,
        dom_text="开通会员查看全文",
        candidates=candidates,
        goal="抓取商品详情完整正文",
    )
    assert verdict["should_fast_path"] is True
    assert verdict["dom_truncated"] is True
    assert verdict["api_richer"] is True


def test_execute_api_fast_path_with_fetcher(net_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dom_rows = [{"content": "短…"}]
    candidate = ni.record_candidate(
        run_id="run_exec",
        url="https://api.example.com/detail?id=1",
        rows=[{"id": 1, "content": "full body " * 30}],
        score=7,
        base_dir=net_dir,
    )
    assert candidate is not None
    verdict = evaluate_dom_api_completeness(
        dom_rows=dom_rows,
        dom_text="VIP only",
        candidates=[candidate],
        goal="获取完整详情",
    )

    payload = {"data": [{"id": 1, "content": "full body " * 30}]}

    def _fetcher(url: str, headers: dict, timeout_s: float, method: str, body: str):
        assert headers.get("Cookie") == "sid=abc"
        return 200, {"content-type": "application/json"}, json.dumps(payload).encode()

    out = execute_api_fast_path(
        run_id="run_exec",
        verdict=verdict,
        cookies=[{"name": "sid", "value": "abc", "domain": "api.example.com"}],
        fetcher=_fetcher,
    )
    assert out["applied"] is True
    assert out["row_count"] == 1
    assert len(out["rows"][0]["content"]) > 100


def test_richness_uses_stored_lengths_not_truncated_sample(net_dir: Path) -> None:
    # DOM preview is long enough that a 180-char sample would hide the gap.
    dom_rows = [{"content": "预览" * 80}]  # 160 chars
    candidate = ni.record_candidate(
        run_id="run_rich",
        url="https://api.example.com/detail?id=1",
        rows=[{"content": "完整正文" * 500}],  # 2000 chars, sample caps at ~180
        score=6,
        base_dir=net_dir,
    )
    assert candidate is not None
    assert len(candidate["sample"][0]["content"]) <= 181
    verdict = evaluate_dom_api_completeness(
        dom_rows=dom_rows,
        dom_text="",
        candidates=[candidate],
        goal="抓取完整正文",
    )
    assert verdict["api_richer"] is True
    assert verdict["should_fast_path"] is True


def test_execute_replays_captured_auth_headers(net_dir: Path) -> None:
    candidate = ni.record_candidate(
        run_id="run_token",
        url="https://api.example.com/detail?id=1",
        rows=[{"id": 1, "content": "short…"}],
        score=7,
        request_headers={"Authorization": "Bearer tok-123"},
        base_dir=net_dir,
    )
    verdict = evaluate_dom_api_completeness(
        dom_rows=[{"content": "short…"}],
        dom_text="VIP only",
        candidates=[candidate],
        goal="获取完整详情",
    )
    seen: dict[str, object] = {}

    def _fetcher(url, headers, timeout_s, method, body):
        seen["auth"] = headers.get("authorization")
        seen["url"] = url
        return 200, {"content-type": "application/json"}, json.dumps({"data": [{"id": 1, "content": "full " * 40}]}).encode()

    out = execute_api_fast_path(run_id="run_token", verdict=verdict, cookies=[], fetcher=_fetcher)
    assert seen["auth"] == "Bearer tok-123"
    # detail endpoint must not be force-paginated
    assert "page=" not in str(seen["url"])
    assert out["applied"] is True
    assert "authorization" in out["auth_headers_used"]


def test_execute_falls_back_to_captured_payload_on_http_error(net_dir: Path) -> None:
    candidate = ni.record_candidate(
        run_id="run_fb",
        url="https://api.example.com/detail?id=1",
        rows=[{"id": 1, "content": "完整正文" * 60}],
        score=7,
        base_dir=net_dir,
    )
    verdict = evaluate_dom_api_completeness(
        dom_rows=[{"content": "预览…"}],
        dom_text="开通会员查看全文",
        candidates=[candidate],
        goal="获取完整正文",
    )

    def _forbidden(url, headers, timeout_s, method, body):
        return 403, {"content-type": "application/json"}, json.dumps({"error": "no"}).encode()

    out = execute_api_fast_path(run_id="run_fb", verdict=verdict, cookies=[], fetcher=_forbidden)
    assert out["applied"] is True
    assert out["source"] == "captured_payload"
    assert out["row_count"] == 1
    assert "完整正文" in out["rows"][0]["content"]


def test_build_cookie_header_filters_domain() -> None:
    header = build_cookie_header(
        [
            {"name": "a", "value": "1", "domain": "api.example.com"},
            {"name": "b", "value": "2", "domain": "other.com"},
        ],
        url="https://api.example.com/detail",
    )
    assert header == "a=1"

"""S12: api_replay automatic pagination (page / cursor / next-url modes).

Covers paginate_replay stop conditions (empty page, duplicate page, short
page, target reached, max_pages cap, mid-flight http error), merged single
artifact, and route_executor auto-pagination wiring.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from visual_web_agent import api_replay
from visual_web_agent.route_executor import execute_route


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_api_replay_pagination"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _stub_artifacts(monkeypatch: pytest.MonkeyPatch, local_tmp_path: Path) -> None:
    monkeypatch.setattr(
        api_replay, "resolve_artifact_path", lambda filename, subdir="": local_tmp_path / subdir / filename
    )
    monkeypatch.setattr(api_replay, "register_artifact", lambda path: None)
    monkeypatch.setattr(api_replay, "artifact_url", lambda path: "/download/" + Path(path).name)


def _rows(start: int, count: int) -> list[dict[str, int]]:
    return [{"id": i} for i in range(start, start + count)]


# ---------------------------------------------------------------------------
# build_replay_url cursor support
# ---------------------------------------------------------------------------


def test_build_replay_url_substitutes_cursor_key() -> None:
    url = api_replay.build_replay_url(
        "https://api.example.com/items?cursor=abc&limit=*",
        page_size=10,
        cursor="tok2",
    )
    assert url == "https://api.example.com/items?cursor=tok2&limit=10"


def test_build_replay_url_appends_cursor_when_absent() -> None:
    url = api_replay.build_replay_url(
        "https://api.example.com/items?q=x",
        cursor="tok9",
    )
    query = parse_qs(urlparse(url).query)
    assert query["cursor"] == ["tok9"]
    assert "page" not in query  # cursor mode suppresses numeric page injection


# ---------------------------------------------------------------------------
# numeric page mode
# ---------------------------------------------------------------------------


def test_paginate_numeric_pages_until_short_page() -> None:
    pages = {1: _rows(1, 3), 2: _rows(4, 3), 3: _rows(7, 1)}

    def fetcher(url, headers, timeout_s, method, body):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        return 200, {"content-type": "application/json"}, json.dumps({"data": pages.get(page, [])})

    result = api_replay.paginate_replay(
        run_id="pg_numeric",
        candidate={"endpoint": "https://api.example.com/items?page=*&limit=*", "method": "GET"},
        page_size=3,
        max_pages=10,
        fetcher=fetcher,
    )

    assert result["status"] == "success"
    assert result["row_count"] == 7
    assert result["page_count"] == 3
    assert result["stop_reason"] == "short_page"
    assert result["truncated"] is False
    assert [r["id"] for r in result["rows"]] == [1, 2, 3, 4, 5, 6, 7]


def test_paginate_stops_on_empty_page() -> None:
    pages = {1: _rows(1, 2), 2: []}

    def fetcher(url, headers, timeout_s, method, body):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        return 200, {"content-type": "application/json"}, json.dumps({"data": pages.get(page, [])})

    result = api_replay.paginate_replay(
        run_id="pg_empty",
        candidate={"endpoint": "https://api.example.com/items?page=*&limit=*", "method": "GET"},
        page_size=2,
        fetcher=fetcher,
    )
    assert result["row_count"] == 2
    assert result["stop_reason"] == "empty_page"


def test_paginate_stops_on_duplicate_page() -> None:
    def fetcher(url, headers, timeout_s, method, body):
        # Server ignores the page param and always returns the same rows.
        return 200, {"content-type": "application/json"}, json.dumps({"data": _rows(1, 3)})

    result = api_replay.paginate_replay(
        run_id="pg_dup",
        candidate={"endpoint": "https://api.example.com/items?page=*&limit=*", "method": "GET"},
        page_size=3,
        max_pages=10,
        fetcher=fetcher,
    )
    assert result["row_count"] == 3
    assert result["page_count"] == 2
    assert result["stop_reason"] == "duplicate_page"


def test_paginate_respects_max_pages_cap_and_marks_truncated() -> None:
    def fetcher(url, headers, timeout_s, method, body):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        return 200, {"content-type": "application/json"}, json.dumps({"data": _rows(page * 100, 2)})

    result = api_replay.paginate_replay(
        run_id="pg_cap",
        candidate={"endpoint": "https://api.example.com/items?page=*&limit=*", "method": "GET"},
        page_size=2,
        max_pages=3,
        fetcher=fetcher,
    )
    assert result["page_count"] == 3
    assert result["row_count"] == 6
    assert result["stop_reason"] == "max_pages"
    assert result["truncated"] is True


def test_paginate_stops_when_target_rows_reached() -> None:
    def fetcher(url, headers, timeout_s, method, body):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        return 200, {"content-type": "application/json"}, json.dumps({"data": _rows(page * 10, 3)})

    result = api_replay.paginate_replay(
        run_id="pg_target",
        candidate={"endpoint": "https://api.example.com/items?page=*&limit=*", "method": "GET"},
        page_size=3,
        max_pages=10,
        target_rows=5,
        fetcher=fetcher,
    )
    assert result["row_count"] >= 5
    assert result["stop_reason"] == "target_reached"


def test_paginate_keeps_collected_rows_on_midflight_http_error() -> None:
    def fetcher(url, headers, timeout_s, method, body):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        if page >= 2:
            return 500, {"content-type": "application/json"}, json.dumps({"error": "boom"})
        return 200, {"content-type": "application/json"}, json.dumps({"data": _rows(1, 3)})

    result = api_replay.paginate_replay(
        run_id="pg_err",
        candidate={"endpoint": "https://api.example.com/items?page=*&limit=*", "method": "GET"},
        page_size=3,
        fetcher=fetcher,
    )
    # Page 1 rows survive; the failure is reported through stop_reason.
    assert result["row_count"] == 3
    assert result["stop_reason"] == "http_error"
    assert result["status"] == "success"
    assert Path(result["artifact"]["path"]).exists()


# ---------------------------------------------------------------------------
# cursor mode
# ---------------------------------------------------------------------------


def test_paginate_follows_cursor_tokens() -> None:
    chunks = {"": (_rows(1, 2), "tok2"), "tok2": (_rows(3, 2), "tok3"), "tok3": (_rows(5, 1), "")}

    def fetcher(url, headers, timeout_s, method, body):
        query = parse_qs(urlparse(url).query)
        token = query.get("cursor", [""])[0]
        rows, nxt = chunks[token]
        payload: dict = {"data": rows}
        if nxt:
            payload["next_cursor"] = nxt
        return 200, {"content-type": "application/json"}, json.dumps(payload)

    result = api_replay.paginate_replay(
        run_id="pg_cursor",
        candidate={"endpoint": "https://api.example.com/items?cursor=&limit=*", "method": "GET"},
        page_size=2,
        max_pages=10,
        fetcher=fetcher,
    )
    assert result["row_count"] == 5
    assert [r["id"] for r in result["rows"]] == [1, 2, 3, 4, 5]
    # Last chunk has no next_cursor and is short -> short_page stop.
    assert result["stop_reason"] == "short_page"


def test_paginate_follows_server_next_url_verbatim() -> None:
    seen_urls: list[str] = []

    def fetcher(url, headers, timeout_s, method, body):
        seen_urls.append(url)
        if "step=2" in url:
            return 200, {"content-type": "application/json"}, json.dumps({"results": _rows(3, 2), "next": None})
        return (
            200,
            {"content-type": "application/json"},
            json.dumps({"results": _rows(1, 2), "next": "https://api.example.com/items?step=2&custom=1"}),
        )

    result = api_replay.paginate_replay(
        run_id="pg_next",
        candidate={"endpoint": "https://api.example.com/items", "method": "GET"},
        page_size=2,
        max_pages=10,
        fetcher=fetcher,
    )
    assert result["row_count"] == 4
    # The advertised next URL must be fetched untouched (params preserved).
    assert seen_urls[1] == "https://api.example.com/items?step=2&custom=1"


def test_paginate_writes_single_merged_artifact() -> None:
    pages = {1: _rows(1, 2), 2: _rows(3, 1)}

    def fetcher(url, headers, timeout_s, method, body):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        return 200, {"content-type": "application/json"}, json.dumps({"data": pages.get(page, [])})

    result = api_replay.paginate_replay(
        run_id="pg_artifact",
        candidate={"endpoint": "https://api.example.com/items?page=*&limit=*", "method": "GET"},
        page_size=2,
        fetcher=fetcher,
    )
    artifact = Path(result["artifact"]["path"])
    assert artifact.exists()
    lines = artifact.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    # Exactly one artifact file in the api_replay subdir (no per-page spam).
    assert len(list(artifact.parent.glob("*.jsonl"))) == 1


# ---------------------------------------------------------------------------
# route_executor wiring
# ---------------------------------------------------------------------------


class _NoSpider:
    def run(self, payload: dict) -> dict:  # pragma: no cover - must not be called
        raise AssertionError("spider_lite should not run when api_replay paginates successfully")


def test_route_executor_auto_paginates_when_target_exceeds_page_size() -> None:
    pages = {1: _rows(1, 2), 2: _rows(3, 2), 3: _rows(5, 1)}

    def fetcher(url, headers, timeout_s, method, body):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        return 200, {"content-type": "application/json"}, json.dumps({"data": pages.get(page, [])})

    result = execute_route(
        {
            "goal": "Extract top 5 records from API",
            "url": "https://example.com/list",
            "allow_network": True,
            "page_size": 2,
            "network_candidates": [
                {"endpoint": "https://api.example.com/items?page=*&limit=*", "method": "GET", "score": 9},
            ],
            "api_replay_fetcher": fetcher,
            "run_id": "route_pg_auto",
        },
        spider_lite=_NoSpider(),
    )

    attempt = next(item for item in result["attempts"] if item["capability"] == "api_replay")
    assert result["status"] == "completed"
    assert result["capability"] == "api_replay"
    assert result["result"]["row_count"] == 5
    assert attempt["page_count"] >= 3
    assert attempt["stop_reason"] in {"short_page", "target_reached"}


def test_route_executor_explicit_paginate_flag() -> None:
    pages = {1: _rows(1, 2), 2: []}

    def fetcher(url, headers, timeout_s, method, body):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        return 200, {"content-type": "application/json"}, json.dumps({"data": pages.get(page, [])})

    result = execute_route(
        {
            "goal": "Extract top 2 records from API",
            "url": "https://example.com/list",
            "allow_network": True,
            "paginate": True,
            "page_size": 2,
            "network_candidates": [
                {"endpoint": "https://api.example.com/items?page=*&limit=*", "method": "GET", "score": 9},
            ],
            "api_replay_fetcher": fetcher,
            "run_id": "route_pg_flag",
        },
        spider_lite=_NoSpider(),
    )

    attempt = next(item for item in result["attempts"] if item["capability"] == "api_replay")
    assert result["status"] == "completed"
    assert result["result"]["row_count"] == 2
    # Target (2) satisfied by the first page -> pagination stops immediately.
    assert attempt["page_count"] == 1
    assert attempt["stop_reason"] == "target_reached"

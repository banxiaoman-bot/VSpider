from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visual_web_agent.extraction_engine import generic


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_extractor_engine_tests"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_extracts_nested_api_json_rows() -> None:
    payload = {
        "code": 0,
        "data": {
            "items": [
                {"id": 1, "name": "Alice", "score": 98},
                {"id": 2, "name": "Bob", "score": 95},
            ]
        },
    }

    result = generic.extract(payload)

    assert result["source_family"] == "API_JSON"
    assert result["row_count"] == 2
    assert result["fields"] == ["id", "name", "score"]
    assert result["rows"][0]["name"] == "Alice"


def test_extracts_html_table_rows() -> None:
    html = """
    <table>
      <thead><tr><th>Name</th><th>Price</th></tr></thead>
      <tbody>
        <tr><td>Phone</td><td>1999</td></tr>
        <tr><td>Laptop</td><td>7999</td></tr>
      </tbody>
    </table>
    """

    result = generic.extract(html, source_type="html")

    assert result["source_family"] == "DOM_TABLE"
    assert result["row_count"] == 2
    assert result["rows"][0] == {"name": "Phone", "price": "1999"}


def test_extracts_html_card_rows() -> None:
    html = """
    <div class="product-card">
      <a class="title" href="/p/1">Phone</a>
      <span class="price">1999</span>
    </div>
    <div class="product-card">
      <a class="title" href="/p/2">Laptop</a>
      <span class="price">7999</span>
    </div>
    """

    result = generic.extract(html, source_type="html")

    assert result["source_family"] == "DOM_CARDS"
    assert result["row_count"] == 2
    assert result["rows"][0]["title"] == "Phone"
    assert result["rows"][0]["price"] == "1999"
    assert result["rows"][0]["url"] == "/p/1"


def test_requested_fields_filtering() -> None:
    result = generic.extract(
        [{"id": 1, "name": "Alice", "score": 98}],
        requested_fields=["name"],
    )

    assert result["fields"] == ["name"]
    assert result["rows"] == [{"name": "Alice"}]


def test_selector_api_css_xpath_text_and_regex() -> None:
    html = """
    <html><body>
      <div class="quote"><span class="text">The world is changed</span><a href="/q/1">More</a></div>
      <div class="quote"><span class="text">I feel it in the water</span><a href="/q/2">More</a></div>
    </body></html>
    """

    css_text = generic.select(html, selector=".quote .text::text")
    css_attr = generic.select(html, selector=".quote a::attr(href)")
    xpath_text = generic.select(html, selector='//span[@class="text"]/text()', selector_type="xpath", mode="first")
    text_hit = generic.select(html, selector="water", selector_type="text", tag="span", mode="first")
    regex_count = generic.select(html, selector=r"\bworld\b", selector_type="regex", mode="count")

    assert css_text["count"] == 2
    assert css_text["results"] == ["The world is changed", "I feel it in the water"]
    assert css_attr["results"] == ["/q/1", "/q/2"]
    assert xpath_text["first"] == "The world is changed"
    assert text_hit["first"] == "I feel it in the water"
    assert regex_count["count"] >= 1
    assert regex_count["results"] == []


def test_export_jsonl(monkeypatch: pytest.MonkeyPatch, local_tmp_path: Path) -> None:
    artifact_root = local_tmp_path / "artifacts"
    monkeypatch.setattr(generic, "resolve_artifact_path", lambda filename, subdir="": artifact_root / subdir / filename)
    monkeypatch.setattr(generic, "register_artifact", lambda path: None)
    monkeypatch.setattr(generic, "artifact_url", lambda path: "/download/" + Path(path).name)

    result = generic.extract([{"id": 1, "name": "Alice"}])
    artifact = generic.export_jsonl(result, run_id="extract_run")

    path = Path(artifact["path"])
    assert path.exists()
    assert artifact["url"].startswith("/download/extract_extract_run_")
    assert path.read_text(encoding="utf-8").strip() == '{"id":1,"name":"Alice"}'


def test_api_extractor_run_endpoint() -> None:
    import api_server

    client = TestClient(api_server.app)
    resp = client.post(
        "/api/extractor/run",
        json={
            "source": {"rows": [{"id": 1, "name": "Alice"}]},
            "source_type": "json",
            "requested_fields": ["name"],
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "success"
    assert payload["artifact"] is None
    assert payload["result"]["rows"] == [{"name": "Alice"}]


def test_api_extractor_select_endpoint() -> None:
    import api_server

    client = TestClient(api_server.app)
    resp = client.post(
        "/api/extractor/select",
        json={
            "source": '<div class="quote"><a href="/q/1">Hello</a></div>',
            "selector": ".quote a::attr(href)",
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "success"
    assert payload["result"]["first"] == "/q/1"


def test_api_extractor_missing_source_returns_400() -> None:
    import api_server

    client = TestClient(api_server.app)
    resp = client.post("/api/extractor/run", json={"source": ""})
    assert resp.status_code == 400


def test_extractor_source_wiring() -> None:
    root = Path(__file__).resolve().parent.parent
    api_src = (root / "api_server.py").read_text(encoding="utf-8")
    init_src = (root / "visual_web_agent" / "extraction_engine" / "__init__.py").read_text(encoding="utf-8")

    assert "from visual_web_agent.extraction_engine import generic as _extractor_engine" in api_src
    assert '@app.post("/api/extractor/run"' in api_src
    assert '@app.post("/api/extractor/select"' in api_src
    assert "_extractor_engine.extract" in api_src
    assert "_extractor_engine.select" in api_src
    assert "_extractor_engine.export_jsonl" in api_src
    assert "from .generic import export_jsonl, extract, extract_html_cards, extract_html_tables, select" in init_src

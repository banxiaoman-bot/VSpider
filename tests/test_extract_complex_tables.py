"""Regression tests for complex-structure extraction (EXTRACT-COMPLEX-1).

Locks in fixes probed during the 2026-06 capability audit:

* ``_TableParser`` / ``_TableGrid`` — nested-table stack isolation and
  rowspan/colspan occupancy expansion
* ``_rows_from_table`` — thead/th header signals, stacked (multi-row) header
  merging, duplicate header dedup, headerless data tables keeping row 0
* ``_extract_json_rows`` — dict-quality weighting beats longer scalar lists
* ``_select_css_nodes`` — direct-child combinator ``>``
* ``cascader_pick.parse`` — trailing CJK punctuation strip and the
  ``级联选择：`` trigger form
"""

from __future__ import annotations

from visual_web_agent.extraction_engine import generic
from visual_web_agent.semantic_macros import cascader_pick


# ════════════════════════════════════════════════════════════════════
#                       TABLE: rowspan / colspan
# ════════════════════════════════════════════════════════════════════


class TestRowColSpan:
    HTML = """
    <table>
      <tr><th>区域</th><th>城市</th><th>销量</th></tr>
      <tr><td rowspan="2">华北</td><td>北京</td><td>100</td></tr>
      <tr><td>天津</td><td>80</td></tr>
      <tr><td colspan="2">华东合计</td><td>300</td></tr>
    </table>
    """

    def test_rowspan_value_carries_down(self) -> None:
        result = generic.extract(self.HTML, source_type="html")
        rows = result["rows"]
        assert rows[1] == {"区域": "华北", "城市": "天津", "销量": "80"}

    def test_colspan_expands_without_column_shift(self) -> None:
        result = generic.extract(self.HTML, source_type="html")
        assert result["rows"][2]["销量"] == "300"

    def test_malicious_span_is_clamped(self) -> None:
        html = '<table><tr><th>a</th></tr><tr><td colspan="99999" rowspan="99999">x</td></tr></table>'
        result = generic.extract(html, source_type="html")
        # Must terminate fast and not blow up width beyond the clamp.
        assert result["row_count"] >= 1
        assert len(result["fields"]) <= generic._TableParser._MAX_SPAN


class TestStackedHeaders:
    HTML = """
    <table>
      <thead>
        <tr><th rowspan="2">姓名</th><th colspan="2">成绩</th></tr>
        <tr><th>语文</th><th>数学</th></tr>
      </thead>
      <tbody>
        <tr><td>张三</td><td>90</td><td>85</td></tr>
      </tbody>
    </table>
    """

    def test_leaf_labels_win(self) -> None:
        result = generic.extract(self.HTML, source_type="html")
        assert result["fields"] == ["姓名", "语文", "数学"]
        assert result["rows"][0] == {"姓名": "张三", "语文": "90", "数学": "85"}


class TestNestedTables:
    HTML = """
    <table>
      <tr><th>订单号</th><th>明细</th></tr>
      <tr>
        <td>ORD-1</td>
        <td>
          <table>
            <tr><th>商品</th><th>数量</th></tr>
            <tr><td>苹果</td><td>3</td></tr>
          </table>
        </td>
      </tr>
      <tr><td>ORD-2</td><td>无</td></tr>
    </table>
    """

    def test_outer_rows_survive_inner_table(self) -> None:
        result = generic.extract(self.HTML, source_type="html")
        ids = [row.get("订单号") for row in result["rows"]]
        assert "ORD-1" in ids and "ORD-2" in ids

    def test_inner_header_rows_do_not_leak_into_outer(self) -> None:
        result = generic.extract(self.HTML, source_type="html")
        flat = " ".join(str(v) for row in result["rows"] for v in row.values())
        assert "商品" not in flat and "数量" not in flat


class TestHeaderHeuristics:
    def test_duplicate_th_headers_get_suffixed(self) -> None:
        html = """
        <table>
          <tr><th>名称</th><th>价格&nbsp;(&yen;)</th><th>名称</th></tr>
          <tr><td>  </td><td></td><td></td></tr>
          <tr><td>商品&amp;A</td><td>1,999</td><td>🔥热卖</td></tr>
        </table>
        """
        result = generic.extract(html, source_type="html")
        assert result["fields"] == ["名称", "价格", "名称_2"]
        assert result["row_count"] == 1
        assert result["rows"][0]["名称"] == "商品&A"

    def test_headerless_data_table_keeps_first_row(self) -> None:
        html = """
        <table>
          <tr><td>iPhone 15</td><td>5999</td></tr>
          <tr><td>iPhone 15</td><td>4999</td></tr>
          <tr><td>Pixel 9</td><td>4999</td></tr>
        </table>
        """
        result = generic.extract(html, source_type="html")
        assert result["row_count"] == 3
        assert result["fields"] == ["col_1", "col_2"]

    def test_headerless_text_to_numeric_flip_still_promotes_header(self) -> None:
        """No <th> but row0 is all-text while the body column is numeric —
        the legacy header guess must keep working (td-only header rows)."""
        html = """
        <table>
          <tr><td>Name</td><td>Price</td></tr>
          <tr><td>Phone</td><td>1999</td></tr>
          <tr><td>Laptop</td><td>7999</td></tr>
        </table>
        """
        result = generic.extract(html, source_type="html")
        assert result["fields"] == ["name", "price"]
        assert result["row_count"] == 2


# ════════════════════════════════════════════════════════════════════
#                       MULTI-TABLE pages
# ════════════════════════════════════════════════════════════════════


class TestMultiTablePages:
    HTML = """
    <h2>表A</h2>
    <table><tr><th>a</th></tr><tr><td>1</td></tr></table>
    <h2>表B（更大）</h2>
    <table><tr><th>x</th><th>y</th></tr>
      <tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></table>
    """

    def test_default_keeps_largest_table_and_reports_count(self) -> None:
        result = generic.extract(self.HTML, source_type="html")
        assert result["fields"] == ["x", "y"]
        assert result["row_count"] == 2
        assert result["table_count"] == 2
        assert "tables" not in result

    def test_all_tables_returns_every_table(self) -> None:
        result = generic.extract(self.HTML, source_type="html", all_tables=True)
        tables = result["tables"]
        assert len(tables) == 2
        assert tables[0]["fields"] == ["x", "y"] and tables[0]["row_count"] == 2
        assert tables[1]["fields"] == ["a"] and tables[1]["rows"] == [{"a": "1"}]

    def test_requested_fields_filter_applies_per_table(self) -> None:
        result = generic.extract(
            self.HTML, source_type="html", all_tables=True, requested_fields=["x"]
        )
        # 表A 没有 x 列，被过滤为空后整表剔除
        assert len(result["tables"]) == 1
        assert result["tables"][0]["fields"] == ["x"]

    def test_extract_html_tables_all_helper(self) -> None:
        tables = generic.extract_html_tables_all(self.HTML)
        assert [len(t) for t in tables] == [2, 1]

    def test_json_source_reports_zero_tables(self) -> None:
        result = generic.extract({"items": [{"id": 1}]})
        assert result["table_count"] == 0

    def test_api_endpoint_passes_all_tables_through(self) -> None:
        import api_server
        from fastapi.testclient import TestClient

        client = TestClient(api_server.app)
        resp = client.post(
            "/api/extractor/run",
            json={"source": self.HTML, "source_type": "html", "all_tables": True},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["result"]["table_count"] == 2
        assert len(payload["result"]["tables"]) == 2


# ════════════════════════════════════════════════════════════════════
#                       JSON: quality-weighted row pick
# ════════════════════════════════════════════════════════════════════


class TestJsonRowQuality:
    def test_dict_records_beat_longer_scalar_list(self) -> None:
        payload = {
            "meta": {"trace": ["a", "b", "c", "d", "e", "f"]},
            "data": {
                "result": {
                    "records": [
                        {"id": 1, "name": "Alice"},
                        {"id": 2, "name": "Bob"},
                        {"id": 3, "city": "Shanghai"},
                    ]
                }
            },
        }
        result = generic.extract(payload)
        assert result["row_count"] == 3
        assert "name" in result["fields"]

    def test_scalar_list_still_extracts_when_alone(self) -> None:
        result = generic.extract({"tags": ["x", "y", "z"]})
        assert result["row_count"] == 3
        assert result["fields"] == ["value"]


# ════════════════════════════════════════════════════════════════════
#                       CSS: direct-child combinator
# ════════════════════════════════════════════════════════════════════


class TestCssChildCombinator:
    HTML = (
        '<div class="a"><p class="b">hit</p>'
        '<section><p class="b">nested</p></section></div>'
        '<p class="b">outside</p>'
    )

    def test_child_combinator_only_matches_direct_children(self) -> None:
        out = generic.select(self.HTML, selector=".a > .b::text")
        assert out["results"] == ["hit"]

    def test_descendant_still_matches_all_inside(self) -> None:
        out = generic.select(self.HTML, selector=".a .b::text")
        assert sorted(out["results"]) == ["hit", "nested"]

    def test_compact_child_syntax_without_spaces(self) -> None:
        out = generic.select(self.HTML, selector=".a>.b::text")
        assert out["results"] == ["hit"]


# ════════════════════════════════════════════════════════════════════
#                       CASCADER: parser hardening
# ════════════════════════════════════════════════════════════════════


class TestCascaderParseHardening:
    def test_trailing_cjk_comma_stripped_from_last_segment(self) -> None:
        step = cascader_pick.parse(
            "在级联选择器中依次展开：华东 -> 江苏 -> 南京 -> 玄武区，确认完成"
        )
        assert step is not None
        assert step["path"] == ["华东", "江苏", "南京", "玄武区"]

    def test_cascader_colon_trigger_with_mixed_arrows(self) -> None:
        step = cascader_pick.parse("级联选择：北京 → 海淀 -> 中关村")
        assert step is not None
        assert step["path"] == ["北京", "海淀", "中关村"]

    def test_math_comparison_still_not_hijacked(self) -> None:
        assert cascader_pick.parse("比较 3 > 2 > 1 的大小关系") is None

    def test_plain_click_chain_still_not_hijacked(self) -> None:
        assert cascader_pick.parse("点击 A > 然后做 B > 完成 C") is None

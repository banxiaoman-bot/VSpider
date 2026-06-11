"""Field-name case-insensitivity contract lock (Slice EXTRACT-FIELD-CASE-1).

The functional scenario run surfaced that the extraction engine normalises
table headers to lowercase ("Order" -> "order"). Verification showed the
schema-matching chain is already case-tolerant end to end:

- normalize_field_key lowercases and strips separators,
- requested_field_coverage normalises BOTH row keys and requested fields
  through _field_aliases (with bidirectional substring matching),
- filter_undercomplete_rows inherits that tolerance.

No fix was needed; these tests lock the contract so a future "optimisation"
cannot silently reintroduce case-sensitive matching, which would make every
user goal with capitalised field names drop all extracted rows.
"""

from __future__ import annotations

import pytest

from visual_web_agent.extraction_engine.generic import extract as engine_extract
from visual_web_agent.page_data_controller import (
    PageDataController,
    normalize_field_key,
)

HTML = """
<table>
  <thead><tr><th>Order</th><th>Review Count</th><th>客户名称</th></tr></thead>
  <tbody>
    <tr><td>ORD-1</td><td>12</td><td>Ada</td></tr>
    <tr><td>ORD-2</td><td>7</td><td>Lin</td></tr>
  </tbody>
</table>
"""


class TestNormalizeFieldKey:
    @pytest.mark.parametrize(
        "left, right",
        [
            ("Order", "order"),
            ("ORDER", "order"),
            ("Review Count", "review_count"),
            ("review-count", "REVIEW COUNT"),
            ("Source URL", "source_url"),
            ("客户名称", "客户名称"),
        ],
    )
    def test_case_and_separator_insensitive(self, left: str, right: str) -> None:
        assert normalize_field_key(left) == normalize_field_key(right)

    def test_non_string_and_empty_inputs(self) -> None:
        assert normalize_field_key(None) == ""
        assert normalize_field_key(123) == "123"


class TestCoverageCaseTolerance:
    def test_capitalised_goal_fields_match_lowercase_engine_keys(self) -> None:
        """User goals say "Order"/"Review Count"; the engine emits lowercase."""
        controller = PageDataController(requested_fields=["Order", "Review Count"])
        engine_row = {"order": "ORD-1", "review_count": "12"}
        hit, total = controller.requested_field_coverage(engine_row)
        assert (hit, total) == (2, 2)

    def test_lowercase_goal_fields_match_capitalised_row_keys(self) -> None:
        controller = PageDataController(requested_fields=["order", "review count"])
        scraped_row = {"Order": "ORD-1", "Review Count": "12"}
        hit, total = controller.requested_field_coverage(scraped_row)
        assert (hit, total) == (2, 2)

    def test_filter_keeps_rows_despite_case_mismatch(self) -> None:
        controller = PageDataController(requested_fields=["Order", "Review Count"])
        rows = [
            {"order": "ORD-1", "review_count": "12"},
            {"order": "ORD-2", "review_count": "7"},
        ]
        kept, stats = controller.filter_undercomplete_rows(rows)
        assert len(kept) == 2
        assert stats["dropped"] == 0

    def test_chinese_fields_unaffected(self) -> None:
        controller = PageDataController(requested_fields=["客户名称"])
        hit, total = controller.requested_field_coverage({"客户名称": "Ada"})
        assert (hit, total) == (1, 1)


class TestEngineHeaderContract:
    def test_engine_emits_lowercase_keys(self) -> None:
        """Lock the producer side: header keys are lowercased by the engine."""
        rows = engine_extract(HTML, source_type="html")["rows"]
        assert len(rows) == 2
        assert "order" in rows[0]
        assert "Order" not in rows[0]

    def test_engine_keys_flow_into_coverage_end_to_end(self) -> None:
        rows = engine_extract(HTML, source_type="html")["rows"]
        controller = PageDataController(requested_fields=["Order", "客户名称"])
        kept, stats = controller.filter_undercomplete_rows(list(rows))
        assert len(kept) == 2
        assert stats["dropped"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

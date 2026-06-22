"""Regression: ``_check_template`` must be capability-aware.

Root cause of the cross-file ordering flakiness in
``test_route_executor_cross_system`` / ``test_capability_router``: when the
planner matched a template (e.g. ``api_replay`` / ``crawl_pagination``) but a
*different* deterministic capability (``extractor_select``) actually produced
the result, ``_check_template`` evaluated the template's success against the
template's expected result shape (``response`` / ``items`` / ``rows``) -- never
the executing capability's shape (``results``) -- so the template check always
vetoed an otherwise-successful extraction and ``execute_route`` fell back.

These tests pin the corrected, capability-aware behaviour:

- ``crawl_pagination``'s count check honours the executing capability's shape
  (so an ``extractor_select`` result with N results counts as N), and
- the family-specific checks (``api_replay`` / ``login`` / ``visual``) only
  veto when the executing capability belongs to that template's family;
  a different deterministic capability that already met count/fields/artifact
  is not vetoed.
"""

from __future__ import annotations

from visual_web_agent.success_verifier import verify_route_success


def _checks(verification: dict) -> dict[str, bool]:
    return {c["name"]: bool(c["passed"]) for c in verification["checks"]}


class TestCrawlPaginationCountIsCapabilityAware:
    def test_extractor_select_results_count_for_pagination_template(self) -> None:
        route = {"template": {"task_type": "crawl_pagination"}}
        v = verify_route_success(
            route, capability="extractor_select", result={"results": ["A", "B"]}
        )
        assert _checks(v).get("template_count") is True
        assert v["passed"] is True

    def test_spider_results_still_count_for_pagination_template(self) -> None:
        route = {"template": {"task_type": "crawl_pagination"}}
        v = verify_route_success(
            route,
            capability="spider_lite",
            result={"item_count": 3, "items": [{"v": 1}, {"v": 2}, {"v": 3}]},
        )
        assert _checks(v).get("template_count") is True

    def test_empty_extraction_fails_pagination_count(self) -> None:
        route = {"template": {"task_type": "crawl_pagination"}}
        v = verify_route_success(
            route, capability="extractor_select", result={"results": []}
        )
        assert _checks(v).get("template_count") is False


class TestFamilyTemplatesOnlyVetoOwnCapability:
    def test_api_template_skipped_for_extractor_select(self) -> None:
        route = {"template": {"task_type": "api_replay"}}
        v = verify_route_success(
            route, capability="extractor_select", result={"results": ["A", "B"]}
        )
        assert "template_api" not in _checks(v)
        assert v["passed"] is True

    def test_api_template_enforced_for_api_capability(self) -> None:
        route = {"template": {"task_type": "api_replay"}}
        ok = verify_route_success(
            route, capability="api_replay", result={"response": {"ok": True}}
        )
        assert _checks(ok).get("template_api") is True
        rows_ok = verify_route_success(
            route,
            capability="api_replay",
            result={"row_count": 1, "rows": [{"id": 1}]},
        )
        assert _checks(rows_ok).get("template_api") is True
        bad = verify_route_success(route, capability="api_replay", result={})
        assert _checks(bad).get("template_api") is False

    def test_login_template_skipped_for_extractor_select(self) -> None:
        route = {"template": {"task_type": "login_then_action"}}
        v = verify_route_success(
            route, capability="extractor_select", result={"results": ["A"]}
        )
        assert "template_login" not in _checks(v)

    def test_visual_template_skipped_for_extractor_select(self) -> None:
        route = {"template": {"task_type": "visual_recovery"}}
        v = verify_route_success(
            route, capability="extractor_select", result={"results": ["A"]}
        )
        assert "template_visual" not in _checks(v)

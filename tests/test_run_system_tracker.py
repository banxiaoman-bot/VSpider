"""Tests for ``visual_web_agent.run_system_tracker`` (E1b).

Pure-logic coverage: URL->system resolution by domain specificity,
transition recording, sticky behaviour for unknown domains, and the
``build_run_system_tracker`` factory's tolerance of malformed routes.
"""

from __future__ import annotations

from visual_web_agent.run_system_tracker import (
    VERSION,
    RunSystemTracker,
    SystemTransition,
    build_run_system_tracker,
)


_SYSTEMS = [
    {"id": "system_1", "domain": "a.example", "name": "A", "type": "web"},
    {"id": "system_2", "domain": "b.example", "name": "B", "type": "web"},
    {"id": "system_logical", "domain": "", "name": "logical", "type": "logical"},
]


def _tracker() -> RunSystemTracker:
    return RunSystemTracker(systems=[dict(s) for s in _SYSTEMS])


# ---------------------------------------------------------------------------
# resolve_system_for_url
# ---------------------------------------------------------------------------


class TestResolveSystemForUrl:
    def test_exact_domain_match(self) -> None:
        t = _tracker()
        sys = t.resolve_system_for_url("https://a.example/list")
        assert sys is not None and sys["id"] == "system_1"

    def test_subdomain_suffix_match(self) -> None:
        t = _tracker()
        sys = t.resolve_system_for_url("https://www.b.example/form?x=1")
        assert sys is not None and sys["id"] == "system_2"

    def test_unknown_domain_returns_none(self) -> None:
        t = _tracker()
        assert t.resolve_system_for_url("https://other.test/page") is None

    def test_blank_url_returns_none(self) -> None:
        t = _tracker()
        assert t.resolve_system_for_url("") is None
        assert t.resolve_system_for_url("not a url") is None

    def test_logical_system_without_domain_never_matches(self) -> None:
        t = _tracker()
        # No URL should resolve to the domainless logical system.
        assert t.resolve_system_for_url("https://logical/") is None

    def test_port_and_userinfo_stripped(self) -> None:
        t = _tracker()
        sys = t.resolve_system_for_url("https://user:pass@a.example:8443/x")
        assert sys is not None and sys["id"] == "system_1"

    def test_most_specific_domain_wins(self) -> None:
        t = RunSystemTracker(systems=[
            {"id": "broad", "domain": "example.com", "name": "broad", "type": "web"},
            {"id": "specific", "domain": "shop.example.com", "name": "specific", "type": "web"},
        ])
        sys = t.resolve_system_for_url("https://shop.example.com/p/1")
        assert sys is not None and sys["id"] == "specific"


# ---------------------------------------------------------------------------
# observe / transitions
# ---------------------------------------------------------------------------


class TestObserve:
    def test_first_observation_is_a_transition_from_empty(self) -> None:
        t = _tracker()
        tr = t.observe("https://a.example/list", step=1)
        assert isinstance(tr, SystemTransition)
        assert tr.from_system_id == ""
        assert tr.to_system_id == "system_1"
        assert tr.step == 1
        assert t.current_system_id == "system_1"

    def test_same_system_no_transition(self) -> None:
        t = _tracker()
        t.observe("https://a.example/list", step=1)
        again = t.observe("https://a.example/detail/2", step=2)
        assert again is None
        assert len(t.transitions) == 1

    def test_cross_system_records_transition(self) -> None:
        t = _tracker()
        t.observe("https://a.example/list", step=1)
        tr = t.observe("https://b.example/form", step=5)
        assert tr is not None
        assert tr.from_system_id == "system_1"
        assert tr.to_system_id == "system_2"
        assert tr.step == 5
        assert len(t.transitions) == 2

    def test_unknown_domain_keeps_current_sticky(self) -> None:
        t = _tracker()
        t.observe("https://a.example/list", step=1)
        # Wander to an unplanned domain -> no transition, stays system_1.
        assert t.observe("https://cdn.other.test/asset.js", step=2) is None
        assert t.current_system_id == "system_1"
        assert len(t.transitions) == 1

    def test_return_trip_records_two_transitions(self) -> None:
        t = _tracker()
        t.observe("https://a.example/", step=1)
        t.observe("https://b.example/", step=2)
        back = t.observe("https://a.example/done", step=3)
        assert back is not None
        assert back.to_system_id == "system_1"
        assert len(t.transitions) == 3
        assert t.visited_system_ids == ["system_1", "system_2"]

    def test_visited_ids_are_unique_and_ordered(self) -> None:
        t = _tracker()
        for url, step in [
            ("https://a.example/", 1),
            ("https://b.example/", 2),
            ("https://a.example/x", 3),
            ("https://b.example/y", 4),
        ]:
            t.observe(url, step=step)
        assert t.visited_system_ids == ["system_1", "system_2"]


# ---------------------------------------------------------------------------
# cross_system_active + summary
# ---------------------------------------------------------------------------


class TestSummary:
    def test_single_system_not_cross(self) -> None:
        t = _tracker()
        t.observe("https://a.example/list", step=1)
        assert t.cross_system_active is False
        summary = t.summary()
        assert summary["cross_system"] is False
        assert summary["visited_count"] == 1
        assert summary["transition_count"] == 1

    def test_two_systems_cross_active(self) -> None:
        t = _tracker()
        t.observe("https://a.example/list", step=1)
        t.observe("https://b.example/form", step=2)
        assert t.cross_system_active is True
        summary = t.summary()
        assert summary["version"] == VERSION
        assert summary["cross_system"] is True
        assert summary["visited_count"] == 2
        assert summary["transition_count"] == 2
        assert summary["current_system_id"] == "system_2"
        assert len(summary["systems"]) == 3  # includes logical
        assert all("id" in s and "domain" in s for s in summary["systems"])

    def test_summary_transitions_are_serialisable(self) -> None:
        import json

        t = _tracker()
        t.observe("https://a.example/", step=1)
        t.observe("https://b.example/", step=2)
        encoded = json.dumps(t.summary(), ensure_ascii=False)
        decoded = json.loads(encoded)
        assert decoded["transitions"][1]["from_system_id"] == "system_1"
        assert decoded["transitions"][1]["to_system_id"] == "system_2"


# ---------------------------------------------------------------------------
# build_run_system_tracker factory
# ---------------------------------------------------------------------------


class TestBuildRunSystemTracker:
    def test_builds_from_workflow_graph(self) -> None:
        route = {"workflow_graph": {"systems": _SYSTEMS}}
        t = build_run_system_tracker(route)
        assert len(t.systems) == 3
        assert t.resolve_system_for_url("https://a.example/")["id"] == "system_1"

    def test_none_route_yields_inert_tracker(self) -> None:
        t = build_run_system_tracker(None)
        assert t.systems == []
        assert t.observe("https://a.example/", step=1) is None
        assert t.cross_system_active is False

    def test_missing_workflow_graph_yields_inert_tracker(self) -> None:
        t = build_run_system_tracker({"intent": {}})
        assert t.systems == []
        assert t.observe("https://a.example/", step=1) is None

    def test_malformed_systems_filtered(self) -> None:
        route = {"workflow_graph": {"systems": [
            {"id": "system_1", "domain": "a.example", "name": "A", "type": "web"},
            "garbage",
            42,
            {"id": "system_2", "domain": "b.example", "name": "B", "type": "web"},
        ]}}
        t = build_run_system_tracker(route)
        assert len(t.systems) == 2

    def test_systems_not_a_list_yields_inert(self) -> None:
        t = build_run_system_tracker({"workflow_graph": {"systems": "nope"}})
        assert t.systems == []

    def test_inert_tracker_summary_shape(self) -> None:
        t = build_run_system_tracker(None)
        summary = t.summary()
        assert summary["version"] == VERSION
        assert summary["system_count"] == 0
        assert summary["cross_system"] is False
        assert summary["transitions"] == []

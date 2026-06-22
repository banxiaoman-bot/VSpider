"""Tests for ``route_executor`` cross-system metadata (E1).

The deterministic executor stays offline (no browser), but E1 teaches it
to honour a ``per_step_systems`` mapping so every attempt is tagged with
the ``system_id`` the planner assigned, and the result exposes
``systems_involved`` + ``system_attempts`` for downstream consumers.

These tests pin:

- backward compatibility (no ``per_step_systems`` -> single
  ``system_1`` bucket),
- per-capability system tagging on attempts,
- ``system_attempts`` bucketing,
- malformed ``per_step_systems`` is ignored rather than crashing,
- the helper functions ``_normalise_per_step_systems`` and
  ``_stamp_system_metadata`` in isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from visual_web_agent.extraction_engine import generic
from visual_web_agent.route_executor import (
    _build_session_plan,
    _normalise_per_step_systems,
    _stamp_system_metadata,
    execute_route,
)
from visual_web_agent.session_router import SessionRouter, SystemAuthPlan


_SELECTOR_SOURCE = """
<html><body>
  <div class="quote"><span class="text">Alpha</span></div>
  <div class="quote"><span class="text">Beta</span></div>
</body></html>
"""


@pytest.fixture
def _artifact_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(generic, "resolve_artifact_path", lambda filename, subdir="": tmp_path / subdir / filename)
    monkeypatch.setattr(generic, "register_artifact", lambda path: None)
    monkeypatch.setattr(generic, "artifact_url", lambda path: "/download/" + Path(path).name)
    return tmp_path


# ---------------------------------------------------------------------------
# _normalise_per_step_systems
# ---------------------------------------------------------------------------


class TestNormalisePerStepSystems:
    def test_none_returns_empty(self) -> None:
        assert _normalise_per_step_systems(None) == {}

    def test_non_list_returns_empty(self) -> None:
        assert _normalise_per_step_systems({"capability": "x"}) == {}
        assert _normalise_per_step_systems("nope") == {}

    def test_indexes_by_capability(self) -> None:
        raw = [
            {"capability": "extractor_select", "system_id": "system_2", "auth_profile": "logged_in"},
            {"capability": "spider_lite", "system_id": "system_1"},
        ]
        out = _normalise_per_step_systems(raw)
        assert out["extractor_select"]["system_id"] == "system_2"
        assert out["extractor_select"]["auth_profile"] == "logged_in"
        assert out["spider_lite"]["system_id"] == "system_1"
        assert out["spider_lite"]["auth_profile"] == "auto"

    def test_drops_items_missing_capability_or_system(self) -> None:
        raw = [
            {"capability": "", "system_id": "system_1"},
            {"capability": "extractor_select", "system_id": ""},
            {"system_id": "system_1"},
            {"capability": "generic_extractor", "system_id": "system_3"},
            "garbage",
            42,
        ]
        out = _normalise_per_step_systems(raw)
        assert list(out.keys()) == ["generic_extractor"]
        assert out["generic_extractor"]["system_id"] == "system_3"

    def test_captures_step_id(self) -> None:
        raw = [{"capability": "spider_lite", "system_id": "system_1", "step_id": "step_07"}]
        out = _normalise_per_step_systems(raw)
        assert out["spider_lite"]["step_id"] == "step_07"


# ---------------------------------------------------------------------------
# _stamp_system_metadata
# ---------------------------------------------------------------------------


class TestStampSystemMetadata:
    def test_empty_attempts_default_to_system_1(self) -> None:
        systems, buckets = _stamp_system_metadata([], {})
        assert systems == ["system_1"]
        assert buckets == {"system_1": []}

    def test_untagged_attempts_bucket_into_system_1(self) -> None:
        attempts = [
            {"capability": "extractor_select", "status": "attempted"},
            {"capability": "spider_lite", "status": "skipped"},
        ]
        systems, buckets = _stamp_system_metadata(attempts, {})
        assert systems == ["system_1"]
        assert len(buckets["system_1"]) == 2
        assert all(a["system_id"] == "system_1" for a in attempts)

    def test_tagged_attempts_bucket_per_system(self) -> None:
        attempts = [
            {"capability": "extractor_select", "status": "attempted"},
            {"capability": "spider_lite", "status": "attempted"},
        ]
        mapping = {
            "extractor_select": {"system_id": "system_1", "auth_profile": "auto"},
            "spider_lite": {"system_id": "system_2", "auth_profile": "auto"},
        }
        systems, buckets = _stamp_system_metadata(attempts, mapping)
        assert systems == ["system_1", "system_2"]
        assert buckets["system_1"][0]["capability"] == "extractor_select"
        assert buckets["system_2"][0]["capability"] == "spider_lite"

    def test_auth_profile_stamped_when_not_auto(self) -> None:
        attempts = [{"capability": "extractor_select", "status": "attempted"}]
        mapping = {"extractor_select": {"system_id": "system_2", "auth_profile": "logged_in"}}
        _stamp_system_metadata(attempts, mapping)
        assert attempts[0]["auth_profile"] == "logged_in"

    def test_auth_profile_auto_not_stamped(self) -> None:
        attempts = [{"capability": "extractor_select", "status": "attempted"}]
        mapping = {"extractor_select": {"system_id": "system_2", "auth_profile": "auto"}}
        _stamp_system_metadata(attempts, mapping)
        assert "auth_profile" not in attempts[0]

    def test_step_id_stamped_when_present(self) -> None:
        attempts = [{"capability": "spider_lite", "status": "attempted"}]
        mapping = {"spider_lite": {"system_id": "system_1", "auth_profile": "auto", "step_id": "step_09"}}
        _stamp_system_metadata(attempts, mapping)
        assert attempts[0]["step_id"] == "step_09"


# ---------------------------------------------------------------------------
# _stamp_system_metadata with a SessionRouter (E1c-2)
# ---------------------------------------------------------------------------


def _router(systems: list[dict]) -> SessionRouter:
    return SessionRouter(run_id="route_executor", plan=SystemAuthPlan(systems=systems))


class TestStampSystemMetadataWithRouter:
    def test_plan_auth_profile_used_when_per_step_auto(self) -> None:
        attempts = [{"capability": "extractor_select", "status": "attempted"}]
        mapping = {"extractor_select": {"system_id": "system_2", "auth_profile": "auto"}}
        router = _router([{"id": "system_2", "domain": "beta.com", "auth_profile": "beta_login"}])
        _stamp_system_metadata(attempts, mapping, router)
        assert attempts[0]["auth_profile"] == "beta_login"

    def test_per_step_explicit_overrides_plan(self) -> None:
        attempts = [{"capability": "extractor_select", "status": "attempted"}]
        mapping = {"extractor_select": {"system_id": "system_2", "auth_profile": "explicit_login"}}
        router = _router([{"id": "system_2", "domain": "beta.com", "auth_profile": "beta_login"}])
        _stamp_system_metadata(attempts, mapping, router)
        assert attempts[0]["auth_profile"] == "explicit_login"

    def test_no_stamp_when_neither_per_step_nor_plan_declares(self) -> None:
        attempts = [{"capability": "extractor_select", "status": "attempted"}]
        mapping = {"extractor_select": {"system_id": "system_2", "auth_profile": "auto"}}
        router = _router([{"id": "system_2", "domain": "beta.com", "auth_profile": "auto"}])
        _stamp_system_metadata(attempts, mapping, router)
        assert "auth_profile" not in attempts[0]


# ---------------------------------------------------------------------------
# _build_session_plan (E1c-2)
# ---------------------------------------------------------------------------


class TestBuildSessionPlan:
    def test_resolves_profile_and_domain_per_system(self) -> None:
        router = _router([
            {"id": "system_1", "domain": "alpha.com", "auth_profile": "alpha_login"},
            {"id": "system_2", "domain": "beta.com", "auth_profile": "auto"},
        ])
        plan = _build_session_plan(["system_1", "system_2"], router)
        assert plan == [
            {"system_id": "system_1", "auth_profile": "alpha_login", "domain": "alpha.com"},
            {"system_id": "system_2", "auth_profile": "auto", "domain": "beta.com"},
        ]

    def test_unknown_system_defaults_auto_blank_domain(self) -> None:
        router = _router([])
        plan = _build_session_plan(["system_1"], router)
        assert plan == [{"system_id": "system_1", "auth_profile": "auto", "domain": ""}]


# ---------------------------------------------------------------------------
# execute_route end-to-end
# ---------------------------------------------------------------------------


class TestExecuteRouteCrossSystem:
    def test_no_per_step_systems_is_backward_compatible(self, _artifact_tmp) -> None:
        result = execute_route({
            "goal": "提取前 2 条 quote 文本",
            "source": _SELECTOR_SOURCE,
            "selector": ".quote .text::text",
            "export": True,
            "run_id": "xsys_backward_compatible",
        })
        assert result["status"] == "completed"
        assert result["systems_involved"] == ["system_1"]
        assert "system_1" in result["system_attempts"]
        # Every attempt carries a system_id now.
        assert all("system_id" in a for a in result["attempts"])

    def test_per_step_systems_tags_completed_path(self, _artifact_tmp) -> None:
        result = execute_route({
            "goal": "提取前 2 条 quote 文本",
            "source": _SELECTOR_SOURCE,
            "selector": ".quote .text::text",
            "export": True,
            "run_id": "xsys_tags_completed",
            "per_step_systems": [
                {"capability": "extractor_select", "system_id": "system_2", "auth_profile": "logged_in"},
            ],
        })
        assert result["status"] == "completed"
        assert "system_2" in result["systems_involved"]
        assert "system_2" in result["system_attempts"]
        select_attempt = next(
            a for a in result["attempts"] if a["capability"] == "extractor_select"
        )
        assert select_attempt["system_id"] == "system_2"
        assert select_attempt["auth_profile"] == "logged_in"

    def test_fallback_path_emits_system_metadata(self) -> None:
        result = execute_route({
            "goal": "爬取列表数据并导出",
            "url": "https://example.com/",
            "extract": {"selector": ".quote .text::text"},
            # allow_network omitted -> spider_lite is skipped -> fallback/skipped
        })
        assert result["completed"] is False
        assert result["systems_involved"] == ["system_1"]
        assert "system_1" in result["system_attempts"]
        assert all("system_id" in a for a in result["attempts"])

    def test_multi_system_buckets_split_attempts(self) -> None:
        # Force both selector (fails -> attempted/skipped) and spider path
        # so we get >1 capability in attempts, each tagged to its system.
        result = execute_route({
            "goal": "提取数据",
            "source": _SELECTOR_SOURCE,
            "selector": ".does-not-exist::text",  # selector attempt fails verification
            "per_step_systems": [
                {"capability": "extractor_select", "system_id": "system_1"},
                {"capability": "generic_extractor", "system_id": "system_2"},
            ],
        })
        # Selector failed -> generic_extractor attempted next on the source.
        involved = result["systems_involved"]
        assert "system_1" in involved
        # generic_extractor should be tagged to system_2 in its bucket.
        if "generic_extractor" in {a["capability"] for a in result["attempts"]}:
            assert "system_2" in involved
            ge = next(a for a in result["attempts"] if a["capability"] == "generic_extractor")
            assert ge["system_id"] == "system_2"

    def test_malformed_per_step_systems_does_not_crash(self, _artifact_tmp) -> None:
        result = execute_route({
            "goal": "提取前 2 条 quote 文本",
            "source": _SELECTOR_SOURCE,
            "selector": ".quote .text::text",
            "export": True,
            "run_id": "xsys_malformed",
            "per_step_systems": "this is not a list",
        })
        assert result["status"] == "completed"
        assert result["systems_involved"] == ["system_1"]

    def test_execute_route_includes_session_plan(self, _artifact_tmp) -> None:
        """E1c-2: every result exposes a session_plan aligned with
        systems_involved, each entry carrying system_id / auth_profile /
        domain so a downstream executor can pre-resolve sessions."""
        result = execute_route({
            "goal": "提取前 2 条 quote 文本",
            "source": _SELECTOR_SOURCE,
            "selector": ".quote .text::text",
            "export": True,
            "run_id": "xsys_session_plan",
        })
        assert "session_plan" in result
        sp_ids = [entry["system_id"] for entry in result["session_plan"]]
        assert sp_ids == result["systems_involved"]
        assert all(
            {"system_id", "auth_profile", "domain"} <= set(entry)
            for entry in result["session_plan"]
        )

    def test_fallback_path_includes_session_plan(self) -> None:
        result = execute_route({
            "goal": "爬取列表数据并导出",
            "url": "https://example.com/",
            "extract": {"selector": ".quote .text::text"},
        })
        assert "session_plan" in result
        sp_ids = [entry["system_id"] for entry in result["session_plan"]]
        assert sp_ids == result["systems_involved"]

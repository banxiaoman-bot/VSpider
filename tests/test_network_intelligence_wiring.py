"""Behavior-level regression for the browser_env <-> network_intelligence wiring.

The existing ``test_network_intelligence.py::test_browser_and_main_wiring_source_pins``
only pins source strings; it proves the wiring *exists* but not that it
*behaves*. This module exercises the real methods on a bare ``BrowserEnv``
instance (built via ``__new__`` to skip the Playwright-heavy ``__init__``):

* ``configure_network_intelligence`` enables / disables the per-run track and
  clears the dedup set.
* ``_record_network_candidate_safe`` is a no-op when disabled or row-less,
  forwards the correct payload to ``network_intelligence.record_candidate``
  when active, dedups identical ``(url, rows-schema)`` fingerprints, resets the
  dedup set on reconfigure, and degrades gracefully when the Playwright
  ``Response`` is missing attributes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from visual_web_agent import browser_env as be
from visual_web_agent.browser_env import BrowserEnv


def _bare_env() -> BrowserEnv:
    """A ``BrowserEnv`` with only the network-intel fields initialised."""
    env = BrowserEnv.__new__(BrowserEnv)
    env._network_run_id = None
    env._network_candidates_seen = set()
    return env


def _make_response(
    *,
    url: str = "https://api.example.com/items?page=1",
    status: int = 200,
    method: str | None = "GET",
    resource_type: str | None = "xhr",
    content_type: str = "application/json",
    request_headers: dict | None = None,
    post_data: str | None = None,
) -> SimpleNamespace:
    request_kwargs: dict[str, Any] = {"headers": request_headers or {}, "post_data": post_data}
    if method is not None:
        request_kwargs["method"] = method
    if resource_type is not None:
        request_kwargs["resource_type"] = resource_type
    return SimpleNamespace(
        url=url,
        status=status,
        headers={"content-type": content_type},
        request=SimpleNamespace(**request_kwargs),
    )


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture every forwarded call to ``network_intelligence.record_candidate``."""
    calls: list[dict] = []

    def _fake_record(**kwargs: Any) -> dict:
        calls.append(kwargs)
        return kwargs

    monkeypatch.setattr(be, "_record_network_candidate", _fake_record)
    return calls


class TestConfigure:
    def test_enables_run_id(self) -> None:
        env = _bare_env()
        env.configure_network_intelligence("20260531_020153")
        assert env._network_run_id == "20260531_020153"

    def test_blank_run_id_disables(self) -> None:
        env = _bare_env()
        env.configure_network_intelligence("   ")
        assert env._network_run_id is None

    def test_none_disables(self) -> None:
        env = _bare_env()
        env._network_run_id = "old"
        env.configure_network_intelligence(None)
        assert env._network_run_id is None

    def test_reconfigure_clears_seen(self) -> None:
        env = _bare_env()
        env._network_candidates_seen.add("stale")
        env.configure_network_intelligence("run_x")
        assert env._network_candidates_seen == set()


class TestRecordSafe:
    def test_noop_when_disabled(self, captured: list[dict]) -> None:
        env = _bare_env()  # _network_run_id stays None
        env._record_network_candidate_safe(response=_make_response(), rows=[{"id": 1}])
        assert captured == []

    def test_noop_when_no_rows(self, captured: list[dict]) -> None:
        env = _bare_env()
        env.configure_network_intelligence("run_x")
        env._record_network_candidate_safe(response=_make_response(), rows=[])
        assert captured == []

    def test_forwards_payload(self, captured: list[dict]) -> None:
        env = _bare_env()
        env.configure_network_intelligence("run_x")
        rows = [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]
        env._record_network_candidate_safe(
            response=_make_response(
                url="https://api.example.com/list?page=2",
                method="POST",
                status=201,
                content_type="application/json; charset=utf-8",
                request_headers={"Authorization": "Bearer t"},
                post_data='{"q": 1}',
            ),
            rows=rows,
            score=7,
        )
        assert len(captured) == 1
        call = captured[0]
        assert call["run_id"] == "run_x"
        assert call["url"] == "https://api.example.com/list?page=2"
        assert call["method"] == "POST"
        assert call["status"] == 201
        assert call["resource_type"] == "xhr"
        assert call["rows"] == rows
        assert call["score"] == 7
        assert call["content_type"] == "application/json; charset=utf-8"
        assert call["request_headers"] == {"Authorization": "Bearer t"}
        assert call["request_body"] == '{"q": 1}'

    def test_dedup_same_fingerprint(self, captured: list[dict]) -> None:
        env = _bare_env()
        env.configure_network_intelligence("run_x")
        rows = [{"id": 1, "title": "a"}]
        for _ in range(3):
            env._record_network_candidate_safe(
                response=_make_response(url="https://api.example.com/x?page=1"),
                rows=rows,
            )
        assert len(captured) == 1

    def test_different_url_not_deduped(self, captured: list[dict]) -> None:
        env = _bare_env()
        env.configure_network_intelligence("run_x")
        rows = [{"id": 1}]
        env._record_network_candidate_safe(
            response=_make_response(url="https://api.example.com/x?page=1"), rows=rows
        )
        env._record_network_candidate_safe(
            response=_make_response(url="https://api.example.com/x?page=2"), rows=rows
        )
        assert len(captured) == 2

    def test_reconfigure_resets_dedup(self, captured: list[dict]) -> None:
        env = _bare_env()
        env.configure_network_intelligence("run_a")
        rows = [{"id": 1}]
        resp = _make_response(url="https://api.example.com/x?page=1")
        env._record_network_candidate_safe(response=resp, rows=rows)
        env.configure_network_intelligence("run_b")  # clears seen
        env._record_network_candidate_safe(response=resp, rows=rows)
        assert len(captured) == 2

    def test_missing_response_attrs_fall_back(self, captured: list[dict]) -> None:
        env = _bare_env()
        env.configure_network_intelligence("run_x")
        # request lacks method / resource_type / post_data -> safe defaults
        response = SimpleNamespace(
            url="https://api.example.com/y",
            status=200,
            headers={"content-type": "application/json"},
            request=SimpleNamespace(headers={}),
        )
        env._record_network_candidate_safe(response=response, rows=[{"id": 1}])
        assert len(captured) == 1
        call = captured[0]
        assert call["method"] == "GET"
        assert call["resource_type"] == "xhr"
        assert call["request_body"] is None

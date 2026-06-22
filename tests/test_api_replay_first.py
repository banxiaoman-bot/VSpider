"""E6: api_replay priority lift when a capture fixture already exists.

- network_intelligence.find_candidates_for_host: host-level index over
  runs/network/*.jsonl capture files (score-sorted, age-gated).
- capability_router: data-flavoured goals probe the host; on a hit the
  fallback_chain leads with api_replay and signals carry
  api_replay_available (additive contract fields only).
"""
from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

import pytest

from visual_web_agent import network_intelligence as netintel
from visual_web_agent import capability_router


ROWS = [{"id": i, "title": f"row {i}", "price": i * 10} for i in range(1, 25)]


@pytest.fixture()
def local_tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".tmp_api_replay_first_tests"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _record(base: Path, run_id: str, url: str, *, rows=None) -> dict:
    candidate = netintel.record_candidate(
        run_id=run_id,
        url=url,
        rows=rows if rows is not None else ROWS,
        base_dir=base,
    )
    assert candidate is not None
    return candidate


class TestFindCandidatesForHost:
    def test_matches_host_across_runs_sorted_by_score(self, local_tmp_path: Path) -> None:
        _record(local_tmp_path, "run_a", "https://api.example.com/list?page=1", rows=ROWS[:6])
        _record(local_tmp_path, "run_b", "https://api.example.com/search?q=x", rows=ROWS)

        hits = netintel.find_candidates_for_host("api.example.com", base_dir=local_tmp_path)

        assert len(hits) == 2
        scores = [int(h["score"]) for h in hits]
        assert scores == sorted(scores, reverse=True)
        assert {h["run_id"] for h in hits} == {"run_a", "run_b"}

    def test_other_hosts_filtered_out(self, local_tmp_path: Path) -> None:
        _record(local_tmp_path, "run_a", "https://api.example.com/list")
        _record(local_tmp_path, "run_b", "https://other.example.net/list")

        hits = netintel.find_candidates_for_host("api.example.com", base_dir=local_tmp_path)

        assert len(hits) == 1
        assert "api.example.com" in hits[0]["url"]

    def test_min_score_filter(self, local_tmp_path: Path) -> None:
        _record(local_tmp_path, "run_a", "https://api.example.com/list")

        assert netintel.find_candidates_for_host(
            "api.example.com", base_dir=local_tmp_path, min_score=99
        ) == []

    def test_expired_candidates_excluded(self, local_tmp_path: Path) -> None:
        stale = netintel.build_candidate(run_id="run_old", url="https://api.example.com/list", rows=ROWS)
        stale["ts"] = time.time() - 10_000
        netintel._atomic_append_jsonl(netintel.network_path("run_old", local_tmp_path), stale)

        assert netintel.find_candidates_for_host(
            "api.example.com", base_dir=local_tmp_path, max_age_s=3600
        ) == []
        # no age gate -> visible
        assert len(netintel.find_candidates_for_host(
            "api.example.com", base_dir=local_tmp_path, max_age_s=0
        )) == 1

    def test_empty_dir_and_blank_host(self, local_tmp_path: Path) -> None:
        assert netintel.find_candidates_for_host("api.example.com", base_dir=local_tmp_path) == []
        assert netintel.find_candidates_for_host("", base_dir=local_tmp_path) == []


class TestRouterApiReplayFirst:
    GOAL = "抓取列表数据导出 jsonl"
    URL = "https://api.example.com/list"

    def _route(self, monkeypatch, *, hits, goal=GOAL, url=URL, raise_probe=False):
        calls: list[tuple] = []

        def fake_find(host, **kwargs):
            calls.append((host, kwargs))
            if raise_probe:
                raise RuntimeError("probe boom")
            return hits

        monkeypatch.setattr(netintel, "find_candidates_for_host", fake_find)
        route = capability_router.route_task(goal, url=url, context={})
        return route, calls

    def test_capture_hit_leads_chain_and_sets_signal(self, monkeypatch) -> None:
        hit = {"run_id": "run_a", "endpoint": "https://api.example.com/list?page=*", "score": 7}
        route, calls = self._route(monkeypatch, hits=[hit])

        assert calls and calls[0][0] == "api.example.com"
        assert route["signals"].get("api_replay_available") is True
        assert route["fallback_chain"][0]["capability"] == "api_replay"
        capture = route["strategy_context"].get("api_replay_capture")
        assert capture["host"] == "api.example.com"
        assert capture["source_run_id"] == "run_a"
        assert capture["top_endpoint"] == hit["endpoint"]
        assert capture["candidates"] == 1

    def test_no_fixture_keeps_chain_order(self, monkeypatch) -> None:
        route, calls = self._route(monkeypatch, hits=[])

        assert calls  # probed but missed
        assert not route["signals"].get("api_replay_available")
        assert route["fallback_chain"][0]["capability"] != "api_replay"
        assert "api_replay_capture" not in route["strategy_context"]

    def test_probe_failure_is_quiet(self, monkeypatch) -> None:
        route, _ = self._route(monkeypatch, hits=[], raise_probe=True)

        assert not route["signals"].get("api_replay_available")
        assert route["fallback_chain"]  # route still healthy

    def test_no_url_skips_probe(self, monkeypatch) -> None:
        route, calls = self._route(monkeypatch, hits=[{"run_id": "x", "score": 9}], url="")

        assert calls == []
        assert not route["signals"].get("api_replay_available")

    def test_non_data_goal_skips_probe(self, monkeypatch) -> None:
        # neutral URL too: goal+url text must not trip structured/crawl/api regexes
        route, calls = self._route(
            monkeypatch,
            hits=[{"run_id": "x", "score": 9}],
            goal="点击登录按钮",
            url="https://www.example.com/home",
        )

        assert calls == []
        assert not route["signals"].get("api_replay_available")

"""Regression: RPA cache load chain must resolve its cross-module helpers.

After main.py was split into phase modules, the RPA cache load/normalize/write
helpers ended up referenced across `rpa_cache` / `pagination_helpers` /
`rpa_replay` without imports, so `_load_exact_rpa_cache` raised
``NameError: _load_rpa_cache_payload is not defined`` whenever a cache file
existed (crashing every 2nd run of the same url+goal). These tests exercise the
full load chain end-to-end on a temp cache dir.
"""

from __future__ import annotations

import json


def test_normalize_rpa_cache_payload_resolves_helpers() -> None:
    from visual_web_agent.phases.rpa_replay import _normalize_rpa_cache_payload

    out = _normalize_rpa_cache_payload({"trail": [], "replayable": True})
    assert isinstance(out, dict)
    assert "trail" in out


def test_pagination_load_rpa_cache_payload_round_trip(tmp_path) -> None:
    from visual_web_agent.phases.pagination_helpers import _load_rpa_cache_payload

    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps({"trail": [], "replayable": True, "completes_task": True}),
        encoding="utf-8",
    )
    payload = _load_rpa_cache_payload(path)
    assert payload is not None
    assert isinstance(payload, dict)


def test_load_exact_rpa_cache_returns_payload_without_nameerror(tmp_path, monkeypatch) -> None:
    from visual_web_agent.phases import rpa_cache

    monkeypatch.setattr(rpa_cache, "_RPA_CACHE_DIR", tmp_path)
    url = "https://quotes.toscrape.com"
    goal = "提取首页所有名言"
    path = rpa_cache._rpa_cache_path(url, goal, normalized=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "trail": [],
                "replayable": True,
                "completes_task": True,
                "source_url": url,
                "source_goal": goal,
            }
        ),
        encoding="utf-8",
    )

    exact_path, payload, reason = rpa_cache._load_exact_rpa_cache(url, goal)
    assert payload is not None
    assert isinstance(payload, dict)
    assert reason == "normalized exact hash match"

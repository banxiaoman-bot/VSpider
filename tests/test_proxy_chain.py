"""Slice PROXY-1: proxy-chain rotation strategy (pure).

Borrowed from crawl4ai's proxy rotation (RoundRobin / failover). A pure module
that parses heterogeneous proxy specs into Playwright-compatible proxy dicts
(`{server, username?, password?}`) and rotates through them, so the browser
substrate can later swap a single static proxy for a resilient chain
(mission §一 "通用 / 遇阻即换路"). No network, fully stub-testable; browser
wiring is a separate slice (browser_env.py is oversized per workflow §三).
"""

from __future__ import annotations

from types import SimpleNamespace

from visual_web_agent.proxy_chain import (
    ProxyChain,
    build_chain_from_config,
    build_proxy_chain,
    parse_proxy,
    should_rotate_on_challenge,
)


# --- parse_proxy -----------------------------------------------------------

def test_parse_plain_host_port() -> None:
    assert parse_proxy("1.2.3.4:8080") == {"server": "1.2.3.4:8080"}


def test_parse_with_scheme() -> None:
    assert parse_proxy("http://1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}


def test_parse_with_credentials() -> None:
    assert parse_proxy("user:pass@1.2.3.4:8080") == {
        "server": "1.2.3.4:8080",
        "username": "user",
        "password": "pass",
    }


def test_parse_with_scheme_and_credentials() -> None:
    assert parse_proxy("http://user:pass@1.2.3.4:8080") == {
        "server": "http://1.2.3.4:8080",
        "username": "user",
        "password": "pass",
    }


def test_parse_dict_passthrough_and_empty() -> None:
    assert parse_proxy({"server": "h:1", "username": "u", "password": "p"}) == {
        "server": "h:1",
        "username": "u",
        "password": "p",
    }
    assert parse_proxy("") is None
    assert parse_proxy(None) is None
    assert parse_proxy({"server": ""}) is None


# --- rotation --------------------------------------------------------------

def test_round_robin_next_cycles() -> None:
    chain = build_proxy_chain(["a:1", "b:2"], strategy="round_robin")
    assert chain.next()["server"] == "a:1"
    assert chain.next()["server"] == "b:2"
    assert chain.next()["server"] == "a:1"


def test_failover_next_stays_until_marked_failed() -> None:
    chain = build_proxy_chain(["a:1", "b:2"], strategy="failover")
    assert chain.next()["server"] == "a:1"
    assert chain.next()["server"] == "a:1"
    assert chain.mark_failed()["server"] == "b:2"
    assert chain.next()["server"] == "b:2"


def test_current_does_not_advance() -> None:
    chain = build_proxy_chain(["a:1", "b:2"])
    assert chain.current()["server"] == "a:1"
    assert chain.current()["server"] == "a:1"


def test_empty_chain_is_safe() -> None:
    chain = build_proxy_chain([])
    assert len(chain) == 0
    assert chain.next() is None
    assert chain.current() is None
    assert chain.mark_failed() is None


def test_unknown_strategy_falls_back_to_round_robin() -> None:
    chain = build_proxy_chain(["a:1", "b:2"], strategy="nonsense")
    assert chain.strategy == "round_robin"
    assert chain.next()["server"] == "a:1"
    assert chain.next()["server"] == "b:2"


def test_invalid_specs_are_dropped() -> None:
    chain = build_proxy_chain(["", None, "good:1", {"server": ""}])
    assert len(chain) == 1
    assert chain.next()["server"] == "good:1"


# --- build_chain_from_config (browser_env seam) ----------------------------

def test_config_chain_takes_priority() -> None:
    cfg = SimpleNamespace(PROXY_CHAIN=["a:1", "b:2"], PROXY_SERVER="ignored:9", PROXY_STRATEGY="round_robin")
    chain = build_chain_from_config(cfg)
    assert len(chain) == 2
    assert chain.next()["server"] == "a:1"
    assert chain.next()["server"] == "b:2"


def test_config_comma_string_chain_is_split() -> None:
    cfg = SimpleNamespace(PROXY_CHAIN="a:1, b:2 , c:3", PROXY_SERVER="")
    chain = build_chain_from_config(cfg)
    assert len(chain) == 3


def test_config_falls_back_to_single_static_server() -> None:
    cfg = SimpleNamespace(
        PROXY_CHAIN=[],
        PROXY_SERVER="1.2.3.4:8080",
        PROXY_USERNAME="u",
        PROXY_PASSWORD="p",
    )
    chain = build_chain_from_config(cfg)
    assert len(chain) == 1
    assert chain.current() == {"server": "1.2.3.4:8080", "username": "u", "password": "p"}


def test_config_empty_chain_when_no_proxy() -> None:
    cfg = SimpleNamespace(PROXY_CHAIN=[], PROXY_SERVER="")
    chain = build_chain_from_config(cfg)
    assert len(chain) == 0
    assert chain.current() is None


def test_config_strategy_is_honored() -> None:
    cfg = SimpleNamespace(PROXY_CHAIN=["a:1", "b:2"], PROXY_SERVER="", PROXY_STRATEGY="failover")
    chain = build_chain_from_config(cfg)
    assert chain.strategy == "failover"
    assert chain.next()["server"] == "a:1"
    assert chain.next()["server"] == "a:1"


def test_config_missing_attrs_safe() -> None:
    # a bare object with no proxy attributes at all -> empty chain, no crash
    chain = build_chain_from_config(SimpleNamespace())
    assert len(chain) == 0
    assert chain.current() is None


# --- PROXY-3: health scoring + quarantine ----------------------------------

def test_mark_failed_single_advances_by_one() -> None:
    # backward compat: one failure (default threshold 3) just advances, no quarantine
    chain = build_proxy_chain(["a:1", "b:2", "c:3"])
    assert chain.mark_failed()["server"] == "b:2"
    assert chain.healthy_count() == 3


def test_quarantine_after_threshold_skips_proxy() -> None:
    chain = ProxyChain(["a:1", "b:2", "c:3"], quarantine_threshold=2)
    chain.mark_failed({"server": "a:1"})  # a fail #1 -> advance to b
    chain.mark_failed({"server": "a:1"})  # a fail #2 -> a quarantined
    stats = {s["server"]: s for s in chain.stats()}
    assert stats["a:1"]["quarantined"] is True
    assert chain.healthy_count() == 2
    assert chain.current()["server"] != "a:1"


def test_report_success_clears_quarantine() -> None:
    chain = ProxyChain(["a:1", "b:2"], quarantine_threshold=1)
    chain.mark_failed({"server": "a:1"})  # a quarantined immediately
    assert {s["server"]: s for s in chain.stats()}["a:1"]["quarantined"] is True
    chain.report_success({"server": "a:1"})
    s = {x["server"]: x for x in chain.stats()}["a:1"]
    assert s["quarantined"] is False
    assert s["consecutive_failures"] == 0
    assert s["successes"] == 1


def test_all_quarantined_resets_for_fresh_chance() -> None:
    chain = ProxyChain(["a:1", "b:2"], quarantine_threshold=1)
    chain.mark_failed({"server": "a:1"})  # a quarantined
    chain.mark_failed({"server": "b:2"})  # b quarantined -> all -> reset
    assert chain.healthy_count() == 2
    assert chain.current() is not None


def test_stats_shape_and_counts() -> None:
    chain = ProxyChain(["a:1", "b:2"])
    chain.report_success({"server": "a:1"})
    chain.mark_failed({"server": "b:2"})
    stats = {s["server"]: s for s in chain.stats()}
    assert stats["a:1"]["successes"] == 1
    assert stats["b:2"]["failures"] == 1
    assert set(stats["a:1"]) == {
        "server", "successes", "failures", "consecutive_failures", "quarantined",
    }


def test_empty_chain_health_ops_safe() -> None:
    chain = build_proxy_chain([])
    chain.report_success()  # no crash
    assert chain.mark_failed() is None
    assert chain.healthy_count() == 0
    assert chain.stats() == []


# --- PROXY-3: should_rotate_on_challenge policy ----------------------------

def test_rotate_not_detected_is_false() -> None:
    assert should_rotate_on_challenge(SimpleNamespace(detected=False, cleared=True)) is False


def test_rotate_when_not_cleared() -> None:
    result = SimpleNamespace(detected=True, cleared=False, action="hitl")
    assert should_rotate_on_challenge(result) is True


def test_rotate_on_max_hitl() -> None:
    result = SimpleNamespace(detected=True, cleared=True, action="max_hitl")
    assert should_rotate_on_challenge(result) is True


def test_rotate_on_repeated_encounters() -> None:
    result = SimpleNamespace(detected=True, cleared=True, action="passive_wait")
    state = SimpleNamespace(encounter_count=3)
    assert should_rotate_on_challenge(result, state, min_encounters=2) is True


def test_no_rotate_single_cleared_encounter() -> None:
    result = SimpleNamespace(detected=True, cleared=True, action="passive_wait")
    state = SimpleNamespace(encounter_count=1)
    assert should_rotate_on_challenge(result, state, min_encounters=2) is False


def test_rotate_accepts_dict_inputs() -> None:
    assert should_rotate_on_challenge({"detected": True, "cleared": False}) is True
    assert should_rotate_on_challenge({"detected": False}) is False

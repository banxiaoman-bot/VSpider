"""Slice PROXY-4a: browser-substrate proxy reroute mechanism.

The pure rotation policy (PROXY-3 ``should_rotate_on_challenge`` / ``mark_failed``)
already exists; this wires a *minimal* browser-side mechanism on top of it:

- ``BrowserEnv._ensure_proxy_chain`` builds the chain **once** so rotation state
  survives ``restart()`` (``start()`` re-entry must not reset it back to proxy 0).
- ``BrowserEnv.reroute_proxy_on_block`` consults the policy and, when warranted,
  marks the current proxy failed and ``restart``s so the relaunch picks the next.

Pure-Python: ``BrowserEnv()`` instantiates with no Playwright launch (mirrors
``test_notice_severity.py``); ``restart`` is monkeypatched to an async recorder so
no real browser is needed.
"""

from __future__ import annotations

import asyncio

from visual_web_agent.browser_env import BrowserEnv
from visual_web_agent.proxy_chain import ProxyChain


class _Result:
    """Duck-typed stand-in for bot_challenge_guard.BotChallengeStepResult."""

    def __init__(self, detected: bool, cleared: bool = True, action: str = "") -> None:
        self.detected = detected
        self.cleared = cleared
        self.action = action


def _two_proxy_env(monkeypatch) -> tuple[BrowserEnv, list]:
    env = BrowserEnv()
    env._proxy_chain = ProxyChain(["http://p1:1111", "http://p2:2222"])
    calls: list = []

    async def fake_restart(url: str, reason: str = "") -> None:
        calls.append((url, reason))

    monkeypatch.setattr(env, "restart", fake_restart)
    return env, calls


# --- build-once chain (rotation survives restart) --------------------------

def test_ensure_proxy_chain_caches_first_build() -> None:
    env = BrowserEnv()
    assert env._proxy_chain is None
    first = env._ensure_proxy_chain()
    assert first is not None
    # second call must reuse the same object (start() re-entry won't rebuild)
    assert env._ensure_proxy_chain() is first


def test_ensure_proxy_chain_preserves_rotation_state() -> None:
    env = BrowserEnv()
    preset = ProxyChain(["http://a:1", "http://b:2"])
    env._proxy_chain = preset
    preset.mark_failed()  # advance to the second proxy
    got = env._ensure_proxy_chain()
    assert got is preset  # not rebuilt
    assert got.current()["server"] == "http://b:2"  # advanced state kept


# --- reroute_proxy_on_block -------------------------------------------------

def test_reroute_rotates_and_restarts(monkeypatch) -> None:
    env, calls = _two_proxy_env(monkeypatch)
    assert env._proxy_chain.current()["server"] == "http://p1:1111"
    ok = asyncio.run(
        env.reroute_proxy_on_block("https://t.example", result=_Result(True, cleared=False))
    )
    assert ok is True
    assert len(calls) == 1 and calls[0][0] == "https://t.example"
    assert env._proxy_chain.current()["server"] == "http://p2:2222"


def test_no_reroute_when_policy_declines(monkeypatch) -> None:
    env, calls = _two_proxy_env(monkeypatch)
    ok = asyncio.run(
        env.reroute_proxy_on_block("https://t", result=_Result(detected=False))
    )
    assert ok is False
    assert calls == []
    assert env._proxy_chain.current()["server"] == "http://p1:1111"


def test_no_reroute_with_single_proxy(monkeypatch) -> None:
    env, calls = _two_proxy_env(monkeypatch)
    env._proxy_chain = ProxyChain(["http://only:1"])
    ok = asyncio.run(
        env.reroute_proxy_on_block("https://t", result=_Result(True, cleared=False))
    )
    assert ok is False
    assert calls == []


def test_no_reroute_without_chain(monkeypatch) -> None:
    env, calls = _two_proxy_env(monkeypatch)
    env._proxy_chain = None
    ok = asyncio.run(
        env.reroute_proxy_on_block("https://t", result=_Result(True, cleared=False))
    )
    assert ok is False
    assert calls == []


# --- PROXY-4b: per-run reroute budget -------------------------------------

def test_bot_challenge_state_has_reroute_budget_fields() -> None:
    from visual_web_agent.bot_challenge_guard import BotChallengeState

    st = BotChallengeState()
    assert st.reroute_count == 0
    assert st.max_reroute_per_run == 3


def test_reroute_respects_budget(monkeypatch) -> None:
    from visual_web_agent.bot_challenge_guard import BotChallengeState

    env, calls = _two_proxy_env(monkeypatch)
    st = BotChallengeState(max_reroute_per_run=1)
    r1 = asyncio.run(
        env.reroute_proxy_on_block("https://t", result=_Result(True, cleared=False), state=st)
    )
    assert r1 is True
    assert st.reroute_count == 1
    # budget exhausted -> second block does not reroute / restart again
    r2 = asyncio.run(
        env.reroute_proxy_on_block("https://t", result=_Result(True, cleared=False), state=st)
    )
    assert r2 is False
    assert st.reroute_count == 1
    assert len(calls) == 1

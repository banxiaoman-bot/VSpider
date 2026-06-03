"""Proxy-chain rotation strategy (crawl4ai borrow, pure / stdlib only).

Parses heterogeneous proxy specs into Playwright-compatible proxy dicts
(``{"server", "username"?, "password"?}`` — the same shape ``browser_env`` already
feeds Playwright) and rotates through them so the browser substrate can swap a
single static proxy for a resilient chain (mission §一 "通用 / \u9047\u963b\u5373\u6362\u8def").

Strategies:

- ``round_robin`` — every :meth:`ProxyChain.next` returns the next proxy,
  cycling.
- ``failover`` — :meth:`ProxyChain.next` sticks to the current proxy until
  :meth:`ProxyChain.mark_failed` advances past it.

Pure functions / no network, so the whole module is stub-testable. Browser
wiring is a separate slice (``browser_env.py`` is oversized per workflow §三).
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = [
    "ROUND_ROBIN",
    "FAILOVER",
    "parse_proxy",
    "ProxyChain",
    "build_proxy_chain",
    "build_chain_from_config",
    "should_rotate_on_challenge",
]

ROUND_ROBIN = "round_robin"
FAILOVER = "failover"
_VALID_STRATEGIES = {ROUND_ROBIN, FAILOVER}


def parse_proxy(spec: Any) -> dict[str, str] | None:
    """Coerce a proxy ``spec`` into ``{"server", "username"?, "password"?}``.

    Accepts ``"host:port"``, ``"scheme://host:port"``,
    ``"user:pass@host:port"``, ``"scheme://user:pass@host:port"`` and an
    already-shaped ``dict``. Returns ``None`` for empty / server-less specs.
    """
    if spec is None:
        return None
    if isinstance(spec, dict):
        server = str(spec.get("server") or "").strip()
        if not server:
            return None
        out: dict[str, str] = {"server": server}
        if spec.get("username"):
            out["username"] = str(spec["username"])
        if spec.get("password"):
            out["password"] = str(spec["password"])
        return out

    text = str(spec or "").strip()
    if not text:
        return None
    scheme = ""
    rest = text
    if "://" in text:
        head, rest = text.split("://", 1)
        scheme = head + "://"
    username = ""
    password = ""
    if "@" in rest:
        cred, host = rest.rsplit("@", 1)
        if ":" in cred:
            username, password = cred.split(":", 1)
        else:
            username = cred
        rest = host
    server = scheme + rest
    if not rest:
        return None
    out = {"server": server}
    if username:
        out["username"] = username
    if password:
        out["password"] = password
    return out


class ProxyChain:
    """Rotate through a list of proxies (round-robin or failover)."""

    def __init__(
        self,
        proxies: Iterable[Any] = (),
        *,
        strategy: str = ROUND_ROBIN,
        quarantine_threshold: int = 3,
    ) -> None:
        self.proxies: list[dict[str, str]] = [p for p in (parse_proxy(s) for s in proxies) if p]
        strat = str(strategy or ROUND_ROBIN).strip().lower()
        self.strategy = strat if strat in _VALID_STRATEGIES else ROUND_ROBIN
        self._idx = 0
        # health scoring (PROXY-3): per-proxy success/failure counters; a proxy
        # quarantined after `quarantine_threshold` consecutive failures is skipped
        # when advancing until the whole chain is exhausted (then reset).
        self.quarantine_threshold = max(1, int(quarantine_threshold or 1))
        self._stats: list[dict[str, Any]] = [
            {"successes": 0, "failures": 0, "consecutive_failures": 0, "quarantined": False}
            for _ in self.proxies
        ]

    def __len__(self) -> int:
        return len(self.proxies)

    def current(self) -> dict[str, str] | None:
        """The active proxy without advancing (``None`` when the chain empty)."""
        if not self.proxies:
            return None
        return self.proxies[self._idx % len(self.proxies)]

    def next(self) -> dict[str, str] | None:
        """Next proxy: round-robin advances; failover holds the current one."""
        if not self.proxies:
            return None
        if self.strategy == ROUND_ROBIN:
            proxy = self.proxies[self._idx % len(self.proxies)]
            self._idx = (self._idx + 1) % len(self.proxies)
            return proxy
        return self.current()

    def report_success(self, proxy: Any = None) -> None:
        """Record a success for the current (or given) proxy; clears quarantine."""
        if not self.proxies:
            return
        st = self._stats[self._resolve_index(proxy)]
        st["successes"] += 1
        st["consecutive_failures"] = 0
        st["quarantined"] = False

    def mark_failed(self, proxy: Any = None) -> dict[str, str] | None:
        """Record a failure, quarantine the proxy past the threshold, and advance
        to the next healthy (non-quarantined) proxy; return the new current one.

        Backward compatible: with the default threshold a single failure simply
        advances by one (no proxy is quarantined yet).
        """
        if not self.proxies:
            return None
        idx = self._resolve_index(proxy)
        st = self._stats[idx]
        st["failures"] += 1
        st["consecutive_failures"] += 1
        if st["consecutive_failures"] >= self.quarantine_threshold:
            st["quarantined"] = True
        self._advance_to_healthy(after=idx)
        return self.current()

    def healthy_count(self) -> int:
        """Number of proxies not currently quarantined."""
        return sum(1 for st in self._stats if not st["quarantined"])

    def stats(self) -> list[dict[str, Any]]:
        """Per-proxy health snapshot (server + success/failure counters)."""
        out: list[dict[str, Any]] = []
        for proxy, st in zip(self.proxies, self._stats):
            out.append({
                "server": proxy.get("server", ""),
                "successes": int(st["successes"]),
                "failures": int(st["failures"]),
                "consecutive_failures": int(st["consecutive_failures"]),
                "quarantined": bool(st["quarantined"]),
            })
        return out

    def _resolve_index(self, proxy: Any = None) -> int:
        """Index of ``proxy`` (matched by server) or the current index."""
        if proxy is not None and self.proxies:
            if isinstance(proxy, dict):
                server = str(proxy.get("server") or "")
            else:
                server = str(getattr(proxy, "server", "") or proxy or "")
            for i, p in enumerate(self.proxies):
                if p.get("server", "") == server:
                    return i
        return self._idx % len(self.proxies)

    def _advance_to_healthy(self, *, after: int | None = None) -> None:
        """Move ``_idx`` to the next non-quarantined proxy after ``after``.

        If every proxy is quarantined, clear all quarantine flags (give the whole
        chain a fresh chance) and advance by one — the chain never gets stuck
        with no usable proxy.
        """
        n = len(self.proxies)
        if n == 0:
            return
        start = after if after is not None else self._idx
        for step in range(1, n + 1):
            cand = (start + step) % n
            if not self._stats[cand]["quarantined"]:
                self._idx = cand
                return
        for st in self._stats:
            st["quarantined"] = False
        self._idx = (start + 1) % n


def build_proxy_chain(specs: Iterable[Any], *, strategy: str = ROUND_ROBIN) -> ProxyChain:
    """Factory: build a :class:`ProxyChain`; unknown strategy → round-robin."""
    return ProxyChain(specs, strategy=strategy)


def build_chain_from_config(cfg: Any) -> ProxyChain:
    """Build a :class:`ProxyChain` from a config module / object.

    Reads ``PROXY_CHAIN`` (list, or comma-separated str) first; falls back to a
    single-entry chain assembled from ``PROXY_SERVER`` (+ ``PROXY_USERNAME`` /
    ``PROXY_PASSWORD``) for backward compatibility with the legacy single static
    proxy. Returns an empty chain (``current() is None``) when nothing is
    configured. ``PROXY_STRATEGY`` (default round-robin) picks the rotation.

    This is the seam ``browser_env`` uses so proxy logic stays out of that
    oversized file (workflow §三).
    """
    specs: list[Any] = []
    chain_specs = getattr(cfg, "PROXY_CHAIN", None) or []
    if isinstance(chain_specs, str):
        chain_specs = [p.strip() for p in chain_specs.split(",") if p.strip()]
    for spec in chain_specs:
        if spec:
            specs.append(spec)
    if not specs:
        server = str(getattr(cfg, "PROXY_SERVER", "") or "").strip()
        if server:
            single: dict[str, str] = {"server": server}
            user = str(getattr(cfg, "PROXY_USERNAME", "") or "").strip()
            pwd = str(getattr(cfg, "PROXY_PASSWORD", "") or "").strip()
            if user:
                single["username"] = user
            if pwd:
                single["password"] = pwd
            specs.append(single)
    strategy = str(getattr(cfg, "PROXY_STRATEGY", ROUND_ROBIN) or ROUND_ROBIN).strip().lower()
    return ProxyChain(specs, strategy=strategy)


def _attr(obj: Any, key: str, default: Any) -> Any:
    """Read ``key`` from an object or a plain dict (duck-typed access)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def should_rotate_on_challenge(result: Any, state: Any = None, *, min_encounters: int = 2) -> bool:
    """Pure policy: should a bot-challenge step trigger a proxy rotation?

    Duck-typed over ``bot_challenge_guard.BotChallengeStepResult`` (``detected``
    / ``cleared`` / ``action``) and ``BotChallengeState`` (``encounter_count``).
    Rotate when the challenge was detected and either (a) it was not cleared,
    (b) the run hit the HITL ceiling (``action == "max_hitl"``), or (c) the same
    IP keeps getting challenged (``encounter_count >= min_encounters``) — all
    signs the current proxy / IP is flagged. A not-detected step never rotates.

    This is the decision the (future) ``browser_env`` re-route-on-block wiring
    consults; kept pure here so it is stub-testable and stays out of the
    oversized browser substrate (workflow §三).
    """
    if not bool(_attr(result, "detected", False)):
        return False
    if not bool(_attr(result, "cleared", True)):
        return True
    if str(_attr(result, "action", "") or "") == "max_hitl":
        return True
    encounters = int(_attr(state, "encounter_count", 0) or 0) if state is not None else 0
    return encounters >= max(1, int(min_encounters or 1))

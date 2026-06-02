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

    def __init__(self, proxies: Iterable[Any] = (), *, strategy: str = ROUND_ROBIN) -> None:
        self.proxies: list[dict[str, str]] = [p for p in (parse_proxy(s) for s in proxies) if p]
        strat = str(strategy or ROUND_ROBIN).strip().lower()
        self.strategy = strat if strat in _VALID_STRATEGIES else ROUND_ROBIN
        self._idx = 0

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

    def mark_failed(self, proxy: Any = None) -> dict[str, str] | None:
        """Advance past the current/failed proxy; return the new current one."""
        if not self.proxies:
            return None
        self._idx = (self._idx + 1) % len(self.proxies)
        return self.current()


def build_proxy_chain(specs: Iterable[Any], *, strategy: str = ROUND_ROBIN) -> ProxyChain:
    """Factory: build a :class:`ProxyChain`; unknown strategy → round-robin."""
    return ProxyChain(specs, strategy=strategy)

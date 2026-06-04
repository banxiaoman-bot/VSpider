"""SSRF guard: validate outbound URLs before the server fetches them.

VSpider fetches user / sitemap-supplied URLs server-side (``spider_lite``,
``url_seeder``, ``media_harvester``). Without a guard, a crafted URL can point
the server at internal-only addresses -- loopback ``127.0.0.1``, RFC1918
(``10/8``, ``172.16/12``, ``192.168/16``), link-local ``169.254.0.0/16``
(including the cloud-metadata endpoint ``169.254.169.254``), IPv6 ``::1`` /
``fc00::/7`` -- or a non-HTTP scheme (``file://``, ``gopher://``, ``data:`` ...).
That is a classic Server-Side Request Forgery (SSRF) vector.

This module centralises the check (mission §一-3 通用: one guard, every fetch
site) so callers do only ``check_url(url)`` (raises :class:`UrlGuardError`) or
``is_url_allowed(url)`` (bool).

Policy (default = secure, offline-safe):
    1. Scheme must be ``http`` / ``https`` (everything else rejected).
    2. A literal-IP host is rejected when it is private / loopback / link-local
       / reserved / multicast / unspecified -- this catches the textbook
       payloads (``169.254.169.254``, ``127.0.0.1``, ``10.x`` ...) with no DNS.
    3. A hostname on the static blocklist (``localhost``, ``*.localhost``,
       cloud-metadata hostnames) is rejected -- again with no DNS.
    4. DNS resolution of an *arbitrary* hostname (to catch a public name that
       resolves to a private address / DNS-rebinding) is performed only when a
       ``resolver`` is injected or ``VSPIDER_SSRF_RESOLVE_DNS=1`` is set. This
       keeps the offline-first test-suite hermetic by default; production can
       opt in to the stricter check. When resolution runs, **every** resolved
       address must be public (a single private answer rejects the URL).

Escape hatch: ``VSPIDER_ALLOW_PRIVATE_URLS=1`` permits private / loopback
targets for local development / internal scraping (mission §一-3 通用: keep the
tool usable for legitimate local use, but make the relaxation explicit).

The DNS resolver is injectable (``resolver=`` parameter) so the whole module is
unit-testable with no network.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Callable, Iterable, List, Optional
from urllib.parse import urlsplit

__all__ = [
    "UrlGuardError",
    "check_url",
    "is_url_allowed",
    "allow_private_default",
]

# Schemes the server is ever allowed to fetch. Everything else (file, ftp,
# gopher, data, about, javascript, ...) is an SSRF / local-file vector.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that are always unsafe regardless of DNS (offline-safe blocklist):
# loopback aliases + cloud-metadata service names.
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
    }
)

Resolver = Callable[[str], Iterable[str]]


class UrlGuardError(ValueError):
    """Raised when a URL is rejected by the SSRF guard."""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def allow_private_default() -> bool:
    """Whether private / loopback targets are permitted (env opt-in)."""
    return _env_flag("VSPIDER_ALLOW_PRIVATE_URLS", default=False)


def _resolve_dns_default() -> bool:
    """Whether to resolve arbitrary hostnames to IPs by default (env opt-in)."""
    return _env_flag("VSPIDER_SSRF_RESOLVE_DNS", default=False)


def _default_resolver(host: str) -> List[str]:
    """Resolve ``host`` to every A / AAAA address (deduped, scope-id stripped)."""
    infos = socket.getaddrinfo(host, None)
    out: List[str] = []
    seen: set[str] = set()
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]  # drop IPv6 scope id (fe80::1%eth0)
        if addr and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def _hostname_is_statically_blocked(host: str) -> bool:
    """True for loopback aliases / metadata hostnames (no DNS needed)."""
    h = str(host or "").strip().rstrip(".").lower()
    if not h:
        return True
    if h in _BLOCKED_HOSTNAMES:
        return True
    return h.endswith(".localhost")


def _ip_is_blocked(ip: str) -> bool:
    """True when ``ip`` is any non-public address (or unparseable)."""
    try:
        addr = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return True  # unparseable -> fail closed
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) must be judged on the embedded v4 addr.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def check_url(
    url: str,
    *,
    resolver: Optional[Resolver] = None,
    allow_private: Optional[bool] = None,
    resolve_dns: Optional[bool] = None,
) -> None:
    """Raise :class:`UrlGuardError` if ``url`` is unsafe to fetch server-side.

    See the module docstring for the policy. ``allow_private`` defaults to the
    ``VSPIDER_ALLOW_PRIVATE_URLS`` env flag; ``resolve_dns`` defaults to "on
    when a ``resolver`` is given or ``VSPIDER_SSRF_RESOLVE_DNS`` is set".
    Returns ``None`` when the URL is allowed.
    """
    raw = str(url or "").strip()
    if not raw:
        raise UrlGuardError("empty URL")

    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UrlGuardError(f"scheme not allowed: {scheme or '(none)'}")

    try:
        host = parts.hostname
    except ValueError as exc:  # malformed authority (bad IPv6 literal, etc.)
        raise UrlGuardError(f"invalid host in URL: {exc}") from exc
    if not host:
        raise UrlGuardError("URL has no host")

    if allow_private is None:
        allow_private = allow_private_default()
    if allow_private:
        return

    # A literal-IP host is checked directly (no DNS) -- catches the textbook
    # payloads regardless of any resolution policy.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_blocked(str(literal)):
            raise UrlGuardError(f"blocked address: {host}")
        return

    # Hostname: static blocklist always applies (offline-safe).
    if _hostname_is_statically_blocked(host):
        raise UrlGuardError(f"blocked host: {host}")

    if resolve_dns is None:
        resolve_dns = resolver is not None or _resolve_dns_default()
    if not resolve_dns:
        return  # offline-safe default: arbitrary hostname allowed unresolved

    resolve = resolver or _default_resolver
    try:
        addrs = list(resolve(host))
    except Exception as exc:  # DNS failure -> fail closed
        raise UrlGuardError(f"DNS resolution failed for {host}: {exc}") from exc
    if not addrs:
        raise UrlGuardError(f"no addresses resolved for {host}")
    for addr in addrs:
        if _ip_is_blocked(addr):
            raise UrlGuardError(f"blocked address: {host} -> {addr}")


def is_url_allowed(
    url: str,
    *,
    resolver: Optional[Resolver] = None,
    allow_private: Optional[bool] = None,
    resolve_dns: Optional[bool] = None,
) -> bool:
    """Boolean form of :func:`check_url` (never raises)."""
    try:
        check_url(url, resolver=resolver, allow_private=allow_private, resolve_dns=resolve_dns)
        return True
    except UrlGuardError:
        return False

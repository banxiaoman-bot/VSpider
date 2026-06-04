"""SSRF guard regression (url_guard).

VSpider fetches user/sitemap-supplied URLs server-side (spider_lite,
url_seeder, media_harvester). ``url_guard.check_url`` is the central SSRF
gate: it must reject non-HTTP schemes and any URL whose host resolves to a
private / loopback / link-local (cloud-metadata) / reserved address, while
allowing genuine public targets. The resolver is injected so these tests run
with zero network.
"""

from __future__ import annotations

import pytest

from visual_web_agent import url_guard
from visual_web_agent.url_guard import UrlGuardError, check_url, is_url_allowed

# example.com — a stable public address used as the "allowed" baseline.
PUBLIC_IP = "93.184.216.34"


def _resolver(mapping):
    """Build a stub DNS resolver: host -> list[str] of addresses."""
    return lambda host: list(mapping.get(host, []))


class TestSchemeAndShape:
    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_empty_blocked(self, bad):
        assert is_url_allowed(bad) is False

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com/_",
            "data:text/plain,hi",
            "javascript:alert(1)",
            "//example.com/x",  # scheme-relative -> no scheme
            "example.com/x",  # bare host -> no scheme
        ],
    )
    def test_non_http_scheme_blocked(self, url):
        # even with a public-resolving host, a bad scheme is rejected
        assert is_url_allowed(url, resolver=_resolver({"example.com": [PUBLIC_IP]})) is False

    def test_no_host_blocked(self):
        assert is_url_allowed("http://", resolver=_resolver({})) is False


class TestPublicAllowed:
    def test_public_hostname_allowed(self):
        assert (
            is_url_allowed("https://example.com/path", resolver=_resolver({"example.com": [PUBLIC_IP]}))
            is True
        )

    def test_public_literal_ip_allowed(self):
        assert is_url_allowed(f"http://{PUBLIC_IP}/x") is True

    def test_check_url_returns_none_for_public(self):
        # check_url must not raise on a public target
        assert check_url(f"https://{PUBLIC_IP}/") is None


class TestSsrfBlocked:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # loopback
            "10.0.0.5",  # RFC1918
            "172.16.0.1",  # RFC1918
            "192.168.1.1",  # RFC1918
            "169.254.169.254",  # link-local / cloud metadata
            "0.0.0.0",  # unspecified
            "::1",  # IPv6 loopback
            "fd00::1",  # IPv6 ULA (private)
        ],
    )
    def test_literal_private_blocked(self, ip):
        host = f"[{ip}]" if ":" in ip else ip
        assert is_url_allowed(f"http://{host}/x") is False

    def test_localhost_resolving_to_loopback_blocked(self):
        assert (
            is_url_allowed("http://localhost/x", resolver=_resolver({"localhost": ["127.0.0.1"]}))
            is False
        )

    def test_metadata_hostname_blocked(self):
        assert (
            is_url_allowed(
                "http://metadata.internal/latest/meta-data/",
                resolver=_resolver({"metadata.internal": ["169.254.169.254"]}),
            )
            is False
        )

    def test_dns_rebind_mixed_addresses_blocked(self):
        # one public, one private -> must block (defense against DNS rebinding)
        assert (
            is_url_allowed(
                "http://evil.example/x",
                resolver=_resolver({"evil.example": [PUBLIC_IP, "127.0.0.1"]}),
            )
            is False
        )

    def test_dns_failure_blocked(self):
        def boom(host):
            raise OSError("nxdomain")

        assert is_url_allowed("http://nope.example/x", resolver=boom) is False

    def test_no_addresses_resolved_blocked(self):
        assert is_url_allowed("http://empty.example/x", resolver=_resolver({"empty.example": []})) is False

    def test_check_url_raises_for_private(self):
        with pytest.raises(UrlGuardError):
            check_url("http://127.0.0.1/x")


class TestStaticHostnameBlocklist:
    # These are blocked with NO resolver and NO env opt-in (offline-safe).
    @pytest.mark.parametrize(
        "host",
        ["localhost", "LocalHost", "foo.localhost", "metadata.google.internal", "metadata"],
    )
    def test_blocked_without_resolver_or_env(self, host, monkeypatch):
        monkeypatch.delenv("VSPIDER_SSRF_RESOLVE_DNS", raising=False)
        monkeypatch.delenv("VSPIDER_ALLOW_PRIVATE_URLS", raising=False)
        assert is_url_allowed(f"http://{host}/x") is False


class TestOfflineDefaultDnsPolicy:
    def test_plain_hostname_allowed_unresolved_by_default(self, monkeypatch):
        # default (no resolver, env off): an arbitrary hostname is allowed
        # without DNS so the offline test-suite stays hermetic.
        monkeypatch.delenv("VSPIDER_SSRF_RESOLVE_DNS", raising=False)
        monkeypatch.delenv("VSPIDER_ALLOW_PRIVATE_URLS", raising=False)
        assert is_url_allowed("http://some-public-host.example/x") is True

    def test_env_resolve_dns_enables_resolution(self, monkeypatch):
        # opt-in env -> arbitrary hostname is resolved via _default_resolver
        monkeypatch.setenv("VSPIDER_SSRF_RESOLVE_DNS", "1")
        monkeypatch.setattr(url_guard, "_default_resolver", lambda host: ["127.0.0.1"])
        assert is_url_allowed("http://rebind.example/x") is False

    def test_injected_resolver_forces_resolution(self):
        # passing a resolver always resolves, regardless of env
        assert is_url_allowed("http://rebind.example/x", resolver=lambda h: ["10.0.0.1"]) is False

    def test_resolve_dns_false_skips_even_with_resolver(self):
        # explicit resolve_dns=False -> hostname allowed even though resolver given
        assert is_url_allowed("http://x.example/x", resolver=lambda h: ["10.0.0.1"], resolve_dns=False) is True


class TestAllowPrivateOptIn:
    def test_allow_private_param_permits(self):
        assert is_url_allowed("http://127.0.0.1/x", allow_private=True) is True

    def test_allow_private_env_permits(self, monkeypatch):
        monkeypatch.setenv("VSPIDER_ALLOW_PRIVATE_URLS", "1")
        assert is_url_allowed("http://127.0.0.1/x") is True
        assert (
            is_url_allowed("http://localhost/x", resolver=_resolver({"localhost": ["127.0.0.1"]}))
            is True
        )

    def test_default_is_secure(self, monkeypatch):
        monkeypatch.delenv("VSPIDER_ALLOW_PRIVATE_URLS", raising=False)
        assert is_url_allowed("http://127.0.0.1/x") is False

    def test_allow_private_param_overrides_env_off(self, monkeypatch):
        monkeypatch.delenv("VSPIDER_ALLOW_PRIVATE_URLS", raising=False)
        # explicit param wins regardless of env
        assert is_url_allowed("http://10.1.2.3/x", allow_private=True) is True
        assert is_url_allowed("http://10.1.2.3/x", allow_private=False) is False

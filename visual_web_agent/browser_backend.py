from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse


@dataclass(frozen=True)
class BrowserBackendInfo:
    name: str
    kind: str
    transport: str
    supports_headed: bool = True
    supports_remote: bool = False
    supports_stealth: bool = False
    supports_persistent_context: bool = False
    status: str = "available"
    notes: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "transport": self.transport,
            "supports_headed": self.supports_headed,
            "supports_remote": self.supports_remote,
            "supports_stealth": self.supports_stealth,
            "supports_persistent_context": self.supports_persistent_context,
            "status": self.status,
            "notes": list(self.notes),
            "config": dict(self.config),
        }


@dataclass(frozen=True)
class BrowserBackendHealth:
    status: str
    reachable: bool | None = None
    check_kind: str = "metadata"
    checked_at: float = field(default_factory=time.time)
    latency_ms: float | None = None
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reachable": self.reachable,
            "check_kind": self.check_kind,
            "checked_at": self.checked_at,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class BrowserBackendSession:
    playwright: Any | None = None
    browser: Any | None = None
    context: Any | None = None
    page: Any | None = None
    backend_info: dict[str, Any] = field(default_factory=dict)


class BrowserBackend(Protocol):
    def info(self) -> BrowserBackendInfo:
        ...

    def health(self) -> dict[str, Any]:
        ...

    async def create_session(self, *, headed: bool = False) -> BrowserBackendSession:
        ...


class PlaywrightBrowserBackend:
    def info(self) -> BrowserBackendInfo:
        return BrowserBackendInfo(
            name="playwright_chromium",
            kind="playwright",
            transport="local_process",
            supports_headed=True,
            supports_remote=False,
            supports_stealth=False,
            supports_persistent_context=False,
            notes=("Default local Chromium backend used by BrowserControlManager.",),
        )

    def health(self) -> dict[str, Any]:
        return browser_backend_health_from_info(self.info(), reachable=True, check_kind="local_metadata")

    async def create_session(self, *, headed: bool = False) -> BrowserBackendSession:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=not headed)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        return BrowserBackendSession(
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            backend_info=self.info().to_dict(),
        )


class RemotePlaywrightBrowserBackend:
    def __init__(self, *, endpoint: str = "", mode: str = "") -> None:
        self.endpoint = str(endpoint or _remote_endpoint_from_env() or "").strip()
        self.mode = str(mode or _remote_mode_from_env(self.endpoint) or "cdp").strip().lower()

    def info(self) -> BrowserBackendInfo:
        return BrowserBackendInfo(
            name="remote_playwright",
            kind="playwright_remote",
            transport=self.mode if self.mode in {"cdp", "ws"} else "cdp_or_ws",
            supports_headed=True,
            supports_remote=True,
            supports_stealth=False,
            supports_persistent_context=True,
            status="available" if self.endpoint else "planned",
            notes=("Connects to an existing Playwright/CDP browser endpoint when configured.",),
            config={
                "endpoint_configured": bool(self.endpoint),
                "mode": self.mode,
                "env_backend": str(os.getenv("VSPIDER_BROWSER_BACKEND") or ""),
            },
        )

    def health(self) -> dict[str, Any]:
        if not self.endpoint:
            return BrowserBackendHealth(
                status="not_configured",
                reachable=False,
                check_kind="remote_endpoint_config",
                error="remote playwright endpoint is not configured",
                details={"endpoint_configured": False, "mode": self.mode},
            ).to_dict()
        timeout = _browser_health_timeout()
        start = time.perf_counter()
        target = _endpoint_host_port(self.endpoint)
        if target is None:
            return BrowserBackendHealth(
                status="unhealthy",
                reachable=False,
                check_kind="tcp_probe",
                error="remote playwright endpoint host/port is invalid",
                details={"endpoint_configured": True, "mode": self.mode},
            ).to_dict()
        host, port = target
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
            latency_ms = round((time.perf_counter() - start) * 1000, 3)
            return BrowserBackendHealth(
                status="healthy",
                reachable=True,
                check_kind="tcp_probe",
                latency_ms=latency_ms,
                details={"endpoint_configured": True, "mode": self.mode},
            ).to_dict()
        except Exception as exc:
            latency_ms = round((time.perf_counter() - start) * 1000, 3)
            return BrowserBackendHealth(
                status="unhealthy",
                reachable=False,
                check_kind="tcp_probe",
                latency_ms=latency_ms,
                error=f"{type(exc).__name__}: {exc}",
                details={"endpoint_configured": True, "mode": self.mode},
            ).to_dict()

    async def create_session(self, *, headed: bool = False) -> BrowserBackendSession:
        if not self.endpoint:
            raise RuntimeError("remote playwright endpoint is not configured")
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            if self.mode in {"ws", "websocket", "playwright_ws"}:
                browser = await playwright.chromium.connect(self.endpoint)
            else:
                browser = await playwright.chromium.connect_over_cdp(self.endpoint)
            context = await browser.new_context(ignore_https_errors=True)
            page = await context.new_page()
            return BrowserBackendSession(
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
                backend_info=self.info().to_dict(),
            )
        except Exception:
            try:
                await playwright.stop()
            except Exception:
                pass
            raise


def list_browser_backends() -> list[dict[str, Any]]:
    return [
        PlaywrightBrowserBackend().info().to_dict(),
        RemotePlaywrightBrowserBackend().info().to_dict(),
        BrowserBackendInfo(
            name="stealth_browser",
            kind="stealth_remote",
            transport="remote_api",
            supports_headed=True,
            supports_remote=True,
            supports_stealth=True,
            supports_persistent_context=True,
            status="planned",
            notes=("Reserved backend slot for Camofox-like stealth browsers when available.",),
        ).to_dict(),
    ]


def build_default_browser_backend(kind: str = "") -> BrowserBackend:
    selected = str(kind or os.getenv("VSPIDER_BROWSER_BACKEND") or "playwright").strip().lower()
    if selected in {"playwright", "playwright_chromium", "local", "local_playwright"}:
        return PlaywrightBrowserBackend()
    if selected in {"remote", "remote_playwright", "playwright_remote", "cdp", "ws"}:
        return RemotePlaywrightBrowserBackend(mode=selected if selected in {"cdp", "ws"} else "")
    raise ValueError(f"unsupported browser backend: {kind}")


def browser_backend_health_from_info(
    info: BrowserBackendInfo | dict[str, Any],
    *,
    reachable: bool | None = None,
    check_kind: str = "metadata",
) -> dict[str, Any]:
    data = info.to_dict() if isinstance(info, BrowserBackendInfo) else dict(info or {})
    status = str(data.get("status") or "available")
    health_status = "healthy" if status == "available" else "not_configured"
    return BrowserBackendHealth(
        status=health_status,
        reachable=reachable,
        check_kind=check_kind,
        details={
            "backend_status": status,
            "endpoint_configured": bool((data.get("config") or {}).get("endpoint_configured")),
        },
    ).to_dict()


def _remote_endpoint_from_env() -> str:
    for name in (
        "VSPIDER_REMOTE_BROWSER_ENDPOINT",
        "VSPIDER_BROWSER_CDP_ENDPOINT",
        "VSPIDER_BROWSER_WS_ENDPOINT",
    ):
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _remote_mode_from_env(endpoint: str = "") -> str:
    value = str(os.getenv("VSPIDER_REMOTE_BROWSER_MODE") or "").strip().lower()
    if value in {"cdp", "ws", "websocket", "playwright_ws"}:
        return "ws" if value in {"ws", "websocket", "playwright_ws"} else "cdp"
    endpoint_value = str(endpoint or "").strip().lower()
    if endpoint_value.startswith("ws://") or endpoint_value.startswith("wss://"):
        return "ws"
    return "cdp"


def _browser_health_timeout() -> float:
    try:
        value = float(os.getenv("VSPIDER_BROWSER_HEALTH_TIMEOUT_MS", "750")) / 1000.0
    except Exception:
        value = 0.75
    return max(0.05, min(5.0, value))


def _endpoint_host_port(endpoint: str) -> tuple[str, int] | None:
    value = str(endpoint or "").strip()
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"//{value}")
    host = parsed.hostname
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        scheme = parsed.scheme.lower()
        if scheme in {"https", "wss"}:
            port = 443
        elif scheme in {"http", "ws"}:
            port = 80
        else:
            return None
    return host, int(port)

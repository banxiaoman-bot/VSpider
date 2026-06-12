"""Cross-system anti-bot scenarios against the dual-system fixture site.

Alpha (商品目录) stays clean; Beta (订单系统) sits behind a Cloudflare-style
WAF interstitial (``make_beta_handler(store, challenge=True)``):

C1 HTTP 层    - 无 cf_clearance 一律 403 interstitial（GET/POST 同拦），
                /cdn-cgi/challenge 颁发 cookie 后 302 放行回原路径。
C2 探针识别   - 真 Chromium 打开 Beta 时 bot_challenge_guard 的 _PROBE_JS
                必须把 interstitial 判为 cloudflare，而 Alpha 页判为无挑战。
C3 被动等待   - handle_bot_challenge_step（async Chromium）在被动等待窗口内
                靠 cf_clearance / URL 离开 interstitial 自行通过，无 HITL。
C4 跨系统接力 - Alpha 抓 SKU → Beta 过盾 → 登录 → 下单写回 store；
                cf_clearance / beta_session 只存在于 Beta origin，不泄漏给 Alpha。
"""

from __future__ import annotations

import asyncio
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.scenario_site import (
    CHALLENGE_AUTOPASS_MS,
    ScenarioServer,
    ScenarioStore,
    make_alpha_handler,
    make_beta_handler,
)

from visual_web_agent.bot_challenge_guard import (
    _PROBE_JS,
    BotChallengeState,
    classify_probe_payload,
    clearance_from_cookies,
    handle_bot_challenge_step,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ensure_browsers_path() -> None:
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if default.is_dir() and any(default.glob("chromium*")):
        return
    fallback = Path.home() / "AppData" / "Local" / "ms-playwright"
    if fallback.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(fallback)


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright is not installed")
    _ensure_browsers_path()
    pw = sync_playwright().start()
    try:
        instance = pw.chromium.launch(headless=True)
    except Exception as exc:
        pw.stop()
        pytest.skip(f"chromium not launchable: {exc}")
    yield instance
    instance.close()
    pw.stop()


@pytest.fixture(scope="module")
def site():
    """(store, alpha, beta) — beta runs behind the WAF interstitial."""

    shared = ScenarioStore()
    alpha = ScenarioServer(make_alpha_handler(shared), host="127.0.0.1").start()
    beta = ScenarioServer(
        make_beta_handler(shared, challenge=True), host="localhost"
    ).start()
    yield shared, alpha, beta
    alpha.stop()
    beta.stop()


@pytest.fixture()
def store(site):
    shared, _, _ = site
    shared.submissions.clear()
    shared.orders.clear()
    shared.beta_logins.clear()
    shared.waf_blocks = 0
    shared.waf_clearances = 0
    return shared


@pytest.fixture()
def context(browser):
    ctx = browser.new_context()
    yield ctx
    ctx.close()


def _http_get(url: str, cookie: str = "") -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url)
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request) as resp:  # noqa: S310 (local fixture)
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


# ---------------------------------------------------------------------------
# C1 HTTP 层：盾拦截 / 颁发 / 放行
# ---------------------------------------------------------------------------


class TestC1WafHttpGate:
    def test_blocked_without_clearance_then_cleared(self, site, store) -> None:
        _, _, beta = site

        status, _, body = _http_get(f"{beta.base_url}/orders")
        assert status == 403
        text = body.decode("utf-8")
        assert "Just a moment" in text
        assert "challenge-form" in text
        assert store.waf_blocks == 1

        # The clearance endpoint issues the cookie and bounces back.
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(f"{beta.base_url}/cdn-cgi/challenge?next=/orders")
            raise AssertionError("expected 302")
        except urllib.error.HTTPError as err:
            assert err.code == 302
            assert err.headers.get("Location") == "/orders"
            assert "cf_clearance=" in (err.headers.get("Set-Cookie") or "")
        assert store.waf_clearances == 1

        # With the clearance cookie the WAF steps aside and the normal auth
        # flow takes over (redirects to /login which renders 200).
        status, _, body = _http_get(
            f"{beta.base_url}/orders", cookie="cf_clearance=fixture-cleared"
        )
        assert status == 200
        assert "Beta 订单系统 - 登录".encode("utf-8") in body

    def test_post_is_gated_too(self, site, store) -> None:
        _, _, beta = site
        request = urllib.request.Request(
            f"{beta.base_url}/orders/new", data=b"sku=X&qty=1&customer=t"
        )
        try:
            with urllib.request.urlopen(request) as resp:
                status, body = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, body = err.code, err.read()
        assert status == 403
        assert b"challenge-form" in body
        assert store.orders == []


# ---------------------------------------------------------------------------
# C2 探针识别：interstitial → cloudflare；Alpha 干净页 → 无挑战
# ---------------------------------------------------------------------------


class TestC2ProbeClassification:
    def test_interstitial_classified_as_cloudflare(self, site, store, context) -> None:
        _, _, beta = site
        page = context.new_page()
        # Hold the interstitial still so the probe can observe it.
        page.route("**/cdn-cgi/challenge*", lambda route: route.abort())
        page.goto(f"{beta.base_url}/orders", wait_until="domcontentloaded")
        probe = classify_probe_payload(page.evaluate(_PROBE_JS))
        assert probe is not None
        assert probe.vendor == "cloudflare"
        assert "just a moment" in probe.title.lower()

    def test_alpha_page_is_clean(self, site, store, context) -> None:
        _, alpha, _ = site
        page = context.new_page()
        page.goto(f"{alpha.base_url}/", wait_until="domcontentloaded")
        assert classify_probe_payload(page.evaluate(_PROBE_JS)) is None

    def test_beta_clean_after_autopass(self, site, store, context) -> None:
        _, _, beta = site
        page = context.new_page()
        page.goto(f"{beta.base_url}/orders", wait_until="domcontentloaded")
        # Interstitial auto-passes, clearance cookie lands, then /login renders.
        page.wait_for_url("**/login", timeout=10_000)
        assert classify_probe_payload(page.evaluate(_PROBE_JS)) is None
        assert clearance_from_cookies(context.cookies(beta.base_url))


# ---------------------------------------------------------------------------
# C3 被动等待：guard 在 passive window 内自行通过（async Chromium 全链路）
# ---------------------------------------------------------------------------


def _run_async_in_thread(coro_factory, timeout: float = 180.0):
    """asyncio.run in a worker thread.

    The sync Playwright fixture keeps an asyncio loop running on the main
    thread, so asyncio.run() there raises; the async scenario gets its own
    thread + loop + browser instead.
    """

    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # propagate Skipped / assertion errors
            box["error"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    if worker.is_alive():
        raise TimeoutError("async bot-challenge scenario timed out")
    if "error" in box:
        raise box["error"]
    return box["value"]


class _AsyncBrowserAdapter:
    """Minimal async surface bot_challenge_guard expects from BrowserEnv."""

    def __init__(self, page, ctx) -> None:
        self._page = page
        self._context = ctx

    async def _ensure_active_page(self, reason: str = ""):
        return self._page

    @property
    def current_url(self) -> str:
        return self._page.url


class TestC3PassiveWaitClears:
    def test_handle_step_clears_without_hitl(self, site, store) -> None:
        _, _, beta = site
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            pytest.skip("playwright is not installed")
        _ensure_browsers_path()

        hitl_calls: list[str] = []

        async def _hitl(reason: str) -> None:
            hitl_calls.append(reason)

        async def _run():
            async with async_playwright() as pw:
                try:
                    instance = await pw.chromium.launch(headless=True)
                except Exception as exc:
                    pytest.skip(f"chromium not launchable: {exc}")
                ctx = await instance.new_context()
                page = await ctx.new_page()
                await page.goto(
                    f"{beta.base_url}/orders", wait_until="domcontentloaded"
                )
                adapter = _AsyncBrowserAdapter(page, ctx)
                state = BotChallengeState()
                result = await handle_bot_challenge_step(
                    adapter,
                    state,
                    hitl_callback=_hitl,
                    passive_wait_seconds=max(8.0, CHALLENGE_AUTOPASS_MS / 1000 * 6),
                    poll_interval=0.4,
                )
                cookies = await ctx.cookies(beta.base_url)
                await instance.close()
                return result, state, cookies

        result, state, cookies = _run_async_in_thread(_run)

        assert result.detected is True
        assert result.cleared is True
        assert result.action == "passive_wait"
        assert result.vendor == "cloudflare"
        assert hitl_calls == []
        assert state.hitl_count == 0
        assert state.encounter_count == 1
        assert state.last_vendor == "cloudflare"
        assert clearance_from_cookies(cookies)
        assert store.waf_clearances >= 1


# ---------------------------------------------------------------------------
# C4 跨系统接力：Alpha 抓数 → Beta 过盾+登录+下单；clearance 不跨 origin
# ---------------------------------------------------------------------------


class TestC4CrossSystemRelay:
    def test_alpha_to_beta_relay_through_waf(self, site, store, context) -> None:
        _, alpha, beta = site

        # 1) Alpha：抓第一行 SKU（无挑战系统）。
        page = context.new_page()
        page.goto(f"{alpha.base_url}/", wait_until="domcontentloaded")
        sku = page.locator("#product-table tbody tr").first.get_attribute("data-sku")
        assert sku

        # 2) Beta：过盾（auto-pass）→ 落到 /login → 真登录 → /orders。
        page.goto(f"{beta.base_url}/orders", wait_until="domcontentloaded")
        page.wait_for_url("**/login", timeout=10_000)
        page.fill("#l-user", "ops")
        page.fill("#l-pass", "secret")
        page.click("#l-submit")
        page.wait_for_url("**/orders", timeout=10_000)

        # 3) 用 Alpha 的 SKU 在 Beta 下单。
        page.fill("#o-sku", sku)
        page.fill("#o-qty", "3")
        page.fill("#o-customer", "跨系统中继")
        page.click("#o-submit")
        page.wait_for_url("**/orders", timeout=10_000)

        assert store.orders and store.orders[-1]["sku"] == sku
        assert store.beta_logins == ["ops"]
        assert store.waf_blocks >= 1 and store.waf_clearances >= 1

        # 4) clearance / 会话 cookie 只属于 Beta origin，不泄漏到 Alpha。
        beta_cookies = {c["name"] for c in context.cookies(beta.base_url)}
        alpha_cookies = {c["name"] for c in context.cookies(alpha.base_url)}
        assert "cf_clearance" in beta_cookies
        assert "beta_session" in beta_cookies
        assert "cf_clearance" not in alpha_cookies
        assert "beta_session" not in alpha_cookies

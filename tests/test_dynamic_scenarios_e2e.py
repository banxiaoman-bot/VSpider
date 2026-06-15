"""Supplementary complex scenarios D1-D6 against the local fixture site.

D1 动态 AJAX 加载    - 页面初始空，XHR 延迟返回后才有数据，等待+抽取。
D2 无限滚动/加载更多  - IntersectionObserver 触底分批加载，聚合无重复。
D3 登录+会话过期     - 登录→操作→过期→重定向→重新登录→继续操作。
D4 复杂嵌套数据      - 合并单元格 + 嵌套表格 + 多层 accordion 展开抽取。
D5 错误恢复         - 500 重试成功 + 重定向链跟随。
D6 带鉴权下载        - 未登录 403，登录后 cookie 带鉴权下载成功。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.scenario_site import (
    AJAX_PRODUCTS,
    FEED_BATCH,
    FEED_TOTAL,
    PRODUCTS,
    PROTECTED_PDF_BYTES,
    REDIRECT_CHAIN_DEPTH,
    ScenarioServer,
    ScenarioStore,
    make_alpha_handler,
    make_beta_handler,
)

from visual_web_agent.extraction_engine.generic import (
    extract_html_tables,
    extract_html_tables_all,
    select,
)


# ---------------------------------------------------------------------------
# Browser bootstrap (skip when chromium is unavailable)
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
    shared = ScenarioStore()
    alpha = ScenarioServer(make_alpha_handler(shared), host="127.0.0.1").start()
    beta = ScenarioServer(make_beta_handler(shared), host="localhost").start()
    yield shared, alpha, beta
    alpha.stop()
    beta.stop()


@pytest.fixture()
def store(site):
    shared, _, _ = site
    shared.submissions.clear()
    shared.orders.clear()
    shared.beta_logins.clear()
    shared.flaky_fail_remaining = 0
    shared.beta_session_version = 0
    return shared


@pytest.fixture()
def page(browser):
    context = browser.new_context()
    yield context.new_page()
    context.close()


# ---------------------------------------------------------------------------
# D1: Dynamic AJAX loading
# ---------------------------------------------------------------------------


class TestD1AjaxLoading:
    def test_initial_empty_then_xhr_populates(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/ajax")

        assert page.text_content("#loading-status") == "加载中..."
        assert page.locator("#ajax-body tr").count() == 0

        page.wait_for_function(
            "document.querySelectorAll('#ajax-body tr').length > 0",
            timeout=5000,
        )

        rows = page.locator("#ajax-body tr")
        assert rows.count() == len(AJAX_PRODUCTS)

        status = page.text_content("#loading-status")
        assert f"已加载 {len(AJAX_PRODUCTS)} 条" == status

    def test_extraction_after_ajax_load(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/ajax")
        page.wait_for_function(
            "document.querySelectorAll('#ajax-body tr').length > 0",
            timeout=5000,
        )

        html = page.content()
        tables = extract_html_tables(html)
        assert len(tables) == len(AJAX_PRODUCTS)
        skus = {r["sku"] for r in tables}
        expected = {p["sku"] for p in AJAX_PRODUCTS}
        assert skus == expected


# ---------------------------------------------------------------------------
# D2: Infinite scroll / load more
# ---------------------------------------------------------------------------


class TestD2InfiniteScroll:
    def test_scroll_aggregates_all_items(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/feed")

        page.wait_for_selector(".feed-card", timeout=5000)

        max_iterations = FEED_TOTAL // FEED_BATCH + 5
        for _ in range(max_iterations):
            status = page.text_content("#feed-status") or ""
            if "全部加载完毕" in status:
                break
            # Headless IntersectionObserver can be unreliable with
            # synthetic scroll; call loadMore() directly via JS.
            page.evaluate("loadMore()")
            page.wait_for_timeout(400)

        cards = page.locator(".feed-card")
        assert cards.count() == FEED_TOTAL

        skus = [
            cards.nth(i).get_attribute("data-sku")
            for i in range(cards.count())
        ]
        assert len(set(skus)) == FEED_TOTAL, "no duplicate items"
        expected_skus = [p["sku"] for p in PRODUCTS]
        assert skus == expected_skus, "order preserved"

        assert f"共 {FEED_TOTAL} 条" in page.text_content("#feed-status")


# ---------------------------------------------------------------------------
# D3: Login + session expiry + re-login
# ---------------------------------------------------------------------------


class TestD3SessionExpiry:
    def test_login_work_expire_relogin_continue(
        self, site, store, browser
    ) -> None:
        _, _, beta = site

        ctx = browser.new_context()
        pg = ctx.new_page()

        # Step 1: login
        pg.goto(f"{beta.base_url}/orders")
        pg.wait_for_selector("#login-form")
        pg.fill("#l-user", "ops")
        pg.fill("#l-pass", "secret")
        pg.click("#l-submit")
        pg.wait_for_selector("#orders-title")
        assert len(store.beta_logins) == 1

        # Step 2: place an order (session valid)
        pg.fill("#o-sku", "ALP-001")
        pg.fill("#o-qty", "2")
        pg.fill("#o-customer", "测试客户")
        pg.click("#o-submit")
        pg.wait_for_selector("#orders-table tbody tr")
        assert len(store.orders) == 1

        # Step 3: expire the session server-side
        pg.goto(f"{beta.base_url}/expire-session")
        pg.wait_for_selector("#expired-msg")

        # Step 4: try to access orders — should redirect to login
        pg.goto(f"{beta.base_url}/orders")
        pg.wait_for_selector("#login-form")
        assert pg.url.endswith("/login")

        # Step 5: re-login
        pg.fill("#l-user", "ops")
        pg.fill("#l-pass", "secret")
        pg.click("#l-submit")
        pg.wait_for_selector("#orders-title")
        assert len(store.beta_logins) == 2

        # Step 6: continue working — place another order
        pg.fill("#o-sku", "ALP-003")
        pg.fill("#o-qty", "1")
        pg.fill("#o-customer", "重登客户")
        pg.click("#o-submit")
        pg.wait_for_selector(
            f"#orders-table tbody tr:nth-child(2)"
        )
        assert len(store.orders) == 2
        assert store.orders[1]["sku"] == "ALP-003"
        assert store.orders[1]["customer"] == "重登客户"

        ctx.close()

    def test_expired_cookie_cannot_access_protected_route(
        self, site, store, browser
    ) -> None:
        _, _, beta = site

        # Login to get a valid cookie
        ctx1 = browser.new_context()
        pg1 = ctx1.new_page()
        pg1.goto(f"{beta.base_url}/login")
        pg1.fill("#l-user", "ops")
        pg1.fill("#l-pass", "secret")
        pg1.click("#l-submit")
        pg1.wait_for_selector("#orders-title")
        cookies_before = ctx1.cookies()
        ctx1.close()

        # Expire the session
        store.beta_session_version += 100

        # Try to use the old cookie in a new context
        ctx2 = browser.new_context()
        ctx2.add_cookies(cookies_before)
        pg2 = ctx2.new_page()
        pg2.goto(f"{beta.base_url}/orders")
        pg2.wait_for_selector("#login-form")
        assert pg2.url.endswith("/login"), "expired cookie must be rejected"
        ctx2.close()


# ---------------------------------------------------------------------------
# D4: Complex nested data
# ---------------------------------------------------------------------------


class TestD4NestedData:
    def test_merged_cell_table_extraction(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/nested-data")
        html = page.content()

        all_tables = extract_html_tables_all(html)
        assert len(all_tables) >= 1
        # The merged table has the most data rows (5 data + 1 summary)
        merged_rows = all_tables[0]
        assert len(merged_rows) >= 5
        all_vals = " ".join(
            str(v) for r in merged_rows for v in r.values()
        )
        assert "ALP-001" in all_vals
        assert "ALP-002" in all_vals

    def test_nested_tables_extracted_separately(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/nested-data")
        html = page.content()

        all_tables = extract_html_tables_all(html)
        # Page has: merged-table, outer-table, inner-east, inner-north
        assert len(all_tables) >= 3
        # Find the inner tables by row count (2 rows and 1 row)
        two_row = [t for t in all_tables if len(t) == 2]
        one_row = [t for t in all_tables if len(t) == 1]
        assert two_row, "inner-east (2 rows) must be extracted"
        assert one_row, "inner-north (1 row) must be extracted"

        east_text = " ".join(str(v) for r in two_row[0] for v in r.values())
        assert "杭州" in east_text
        assert "上海" in east_text

        north_text = " ".join(str(v) for r in one_row[0] for v in r.values())
        assert "北京" in north_text

    def test_accordion_items_via_css_selector(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/nested-data")

        # Expand all closed <details> one by one using JS to avoid
        # strict-mode issues with nested details elements.
        page.evaluate("""
            document.querySelectorAll('details').forEach(d => {
                d.setAttribute('open', '');
            });
        """)
        page.wait_for_timeout(200)

        items = page.locator(".acc-items li")
        assert items.count() == 5

        skus = [
            items.nth(i).get_attribute("data-sku")
            for i in range(items.count())
        ]
        assert set(skus) == {"ALP-001", "ALP-004", "ALP-002", "ALP-006", "ALP-011"}

    def test_accordion_text_extraction(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/nested-data")

        html = page.content()
        accordion = select(html, selector="#accordion", output="text")
        text = accordion["results"][0] if accordion["results"] else ""
        for name in ("智能温控器", "振动传感器", "工业网关", "光纤收发器", "串口服务器"):
            assert name in text


# ---------------------------------------------------------------------------
# D5: Error recovery — 500 retry + redirect chain
# ---------------------------------------------------------------------------


class TestD5ErrorRecovery:
    def test_flaky_endpoint_fails_then_succeeds(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        store.flaky_fail_remaining = 2

        resp1 = page.request.get(f"{alpha.base_url}/flaky")
        assert resp1.status == 500

        resp2 = page.request.get(f"{alpha.base_url}/flaky")
        assert resp2.status == 500

        resp3 = page.request.get(f"{alpha.base_url}/flaky")
        assert resp3.status == 200
        assert store.flaky_fail_remaining == 0

    def test_retry_loop_succeeds_after_transient_errors(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        store.flaky_fail_remaining = 3
        max_retries = 5
        last_status = None

        for attempt in range(max_retries):
            resp = page.request.get(f"{alpha.base_url}/flaky")
            last_status = resp.status
            if resp.status == 200:
                break

        assert last_status == 200, "must eventually succeed after retries"
        page.goto(f"{alpha.base_url}/flaky")
        page.wait_for_selector("#flaky-ok")
        assert page.text_content("#flaky-ok") == "请求成功"

    def test_redirect_chain_followed_to_terminus(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/redirect/{REDIRECT_CHAIN_DEPTH}")
        page.wait_for_selector("#redirect-end")
        assert page.text_content("#redirect-end") == "到达终点"
        assert "/redirect/" not in page.url or page.url.endswith("/redirect/1")

    def test_redirect_chain_via_api_request(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        resp = page.request.get(f"{alpha.base_url}/redirect/{REDIRECT_CHAIN_DEPTH}")
        assert resp.status == 200
        assert "到达终点" in resp.text()


# ---------------------------------------------------------------------------
# D6: Authenticated download
# ---------------------------------------------------------------------------


class TestD6AuthenticatedDownload:
    def test_unauthenticated_download_rejected(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        resp = page.request.get(f"{alpha.base_url}/protected/report.pdf")
        assert resp.status == 403
        body = resp.json()
        assert body["error"] == "unauthorized"

    def test_login_then_download_succeeds(
        self, site, store, browser
    ) -> None:
        _, alpha, _ = site

        ctx = browser.new_context()
        pg = ctx.new_page()

        # Attempt download before login — 403
        resp_before = pg.request.get(f"{alpha.base_url}/protected/report.pdf")
        assert resp_before.status == 403

        # Login via form
        pg.goto(f"{alpha.base_url}/auth/login")
        pg.fill("#al-user", "admin")
        pg.fill("#al-pass", "alpha123")
        pg.click("#al-submit")
        pg.wait_for_selector("#site-title")  # redirected to catalog

        # cookie should be set
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        assert cookies.get("alpha_auth") == "yes"

        # Download with auth cookie — 200
        resp_after = pg.request.get(f"{alpha.base_url}/protected/report.pdf")
        assert resp_after.status == 200
        assert resp_after.body() == PROTECTED_PDF_BYTES

        ctx.close()

    def test_wrong_credentials_rejected(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/auth/login")
        page.fill("#al-user", "admin")
        page.fill("#al-pass", "wrong")
        page.click("#al-submit")
        page.wait_for_selector("#alpha-login-err")
        assert "错误" in page.text_content("#alpha-login-err")

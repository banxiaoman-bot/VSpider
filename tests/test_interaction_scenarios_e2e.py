"""Interaction-focused scenarios F1-F6 against the local fixture site.

F1 搜索+筛选+排序   - 关键词搜索 + 类目下拉筛选 + 价格排序 + 结果实时更新。
F2 拖拽排序         - 列表项拖拽重排 + 服务端持久化新顺序。
F3 Cookie同意横幅   - 遮罩层阻断交互，接受后解除 + cookie 记录。
F4 多标签页并行     - 同时打开多标签操作不干扰。
F5 键盘交互        - Tab导航 + Enter提交 + 快捷键触发。
F6 剪贴板操作       - 复制按钮写入剪贴板 + 粘贴验证。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.scenario_site import (
    PRODUCTS,
    ScenarioServer,
    ScenarioStore,
    make_alpha_handler,
    make_beta_handler,
)


# ---------------------------------------------------------------------------
# Browser bootstrap
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
    shared.drag_order = [p["sku"] for p in PRODUCTS[:5]]
    shared.consent_given = 0
    return shared


@pytest.fixture()
def page(browser):
    context = browser.new_context()
    yield context.new_page()
    context.close()


# ---------------------------------------------------------------------------
# F1: Search + filter + sort
# ---------------------------------------------------------------------------


class TestF1SearchFilterSort:
    def test_initial_load_shows_all(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/search")

        items = page.locator(".search-item")
        assert items.count() == len(PRODUCTS)
        assert f"找到 {len(PRODUCTS)} 个结果" in page.text_content("#result-count")

    def test_keyword_search_filters(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/search")

        page.fill("#search-input", "网关")
        page.wait_for_timeout(200)

        items = page.locator(".search-item")
        assert items.count() == 1
        assert items.first.get_attribute("data-sku") == "ALP-002"

    def test_category_filter(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/search")

        page.select_option("#filter-category", "传感")
        page.wait_for_timeout(200)

        items = page.locator(".search-item")
        assert items.count() == 3
        skus = {items.nth(i).get_attribute("data-sku") for i in range(items.count())}
        expected = {p["sku"] for p in PRODUCTS if p["category"] == "传感"}
        assert skus == expected

    def test_price_sort_ascending(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/search")

        page.select_option("#sort-by", "price_asc")
        page.wait_for_timeout(200)

        items = page.locator(".search-item")
        prices = [
            float(items.nth(i).get_attribute("data-price"))
            for i in range(items.count())
        ]
        assert prices == sorted(prices)

    def test_combined_search_filter_sort(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/search")

        page.select_option("#filter-category", "网络")
        page.select_option("#sort-by", "price_desc")
        page.wait_for_timeout(200)

        items = page.locator(".search-item")
        assert items.count() == 3
        prices = [
            float(items.nth(i).get_attribute("data-price"))
            for i in range(items.count())
        ]
        assert prices == sorted(prices, reverse=True)


# ---------------------------------------------------------------------------
# F2: Drag and drop reorder
# ---------------------------------------------------------------------------


class TestF2DragReorder:
    def test_drag_reorder_persists_to_server(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/drag")

        items_before = page.locator(".drag-item")
        assert items_before.count() == 5

        original_order = [
            items_before.nth(i).get_attribute("data-sku")
            for i in range(items_before.count())
        ]
        assert original_order == store.drag_order

        # Simulate reorder via JS (drag events unreliable in headless)
        page.evaluate("""
            var list = document.getElementById('drag-list');
            var first = list.children[0];
            list.appendChild(first);
        """)

        page.click("#save-order")
        page.wait_for_function(
            "document.getElementById('drag-status').textContent !== ''",
            timeout=5000,
        )

        assert "已保存" in page.text_content("#drag-status")
        expected = original_order[1:] + [original_order[0]]
        assert store.drag_order == expected


# ---------------------------------------------------------------------------
# F3: Cookie consent banner
# ---------------------------------------------------------------------------


class TestF3CookieConsent:
    def test_overlay_blocks_then_accept_unlocks(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/consent")

        assert page.locator("#consent-overlay").is_visible()
        main_pe = page.evaluate(
            "getComputedStyle(document.getElementById('main-content')).pointerEvents"
        )
        assert main_pe == "none"

        page.click("#consent-accept")
        page.wait_for_timeout(300)

        assert not page.locator("#consent-overlay").is_visible()
        assert store.consent_given == 1

        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        assert cookies.get("consent") == "accepted"

        page.click("#action-btn")
        page.wait_for_timeout(200)
        assert page.text_content("#action-result") == "操作成功"

    def test_reject_also_removes_overlay(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/consent")

        page.click("#consent-reject")
        page.wait_for_timeout(300)

        assert not page.locator("#consent-overlay").is_visible()
        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        assert "consent" not in cookies or cookies.get("consent") != "accepted"

    def test_accepted_cookie_skips_overlay_on_revisit(
        self, site, store, browser
    ) -> None:
        _, alpha, _ = site
        ctx = browser.new_context()
        pg = ctx.new_page()

        pg.goto(f"{alpha.base_url}/consent")
        pg.click("#consent-accept")
        pg.wait_for_timeout(300)

        pg.goto(f"{alpha.base_url}/consent")
        pg.wait_for_timeout(300)
        assert not pg.locator("#consent-overlay").is_visible()

        ctx.close()


# ---------------------------------------------------------------------------
# F4: Multi-tab concurrent operations
# ---------------------------------------------------------------------------


class TestF4MultiTab:
    def test_independent_tabs_dont_interfere(
        self, site, store, browser
    ) -> None:
        _, alpha, _ = site
        ctx = browser.new_context()
        tab1 = ctx.new_page()
        tab2 = ctx.new_page()

        tab1.goto(f"{alpha.base_url}/search")
        tab2.goto(f"{alpha.base_url}/search")

        tab1.fill("#search-input", "温控")
        tab1.wait_for_timeout(200)

        tab2.fill("#search-input", "网关")
        tab2.wait_for_timeout(200)

        items1 = tab1.locator(".search-item")
        items2 = tab2.locator(".search-item")

        assert items1.count() == 1
        assert items1.first.get_attribute("data-sku") == "ALP-001"

        assert items2.count() == 1
        assert items2.first.get_attribute("data-sku") == "ALP-002"

        tab1.close()
        tab2.close()
        ctx.close()

    def test_catalog_and_form_in_parallel_tabs(
        self, site, store, browser
    ) -> None:
        _, alpha, _ = site
        ctx = browser.new_context()
        tab_cat = ctx.new_page()
        tab_form = ctx.new_page()

        tab_cat.goto(alpha.base_url)
        tab_form.goto(f"{alpha.base_url}/validated-form")

        tab_cat.wait_for_selector("#product-table")
        tab_form.wait_for_selector("#vform")

        assert tab_cat.locator("#product-table tbody tr").count() == 4
        tab_form.fill("#vf-name", "并行测试")
        tab_form.fill("#vf-email", "par@test.com")
        tab_form.click("#vf-submit")
        tab_form.wait_for_selector("#vf-done")
        assert "并行测试" in tab_form.text_content("#vf-echo-name")

        assert tab_cat.locator("#product-table tbody tr").count() == 4

        tab_cat.close()
        tab_form.close()
        ctx.close()


# ---------------------------------------------------------------------------
# F5: Keyboard-driven interactions
# ---------------------------------------------------------------------------


class TestF5KeyboardInteraction:
    def test_tab_navigation_order(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/keyboard")

        page.click("#kb-f1")
        page.keyboard.type("A")
        page.keyboard.press("Tab")
        page.keyboard.type("B")
        page.keyboard.press("Tab")
        page.keyboard.type("C")

        assert page.input_value("#kb-f1") == "A"
        assert page.input_value("#kb-f2") == "B"
        assert page.input_value("#kb-f3") == "C"

    def test_enter_submits_form(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/keyboard")

        page.fill("#kb-f1", "X")
        page.fill("#kb-f2", "Y")
        page.fill("#kb-f3", "Z")
        page.press("#kb-f3", "Enter")

        page.wait_for_selector("#kb-done")
        assert page.text_content("#kb-echo") == "X|Y|Z"

    def test_keyboard_shortcut_ctrl_s(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/keyboard")

        page.click("#shortcut-area")
        page.keyboard.press("Control+s")
        page.wait_for_timeout(200)

        assert page.text_content("#shortcut-result") == "快捷保存触发"

    def test_escape_shortcut(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/keyboard")

        page.click("#shortcut-area")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

        assert page.text_content("#shortcut-result") == "关闭触发"


# ---------------------------------------------------------------------------
# F6: Clipboard operations
# ---------------------------------------------------------------------------


class TestF6Clipboard:
    def test_copy_button_writes_to_clipboard(
        self, site, store, browser
    ) -> None:
        _, alpha, _ = site
        ctx = browser.new_context(
            permissions=["clipboard-read", "clipboard-write"]
        )
        pg = ctx.new_page()
        pg.goto(f"{alpha.base_url}/clipboard")

        pg.click("#btn-copy")
        pg.wait_for_function(
            "document.getElementById('copy-status').textContent === '已复制'",
            timeout=5000,
        )

        clip = pg.evaluate("navigator.clipboard.readText()")
        assert clip == "SKU-CLIP-TEST-001"
        ctx.close()

    def test_paste_into_input(self, site, store, browser) -> None:
        _, alpha, _ = site
        ctx = browser.new_context(
            permissions=["clipboard-read", "clipboard-write"]
        )
        pg = ctx.new_page()
        pg.goto(f"{alpha.base_url}/clipboard")

        pg.evaluate("navigator.clipboard.writeText('PASTED-VALUE')")
        pg.click("#paste-target")
        pg.keyboard.press("Control+v")
        pg.wait_for_timeout(300)

        assert pg.input_value("#paste-target") == "PASTED-VALUE"
        ctx.close()

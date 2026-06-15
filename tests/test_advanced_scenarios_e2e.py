"""Advanced scenarios E1-E6 against the local fixture site.

E1 多步向导流程     - 3步采购向导（基本信息→商品→确认），步骤间校验。
E2 动态表单校验     - 客户端验证错误提示 + 条件字段显隐（企业→显示税号）。
E3 浏览器弹窗处理   - alert/confirm/prompt 响应与结果校验。
E4 SPA pushState   - URL 变更无刷新、前进/后退、内容切换。
E5 轮询更新        - 页面自动轮询 API 更新内容，直到完成。
E6 批量操作        - 全选/部分选 + 批量删除/导出，服务端状态正确。
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
    shared.submissions.clear()
    shared.orders.clear()
    shared.wizard_submissions.clear()
    shared.poll_counter = 0
    shared.batch_items = [
        {"id": i, "sku": p["sku"], "name": p["name"], "selected": False}
        for i, p in enumerate(PRODUCTS[:8], start=1)
    ]
    return shared


@pytest.fixture()
def page(browser):
    context = browser.new_context()
    yield context.new_page()
    context.close()


# ---------------------------------------------------------------------------
# E1: Multi-step wizard
# ---------------------------------------------------------------------------


class TestE1Wizard:
    def test_full_wizard_flow(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/wizard/step/1")

        assert "步骤 1/3" in page.text_content("#wizard-progress")
        page.fill("#w-company", "杭州测试科技")
        page.fill("#w-contact", "张三")
        page.fill("#w-phone", "13800138000")
        page.click("#w-next")

        page.wait_for_selector("#wizard-title")
        assert "第2步" in page.text_content("#wizard-title")
        assert "杭州测试科技" in page.text_content("#w-company-echo")
        page.fill("#w-sku", "ALP-005")
        page.fill("#w-qty", "10")
        page.click("#w-next")

        page.wait_for_selector("#wizard-review")
        assert "第3步" in page.text_content("#wizard-title")
        assert page.text_content("#r-company") == "杭州测试科技"
        assert page.text_content("#r-sku") == "ALP-005"
        assert page.text_content("#r-qty") == "10"
        page.click("#w-submit")

        page.wait_for_selector("#wizard-done")
        assert "WZ-0001" in page.text_content("#wizard-order-no")
        assert len(store.wizard_submissions) == 1
        sub = store.wizard_submissions[0]
        assert sub["company"] == "杭州测试科技"
        assert sub["sku"] == "ALP-005"
        assert sub["qty"] == "10"

    def test_wizard_step_validation_blocks_empty_fields(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/wizard/step/1")
        page.fill("#w-company", "")
        page.fill("#w-contact", "")

        # HTML5 required will block in Playwright, so submit via JS
        page.evaluate("document.getElementById('wizard-form').submit()")
        page.wait_for_selector("#wizard-err")
        assert "必填" in page.text_content("#wizard-err")
        assert len(store.wizard_submissions) == 0

    def test_wizard_back_navigation(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/wizard/step/1")
        page.fill("#w-company", "回退测试公司")
        page.fill("#w-contact", "李四")
        page.click("#w-next")
        page.wait_for_selector("#w-back")

        page.click("#w-back")
        page.wait_for_selector("#w-company")
        assert "步骤 1/3" in page.text_content("#wizard-progress")


# ---------------------------------------------------------------------------
# E2: Dynamic form validation + conditional fields
# ---------------------------------------------------------------------------


class TestE2DynamicFormValidation:
    def test_client_side_validation_shows_errors(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/validated-form")

        page.fill("#vf-name", "")
        page.fill("#vf-email", "not-an-email")
        page.click("#vf-submit")

        assert page.locator("#err-name").is_visible()
        assert page.locator("#err-email").is_visible()

    def test_valid_input_hides_errors(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/validated-form")

        page.fill("#vf-name", "王五")
        page.fill("#vf-email", "wang@test.com")
        page.click("#vf-submit")

        page.wait_for_selector("#vf-done")
        assert page.text_content("#vf-echo-name") == "姓名：王五"

    def test_enterprise_shows_tax_fields(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/validated-form")

        assert not page.locator("#enterprise-fields").is_visible()

        page.select_option("#vf-type", "enterprise")
        assert page.locator("#enterprise-fields").is_visible()

        page.fill("#vf-name", "赵六")
        page.fill("#vf-email", "zhao@corp.com")
        page.fill("#vf-taxid", "91110000MA001")
        page.fill("#vf-license", "京B20260001")
        page.click("#vf-submit")

        page.wait_for_selector("#vf-done")
        assert page.text_content("#vf-echo-type") == "类型：enterprise"
        assert "91110000MA001" in page.text_content("#vf-echo-taxid")

    def test_switch_back_to_personal_hides_fields(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/validated-form")

        page.select_option("#vf-type", "enterprise")
        assert page.locator("#enterprise-fields").is_visible()

        page.select_option("#vf-type", "personal")
        assert not page.locator("#enterprise-fields").is_visible()


# ---------------------------------------------------------------------------
# E3: Browser dialogs (alert / confirm / prompt)
# ---------------------------------------------------------------------------


class TestE3BrowserDialogs:
    def test_alert_accepted(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/dialogs")

        alert_message = None

        def handle_alert(dialog):
            nonlocal alert_message
            alert_message = dialog.message
            dialog.accept()

        page.on("dialog", handle_alert)
        page.click("#btn-alert")
        page.wait_for_timeout(200)
        assert alert_message == "操作成功！"

    def test_confirm_accepted(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/dialogs")

        page.on("dialog", lambda d: d.accept())
        page.click("#btn-confirm")
        page.wait_for_timeout(200)
        assert page.text_content("#confirm-result") == "已确认"

    def test_confirm_dismissed(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/dialogs")

        page.on("dialog", lambda d: d.dismiss())
        page.click("#btn-confirm")
        page.wait_for_timeout(200)
        assert page.text_content("#confirm-result") == "已取消"

    def test_prompt_with_input(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/dialogs")

        page.on("dialog", lambda d: d.accept("自定义备注内容"))
        page.click("#btn-prompt")
        page.wait_for_timeout(200)
        assert page.text_content("#prompt-result") == "自定义备注内容"

    def test_prompt_dismissed(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/dialogs")

        page.on("dialog", lambda d: d.dismiss())
        page.click("#btn-prompt")
        page.wait_for_timeout(200)
        assert page.text_content("#prompt-result") == "(取消)"


# ---------------------------------------------------------------------------
# E4: SPA pushState navigation
# ---------------------------------------------------------------------------


class TestE4SpaNavigation:
    def test_pushstate_changes_url_and_content(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/spa/home")

        assert page.locator("#page-home").is_visible()
        assert not page.locator("#page-products").is_visible()

        page.click("a[data-page='products']")
        page.wait_for_timeout(200)

        assert "/spa/products" in page.url
        assert not page.locator("#page-home").is_visible()
        assert page.locator("#page-products").is_visible()
        assert "工业网关" in page.text_content("#page-products")

    def test_back_forward_navigation(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/spa/home")

        page.click("a[data-page='products']")
        page.wait_for_timeout(200)
        page.click("a[data-page='about']")
        page.wait_for_timeout(200)

        assert "/spa/about" in page.url
        assert page.locator("#page-about").is_visible()

        page.go_back()
        page.wait_for_timeout(200)
        assert "/spa/products" in page.url
        assert page.locator("#page-products").is_visible()

        page.go_back()
        page.wait_for_timeout(200)
        assert "/spa/home" in page.url
        assert page.locator("#page-home").is_visible()

        page.go_forward()
        page.wait_for_timeout(200)
        assert "/spa/products" in page.url
        assert page.locator("#page-products").is_visible()


# ---------------------------------------------------------------------------
# E5: Polling updates
# ---------------------------------------------------------------------------


class TestE5PollingUpdates:
    def test_polling_reaches_completion(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/polling")

        page.wait_for_function(
            "document.getElementById('poll-status').textContent === '已完成'",
            timeout=10000,
        )

        value = page.text_content("#poll-value")
        assert int(value) >= 5
        assert store.poll_counter >= 5

    def test_api_status_increments(self, site, store, page) -> None:
        _, alpha, _ = site
        store.poll_counter = 0

        for i in range(1, 4):
            resp = page.request.get(f"{alpha.base_url}/api/status")
            data = resp.json()
            assert data["progress"] == i
            assert data["done"] is (i >= 5)

        assert store.poll_counter == 3


# ---------------------------------------------------------------------------
# E6: Batch operations
# ---------------------------------------------------------------------------


class TestE6BatchOperations:
    def test_select_all_and_batch_delete(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/batch")

        rows_before = page.locator("#batch-table tbody tr")
        assert rows_before.count() == 8

        page.check("#select-all")
        checks = page.locator(".item-check")
        for i in range(checks.count()):
            assert checks.nth(i).is_checked()

        page.on("dialog", lambda d: d.accept())
        page.click("#btn-delete")
        page.wait_for_function(
            "document.getElementById('batch-status').textContent !== ''",
            timeout=5000,
        )

        assert "已删除 8 项" in page.text_content("#batch-status")
        assert page.locator("#batch-table tbody tr").count() == 0
        assert len(store.batch_items) == 0

    def test_partial_select_and_export(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/batch")

        checks = page.locator(".item-check")
        checks.nth(0).check()
        checks.nth(2).check()

        page.click("#btn-export")
        page.wait_for_function(
            "document.getElementById('batch-status').textContent !== ''",
            timeout=5000,
        )

        assert "已导出 2 项" in page.text_content("#batch-status")
        assert len(store.batch_items) == 8  # export doesn't remove

    def test_delete_partial_removes_from_store(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/batch")

        checks = page.locator(".item-check")
        checks.nth(1).check()
        checks.nth(3).check()

        page.on("dialog", lambda d: d.accept())
        page.click("#btn-delete")
        page.wait_for_function(
            "document.getElementById('batch-status').textContent !== ''",
            timeout=5000,
        )

        assert "已删除 2 项" in page.text_content("#batch-status")
        assert len(store.batch_items) == 6
        remaining_ids = {it["id"] for it in store.batch_items}
        assert 2 not in remaining_ids
        assert 4 not in remaining_ids

    def test_empty_selection_shows_alert(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/batch")

        alert_msg = None

        def handle(d):
            nonlocal alert_msg
            alert_msg = d.message
            d.accept()

        page.on("dialog", handle)
        page.click("#btn-delete")
        page.wait_for_timeout(300)
        assert alert_msg == "请先选择项目"

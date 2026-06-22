"""Complex end-to-end scenarios against the local dual-system fixture site.

Five scenario groups, all driven through a REAL headless Chromium (skipped
cleanly when no Playwright browser is installed) plus VSpider's own
deterministic extraction / media-harvest / session-router code:

S1 复杂表单    - text/email/password/select/radio/checkbox/date/number/
                 textarea/file-upload + iframe 子表单 + shadow-DOM 输入 +
                 contenteditable 富文本, 一次提交, 服务端逐字段校验。
S2 非表单填写  - 详情页 contenteditable 草稿 + localStorage 持久化。
S3 多类型爬取  - 表格/文字 (extraction_engine) + 图片/SVG/CSV/PDF/ZIP
                 真下载 (media_harvester -> manifest.json, sha256 去重)。
S4 跨页面      - 3 页分页聚合 12 行 + target=_blank 新标签详情页跳转。
S5 跨系统      - SessionRouter cookie 子集隔离 (alpha ✗ beta ✓) +
                 真实登录 + A 系统数据中继到 B 系统下单。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.scenario_site import (
    CSV_TEXT,
    PDF_BYTES,
    PNG_BYTES,
    PRODUCTS,
    TOTAL_PAGES,
    ZIP_BYTES,
    ScenarioServer,
    ScenarioStore,
    make_alpha_handler,
    make_beta_handler,
)

from visual_web_agent.extraction_engine.generic import (
    extract_html_tables,
    select,
)
from visual_web_agent.media_harvester import collect_from_html
from visual_web_agent.media_harvester.harvester import harvest_to_run
from visual_web_agent.session_router import SessionRouter, SystemAuthPlan


# ---------------------------------------------------------------------------
# Browser bootstrap (skip when chromium is unavailable)
# ---------------------------------------------------------------------------


def _ensure_browsers_path() -> None:
    """Resolve the ms-playwright cache even when LOCALAPPDATA is redirected
    (sandboxed shells); a wrong path just leads to the launch-skip below."""

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
    """(store, alpha_server, beta_server) - two systems on separate hosts."""

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
    return shared


@pytest.fixture()
def page(browser):
    context = browser.new_context()
    yield context.new_page()
    context.close()


# ---------------------------------------------------------------------------
# S1 复杂表单：原生控件 + iframe 子表单 + shadow DOM + 富文本 + 文件上传
# ---------------------------------------------------------------------------


class TestS1ComplexFormSubmission:
    def test_full_form_roundtrip(self, site, store, page, tmp_path) -> None:
        _, alpha, _ = site
        attachment = tmp_path / "需求清单.txt"
        attachment.write_bytes("型号A x3\n型号B x1\n".encode("utf-8"))

        page.goto(f"{alpha.base_url}/form")

        # --- native form controls -------------------------------------
        page.fill("#f-customer", "杭州精工科技")
        page.fill("#f-email", "buyer@jinggong.example")
        page.fill("#f-passcode", "p@ss-2026")
        page.select_option("#f-region", "east")
        page.check("#f-priority input[value='high']")
        page.check("#f-channels input[value='email']")
        page.check("#f-channels input[value='sms']")
        page.fill("#f-date", "2026-07-01")
        page.fill("#f-qty", "5")
        page.fill("#f-req", "需在7月第一周送达，含安装调试。")
        page.set_input_files("#f-file", str(attachment))

        # --- iframe sub-form ------------------------------------------
        frame = page.frame_locator("#invoice-frame")
        frame.locator("#invoice-title").fill("杭州精工科技有限公司")
        frame.locator("#invoice-tax").fill("91330100MA27XW0001")

        # --- shadow-DOM widget (Playwright pierces open shadow roots) --
        page.fill("#shadow-sku", "ALP-002")

        # --- contenteditable rich notes --------------------------------
        page.click("#rich-notes")
        page.keyboard.type("交期敏感，")
        page.evaluate(
            "document.execCommand('insertHTML', false, '<b>加急处理</b>')"
        )

        page.click("#f-submit")
        page.wait_for_selector("#result-title")

        # --- result page echo ------------------------------------------
        assert page.text_content("#echo-customer") == "客户：杭州精工科技"
        assert page.text_content("#echo-region") == "地区：east"
        assert page.text_content("#echo-priority") == "优先级：high"
        assert page.text_content("#echo-channels") == "渠道：email,sms"
        assert page.text_content("#echo-sku") == "关联SKU：ALP-002"
        assert "杭州精工科技有限公司" in page.text_content("#echo-invoice")

        # --- server-side record, field by field -------------------------
        assert len(store.submissions) == 1
        sub = store.submissions[0]
        assert sub["customer"] == "杭州精工科技"
        assert sub["email"] == "buyer@jinggong.example"
        assert sub["passcode"] == "p@ss-2026"
        assert sub["region"] == "east"
        assert sub["priority"] == "high"
        assert sub["channel"] == ["email", "sms"]
        assert sub["deliver_date"] == "2026-07-01"
        assert sub["quantity"] == "5"
        assert "安装调试" in sub["requirement"]
        assert sub["form_token"] == "tok-fixture-001"
        # non-form widgets folded into the submission
        assert sub["invoice_title"] == "杭州精工科技有限公司"
        assert sub["invoice_tax"] == "91330100MA27XW0001"
        assert sub["sku_code"] == "ALP-002"
        assert "<b>加急处理</b>" in sub["rich_notes_html"]
        assert "交期敏感" in sub["rich_notes_html"]
        # file upload arrived with the right name and size
        upload = sub["attachment"]
        assert upload["filename"].endswith(".txt")
        assert upload["size"] == len("型号A x3\n型号B x1\n".encode("utf-8"))

        # submit response set a cookie on the alpha origin
        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        assert cookies.get("alpha_last_submit") == "ok"


# ---------------------------------------------------------------------------
# S2 非表单信息填写：contenteditable 草稿 + localStorage
# ---------------------------------------------------------------------------


class TestS2NonFormDraft:
    def test_contenteditable_draft_persists_to_local_storage(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        product = PRODUCTS[2]
        page.goto(f"{alpha.base_url}/product/{product['id']}")

        page.click("#comment-editor")
        page.keyboard.type("现场已有旧型号，")
        page.evaluate(
            "document.execCommand('insertHTML', false, '<i>需确认兼容性</i>')"
        )
        page.click("#save-draft")

        assert page.text_content("#draft-status") == "草稿已保存"
        stored = page.evaluate(
            f"localStorage.getItem('draft_{product['id']}')"
        )
        assert "现场已有旧型号" in stored
        assert "<i>需确认兼容性</i>" in stored

        # reload keeps the draft in storage (editor itself is volatile)
        page.reload()
        stored_after = page.evaluate(
            f"localStorage.getItem('draft_{product['id']}')"
        )
        assert stored_after == stored


# ---------------------------------------------------------------------------
# S3 多类型数据爬取：文字 / 表格 / 图片 / SVG / CSV / PDF / ZIP
# ---------------------------------------------------------------------------


class TestS3MultiKindCrawl:
    def test_text_and_table_extraction(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(alpha.base_url)
        html = page.content()

        rows = extract_html_tables(html)
        assert len(rows) == 4  # page 1 of the product table
        # header names are normalised by the engine (lowercase, CJK kept)
        assert rows[0]["sku"] == "ALP-001"
        assert rows[0]["名称"] == "智能温控器"
        assert {"sku", "名称", "价格", "库存", "类目"} <= set(rows[0])

        title = select(html, selector="#site-title", output="text")
        assert title["results"] == ["Alpha 商品目录"]
        intro = select(html, selector="#intro", output="text")
        assert "12 个商品" in intro["results"][0]

    def test_media_harvest_downloads_every_kind(
        self, site, store, page, tmp_path, monkeypatch
    ) -> None:
        _, alpha, _ = site
        # the fixture site lives on loopback; use the documented SSRF
        # escape hatch instead of weakening the guard itself
        monkeypatch.setenv("VSPIDER_ALLOW_PRIVATE_URLS", "1")
        page.goto(alpha.base_url)
        candidates = collect_from_html(page.content(), page.url)

        kinds = {c.kind for c in candidates}
        assert "media_image" in kinds          # png thumbnails + svg logo
        assert "media_pdf" in kinds            # spec.pdf
        assert "media_archive" in kinds        # bundle.zip
        assert "file_generic" in kinds         # a[download] report.csv
        svg_urls = [c.url for c in candidates if c.url.endswith(".svg")]
        assert svg_urls, "served svg must be collected via <img src>"

        report = harvest_to_run(
            candidates, "scenario_s3", output_kind="mixed", base_dir=tmp_path
        )
        assert not report.failed
        # every distinct URL is downloaded; csv/pdf/zip/svg all land too.
        artifacts = list((tmp_path / "scenario_s3" / "artifacts").iterdir())
        assert len(report.downloaded) == len(candidates)
        assert artifacts, "artifacts folder must not be empty"

        manifest_path = tmp_path / "scenario_s3" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        items = manifest["items"]
        by_kind = {}
        for item in items:
            by_kind.setdefault(item["kind"], []).append(item)
        assert by_kind.get("media_image"), "png/svg images must be in manifest"
        assert by_kind.get("media_pdf"), "pdf must be in manifest"
        assert by_kind.get("media_archive"), "zip must be in manifest"
        assert by_kind.get("file_generic"), "csv download must be in manifest"

        # verify the actual bytes survived the trip (paths are absolute)
        def _read(kind: str, suffix: str) -> bytes:
            for item in by_kind[kind]:
                p = Path(item["path"])
                if p.suffix == suffix:
                    return p.read_bytes()
            raise AssertionError(f"no {suffix} artifact for {kind}")

        assert _read("media_pdf", ".pdf") == PDF_BYTES
        assert _read("media_archive", ".zip") == ZIP_BYTES
        assert _read("file_generic", ".csv").decode("utf-8") == CSV_TEXT
        assert _read("media_image", ".png") == PNG_BYTES
        # the svg artifact's on-disk extension depends on mime sniffing;
        # find it through its source_url instead
        def _source_urls(item) -> list[str]:
            raw = item.get("source_url")
            return [str(u) for u in raw] if isinstance(raw, list) else [str(raw)]

        svg_items = [
            i for i in items
            if any(u.endswith(".svg") for u in _source_urls(i))
        ]
        assert svg_items, "svg image must be harvested"
        svg_bytes = Path(svg_items[0]["path"]).read_bytes()
        assert b"<svg" in svg_bytes and "价格走势".encode("utf-8") in svg_bytes

        # identical thumbnails share one sha256 in the manifest
        png_shas = {i["sha256"] for i in by_kind["media_image"]
                    if i["path"].endswith(".png")}
        assert len(png_shas) == 1


# ---------------------------------------------------------------------------
# S4 跨页面：分页聚合 + 新标签详情跳转
# ---------------------------------------------------------------------------


class TestS4CrossPageNavigation:
    def test_paginate_and_aggregate_all_rows(self, site, store, page) -> None:
        _, alpha, _ = site
        page.goto(alpha.base_url)
        seen: list[dict] = []
        pages_visited = 0
        while True:
            pages_visited += 1
            seen.extend(extract_html_tables(page.content()))
            next_link = page.locator("#next-page")
            if next_link.count() == 0:
                break
            next_link.click()
            page.wait_for_selector("#product-table")
        assert pages_visited == TOTAL_PAGES == 3
        skus = [row["sku"] for row in seen]
        assert len(skus) == len(PRODUCTS) == 12
        assert len(set(skus)) == 12, "no duplicate rows across pages"
        assert skus[0] == "ALP-001" and skus[-1] == "ALP-012"

    def test_detail_opens_in_new_tab_and_matches_row(
        self, site, store, page
    ) -> None:
        _, alpha, _ = site
        page.goto(f"{alpha.base_url}/page/2")
        first_row_sku = page.locator(
            "#product-table tbody tr"
        ).first.get_attribute("data-sku")

        with page.context.expect_page() as popup_info:
            page.locator(".detail-link").first.click()
        detail = popup_info.value
        detail.wait_for_selector("#product-name")

        assert detail.text_content("#meta-sku") == first_row_sku
        assert detail.locator("#gallery img").count() == 2
        assert detail.locator("#trend-svg").count() == 1
        # the catalog tab stayed where it was
        assert "/page/2" in page.url
        assert len(page.context.pages) == 2
        detail.close()


# ---------------------------------------------------------------------------
# S5 跨系统：cookie 子集隔离 + 真实登录 + 数据中继下单
# ---------------------------------------------------------------------------


class TestS5CrossSystemRelay:
    def _systems(self, alpha, beta) -> list[dict]:
        return [
            {"id": "system_1", "name": "Alpha 目录", "domain": "127.0.0.1",
             "type": "web", "auth_profile": "alpha_guest"},
            {"id": "system_2", "name": "Beta 订单", "domain": "localhost",
             "type": "web", "auth_profile": "beta_ops"},
        ]

    def _full_state(self) -> dict:
        return {
            "cookies": [
                {"name": "alpha_last_submit", "value": "ok",
                 "domain": "127.0.0.1", "path": "/"},
                {"name": "beta_session", "value": "beta-ok",
                 "domain": "localhost", "path": "/"},
                {"name": "tracker", "value": "junk",
                 "domain": ".ads.example", "path": "/"},
            ],
            "origins": [],
        }

    def test_cookie_subsets_isolate_real_contexts(
        self, site, store, browser
    ) -> None:
        _, alpha, beta = site
        router = SessionRouter(
            run_id="scenario_s5",
            plan=SystemAuthPlan(systems=self._systems(alpha, beta)),
        )
        full_state = self._full_state()
        sub_alpha = router.storage_state_for_system("system_1", full_state)
        sub_beta = router.storage_state_for_system("system_2", full_state)
        assert [c["name"] for c in sub_alpha["cookies"]] == ["alpha_last_submit"]
        assert [c["name"] for c in sub_beta["cookies"]] == ["beta_session"]

        # context armed with ONLY the alpha subset must bounce off beta
        ctx_alpha = browser.new_context()
        ctx_alpha.add_cookies(sub_alpha["cookies"])
        page_a = ctx_alpha.new_page()
        page_a.goto(f"{beta.base_url}/orders")
        page_a.wait_for_selector("#beta-title")
        assert page_a.url.endswith("/login"), "alpha cookies must not enter beta"
        ctx_alpha.close()

        # context armed with the beta subset goes straight in
        ctx_beta = browser.new_context()
        ctx_beta.add_cookies(sub_beta["cookies"])
        page_b = ctx_beta.new_page()
        page_b.goto(f"{beta.base_url}/orders")
        page_b.wait_for_selector("#orders-title")
        assert page_b.url.endswith("/orders")
        ctx_beta.close()

    def test_relay_alpha_product_into_beta_order(
        self, site, store, browser
    ) -> None:
        _, alpha, beta = site

        # --- system A: crawl the SKU to relay ---------------------------
        ctx_alpha = browser.new_context()
        page_a = ctx_alpha.new_page()
        page_a.goto(alpha.base_url)
        rows = extract_html_tables(page_a.content())
        relay_row = rows[1]
        assert relay_row["sku"] == "ALP-002"
        ctx_alpha.close()

        # --- system B: REAL login, then place the relayed order ---------
        ctx_beta = browser.new_context()
        page_b = ctx_beta.new_page()
        page_b.goto(f"{beta.base_url}/orders")  # redirected: not logged in
        page_b.wait_for_selector("#login-form")
        page_b.fill("#l-user", "ops")
        page_b.fill("#l-pass", "secret")
        page_b.click("#l-submit")
        page_b.wait_for_selector("#orders-title")
        assert store.beta_logins == ["ops"]

        page_b.fill("#o-sku", relay_row["sku"])
        page_b.fill("#o-qty", "3")
        page_b.fill("#o-customer", f"中继自Alpha-{relay_row['名称']}")
        page_b.click("#o-submit")
        page_b.wait_for_selector("#orders-table tbody tr")

        # order visible in B's table and recorded server-side
        row_text = page_b.locator("#orders-table tbody tr").first.text_content()
        assert "ALP-002" in row_text
        assert len(store.orders) == 1
        order = store.orders[0]
        assert order["sku"] == "ALP-002"
        assert order["qty"] == "3"
        assert order["customer"] == "中继自Alpha-工业网关"

        # wrong-password path stays out
        ctx_bad = browser.new_context()
        page_bad = ctx_bad.new_page()
        page_bad.goto(f"{beta.base_url}/login")
        page_bad.fill("#l-user", "ops")
        page_bad.fill("#l-pass", "wrong")
        page_bad.click("#l-submit")
        page_bad.wait_for_selector("#login-error")
        assert "错误" in page_bad.text_content("#login-error")
        ctx_bad.close()
        ctx_beta.close()

"""Local dual-system fixture site for complex end-to-end scenario tests.

Two independent HTTP apps (each on its own 127.0.0.1 / localhost port):

* **Alpha 商品目录** (`AlphaHandler`) - public catalog system:
  - ``/`` + ``/page/2`` + ``/page/3``: paginated product table + product
    cards (img thumbnails) + inline SVG chart + download links.
  - ``/product/<id>``: detail page (opens via ``target=_blank`` from the
    catalog), gallery images, inline SVG trend, contenteditable comment
    editor persisted to ``localStorage``.
  - ``/form``: complex order form - text/email/password/select/radio/
    checkbox/date/number/textarea/hidden/file upload, plus three
    *non-form* widgets the page JS folds into hidden inputs on submit:
    an ``iframe`` sub-form (发票抬头), a shadow-DOM ``<sku-picker>``
    input, and a contenteditable rich-notes div.
  - ``POST /submit``: multipart parser; submission recorded in the store
    and echoed back on a result page.
  - Static assets: PNG images, served SVG, CSV / PDF / ZIP downloads.

* **Beta 订单系统** (`BetaHandler`) - cookie-gated back office:
  - ``GET/POST /login``: sets ``beta_session=v{N}`` on success (versioned;
    legacy ``beta-ok`` value still accepted for backward compatibility).
  - ``/orders``: requires the auth cookie (302 to /login otherwise);
    lists recorded orders and offers a "new order" relay form.
  - ``POST /orders/new``: requires the cookie; appends to the store.
  - Optional WAF gate (``make_beta_handler(store, challenge=True)``):
    every request without a ``cf_clearance`` cookie gets a Cloudflare-style
    403 interstitial ("Just a moment...", ``#challenge-form``) whose JS
    auto-passes via ``/cdn-cgi/challenge`` — that endpoint issues the
    ``cf_clearance`` cookie and 302s back to the original path. Block /
    clearance counts are recorded on the store for assertions.

Everything is deterministic and offline; no external network access.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

# ---------------------------------------------------------------------------
# Binary fixtures
# ---------------------------------------------------------------------------

# 1x1 red PNG (valid file, smallest practical payload).
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<<>>\n%%EOF\n"

ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 26 + b"scenario-zip-payload"

CSV_TEXT = (
    "sku,name,price,stock\n"
    "ALP-001,智能温控器,199.00,42\n"
    "ALP-002,工业网关,1299.00,7\n"
    "ALP-003,边缘计算盒,2599.00,3\n"
)

SVG_TREND = """<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80" id="trend-svg">
  <title>价格走势</title>
  <polyline points="0,70 40,55 80,60 120,30 160,35 200,10"
            fill="none" stroke="#2c7be5" stroke-width="3"/>
  <text x="8" y="16" font-size="12">trend</text>
</svg>"""


PRODUCTS: list[dict[str, Any]] = [
    {
        "id": i,
        "sku": f"ALP-{i:03d}",
        "name": name,
        "price": price,
        "stock": stock,
        "category": cat,
    }
    for i, (name, price, stock, cat) in enumerate(
        [
            ("智能温控器", "199.00", 42, "传感"),
            ("工业网关", "1299.00", 7, "网络"),
            ("边缘计算盒", "2599.00", 3, "计算"),
            ("振动传感器", "459.00", 18, "传感"),
            ("PLC 扩展模块", "899.00", 11, "控制"),
            ("光纤收发器", "329.00", 25, "网络"),
            ("条码扫描枪", "549.00", 9, "外设"),
            ("工控一体机", "4599.00", 2, "计算"),
            ("温湿度记录仪", "269.00", 31, "传感"),
            ("继电器组", "129.00", 64, "控制"),
            ("串口服务器", "699.00", 14, "网络"),
            ("安全光幕", "1899.00", 5, "安防"),
        ],
        start=1,
    )
]

PAGE_SIZE = 4
TOTAL_PAGES = (len(PRODUCTS) + PAGE_SIZE - 1) // PAGE_SIZE


@dataclass
class ScenarioStore:
    """Shared mutable state both apps write into (assertions read it back)."""

    submissions: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    beta_logins: list[str] = field(default_factory=list)
    waf_blocks: int = 0
    waf_clearances: int = 0
    # When False the interstitial renders without the auto-pass script, so
    # the challenge never clears by itself (stubborn-WAF / HITL scenarios).
    waf_autopass: bool = True
    # D3: session expiry tracking
    beta_session_version: int = 0
    # D5: flaky endpoint tracking — first N requests return 500
    flaky_fail_remaining: int = 0
    # E1: wizard submissions
    wizard_submissions: list[dict[str, Any]] = field(default_factory=list)
    # E5: polling counter (increments on each /api/status call)
    poll_counter: int = 0
    # E6: batch items (mutable list for batch ops)
    batch_items: list[dict[str, Any]] = field(default_factory=lambda: [
        {"id": i, "sku": p["sku"], "name": p["name"], "selected": False}
        for i, p in enumerate(PRODUCTS[:8], start=1)
    ])
    # F2: drag order persisted server-side
    drag_order: list[str] = field(default_factory=lambda: [
        p["sku"] for p in PRODUCTS[:5]
    ])
    # F3: cookie consent tracking
    consent_given: int = 0
    # G2: rate limiting — first N requests return 429
    rate_limit_remaining: int = 0
    # G4: iframe message log (postMessage results)
    iframe_messages: list[dict[str, Any]] = field(default_factory=list)
    # G5: localStorage write-back log
    localstorage_writes: list[dict[str, Any]] = field(default_factory=list)
    # G6: server-side paginated table export log
    csv_exports: int = 0


# ---------------------------------------------------------------------------
# Multipart helper (cgi was removed in Python 3.13)
# ---------------------------------------------------------------------------


def parse_multipart(body: bytes, content_type: str) -> dict[str, Any]:
    """Tiny multipart/form-data parser good enough for the fixture forms."""

    boundary = ""
    for token in content_type.split(";"):
        token = token.strip()
        if token.startswith("boundary="):
            boundary = token[len("boundary="):].strip('"')
    if not boundary:
        return {}
    delim = b"--" + boundary.encode("ascii")
    fields: dict[str, Any] = {}
    for part in body.split(delim):
        # Trim exactly the CRLF framing around the part so binary payloads
        # that legitimately end in \n / \r survive intact.
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, content = part.split(b"\r\n\r\n", 1)
        name = ""
        filename = None
        for line in raw_headers.decode("utf-8", "replace").split("\r\n"):
            if line.lower().startswith("content-disposition"):
                for piece in line.split(";"):
                    piece = piece.strip()
                    if piece.startswith("name="):
                        name = piece[5:].strip('"')
                    elif piece.startswith("filename="):
                        filename = piece[9:].strip('"')
        if not name:
            continue
        if filename is not None:
            fields[name] = {
                "filename": filename,
                "size": len(content),
                "content": content,
            }
        else:
            value = content.decode("utf-8", "replace")
            if name in fields and isinstance(fields[name], list):
                fields[name].append(value)
            elif name in fields:
                fields[name] = [fields[name], value]
            else:
                fields[name] = value
    return fields


# ---------------------------------------------------------------------------
# HTML builders (Alpha)
# ---------------------------------------------------------------------------


def _page_shell(title: str, body: str) -> bytes:
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title></head>
<body>{body}</body></html>"""
    return html.encode("utf-8")


def _catalog_html(page: int) -> bytes:
    start = (page - 1) * PAGE_SIZE
    chunk = PRODUCTS[start : start + PAGE_SIZE]
    rows = "\n".join(
        f"<tr data-sku='{p['sku']}'><td>{p['sku']}</td>"
        f"<td><a href='/product/{p['id']}' target='_blank' class='detail-link'>{p['name']}</a></td>"
        f"<td>{p['price']}</td><td>{p['stock']}</td><td>{p['category']}</td></tr>"
        for p in chunk
    )
    cards = "\n".join(
        f"<div class='card'><img src='/img/p{p['id']}.png' alt='{p['name']}缩略图'>"
        f"<h3>{p['name']}</h3><p class='price'>¥{p['price']}</p></div>"
        for p in chunk
    )
    prev_link = (
        f"<a id='prev-page' href='/page/{page - 1}'>上一页</a>" if page > 1 else ""
    )
    next_link = (
        f"<a id='next-page' href='/page/{page + 1}'>下一页</a>"
        if page < TOTAL_PAGES
        else ""
    )
    body = f"""
<header><img src="/img/banner.png" alt="Alpha banner">
  <h1 id="site-title">Alpha 商品目录</h1>
  <p id="intro">本页提供工业物联网产品清单，共 {len(PRODUCTS)} 个商品，分 {TOTAL_PAGES} 页展示。</p>
  {SVG_TREND}
</header>
<main>
  <table id="product-table">
    <thead><tr><th>SKU</th><th>名称</th><th>价格</th><th>库存</th><th>类目</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <section id="cards">{cards}</section>
  <nav id="pager">第 {page}/{TOTAL_PAGES} 页 {prev_link} {next_link}</nav>
  <aside id="downloads">
    <a href="/files/report.csv" download>下载库存报表(CSV)</a>
    <a href="/files/spec.pdf">产品规格书(PDF)</a>
    <a href="/files/bundle.zip">驱动打包(ZIP)</a>
    <img src="/svg/logo.svg" alt="alpha logo" id="logo-img">
  </aside>
  <a href="/form" id="goto-form">前往采购申请单</a>
</main>"""
    return _page_shell(f"Alpha 商品目录 - 第{page}页", body)


def _detail_html(product: dict[str, Any]) -> bytes:
    body = f"""
<h1 id="product-name">{product['name']}</h1>
<dl id="product-meta">
  <dt>SKU</dt><dd id="meta-sku">{product['sku']}</dd>
  <dt>价格</dt><dd id="meta-price">{product['price']}</dd>
  <dt>库存</dt><dd id="meta-stock">{product['stock']}</dd>
  <dt>类目</dt><dd id="meta-category">{product['category']}</dd>
</dl>
<p id="desc">{product['name']} 适用于工业现场环境，支持 Modbus / MQTT 协议接入。</p>
<div id="gallery">
  <img src="/img/p{product['id']}.png" alt="主图">
  <img src="/img/p{product['id']}_alt.png" alt="侧视图">
</div>
{SVG_TREND}
<a href="/files/spec.pdf" id="spec-link">下载规格书</a>
<section id="comment-box">
  <h2>备注草稿（非表单）</h2>
  <div id="comment-editor" contenteditable="true" data-placeholder="输入备注..."></div>
  <button id="save-draft" type="button">保存草稿</button>
  <span id="draft-status">未保存</span>
</section>
<script>
document.getElementById('save-draft').addEventListener('click', function () {{
  var html = document.getElementById('comment-editor').innerHTML;
  localStorage.setItem('draft_{product['id']}', html);
  document.getElementById('draft-status').textContent = '草稿已保存';
}});
</script>"""
    return _page_shell(f"{product['name']} - 详情", body)


def _iframe_form_html() -> bytes:
    body = """
<h3>发票信息（iframe 子表单）</h3>
<label>发票抬头 <input id="invoice-title" name="invoice_title" type="text"></label>
<label>税号 <input id="invoice-tax" name="invoice_tax" type="text"></label>"""
    return _page_shell("发票子表单", body)


def _form_html() -> bytes:
    body = """
<h1 id="form-title">采购申请单</h1>
<form id="purchase-form" method="post" action="/submit" enctype="multipart/form-data">
  <label>客户名称 <input type="text" name="customer" id="f-customer" required></label>
  <label>邮箱 <input type="email" name="email" id="f-email"></label>
  <label>登录口令 <input type="password" name="passcode" id="f-passcode"></label>
  <label>地区
    <select name="region" id="f-region">
      <option value="">请选择</option>
      <option value="east">华东</option>
      <option value="north">华北</option>
      <option value="south">华南</option>
    </select>
  </label>
  <fieldset id="f-priority">
    <legend>优先级</legend>
    <label><input type="radio" name="priority" value="low">低</label>
    <label><input type="radio" name="priority" value="mid">中</label>
    <label><input type="radio" name="priority" value="high">高</label>
  </fieldset>
  <fieldset id="f-channels">
    <legend>通知渠道</legend>
    <label><input type="checkbox" name="channel" value="email">邮件</label>
    <label><input type="checkbox" name="channel" value="sms">短信</label>
    <label><input type="checkbox" name="channel" value="phone">电话</label>
  </fieldset>
  <label>交付日期 <input type="date" name="deliver_date" id="f-date"></label>
  <label>数量 <input type="number" name="quantity" id="f-qty" min="1"></label>
  <label>需求说明 <textarea name="requirement" id="f-req" rows="3"></textarea></label>
  <label>附件 <input type="file" name="attachment" id="f-file"></label>
  <input type="hidden" name="form_token" value="tok-fixture-001">
  <input type="hidden" name="invoice_title" id="h-invoice-title">
  <input type="hidden" name="invoice_tax" id="h-invoice-tax">
  <input type="hidden" name="sku_code" id="h-sku-code">
  <input type="hidden" name="rich_notes_html" id="h-rich-notes">
  <button type="submit" id="f-submit">提交申请</button>
</form>

<h2>补充信息（非 form 控件）</h2>
<iframe id="invoice-frame" src="/form/iframe" width="400" height="160"></iframe>
<sku-picker id="sku-widget"></sku-picker>
<div id="rich-notes" contenteditable="true"
     style="border:1px solid #999;min-height:60px"></div>

<script>
customElements.define('sku-picker', class extends HTMLElement {
  connectedCallback() {
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML = `<label>关联 SKU
      <input id="shadow-sku" type="text" placeholder="ALP-...">
    </label>`;
  }
});
document.getElementById('purchase-form').addEventListener('submit', function () {
  var frameDoc = document.getElementById('invoice-frame').contentDocument;
  document.getElementById('h-invoice-title').value =
    frameDoc.getElementById('invoice-title').value;
  document.getElementById('h-invoice-tax').value =
    frameDoc.getElementById('invoice-tax').value;
  var widget = document.getElementById('sku-widget');
  document.getElementById('h-sku-code').value =
    widget.shadowRoot.getElementById('shadow-sku').value;
  document.getElementById('h-rich-notes').value =
    document.getElementById('rich-notes').innerHTML;
});
</script>"""
    return _page_shell("采购申请单", body)


def _submit_result_html(fields: dict[str, Any]) -> bytes:
    attachment = fields.get("attachment") or {}
    channels = fields.get("channel") or []
    if isinstance(channels, str):
        channels = [channels]
    body = f"""
<h1 id="result-title">提交成功</h1>
<ul id="echo">
  <li id="echo-customer">客户：{fields.get('customer', '')}</li>
  <li id="echo-region">地区：{fields.get('region', '')}</li>
  <li id="echo-priority">优先级：{fields.get('priority', '')}</li>
  <li id="echo-channels">渠道：{','.join(channels)}</li>
  <li id="echo-sku">关联SKU：{fields.get('sku_code', '')}</li>
  <li id="echo-invoice">发票抬头：{fields.get('invoice_title', '')}</li>
  <li id="echo-file">附件：{attachment.get('filename', '')}（{attachment.get('size', 0)} 字节）</li>
</ul>
<a href="/" id="back-home">返回目录</a>"""
    return _page_shell("提交成功", body)


# ---------------------------------------------------------------------------
# D1: AJAX-loaded page (data arrives via XHR, not in initial HTML)
# ---------------------------------------------------------------------------

AJAX_PRODUCTS = PRODUCTS[:6]
AJAX_DELAY_MS = 400

def _ajax_page_html() -> bytes:
    body = """
<h1 id="ajax-title">动态加载产品列表</h1>
<p id="loading-status">加载中...</p>
<table id="ajax-table">
  <thead><tr><th>SKU</th><th>名称</th><th>价格</th><th>库存</th></tr></thead>
  <tbody id="ajax-body"></tbody>
</table>
<script>
setTimeout(function() {
  fetch('/api/products').then(r => r.json()).then(function(data) {
    var tbody = document.getElementById('ajax-body');
    data.forEach(function(p) {
      var tr = document.createElement('tr');
      tr.innerHTML = '<td>' + p.sku + '</td><td>' + p.name + '</td>' +
                     '<td>' + p.price + '</td><td>' + p.stock + '</td>';
      tbody.appendChild(tr);
    });
    document.getElementById('loading-status').textContent =
      '已加载 ' + data.length + ' 条';
  });
}, """ + str(AJAX_DELAY_MS) + """);
</script>"""
    return _page_shell("动态加载", body)


# ---------------------------------------------------------------------------
# D2: Infinite scroll / load-more (IntersectionObserver)
# ---------------------------------------------------------------------------

FEED_BATCH = 3  # items per scroll batch
FEED_TOTAL = len(PRODUCTS)

def _feed_page_html() -> bytes:
    body = """
<h1 id="feed-title">无限滚动商品流</h1>
<div id="feed-container" style="height:300px;overflow-y:auto">
  <div id="feed-items"></div>
  <div id="feed-sentinel" style="height:1px"></div>
</div>
<p id="feed-status">加载中...</p>
<script>
var offset = 0, loading = false, done = false;
function loadMore() {
  if (loading || done) return;
  loading = true;
  fetch('/api/feed?offset=' + offset + '&limit=""" + str(FEED_BATCH) + """')
    .then(r => r.json()).then(function(data) {
      var container = document.getElementById('feed-items');
      data.items.forEach(function(p) {
        var div = document.createElement('div');
        div.className = 'feed-card';
        div.setAttribute('data-sku', p.sku);
        div.innerHTML = '<b>' + p.name + '</b> ¥' + p.price;
        container.appendChild(div);
      });
      offset += data.items.length;
      loading = false;
      if (!data.has_more) {
        done = true;
        document.getElementById('feed-status').textContent =
          '全部加载完毕（共 ' + offset + ' 条）';
      } else {
        document.getElementById('feed-status').textContent =
          '已加载 ' + offset + ' 条';
      }
    });
}
var obs = new IntersectionObserver(function(entries) {
  if (entries[0].isIntersecting) loadMore();
}, {root: document.getElementById('feed-container')});
obs.observe(document.getElementById('feed-sentinel'));
</script>"""
    return _page_shell("无限滚动", body)


# ---------------------------------------------------------------------------
# D4: Nested data — rowspan/colspan table + multi-layer accordion
# ---------------------------------------------------------------------------

def _nested_data_html() -> bytes:
    body = """
<h1 id="nested-title">复杂嵌套数据</h1>

<h2>一、合并单元格表格</h2>
<table id="merged-table">
  <thead><tr><th>类目</th><th>SKU</th><th>名称</th><th>价格</th></tr></thead>
  <tbody>
    <tr><td rowspan="2">传感</td><td>ALP-001</td><td>智能温控器</td><td>199.00</td></tr>
    <tr><td>ALP-004</td><td>振动传感器</td><td>459.00</td></tr>
    <tr><td rowspan="3">网络</td><td>ALP-002</td><td>工业网关</td><td>1299.00</td></tr>
    <tr><td>ALP-006</td><td>光纤收发器</td><td>329.00</td></tr>
    <tr><td>ALP-011</td><td>串口服务器</td><td>699.00</td></tr>
    <tr><td colspan="2">合计</td><td colspan="2">5 款产品</td></tr>
  </tbody>
</table>

<h2>二、嵌套表格</h2>
<table id="outer-table">
  <thead><tr><th>分区</th><th>详情</th></tr></thead>
  <tbody>
    <tr><td>华东区</td><td>
      <table class="inner-table" id="inner-east">
        <thead><tr><th>城市</th><th>仓库</th><th>库存</th></tr></thead>
        <tbody>
          <tr><td>杭州</td><td>HZ-W01</td><td>120</td></tr>
          <tr><td>上海</td><td>SH-W03</td><td>85</td></tr>
        </tbody>
      </table>
    </td></tr>
    <tr><td>华北区</td><td>
      <table class="inner-table" id="inner-north">
        <thead><tr><th>城市</th><th>仓库</th><th>库存</th></tr></thead>
        <tbody>
          <tr><td>北京</td><td>BJ-W02</td><td>200</td></tr>
        </tbody>
      </table>
    </td></tr>
  </tbody>
</table>

<h2>三、多层折叠面板</h2>
<div id="accordion">
  <details class="level-1" open>
    <summary>传感器类</summary>
    <details class="level-2">
      <summary>温度传感器</summary>
      <ul class="acc-items"><li data-sku="ALP-001">智能温控器 ¥199</li></ul>
    </details>
    <details class="level-2">
      <summary>振动传感器</summary>
      <ul class="acc-items"><li data-sku="ALP-004">振动传感器 ¥459</li></ul>
    </details>
  </details>
  <details class="level-1">
    <summary>网络设备类</summary>
    <details class="level-2">
      <summary>有线设备</summary>
      <ul class="acc-items">
        <li data-sku="ALP-002">工业网关 ¥1299</li>
        <li data-sku="ALP-006">光纤收发器 ¥329</li>
      </ul>
    </details>
    <details class="level-2">
      <summary>串口设备</summary>
      <ul class="acc-items"><li data-sku="ALP-011">串口服务器 ¥699</li></ul>
    </details>
  </details>
</div>"""
    return _page_shell("复杂嵌套数据", body)


# ---------------------------------------------------------------------------
# D5: Error-recovery endpoints (flaky 500, redirect chain)
# ---------------------------------------------------------------------------

REDIRECT_CHAIN_DEPTH = 3

def _flaky_success_html() -> bytes:
    return _page_shell("成功", '<h1 id="flaky-ok">请求成功</h1><p>恢复正常。</p>')


# ---------------------------------------------------------------------------
# D6: Authenticated download (cookie-gated binary file)
# ---------------------------------------------------------------------------

PROTECTED_PDF_BYTES = b"%PDF-1.4 PROTECTED\n1 0 obj\n<</Type/Catalog>>\nendobj\n%%EOF\n"


# ---------------------------------------------------------------------------
# E1: Multi-step wizard (3 steps with inter-step validation)
# ---------------------------------------------------------------------------

def _wizard_step_html(step: int, data: dict[str, Any] | None = None) -> bytes:
    data = data or {}
    if step == 1:
        body = """
<h1 id="wizard-title">采购向导 - 第1步：基本信息</h1>
<form id="wizard-form" method="post" action="/wizard/step/2">
  <label>公司名称 <input type="text" name="company" id="w-company" required></label>
  <label>联系人 <input type="text" name="contact" id="w-contact" required></label>
  <label>电话 <input type="tel" name="phone" id="w-phone"></label>
  <button type="submit" id="w-next">下一步</button>
</form>
<p id="wizard-progress">步骤 1/3</p>"""
    elif step == 2:
        body = f"""
<h1 id="wizard-title">采购向导 - 第2步：选择商品</h1>
<p id="w-company-echo">公司：{data.get('company', '')}</p>
<form id="wizard-form" method="post" action="/wizard/step/3">
  <input type="hidden" name="company" value="{data.get('company', '')}">
  <input type="hidden" name="contact" value="{data.get('contact', '')}">
  <input type="hidden" name="phone" value="{data.get('phone', '')}">
  <label>商品 SKU <input type="text" name="sku" id="w-sku" required></label>
  <label>数量 <input type="number" name="qty" id="w-qty" min="1" required></label>
  <a href="/wizard/step/1" id="w-back">上一步</a>
  <button type="submit" id="w-next">下一步</button>
</form>
<p id="wizard-progress">步骤 2/3</p>"""
    else:
        body = f"""
<h1 id="wizard-title">采购向导 - 第3步：确认提交</h1>
<dl id="wizard-review">
  <dt>公司</dt><dd id="r-company">{data.get('company', '')}</dd>
  <dt>联系人</dt><dd id="r-contact">{data.get('contact', '')}</dd>
  <dt>SKU</dt><dd id="r-sku">{data.get('sku', '')}</dd>
  <dt>数量</dt><dd id="r-qty">{data.get('qty', '')}</dd>
</dl>
<form id="wizard-form" method="post" action="/wizard/submit">
  <input type="hidden" name="company" value="{data.get('company', '')}">
  <input type="hidden" name="contact" value="{data.get('contact', '')}">
  <input type="hidden" name="phone" value="{data.get('phone', '')}">
  <input type="hidden" name="sku" value="{data.get('sku', '')}">
  <input type="hidden" name="qty" value="{data.get('qty', '')}">
  <a href="/wizard/step/2" id="w-back">上一步</a>
  <button type="submit" id="w-submit">确认提交</button>
</form>
<p id="wizard-progress">步骤 3/3</p>"""
    return _page_shell(f"采购向导 - 步骤{step}", body)


# ---------------------------------------------------------------------------
# E2: Dynamic form validation + conditional fields
# ---------------------------------------------------------------------------

def _validated_form_html() -> bytes:
    body = """
<h1 id="vform-title">动态校验表单</h1>
<form id="vform" method="post" action="/validated-form/submit" novalidate>
  <label>姓名 <input type="text" name="name" id="vf-name" required></label>
  <span class="error" id="err-name" style="display:none;color:red">姓名必填</span>

  <label>邮箱 <input type="email" name="email" id="vf-email" required></label>
  <span class="error" id="err-email" style="display:none;color:red">邮箱格式不正确</span>

  <label>客户类型
    <select name="client_type" id="vf-type">
      <option value="personal">个人</option>
      <option value="enterprise">企业</option>
    </select>
  </label>

  <div id="enterprise-fields" style="display:none">
    <label>企业税号 <input type="text" name="tax_id" id="vf-taxid"></label>
    <label>营业执照号 <input type="text" name="license" id="vf-license"></label>
  </div>

  <button type="submit" id="vf-submit">提交</button>
  <p id="vf-result" style="display:none"></p>
</form>
<script>
document.getElementById('vf-type').addEventListener('change', function() {
  document.getElementById('enterprise-fields').style.display =
    this.value === 'enterprise' ? 'block' : 'none';
});
document.getElementById('vform').addEventListener('submit', function(e) {
  var valid = true;
  var name = document.getElementById('vf-name');
  var email = document.getElementById('vf-email');
  document.getElementById('err-name').style.display =
    name.value.trim() ? 'none' : (valid = false, 'inline');
  var emailRe = /^[^@]+@[^@]+\\.[^@]+$/;
  document.getElementById('err-email').style.display =
    emailRe.test(email.value) ? 'none' : (valid = false, 'inline');
  if (!valid) { e.preventDefault(); }
});
</script>"""
    return _page_shell("动态校验表单", body)


# ---------------------------------------------------------------------------
# E3: Browser dialog triggers (alert / confirm / prompt)
# ---------------------------------------------------------------------------

def _dialog_page_html() -> bytes:
    body = """
<h1 id="dialog-title">浏览器弹窗测试</h1>
<button id="btn-alert" onclick="alert('操作成功！')">触发 Alert</button>
<button id="btn-confirm" onclick="
  var ok = confirm('确认删除此记录？');
  document.getElementById('confirm-result').textContent = ok ? '已确认' : '已取消';
">触发 Confirm</button>
<button id="btn-prompt" onclick="
  var val = prompt('请输入备注：', '默认备注');
  document.getElementById('prompt-result').textContent = val !== null ? val : '(取消)';
">触发 Prompt</button>
<p id="confirm-result"></p>
<p id="prompt-result"></p>"""
    return _page_shell("弹窗测试", body)


# ---------------------------------------------------------------------------
# E4: SPA-like pushState navigation
# ---------------------------------------------------------------------------

def _spa_page_html() -> bytes:
    body = """
<h1 id="spa-title">SPA 导航演示</h1>
<nav id="spa-nav">
  <a href="/spa/home" class="spa-link" data-page="home">首页</a>
  <a href="/spa/products" class="spa-link" data-page="products">产品</a>
  <a href="/spa/about" class="spa-link" data-page="about">关于</a>
</nav>
<div id="spa-content">
  <section id="page-home" class="spa-page">首页内容：欢迎使用 VSpider</section>
  <section id="page-products" class="spa-page" style="display:none">产品列表：智能温控器、工业网关</section>
  <section id="page-about" class="spa-page" style="display:none">关于我们：VSpider 团队</section>
</div>
<script>
function showPage(name) {
  document.querySelectorAll('.spa-page').forEach(function(el) {
    el.style.display = 'none';
  });
  var target = document.getElementById('page-' + name);
  if (target) target.style.display = 'block';
}
document.querySelectorAll('.spa-link').forEach(function(link) {
  link.addEventListener('click', function(e) {
    e.preventDefault();
    var page = this.getAttribute('data-page');
    history.pushState({page: page}, '', this.href);
    showPage(page);
  });
});
window.addEventListener('popstate', function(e) {
  var page = (e.state && e.state.page) || 'home';
  showPage(page);
});
</script>"""
    return _page_shell("SPA 导航", body)


# ---------------------------------------------------------------------------
# E5: Polling updates (counter increments on each API call)
# ---------------------------------------------------------------------------

def _polling_page_html() -> bytes:
    body = """
<h1 id="poll-title">实时状态监控</h1>
<p>任务进度：<span id="poll-value">0</span></p>
<p id="poll-status">运行中</p>
<script>
var interval = setInterval(function() {
  fetch('/api/status').then(function(r) { return r.json(); }).then(function(d) {
    document.getElementById('poll-value').textContent = d.progress;
    if (d.done) {
      document.getElementById('poll-status').textContent = '已完成';
      clearInterval(interval);
    }
  });
}, 500);
</script>"""
    return _page_shell("轮询监控", body)


# ---------------------------------------------------------------------------
# E6: Batch operations (select all / partial select + delete / export)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# F1: Search + filter + sort
# ---------------------------------------------------------------------------

def _search_page_html() -> bytes:
    products_js = json.dumps(
        [{"sku": p["sku"], "name": p["name"], "price": float(p["price"]),
          "category": p["category"]} for p in PRODUCTS],
        ensure_ascii=False,
    )
    body = f"""
<h1 id="search-title">产品搜索</h1>
<div id="search-controls">
  <input type="text" id="search-input" placeholder="搜索..." oninput="applyFilters()">
  <select id="filter-category" onchange="applyFilters()">
    <option value="">全部类目</option>
    <option value="传感">传感</option>
    <option value="网络">网络</option>
    <option value="计算">计算</option>
    <option value="控制">控制</option>
    <option value="外设">外设</option>
    <option value="安防">安防</option>
  </select>
  <select id="sort-by" onchange="applyFilters()">
    <option value="default">默认排序</option>
    <option value="price_asc">价格升序</option>
    <option value="price_desc">价格降序</option>
    <option value="name">名称排序</option>
  </select>
</div>
<p id="result-count"></p>
<ul id="search-results"></ul>
<script>
var ALL_PRODUCTS = {products_js};
function applyFilters() {{
  var q = document.getElementById('search-input').value.toLowerCase();
  var cat = document.getElementById('filter-category').value;
  var sort = document.getElementById('sort-by').value;
  var items = ALL_PRODUCTS.filter(function(p) {{
    var matchQ = !q || p.name.toLowerCase().indexOf(q) >= 0 || p.sku.toLowerCase().indexOf(q) >= 0;
    var matchCat = !cat || p.category === cat;
    return matchQ && matchCat;
  }});
  if (sort === 'price_asc') items.sort(function(a,b){{ return a.price - b.price; }});
  else if (sort === 'price_desc') items.sort(function(a,b){{ return b.price - a.price; }});
  else if (sort === 'name') items.sort(function(a,b){{ return a.name.localeCompare(b.name); }});
  document.getElementById('result-count').textContent = '找到 ' + items.length + ' 个结果';
  var ul = document.getElementById('search-results');
  ul.innerHTML = '';
  items.forEach(function(p) {{
    var li = document.createElement('li');
    li.className = 'search-item';
    li.setAttribute('data-sku', p.sku);
    li.setAttribute('data-price', p.price);
    li.textContent = p.sku + ' - ' + p.name + ' ¥' + p.price;
    ul.appendChild(li);
  }});
}}
applyFilters();
</script>"""
    return _page_shell("产品搜索", body)


# ---------------------------------------------------------------------------
# F2: Drag-and-drop reorder
# ---------------------------------------------------------------------------

def _drag_page_html(order: list[str]) -> bytes:
    items = "\n".join(
        f"<li class='drag-item' draggable='true' data-sku='{sku}'>{sku}</li>"
        for sku in order
    )
    body = f"""
<h1 id="drag-title">拖拽排序</h1>
<ul id="drag-list">{items}</ul>
<button id="save-order" onclick="saveOrder()">保存顺序</button>
<p id="drag-status"></p>
<script>
var list = document.getElementById('drag-list');
var dragged = null;
list.addEventListener('dragstart', function(e) {{ dragged = e.target; }});
list.addEventListener('dragover', function(e) {{ e.preventDefault(); }});
list.addEventListener('drop', function(e) {{
  e.preventDefault();
  if (e.target.classList.contains('drag-item') && e.target !== dragged) {{
    list.insertBefore(dragged, e.target);
  }}
}});
function saveOrder() {{
  var items = document.querySelectorAll('.drag-item');
  var order = [];
  items.forEach(function(li) {{ order.push(li.getAttribute('data-sku')); }});
  fetch('/api/drag-order', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{order: order}})
  }}).then(function(r) {{ return r.json(); }}).then(function(d) {{
    document.getElementById('drag-status').textContent = d.message;
  }});
}}
</script>"""
    return _page_shell("拖拽排序", body)


# ---------------------------------------------------------------------------
# F3: Cookie consent banner (blocks interaction until accepted)
# ---------------------------------------------------------------------------

def _consent_page_html() -> bytes:
    body = """
<div id="consent-overlay" style="position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.7);z-index:9999;display:flex;align-items:center;justify-content:center">
  <div id="consent-dialog" style="background:white;padding:20px;border-radius:8px;max-width:400px">
    <h2>Cookie 使用同意</h2>
    <p>本站使用 Cookie 提升体验。继续使用即表示同意。</p>
    <button id="consent-accept" onclick="acceptConsent()">接受</button>
    <button id="consent-reject" onclick="rejectConsent()">拒绝</button>
  </div>
</div>
<div id="main-content" style="pointer-events:none;opacity:0.3">
  <h1 id="consent-page-title">受保护的内容</h1>
  <p id="content-text">您已通过 Cookie 同意验证。</p>
  <button id="action-btn" onclick="document.getElementById('action-result').textContent='操作成功'">执行操作</button>
  <p id="action-result"></p>
</div>
<script>
function acceptConsent() {
  document.cookie = 'consent=accepted; Path=/';
  document.getElementById('consent-overlay').style.display = 'none';
  document.getElementById('main-content').style.pointerEvents = 'auto';
  document.getElementById('main-content').style.opacity = '1';
  fetch('/api/consent', {method: 'POST', body: 'accepted'});
}
function rejectConsent() {
  document.getElementById('consent-overlay').style.display = 'none';
  document.getElementById('main-content').style.pointerEvents = 'auto';
  document.getElementById('main-content').style.opacity = '1';
  fetch('/api/consent', {method: 'POST', body: 'rejected'});
}
if (document.cookie.indexOf('consent=accepted') >= 0) {
  document.getElementById('consent-overlay').style.display = 'none';
  document.getElementById('main-content').style.pointerEvents = 'auto';
  document.getElementById('main-content').style.opacity = '1';
}
</script>"""
    return _page_shell("Cookie 同意", body)


# ---------------------------------------------------------------------------
# F5: Keyboard navigation page
# ---------------------------------------------------------------------------

def _keyboard_page_html() -> bytes:
    body = """
<h1 id="kb-title">键盘交互测试</h1>
<form id="kb-form" method="post" action="/keyboard/submit">
  <label>字段1 <input type="text" name="field1" id="kb-f1" tabindex="1"></label>
  <label>字段2 <input type="text" name="field2" id="kb-f2" tabindex="2"></label>
  <label>字段3 <input type="text" name="field3" id="kb-f3" tabindex="3"></label>
  <button type="submit" id="kb-submit" tabindex="4">提交</button>
</form>
<p id="kb-result"></p>
<div id="shortcut-area" tabindex="0">
  <p>快捷键区域（按 Ctrl+S 保存，Escape 关闭）</p>
  <p id="shortcut-result"></p>
</div>
<script>
document.getElementById('shortcut-area').addEventListener('keydown', function(e) {
  if (e.ctrlKey && e.key === 's') {
    e.preventDefault();
    document.getElementById('shortcut-result').textContent = '快捷保存触发';
  }
  if (e.key === 'Escape') {
    document.getElementById('shortcut-result').textContent = '关闭触发';
  }
});
</script>"""
    return _page_shell("键盘交互", body)


# ---------------------------------------------------------------------------
# F6: Clipboard operations
# ---------------------------------------------------------------------------

def _clipboard_page_html() -> bytes:
    body = """
<h1 id="clip-title">剪贴板操作</h1>
<p id="copy-source">SKU-CLIP-TEST-001</p>
<button id="btn-copy" onclick="
  navigator.clipboard.writeText(document.getElementById('copy-source').textContent)
    .then(function() { document.getElementById('copy-status').textContent = '已复制'; });
">复制 SKU</button>
<p id="copy-status"></p>
<label>粘贴区 <input type="text" id="paste-target"></label>"""
    return _page_shell("剪贴板", body)


# ---------------------------------------------------------------------------
# E6: Batch operations (select all / partial select + delete / export)
# ---------------------------------------------------------------------------

def _batch_page_html(items: list[dict[str, Any]]) -> bytes:
    rows = "\n".join(
        "<tr data-id='{id}'>"
        "<td><input type='checkbox' class='item-check' value='{id}'></td>"
        "<td>{sku}</td><td>{name}</td></tr>".format(**it)
        for it in items
    )
    body = f"""
<h1 id="batch-title">批量操作</h1>
<div id="batch-toolbar">
  <label><input type="checkbox" id="select-all"> 全选</label>
  <button id="btn-delete" onclick="batchAction('delete')">批量删除</button>
  <button id="btn-export" onclick="batchAction('export')">批量导出</button>
  <span id="batch-status"></span>
</div>
<table id="batch-table">
  <thead><tr><th></th><th>SKU</th><th>名称</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<script>
document.getElementById('select-all').addEventListener('change', function() {{
  document.querySelectorAll('.item-check').forEach(function(cb) {{
    cb.checked = document.getElementById('select-all').checked;
  }});
}});
function batchAction(action) {{
  var ids = [];
  document.querySelectorAll('.item-check:checked').forEach(function(cb) {{
    ids.push(parseInt(cb.value));
  }});
  if (ids.length === 0) {{ alert('请先选择项目'); return; }}
  fetch('/api/batch', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{action: action, ids: ids}})
  }}).then(function(r) {{ return r.json(); }}).then(function(d) {{
    document.getElementById('batch-status').textContent = d.message;
    if (action === 'delete') {{
      ids.forEach(function(id) {{
        var row = document.querySelector('tr[data-id=\"' + id + '\"]');
        if (row) row.remove();
      }});
    }}
  }});
}}
</script>"""
    return _page_shell("批量操作", body)


# ---------------------------------------------------------------------------
# G1: Server-Sent Events (SSE) streaming page
# ---------------------------------------------------------------------------

SSE_EVENTS = [
    {"id": 1, "type": "price_update", "sku": "ALP-001", "price": "189.00"},
    {"id": 2, "type": "stock_alert", "sku": "ALP-003", "stock": 1},
    {"id": 3, "type": "price_update", "sku": "ALP-002", "price": "1199.00"},
    {"id": 4, "type": "new_order", "order_no": "BO-LIVE-001", "sku": "ALP-001"},
    {"id": 5, "type": "done", "message": "stream_complete"},
]


def _sse_page_html() -> bytes:
    events_json = json.dumps(SSE_EVENTS, ensure_ascii=False)
    body = f"""
<h1 id="sse-title">实时数据面板</h1>
<p>事件计数：<span id="sse-count">0</span></p>
<table id="sse-events">
  <thead><tr><th>ID</th><th>类型</th><th>详情</th></tr></thead>
  <tbody></tbody>
</table>
<p id="sse-status">connecting</p>
<script>
var EVENTS = {events_json};
var count = 0;
var tbody = document.querySelector('#sse-events tbody');
var statusEl = document.getElementById('sse-status');
statusEl.textContent = 'streaming';
function processEvent(idx) {{
  if (idx >= EVENTS.length) return;
  var d = EVENTS[idx];
  count++;
  document.getElementById('sse-count').textContent = count;
  var tr = document.createElement('tr');
  tr.innerHTML = '<td>' + d.id + '</td><td>' + d.type +
    '</td><td>' + JSON.stringify(d) + '</td>';
  tr.dataset.eventId = d.id;
  tbody.appendChild(tr);
  if (d.type === 'done') {{
    statusEl.textContent = 'complete';
  }} else {{
    setTimeout(function() {{ processEvent(idx + 1); }}, 80);
  }}
}}
setTimeout(function() {{ processEvent(0); }}, 100);
</script>"""
    return _page_shell("SSE 实时面板", body)


# ---------------------------------------------------------------------------
# G2: Rate limiting page (429 + Retry-After)
# ---------------------------------------------------------------------------

def _rate_limit_page_html() -> bytes:
    body = """
<h1 id="rl-title">限流测试页</h1>
<p id="rl-status">idle</p>
<p>成功响应：<span id="rl-success">0</span></p>
<p>429 次数：<span id="rl-blocked">0</span></p>
<button id="rl-fetch" onclick="doFetch()">发起请求</button>
<script>
var success = 0, blocked = 0;
function doFetch() {
  document.getElementById('rl-status').textContent = 'fetching';
  fetch('/api/rate-limited').then(function(r) {
    if (r.status === 429) {
      blocked++;
      document.getElementById('rl-blocked').textContent = blocked;
      document.getElementById('rl-status').textContent = 'rate_limited';
      var ra = r.headers.get('Retry-After');
      document.getElementById('rl-status').dataset.retryAfter = ra || '1';
    } else {
      return r.json().then(function(d) {
        success++;
        document.getElementById('rl-success').textContent = success;
        document.getElementById('rl-status').textContent = 'ok';
        document.getElementById('rl-status').dataset.payload = JSON.stringify(d);
      });
    }
  });
}
</script>"""
    return _page_shell("限流测试", body)


# ---------------------------------------------------------------------------
# G3: Lazy-loading images
# ---------------------------------------------------------------------------

LAZY_IMAGES = [
    {"src": f"/img/lazy-{i}.png", "alt": f"lazy-img-{i}"} for i in range(1, 9)
]


def _lazy_images_page_html() -> bytes:
    imgs = "\n".join(
        f'<div class="lazy-card" style="height:300px;margin:20px 0;">'
        f'<img data-src="{im["src"]}" alt="{im["alt"]}" class="lazy" '
        f'style="width:100px;height:100px;" /></div>'
        for im in LAZY_IMAGES
    )
    body = f"""
<h1 id="lazy-title">懒加载图片</h1>
<p>已加载：<span id="lazy-loaded">0</span> / {len(LAZY_IMAGES)}</p>
{imgs}
<script>
var loaded = 0;
var observer = new IntersectionObserver(function(entries) {{
  entries.forEach(function(entry) {{
    if (entry.isIntersecting) {{
      var img = entry.target;
      img.src = img.dataset.src;
      img.classList.add('loaded');
      loaded++;
      document.getElementById('lazy-loaded').textContent = loaded;
      observer.unobserve(img);
    }}
  }});
}}, {{ threshold: 0.1 }});
document.querySelectorAll('img.lazy').forEach(function(img) {{
  observer.observe(img);
}});
</script>"""
    return _page_shell("懒加载", body)


# ---------------------------------------------------------------------------
# G4: Multi-iframe postMessage communication
# ---------------------------------------------------------------------------

def _iframe_parent_html(alpha_base: str) -> bytes:
    body = f"""
<h1 id="iframe-title">跨框架通信</h1>
<p>收到回复：<span id="iframe-replies">0</span></p>
<div id="iframe-results"></div>
<iframe id="frame-a" src="/iframe/child-a" style="width:300px;height:200px;"></iframe>
<iframe id="frame-b" src="/iframe/child-b" style="width:300px;height:200px;"></iframe>
<button id="iframe-send" onclick="sendAll()">广播消息</button>
<script>
var replies = 0;
var results = document.getElementById('iframe-results');
window.addEventListener('message', function(e) {{
  replies++;
  document.getElementById('iframe-replies').textContent = replies;
  var p = document.createElement('p');
  p.className = 'iframe-reply';
  p.dataset.from = e.data.from;
  p.textContent = e.data.from + ': ' + e.data.result;
  results.appendChild(p);
}});
function sendAll() {{
  var frames = document.querySelectorAll('iframe');
  frames.forEach(function(f) {{
    f.contentWindow.postMessage({{action: 'compute', value: 42}}, '*');
  }});
}}
</script>"""
    return _page_shell("跨框架通信", body)


def _iframe_child_html(child_id: str, multiplier: int) -> bytes:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Child {child_id}</title></head>
<body>
<p id="child-label">Child {child_id}</p>
<p id="child-status">waiting</p>
<script>
window.addEventListener('message', function(e) {{
  if (e.data && e.data.action === 'compute') {{
    var result = e.data.value * {multiplier};
    document.getElementById('child-status').textContent = 'computed: ' + result;
    e.source.postMessage({{from: '{child_id}', result: result}}, '*');
  }}
}});
</script>
</body></html>""".encode("utf-8")


# ---------------------------------------------------------------------------
# G5: localStorage persistence page
# ---------------------------------------------------------------------------

def _localstorage_page_html() -> bytes:
    body = """
<h1 id="ls-title">本地存储管理</h1>
<form id="ls-form">
  <label>键 <input type="text" id="ls-key" name="key"></label>
  <label>值 <input type="text" id="ls-value" name="value"></label>
  <button type="button" id="ls-save" onclick="saveItem()">保存</button>
</form>
<p>已存储：<span id="ls-count">0</span></p>
<table id="ls-table">
  <thead><tr><th>键</th><th>值</th></tr></thead>
  <tbody></tbody>
</table>
<button id="ls-clear" onclick="clearAll()">清空</button>
<script>
function refreshTable() {
  var tbody = document.querySelector('#ls-table tbody');
  tbody.innerHTML = '';
  var count = 0;
  for (var i = 0; i < localStorage.length; i++) {
    var k = localStorage.key(i);
    if (k.startsWith('ls_')) {
      var tr = document.createElement('tr');
      tr.innerHTML = '<td class="ls-k">' + k.slice(3) + '</td>' +
        '<td class="ls-v">' + localStorage.getItem(k) + '</td>';
      tbody.appendChild(tr);
      count++;
    }
  }
  document.getElementById('ls-count').textContent = count;
}
function saveItem() {
  var k = document.getElementById('ls-key').value;
  var v = document.getElementById('ls-value').value;
  if (k) {
    localStorage.setItem('ls_' + k, v);
    refreshTable();
  }
}
function clearAll() {
  var keys = [];
  for (var i = 0; i < localStorage.length; i++) {
    var k = localStorage.key(i);
    if (k.startsWith('ls_')) keys.push(k);
  }
  keys.forEach(function(k) { localStorage.removeItem(k); });
  refreshTable();
}
refreshTable();
</script>"""
    return _page_shell("本地存储", body)


# ---------------------------------------------------------------------------
# G6: Server-side paginated + filterable + exportable table
# ---------------------------------------------------------------------------

G6_RECORDS = [
    {"id": i, "sku": p["sku"], "name": p["name"],
     "price": float(p["price"]), "category": p["category"]}
    for i, p in enumerate(PRODUCTS, start=1)
]

G6_PAGE_SIZE = 3


def _g6_table_html(records: list[dict], page: int, total_pages: int,
                   category: str, sort_by: str) -> bytes:
    rows = "\n".join(
        f'<tr data-id="{r["id"]}"><td>{r["sku"]}</td><td>{r["name"]}</td>'
        f'<td>{r["price"]}</td><td>{r["category"]}</td></tr>'
        for r in records
    )
    cats = sorted({r["category"] for r in G6_RECORDS})
    cat_opts = '<option value="">全部</option>' + "".join(
        f'<option value="{c}"{"selected" if c == category else ""}>{c}</option>'
        for c in cats
    )
    prev_disabled = "disabled" if page <= 1 else ""
    next_disabled = "disabled" if page >= total_pages else ""
    body = f"""
<h1 id="g6-title">服务端分页表</h1>
<form id="g6-filter" method="get" action="/server-table">
  <select name="category" id="g6-cat">{cat_opts}</select>
  <select name="sort" id="g6-sort">
    <option value="id"{"selected" if sort_by == "id" else ""}>默认</option>
    <option value="price_asc"{"selected" if sort_by == "price_asc" else ""}>价格↑</option>
    <option value="price_desc"{"selected" if sort_by == "price_desc" else ""}>价格↓</option>
  </select>
  <button type="submit" id="g6-apply">筛选</button>
</form>
<table id="g6-table">
  <thead><tr><th>SKU</th><th>名称</th><th>价格</th><th>类目</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<p id="g6-page-info">第 {page} 页 / 共 {total_pages} 页</p>
<a id="g6-prev" href="/server-table?page={page-1}&category={category}&sort={sort_by}"
   {"" if prev_disabled else ""}>{" " if prev_disabled else "上一页"}</a>
<a id="g6-next" href="/server-table?page={page+1}&category={category}&sort={sort_by}"
   {"" if next_disabled else ""}>{" " if next_disabled else "下一页"}</a>
<a id="g6-export" href="/api/export-csv?category={category}&sort={sort_by}">导出 CSV</a>
"""
    return _page_shell("服务端分页", body)


# ---------------------------------------------------------------------------
# H1: Shadow DOM deep interaction (custom element with internal form)
# ---------------------------------------------------------------------------

def _shadow_dom_page_html() -> bytes:
    body = """
<h1 id="shadow-title">Shadow DOM 深度交互</h1>
<div id="shadow-host"></div>
<p>提交结果：<span id="shadow-result">none</span></p>
<script>
class ProductCard extends HTMLElement {
  constructor() {
    super();
    var shadow = this.attachShadow({mode: 'open'});
    shadow.innerHTML = `
      <style>
        :host { display: block; border: 1px solid #ccc; padding: 12px; }
        .title { font-weight: bold; color: #333; }
        input { padding: 4px; }
      </style>
      <div class="title"><slot name="heading">默认标题</slot></div>
      <form id="inner-form">
        <label>数量 <input type="number" id="qty-input" name="qty" value="1"></label>
        <label>备注 <input type="text" id="note-input" name="note" placeholder="可选"></label>
        <button type="button" id="submit-btn">提交</button>
      </form>
      <p id="inner-status">waiting</p>
    `;
    shadow.getElementById('submit-btn').addEventListener('click', () => {
      var qty = shadow.getElementById('qty-input').value;
      var note = shadow.getElementById('note-input').value;
      shadow.getElementById('inner-status').textContent = 'submitted: ' + qty + '/' + note;
      this.dispatchEvent(new CustomEvent('card-submit', {
        detail: { qty: qty, note: note },
        bubbles: true
      }));
    });
  }
}
customElements.define('product-card', ProductCard);
var host = document.getElementById('shadow-host');
host.innerHTML = '<product-card><span slot="heading">测试商品卡</span></product-card>';
document.addEventListener('card-submit', function(e) {
  document.getElementById('shadow-result').textContent =
    e.detail.qty + '|' + e.detail.note;
});
</script>"""
    return _page_shell("Shadow DOM", body)


# ---------------------------------------------------------------------------
# H2: Web Worker computation
# ---------------------------------------------------------------------------

WORKER_JS = """
self.onmessage = function(e) {
  var n = e.data.n;
  var sum = 0;
  for (var i = 1; i <= n; i++) { sum += i; }
  self.postMessage({ n: n, sum: sum });
};
"""


def _worker_page_html() -> bytes:
    body = """
<h1 id="worker-title">Web Worker 计算</h1>
<p>状态：<span id="worker-status">idle</span></p>
<p>结果：<span id="worker-result">none</span></p>
<button id="worker-start" onclick="startWorker()">开始计算</button>
<script>
function startWorker() {
  document.getElementById('worker-status').textContent = 'computing';
  var blob = new Blob([document.getElementById('worker-src').textContent],
    {type: 'application/javascript'});
  var url = URL.createObjectURL(blob);
  var w = new Worker(url);
  w.onmessage = function(e) {
    document.getElementById('worker-result').textContent =
      'sum(1..' + e.data.n + ')=' + e.data.sum;
    document.getElementById('worker-status').textContent = 'done';
    w.terminate();
    URL.revokeObjectURL(url);
  };
  w.postMessage({n: 100});
}
</script>
<script type="text/plain" id="worker-src">
self.onmessage = function(e) {
  var n = e.data.n;
  var sum = 0;
  for (var i = 1; i <= n; i++) { sum += i; }
  self.postMessage({ n: n, sum: sum });
};
</script>"""
    return _page_shell("Web Worker", body)


# ---------------------------------------------------------------------------
# H3: MutationObserver page
# ---------------------------------------------------------------------------

def _mutation_observer_page_html() -> bytes:
    body = """
<h1 id="mo-title">MutationObserver 监听</h1>
<p>突变计数：<span id="mo-count">0</span></p>
<div id="mo-target"></div>
<button id="mo-add" onclick="addNode()">添加节点</button>
<button id="mo-modify" onclick="modifyNode()">修改节点</button>
<script>
var moCount = 0;
var target = document.getElementById('mo-target');
var observer = new MutationObserver(function(mutations) {
  moCount += mutations.length;
  document.getElementById('mo-count').textContent = moCount;
});
observer.observe(target, { childList: true, subtree: true, attributes: true });
var nodeIdx = 0;
function addNode() {
  nodeIdx++;
  var p = document.createElement('p');
  p.className = 'mo-item';
  p.id = 'mo-item-' + nodeIdx;
  p.textContent = '节点 ' + nodeIdx;
  target.appendChild(p);
}
function modifyNode() {
  var last = target.querySelector('.mo-item:last-child');
  if (last) {
    last.setAttribute('data-modified', 'true');
    last.textContent = last.textContent + ' (已修改)';
  }
}
</script>"""
    return _page_shell("MutationObserver", body)


# ---------------------------------------------------------------------------
# H4: Theme switching (dark/light mode)
# ---------------------------------------------------------------------------

def _theme_page_html() -> bytes:
    body = """
<h1 id="theme-title">主题切换</h1>
<button id="theme-toggle" onclick="toggleTheme()">切换主题</button>
<p id="theme-label">当前主题：light</p>
<div id="theme-box" style="padding:20px; background:#fff; color:#000;">
  示例内容
</div>
<style>
  body.dark { background: #1a1a1a; color: #eee; }
  body.dark #theme-box { background: #333; color: #eee; }
</style>
<script>
var saved = localStorage.getItem('theme');
if (saved === 'dark') { applyDark(); }
function toggleTheme() {
  if (document.body.classList.contains('dark')) {
    document.body.classList.remove('dark');
    localStorage.setItem('theme', 'light');
    document.getElementById('theme-label').textContent = '当前主题：light';
  } else {
    applyDark();
  }
}
function applyDark() {
  document.body.classList.add('dark');
  localStorage.setItem('theme', 'dark');
  document.getElementById('theme-label').textContent = '当前主题：dark';
}
</script>"""
    return _page_shell("主题切换", body)


# ---------------------------------------------------------------------------
# H5: Tooltip / hover interactions
# ---------------------------------------------------------------------------

TOOLTIP_ITEMS = [
    {"id": "tip-1", "label": "智能温控器", "tip": "工业级温度控制设备，精度±0.1°C"},
    {"id": "tip-2", "label": "工业网关", "tip": "支持 Modbus/OPC UA 协议转换"},
    {"id": "tip-3", "label": "边缘计算盒", "tip": "ARM Cortex-A72, 8GB RAM, -20~70°C"},
]


def _tooltip_page_html() -> bytes:
    items = "\n".join(
        f'<div class="tip-trigger" id="{t["id"]}" '
        f'data-tooltip="{t["tip"]}" '
        f'onmouseenter="showTip(this)" onmouseleave="hideTip()">'
        f'{t["label"]}</div>'
        for t in TOOLTIP_ITEMS
    )
    body = f"""
<h1 id="tip-title">Tooltip 提示</h1>
<div id="tip-container">{items}</div>
<div id="tooltip-box" style="display:none; position:absolute;
  background:#333; color:#fff; padding:8px; border-radius:4px;
  font-size:13px; max-width:240px; z-index:9999;"></div>
<script>
function showTip(el) {{
  var box = document.getElementById('tooltip-box');
  box.textContent = el.dataset.tooltip;
  box.style.display = 'block';
  var rect = el.getBoundingClientRect();
  box.style.left = rect.left + 'px';
  box.style.top = (rect.bottom + 4) + 'px';
}}
function hideTip() {{
  document.getElementById('tooltip-box').style.display = 'none';
}}
</script>"""
    return _page_shell("Tooltip", body)


# ---------------------------------------------------------------------------
# H6: Multi-step file processing (upload → process → download)
# ---------------------------------------------------------------------------

def _file_process_page_html() -> bytes:
    body = """
<h1 id="fp-title">文件处理流水线</h1>
<form id="fp-form" method="post" action="/api/file-process" enctype="multipart/form-data">
  <input type="file" id="fp-file" name="file">
  <select id="fp-action" name="action">
    <option value="uppercase">转大写</option>
    <option value="reverse">反转内容</option>
    <option value="linecount">行数统计</option>
  </select>
  <button type="submit" id="fp-submit">处理</button>
</form>
<p id="fp-status">waiting</p>
<div id="fp-result"></div>"""
    return _page_shell("文件处理", body)


def _file_process_result_html(action: str, filename: str, result: str) -> bytes:
    body = f"""
<h1 id="fp-done">处理完成</h1>
<p id="fp-action">操作：{action}</p>
<p id="fp-filename">文件：{filename}</p>
<pre id="fp-output">{result}</pre>
<a id="fp-download" href="/api/file-download?content={result}"
   download="result.txt">下载结果</a>"""
    return _page_shell("处理结果", body)


# ---------------------------------------------------------------------------
# I1: Modal dialog element
# ---------------------------------------------------------------------------

def _modal_page_html() -> bytes:
    body = """
<h1 id="modal-title">Modal 对话框</h1>
<button id="modal-open" onclick="document.getElementById('my-dialog').showModal()">
  打开对话框
</button>
<p>最近提交：<span id="modal-result">none</span></p>

<dialog id="my-dialog">
  <h2 id="dialog-heading">订单确认</h2>
  <form id="dialog-form" method="dialog">
    <label>商品 <input type="text" id="dlg-product" name="product"></label>
    <label>数量 <input type="number" id="dlg-qty" name="qty" value="1"></label>
    <button type="submit" id="dlg-confirm" value="confirm">确认</button>
    <button type="submit" id="dlg-cancel" value="cancel">取消</button>
  </form>
</dialog>

<script>
var dlg = document.getElementById('my-dialog');
dlg.addEventListener('close', function() {
  if (dlg.returnValue === 'confirm') {
    var p = document.getElementById('dlg-product').value;
    var q = document.getElementById('dlg-qty').value;
    document.getElementById('modal-result').textContent = p + 'x' + q;
  } else {
    document.getElementById('modal-result').textContent = 'cancelled';
  }
});
</script>"""
    return _page_shell("Modal", body)


# ---------------------------------------------------------------------------
# I2: Progress bar / task queue
# ---------------------------------------------------------------------------

def _progress_page_html() -> bytes:
    body = """
<h1 id="prog-title">任务进度</h1>
<progress id="prog-bar" value="0" max="100"></progress>
<p>进度：<span id="prog-value">0</span>%</p>
<p id="prog-status">idle</p>
<button id="prog-start" onclick="startTask()">开始</button>
<button id="prog-cancel" onclick="cancelTask()">取消</button>
<button id="prog-reset" onclick="resetTask()">重置</button>
<script>
var timer = null;
var progress = 0;
function startTask() {
  if (timer) return;
  document.getElementById('prog-status').textContent = 'running';
  timer = setInterval(function() {
    progress += 10;
    document.getElementById('prog-bar').value = progress;
    document.getElementById('prog-value').textContent = progress;
    if (progress >= 100) {
      clearInterval(timer);
      timer = null;
      document.getElementById('prog-status').textContent = 'complete';
    }
  }, 100);
}
function cancelTask() {
  if (timer) { clearInterval(timer); timer = null; }
  document.getElementById('prog-status').textContent = 'cancelled';
}
function resetTask() {
  if (timer) { clearInterval(timer); timer = null; }
  progress = 0;
  document.getElementById('prog-bar').value = 0;
  document.getElementById('prog-value').textContent = '0';
  document.getElementById('prog-status').textContent = 'idle';
}
</script>"""
    return _page_shell("任务进度", body)


# ---------------------------------------------------------------------------
# I3: Responsive layout (media query viewport-dependent)
# ---------------------------------------------------------------------------

def _responsive_page_html() -> bytes:
    body = """
<h1 id="resp-title">响应式布局</h1>
<div id="resp-layout" class="layout-wide">
  <div id="resp-sidebar" style="background:#e0e0e0;padding:10px;">侧边栏</div>
  <div id="resp-main" style="background:#f5f5f5;padding:10px;">主内容区</div>
</div>
<p id="resp-mode">wide</p>
<style>
  #resp-layout { display: flex; gap: 10px; }
  #resp-sidebar { width: 200px; flex-shrink: 0; }
  #resp-main { flex: 1; }
  @media (max-width: 600px) {
    #resp-layout { flex-direction: column; }
    #resp-sidebar { width: 100%; }
  }
</style>
<script>
function checkMode() {
  var w = window.innerWidth;
  document.getElementById('resp-mode').textContent = w <= 600 ? 'narrow' : 'wide';
}
window.addEventListener('resize', checkMode);
checkMode();
</script>"""
    return _page_shell("响应式", body)


# ---------------------------------------------------------------------------
# I4: HTTP caching with ETag
# ---------------------------------------------------------------------------

ETAG_CONTENT_V1 = {"version": 1, "data": "original content"}
ETAG_CONTENT_V2 = {"version": 2, "data": "updated content"}


# ---------------------------------------------------------------------------
# I5: Rich text editing (contenteditable + execCommand)
# ---------------------------------------------------------------------------

def _richtext_page_html() -> bytes:
    body = """
<h1 id="rt-title">富文本编辑</h1>
<div id="rt-toolbar">
  <button id="rt-bold" onclick="document.execCommand('bold')">B</button>
  <button id="rt-italic" onclick="document.execCommand('italic')">I</button>
  <button id="rt-underline" onclick="document.execCommand('underline')">U</button>
</div>
<div id="rt-editor" contenteditable="true"
     style="border:1px solid #ccc; min-height:100px; padding:8px;">
  在此编辑内容
</div>
<p>HTML 输出：</p>
<pre id="rt-output"></pre>
<button id="rt-export" onclick="exportHtml()">导出 HTML</button>
<script>
function exportHtml() {
  document.getElementById('rt-output').textContent =
    document.getElementById('rt-editor').innerHTML;
}
</script>"""
    return _page_shell("富文本", body)


# ---------------------------------------------------------------------------
# I6: Data attribute manipulation
# ---------------------------------------------------------------------------

I6_ITEMS = [
    {"id": f"item-{i}", "name": p["name"], "category": p["category"],
     "price": p["price"]}
    for i, p in enumerate(PRODUCTS[:8], start=1)
]


def _data_attr_page_html() -> bytes:
    items_html = "\n".join(
        f'<div class="da-item" id="{it["id"]}" '
        f'data-category="{it["category"]}" data-price="{it["price"]}" '
        f'data-selected="false">'
        f'<span class="da-name">{it["name"]}</span> '
        f'<span class="da-price">{it["price"]}</span> '
        f'<button class="da-toggle" onclick="toggleItem(\'{it["id"]}\')">选择</button>'
        f'</div>'
        for it in I6_ITEMS
    )
    body = f"""
<h1 id="da-title">Data 属性操作</h1>
<div id="da-filters">
  <button id="da-filter-all" onclick="filterBy('')">全部</button>
  <button id="da-filter-sensor" onclick="filterBy('传感')">传感</button>
  <button id="da-filter-network" onclick="filterBy('网络')">网络</button>
</div>
<p>已选：<span id="da-selected-count">0</span></p>
<div id="da-list">{items_html}</div>
<script>
function filterBy(cat) {{
  document.querySelectorAll('.da-item').forEach(function(el) {{
    if (!cat || el.dataset.category === cat) {{
      el.style.display = '';
    }} else {{
      el.style.display = 'none';
    }}
  }});
}}
function toggleItem(id) {{
  var el = document.getElementById(id);
  var selected = el.dataset.selected === 'true';
  el.dataset.selected = selected ? 'false' : 'true';
  el.style.background = selected ? '' : '#d4edda';
  updateCount();
}}
function updateCount() {{
  var count = document.querySelectorAll('.da-item[data-selected="true"]').length;
  document.getElementById('da-selected-count').textContent = count;
}}
</script>"""
    return _page_shell("Data 属性", body)


# ---------------------------------------------------------------------------
# J1: Tab panel navigation
# ---------------------------------------------------------------------------

J1_TABS = [
    {"id": "tab-overview", "label": "概览", "content": "产品概览：共 12 款工业设备在售。"},
    {"id": "tab-specs", "label": "规格", "content": "温控器规格：精度±0.1°C，量程-20~200°C。"},
    {"id": "tab-reviews", "label": "评价", "content": "用户好评率 96%，共 328 条评价。"},
]


def _tab_page_html() -> bytes:
    btns = " ".join(
        f'<button class="tab-btn" data-tab="{t["id"]}" '
        f'aria-selected="{str(i == 0).lower()}" '
        f'onclick="switchTab(\'{t["id"]}\')">{t["label"]}</button>'
        for i, t in enumerate(J1_TABS)
    )
    panels = "\n".join(
        f'<div class="tab-panel" id="{t["id"]}" '
        f'style="display:{"block" if i == 0 else "none"};">'
        f'{t["content"]}</div>'
        for i, t in enumerate(J1_TABS)
    )
    body = f"""
<h1 id="tab-title">标签页面板</h1>
<div id="tab-bar" role="tablist">{btns}</div>
<div id="tab-content">{panels}</div>
<script>
function switchTab(tabId) {{
  document.querySelectorAll('.tab-panel').forEach(function(p) {{
    p.style.display = p.id === tabId ? 'block' : 'none';
  }});
  document.querySelectorAll('.tab-btn').forEach(function(b) {{
    b.setAttribute('aria-selected', b.dataset.tab === tabId ? 'true' : 'false');
  }});
  location.hash = tabId;
}}
window.addEventListener('hashchange', function() {{
  var h = location.hash.slice(1);
  if (h) switchTab(h);
}});
if (location.hash) switchTab(location.hash.slice(1));
</script>"""
    return _page_shell("标签页", body)


# ---------------------------------------------------------------------------
# J2: Inline table editing
# ---------------------------------------------------------------------------

def _inline_edit_page_html() -> bytes:
    rows_html = "\n".join(
        f'<tr data-id="{p["id"]}">'
        f'<td class="ie-sku">{p["sku"]}</td>'
        f'<td class="ie-name" data-field="name" ondblclick="startEdit(this)">{p["name"]}</td>'
        f'<td class="ie-price" data-field="price" ondblclick="startEdit(this)">{p["price"]}</td>'
        f'</tr>'
        for p in PRODUCTS[:5]
    )
    body = f"""
<h1 id="ie-title">内联编辑表格</h1>
<p>最后编辑：<span id="ie-last-edit">none</span></p>
<table id="ie-table">
  <thead><tr><th>SKU</th><th>名称</th><th>价格</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>
<script>
var editingCell = null;
function startEdit(td) {{
  if (editingCell) return;
  editingCell = td;
  var original = td.textContent;
  td.dataset.original = original;
  var input = document.createElement('input');
  input.type = 'text';
  input.value = original;
  input.className = 'ie-input';
  input.onkeydown = function(e) {{
    if (e.key === 'Enter') {{ saveEdit(td, input.value); }}
    if (e.key === 'Escape') {{ cancelEdit(td); }}
  }};
  input.onblur = function() {{ saveEdit(td, input.value); }};
  td.textContent = '';
  td.appendChild(input);
  input.focus();
}}
function saveEdit(td, value) {{
  td.textContent = value;
  editingCell = null;
  var row = td.closest('tr');
  document.getElementById('ie-last-edit').textContent =
    row.dataset.id + ':' + td.dataset.field + '=' + value;
}}
function cancelEdit(td) {{
  td.textContent = td.dataset.original;
  editingCell = null;
}}
</script>"""
    return _page_shell("内联编辑", body)


# ---------------------------------------------------------------------------
# J3: Typeahead autocomplete
# ---------------------------------------------------------------------------

def _autocomplete_page_html() -> bytes:
    products_json = json.dumps(
        [{"sku": p["sku"], "name": p["name"]} for p in PRODUCTS],
        ensure_ascii=False,
    )
    body = f"""
<h1 id="ac-title">自动补全搜索</h1>
<input type="text" id="ac-input" placeholder="输入产品名..." autocomplete="off">
<div id="ac-suggestions" style="border:1px solid #ccc; display:none;"></div>
<p>已选：<span id="ac-selected">none</span></p>
<script>
var ALL = {products_json};
var timer = null;
document.getElementById('ac-input').addEventListener('input', function() {{
  clearTimeout(timer);
  var q = this.value.trim().toLowerCase();
  timer = setTimeout(function() {{
    var box = document.getElementById('ac-suggestions');
    if (!q) {{ box.style.display = 'none'; return; }}
    var matches = ALL.filter(function(p) {{
      return p.name.toLowerCase().indexOf(q) >= 0 ||
             p.sku.toLowerCase().indexOf(q) >= 0;
    }});
    if (matches.length === 0) {{ box.style.display = 'none'; return; }}
    box.innerHTML = matches.map(function(p) {{
      return '<div class="ac-option" data-sku="' + p.sku + '" ' +
        'onclick="selectOption(this)">' + p.sku + ' - ' + p.name + '</div>';
    }}).join('');
    box.style.display = 'block';
  }}, 150);
}});
function selectOption(el) {{
  document.getElementById('ac-input').value = el.textContent;
  document.getElementById('ac-selected').textContent = el.dataset.sku;
  document.getElementById('ac-suggestions').style.display = 'none';
}}
</script>"""
    return _page_shell("自动补全", body)


# ---------------------------------------------------------------------------
# J4: Toast notifications
# ---------------------------------------------------------------------------

def _toast_page_html() -> bytes:
    body = """
<h1 id="toast-title">Toast 通知</h1>
<button id="toast-success" onclick="showToast('success','操作成功')">成功</button>
<button id="toast-error" onclick="showToast('error','操作失败')">错误</button>
<button id="toast-warning" onclick="showToast('warning','请注意')">警告</button>
<div id="toast-container" style="position:fixed;top:10px;right:10px;z-index:9999;"></div>
<p>历史计数：<span id="toast-count">0</span></p>
<script>
var toastCount = 0;
function showToast(type, msg) {
  toastCount++;
  document.getElementById('toast-count').textContent = toastCount;
  var el = document.createElement('div');
  el.className = 'toast toast-' + type;
  el.textContent = msg;
  el.style.cssText = 'padding:10px 20px;margin:5px 0;border-radius:4px;' +
    'color:#fff;font-size:14px;opacity:1;transition:opacity 0.3s;';
  el.style.background = type === 'success' ? '#28a745' :
    type === 'error' ? '#dc3545' : '#ffc107';
  if (type === 'warning') el.style.color = '#333';
  document.getElementById('toast-container').appendChild(el);
  setTimeout(function() {
    el.style.opacity = '0';
    setTimeout(function() { el.remove(); }, 300);
  }, 800);
}
</script>"""
    return _page_shell("Toast", body)


# ---------------------------------------------------------------------------
# J5: URL hash navigation
# ---------------------------------------------------------------------------

J5_SECTIONS = [
    {"hash": "home", "title": "首页", "content": "欢迎来到首页"},
    {"hash": "about", "title": "关于", "content": "关于我们的公司介绍"},
    {"hash": "contact", "title": "联系", "content": "联系方式：support@example.com"},
]


def _hash_nav_page_html() -> bytes:
    links = " ".join(
        f'<a href="#{s["hash"]}" class="hash-link">{s["title"]}</a>'
        for s in J5_SECTIONS
    )
    sections_json = json.dumps(
        {s["hash"]: s for s in J5_SECTIONS}, ensure_ascii=False
    )
    body = f"""
<h1 id="hash-title">Hash 导航</h1>
<nav id="hash-nav">{links}</nav>
<div id="hash-content">
  <h2 id="hash-section-title">首页</h2>
  <p id="hash-section-body">欢迎来到首页</p>
</div>
<script>
var SECTIONS = {sections_json};
function navigate() {{
  var h = location.hash.slice(1) || 'home';
  var sec = SECTIONS[h];
  if (sec) {{
    document.getElementById('hash-section-title').textContent = sec.title;
    document.getElementById('hash-section-body').textContent = sec.content;
  }}
}}
window.addEventListener('hashchange', navigate);
navigate();
</script>"""
    return _page_shell("Hash 导航", body)


# ---------------------------------------------------------------------------
# J6: ARIA accessibility
# ---------------------------------------------------------------------------

def _aria_page_html() -> bytes:
    body = """
<h1 id="aria-title">无障碍测试</h1>

<div id="aria-accordion">
  <button class="acc-header" aria-expanded="false" aria-controls="acc-panel-1"
    onclick="toggleAcc(this, 'acc-panel-1')">
    产品信息
  </button>
  <div id="acc-panel-1" class="acc-panel" role="region" aria-hidden="true"
    style="display:none;">
    工业级传感器系列，支持多种协议。
  </div>

  <button class="acc-header" aria-expanded="false" aria-controls="acc-panel-2"
    onclick="toggleAcc(this, 'acc-panel-2')">
    技术规格
  </button>
  <div id="acc-panel-2" class="acc-panel" role="region" aria-hidden="true"
    style="display:none;">
    温度范围 -40°C ~ 125°C，精度 ±0.5%。
  </div>
</div>

<div id="aria-listbox" role="listbox" aria-label="产品选择">
  <div role="option" id="opt-1" aria-selected="false" tabindex="0"
    onclick="selectOpt(this)">智能温控器</div>
  <div role="option" id="opt-2" aria-selected="false" tabindex="0"
    onclick="selectOpt(this)">工业网关</div>
  <div role="option" id="opt-3" aria-selected="false" tabindex="0"
    onclick="selectOpt(this)">边缘计算盒</div>
</div>
<p>已选产品：<span id="aria-selected">none</span></p>

<script>
function toggleAcc(btn, panelId) {
  var panel = document.getElementById(panelId);
  var expanded = btn.getAttribute('aria-expanded') === 'true';
  btn.setAttribute('aria-expanded', expanded ? 'false' : 'true');
  panel.setAttribute('aria-hidden', expanded ? 'true' : 'false');
  panel.style.display = expanded ? 'none' : 'block';
}
function selectOpt(el) {
  document.querySelectorAll('[role=option]').forEach(function(o) {
    o.setAttribute('aria-selected', 'false');
  });
  el.setAttribute('aria-selected', 'true');
  document.getElementById('aria-selected').textContent = el.textContent;
}
</script>"""
    return _page_shell("无障碍", body)


# ---------------------------------------------------------------------------
# K1: Tree view
# ---------------------------------------------------------------------------

TREE_DATA = {
    "id": "root", "label": "产品目录", "children": [
        {"id": "sensor", "label": "传感器", "children": [
            {"id": "temp", "label": "温控器", "children": []},
            {"id": "humid", "label": "温湿度记录仪", "children": []},
            {"id": "vibr", "label": "振动传感器", "children": []},
        ]},
        {"id": "network", "label": "网络设备", "children": [
            {"id": "gateway", "label": "工业网关", "children": []},
            {"id": "fiber", "label": "光纤收发器", "children": []},
        ]},
        {"id": "compute", "label": "计算设备", "children": [
            {"id": "edge", "label": "边缘计算盒", "children": []},
        ]},
    ]
}


def _tree_node_html(node: dict, depth: int = 0) -> str:
    has_children = bool(node.get("children"))
    toggle = (
        f'<span class="tree-toggle" onclick="toggleNode(\'{node["id"]}\')">'
        f'▶</span>' if has_children else '<span class="tree-leaf">·</span>'
    )
    children_html = ""
    if has_children:
        items = "\n".join(_tree_node_html(c, depth + 1) for c in node["children"])
        children_html = (
            f'<ul class="tree-children" id="children-{node["id"]}" '
            f'style="display:none;">{items}</ul>'
        )
    return (
        f'<li class="tree-node" id="node-{node["id"]}" data-depth="{depth}">'
        f'{toggle}'
        f'<span class="tree-label" onclick="selectNode(\'{node["id"]}\')">'
        f'{node["label"]}</span>'
        f'{children_html}</li>'
    )


def _tree_page_html() -> bytes:
    tree_html = _tree_node_html(TREE_DATA)
    body = f"""
<h1 id="tree-title">树形视图</h1>
<p>已选：<span id="tree-selected">none</span></p>
<ul id="tree-root">{tree_html}</ul>
<script>
function toggleNode(id) {{
  var children = document.getElementById('children-' + id);
  var toggle = document.querySelector('#node-' + id + ' > .tree-toggle');
  if (children.style.display === 'none') {{
    children.style.display = 'block';
    toggle.textContent = '▼';
    document.getElementById('node-' + id).dataset.expanded = 'true';
  }} else {{
    children.style.display = 'none';
    toggle.textContent = '▶';
    document.getElementById('node-' + id).dataset.expanded = 'false';
  }}
}}
function selectNode(id) {{
  document.querySelectorAll('.tree-label').forEach(function(l) {{
    l.style.background = '';
  }});
  var label = document.querySelector('#node-' + id + ' > .tree-label');
  label.style.background = '#d4edda';
  document.getElementById('tree-selected').textContent = label.textContent;
}}
</script>"""
    return _page_shell("树形视图", body)


# ---------------------------------------------------------------------------
# K2: Carousel / slider
# ---------------------------------------------------------------------------

CAROUSEL_SLIDES = [
    {"title": "智能温控器", "desc": "精度±0.1°C", "color": "#3498db"},
    {"title": "工业网关", "desc": "Modbus/OPC UA", "color": "#2ecc71"},
    {"title": "边缘计算盒", "desc": "ARM Cortex-A72", "color": "#e74c3c"},
    {"title": "振动传感器", "desc": "三轴加速度", "color": "#f39c12"},
]


def _carousel_page_html() -> bytes:
    slides = "\n".join(
        f'<div class="carousel-slide" data-index="{i}" '
        f'style="display:{"block" if i == 0 else "none"}; '
        f'background:{s["color"]}; color:#fff; padding:40px; text-align:center;">'
        f'<h2>{s["title"]}</h2><p>{s["desc"]}</p></div>'
        for i, s in enumerate(CAROUSEL_SLIDES)
    )
    dots = " ".join(
        f'<span class="carousel-dot" data-index="{i}" '
        f'onclick="goTo({i})" '
        f'style="cursor:pointer; padding:4px 8px; '
        f'{"font-weight:bold;" if i == 0 else ""}">{i + 1}</span>'
        for i in range(len(CAROUSEL_SLIDES))
    )
    body = f"""
<h1 id="carousel-title">轮播图</h1>
<div id="carousel-container">{slides}</div>
<div id="carousel-dots">{dots}</div>
<button id="carousel-prev" onclick="prev()">上一张</button>
<button id="carousel-next" onclick="next()">下一张</button>
<p>当前：<span id="carousel-current">0</span> / {len(CAROUSEL_SLIDES) - 1}</p>
<script>
var current = 0;
var total = {len(CAROUSEL_SLIDES)};
function goTo(idx) {{
  document.querySelectorAll('.carousel-slide').forEach(function(s) {{
    s.style.display = parseInt(s.dataset.index) === idx ? 'block' : 'none';
  }});
  document.querySelectorAll('.carousel-dot').forEach(function(d) {{
    d.style.fontWeight = parseInt(d.dataset.index) === idx ? 'bold' : '';
  }});
  current = idx;
  document.getElementById('carousel-current').textContent = current;
}}
function next() {{ goTo((current + 1) % total); }}
function prev() {{ goTo((current - 1 + total) % total); }}
</script>"""
    return _page_shell("轮播图", body)


# ---------------------------------------------------------------------------
# K3: Client-side table sort
# ---------------------------------------------------------------------------

def _sortable_table_html() -> bytes:
    rows = "\n".join(
        f'<tr><td>{p["sku"]}</td><td>{p["name"]}</td>'
        f'<td data-value="{p["price"]}">{p["price"]}</td>'
        f'<td data-value="{p["stock"]}">{p["stock"]}</td></tr>'
        for p in PRODUCTS
    )
    body = f"""
<h1 id="sort-title">客户端排序表</h1>
<p>排序列：<span id="sort-col">none</span> <span id="sort-dir">none</span></p>
<table id="sort-table">
  <thead><tr>
    <th data-col="0" onclick="sortTable(0,'text')">SKU</th>
    <th data-col="1" onclick="sortTable(1,'text')">名称</th>
    <th data-col="2" onclick="sortTable(2,'number')">价格</th>
    <th data-col="3" onclick="sortTable(3,'number')">库存</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
<script>
var sortState = {{}};
function sortTable(col, type) {{
  var dir = sortState[col] === 'asc' ? 'desc' : 'asc';
  sortState = {{}};
  sortState[col] = dir;
  var tbody = document.querySelector('#sort-table tbody');
  var rows = Array.from(tbody.querySelectorAll('tr'));
  rows.sort(function(a, b) {{
    var va = a.cells[col].dataset.value || a.cells[col].textContent;
    var vb = b.cells[col].dataset.value || b.cells[col].textContent;
    if (type === 'number') {{ va = parseFloat(va); vb = parseFloat(vb); }}
    var cmp = va < vb ? -1 : (va > vb ? 1 : 0);
    return dir === 'asc' ? cmp : -cmp;
  }});
  rows.forEach(function(r) {{ tbody.appendChild(r); }});
  document.getElementById('sort-col').textContent = col;
  document.getElementById('sort-dir').textContent = dir;
}}
</script>"""
    return _page_shell("排序表", body)


# ---------------------------------------------------------------------------
# K4: Drag-and-drop file upload zone
# ---------------------------------------------------------------------------

def _dropzone_page_html() -> bytes:
    body = """
<h1 id="dz-title">拖拽上传</h1>
<div id="drop-zone" style="border:2px dashed #ccc; padding:40px; text-align:center;">
  拖拽文件到此处或 <input type="file" id="dz-input" multiple>
</div>
<p>已选文件：<span id="dz-count">0</span></p>
<div id="dz-preview"></div>
<script>
var zone = document.getElementById('drop-zone');
zone.addEventListener('dragover', function(e) { e.preventDefault(); zone.style.borderColor = '#2ecc71'; });
zone.addEventListener('dragleave', function() { zone.style.borderColor = '#ccc'; });
zone.addEventListener('drop', function(e) {
  e.preventDefault();
  zone.style.borderColor = '#ccc';
  handleFiles(e.dataTransfer.files);
});
document.getElementById('dz-input').addEventListener('change', function(e) {
  handleFiles(e.target.files);
});
function handleFiles(files) {
  var preview = document.getElementById('dz-preview');
  document.getElementById('dz-count').textContent = files.length;
  preview.innerHTML = '';
  Array.from(files).forEach(function(f) {
    var div = document.createElement('div');
    div.className = 'dz-file';
    div.textContent = f.name + ' (' + f.size + ' bytes)';
    preview.appendChild(div);
  });
}
</script>"""
    return _page_shell("拖拽上传", body)


# ---------------------------------------------------------------------------
# K5: Date picker
# ---------------------------------------------------------------------------

def _datepicker_page_html() -> bytes:
    body = """
<h1 id="dp-title">日期选择器</h1>
<p>已选日期：<span id="dp-selected">none</span></p>
<div id="dp-header">
  <button id="dp-prev-month" onclick="changeMonth(-1)">◀</button>
  <span id="dp-month-label"></span>
  <button id="dp-next-month" onclick="changeMonth(1)">▶</button>
</div>
<div id="dp-grid"></div>
<script>
var viewYear, viewMonth;
(function() {
  var now = new Date();
  viewYear = now.getFullYear();
  viewMonth = now.getMonth();
  renderCalendar();
})();
function changeMonth(delta) {
  viewMonth += delta;
  if (viewMonth < 0) { viewMonth = 11; viewYear--; }
  if (viewMonth > 11) { viewMonth = 0; viewYear++; }
  renderCalendar();
}
function renderCalendar() {
  document.getElementById('dp-month-label').textContent =
    viewYear + '-' + String(viewMonth + 1).padStart(2, '0');
  var grid = document.getElementById('dp-grid');
  grid.innerHTML = '';
  var first = new Date(viewYear, viewMonth, 1);
  var last = new Date(viewYear, viewMonth + 1, 0);
  for (var d = 1; d <= last.getDate(); d++) {
    var btn = document.createElement('button');
    btn.className = 'dp-day';
    btn.textContent = d;
    btn.dataset.date = viewYear + '-' +
      String(viewMonth + 1).padStart(2, '0') + '-' +
      String(d).padStart(2, '0');
    btn.onclick = function() { selectDate(this.dataset.date); };
    grid.appendChild(btn);
  }
}
function selectDate(dateStr) {
  document.querySelectorAll('.dp-day').forEach(function(b) {
    b.style.background = '';
  });
  var btn = document.querySelector('.dp-day[data-date="' + dateStr + '"]');
  if (btn) btn.style.background = '#3498db';
  document.getElementById('dp-selected').textContent = dateStr;
}
</script>"""
    return _page_shell("日期选择器", body)


# ---------------------------------------------------------------------------
# K6: Virtual scroll
# ---------------------------------------------------------------------------

VIRTUAL_TOTAL = 1000
VIRTUAL_ITEM_HEIGHT = 40


def _virtual_scroll_page_html() -> bytes:
    body = f"""
<h1 id="vs-title">虚拟滚动</h1>
<p>可见范围：<span id="vs-range">0-0</span> / {VIRTUAL_TOTAL}</p>
<div id="vs-container" style="height:400px; overflow-y:auto; border:1px solid #ccc;">
  <div id="vs-spacer" style="height:{VIRTUAL_TOTAL * VIRTUAL_ITEM_HEIGHT}px; position:relative;">
  </div>
</div>
<script>
var TOTAL = {VIRTUAL_TOTAL};
var ITEM_H = {VIRTUAL_ITEM_HEIGHT};
var BUFFER = 5;
var container = document.getElementById('vs-container');
var spacer = document.getElementById('vs-spacer');
function renderVisible() {{
  var scrollTop = container.scrollTop;
  var viewH = container.clientHeight;
  var startIdx = Math.max(0, Math.floor(scrollTop / ITEM_H) - BUFFER);
  var endIdx = Math.min(TOTAL, Math.ceil((scrollTop + viewH) / ITEM_H) + BUFFER);
  document.getElementById('vs-range').textContent = startIdx + '-' + (endIdx - 1);
  var existing = spacer.querySelectorAll('.vs-item');
  existing.forEach(function(el) {{ el.remove(); }});
  for (var i = startIdx; i < endIdx; i++) {{
    var div = document.createElement('div');
    div.className = 'vs-item';
    div.style.cssText = 'position:absolute; top:' + (i * ITEM_H) + 'px; ' +
      'height:' + ITEM_H + 'px; width:100%; box-sizing:border-box; ' +
      'padding:8px; border-bottom:1px solid #eee;';
    div.dataset.index = i;
    div.textContent = '项目 #' + i;
    spacer.appendChild(div);
  }}
}}
container.addEventListener('scroll', renderVisible);
renderVisible();
</script>"""
    return _page_shell("虚拟滚动", body)


# ---------------------------------------------------------------------------
# Alpha handler
# ---------------------------------------------------------------------------


def make_alpha_handler(store: ScenarioStore):
    class AlphaHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence test output
            pass

        def _send(self, body: bytes, mime: str = "text/html; charset=utf-8",
                  status: int = 200, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if path in ("/", "/page/1"):
                return self._send(_catalog_html(1))
            if path.startswith("/page/"):
                try:
                    page = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    return self._send(b"bad page", status=404)
                if 1 <= page <= TOTAL_PAGES:
                    return self._send(_catalog_html(page))
                return self._send(b"no such page", status=404)
            if path.startswith("/product/"):
                try:
                    pid = int(path.rsplit("/", 1)[-1])
                    product = next(p for p in PRODUCTS if p["id"] == pid)
                except (ValueError, StopIteration):
                    return self._send(b"no such product", status=404)
                return self._send(_detail_html(product))
            if path == "/form":
                return self._send(_form_html())
            if path == "/form/iframe":
                return self._send(_iframe_form_html())
            if path.startswith("/img/") and path.endswith(".png"):
                return self._send(PNG_BYTES, mime="image/png")
            if path == "/svg/logo.svg":
                return self._send(SVG_TREND.encode("utf-8"), mime="image/svg+xml")
            if path == "/files/report.csv":
                return self._send(
                    CSV_TEXT.encode("utf-8"), mime="text/csv",
                    extra={"Content-Disposition": "attachment; filename=report.csv"},
                )
            if path == "/files/spec.pdf":
                return self._send(PDF_BYTES, mime="application/pdf")
            if path == "/files/bundle.zip":
                return self._send(ZIP_BYTES, mime="application/zip")
            # --- D1: AJAX page + API endpoint ---
            if path == "/ajax":
                return self._send(_ajax_page_html())
            if path == "/api/products":
                time.sleep(AJAX_DELAY_MS / 1000)
                payload = json.dumps(
                    [{"sku": p["sku"], "name": p["name"],
                      "price": p["price"], "stock": p["stock"]}
                     for p in AJAX_PRODUCTS],
                    ensure_ascii=False,
                ).encode("utf-8")
                return self._send(payload, mime="application/json")
            # --- D2: infinite scroll page + feed API ---
            if path == "/feed":
                return self._send(_feed_page_html())
            if path == "/api/feed":
                offset = int((qs.get("offset") or ["0"])[0])
                limit = int((qs.get("limit") or [str(FEED_BATCH)])[0])
                chunk = PRODUCTS[offset:offset + limit]
                has_more = (offset + limit) < FEED_TOTAL
                payload = json.dumps({
                    "items": [{"sku": p["sku"], "name": p["name"],
                               "price": p["price"]} for p in chunk],
                    "has_more": has_more,
                    "total": FEED_TOTAL,
                }, ensure_ascii=False).encode("utf-8")
                return self._send(payload, mime="application/json")
            # --- D4: nested data page ---
            if path == "/nested-data":
                return self._send(_nested_data_html())
            # --- D5: flaky endpoint (500 then success) ---
            if path == "/flaky":
                if store.flaky_fail_remaining > 0:
                    store.flaky_fail_remaining -= 1
                    return self._send(
                        b"Internal Server Error", status=500,
                        mime="text/plain")
                return self._send(_flaky_success_html())
            # --- D5: redirect chain ---
            if path.startswith("/redirect/"):
                try:
                    depth = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    depth = 0
                if depth > 1:
                    return self._send(
                        b"", status=302,
                        extra={"Location": f"/redirect/{depth - 1}"})
                return self._send(
                    _page_shell("重定向终点",
                                '<h1 id="redirect-end">到达终点</h1>'))
            # --- D6: authenticated download ---
            if path == "/protected/report.pdf":
                cookie = self.headers.get("Cookie") or ""
                if "alpha_auth=yes" not in cookie.replace(" ", ""):
                    return self._send(
                        b'{"error":"unauthorized"}', status=403,
                        mime="application/json")
                return self._send(
                    PROTECTED_PDF_BYTES, mime="application/pdf",
                    extra={"Content-Disposition":
                           "attachment; filename=protected_report.pdf"})
            if path == "/auth/login":
                body = """
<h1 id="alpha-login-title">Alpha 登录</h1>
<form id="alpha-login-form" method="post" action="/auth/login">
  <label>用户 <input type="text" name="user" id="al-user"></label>
  <label>密码 <input type="password" name="pass" id="al-pass"></label>
  <button type="submit" id="al-submit">登录</button>
</form>"""
                return self._send(_page_shell("Alpha 登录", body))
            # --- E1: wizard ---
            if path == "/wizard/step/1":
                return self._send(_wizard_step_html(1))
            # --- E2: validated form ---
            if path == "/validated-form":
                return self._send(_validated_form_html())
            # --- E3: dialog triggers ---
            if path == "/dialogs":
                return self._send(_dialog_page_html())
            # --- E4: SPA navigation ---
            if path.startswith("/spa"):
                return self._send(_spa_page_html())
            # --- E5: polling page + API ---
            if path == "/polling":
                return self._send(_polling_page_html())
            if path == "/api/status":
                store.poll_counter += 1
                done = store.poll_counter >= 5
                payload = json.dumps({
                    "progress": store.poll_counter,
                    "done": done,
                }).encode("utf-8")
                return self._send(payload, mime="application/json")
            # --- E6: batch operations page ---
            if path == "/batch":
                return self._send(_batch_page_html(store.batch_items))
            # --- F1: search page ---
            if path == "/search":
                return self._send(_search_page_html())
            # --- F2: drag reorder page ---
            if path == "/drag":
                return self._send(_drag_page_html(store.drag_order))
            # --- F3: cookie consent page ---
            if path == "/consent":
                return self._send(_consent_page_html())
            # --- F5: keyboard page ---
            if path == "/keyboard":
                return self._send(_keyboard_page_html())
            # --- F6: clipboard page ---
            if path == "/clipboard":
                return self._send(_clipboard_page_html())
            # --- G1: SSE-style streaming page (client-side simulation) ---
            if path == "/sse":
                return self._send(_sse_page_html())
            # --- G2: rate limiting page + API ---
            if path == "/rate-limit":
                return self._send(_rate_limit_page_html())
            if path == "/api/rate-limited":
                if store.rate_limit_remaining > 0:
                    store.rate_limit_remaining -= 1
                    return self._send(
                        b'{"error":"rate_limited"}', status=429,
                        mime="application/json",
                        extra={"Retry-After": "1"})
                return self._send(
                    json.dumps({"status": "ok", "data": "rate_limit_passed"})
                    .encode("utf-8"),
                    mime="application/json")
            # --- G3: lazy images page ---
            if path == "/lazy-images":
                return self._send(_lazy_images_page_html())
            if path.startswith("/img/lazy-") and path.endswith(".png"):
                return self._send(PNG_BYTES, mime="image/png")
            # --- G4: iframe communication ---
            if path == "/iframe-comm":
                return self._send(
                    _iframe_parent_html(f"http://127.0.0.1:{self.server.server_address[1]}"))
            if path == "/iframe/child-a":
                return self._send(_iframe_child_html("child-a", 2))
            if path == "/iframe/child-b":
                return self._send(_iframe_child_html("child-b", 3))
            # --- G5: localStorage page ---
            if path == "/localstorage":
                return self._send(_localstorage_page_html())
            # --- G6: server-side paginated table ---
            if path == "/server-table":
                category = (qs.get("category") or [""])[0]
                sort_by = (qs.get("sort") or ["id"])[0]
                page_num = int((qs.get("page") or ["1"])[0])
                filtered = [r for r in G6_RECORDS
                            if not category or r["category"] == category]
                if sort_by == "price_asc":
                    filtered.sort(key=lambda r: r["price"])
                elif sort_by == "price_desc":
                    filtered.sort(key=lambda r: r["price"], reverse=True)
                total_p = max(1, (len(filtered) + G6_PAGE_SIZE - 1) // G6_PAGE_SIZE)
                page_num = max(1, min(page_num, total_p))
                start = (page_num - 1) * G6_PAGE_SIZE
                page_data = filtered[start:start + G6_PAGE_SIZE]
                return self._send(
                    _g6_table_html(page_data, page_num, total_p, category, sort_by))
            if path == "/api/export-csv":
                store.csv_exports += 1
                category = (qs.get("category") or [""])[0]
                sort_by = (qs.get("sort") or ["id"])[0]
                filtered = [r for r in G6_RECORDS
                            if not category or r["category"] == category]
                if sort_by == "price_asc":
                    filtered.sort(key=lambda r: r["price"])
                elif sort_by == "price_desc":
                    filtered.sort(key=lambda r: r["price"], reverse=True)
                lines = ["sku,name,price,category"]
                for r in filtered:
                    lines.append(f'{r["sku"]},{r["name"]},{r["price"]},{r["category"]}')
                csv_body = "\n".join(lines).encode("utf-8")
                return self._send(
                    csv_body, mime="text/csv",
                    extra={"Content-Disposition": "attachment; filename=export.csv"})
            # --- H1: Shadow DOM page ---
            if path == "/shadow-dom":
                return self._send(_shadow_dom_page_html())
            # --- H2: Web Worker page ---
            if path == "/worker":
                return self._send(_worker_page_html())
            # --- H3: MutationObserver page ---
            if path == "/mutation-observer":
                return self._send(_mutation_observer_page_html())
            # --- H4: Theme switching page ---
            if path == "/theme":
                return self._send(_theme_page_html())
            # --- H5: Tooltip page ---
            if path == "/tooltip":
                return self._send(_tooltip_page_html())
            # --- H6: File processing page ---
            if path == "/file-process":
                return self._send(_file_process_page_html())
            if path == "/api/file-download":
                content = (qs.get("content") or [""])[0]
                return self._send(
                    content.encode("utf-8"), mime="text/plain",
                    extra={"Content-Disposition": "attachment; filename=result.txt"})
            # --- I1: Modal dialog ---
            if path == "/modal":
                return self._send(_modal_page_html())
            # --- I2: Progress bar ---
            if path == "/progress":
                return self._send(_progress_page_html())
            # --- I3: Responsive layout ---
            if path == "/responsive":
                return self._send(_responsive_page_html())
            # --- I4: ETag caching API ---
            if path == "/api/cached-data":
                version = int((qs.get("v") or ["1"])[0])
                data = ETAG_CONTENT_V1 if version == 1 else ETAG_CONTENT_V2
                etag = f'"v{version}"'
                if_none_match = self.headers.get("If-None-Match", "")
                if if_none_match == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                return self._send(
                    payload, mime="application/json",
                    extra={"ETag": etag, "Cache-Control": "must-revalidate"})
            # --- I5: Rich text editor ---
            if path == "/richtext":
                return self._send(_richtext_page_html())
            # --- I6: Data attributes page ---
            if path == "/data-attr":
                return self._send(_data_attr_page_html())
            # --- J1: Tab panel ---
            if path == "/tabs":
                return self._send(_tab_page_html())
            # --- J2: Inline table editing ---
            if path == "/inline-edit":
                return self._send(_inline_edit_page_html())
            # --- J3: Autocomplete ---
            if path == "/autocomplete":
                return self._send(_autocomplete_page_html())
            # --- J4: Toast notifications ---
            if path == "/toast":
                return self._send(_toast_page_html())
            # --- J5: Hash navigation ---
            if path == "/hash-nav":
                return self._send(_hash_nav_page_html())
            # --- J6: ARIA accessibility ---
            if path == "/aria":
                return self._send(_aria_page_html())
            # --- K1: Tree view ---
            if path == "/tree":
                return self._send(_tree_page_html())
            # --- K2: Carousel ---
            if path == "/carousel":
                return self._send(_carousel_page_html())
            # --- K3: Sortable table ---
            if path == "/sortable":
                return self._send(_sortable_table_html())
            # --- K4: Drop zone ---
            if path == "/dropzone":
                return self._send(_dropzone_page_html())
            # --- K5: Date picker ---
            if path == "/datepicker":
                return self._send(_datepicker_page_html())
            # --- K6: Virtual scroll ---
            if path == "/virtual-scroll":
                return self._send(_virtual_scroll_page_html())
            return self._send(b"not found", status=404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            if path == "/submit":
                fields = parse_multipart(
                    body, self.headers.get("Content-Type") or ""
                )
                store.submissions.append(fields)
                return self._send(
                    _submit_result_html(fields),
                    extra={"Set-Cookie": "alpha_last_submit=ok; Path=/"},
                )
            if path == "/auth/login":
                fields = {
                    k: unquote(v[0]).replace("+", " ")
                    for k, v in parse_qs(
                        body.decode("utf-8", "replace"),
                        keep_blank_values=True,
                    ).items()
                }
                if fields.get("user") == "admin" and fields.get("pass") == "alpha123":
                    return self._send(
                        b"", status=302,
                        extra={
                            "Location": "/",
                            "Set-Cookie": "alpha_auth=yes; Path=/",
                        })
                return self._send(
                    _page_shell("登录失败",
                                '<p id="alpha-login-err">用户名或密码错误</p>'),
                    status=401)
            # --- E1: wizard POST steps ---
            if path == "/wizard/step/2":
                data = self._parse_form(body)
                if not data.get("company") or not data.get("contact"):
                    return self._send(
                        _page_shell("校验失败",
                                    '<p id="wizard-err">公司名称和联系人必填</p>'),
                        status=400)
                return self._send(_wizard_step_html(2, data))
            if path == "/wizard/step/3":
                data = self._parse_form(body)
                if not data.get("sku") or not data.get("qty"):
                    return self._send(
                        _page_shell("校验失败",
                                    '<p id="wizard-err">商品和数量必填</p>'),
                        status=400)
                return self._send(_wizard_step_html(3, data))
            if path == "/wizard/submit":
                data = self._parse_form(body)
                store.wizard_submissions.append(data)
                result_body = f"""
<h1 id="wizard-done">采购单已提交</h1>
<p id="wizard-order-no">单号：WZ-{len(store.wizard_submissions):04d}</p>
<p>公司：{data.get('company', '')}</p>
<p>SKU：{data.get('sku', '')} x {data.get('qty', '')}</p>"""
                return self._send(_page_shell("提交成功", result_body))
            # --- E2: validated form POST ---
            if path == "/validated-form/submit":
                data = self._parse_form(body)
                result = f"""
<h1 id="vf-done">表单提交成功</h1>
<p id="vf-echo-name">姓名：{data.get('name', '')}</p>
<p id="vf-echo-type">类型：{data.get('client_type', '')}</p>
<p id="vf-echo-taxid">税号：{data.get('tax_id', '')}</p>"""
                return self._send(_page_shell("提交成功", result))
            # --- F2: drag order API ---
            if path == "/api/drag-order":
                data = json.loads(body.decode("utf-8"))
                store.drag_order = data.get("order", [])
                payload = json.dumps(
                    {"message": f"顺序已保存（{len(store.drag_order)} 项）"}
                ).encode("utf-8")
                return self._send(payload, mime="application/json")
            # --- F3: consent API ---
            if path == "/api/consent":
                store.consent_given += 1
                return self._send(
                    b'{"ok":true}', mime="application/json")
            # --- F5: keyboard form submit ---
            if path == "/keyboard/submit":
                data = self._parse_form(body)
                result = (
                    f'<h1 id="kb-done">提交成功</h1>'
                    f'<p id="kb-echo">{data.get("field1","")}'
                    f'|{data.get("field2","")}'
                    f'|{data.get("field3","")}</p>'
                )
                return self._send(_page_shell("键盘提交成功", result))
            # --- H6: file processing API ---
            if path == "/api/file-process":
                ct = self.headers.get("Content-Type") or ""
                fields = parse_multipart(body, ct)
                action = fields.get("action", "uppercase")
                file_entry = fields.get("file", {})
                filename = "uploaded"
                text = ""
                if isinstance(file_entry, dict):
                    filename = file_entry.get("filename", "uploaded")
                    raw = file_entry.get("content", b"")
                    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                if action == "uppercase":
                    result = text.upper()
                elif action == "reverse":
                    result = text[::-1]
                elif action == "linecount":
                    result = str(text.count("\n") + (1 if text else 0))
                else:
                    result = text
                return self._send(_file_process_result_html(action, filename, result))
            # --- E6: batch API ---
            if path == "/api/batch":
                data = json.loads(body.decode("utf-8"))
                action = data.get("action", "")
                ids = data.get("ids", [])
                if action == "delete":
                    store.batch_items = [
                        it for it in store.batch_items if it["id"] not in ids
                    ]
                    msg = f"已删除 {len(ids)} 项"
                elif action == "export":
                    msg = f"已导出 {len(ids)} 项"
                else:
                    msg = "未知操作"
                payload = json.dumps({"message": msg, "action": action,
                                      "affected": ids}).encode("utf-8")
                return self._send(payload, mime="application/json")
            return self._send(b"not found", status=404)

        def _parse_form(self, body: bytes) -> dict[str, str]:
            return {
                k: unquote(v[0]).replace("+", " ")
                for k, v in parse_qs(
                    body.decode("utf-8", "replace"),
                    keep_blank_values=True,
                ).items()
            }

    return AlphaHandler


# ---------------------------------------------------------------------------
# Beta handler (cookie-gated orders)
# ---------------------------------------------------------------------------

BETA_COOKIE_NAME = "beta_session"
BETA_COOKIE = "beta_session=beta-ok"

CLEARANCE_COOKIE = "cf_clearance=fixture-cleared"

# How long the interstitial waits before auto-passing (ms); long enough for a
# probe to observe the challenge, short enough for passive-wait tests.
CHALLENGE_AUTOPASS_MS = 700


def _beta_challenge_html(next_path: str, autopass: bool = True) -> bytes:
    """Cloudflare-style interstitial the bot_challenge_guard probe must flag."""

    autopass_js = f"""
  <script>
    setTimeout(function () {{
      window.location.href = '/cdn-cgi/challenge?next=' +
        encodeURIComponent('{next_path}');
    }}, {CHALLENGE_AUTOPASS_MS});
  </script>""" if autopass else ""
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Just a moment...</title></head>
<body data-ray="fixture-ray-0001">
  <h1>Checking your browser before accessing Beta 订单系统</h1>
  <p>This process is automatic. Please wait while we verify you are human.</p>
  <form id="challenge-form" action="/cdn-cgi/challenge" method="get">
    <input type="hidden" name="next" value="{next_path}">
  </form>{autopass_js}
</body></html>"""
    return html.encode("utf-8")


def _beta_login_html(error: str = "") -> bytes:
    err = f"<p id='login-error'>{error}</p>" if error else ""
    body = f"""
<h1 id="beta-title">Beta 订单系统 - 登录</h1>{err}
<form id="login-form" method="post" action="/login">
  <label>账号 <input type="text" name="user" id="l-user"></label>
  <label>密码 <input type="password" name="password" id="l-pass"></label>
  <button type="submit" id="l-submit">登录</button>
</form>"""
    return _page_shell("Beta 登录", body)


def _beta_orders_html(orders: list[dict[str, Any]]) -> bytes:
    rows = "\n".join(
        f"<tr><td>{o['order_no']}</td><td>{o['sku']}</td>"
        f"<td>{o['qty']}</td><td>{o['customer']}</td></tr>"
        for o in orders
    )
    body = f"""
<h1 id="orders-title">Beta 订单系统</h1>
<table id="orders-table">
  <thead><tr><th>单号</th><th>SKU</th><th>数量</th><th>客户</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<form id="new-order-form" method="post" action="/orders/new">
  <label>SKU <input type="text" name="sku" id="o-sku"></label>
  <label>数量 <input type="number" name="qty" id="o-qty"></label>
  <label>客户 <input type="text" name="customer" id="o-customer"></label>
  <button type="submit" id="o-submit">下单</button>
</form>"""
    return _page_shell("Beta 订单", body)


def make_beta_handler(store: ScenarioStore, challenge: bool = False):
    class BetaHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: bytes, mime: str = "text/html; charset=utf-8",
                  status: int = 200, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            cookie = self.headers.get("Cookie") or ""
            for part in cookie.split(";"):
                part = part.strip()
                if part.startswith(BETA_COOKIE_NAME + "="):
                    val = part.split("=", 1)[1]
                    if val.startswith("v"):
                        return val == f"v{store.beta_session_version}"
                    return val == "beta-ok"
            return False

        def _cleared(self) -> bool:
            cookie = self.headers.get("Cookie") or ""
            return CLEARANCE_COOKIE.split("=", 1)[0] + "=" in cookie.replace(" ", "")

        def _waf_gate(self, path: str) -> bool:
            """Serve the interstitial / clearance endpoint. True = handled."""
            if not challenge:
                return False
            if path == "/cdn-cgi/challenge":
                store.waf_clearances += 1
                query = parse_qs(urlparse(self.path).query)
                next_path = (query.get("next") or ["/orders"])[0]
                if not next_path.startswith("/"):
                    next_path = "/orders"
                self._send(
                    b"", status=302,
                    extra={
                        "Location": next_path,
                        "Set-Cookie": f"{CLEARANCE_COOKIE}; Path=/",
                    },
                )
                return True
            if not self._cleared():
                store.waf_blocks += 1
                self._send(
                    _beta_challenge_html(path, autopass=store.waf_autopass),
                    status=403,
                )
                return True
            return False

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if self._waf_gate(path):
                return
            if path == "/login":
                return self._send(_beta_login_html())
            if path == "/orders":
                if not self._authed():
                    return self._send(
                        b"", status=302, extra={"Location": "/login"}
                    )
                return self._send(_beta_orders_html(store.orders))
            if path == "/expire-session":
                store.beta_session_version += 100
                return self._send(
                    _page_shell("会话已过期",
                                '<h1 id="expired-msg">会话已失效</h1>'))
            if path == "/":
                return self._send(b"", status=302, extra={"Location": "/orders"})
            return self._send(b"not found", status=404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if self._waf_gate(path):
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace")
            fields = {
                k: unquote(v[0]).replace("+", " ")
                for k, v in parse_qs(body, keep_blank_values=True).items()
            }
            if path == "/login":
                if fields.get("user") == "ops" and fields.get("password") == "secret":
                    store.beta_logins.append(fields["user"])
                    store.beta_session_version += 1
                    cookie_val = f"v{store.beta_session_version}"
                    return self._send(
                        b"", status=302,
                        extra={
                            "Location": "/orders",
                            "Set-Cookie": f"{BETA_COOKIE_NAME}={cookie_val}; Path=/",
                        },
                    )
                return self._send(_beta_login_html("账号或密码错误"))
            if path == "/orders/new":
                if not self._authed():
                    return self._send(b"forbidden", status=403)
                order = {
                    "order_no": f"BO-{len(store.orders) + 1:04d}",
                    "sku": fields.get("sku", ""),
                    "qty": fields.get("qty", ""),
                    "customer": fields.get("customer", ""),
                }
                store.orders.append(order)
                return self._send(
                    b"", status=302, extra={"Location": "/orders"}
                )
            return self._send(b"not found", status=404)

    return BetaHandler


# ---------------------------------------------------------------------------
# Server wrapper
# ---------------------------------------------------------------------------


class ScenarioServer:
    """One HTTP app bound to a host (127.0.0.1 / localhost) on a free port."""

    def __init__(self, handler_cls, host: str = "127.0.0.1") -> None:
        self.httpd = ThreadingHTTPServer((host, 0), handler_cls)
        self.host = host
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://{host}:{self.port}"
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )

    def start(self) -> "ScenarioServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

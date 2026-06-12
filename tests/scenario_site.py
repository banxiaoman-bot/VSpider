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
  - ``GET/POST /login``: sets ``beta_session=beta-ok`` on success.
  - ``/orders``: requires the auth cookie (302 to /login otherwise);
    lists recorded orders and offers a "new order" relay form.
  - ``POST /orders/new``: requires the cookie; appends to the store.

Everything is deterministic and offline; no external network access.
"""

from __future__ import annotations

import base64
import threading
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
            path = urlparse(self.path).path
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
            return self._send(b"not found", status=404)

    return AlphaHandler


# ---------------------------------------------------------------------------
# Beta handler (cookie-gated orders)
# ---------------------------------------------------------------------------

BETA_COOKIE = "beta_session=beta-ok"


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


def make_beta_handler(store: ScenarioStore):
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
            return BETA_COOKIE in cookie.replace(" ", "")

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/login":
                return self._send(_beta_login_html())
            if path == "/orders":
                if not self._authed():
                    return self._send(
                        b"", status=302, extra={"Location": "/login"}
                    )
                return self._send(_beta_orders_html(store.orders))
            if path == "/":
                return self._send(b"", status=302, extra={"Location": "/orders"})
            return self._send(b"not found", status=404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace")
            fields = {
                k: unquote(v[0]).replace("+", " ")
                for k, v in parse_qs(body, keep_blank_values=True).items()
            }
            if path == "/login":
                if fields.get("user") == "ops" and fields.get("password") == "secret":
                    store.beta_logins.append(fields["user"])
                    return self._send(
                        b"", status=302,
                        extra={
                            "Location": "/orders",
                            "Set-Cookie": f"{BETA_COOKIE}; Path=/",
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

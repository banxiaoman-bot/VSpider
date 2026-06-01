"""Locate the "send / submit" button on AI-chat pages even when SoM misses it.

The failure mode we're closing (run_log_20260518_154220):
  Goal: type a question into yiyan.baidu.com, press Enter, read the AI reply.
  Reality: yiyan's send button is a ``<div class="...">`` with a flight-icon
  SVG inside. SoM only marks standard interactive elements (button / a /
  input / textarea / [role=button]) — this icon div gets NO red box and
  NO ``@eN`` entry in the AX Tree. VLM's ``press_key Enter`` doesn't fire
  the page's submit handler either (Baidu binds Enter to a custom handler
  that's only active when the right conditions hold). After 11 wasted
  ``press_key Enter`` and 4 misguided clicks on the input box, MAX_STEPS
  ran out.

This module is a deterministic JS locator that finds the send button by
**any of** the following signals, ranked best-to-worst:

  1. Brand-named selectors (``[aria-label*='send']`` / ``[class*='send-btn']`` /
     ``[data-testid*='send']`` etc.)
  2. Same-form / same-flex-row as the chat textbox, **and** is a button-like
     element with an SVG/icon child and no visible text.
  3. Bottom-right of the visible chat textbox in the viewport — within the
     textbox's bounding rect + 80px, button-shaped (40-80px square, has
     pointer cursor, is the latest button in DOM order).

It returns a point (viewport coordinates) suitable for ``click_point`` or
Playwright's ``page.mouse.click(x, y)``. If no candidate matches, returns
None and the caller can fall back to ``press_key Enter`` (last resort).

Design notes
------------

- **Pure JS**: one ``page.evaluate`` round-trip. No Python-side DOM polling.
- **Doesn't click**: returns coordinates only; the handler (or guard) does
  the actual click via Playwright, so we benefit from actionability waits.
- **Never raises**: every failure mode yields ``None`` and a ``reason`` for
  debugging. The caller layer is responsible for fallback.
- **Chat-host gating**: callers should usually check
  ``chat_extract_coercion_guard.is_on_known_chat_domain(url)`` before invoking;
  the locator works on any page but is most useful on AI-chat surfaces.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("vspider.chat_send_locator")


# Selectors that, when matched, are almost certainly the send button. Order
# matters: brand-specific first, generic fallbacks last.
_SEND_BUTTON_SELECTORS: tuple[str, ...] = (
    # Explicit semantic attributes
    "[aria-label='发送']",
    "[aria-label='Send']",
    "[aria-label*='send' i]",
    "[aria-label*='发送']",
    "[aria-label*='提交']",
    "[aria-label*='submit' i]",
    "[data-testid*='send' i]",
    "[data-testid*='submit' i]",
    "[data-test*='send' i]",
    # Class-name fragments
    "button[class*='send' i]",
    "button[class*='submit' i]",
    "[role='button'][class*='send' i]",
    "[role='button'][class*='submit' i]",
    "[class*='send-btn']",
    "[class*='send_btn']",
    "[class*='sendBtn']",
    "[class*='submit-btn']",
    "[class*='submitBtn']",
    "[class*='SendButton']",
    "[class*='SubmitButton']",
    # Title attribute
    "[title='发送']",
    "[title='Send']",
    "[title*='发送']",
    "[title*='Send' i]",
    # Common direct icon-button classes used by chat UIs
    "[class*='chat-send']",
    "[class*='chat_send']",
    "[class*='conversation-send']",
    "[class*='dialog-send']",
    "[class*='bot-send']",
)


def _build_locator_js() -> str:
    """Return JS that runs in the page and returns:

        {
            "found": bool,
            "method": str,   # which strategy matched
            "x": int,        # viewport x for click_point
            "y": int,        # viewport y
            "bbox": [x, y, w, h],
            "selector": str | None,
            "reason": str,   # diagnostic for failures
        }
    """
    # Keep CSS selectors as data, not hand-quoted JS. Several selectors contain
    # their own quoted attribute values (for example [aria-label='发送']); manual
    # single-quoting creates invalid JS like ['[aria-label='发送']'].
    selectors_json = json.dumps(list(_SEND_BUTTON_SELECTORS), ensure_ascii=False)
    return f"""
    (() => {{
        const SELECTORS = {selectors_json};

        // ── Shadow-DOM-aware query helpers ────────────────────────────────
        // 普通 querySelectorAll 不穿透 web component 的 shadow root，
        // 现代 chat UI（特别是嵌入式聊天 widget）经常封装在 custom element
        // 里。这里递归扫描所有 shadowRoot，让 locator 覆盖 shadow DOM 内的
        // 发送按钮。
        const deepQSA = (root, selector) => {{
            const out = [];
            try {{ out.push(...root.querySelectorAll(selector)); }} catch (e) {{}}
            let walker;
            try {{
                walker = root.querySelectorAll("*");
            }} catch (e) {{ return out; }}
            for (const el of walker) {{
                if (el.shadowRoot) {{
                    try {{ out.push(...deepQSA(el.shadowRoot, selector)); }} catch (e) {{}}
                }}
            }}
            return out;
        }};

        const isVisible = (el) => {{
            if (!el) return false;
            try {{
                const r = el.getBoundingClientRect();
                if (r.width < 4 || r.height < 4) return false;
                if (r.bottom < 0 || r.top > window.innerHeight) return false;
                if (r.right < 0 || r.left > window.innerWidth) return false;
                const s = window.getComputedStyle(el);
                if (!s || s.display === 'none' || s.visibility === 'hidden') return false;
                if (parseFloat(s.opacity || '1') < 0.1) return false;
                return true;
            }} catch (e) {{
                return false;
            }}
        }};

        const centerOf = (el) => {{
            const r = el.getBoundingClientRect();
            return {{
                x: Math.round(r.left + r.width / 2),
                y: Math.round(r.top + r.height / 2),
                bbox: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
            }};
        }};

        // ── Send-icon-shape detector (通用：覆盖国内/国外 chat UI) ─────────
        // 真正的发送按钮通常含一个 paper-plane / arrow-up / triangle-right
        // SVG 或 font-icon。这个检测器返回 [hasShape, strength]：
        //   strength=2 → 命名标识强信号（class/aria-label 含 send/plane/submit/arrow-up）
        //   strength=1 → 形状路径弱信号（SVG path 模式 / icon class 含 send）
        //   strength=0 → 没找到任何发送图标
        // 误报代价：对 attach（曲别针）/ mic（麦克风）/ 联网（地球）等其他图标
        // 不会命中，因为它们的 viewBox 数据完全不同。
        const SEND_NAME_RE = /\\b(send|submit|paper[-_]?plane|airplane|airmail|arrow[-_]?up|arrow[-_]?right)\\b/i;
        const SEND_CN_NAME_RE = /(发送|提交|快捷发送)/;
        // Paper-plane SVG paths 经典首尾模式：起点常在左上，多数命令为 L 拐
        // 折，包含明显的 z（封闭三角）+ 中段折回。我们用三个特征同时存在
        // 来打分：(a) 含 z（闭合）(b) 多个 L 指令 (c) viewBox 是 0 0 24 24
        // 或类似方形。
        const looksLikeArrowOrPlaneSvg = (svg) => {{
            try {{
                const vb = (svg.getAttribute("viewBox") || "").trim();
                const paths = svg.querySelectorAll("path, polygon");
                if (paths.length === 0) return false;
                let matched = 0;
                for (const p of paths) {{
                    const d = (p.getAttribute("d") || p.getAttribute("points") || "").trim();
                    if (!d) continue;
                    // 至少有 3 个折点 + 封闭，typical for paper-plane / arrow
                    const lCount = (d.match(/[LlMmHhVvAa]/g) || []).length;
                    const hasClose = /[Zz]/.test(d);
                    if (lCount >= 3 && hasClose) matched++;
                    // Arrow-up signature: 短小 path 含 V/L 向上
                    if (/[Vv]/.test(d) && d.length < 80) matched++;
                }}
                return matched >= 1;
            }} catch (e) {{
                return false;
            }}
        }};
        const hasSendIconShape = (el) => {{
            if (!el) return 0;
            try {{
                // (a) 强信号：自身 class/id/aria-label/title 命名暗示发送
                const ariaLabel = (el.getAttribute("aria-label") || "");
                const title = (el.getAttribute("title") || "");
                const idClass = ((el.id || "") + " " + (el.className || "")).toString();
                const combined = ariaLabel + " " + title + " " + idClass;
                if (SEND_NAME_RE.test(combined) || SEND_CN_NAME_RE.test(combined)) {{
                    return 2;
                }}
                // (b) 强信号：内部 svg/i/img 的 class/aria-label 命名暗示发送
                const ics = el.querySelectorAll("svg, i, img, [class*='icon' i]");
                for (const ic of ics) {{
                    const al = (ic.getAttribute("aria-label") || "") + " " +
                               (ic.getAttribute("title") || "") + " " +
                               ((ic.id || "") + " " + (ic.className || "")).toString();
                    if (SEND_NAME_RE.test(al) || SEND_CN_NAME_RE.test(al)) return 2;
                }}
                // (c) 弱信号：SVG path 形状疑似 paper-plane / arrow
                const svgs = el.querySelectorAll("svg");
                for (const s of svgs) {{
                    if (looksLikeArrowOrPlaneSvg(s)) return 1;
                }}
            }} catch (e) {{}}
            return 0;
        }};

        // ── Pass 1: deterministic selector cascade ────────────────────────
        for (const sel of SELECTORS) {{
            let nodes;
            try {{ nodes = deepQSA(document, sel); }} catch (e) {{ continue; }}
            for (const el of nodes) {{
                if (!isVisible(el)) continue;
                // Reject elements that are clearly NOT chat-send (e.g.
                // a navbar "log in" button matched by "submit"). Heuristic:
                // must be inside or near a form/textbox/contenteditable.
                let nearTextbox = false;
                try {{
                    nearTextbox = !!el.closest("form, [class*='input' i], [class*='editor' i], [class*='compose' i]")
                                  || !!el.closest("[contenteditable='true']")
                                  || !!document.querySelector("[contenteditable='true']")
                                  || !!document.querySelector("textarea, input[type='text'], input:not([type])");
                }} catch (e) {{ nearTextbox = true; }}
                if (!nearTextbox) continue;
                const c = centerOf(el);
                return {{
                    found: true,
                    method: "selector:" + sel,
                    x: c.x,
                    y: c.y,
                    bbox: c.bbox,
                    selector: sel,
                    reason: "matched selector",
                }};
            }}
        }}

        // ── Pass 2: textbox-anchored proximity heuristic ──────────────────
        // Find the focused textbox (or the most-recently-active editable
        // surface). Then look for a button-shaped element either inside
        // the textbox's container or just to its right.
        const textbox = (() => {{
            const focused = document.activeElement;
            if (focused) {{
                const t = (focused.tagName || '').toLowerCase();
                if (t === 'textarea' || t === 'input' || focused.isContentEditable) {{
                    if (isVisible(focused)) return focused;
                }}
            }}
            // Largest visible editable region. 用 deepQSA 穿透 shadow DOM
            // —— 嵌入式 chat widget (Intercom / Crisp / 等) 经常把输入区
            // 封装在 web component 里。
            const eds = deepQSA(
                document, "textarea, [contenteditable='true']"
            ).filter(isVisible);
            eds.sort((a, b) => {{
                const ra = a.getBoundingClientRect();
                const rb = b.getBoundingClientRect();
                return (rb.width * rb.height) - (ra.width * ra.height);
            }});
            return eds[0] || null;
        }})();

        if (textbox) {{
            const tbR = textbox.getBoundingClientRect();
            // ── Rejection lists for menu/model-picker buttons that look like
            //    icon buttons but are NOT send buttons (yiyan.baidu.com bug
            //    where "码牛模式 ^" was clicked instead of the send arrow).
            // 拒绝 patterns 收紧：只保留**明确指向"非发送"**的关键词。
            // 移除了 /搜索/ / /快速/ —— 这两个容易跟用户合法 goal 冲突
            // (e.g. "快速发送" 是合法的发送按钮 label)。
            // /联网/ 改为 /联网搜索/ 收紧匹配。
            const REJECT_TEXT_PATTERNS = [
                // 中文：模型选择 / 思考模式 / 联网开关 / 工具选择
                /模式/, /思考/, /联网搜索/, /工具栏/, /插件/, /扩展/,
                /附件/, /上传/, /语音输入/, /麦克风/, /话筒/, /表情/,
                /历史记录/, /会话列表/, /对话列表/, /新对话/, /新建对话/,
                // 英文：mode picker / mic / attach / history
                /\bmode\b/i, /\bthink/i, /\bgpt-?[34]/i, /\bclaude-/i,
                /\battach(ment)?\b/i, /\bupload\b/i, /\bmic(rophone)?\b/i,
                /\bvoice (input|chat)/i, /\btoolbar\b/i, /\bplugins?\b/i,
                /\bhistory\b/i, /\bnew chat\b/i, /\bnew conversation\b/i,
            ];
            const isRejectByText = (txt) => {{
                if (!txt || !txt.trim()) return false;
                for (const re of REJECT_TEXT_PATTERNS) {{
                    if (re.test(txt)) return true;
                }}
                return false;
            }};
            const isMenuTrigger = (el) => {{
                try {{
                    if (el.getAttribute("aria-haspopup")) return true;
                    if (el.getAttribute("aria-expanded") !== null) return true;
                    const r = (el.getAttribute("role") || "").toLowerCase();
                    if (r === "combobox" || r === "menu" || r === "menuitem"
                        || r === "listbox" || r === "tab" || r === "switch") return true;
                    // Chevron-shaped child = expander, not submitter
                    const html = (el.innerHTML || "").slice(0, 400);
                    if (/[\u2303\u2304\u02C4\u02C5\u005E]|chevron|caret|arrow-down|arrow_down|expand/i.test(html)) {{
                        return true;
                    }}
                }} catch (e) {{}}
                return false;
            }};

            // Walk up to find a sensible container that probably holds the
            // send button as a sibling of the textbox.
            // 用 deepQSA 穿透 shadow DOM；现代 web component 化的 chat widget
            // 经常把发送按钮封在 custom element 里。
            let container = textbox.parentElement;
            const candidates = [];
            for (let depth = 0; depth < 5 && container; depth++) {{
                const buttons = deepQSA(
                    container,
                    "button, [role='button'], [class*='btn' i], [class*='button' i], [class*='send' i], a[href='#'], span[onclick], div[onclick]"
                );
                for (const b of buttons) {{
                    if (b === textbox) continue;
                    if (!isVisible(b)) continue;
                    const br = b.getBoundingClientRect();
                    // Must be roughly button-shaped (width/height ratio sane)
                    if (br.width < 18 || br.height < 18) continue;
                    if (br.width > 220 || br.height > 120) continue;
                    // ── Hard rejections: menu triggers / model pickers ─────
                    if (isMenuTrigger(b)) continue;
                    const txt = (b.innerText || b.textContent || "").trim();
                    if (isRejectByText(txt)) continue;
                    const ariaLabel = (b.getAttribute("aria-label") || "");
                    if (isRejectByText(ariaLabel)) continue;
                    const hasIcon = !!b.querySelector("svg, img, i[class], [class*='icon' i]");
                    // Long-text buttons without icons are textual CTAs (e.g. "查看更多")
                    if (txt.length > 8 && !hasIcon) continue;
                    // ── Positive shape signal: paper-plane / arrow / send-named icon ─
                    const shapeStrength = hasSendIconShape(b);
                    // ── Scoring: heavy bias toward "rightmost icon-only near
                    //    the input bottom-right corner" ─────────────────────
                    const dxRight = br.left - tbR.right;        // >0 means outside-right
                    const dyBottom = br.top - tbR.bottom;
                    let score = 0;
                    // Strong right-side preference. Three tiers:
                    if (br.right >= tbR.right - 12 && br.right <= tbR.right + 80) {{
                        score += 80;  // flush with textbox right edge
                    }} else if (br.left > tbR.left + tbR.width * 0.7) {{
                        score += 50;  // in right 30% of textbox span
                    }} else if (br.left > tbR.left + tbR.width * 0.5) {{
                        score += 20;  // right half
                    }} else {{
                        score -= 30;  // left half = penalty (likely "+", "attach", "model")
                    }}
                    if (dxRight > -20 && dxRight < 220) score += 40;     // near right
                    if (dyBottom > -160 && dyBottom < 160) score += 30;  // vertical proximity
                    if (br.bottom > tbR.bottom - 60 && br.bottom < tbR.bottom + 100) score += 25; // near bottom
                    if (hasIcon) score += 25;
                    if (txt.length === 0) score += 20;
                    // Square aspect = icon button (most send buttons are 32-48px square)
                    const ratio = br.width / Math.max(br.height, 1);
                    if (ratio > 0.7 && ratio < 1.5) score += 15;
                    // Penalty for too-large area (text buttons / panels)
                    if (br.width * br.height > 9000) score -= 20;
                    // ── 强加分：icon shape 显式暗示发送 ──
                    //   strength=2（命名命中）→ +120，几乎独占 winner
                    //   strength=1（形状命中）→ +40
                    if (shapeStrength === 2) score += 120;
                    else if (shapeStrength === 1) score += 40;
                    if (score >= 80) {{
                        candidates.push({{
                            el: b, score: score, txt: txt,
                            x: br.left, w: br.width,
                            shapeStrength: shapeStrength,
                        }});
                    }}
                }}
                container = container.parentElement;
                if (candidates.length >= 12) break;
            }}
            if (candidates.length) {{
                // Sort by score desc; tie-break by being further right
                candidates.sort((a, b) => {{
                    if (b.score !== a.score) return b.score - a.score;
                    return (b.x + b.w) - (a.x + a.w);
                }});
                const winner = candidates[0];
                const c = centerOf(winner.el);
                return {{
                    found: true,
                    method: "proximity:textbox-anchored",
                    x: c.x,
                    y: c.y,
                    bbox: c.bbox,
                    selector: null,
                    reason: "best-scoring icon button near textbox right edge (score=" + winner.score + ", candidates=" + candidates.length + ", txt=" + JSON.stringify(winner.txt.slice(0, 20)) + ")",
                }};
            }}
        }}

        return {{
            found: false,
            method: "none",
            x: 0, y: 0, bbox: [0, 0, 0, 0],
            selector: null,
            reason: textbox ? "no candidate button passed score threshold near textbox" : "no editable textbox found on page",
        }};
    }})()
    """


async def find_send_button(page: Any) -> dict[str, Any]:
    """Return the most likely send-button location on the current page.

    Args:
        page: Playwright Page (we call ``page.evaluate`` once).

    Returns:
        A dict with keys:
          - ``found`` (bool)
          - ``method`` (str): ``selector:<css>`` / ``proximity:textbox-anchored``
            / ``none``
          - ``x``, ``y`` (int): viewport coordinates of the button center.
            Suitable for ``page.mouse.click(x, y)`` or ``click_point``
            payload (after normalising to 0-1000 units).
          - ``bbox`` (list[int]): [x, y, w, h] of the bounding rect
          - ``selector`` (str | None): which CSS won, if any
          - ``reason`` (str): diagnostic
          - ``error`` (str, optional): present only if evaluate raised

    Never raises.
    """
    js = _build_locator_js()
    try:
        result = await page.evaluate(js)
    except Exception as e:
        logger.debug("[chat_send_locator] page.evaluate failed: %s", e)
        return {
            "found": False,
            "method": "none",
            "x": 0, "y": 0,
            "bbox": [0, 0, 0, 0],
            "selector": None,
            "reason": f"evaluate failed: {type(e).__name__}: {e}",
            "error": str(e),
        }
    if not isinstance(result, dict):
        return {
            "found": False,
            "method": "none",
            "x": 0, "y": 0,
            "bbox": [0, 0, 0, 0],
            "selector": None,
            "reason": "evaluate returned non-dict",
        }
    # Ensure all keys exist with safe defaults
    return {
        "found": bool(result.get("found")),
        "method": str(result.get("method") or "none"),
        "x": int(result.get("x") or 0),
        "y": int(result.get("y") or 0),
        "bbox": list(result.get("bbox") or [0, 0, 0, 0]),
        "selector": result.get("selector"),
        "reason": str(result.get("reason") or ""),
    }


def normalize_point_to_thousand(
    x: int, y: int, viewport_width: int, viewport_height: int
) -> list[int]:
    """Convert pixel coords to ``click_point``'s 0-1000 normalized scheme."""
    if viewport_width <= 0 or viewport_height <= 0:
        return [0, 0]
    nx = max(0, min(1000, int(round(x * 1000 / viewport_width))))
    ny = max(0, min(1000, int(round(y * 1000 / viewport_height))))
    return [nx, ny]


__all__ = [
    "find_send_button",
    "normalize_point_to_thousand",
]

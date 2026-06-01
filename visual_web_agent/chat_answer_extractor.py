"""Generic AI-chat answer extractor.

Why this module exists
----------------------
The generic ``extract`` action grabs DOM_LIST and AX text, which works for
search-result / table pages but **fails on chat pages** for two reasons:

  1. **Streaming**: the AI answer is still being appended to the DOM when the
     VLM looks. Whatever it copies is partial or empty.
  2. **Search-then-answer UX**: Baidu's ``yiyan.baidu.com`` / ``chat.baidu.com``
     render search-result cards above/alongside the streaming AI answer. A
     generic DOM_LIST extractor picks up the search cards (because they are
     repeated, prominent list items) and **misses the answer block entirely**.

This module is a deterministic JS-driven extraction step the VLM can call once
the user's question has been submitted into a chat-shaped page. It does three
things in order:

  1. Wait for streaming to stabilise — poll the answer-block text length until
     it stops growing (or hard-cap at ``timeout``).
  2. Probe a cascade of *known* answer-block selectors (covers 文心/通义/豆包/
     Kimi/Claude/ChatGPT/智谱/元宝/DeepSeek/Perplexity).
  3. Fallback: the largest visible text block on the page that is **not** an
     ``ol``/``ul``/``nav``/``footer``/sidebar/search-card.

The result is a dict the calling Handler can drop into ``workflow_memory``
and ``extracted_data`` — see ``extract_chat_answer`` for the schema.

Failure modes intentionally covered
-----------------------------------
- Page is mid-stream → wait loop keeps polling for ≤ ``timeout`` seconds.
- Site uses an obscure class name → selector cascade falls through to the
  text-density heuristic.
- Page is a pure search page (drift guard misfire safety net) → the heuristic
  excludes search-card containers and lists, so we don't return junk.
- JS evaluation crashes → caller wraps in try/except and returns a structured
  error instead of leaking the exception into the action loop.

Author note: stay JS-side as much as possible — one ``page.evaluate`` round
trip beats a Python loop poking the DOM step by step.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)


# ── Selector cascade (most-specific first) ──────────────────────────────────
# Each entry is a CSS selector probed across the whole DOM. For each selector
# we keep ALL matching nodes and pick the LARGEST text-length one (not the
# first), because streaming chat UIs frequently render the answer last and
# wrap it in the same generic class as the question bubble.
ANSWER_SELECTORS: tuple[str, ...] = (
    # ── Brand-named answer blocks (highest confidence) ──
    "[class*='ai-answer']",
    "[class*='ai_answer']",
    "[class*='chat-answer']",
    "[class*='chat_answer']",
    "[class*='ai-response']",
    "[class*='ai_response']",
    "[class*='chat-response']",
    "[class*='ai-generated']",
    "[class*='ai_generated']",
    "[class*='answer-content']",
    "[class*='answer_content']",
    "[class*='response-content']",
    "[class*='response_content']",
    "[class*='generated-content']",
    # ── Specific known landmarks ──
    "[data-message-author-role='assistant']",   # ChatGPT
    "[data-testid*='conversation-turn']",        # ChatGPT variants
    "[data-testid*='assistant']",
    "[data-role='assistant']",
    "[data-author='assistant']",
    # ── 百度文心/搜索-然后-回答 系特有 ──
    # 百度文心常用：.ai-content-* / .smart-card / .robot-bubble / .markdown-content
    # 即使没文档化，下面的 fragment 都能 catch
    "[class*='robot-bubble']",
    "[class*='bot-bubble']",
    "[class*='ai-bubble']",
    "[class*='ai-content']",
    "[class*='ai_content']",
    "[class*='smart-answer']",
    "[class*='smart_answer']",
    "[class*='smart-card']",
    # ── 通义/Kimi/豆包 等常见 ──
    "[class*='markdown-body']",
    "[class*='markdown-content']",
    "[class*='prose']",
    "[class*='reply-content']",
    "[class*='bot-message']",
    "[class*='assistant-message']",
    "[class*='message-content']",
    "[class*='message_content']",
    "[class*='dialog-bubble']",
    "[class*='dialog-content']",
    "[class*='conversation-content']",
    "article[class*='answer']",
    "article[class*='response']",
    # ── 通用兜底（最宽，可能误中，但放在 cascade 最后） ──
    # 注意：放在最后才用，且依然走"最大文本节点"挑选
    "[class*='bubble'][class*='bot']",
    "[class*='bubble'][class*='reply']",
    "[class*='answer']",
    "[class*='reply']",
)

# Sites which we recognise as chat hosts. The hostname check enables an
# "aggressive fallback" mode where, if no selector hits, we treat the
# largest text density on the page as the answer (search-result containers
# already excluded). On non-chat hosts we stay conservative.
KNOWN_CHAT_HOSTS: frozenset[str] = frozenset({
    "yiyan.baidu.com",
    "chat.baidu.com",
    "chat.openai.com",
    "chatgpt.com",
    "claude.ai",
    "tongyi.aliyun.com",
    "chat.qwen.ai",
    "www.doubao.com",
    "doubao.com",
    "kimi.moonshot.cn",
    "chatglm.cn",
    "yuanbao.tencent.com",
    "chat.deepseek.com",
    "gemini.google.com",
    "copilot.microsoft.com",
    "www.perplexity.ai",
    "perplexity.ai",
})

# Streaming indicators — when present, the AI is still typing. Keep polling
# instead of accepting the current (partial) answer.
STREAMING_INDICATORS: tuple[str, ...] = (
    "[class*='is-streaming']",
    "[class*='is-typing']",
    "[class*='is-generating']",
    "[class*='loading-dots']",
    "[class*='typing-indicator']",
    "[class*='generating']",
    "[class*='streaming']",
    "[class*='result-loading']",
    "[aria-busy='true']",
)

# Containers that should be EXCLUDED from the largest-text fallback. These are
# almost always search results, navigation, ads, or sidebars on the
# "search-then-answer" hybrid pages.
EXCLUDE_CONTAINERS: tuple[str, ...] = (
    "header",
    "nav",
    "footer",
    "aside",
    "[role='search']",
    "[role='navigation']",
    "[role='banner']",
    "[role='contentinfo']",
    "[class*='search-result']",
    "[class*='search_result']",
    "[class*='sresult']",
    "[class*='result-list']",
    "[class*='result_list']",
    "[class*='sidebar']",
    "[class*='side-bar']",
    "[class*='related']",
    "[class*='ad-']",
    "[class*='ads-']",
    "[id*='search-result']",
)

# Tunables — chosen so we are responsive on fast answers without abandoning
# streamers that produce long replies. Defaults are tuned for the worst case
# (百度文心长回答约 15-20s 流式)。
MIN_ANSWER_LEN = 80           # below this we treat the block as "not the answer yet"
POLL_INTERVAL_SEC = 0.8       # how often the wait loop re-evaluates
STABLE_TICKS_REQUIRED = 3     # text length unchanged this many ticks → stable
DEFAULT_TIMEOUT_SEC = 25.0    # hard cap; tuned for long streaming answers
MAX_RETURN_CHARS = 8000       # truncate to keep memory + log size sane
STREAMING_GRACE_MS = 1500     # extra wait once streaming indicator disappears


_THINKING_DONE_RE = re.compile(
    r"(?:思考完成\s*[:：]?\s*(?:准备输出结果)?|准备输出结果)\s*",
    re.IGNORECASE,
)

_ANSWER_TAIL_MARKERS: tuple[str, ...] = (
    "深度分析需求并解答，你需要什么帮助？",
    "码牛模式",
    "全选参考",
    "var _hmt=",
    "performance.mark",
    "serviceWorker",
    "deepseek是做什么的",
    "介绍一下deepseek",
    "介绍一下DeepSeek的业务范畴",
    "DeepSeek有哪些竞争对手",
)


def clean_chat_answer_text(text: str) -> str:
    """Trim common chat-page chrome from extracted answer text.

    Some chat sites expose hidden reasoning text and page chrome through
    ``innerText``/``textContent`` when selector extraction misses and the handler
    falls back to a whole-page sweep. Keep the final answer segment, not the
    thought trace, composer placeholder, recommendation chips, or injected JS.
    """
    cleaned = str(text or "").replace("\xa0", " ").replace("\ufeff", "")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")

    matches = list(_THINKING_DONE_RE.finditer(cleaned))
    if matches:
        cleaned = cleaned[matches[-1].end():]
    else:
        cleaned = re.sub(r"^\s*深度思考已完成\s*", "", cleaned)

    cut_positions: list[int] = []
    for marker in _ANSWER_TAIL_MARKERS:
        idx = cleaned.find(marker)
        if idx > 0:
            cut_positions.append(idx)
    if cut_positions:
        cleaned = cleaned[: min(cut_positions)]

    cleaned = re.split(
        r"var\s+_hmt\s*=|performance\.mark|serviceWorker",
        cleaned,
        maxsplit=1,
    )[0]
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _build_extract_js() -> str:
    """Return a self-contained JS snippet that probes for the answer block.

    The snippet runs **in the page**, not in Python. It returns a JSON-friendly
    object:

        {
            "method": "selector:<css>" | "fallback:largest-text" | "none",
            "selector": "<css>" or null,
            "text": "...",
            "length": <int>,
            "html_len": <int>,
            "title": "<document.title>",
            "url": "<location.href>",
            "candidates_scanned": <int>
        }

    All the heavy lifting (selector cascade + density heuristic) is JS so we
    don't pay a round-trip per probe.
    """
    selectors_json = "[" + ",".join(f"'{s}'" for s in ANSWER_SELECTORS) + "]"
    excludes_json = "[" + ",".join(f"'{s}'" for s in EXCLUDE_CONTAINERS) + "]"
    streaming_json = "[" + ",".join(f"'{s}'" for s in STREAMING_INDICATORS) + "]"

    return f"""
    (() => {{
        const SELECTORS = {selectors_json};
        const EXCLUDES = {excludes_json};
        const STREAMING_SELECTORS = {streaming_json};
        const MIN_LEN = {MIN_ANSWER_LEN};

        // ── Scroll the page (and any inner scroll containers) to bottom ────
        // Streaming chat UIs frequently render only what's near the viewport.
        // We poke the page-level scroll AND any scrollable descendants so the
        // streamer keeps appending tokens we can read.
        try {{ window.scrollTo({{top: document.body.scrollHeight, behavior: 'instant'}}); }} catch (e) {{}}
        try {{
            const scrollables = document.querySelectorAll('[class*="scroll"], [class*="overflow"], main, article, [role="main"]');
            for (const sc of scrollables) {{
                try {{
                    if (sc.scrollHeight > sc.clientHeight + 8) {{
                        sc.scrollTop = sc.scrollHeight;
                    }}
                }} catch (e) {{}}
            }}
        }} catch (e) {{}}

        const visibleText = (el) => {{
            if (!el) return "";
            try {{
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return "";
            }} catch (e) {{ /* detached */ }}
            try {{
                const style = window.getComputedStyle(el);
                if (!style || style.display === "none" || style.visibility === "hidden") return "";
            }} catch (e) {{ /* shadow / cross-origin */ }}
            return (el.innerText || el.textContent || "").trim();
        }};

        const isExcluded = (el) => {{
            for (const sel of EXCLUDES) {{
                try {{ if (el.closest(sel)) return true; }} catch (e) {{ /* invalid sel */ }}
            }}
            return false;
        }};

        // ── Streaming indicator probe ──────────────────────────────────────
        let streaming = false;
        for (const sel of STREAMING_SELECTORS) {{
            try {{
                const node = document.querySelector(sel);
                if (node) {{
                    const rect = node.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {{
                        streaming = true;
                        break;
                    }}
                }}
            }} catch (e) {{}}
        }}

        // ── Pass 1: cascade — keep the LARGEST hit, don't bail on first ────
        let scanned = 0;
        let best = null;
        for (const sel of SELECTORS) {{
            let nodes;
            try {{ nodes = document.querySelectorAll(sel); }} catch (e) {{ continue; }}
            for (const el of nodes) {{
                scanned += 1;
                if (isExcluded(el)) continue;
                const txt = visibleText(el);
                if (!txt || txt.length < MIN_LEN) continue;
                if (best === null || txt.length > best.len) {{
                    best = {{
                        el: el,
                        sel: sel,
                        text: txt,
                        len: txt.length,
                        html_len: (el.innerHTML || "").length,
                    }};
                }}
            }}
            // Early-out: once we have a clearly long answer (>=400 chars) on a
            // brand-named selector, skip the looser fallbacks below.
            if (best !== null && best.len >= 400 && best.sel.indexOf("[class*='answer']") === -1 && best.sel.indexOf("[class*='reply']") === -1) {{
                break;
            }}
        }}

        if (best !== null) {{
            return {{
                method: "selector:" + best.sel,
                selector: best.sel,
                text: best.text,
                length: best.len,
                html_len: best.html_len,
                title: document.title || "",
                url: location.href,
                streaming: streaming,
                candidates_scanned: scanned,
            }};
        }}

        // ── Pass 2: largest-text fallback ──────────────────────────────────
        // Score each candidate by char count of OWN text, prefer paragraphs
        // grouped in <article>, <main>, [class*='content'], divs with many <p>.
        const candidates = [];
        const root = document.body;
        if (root) {{
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, null);
            let node = walker.nextNode();
            while (node) {{
                if (!isExcluded(node)) {{
                    const tag = (node.tagName || "").toLowerCase();
                    if (!["script", "style", "noscript", "svg", "canvas", "ol", "ul", "li", "nav", "header", "footer", "aside"].includes(tag)) {{
                        let isCandidate = false;
                        if (tag === "article" || tag === "main") {{
                            isCandidate = true;
                        }} else {{
                            const paras = node.querySelectorAll(":scope > p, :scope > div > p");
                            if (paras && paras.length >= 2) isCandidate = true;
                        }}
                        if (isCandidate) {{
                            const t = visibleText(node);
                            if (t && t.length >= MIN_LEN) {{
                                candidates.push({{ el: node, text: t, len: t.length }});
                            }}
                        }}
                    }}
                }}
                node = walker.nextNode();
            }}
        }}

        if (candidates.length > 0) {{
            candidates.sort((a, b) => b.len - a.len);
            const top = candidates[0];
            return {{
                method: "fallback:largest-text",
                selector: null,
                text: top.text,
                length: top.len,
                html_len: (top.el.innerHTML || "").length,
                title: document.title || "",
                url: location.href,
                streaming: streaming,
                candidates_scanned: scanned + candidates.length,
            }};
        }}

        return {{
            method: "none",
            selector: null,
            text: "",
            length: 0,
            html_len: 0,
            title: document.title || "",
            url: location.href,
            streaming: streaming,
            candidates_scanned: scanned,
        }};
    }})()
    """


async def _probe_once(page: Any) -> dict[str, Any]:
    """Single JS evaluation. Returns the raw probe payload (may have method=none)."""
    js = _build_extract_js()
    try:
        result = await page.evaluate(js)
    except Exception as e:
        logger.debug("[chat_extract] page.evaluate failed: %s", e)
        return {
            "method": "none",
            "selector": None,
            "text": "",
            "length": 0,
            "html_len": 0,
            "title": "",
            "url": "",
            "candidates_scanned": 0,
            "error": str(e),
        }
    if not isinstance(result, dict):
        return {
            "method": "none",
            "selector": None,
            "text": "",
            "length": 0,
            "html_len": 0,
            "title": "",
            "url": "",
            "candidates_scanned": 0,
        }
    return result


async def extract_chat_answer(
    page: Any,
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    min_length: int = MIN_ANSWER_LEN,
    poll_interval: float = POLL_INTERVAL_SEC,
    stable_ticks: int = STABLE_TICKS_REQUIRED,
) -> dict[str, Any]:
    """Extract the AI-generated answer from a chat-style page.

    Args:
        page: Playwright Page (sync DOM access required).
        timeout: Maximum seconds to wait for streaming to stabilise.
        min_length: Minimum text length to consider a block as "the answer".
        poll_interval: Seconds between probes during the wait loop.
        stable_ticks: Consecutive ticks with unchanged length to declare stable.

    Returns:
        A dict with keys:
          - ``ok`` (bool): True iff an answer was found.
          - ``answer`` (str): The extracted text (truncated to MAX_RETURN_CHARS).
          - ``method`` (str): ``selector:<css>`` / ``fallback:largest-text`` / ``none``.
          - ``selector`` (str | None): Which CSS hit, if any.
          - ``length`` (int): Original (pre-truncation) text length.
          - ``stable`` (bool): True if text length stabilised before timeout.
          - ``wait_ms`` (int): Wall-clock time spent waiting.
          - ``url`` / ``title``: Page metadata at extraction time.
          - ``error`` (str, optional): Present only if all probes failed.

    The function never raises — every failure mode is encoded into the result
    dict so the calling Handler can record a clean ``ActionResult`` even when
    the page is uncooperative.
    """
    deadline = time.monotonic() + max(2.0, float(timeout))
    last_len = -1
    stable_count = 0
    best: dict[str, Any] | None = None
    started = time.monotonic()
    poll_count = 0
    saw_streaming = False

    while time.monotonic() < deadline:
        poll_count += 1
        probe = await _probe_once(page)
        cur_len = int(probe.get("length") or 0)
        cur_streaming = bool(probe.get("streaming"))
        if cur_streaming:
            saw_streaming = True

        logger.debug(
            "[chat_extract] poll #%d len=%d method=%s streaming=%s",
            poll_count, cur_len, probe.get("method"), cur_streaming,
        )

        if cur_len >= min_length:
            # Track the longest probe seen — streaming may temporarily shrink
            # text length (re-render), and we want to keep the high-water mark.
            if best is None or cur_len >= int(best.get("length") or 0):
                best = probe
            # Stability requires BOTH unchanged length AND no streaming flag.
            # Without the streaming check we end up grabbing a half-finished
            # answer between two token bursts that happen to have equal length.
            if cur_len == last_len and not cur_streaming:
                stable_count += 1
                if stable_count >= stable_ticks:
                    break
            else:
                stable_count = 0
                last_len = cur_len
        else:
            stable_count = 0
            last_len = cur_len
            if best is None:
                best = probe

        try:
            await asyncio.sleep(poll_interval)
        except Exception:
            break

    # ── Streaming-grace tail ──────────────────────────────────────────────
    # If we ever saw the streaming indicator and the loop ended because of
    # stability (not timeout), give the page one more probe after a short
    # grace period — some sites set is-streaming=false a beat before the last
    # token lands in the DOM.
    if saw_streaming and best is not None and time.monotonic() < deadline:
        try:
            await asyncio.sleep(min(STREAMING_GRACE_MS / 1000.0, 2.0))
            tail = await _probe_once(page)
            if int(tail.get("length") or 0) >= int(best.get("length") or 0):
                best = tail
                poll_count += 1
        except Exception:
            pass

    wait_ms = int((time.monotonic() - started) * 1000)

    if best is None or int(best.get("length") or 0) < min_length:
        return {
            "ok": False,
            "answer": (best or {}).get("text") or "",
            "method": (best or {}).get("method") or "none",
            "selector": (best or {}).get("selector"),
            "length": int((best or {}).get("length") or 0),
            "stable": False,
            "wait_ms": wait_ms,
            "polls": poll_count,
            "url": (best or {}).get("url") or "",
            "title": (best or {}).get("title") or "",
            "error": (best or {}).get("error")
                or f"no chat-answer block found (min_length={min_length}, polls={poll_count})",
        }

    text = clean_chat_answer_text(str(best.get("text") or ""))
    truncated = text[:MAX_RETURN_CHARS]

    logger.info(
        "[chat_extract] OK method=%s len=%d (truncated to %d) wait_ms=%d polls=%d",
        best.get("method"),
        len(text),
        len(truncated),
        wait_ms,
        poll_count,
    )

    return {
        "ok": True,
        "answer": truncated,
        "method": best.get("method"),
        "selector": best.get("selector"),
        "length": len(text),
        "stable": stable_count >= stable_ticks,
        "wait_ms": wait_ms,
        "polls": poll_count,
        "url": best.get("url") or "",
        "title": best.get("title") or "",
    }


__all__ = [
    "extract_chat_answer",
    "clean_chat_answer_text",
    "ANSWER_SELECTORS",
    "EXCLUDE_CONTAINERS",
    "MIN_ANSWER_LEN",
    "DEFAULT_TIMEOUT_SEC",
    "MAX_RETURN_CHARS",
]

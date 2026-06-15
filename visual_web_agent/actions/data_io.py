from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from ._base import (
    ActionContext,
    ActionHandler,
    ActionRegistry,
    UnknownActionError,
    _click_locator_with_js_fallback,
    _is_navigation_context_destroyed,
    _resolve_env_placeholders,
    _scroll_largest_container,
)

try:
    from ..vlm_client import VSpiderAction
    from ..browser_env import ActionExecutionError
    from ..auth_vault import SecretResolutionError, resolve_env_placeholders as _resolve_env
    from ..artifact_manager import register_download_artifact
    from ..page_data_controller import DATA_SIGNATURE_JS, pagination_moved
    from ..chat_answer_extractor import clean_chat_answer_text, extract_chat_answer
    from ..chat_send_locator import find_send_button as _chat_find_send_button
except ImportError:
    from vlm_client import VSpiderAction
    from browser_env import ActionExecutionError
    from auth_vault import SecretResolutionError, resolve_env_placeholders as _resolve_env
    from artifact_manager import register_download_artifact
    from page_data_controller import DATA_SIGNATURE_JS, pagination_moved
    from chat_answer_extractor import clean_chat_answer_text, extract_chat_answer
    from chat_send_locator import find_send_button as _chat_find_send_button

if TYPE_CHECKING:
    from playwright.async_api import Page
    from ..browser_env import BrowserEnv

logger = logging.getLogger("vspider.actions")


"""ExtractLink / DownloadImage / Upload / SaveToMemory / ChatExtract / ChatSubmit handlers"""


# ════════════════════════════════════════════════════════════════
#  批次 3：数据 & 记忆动作（extract_link / download_image /
#                          upload / save_to_memory）
# ════════════════════════════════════════════════════════════════

def _persist_extracted_link(target_id: object, extracted_url: str) -> str:
    """Persist one extracted link honoring the active run's output_contract
    container (run-scoped), instead of a blind CWD ``output_links.xlsx``
    (mission §一-A: never default to Excel). Returns the written path, or ``""``
    when no run is active (so unit/test envs never litter the CWD).
    """
    try:
        from ..io_contract import (
            current_base_dir,
            current_run_id,
            read_output_contract,
        )
        from ..data_writers import save_run_dataset
    except ImportError:  # pragma: no cover - standalone import fallback
        from io_contract import (  # type: ignore[no-redef]
            current_base_dir,
            current_run_id,
            read_output_contract,
        )
        from data_writers import save_run_dataset  # type: ignore[no-redef]

    run_id = current_run_id()
    if not run_id:
        return ""
    base = current_base_dir()
    try:
        contract = read_output_contract(run_id, base_dir=base)
    except Exception:
        contract = None
    try:
        return save_run_dataset(
            {"target_id": target_id, "url": extracted_url},
            run_id=run_id,
            output_contract=contract,
            produced_by="extract_link",
            filename_hint="extract_link",
            source_url=str(extracted_url or ""),
            base_dir=base,
        )
    except Exception as exc:  # pragma: no cover - never break the action loop
        logger.warning("[EXTRACT_LINK] contract-aware save failed: %s", exc)
        return ""


@ActionRegistry.register("extract_link")
class ExtractLinkHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing extract_link: element #{target_id} ({selector})")
        try:
            target = await browser._resolve_action_target(target_id, "click")
            if not target:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
                )
            extracted_url = await target.handle.evaluate(
                """el => el.getAttribute('data-som-url') || el.getAttribute('href') || el.getAttribute('src') || ''"""
            )
            if extracted_url:
                logger.info(
                    f"[EXTRACT_LINK] Successfully extracted URL: {extracted_url}"
                )
                print(
                    f"\n\033[1;32m[LINK EXTRACTED]\033[0m "
                    f"\033[36m{extracted_url}\033[0m\n"
                )
                _persist_extracted_link(target_id, extracted_url)
            else:
                logger.warning(
                    f"[EXTRACT_LINK] No URL found in data-som-url for element #{target_id}"
                )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"Extract link for element #{target_id} failed: {e}")
        return None


@ActionRegistry.register("download_image")
class DownloadImageHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        target_id = ctx.action.target_id
        selector = f'[data-som-id="{target_id}"]'
        logger.info(f"Executing download_image: element #{target_id} ({selector})")
        try:
            target = await browser._resolve_action_target(target_id, "click")
            if not target:
                raise RuntimeError(
                    f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
                )
            extracted_url = await target.handle.evaluate(
                """el => el.getAttribute('data-som-url') || el.getAttribute('src') || el.getAttribute('href') || ''"""
            )

            if extracted_url:
                abs_url = urllib.parse.urljoin(page.url, extracted_url)
                logger.info(f"[DOWNLOAD_IMAGE] Target URL: {abs_url}")

                response = await browser._context.request.get(abs_url)
                img_bytes = await response.body()

                img_dir = browser._download_dir / "images"
                img_dir.mkdir(parents=True, exist_ok=True)

                content_type = response.headers.get("content-type", "")
                ext = ".png"
                if "jpeg" in content_type or "jpg" in content_type:
                    ext = ".jpg"
                elif "gif" in content_type:
                    ext = ".gif"

                filename = f"img_{int(time.time())}{ext}"
                filepath = img_dir / filename
                filepath.write_bytes(img_bytes)

                print(
                    f"\n\033[1;32m✅ 成功下载图片:\033[0m "
                    f"\033[36m{filepath.resolve()}\033[0m\n"
                )
                logger.info(f"[DOWNLOAD_IMAGE] Saved local: {filepath.resolve()}")
                register_download_artifact(
                    filepath,
                    source_url=abs_url,
                    mime=content_type,
                    produced_by="browser_action",
                    step_id="download_image",
                )
            else:
                logger.warning(
                    f"[DOWNLOAD_IMAGE] No URL found for element #{target_id}"
                )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"Download image for element #{target_id} failed: {e}")
        return None


@ActionRegistry.register("upload")
class UploadHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        target_id = ctx.action.target_id
        type_value = ctx.action.type_value
        logger.info(f"Executing upload: element #{target_id} <- {type_value!r}")
        file_path: Optional[Path] = None
        if type_value and type_value.strip():
            candidate = Path(type_value.strip())
            if candidate.exists():
                file_path = candidate
                logger.info(f"[UPLOAD] Using path from type_value: {file_path}")
        if file_path is None and browser._upload_file and browser._upload_file.exists():
            file_path = browser._upload_file
            logger.info(f"[UPLOAD] Using pre-configured --upload-file: {file_path}")
        if file_path is None:
            logger.error(
                f"[UPLOAD] No valid file found. "
                f"Provide a real path via --upload-file or in type_value. "
                f"Got: {type_value!r}"
            )
            return None
        try:
            await browser._clear_som_overlays()
            file_target = await browser._find_file_input(target_id)
            if not file_target:
                raise RuntimeError(
                    f"No <input type='file'> found near element #{target_id}"
                )
            await file_target.handle.set_input_files(str(file_path.resolve()))
            # page_echo evidence: read back input.files to prove the file
            # actually landed on the control instead of trusting the call.
            page_echo = await file_target.handle.evaluate(
                """el => el && el.files
                    ? Array.from(el.files).map(f => ({name: f.name, size: f.size}))
                    : []"""
            )
            echo_names = [
                str(item.get("name") or "")
                for item in (page_echo or [])
                if isinstance(item, dict)
            ]
            if file_path.name not in echo_names:
                raise RuntimeError(
                    f"upload page_echo mismatch on element #{target_id}: "
                    f"expected file {file_path.name!r}, "
                    f"input.files reported {echo_names!r}"
                )
            _upload_xpath = await browser._get_xpath(file_target.handle)
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "upload",
                    "xpath": _upload_xpath or "",
                    "type_value": str(file_path.resolve()),
                    "uploaded_file": file_path.name,
                    "page_echo": page_echo or [],
                    "verified": True,
                })
            )
            logger.info(
                f"[UPLOAD] File injected successfully: {file_path.resolve()} "
                f"(page_echo={echo_names!r})"
            )
            print(
                f"\n\033[1;32m✅ 文件上传成功:\033[0m "
                f"\033[36m{file_path.resolve()}\033[0m\n"
            )
        except Exception as e:
            browser._last_action_error = e
            logger.error(f"[UPLOAD] Failed for element #{target_id}: {e}")

        await browser._wait_after_action(light_action=True)
        return None


@ActionRegistry.register("save_to_memory")
class SaveToMemoryHandler(ActionHandler):
    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        target_id = ctx.action.target_id
        type_value = ctx.action.type_value
        memory_key = ctx.action.memory_key or ""
        workflow_memory = ctx.workflow_memory

        effective_key = memory_key
        if not effective_key:
            effective_key = f"temp_var_{int(time.time())}"
            logger.warning(
                f"[MEMORY] save_to_memory: memory_key missing, "
                f"auto-generated key='{effective_key}'"
            )
            print(
                f"\033[1;33m⚠️  [MEMORY]\033[0m "
                f"VLM 未提供 memory_key，已动态生成临时命名为 '{effective_key}'"
            )

        logger.info(
            f"Executing save_to_memory: element #{target_id} → key={effective_key!r}"
        )

        try:
            extracted_text: str = ""

            if type_value and type_value.strip():
                extracted_text = type_value.strip()
                logger.info(
                    f"[MEMORY] Value sourced from type_value: {extracted_text!r}"
                )
                print(
                    f"\033[36m🧠 [MEMORY]\033[0m "
                    f"VLM 直接传递了文本值: {extracted_text!r}"
                )

            elif target_id and target_id != 0:
                await browser._clear_som_overlays()
                target = await browser._resolve_action_target(target_id, "click")
                if not target:
                    raise RuntimeError(
                        f"Element #{target_id} not found on active page or its iframes "
                    f"(页面在上一步后发生变化导致 SoM ID 失效。下一步建议："
                    f"(1) 用 click_text(type_value=「你 thought 里提到的可见文字」) 重试；"
                    f"(2) 先 wait(2) 让页面稳定，再重新观察截图取最新的 SoM ID。)"
                    )
                extracted_text = str(
                    await target.handle.evaluate(
                        """el => {
                            const text = (el.innerText || el.textContent || '').trim();
                            const val  = (el.value || '').trim();
                            return text || val || '';
                        }"""
                    )
                ).strip()
                logger.info(
                    f"[MEMORY] Value sourced from DOM element #{target_id}: "
                    f"{extracted_text!r}"
                )
                print(
                    f"\033[36m🔍 [MEMORY]\033[0m "
                    f"从页面元素提取了文本: {extracted_text!r}"
                )

            else:
                raise RuntimeError(
                    "save_to_memory 失败：VLM 既未提供 type_value，"
                    "也未提供有效的 target_id，无法获取要保存的值"
                )

            if not extracted_text:
                logger.warning(
                    f"[MEMORY] save_to_memory: extracted value is empty "
                    f"(element #{target_id})"
                )
            else:
                if workflow_memory is not None:
                    workflow_memory[effective_key] = extracted_text
                    workflow_memory["latest_memory"] = extracted_text
                logger.info(
                    f"[MEMORY] Saved: {effective_key!r} = {extracted_text!r} "
                    f"(latest_memory also updated)"
                )
                print(
                    f"\n\033[1;36m[MEMORY SAVED]\033[0m "
                    f"\033[33m{effective_key}\033[0m = "
                    f"\033[32m{extracted_text!r}\033[0m  "
                    f"\033[90m(latest_memory 已同步)\033[0m\n"
                )

        except Exception as e:
            browser._last_action_error = e
            logger.error(f"[MEMORY] save_to_memory failed: {e}")

        await browser._wait_after_action(light_action=True)
        return None


@ActionRegistry.register("chat_extract")
class ChatExtractHandler(ActionHandler):
    """Deterministic AI-chat answer extractor.

    Solves the failure mode where generic ``extract`` grabs search-result
    cards instead of the streaming AI answer on hybrid search-then-answer
    pages (yiyan.baidu.com, chat.baidu.com, etc.). Delegates to
    ``chat_answer_extractor.extract_chat_answer`` which:
      1. Polls until the answer-block text stabilises (streaming complete).
      2. Probes a curated selector cascade for known AI-answer containers.
      3. Falls back to the largest text block outside lists/nav/footer.

    Args from VSpiderAction:
      - ``type_value``: optional JSON ``{"timeout": 12, "min_length": 40}``;
        any plain string is accepted and ignored (logged at debug).
      - ``memory_key``: where to store the result; defaults to
        ``chat_answer_<ts>`` if missing.

    On success the answer text is written to:
      - ``workflow_memory[memory_key]`` = {"answer", "method", "url", ...}
      - ``workflow_memory["latest_memory"]`` = answer[:200]
      - the returned action result's ``extracted_data`` for VLM visibility.
    """

    _DEFAULT_TIMEOUT: ClassVar[float] = 25.0
    _DEFAULT_MIN_LEN: ClassVar[int] = 80

    # JS sweeping the whole document innerText, excluding nav/footer/search,
    # used as the absolute last-resort fallback when both selector cascade
    # AND largest-text heuristic come back empty.
    _WHOLE_PAGE_SWEEP_JS: ClassVar[str] = """
    (() => {
        const EXCLUDES = [
            'header', 'nav', 'footer', 'aside',
            'script', 'style', 'noscript', 'template',
            "[role='search']", "[role='navigation']", "[role='banner']", "[role='contentinfo']",
            "[class*='search-result']", "[class*='search_result']", "[class*='sresult']",
            "[class*='result-list']", "[class*='result_list']",
            "[class*='sidebar']", "[class*='side-bar']", "[class*='related']",
            "[class*='ad-']", "[class*='ads-']", "[id*='search-result']",
        ];
        // Clone the body and surgically strip excluded subtrees so innerText
        // reflects only the "content" zone.
        const clone = document.body ? document.body.cloneNode(true) : null;
        if (!clone) return { text: "", length: 0 };
        for (const sel of EXCLUDES) {
            try {
                clone.querySelectorAll(sel).forEach(n => n.parentNode && n.parentNode.removeChild(n));
            } catch (e) {}
        }
        const text = (clone.innerText || clone.textContent || "").trim();
        return { text: text, length: text.length };
    })()
    """

    @staticmethod
    def _parse_options(type_value: str) -> dict[str, Any]:
        """Accept either JSON or a bare URL/empty string. Never raises."""
        tv = (type_value or "").strip()
        if not tv:
            return {}
        if tv.startswith("{") and tv.endswith("}"):
            try:
                obj = json.loads(tv)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    async def _scroll_page_to_bottom(page: Any) -> None:
        """Trigger lazy-render of streaming chat content by scrolling the page
        and any inner scroll containers to the bottom. Some chat UIs only
        keep recent answer chunks alive in the DOM until the user is near
        the bottom; without this nudge, ``innerText`` is short."""
        try:
            await page.evaluate(
                """() => {
                    try { window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'}); } catch(e){}
                    const sc = document.querySelectorAll('[class*="scroll"], [class*="overflow"], main, article, [role="main"]');
                    for (const el of sc) {
                        try {
                            if (el.scrollHeight > el.clientHeight + 8) el.scrollTop = el.scrollHeight;
                        } catch(e){}
                    }
                }"""
            )
        except Exception as e:
            logger.debug("[chat_extract] pre-scroll failed: %s", e)

    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        action = ctx.action

        opts = self._parse_options(action.type_value or "")
        try:
            timeout = float(opts.get("timeout") or self._DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = self._DEFAULT_TIMEOUT
        try:
            min_length = int(opts.get("min_length") or self._DEFAULT_MIN_LEN)
        except (TypeError, ValueError):
            min_length = self._DEFAULT_MIN_LEN

        memory_key = (action.memory_key or "").strip()
        if not memory_key:
            memory_key = f"chat_answer_{int(time.time())}"
            logger.warning(
                "[chat_extract] memory_key missing — auto-generated %s",
                memory_key,
            )

        logger.info(
            "[chat_extract] start url=%s timeout=%.1fs min_len=%d key=%s",
            (page.url or "")[:90],
            timeout,
            min_length,
            memory_key,
        )
        print(
            f"\033[36m💬 [CHAT EXTRACT]\033[0m 等待 AI 回答流式完成 "
            f"(≤{timeout:.0f}s)，memory_key={memory_key!r}"
        )

        # Pre-scroll: nudge lazy-rendered streaming content into the DOM
        # BEFORE we start polling. This catches the common failure where the
        # answer is below the viewport and innerText only returns the first
        # paragraph.
        await self._scroll_page_to_bottom(page)
        try:
            await asyncio.sleep(0.4)
        except Exception:
            pass

        try:
            result = await extract_chat_answer(
                page,
                timeout=timeout,
                min_length=min_length,
            )
        except Exception as e:
            browser._last_action_error = e
            logger.error("[chat_extract] extractor crashed: %s", e)
            return None

        # ── Last-resort fallback: whole-page innerText sweep ───────────────
        # If the structured extractor came back empty/short on a chat-shaped
        # URL, do one final JS sweep — strip nav/footer/search-result from a
        # clone of <body>, then return whatever innerText is left. Better to
        # surface a 5-KB blob the VLM can summarise than a blank action.
        if (not result.get("ok")) or int(result.get("length") or 0) < min_length:
            try:
                sweep = await page.evaluate(self._WHOLE_PAGE_SWEEP_JS)
                sweep_text = clean_chat_answer_text(
                    str((sweep or {}).get("text") or "").strip()
                )
                if len(sweep_text) >= max(min_length, 120):
                    logger.info(
                        "[chat_extract] selector cascade missed — using whole-page sweep "
                        "(%d chars)", len(sweep_text),
                    )
                    result = {
                        "ok": True,
                        "answer": sweep_text[:8000],
                        "method": "fallback:page-innertext",
                        "selector": None,
                        "length": len(sweep_text),
                        "stable": True,
                        "wait_ms": int(result.get("wait_ms") or 0),
                        "polls": int(result.get("polls") or 0),
                        "url": page.url,
                        "title": (result.get("title") or ""),
                    }
            except Exception as e:
                logger.debug("[chat_extract] sweep fallback failed: %s", e)

        answer = str(result.get("answer") or "")
        ok = bool(result.get("ok"))
        method = str(result.get("method") or "none")

        workflow_memory = ctx.workflow_memory
        if workflow_memory is not None:
            workflow_memory[memory_key] = {
                "answer": answer,
                "method": method,
                "selector": result.get("selector"),
                "length": int(result.get("length") or 0),
                "stable": bool(result.get("stable")),
                "wait_ms": int(result.get("wait_ms") or 0),
                "url": result.get("url") or page.url,
                "title": result.get("title") or "",
            }
            if answer:
                workflow_memory["latest_memory"] = answer[:200]
                # Sentinel: tells the main loop "the chat task's answer is in
                # workflow_memory; you can stop". Without this main.py has no
                # way to know chat_extract is a TERMINAL action, and VLM gets
                # stuck re-emitting chat_extract every step (run_log_20260518_141728
                # ran 18 chat_extract iterations chasing the same answer).
                workflow_memory["__chat_extract_completed"] = {
                    "memory_key": memory_key,
                    "length": int(result.get("length") or 0),
                    "method": method,
                    "url": result.get("url") or page.url,
                }

        if ok:
            print(
                f"\033[32m✅ [CHAT EXTRACT]\033[0m method={method} "
                f"len={result.get('length')} wait={result.get('wait_ms')}ms"
            )
        else:
            err = result.get("error") or "no answer block matched selectors or fallback"
            logger.warning("[chat_extract] failed: %s", err)
            print(f"\033[33m⚠️  [CHAT EXTRACT]\033[0m 未匹配到回答块：{err}")

        # Mirror to action for downstream consumers / RPA trail / VLM history
        try:
            action.extracted_data = {
                "ok": ok,
                "answer": answer,
                "method": method,
                "memory_key": memory_key,
            }
        except Exception:
            pass

        try:
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "chat_extract",
                    "type_value": json.dumps(
                        {"timeout": timeout, "min_length": min_length},
                        ensure_ascii=False,
                    ),
                    "memory_key": memory_key,
                })
            )
        except Exception as _trail_err:
            logger.debug("[chat_extract] rpa_trail append failed: %s", _trail_err)

        return None


# ════════════════════════════════════════════════════════════════
#  Chat Submit —— 治"按 Enter 按不出去"的 chat 站点
# ════════════════════════════════════════════════════════════════

@ActionRegistry.register("chat_submit")
class ChatSubmitHandler(ActionHandler):
    """Deterministic click on the chat send button.

    Solves run_log_20260518_154220: yiyan.baidu.com 的发送按钮是 ``<div>``，
    SoM 漏标 + ``press_key Enter`` 不触发 → MAX_STEPS。

    流程：
      1. 调用 ``chat_send_locator.find_send_button(page)`` 在页面里启发式
         定位发送按钮（先 CSS 选择器级联，再 textbox-anchored 邻近度）。
      2. 找到则用 ``page.mouse.click(x, y)`` 在视口坐标点击（带 actionability）。
      3. 找不到则抛 ``ActionExecutionError``，附带"换 click_point 或 ask_human"
         的明确建议，让 VLM 自决下一步。

    ``type_value`` 接受可选 JSON ``{"wait_after_ms": 1500}`` 调节点击后等待。
    ``target_id`` 不需要（chat_submit 自带 locator）。
    """

    _DEFAULT_WAIT_AFTER_MS: ClassVar[int] = 1200

    @staticmethod
    def _parse_options(type_value: str) -> dict[str, Any]:
        tv = (type_value or "").strip()
        if not tv:
            return {}
        if tv.startswith("{") and tv.endswith("}"):
            try:
                obj = json.loads(tv)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        return {}

    async def execute(self, ctx: ActionContext) -> Optional["Page"]:
        browser = ctx.browser
        page = ctx.page
        action = ctx.action

        opts = self._parse_options(action.type_value or "")
        try:
            wait_after_ms = int(opts.get("wait_after_ms") or self._DEFAULT_WAIT_AFTER_MS)
        except (TypeError, ValueError):
            wait_after_ms = self._DEFAULT_WAIT_AFTER_MS

        # ── 1. 启发式定位发送按钮 ────────────────────────────────
        loc = await _chat_find_send_button(page)
        if not loc.get("found"):
            reason = str(loc.get("reason") or "no candidate matched")
            logger.warning("[chat_submit] locator failed: %s", reason)
            raise ActionExecutionError(
                f"chat_submit 未找到发送按钮：{reason}。\n"
                "建议下一步：\n"
                "  - 如果你能在截图上看到发送按钮（飞机/箭头图标），"
                "    用 click_point 配合归一化坐标点它；\n"
                "  - 或者输出 ask_human 让用户手动发送一次。"
            )

        method = str(loc.get("method") or "?")
        x, y = int(loc.get("x") or 0), int(loc.get("y") or 0)
        bbox = list(loc.get("bbox") or [0, 0, 0, 0])
        selector = loc.get("selector")

        # ── 2. 点击 ──────────────────────────────────────────────
        try:
            await page.mouse.move(x, y)
            await page.mouse.click(x, y)
            logger.info(
                "[chat_submit] clicked send button at (%d,%d) bbox=%s method=%s",
                x, y, bbox, method,
            )
            print(
                f"\033[1;35m🚀 [CHAT SUBMIT]\033[0m method={method} "
                f"click=({x},{y}) bbox={bbox} selector={selector}"
            )
        except Exception as e:
            logger.warning("[chat_submit] mouse.click failed: %s", e)
            raise ActionExecutionError(
                f"chat_submit 找到了按钮坐标 ({x},{y}) 但点击失败: {e}。"
                "考虑改用 click_point 或检查页面是否被遮挡。"
            ) from e

        # ── 3. 等待一段时间让 chat UI 开始响应 ──────────────────────
        try:
            await asyncio.sleep(max(0.0, wait_after_ms / 1000.0))
            await browser._wait_for_page_stable()
        except Exception as _w_err:
            logger.debug("[chat_submit] post-click wait skipped: %s", _w_err)

        # ── 4. RPA trail ────────────────────────────────────────
        try:
            browser.rpa_trail.append(
                ctx.with_rpa_meta({
                    "action": "chat_submit",
                    "type_value": json.dumps(
                        {"wait_after_ms": wait_after_ms},
                        ensure_ascii=False,
                    ),
                    "click_point_x": x,
                    "click_point_y": y,
                    "method": method,
                    "selector": selector,
                })
            )
        except Exception as _trail_err:
            logger.debug("[chat_submit] rpa_trail append failed: %s", _trail_err)

        return None


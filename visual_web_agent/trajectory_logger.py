"""HTML trajectory logger for VSpider runs."""

from __future__ import annotations

import html as _html
import json
from datetime import datetime
from pathlib import Path
from typing import Any


_TAIL = "\n</main>\n</body>\n</html>"

_ACTION_CSS: dict[str, str] = {
    "click": "click",
    "type": "type",
    "hover": "hover",
    "scroll": "scroll",
    "smooth_scroll": "scroll",
    "select": "select",
    "press_key": "press_key",
    "goto": "goto",
    "upload": "upload",
    "extract": "extract",
    "extract_link": "extract_link",
    "download_image": "download_image",
    "close_tab": "close_tab",
    "switch_tab": "switch_tab",
    "save_to_memory": "save_to_memory",
    "next_page": "goto",
    "click_text": "click",
    "done": "done",
    "ask_human": "ask_human",
    "error": "error",
    "captcha_detected": "captcha",
    "xhr_intercepted": "xhr",
}


def _e(value: Any) -> str:
    return _html.escape("" if value is None else str(value))


def _truncate(value: Any, limit: int = 140) -> str:
    text = "" if value is None else str(value)
    return text[:limit] + ("..." if len(text) > limit else "")


def _build_header(goal: str, start_time: str) -> str:
    sg = _e(goal)
    st = _e(start_time)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>VSpider Trajectory - {st}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#f3f4f6;color:#1f2937;font-size:14px;line-height:1.55}}
.page-header{{background:linear-gradient(135deg,#1e293b,#0f172a);color:white;padding:22px 36px;box-shadow:0 2px 10px rgba(0,0,0,.25)}}
.page-header h1{{font-size:20px;margin-bottom:6px}}
.page-header .meta{{font-family:Consolas,'SF Mono',monospace;font-size:12px;opacity:.72}}
main{{max-width:1500px;margin:24px auto;padding:0 24px 48px;display:flex;flex-direction:column;gap:14px}}
.step-card{{display:flex;background:white;border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(15,23,42,.08);border-left:4px solid transparent}}
.step-card.has-error{{border-left-color:#ef4444}}
.step-card.is-done{{border-left-color:#10b981}}
.step-card.is-xhr{{border-left-color:#6366f1}}
.card-img{{flex:0 0 42%;background:#111827;display:flex;align-items:center;justify-content:center;min-height:220px;max-height:520px;overflow:auto}}
.card-img img{{display:block;max-width:100%;height:auto}}
.no-img{{color:#94a3b8;text-align:center;padding:24px}}
.card-body{{flex:1;padding:18px 20px;display:flex;flex-direction:column;gap:12px;min-width:0}}
.step-badge{{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:50%;background:#334155;color:white;font-weight:700;font-size:13px}}
.step-badge.s-error{{background:#ef4444}}
.step-badge.s-done{{background:#10b981}}
.step-badge.s-captcha{{background:#f59e0b}}
.step-badge.s-human{{background:#8b5cf6}}
.step-badge.s-xhr{{background:#6366f1}}
.action-badge{{display:inline-block;padding:3px 11px;border-radius:999px;font-size:12px;font-weight:700}}
.a-click{{background:#dbeafe;color:#1d4ed8}}
.a-type{{background:#dcfce7;color:#166534}}
.a-hover{{background:#f3e8ff;color:#7e22ce}}
.a-scroll{{background:#e0e7ff;color:#3730a3}}
.a-select{{background:#fef9c3;color:#854d0e}}
.a-press_key{{background:#ccfbf1;color:#0f766e}}
.a-goto{{background:#e0f2fe;color:#0369a1}}
.a-upload{{background:#fff7ed;color:#9a3412}}
.a-extract{{background:#fef3c7;color:#92400e}}
.a-extract_link{{background:#fce7f3;color:#9d174d}}
.a-download_image{{background:#ede9fe;color:#6d28d9}}
.a-close_tab{{background:#fee2e2;color:#991b1b}}
.a-switch_tab{{background:#e0f2fe;color:#0369a1}}
.a-save_to_memory{{background:#dcfce7;color:#166534}}
.a-done{{background:#d1fae5;color:#065f46}}
.a-ask_human{{background:#ede9fe;color:#6d28d9}}
.a-error{{background:#fee2e2;color:#991b1b}}
.a-captcha{{background:#ffedd5;color:#9a3412}}
.a-xhr{{background:#e0e7ff;color:#3730a3}}
.a-default{{background:#e5e7eb;color:#374151}}
.thought{{padding:10px 12px;border-radius:8px;background:#f8fafc;border:1px solid #e2e8f0;white-space:pre-wrap}}
.params{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}}
.param{{border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;background:#fff}}
.lbl{{font-size:11px;color:#64748b;background:#f8fafc;padding:5px 8px;border-bottom:1px solid #e5e7eb}}
.val{{font-family:Consolas,'SF Mono',monospace;font-size:12px;padding:7px 8px;word-break:break-all}}
.memory,.err-block{{border-radius:8px;padding:10px 12px}}
.memory{{background:#f0fdf4;border:1px solid #bbf7d0}}
.err-block{{background:#fef2f2;border:1px solid #fecaca;color:#991b1b}}
.sec-title{{font-weight:700;margin-bottom:6px}}
.kv{{font-family:Consolas,'SF Mono',monospace;font-size:12px}}
.kv-key{{font-weight:700;color:#166534}}
@media(max-width:960px){{.step-card{{flex-direction:column}}.card-img{{flex:none;width:100%;max-height:280px}}}}
</style>
</head>
<body>
<header class="page-header">
  <h1>VSpider Trajectory</h1>
  <div class="meta">Generated: {st} | Goal: {sg}</div>
</header>
<main id="steps">"""


class HtmlLogger:
    """Append-only HTML trajectory logger."""

    def __init__(
        self,
        goal: str = "",
        log_dir: str | Path = "logs",
        run_id: str = "",
    ) -> None:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        ts = str(run_id or "").strip() or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = log_path / f"run_log_{ts}.html"
        header = _build_header(goal=goal, start_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write(header + _TAIL)
        print(f"\033[36m[HtmlLogger]\033[0m log file: {self.path.resolve()}")

    def _write_card(self, card_html: str) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
            if text.endswith(_TAIL):
                text = text[: -len(_TAIL)]
            text += card_html + _TAIL
            self.path.write_text(text, encoding="utf-8", newline="\n")
        except Exception as exc:
            print(f"\033[31m[HtmlLogger]\033[0m write failed: {exc}")

    def log_step(
        self,
        step_num: int,
        screenshot_path: str | None,
        action_dict: list[dict] | dict | None,
        error_msg: str | None = None,
        memory_state: dict | None = None,
    ) -> None:
        if isinstance(action_dict, list):
            batch_size = len(action_dict)
            primary = action_dict[0] if action_dict else {}
        elif isinstance(action_dict, dict):
            batch_size = 1
            primary = action_dict
        else:
            batch_size = 0
            primary = {}

        action = primary.get("action", "?")
        thought = primary.get("thought") or primary.get("progress_review") or ""
        target_id = primary.get("target_id")
        type_value = primary.get("type_value")
        memory_key = primary.get("memory_key")
        status = primary.get("status")
        reasoning_text_source = primary.get("reasoning_text_source")
        extract_text_source = primary.get("extract_text_source")
        extracted = primary.get("extracted_data")

        if error_msg:
            badge_cls = "s-error"
        elif action == "done":
            badge_cls = "s-done"
        elif action == "captcha_detected":
            badge_cls = "s-captcha"
        elif action == "ask_human":
            badge_cls = "s-human"
        elif action == "xhr_intercepted":
            badge_cls = "s-xhr"
        else:
            badge_cls = ""

        card_cls = "step-card"
        if error_msg:
            card_cls += " has-error"
        elif action == "done":
            card_cls += " is-done"
        elif action == "xhr_intercepted":
            card_cls += " is-xhr"

        if screenshot_path and Path(screenshot_path).exists():
            uri = Path(screenshot_path).resolve().as_uri()
            img_html = f'<a href="{uri}" target="_blank"><img src="{uri}" alt="Step {step_num}" loading="lazy"></a>'
        else:
            img_html = '<div class="no-img">No screenshot</div>'

        a_css = _ACTION_CSS.get(str(action), "default")
        header_row = (
            '<div style="display:flex;align-items:center;gap:10px;margin-bottom:0">'
            f'<span class="step-badge {badge_cls}">#{step_num}</span>'
            f'<span class="action-badge a-{a_css}">{_e(action)}</span>'
            '</div>'
        )

        batch_html = ""
        if batch_size > 1 and isinstance(action_dict, list):
            items = "".join(
                f'<span style="margin-right:8px;font-size:11px;"><b>{i + 1}.</b>&nbsp;'
                f'{_e(d.get("action", "?"))}'
                f'{" -> " + _e(d.get("target_id")) if d.get("target_id") else ""}</span>'
                for i, d in enumerate(action_dict)
            )
            batch_html = (
                '<div style="background:#f0f9ff;border:1px solid #bae6fd;'
                'border-radius:6px;padding:6px 10px;font-size:11px;color:#0369a1">'
                f'<b>Batch ({batch_size} actions):</b>&nbsp;{items}</div>'
            )

        params: list[tuple[str, str]] = []
        if target_id is not None:
            params.append(("target_id", str(target_id)))
        if type_value:
            params.append(("type_value", _truncate(type_value, 100)))
        if memory_key:
            params.append(("memory_key", str(memory_key)))
        if status:
            params.append(("status", str(status)))
        for extra_key in ("execution_method", "strategy", "landed_url"):
            extra_value = primary.get(extra_key)
            if extra_value:
                params.append((extra_key, _truncate(extra_value, 140)))
        if reasoning_text_source:
            params.append(("reasoning_text_source", str(reasoning_text_source)))
        if extract_text_source:
            params.append(("extract_text_source", str(extract_text_source)))
        if extracted is not None:
            try:
                ext_str = json.dumps(extracted, ensure_ascii=False, indent=None)
            except Exception:
                ext_str = str(extracted)
            params.append(("extracted_data", _truncate(ext_str, 140)))

        params_html = "".join(
            f'<div class="param"><div class="lbl">{_e(k)}</div><div class="val">{_e(v)}</div></div>'
            for k, v in params
        )
        params_block = f'<div class="params">{params_html}</div>' if params else ""

        memory_html = ""
        if memory_state:
            rows = "".join(
                f'<div><span class="kv-key">{_e(k)}</span>&nbsp;=&nbsp;{_e(v)}</div>'
                for k, v in memory_state.items()
            )
            memory_html = f'<div class="memory"><div class="sec-title">Memory</div><div class="kv">{rows}</div></div>'

        error_html = ""
        if error_msg:
            error_html = f'<div class="err-block"><div class="sec-title">Execution Error</div><div class="msg">{_e(error_msg)}</div></div>'

        thought_html = f'<div class="thought">{_e(thought)}</div>' if thought else ""

        card = (
            f'\n<div class="{card_cls}">'
            f'<div class="card-img">{img_html}</div>'
            '<div class="card-body">'
            f'{header_row}{batch_html}{thought_html}{params_block}{memory_html}{error_html}'
            '</div></div>'
        )
        self._write_card(card)

    def finalize(self) -> None:
        footer = (
            f'\n<div style="text-align:center;padding:28px 0 8px;color:#9ca3af;'
            f'font-size:12px;letter-spacing:.3px">Log complete - '
            f'{_e(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}</div>'
        )
        self._write_card(footer)
        print(f"\033[36m[HtmlLogger]\033[0m finalized: {self.path.resolve()}")

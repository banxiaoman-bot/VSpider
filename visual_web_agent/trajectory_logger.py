"""
VSpider 可视化执行日志生成器 (Trajectory Auditing)

将 Agent 每步的截图、VLM 思考、动作参数、记忆库状态和异常信息实时写入
一个精美的单文件 HTML 页面，方便复盘无人值守时的运行轨迹。

设计原则：
  - 崩溃安全：追加写 + 尾部覆写，任意时刻 HTML 均可在浏览器中正常打开。
  - 零依赖：只用标准库，不引入任何第三方包。
  - 自包含：图片通过 file:/// 绝对 URI 引用，拷贝到任何目录后图片仍可显示。

用法（被 main.py 调用，无需手动实例化）：
  html_logger = HtmlLogger(goal="查询订单")
  html_logger.log_step(step_num=1, screenshot_path="screenshots/step_01.png",
                       action_dict=decision, memory_state=workflow_memory)
  html_logger.finalize()
"""

import html as _html
import json
from datetime import datetime
from pathlib import Path


# ── 文件末尾标记：seek 时定位用，确保任意时刻文件是合法 HTML ─────────────
_TAIL = "\n</main>\n</body>\n</html>"

# ── action → CSS class 后缀映射 ──────────────────────────────────────────
_ACTION_CSS: dict[str, str] = {
    "click":            "click",
    "type":             "type",
    "hover":            "hover",
    "scroll":           "scroll",
    "select":           "select",
    "press_key":        "press_key",
    "goto":             "goto",
    "upload":           "upload",
    "extract":          "extract",
    "extract_link":     "extract_link",
    "download_image":   "download_image",
    "close_tab":        "close_tab",
    "save_to_memory":   "save_to_memory",
    "done":             "done",
    "ask_human":        "ask_human",
    "error":            "error",
    "captcha_detected": "captcha",
    "xhr_intercepted":  "xhr",
}


# ════════════════════════════════════════════════════════════════════════════
#  HTML 片段生成函数
# ════════════════════════════════════════════════════════════════════════════

def _build_header(goal: str, start_time: str) -> str:
    """返回 HTML 骨架 + 内联 CSS（不含 _TAIL，由 __init__ 在末尾拼接）。"""
    sg = _html.escape(goal)
    st = _html.escape(start_time)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>VSpider 轨迹 — {st}</title>
<style>
/* ── Reset ─────────────────────────────────────────────────── */
*{{box-sizing:border-box;margin:0;padding:0}}
body{{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,
              'Helvetica Neue',sans-serif;
  background:#f3f4f6;color:#1f2937;line-height:1.55;font-size:14px
}}

/* ── 顶部 Header ─────────────────────────────────────────────── */
.page-header{{
  background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);
  color:#fff;padding:22px 36px;
  box-shadow:0 2px 10px rgba(0,0,0,.3)
}}
.page-header h1{{font-size:19px;font-weight:700;letter-spacing:.4px}}
.page-header .meta{{
  font-size:12px;opacity:.55;margin-top:6px;
  font-family:'SF Mono',Consolas,monospace
}}

/* ── 主内容区 ─────────────────────────────────────────────────── */
main{{
  max-width:1500px;margin:24px auto;
  padding:0 24px 48px;
  display:flex;flex-direction:column;gap:14px
}}

/* ── 步骤卡片 ─────────────────────────────────────────────────── */
.step-card{{
  background:#fff;border-radius:12px;overflow:hidden;
  box-shadow:0 1px 3px rgba(0,0,0,.07),0 2px 8px rgba(0,0,0,.04);
  display:flex;transition:box-shadow .15s
}}
.step-card:hover{{box-shadow:0 4px 16px rgba(0,0,0,.10)}}
.step-card.has-error{{border-left:4px solid #ef4444}}
.step-card.is-done  {{border-left:4px solid #10b981}}
.step-card.is-xhr   {{border-left:4px solid #6366f1}}

/* ── 截图侧（左）─────────────────────────────────────────────── */
.card-img{{
  flex:0 0 430px;width:430px;
  background:#0f172a;
  display:flex;align-items:center;justify-content:center;
  padding:12px;min-height:180px
}}
.card-img a{{display:block;line-height:0}}
.card-img img{{
  max-width:406px;width:100%;height:auto;
  border-radius:6px;display:block;
  transition:opacity .2s;cursor:zoom-in
}}
.card-img img:hover{{opacity:.82}}
.card-img .no-img{{
  color:#475569;font-size:12px;
  text-align:center;padding:28px 20px;line-height:1.9
}}

/* ── 信息侧（右）─────────────────────────────────────────────── */
.card-body{{
  flex:1;padding:18px 22px;
  display:flex;flex-direction:column;gap:10px;
  min-width:0;overflow:hidden
}}

/* ── 步骤序号圆圈 ─────────────────────────────────────────────── */
.step-badge{{
  display:inline-flex;align-items:center;justify-content:center;
  width:30px;height:30px;border-radius:50%;
  font-weight:800;font-size:12px;color:#fff;
  flex-shrink:0;background:#3b82f6
}}
.step-badge.s-done    {{background:#10b981}}
.step-badge.s-error   {{background:#ef4444}}
.step-badge.s-captcha {{background:#f59e0b}}
.step-badge.s-human   {{background:#8b5cf6}}
.step-badge.s-xhr     {{background:#6366f1}}

/* ── Action 徽标 ─────────────────────────────────────────────── */
.action-badge{{
  display:inline-block;padding:3px 11px;
  border-radius:20px;font-size:11px;font-weight:700;
  letter-spacing:.5px;text-transform:uppercase;
  font-family:'SF Mono',Consolas,monospace
}}
/* 每种 action 一套配色 */
.a-click         {{background:#dbeafe;color:#1d4ed8}}
.a-type          {{background:#d1fae5;color:#065f46}}
.a-hover         {{background:#fae8ff;color:#7e22ce}}
.a-scroll        {{background:#e0e7ff;color:#3730a3}}
.a-select        {{background:#fef9c3;color:#854d0e}}
.a-press_key     {{background:#f0fdf4;color:#166534}}
.a-goto          {{background:#e0f2fe;color:#0369a1}}
.a-upload        {{background:#fff7ed;color:#9a3412}}
.a-extract       {{background:#fef3c7;color:#92400e}}
.a-extract_link  {{background:#fce7f3;color:#9d174d}}
.a-download_image{{background:#ede9fe;color:#6d28d9}}
.a-close_tab     {{background:#f1f5f9;color:#475569}}
.a-save_to_memory{{background:#cffafe;color:#0e7490}}
.a-done          {{background:#d1fae5;color:#065f46}}
.a-ask_human     {{background:#ede9fe;color:#6d28d9}}
.a-error         {{background:#fee2e2;color:#b91c1c}}
.a-captcha       {{background:#fef3c7;color:#92400e}}
.a-xhr           {{background:#e0e7ff;color:#4338ca}}
.a-default       {{background:#f3f4f6;color:#374151}}

/* ── 思考过程 ─────────────────────────────────────────────────── */
.thought{{
  background:#f8fafc;border-left:3px solid #3b82f6;
  padding:9px 13px;border-radius:0 6px 6px 0;
  font-size:13px;color:#374151;
  white-space:pre-wrap;word-break:break-word;line-height:1.65
}}

/* ── 参数网格 ─────────────────────────────────────────────────── */
.params{{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(155px,1fr));
  gap:7px
}}
.param{{background:#f8fafc;border-radius:6px;padding:6px 10px}}
.param .lbl{{
  font-size:10px;font-weight:700;
  text-transform:uppercase;color:#9ca3af;letter-spacing:.4px
}}
.param .val{{
  font-size:12px;color:#1f2937;margin-top:2px;
  word-break:break-all;
  font-family:'SF Mono',Consolas,monospace
}}

/* ── 记忆库 ──────────────────────────────────────────────────── */
.memory{{
  background:#f0fdf4;border:1px solid #bbf7d0;
  border-radius:8px;padding:9px 13px
}}
.sec-title{{
  font-size:10px;font-weight:700;
  text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px
}}
.memory .sec-title{{color:#15803d}}
.memory .kv{{
  font-size:12px;color:#166534;
  font-family:'SF Mono',Consolas,monospace;line-height:1.9
}}
.memory .kv .kv-key{{font-weight:700}}

/* ── 错误块 ──────────────────────────────────────────────────── */
.err-block{{
  background:#fef2f2;border:1px solid #fecaca;border-radius:8px;
  padding:9px 13px
}}
.err-block .sec-title{{color:#dc2626}}
.err-block .msg{{
  font-size:12px;color:#7f1d1d;
  font-family:'SF Mono',Consolas,monospace;
  white-space:pre-wrap;word-break:break-all;line-height:1.7
}}

/* ── 响应式折叠 ──────────────────────────────────────────────── */
@media(max-width:960px){{
  .step-card{{flex-direction:column}}
  .card-img{{flex:none;width:100%;max-height:280px;overflow:hidden}}
  .card-img img{{max-width:100%}}
}}
</style>
</head>
<body>
<header class="page-header">
  <h1>🕷 VSpider 执行轨迹</h1>
  <div class="meta">生成时间：{st}　｜　目标：{sg}</div>
</header>
<main id="steps">"""


# ════════════════════════════════════════════════════════════════════════════
#  HtmlLogger 类
# ════════════════════════════════════════════════════════════════════════════

class HtmlLogger:
    """
    实时写入 HTML 轨迹日志。

    文件在任意时刻均是合法 HTML（尾部覆写策略），
    即使进程崩溃，已记录的步骤仍可在浏览器中打开查看。
    """

    def __init__(self, goal: str = "", log_dir: str | Path = "logs") -> None:
        """
        在 log_dir 目录下创建带时间戳的 HTML 文件并写入骨架。

        Args:
            goal:    任务目标描述（显示在页面顶部）
            log_dir: 日志目录，默认为 logs/（相对于工作目录）
        """
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path: Path = log_path / f"run_log_{ts}.html"

        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = _build_header(goal=goal, start_time=start_time)

        # 初始写入骨架 + 收尾标记（使文件从创建起就是合法 HTML）
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(header + _TAIL)

        print(f"\033[36m[HtmlLogger]\033[0m 轨迹日志已创建: {self.path.resolve()}")

    # ── 内部工具 ────────────────────────────────────────────────────────────

    def _write_card(self, card_html: str) -> None:
        """
        将一段 HTML 卡片追加到日志文件。

        策略：在文件末尾定位 _TAIL 标记 → 截断 → 写入新卡片 + 新 _TAIL。
        这样文件始终以合法的 </main></body></html> 结尾。
        若因意外找不到标记则直接追加（宽松容错）。
        """
        try:
            tail_bytes = _TAIL.encode("utf-8")
            payload = (card_html + _TAIL).encode("utf-8")

            with open(self.path, "r+b") as f:
                content = f.read()
                tail_pos = content.rfind(tail_bytes)
                if tail_pos == -1:
                    f.seek(0, 2)          # 找不到标记：追加到末尾
                else:
                    f.seek(tail_pos)
                    f.truncate()          # 截掉旧的 _TAIL
                f.write(payload)          # 写新卡片 + 新 _TAIL
        except Exception as exc:
            # 日志系统自身不能让主流程崩溃
            print(f"\033[33m[HtmlLogger] write_card failed: {exc}\033[0m")

    @staticmethod
    def _e(text: object) -> str:
        """HTML 转义辅助，任意类型均安全转换。"""
        return _html.escape(str(text) if text is not None else "")

    # ── 公开接口 ────────────────────────────────────────────────────────────

    def log_step(
        self,
        step_num: int,
        screenshot_path: str | None,
        action_dict: "list[dict] | dict | None",
        error_msg: str | None = None,
        memory_state: dict | None = None,
    ) -> None:
        """
        将本步骤的完整状态记录为一张 HTML 卡片并追加到日志文件。

        Args:
            step_num:        当前步骤编号
            screenshot_path: 截图文件路径（None 或不存在时显示占位符）
            action_dict:     VLM 返回的决策（list[dict] 批次、单个 dict、或 None）
            error_msg:       本步执行异常信息（None 表示无错误）
            memory_state:    当前 workflow_memory 快照（None 表示记忆库为空）
        """
        e = self._e  # 短别名

        # ── 规范化 action_dict：统一转为单个 dict（取批次第一个动作）────
        _batch_size = 0
        if isinstance(action_dict, list):
            _batch_size = len(action_dict)
            _primary = action_dict[0] if action_dict else {}
        elif isinstance(action_dict, dict):
            _primary = action_dict
        else:
            _primary = {}

        # ── 从主动作中提取字段 ───────────────────────────────────────────
        action     = _primary.get("action")     or "—"
        thought    = _primary.get("thought")    or ""
        target_id  = _primary.get("target_id")
        type_value = _primary.get("type_value") or ""
        memory_key = _primary.get("memory_key") or ""
        status     = _primary.get("status")     or ""
        extracted  = _primary.get("extracted_data")

        # ── 步骤圆圈样式 ─────────────────────────────────────────────────
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

        # ── 卡片外框样式 ─────────────────────────────────────────────────
        card_cls = "step-card"
        if error_msg:
            card_cls += " has-error"
        elif action == "done":
            card_cls += " is-done"
        elif action == "xhr_intercepted":
            card_cls += " is-xhr"

        # ── 截图区 ───────────────────────────────────────────────────────
        if screenshot_path and Path(screenshot_path).exists():
            uri = Path(screenshot_path).resolve().as_uri()
            img_html = (
                f'<a href="{uri}" target="_blank" title="点击新标签页查看原图">'
                f'<img src="{uri}" alt="Step {step_num}" loading="lazy">'
                f'</a>'
            )
        else:
            img_html = (
                '<div class="no-img">'
                '📷&nbsp;截图不可用<br>'
                '<small style="opacity:.6">（本步未截图或文件不存在）</small>'
                '</div>'
            )

        # ── Action 徽标 ──────────────────────────────────────────────────
        a_css = _ACTION_CSS.get(action, "default")
        action_badge = (
            f'<span class="action-badge a-{a_css}">{e(action)}</span>'
        )

        # ── 步骤头部行（序号 + 徽标）────────────────────────────────────
        header_row = (
            f'<div style="display:flex;align-items:center;gap:10px;'
            f'margin-bottom:0">'
            f'<span class="step-badge {badge_cls}">#{step_num}</span>'
            f'{action_badge}'
            f'</div>'
        )

        # ── 思考过程 ─────────────────────────────────────────────────────
        thought_html = (
            f'<div class="thought">{e(thought)}</div>'
            if thought else ""
        )

        # ── 批次总览（连招模式，仅当 batch_size > 1 时显示）──────────────
        batch_html = ""
        if _batch_size > 1 and isinstance(action_dict, list):
            _items = "".join(
                f'<span style="margin-right:8px;font-size:11px;">'
                f'<b>{i + 1}.</b>&nbsp;{e(d.get("action", "?"))}'
                f'{"→" + e(str(d.get("target_id", ""))) if d.get("target_id") else ""}'
                f'</span>'
                for i, d in enumerate(action_dict)
            )
            batch_html = (
                f'<div style="background:#f0f9ff;border:1px solid #bae6fd;'
                f'border-radius:6px;padding:6px 10px;font-size:11px;color:#0369a1">'
                f'<b>🚀 连招批次（{_batch_size} 个动作）：</b>&nbsp;{_items}'
                f'</div>'
            )

        # ── 参数网格 ─────────────────────────────────────────────────────
        params: list[tuple[str, str]] = []
        if target_id is not None:
            params.append(("target_id", str(target_id)))
        if type_value:
            tv_display = type_value[:100] + ("…" if len(type_value) > 100 else "")
            params.append(("type_value", tv_display))
        if memory_key:
            params.append(("memory_key", memory_key))
        if status:
            params.append(("status", status))
        if extracted is not None:
            try:
                ext_str = json.dumps(extracted, ensure_ascii=False, indent=None)
            except Exception:
                ext_str = str(extracted)
            params.append(("extracted_data",
                            ext_str[:140] + ("…" if len(ext_str) > 140 else "")))

        params_html = "".join(
            f'<div class="param">'
            f'<div class="lbl">{e(k)}</div>'
            f'<div class="val">{e(v)}</div>'
            f'</div>'
            for k, v in params
        )
        params_block = (
            f'<div class="params">{params_html}</div>' if params else ""
        )

        # ── 记忆库块 ─────────────────────────────────────────────────────
        memory_html = ""
        if memory_state:
            kv_rows = "".join(
                f'<div>'
                f'<span class="kv-key">{e(k)}</span>'
                f'&nbsp;=&nbsp;{e(v)}'
                f'</div>'
                for k, v in memory_state.items()
            )
            memory_html = (
                f'<div class="memory">'
                f'<div class="sec-title">🧠 当前记忆库</div>'
                f'<div class="kv">{kv_rows}</div>'
                f'</div>'
            )

        # ── 错误块 ───────────────────────────────────────────────────────
        error_html = ""
        if error_msg:
            error_html = (
                f'<div class="err-block">'
                f'<div class="sec-title">❌ 执行异常</div>'
                f'<div class="msg">{e(error_msg)}</div>'
                f'</div>'
            )

        # ── 组装完整卡片 ─────────────────────────────────────────────────
        card = (
            f'\n<div class="{card_cls}">'
            f'<div class="card-img">{img_html}</div>'
            f'<div class="card-body">'
            f'{header_row}'
            f'{batch_html}'
            f'{thought_html}'
            f'{params_block}'
            f'{memory_html}'
            f'{error_html}'
            f'</div>'
            f'</div>'
        )

        self._write_card(card)

    def finalize(self) -> None:
        """
        写入收尾汇总行并打印日志路径。

        非必须调用——进程崩溃时已写入的步骤仍可在浏览器打开。
        正常退出时调用可在页面底部显示完成时间。
        """
        finish_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        footer = (
            f'\n<div style="text-align:center;padding:28px 0 8px;'
            f'color:#9ca3af;font-size:12px;letter-spacing:.3px">'
            f'✅&nbsp;日志记录完毕 · {_html.escape(finish_time)}'
            f'</div>'
        )
        self._write_card(footer)
        print(f"\033[36m[HtmlLogger]\033[0m 轨迹日志已保存: {self.path.resolve()}")

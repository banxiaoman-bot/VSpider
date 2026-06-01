"""E1: scan logs/run_log_*.html for failure / loop patterns and rank.

Usage
-----
    python scripts/scan_failed_runs.py [--limit N] [--out summary.md]

The scanner is intentionally simple:
  * Each run log is HTML containing the full agent trace.
  * Outcome is parsed from sentinel markers (``[DONE] ...`` / ``[FAIL]`` / ``[ERROR]``).
  * Loop / dead-end patterns are detected by sliding-window action repeats
    and a fixed list of ``[GUARD]`` / ``[LOOP]`` / ``[ASK_HUMAN]`` markers.

Output is a markdown report sorted by frequency of each pattern, with a
representative example (filename + first-occurrence snippet) for each.
"""

from __future__ import annotations

import argparse
import collections
import html
import json
import re
import sys
from pathlib import Path
from statistics import median

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


# ── Outcome markers (greppable strings written by main.py / _broadcast_log) ─
_OK_MARKERS = (
    "[DONE] Task completed",
    "任务执行完成",
    "✅ 任务已完成",
)
_FAIL_MARKERS = (
    "[FAIL]",
    "[ERROR] Agent exception",
    "Agent crashed",
    "TimeoutError",
    "MaxRetriesExceeded",
)


# ── Pattern categories (ranked) ─────────────────────────────────────────
_PATTERN_RULES: list[tuple[str, re.Pattern[str], str]] = [
    # (label, regex on the post-strip text, short hint)
    # Use precise sentinels — these correspond to actual log lines emitted
    # by main.py / actions.py, not user-facing words that appear in goals.
    ("loop_guard_triggered",  re.compile(r"\[LOOP GUARD\] (?:dead loop|killed|cooldown)"), "URL-aware loop guard fired"),
    ("execution_error_click", re.compile(r"Execution Error action=click target_id=\d+: Element #\d+ not found"), "click target SoM ID dropped out of next snapshot"),
    ("execution_error_other", re.compile(r"Execution Error action=(?!click)\w+ target_id=\d+"), "non-click action raised ActionExecutionError"),
    ("element_not_found",     re.compile(r"Element #\d+ not found on active page"), "Generic 'element #N not found' (any action)"),
    ("ax_resolve_fail",       re.compile(r"\[AX RESOLVE\] (?:failed|gave up|exhausted)"), "AX tree couldn't disambiguate target"),
    ("ask_human_pending",     re.compile(r"\[HITL\] (?:Agent 已挂起|waiting for human)"), "Agent asked for human intervention"),
    ("captcha_detected",      re.compile(r"\[CAPTCHA\] captcha[ _]?detected", re.I), "CAPTCHA encountered"),
    ("nav_timeout",           re.compile(r"(?:goto|wait_for_load_state|wait_for_url).{0,40}Timeout", re.I), "Navigation/wait timed out"),
    ("vlm_invalid_json",      re.compile(r"VLM .{0,40}invalid JSON|JSONDecodeError|Pydantic.*ValidationError"), "VLM returned malformed JSON"),
    ("login_wall",            re.compile(r"未登录|登录后查看|请先登录|please sign in|login required", re.I), "Hit a login wall mid-task"),
    ("form_set_unverified",   re.compile(r"\[FORM[_ ]VERIFY\] .{0,80}failed", re.I), "form_set wrote a value but verification failed"),
    ("extract_zero_rows",     re.compile(r"(?:extracted 0 rows|抓取 0 条|提取条数: ?0|extracted_data \[\])"), "Extraction returned no rows"),
    ("infinite_scroll_stuck", re.compile(r"smooth_scroll.*?no new content|懒加载.*?没有新增", re.I), "Scroll didn't load more content"),
    ("max_steps_hit",         re.compile(r"达到最大步数|超出最大步数|max[_ ]?steps reached", re.I), "Ran out of step budget"),
    ("xhr_giveup",            re.compile(r"XHR (?:截胡|intercept).{0,30}未命中|XHR pattern .{0,30}not matched", re.I), "XHR interceptor didn't hit any payload"),
    ("page_close_unexpected", re.compile(r"Target page, context or browser has been closed|page (?:was )?closed unexpectedly", re.I), "Underlying page died"),
    ("done_status_error",     re.compile(r"status\s+error\b"), "Any step that ended with status=error"),
    ("done_status_success",   re.compile(r"status\s+success\b"), "Any step that ended with status=success (info)"),
]


# ── L-scanner: phase event jsonl reading ────────────────────────────────


_RUN_ID_RX = re.compile(r"run_log_(\d{8}_\d{6})\.html$")


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile (matches numpy default). Empty → 0."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    if lo == hi:
        return float(s[lo])
    return float(s[lo] + (s[hi] - s[lo]) * (k - lo))


def parse_phase_jsonl(path: Path) -> dict[str, dict]:
    """Read ``logs/phase_<run_id>.jsonl`` and return per-phase aggregates.

    Returns a dict keyed by phase name with stats:
        ``count`` / ``warn`` / ``error`` / ``avg_ms`` / ``p50_ms`` /
        ``p95_ms`` / ``max_ms``.

    Rows without ``duration_ms`` (e.g. ``finalize``, ``guard``) are still
    counted but excluded from the timing percentiles.
    Robust against partial / malformed lines: bad rows are skipped silently.
    """
    if not path.exists():
        return {}
    by_phase: dict[str, list[dict]] = collections.defaultdict(list)
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                phase = str(row.get("phase", "unknown")) or "unknown"
                by_phase[phase].append(row)
    except Exception:
        return {}

    stats: dict[str, dict] = {}
    for phase, rows in by_phase.items():
        durations = [
            float(r["duration_ms"]) for r in rows
            if isinstance(r.get("duration_ms"), (int, float))
        ]
        warn_n = sum(1 for r in rows if r.get("severity") == "warn")
        err_n = sum(1 for r in rows if r.get("severity") == "error")
        stats[phase] = {
            "count": len(rows),
            "warn": warn_n,
            "error": err_n,
            "avg_ms": (sum(durations) / len(durations)) if durations else None,
            "p50_ms": median(durations) if durations else None,
            "p95_ms": _percentile(durations, 95) if durations else None,
            "max_ms": max(durations) if durations else None,
        }
    return stats


def parse_run(path: Path) -> dict:
    """Return ``{filename, outcome, length, patterns:[label,...]}``."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"file": path.name, "outcome": "unreadable", "patterns": [], "err": str(e)}

    # Strip <style> / <script> blocks first — their CSS class selectors
    # like ``.a-download_image`` and color names polluted the regex.
    body = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.S | re.I)
    body = re.sub(r"<script[^>]*>.*?</script>", " ", body, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"\s+", " ", text)

    # Per-step badge counts. MUST anchor on ``class="`` (the HTML attribute
    # form) because the <style> block defines selectors like
    # ``.step-badge.s-captcha`` and ``.step-card.has-error`` that would
    # otherwise produce one false positive per file.
    err_steps     = len(re.findall(r'class="step-card[^"]*has-error', raw))
    done_steps    = len(re.findall(r'class="step-card[^"]*is-done', raw))
    captcha_steps = len(re.findall(r'class="step-badge[^"]*s-captcha', raw))
    human_steps   = len(re.findall(r'class="step-badge[^"]*s-human', raw))
    total_steps   = len(re.findall(r'class="step-card\b', raw))

    # Outcome: look at the LAST step-card's class to decide ok/fail/unfinished.
    last_card = re.findall(r'class="step-card[^"]*"', raw)
    last_class = last_card[-1] if last_card else ""
    if "is-done" in last_class:
        outcome = "ok"
    elif "has-error" in last_class:
        outcome = "fail"
    elif total_steps == 0:
        outcome = "empty"
    else:
        outcome = "unfinished"  # ran but never emitted done/error sentinel

    patterns = []
    for label, rx, _hint in _PATTERN_RULES:
        m = rx.search(text)
        if m:
            patterns.append((label, m.start()))

    # L-scanner: pair the html log with its phase event jsonl by run_id.
    phase_stats: dict[str, dict] = {}
    m_rid = _RUN_ID_RX.search(path.name)
    if m_rid:
        phase_path = path.parent / f"phase_{m_rid.group(1)}.jsonl"
        phase_stats = parse_phase_jsonl(phase_path)

    return {
        "file": path.name,
        "outcome": outcome,
        "total_steps": total_steps,
        "err_steps": err_steps,
        "done_steps": done_steps,
        "captcha_steps": captcha_steps,
        "human_steps": human_steps,
        "patterns": patterns,
        "phase_stats": phase_stats,
        "size": len(raw),
        "_text": text,  # for snippet extraction
    }


def first_snippet(text: str, around: int, window: int = 200) -> str:
    lo = max(0, around - window)
    hi = min(len(text), around + window)
    snippet = text[lo:hi]
    snippet = snippet.replace("\n", " ").strip()
    return f"...{snippet}..."


def build_report(limit: int) -> str:
    files = sorted(LOGS_DIR.glob("run_log_*.html"), reverse=True)[:limit]
    if not files:
        return f"No run_log_*.html files in {LOGS_DIR}."

    runs = [parse_run(p) for p in files]
    by_outcome = collections.Counter(r["outcome"] for r in runs)
    pattern_counts: collections.Counter[str] = collections.Counter()
    pattern_examples: dict[str, tuple[str, str]] = {}  # label → (filename, snippet)
    for r in runs:
        for label, pos in r["patterns"]:
            pattern_counts[label] += 1
            if label not in pattern_examples:
                pattern_examples[label] = (
                    r["file"], first_snippet(r["_text"], pos),
                )

    lines: list[str] = []
    lines.append(f"# Run log scan — last {len(files)} runs\n")
    lines.append("## Outcomes (by LAST step-card class)")
    for k in ("ok", "fail", "unfinished", "empty", "unknown", "unreadable"):
        if by_outcome.get(k):
            lines.append(f"- **{k}**: {by_outcome[k]}")
    # Aggregate step stats
    tot_steps = sum(r.get("total_steps", 0) for r in runs)
    tot_err   = sum(r.get("err_steps", 0) for r in runs)
    tot_done  = sum(r.get("done_steps", 0) for r in runs)
    tot_capt  = sum(r.get("captcha_steps", 0) for r in runs)
    tot_human = sum(r.get("human_steps", 0) for r in runs)
    err_rate = (tot_err / tot_steps * 100) if tot_steps else 0.0
    lines.append("")
    lines.append("## Step-level stats")
    lines.append(f"- total steps: **{tot_steps}** "
                 f"(err {tot_err} = {err_rate:.1f}%, done {tot_done}, "
                 f"captcha {tot_capt}, human {tot_human})")
    lines.append("")

    # ── L-scanner: aggregate per-phase stats across all runs in window ──
    # Each run contributes its own per-phase counts and durations; we
    # combine them into one table that lets you see "across the last N runs,
    # how often does som_inject fire and how slow is its p95?"
    phase_runs_with_data = sum(1 for r in runs if r.get("phase_stats"))
    if phase_runs_with_data:
        agg_count: collections.Counter[str] = collections.Counter()
        agg_warn: collections.Counter[str] = collections.Counter()
        agg_error: collections.Counter[str] = collections.Counter()
        agg_durs: dict[str, list[float]] = collections.defaultdict(list)
        for r in runs:
            ps = r.get("phase_stats") or {}
            for phase, st in ps.items():
                agg_count[phase] += int(st.get("count", 0))
                agg_warn[phase] += int(st.get("warn", 0))
                agg_error[phase] += int(st.get("error", 0))
                # Reconstruct durations from per-run aggregates is lossy.
                # Re-read jsonl files to get accurate p95/avg across runs.
        # Re-walk for accurate cross-run percentiles
        for r in runs:
            m_rid = _RUN_ID_RX.search(r["file"])
            if not m_rid:
                continue
            jp = LOGS_DIR / f"phase_{m_rid.group(1)}.jsonl"
            if not jp.exists():
                continue
            try:
                with jp.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        d = row.get("duration_ms")
                        if isinstance(d, (int, float)):
                            agg_durs[str(row.get("phase") or "unknown")].append(float(d))
            except Exception:
                continue

        lines.append(
            f"## Phase timings (from {phase_runs_with_data} runs with phase jsonl)"
        )
        lines.append(
            "| phase | count | warn | error | avg ms | p50 ms | p95 ms | max ms |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        # Sort by count desc so the noisy phases sit at the top
        for phase in sorted(agg_count, key=lambda k: -agg_count[k]):
            durs = agg_durs.get(phase, [])
            if durs:
                avg = sum(durs) / len(durs)
                p50 = median(durs)
                p95 = _percentile(durs, 95)
                mx = max(durs)
                lines.append(
                    f"| `{phase}` | {agg_count[phase]} | {agg_warn[phase]} | "
                    f"{agg_error[phase]} | {avg:.0f} | {p50:.0f} | "
                    f"{p95:.0f} | {mx:.0f} |"
                )
            else:
                lines.append(
                    f"| `{phase}` | {agg_count[phase]} | {agg_warn[phase]} | "
                    f"{agg_error[phase]} | — | — | — | — |"
                )
        lines.append("")

    lines.append("## Patterns (ranked by frequency)")
    if not pattern_counts:
        lines.append("_No known patterns matched._")
    else:
        hint_map = {label: hint for label, _, hint in _PATTERN_RULES}
        for label, n in pattern_counts.most_common():
            ex_file, ex_snip = pattern_examples[label]
            lines.append(f"### `{label}` × {n}")
            lines.append(f"_{hint_map[label]}_")
            lines.append(f"- Example: `{ex_file}`")
            lines.append(f"- Snippet: {ex_snip[:380]}")
            lines.append("")

    lines.append("## Failed runs (most recent first)")
    fail_runs = [r for r in runs if r["outcome"] == "fail"]
    if not fail_runs:
        lines.append("_No failed runs in this window._")
    else:
        for r in fail_runs[:15]:
            tags = ", ".join(label for label, _ in r["patterns"]) or "—"
            lines.append(f"- `{r['file']}` — patterns: {tags}")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=80,
                    help="how many most-recent runs to scan (default 80)")
    ap.add_argument("--out", type=str, default="",
                    help="write report to file (default: stdout)")
    args = ap.parse_args()

    report = build_report(args.limit)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"[E1] wrote {len(report)} chars to {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

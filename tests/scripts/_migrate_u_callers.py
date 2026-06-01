"""U: byte-level migration to thread ``notice_severity=...`` into every
``broadcast_phase(...)`` call that already has a BrowserEnv in scope.

Sites covered (and why):
  • main.py vlm_call site   — ``browser`` is in scope (used 5 lines above)
  • main.py guard site      — ``browser`` is in scope
  • main.py action site     — ``browser`` is in scope
  • browser_env.py som_inject site — ``self`` IS the BrowserEnv

The finalize phase event in main.py:_broadcast_done_safe is intentionally
NOT touched: that function has no browser argument and finalize is a
whole-run summary, not tied to a specific notice.

Each replacement uses a unique multi-line anchor that includes both the
phase name and a nearby distinctive string (e.g. ``_action_t0``), so it
matches exactly once. Idempotent: each site checks for the sentinel
``notice_severity=`` substring inside its anchor block and skips if
already migrated.

Run: ``python tests/scripts/_migrate_u_callers.py``
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "visual_web_agent" / "main.py"
BENV = ROOT / "visual_web_agent" / "browser_env.py"


def _detect_eol(raw: bytes) -> str:
    return "\r\n" if b"\r\n" in raw else "\n"


def _patch(
    path: Path,
    sites: list[tuple[str, str, str]],
    label: str,
) -> int:
    """Apply ``(name, old_substring, new_substring)`` tuples to ``path``.

    ``old_substring`` / ``new_substring`` use ``\n``; this function
    converts both to the file's actual EOL flavor before matching.
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    eol = _detect_eol(raw)

    changes = 0
    for name, old_lf, new_lf in sites:
        old = old_lf.replace("\n", eol)
        new = new_lf.replace("\n", eol)
        if old not in text:
            if "notice_severity=" in new and "notice_severity=" in text:
                # Already migrated — check whether THIS specific anchor's
                # new form is present (best-effort sanity).
                if new in text:
                    print(f"[skip] {label}: {name} already migrated")
                    continue
            raise SystemExit(
                f"FATAL: {label}: anchor for {name!r} not found. "
                f"Inspect file or re-run after upstream changes."
            )
        text = text.replace(old, new, 1)
        changes += 1
        print(f"[ok ] {label}: {name}")

    if changes:
        path.write_bytes(text.encode("utf-8"))
    return changes


# ── main.py sites ──────────────────────────────────────────────────────
MAIN_SITES = [
    (
        "vlm_call",
        # old
        "                        broadcast_phase(\n"
        "                            \"vlm_call\",\n"
        "                            severity=\"info\",\n"
        "                            message=f\"step {step}\",\n"
        "                            step=step,\n"
        "                            duration_ms=int((time.time() - _vlm_t0) * 1000),\n"
        "                        )\n",
        # new
        "                        broadcast_phase(\n"
        "                            \"vlm_call\",\n"
        "                            severity=\"info\",\n"
        "                            message=f\"step {step}\",\n"
        "                            step=step,\n"
        "                            duration_ms=int((time.time() - _vlm_t0) * 1000),\n"
        "                            notice_severity=getattr(browser, \"_last_notice_severity\", None),\n"
        "                        )\n",
    ),
    (
        "guard",
        "                                    _bp_guard(\n"
        "                                        \"guard\",\n"
        "                                        severity=\"warn\",\n"
        "                                        message=f\"session_drop: {_orig_sd_action} → ask_human\",\n"
        "                                        step=step,\n"
        "                                        extra={\n"
        "                                            \"guard\": \"session_drop\",\n"
        "                                            \"orig_action\": _orig_sd_action,\n"
        "                                            \"return_url\": _sd_signal.return_url[:200],\n"
        "                                        },\n"
        "                                    )\n",
        "                                    _bp_guard(\n"
        "                                        \"guard\",\n"
        "                                        severity=\"warn\",\n"
        "                                        message=f\"session_drop: {_orig_sd_action} → ask_human\",\n"
        "                                        step=step,\n"
        "                                        notice_severity=getattr(browser, \"_last_notice_severity\", None),\n"
        "                                        extra={\n"
        "                                            \"guard\": \"session_drop\",\n"
        "                                            \"orig_action\": _orig_sd_action,\n"
        "                                            \"return_url\": _sd_signal.return_url[:200],\n"
        "                                        },\n"
        "                                    )\n",
    ),
    (
        "action",
        "                            broadcast_phase(\n"
        "                                \"action\",\n"
        "                                severity=\"error\" if _act_err else \"info\",\n"
        "                                message=action or \"(no-op)\",\n"
        "                                step=step,\n"
        "                                duration_ms=int((time.time() - _action_t0) * 1000),\n"
        "                                extra={\"action_name\": action or \"\"},\n"
        "                            )\n",
        "                            broadcast_phase(\n"
        "                                \"action\",\n"
        "                                severity=\"error\" if _act_err else \"info\",\n"
        "                                message=action or \"(no-op)\",\n"
        "                                step=step,\n"
        "                                duration_ms=int((time.time() - _action_t0) * 1000),\n"
        "                                notice_severity=getattr(browser, \"_last_notice_severity\", None),\n"
        "                                extra={\"action_name\": action or \"\"},\n"
        "                            )\n",
    ),
]


# ── browser_env.py site ────────────────────────────────────────────────
# Need to view exact text first; written here against the snippet we saw.
BENV_SITES = [
    (
        "som_inject",
        "            broadcast_phase(\n"
        "                \"som_inject\",\n"
        "                severity=\"warn\" if _som_heavy else \"info\",\n"
        "                message=f\"{total_elements} elements / {injected_frames} frames\",\n",
        "            broadcast_phase(\n"
        "                \"som_inject\",\n"
        "                severity=\"warn\" if _som_heavy else \"info\",\n"
        "                message=f\"{total_elements} elements / {injected_frames} frames\",\n"
        "                notice_severity=self._last_notice_severity,\n",
    ),
]


def main() -> None:
    n1 = _patch(MAIN, MAIN_SITES, "main.py")
    n2 = _patch(BENV, BENV_SITES, "browser_env.py")
    print(f"Done. Total replacements: {n1 + n2}")


if __name__ == "__main__":
    main()

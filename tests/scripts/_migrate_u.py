"""U: byte-level migration to add ``notice_severity`` to broadcast_phase.

Why a script: ``api_server.py`` is CRLF-encoded; the chat ``edit`` tool
matches raw bytes and was rejecting LF-only ``old_string`` blocks.

This script:
  1. Adds ``notice_severity: str | None = None,`` to the broadcast_phase
     signature, right after ``duration_ms: int | None = None,``.
  2. Extends the docstring's "保留字段" line to mention notice_severity.
  3. Inserts validation + payload[notice_severity] = ... right before the
     ``if extra:`` block.

Idempotent: running twice is a no-op (each insertion checks for its own
sentinel substring).

Run: ``python tests/scripts/_migrate_u.py``
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "api_server.py"


def main() -> None:
    raw = TARGET.read_bytes()
    text = raw.decode("utf-8")

    # Detect EOL once so re-inserted blocks match the file's flavor.
    eol = "\r\n" if b"\r\n" in raw else "\n"

    changes = 0

    # ── 1) signature: add notice_severity param ────────────────────
    sig_old = (
        f"    duration_ms: int | None = None,{eol}"
        f"    extra: dict[str, Any] | None = None,{eol}"
    )
    sig_new = (
        f"    duration_ms: int | None = None,{eol}"
        f"    notice_severity: str | None = None,{eol}"
        f"    extra: dict[str, Any] | None = None,{eol}"
    )
    if sig_old not in text:
        raise SystemExit("FATAL: signature anchor not found (already migrated?)")
    if "notice_severity: str | None = None," in text:
        print("[skip] signature already has notice_severity")
    else:
        text = text.replace(sig_old, sig_new, 1)
        changes += 1
        print("[ok ] signature: notice_severity added")

    # ── 2) docstring: extend 保留字段 line ─────────────────────────
    doc_old = (
        f"    保留字段：``type``, ``phase``, ``severity``, ``message``, "
        f"``step``, ``duration_ms``, ``ts``。{eol}"
    )
    doc_new = (
        f"    保留字段：``type``, ``phase``, ``severity``, ``message``, "
        f"``step``, ``duration_ms``, ``notice_severity``, ``ts``。{eol}"
        f"{eol}"
        f"    U: ``notice_severity`` carries the BrowserEnv "
        f"``_last_notice_severity`` snapshot at emit time. It reflects "
        f"the agent-visible notice the next VLM step will read (set via "
        f"``set_tab_notice``). The frontend Timeline uses "
        f"``max(severity, notice_severity)`` to color chips so a "
        f"technically-successful action that raised a *warn* notice "
        f"still appears amber instead of green. Callers pass "
        f"``notice_severity=browser._last_notice_severity`` when a "
        f"BrowserEnv is in scope; ``None`` means \"field omitted from "
        f"payload\" (back-compat).{eol}"
    )
    if doc_old not in text:
        if "``notice_severity``" in text:
            print("[skip] docstring already mentions notice_severity")
        else:
            raise SystemExit("FATAL: docstring anchor not found")
    else:
        text = text.replace(doc_old, doc_new, 1)
        changes += 1
        print("[ok ] docstring: notice_severity documented")

    # ── 3) payload assembly: add validation + payload write ────────
    body_old = (
        f"    if duration_ms is not None:{eol}"
        f"        payload[\"duration_ms\"] = int(duration_ms){eol}"
        f"    if extra:{eol}"
    )
    body_new = (
        f"    if duration_ms is not None:{eol}"
        f"        payload[\"duration_ms\"] = int(duration_ms){eol}"
        f"    # U: only include notice_severity when caller actually passed{eol}"
        f"    # one (None = \"no browser context\" — keep payload lean).{eol}"
        f"    if notice_severity is not None:{eol}"
        f"        if notice_severity not in (\"info\", \"warn\", \"error\"):{eol}"
        f"            notice_severity = \"info\"{eol}"
        f"        payload[\"notice_severity\"] = notice_severity{eol}"
        f"    if extra:{eol}"
    )
    if body_old not in text:
        if "payload[\"notice_severity\"]" in text:
            print("[skip] payload write already present")
        else:
            raise SystemExit("FATAL: payload anchor not found")
    else:
        text = text.replace(body_old, body_new, 1)
        changes += 1
        print("[ok ] payload: notice_severity write added")

    if changes == 0:
        print("No changes (already migrated).")
        return

    TARGET.write_bytes(text.encode("utf-8"))
    print(f"Wrote {TARGET} ({changes} changes).")


if __name__ == "__main__":
    main()

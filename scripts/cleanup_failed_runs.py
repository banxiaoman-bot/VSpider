"""K5: prune ``runs/failed/<run_id>.json`` archive entries by retention policy.

Designed to be safe-to-cron:

  * Best-effort I/O (a locked file does not abort the rest of the pass)
  * ``--dry-run`` previews EXACTLY what would be deleted, no disk writes
  * Exit code is 0 on a clean run, 2 on any per-file error so cron can
    surface trouble via its usual non-zero-exit hooks

Examples
--------
    # Keep only the 20 newest records, drop the rest unconditionally
    python scripts/cleanup_failed_runs.py --keep-last 20

    # Drop anything older than 30 days
    python scripts/cleanup_failed_runs.py --older-than 30d

    # Combine: keep at least 20 records AND keep anything < 30d old.
    # A record dies only if BOTH guards say so.
    python scripts/cleanup_failed_runs.py --keep-last 20 --older-than 30d

    # Also delete the referenced HTML log + phase/event JSONL files
    python scripts/cleanup_failed_runs.py --keep-last 20 --purge-related

    # Preview without touching disk
    python scripts/cleanup_failed_runs.py --keep-last 5 --dry-run

Duration tokens accepted by ``--older-than``: ``Ns`` / ``Nm`` / ``Nh`` /
``Nd`` / ``Nw`` (seconds / minutes / hours / days / weeks).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Resolve the project root so ``visual_web_agent`` is importable when
# the user runs this script directly from a checkout (`python scripts/...`).
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from visual_web_agent import failure_archive as fa  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cleanup_failed_runs.py",
        description=(
            "Prune runs/failed/<run_id>.json by retention policy. "
            "Combine --keep-last and --older-than to express "
            "'keep at least N AND keep anything newer than D'."
        ),
    )
    p.add_argument(
        "--keep-last",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Keep only the N newest records (by archive ts). "
            "Combine with --older-than for AND semantics."
        ),
    )
    p.add_argument(
        "--older-than",
        type=str,
        default=None,
        metavar="DURATION",
        help=(
            "Delete records older than this duration. "
            "Accepts Ns / Nm / Nh / Nd / Nw (e.g. 30d, 12h, 2w)."
        ),
    )
    p.add_argument(
        "--purge-related",
        action="store_true",
        help=(
            "Also delete the referenced HTML log + phase/event JSONL files "
            "for each pruned record. Use with care — these files are useful "
            "for post-mortem inspection."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without touching disk.",
    )
    p.add_argument(
        "--base-dir",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Override the runs/ root. Default: <project_root>/runs. "
            "Tests use this; cron jobs usually don't."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the cleanup summary as JSON to stdout instead of text.",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print one line per processed record.",
    )
    return p


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> float | None:
    """Resolve --older-than into seconds + range-check --keep-last.

    Returns the parsed older_than_s (or None when not provided).
    Calls parser.error() on bad input — that exits with code 2.
    """
    if args.keep_last is None and args.older_than is None:
        parser.error(
            "must specify at least one of --keep-last / --older-than "
            "(no policy = nothing to prune)"
        )
    if args.keep_last is not None and args.keep_last < 0:
        parser.error(f"--keep-last must be >= 0, got {args.keep_last}")

    older_than_s: float | None = None
    if args.older_than is not None:
        try:
            older_than_s = fa.parse_duration(args.older_than)
        except ValueError as exc:
            parser.error(f"--older-than: {exc}")
    return older_than_s


def _print_text_summary(summary: dict, *, verbose: bool) -> None:
    """Render the summary dict as a human-friendly stdout report."""
    mode = "DRY-RUN" if summary.get("dry_run") else "APPLIED"
    print(f"[cleanup_failed_runs] {mode}")
    print(f"  scanned: {summary.get('scanned', 0)}")
    print(f"  deleted: {summary.get('deleted', 0)}")
    print(f"  kept:    {summary.get('kept', 0)}")
    errs = summary.get("errors") or []
    if errs:
        print(f"  errors:  {len(errs)}")
        # Show the first few so cron mails surface them.
        for err in errs[:5]:
            print(f"    - {err}")
        if len(errs) > 5:
            print(f"    ... ({len(errs) - 5} more)")
    if verbose:
        details = summary.get("details") or []
        if not details:
            print("  (no records selected for deletion)")
        else:
            print("  details:")
            for d in details:
                rid = d.get("run_id") or "<unknown>"
                marker = "[would-delete]" if d.get("would_delete") else (
                    "[deleted]" if d.get("archive") else "[error]"
                )
                related = d.get("related") or {}
                if related:
                    rel_str = ", ".join(
                        f"{k}={'y' if v else 'n'}" for k, v in related.items()
                    )
                    print(f"    {marker} {rid}  ({rel_str})")
                else:
                    print(f"    {marker} {rid}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    older_than_s = _validate_args(args, parser)

    summary = fa.cleanup_failed_runs(
        keep_last=args.keep_last,
        older_than_s=older_than_s,
        purge_related=args.purge_related,
        dry_run=args.dry_run,
        base_dir=args.base_dir,
    )

    if args.json:
        # Keep the JSON shape stable so downstream automation can rely
        # on the same keys exposed by failure_archive.cleanup_failed_runs.
        json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_text_summary(summary, verbose=args.verbose)

    # Exit 2 if any per-file error was reported. Cron treats anything
    # non-zero as a failure and will mail the operator.
    return 2 if (summary.get("errors") or []) else 0


if __name__ == "__main__":
    raise SystemExit(main())

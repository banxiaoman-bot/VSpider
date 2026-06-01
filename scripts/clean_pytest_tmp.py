from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


_DEFAULT_PATTERNS = (".tmp_pytest_validate_*",)
_EXTRA_PATTERNS = (".tmp_phase_tests", ".tmp_k5")


def discover_pytest_tmp_dirs(root: Path, *, include_extra: bool = True) -> list[Path]:
    root = Path(root).resolve()
    patterns = _DEFAULT_PATTERNS + (_EXTRA_PATTERNS if include_extra else ())
    found: dict[Path, None] = {}
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.is_dir() and path.parent == root:
                found[path] = None
    return sorted(found.keys(), key=lambda item: item.name)


def clean_pytest_tmp_dirs(root: Path, *, apply: bool = False, include_extra: bool = True) -> dict:
    root = Path(root).resolve()
    targets = discover_pytest_tmp_dirs(root, include_extra=include_extra)
    deleted: list[str] = []
    errors: list[dict[str, str]] = []
    for path in targets:
        if not apply:
            continue
        try:
            shutil.rmtree(path)
            deleted.append(str(path))
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return {
        "root": str(root),
        "apply": bool(apply),
        "scanned": len(targets),
        "deleted": len(deleted),
        "targets": [str(path) for path in targets],
        "errors": errors,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clean_pytest_tmp.py")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--default-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _print_text(summary: dict) -> None:
    mode = "APPLY" if summary.get("apply") else "DRY-RUN"
    print(f"[clean_pytest_tmp] {mode}")
    print(f"  root:    {summary.get('root')}")
    print(f"  scanned: {summary.get('scanned', 0)}")
    print(f"  deleted: {summary.get('deleted', 0)}")
    targets = summary.get("targets") or []
    for target in targets:
        print(f"  - {target}")
    errors = summary.get("errors") or []
    if errors:
        print(f"  errors: {len(errors)}")
        for item in errors[:5]:
            print(f"  ! {item.get('path')}: {item.get('error')}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    summary = clean_pytest_tmp_dirs(
        Path(args.root),
        apply=bool(args.apply),
        include_extra=not bool(args.default_only),
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_text(summary)
    return 2 if summary.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET_TESTS = ("tests/test_capability_router.py", "tests/test_timeline_replay_search.py")
DEFAULT_CORE_TESTS = (
    "tests/test_browser_control.py",
    "tests/test_browser_pool.py",
    "tests/test_capability_router.py",
    "tests/test_timeline_replay_search.py",
)


def _slug(text: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in str(text)).strip("_") or "validation"


def build_validation_commands(
    milestone: str,
    *,
    target_tests: list[str] | None = None,
    core_tests: list[str] | None = None,
    include_target: bool = True,
    include_build: bool = True,
    include_core: bool = True,
    include_full: bool = True,
) -> list[dict[str, str]]:
    slug = _slug(milestone)
    commands: list[dict[str, str]] = []
    if include_target:
        tests = target_tests or list(DEFAULT_TARGET_TESTS)
        commands.append({
            "name": "target",
            "cwd": str(ROOT),
            "command": " ".join([
                sys.executable,
                "-m",
                "pytest",
                *tests,
                "-q",
                "--basetemp",
                f".tmp_pytest_validate_{slug}_target",
            ]),
        })
    if include_build:
        commands.append({
            "name": "frontend_build",
            "cwd": str(ROOT / "vspider-ui"),
            "command": "npm run build",
        })
    if include_core:
        tests = core_tests or list(DEFAULT_CORE_TESTS)
        commands.append({
            "name": "core",
            "cwd": str(ROOT),
            "command": " ".join([
                sys.executable,
                "-m",
                "pytest",
                *tests,
                "-q",
                "--basetemp",
                f".tmp_pytest_validate_{slug}_core",
            ]),
        })
    if include_full:
        commands.append({
            "name": "full",
            "cwd": str(ROOT),
            "command": " ".join([
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "-q",
                "--basetemp",
                f".tmp_pytest_validate_{slug}_full",
            ]),
        })
    return commands


def run_validation(commands: list[dict[str, str]], *, dry_run: bool = False) -> dict:
    steps: list[dict] = []
    for item in commands:
        step = dict(item)
        if dry_run:
            step.update({"returncode": None, "skipped": True})
            steps.append(step)
            continue
        result = subprocess.run(
            item["command"],
            cwd=item["cwd"],
            shell=True,
            text=True,
        )
        step["returncode"] = result.returncode
        steps.append(step)
        if result.returncode != 0:
            return {"status": "failed", "failed_step": item["name"], "steps": steps}
    return {"status": "success", "failed_step": "", "steps": steps}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="validate_y.py")
    parser.add_argument("milestone")
    parser.add_argument("--target-test", action="append", default=[])
    parser.add_argument("--core-test", action="append", default=[])
    parser.add_argument("--skip-target", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-core", action="store_true")
    parser.add_argument("--skip-full", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _print_text(summary: dict) -> None:
    print(f"[validate_y] {summary.get('status')}")
    for step in summary.get("steps") or []:
        code = step.get("returncode")
        marker = "dry-run" if step.get("skipped") else str(code)
        print(f"  - {step.get('name')}: {marker}")
        print(f"    cwd: {step.get('cwd')}")
        print(f"    cmd: {step.get('command')}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    commands = build_validation_commands(
        args.milestone,
        target_tests=args.target_test or None,
        core_tests=args.core_test or None,
        include_target=not args.skip_target,
        include_build=not args.skip_build,
        include_core=not args.skip_core,
        include_full=not args.skip_full,
    )
    summary = run_validation(commands, dry_run=bool(args.dry_run))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_text(summary)
    return 0 if summary.get("status") == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Command line helpers for inspecting VSpider runtime skills."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .authoring import main as authoring_main
from .lint import main as lint_main
from .replay import main as replay_main
from .registry import build_default_skill_registry
from ..extraction_engine.strategies import infer_goal_strategy_context


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_skill_table(skills: list[dict[str, object]], *, show_score: bool = False) -> None:
    if not skills:
        print("No skills registered.")
        return
    score_header = "score  " if show_score else ""
    print(f"{'name':<32} {'capability':<16} {'action':<32} {score_header}rows fields")
    for skill in skills:
        fields = ",".join(str(item) for item in skill.get("required_fields") or [])
        score_text = f"{float(skill.get('score') or 0):<6.2f}" if show_score else ""
        print(
            f"{str(skill.get('name') or ''):<32} "
            f"{str(skill.get('capability') or ''):<16} "
            f"{str(skill.get('action') or ''):<32} "
            f"{score_text} "
            f"{skill.get('expected_rows')!s:<4} {fields}"
        )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "scaffold":
        return authoring_main(raw_argv[1:])
    if raw_argv and raw_argv[0] == "lint":
        return lint_main(raw_argv[1:])

    parser = argparse.ArgumentParser(
        prog="python -m visual_web_agent.skills",
        description="Inspect VSpider runtime skills.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list registered skills")
    list_parser.add_argument("--json", action="store_true", help="emit JSON")
    list_parser.add_argument("--no-history", action="store_true", help="skip report history")

    match_parser = subparsers.add_parser("match", help="show skills matching a URL and goal")
    match_parser.add_argument("--url", required=True, help="current page URL")
    match_parser.add_argument("--goal", required=True, help="user goal")
    match_parser.add_argument("--json", action="store_true", help="emit JSON")
    match_parser.add_argument("--no-history", action="store_true", help="skip report history")

    replay_parser = subparsers.add_parser("replay", help="inspect skill replay snapshots")
    replay_parser.add_argument("replay_args", nargs=argparse.REMAINDER)

    scaffold_parser = subparsers.add_parser("scaffold", help="scaffold a runtime skill")
    scaffold_parser.add_argument("scaffold_args", nargs=argparse.REMAINDER)

    lint_parser = subparsers.add_parser("lint", help="lint registered runtime skills")
    lint_parser.add_argument("lint_args", nargs=argparse.REMAINDER)

    args = parser.parse_args(raw_argv)

    if args.command == "replay":
        replay_args = list(args.replay_args or [])
        if not replay_args:
            replay_args = ["list"]
        return replay_main(replay_args)

    if args.command == "scaffold":
        return authoring_main(list(args.scaffold_args or []))

    if args.command == "lint":
        return lint_main(list(args.lint_args or []))

    registry = build_default_skill_registry(load_history=not args.no_history)

    if args.command == "list":
        skills = registry.list_skills()
        if args.json:
            _print_json({"count": len(skills), "skills": skills})
        else:
            _print_skill_table(skills)
        return 0

    if args.command == "match":
        strategy_context = infer_goal_strategy_context(args.goal, url=args.url)
        matches = registry.ranked_matches(
            url=args.url,
            goal=args.goal,
            strategy_context=strategy_context,
        )
        payload = {
            "url": args.url,
            "goal": args.goal,
            "strategy_context": strategy_context,
            "matched": bool(matches),
            "first_match": matches[0].skill.name if matches else "",
            "matches": [
                {
                    **match.as_dict(),
                    "metadata": match.skill.dispatch_metadata(url=args.url, goal=args.goal),
                }
                for match in matches
            ],
        }
        if args.json:
            _print_json(payload)
        else:
            if not matches:
                print("No matching skills.")
                return 1
            print(f"First match: {payload['first_match']}")
            _print_skill_table(payload["matches"], show_score=True)  # type: ignore[arg-type]
            for match in payload["matches"]:
                print(f"  {match['name']} reasons={match.get('reasons') or []}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

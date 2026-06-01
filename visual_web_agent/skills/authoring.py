"""Authoring helpers for VSpider runtime skills."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SKILL_DIR = ROOT / "visual_web_agent" / "skills" / "interaction"
DEFAULT_TEST_DIR = ROOT / "tests"
DEFAULT_REGISTRY_PATH = ROOT / "visual_web_agent" / "skills" / "registry.py"


@dataclass(frozen=True)
class ScaffoldResult:
    skill_path: Path
    test_path: Path | None
    class_name: str
    skill_name: str
    registry_import: str
    registry_entry: str
    registry_path: Path | None = None
    registered: bool = False
    registry_changed: bool = False

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "skill_path": str(self.skill_path),
            "test_path": str(self.test_path or ""),
            "class_name": self.class_name,
            "skill_name": self.skill_name,
            "registry_import": self.registry_import,
            "registry_entry": self.registry_entry,
            "registry_path": str(self.registry_path or ""),
            "registered": self.registered,
            "registry_changed": self.registry_changed,
        }


def normalize_skill_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "").strip()).strip("_").lower()
    if not name:
        raise ValueError("skill name is required")
    if name[0].isdigit():
        name = f"skill_{name}"
    return name


def class_name_for_skill(skill_name: str) -> str:
    return "".join(part.capitalize() for part in normalize_skill_name(skill_name).split("_")) + "Skill"


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _tuple_literal(items: list[str]) -> str:
    if not items:
        return "()"
    if len(items) == 1:
        return f"({items[0]!r},)"
    return "(" + ", ".join(repr(item) for item in items) + ")"


def _regex_literal(items: list[str]) -> str:
    if not items:
        return r"(?!)"
    return "|".join(re.escape(item) for item in items)


def render_skill_template(
    *,
    skill_name: str,
    class_name: str,
    capability: str,
    aliases: list[str],
    url_patterns: list[str],
    goal_patterns: list[str],
    required_fields: list[str],
    expected_rows: int,
) -> str:
    source = f"{skill_name.upper()}_MACRO"
    action = f"{skill_name}_macro"
    first_field = required_fields[0] if required_fields else "value"
    return f'''"""Runtime skill scaffold for {skill_name}."""

from __future__ import annotations

import re
from typing import Any

from ..base import AgentSkill, SkillResult, SkillVerification


class {class_name}(AgentSkill):
    name = "{skill_name}"
    action = "{action}"
    source = "{source}"
    capability = "{capability}"
    aliases = {_tuple_literal(aliases)}
    required_fields = {_tuple_literal(required_fields)}
    expected_rows = {int(expected_rows)}

    def match(self, *, url: str, goal: str) -> bool:
        url_text = str(url or "")
        goal_text = str(goal or "")
        return (
            bool(re.search(r"{_regex_literal(url_patterns)}", url_text, re.I))
            and bool(re.search(r"{_regex_literal(goal_patterns)}", goal_text, re.I))
        )

    def dispatch_metadata(self, *, url: str, goal: str) -> dict[str, Any]:
        return {{
            "url": url,
            "aliases": list(self.aliases),
        }}

    async def run(self, browser: Any, goal: str) -> SkillResult:
        page = await browser._ensure_active_page(reason="{skill_name} skill")
        rows: list[dict[str, Any]] = []
        if page:
            # TODO: implement deterministic browser workflow here.
            # Keep returned rows stable and replay-friendly.
            rows.append({{
                "{first_field}": "",
                "url": getattr(page, "url", ""),
            }})
        return SkillResult(
            source=self.source,
            rows=rows,
            expected_rows=self.expected_rows,
            required_fields=list(self.required_fields),
            metadata=self.dispatch_metadata(url=getattr(browser, "current_url", ""), goal=goal),
            verifications=[
                SkillVerification(
                    name=f"{{self.source}}_implemented",
                    success=bool(rows and str(rows[0].get("{first_field}") or "").strip()),
                    expected="non-empty {first_field}",
                    observed=rows[0].get("{first_field}", "") if rows else "",
                )
            ],
        )
'''


def render_test_template(
    *,
    module_name: str,
    class_name: str,
    skill_name: str,
    url_example: str,
    goal_example: str,
) -> str:
    return f'''from visual_web_agent.skills.interaction.{module_name} import {class_name}


def test_{skill_name}_matches_expected_surface() -> None:
    skill = {class_name}()

    assert skill.match(
        url={url_example!r},
        goal={goal_example!r},
    )
    assert skill.name == {skill_name!r}
'''


def register_skill_in_registry(
    *,
    registry_path: Path | str = DEFAULT_REGISTRY_PATH,
    import_line: str,
    entry_line: str,
) -> bool:
    path = Path(registry_path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    changed = False

    if import_line not in lines:
        import_indexes = [
            index for index, line in enumerate(lines)
            if line.startswith("from .interaction.")
        ]
        if not import_indexes:
            raise ValueError(f"could not find interaction imports in {path}")
        lines.insert(import_indexes[-1] + 1, import_line)
        changed = True

    if not any(line.strip() == entry_line for line in lines):
        insert_index = next(
            (
                index for index, line in enumerate(lines)
                if line.strip().startswith("], history=")
            ),
            None,
        )
        if insert_index is None:
            raise ValueError(f"could not find SkillRegistry list in {path}")
        lines.insert(insert_index, f"        {entry_line}")
        changed = True

    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def scaffold_skill(
    *,
    name: str,
    capability: str = "interaction",
    aliases: list[str] | None = None,
    url_patterns: list[str] | None = None,
    goal_patterns: list[str] | None = None,
    required_fields: list[str] | None = None,
    expected_rows: int = 1,
    skill_dir: Path | str = DEFAULT_SKILL_DIR,
    test_dir: Path | str = DEFAULT_TEST_DIR,
    registry_path: Path | str = DEFAULT_REGISTRY_PATH,
    with_test: bool = True,
    register: bool = False,
    force: bool = False,
) -> ScaffoldResult:
    skill_name = normalize_skill_name(name)
    class_name = class_name_for_skill(skill_name)
    aliases = aliases or []
    url_patterns = url_patterns or aliases[:1] or [skill_name]
    goal_patterns = goal_patterns or aliases[1:] or [skill_name]
    required_fields = required_fields or ["value"]
    skill_path = Path(skill_dir) / f"{skill_name}.py"
    test_path = Path(test_dir) / f"test_{skill_name}_skill.py" if with_test else None
    for path in [skill_path, test_path]:
        if path and path.exists() and not force:
            raise FileExistsError(f"{path} already exists; pass --force to overwrite")
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(
        render_skill_template(
            skill_name=skill_name,
            class_name=class_name,
            capability=capability,
            aliases=aliases,
            url_patterns=url_patterns,
            goal_patterns=goal_patterns,
            required_fields=required_fields,
            expected_rows=expected_rows,
        ),
        encoding="utf-8",
    )
    if test_path:
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(
            render_test_template(
                module_name=skill_name,
                class_name=class_name,
                skill_name=skill_name,
                url_example=url_patterns[0],
                goal_example=goal_patterns[0],
            ),
            encoding="utf-8",
        )
    result = ScaffoldResult(
        skill_path=skill_path,
        test_path=test_path,
        class_name=class_name,
        skill_name=skill_name,
        registry_import=f"from .interaction.{skill_name} import {class_name}",
        registry_entry=f"{class_name}(),",
    )
    if register:
        changed = register_skill_in_registry(
            registry_path=registry_path,
            import_line=result.registry_import,
            entry_line=result.registry_entry,
        )
        result = replace(
            result,
            registry_path=Path(registry_path),
            registered=True,
            registry_changed=changed,
        )
    return result


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scaffold a VSpider runtime skill.")
    parser.add_argument("--name", required=True, help="snake_case or title skill name")
    parser.add_argument("--capability", default="interaction")
    parser.add_argument("--aliases", default="", help="comma-separated aliases")
    parser.add_argument("--url-patterns", default="", help="comma-separated URL text fragments")
    parser.add_argument("--goal-patterns", default="", help="comma-separated goal text fragments")
    parser.add_argument("--fields", default="value", help="comma-separated required fields")
    parser.add_argument("--expected-rows", type=int, default=1)
    parser.add_argument("--skill-dir", default=str(DEFAULT_SKILL_DIR))
    parser.add_argument("--test-dir", default=str(DEFAULT_TEST_DIR))
    parser.add_argument("--registry-path", default=str(DEFAULT_REGISTRY_PATH))
    parser.add_argument("--register", action="store_true", help="add import and instance to the skill registry")
    parser.add_argument("--no-test", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = scaffold_skill(
        name=args.name,
        capability=args.capability,
        aliases=parse_csv(args.aliases),
        url_patterns=parse_csv(args.url_patterns),
        goal_patterns=parse_csv(args.goal_patterns),
        required_fields=parse_csv(args.fields),
        expected_rows=args.expected_rows,
        skill_dir=args.skill_dir,
        test_dir=args.test_dir,
        registry_path=args.registry_path,
        with_test=not args.no_test,
        register=args.register,
        force=args.force,
    )
    if args.json:
        _print_json(result.as_dict())
    else:
        print(f"Created skill: {result.skill_path}")
        if result.test_path:
            print(f"Created test:  {result.test_path}")
        if result.registered:
            changed = "updated" if result.registry_changed else "already up to date"
            print(f"Registry:      {result.registry_path} ({changed})")
        else:
            print("Registry import:")
            print(f"  {result.registry_import}")
            print("Registry entry:")
            print(f"  {result.registry_entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

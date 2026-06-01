"""Quality checks for VSpider runtime skills."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
from typing import Any

from .base import AgentSkill
from .registry import SkillRegistry
from .registry import build_default_skill_registry


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INTERACTION_DIR = ROOT / "visual_web_agent" / "skills" / "interaction"


@dataclass(frozen=True)
class SkillLintIssue:
    severity: str
    code: str
    skill: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "skill": self.skill,
            "message": self.message,
        }


def _issue(severity: str, code: str, skill: str, message: str) -> SkillLintIssue:
    return SkillLintIssue(
        severity=severity,
        code=code,
        skill=skill or "<unknown>",
        message=message,
    )


def _is_sequence_of_strings(value: Any) -> bool:
    return isinstance(value, (tuple, list)) and all(isinstance(item, str) for item in value)


def _source_text(skill: AgentSkill) -> str:
    try:
        path = inspect.getsourcefile(skill.__class__)
        if path:
            return Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""
    return ""


def lint_skill(skill: AgentSkill) -> list[SkillLintIssue]:
    name = str(getattr(skill, "name", "") or "")
    issues: list[SkillLintIssue] = []
    if not name:
        issues.append(_issue("error", "missing_name", name, "skill.name is required"))
    if not str(getattr(skill, "action", "") or "").strip():
        issues.append(_issue("error", "missing_action", name, "skill.action is required"))
    if not str(getattr(skill, "source", "") or "").strip():
        issues.append(_issue("error", "missing_source", name, "skill.source is required"))
    elif str(skill.source) != str(skill.source).upper():
        issues.append(_issue("warning", "source_not_uppercase", name, "skill.source should be uppercase"))

    aliases = getattr(skill, "aliases", ())
    if not _is_sequence_of_strings(aliases):
        issues.append(_issue("error", "invalid_aliases", name, "skill.aliases must be a tuple/list of strings"))
    elif not aliases:
        issues.append(_issue("warning", "missing_aliases", name, "skill.aliases should include URL/goal hints"))

    fields = getattr(skill, "required_fields", ())
    if not _is_sequence_of_strings(fields):
        issues.append(_issue("error", "invalid_required_fields", name, "required_fields must be a tuple/list of strings"))
    elif not fields:
        issues.append(_issue("error", "missing_required_fields", name, "required_fields is required for replay checks"))
    elif len(set(fields)) != len(fields):
        issues.append(_issue("error", "duplicate_required_fields", name, "required_fields contains duplicates"))

    expected_rows = getattr(skill, "expected_rows", 0)
    if not isinstance(expected_rows, int) or expected_rows < 1:
        issues.append(_issue("error", "invalid_expected_rows", name, "expected_rows must be an integer >= 1"))

    if not callable(getattr(skill, "match", None)):
        issues.append(_issue("error", "missing_match", name, "match(url, goal) is required"))
    else:
        try:
            result = skill.match(url="", goal="")
            if not isinstance(result, bool):
                issues.append(_issue("error", "match_not_bool", name, "match() must return bool"))
        except Exception as exc:
            issues.append(_issue("error", "match_raises", name, f"match() raised on empty input: {exc!r}"))

    if not inspect.iscoroutinefunction(getattr(skill, "run", None)):
        issues.append(_issue("error", "run_not_async", name, "run() must be async"))

    try:
        metadata = skill.dispatch_metadata(url="", goal="")
        if not isinstance(metadata, dict):
            issues.append(_issue("warning", "metadata_not_dict", name, "dispatch_metadata() should return a dict"))
    except Exception as exc:
        issues.append(_issue("warning", "metadata_raises", name, f"dispatch_metadata() raised: {exc!r}"))

    source_text = _source_text(skill)
    if source_text:
        if "SkillResult(" not in source_text:
            issues.append(_issue("warning", "missing_skill_result", name, "skill source should construct SkillResult"))
        if "SkillVerification(" not in source_text:
            issues.append(_issue("warning", "missing_verification", name, "skill should emit at least one SkillVerification"))
        if "required_fields=list(self.required_fields)" not in source_text:
            issues.append(_issue("warning", "missing_required_field_contract", name, "SkillResult should copy self.required_fields"))
    return issues


def _registered_modules(registry: SkillRegistry) -> set[str]:
    return {
        str(skill.__class__.__module__).rsplit(".", 1)[-1]
        for skill in registry.matching(url="", goal="")
    } | {
        str(item.__class__.__module__).rsplit(".", 1)[-1]
        for item in getattr(registry, "_skills", [])
    }


def lint_skill_registry(
    registry: SkillRegistry | None = None,
    *,
    include_files: bool = False,
    skill_dir: Path | str = DEFAULT_INTERACTION_DIR,
) -> dict[str, Any]:
    registry = registry or build_default_skill_registry(load_history=False)
    skills = list(getattr(registry, "_skills", []))
    issues: list[SkillLintIssue] = []
    seen_names: set[str] = set()
    for skill in skills:
        name = str(getattr(skill, "name", "") or "")
        if name in seen_names:
            issues.append(_issue("error", "duplicate_skill_name", name, "duplicate registered skill name"))
        seen_names.add(name)
        issues.extend(lint_skill(skill))

    if include_files:
        registered = _registered_modules(registry)
        folder = Path(skill_dir)
        if folder.exists():
            for path in sorted(folder.glob("*.py")):
                if path.name == "__init__.py":
                    continue
                if path.stem not in registered:
                    issues.append(_issue(
                        "warning",
                        "unregistered_skill_file",
                        path.stem,
                        f"{path} is not registered in the default registry",
                    ))

    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    return {
        "ok": not errors,
        "checked": len(skills),
        "errors": len(errors),
        "warnings": len(warnings),
        "issues": [issue.as_dict() for issue in issues],
    }


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint VSpider runtime skills.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--include-files", action="store_true", help="warn about unregistered interaction skill files")
    parser.add_argument("--skill-dir", default=str(DEFAULT_INTERACTION_DIR))
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(argv)

    result = lint_skill_registry(include_files=args.include_files, skill_dir=args.skill_dir)
    if args.json:
        _print_json(result)
    else:
        print(
            f"ok={result['ok']} checked={result['checked']} "
            f"errors={result['errors']} warnings={result['warnings']}"
        )
        for issue in result.get("issues", []):
            print(
                f"  [{str(issue.get('severity')).upper()}] "
                f"{issue.get('skill')} {issue.get('code')}: {issue.get('message')}"
            )
    failed = bool(result["errors"] or (args.strict and result["warnings"]))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

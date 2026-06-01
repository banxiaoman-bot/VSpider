"""Base types for VSpider runtime skills."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillVerification:
    name: str
    success: bool
    expected: Any = ""
    observed: Any = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillResult:
    source: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    expected_rows: int = 1
    required_fields: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    verifications: list[SkillVerification] = field(default_factory=list)


class AgentSkill:
    """Small deterministic workflow unit selected by URL and goal."""

    name = ""
    action = ""
    source = ""
    capability = "interaction"
    aliases: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    expected_rows = 1

    def match(self, *, url: str, goal: str) -> bool:
        raise NotImplementedError

    async def run(self, browser: Any, goal: str) -> SkillResult:
        raise NotImplementedError

    def dispatch_metadata(self, *, url: str, goal: str) -> dict[str, Any]:
        return {"url": url, "aliases": list(self.aliases)}

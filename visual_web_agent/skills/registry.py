"""Registry for deterministic runtime skills."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Iterable

from .base import AgentSkill
from .interaction.demoqa_droppable_slider import DemoQADroppableSliderSkill
from .interaction.demoqa_slider import DemoQASliderSkill
from .interaction.internet_hovers import InternetHoversSkill
from .interaction.modal_dialogs import ModalDialogSkill
from .interaction.reactrouter_docs import ReactRouterDocsSkill
from .interaction.selectorshub_shadow_iframe import SelectorsHubShadowIframeSkill
from .interaction.wikipedia_new_tabs import WikipediaNewTabsSkill


@dataclass(frozen=True)
class SkillMatch:
    skill: AgentSkill
    score: float
    reasons: list[str] = field(default_factory=list)
    history_runs: int = 0
    history_success_rate: float | None = None
    strategy_context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.skill.name,
            "action": self.skill.action,
            "source": self.skill.source,
            "capability": self.skill.capability,
            "aliases": list(self.skill.aliases),
            "required_fields": list(self.skill.required_fields),
            "expected_rows": self.skill.expected_rows,
            "score": self.score,
            "reasons": list(self.reasons),
            "history_runs": self.history_runs,
            "history_success_rate": self.history_success_rate,
            "strategy_context": dict(self.strategy_context or {}),
        }


class SkillRegistry:
    def __init__(
        self,
        skills: Iterable[AgentSkill] = (),
        *,
        history: dict[str, dict[str, float | int]] | None = None,
    ) -> None:
        self._skills: list[AgentSkill] = []
        self._history = history or {}
        for skill in skills:
            self.register(skill)

    def register(self, skill: AgentSkill) -> AgentSkill:
        if not skill.name:
            raise ValueError("skill.name is required")
        if any(existing.name == skill.name for existing in self._skills):
            raise ValueError(f"skill already registered: {skill.name}")
        self._skills.append(skill)
        return skill

    def list_skills(self) -> list[dict[str, object]]:
        return [
            {
                "name": skill.name,
                "action": skill.action,
                "source": skill.source,
                "capability": skill.capability,
                "aliases": list(skill.aliases),
                "required_fields": list(skill.required_fields),
                "expected_rows": skill.expected_rows,
                "history": self._history.get(skill.name, {}),
            }
            for skill in self._skills
        ]

    def score(
        self,
        skill: AgentSkill,
        *,
        url: str,
        goal: str,
        strategy_context: dict[str, Any] | None = None,
    ) -> SkillMatch:
        score = 0.0
        reasons: list[str] = []
        url_text = str(url or "").lower()
        goal_text = str(goal or "").lower()
        strategy = dict(strategy_context or {})
        if skill.match(url=url, goal=goal):
            score += 100.0
            reasons.append("match=true")
        for alias in skill.aliases:
            alias_text = str(alias or "").strip().lower()
            if not alias_text:
                continue
            if alias_text in url_text:
                score += 40.0
                reasons.append(f"url_alias:{alias}")
            if alias_text in goal_text:
                score += 8.0
                reasons.append(f"goal_alias:{alias}")
        capability = str(skill.capability or "").strip().lower()
        if capability and capability in goal_text:
            score += 5.0
            reasons.append(f"goal_capability:{skill.capability}")
        strategy_capabilities = {
            str(item or "").strip().lower()
            for item in strategy.get("capabilities") or []
            if str(item or "").strip()
        }
        strategy_actions = {
            str(item or "").strip().lower()
            for item in strategy.get("preferred_actions") or []
            if str(item or "").strip()
        }
        strategy_modes = {
            str(item or "").strip().lower()
            for item in strategy.get("preferred_modes") or []
            if str(item or "").strip()
        }
        if capability and capability in strategy_capabilities:
            score += 15.0
            reasons.append(f"strategy_capability:{skill.capability}")
        if str(skill.action or "").strip().lower() in strategy_actions:
            score += 20.0
            reasons.append(f"strategy_action:{skill.action}")
        if "macro_first" in strategy_modes and str(skill.action or "").endswith("_macro"):
            score += 5.0
            reasons.append("strategy_mode:macro_first")
        history = self._history.get(skill.name) or {}
        history_runs = int(history.get("runs") or 0)
        history_success_rate = (
            float(history["success_rate"])
            if "success_rate" in history
            else None
        )
        if history_runs and history_success_rate is not None:
            bonus = round(max(0.0, min(1.0, history_success_rate)) * 10.0, 2)
            score += bonus
            reasons.append(f"history_success_rate:{history_success_rate:.2f}")
        return SkillMatch(
            skill=skill,
            score=round(score, 2),
            reasons=reasons,
            history_runs=history_runs,
            history_success_rate=history_success_rate,
            strategy_context=strategy,
        )

    def ranked_matches(
        self,
        *,
        url: str,
        goal: str,
        strategy_context: dict[str, Any] | None = None,
    ) -> list[SkillMatch]:
        scored: list[tuple[int, SkillMatch]] = []
        for index, skill in enumerate(self._skills):
            match = self.score(
                skill,
                url=url,
                goal=goal,
                strategy_context=strategy_context,
            )
            if skill.match(url=url, goal=goal):
                scored.append((index, match))
        scored.sort(key=lambda item: (-item[1].score, item[0]))
        return [item[1] for item in scored]

    def matching(
        self,
        *,
        url: str,
        goal: str,
        strategy_context: dict[str, Any] | None = None,
    ) -> list[AgentSkill]:
        return [
            match.skill
            for match in self.ranked_matches(
                url=url,
                goal=goal,
                strategy_context=strategy_context,
            )
        ]

    def best_match(
        self,
        *,
        url: str,
        goal: str,
        strategy_context: dict[str, Any] | None = None,
    ) -> SkillMatch | None:
        matches = self.ranked_matches(
            url=url,
            goal=goal,
            strategy_context=strategy_context,
        )
        return matches[0] if matches else None

    def first_match(
        self,
        *,
        url: str,
        goal: str,
        strategy_context: dict[str, Any] | None = None,
    ) -> AgentSkill | None:
        match = self.best_match(
            url=url,
            goal=goal,
            strategy_context=strategy_context,
        )
        return match.skill if match else None


def load_skill_history(
    reports_dir: Path | None = None,
    *,
    max_reports: int = 25,
) -> dict[str, dict[str, float | int]]:
    root = Path(__file__).resolve().parents[2]
    folder = reports_dir or (root / "workspace" / "agent_case_reports")
    if not folder.exists():
        return {}
    counts: dict[str, dict[str, int]] = {}
    reports = sorted(
        folder.glob("agent_cases_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:max(0, int(max_reports))]
    for path in reports:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for result in payload.get("results") or []:
            diagnostics = (result or {}).get("diagnostics") or {}
            summary = diagnostics.get("skill_summary") or {}
            skills = summary.get("skills") or []
            if not skills:
                continue
            ok = bool((result or {}).get("ok"))
            for skill_name in {str(item) for item in skills if item}:
                item = counts.setdefault(skill_name, {"runs": 0, "successes": 0})
                item["runs"] += 1
                item["successes"] += 1 if ok else 0
    history: dict[str, dict[str, float | int]] = {}
    for skill_name, item in counts.items():
        runs = int(item.get("runs") or 0)
        successes = int(item.get("successes") or 0)
        history[skill_name] = {
            "runs": runs,
            "successes": successes,
            "success_rate": round(successes / runs, 4) if runs else 0.0,
        }
    return history


def build_default_skill_registry(*, load_history: bool = False) -> SkillRegistry:
    history = load_skill_history() if load_history else {}
    return SkillRegistry([
        ModalDialogSkill(),
        ReactRouterDocsSkill(),
        InternetHoversSkill(),
        DemoQADroppableSliderSkill(),
        DemoQASliderSkill(),
        SelectorsHubShadowIframeSkill(),
        WikipediaNewTabsSkill(),
    ], history=history)

"""Three-backend task-type traffic-splitting (分流) skeleton for the browser substrate.

VSpider can drive three very different browsers, each solving a different
problem (see architecture discussion):

* ``chromium``     - default Playwright Chromium. Renders pixels, so it is the
  only safe choice for the visual main path (SoM screenshots -> VLM, AX tree,
  login / interaction). Medium anti-bot (JS-injection stealth). Maps onto the
  existing ``playwright_chromium`` backend slot.
* ``lightpanda``   - ultra-light, no rendering engine. ~16x less memory / ~9x
  faster for pure DOM/JS/network work, exposed over CDP (``ws://...:9222``).
  CANNOT take screenshots / feed the VLM. Maps onto ``remote_playwright``.
* ``cloakbrowser`` - source-patched stealth Chromium for hard anti-bot sites
  (Cloudflare Turnstile / fingerprinting). Renders pixels. Private binary,
  opt-in. Maps onto the reserved ``stealth_browser`` slot (planned).

``build_default_browser_backend()`` is a flat env/kind switch with no notion of
the task being run. This module is the deterministic *task-type* router on top
of those three personas. The routing maths is split into two independent stages
so the intent stays visible even before the other backends are wired:

1. **Intent ranking** - score each persona purely on task-type fit. The top of
   this order is ``preferred``.
2. **Availability gating** - ``selected`` is the first *available* persona along
   the preferred order (degrading gracefully when the ideal one is not yet
   wired). ``fallback_chain`` is the remaining available personas.

Hard rules baked into routing:

* Vision / screenshot tasks must never be routed to ``lightpanda`` (no render).
* High anti-bot risk prefers ``cloakbrowser`` and degrades to chromium stealth.
* Bulk, pixel-free harvesting prefers ``lightpanda`` for throughput.

Scope: skeleton. Personas are data-driven so availability/wiring can evolve
without touching the routing maths. ``cloakbrowser`` instantiation stays an
explicit ``NotImplementedError`` until the slot ships.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

try:
    from .browser_backend import BrowserBackend, build_default_browser_backend
except ImportError:  # pragma: no cover - relative/script import fallback
    from browser_backend import BrowserBackend, build_default_browser_backend


ROUTE_CONTRACT_VERSION = "browser_backend_route.v1"

# The three personas (what the caller routes between).
BACKEND_CHROMIUM = "chromium"
BACKEND_LIGHTPANDA = "lightpanda"
BACKEND_CLOAK = "cloakbrowser"

# Persona -> concrete browser_backend slot it is built from.
_PERSONA_TO_SLOT = {
    BACKEND_CHROMIUM: "playwright_chromium",
    BACKEND_LIGHTPANDA: "remote_playwright",
    BACKEND_CLOAK: "stealth_browser",
}

_KIND_ALIASES = {
    "chromium": BACKEND_CHROMIUM,
    "chrome": BACKEND_CHROMIUM,
    "playwright": BACKEND_CHROMIUM,
    "playwright_chromium": BACKEND_CHROMIUM,
    "local": BACKEND_CHROMIUM,
    "lightpanda": BACKEND_LIGHTPANDA,
    "remote": BACKEND_LIGHTPANDA,
    "remote_playwright": BACKEND_LIGHTPANDA,
    "cdp": BACKEND_LIGHTPANDA,
    "ws": BACKEND_LIGHTPANDA,
    "cloak": BACKEND_CLOAK,
    "cloakbrowser": BACKEND_CLOAK,
    "stealth": BACKEND_CLOAK,
    "stealth_browser": BACKEND_CLOAK,
}

_ANTI_BOT_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def resolve_kind_alias(kind: str) -> str:
    """Normalize a user/env backend hint to a canonical persona name."""

    return _KIND_ALIASES.get(str(kind or "").strip().lower(), "")


def _cdp_endpoint_configured() -> bool:
    for name in (
        "VSPIDER_REMOTE_BROWSER_ENDPOINT",
        "VSPIDER_BROWSER_CDP_ENDPOINT",
        "VSPIDER_BROWSER_WS_ENDPOINT",
        "VSPIDER_LIGHTPANDA_ENDPOINT",
    ):
        if str(os.getenv(name) or "").strip():
            return True
    return False


@dataclass(frozen=True)
class BackendPersona:
    """Static capability profile of one routable browser backend."""

    name: str
    backend_slot: str
    renders: bool
    anti_bot_level: str  # none / low / medium / high
    throughput: str  # low / medium / high
    status: str  # available / planned / not_configured
    notes: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.status == "available"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "backend_slot": self.backend_slot,
            "renders": self.renders,
            "anti_bot_level": self.anti_bot_level,
            "throughput": self.throughput,
            "status": self.status,
            "notes": list(self.notes),
        }


def default_backend_personas() -> list[BackendPersona]:
    """The three personas with availability resolved from the environment."""

    lightpanda_status = "available" if _cdp_endpoint_configured() else "not_configured"
    return [
        BackendPersona(
            name=BACKEND_CHROMIUM,
            backend_slot=_PERSONA_TO_SLOT[BACKEND_CHROMIUM],
            renders=True,
            anti_bot_level="medium",
            throughput="low",
            status="available",
            notes=("visual main path: SoM/VLM/screenshot/login/interaction",),
        ),
        BackendPersona(
            name=BACKEND_LIGHTPANDA,
            backend_slot=_PERSONA_TO_SLOT[BACKEND_LIGHTPANDA],
            renders=False,
            anti_bot_level="low",
            throughput="high",
            status=lightpanda_status,
            notes=(
                "no rendering engine; pixel-free DOM/API/list harvest over CDP",
                "set VSPIDER_REMOTE_BROWSER_ENDPOINT to a Lightpanda ws:// endpoint",
                "windows: run binary inside WSL2 (ws://127.0.0.1:9222 auto-forwarded); ubuntu: native glibc binary; router only connects, never launches",
            ),
        ),
        BackendPersona(
            name=BACKEND_CLOAK,
            backend_slot=_PERSONA_TO_SLOT[BACKEND_CLOAK],
            renders=True,
            anti_bot_level="high",
            throughput="low",
            status="planned",
            notes=(
                "source-patched stealth Chromium for hard anti-bot sites (opt-in)",
                "private binary: keep as optional extra dep + gitignore the downloaded binary; never commit it (commit/push of this stub is license-safe)",
            ),
        ),
    ]


@dataclass(frozen=True)
class TaskProfile:
    """Task-type signals that drive the split across the three personas."""

    needs_vision: bool = False  # SoM screenshot -> VLM, visual interaction
    needs_screenshot: bool = False
    anti_bot_level: str = "none"  # none / low / high (detected site risk)
    bulk_scale: bool = False  # high-concurrency pixel-free DOM/API harvest
    explicit_backend: str = ""

    def needs_render(self) -> bool:
        return bool(self.needs_vision or self.needs_screenshot)

    def to_dict(self) -> dict[str, Any]:
        return {
            "needs_vision": self.needs_vision,
            "needs_screenshot": self.needs_screenshot,
            "anti_bot_level": self.anti_bot_level,
            "bulk_scale": self.bulk_scale,
            "explicit_backend": self.explicit_backend,
        }


@dataclass
class BackendCandidate:
    """One persona scored on task-type fit (an evidence record)."""

    name: str
    backend_slot: str
    status: str
    eligible: bool
    fit_score: int
    reasons: list[str] = field(default_factory=list)
    persona: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "backend_slot": self.backend_slot,
            "status": self.status,
            "eligible": self.eligible,
            "fit_score": self.fit_score,
            "reasons": list(self.reasons),
            "persona": dict(self.persona),
        }


@dataclass
class BackendRoutingDecision:
    """The 分流 result.

    ``preferred`` is the best persona by task-type intent (may not be wired yet);
    ``selected`` is the first *available* persona along the preferred order
    (what actually runs now); ``fallback_chain`` lists the remaining available
    personas. ``candidates`` carries the full evidence for every persona.
    """

    contract_version: str
    preferred: str | None
    selected: str | None
    backend_slot: str
    preference_order: list[str]
    fallback_chain: list[str]
    task_profile: dict[str, Any]
    candidates: list[dict[str, Any]]
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "preferred": self.preferred,
            "selected": self.selected,
            "backend_slot": self.backend_slot,
            "preference_order": list(self.preference_order),
            "fallback_chain": list(self.fallback_chain),
            "task_profile": dict(self.task_profile),
            "candidates": [dict(item) for item in self.candidates],
            "reasons": list(self.reasons),
        }


class BrowserBackendRouter:
    """Deterministic router that splits browser work by task type across 3 personas."""

    _SCORE_EXPLICIT = 1000
    _SCORE_ANTI_BOT_FULL = 60
    _SCORE_ANTI_BOT_PARTIAL = 20
    _SCORE_ANTI_BOT_PENALTY = -20
    _SCORE_ANTI_BOT_LOW_UNIT = 5
    _SCORE_BULK_HIGH = 50
    _SCORE_BULK_MEDIUM = 20
    _SCORE_VISUAL_DEFAULT = 30

    def __init__(self, *, personas: list[BackendPersona] | None = None) -> None:
        self._personas = list(personas) if personas is not None else None

    def _backends(self) -> list[BackendPersona]:
        if self._personas is not None:
            return self._personas
        return default_backend_personas()

    def route(self, task_profile: TaskProfile | None = None) -> BackendRoutingDecision:
        task = task_profile or TaskProfile()
        explicit = resolve_kind_alias(task.explicit_backend) or resolve_kind_alias(
            os.getenv("VSPIDER_BROWSER_BACKEND") or ""
        )

        candidates = [self._evaluate(persona, task, explicit) for persona in self._backends()]
        # Stage 1: intent ranking (task-type fit only, availability-independent).
        ranked = sorted(
            (c for c in candidates if c.eligible),
            key=lambda c: c.fit_score,
            reverse=True,
        )
        preference_order = [c.name for c in ranked]
        preferred = preference_order[0] if preference_order else None

        # Stage 2: availability gating.
        available = [c for c in ranked if c.status == "available"]
        selected_cand = available[0] if available else (ranked[0] if ranked else None)
        selected = selected_cand.name if selected_cand else None
        backend_slot = selected_cand.backend_slot if selected_cand else ""
        fallback_chain = [c.name for c in available if c.name != selected]

        reasons = self._decision_reasons(task, explicit, ranked, preferred, selected_cand)

        return BackendRoutingDecision(
            contract_version=ROUTE_CONTRACT_VERSION,
            preferred=preferred,
            selected=selected,
            backend_slot=backend_slot,
            preference_order=preference_order,
            fallback_chain=fallback_chain,
            task_profile=task.to_dict(),
            candidates=[c.to_dict() for c in candidates],
            reasons=reasons,
        )

    def _evaluate(
        self,
        persona: BackendPersona,
        task: TaskProfile,
        explicit: str,
    ) -> BackendCandidate:
        reasons: list[str] = [f"status={persona.status}"]
        eligible = True

        if task.needs_render() and not persona.renders:
            eligible = False
            reasons.append("no rendering engine; cannot serve vision/screenshot tasks")

        score = 0
        if explicit and persona.name == explicit:
            score += self._SCORE_EXPLICIT
            reasons.append("explicit backend override")

        task_rank = _ANTI_BOT_RANK.get(task.anti_bot_level, 0)
        persona_rank = _ANTI_BOT_RANK.get(persona.anti_bot_level, 0)
        if task_rank >= _ANTI_BOT_RANK["high"]:
            if persona_rank >= _ANTI_BOT_RANK["high"]:
                score += self._SCORE_ANTI_BOT_FULL
                reasons.append("anti-bot:high matched")
            elif persona_rank >= _ANTI_BOT_RANK["medium"]:
                score += self._SCORE_ANTI_BOT_PARTIAL
                reasons.append("anti-bot:medium partial cover")
            else:
                score += self._SCORE_ANTI_BOT_PENALTY
                reasons.append("anti-bot:too weak for high-risk task")
        elif task_rank > 0:
            score += persona_rank * self._SCORE_ANTI_BOT_LOW_UNIT

        if task.bulk_scale and not task.needs_render():
            if persona.throughput == "high":
                score += self._SCORE_BULK_HIGH
                reasons.append("bulk-scale: high throughput")
            elif persona.throughput == "medium":
                score += self._SCORE_BULK_MEDIUM

        is_plain_visual = task_rank == 0 and not task.bulk_scale
        if is_plain_visual and persona.name == BACKEND_CHROMIUM:
            score += self._SCORE_VISUAL_DEFAULT
            reasons.append("default visual main path")

        return BackendCandidate(
            name=persona.name,
            backend_slot=persona.backend_slot,
            status=persona.status,
            eligible=eligible,
            fit_score=score,
            reasons=reasons,
            persona=persona.to_dict(),
        )

    def _decision_reasons(
        self,
        task: TaskProfile,
        explicit: str,
        ranked: list[BackendCandidate],
        preferred: str | None,
        selected_cand: BackendCandidate | None,
    ) -> list[str]:
        reasons: list[str] = []
        if explicit:
            reasons.append(f"explicit backend hint: {explicit}")
        if task.needs_render():
            reasons.append("vision/screenshot required -> lightpanda excluded (no render)")
        if _ANTI_BOT_RANK.get(task.anti_bot_level, 0) >= _ANTI_BOT_RANK["high"]:
            reasons.append("high anti-bot risk -> prefer cloakbrowser, degrade to chromium stealth")
        if task.bulk_scale and not task.needs_render():
            reasons.append("bulk pixel-free harvest -> prefer lightpanda throughput")
        if not any((explicit, task.needs_render(), task.bulk_scale)) and _ANTI_BOT_RANK.get(
            task.anti_bot_level, 0
        ) == 0:
            reasons.append("plain visual task -> default chromium")

        if not ranked:
            reasons.append("no eligible backend satisfied the task profile")
            return reasons

        selected = selected_cand.name if selected_cand else None
        if preferred and selected and preferred != selected:
            reasons.append(
                f"preferred {preferred!r} not available -> degraded to {selected!r}"
            )
        elif selected_cand is not None and selected_cand.status != "available":
            reasons.append(
                f"selected {selected!r} is not yet available (status={selected_cand.status})"
            )
        return reasons

    def build(self, decision: BackendRoutingDecision) -> BrowserBackend:
        return build_backend_for(decision.selected)


def build_backend_for(name: str | None) -> BrowserBackend:
    """Instantiate a concrete backend from a routing decision's selection."""

    persona = resolve_kind_alias(name or "") or str(name or "")
    if persona == BACKEND_CHROMIUM:
        return build_default_browser_backend("playwright")
    if persona == BACKEND_LIGHTPANDA:
        return build_default_browser_backend("remote")
    if persona == BACKEND_CLOAK:
        raise NotImplementedError(
            "cloakbrowser maps to the reserved stealth_browser slot; no runtime implementation yet"
        )
    raise ValueError(f"unknown browser backend persona: {name!r}")


def route_browser_backend(**kwargs: Any) -> BackendRoutingDecision:
    """Convenience: route with ad-hoc task-profile keyword arguments."""

    return BrowserBackendRouter().route(TaskProfile(**kwargs))

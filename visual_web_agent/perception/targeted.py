"""Intent-driven local perception probes.

This module asks the browser for a compact set of likely target elements
instead of collecting a full-page AX tree or SoM map. It is intentionally a
read-only probe: callers can use the returned selector/evidence to decide
whether to run a deterministic action, ask the model to arbitrate, or fall back
to global perception.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


DEFAULT_KINDS = ("input", "button", "link", "table", "dialog")


@dataclass(frozen=True)
class TargetCandidate:
    kind: str
    text: str
    role: str = ""
    tag: str = ""
    selector: str = ""
    frame_url: str = ""
    frame_name: str = ""
    shadow: bool = False
    bbox: dict[str, float] | None = None
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    attributes: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "role": self.role,
            "tag": self.tag,
            "selector": self.selector,
            "frame_url": self.frame_url,
            "frame_name": self.frame_name,
            "shadow": self.shadow,
            "bbox": self.bbox or {},
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "attributes": self.attributes or {},
        }


@dataclass(frozen=True)
class TargetedProbeResult:
    goal: str
    requested_kinds: tuple[str, ...]
    inferred_kinds: tuple[str, ...]
    candidates: tuple[TargetCandidate, ...]
    frames_checked: int = 0
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.candidates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "goal": self.goal,
            "requested_kinds": list(self.requested_kinds),
            "inferred_kinds": list(self.inferred_kinds),
            "frames_checked": self.frames_checked,
            "errors": list(self.errors),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _goal_terms(goal: str) -> list[str]:
    text = _norm(goal).lower()
    terms = re.findall(r"[a-zA-Z0-9_@.+-]{2,}|[\u4e00-\u9fff]{1,8}", text)
    stop = {
        "the", "and", "then", "with", "that", "this", "page", "click",
        "input", "button", "link", "table", "dialog", "modal", "find",
        "get", "extract", "open", "enter", "type", "搜索", "点击", "输入",
        "提取", "打开", "页面", "按钮", "链接", "表格",
    }
    return [term for term in terms if term not in stop]


def infer_target_kinds(goal: str) -> tuple[str, ...]:
    text = _norm(goal).lower()
    kinds: list[str] = []
    patterns = [
        ("input", r"input|textbox|textarea|search|type|enter|fill|输入|填写|搜索|检索|文本框|输入框"),
        ("button", r"button|submit|confirm|close|next|按钮|提交|确认|关闭|下一步"),
        ("link", r"link|href|profile|new tab|链接|超链接|跳转|打开"),
        ("table", r"table|row|column|cell|表格|行|列|单元格"),
        ("dialog", r"dialog|modal|popup|弹窗|对话框|浮层"),
    ]
    for kind, pattern in patterns:
        if re.search(pattern, text, re.I):
            kinds.append(kind)
    return tuple(kinds or DEFAULT_KINDS)


def _candidate_text(candidate: dict[str, Any]) -> str:
    attrs = candidate.get("attributes") if isinstance(candidate.get("attributes"), dict) else {}
    return " ".join(
        _norm(value).lower()
        for value in [
            candidate.get("text"),
            candidate.get("role"),
            candidate.get("tag"),
            attrs.get("id"),
            attrs.get("name"),
            attrs.get("type"),
            attrs.get("placeholder"),
            attrs.get("ariaLabel"),
            attrs.get("title"),
        ]
        if value
    )


def score_candidate(candidate: dict[str, Any], *, goal: str, inferred_kinds: tuple[str, ...]) -> tuple[float, list[str]]:
    score = 0.0
    evidence = list(candidate.get("evidence") or [])
    kind = str(candidate.get("kind") or "")
    text = _candidate_text(candidate)
    if kind in inferred_kinds:
        score += 0.35
        evidence.append(f"kind:{kind}")
    for term in _goal_terms(goal):
        if term.lower() in text:
            score += 0.12
            evidence.append(f"goal_term:{term}")
    attrs = candidate.get("attributes") if isinstance(candidate.get("attributes"), dict) else {}
    if attrs.get("placeholder"):
        score += 0.06
        evidence.append("has_placeholder")
    if attrs.get("ariaLabel"):
        score += 0.05
        evidence.append("has_aria_label")
    if attrs.get("id") or attrs.get("name"):
        score += 0.04
        evidence.append("has_stable_attr")
    bbox = candidate.get("bbox") if isinstance(candidate.get("bbox"), dict) else {}
    try:
        if float(bbox.get("width") or 0) > 0 and float(bbox.get("height") or 0) > 0:
            score += 0.08
            evidence.append("visible_bbox")
    except Exception:
        pass
    if candidate.get("shadow"):
        evidence.append("inside_shadow_root")
    if candidate.get("frame_url") and candidate.get("frame_url") != "about:blank":
        evidence.append("frame_context")
    return round(min(1.0, score), 3), list(dict.fromkeys(evidence))


def rank_candidates(
    candidates: list[dict[str, Any]],
    *,
    goal: str,
    inferred_kinds: tuple[str, ...] | None = None,
    limit: int = 30,
) -> list[TargetCandidate]:
    inferred = inferred_kinds or infer_target_kinds(goal)
    ranked: list[TargetCandidate] = []
    for raw in candidates:
        confidence, evidence = score_candidate(raw, goal=goal, inferred_kinds=inferred)
        if confidence <= 0:
            continue
        ranked.append(
            TargetCandidate(
                kind=str(raw.get("kind") or ""),
                text=_norm(raw.get("text")),
                role=_norm(raw.get("role")),
                tag=_norm(raw.get("tag")),
                selector=_norm(raw.get("selector")),
                frame_url=_norm(raw.get("frame_url")),
                frame_name=_norm(raw.get("frame_name")),
                shadow=bool(raw.get("shadow")),
                bbox=raw.get("bbox") if isinstance(raw.get("bbox"), dict) else {},
                confidence=confidence,
                evidence=tuple(evidence),
                attributes=dict(raw.get("attributes") or {}),
            )
        )
    ranked.sort(key=lambda item: (-item.confidence, item.kind, item.text[:80]))
    return ranked[: max(1, int(limit or 1))]


def choose_click_handoff_candidate(
    result: TargetedProbeResult,
    *,
    target_text: str,
    min_confidence: float = 0.55,
) -> TargetCandidate | None:
    """Pick a high-confidence button/link candidate for direct selector click.

    The policy is intentionally conservative: it only chooses visible
    button/link candidates that have a selector, meet the confidence threshold,
    and share at least one useful term with the requested click text.
    """
    target = _norm(target_text).lower()
    if not target:
        return None
    terms = [term for term in _goal_terms(target) if len(term) >= 2]
    for candidate in result.candidates:
        if candidate.kind not in {"button", "link"}:
            continue
        if not candidate.selector:
            continue
        if candidate.confidence < min_confidence:
            continue
        haystack = _norm(candidate.text).lower()
        if not haystack:
            continue
        if target in haystack or any(term in haystack for term in terms):
            return candidate
    return None


def choose_type_handoff_candidate(
    result: TargetedProbeResult,
    *,
    min_confidence: float = 0.5,
) -> TargetCandidate | None:
    """Pick a high-confidence text-like input for direct fill/type handoff."""
    blocked_types = {"button", "submit", "reset", "file", "checkbox", "radio", "range", "hidden"}
    for candidate in result.candidates:
        if candidate.kind != "input":
            continue
        if not candidate.selector:
            continue
        if candidate.confidence < min_confidence:
            continue
        attrs = candidate.attributes or {}
        input_type = str(attrs.get("type") or "").strip().lower()
        if input_type in blocked_types:
            continue
        if candidate.tag not in {"input", "textarea"}:
            continue
        return candidate
    return None


_PROBE_JS = r"""
({kinds, limit}) => {
  const wanted = new Set(kinds || []);
  const out = [];
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const cssEscape = (v) => {
    try { return CSS.escape(String(v)); } catch (_) { return String(v).replace(/["\\]/g, '\\$&'); }
  };
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' &&
      s.visibility !== 'hidden' && Number(s.opacity || 1) !== 0;
  };
  const textOf = (el) => clean([
    el.getAttribute && el.getAttribute('aria-label'),
    el.getAttribute && el.getAttribute('placeholder'),
    el.getAttribute && el.getAttribute('title'),
    el.innerText,
    el.textContent,
    el.value
  ].filter(Boolean).join(' '));
  const roleOf = (el) => clean((el.getAttribute && el.getAttribute('role')) || '');
  const attrsOf = (el) => ({
    id: clean(el.id),
    name: clean(el.getAttribute && el.getAttribute('name')),
    type: clean(el.getAttribute && el.getAttribute('type')),
    placeholder: clean(el.getAttribute && el.getAttribute('placeholder')),
    ariaLabel: clean(el.getAttribute && el.getAttribute('aria-label')),
    title: clean(el.getAttribute && el.getAttribute('title')),
    href: clean(el.getAttribute && el.getAttribute('href')),
  });
  const selectorOf = (el) => {
    if (!el || !el.tagName) return '';
    const tag = el.tagName.toLowerCase();
    if (el.id) return `${tag}#${cssEscape(el.id)}`;
    const name = el.getAttribute && el.getAttribute('name');
    if (name) return `${tag}[name="${cssEscape(name)}"]`;
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return `${tag}[aria-label="${cssEscape(aria)}"]`;
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 4) {
      const curTag = cur.tagName.toLowerCase();
      if (cur.id) {
        parts.unshift(`${curTag}#${cssEscape(cur.id)}`);
        break;
      }
      let index = 1;
      let sib = cur;
      while ((sib = sib.previousElementSibling)) {
        if (sib.tagName === cur.tagName) index += 1;
      }
      parts.unshift(`${curTag}:nth-of-type(${index})`);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };
  const kindOf = (el) => {
    const tag = (el.tagName || '').toLowerCase();
    const role = roleOf(el).toLowerCase();
    const type = clean(el.getAttribute && el.getAttribute('type')).toLowerCase();
    if (tag === 'table' || role === 'table' || role === 'grid') return 'table';
    if (tag === 'dialog' || role === 'dialog' || el.getAttribute('aria-modal') === 'true') return 'dialog';
    if (tag === 'a' && el.getAttribute('href')) return 'link';
    if (tag === 'button' || role === 'button' || ['button', 'submit', 'reset'].includes(type)) return 'button';
    if (['input', 'textarea', 'select'].includes(tag) || ['textbox', 'searchbox', 'combobox'].includes(role)) return 'input';
    return '';
  };
  const add = (el, shadow) => {
    if (!visible(el)) return;
    const kind = kindOf(el);
    if (!kind || (wanted.size && !wanted.has(kind))) return;
    const r = el.getBoundingClientRect();
    const attrs = attrsOf(el);
    const evidence = [];
    if (attrs.id) evidence.push('id');
    if (attrs.name) evidence.push('name');
    if (attrs.placeholder) evidence.push('placeholder');
    if (attrs.ariaLabel) evidence.push('aria_label');
    if (shadow) evidence.push('shadow_root');
    out.push({
      kind,
      text: textOf(el),
      role: roleOf(el),
      tag: (el.tagName || '').toLowerCase(),
      selector: selectorOf(el),
      shadow: Boolean(shadow),
      bbox: {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)},
      evidence,
      attributes: attrs
    });
  };
  const walkRoot = (root, shadow) => {
    const selectors = [
      'input', 'textarea', 'select', 'button', '[role="button"]',
      'a[href]', 'table', '[role="table"]', '[role="grid"]',
      'dialog', '[role="dialog"]', '[aria-modal="true"]'
    ].join(',');
    root.querySelectorAll(selectors).forEach((el) => add(el, shadow));
    root.querySelectorAll('*').forEach((el) => {
      if (el.shadowRoot) walkRoot(el.shadowRoot, true);
    });
  };
  walkRoot(document, false);
  const seen = new Set();
  return out.filter((item) => {
    const key = [item.kind, item.selector, item.text, item.shadow].join('|');
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, Math.max(1, Number(limit || 100)));
}
"""


async def _probe_frame(frame: Any, *, kinds: tuple[str, ...], limit: int) -> tuple[list[dict[str, Any]], str]:
    try:
        items = await frame.evaluate(_PROBE_JS, {"kinds": list(kinds), "limit": limit})
    except Exception as exc:
        return [], f"{getattr(frame, 'url', '')}: {exc!r}"
    frame_url = _norm(getattr(frame, "url", ""))
    frame_name = ""
    try:
        frame_name = _norm(frame.name)
    except Exception:
        frame_name = ""
    for item in items or []:
        item["frame_url"] = frame_url
        item["frame_name"] = frame_name
    return list(items or []), ""


async def probe_page(
    page: Any,
    *,
    goal: str,
    kinds: tuple[str, ...] | list[str] | None = None,
    limit: int = 30,
) -> TargetedProbeResult:
    inferred = infer_target_kinds(goal)
    requested = tuple(kinds or inferred)
    raw_candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    frames = list(getattr(page, "frames", []) or [page.main_frame])
    per_frame_limit = max(10, min(120, int(limit or 30) * 3))
    for frame in frames:
        items, error = await _probe_frame(frame, kinds=requested, limit=per_frame_limit)
        raw_candidates.extend(items)
        if error:
            errors.append(error)
    ranked = rank_candidates(
        raw_candidates,
        goal=goal,
        inferred_kinds=inferred,
        limit=limit,
    )
    return TargetedProbeResult(
        goal=goal,
        requested_kinds=requested,
        inferred_kinds=inferred,
        candidates=tuple(ranked),
        frames_checked=len(frames),
        errors=tuple(errors[:10]),
    )


def format_probe_text(result: TargetedProbeResult, *, limit: int = 20) -> str:
    lines = [
        (
            f"ok={result.ok} candidates={len(result.candidates)} "
            f"frames={result.frames_checked} inferred={list(result.inferred_kinds)}"
        )
    ]
    for index, candidate in enumerate(result.candidates[: max(1, int(limit or 1))], start=1):
        bbox = candidate.bbox or {}
        pos = ""
        if bbox:
            pos = f" @ ({bbox.get('x')},{bbox.get('y')}) {bbox.get('width')}x{bbox.get('height')}"
        frame = f" frame={candidate.frame_name or candidate.frame_url}" if candidate.frame_url else ""
        lines.append(
            f"{index:02d}. {candidate.kind:<6} conf={candidate.confidence:.2f} "
            f"{candidate.text[:120]!r}{pos}{frame}"
        )
        if candidate.selector:
            lines.append(f"    selector={candidate.selector}")
        if candidate.evidence:
            lines.append(f"    evidence={', '.join(candidate.evidence[:8])}")
    if result.errors:
        lines.append("errors:")
        lines.extend(f"  - {item[:200]}" for item in result.errors[:5])
    return "\n".join(lines)


def dumps_probe(result: TargetedProbeResult) -> str:
    return json.dumps(result.as_dict(), ensure_ascii=False, indent=2)

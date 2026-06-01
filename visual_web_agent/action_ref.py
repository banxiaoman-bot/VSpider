from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_ACTION_REF_VERSION = "action_ref.v1"


@dataclass(frozen=True)
class BoundingBox:
    x: float
    y: float
    width: float
    height: float

    def to_dict(self) -> dict[str, float]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class ActionRef:
    ref: str
    source: str
    selector: str = ""
    selectors: dict[str, str] = field(default_factory=dict)
    role: str = ""
    name: str = ""
    tag: str = ""
    text: str = ""
    bbox: BoundingBox | None = None
    point: tuple[float, float] | None = None
    som_id: int | None = None
    ax_id: str = ""
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": _ACTION_REF_VERSION,
            "ref": self.ref,
            "source": self.source,
            "selector": self.selector,
            "selectors": dict(self.selectors),
            "role": self.role,
            "name": self.name,
            "tag": self.tag,
            "text": self.text,
            "bbox": self.bbox.to_dict() if self.bbox else None,
            "point": list(self.point) if self.point else None,
            "som_id": self.som_id,
            "ax_id": self.ax_id,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }


def action_ref_schema() -> dict[str, Any]:
    return {
        "version": _ACTION_REF_VERSION,
        "fields": {
            "ref": "stable public reference such as @e1 or som:5",
            "source": "browser_ref|som|selector|role|bbox|point|ax|unknown",
            "selector": "preferred executable selector when available",
            "selectors": "css/xpath/role/text selector alternatives",
            "role": "accessibility or inferred role",
            "name": "accessible name or compact visible label",
            "tag": "DOM tag when known",
            "text": "visible text when known",
            "bbox": "viewport box {x,y,width,height} when known",
            "point": "normalized or viewport point [x,y] when known",
            "som_id": "SoM numeric target id when known",
            "ax_id": "accessibility/snapshot id such as @e5 when known",
            "confidence": "0..1 confidence score",
            "metadata": "non-authoritative source-specific metadata",
        },
        "preferred_sources": ["browser_ref", "selector", "role", "som", "bbox", "point", "ax"],
        "notes": [
            "Use selector/browser_ref before visual point clicks when available.",
            "Treat bbox/point refs as last-resort visual grounding evidence, not stable selectors.",
        ],
    }


def normalize_action_ref(raw: Any, *, default_source: str = "unknown", session_id: str = "") -> dict[str, Any]:
    if isinstance(raw, ActionRef):
        return raw.to_dict()
    if isinstance(raw, str):
        return _from_string(raw, default_source=default_source, session_id=session_id).to_dict()
    if not isinstance(raw, dict):
        return ActionRef(ref="", source=default_source, metadata={"raw": raw}).to_dict()
    inferred_source = _infer_source(raw)
    source = str(raw.get("source") or (inferred_source if default_source in {"", "unknown"} else default_source) or inferred_source)
    ref = _ref_from_raw(raw, source)
    selectors = _selectors_from_raw(raw)
    selector = str(raw.get("selector") or selectors.get("css") or selectors.get("role") or selectors.get("xpath") or "")
    bbox = _bbox_from_raw(raw)
    point = _point_from_raw(raw)
    som_id = _som_id_from_raw(raw, ref)
    ax_id = str(raw.get("ax_id") or raw.get("ref") or "") if source in {"ax", "browser_ref"} else str(raw.get("ax_id") or "")
    role = str(raw.get("role") or (raw.get("element") or {}).get("role") or "")
    name = str(raw.get("name") or raw.get("label") or (raw.get("element") or {}).get("name") or raw.get("text") or "")
    tag = str(raw.get("tag") or (raw.get("element") or {}).get("tag") or "")
    text = str(raw.get("text") or raw.get("inner_text") or name or "")
    confidence = _clamp_confidence(raw.get("confidence", raw.get("score", 1.0)))
    metadata = {
        key: value
        for key, value in raw.items()
        if key not in {
            "ref", "source", "selector", "selectors", "role", "name", "label", "tag", "text", "inner_text",
            "bbox", "box", "bounding_box", "point", "som_id", "target_id", "ax_id", "confidence", "score",
            "element",
        }
    }
    if session_id:
        metadata.setdefault("session_id", session_id)
    return ActionRef(
        ref=ref,
        source=source,
        selector=selector,
        selectors=selectors,
        role=role,
        name=name,
        tag=tag,
        text=text,
        bbox=bbox,
        point=point,
        som_id=som_id,
        ax_id=ax_id,
        confidence=confidence,
        metadata=metadata,
    ).to_dict()


def normalize_action_refs(items: list[Any], *, default_source: str = "unknown", session_id: str = "") -> list[dict[str, Any]]:
    return [normalize_action_ref(item, default_source=default_source, session_id=session_id) for item in items]


def action_refs_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    session_id = str(snapshot.get("session_id") or "")
    rows: list[dict[str, Any]] = []
    for item in snapshot.get("items") or []:
        if not isinstance(item, dict):
            continue
        rows.append(normalize_action_ref(item, default_source="browser_ref", session_id=session_id))
    return rows


def summarize_action_ref_sources(action_refs: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    stable = 0
    visual = 0
    for item in action_refs:
        source = str(item.get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
        if item.get("selector") or source in {"browser_ref", "selector", "role", "ax"}:
            stable += 1
        if item.get("bbox") or item.get("point") or source in {"bbox", "point", "som"}:
            visual += 1
    return {
        "version": _ACTION_REF_VERSION,
        "count": len(action_refs),
        "sources": counts,
        "stable_count": stable,
        "visual_count": visual,
    }


def _from_string(value: str, *, default_source: str, session_id: str) -> ActionRef:
    raw = str(value or "").strip()
    source = default_source or "unknown"
    som_id = _som_id_from_raw({}, raw)
    if re.fullmatch(r"@?e\d+", raw, re.I):
        source = "browser_ref"
        ref = raw if raw.startswith("@") else f"@{raw}"
        return ActionRef(ref=ref, source=source, ax_id=ref, metadata={"session_id": session_id} if session_id else {})
    if som_id is not None:
        return ActionRef(ref=f"som:{som_id}", source="som", som_id=som_id, metadata={"raw_ref": raw})
    if raw.startswith(("#", ".", "//", "xpath=", "css=", "role=", "text=")) or " > " in raw:
        selectors = {"css": raw[4:] if raw.startswith("css=") else raw}
        if raw.startswith("xpath=") or raw.startswith("//"):
            selectors = {"xpath": raw[6:] if raw.startswith("xpath=") else raw}
        elif raw.startswith("role="):
            selectors = {"role": raw}
        elif raw.startswith("text="):
            selectors = {"text": raw[5:]}
        return ActionRef(ref=_stable_ref("selector", raw), source="selector", selector=raw, selectors=selectors)
    return ActionRef(ref=raw, source=source, name=raw, metadata={"session_id": session_id} if session_id else {})


def _infer_source(raw: dict[str, Any]) -> str:
    ref = str(raw.get("ref") or "")
    if ref.startswith("@e") or re.fullmatch(r"@?e\d+", ref, re.I):
        return "browser_ref"
    if raw.get("target_id") is not None or raw.get("som_id") is not None:
        return "som"
    if raw.get("selector") or raw.get("selectors"):
        return "selector"
    if raw.get("role"):
        return "role"
    if raw.get("bbox") or raw.get("box") or raw.get("bounding_box"):
        return "bbox"
    if raw.get("point"):
        return "point"
    return "unknown"


def _ref_from_raw(raw: dict[str, Any], source: str) -> str:
    ref = str(raw.get("ref") or "").strip()
    if ref:
        return ref if not re.fullmatch(r"e\d+", ref, re.I) else f"@{ref}"
    som_id = _som_id_from_raw(raw, "")
    if som_id is not None:
        return f"som:{som_id}"
    selector = str(raw.get("selector") or (_selectors_from_raw(raw).get("css") or ""))
    if selector:
        return _stable_ref("selector", selector)
    role = str(raw.get("role") or (raw.get("element") or {}).get("role") or "")
    name = str(raw.get("name") or (raw.get("element") or {}).get("name") or raw.get("text") or "")
    if role or name:
        return _stable_ref("role", f"{role}:{name}")
    if raw.get("point"):
        return _stable_ref("point", str(raw.get("point")))
    if raw.get("bbox") or raw.get("box") or raw.get("bounding_box"):
        return _stable_ref("bbox", str(raw.get("bbox") or raw.get("box") or raw.get("bounding_box")))
    return _stable_ref(source or "unknown", str(raw))


def _selectors_from_raw(raw: dict[str, Any]) -> dict[str, str]:
    selectors: dict[str, str] = {}
    direct = raw.get("selectors")
    if isinstance(direct, dict):
        for key, value in direct.items():
            if value not in (None, ""):
                selectors[str(key)] = str(value)
    selector = raw.get("selector")
    if selector not in (None, ""):
        selectors.setdefault("css", str(selector))
    element = raw.get("element") if isinstance(raw.get("element"), dict) else {}
    role_selector = raw.get("role_selector") or element.get("role_selector")
    if role_selector not in (None, ""):
        selectors.setdefault("role", str(role_selector))
    return selectors


def _bbox_from_raw(raw: dict[str, Any]) -> BoundingBox | None:
    value = raw.get("bbox") or raw.get("box") or raw.get("bounding_box")
    if isinstance(value, dict):
        try:
            return BoundingBox(
                x=float(value.get("x") or 0),
                y=float(value.get("y") or 0),
                width=float(value.get("width") or value.get("w") or 0),
                height=float(value.get("height") or value.get("h") or 0),
            )
        except Exception:
            return None
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return BoundingBox(x=float(value[0]), y=float(value[1]), width=float(value[2]), height=float(value[3]))
        except Exception:
            return None
    return None


def _point_from_raw(raw: dict[str, Any]) -> tuple[float, float] | None:
    value = raw.get("point")
    if isinstance(value, dict):
        try:
            return (float(value.get("x") or 0), float(value.get("y") or 0))
        except Exception:
            return None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return (float(value[0]), float(value[1]))
        except Exception:
            return None
    return None


def _som_id_from_raw(raw: dict[str, Any], ref: str) -> int | None:
    for key in ("som_id", "target_id"):
        if raw.get(key) not in (None, ""):
            try:
                value = int(raw.get(key))
                return value if value > 0 else None
            except Exception:
                pass
    text = str(ref or "")
    match = re.fullmatch(r"(?:som:|target:)?(\d+)", text, re.I)
    if match:
        value = int(match.group(1))
        return value if value > 0 else None
    return None


def _clamp_confidence(value: Any) -> float:
    try:
        numeric = float(value)
    except Exception:
        numeric = 1.0
    return max(0.0, min(1.0, numeric))


def _stable_ref(prefix: str, value: str) -> str:
    text = str(value or "")
    checksum = 0
    for ch in text:
        checksum = ((checksum * 131) + ord(ch)) % 100000
    return f"{prefix}:{checksum:05d}"

"""``output_contract.v1`` - inferred run output shape.

Pure data + helpers. Wraps :func:`extraction_engine.strategies.infer_goal_output_contract`
and extends it with ``output_kind`` / ``container`` / ``media_hint`` /
``post_process`` while remaining backward compatible with existing callers.

Backward compatibility:
  Old callers of ``infer_goal_output_contract`` still receive ``mode`` /
  ``answer_required`` / ``artifact_required`` / ``save_artifact`` /
  ``structured_rows`` / ``reasons``. The new wrapper adds keys; it never
  removes or renames.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class OutputPrediction:
    kind: str = ""
    mode: str = ""
    confidence: float | None = None
    source: str = "model"

    def normalized(self) -> "OutputPrediction":
        return OutputPrediction(
            kind=_normalize_model_prediction(self.kind),
            mode=str(self.mode or "").strip().lower(),
            confidence=self.confidence,
            source=str(self.source or "model"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "mode": self.mode,
            "confidence": self.confidence,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "OutputPrediction":
        data = dict(payload or {}) if isinstance(payload, dict) else {}
        return cls(
            kind=str(data.get("kind") or data.get("output_kind") or data.get("predicted_kind") or ""),
            mode=str(data.get("mode") or data.get("output_mode") or data.get("predicted_mode") or ""),
            confidence=data.get("confidence"),
            source=str(data.get("source") or "model"),
        ).normalized()




VERSION = "output_contract.v1"


OUTPUT_KINDS = (
    "answer_text",
    "dataset_rows",
    "dataset_records",
    "media_image",
    "media_video",
    "media_audio",
    "media_pdf",
    "media_archive",
    "file_generic",
    "html_snapshot",
    "screenshot",
    "code_or_text",
    "mixed",
)

CONTAINERS = (
    "inline_text",
    "xlsx",
    "csv",
    "jsonl",
    "json",
    "files_folder",
    "zip",
    "html",
    "markdown",
)


# Media-aware regexes (extend, do NOT replace, the existing
# _SAVE_ARTIFACT_RE / _STRUCTURED_ARTIFACT_RE in strategies.py).

_IMAGE_RE = re.compile(
    r"\b(image|photo|picture|jpg|jpeg|png|gif|webp|thumbnail)s?\b"
    r"|\u56fe\u7247|\u56fe\u50cf|\u7167\u7247|\u622a\u56fe|\u7f29\u7565\u56fe",
    re.I,
)
_VIDEO_RE = re.compile(
    r"\b(video|mp4|mkv|webm|stream|movie|clip)s?\b"
    r"|\u89c6\u9891|\u5f55\u50cf|\u76f4\u64ad|\u7247\u6bb5",
    re.I,
)
_AUDIO_RE = re.compile(
    r"\b(audio|mp3|wav|podcast|voice|sound|speech)s?\b"
    r"|\u97f3\u9891|\u5f55\u97f3|\u8bed\u97f3|\u64ad\u5ba2|\u58f0\u97f3",
    re.I,
)
_PDF_RE = re.compile(
    r"\b(pdf|report|whitepaper|manuscript|brochure)s?\b"
    r"|\u62a5\u544a(?:\u6587\u4ef6)?|\u767d\u76ae\u4e66|\u8bf4\u660e\u4e66|\u624b\u518c",
    re.I,
)
_ARCHIVE_RE = re.compile(
    r"\b(archive|zip|rar|7z|tar\.gz|tgz|bundle|package)s?\b"
    r"|\u538b\u7f29\u5305|\u6253\u5305(?:\u4e0b\u8f7d)?",
    re.I,
)
_HTML_SNAPSHOT_RE = re.compile(
    r"\b(snapshot|archive page|save page|whole page|offline copy|page html)\b"
    r"|\u9875\u9762\u5feb\u7167|\u6574\u9875\u4fdd\u5b58|\u79bb\u7ebf\u526f\u672c|\u672c\u5730\u5b58\u9875",
    re.I,
)
_SCREENSHOT_RE = re.compile(
    r"\bscreen\s*shot\b|screen capture"
    r"|\u622a\u5c4f|\u622a\u56fe(?!\u8f6f\u4ef6)",
    re.I,
)
_CODE_RE = re.compile(
    r"\b(source code|snippet|markdown report|md file|notebook)\b"
    r"|\u6e90\u7801|\u4ee3\u7801\u7247\u6bb5|\u62a5\u544a\.md|\u7b14\u8bb0\u672c",
    re.I,
)
_ANSWER_FORCE_RE = re.compile(
    r"\b(just answer|only the answer|one line|short answer|yes or no)\b"
    r"|\u53ea\u9700\u8981\u7b54\u6848|\u53ea\u8981\u7ed3\u679c|\u4e00\u53e5\u8bdd|\u662f\u4e0d\u662f|\u6709\u6ca1\u6709",
    re.I,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_FIELD_KEY_RE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]+")


def _field_key(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower().replace("-", "_")
    text = _FIELD_KEY_RE.sub("_", text).strip("_")
    return text


def _iter_field_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_iter_field_values(item))
        return out
    text = str(value or "").strip()
    if not text:
        return []
    if any(sep in text for sep in (",", "，", "、", ";", "；", "\n")):
        return [
            item.strip()
            for item in re.split(r"[,，、;；\n]+", text)
            if item.strip()
        ]
    return [text]


def normalize_output_fields(*values: Any) -> list[str]:
    """Canonical ordered field list for output contracts and their aliases."""

    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _iter_field_values(value):
            key = _field_key(item)
            if key and key not in seen:
                out.append(item)
                seen.add(key)
    return out


def output_contract_fields(contract: "OutputContract | dict[str, Any] | None") -> list[str]:
    """Return the canonical field constraints carried by an output contract."""

    if isinstance(contract, OutputContract):
        return normalize_output_fields(contract.fields)
    if not isinstance(contract, dict):
        return []
    values: list[Any] = [
        contract.get("fields"),
        contract.get("required_fields"),
        contract.get("requested_fields"),
    ]
    pipeline = contract.get("item_pipeline")
    if isinstance(pipeline, dict):
        values.extend([
            pipeline.get("fields"),
            pipeline.get("required_fields"),
            pipeline.get("requested_fields"),
        ])
    return normalize_output_fields(*values)


def normalize_output_contract_dict(contract: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize legacy field aliases into the canonical ``fields`` key."""

    data = dict(contract or {}) if isinstance(contract, dict) else {}
    fields = output_contract_fields(data)
    if fields:
        data["fields"] = fields
        data["required_fields"] = list(fields)
        data["requested_fields"] = list(fields)
    return data


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class OutputContract:
    mode: str = "default"
    output_kind: str = "mixed"
    container: str = "files_folder"
    fields: list[str] = field(default_factory=list)
    post_process: list[str] = field(default_factory=list)
    user_explicit: bool = False
    reasons: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    version: str = VERSION

    def __post_init__(self) -> None:
        self.fields = normalize_output_fields(self.fields)

    def to_dict(self) -> dict[str, Any]:
        fields = normalize_output_fields(self.fields)
        return {
            "version": self.version,
            "mode": self.mode,
            "output_kind": self.output_kind if self.output_kind in OUTPUT_KINDS else "mixed",
            "container": self.container if self.container in CONTAINERS else "files_folder",
            "fields": fields,
            "required_fields": list(fields),
            "requested_fields": list(fields),
            "post_process": list(self.post_process or []),
            "user_explicit": bool(self.user_explicit),
            "reasons": list(self.reasons or []),
            "created_at": self.created_at,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def default_container_for_kind(output_kind: str, *, row_count: int | None = None) -> str:
    """Map ``output_kind`` to a sensible default ``container``.

    Callers can still override; this is policy, not law.
    """

    kind = output_kind or "mixed"
    if kind == "answer_text":
        return "inline_text"
    if kind == "dataset_rows":
        if row_count is not None and row_count >= 100:
            return "jsonl"
        return "xlsx"
    if kind == "dataset_records":
        return "jsonl"
    if kind == "html_snapshot":
        return "html"
    if kind == "code_or_text":
        return "markdown"
    if kind in {
        "media_image", "media_video", "media_audio",
        "media_pdf", "media_archive", "file_generic",
        "screenshot", "mixed",
    }:
        return "files_folder"
    return "files_folder"


def _detect_media_kind(text: str) -> tuple[str, list[str]]:
    """Try to map the goal text to a single ``media_*`` ``output_kind``."""

    matches: list[tuple[str, str]] = []
    if _IMAGE_RE.search(text):
        matches.append(("media_image", "goal_mentions_image"))
    if _VIDEO_RE.search(text):
        matches.append(("media_video", "goal_mentions_video"))
    if _AUDIO_RE.search(text):
        matches.append(("media_audio", "goal_mentions_audio"))
    if _PDF_RE.search(text):
        matches.append(("media_pdf", "goal_mentions_pdf"))
    if _ARCHIVE_RE.search(text):
        matches.append(("media_archive", "goal_mentions_archive"))

    if not matches:
        return "", []
    if len(matches) == 1:
        kind, reason = matches[0]
        return kind, [reason]
    # multiple media kinds requested -> mixed
    return "mixed", [r for _, r in matches] + ["multiple_media_kinds"]


def _normalize_model_prediction(value: str) -> str:
    value = str(value or "").strip().lower()
    if not value:
        return ""
    aliases = {
        "answer": "answer_text",
        "text": "answer_text",
        "article": "html_snapshot",
        "snapshot": "html_snapshot",
        "page": "html_snapshot",
        "screenshot": "screenshot",
        "image": "media_image",
        "photo": "media_image",
        "picture": "media_image",
        "video": "media_video",
        "audio": "media_audio",
        "pdf": "media_pdf",
        "archive": "media_archive",
        "zip": "media_archive",
        "dataset": "dataset_rows",
        "rows": "dataset_rows",
        "records": "dataset_records",
    }
    return aliases.get(value, value if value in OUTPUT_KINDS else "")


def _is_compatible_model_prediction(goal_kind: str, predicted_kind: str) -> bool:
    if not predicted_kind:
        return False
    if goal_kind == predicted_kind:
        return True
    if goal_kind == "mixed":
        return True
    if goal_kind == "answer_text" and predicted_kind in {"answer_text", "code_or_text"}:
        return True
    if goal_kind in {"dataset_rows", "dataset_records"} and predicted_kind in {"dataset_rows", "dataset_records"}:
        return True
    if goal_kind.startswith("media_") and predicted_kind.startswith("media_"):
        return True
    if goal_kind == "html_snapshot" and predicted_kind in {"html_snapshot", "file_generic"}:
        return True
    if goal_kind == "screenshot" and predicted_kind == "screenshot":
        return True
    return False


def infer_output_contract(
    goal: str,
    *,
    target_count: int | None = None,
    requested_fields: list[str] | tuple[str, ...] | None = None,
    user_explicit_kind: str = "",
    user_explicit_container: str = "",
    model_predicted_kind: str = "",
    model_predicted_mode: str = "",
) -> OutputContract:
    """Infer the full :class:`OutputContract` from a goal string.

    This function calls the legacy ``infer_goal_output_contract`` for the
    coarse ``mode`` (answer / artifact / mixed / default) and ``reasons``,
    then layers media / snapshot / screenshot / code detection on top to
    produce ``output_kind`` and ``container``.

    User-provided overrides (``user_explicit_kind`` / ``user_explicit_container``)
    win unconditionally. ``model_predicted_kind`` / ``model_predicted_mode``
    are advisory signals that can upgrade the final decision when they are
    compatible with the task intent.
    """

    from visual_web_agent.extraction_engine.strategies import (
        infer_goal_output_contract as _legacy_infer,
    )

    legacy = _legacy_infer(
        goal,
        target_count=target_count,
        requested_fields=requested_fields,
    )

    text = str(goal or "")
    mode = str(legacy.get("mode") or "default")
    reasons: list[str] = [str(r) for r in (legacy.get("reasons") or [])]

    fields_list: list[str] = normalize_output_fields(requested_fields)
    post_process: list[str] = []
    user_explicit = False

    legacy_kind = ""
    media_kind, media_reasons = _detect_media_kind(text)
    if media_kind:
        legacy_kind = media_kind
        reasons.extend(media_reasons)
    elif _HTML_SNAPSHOT_RE.search(text):
        legacy_kind = "html_snapshot"
        reasons.append("goal_mentions_page_snapshot")
    elif _SCREENSHOT_RE.search(text):
        legacy_kind = "screenshot"
        reasons.append("goal_mentions_screenshot")
    elif _CODE_RE.search(text):
        legacy_kind = "code_or_text"
        reasons.append("goal_mentions_code_or_report")
    elif _ANSWER_FORCE_RE.search(text):
        legacy_kind = "answer_text"
        reasons.append("goal_forces_short_answer")
    else:
        if mode == "answer":
            legacy_kind = "answer_text"
        elif mode == "artifact":
            legacy_kind = "dataset_rows" if bool(legacy.get("structured_rows")) else "file_generic"
        elif mode == "mixed":
            legacy_kind = "mixed"
        else:
            legacy_kind = "mixed"

    model_kind = _normalize_model_prediction(model_predicted_kind)
    model_mode = str(model_predicted_mode or "").strip().lower()
    if model_kind:
        reasons.append("model_predicted_output_kind")
    if model_mode:
        reasons.append("model_predicted_output_mode")

    # 1. user explicit overrides always win.
    if user_explicit_kind and user_explicit_kind in OUTPUT_KINDS:
        kind = user_explicit_kind
        user_explicit = True
        reasons.append("user_explicit_output_kind")
    else:
        kind = legacy_kind
        if model_kind and _is_compatible_model_prediction(kind, model_kind):
            if kind == "mixed" and model_kind != "mixed":
                kind = model_kind
                reasons.append("model_refined_mixed_output")
            elif kind in {"answer_text", "dataset_rows", "dataset_records", "html_snapshot", "screenshot", "code_or_text", "file_generic"}:
                kind = model_kind
                reasons.append("model_confirmed_output_kind")
        elif not kind and model_kind:
            kind = model_kind
            reasons.append("model_supplied_output_kind")

        if model_mode == "answer" and kind == "mixed":
            kind = "answer_text"
            reasons.append("model_prefers_answer_mode")
        elif model_mode == "artifact" and kind == "mixed":
            kind = "dataset_rows" if bool(legacy.get("structured_rows")) else "file_generic"
            reasons.append("model_prefers_artifact_mode")

    # 2. container
    if user_explicit_container and user_explicit_container in CONTAINERS:
        container = user_explicit_container
        user_explicit = True
        reasons.append("user_explicit_container")
    else:
        container = default_container_for_kind(kind, row_count=target_count)

    # 3. post_process hints
    if kind == "media_image":
        post_process.append("dedup")
    if kind == "media_pdf" and re.search(r"ocr|\u8bc6\u522b\u6587\u5b57|\u6587\u672c\u62bd\u53d6", text, re.I):
        post_process.append("ocr")
        reasons.append("goal_requests_ocr")
    if kind == "media_video" and re.search(r"transcode|\u8f6c\u7801|\u538b\u7f29\u89c6\u9891", text, re.I):
        post_process.append("transcode")
        reasons.append("goal_requests_transcode")

    return OutputContract(
        mode=mode,
        output_kind=kind if kind in OUTPUT_KINDS else "mixed",
        container=container,
        fields=fields_list,
        post_process=post_process,
        user_explicit=user_explicit,
        reasons=list(dict.fromkeys(reasons)),
    )

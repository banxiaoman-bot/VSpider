"""``input_contract.v1`` - normalized run inputs.

Pure data + helpers. No FS / network IO. Designed to be importable from
``api_server.py``, ``smart_batch_runner.py``, CLI, or replay tooling without
side effects.

Key responsibilities:

- Parse the loose ``target_url`` / ``urls`` / ``file`` form fields into a
  structured ``InputContract`` instance.
- Infer ``AttachmentSpec.intent`` from filename / mime / goal when caller did
  not pass one explicitly.
- Provide ``to_dict()`` for stable JSON serialization (drives
  ``runs/<id>/input_contract.json``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlparse


VERSION = "input_contract.v1"


URL_ROLES = ("start", "reference", "dataset", "api", "unknown")
ATTACHMENT_INTENTS = (
    "batch_rows",
    "upload_to_page",
    "prompt_context",
    "media_source",
    "unknown",
)


_ROW_GOAL_RE = re.compile(
    r"\b(batch|per[- ]row|each row|fill in|submit each|every row|"
    r"iterate|loop over|for each)\b"
    r"|\u6309\u884c|\u6309\u6761|\u9010\u884c|\u9010\u6761|\u6279\u91cf|"
    r"\u586b\u62a5|\u586b\u5199|\u63d0\u4ea4(?:\u8868\u5355|\u4e3b\u8868)?",
    re.I,
)
_UPLOAD_GOAL_RE = re.compile(
    r"\b(upload|attach|submit (?:this|the) (?:file|pdf|image|photo))\b"
    r"|\u4e0a\u4f20|\u9644\u4ef6|\u63d0\u4ea4(?:\u9644\u4ef6|\u8be5|\u8fd9\u4e2a)",
    re.I,
)
_READ_GOAL_RE = re.compile(
    r"\b(read|summarize|extract from|analy[sz]e (?:this|the))\b"
    r"|\u9605\u8bfb|\u603b\u7ed3|\u4ece(?:\u8be5|\u8fd9\u4e2a).*\u62bd\u53d6"
    r"|\u603b\u7ed3.*(?:pdf|\u6587\u4ef6|\u9644\u4ef6)",
    re.I,
)
_URL_FIELD_NAMES = {
    "url", "urls", "link", "links", "site", "homepage", "page",
    "\u7f51\u5740", "\u94fe\u63a5", "\u9875\u9762", "\u5730\u5740",
}


_DATAFRAME_SUFFIXES = {".csv", ".tsv", ".xls", ".xlsx", ".xlsm", ".ods", ".parquet"}
_TEXT_SUFFIXES = {".txt", ".md", ".html", ".htm"}
_JSON_SUFFIXES = {".json", ".jsonl", ".ndjson", ".yaml", ".yml"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv"}
_AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}
_ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"}
_PDF_SUFFIXES = {".pdf"}
# Word-processor and presentation suffixes. Excel-family suffixes live in
# ``_DATAFRAME_SUFFIXES`` above and are split by goal verbs into either
# ``batch_rows`` or ``prompt_context`` (see ``infer_attachment_intent``).
_WORD_SUFFIXES = {".doc", ".docx", ".docm", ".odt", ".rtf"}
_PRESENTATION_SUFFIXES = {".ppt", ".pptx", ".pptm", ".odp"}
_EMAIL_SUFFIXES = {".eml", ".msg"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _suffix(name: str) -> str:
    if not name:
        return ""
    idx = name.rfind(".")
    return name[idx:].lower() if idx >= 0 else ""


def _is_array_json_payload(sample: bytes | None) -> bool:
    if not sample:
        return False
    head = sample.lstrip()[:16]
    return head.startswith(b"[") or head.startswith(b"\n[")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class UrlSpec:
    url: str
    role: str = "start"
    system_id: str = "auto"
    auth_profile: str = "auto"

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "role": self.role if self.role in URL_ROLES else "unknown",
            "system_id": self.system_id or "auto",
            "auth_profile": self.auth_profile or "auto",
        }


@dataclass
class AttachmentSpec:
    path: str
    filename: str = ""
    mime: str = ""
    size: int = 0
    sha256: str = ""
    intent: str = "unknown"
    schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "filename": self.filename,
            "mime": self.mime,
            "size": int(self.size or 0),
            "sha256": self.sha256,
            "intent": self.intent if self.intent in ATTACHMENT_INTENTS else "unknown",
            "schema": dict(self.schema or {}),
        }


@dataclass
class ModelOverrides:
    vlm: dict[str, Any] = field(default_factory=dict)
    semantic: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vlm": {k: v for k, v in (self.vlm or {}).items() if v not in (None, "")},
            "semantic": {k: v for k, v in (self.semantic or {}).items() if v not in (None, "")},
        }

    def is_empty(self) -> bool:
        d = self.to_dict()
        return not d["vlm"] and not d["semantic"]


@dataclass
class Constraints:
    max_runs: int = 0
    rate_limit_qps: float = 0.0
    allow_cross_system: bool = True
    max_steps: int = 0
    proxy_server: str = ""
    proxy_username: str = ""
    proxy_password: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "max_runs": int(self.max_runs or 0),
            "rate_limit_qps": float(self.rate_limit_qps or 0.0),
            "allow_cross_system": bool(self.allow_cross_system),
            "max_steps": int(self.max_steps or 0),
            "proxy_server": str(self.proxy_server or ""),
            "proxy_username": str(self.proxy_username or ""),
            "proxy_password": str(self.proxy_password or ""),
        }
        for key, value in (self.extra or {}).items():
            skey = str(key)
            if skey and skey not in payload:
                payload[skey] = _json_safe(value)
        return payload


@dataclass
class InputContract:
    goal: str
    urls: list[UrlSpec] = field(default_factory=list)
    attachments: list[AttachmentSpec] = field(default_factory=list)
    auth_profiles: list[str] = field(default_factory=list)
    model_overrides: ModelOverrides = field(default_factory=ModelOverrides)
    constraints: Constraints = field(default_factory=Constraints)
    source: str = "api"
    created_at: str = field(default_factory=_now_iso)
    version: str = VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "goal": self.goal,
            "urls": [u.to_dict() for u in self.urls],
            "attachments": [a.to_dict() for a in self.attachments],
            "auth_profiles": [str(p) for p in self.auth_profiles if p],
            "model_overrides": self.model_overrides.to_dict(),
            "constraints": self.constraints.to_dict(),
            "source": self.source,
            "created_at": self.created_at,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @property
    def primary_start_url(self) -> str:
        for url in self.urls:
            if url.role == "start" and url.url:
                return url.url
        return self.urls[0].url if self.urls else ""

    @property
    def has_cross_system(self) -> bool:
        seen: set[str] = set()
        for url in self.urls:
            host = ""
            try:
                host = urlparse(url.url).netloc.split("@")[-1].split(":")[0]
            except Exception:
                host = ""
            if host:
                seen.add(host)
        return len(seen) > 1


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


def parse_urls_field(raw: Any) -> list[UrlSpec]:
    """Accept many shapes and normalize to ``list[UrlSpec]``.

    Accepts:
    - ``None`` / empty string -> []
    - JSON string of list[dict] or list[str]
    - list[str] / list[dict]
    - single string (newline / comma separated)
    """

    if raw is None:
        return []

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("[") or text.startswith("{"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                return parse_urls_field(parsed)
        parts: list[str] = []
        for piece in re.split(r"[\n,;]+", text):
            piece = piece.strip()
            if piece:
                parts.append(piece)
        return [UrlSpec(url=p) for p in parts]

    if isinstance(raw, dict):
        return [_url_from_dict(raw)] if raw.get("url") else []

    if isinstance(raw, Iterable):
        out: list[UrlSpec] = []
        for item in raw:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    out.append(UrlSpec(url=s))
            elif isinstance(item, dict):
                spec = _url_from_dict(item)
                if spec.url:
                    out.append(spec)
        return out

    return []


def _url_host(url: str) -> str:
    try:
        return urlparse(url).netloc.split("@")[-1].split(":")[0].strip().lower()
    except Exception:
        return ""


def assign_system_ids(url_specs: list[UrlSpec]) -> list[UrlSpec]:
    """S7: give multi-host contracts deterministic per-host ``system_id``s.

    When a contract spans more than one host, every ``system_id="auto"``
    entry gets ``sys_<host-slug>`` (same host -> same id) so the workflow
    graph and SessionRouter can treat each host as a first-class system.
    Single-host contracts and user-provided ids are left untouched, keeping
    the legacy single-system behaviour byte-identical. Mutates in place and
    returns the same list for chaining.
    """

    hosts = {_url_host(spec.url) for spec in url_specs if _url_host(spec.url)}
    if len(hosts) <= 1:
        return url_specs
    for spec in url_specs:
        if (spec.system_id or "auto") != "auto":
            continue
        host = _url_host(spec.url)
        if not host:
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", host).strip("_")
        if slug:
            spec.system_id = f"sys_{slug}"
    return url_specs


def _url_from_dict(item: dict[str, Any]) -> UrlSpec:
    return UrlSpec(
        url=str(item.get("url") or "").strip(),
        role=str(item.get("role") or "start").strip() or "start",
        system_id=str(item.get("system_id") or "auto").strip() or "auto",
        auth_profile=str(item.get("auth_profile") or "auto").strip() or "auto",
    )


# ---------------------------------------------------------------------------
# goal -> URL inference (deterministic, no LLM)
# ---------------------------------------------------------------------------

# Boundary chars that terminate a URL inside free-form (often CJK) goal text.
_URL_STOP = r"\s\u3000\"'<>（）()【】，。；：！？、"

# TLDs recognized in *bare* domains (no scheme / www). Deliberately conservative
# so code-ish tokens are NOT mistaken for sites: .io/.ai/.so/.co/.me/.run/.sh/.js
# are excluded because "scipy.io", "lib.so", "asyncio.run", "node.js" are common.
# Multi-part TLDs are listed first so they win the alternation.
_BARE_TLDS = (
    "com.cn", "org.cn", "net.cn", "gov.cn", "edu.cn",
    "com", "cn", "org", "net", "gov", "edu",
    "xyz", "top", "vip", "info", "biz", "club", "site", "store", "tech", "online",
)

_SCHEME_URL_RE = re.compile(
    rf"https?://[^{_URL_STOP}]+|www\.[^{_URL_STOP}]+",
    re.I,
)
_BARE_DOMAIN_RE = re.compile(
    r"(?<![\w@./-])"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    rf"(?:{'|'.join(_BARE_TLDS)})"
    rf"(?:/[^{_URL_STOP}]*)?",
    re.I,
)

_URL_TRAILING_TRIM = "/.,;:!?，。；：！？、)）】"


def infer_urls_from_goal(goal: str) -> list[UrlSpec]:
    """Extract start URLs the user literally wrote inside a free-form goal.

    Deterministic only -- recognizes ``http(s)://`` / ``www.`` forms and bare
    domains ending in a conservative common-TLD set, normalizing bare / www
    forms to ``https://`` and de-duplicating. It does **not** guess or suggest
    a search-engine entry for abstract goals; that (LLM) concern is layered on
    elsewhere. Returns ``[]`` when nothing URL-like is present.
    """

    text = goal or ""
    if not text.strip():
        return []

    out: list[UrlSpec] = []
    seen: set[str] = set()
    consumed: list[tuple[int, int]] = []

    def _add(candidate: str) -> None:
        u = candidate.strip().rstrip(_URL_TRAILING_TRIM)
        if not u:
            return
        key = u.lower().rstrip("/")
        if key in seen:
            return
        seen.add(key)
        out.append(UrlSpec(url=u, role="start"))

    for m in _SCHEME_URL_RE.finditer(text):
        consumed.append(m.span())
        raw = m.group(0)
        _add(("https://" + raw) if raw.lower().startswith("www.") else raw)

    for m in _BARE_DOMAIN_RE.finditer(text):
        start, _ = m.span()
        if any(cs <= start < ce for cs, ce in consumed):
            continue  # already captured as part of a scheme / www URL
        _add("https://" + m.group(0))

    return out


# ---------------------------------------------------------------------------
# goal -> default entry suggestion (LLM-assisted, deterministic fallback)
# ---------------------------------------------------------------------------

# Login-free search entry. ``{q}`` is url-quoted before formatting.
_SEARCH_ENTRY_TEMPLATE = "https://www.bing.com/search?q={q}"

_ENTRY_LLM_PROMPT = (
    "\u4f60\u662f\u4e00\u4e2a\u7f51\u9875\u81ea\u52a8\u5316\u52a9\u624b\u3002\u7528\u6237\u7684\u4efb\u52a1\u76ee\u6807\u5982\u4e0b\uff1a\n"
    "{goal}\n\n"
    "\u5982\u679c\u4f60\u77e5\u9053\u5b8c\u6210\u8be5\u76ee\u6807\u6700\u5408\u9002\u7684\u8d77\u59cb\u7f51\u7ad9\uff0c\u8bf7\u53ea\u56de\u590d\u8be5\u7f51\u7ad9\u7684 URL"
    "\uff08\u5355\u884c\uff0c\u4e0d\u8981\u89e3\u91ca\uff09\u3002\u5982\u679c\u6ca1\u6709\u660e\u786e\u7ad9\u70b9\u3001\u5e94\u5f53\u7528\u641c\u7d22\u5f15\u64ce\uff0c\u8bf7\u53ea\u56de\u590d NONE\u3002"
)


@dataclass
class EntrySuggestion:
    """A suggested start URL for a goal that carried no literal URL.

    ``source``:
      - ``llm``             - the injected model proposed a specific site
      - ``search_fallback`` - deterministic search-engine entry (always works)

    ``needs_confirmation`` is True because the URL is a guess, not something the
    user typed; callers should let the user confirm before committing to it.
    """

    url: str
    source: str
    needs_confirmation: bool = True
    reason: str = ""

    def to_url_spec(self) -> "UrlSpec":
        return UrlSpec(url=self.url, role="start")

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "source": self.source,
            "needs_confirmation": bool(self.needs_confirmation),
            "reason": self.reason,
        }


def _search_entry(goal: str, template: str) -> EntrySuggestion:
    from urllib.parse import quote_plus

    return EntrySuggestion(
        url=template.format(q=quote_plus(goal.strip())),
        source="search_fallback",
        needs_confirmation=True,
        reason="goal carried no explicit URL; defaulting to a search-engine entry",
    )


def suggest_entry_url(
    goal: str,
    *,
    llm: Callable[[str], str] | None = None,
    search_template: str = _SEARCH_ENTRY_TEMPLATE,
) -> EntrySuggestion | None:
    """Suggest a start URL for an abstract goal that has no literal URL.

    Deterministic-first: when ``llm`` is omitted (or it fails / declines) a
    search-engine URL is returned, which always gives the agent somewhere to
    start. When ``llm`` is provided it is asked for a specific site; its answer
    is parsed with :func:`infer_urls_from_goal` (so bare domains are accepted)
    and used only if it yields a URL.

    ``llm`` is injected as a ``prompt -> text`` callable so this module performs
    no network IO of its own and stays trivially testable. Returns ``None`` for
    an empty goal.
    """

    text = (goal or "").strip()
    if not text:
        return None

    if llm is not None:
        raw = ""
        try:
            raw = llm(_ENTRY_LLM_PROMPT.format(goal=text)) or ""
        except Exception:
            raw = ""
        parsed = infer_urls_from_goal(raw)
        if parsed:
            return EntrySuggestion(
                url=parsed[0].url,
                source="llm",
                needs_confirmation=True,
                reason="model suggested a specific entry site",
            )

    return _search_entry(text, search_template)


# ---------------------------------------------------------------------------
# Attachment intent inference
# ---------------------------------------------------------------------------


def infer_attachment_intent(
    *,
    filename: str = "",
    mime: str = "",
    goal: str = "",
    sample_bytes: bytes | None = None,
    columns: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Return one of ``ATTACHMENT_INTENTS``.

    Rules in priority order:
    1. Filename suffix gives the strongest signal.
    2. Goal verbs (upload/attach vs read/summarize) refine ambiguous types
       like pdf/image.
    3. Tabular column hints (``url``/``link``/etc.) keep tabular files as
       ``batch_rows``.
    4. Unknown types default to ``unknown``.
    """

    suffix = _suffix(filename)
    mime_l = (mime or "").lower()
    goal_text = goal or ""

    # Tabular / spreadsheet. Default to batch_rows so smart_batch_runner can
    # consume row-shaped data; if the user explicitly says "summarize / read
    # this spreadsheet", flip to prompt_context.
    if suffix in _DATAFRAME_SUFFIXES or mime_l in {
        "text/csv", "text/tab-separated-values",
        "application/vnd.ms-excel",
        "application/vnd.ms-excel.sheet.macroEnabled.12",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/parquet",
    }:
        if _READ_GOAL_RE.search(goal_text) and not _ROW_GOAL_RE.search(goal_text):
            return "prompt_context"
        if _UPLOAD_GOAL_RE.search(goal_text) and not _ROW_GOAL_RE.search(goal_text):
            return "upload_to_page"
        return "batch_rows"

    if suffix in _JSON_SUFFIXES:
        # .jsonl / .ndjson are line-delimited records by definition.
        if suffix in {".jsonl", ".ndjson"}:
            return "batch_rows"
        if _is_array_json_payload(sample_bytes):
            return "batch_rows"
        if columns:
            return "batch_rows"
        return "prompt_context"

    if suffix in _TEXT_SUFFIXES or mime_l.startswith("text/"):
        return "prompt_context"

    # Word documents: default to prompt_context (read/summarize) unless the
    # user explicitly asks to upload them.
    if suffix in _WORD_SUFFIXES or mime_l in {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-word.document.macroEnabled.12",
        "application/vnd.oasis.opendocument.text",
        "application/rtf",
    }:
        if _UPLOAD_GOAL_RE.search(goal_text):
            return "upload_to_page"
        return "prompt_context"

    # Presentations: same policy as Word.
    if suffix in _PRESENTATION_SUFFIXES or mime_l in {
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
        "application/vnd.oasis.opendocument.presentation",
    }:
        if _UPLOAD_GOAL_RE.search(goal_text):
            return "upload_to_page"
        return "prompt_context"

    # Email files: read as prompt_context by default.
    if suffix in _EMAIL_SUFFIXES or mime_l in {
        "message/rfc822", "application/vnd.ms-outlook",
    }:
        if _UPLOAD_GOAL_RE.search(goal_text):
            return "upload_to_page"
        return "prompt_context"

    if suffix in _PDF_SUFFIXES or mime_l == "application/pdf":
        if _UPLOAD_GOAL_RE.search(goal_text):
            return "upload_to_page"
        if _READ_GOAL_RE.search(goal_text):
            return "prompt_context"
        return "upload_to_page"

    if suffix in _IMAGE_SUFFIXES or mime_l.startswith("image/"):
        if _UPLOAD_GOAL_RE.search(goal_text):
            return "upload_to_page"
        return "prompt_context"

    if (
        suffix in _VIDEO_SUFFIXES
        or suffix in _AUDIO_SUFFIXES
        or mime_l.startswith("video/")
        or mime_l.startswith("audio/")
    ):
        if _UPLOAD_GOAL_RE.search(goal_text):
            return "upload_to_page"
        return "media_source"

    if suffix in _ARCHIVE_SUFFIXES:
        return "upload_to_page"

    return "unknown"


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def build_input_contract(
    *,
    goal: str,
    target_url: str = "",
    urls: Any = None,
    attachments: list[dict[str, Any]] | None = None,
    auth_profiles: str | list[str] = "",
    vlm_options: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    source: str = "api",
) -> InputContract:
    """Build a normalized :class:`InputContract` from raw API form-style inputs.

    ``target_url`` is treated as a legacy alias for ``urls=[{url}]``. When
    ``urls`` contains entries, it is authoritative and ``target_url`` is ignored.
    """

    goal_text = (goal or "").strip()

    url_specs: list[UrlSpec] = parse_urls_field(urls)
    tu = (target_url or "").strip()
    if not url_specs and tu:
        url_specs.insert(0, UrlSpec(url=tu, role="start"))

    if not url_specs:
        # goal -> URL fallback: only when the caller supplied no explicit url.
        url_specs = infer_urls_from_goal(goal_text)

    for spec in url_specs:
        spec.role = spec.role if spec.role in URL_ROLES else "unknown"

    # S7: multi-host submissions get deterministic per-host system ids so
    # cross-system scheduling has stable identities from the contract on.
    assign_system_ids(url_specs)

    attachment_specs: list[AttachmentSpec] = []
    for raw in attachments or []:
        if not isinstance(raw, dict):
            continue
        filename = str(raw.get("filename") or "")
        mime = str(raw.get("mime") or "")
        intent = str(raw.get("intent") or "").strip()
        columns = raw.get("schema", {}).get("columns") if isinstance(raw.get("schema"), dict) else None
        if intent not in ATTACHMENT_INTENTS:
            intent = infer_attachment_intent(
                filename=filename,
                mime=mime,
                goal=goal_text,
                sample_bytes=raw.get("sample_bytes") if isinstance(raw.get("sample_bytes"), (bytes, bytearray)) else None,
                columns=columns,
            )
        attachment_specs.append(
            AttachmentSpec(
                path=str(raw.get("path") or ""),
                filename=filename,
                mime=mime,
                size=int(raw.get("size") or 0),
                sha256=str(raw.get("sha256") or ""),
                intent=intent,
                schema=dict(raw.get("schema") or {}),
            )
        )

    if isinstance(auth_profiles, str):
        profile_list = [p.strip() for p in re.split(r"[,;]+", auth_profiles) if p.strip()]
    else:
        profile_list = [str(p).strip() for p in (auth_profiles or []) if str(p).strip()]

    vlm_opts = vlm_options or {}
    overrides = ModelOverrides(
        vlm={
            "base_url": vlm_opts.get("base_url") or "",
            "api_key": vlm_opts.get("api_key") or "",
            "model": vlm_opts.get("model") or "",
            "temperature": vlm_opts.get("temperature"),
            "max_tokens": vlm_opts.get("max_tokens"),
            "model_type": vlm_opts.get("model_type") or "",
        },
        semantic={
            "base_url": vlm_opts.get("semantic_base_url") or "",
            "api_key": vlm_opts.get("semantic_api_key") or "",
            "model": vlm_opts.get("semantic_model") or "",
        },
    )

    cons = constraints or {}
    known_constraint_keys = {
        "max_runs", "rate_limit_qps", "allow_cross_system", "max_steps",
        "proxy_server", "proxy", "proxy_username", "proxy_user",
        "proxy_password", "proxy_pass",
    }
    constraint_extra = {
        str(k): _json_safe(v)
        for k, v in cons.items()
        if str(k) not in known_constraint_keys and v is not None
    }
    constraint_obj = Constraints(
        max_runs=int(cons.get("max_runs") or 0),
        rate_limit_qps=float(cons.get("rate_limit_qps") or 0.0),
        allow_cross_system=bool(cons.get("allow_cross_system", True)),
        max_steps=int(cons.get("max_steps") or 0),
        proxy_server=str(cons.get("proxy_server") or cons.get("proxy") or ""),
        proxy_username=str(cons.get("proxy_username") or cons.get("proxy_user") or ""),
        proxy_password=str(cons.get("proxy_password") or cons.get("proxy_pass") or ""),
        extra=constraint_extra,
    )

    return InputContract(
        goal=goal_text,
        urls=url_specs,
        attachments=attachment_specs,
        auth_profiles=profile_list,
        model_overrides=overrides,
        constraints=constraint_obj,
        source=source or "api",
    )

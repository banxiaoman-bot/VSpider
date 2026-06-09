from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit


SECRET_KEY_PARTS = ("api_key", "password", "secret", "token")


def is_secret_key(key: Any) -> bool:
    lowered = str(key or "").lower()
    return any(part in lowered for part in SECRET_KEY_PARTS)


def redact_proxy_userinfo(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): redact_secret_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_proxy_userinfo(item) for item in value]
    if not isinstance(value, str):
        return value

    text = value.strip()
    if "@" not in text:
        return value
    if "," in text:
        return ",".join(str(redact_proxy_userinfo(part.strip())) for part in text.split(","))

    try:
        parsed = urlsplit(text)
    except Exception:
        parsed = None
    if parsed and parsed.scheme and parsed.netloc and "@" in parsed.netloc:
        host_port = parsed.netloc.rsplit("@", 1)[1]
        return urlunsplit((parsed.scheme, host_port, parsed.path, parsed.query, parsed.fragment))

    if "://" not in text:
        userinfo, host_port = text.rsplit("@", 1)
        if ":" in userinfo and host_port and not any(ch.isspace() for ch in host_port):
            return host_port
    return value


def redact_secret_value(key: Any, value: Any) -> Any:
    if is_secret_key(key) and value not in (None, ""):
        return "***"
    if isinstance(value, dict):
        return redact_secret_mapping(value)
    if isinstance(value, (list, tuple, set)):
        return [redact_secret_value("", item) for item in value]
    return redact_proxy_userinfo(value)


def redact_secret_mapping(value: Any, *, drop_empty: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, raw_value in dict(value or {}).items():
        if drop_empty and raw_value in (None, ""):
            continue
        out[str(key)] = redact_secret_value(key, raw_value)
    return out


# Keys whose values are opaque blobs (screenshots, base64 frames, raw bytes).
# They never carry structured secrets and are too large to walk on the live
# broadcast hot path, so they pass through event redaction untouched.
EVENT_BLOB_KEYS = frozenset(
    {"data", "image", "screenshot", "b64", "frame", "thumbnail", "bytes"}
)

# Strings longer than this are treated as opaque payloads (base64 / HTML
# response bodies) and skipped, keeping the per-frame broadcast path cheap.
EVENT_MAX_SCAN_LEN = 2048


def _redact_event_value(key: Any, value: Any, *, max_str: int) -> Any:
    if str(key).lower() in EVENT_BLOB_KEYS:
        return value
    if is_secret_key(key) and value not in (None, ""):
        return "***"
    if isinstance(value, dict):
        return redact_event_payload(value, max_str=max_str)
    if isinstance(value, (list, tuple)):
        return [_redact_event_value("", item, max_str=max_str) for item in value]
    if isinstance(value, str):
        if len(value) > max_str:
            return value
        return redact_proxy_userinfo(value)
    return value


def redact_event_payload(payload: Any, *, max_str: int = EVENT_MAX_SCAN_LEN) -> Any:
    """Cheap, broadcast-safe redaction for live event / WebSocket payloads.

    Masks ``api_key`` / ``password`` / ``secret`` / ``token`` keys at any depth
    and strips URL-embedded credentials, but leaves opaque blob fields
    (screenshot ``data``, base64) and over-long strings untouched so the
    per-frame broadcast path stays cheap. Returns a new object; the input is
    never mutated.
    """
    if not isinstance(payload, dict):
        return payload
    return {
        str(key): _redact_event_value(key, raw_value, max_str=max_str)
        for key, raw_value in payload.items()
    }

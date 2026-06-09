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

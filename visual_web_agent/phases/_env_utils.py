"""Shared environment / pattern-matching utilities (extracted from main.py).

Pure helper functions used across multiple phases modules and main.py.
"""
import os
import re
from functools import lru_cache


def _env_or_default(name: str, default: str) -> str:
    value = os.getenv(name, "")
    return value.strip() if value and value.strip() else default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except Exception:
        return default
    return value if value > 0 else default


@lru_cache(maxsize=None)
def _split_env_keywords(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "") or ""
    if not raw.strip():
        return ()
    parts = [
        part.strip()
        for part in re.split(r"[\r\n,;|]+", raw)
        if part and part.strip()
    ]
    return tuple(parts)


def _text_matches_patterns(text: str, base_patterns: list[str], extra_env_name: str = "") -> bool:
    patterns = list(base_patterns)
    if extra_env_name:
        patterns.extend(re.escape(item) for item in _split_env_keywords(extra_env_name))
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)

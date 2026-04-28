"""
Local secret placeholder resolver.

The VLM should only see placeholders such as {{env:OA_PASS}}. This module
resolves them immediately before browser-side input, so secrets stay out of
model prompts, action JSON, trajectory logs, and RPA recordings.
"""

from __future__ import annotations

import os
import re


ENV_PLACEHOLDER_RE = re.compile(r"\{\{\s*env:([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class SecretResolutionError(RuntimeError):
    pass


def resolve_env_placeholders(text: str) -> tuple[str, bool, list[str]]:
    """Resolve {{env:VAR}} placeholders from process environment."""
    if not text or "{{" not in text:
        return text, False, []

    used: list[str] = []

    def repl(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        value = os.getenv(name)
        if value is None:
            raise SecretResolutionError(
                f"Auth Vault missing environment variable {name!r}; "
                "refusing to type unresolved placeholder."
            )
        used.append(name)
        return value

    return ENV_PLACEHOLDER_RE.sub(repl, text), bool(used), used


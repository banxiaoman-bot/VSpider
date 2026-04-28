"""
Auth Matrix utilities for VSpider.

This module keeps authentication state handling outside the VLM prompt path:
multiple Playwright storage_state files can be selected, checked for conflicts,
merged, and injected into a persistent browser context.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("vspider.auth")


@dataclass(frozen=True)
class AuthLoadResult:
    profiles: list[str]
    files: list[Path]
    state: dict
    warnings: list[str]

    @property
    def enabled(self) -> bool:
        return bool(self.files)

    def prompt_note(self) -> str:
        if not self.enabled:
            return "未加载 auth profile。"
        names = ", ".join(self.profiles)
        cookie_count = len(self.state.get("cookies") or [])
        origin_count = len(self.state.get("origins") or [])
        return f"已加载 auth profile: {names}（cookies={cookie_count}, origins={origin_count}）。"


def project_auth_dir() -> Path:
    return Path(__file__).resolve().parents[1] / ".auth"


def parse_profile_names(raw: str | None) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[\s,;]+", text) if part.strip()]


def _normalize_host(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        try:
            text = urlparse(text).hostname or text
        except Exception:
            pass
    text = text.lstrip(".").split("/")[0].split(":")[0]
    return text


def _host_matches(host: str, domain: str) -> bool:
    host = _normalize_host(host)
    domain = _normalize_host(domain)
    return bool(host and domain and (host == domain or host.endswith(f".{domain}")))


def _site_key(domain: str) -> str:
    host = _normalize_host(domain)
    if not host:
        return ""
    labels = [part for part in host.split(".") if part]
    if len(labels) <= 2:
        return host

    two_part_public_suffixes = {
        "com.cn", "net.cn", "org.cn", "gov.cn",
        "co.uk", "org.uk", "ac.uk",
        "com.hk", "com.tw", "co.jp",
    }
    suffix2 = ".".join(labels[-2:])
    if suffix2 in two_part_public_suffixes and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _state_domains(state: dict) -> set[str]:
    domains: set[str] = set()
    for cookie in state.get("cookies") or []:
        domain = _normalize_host(str(cookie.get("domain") or ""))
        if domain:
            domains.add(domain)
    for origin in state.get("origins") or []:
        origin_url = str(origin.get("origin") or "")
        domain = _normalize_host(origin_url)
        if domain:
            domains.add(domain)
    return domains


def _load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a storage_state JSON object")
    return data


def _profile_path(auth_dir: Path, profile_name: str) -> Path:
    name = profile_name.strip()
    if name.endswith(".json"):
        return auth_dir / name
    return auth_dir / f"{name}.json"


def _available_profile_files(auth_dir: Path) -> list[Path]:
    if not auth_dir.exists():
        return []
    return sorted(path for path in auth_dir.glob("*.json") if path.is_file())


def _auto_select_files(auth_dir: Path, start_url: str) -> list[Path]:
    host = _normalize_host(start_url)
    if not host:
        return []

    hits: list[tuple[int, Path]] = []
    for path in _available_profile_files(auth_dir):
        try:
            state = _load_json(path)
        except Exception as exc:
            logger.warning("[AUTH] Skip invalid auth profile %s: %s", path, exc)
            continue
        matched_domains = [domain for domain in _state_domains(state) if _host_matches(host, domain)]
        if matched_domains:
            best_len = max(len(domain) for domain in matched_domains)
            hits.append((best_len, path))

    hits.sort(key=lambda item: (-item[0], item[1].name))
    return [path for _, path in hits]


def _dedupe_cookies(cookies: list[dict]) -> list[dict]:
    result: dict[tuple[str, str, str], dict] = {}
    for cookie in cookies:
        key = (
            str(cookie.get("name") or ""),
            str(cookie.get("domain") or ""),
            str(cookie.get("path") or "/"),
        )
        result[key] = cookie
    return list(result.values())


def _merge_origins(origins: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for origin_data in origins:
        origin = str(origin_data.get("origin") or "")
        if not origin:
            continue
        target = merged.setdefault(origin, {"origin": origin, "localStorage": []})
        items_by_name = {
            str(item.get("name") or ""): dict(item)
            for item in target.get("localStorage") or []
            if item.get("name") is not None
        }
        for item in origin_data.get("localStorage") or []:
            if item.get("name") is not None:
                items_by_name[str(item.get("name"))] = dict(item)
        target["localStorage"] = list(items_by_name.values())
    return list(merged.values())


def merge_storage_states(files: list[Path]) -> tuple[dict, list[str]]:
    merged_cookies: list[dict] = []
    merged_origins: list[dict] = []
    seen_sites: dict[str, Path] = {}
    warnings: list[str] = []

    for path in files:
        state = _load_json(path)
        domains = _state_domains(state)
        for domain in sorted(domains):
            site = _site_key(domain)
            if not site:
                continue
            previous = seen_sites.get(site)
            if previous and previous != path:
                raise ValueError(
                    "auth profile domain conflict: "
                    f"{previous.name} and {path.name} both contain state for site {site!r} "
                    f"(domain {domain!r}). "
                    "Load only one account per site/domain."
                )
            seen_sites[site] = path

        merged_cookies.extend(cookie for cookie in state.get("cookies") or [] if isinstance(cookie, dict))
        merged_origins.extend(origin for origin in state.get("origins") or [] if isinstance(origin, dict))

        if not domains:
            warnings.append(f"{path.name} has no cookies/origins")

    return {
        "cookies": _dedupe_cookies(merged_cookies),
        "origins": _merge_origins(merged_origins),
    }, warnings


def load_auth_profiles(
    raw_profiles: str | None,
    start_url: str,
    auth_dir: str | Path | None = None,
) -> AuthLoadResult:
    names = parse_profile_names(raw_profiles)
    auth_root = Path(auth_dir) if auth_dir else project_auth_dir()
    if not names:
        return AuthLoadResult([], [], {"cookies": [], "origins": []}, [])

    if len(names) == 1 and names[0].lower() == "auto":
        files = _auto_select_files(auth_root, start_url)
        profile_names = [path.stem for path in files]
    else:
        files = [_profile_path(auth_root, name) for name in names]
        missing = [path for path in files if not path.exists()]
        if missing:
            missing_names = ", ".join(path.name for path in missing)
            raise FileNotFoundError(f"auth profile file(s) not found in {auth_root}: {missing_names}")
        profile_names = [path.stem for path in files]

    if not files:
        return AuthLoadResult([], [], {"cookies": [], "origins": []}, [])

    state, warnings = merge_storage_states(files)
    return AuthLoadResult(profile_names, files, state, warnings)


def build_storage_init_script(state: dict) -> str:
    origins_payload: dict[str, dict[str, str]] = {}
    for origin_data in state.get("origins") or []:
        origin = str(origin_data.get("origin") or "")
        if not origin:
            continue
        items: dict[str, str] = {}
        for item in origin_data.get("localStorage") or []:
            name = item.get("name")
            if name is None:
                continue
            items[str(name)] = str(item.get("value") or "")
        if items:
            origins_payload[origin] = items

    if not origins_payload:
        return ""

    payload = json.dumps(origins_payload, ensure_ascii=False)
    return f"""
(() => {{
    const authOrigins = {payload};
    const items = authOrigins[window.location.origin];
    if (!items) return;
    for (const [key, value] of Object.entries(items)) {{
        try {{ window.localStorage.setItem(key, value); }} catch (_) {{}}
    }}
}})();
""".strip()


async def apply_storage_state_to_context(context, state: dict) -> tuple[int, int]:
    cookies = [cookie for cookie in (state.get("cookies") or []) if isinstance(cookie, dict)]
    if cookies:
        await context.add_cookies(cookies)

    script = build_storage_init_script(state)
    origin_count = len(state.get("origins") or [])
    if script:
        await context.add_init_script(script)

    return len(cookies), origin_count

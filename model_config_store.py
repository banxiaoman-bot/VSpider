"""Server-side persistence for the frontend model configuration (model_config.json).

Spec: docs/superpowers/specs/2026-06-22-model-config-server-persistence-design.md

单套全局配置，落盘在项目根（与 .env 同级）。方案 B：永不写 .env。
apiKey 读取走 masked_config（永不回明文）；save 时空 apiKey 保留旧值。

resolve_vlm_options 只负责 form > json 两层；.env 第三层由 start_batch 下游
config.py 兜底（spec §5.3「现状不变」）。文件不存在 => 不注入 json（等价现状）。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODEL_CONFIG_PATH = Path(
    os.getenv("VSPIDER_MODEL_CONFIG_PATH", "").strip()
    or (Path(__file__).resolve().parent / "model_config.json")
)


def _empty_skeleton() -> dict[str, Any]:
    return {
        "version": 1,
        "vlm": {
            "base_url": "",
            "api_key": "",
            "model": "",
            "model_type": "vl",
            "temperature": 0.1,
            "max_tokens": 4096,
        },
        "semantic": {"base_url": "", "api_key": "", "model": ""},
        "updated_at": "",
    }


def load_model_config() -> dict[str, Any]:
    """Read config; return empty skeleton on missing/corrupt (never raise)."""
    skel = _empty_skeleton()
    try:
        data = json.loads(MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return skel
    if not isinstance(data, dict):
        return skel
    for section in ("vlm", "semantic"):
        sec = data.get(section)
        if isinstance(sec, dict):
            skel[section].update({k: v for k, v in sec.items() if k in skel[section]})
    if isinstance(data.get("version"), int):
        skel["version"] = data["version"]
    if isinstance(data.get("updated_at"), str):
        skel["updated_at"] = data["updated_at"]
    return skel


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".model_config_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_model_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Overwrite config from *payload* ({"vlm":{...},"semantic":{...}}; subset ok).

    Blank api_key preserves the existing value. Returns the persisted full config.
    """
    current = load_model_config()
    payload = payload if isinstance(payload, dict) else {}
    for section in ("vlm", "semantic"):
        incoming = payload.get(section)
        if not isinstance(incoming, dict):
            continue
        for key, value in incoming.items():
            if key not in current[section]:
                continue
            if key == "api_key" and (value is None or str(value).strip() == ""):
                continue  # preserve existing key when blank
            current[section][key] = value
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(MODEL_CONFIG_PATH, json.dumps(current, ensure_ascii=False, indent=2))
    return current


def _mask_key(key: Any) -> str:
    key = str(key or "")
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


def masked_config() -> dict[str, Any]:
    """Config with api_key -> mask + has_api_key flag; never any plaintext key."""
    cfg = load_model_config()
    for section in ("vlm", "semantic"):
        raw_key = cfg[section].get("api_key", "")
        cfg[section]["has_api_key"] = bool(str(raw_key).strip())
        cfg[section]["api_key"] = _mask_key(raw_key)
    return cfg


def resolve_vlm_options(form: dict[str, Any]) -> dict[str, Any]:
    """Merge precedence form > json for vlm_options keys; .env left to downstream.

    *form*: the already-filtered vlm_options from start_batch (non-empty Form only).
    Missing config file => return cleaned form (no json injection). Blanks dropped
    so downstream config.py (.env) still applies.
    """
    form = dict(form) if isinstance(form, dict) else {}
    if not MODEL_CONFIG_PATH.exists():
        return {k: v for k, v in form.items() if v not in ("", None)}
    cfg = load_model_config()
    vlm = cfg.get("vlm", {})
    sem = cfg.get("semantic", {})
    json_layer = {
        "model": vlm.get("model", ""),
        "model_type": vlm.get("model_type", ""),
        "base_url": vlm.get("base_url", ""),
        "api_key": vlm.get("api_key", ""),
        "temperature": vlm.get("temperature", None),
        "max_tokens": vlm.get("max_tokens", None),
        "semantic_model": sem.get("model", ""),
        "semantic_base_url": sem.get("base_url", ""),
        "semantic_api_key": sem.get("api_key", ""),
    }
    resolved = dict(form)
    for key, json_val in json_layer.items():
        if resolved.get(key) in ("", None) and json_val not in ("", None):
            resolved[key] = json_val
    return {k: v for k, v in resolved.items() if v not in ("", None)}

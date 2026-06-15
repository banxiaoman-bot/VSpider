"""CLI configuration and runtime override utilities (extracted from main.py).

Functions for runtime config overrides, CLI JSON parsing, VLM options
building, and action tool metadata resolution.
"""
import argparse
import json
import logging
import re
from typing import Any

logger = logging.getLogger("vspider.main")


def _runtime_config_module():
    try:
        from . import config as runtime_config
    except ImportError:
        import config as runtime_config
    return runtime_config


def _apply_runtime_overrides(args) -> None:
    """Apply runtime config overrides from CLI arguments and natural-language constraints."""
    config = _runtime_config_module()

    config.BROWSER_USER_DATA_DIR = args.user_data_dir
    if hasattr(args, "auth_profiles") and args.auth_profiles is not None:
        config.AUTH_PROFILES = args.auth_profiles

    override_text = "\n".join(
        part for part in (args.constraints, args.context, args.goal, args.output) if part
    )
    viewport_match = re.search(r"(\d{3,4})\s*[xX]\s*(\d{3,4})", override_text)
    if viewport_match:
        width = int(viewport_match.group(1))
        height = int(viewport_match.group(2))
        if width >= 800 and height >= 600:
            config.VIEWPORT_WIDTH = width
            config.VIEWPORT_HEIGHT = height
            logger.info(f"[VIEWPORT] Runtime override from prompt: {width}x{height}")


def _parse_cli_json_object(parser: argparse.ArgumentParser, raw: str, option_name: str) -> dict:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        parser.error(f"{option_name} must be valid JSON: {exc}")
    if not isinstance(parsed, dict):
        parser.error(f"{option_name} must be a JSON object")
    return parsed


def _build_cli_run_constraints(parser: argparse.ArgumentParser, args: argparse.Namespace) -> dict | None:
    constraints = _parse_cli_json_object(
        parser,
        getattr(args, "run_constraints_json", ""),
        "--run-constraints-json",
    )
    if bool(getattr(args, "resume", False)):
        constraints["resume"] = True
    return constraints or None


def _build_cli_vlm_options(args: argparse.Namespace) -> dict | None:
    options: dict[str, Any] = {}
    field_map = {
        "vlm_model": "model",
        "semantic_model": "semantic_model",
        "vlm_model_type": "model_type",
        "vlm_base_url": "base_url",
        "vlm_api_key": "api_key",
        "semantic_base_url": "semantic_base_url",
        "semantic_api_key": "semantic_api_key",
    }
    for attr, key in field_map.items():
        value = getattr(args, attr, "")
        if isinstance(value, str):
            value = value.strip()
        if value not in ("", None):
            options[key] = value
    if getattr(args, "vlm_temperature", None) is not None:
        options["temperature"] = float(args.vlm_temperature)
    if getattr(args, "vlm_max_tokens", None) is not None:
        options["max_tokens"] = int(args.vlm_max_tokens)
    return options or None


def _resolve_action_tool_metadata(
    action_registry,
    action_name: str,
    *,
    goal: str = "",
    selected_tools: list[dict] | None = None,
) -> dict[str, Any] | None:
    """Resolve safe event metadata for an executed action.

    Exact tool actions keep their metadata directly. Generic aliases like click
    are only labeled when that tool was actually selected for the current goal,
    which avoids auth/profile prompt noise leaking unrelated tool labels into
    normal browser actions.
    """
    tool = action_registry.resolve_for_action(action_name, goal=goal)
    if not tool:
        return None

    normalized_action = str(action_name or "").strip().lower()
    normalized_tool_name = str(tool.get("name") or "").strip().lower()
    if normalized_action != normalized_tool_name:
        selected_names = {
            str(item.get("name") or "").strip().lower()
            for item in (selected_tools or [])
            if isinstance(item, dict)
        }
        if normalized_tool_name not in selected_names:
            return None

    return {
        "name": tool.get("name"),
        "capability": tool.get("capability"),
        "evidence": tool.get("evidence") or [],
        "risk": tool.get("risk") or "",
    }



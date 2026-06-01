"""Small debug CLI for inspecting browser state without running the agent loop."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

try:
    from .action_registry import build_default_action_registry
    from .browser_env import BrowserEnv
    from .browser_state import BrowserStateSnapshot
    from .config import SCREENSHOT_DIR
    from .perception.targeted import dumps_probe
    from .perception.targeted import format_probe_text
    from .perception.targeted import probe_page
except ImportError:
    from action_registry import build_default_action_registry
    from browser_env import BrowserEnv
    from browser_state import BrowserStateSnapshot
    from config import SCREENSHOT_DIR
    from perception.targeted import dumps_probe
    from perception.targeted import format_probe_text
    from perception.targeted import probe_page


def _clip(text: Any, limit: int = 180) -> str:
    value = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    return value if len(value) <= limit else value[:limit] + "..."


def _format_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _format_som_element(el: dict[str, Any]) -> dict[str, Any]:
    rect = el.get("rect") if isinstance(el.get("rect"), dict) else {}
    return {
        "id": el.get("id"),
        "role": el.get("role") or el.get("tag") or "",
        "name": _clip(el.get("name") or el.get("text") or "", 140),
        "state": _clip(el.get("state") or "", 80),
        "input_desc": _clip(el.get("inputDesc") or "", 140),
        "x": rect.get("x"),
        "y": rect.get("y"),
        "width": rect.get("width"),
        "height": rect.get("height"),
    }


def _summarize_som_elements(
    elements: list[dict[str, Any]],
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return [_format_som_element(el) for el in elements[: max(1, int(limit or 1))]]


def _print_clickable(elements: list[dict[str, Any]], *, limit: int = 30) -> None:
    for item in _summarize_som_elements(elements, limit=limit):
        label = item["name"] or item["input_desc"] or item["state"]
        rect = ""
        if item.get("x") is not None and item.get("y") is not None:
            rect = f" @ ({item.get('x')}, {item.get('y')})"
        print(f"@e{item['id']} [{item['role']}] {_clip(label, 120)}{rect}")


async def _dom_shape(browser: BrowserEnv) -> dict[str, Any]:
    page = await browser._ensure_active_page(reason="debug cli dom shape")
    if not page:
        return {}
    try:
        return await page.evaluate(
            """() => ({
                tables: document.querySelectorAll('table').length,
                forms: document.querySelectorAll('form').length,
                inputs: document.querySelectorAll('input, textarea, select').length,
                buttons: document.querySelectorAll('button, [role=button], input[type=button], input[type=submit]').length,
                links: document.querySelectorAll('a[href]').length,
                body_text_length: (document.body?.innerText || '').length
            })"""
        )
    except Exception:
        return {}


async def _open_and_mark(url: str, *, step: int = 0) -> tuple[BrowserEnv, str, str]:
    browser = BrowserEnv()
    try:
        await browser.start(url)
        screenshot_b64, input_desc = await browser.mark_and_screenshot(step=step)
        return browser, screenshot_b64, input_desc
    except Exception:
        await browser.close()
        raise


async def _cmd_state(args: argparse.Namespace) -> int:
    browser, _screenshot_b64, _input_desc = await _open_and_mark(args.url, step=args.step)
    try:
        ax_tree = ""
        if not args.no_ax:
            ax_tree = await browser.extract_accessibility_tree()
        screenshot_path = str(Path(SCREENSHOT_DIR) / f"step_{args.step:02d}.png")
        snapshot = await BrowserStateSnapshot.from_browser(
            browser,
            step=args.step,
            screenshot_path=screenshot_path,
            ax_tree_text=ax_tree,
            dom_shape=await _dom_shape(browser),
            last_action_result=getattr(browser, "_last_action_result", None),
        )
        payload = snapshot.to_dict()
        if args.json:
            print(_format_json(payload))
        else:
            print(f"URL: {payload['url']}")
            print(f"Title: {payload['title']}")
            print(f"Screenshot: {payload['screenshot_path']}")
            print(f"Interactive: {payload['interactive_count']}")
            print(f"AX lines: {payload['ax_line_count']}")
            print(f"DOM shape: {_format_json(payload['dom_shape'])}")
            if payload.get("visible_text_excerpt"):
                print(f"Visible text: {_clip(payload['visible_text_excerpt'], args.text_limit)}")
        return 0
    finally:
        await browser.close()


async def _cmd_clickable(args: argparse.Namespace) -> int:
    browser, _screenshot_b64, _input_desc = await _open_and_mark(args.url, step=args.step)
    try:
        elements = getattr(browser, "_last_som_elements", []) or []
        if args.json:
            print(_format_json(_summarize_som_elements(elements, limit=args.limit)))
        else:
            _print_clickable(elements, limit=args.limit)
        return 0
    finally:
        await browser.close()


async def _cmd_screenshot(args: argparse.Namespace) -> int:
    browser, _screenshot_b64, _input_desc = await _open_and_mark(args.url, step=args.step)
    try:
        path = Path(SCREENSHOT_DIR) / f"step_{args.step:02d}.png"
        if args.json:
            print(_format_json({"screenshot_path": str(path), "debug_path": str(Path(SCREENSHOT_DIR) / "debug.png")}))
        else:
            print(f"Screenshot: {path}")
            print(f"Latest debug copy: {Path(SCREENSHOT_DIR) / 'debug.png'}")
        return 0
    finally:
        await browser.close()


def _cmd_tools(args: argparse.Namespace) -> int:
    registry = build_default_action_registry()
    payload = {
        "tools": registry.list_tools(include_disabled=args.include_disabled),
        "selected_tools": registry.select_for_goal(args.goal) if args.goal else [],
    }
    if args.json:
        print(_format_json(payload))
    else:
        if args.goal:
            print("Selected:")
            for tool in payload["selected_tools"]:
                print(f"- {tool['name']} ({tool['capability']}) score={tool.get('match_score')}")
            print("")
        print("Tools:")
        for tool in payload["tools"]:
            state = "enabled" if tool.get("enabled") else "disabled"
            bound = "bound" if tool.get("handler_bound") else "unbound"
            print(f"- {tool['name']} ({tool['capability']}, {state}, {bound})")
    return 0


async def _cmd_probe(args: argparse.Namespace) -> int:
    browser = BrowserEnv()
    try:
        await browser.start(args.url)
        page = await browser._ensure_active_page(reason="debug cli targeted probe")
        if not page:
            payload = {"ok": False, "error": "no active page"}
            print(_format_json(payload) if args.json else "No active page.")
            return 1
        kinds = tuple(item.strip() for item in (args.kind or []) if item.strip())
        result = await probe_page(
            page,
            goal=args.goal,
            kinds=kinds or None,
            limit=args.limit,
        )
        if args.json:
            print(dumps_probe(result))
        else:
            print(format_probe_text(result, limit=args.limit))
        return 0 if result.ok else 1
    finally:
        await browser.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m visual_web_agent.debug_cli",
        description="Inspect VSpider browser state without running the full agent.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_browser_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--url", required=True, help="Page URL to inspect.")
        p.add_argument("--step", type=int, default=0, help="Screenshot step number.")
        p.add_argument("--json", action="store_true", help="Print JSON output.")

    state = sub.add_parser("state", help="Print compact BrowserStateSnapshot.")
    add_browser_args(state)
    state.add_argument("--no-ax", action="store_true", help="Skip AX Tree extraction.")
    state.add_argument("--text-limit", type=int, default=260, help="Visible text print limit.")

    clickable = sub.add_parser("clickable", help="Print visible SoM interactive elements.")
    add_browser_args(clickable)
    clickable.add_argument("--limit", type=int, default=30, help="Maximum elements to print.")

    screenshot = sub.add_parser("screenshot", help="Capture a marked screenshot.")
    add_browser_args(screenshot)

    tools = sub.add_parser("tools", help="Inspect registered deterministic tools.")
    tools.add_argument("--goal", default="", help="Optional goal text for candidate selection.")
    tools.add_argument("--json", action="store_true", help="Print JSON output.")
    tools.add_argument("--include-disabled", action="store_true", help="Show disabled tools too.")

    probe = sub.add_parser("probe", help="Run targeted local perception for a goal.")
    probe.add_argument("--url", required=True, help="Page URL to inspect.")
    probe.add_argument("--goal", required=True, help="Task goal or element intent.")
    probe.add_argument(
        "--kind",
        action="append",
        choices=["input", "button", "link", "table", "dialog"],
        default=[],
        help="Limit probe to a candidate kind; can be repeated.",
    )
    probe.add_argument("--limit", type=int, default=20, help="Maximum candidates to print.")
    probe.add_argument("--json", action="store_true", help="Print JSON output.")

    return parser


async def _amain(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "state":
        return await _cmd_state(args)
    if args.command == "clickable":
        return await _cmd_clickable(args)
    if args.command == "screenshot":
        return await _cmd_screenshot(args)
    if args.command == "tools":
        return _cmd_tools(args)
    if args.command == "probe":
        return await _cmd_probe(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())

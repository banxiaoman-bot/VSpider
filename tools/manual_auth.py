"""
Manual auth profile recorder for VSpider.

Usage:
    python tools/manual_auth.py --url https://www.bilibili.com --profile bilibili_default

The script opens a visible Chromium window. Log in manually, then return to the
terminal and press Enter. The resulting Playwright storage_state is saved to
.auth/<profile>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _sanitize_profile_name(value: str) -> str:
    name = (value or "").strip()
    if not name:
        raise ValueError("--profile is required")
    if name.endswith(".json"):
        name = name[:-5]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._-")
    if not name:
        raise ValueError("--profile contains no usable filename characters")
    return name


def _default_profile_name(url: str) -> str:
    host = urlparse(url).hostname or "site"
    host = host.lower()
    for prefix in ("www.", "passport.", "login."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    base = host.split(".")[0] if host else "site"
    return _sanitize_profile_name(f"{base}_default")


async def _run(args: argparse.Namespace) -> None:
    profile = _sanitize_profile_name(args.profile or _default_profile_name(args.url))
    auth_dir = Path(args.auth_dir).resolve()
    auth_dir.mkdir(parents=True, exist_ok=True)
    output_path = auth_dir / f"{profile}.json"

    user_data_dir = Path(args.user_data_dir).resolve() if args.user_data_dir else None
    if user_data_dir:
        user_data_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        if user_data_dir:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=False,
                viewport={"width": args.width, "height": args.height},
                ignore_https_errors=True,
                accept_downloads=True,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            browser = None
        else:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(
                viewport={"width": args.width, "height": args.height},
                ignore_https_errors=True,
            )
            page = await context.new_page()

        await page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout)
        print()
        print(f"[manual_auth] Opened: {args.url}")
        print("[manual_auth] Complete login manually in the browser window.")
        print("[manual_auth] After the page shows the logged-in state, press Enter here to save.")
        await asyncio.to_thread(input)

        state = await context.storage_state()
        output_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"[manual_auth] Saved {len(state.get('cookies') or [])} cookies and "
            f"{len(state.get('origins') or [])} origins -> {output_path}"
        )

        await context.close()
        if browser:
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record a Playwright storage_state auth profile for VSpider.",
    )
    parser.add_argument("--url", required=True, help="Site URL to open for manual login.")
    parser.add_argument(
        "--profile",
        default="",
        help="Profile name to save under .auth/<profile>.json. Default: domain_default.",
    )
    parser.add_argument(
        "--auth-dir",
        default=str(_project_root() / ".auth"),
        help="Directory for auth profiles. Default: project .auth/",
    )
    parser.add_argument(
        "--user-data-dir",
        default="",
        help="Optional temporary persistent browser profile for the manual login window.",
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout", type=int, default=60000)
    args = parser.parse_args()

    if sys.platform == "win32" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

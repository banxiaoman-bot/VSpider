"""VLM connectivity & schema sanity diagnostic.

Run this when the agent dies with "VLM 请求最终失败" or "VLM API consecutive
failures" — instead of guessing whether it's auth / network / rate-limit /
schema, this script bypasses the whole agent stack and tries the raw
DashScope (or other OpenAI-compatible) endpoint with the same client setup
the agent uses.

Usage:
    python diagnose_vlm.py
    python diagnose_vlm.py --text-only          # skip image probe
    python diagnose_vlm.py --skip-structured    # skip json_schema probe

Exit codes:
    0  everything OK
    1  one or more probes failed (see stderr)

The script never modifies state — it only sends two small requests and
prints whatever it gets back, including full HTTP error bodies if any.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
from pathlib import Path


def _load_dotenv_if_present() -> None:
    """Cheap .env loader so this script behaves like the agent."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception as e:
        print(f"⚠️  .env load failed (continuing): {e}", file=sys.stderr)


_load_dotenv_if_present()


def _print_config(cfg: dict) -> None:
    print("─── VLM configuration ─────────────────────────────")
    for k, v in cfg.items():
        if "KEY" in k.upper() and v:
            masked = v[:6] + "..." + v[-4:] if len(v) > 12 else "<short>"
            print(f"  {k}: {masked}  ({len(v)} chars)")
        else:
            print(f"  {k}: {v!r}")
    print()


async def _probe_text(client, model: str) -> tuple[bool, str]:
    """Tiny text-only chat completion — auth + base_url + model existence."""
    try:
        r = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Reply with the single word OK."},
                {"role": "user", "content": "Say OK."},
            ],
            max_tokens=8,
            temperature=0,
        )
        msg = (r.choices[0].message.content or "").strip()
        return True, msg or "<empty>"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def _probe_structured(client, model: str) -> tuple[bool, str]:
    """Test json_schema response_format with the agent's actual batch schema."""
    try:
        from visual_web_agent.vlm_client import _VSPIDER_BATCH_SCHEMA
    except Exception as e:
        return False, f"could not import VSPIDER_BATCH_SCHEMA: {e}"
    try:
        r = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Output one action: action=done, target_id=0, all other "
                        "string fields empty. JSON only."
                    ),
                },
                {"role": "user", "content": "Emit the action."},
            ],
            max_tokens=300,
            temperature=0,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "VSpiderActionBatch",
                    "strict": False,
                    "schema": _VSPIDER_BATCH_SCHEMA,
                },
            },
        )
        raw = (r.choices[0].message.content or "").strip()
        try:
            parsed = json.loads(raw)
            return True, json.dumps(parsed, ensure_ascii=False)[:200]
        except Exception:
            return True, f"<not JSON> {raw[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def _probe_image(client, model: str) -> tuple[bool, str]:
    """Tiny image probe — 1×1 PNG. Catches content-moderation / format issues."""
    # 1×1 transparent PNG, base64-encoded
    tiny_png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    try:
        r = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{tiny_png_b64}"},
                        },
                        {"type": "text", "text": "Describe this image in <= 5 words."},
                    ],
                }
            ],
            max_tokens=32,
            temperature=0,
        )
        msg = (r.choices[0].message.content or "").strip()
        return True, msg or "<empty>"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def main(args) -> int:
    try:
        import httpx
        from openai import AsyncOpenAI
        from visual_web_agent import config as runtime_config
    except Exception as e:
        print(f"❌ Import failure (env not set up?): {e}", file=sys.stderr)
        traceback.print_exc()
        return 1

    cfg = {
        "VLM_API_BASE": getattr(runtime_config, "VLM_API_BASE", "<missing>"),
        "VLM_API_KEY": getattr(runtime_config, "VLM_API_KEY", "<missing>"),
        "VLM_MODEL_NAME": getattr(runtime_config, "VLM_MODEL_NAME", "<missing>"),
        "VLM_TIMEOUT": getattr(runtime_config, "VLM_TIMEOUT", "<missing>"),
        "VLM_TEXT_ONLY": getattr(runtime_config, "VLM_TEXT_ONLY", "<missing>"),
    }
    _print_config(cfg)

    if not cfg["VLM_API_KEY"] or cfg["VLM_API_KEY"] in ("EMPTY", "<missing>"):
        print("❌ VLM_API_KEY is missing or placeholder. Edit .env.", file=sys.stderr)
        return 1

    http_client = httpx.AsyncClient(timeout=httpx.Timeout(cfg["VLM_TIMEOUT"] or 60))
    client = AsyncOpenAI(
        api_key=cfg["VLM_API_KEY"],
        base_url=cfg["VLM_API_BASE"],
        timeout=cfg["VLM_TIMEOUT"] or 60,
        http_client=http_client,
    )

    results: list[tuple[str, bool, str]] = []

    print("─── 1. Text probe ────────────────────────────────")
    ok, msg = await _probe_text(client, cfg["VLM_MODEL_NAME"])
    print(f"  {'✅' if ok else '❌'} {msg}\n")
    results.append(("text", ok, msg))

    if not args.skip_structured:
        print("─── 2. Structured-output (json_schema) probe ─────")
        ok, msg = await _probe_structured(client, cfg["VLM_MODEL_NAME"])
        print(f"  {'✅' if ok else '❌'} {msg}\n")
        results.append(("structured", ok, msg))

    if not args.text_only:
        print("─── 3. Image-input probe ─────────────────────────")
        ok, msg = await _probe_image(client, cfg["VLM_MODEL_NAME"])
        print(f"  {'✅' if ok else '❌'} {msg}\n")
        results.append(("image", ok, msg))

    await http_client.aclose()

    print("─── Summary ──────────────────────────────────────")
    any_fail = False
    for name, ok, _ in results:
        mark = "✅" if ok else "❌"
        print(f"  {mark}  {name}")
        if not ok:
            any_fail = True

    if any_fail:
        print(
            "\n💡 At least one probe failed. Common causes:\n"
            "   • API key wrong / revoked / wrong vendor → check .env VLM_API_KEY\n"
            "   • VLM_API_BASE points at the wrong endpoint (DashScope vs OpenAI vs local)\n"
            "   • Model name doesn't exist in your account → check VLM_MODEL_NAME\n"
            "   • Rate-limited (HTTP 429) → wait or switch keys\n"
            "   • Image probe-only fail → content moderation tripping (data_inspection_failed)\n"
            "   • Structured-only fail → backend doesn't support json_schema → set VLM_TEXT_ONLY=1 or change model\n"
        )
        return 1
    print("\n🎉 All probes passed. Agent VLM stack should work.")
    return 0


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--text-only", action="store_true",
                   help="skip the image probe (use when on text-only models)")
    p.add_argument("--skip-structured", action="store_true",
                   help="skip the json_schema probe")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))

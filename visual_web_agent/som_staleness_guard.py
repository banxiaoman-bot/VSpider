"""Stale SoM reference guard: detect VLM citing a `@eN` target_id across pages.

SoM (Set-of-Mark) red-box numbers are reassigned **per page** at screenshot
time. If the VLM cites ``target_id=21`` after a navigation, the element at
``[data-som-id=21]`` on the new page is almost certainly a different element
than the one the VLM is reasoning about — yet the action will execute happily
on whatever happens to be tagged 21.

Failure mode observed in Bing multi-tab runs:
  Step N    @e21 = "first non-ad result"   (URL=bing.com)
  Step N+4  @e21 = course tile             (URL=aidaxue.com)
  VLM keeps emitting ``click target_id=21`` because thought history says
  "@e21 is the bing result", but the click lands on aidaxue's course tile.

Detection is structural: if the same ``target_id`` was emitted on a different
normalized URL within the last K decisions, refuse this one and force the
VLM to re-read the current screenshot's numbering.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


_ELEMENT_TARGET_ACTIONS = frozenset(
    {
        "click",
        "click_text",
        "click_new_tab",
        "type",
        "hover",
        "select",
        "upload",
        "press_key",
        "find_text",
        "next_page",
        "save_to_memory",
        "download_image",
        "fetch_link_content",
        "fetch_links_batch",
    }
)


def _default_url_normalizer(url: str) -> str:
    """Strip query and fragment so timestamp nonces don't fragment the key."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
    except Exception:
        return url[:120]


def detect_stale_som_reference(
    head_decision: dict[str, Any],
    recent_actions: Iterable[tuple],
    current_url: str,
    *,
    window: int = 4,
    url_normalizer: Callable[[str], str] | None = None,
) -> str | None:
    """Return a warning string when the head decision's ``target_id`` was
    used recently on a different page.

    Args:
        head_decision: decision dict with ``action`` and ``target_id`` keys.
        recent_actions: iterable of ``(action, target_id, point_bucket, url)``
            tuples (the shape main.py's ``_last_actions`` uses). Older entries
            beyond ``window`` are ignored.
        current_url: URL the head decision is about to act on.
        window: how many recent action records to consider.
        url_normalizer: optional callable to canonicalize URLs (default strips
            query/fragment).

    Returns:
        Warning text suitable for ``vlm.inject_error_feedback`` if stale,
        otherwise ``None``.
    """
    if not isinstance(head_decision, dict):
        return None
    action = str(head_decision.get("action") or "")
    if action not in _ELEMENT_TARGET_ACTIONS:
        return None
    target_ids: list[int] = []
    try:
        target_id = int(head_decision.get("target_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    if target_id > 0:
        target_ids.append(target_id)
    if action == "fetch_links_batch":
        target_ids.extend(_extract_batch_target_ids(head_decision.get("type_value")))
    target_ids = list(dict.fromkeys(tid for tid in target_ids if tid > 0))
    if not target_ids:
        return None

    normalize = url_normalizer or _default_url_normalizer
    current_key = normalize(current_url)
    if not current_key:
        return None

    prior_keys: set[str] = set()
    history = list(recent_actions)
    if window > 0:
        history = history[-window:]
    for rec in history:
        if not rec or len(rec) < 4:
            continue
        try:
            rec_tid = int(rec[1])
        except (TypeError, ValueError):
            continue
        if rec_tid not in target_ids:
            continue
        rec_url = str(rec[3] or "")
        if not rec_url:
            continue
        prior_key = normalize(rec_url)
        if prior_key and prior_key != current_key:
            prior_keys.add(prior_key)

    if not prior_keys:
        return None

    short_priors = sorted(k[:80] for k in prior_keys)
    logger.info(
        "[SOM STALE] target_ids=%s reused across URLs: priors=%s current=%s",
        target_ids,
        short_priors,
        current_key[:80],
    )
    target_label = ",".join(str(tid) for tid in target_ids[:8])
    return (
        f"⚠️ [SOM STALE GUARD] target_id={target_label} 在最近 {window} 步内"
        f"被在不同 URL 上使用过：\n"
        f"  prior: {short_priors}\n"
        f"  current: {current_key[:160]}\n"
        "🚨 @eN 红框编号是【按当前页面】重新分配的，跨页复用 = 不同元素。\n"
        "请只参考【当前截图】的红框编号；如果你要点的元素在新页面里没有编号，"
        "改用 click_text / find_text 按文字定位，或先 scroll / wait 让红框刷新。"
    )


def _extract_batch_target_ids(type_value: Any) -> list[int]:
    text = str(type_value or "").strip()
    if not text:
        return []
    raw: Any = None
    if text[0] in "[{":
        try:
            raw = json.loads(text)
        except Exception:
            raw = None
    candidates: list[Any] = []
    if isinstance(raw, dict):
        for key in ("target_ids", "ids"):
            value = raw.get(key)
            if isinstance(value, (list, tuple)):
                candidates.extend(value)
            elif value is not None:
                candidates.extend(re.findall(r"@?e?(\d+)", str(value), flags=re.I))
    elif isinstance(raw, list):
        candidates.extend(raw)
    else:
        candidates.extend(re.findall(r"@?e?(\d+)", text, flags=re.I))

    out: list[int] = []
    seen: set[int] = set()
    for item in candidates:
        try:
            if isinstance(item, str):
                match = re.search(r"\d+", item)
                if not match:
                    continue
                tid = int(match.group(0))
            else:
                tid = int(item)
        except (TypeError, ValueError):
            continue
        if tid > 0 and tid not in seen:
            out.append(tid)
            seen.add(tid)
    return out

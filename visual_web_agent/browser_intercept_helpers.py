"""
Browser helper functions extracted from browser_env.py

Pure functions that do not require BrowserEnv instance state.
"""

import hashlib
import json
import re


def _format_som_element(el: dict) -> str:
    """
    将 SoM v6 单条元素字典格式化为标准化描述行。

    输出格式：
      [ID: 15] Role: button, Name: "提交表单", State: disabled | type=submit | 关联信息: ...

    兼容 v5：如果缺少 role/name/state 字段，回退到 tag+text 格式。
    """
    eid = el.get("id", "?")
    role = (el.get("role") or el.get("tag") or "?").strip()
    name = (el.get("name") or el.get("text") or "").strip()
    state_str = (el.get("state") or "").strip()

    # 主体行：[ID: N] Role: xxx, Name: "yyy", State: zzz
    parts = [f"Role: {role}"]
    if name:
        parts.append(f'Name: "{name[:80]}"')
    if state_str:
        parts.append(f"State: {state_str}")

    line = f"[ID: {eid}] " + ", ".join(parts)

    # 附加 inputDesc
    desc = (el.get("inputDesc") or "").strip()
    if desc:
        line += f" | {desc}"

    # 附加 parentContext
    parent_ctx = (el.get("parentContext") or "").strip()
    if parent_ctx:
        line += f" | 关联信息: {parent_ctx}"

    # 风险提示：语音/拍照/扫码等辅助入口
    risk_text = " ".join(part for part in (name, desc, parent_ctx) if part)
    if re.search(
        r"语音|麦克风|microphone|voice|camera|相机|拍照|图片搜索|以图搜图|扫码|扫一扫|lens",
        risk_text,
        flags=re.IGNORECASE,
    ):
        line += (
            " | 风险提示: 语音/拍照/扫码等辅助入口，"
            "除非目标明确要求，否则不要优先点击"
        )

    return line



def screenshot_looks_visually_blank(screenshot_bytes: bytes) -> tuple[bool, str]:
    """Heuristic visual blank detector for pages whose DOM exists but paint is white.

    It intentionally detects only extreme cases. Many real sites use white
    backgrounds, so this is combined with DOM text checks before reloading.
    """
    try:
        import io
        from PIL import Image
    except Exception:
        return False, "Pillow unavailable"

    try:
        with Image.open(io.BytesIO(screenshot_bytes)) as img:
            rgb = img.convert("RGB")
            width, height = rgb.size
            if width <= 0 or height <= 0:
                return False, "invalid image size"

            # Sample every N pixels instead of scanning the whole bitmap.
            step = max(1, int(((width * height) / 12000) ** 0.5))
            total = 0
            near_white = 0
            dark_or_colored = 0
            content_total = 0
            content_near_white = 0
            content_signal = 0
            content_y_start = min(height - 1, max(80, int(height * 0.18)))
            # Ignore the first 80px less aggressively by still sampling it;
            # navigation bars are useful but should not hide a blank body.
            for y in range(0, height, step):
                for x in range(0, width, step):
                    r, g, b = rgb.getpixel((x, y))
                    total += 1
                    if r >= 245 and g >= 245 and b >= 245:
                        near_white += 1
                    if min(r, g, b) < 180 or (max(r, g, b) - min(r, g, b)) > 35:
                        dark_or_colored += 1
                    if y >= content_y_start and x < width - 24:
                        content_total += 1
                        if r >= 245 and g >= 245 and b >= 245:
                            content_near_white += 1
                        if min(r, g, b) < 180 or (max(r, g, b) - min(r, g, b)) > 35:
                            content_signal += 1

            if total == 0:
                return False, "empty sample"
            white_ratio = near_white / total
            signal_ratio = dark_or_colored / total
            content_white_ratio = (
                content_near_white / content_total if content_total else 0.0
            )
            content_signal_ratio = (
                content_signal / content_total if content_total else 1.0
            )
            is_blank = (
                (white_ratio >= 0.78 and signal_ratio <= 0.12)
                or (content_white_ratio >= 0.88 and content_signal_ratio <= 0.06)
            )
            return (
                is_blank,
                f"white_ratio={white_ratio:.2f}, signal_ratio={signal_ratio:.2f}, "
                f"content_white={content_white_ratio:.2f}, "
                f"content_signal={content_signal_ratio:.2f}, sample={total}",
            )
    except Exception as e:
        return False, f"visual blank probe failed: {e}"


def extract_data_list(json_body, threshold: int = 5) -> list[dict] | None:
    """
    启发式探测 JSON 响应中的数据列表。

    检查策略（按优先级）：
    1. json_body 本身是 list[dict] 且长度 >= 阈值
    2. json_body 是 dict，遍历所有值，找第一个符合条件的 list[dict]
    3. 递归检查常见嵌套路径：data.rows, data.list, data.records, result.data 等
    """
    threshold = threshold

    # 情况 1：顶层就是列表
    if isinstance(json_body, list):
        if len(json_body) >= threshold and isinstance(json_body[0], dict):
            return json_body
        return None

    # 情况 2：dict，遍历所有值
    if isinstance(json_body, dict):
        # 优先检查常见键名
        priority_keys = [
            "data", "rows", "list", "records", "items",
            "result", "results", "content", "details",
        ]
        # 先查优先键
        for key in priority_keys:
            val = json_body.get(key)
            if isinstance(val, list) and len(val) >= threshold:
                if val and isinstance(val[0], dict):
                    return val
            # 可能嵌套一层：data.rows, data.list
            if isinstance(val, dict):
                for sub_key in priority_keys:
                    sub_val = val.get(sub_key)
                    if isinstance(sub_val, list) and len(sub_val) >= threshold:
                        if sub_val and isinstance(sub_val[0], dict):
                            return sub_val

        # 兜底：遍历所有值
        for key, val in json_body.items():
            if key.startswith("_"):
                continue
            if isinstance(val, list) and len(val) >= threshold:
                if val and isinstance(val[0], dict):
                    return val

    return None


def score_intercept_candidate(url: str, rows: list[dict], *, min_list_size: int, schema_fingerprints: set, endpoint_scores: dict) -> tuple[int, str]:
    fingerprint = schema_fingerprint(rows)
    if not fingerprint:
        return 0, ""

    score = 0
    row_count = len(rows)
    keys = set(fingerprint.split("|"))
    lower_url = (url or "").lower()

    if row_count >= min_list_size:
        score += 2
    if row_count >= 20:
        score += 2
    if row_count >= 50:
        score += 1

    url_markers = (
        "api", "ajax", "xhr", "search", "query", "list", "page", "record",
        "item", "order", "table", "data", "result", "export",
    )
    if any(marker in lower_url for marker in url_markers):
        score += 2

    business_keys = {
        "id", "uuid", "uid", "url", "link", "title", "name", "price", "amount",
        "date", "time", "status", "type", "code", "no", "number", "order_id",
        "created_at", "updated_at",
    }
    if keys & business_keys:
        score += 2
    if len(keys) >= 3:
        score += 1

    if fingerprint in schema_fingerprints:
        score += 5

    noise_keys = {
        "children", "routes", "menus", "permissions", "locale", "i18n",
        "config", "settings", "schema",
    }
    if keys & noise_keys and row_count < 20:
        score -= 3

    endpoint_key = re.sub(
        r"([?&](page|current|offset|limit|size|pageSize)=)[^&]+",
        r"\1*",
        lower_url,
    )
    endpoint_scores[endpoint_key] = max(
        score, endpoint_scores.get(endpoint_key, 0)
    )
    return score, fingerprint


def stable_json_hash(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def schema_fingerprint(rows: list[dict]) -> str:
    key_set: set[str] = set()
    for row in rows[:5]:
        if isinstance(row, dict):
            key_set.update(str(k) for k in row.keys())
    return "|".join(sorted(key_set))


def row_dedup_key(row: dict, unique_key) -> str:
    if unique_key:
        keys = (
            [unique_key]
            if isinstance(unique_key, str)
            else list(unique_key)
        )
        values = [row.get(k) for k in keys if k in row and row.get(k) not in (None, "")]
        if values:
            return "key:" + stable_json_hash(values)
    return "hash:" + stable_json_hash(row)


def flatten_ax_tree_for_extract(
    node: dict, out: list[str], depth: int = 0, max_depth: int = 15,
) -> None:
    """递归扁平化 AX Tree 节点为纯语义文本行（专用于数据提取）。"""
    if not isinstance(node, dict) or depth > max_depth:
        return

    name = (node.get("name") or "").strip()
    value = (node.get("value") or "").strip()

    text = name or value
    if text:
        line = "  " * min(depth, 4) + text[:200]
        if not out or out[-1].strip() != line.strip():
            out.append(line)

    for child in node.get("children") or []:
        flatten_ax_tree_for_extract(child, out, depth + 1, max_depth)


def flatten_ax_tree(
    node: dict, out: list[str], depth: int = 0, max_depth: int = 12,
) -> None:
    """递归扁平化 Playwright AX 快照节点为缩进文本行。"""
    if not isinstance(node, dict) or depth > max_depth:
        return

    role = (node.get("role") or "").strip()
    name = (node.get("name") or "").strip()
    value = (node.get("value") or "").strip()
    description = (node.get("description") or "").strip()

    is_noise_container = (
        role in ("RootWebArea", "generic", "none", "") and not (name or value or description)
    )

    if not is_noise_container:
        parts = [f"Role: {role or '?'}"]
        if name:
            parts.append(f'Name: "{name[:80]}"')
        if value:
            parts.append(f'Value: "{value[:40]}"')
        if description and description != name:
            parts.append(f'Desc: "{description[:60]}"')

        states: list[str] = []
        if node.get("disabled"):
            states.append("disabled")
        checked = node.get("checked")
        if checked in (True, "true", "mixed"):
            states.append(f"checked={checked}")
        expanded = node.get("expanded")
        if expanded in (True, False, "true", "false"):
            states.append(f"expanded={expanded}")
        if node.get("focused"):
            states.append("focused")
        if node.get("required"):
            states.append("required")
        if node.get("selected"):
            states.append("selected")
        if states:
            parts.append(f"State: {','.join(states)}")

        out.append("  " * depth + ", ".join(parts))

    next_depth = depth if is_noise_container else depth + 1
    for child in node.get("children") or []:
        flatten_ax_tree(child, out, next_depth, max_depth)


def dedupe_intercept_rows(rows: list[dict], unique_key, seen_keys: set) -> list[dict]:
    new_rows: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row_dedup_key(row, unique_key)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        new_rows.append(row)
    return new_rows

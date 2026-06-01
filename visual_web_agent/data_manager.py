"""
VSpider 数据管理模块

负责将 VLM 提取或网络层拦截的结构化数据保存至 Excel 文件。
支持：
- VLM extract 提取保存（save_to_excel）
- XHR/Fetch 网络拦截数据保存（save_intercepted_data）
- 追加写入 + 自动去重
- 写入前二次过滤（FilterRule）
"""

import re
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Callable

import pandas as pd

try:
    from .artifact_manager import register_artifact, resolve_artifact_path, resolve_output_path
    from .data_sanitizer import (
        TOOLTIP_INTERNAL_KEY,
        TOOLTIP_UNIQUE_KEY,
        extract_tooltip_primary_key,
    )
    from .io_contract import (
        append_manifest_item as _append_manifest_item,
        current_base_dir as _current_base_dir,
        current_run_id as _current_run_id,
    )
except ImportError:
    from artifact_manager import register_artifact, resolve_artifact_path, resolve_output_path
    from data_sanitizer import (
        TOOLTIP_INTERNAL_KEY,
        TOOLTIP_UNIQUE_KEY,
        extract_tooltip_primary_key,
    )
    from io_contract import (
        append_manifest_item as _append_manifest_item,
        current_base_dir as _current_base_dir,
        current_run_id as _current_run_id,
    )


_XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def _record_in_run_manifest(
    abs_path: str,
    *,
    rows: int,
    produced_by: str,
) -> None:
    """Best-effort: when a run is active, also write the xlsx into the
    run-scoped ``manifest.json`` so the IO contract closes for the
    structured-extraction path that still goes through legacy
    ``save_to_excel`` instead of the new ``save_artifact`` dispatcher.

    Any failure is swallowed (logged) to keep the legacy hot path safe.
    """
    rid = _current_run_id()
    if not rid:
        return
    try:
        import hashlib

        path_obj = Path(abs_path)
        if not path_obj.exists():
            return
        body = path_obj.read_bytes()
        sha = hashlib.sha256(body).hexdigest()
        _append_manifest_item(
            rid,
            kind="dataset_rows",
            path=str(path_obj),
            size=len(body),
            sha256=sha,
            mime=_XLSX_MIME,
            produced_by=produced_by,
            extra={"row_count": int(rows or 0)},
            base_dir=_current_base_dir(),
        )
    except Exception as exc:
        logger.debug("[IO CONTRACT] manifest append for %s skipped: %s", abs_path, exc)

logger = logging.getLogger("vspider.data")

# ANSI 颜色
_GREEN_BOLD = "\033[1;32m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


_SYSTEM_COLS = {"_extracted_at", "_row_hash"}


def _nested_get(row: dict, path: tuple[str, ...]) -> Any:
    cur: Any = row
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _feed_labels(row: dict) -> list[str]:
    labels: list[str] = []
    category = row.get("category")
    if isinstance(category, dict):
        labels.append(_clean_text(category.get("category_name")))
    for tag in row.get("tags") or []:
        if isinstance(tag, dict):
            labels.append(_clean_text(tag.get("tag_name")))
            labels.append(_clean_text(tag.get("tag_alias")))
    article = row.get("article_info")
    if isinstance(article, dict):
        labels.append(_clean_text(article.get("mark_content")))
    return [label for label in labels if label]


def _is_promoted_feed_row(row: dict) -> bool:
    labels = " ".join(_feed_labels(row)).lower()
    return any(
        marker in labels
        for marker in ("广告", "推广", "ad", "ads", "sponsored")
    )


def _normalize_intercept_row(row: dict) -> dict | None:
    """Flatten common feed/article API rows before Excel persistence."""
    if not isinstance(row, dict):
        return None
    source = row.get("item_info") if isinstance(row.get("item_info"), dict) else row
    ad_payload_keys = ("advertisement_info", "advert_info", "ad_info", "ads_info")
    if any(isinstance(source.get(key), dict) for key in ad_payload_keys):
        return None
    if _clean_text(source.get("advert_id") or row.get("advert_id")):
        return None
    if str(source.get("item_type") or row.get("item_type") or "").strip().lower() in {
        "ad",
        "ads",
        "advert",
        "advertisement",
        "14",
    }:
        return None
    content = source.get("content") if isinstance(source.get("content"), dict) else {}
    content_counter = (
        source.get("content_counter") if isinstance(source.get("content_counter"), dict) else {}
    )
    content_author = source.get("author") if isinstance(source.get("author"), dict) else {}
    if content:
        title = _clean_text(content.get("title") or source.get("title"))
        author_name = _clean_text(content_author.get("name") or source.get("author"))
        digg_count = content_counter.get("like")
        article_id = _clean_text(content.get("content_id") or source.get("content_id"))
        if title and (author_name or digg_count is not None):
            return {
                "title": title,
                "author": author_name,
                "digg_count": digg_count,
                "url": f"https://juejin.cn/post/{article_id}" if article_id else "",
                "article_id": article_id,
            }

    article = source.get("article_info") if isinstance(source.get("article_info"), dict) else {}
    author = source.get("author_user_info") if isinstance(source.get("author_user_info"), dict) else {}
    if article:
        if _is_promoted_feed_row(source):
            return None
        title = _clean_text(article.get("title") or source.get("title"))
        author_name = _clean_text(author.get("user_name") or source.get("author") or source.get("user_name"))
        digg_count = article.get("digg_count")
        link_url = _clean_text(article.get("link_url") or source.get("url") or source.get("link_url"))
        article_id = _clean_text(article.get("article_id") or source.get("article_id"))
        if not link_url and article_id:
            link_url = f"https://juejin.cn/post/{article_id}"
        if title and (author_name or digg_count is not None):
            tags = [
                _clean_text(tag.get("tag_name"))
                for tag in source.get("tags") or []
                if isinstance(tag, dict) and _clean_text(tag.get("tag_name"))
            ]
            return {
                "title": title,
                "author": author_name,
                "digg_count": digg_count,
                "url": link_url,
                "article_id": article_id,
                "tags": ", ".join(tags),
            }
    if {"user_name", "articles"} & set(source.keys()) and not {"title", "content", "article_info"} & set(source.keys()):
        return None
    return row


def _normalize_intercept_rows(rows: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for row in rows or []:
        item = _normalize_intercept_row(row)
        if item:
            normalized.append(item)
    return normalized


def _is_article_dataframe(df: pd.DataFrame) -> bool:
    return {"title", "author", "digg_count"}.issubset(set(map(str, df.columns)))

_COLUMN_ALIAS_SIGNATURES = {
    "rank": {
        "rank", "ranking", "no", "num", "number",
        "序号", "排名", "名次",
    },
    "title": {
        "title", "movie", "film", "product", "item", "subject",
        "标题", "名称", "电影", "商品", "项目",
    },
    "rating": {
        "score", "rating", "rate", "stars", "star", "grade",
        "评分", "得分", "分数", "评级",
    },
    "review_count": {
        "reviews", "review", "review_count", "votes", "vote", "comments",
        "comment", "comment_count", "ratings_count", "评价人数", "评价数",
        "评论数", "投票数", "人评价", "点评数",
    },
    "description": {
        "summary", "intro", "introduction", "description", "desc", "brief",
        "quote", "slogan", "tagline", "abstract", "简介", "介绍", "摘要",
        "一句话简介", "一句话", "短评", "描述",
    },
    "price": {"price", "amount", "cost", "售价", "价格", "金额"},
    "url": {"url", "link", "href", "链接", "地址"},
    # Do not canonicalize to a single output name here: "age" can be the
    # established schema in one run while "time" is used in another. The shared
    # signature lets _align_new_columns_to_existing merge later drift into the
    # first established column without forcing a domain-specific column name.
    "temporal": {
        "time", "age", "date", "created", "created_at", "published",
        "published_at", "posted", "posted_at", "updated", "updated_at",
        "时间", "日期", "发布时间", "创建时间", "更新时间",
    },
}


def _column_signature(column: str) -> str:
    normalized = re.sub(r"[\s_\-（）()]+", "", str(column or "").strip().lower())
    normalized = re.sub(r"\.\d+$", "", normalized)
    for signature, aliases in _COLUMN_ALIAS_SIGNATURES.items():
        for alias in aliases:
            alias_norm = re.sub(r"[\s_\-（）()]+", "", alias.lower())
            if normalized == alias_norm or alias_norm in normalized:
                return signature
    return normalized


def _series_text_values(series: pd.Series) -> list[str]:
    values: list[str] = []
    for value in series.dropna().tolist():
        text = str(value).strip()
        if text:
            values.append(text)
    return values


def _values_look_like_review_count(values: list[str]) -> bool:
    if not values:
        return False
    hits = 0
    for text in values:
        lower = text.lower()
        if any(marker in lower for marker in ("review", "reviews", "comment", "vote")):
            hits += 1
            continue
        if re.search(r"\d[\d,.\s]*(?:人评价|人評價|条评价|條評價|评价|評價|评论|評論|votes?)", text):
            hits += 1
            continue
        match = re.search(r"\d[\d,.\s]*", text)
        if match:
            try:
                number = float(match.group(0).replace(",", "").replace(" ", ""))
            except ValueError:
                number = 0.0
            if number > 10:
                hits += 1
    return hits >= max(1, len(values) // 2)


def _values_look_like_rating(values: list[str]) -> bool:
    if not values:
        return False
    numeric = 0
    in_range = 0
    for text in values:
        match = re.search(r"\d+(?:\.\d+)?", text)
        if not match:
            continue
        numeric += 1
        try:
            number = float(match.group(0))
        except ValueError:
            continue
        if 0 <= number <= 10:
            in_range += 1
    return numeric > 0 and in_range >= max(1, numeric // 2)


def _preferred_column_name(column: str, series: pd.Series) -> str:
    raw = str(column or "").strip() or "value"
    values = _series_text_values(series)
    signature = _column_signature(raw)

    if signature == "rating" and _values_look_like_review_count(values):
        return "review_count"
    if signature == "rating" and _values_look_like_rating(values):
        return "rating"
    if signature in {
        "rank",
        "title",
        "rating",
        "review_count",
        "description",
        "price",
        "url",
    }:
        return signature
    return raw


def _normalize_extracted_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize extracted columns and merge duplicate semantic columns."""
    if df.empty:
        return df

    normalized = pd.DataFrame(index=df.index)
    for idx, column in enumerate(list(df.columns)):
        series = df.iloc[:, idx]
        target = _preferred_column_name(str(column), series)
        if target not in normalized.columns:
            normalized[target] = series
            continue

        current = normalized[target]
        current_empty = current.isna() | (current.astype(str).str.strip() == "")
        incoming_empty = series.isna() | (series.astype(str).str.strip() == "")
        equivalent = current.astype(str) == series.astype(str)
        if (current_empty | incoming_empty | equivalent).all():
            normalized[target] = current.where(~current_empty, series)
            continue

        suffix = 2
        unique_target = f"{target}_{suffix}"
        while unique_target in normalized.columns:
            suffix += 1
            unique_target = f"{target}_{suffix}"
        normalized[unique_target] = series

    return normalized.dropna(axis=1, how="all")


def _fill_sequential_rank_if_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Fill missing rank values when the existing rank column is row-order based."""
    if df.empty or "rank" not in df.columns:
        return df

    ranks = pd.to_numeric(df["rank"], errors="coerce")
    present = ranks.dropna()
    if present.empty:
        return df

    # Only auto-fill when existing ranks agree with output row order. This keeps
    # generic tables safe while repairing common paginated ranked-list drift.
    for idx, value in present.items():
        try:
            if int(value) != int(idx) + 1:
                return df
        except Exception:
            return df

    missing = ranks.isna()
    if missing.any():
        df = df.copy()
        df.loc[missing, "rank"] = [int(idx) + 1 for idx in df.index[missing]]
    return df


def _align_new_columns_to_existing(
    df_new: pd.DataFrame,
    df_existing: pd.DataFrame,
) -> pd.DataFrame:
    """Map synonymous new columns onto existing output columns before append.

    VLM extraction can drift across pages: e.g. page 1 uses ``reviews`` and
    ``summary`` while page 2 uses ``votes`` and ``intro``. Prefer the established
    Excel schema once a file exists, instead of creating split synonym columns.
    """
    if df_new.empty or df_existing.empty:
        return df_new

    existing_cols = [c for c in df_existing.columns if c not in _SYSTEM_COLS]
    existing_by_sig: dict[str, str] = {}
    for col in existing_cols:
        existing_by_sig.setdefault(_column_signature(col), col)

    rename: dict[str, str] = {}
    for col in df_new.columns:
        if col in df_existing.columns or col in _SYSTEM_COLS:
            continue
        target = existing_by_sig.get(_column_signature(col))
        if target and target not in df_new.columns:
            rename[col] = target

    if rename:
        logger.info("[SCHEMA ALIGN] Renaming extracted columns before append: %s", rename)
        df_new = df_new.rename(columns=rename)

    return df_new


def _apply_tooltip_upsert_key(df_combined: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    before_dedup = len(df_combined)
    keys: list[str] = []
    for idx, row in df_combined.iterrows():
        row_dict = {
            key: value
            for key, value in row.to_dict().items()
            if key != TOOLTIP_INTERNAL_KEY
        }
        key = extract_tooltip_primary_key(row_dict)
        keys.append(key if key else f"row|{idx}")

    df_with_key = df_combined.copy()
    df_with_key[TOOLTIP_INTERNAL_KEY] = keys
    df_with_key = df_with_key.drop_duplicates(
        subset=[TOOLTIP_INTERNAL_KEY], keep="last"
    ).reset_index(drop=True)
    df_with_key = df_with_key.drop(columns=[TOOLTIP_INTERNAL_KEY], errors="ignore")
    return df_with_key, before_dedup - len(df_with_key)


# ========== 过滤规则定义 ==========

class FilterRule:
    """
    数据过滤规则。

    支持四种过滤模式：
    - regex: 正则匹配（字段值必须匹配正则表达式）
    - range: 数值范围（字段值必须在 [min, max] 区间内）
    - keyword: 关键词包含（字段值必须包含指定关键词）
    - custom: 自定义函数（传入行 dict，返回 True 保留）
    """

    def __init__(
        self,
        column: str = "",
        mode: str = "keyword",
        pattern: str = "",
        min_val: float = None,
        max_val: float = None,
        keywords: list[str] = None,
        custom_fn: Callable[[dict], bool] = None,
    ):
        self.column = column
        self.mode = mode
        self.pattern = pattern
        self.min_val = min_val
        self.max_val = max_val
        self.keywords = keywords or []
        self.custom_fn = custom_fn

    def match(self, row: dict) -> bool:
        """判断一行数据是否符合过滤条件。返回 True 表示保留。"""
        if self.mode == "custom" and self.custom_fn:
            return self.custom_fn(row)

        value = row.get(self.column)
        if value is None:
            return False

        if self.mode == "regex":
            return bool(re.search(self.pattern, str(value)))

        elif self.mode == "range":
            try:
                num = float(value)
                if self.min_val is not None and num < self.min_val:
                    return False
                if self.max_val is not None and num > self.max_val:
                    return False
                return True
            except (ValueError, TypeError):
                return False

        elif self.mode == "keyword":
            val_str = str(value)
            return any(kw in val_str for kw in self.keywords)

        return True


def apply_filters(data: list[dict], rules: list[FilterRule]) -> list[dict]:
    """
    对数据列表应用过滤规则。所有规则是 AND 关系（全部满足才保留）。
    """
    if not rules:
        return data

    filtered = []
    for row in data:
        if all(rule.match(row) for rule in rules):
            filtered.append(row)

    before = len(data)
    after = len(filtered)
    if before != after:
        logger.info(f"Filter applied: {before} rows -> {after} rows ({before - after} filtered out)")
    return filtered


# ========== 核心保存函数（通用） ==========

def _save_dataframe_to_excel(
    df_new: pd.DataFrame,
    filepath: Path,
    unique_key: str | list[str] = None,
) -> tuple[str, int]:
    """
    内部通用函数：将 DataFrame 追加写入 Excel 文件，支持去重。

    Returns:
        (文件绝对路径, 总行数)
    """
    df_new = _normalize_extracted_dataframe(df_new)

    # 添加提取时间戳
    df_new["_extracted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 追加或新建
    if filepath.exists():
        try:
            df_existing = pd.read_excel(filepath, engine="openpyxl")
            df_existing = _normalize_extracted_dataframe(df_existing)
            df_new = _align_new_columns_to_existing(df_new, df_existing)
            df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        except Exception as e:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            fallback = filepath.with_name(f"{filepath.stem}_part_{timestamp}{filepath.suffix}")
            logger.warning(
                "Failed to read existing Excel; preserving it and writing "
                "current batch to a part file instead of overwriting: %s -> %s",
                e,
                fallback,
            )
            filepath = fallback
            df_combined = df_new
    else:
        df_combined = df_new

    df_combined = _fill_sequential_rank_if_safe(df_combined)

    # 去重
    if unique_key == TOOLTIP_UNIQUE_KEY:
        df_combined, removed = _apply_tooltip_upsert_key(df_combined)
        if removed > 0:
            logger.info(
                f"[TOOLTIP UPSERT] Dedup by dynamic trigger key: removed {removed} stale rows"
            )
    elif unique_key:
        # 显式 unique_key：精确按指定列去重
        subset = [unique_key] if isinstance(unique_key, str) else unique_key
        valid_cols = [c for c in subset if c in df_combined.columns]
        if valid_cols:
            before_dedup = len(df_combined)
            df_combined = df_combined.drop_duplicates(
                subset=valid_cols, keep="last"
            ).reset_index(drop=True)
            removed = before_dedup - len(df_combined)
            if removed > 0:
                logger.info(f"Dedup by {valid_cols}: removed {removed} duplicates")
    else:
        # 自动 hash 去重：保护快速翻页场景下的"AJAX 未完成 + 上一页 DOM 仍存"导致的重复落盘
        # 适用 DataTables / 动态表格等 URL 不变的翻页场景（fast-pagination race）
        # 哈希基于所有数据列（剥离 _extracted_at 等系统列），保证内容相同的行被识别为重复
        _data_cols = [c for c in df_combined.columns if c not in _SYSTEM_COLS]
        if _data_cols and len(df_combined) >= 2:
            import hashlib
            def _row_fingerprint(row):
                # 按列名稳定排序后串接成字符串，再 sha1 截 16 字符
                _vals = [f"{c}={row[c]!s}" for c in sorted(_data_cols)]
                return hashlib.sha1("\x1f".join(_vals).encode("utf-8")).hexdigest()[:16]
            try:
                _hashes = df_combined.apply(_row_fingerprint, axis=1)
                before_dedup = len(df_combined)
                _keep_mask = ~_hashes.duplicated(keep="last")
                df_combined = df_combined[_keep_mask].reset_index(drop=True)
                removed = before_dedup - len(df_combined)
                if removed > 0:
                    logger.info(
                        f"[AUTO HASH DEDUP] 自动去重移除 {removed} 行重复数据"
                        f"（疑似快翻页 AJAX race），保留最新副本"
                    )
            except Exception as _hash_err:
                logger.warning(f"[AUTO HASH DEDUP] 失败忽略：{_hash_err}")

    # 写入
    df_combined.to_excel(filepath, index=False, engine="openpyxl")
    return str(filepath.resolve()), len(df_combined)


# ========== VLM extract 数据保存 ==========

def save_to_excel(
    data,
    filename: str = "output.xlsx",
    filters: list[FilterRule] = None,
    unique_key: str | list[str] = None,
) -> str:
    """
    将 VLM extract 动作提取的数据保存至 Excel。

    Args:
        data: dict / list[dict] / list
        filename: 输出文件名
        filters: 可选过滤规则
        unique_key: 去重字段
    """
    filepath = resolve_output_path(filename)

    # 统一转换为 list[dict]
    if isinstance(data, dict):
        data_list = [data]
    elif isinstance(data, list):
        if len(data) == 0:
            logger.warning("Extracted data is empty, skipping save.")
            return str(filepath.resolve())
        if isinstance(data[0], dict):
            data_list = data
        else:
            data_list = [{"value": v} for v in data]
    else:
        data_list = [{"raw_data": str(data)}]

    # 过滤
    if filters:
        data_list = apply_filters(data_list, filters)
        if not data_list:
            logger.warning("All data filtered out by rules, nothing to save.")
            return str(filepath.resolve())

    df_new = pd.DataFrame(data_list)
    abs_path, total = _save_dataframe_to_excel(df_new, filepath, unique_key)
    logger.info(f"[VLM Extract] Saved to: {abs_path} (total {total} rows)")
    register_artifact(
        abs_path,
        kind="dataset_rows",
        mime=_XLSX_MIME,
        produced_by="vlm_extract",
    )
    return abs_path


# ========== XHR/Fetch 网络拦截数据保存 ==========

def save_intercepted_data(
    json_list: list[dict],
    filename: str = "output.xlsx",
    unique_key: str | list[str] = None,
) -> str:
    """
    将网络层拦截到的 JSON 数据保存至 Excel。

    专为 XHR/Fetch 拦截场景设计：
    - 接收 list[dict] 格式的纯 JSON 数据
    - 自动追加到已有文件
    - 支持按 unique_key 去重（防止翻页重复拦截）
    - 打印醒目的绿色日志

    Args:
        json_list: 拦截到的字典列表（API 响应中的数据行）
        filename: 输出文件名（默认 output.xlsx）
        unique_key: 去重字段（如 "id"、"order_no" 等）

    Returns:
        保存的文件绝对路径
    """
    filepath = resolve_output_path(filename)

    if not json_list or len(json_list) == 0:
        logger.warning("[XHR Intercept] Empty data list, skipping save.")
        return str(filepath.resolve())

    json_list = _normalize_intercept_rows(json_list)
    if not json_list:
        logger.warning("[XHR Intercept] All intercepted rows filtered out after normalization.")
        return str(filepath.resolve())

    df_new = pd.DataFrame(json_list)
    new_count = len(df_new)
    if filepath.exists() and not df_new.empty:
        try:
            existing_columns = pd.read_excel(filepath, engine="openpyxl", nrows=0)
            if _is_article_dataframe(existing_columns) and not _is_article_dataframe(df_new):
                logger.info(
                    "[XHR Intercept] Skipping non-article payload because %s already has article schema.",
                    filepath.name,
                )
                return str(filepath.resolve())
        except Exception:
            pass

    abs_path, total = _save_dataframe_to_excel(df_new, filepath, unique_key)

    # 醒目的绿色成功日志
    print(
        f"\n{_GREEN_BOLD}[XHR INTERCEPT]{_RESET} "
        f"{_CYAN}Successfully intercepted and saved {new_count} records. "
        f"Total: {total} rows in {filepath.name}{_RESET}\n"
    )
    logger.info(
        f"[XHR Intercept] Saved {new_count} new records -> {abs_path} "
        f"(total {total} rows)"
    )
    register_artifact(
        abs_path,
        kind="dataset_rows",
        mime=_XLSX_MIME,
        produced_by="xhr_intercept",
    )
    return abs_path
